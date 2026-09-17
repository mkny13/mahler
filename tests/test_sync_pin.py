"""`/mahler platform X` must stick (mahler#20).

sync() re-derives pin=pin_of(labels) on every tick, so the comment command
edits the platform:* labels on GitHub and mirrors the change into the ledger.
Everything here runs against an in-memory Ledger with the gh CLI replaced by
FakeGH: no GitHub, no subprocesses. FakeGH applies label edits to its stored
issues, so a further sync() sees them — that is the regression this file pins
down: a ledger-only pin used to be wiped by the next tick's upsert.
"""

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler import config, scheduler, sync, tick
from mahler.gh import GHError, has_sections
from mahler.ledger import Ledger, iso
from mahler import prompt

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


def t(minutes):
    return iso(NOW + timedelta(minutes=minutes))


class FakeGH:
    """In-memory stand-in for gh.GH. open_issues() feeds sync(); set_pin_labels
    edits the stored labels, so the next sync() reads the new pin back."""

    def __init__(self, issues=None):
        self.issues = issues or {}     # number -> {"title", "labels", "comments"}
        self.edits = []                # (number, want, current_labels)
        self.fail_pin = False
        self.fail_state_label = False
        self.fail_issue_state_for = set()

    def open_issues(self):
        return [{"number": n, "title": i["title"], "body": i.get("body", ""),
                 "createdAt": t(-60), "updatedAt": t(0),
                 "url": f"https://github.com/x/y/issues/{n}",
                 "labels": [{"name": l} for l in i["labels"]],
                 "comments": [{"createdAt": at, "body": body}
                              for at, body in i.get("comments", [])]}
                for n, i in sorted(self.issues.items())]

    def issues_changed(self, etag=None):
        return (True, None)              # this fake's repo is always "modified"

    def add_label(self, number, label):
        if label not in self.issues[number]["labels"]:
            self.issues[number]["labels"].append(label)

    def issue_state(self, number):
        if number in self.fail_issue_state_for:
            raise GHError("github down")
        return "OPEN"

    def blocked_by_of(self, number):
        return list(self.issues[number].get("blocked_by", []))

    def comment(self, number, body):
        pass

    def set_pin_labels(self, number, want, current_labels):
        if self.fail_pin:
            raise GHError("github down")
        self.edits.append((number, want, list(current_labels)))
        keep = f"platform:{want}" if want else None
        labels = [l for l in current_labels
                  if not l.startswith("platform:") or l == keep]
        if keep and keep not in labels:
            labels.append(keep)
        self.issues[number]["labels"] = labels

    def set_state_label(self, number, state, current_labels):
        if self.fail_state_label:
            raise GHError("github down")
        self.issues[number]["labels"] = current_labels


class PinTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["projects"]["x"] = {"path": tmp.name, "repo": "x/y"}
        self.gh = FakeGH({5: {"title": "An issue", "labels": ["mahler:inbox"],
                              "comments": []}})
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)
        self.ctx = scheduler.Ctx(self.cfg, self.led)
        self.sync()                     # the item exists, nothing pinned yet

    def sync(self):
        with mock.patch.object(self.ctx, "gh", return_value=self.gh):
            sync.sync(self.ctx, "x")

    def command(self, body, minutes=1):
        """A user comment arrives; sync processes it."""
        self.gh.issues[5]["comments"].append((t(minutes), body))
        self.sync()

    def lines(self):
        return "\n".join(self.ctx.lines)

    def test_platform_command_sets_the_label_and_sticks(self):
        self.command("/mahler platform agy-claude")
        self.assertEqual(self.gh.edits, [(5, "agy-claude", ["mahler:inbox"])])
        self.assertEqual(self.led.item("x", 5)["pin"], "agy-claude")
        self.assertIn("platform:agy-claude", self.gh.issues[5]["labels"])
        # The regression: two further ticks must not wipe the pin.
        self.sync()
        self.sync()
        self.assertEqual(self.led.item("x", 5)["pin"], "agy-claude")
        self.assertEqual(len(self.gh.edits), 1)     # the command ran once

    def test_switching_platforms_replaces_the_label(self):
        self.command("/mahler platform agy-gemini")
        self.command("/mahler platform claude", minutes=2)
        self.assertEqual([e[1] for e in self.gh.edits], ["agy-gemini", "claude"])
        self.assertNotIn("platform:agy-gemini", self.gh.issues[5]["labels"])
        self.assertEqual(self.led.item("x", 5)["pin"], "claude")

    def test_platform_none_unpins(self):
        self.command("/mahler platform agy-claude")
        self.command("/mahler platform none", minutes=2)
        self.assertEqual(self.gh.edits[-1][:2], (5, None))
        self.assertNotIn("platform:agy-claude", self.gh.issues[5]["labels"])
        self.assertIsNone(self.led.item("x", 5)["pin"])
        self.sync()                     # the next tick keeps it unpinned
        self.assertIsNone(self.led.item("x", 5)["pin"])

    def test_platform_auto_unpins(self):
        self.command("/mahler platform claude")
        self.command("/mahler platform auto", minutes=2)
        self.assertEqual(self.gh.edits[-1][:2], (5, None))
        self.assertNotIn("platform:claude", self.gh.issues[5]["labels"])
        self.assertIsNone(self.led.item("x", 5)["pin"])
        self.sync()
        self.assertIsNone(self.led.item("x", 5)["pin"])

    def test_unknown_platform_changes_nothing(self):
        before = list(self.gh.issues[5]["labels"])
        self.command("/mahler platform nosuch")
        self.assertIn("unknown platform 'nosuch'", self.lines())
        self.assertEqual(self.gh.edits, [])
        self.assertEqual(self.gh.issues[5]["labels"], before)
        self.assertIsNone(self.led.item("x", 5)["pin"])

    def test_bare_platform_is_logged_without_change(self):
        before = list(self.gh.issues[5]["labels"])
        self.command("/mahler platform")
        self.assertIn("instruction 'platform'", self.lines())
        self.assertEqual(self.gh.edits, [])
        self.assertEqual(self.gh.issues[5]["labels"], before)
        self.assertIsNone(self.led.item("x", 5)["pin"])

    def test_hand_set_label_still_pins_without_a_command(self):
        self.gh.issues[5]["labels"].append("platform:agy-gemini")
        self.sync()
        self.assertEqual(self.led.item("x", 5)["pin"], "agy-gemini")
        self.assertEqual(self.gh.edits, [])

    def test_set_pin_label_failure_still_updates_the_ledger(self):
        """The label edit failing must not stop the ledger's pin from taking
        effect this tick (D9): only the GitHub-side mirror is behind."""
        self.gh.fail_pin = True
        self.command("/mahler platform agy-claude")
        self.assertEqual(self.gh.edits, [])
        self.assertNotIn("platform:agy-claude", self.gh.issues[5]["labels"])
        self.assertEqual(self.led.item("x", 5)["pin"], "agy-claude")
        self.assertIn("platform label update failed", self.lines())

    def test_closed_issue_lookup_failure_is_skipped_not_raised(self):
        """sync()'s per-item closed-issue check must not break the tick when
        GitHub is unreachable — the item is just checked again next tick."""
        del self.gh.issues[5]                # no longer returned by open_issues()
        self.gh.fail_issue_state_for = {5}
        self.sync()                          # must not raise
        self.assertEqual(self.led.item("x", 5)["state"], "inbox")   # unchanged


