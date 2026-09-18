"""Tests for release ledger, rolling drafts, note synthesis, and SemVer proposals (DESIGN D31)."""

from datetime import datetime, timedelta, timezone
import json
import unittest

from mahler.ledger import Ledger, iso
from mahler.releases import (
    ReleaseConflictError,
    ReleaseDraft,
    ReleaseItem,
    check_readiness,
    create_release,
    format_preview,
    get_draft,
    get_release,
    get_release_items,
    list_releases,
    normalize_semver,
    parse_semver,
    propose_next_version,
    publish_release,
    semver_options,
    synthesize_notes,
    validate_semver,
    version_key,
    build_feed,
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
        self.assertEqual(parse_semver("v0.83"), (0, 83))
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

    def test_two_part_sequence_always_advances_second_component(self):
        feature = [ReleaseItem("p", 1, labels=["type:feature"])]
        fix = [ReleaseItem("p", 2, labels=["type:bug"])]
        self.assertEqual(propose_next_version("0.83", feature), "0.84")
        self.assertEqual(propose_next_version("0.83", fix), "0.84")


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


class SemVerValidationTests(unittest.TestCase):
    def test_valid_strict_semver(self):
        self.assertEqual(validate_semver("0.1.0"), (0, 1, 0))
        self.assertEqual(validate_semver("v1.2.3"), (1, 2, 3))
        self.assertEqual(validate_semver("10.20.30"), (10, 20, 30))
        self.assertEqual(normalize_semver("v1.2.3"), "1.2.3")
        self.assertEqual(normalize_semver("0.1.0"), "0.1.0")
        self.assertEqual(validate_semver("v0.83"), (0, 83))
        self.assertEqual(normalize_semver("v0.83"), "0.83")
        self.assertEqual(version_key("0.83"), (0, 83, 0))

    def test_invalid_strict_semver(self):
        for invalid in ["", "   ", "1", "1.2.3.4", "01.2.3", "1.02.3", "abc", "v", "1.2.3-beta"]:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    validate_semver(invalid)


class PreviewFormattingTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.led = Ledger(":memory:", clock=self.clock)
        self.addCleanup(self.led.close)

    def test_preview_output_contents(self):
        self.led.snapshot_release_item("proj", 1, pr=10, title="Add export",
                                      summary="added CSV export",
                                      merge_sha="sha001", labels=["type:feature"])
        self.led.snapshot_release_item("proj", 2, pr=11, title="Fix bug",
                                      summary="fixed null pointer",
                                      merge_sha="sha002", labels=["type:bug"])
        self.led.snapshot_release_item("proj", 3, pr=12, title="Cleanup",
                                      summary="cleaned dead code",
                                      merge_sha="sha003", labels=["type:chore"])
        draft = get_draft(self.led, "proj", now=self.clock())

        # Preview with proposed version
        preview = format_preview(draft, checkpoint_sha="base_sha_123")
        self.assertIn("Release preview for proj:", preview)
        self.assertIn("Proposed version:  0.1.0", preview)
        self.assertIn("Checkpoint SHA:    base_sha_123", preview)
        self.assertIn("Item count:        3", preview)
        self.assertIn("Release suggested: no", preview)
        self.assertIn("### Features", preview)
        self.assertIn("- Add export: added CSV export (#1, PR #10)", preview)
        self.assertIn("### Fixes", preview)
        self.assertIn("- Fix bug: fixed null pointer (#2, PR #11)", preview)
        self.assertIn("<details>", preview)
        self.assertIn("<summary>Maintenance details (1)</summary>", preview)
        self.assertIn("- Cleanup: cleaned dead code (#3, PR #12)", preview)

        # Preview with explicit selected version
        preview_sel = format_preview(draft, version="0.2.0", checkpoint_sha="base_sha_123")
        self.assertIn("Selected version:  0.2.0", preview_sel)
        self.assertIn("Proposed version:  0.1.0", preview_sel)


class MockGH:
    def __init__(self, repo="mkny13/mahler"):
        self.repo = repo
        self.releases = {}
        self.tags = {}
        self.created_releases = []

    def get_release(self, tag):
        return self.releases.get(tag)

    def get_tag_sha(self, tag):
        return self.tags.get(tag)

    def release_create(self, tag, target, title, notes):
        url = f"https://github.com/{self.repo}/releases/tag/{tag}"
        rel = {
            "tagName": tag,
            "targetCommitish": target,
            "body": notes,
            "url": url,
        }
        self.releases[tag] = rel
        self.tags[tag] = target
        self.created_releases.append(rel)
        return url


class PublishReleaseTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.led = Ledger(":memory:", clock=self.clock)
        self.addCleanup(self.led.close)
        self.gh = MockGH()

    def test_publish_initial_release_success(self):
        self.led.snapshot_release_item("proj", 1, pr=10, title="Add feature",
                                      summary="added feature",
                                      merge_sha="sha001", labels=["type:feature"])
        self.led.snapshot_release_item("proj", 2, pr=11, title="Fix bug",
                                      summary="fixed bug",
                                      merge_sha="sha002", labels=["type:bug"])

        res = publish_release(self.led, self.gh, "proj", version="0.1.0", checkpoint_sha="sha002")
        self.assertEqual(res["status"], "published")
        self.assertEqual(res["url"], "https://github.com/mkny13/mahler/releases/tag/v0.1.0")

        # GitHub release was created
        self.assertIn("v0.1.0", self.gh.releases)
        self.assertEqual(self.gh.releases["v0.1.0"]["targetCommitish"], "sha002")

        # Local ledger record created and items sealed
        local_rel = self.led.get_release("proj", version="0.1.0")
        self.assertIsNotNone(local_rel)
        self.assertEqual(local_rel["checkpoint_sha"], "sha002")
        self.assertEqual(len(self.led.unreleased_items("proj")), 0)
        sealed_items = self.led.release_items_for_release(local_rel["id"])
        self.assertEqual([i["number"] for i in sealed_items], [1, 2])

    def test_publish_preserves_two_part_version_and_tag(self):
        create_release(self.led, "proj", version="0.83", checkpoint_sha="sha_old",
                       item_numbers=[])
        self.led.snapshot_release_item("proj", 1, merge_sha="sha084", labels=["type:bug"])
        result = publish_release(self.led, self.gh, "proj", version="v0.84",
                                 checkpoint_sha="sha084")
        self.assertEqual(result["release"]["version"], "0.84")
        self.assertIn("v0.84", self.gh.releases)

    def test_publish_requires_checkpoint_sha(self):
        with self.assertRaises(ValueError):
            publish_release(self.led, self.gh, "proj", version="0.1.0", checkpoint_sha="")

    def test_publish_rejects_non_increasing_version(self):
        create_release(self.led, "proj", version="0.2.0", checkpoint_sha="sha001")

        # Attempt to publish 0.1.0 when 0.2.0 is latest
        with self.assertRaises(ValueError) as cm:
            publish_release(self.led, self.gh, "proj", version="0.1.0", checkpoint_sha="sha002")
        self.assertIn("must be greater than latest recorded version 0.2.0", str(cm.exception))

    def test_publish_reconciles_partially_successful_attempt(self):
        self.led.snapshot_release_item("proj", 1, pr=10, title="Feature",
                                      summary="added feat",
                                      merge_sha="sha001", labels=["type:feature"])
        draft = get_draft(self.led, "proj", now=self.clock())
        notes = draft.notes.render(include_maintenance=True, collapsed_maintenance=True)

        # Pre-seed remote release on GitHub (simulating remote release created but local process crashed)
        self.gh.releases["v0.1.0"] = {
            "tagName": "v0.1.0",
            "targetCommitish": "sha001",
            "body": notes,
            "url": "https://github.com/mkny13/mahler/releases/tag/v0.1.0",
        }
        self.gh.tags["v0.1.0"] = "sha001"

        # Publish should reconcile
        res = publish_release(self.led, self.gh, "proj", version="0.1.0", checkpoint_sha="sha001")
        self.assertEqual(res["status"], "reconciled")
        self.assertEqual(len(self.gh.created_releases), 0)  # No second release created!

        # Local ledger record now completed and items sealed
        local_rel = self.led.get_release("proj", version="0.1.0")
        self.assertIsNotNone(local_rel)
        self.assertEqual(len(self.led.unreleased_items("proj")), 0)

    def test_publish_idempotent_on_repeated_success(self):
        self.led.snapshot_release_item("proj", 1, pr=10, title="Feature",
                                      summary="added feat",
                                      merge_sha="sha001", labels=["type:feature"])

        # First publish
        res1 = publish_release(self.led, self.gh, "proj", version="0.1.0", checkpoint_sha="sha001")
        self.assertEqual(res1["status"], "published")
        self.assertEqual(len(self.gh.created_releases), 1)

        # Repeating the exact command
        res2 = publish_release(self.led, self.gh, "proj", version="0.1.0", checkpoint_sha="sha001")
        self.assertEqual(res2["status"], "reconciled")
        self.assertEqual(len(self.gh.created_releases), 1)  # Still only 1 release created

    def test_publish_refuses_conflicting_remote_release_sha(self):
        self.led.snapshot_release_item("proj", 1, pr=10, title="Feature",
                                      summary="added feat",
                                      merge_sha="sha001", labels=["type:feature"])
        draft = get_draft(self.led, "proj", now=self.clock())
        notes = draft.notes.render(include_maintenance=True, collapsed_maintenance=True)

        # Remote release exists with conflicting SHA
        self.gh.releases["v0.1.0"] = {
            "tagName": "v0.1.0",
            "targetCommitish": "different_sha_999",
            "body": notes,
            "url": "https://github.com/mkny13/mahler/releases/tag/v0.1.0",
        }
        self.gh.tags["v0.1.0"] = "different_sha_999"

        with self.assertRaises(ReleaseConflictError) as cm:
            publish_release(self.led, self.gh, "proj", version="0.1.0", checkpoint_sha="sha001")
        self.assertIn("conflicting with previewed SHA", str(cm.exception))

        # Draft items remain unsealed
        self.assertEqual(len(self.led.unreleased_items("proj")), 1)
        self.assertIsNone(self.led.get_release("proj", version="0.1.0"))

    def test_publish_refuses_conflicting_remote_release_notes(self):
        self.led.snapshot_release_item("proj", 1, pr=10, title="Feature",
                                      summary="added feat",
                                      merge_sha="sha001", labels=["type:feature"])

        # Remote release exists with different notes
        self.gh.releases["v0.1.0"] = {
            "tagName": "v0.1.0",
            "targetCommitish": "sha001",
            "body": "Completely different notes",
            "url": "https://github.com/mkny13/mahler/releases/tag/v0.1.0",
        }
        self.gh.tags["v0.1.0"] = "sha001"

        with self.assertRaises(ReleaseConflictError) as cm:
            publish_release(self.led, self.gh, "proj", version="0.1.0", checkpoint_sha="sha001")
        self.assertIn("notes conflict", str(cm.exception))

        # Draft items remain unsealed
        self.assertEqual(len(self.led.unreleased_items("proj")), 1)
        self.assertIsNone(self.led.get_release("proj", version="0.1.0"))

    def test_publish_refuses_conflicting_remote_tag(self):
        self.led.snapshot_release_item("proj", 1, pr=10, title="Feature",
                                      summary="added feat",
                                      merge_sha="sha001", labels=["type:feature"])

        # Git tag exists at different SHA, but no release
        self.gh.tags["v0.1.0"] = "different_tag_sha_888"

        with self.assertRaises(ReleaseConflictError) as cm:
            publish_release(self.led, self.gh, "proj", version="0.1.0", checkpoint_sha="sha001")
        self.assertIn("remote tag v0.1.0 already exists at SHA different_tag_sha_888", str(cm.exception))

        # Draft items remain unsealed
        self.assertEqual(len(self.led.unreleased_items("proj")), 1)
        self.assertIsNone(self.led.get_release("proj", version="0.1.0"))


class SemverOptionsTests(unittest.TestCase):
    def test_initial_release_options(self):
        opts = semver_options(None, "0.1.0")
        self.assertEqual(opts["proposed"], "0.1.0")
        self.assertEqual(opts["patch"], "0.1.1")
        self.assertEqual(opts["minor"], "0.2.0")
        self.assertEqual(opts["major"], "1.0.0")

    def test_subsequent_release_options(self):
        opts = semver_options("1.2.3", "1.3.0")
        self.assertEqual(opts["proposed"], "1.3.0")
        self.assertEqual(opts["patch"], "1.2.4")
        self.assertEqual(opts["minor"], "1.3.0")
        self.assertEqual(opts["major"], "2.0.0")

    def test_two_part_release_options_preserve_two_parts(self):
        opts = semver_options("0.83", "0.84")
        self.assertEqual(opts["scheme"], "two-part")
        self.assertEqual(opts["proposed"], "0.84")
        self.assertEqual(opts["patch"], "0.84")
        self.assertEqual(opts["minor"], "0.84")
        self.assertEqual(opts["major"], "1.0")


class BuildFeedTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(datetime(2026, 9, 17, 2, 0, tzinfo=timezone.utc))
        self.led = Ledger(":memory:", clock=self.clock)

    def test_empty_feed(self):
        feed = build_feed(self.led, "proj")
        self.assertEqual(feed["schema_version"], 1)
        self.assertEqual(feed["project"], "proj")
        self.assertEqual(feed["generated_at"], "2026-09-17T02:00:00Z")
        self.assertEqual(feed["releases"], [])

    def test_limit_validation(self):
        with self.assertRaises(ValueError):
            build_feed(self.led, "proj", limit=0)
        with self.assertRaises(ValueError):
            build_feed(self.led, "proj", limit=-5)

    def test_feed_preserves_two_part_version(self):
        self.led.create_release("proj", version="0.83", checkpoint_sha="sha83",
                                state="published", item_numbers=[])
        self.assertEqual(build_feed(self.led, "proj")["releases"][0]["version"], "0.83")

    def test_feed_reflects_only_published_releases(self):
        # 1. Shipped item in draft (not released yet)
        self.led.snapshot_release_item("proj", 1, pr=11, title="Draft item",
                                      summary="not released yet", merge_sha="sha_draft",
                                      labels=["type:feature"])
        # 2. Release in draft / non-published state
        self.led.create_release("proj", version="0.1.0", checkpoint_sha="sha_unpub",
                                state="draft", item_numbers=[1])

        feed = build_feed(self.led, "proj")
        # Should be empty because release state is draft, not published
        self.assertEqual(feed["releases"], [])

    def test_feed_conforms_to_schema_v1_and_excludes_sensitive_data(self):
        # Create release 1.0.0 with feature, fix, other, and maintenance
        self.led.snapshot_release_item("proj", 10, pr=100, title="Add dark mode",
                                      summary="persists theme across launches",
                                      merge_sha="sha10", labels=["type:feature"],
                                      shipped_at="2026-09-10T10:00:00Z")
        self.led.snapshot_release_item("proj", 11, pr=101, title="Fix audio stutter",
                                      summary="prevents buffer underrun",
                                      merge_sha="sha11", labels=["type:bug"],
                                      shipped_at="2026-09-11T10:00:00Z")
        self.led.snapshot_release_item("proj", 12, pr=102, title="Update docs",
                                      summary="clarifies install steps",
                                      merge_sha="sha12", labels=["type:docs"],
                                      shipped_at="2026-09-12T10:00:00Z")
        self.led.snapshot_release_item("proj", 13, pr=103, title="Bump dependencies",
                                      summary="updates build tooling",
                                      merge_sha="sha13", labels=["type:chore"],
                                      shipped_at="2026-09-13T10:00:00Z")

        self.led.create_release(
            "proj", version="1.0.0", checkpoint_sha="sha_rel1",
            state="published", published_at="2026-09-14T12:00:00Z",
            remote_url="https://github.com/mkny13/proj/releases/tag/v1.0.0",
            item_numbers=[10, 11, 12, 13],
        )

        feed = build_feed(self.led, "proj")
        self.assertEqual(feed["schema_version"], 1)
        self.assertEqual(feed["project"], "proj")
        self.assertEqual(len(feed["releases"]), 1)

        rel = feed["releases"][0]
        self.assertEqual(rel["version"], "1.0.0")
        self.assertEqual(rel["checkpoint_sha"], "sha_rel1")
        self.assertEqual(rel["published_at"], "2026-09-14T12:00:00Z")
        self.assertEqual(rel["remote_url"], "https://github.com/mkny13/proj/releases/tag/v1.0.0")

        # Sections check
        self.assertEqual(len(rel["sections"]["features"]), 1)
        feat = rel["sections"]["features"][0]
        self.assertEqual(feat, {
            "number": 10,
            "pr": 100,
            "title": "Add dark mode",
            "summary": "persists theme across launches",
        })

        self.assertEqual(len(rel["sections"]["fixes"]), 1)
        fix = rel["sections"]["fixes"][0]
        self.assertEqual(fix, {
            "number": 11,
            "pr": 101,
            "title": "Fix audio stutter",
            "summary": "prevents buffer underrun",
        })

        self.assertEqual(len(rel["sections"]["other"]), 1)
        oth = rel["sections"]["other"][0]
        self.assertEqual(oth, {
            "number": 12,
            "pr": 102,
            "title": "Update docs",
            "summary": "clarifies install steps",
        })

        # Maintenance list separated from main sections
        self.assertEqual(len(rel["maintenance"]), 1)
        maint = rel["maintenance"][0]
        self.assertEqual(maint, {
            "number": 13,
            "pr": 103,
            "title": "Bump dependencies",
            "summary": "updates build tooling",
        })

        # Strict privacy check: ensure NO internal or operational fields leaked into feed items
        for item in [feat, fix, oth, maint]:
            self.assertEqual(set(item.keys()), {"number", "pr", "title", "summary"})
            self.assertNotIn("labels", item)
            self.assertNotIn("merge_sha", item)
            self.assertNotIn("shipped_at", item)
            self.assertNotIn("release_id", item)
            self.assertNotIn("project", item)

    def test_feed_newest_first_and_limits(self):
        # Create 3 published releases
        for i in range(1, 4):
            self.led.snapshot_release_item("proj", i, pr=i + 10, title=f"Feat {i}",
                                          summary=f"sum {i}", labels=["type:feature"])
            self.led.create_release(
                "proj", version=f"0.{i}.0", checkpoint_sha=f"sha_{i}",
                state="published", published_at=f"2026-09-0{i}T12:00:00Z",
                item_numbers=[i]
            )

        # Default query returns newest first
        feed = build_feed(self.led, "proj")
        versions = [r["version"] for r in feed["releases"]]
        self.assertEqual(versions, ["0.3.0", "0.2.0", "0.1.0"])

        # Limit 2
        limited_feed = build_feed(self.led, "proj", limit=2)
        self.assertEqual([r["version"] for r in limited_feed["releases"]], ["0.3.0", "0.2.0"])


if __name__ == "__main__":
    unittest.main()
