"""Watchdog: stuck starts and runs that outlive their shell (mahler#12)."""

import json
import os
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler import config, router, runner, scheduler
from mahler.ledger import Ledger, iso

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


def read(path):
    with open(path) as fh:
        return fh.read().strip()


def ctx_for():
    led = Ledger(":memory:", clock=lambda: NOW)
    return scheduler.Ctx(config.DEFAULTS, led, dry_run=False)


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.ctx = ctx_for()
        self.pol = config.project_policy(config.DEFAULTS, "x")
        self.log = tempfile.NamedTemporaryFile(delete=False)
        self.log.close()
        self.addCleanup(os.unlink, self.log.name)

    def health(self, minutes_ago, content=b""):
        with open(self.log.name, "wb") as fh:
            fh.write(content)
        t = (NOW - timedelta(minutes=minutes_ago)).timestamp()
        os.utime(self.log.name, (t, t))
        run = {"started_at": iso(NOW - timedelta(minutes=minutes_ago)),
               "log_path": self.log.name, "platform": "cline-free", "number": 1}
        return scheduler._health(self.ctx, run, self.pol, NOW)

    def test_empty_log_past_the_startup_limit_is_silent(self):
        self.assertEqual(self.health(11), "silent")

    def test_empty_log_inside_the_startup_limit_is_fine(self):
        self.assertIsNone(self.health(5))

    def test_output_means_the_startup_check_is_over(self):
        self.assertIsNone(self.health(11, b'{"type":"agent_event"}\n'))
        self.assertEqual(self.health(21, b'{"type":"agent_event"}\n'), "hung")

    def test_silent_is_not_the_items_fault(self):
        self.assertIn("silent", scheduler.NO_ATTEMPT)

    def test_overage_stops_the_run_and_holds_all_claude_platforms(self):
        # mahler#136: isUsingOverage true means paid extra usage — stop at once,
        # and because usage is mirrored across claude platforms, both claude and
        # claude-opus (kind: claude) must read hard afterwards.
        ev = json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed_warning", "rateLimitType": "seven_day", "utilization": 0.75,
            "isUsingOverage": True, "resetsAt": 1789462800, "unifiedWindows": {
                "five_hour": {"utilization": 0.25, "resetsAt": 1789257600},
                "seven_day": {"utilization": 0.75, "resetsAt": 1789462800}}}}).encode() + b"\n"
        with open(self.log.name, "wb") as fh:
            fh.write(ev)
        run = {"id": 42, "started_at": iso(NOW - timedelta(minutes=1)),
               "log_path": self.log.name, "platform": "claude", "project": "x", "number": 1}
        with mock.patch.object(self.ctx, "ping") as ping:
            reason = scheduler._health(self.ctx, run, self.pol, NOW)
        self.assertEqual(reason, "quota")
        ping.assert_called_once()
        for pname in ("claude", "claude-opus"):
            state, _ = router.usage_state(self.ctx.led, pname, config.DEFAULTS["platforms"][pname])
            self.assertEqual(state, "hard")
        # a second watchdog pass for the same run must not ping again
        with mock.patch.object(self.ctx, "ping") as ping2:
            scheduler._health(self.ctx, run, self.pol, NOW)
        ping2.assert_not_called()


class HoldTests(unittest.TestCase):
    cfg = config.DEFAULTS

    def test_hold_stops_new_runs_until_it_lifts(self):
        ctx = ctx_for()
        run = {"id": 7, "platform": "cline-free", "project": "x", "number": 1}
        with mock.patch.object(ctx, "ping") as ping:
            scheduler._hold_platform(ctx, run)
        ping.assert_called_once()
        state, detail = router.usage_state(ctx.led, "cline-free", self.cfg["platforms"]["cline-free"])
        self.assertEqual(state, "soft")          # stop starting; don't kill live runs
        self.assertIn("on hold", detail)
        name, _ = router.pick(self.cfg, ctx.led, "build", size="s")
        self.assertNotEqual(name, "cline-free")

    def test_lapsed_hold_is_ignored(self):
        led = Ledger(":memory:", clock=lambda: NOW)
        led.record_usage("cline-free", router.HOLD, 100.0, iso(NOW - timedelta(minutes=1)))
        self.assertEqual(router.usage_state(led, "cline-free",
                                            self.cfg["platforms"]["cline-free"])[0], "ok")


class ReapTests(unittest.TestCase):
    def test_dead_shell_still_gets_its_group_killed_before_finalize(self):
        ctx = ctx_for()
        order = []
        run = {"pid": 4242, "project": "x"}
        with mock.patch.object(ctx.led, "active_runs", return_value=[run]), \
                mock.patch.object(scheduler.runner, "alive", return_value=False), \
                mock.patch.object(scheduler.runner, "kill", side_effect=lambda p: order.append(("kill", p))), \
                mock.patch.object(scheduler, "finalize", side_effect=lambda c, r: order.append(("finalize",))), \
                mock.patch.object(ctx, "policy", return_value={}):
            scheduler.watchdog(ctx)
        self.assertEqual(order, [("kill", 4242), ("finalize",)])

    def test_killing_the_group_reaches_a_child_that_outlived_its_shell(self):
        """What mahler#12 hit: SIGTERM kills the shell, a child shrugs it off,
        and only a group SIGKILL after the leader is gone gets it."""
        with tempfile.TemporaryDirectory() as d:
            pidfile = os.path.join(d, "child")
            proc = subprocess.Popen(
                ["/bin/sh", "-c", f"(trap '' TERM; exec sleep 60) & echo $! > {pidfile}; wait"],
                start_new_session=True)
            for _ in range(50):
                if os.path.exists(pidfile) and read(pidfile):
                    break
                time.sleep(0.05)
            child = int(read(pidfile))
            runner.terminate(proc.pid)
            proc.wait(timeout=5)
            time.sleep(0.2)
            self.assertTrue(runner.alive(child), "child should have ignored SIGTERM")
            runner.kill(proc.pid)
            for _ in range(50):
                try:
                    if os.waitpid(child, os.WNOHANG) != (0, 0):
                        break
                except ChildProcessError:       # not our child: poll instead
                    if not runner.alive(child):
                        break
                time.sleep(0.05)
            self.assertFalse(runner.alive(child))


if __name__ == "__main__":
    unittest.main()
