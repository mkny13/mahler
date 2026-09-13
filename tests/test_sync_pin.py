"""`/mahler platform X` must stick (mahler#20).

sync() re-derives pin=pin_of(labels) on every tick, so the comment command
edits the platform:* labels on GitHub and mirrors the change into the ledger.
Everything here runs against an in-memory Ledger with the gh CLI replaced by
FakeGH: no GitHub, no subprocesses. FakeGH applies label edits to its stored
issues, so a further sync() sees them — that is the regression this file pins
down: a ledger-only pin used to be wiped by the next tick's upsert.
"""

import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler import config, scheduler
from mahler.gh import has_sections
from mahler.ledger import Ledger, iso
from mahler import runner

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


def t(minutes):
    return iso(NOW + timedelta(minutes=minutes))


class FakeGH:
    """In-memory stand-in for gh.GH. open_issues() feeds sync(); set_pin_labels
    edits the stored labels, so the next sync() reads the new pin back."""

    def __init__(self, issues=None):
        self.issues = issues or {}     # number -> {"title", "labels", "comments"}
        self.edits = []                # (number, want, current_labels)

    def open_issues(self):
        return [{"number": n, "title": i["title"], "body": i.get("body", ""),
                 "createdAt": t(-60), "updatedAt": t(0),
                 "url": f"https://github.com/x/y/issues/{n}",
                 "labels": [{"name": l} for l in i["labels"]],
                 "comments": [{"createdAt": at, "body": body}
                              for at, body in i.get("comments", [])]}
                for n, i in sorted(self.issues.items())]

    def add_label(self, number, label):
        if label not in self.issues[number]["labels"]:
            self.issues[number]["labels"].append(label)

    def issue_state(self, number):
        return "OPEN"

    def comment(self, number, body):
        pass

    def set_pin_labels(self, number, want, current_labels):
        self.edits.append((number, want, list(current_labels)))
        keep = f"platform:{want}" if want else None
        labels = [l for l in current_labels
                  if not l.startswith("platform:") or l == keep]
        if keep and keep not in labels:
            labels.append(keep)
        self.issues[number]["labels"] = labels


class PinTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["projects"]["x"] = {"path": tmp.name, "repo": "x/y"}
        self.gh = FakeGH({5: {"title": "An issue", "labels": ["mahler:inbox"],
                              "comments": []}})
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.ctx = scheduler.Ctx(self.cfg, self.led)
        self.sync()                     # the item exists, nothing pinned yet

    def sync(self):
        with mock.patch.object(self.ctx, "gh", return_value=self.gh):
            scheduler.sync(self.ctx, "x")

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

        # Issue 1 has the mahler label.
        # Issue 2 has no labels, but body says Part of #1.
        # Issue 3 has no labels, but body says Part of #2.
        # Issue 4 has no labels and no Part of line.
        issues = {
            1: {"title": "Parent task", "labels": ["mahler"], "body": "Parent"},
            2: {"title": "Child task", "labels": [], "body": "Part of #1\nDo step 1"},
            3: {"title": "Grandchild task", "labels": [], "body": "**Part of:** #2\nDo step 2"},
            4: {"title": "Unrelated backlog issue", "labels": [], "body": "Not in mahler"},
        }
        gh = FakeGH(issues)
        led = Ledger(":memory:", clock=lambda: NOW)
        ctx = scheduler.Ctx(cfg, led)

        with mock.patch.object(ctx, "gh", return_value=gh):
            scheduler.sync(ctx, "proj")

        # 1, 2, and 3 should be synced into the ledger
        self.assertIsNotNone(led.item("proj", 1))
        self.assertIsNotNone(led.item("proj", 2))
        self.assertIsNotNone(led.item("proj", 3))
        # 4 should not be in the ledger
        self.assertIsNone(led.item("proj", 4))

        # GitHub issues 2 and 3 should have gained the 'mahler' label
        self.assertIn("mahler", gh.issues[2]["labels"])
        self.assertIn("mahler", gh.issues[3]["labels"])
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
            scheduler.sync(ctx, "proj")

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
        self.ctx = scheduler.Ctx(self.cfg, self.led)

    def sync(self, issues):
        gh = FakeGH(issues)
        with mock.patch.object(self.ctx, "gh", return_value=gh):
            scheduler.sync(self.ctx, "proj")
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
        candidates = scheduler._candidates(self.ctx, [self.ctx.policy("proj")])
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


class SortRecipeTests(unittest.TestCase):
    def test_recipe_includes_plan_and_no_split_rule(self):
        rendered = runner.render("sort", number=6, repo="x/y", title="Child", rules="")
        self.assertIn("## Plan", rendered)
        self.assertIn("files to change", rendered)
        self.assertIn("ordered steps", rendered)
        self.assertIn("test that proves it", rendered)
        self.assertIn("Do not split it", rendered)
        self.assertIn("Part of #N", rendered)


if __name__ == "__main__":
    unittest.main()
