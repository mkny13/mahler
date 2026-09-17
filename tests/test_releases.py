"""Tests for release ledger, rolling drafts, note synthesis, and SemVer proposals (DESIGN D31)."""

from datetime import datetime, timedelta, timezone
import json
import unittest

from mahler.ledger import Ledger, iso
from mahler.releases import (
    ReleaseDraft,
    ReleaseItem,
    check_readiness,
    create_release,
    get_draft,
    get_release,
    get_release_items,
    list_releases,
    parse_semver,
    propose_next_version,
    synthesize_notes,
)


class Clock:
    def __init__(self, start=None):
        self.t = start or datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


class SemVerProposalTests(unittest.TestCase):
    def test_parse_semver(self):
        self.assertEqual(parse_semver("0.1.0"), (0, 1, 0))
        self.assertEqual(parse_semver("v1.2.3"), (1, 2, 3))
        self.assertEqual(parse_semver("  2.10.4-rc1  "), (2, 10, 4))
        self.assertIsNone(parse_semver(""))
        self.assertIsNone(parse_semver("invalid"))

    def test_initial_release_proposes_0_1_0(self):
        # Initial project release proposes 0.1.0 regardless of labels
        self.assertEqual(propose_next_version(None, []), "0.1.0")
        self.assertEqual(propose_next_version("", []), "0.1.0")
        self.assertEqual(propose_next_version(None, [{"labels": ["type:feature"]}]), "0.1.0")
        self.assertEqual(propose_next_version(None, [{"labels": ["type:bug"]}]), "0.1.0")

    def test_feature_work_proposes_minor(self):
        # Any feature work proposes a minor bump
        items = [
            ReleaseItem("p", 1, title="Add dark mode", labels=["type:feature"]),
            ReleaseItem("p", 2, title="Fix crash", labels=["type:bug"]),
        ]
        self.assertEqual(propose_next_version("0.1.0", items), "0.2.0")
        self.assertEqual(propose_next_version("1.4.2", items), "1.5.0")

    def test_fixes_and_other_work_propose_patch(self):
        # Fixes only
        fixes = [ReleaseItem("p", 1, title="Fix crash", labels=["type:bug"])]
        self.assertEqual(propose_next_version("0.1.0", fixes), "0.1.1")
        self.assertEqual(propose_next_version("1.2.3", fixes), "1.2.4")

        # Chores only
        chores = [ReleaseItem("p", 2, title="Update deps", labels=["type:chore"])]
        self.assertEqual(propose_next_version("0.1.0", chores), "0.1.1")

        # Other non-breaking work (e.g. goal, uat, or untyped)
        other = [
            ReleaseItem("p", 3, title="Audit performance", labels=["type:goal"]),
            ReleaseItem("p", 4, title="Documentation note", labels=[]),
        ]
        self.assertEqual(propose_next_version("0.1.0", other), "0.1.1")

    def test_major_version_is_never_proposed_automatically(self):
        # Even with extensive features or breaking-sounding changes,
        # major versions remain an explicit operator choice
        items = [
            ReleaseItem("p", 1, title="Rewrite architecture", labels=["type:feature"]),
            ReleaseItem("p", 2, title="Drop legacy APIs", labels=["type:feature"]),
        ]
        # Bumps minor, not major
        self.assertEqual(propose_next_version("1.0.0", items), "1.1.0")
        self.assertNotEqual(propose_next_version("1.0.0", items), "2.0.0")


