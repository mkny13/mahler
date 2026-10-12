"""A rejected launch selection stays blocked until configuration repair (#986)."""
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mahler import cli, config, finalize, holds, model_availability as models, platforms, router
from mahler.ledger import Ledger, iso

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)


class ModelAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "ledger.db")
        self.led = Ledger(self.path, clock=lambda: NOW)
        self.addCleanup(lambda: self.led.close())
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.name = "cline-free"
        self.pc = self.cfg["platforms"][self.name]
        self.pc.update(model="retired", effort="high")
        self.run = dict(id=7, platform=self.name, project="x", number=1,
                        role="build", configured_model="retired", effort="high")
        self.ctx = SimpleNamespace(led=self.led, cfg=self.cfg, ping=mock.Mock())

    def reject(self):
        finalize._hold_model_unavailable(self.ctx, self.run, {
            "model_diagnostic": "Error: Invalid model retired TOKEN=secret",
            "available_models": "replacement-low\nreplacement-high"})

    def test_persists_across_restart_time_refresh_unhold_and_duplicate_finalization(self):
        self.reject()
        self.led.close()
        self.led = Ledger(self.path, clock=lambda: NOW + timedelta(days=7))
        self.ctx.led = self.led
        for age in (timedelta(hours=48), timedelta(days=7)):
            with mock.patch.object(self.led, "now", return_value=NOW + age):
                self.led.record_usage(self.name, "5h", 0, iso(NOW + age + timedelta(hours=1)))
                holds.clear(self.cfg, self.led, self.name, by="test")
                self.reject()
                state, detail = router.usage_state(self.led, self.name, self.pc)
                self.assertEqual(state, "soft")
                self.assertIn("configuration needs repair", detail)
                self.assertNotIn("until", detail)
                self.assertIsNone(router.pick(self.cfg, self.led, "build", pin=self.name)[0])
                self.assertEqual(router.capacity_recovery(
                    self.cfg, self.led, {}, "build", pin=self.name), ([], None, None))
        self.ctx.ping.assert_called_once()
        stored = models.blocked(self.led, self.name, self.pc)
        self.assertNotIn("secret", stored["diagnostic"])
        self.assertIn("replacement-high", self.ctx.ping.call_args.args[1])

    def test_only_selection_changes_restore_quota_routing_and_late_result_is_scoped(self):
        self.reject()
        self.pc.update(tier=3, max_size="l", backoff_minutes=99)
        self.assertTrue(models.blocked(self.led, self.name, self.pc))
        for changed in ({"model": "new"}, {"effort": "low"}):
            pc = dict(self.pc, **changed)
            self.assertEqual(router.usage_state(self.led, self.name, pc)[0], "ok")
            self.led.record_usage(self.name, "hold", 100, iso(NOW + timedelta(hours=1)))
            self.led.set_kv(f"hold_reason:{self.name}", "silent")
            self.assertEqual(router.usage_state(self.led, self.name, pc)[0], "soft")
            self.led.clear_usage(self.name, ["hold"])
        self.pc["model"] = "new"
        self.reject()  # late finalizer carries the launch model, not current config
        self.assertFalse(models.blocked(self.led, self.name, self.pc))
        for sibling in ("sibling", "other-account"):
            self.assertEqual(router.usage_state(self.led, sibling, self.pc)[0], "ok")

    def test_legacy_migration_and_no_metadata_one_classification(self):
        self.led.create_run(project="x", number=1, platform=self.name, role="build",
                            epoch=1, status="ended", stop_reason="model_unavailable",
                            configured_model="retired", effort="high")
        self.led.record_usage(self.name, "hold", 100, iso(NOW + timedelta(hours=24)))
        self.led.record_usage(self.name, "weekly", 42, iso(NOW + timedelta(days=7)))
        self.led.set_kv(f"hold_reason:{self.name}", "model_unavailable")
        self.pc["model"] = "new"
        self.assertFalse(models.blocked(self.led, self.name, self.pc))
        self.assertNotIn("hold", self.led.usage(self.name))
        self.assertEqual(self.led.usage(self.name)["weekly"]["used_pct"], 42)
        other = "unrecorded"
        self.led.record_usage(other, "hold", 100, iso(NOW + timedelta(hours=24)))
        self.led.set_kv(f"hold_reason:{other}", "model_unavailable")
        self.assertEqual(router.usage_state(self.led, other, self.pc)[0], "ok")
        self.assertIsNone(self.led.get_kv(f"hold_reason:{other}"))
        models.reject(self.led, other, self.pc, {}, {})
        self.assertEqual(router.usage_state(self.led, other, self.pc)[0], "soft")

    def test_no_metadata_fallback_bounds_to_one_launch(self):
        from mahler import tick
        self.led.record_usage(self.name, "hold", 100, iso(NOW + timedelta(hours=24)))
        self.led.set_kv(f"hold_reason:{self.name}", "model_unavailable")
        self.assertFalse(models.blocked(self.led, self.name, self.pc))
        self.assertTrue(models.probe_pending(self.led, self.name))
        self.cfg["platforms"][self.name]["max_runs"] = 3
        st = {"total": 0, "in_project": {}, "busy_areas": {"x": set(), self.name: set()},
              "busy_files": {self.name: set()}, "per_group": {}, "busy": set(),
              "tiers": [], "probe_led": self.led}
        tick._account_start(self.cfg, self.name, self.name, None, [], st)
        self.assertIn(self.name, st["busy"])
        self.led.create_run(project="x", number=2, platform=self.name, role="build",
                            epoch=1, status="ended", stop_reason="model_unavailable")
        self.assertFalse(models.probe_pending(self.led, self.name))

    def test_unhold_explains_preserved_block_and_missing_list(self):
        finalize._hold_model_unavailable(self.ctx, self.run)
        self.assertIn("Available models: unavailable", self.ctx.ping.call_args.args[1])
        self.assertTrue(holds.inspect(self.cfg, self.led, self.name)["model_block"])
        for dry in (True, False):
            output = io.StringIO()
            with redirect_stdout(output):
                cli.cmd_unhold(SimpleNamespace(platform=self.name, dry_run=dry), self.cfg, self.led)
            self.assertIn("configuration block remains", output.getvalue())
            self.assertTrue(models.blocked(self.led, self.name, self.pc))

    def test_running_work_keeps_genuine_hard_quota_protection(self):
        self.reject()
        self.led.record_usage(self.name, "5h", 100, iso(NOW + timedelta(hours=1)))
        self.assertEqual(router.usage_state(self.led, self.name, self.pc)[0], "hard")

    def test_existing_running_sibling_is_not_stopped(self):
        from mahler import watchdog
        self.reject()
        sibling = "sibling"
        self.cfg["platforms"][sibling] = dict(self.pc, model="working")
        pol = config.project_policy(self.cfg, "x")
        self.ctx.say = mock.Mock()
        with mock.patch.object(watchdog, "_log_idle", return_value=(0, 100)):
            for name in (self.name, sibling):
                run = dict(self.run, platform=name, started_at=iso(NOW), log_path="unused")
                self.assertIsNone(watchdog._health(self.ctx, run, pol, NOW))

    def test_role_specific_selection_and_console_guidance(self):
        from mahler.console import state
        self.pc["build_model"] = "build-only"
        self.pc["sort_model"] = "sort-only"
        run = dict(self.run, role="sort", configured_model="sort-only")
        models.reject(self.led, self.name, self.pc, run, {})
        self.assertFalse(models.blocked(self.led, self.name, self.pc, "build"))
        self.assertTrue(models.blocked(self.led, self.name, self.pc, "plan"))
        row = state._quota_row(self.cfg, self.led, False, self.name, [self.name], [], {})
        self.assertIn("configuration needs repair", row["detail"])
        self.assertIsNone(row["until"])

    def test_agy_plaintext_and_structured_diagnostics_keep_advertised_models(self):
        # Captured rejection from run 11656, preserved in parent issue #972.
        message = ('error: invalid model selection (--model "claude-opus-4-6-thinking" '
                   '--effort ""): model claude-opus-4-6-thinking is not recognized ... '
                   'Available models: Gemini 3.8 Flash (High/Medium/Low), '
                   'Gemini 3.1 Pro (High/Low), Claude Opus 5.5 (Low/Medium/High), '
                   'Claude Sonnet 5.5 (Low/Medium/High), GPT-OSS 120B (Medium)')
        for text in (message, json.dumps({"event": "result", "result": {"status": "ERROR", "message": message}}),
                     "Error: Invalid model: retired\nAvailable models:\nreplacement-low\nreplacement-high"):
            path = Path(self.tmp.name) / "agent.log"
            path.write_text(text)
            log = platforms.read_log(path, "agy")
            self.assertTrue(log["model_unavailable"])
            expected = "replacement-high" if "replacement-high" in text else "GPT-OSS 120B"
            self.assertIn("model", log["model_diagnostic"].lower())
            self.assertIn(expected, log["available_models"])
