"""mahler#206: periodic self-audit of platform tier/capability assumptions."""

import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler import config, platform_audit, scheduler
from mahler.ledger import Ledger, iso

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def proj(**over):
    pol = {"name": "mahler", "enabled": True, "repo": "mkny13/mahler", "path": "/tmp"}
    pol.update(over)
    return pol


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["projects"] = {"mahler": proj()}
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
        self.gh_mock = mock.Mock()
        self.ctx._gh["mkny13/mahler"] = self.gh_mock
        # Due on the very first check (no checkpoint yet).

    def test_files_when_due(self):
        platform_audit.queue(self.ctx, [proj()])
        self.gh_mock.ensure_pass_label.assert_called_once_with("platform-audit")
        self.gh_mock.create_issue.assert_called_once()
        args, kwargs = self.gh_mock.create_issue.call_args
        self.assertEqual(args[0], platform_audit.TITLE)
        self.assertIn("type:chore", args[2])
        self.assertIn("size:l", args[2])
        self.assertIn("pass:platform-audit", args[2])

        cp = self.led.maintenance_checkpoint("mahler", config.PLATFORM_AUDIT_PASS)
        self.assertEqual(iso(NOW), cp["last_filed_at"])

    def test_not_due_skips(self):
        self.led.set_maintenance_checkpoint("mahler", config.PLATFORM_AUDIT_PASS,
                                            last_filed_at=NOW - timedelta(days=1))
        platform_audit.queue(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_not_called()

    def test_dry_run_never_files_or_resets(self):
        self.ctx.dry_run = True
        platform_audit.queue(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_not_called()
        cp = self.led.maintenance_checkpoint("mahler", config.PLATFORM_AUDIT_PASS)
        self.assertIsNone(cp["last_filed_at"])

    def test_disabled_skips(self):
        self.cfg["platform_audit"] = {"enabled": False}
        platform_audit.queue(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_not_called()

    def test_anchor_project_not_managed_skips(self):
        # "mahler" isn't in the passed-in projects list this tick.
        platform_audit.queue(self.ctx, [])
        self.gh_mock.create_issue.assert_not_called()

    def test_open_pass_of_any_kind_blocks_filing(self):
        """D20 discipline (mahler#204): reused here too."""
        self.led.upsert_item("mahler", 99, labels=json.dumps(["pass:security"]), state="ready")
        platform_audit.queue(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_not_called()

    def test_own_open_pass_blocks_refiling(self):
        self.led.upsert_item("mahler", 99, labels=json.dumps(["pass:platform-audit"]),
                             state="working")
        platform_audit.queue(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_not_called()

    def test_open_issue_with_matching_title_blocks_filing(self):
        self.led.upsert_item("mahler", 57, title=platform_audit.TITLE,
                             labels=json.dumps(["type:goal"]), state="ready")
        platform_audit.queue(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_not_called()

    def test_custom_anchor_project(self):
        self.cfg["platform_audit"] = {"project": "other"}
        self.cfg["projects"]["other"] = proj(name="other", repo="mkny13/other")
        self.ctx._gh["mkny13/other"] = self.gh_mock
        platform_audit.queue(self.ctx, [proj(name="other", repo="mkny13/other")])
        self.gh_mock.create_issue.assert_called_once()
        cp = self.led.maintenance_checkpoint("other", config.PLATFORM_AUDIT_PASS)
        self.assertEqual(iso(NOW), cp["last_filed_at"])
        # the default project ("mahler") is untouched
        self.assertIsNone(
            self.led.maintenance_checkpoint("mahler", config.PLATFORM_AUDIT_PASS)["last_filed_at"])


class MergedThroughputTests(unittest.TestCase):
    """The 'mahler' project's own shipped PRs anchor the checkpoint's
    merged_since counter, same throughput signal D20 uses (mahler#206)."""

    def test_shipped_increments_platform_audit_for_anchor_project_only(self):
        led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(led.close)
        cfg = {"defaults": {}, "projects": {}, "platform_audit": {"project": "mahler"}}
        led.reset_maintenance("mahler", config.PLATFORM_AUDIT_PASS)
        led.reset_maintenance("groundwork", config.PLATFORM_AUDIT_PASS)

        from mahler import ship
        ctx = mock.Mock(cfg=cfg, led=led)
        ctx.gh.return_value = mock.Mock()
        ctx.gh.return_value.comment = mock.Mock()

        ship._shipped(ctx, "mahler", 1, 10, {"summary": "x", "title": "x", "attempts": 0},
                     {"state": "MERGED"})
        self.assertEqual(
            led.maintenance_checkpoint("mahler", config.PLATFORM_AUDIT_PASS)["merged_since"], 1)

        ship._shipped(ctx, "groundwork", 2, 11, {"summary": "y", "title": "y", "attempts": 0},
                     {"state": "MERGED"})
        self.assertEqual(
            led.maintenance_checkpoint("groundwork", config.PLATFORM_AUDIT_PASS)["merged_since"], 0)


class VerifiedDateParsingTests(unittest.TestCase):
    def test_finds_nearest_verified_date_for_an_alias(self):
        text = "**Kilo** does the thing. Verified end-to-end 2026-09-13 (mahler#29)."
        dates = platform_audit.verified_dates(text)
        self.assertEqual(dates["kilo"], "2026-09-13")

    def test_platform_with_no_mention_is_absent(self):
        dates = platform_audit.verified_dates("nothing relevant here")
        self.assertNotIn("kilo", dates)

    def test_picks_latest_date_when_multiple_mentions(self):
        text = ("Copilot CLI flags verified end-to-end 2026-09-13 (mahler#25); "
                "the billing probe verified 2026-09-12 (mahler#38).")
        dates = platform_audit.verified_dates(text)
        self.assertEqual(dates["copilot"], "2026-09-13")

    def test_real_design_md_parses_without_crashing(self):
        text = platform_audit._design_md_text()
        self.assertTrue(text)
        dates = platform_audit.verified_dates(text)
        self.assertIsInstance(dates, dict)


class StaleReportTests(unittest.TestCase):
    def _cfg(self):
        return {"platforms": {"a": {"enabled": True}, "b": {"enabled": True},
                              "c": {"enabled": False}}}

    def test_flags_old_date_as_stale(self):
        rows = platform_audit.stale_report(
            self._cfg(), {"a": "2026-01-01", "b": "2026-09-01"}, NOW, stale_days=90)
        by_name = {r[0]: r for r in rows}
        self.assertTrue(by_name["a"][3])
        self.assertFalse(by_name["b"][3])

    def test_missing_date_counts_as_stale(self):
        rows = platform_audit.stale_report(self._cfg(), {}, NOW, stale_days=90)
        by_name = {r[0]: r for r in rows}
        self.assertTrue(by_name["a"][3])
        self.assertIsNone(by_name["a"][1])

    def test_disabled_platform_excluded(self):
        rows = platform_audit.stale_report(self._cfg(), {}, NOW, stale_days=90)
        self.assertNotIn("c", {r[0] for r in rows})


class TierInversionTests(unittest.TestCase):
    def test_flags_lower_tier_outperforming_higher_tier(self):
        rows = [("kilo", 1, 10, 90.0, 0.0, 0), ("claude", 3, 10, 50.0, 0.0, 0)]
        found = platform_audit.tier_inversions(rows)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][0], "kilo")
        self.assertEqual(found[0][3], "claude")

    def test_no_flag_within_margin(self):
        rows = [("kilo", 1, 10, 85.0, 0.0, 0), ("claude", 3, 10, 80.0, 0.0, 0)]
        self.assertEqual(platform_audit.tier_inversions(rows), [])

    def test_no_flag_with_insufficient_runs(self):
        rows = [("kilo", 1, 1, 100.0, 0.0, 0), ("claude", 3, 1, 0.0, 0.0, 0)]
        self.assertEqual(platform_audit.tier_inversions(rows), [])

    def test_no_flag_when_missing_data(self):
        rows = [("kilo", 1, 10, None, None, 0), ("claude", 3, 10, 50.0, 0.0, 0)]
        self.assertEqual(platform_audit.tier_inversions(rows), [])


class BuildBodyTests(unittest.TestCase):
    def test_body_includes_all_sections_and_no_traceback(self):
        led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(led.close)
        cfg = config.load(path="/nonexistent")
        pol = config.platform_audit_policy(cfg)
        body = platform_audit.build_body(cfg, led, pol)
        self.assertIn("DESIGN.md verified-date staleness", body)
        self.assertIn("Observed ledger outcomes", body)
        self.assertIn("Possible tier inconsistencies", body)


if __name__ == "__main__":
    unittest.main()