class NoteSynthesisTests(unittest.TestCase):
    def test_note_grouping_and_classification(self):
        items = [
            ReleaseItem("p", 1, pr=11, title="New login flow", summary="added OAuth login", labels=["type:feature"]),
            ReleaseItem("p", 2, pr=12, title="Fix token refresh", summary="fixed refresh timing", labels=["type:bug"]),
            ReleaseItem("p", 3, pr=13, title="Security audit", summary="audited crypto", labels=["type:chore", "pass:security"]),
            ReleaseItem("p", 4, pr=14, title="Weekly health pass", summary="cleaned dead code", labels=["pass:health"]),
            ReleaseItem("p", 5, pr=15, title="Operator guide", summary="documented deployment", labels=["type:docs"]),
        ]
        notes = synthesize_notes(items)

        self.assertEqual([i.number for i in notes.features], [1])
        self.assertEqual([i.number for i in notes.fixes], [2])
        self.assertEqual([i.number for i in notes.maintenance], [3, 4])
        self.assertEqual([i.number for i in notes.other], [5])

    def test_deterministic_notes_formatting(self):
        items = [
            ReleaseItem("p", 1, pr=11, title="Add export", summary="added CSV export", labels=["type:feature"]),
            ReleaseItem("p", 2, pr=12, title="Fix crash", summary="fixed null pointer", labels=["type:bug"]),
        ]
        notes = synthesize_notes(items)
        main_summary = notes.main_summary

        self.assertIn("### Features", main_summary)
        self.assertIn("- Add export: added CSV export (#1, PR #11)", main_summary)
        self.assertIn("### Fixes", main_summary)
        self.assertIn("- Fix crash: fixed null pointer (#2, PR #12)", main_summary)

    def test_maintenance_excluded_from_main_summary_by_default(self):
        items = [
            ReleaseItem("p", 1, pr=11, title="Add export", summary="added CSV export", labels=["type:feature"]),
            ReleaseItem("p", 2, pr=12, title="Drift audit", summary="checked config drift", labels=["type:chore", "pass:drift"]),
        ]
        notes = synthesize_notes(items)

        # Main summary contains features and fixes, NOT maintenance
        self.assertIn("Add export", notes.main_summary)
        self.assertNotIn("Drift audit", notes.main_summary)

        # summary (default string view) excludes maintenance
        self.assertIn("Add export", notes.summary)
        self.assertNotIn("Drift audit", notes.summary)
        self.assertNotIn("Drift audit", str(notes))

        # expanded_notes contains maintenance wrapped in <details>
        self.assertIn("<details>", notes.expanded_notes)
        self.assertIn("<summary>Maintenance details (1)</summary>", notes.expanded_notes)
        self.assertIn("Drift audit", notes.expanded_notes)

    def test_other_user_facing_changes_section(self):
        items = [
            ReleaseItem("p", 1, pr=11, title="Support dark mode", summary="support dark mode", labels=["type:feature"]),
            ReleaseItem("p", 2, pr=12, title="Milestone checklist", summary="updated checklist", labels=["type:goal"]),
        ]
        notes = synthesize_notes(items)
        self.assertIn("### Features", notes.main_summary)
        self.assertNotIn("Milestone checklist", notes.main_summary)

        self.assertIn("### Other changes", notes.other_section)
        self.assertIn("Milestone checklist", notes.other_section)

        # Both appear in notes.summary, but in their distinct sections
        self.assertIn("### Features", notes.summary)
        self.assertIn("### Other changes", notes.summary)


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)

    def test_empty_draft_is_not_suggested(self):
        suggested, reasons = check_readiness([], self.now)
        self.assertFalse(suggested)
        self.assertEqual(reasons, [])

    def test_five_items_triggers_suggested(self):
        # 5 items all shipped recently (e.g. within 1 hour)
        items = [
            ReleaseItem("p", i, shipped_at=iso(self.now - timedelta(minutes=10 * i)))
            for i in range(1, 6)
        ]
        suggested, reasons = check_readiness(items, self.now)
        self.assertTrue(suggested)
        self.assertTrue(any("5 unreleased items" in r for r in reasons))

    def test_four_items_recent_not_suggested(self):
        items = [
            ReleaseItem("p", i, shipped_at=iso(self.now - timedelta(days=2)))
            for i in range(1, 5)
        ]
        suggested, reasons = check_readiness(items, self.now)
        self.assertFalse(suggested)
        self.assertEqual(reasons, [])

    def test_seven_days_oldest_item_triggers_suggested(self):
        # 1 item shipped exactly 7 days ago
        items = [
            ReleaseItem("p", 1, shipped_at=iso(self.now - timedelta(days=7))),
        ]
        suggested, reasons = check_readiness(items, self.now)
        self.assertTrue(suggested)
        self.assertTrue(any("7 days old" in r for r in reasons))

    def test_readiness_is_advisory_only_and_never_publishes(self):
        # Calling check_readiness does not mutate state or trigger any publish
        items = [
            ReleaseItem("p", i, shipped_at=iso(self.now - timedelta(days=10)))
            for i in range(1, 10)
        ]
        suggested, reasons = check_readiness(items, self.now)
        self.assertTrue(suggested)
        # All items remain untouched
        for it in items:
            self.assertIsNone(it.release_id)


class RollingDraftIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.led = Ledger(":memory:", clock=self.clock)
        self.addCleanup(self.led.close)

    def test_draft_lifecycle(self):
        # Initial draft is empty
        draft0 = get_draft(self.led, "proj", now=self.clock())
        self.assertEqual(draft0.count, 0)
        self.assertEqual(draft0.proposed_version, "0.1.0")
        self.assertFalse(draft0.is_suggested)

        # Ship 2 items (1 feature, 1 bug)
        self.led.snapshot_release_item("proj", 1, pr=10, title="Add notifications",
                                      summary="added push notifications",
                                      merge_sha="sha001", labels=["type:feature"])
        self.clock.advance(hours=1)
        self.led.snapshot_release_item("proj", 2, pr=11, title="Fix battery drain",
                                      summary="fixed background wakeups",
                                      merge_sha="sha002", labels=["type:bug"])

        draft1 = get_draft(self.led, "proj", now=self.clock())
        self.assertEqual(draft1.count, 2)
        self.assertEqual(draft1.proposed_version, "0.1.0")  # First release is 0.1.0
        self.assertEqual(draft1.checkpoint_sha, "sha002")
        self.assertFalse(draft1.is_suggested)
        self.assertIn("### Features", draft1.notes.main_summary)
        self.assertIn("### Fixes", draft1.notes.main_summary)

        # Advance clock by 7 days -> draft becomes suggested
        self.clock.advance(days=7)
        draft2 = get_draft(self.led, "proj", now=self.clock())
        self.assertTrue(draft2.is_suggested)
        self.assertTrue(any("7 days old" in r for r in draft2.readiness_reasons))

        # Complete release 0.1.0
        rel1 = create_release(self.led, "proj")
        self.assertEqual(rel1["version"], "0.1.0")
        self.assertEqual(rel1["checkpoint_sha"], "sha002")

        # Draft is now empty again
        draft3 = get_draft(self.led, "proj", now=self.clock())
        self.assertEqual(draft3.count, 0)
        self.assertFalse(draft3.is_suggested)

        # Ship a bug fix
        self.led.snapshot_release_item("proj", 3, pr=12, title="Fix typo",
                                      summary="fixed spelling error",
                                      merge_sha="sha003", labels=["type:bug"])
        draft4 = get_draft(self.led, "proj", now=self.clock())
        self.assertEqual(draft4.count, 1)
        # Last release was 0.1.0, item is fix -> proposes 0.1.1
        self.assertEqual(draft4.proposed_version, "0.1.1")

        # Ship a feature
        self.led.snapshot_release_item("proj", 4, pr=13, title="Add search",
                                      summary="added search bar",
                                      merge_sha="sha004", labels=["type:feature"])
        draft5 = get_draft(self.led, "proj", now=self.clock())
        self.assertEqual(draft5.count, 2)
        # Has feature -> proposes 0.2.0
        self.assertEqual(draft5.proposed_version, "0.2.0")

        # Past release 0.1.0 and its items remain queryable
        past_rel = get_release(self.led, "proj", "0.1.0")
        self.assertIsNotNone(past_rel)
        past_items = get_release_items(self.led, "proj", "0.1.0")
        self.assertEqual([i.number for i in past_items], [1, 2])


if __name__ == "__main__":
    unittest.main()
