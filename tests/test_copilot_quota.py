"""Empty personal billing reports must not invent org-seat headroom (#265)."""
import copy
import json
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler import config, platforms, router, scheduler, usage
from mahler.console import actions, state
from mahler.ledger import Ledger, iso


class CopilotReportTests(unittest.TestCase):
    def probe(self, payload, code=0):
        env = {"GH_CONFIG_DIR": "/isolated/work-gh"}
        with mock.patch.object(platforms, "_gh_login", return_value="work-user"), \
                mock.patch.object(platforms.subprocess, "run", return_value=
                                  subprocess.CompletedProcess([], code, json.dumps(payload))) as run:
            samples = platforms.probe_copilot(1500, env=env)
        self.assertEqual(run.call_args.kwargs["env"], env)
        self.assertEqual(run.call_args.args[0],
                         ["gh", "api", "/users/work-user/settings/billing/ai_credit/usage"])
        return samples

    def test_empty_org_seat_report_has_no_meter(self):
        samples = self.probe({"usageItems": []})
        self.assertEqual(samples, [])
        self.assertIsInstance(samples, platforms.CopilotNoQuota)

    def test_reported_zero_is_a_real_reading(self):
        self.assertEqual(self.probe({"usageItems": [{"grossQuantity": 0}]})[0][:2],
                         ("monthly", 0.0))

    def test_bad_responses_are_not_evidence_of_no_meter(self):
        for payload, code in [({}, 0), ([], 0), ({"usageItems": None}, 0),
                              ({"usageItems": []}, 1),
                              ({"usageItems": [{}]}, 0),
                              ({"usageItems": [{"grossQuantity": "0"}]}, 0),
                              ({"usageItems": [{"grossQuantity": float("nan")}]}, 0)]:
            with self.subTest(payload=payload, code=code):
                samples = self.probe(payload, code)
                self.assertEqual(samples, [])
                self.assertNotIsInstance(samples, platforms.CopilotNoQuota)


class CopilotFallbackTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 15, tzinfo=timezone.utc)
        self.led = Ledger(":memory:", clock=lambda: self.now)
        self.addCleanup(self.led.con.close)
        pc = copy.deepcopy(config.DEFAULTS["platforms"]["copilot"])
        pc["stale_minutes"] = 15
        self.cfg = config.resolve_platforms(config._merge(config.DEFAULTS, {
            "platforms": {
                "copilot-work": dict(pc, account="work", quota_group="copilot@work"),
                "copilot-high-work": dict(pc, account="work", quota_group="copilot@work")},
            "accounts": {"work": {"env": {"GH_CONFIG_DIR": "/isolated/work-gh"},
                                    "routing": {"build": ["copilot-work", "copilot-high-work"]}}},
            "projects": {"acme": {"enabled": True, "account": "work",
                                    "repo": "x/acme", "path": "/tmp/acme"}},
        }))
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=True)
        self.led.upsert_item("acme", 1, state="ready", priority=2)
        self.projects = [config.project_policy(self.cfg, "acme")]
        self.peers = ("copilot-work", "copilot-high-work")

    def refresh(self, samples):
        with mock.patch.object(platforms, "probe_copilot", return_value=samples) as probe:
            usage.refresh_usage(self.ctx, self.projects)
        return probe

    def test_fallback_fans_out_without_recording_fake_usage(self):
        self.refresh(platforms.CopilotNoQuota()).assert_called_once()
        for name in self.peers:
            self.assertEqual(self.led.usage(name), {})
            self.assertFalse(self.led.get_kv(f"probe:{name}"))
            self.assertEqual(router.usage_state(self.led, name, self.cfg["platforms"][name]),
                             ("ok", "unknown limit (platform reports no quota signal)"))
        self.assertIsNone(self.led.get_kv("copilot:no-quota:copilot"))
        self.refresh(platforms.CopilotNoQuota()).assert_not_called()
        rows = state._quota(self.cfg, self.led, None)
        row = next(r for r in rows if "copilot-work" in r["members"])
        self.assertEqual(row["label"], "unmetered")
        self.assertFalse(row["metered"])

    def test_old_zero_is_hidden_and_quota_error_still_backs_off(self):
        name = "copilot-work"
        pc = self.cfg["platforms"][name]
        self.led.record_usage(name, "monthly", 0, iso(self.now + timedelta(days=5)))
        self.now += timedelta(minutes=20)
        self.refresh(platforms.CopilotNoQuota())
        self.assertIn("unknown limit", router.usage_state(self.led, name, pc)[1])
        self.led.record_usage(name, "monthly", 100, iso(self.now + timedelta(hours=1)))
        self.assertEqual(router.usage_state(self.led, name, pc)[0], "hard")
        actions.clear_backoff(self.cfg, self.led, {"platforms": [name]})
        self.assertEqual(router.usage_state(self.led, name, pc)[0], "ok")

    def test_retries_and_recovers_a_real_meter(self):
        self.refresh(platforms.CopilotNoQuota())
        self.now += timedelta(minutes=16)
        self.refresh([("monthly", 25.0, iso(self.now + timedelta(days=5)))]).assert_called_once()
        for name in self.peers:
            self.assertTrue(router.is_metered(self.led, name, self.cfg["platforms"][name]))
            self.assertEqual(self.led.usage(name)["monthly"]["used_pct"], 25.0)

    def test_failure_is_stale_including_after_fallback_expires(self):
        self.refresh(platforms.CopilotNoQuota())
        self.now += timedelta(minutes=16)
        self.refresh([])
        for name in self.peers:
            self.assertEqual(router.usage_state(self.led, name, self.cfg["platforms"][name])[0],
                             "stale")
            self.assertFalse(self.led.get_kv(f"probe:{name}"))

    def test_manual_override_skips_probing_even_with_old_samples(self):
        for name in self.peers:
            self.cfg["platforms"][name]["metered"] = False
            self.led.record_usage(name, "monthly", 0, iso(self.now + timedelta(days=5)))
        self.now += timedelta(hours=1)
        self.refresh([]).assert_not_called()
