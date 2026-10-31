"""Cline's expired login holds the platform until fixed (mahler#1485).

Runs ended with {"type":"error","message":"cline requires re-authentication."}
for five days because only Copilot's denial was recognised. Now the platform
gets a lasting hold with an auth reason, the owner is pinged once, and the
hold lifts only by a working probe or `mahler unhold`.
"""

import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from mahler import cli, config, finalize, holds, platforms, router, runner, scheduler, usage
from mahler.ledger import Ledger, iso

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
LINE = json.dumps({"type": "error", "message": "cline requires re-authentication."}) + "\n"


class LogTests(unittest.TestCase):
    def read(self, text):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "agent.log")
            Path(path).write_text(text)
            return platforms.read_log(path, "cline")

    def test_reauth_error_is_an_auth_failure(self):
        res = self.read(LINE)
        self.assertTrue(res["auth_denied"])
        self.assertFalse(res["quota_hit"])
        self.assertIn("re-authentication", res["last_error"])

    def test_prose_quoting_it_is_not(self):
        res = self.read(json.dumps({"type": "say", "text": "cline requires re-authentication"}) + "\n")
        self.assertFalse(res["auth_denied"])


class FinalizeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["projects"]["x"] = {"path": self.tmp, "repo": "x/y"}
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)

    def run_once(self, number=5):
        self.led.upsert_item("x", number, state="working", priority=2, sorted_at=iso(NOW))
        run_id = self.led.create_run(project="x", number=number, role="build",
                                     platform="cline-free", epoch=1, status="running")
        self.led.claim("x", number, f"run:{run_id}", "auto",
                       self.cfg["defaults"]["auto_lease_minutes"],
                       platform="cline-free", run_id=run_id)
        d = os.path.join(self.tmp, str(run_id))
        os.makedirs(d)
        run = {"id": run_id, "project": "x", "number": number, "role": "build",
               "platform": "cline-free", "epoch": 1, "pid": None,
               "worktree": os.path.join(d, "wt"), "branch": f"mahler/{number}-x",
               "log_path": os.path.join(d, "agent.log"),
               "status_path": os.path.join(d, "exit"),
               "started_at": iso(NOW), "stop_reason": None}
        Path(run["log_path"]).write_text(LINE)
        Path(run["status_path"]).write_text("1")
        with mock.patch.object(self.ctx, "gh"), \
                mock.patch.object(runner, "snapshot", return_value=None), \
                mock.patch.object(runner, "remove_worktree"), \
                mock.patch.object(self.ctx, "ping") as ping:
            finalize.finalize(self.ctx, run)
        return run_id, ping

    def test_reauth_holds_platform_lastingly_pings_once_and_costs_no_attempt(self):
        run_id, ping = self.run_once()
        self.assertEqual(self.led.run(run_id)["stop_reason"], "auth_denied")
        self.assertEqual(self.led.item("x", 5)["attempts"], 0)
        self.assertEqual(self.led.get_kv("hold_reason:cline-free"), "auth")
        until = router._ts(self.led.usage("cline-free")[router.HOLD]["resets_at"])
        self.assertGreater(until, NOW + timedelta(days=30))
        self.assertEqual(ping.call_count, 1)
        self.assertIn("cline auth", ping.call_args.args[1])
        self.assertEqual(ping.call_args.kwargs.get("priority"), "high")
        # a second dead run while held does not ping again
        _, ping = self.run_once(number=6)
        self.assertEqual(ping.call_count, 0)

    def test_usage_shows_blocked_naming_the_fix_and_router_skips_it(self):
        self.run_once()
        pconf = self.cfg["platforms"]["cline-free"]
        state, detail = router.usage_state(self.led, "cline-free", pconf)
        self.assertEqual(state, "blocked")
        self.assertIn("cline auth", detail)
        self.assertEqual(router.diagnose(self.cfg, self.led, config.project_policy(self.cfg, "x"),
                                         "build")["cline-free"][-1], "hold")

    def test_unhold_clears_it(self):
        self.run_once()
        holds.clear(self.cfg, self.led, "cline-free", by="test")
        state, _ = router.usage_state(self.led, "cline-free", self.cfg["platforms"]["cline-free"])
        self.assertNotEqual(state, "blocked")
        self.assertIsNone(self.led.get_kv("hold_reason:cline-free"))


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)
        finalize._hold_auth_platform(
            mock.Mock(led=self.led, ping=mock.Mock()),
            {"platform": "cline-free", "project": "x", "number": 1, "id": 1}, {})

    def test_probe_classification(self):
        def fake(out, code):
            return mock.Mock(stdout=out, stderr="", returncode=code)
        with mock.patch.object(platforms, "cline_exe", return_value="/bin/cline"):
            with mock.patch("subprocess.run", return_value=fake(LINE, 1)):
                self.assertIs(platforms.probe_cline(), False)
            with mock.patch("subprocess.run", return_value=fake('{"type":"run_result"}', 0)):
                self.assertIs(platforms.probe_cline(), True)
            with mock.patch("subprocess.run", return_value=fake("boom", 1)):
                self.assertIsNone(platforms.probe_cline())

    def test_working_probe_clears_the_hold_but_failing_probe_keeps_it(self):
        with mock.patch.object(platforms, "probe_cline", return_value=False):
            usage.refresh_cline_holds(self.cfg, self.led)
        self.assertEqual(self.led.get_kv("hold_reason:cline-free"), "auth")
        with mock.patch.object(platforms, "probe_cline", return_value=None):
            usage.refresh_cline_holds(self.cfg, self.led, force=True)
        self.assertEqual(self.led.get_kv("hold_reason:cline-free"), "auth")
        with mock.patch.object(platforms, "probe_cline", return_value=True):
            usage.refresh_cline_holds(self.cfg, self.led)      # throttled: just probed
        self.assertEqual(self.led.get_kv("hold_reason:cline-free"), "auth")
        with mock.patch.object(platforms, "probe_cline", return_value=True):
            usage.refresh_cline_holds(self.cfg, self.led, force=True)
        self.assertIsNone(self.led.get_kv("hold_reason:cline-free"))
        self.assertNotIn(router.HOLD, self.led.usage("cline-free"))


if __name__ == "__main__":
    unittest.main()
