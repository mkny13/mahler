"""Watchdog: stuck starts and runs that outlive their shell (mahler#12)."""

import copy
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


class WallClockTests(unittest.TestCase):
    """The 60-minute wall clock stops a run however chatty it is (mahler#91)."""

    def setUp(self):
        self.ctx = ctx_for()
        self.pol = config.project_policy(config.DEFAULTS, "x")
        self.log = tempfile.NamedTemporaryFile(delete=False)
        self.log.close()
        self.addCleanup(os.unlink, self.log.name)

    def health(self, minutes_ago, log_age=None):
        with open(self.log.name, "wb") as fh:
            fh.write(b'{"type":"agent_event"}\n')    # chatty to the very end
        t = (NOW - timedelta(minutes=log_age if log_age is not None else minutes_ago)).timestamp()
        os.utime(self.log.name, (t, t))
        run = {"started_at": iso(NOW - timedelta(minutes=minutes_ago)),
               "log_path": self.log.name, "platform": "cline-free", "number": 1}
        return scheduler._health(self.ctx, run, self.pol, NOW)

    def test_a_chatty_run_inside_the_limit_is_fine(self):
        self.assertIsNone(self.health(55, log_age=0))

    def test_past_the_wall_clock_is_timeout_even_with_fresh_output(self):
        self.assertEqual(self.health(61, log_age=0), "timeout")

    def test_the_wall_clock_beats_the_progress_check(self):
        # stale output past both limits reads as timeout, not hung
        self.assertEqual(self.health(61), "timeout")


class SetupPhaseTests(unittest.TestCase):
    """While a build's setup step runs, agent.log doesn't exist yet; setup.log
    is then the progress signal — a slow dependency install is not a hung run
    (mahler#91). A never-started run with no log at all is the silent case."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.agent_log = os.path.join(self.dir, "agent.log")   # never created
        self.setup_log = os.path.join(self.dir, "setup.log")
        self.ctx = ctx_for()
        self.pol = config.project_policy(config.DEFAULTS, "x")

    def health(self, setup_age=None, started_ago=5):
        if setup_age is not None:
            with open(self.setup_log, "wb") as fh:
                fh.write(b"installing dependencies\n")
            t = (NOW - timedelta(minutes=setup_age)).timestamp()
            os.utime(self.setup_log, (t, t))
        run = {"started_at": iso(NOW - timedelta(minutes=started_ago)),
               "log_path": self.agent_log, "platform": "cline-free", "number": 1}
        return scheduler._health(self.ctx, run, self.pol, NOW)

    def test_an_active_setup_is_not_a_hung_run(self):
        self.assertIsNone(self.health(setup_age=0, started_ago=25))

    def test_a_stale_setup_is_hung(self):
        self.assertEqual(self.health(setup_age=25, started_ago=25), "hung")

    def test_no_log_at_all_is_silent_after_the_startup_limit(self):
        self.assertEqual(self.health(started_ago=11), "silent")

    def test_no_log_shortly_after_start_is_fine(self):
        self.assertIsNone(self.health(started_ago=5))


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


class HumanClaudeTests(unittest.TestCase):
    cfg = config.DEFAULTS

    def _led_ctx(self):
        led = Ledger(":memory:", clock=lambda: NOW)
        return scheduler.Ctx(self.cfg, led, dry_run=True), led

    def _preseed(self, led, pct):
        reset = iso(NOW + timedelta(minutes=30))
        for name in ("claude", "claude-opus"):
            led.record_usage(name, "5h", pct, reset)
            led.record_usage(name, "weekly", pct, reset)

    def test_5h_usage_rise_with_no_live_run_sets_flag(self):
        """A 5h usage increase spotted by the probe, with no Claude run live,
        is treated as human use of the account elsewhere (D23 suppression)."""
        ctx, led = self._led_ctx()
        self._preseed(led, 30.0)
        reset = iso(NOW + timedelta(minutes=30))
        scheduler._record_claude_usage(ctx, [("5h", 40.0, None), ("weekly", 50.0, reset)],
                                       check_human=True)
        self.assertIsNotNone(led.get_kv("human:claude"))

    def test_5h_usage_rise_with_live_claude_run_does_not_set_flag(self):
        """The rise is from Mahler's own run, not a human."""
        ctx, led = self._led_ctx()
        self._preseed(led, 30.0)
        led.create_run(project="x", number=1, role="build", platform="claude", epoch=1)
        reset = iso(NOW + timedelta(minutes=30))
        scheduler._record_claude_usage(ctx, [("5h", 40.0, None), ("weekly", 50.0, reset)],
                                       check_human=True)
        self.assertIsNone(led.get_kv("human:claude"))

    def test_5h_usage_rise_with_live_other_run_does_not_set_flag(self):
        """A free-tier run doesn't explain a 5h rise on the Claude account."""
        ctx, led = self._led_ctx()
        self._preseed(led, 30.0)
        led.create_run(project="x", number=1, role="build", platform="agy-claude", epoch=1)
        reset = iso(NOW + timedelta(minutes=30))
        scheduler._record_claude_usage(ctx, [("5h", 40.0, None), ("weekly", 50.0, reset)],
                                       check_human=True)
        self.assertIsNotNone(led.get_kv("human:claude"))

    def test_no_5h_rise_does_not_set_flag(self):
        ctx, led = self._led_ctx()
        self._preseed(led, 40.0)
        reset = iso(NOW + timedelta(minutes=30))
        scheduler._record_claude_usage(ctx, [("5h", 40.0, None), ("weekly", 50.0, reset)],
                                       check_human=True)
        self.assertIsNone(led.get_kv("human:claude"))

    def test_check_human_false_does_not_set_flag(self):
        """The run's own log should not trigger human detection."""
        ctx, led = self._led_ctx()
        self._preseed(led, 30.0)
        reset = iso(NOW + timedelta(minutes=30))
        scheduler._record_claude_usage(ctx, [("5h", 40.0, None), ("weekly", 50.0, reset)],
                                       check_human=False)
        self.assertIsNone(led.get_kv("human:claude"))