class SyncStoresFilesTests(unittest.TestCase):
    """mahler#210: sync() parses each issue's `## Plan` Files: list into the
    ledger's `files` column every tick, so the scheduler can check overlap
    without an extra GitHub call."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["projects"]["x"] = {"path": tmp.name, "repo": "x/y"}
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)
        self.ctx = scheduler.Ctx(self.cfg, self.led)

    def sync(self, gh):
        with mock.patch.object(self.ctx, "gh", return_value=gh):
            sync.sync(self.ctx, "x")

    def test_files_parsed_from_plan_section(self):
        gh = FakeGH({5: {"title": "An issue", "labels": ["mahler:ready"],
                        "body": "## Plan\nFiles:\n- `a.py`\n- `b.py`\nSteps:\n- x\n",
                        "comments": []}})
        self.sync(gh)
        self.assertEqual(json.loads(self.led.item("x", 5)["files"]), ["a.py", "b.py"])

    def test_files_updated_on_a_later_sync(self):
        gh = FakeGH({5: {"title": "An issue", "labels": ["mahler:ready"],
                        "body": "## Plan\nFiles: `a.py`\n", "comments": []}})
        self.sync(gh)
        self.assertEqual(json.loads(self.led.item("x", 5)["files"]), ["a.py"])
        gh.issues[5]["body"] = "## Plan\nFiles: `a.py`, `c.py`\n"
        self.sync(gh)
        self.assertEqual(json.loads(self.led.item("x", 5)["files"]), ["a.py", "c.py"])

    def test_missing_files_line_stores_empty_list(self):
        gh = FakeGH({5: {"title": "An issue", "labels": ["mahler:ready"],
                        "body": "## Plan\nSteps:\n- x\n", "comments": []}})
        self.sync(gh)
        self.assertEqual(json.loads(self.led.item("x", 5)["files"]), [])


class FilesOfParsingTests(unittest.TestCase):
    """mahler#210: the `## Plan` section's `Files:` list, parsed mechanically
    so the scheduler can block file-overlapping builds without relying on
    the sort agent to notice and hand-label an `area:` collision."""

    def test_inline_comma_separated(self):
        from mahler.gh import files_of
        body = "## Plan\nFiles: `mahler/tick.py`, `mahler/gh.py`\nSteps:\n- do it\n## Done when\n"
        self.assertEqual(files_of(body), ["mahler/tick.py", "mahler/gh.py"])

    def test_inline_wrapped_list(self):
        from mahler.gh import files_of
        body = "## Plan\nFiles: `state.py`, `page.py`, `console.js`,\n`console.css`, `tests/test_console.py`\nSteps:\n"
        self.assertEqual(files_of(body), ["state.py", "page.py", "console.js", "console.css", "tests/test_console.py"])

    def test_inline_prose_on_next_line_is_not_swallowed(self):
        from mahler.gh import files_of
        body = "## Plan\nFiles: `a.py`, `b.py`\n1. Do the thing\n"
        self.assertEqual(files_of(body), ["a.py", "b.py"])

    def test_inline_wrapped_list_stops_at_blank_line(self):
        from mahler.gh import files_of
        body = "## Plan\nFiles: `a.py`,\n\n`b.py`\n"
        self.assertEqual(files_of(body), ["a.py"])

    def test_bullet_list_under_bare_files_label(self):
        from mahler.gh import files_of
        body = (
            "## Plan\n"
            "Files:\n"
            "- `mahler/tick.py`\n"
            "- `mahler/gh.py`\n"
            "\n"
            "Steps:\n"
            "- do it\n"
            "## Done when\n"
        )
        self.assertEqual(files_of(body), ["mahler/tick.py", "mahler/gh.py"])

    def test_bold_files_label(self):
        from mahler.gh import files_of
        body = "## Plan\n**Files:** `a.py`\nSteps:\n- x\n## Done when\n"
        self.assertEqual(files_of(body), ["a.py"])

    def test_backtick_paths_with_trailing_parentheticals(self):
        from mahler.gh import files_of
        body = ("## Plan\nFiles:\n- `a/b.kt`\n"
                "- `a/c.kts` (already present)\n"
                "- `x/YouTubeApi.kt` (new, added by #232)\n")
        self.assertEqual(files_of(body), ["a/b.kt", "a/c.kts", "x/YouTubeApi.kt"])

    def test_files_label_with_extra_words(self):
        from mahler.gh import files_of
        for label in ("**Files to change:**", "Files touched:", "- **Files:**"):
            with self.subTest(label=label):
                body = f"## Plan\n{label} `x/A.swift`, `x/B.swift`\n"
                self.assertEqual(files_of(body), ["x/A.swift", "x/B.swift"])

    def test_files_label_requires_colon(self):
        from mahler.gh import files_of
        for label in ("Files", "**Files**", "Files to change", "Files `a.py`"):
            with self.subTest(label=label):
                self.assertEqual(files_of(f"## Plan\n{label}\n- `a.py`\n"), [])

    def test_prose_bullet_is_dropped_without_losing_clean_paths(self):
        from mahler.gh import files_of
        body = ("## Plan\nFiles:\n"
                "- and update `README.md` only if the count changes.\n"
                "- `mahler/gh.py`\n")
        self.assertEqual(files_of(body), ["mahler/gh.py"])

    def test_path_wrappers_and_invalid_tokens(self):
        from mahler.gh import files_of
        body = ("## Plan\nFiles: 'a.py', \"b.py\", **c.py**, **`d.py`**, "
                "src/module, none, n/a, to change, README, `bad path.py`, "
                "bad\tpath.py, `unclosed.py, stray`tick.py, Start with `e.py`\n")
        self.assertEqual(files_of(body), ["a.py", "b.py", "c.py", "d.py", "src/module"])

    def test_no_plan_section_returns_empty(self):
        from mahler.gh import files_of
        self.assertEqual(files_of("## Problem\nNo plan here"), [])
        self.assertEqual(files_of(None), [])

    def test_plan_section_without_files_line_returns_empty(self):
        from mahler.gh import files_of
        body = "## Plan\nSteps:\n- do it\n## Done when\n"
        self.assertEqual(files_of(body), [])

    def test_none_placeholder_yields_no_files(self):
        from mahler.gh import files_of
        body = "## Plan\nFiles: none\nSteps:\n- x\n## Done when\n"
        self.assertEqual(files_of(body), [])

    def test_only_reads_the_plan_sections_own_files_line(self):
        """A `Files:` mention outside `## Plan` must not leak in."""
        from mahler.gh import files_of
        body = "## Context\nFiles: `unrelated.py`\n## Plan\nSteps:\n- x\n## Done when\n"
        self.assertEqual(files_of(body), [])


class SubIssueScopeTests(unittest.TestCase):
    def test_part_of_parsing(self):
        from mahler.gh import part_of
        self.assertEqual(part_of("Part of #16"), 16)
        self.assertEqual(part_of("part of #16"), 16)
        self.assertEqual(part_of("Part of: #42"), 42)
        self.assertEqual(part_of("**Part of:** #107"), 107)
        self.assertEqual(part_of("  Part of #5\nSome details"), 5)
        self.assertIsNone(part_of("No parent here"))
        self.assertIsNone(part_of(None))

    def test_sub_issues_inherit_scope_label(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["defaults"]["scope"] = "label"
        cfg["defaults"]["scope_label"] = "mahler"
        cfg["projects"]["proj"] = {"path": tmp.name, "repo": "x/y"}

        # Numbered out of chain order on purpose: the grandchild (#1) is
        # discovered before its child (#2) resolves into scope, so a single
        # pass over the issues can't find the chain — only fixed-point
        # iteration (inherit, then re-scan) can. A monotonically increasing
        # numbering here would pass even without that iteration.
        # Issue 3 has the mahler label.
        # Issue 2 has no labels, but body says Part of #3.
        # Issue 1 has no labels, but body says Part of #2.
        # Issue 4 has no labels and no Part of line.
        issues = {
            3: {"title": "Parent task", "labels": ["mahler"], "body": "Parent"},
            2: {"title": "Child task", "labels": [], "body": "Part of #3\nDo step 1"},
            1: {"title": "Grandchild task", "labels": [], "body": "**Part of:** #2\nDo step 2"},
            4: {"title": "Unrelated backlog issue", "labels": [], "body": "Not in mahler"},
        }
        gh = FakeGH(issues)
        led = Ledger(":memory:", clock=lambda: NOW)
        ctx = scheduler.Ctx(cfg, led)

        with mock.patch.object(ctx, "gh", return_value=gh):
            sync.sync(ctx, "proj")

        # 1, 2, and 3 should be synced into the ledger
        self.assertIsNotNone(led.item("proj", 1))
        self.assertIsNotNone(led.item("proj", 2))
        self.assertIsNotNone(led.item("proj", 3))
        # 4 should not be in the ledger
        self.assertIsNone(led.item("proj", 4))

        # GitHub issues 1 and 2 should have gained the 'mahler' label
        self.assertIn("mahler", gh.issues[1]["labels"])
        self.assertIn("mahler", gh.issues[2]["labels"])
        self.assertNotIn("mahler", gh.issues[4]["labels"])

    def test_sub_issue_inherits_scope_from_closed_parent_in_ledger(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["defaults"]["scope"] = "label"
        cfg["defaults"]["scope_label"] = "mahler"
        cfg["projects"]["proj"] = {"path": tmp.name, "repo": "x/y"}

        led = Ledger(":memory:", clock=lambda: NOW)
        # Parent issue 10 is already completed in ledger
        led.upsert_item("proj", 10, state="done", title="Completed parent", priority=2)

        # Child issue 20 arrives referencing #10
        issues = {
            20: {"title": "Followup sub-issue", "labels": [], "body": "Part of #10"},
        }
        gh = FakeGH(issues)
        ctx = scheduler.Ctx(cfg, led)

        with mock.patch.object(ctx, "gh", return_value=gh):
            sync.sync(ctx, "proj")

        self.assertIsNotNone(led.item("proj", 20))
        self.assertIn("mahler", gh.issues[20]["labels"])


class PlannedChildTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["projects"]["proj"] = {"path": tmp.name, "repo": "x/y"}
        self.cfg["defaults"]["settle_minutes"] = 0
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)
        self.ctx = scheduler.Ctx(self.cfg, self.led)

    def sync(self, issues):
        gh = FakeGH(issues)
        with mock.patch.object(self.ctx, "gh", return_value=gh):
            sync.sync(self.ctx, "proj")
        return gh

    def test_planned_sub_issue_is_born_ready_without_a_sort(self):
        self.led.upsert_item("proj", 5, state="parent", title="Parent")
        issues = {
            6: {"title": "Planned child",
                "labels": ["type:feature", "size:s", "p2"],
                "body": "Part of #5\n\n## Plan\nChange one file.\n\n"
                        "## Done when\nThe test passes."},
        }
        self.sync(issues)

        item = self.led.item("proj", 6)
        self.assertEqual(item["state"], "ready")
        self.assertEqual(item["sorted_at"], iso(NOW))
        self.assertEqual(item["parent"], 5)
        event = self.led.q("SELECT detail FROM events WHERE kind='state'")[-1]
        self.assertIn("born ready (planned under #5)", event["detail"])
        self.assertNotIn("proj#6: started sort", "\n".join(self.ctx.lines))
        candidates = tick._candidates(self.ctx, [self.ctx.policy("proj")])
        self.assertEqual([(it["number"], role) for _, role, it in candidates],
                         [(6, "build")])

    def test_incomplete_sub_issue_stays_in_inbox(self):
        cases = [
            ("missing plan", "size:s",
             "Part of #5\n\n## Done when\nThe test passes.", "parent"),
            ("large size", "size:l",
             "Part of #5\n\n## Plan\nChange one file.\n\n"
             "## Done when\nThe test passes.", "parent"),
            ("wrong parent state", "size:s",
             "Part of #5\n\n## Plan\nChange one file.\n\n"
             "## Done when\nThe test passes.", "ready"),
        ]
        for name, size, body, parent_state in cases:
            with self.subTest(name=name):
                self.led = Ledger(":memory:", clock=lambda: NOW)
                self.ctx = scheduler.Ctx(self.cfg, self.led)
                self.led.upsert_item("proj", 5, state=parent_state, title="Parent")
                self.sync({6: {"title": "Child",
                               "labels": ["type:feature", size, "p2"],
                               "body": body}})
                item = self.led.item("proj", 6)
                self.assertEqual(item["state"], "inbox")
                self.assertIsNone(item["sorted_at"])

    def test_has_sections_matches_markdown_headings_case_insensitively(self):
        body = "## plan\nbody\n## DONE WHEN\nchecks"
        self.assertTrue(has_sections(body, "## Plan", "Done when"))
        self.assertFalse(has_sections(body, "Context"))


class SatisfiableDependsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["projects"]["proj"] = {"path": tmp.name, "repo": "x/y", "enabled": True}
        self.cfg["defaults"]["settle_minutes"] = 0
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)
        self.ctx = scheduler.Ctx(self.cfg, self.led)

    def sync_body(self, body, extra=None, blocked_by=()):
        issues = {5: {"title": "Child", "labels": ["mahler:ready"], "body": body,
                      "blocked_by": list(blocked_by)}}
        issues.update(extra or {})
        gh = FakeGH(issues)
        with mock.patch.object(self.ctx, "gh", return_value=gh):
            sync.sync(self.ctx, "proj")
        self.assertEqual(gh.issues[5]["body"], body)
        return json.loads(self.led.item("proj", 5)["depends"])

    def test_parent_is_removed_and_correction_logged_once(self):
        body = "Part of #10\nDepends on: #10, #10"
        self.assertEqual(self.sync_body(body), [])
        self.assertEqual(self.sync_body(body), [])
        lines = [line for line in self.ctx.lines if "dropped depends" in line]
        self.assertEqual(lines, ["proj#5: dropped depends on proj#10 — "
                                 "it is this item's own parent"])
        candidates = tick._candidates(self.ctx, [self.ctx.policy("proj")])
        self.assertEqual([it["number"] for _, _, it in candidates], [5])

    def test_sibling_survives_and_still_blocks_until_done(self):
        self.led.upsert_item("proj", 11, parent=10, state="working")
        self.assertEqual(self.sync_body("Part of #10\nDepends on: #10, #11"), [11])
        self.assertEqual(tick._candidates(self.ctx, [self.ctx.policy("proj")]), [])
        self.led.set_state("proj", 11, "done", "test")
        self.assertEqual([it["number"] for _, _, it in
                          tick._candidates(self.ctx, [self.ctx.policy("proj")])], [5])

    def test_native_blockers_merge_with_body_dependencies_and_deduplicate(self):
        self.assertEqual(self.sync_body("Depends on: #11, #12", blocked_by=[12, 13]),
                         [11, 12, 13])

    def test_native_self_and_ancestor_blockers_are_removed(self):
        self.assertEqual(self.sync_body("Part of #10", blocked_by=[5, 10, 11]), [11])

    def test_self_is_removed(self):
        self.assertEqual(self.sync_body("Depends on: #5"), [])
        self.assertIn("it is the item itself", "\n".join(self.ctx.lines))

    def test_cached_grandparent_is_removed(self):
        self.led.upsert_item("proj", 10, parent=20, state="parent")
        self.assertEqual(self.sync_body("Part of #10\nDepends on: #20"), [])
        self.assertIn("proj#20 — it is an ancestor", "\n".join(self.ctx.lines))

    def test_current_parents_override_cache_and_issue_order(self):
        self.led.upsert_item("proj", 10, parent=30, state="parent")
        extra = {10: {"title": "Parent", "labels": ["mahler:parent"],
                      "body": "Part of #20"}}
        self.assertEqual(self.sync_body("Part of #10\nDepends on: #20, #30", extra), [30])

    def test_cycles_terminate_and_preserve_unrelated_dependencies(self):
        for parents in ({10: 10}, {10: 20, 20: 10}, {10: 5}):
            with self.subTest(parents=parents):
                for number, parent in parents.items():
                    self.led.upsert_item("proj", number, parent=parent, state="parent")
                self.assertEqual(self.sync_body("Part of #10\nDepends on: #10, #99"), [99])

    def test_qualified_local_refs_removed_but_other_repos_kept(self):
        self.cfg["projects"]["other"] = {"path": self.cfg["projects"]["proj"]["path"],
                                          "repo": "x/other", "enabled": True}
        self.assertEqual(self.sync_body(
            "Part of #10\nDepends on: x/y#10, y#5, x/other#10, unknown#5"),
            [{"repo": "x/other", "number": 10}, {"repo": "unknown", "number": 5}])

    def test_ambiguous_qualified_ref_is_preserved(self):
        self.cfg["projects"]["other"] = {"path": self.cfg["projects"]["proj"]["path"],
                                          "repo": "other/y", "enabled": True}
        self.assertEqual(self.sync_body("Part of #10\nDepends on: y#10"),
                         [{"repo": "y", "number": 10}])

    def test_walk_has_a_fixed_bound(self):
        parents = {n: n + 1 for n in range(10, 1000)}
        with mock.patch.object(self.led, "item", wraps=self.led.item) as item:
            self.assertEqual(sync._satisfiable_depends(
                self.ctx, "proj", 5, 10, [10, 999], parents), [999])
        item.assert_not_called()


class MirrorLabelsTests(unittest.TestCase):
    """mirror_labels writes Mahler's state label back to GitHub each tick."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["projects"]["x"] = {"path": tmp.name, "repo": "x/y"}
        self.gh = FakeGH({5: {"title": "An issue", "labels": ["mahler:inbox"],
                              "comments": []}})
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)
        self.ctx = scheduler.Ctx(self.cfg, self.led)
        with mock.patch.object(self.ctx, "gh", return_value=self.gh):
            sync.sync(self.ctx, "x")            # item exists, state=inbox, mirror unset
        self.led.set_state("x", 5, "ready", "you said go")

    def mirror(self):
        with mock.patch.object(self.ctx, "gh", return_value=self.gh):
            sync.mirror_labels(self.ctx, "x")

    def test_label_update_failure_is_recorded_and_mirror_stays_stale(self):
        self.gh.fail_state_label = True
        self.mirror()   # must not raise
        self.assertIsNone(self.led.item("x", 5)["mirror"])
        self.assertIn("label update failed", "\n".join(self.ctx.lines))

    def test_a_later_successful_mirror_recovers(self):
        self.gh.fail_state_label = True
        self.mirror()
        self.gh.fail_state_label = False
        self.mirror()
        self.assertEqual(self.led.item("x", 5)["mirror"], "mahler:ready")


class SortRecipeTests(unittest.TestCase):
    def test_recipe_includes_plan_and_no_split_rule(self):
        rendered = prompt.render("sort", number=6, repo="x/y", title="Child", rules="")
        self.assertIn("## Plan", rendered)
        self.assertIn("Files:", rendered)
        self.assertIn("ordered steps", rendered)
        self.assertIn("what proves it", rendered)
        self.assertIn("Do not split it", rendered)
        self.assertIn("Part of #N", rendered)


if __name__ == "__main__":
    unittest.main()
