"""Kilo expired-login regression coverage (mahler#1775)."""

import copy
import json
import os
import tempfile
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from mahler import cli, config, finalize, holds, platforms, router, runner, scheduler, usage
from mahler.ledger import Ledger, iso

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
ERROR = "Error: Your authentication token is invalid. Please sign in again."
LINE = json.dumps({"type": "error", "error": {"name": "APIError", "data": {"message": ERROR}}}) + "\n"


class LogTests(unittest.TestCase):
    def read(self, text):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "agent.log")
            Path(path).write_text(text)
            return platforms.read_log(path, "kilo")

    def test_reauth_error_is_an_auth_failure(self):
        res = self.read(LINE)
        self.assertTrue(res["auth_denied"])
        self.assertFalse(res["quota_hit"])
        self.assertIn("authentication token", res["last_error"])

    def test_exact_plain_error(self):
        self.assertTrue(platforms.is_auth_denied(ERROR))
        self.assertTrue(self.read(ERROR)["auth_denied"])
        for phrase in ("your authentication token is invalid", "please sign in again"):
            self.assertTrue(platforms.is_auth_denied(phrase))

    def test_prose_quoting_it_is_not(self):
        res = self.read(json.dumps({"type": "say", "text": ERROR}) + "\n")
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
                                     platform="kilo", epoch=1, status="running")
        self.led.claim("x", number, f"run:{run_id}", "auto",
                       self.cfg["defaults"]["auto_lease_minutes"],
                       platform="kilo", run_id=run_id)
        d = os.path.join(self.tmp, str(run_id))
        os.makedirs(d)
        run = {"id": run_id, "project": "x", "number": number, "role": "build",
               "platform": "kilo", "epoch": 1, "pid": None,
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
        self.assertEqual(self.led.get_kv("hold_reason:kilo"), "auth")
        until = router._ts(self.led.usage("kilo")[router.HOLD]["resets_at"])
        self.assertGreater(until, NOW + timedelta(days=30))
        self.assertEqual(ping.call_count, 1)
        self.assertIn("kilo auth", ping.call_args.args[1])
        self.assertEqual(ping.call_args.kwargs.get("priority"), "high")
        # a second dead run while held does not ping again
        _, ping = self.run_once(number=6)
        self.assertEqual(ping.call_count, 0)

    def test_usage_shows_blocked_naming_the_fix_and_router_skips_it(self):
        self.run_once()
        pconf = self.cfg["platforms"]["kilo"]
        state, detail = router.usage_state(self.led, "kilo", pconf)
        self.assertEqual(state, "blocked")
        self.assertIn("kilo auth", detail)
        self.assertEqual(router.diagnose(self.cfg, self.led, config.project_policy(self.cfg, "x"),
                                         "build")["kilo"][-1], "hold")

    def test_unhold_clears_it(self):
        self.run_once()
        holds.clear(self.cfg, self.led, "kilo", by="test")
        state, _ = router.usage_state(self.led, "kilo", self.cfg["platforms"]["kilo"])
        self.assertNotEqual(state, "blocked")
        self.assertIsNone(self.led.get_kv("hold_reason:kilo"))


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)
        finalize._hold_auth_platform(
            mock.Mock(led=self.led, ping=mock.Mock()),
            {"platform": "kilo", "project": "x", "number": 1, "id": 1}, {"kind": "kilo"})

    def test_probe_classification(self):
        def fake(out, code):
            return mock.Mock(stdout=out, stderr="", returncode=code)
        with mock.patch.object(platforms, "kilo_exe", return_value="/bin/kilo"):
            with mock.patch("subprocess.run", return_value=fake(LINE, 1)):
                self.assertIs(platforms.probe_kilo(), False)
            with mock.patch("subprocess.run", return_value=fake('{"type":"run_result"}', 0)):
                self.assertIs(platforms.probe_kilo(), True)
            with mock.patch("subprocess.run", return_value=fake("boom", 1)):
                self.assertIsNone(platforms.probe_kilo())

    def test_denied_credentials_cannot_pass_even_with_zero_exit(self):
        with mock.patch.object(platforms, "kilo_exe", return_value="/bin/kilo"), \
                mock.patch("subprocess.run", return_value=mock.Mock(
                    stdout="", stderr=ERROR, returncode=0)):
            self.assertIs(platforms.probe_kilo(), False)

    def test_probe_is_bounded_and_uses_supplied_environment(self):
        env = {"HOME": "/isolated"}
        with mock.patch.object(platforms, "kilo_exe", return_value="/bin/kilo"), \
                mock.patch("subprocess.run", return_value=mock.Mock(
                    stdout="OK", stderr="", returncode=0)) as run:
            self.assertIs(platforms.probe_kilo({"model": "free-model"}, env=env), True)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["/bin/kilo", "run", "Reply with the single word OK."])
        self.assertIn("--auto", argv)
        self.assertIn("json", argv)
        self.assertIn("free-model", argv)
        self.assertFalse(Path(argv[argv.index("--dir") + 1]).exists())
        self.assertIs(run.call_args.kwargs["env"], env)
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertLessEqual(run.call_args.kwargs["timeout"], 90)

    def test_probe_unavailable_or_timeout_is_unknown(self):
        with mock.patch.object(platforms, "kilo_exe", return_value=None):
            self.assertIsNone(platforms.probe_kilo())
        for error in (OSError("unavailable"), subprocess.TimeoutExpired("kilo", 60)):
            with self.subTest(error=error), \
                    mock.patch.object(platforms, "kilo_exe", return_value="/bin/kilo"), \
                    mock.patch("subprocess.run", side_effect=error):
                self.assertIsNone(platforms.probe_kilo())

    def test_working_probe_clears_the_hold_but_failing_probe_keeps_it(self):
        with mock.patch.object(platforms, "probe_kilo", return_value=False):
            usage.refresh_cline_holds(self.cfg, self.led)
        self.assertEqual(self.led.get_kv("hold_reason:kilo"), "auth")
        with mock.patch.object(platforms, "probe_kilo", return_value=None):
            usage.refresh_cline_holds(self.cfg, self.led, force=True)
        self.assertEqual(self.led.get_kv("hold_reason:kilo"), "auth")
        with mock.patch.object(platforms, "probe_kilo", return_value=True):
            usage.refresh_cline_holds(self.cfg, self.led)      # throttled: just probed
        self.assertEqual(self.led.get_kv("hold_reason:kilo"), "auth")
        with mock.patch.object(platforms, "probe_kilo", return_value=True):
            usage.refresh_cline_holds(self.cfg, self.led, force=True)
        self.assertIsNone(self.led.get_kv("hold_reason:kilo"))
        self.assertNotIn(router.HOLD, self.led.usage("kilo"))


if __name__ == "__main__":
    unittest.main()
