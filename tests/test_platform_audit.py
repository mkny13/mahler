"""mahler#206: periodic self-audit of platform tier/capability assumptions."""

import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler import config, platform_audit, scheduler, tick
from mahler.gh import GHError
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

    def test_pass_filed_this_tick_blocks_audit(self):
        self.ctx.passes_filed.add("mahler")
        platform_audit.queue(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_not_called()

    def test_maintenance_and_audit_share_one_filing(self):
        tick.queue_maintenance(self.ctx, [proj()])
        platform_audit.queue(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_called_once()
        self.assertTrue(self.led.maintenance_due("mahler", config.PLATFORM_AUDIT_PASS))

    def test_combined_dry_run_names_only_one_pass(self):
        self.ctx.dry_run = True
        tick.queue_maintenance(self.ctx, [proj()])
        platform_audit.queue(self.ctx, [proj()])
        self.assertEqual(self.ctx.lines, ["mahler: queuing security pass"])
        self.gh_mock.create_issue.assert_not_called()

    def test_audit_blocks_later_maintenance_even_in_dry_run(self):
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                ctx = scheduler.Ctx(self.cfg, self.led, dry_run=dry_run)
                ctx._gh["mkny13/mahler"] = self.gh_mock
                self.led.set_maintenance_checkpoint(
                    "mahler", config.PLATFORM_AUDIT_PASS,
                    last_filed_at=NOW - timedelta(days=40))
                platform_audit.queue(ctx, [proj()])
                tick.queue_maintenance(ctx, [proj()])
                self.assertEqual(ctx.lines, ["mahler: queuing platform-audit pass"])
                self.assertEqual(ctx.passes_filed, {"mahler"})

    def test_failed_audit_does_not_claim_tick_or_reset_checkpoint(self):
        self.gh_mock.create_issue.side_effect = GHError("unavailable")
        platform_audit.queue(self.ctx, [proj()])
        self.assertEqual(self.ctx.passes_filed, set())
        self.assertIsNone(self.led.maintenance_checkpoint(
            "mahler", config.PLATFORM_AUDIT_PASS)["last_filed_at"])

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
    def test_real_design_md_has_verified_dates_for_enabled_base_platforms(self):
        dates = platform_audit.verified_dates(platform_audit._design_md_text())
        for name, platform in config.DEFAULTS["platforms"].items():
            if platform.get("enabled", True) and "from" not in platform:
                with self.subTest(platform=name):
                    self.assertIn(name, dates)

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


class DerivedPlatformInheritanceTests(unittest.TestCase):
    """mahler#227: a `from = "<base>"` derived platform (D25) inherits its
    root base's verified date instead of being permanently flagged."""

    def _cfg(self, **platforms):
        base = {"claude": {"enabled": True}, "claude-work": {"enabled": True, "from": "claude"}}
        base.update(platforms)
        return {"platforms": base}

    def test_derived_platform_inherits_base_date(self):
        rows = platform_audit.stale_report(
            self._cfg(), {"claude": "2026-09-01"}, NOW, stale_days=90)
        by_name = {r[0]: r for r in rows}
        self.assertEqual(by_name["claude-work"][1], "2026-09-01")
        self.assertFalse(by_name["claude-work"][3])

    def test_inherited_staleness_fires_when_base_is_old(self):
        rows = platform_audit.stale_report(
            self._cfg(), {"claude": "2026-01-01"}, NOW, stale_days=90)
        by_name = {r[0]: r for r in rows}
        self.assertEqual(by_name["claude-work"][1], "2026-01-01")
        self.assertTrue(by_name["claude-work"][3])

    def test_two_hop_chain_resolves_to_root_date(self):
        cfg = self._cfg(**{
            "claude-work": {"enabled": True, "from": "claude"},
            "claude-work-2": {"enabled": True, "from": "claude-work"},
        })
        rows = platform_audit.stale_report(cfg, {"claude": "2026-09-01"}, NOW, stale_days=90)
        by_name = {r[0]: r for r in rows}
        self.assertEqual(by_name["claude-work-2"][1], "2026-09-01")

    def test_from_cycle_does_not_raise_and_reports_no_date(self):
        cfg = self._cfg(**{
            "claude": {"enabled": True, "from": "claude-work"},
            "claude-work": {"enabled": True, "from": "claude"},
        })
        rows = platform_audit.stale_report(cfg, {}, NOW, stale_days=90)
        by_name = {r[0]: r for r in rows}
        self.assertIsNone(by_name["claude-work"][1])
        self.assertTrue(by_name["claude-work"][3])

    def test_unknown_base_does_not_raise_and_reports_no_date(self):
        cfg = self._cfg(**{"claude-work": {"enabled": True, "from": "nonexistent"}})
        rows = platform_audit.stale_report(cfg, {}, NOW, stale_days=90)
        by_name = {r[0]: r for r in rows}
        self.assertIsNone(by_name["claude-work"][1])
        self.assertTrue(by_name["claude-work"][3])

    def test_base_also_missing_date_still_flags(self):
        rows = platform_audit.stale_report(self._cfg(), {}, NOW, stale_days=90)
        by_name = {r[0]: r for r in rows}
        self.assertIsNone(by_name["claude-work"][1])
        self.assertTrue(by_name["claude-work"][3])

    def test_own_date_takes_precedence_over_base_date(self):
        rows = platform_audit.stale_report(
            self._cfg(), {"claude": "2026-01-01", "claude-work": "2026-09-10"},
            NOW, stale_days=90)
        by_name = {r[0]: r for r in rows}
        self.assertEqual(by_name["claude-work"][1], "2026-09-10")

    def test_root_platform_none_for_non_derived_platform(self):
        self.assertIsNone(platform_audit._root_platform(self._cfg(), "claude"))

    def test_root_platform_none_for_unknown_platform(self):
        self.assertIsNone(platform_audit._root_platform(self._cfg(), "nope"))


class TierInversionTests(unittest.TestCase):
    def test_flags_lower_tier_outperforming_higher_tier(self):
        rows = [("kilo", 1, 10, 90.0, 0.0, 0, {"s", "m", "l"}), ("claude", 3, 10, 50.0, 0.0, 0, {"s", "m", "l"})]
        found = platform_audit.tier_inversions(rows)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][0], "kilo")
        self.assertEqual(found[0][3], "claude")

    def test_no_flag_within_margin(self):
        rows = [("kilo", 1, 10, 85.0, 0.0, 0, {"s", "m", "l"}), ("claude", 3, 10, 80.0, 0.0, 0, {"s", "m", "l"})]
        self.assertEqual(platform_audit.tier_inversions(rows), [])

    def test_no_flag_with_insufficient_runs(self):
        rows = [("kilo", 1, 1, 100.0, 0.0, 0, {"s", "m", "l"}), ("claude", 3, 1, 0.0, 0.0, 0, {"s", "m", "l"})]
        self.assertEqual(platform_audit.tier_inversions(rows), [])

    def test_no_flag_when_missing_data(self):
        rows = [("kilo", 1, 10, None, None, 0, {"s", "m", "l"}), ("claude", 3, 10, 50.0, 0.0, 0, {"s", "m", "l"})]
        self.assertEqual(platform_audit.tier_inversions(rows), [])

    def test_size_gate_bounds(self):
        for bounds, expected in [
            ({}, {"s", "m", "l"}),
            ({"max_size": "s"}, {"s"}),
            ({"min_size": "l"}, {"l"}),
            ({"min_size": "m", "max_size": "m"}, {"m"}),
            ({"min_size": "l", "max_size": "s"}, set()),
        ]:
            with self.subTest(bounds=bounds):
                self.assertEqual(platform_audit.size_gate(bounds), expected)

    def test_disjoint_and_overlapping_gates(self):
        for lower, upper, expected in [
            ({"max_size": "s"}, {"min_size": "l"}, False),
            ({"max_size": "m"}, {"min_size": "l"}, False),
            ({"max_size": "s"}, {"max_size": "m"}, True),
            ({}, {"min_size": "l"}, True),
            ({"max_size": "s"}, {}, True),
        ]:
            with self.subTest(lower=lower, upper=upper):
                cfg = {"platforms": {"lo": dict(lower, tier=1),
                                     "hi": dict(upper, tier=3)}}
                outcomes = {"lo": {"runs": 100, "done": 90, "needs_you": 0},
                            "hi": {"runs": 100, "done": 0, "needs_you": 0}}
                rows = platform_audit.outcome_report(cfg, outcomes, {})
                self.assertEqual(bool(platform_audit.tier_inversions(rows)), expected)

    def test_minimum_applies_to_each_side(self):
        for lo_runs, hi_runs in [(9, 20), (20, 9), (10, 10)]:
            with self.subTest(lo_runs=lo_runs, hi_runs=hi_runs):
                rows = [("lo", 1, lo_runs, 90.0, 0.0, 0, {"s"}),
                        ("hi", 3, hi_runs, 0.0, 0.0, 0, {"s"})]
                found = platform_audit.tier_inversions(rows, min_runs=10)
                self.assertEqual(bool(found), lo_runs >= 10 and hi_runs >= 10)


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

    def test_policy_defaults_and_overrides_reach_comparison(self):
        cfg = {"platforms": {"copilot": {"tier": 2, "max_size": "s"},
                             "agy-gemini": {"tier": 3, "max_size": "m"}}}
        led = mock.Mock()
        led.now.return_value = NOW
        led.platform_outcomes.return_value = {
            "copilot": {"runs": 10, "done": 9, "needs_you": 0},
            "agy-gemini": {"runs": 20, "done": 14, "needs_you": 0}}
        led.platform_escalations.return_value = {}
        pol = config.platform_audit_policy(cfg)
        self.assertEqual(pol["inversion_min_runs"], 10)
        self.assertEqual(pol["inversion_margin_pct"], 15.0)
        body = platform_audit.build_body(cfg, led, pol)
        self.assertIn("| Sizes |", body)
        self.assertIn("| copilot | 2 | 10 | 90.0 | 0.0 | 0 | s |", body)
        self.assertIn("`copilot` (tier 2, sizes s, 90.0% of 10 runs)", body)
        self.assertIn("`agy-gemini` (tier 3, sizes s+m, 70.0% of 20 runs)", body)
        for override in [{"inversion_min_runs": 11},
                         {"inversion_margin_pct": 21.0}]:
            with self.subTest(override=override):
                cfg["platform_audit"] = override
                pol = config.platform_audit_policy(cfg)
                body = platform_audit.build_body(cfg, led, pol)
                self.assertNotIn("outperforms", body)
                self.assertIn(f"{pol['inversion_min_runs']} runs on both sides", body)
        led.platform_outcomes.return_value["copilot"] = {
            "runs": 4, "done": 4, "needs_you": 0}
        cfg["platform_audit"] = {}
        self.assertNotIn("outperforms", platform_audit.build_body(
            cfg, led, config.platform_audit_policy(cfg)))
        cfg["platform_audit"] = {"inversion_min_runs": 4, "inversion_margin_pct": 30.0}
        self.assertIn("outperforms", platform_audit.build_body(
            cfg, led, config.platform_audit_policy(cfg)))

    def test_derived_platform_row_shows_via_base(self):
        led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(led.close)
        cfg = config.load(path="/nonexistent")
        cfg["platforms"]["claude-work"] = {"enabled": True, "from": "claude"}
        pol = config.platform_audit_policy(cfg)
        body = platform_audit.build_body(cfg, led, pol)
        self.assertIn("`claude-work` (via `claude`)", body)

    def test_platform_with_own_date_has_no_via_suffix(self):
        led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(led.close)
        cfg = config.load(path="/nonexistent")
        cfg["platforms"]["claude-work"] = {"enabled": True, "from": "claude"}
        pol = config.platform_audit_policy(cfg)
        with mock.patch.object(platform_audit, "verified_dates",
                                return_value={"claude-work": "2026-09-10"}):
            body = platform_audit.build_body(cfg, led, pol)
        self.assertIn("| claude-work | 2026-09-10 |", body)
        self.assertNotIn("(via", body)

    def test_cycle_has_no_via_suffix(self):
        led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(led.close)
        cfg = config.load(path="/nonexistent")
        cfg["platforms"]["claude"]["from"] = "claude-work"
        cfg["platforms"]["claude-work"] = {"enabled": True, "from": "claude"}
        pol = config.platform_audit_policy(cfg)
        body = platform_audit.build_body(cfg, led, pol)
        self.assertNotIn("via", body[:body.index("Observed ledger outcomes")])


if __name__ == "__main__":
    unittest.main()
