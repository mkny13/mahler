"""Codex quota protocol and per-account routing (#276); no real login or server."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler import config, platforms, router, scheduler, usage
from mahler.console import state
from mahler.ledger import Ledger


def response(blocked=False, personal=False):
    return {
        "ordinaryUsageAllowed": not blocked,
        "rateLimits": {
            "primary": {"usedPercent": 100 if blocked else 20,
                        "windowDurationMins": 43200 if personal else 300,
                        "resetsAt": 1900000000},
            "secondary": None if personal else {
                "usedPercent": 58, "windowDurationMins": 10080, "resetsAt": None}},
        "rateLimitResetCredits": {"availableCount": 3, "credits": [
            {"id": "secret-credit-id", "status": "available", "expiresAt": 1900000000},
            {"status": "consumed", "expiresAt": 1800000000}]}}


class CodexProbeTests(unittest.TestCase):
    def probe(self, body=None, mode="ok"):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "codex"
            exe.write_text("#!/usr/bin/env python3\n" + '''
import json, os, sys, time
assert sys.argv[1:] == ["app-server"]
assert os.environ["CODEX_HOME"] == "isolated"
request = json.loads(input())
assert request["method"] == "initialize"
print(json.dumps({"method": "notification"}), flush=True)
print(json.dumps({"id": 1, "result": {}}), flush=True)
assert json.loads(input())["method"] == "initialized"
assert json.loads(input())["method"] == "account/rateLimits/read"
mode = ''' + repr(mode) + '''
if mode == "timeout": time.sleep(5)
if mode == "bad": print("not json", flush=True)
elif mode == "error": print('{"id":2,"error":{}}', flush=True)
elif mode == "partial":
    sys.stdout.write('{"id":2')
    sys.stdout.flush()
    time.sleep(5)
elif mode == "eof": sys.exit(1)
else: print(json.dumps({"id": 2, "result": ''' + repr(body or response()) + '''}), flush=True)
time.sleep(5)
''')
            exe.chmod(0o755)
            with mock.patch.object(platforms, "codex_exe", return_value=str(exe)):
                return platforms.probe_codex(env={**os.environ, "CODEX_HOME": "isolated"},
                                             timeout=0.5)

    def test_windows_null_reset_and_sanitized_credits(self):
        result = self.probe()
        self.assertEqual([(w, p) for w, p, _ in result], [("5h", 20), ("weekly", 58)])
        self.assertIsNone(result[1][2])
        self.assertEqual(result.metadata["reset_credits"], 3)
        self.assertEqual(len(result.metadata["credit_expiries"]), 1)
        self.assertNotIn("secret-credit-id", json.dumps(result.metadata))

    def test_personal_nonstandard_window_is_not_a_weekly_sample(self):
        result = self.probe(response(blocked=True, personal=True))
        self.assertEqual(result, [])
        self.assertTrue(result.metadata["blocked"])
        self.assertEqual(result.metadata["windows"][0]["window"], "43200m")

    def test_failures_and_partial_lines_are_bounded(self):
        for mode in ("timeout", "partial", "bad", "error", "eof"):
            with self.subTest(mode=mode):
                self.assertEqual(self.probe(mode=mode), [])

    def test_malformed_shapes_fail_closed(self):
        for body in ([1], {"rateLimits": []}, {"rateLimits": {"primary": {}}}):
            with self.subTest(body=body):
                self.assertEqual(self.probe(body), [])


class CodexRefreshTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 16, tzinfo=timezone.utc)
        self.led = Ledger(":memory:", clock=lambda: self.now)
        self.addCleanup(self.led.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.personal = str(Path(self.tmp.name) / "personal")
        self.work = str(Path(self.tmp.name) / "work")
        for home in (self.personal, self.work):
            Path(home).mkdir()
            (Path(home) / "auth.json").write_text("{}")
        self.cfg = config.resolve_platforms(config._merge(copy.deepcopy(config.DEFAULTS), {
            "routing": {"sort": [], "build": ["codex", "codex-high"], "plan": []},
            "platforms": {"codex-work": {"from": "codex", "account": "work"}},
            "accounts": {
                "personal": {"env": {"CODEX_HOME": self.personal}},
                "work": {"env": {"CODEX_HOME": self.work},
                         "routing": {"build": ["codex-work"]}}},
            "projects": {"p": {"accounts": ["personal", "work"]}}}))
        self.led.upsert_item("p", 1, state="ready", priority=2)
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=True)
        self.projects = [config.project_policy(self.cfg, "p")]

    def refresh(self, result=None):
        with mock.patch.object(platforms, "probe_codex", return_value=(
                platforms._codex_usage(response()) if result is None else result)) as probe:
            usage.refresh_usage(self.ctx, self.projects)
        return probe

    def test_accounts_peers_throttle_and_retry(self):
        probe = self.refresh()
        self.assertEqual(probe.call_count, 2)
        self.assertEqual({c.kwargs["env"]["CODEX_HOME"] for c in probe.call_args_list},
                         {self.personal, self.work})
        for name in ("codex", "codex-high", "codex-work"):
            self.assertEqual(self.led.usage(name)["weekly"]["used_pct"], 58)
            self.assertEqual(router.usage_state(self.led, name, self.cfg["platforms"][name])[0], "ok")
        self.refresh().assert_not_called()
        self.now += timedelta(minutes=16)
        self.assertEqual(self.refresh([]).call_count, 2)
        self.refresh([]).assert_not_called()
        self.assertEqual(router.usage_state(self.led, "codex", self.cfg["platforms"]["codex"])[0], "stale")

    def test_missing_login_or_home_and_unmetered_are_skipped(self):
        (Path(self.work) / "auth.json").unlink()
        self.refresh().assert_called_once()
        self.cfg["accounts"]["work"]["env"] = {}
        self.refresh().assert_not_called()
        self.cfg["platforms"]["codex"]["metered"] = False
        with mock.patch.object(platforms, "probe_codex") as probe:
            usage.refresh_codex(self.cfg, self.led, "codex", force=True)
        probe.assert_not_called()

    def test_blocked_personal_and_reset_credits_visible_then_recover(self):
        self.refresh(platforms._codex_usage(response(blocked=True, personal=True)))
        pc = self.cfg["platforms"]["codex"]
        status, detail = router.usage_state(self.led, "codex", pc)
        self.assertEqual(status, "hard")
        self.assertIn("43200m 100%", detail)
        self.assertIn("3 reset credits available", detail)
        self.assertIn("next expires", detail)
        rows = state._quota(self.cfg, self.led, None)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertIn("3 reset credits available", row["detail"])
            self.assertFalse(row["available"])
        self.now += timedelta(minutes=16)
        self.refresh()
        self.assertEqual(router.usage_state(self.led, "codex", pc)[0], "ok")

    def test_blocked_with_low_usage_still_hard(self):
        body = response()
        body["ordinaryUsageAllowed"] = False
        self.refresh(platforms._codex_usage(body))
        self.assertEqual(router.usage_state(self.led, "codex", self.cfg["platforms"]["codex"])[0], "hard")

    def test_personal_does_not_inherit_work_home(self):
        del self.cfg["accounts"]["personal"]
        with mock.patch.dict(os.environ, {"CODEX_HOME": self.work, "OPENAI_API_KEY": "secret"}), \
                mock.patch.object(usage.os.path, "isfile", return_value=True), \
                mock.patch.object(platforms, "probe_codex", return_value=[]) as probe:
            usage.refresh_codex(self.cfg, self.led, "codex")
        env = probe.call_args.kwargs["env"]
        self.assertEqual(env["CODEX_HOME"], os.path.expanduser("~/.codex"))
        self.assertNotIn("OPENAI_API_KEY", env)
