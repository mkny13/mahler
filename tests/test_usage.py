"""refresh_usage keeps reviewer quota fresh while PRs wait in verifying (#620)."""
import copy
import unittest
from datetime import datetime, timezone
from unittest import mock

from mahler import config, platforms, router, scheduler, usage
from mahler.ledger import Ledger

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


class VerifyingUsageTests(unittest.TestCase):
    def test_verifying_item_marks_review_platforms_wanted(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["routing"]["review"] = ["agy-claude"]
        cfg["projects"]["x"] = {"path": "/tmp/x", "repo": "x/y"}
        led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(led.close)
        led.upsert_item("x", 1, state="verifying", priority=2)
        ctx = scheduler.Ctx(cfg, led, dry_run=True)
        with mock.patch.object(usage, "_usage_needs_refresh", return_value=False) as needs, \
                mock.patch.object(platforms, "probe_agy", return_value={}), \
                mock.patch.object(router, "usage_state", return_value=("ok", "")):
            usage.refresh_usage(ctx, [config.project_policy(cfg, "x")])
        self.assertIn("agy-claude", {c.args[1] for c in needs.call_args_list})


if __name__ == "__main__":
    unittest.main()