class HumanClaudePresenceTests(unittest.TestCase):
    """D23: presence.human_claude_active walks managed project paths for
    recent Claude Code transcripts."""

    def test_no_path_no_activity(self):
        from mahler import presence
        self.assertFalse(presence.human_claude_active([{"path": "/nonexistent/xyz"}]))

    def test_no_projects_no_activity(self):
        from mahler import presence
        self.assertFalse(presence.human_claude_active([]))

    def test_recent_transcript_is_human_activity(self):
        from datetime import timezone
        from mahler import presence
        now = datetime.now(timezone.utc)
        with mock.patch.object(presence, "last_claude_activity", return_value=now):
            self.assertTrue(presence.human_claude_active([{"path": "/some/path"}]))

    def test_old_transcript_is_not_human_activity(self):
        from mahler import presence
        old = datetime.now(timezone.utc) - timedelta(minutes=25)
        with mock.patch.object(presence, "last_claude_activity", return_value=old):
            self.assertFalse(presence.human_claude_active([{"path": "/some/path"}]))


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


class EscalationTests(unittest.TestCase):
    """A live run past a limit: stopped at once (SIGTERM), SIGKILLed a tick
    later if it ignored SIGTERM, finalized only once the process is gone."""

    def setUp(self):
        self.ctx = ctx_for()
        self.led = self.ctx.led
        for n, holder in ((1, "run:9"), (2, "run:8")):
            self.led.upsert_item("x", n, state="working", sorted_at=iso(NOW))
            self.led.claim("x", n, holder, "auto", 10)
        self.led.create_run(id=9, project="x", number=1, role="build",
                            platform="cline-free", epoch=self.led.lease("x", 1)["epoch"],
                            pid=4242, worktree="/tmp/wt9", branch="b9", base_ref="main",
                            log_path="/nonexistent/9/agent.log",
                            status_path="/nonexistent/9/exit",
                            started_at=iso(NOW - timedelta(minutes=61)))
        # a poisoned row: its platform was dropped from the config
        self.led.create_run(id=8, project="x", number=2, role="build",
                            platform="ghost", epoch=self.led.lease("x", 2)["epoch"],
                            pid=1111, worktree="/tmp/wt8", branch="b8", base_ref="main",
                            log_path="/nonexistent/8/agent.log",
                            status_path="/nonexistent/8/exit",
                            started_at=iso(NOW - timedelta(minutes=1)))

    def test_a_live_run_past_the_wall_clock_is_stopped_at_once(self):
        with mock.patch.object(runner, "alive", return_value=True), \
                mock.patch.object(runner, "terminate") as term:
            scheduler.watchdog(self.ctx)
        term.assert_called_once_with(4242)
        row = self.led.run(9)
        self.assertEqual((row["status"], row["stop_reason"]), ("stopping", "timeout"))

    def test_a_stopping_run_that_ignored_sigterm_is_killed_the_next_tick(self):
        self.led.update_run(9, status="stopping")
        with mock.patch.object(runner, "alive", return_value=True), \
                mock.patch.object(runner, "kill") as kill, \
                mock.patch.object(runner, "terminate") as term, \
                mock.patch.object(scheduler, "finalize") as fin:
            scheduler.watchdog(self.ctx)
        kill.assert_called_once_with(4242)
        term.assert_not_called()
        fin.assert_not_called()

    def test_one_poisoned_run_does_not_disarm_the_watchdog(self):
        real_health = scheduler._health

        def flaky(ctx, run, pol, now):
            if run["platform"] == "ghost":
                raise RuntimeError("poisoned run row")
            return real_health(ctx, run, pol, now)

        with mock.patch.object(runner, "alive", return_value=True), \
                mock.patch.object(scheduler, "_health", side_effect=flaky), \
                mock.patch.object(runner, "terminate") as term:
            scheduler.watchdog(self.ctx)
        term.assert_called_once_with(4242)   # the healthy run was still stopped
        self.assertTrue(any("run 8" in line for line in self.ctx.lines))


