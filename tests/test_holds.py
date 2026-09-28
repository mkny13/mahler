"""Manual platform hold inspection and clearing (issue #555)."""

import copy
import io
import json
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from mahler import cli, config, holds, router
from mahler.ledger import Ledger, iso


NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


class HoldTests(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)

    def held(self, platform="cline-free"):
        until = iso(NOW + timedelta(hours=24))
        for window in (router.HOLD, "5h", "weekly"):
            self.led.record_usage(platform, window, 100, until)
        self.led.set_kv(f"hold_reason:{platform}", "model_unavailable")
        self.led.set_kv(f"credit_state:{platform}", json.dumps({"failures": 2}))
        return until

    def test_clear_removes_only_target_hold_rows_and_resets_keys(self):
        until = self.held()
        self.led.record_usage("kilo", router.HOLD, 100, until)
        self.led.set_kv("hold_reason:kilo", "silent")

        found = holds.clear(self.cfg, self.led, "cline-free", by="test")

        self.assertEqual(found["windows"], [router.HOLD, "5h", "weekly"])
        self.assertEqual(self.led.usage("cline-free"), {})
        self.assertIsNone(self.led.get_kv("hold_reason:cline-free"))
        self.assertEqual(self.led.get_kv("credit_state:cline-free"), "{}")
        self.assertIn(router.HOLD, self.led.usage("kilo"))
        self.assertEqual(self.led.get_kv("hold_reason:kilo"), "silent")
        event = self.led.q1("SELECT detail FROM events WHERE kind='hold_cleared'")
        detail = json.loads(event["detail"])
        self.assertEqual((detail["platform"], detail["by"]), ("cline-free", "test"))

    def test_metered_real_readings_survive(self):
        until = iso(NOW + timedelta(hours=2))
        hold_until = iso(NOW + timedelta(hours=24))
        self.led.record_usage("claude", router.HOLD, 100, hold_until)
        self.led.record_usage("claude", "5h", 100, until)
        self.led.record_usage("claude", "weekly", 72, hold_until)

        holds.clear(self.cfg, self.led, "claude", by="test")

        self.assertEqual(set(self.led.usage("claude")), {"5h", "weekly"})

    def test_unhold_without_explicit_hold_does_not_clear_backoff(self):
        until = iso(NOW + timedelta(hours=2))
        for window in ("5h", "weekly"):
            self.led.record_usage("cline-free", window, 100, until)

        holds.clear(self.cfg, self.led, "cline-free", by="test")

        self.assertEqual(set(self.led.usage("cline-free")), {"5h", "weekly"})

    def test_dry_run_lists_hold_without_mutating_or_event(self):
        self.held()
        output = io.StringIO()
        with redirect_stdout(output):
            rc = cli.cmd_unhold(SimpleNamespace(platform="cline-free", dry_run=True),
                                self.cfg, self.led)
        self.assertEqual(rc, 0)
        self.assertIn("would clear hold, 5h, weekly", output.getvalue())
        self.assertIn("model_unavailable", output.getvalue())
        self.assertIn(router.HOLD, self.led.usage("cline-free"))
        self.assertEqual(self.led.q("SELECT * FROM events"), [])

    def test_unknown_platform_is_refused(self):
        error = io.StringIO()
        with redirect_stderr(error):
            rc = cli.cmd_unhold(SimpleNamespace(platform="missing", dry_run=False),
                                self.cfg, self.led)
        self.assertEqual(rc, 2)
        self.assertIn("unknown platform 'missing'", error.getvalue())
        with self.assertRaisesRegex(ValueError, "unknown platform"):
            holds.clear(self.cfg, self.led, "missing", by="test")


if __name__ == "__main__":
    unittest.main()