class FakeGH:
    def __init__(self, state="OPEN"):
        self.state, self.comments = state, []

    def issue_state(self, number):
        return self.state

    def comment(self, number, body):
        self.comments.append(body)


class TimeoutHandoffTests(unittest.TestCase):
    """A run stopped past a duration limit is snapshotted and handed off with
    an automatic comment on the issue (mahler#91)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["projects"]["x"] = {"path": self.tmp, "repo": "x/y"}
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)
        self.led.upsert_item("x", 5, state="working", priority=2, sorted_at=iso(NOW))
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
        self.gh = FakeGH()
        self.run_id = self.led.create_run(project="x", number=5, role="build",
                                          platform="agy-claude", epoch=1, status="running")
        self.led.claim("x", 5, f"run:{self.run_id}", "auto",
                       self.cfg["defaults"]["auto_lease_minutes"],
                       platform="agy-claude", run_id=self.run_id)
        self.log = os.path.join(self.tmp, "agent.log")
        with open(self.log, "w") as fh:
            fh.write(json.dumps({"event": "result", "result": {"status": "SUCCESS",
                                 "response": "mid-task output, no STATUS line"}}) + "\n")
        wt = os.path.join(self.tmp, "wt")
        os.makedirs(wt, exist_ok=True)
        self.run = {"id": self.run_id, "project": "x", "number": 5, "role": "build",
                    "platform": "agy-claude", "epoch": 1, "pid": None, "nudged": 0,
                    "worktree": wt, "branch": "mahler/5-x",
                    "log_path": self.log, "status_path": os.path.join(self.tmp, "exit"),
                    "started_at": iso(NOW), "stop_reason": None}

    def finalize(self, reason):
        self.run["stop_reason"] = reason
        saved = {"ref": "mahler/snapshot/5-run7", "sha": "abc123", "ahead": 2, "stat": None}
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(runner, "snapshot", return_value=saved) as snap, \
                mock.patch.object(runner, "remove_worktree"), \
                mock.patch.object(self.ctx, "ping"):
            scheduler.finalize(self.ctx, self.run)
        return snap

    def test_a_timed_out_run_is_snapshotted_and_handed_off(self):
        snap = self.finalize("timeout")
        snap.assert_called_once()
        self.assertEqual(len(self.gh.comments), 1)
        comment = self.gh.comments[0]
        self.assertTrue(comment.startswith("<!-- mahler:agent handoff"))
        self.assertIn("reason=timeout", comment)
        self.assertIn("hit the time limit", comment)
        self.assertIn("mahler/snapshot/5-run7", comment)

    def test_a_hung_run_is_snapshotted_and_handed_off(self):
        self.finalize("hung")
        self.assertEqual(len(self.gh.comments), 1)
        comment = self.gh.comments[0]
        self.assertIn("reason=hung", comment)
        self.assertIn("no progress for too long", comment)

    def test_a_timed_out_run_counts_as_a_failed_attempt(self):
        self.finalize("timeout")
        item = self.led.item("x", 5)
        self.assertEqual(item["attempts"], 1)
        self.assertEqual(item["state"], "ready")     # back in the queue for a retry


if __name__ == "__main__":
    unittest.main()