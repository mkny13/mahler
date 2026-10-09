"""Tests for closing parent issues when all sub-issues are done."""

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler import config, scheduler, sync, tick
from mahler.ledger import Ledger, iso
from mahler.gh import GHError

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


def proj(**over):
    pol = {"name": "mahler", "enabled": True, "repo": "mkny13/mahler", "path": "/tmp"}
    pol.update(over)
    return pol


class CloseFinishedParentsTests(unittest.TestCase):
    def setUp(self):
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)
        self.cfg = {"defaults": {}, "projects": {"mahler": proj()}}
        self.ctx = scheduler.Ctx(self.cfg, self.led)
        self.ctx.ping = mock.Mock()
        self.gh = mock.Mock()
        self.ctx._gh["mkny13/mahler"] = self.gh
        self.bodies = {10: "## Done when\n- [x] Works"}
        self.states = {10: "OPEN", 11: "CLOSED", 12: "CLOSED"}
        self.comments = {}
        self.gh.issue_body.side_effect = lambda n: self.bodies[n]
        self.gh.issue_state.side_effect = lambda n: self.states[n]
        self.gh.issue_comments.side_effect = lambda n: self.comments.get(n, [])
        self.gh.comment.side_effect = lambda n, body: self.comments.setdefault(n, []).append({"body": body})
        self.gh.close_issue.side_effect = lambda n: self.states.update({n: "CLOSED"})
        self.gh.parent_issue_inventory.return_value = []
        self.gh.sub_issues.return_value = []
        self.gh.closing_prs.return_value = [{"number": 99, "state": "MERGED"}]
        self.gh.pr_merge_info.return_value = {"state": "MERGED"}
        self.led.upsert_item("mahler", 10, state="parent")
        self.led.upsert_item("mahler", 11, parent=10, state="shipped", pr=99)

    def run_pass(self):
        sync.close_finished_parents(self.ctx, [proj()])

    def test_merged_shipped_child_completes_checked_parent(self):
        before = dict(self.led.item("mahler", 11))
        self.run_pass()
        self.assertEqual(self.led.item("mahler", 10)["state"], "done")
        self.assertEqual(dict(self.led.item("mahler", 11)), before)
        self.assertIn("#11: merged PR #99", self.comments[10][0]["body"])
        self.run_pass()
        self.gh.comment.assert_called_once()
        self.assertEqual(len(self.led.q("SELECT * FROM events WHERE number=10 AND kind='state'")), 1)

    def test_done_child_also_requires_merge(self):
        self.led.upsert_item("mahler", 11, state="done", pr=None)
        self.gh.closing_prs.return_value = []
        self.run_pass()
        self.gh.close_issue.assert_not_called()
        self.ctx.ping.assert_called_once()

    def test_open_or_reopened_child_waits_silently(self):
        self.states[11] = "OPEN"
        self.run_pass()
        self.gh.close_issue.assert_not_called()
        self.ctx.ping.assert_not_called()

    def test_relationship_union_and_exclusions(self):
        self.bodies[10] += "\n## Plan\nSteps:\n1. #12: native overlap\n- #13: historical\n" + \
            "- #10: self\n> - #90: original\n## Context\n- #91 incidental\n## Dependencies\n- #92 dep"
        self.gh.sub_issues.return_value = [12]
        self.gh.parent_issue_inventory.return_value = [
            {"number": 13, "body": "Part of #10"},
            {"number": 90, "body": "> Part of #10"}]
        self.states[13] = "CLOSED"
        self.run_pass()
        self.assertEqual(self.led.item("mahler", 10)["state"], "done")
        comment = self.comments[10][0]["body"]
        for n in (11, 12, 13):
            self.assertEqual(comment.count(f"#{n}:"), 1)
        self.assertNotIn("#90", comment)

    def test_part_of_only_historical_child(self):
        self.led.upsert_item("mahler", 11, parent=None)
        self.gh.parent_issue_inventory.return_value = [{"number": 12, "body": "Part of #10"}]
        self.run_pass()
        self.assertIn("#12: merged", self.comments[10][0]["body"])
        self.assertEqual(self.led.item("mahler", 10)["state"], "done")

    def test_native_only_child(self):
        self.led.upsert_item("mahler", 11, parent=None)
        self.gh.sub_issues.return_value = [12]
        self.run_pass()
        self.assertEqual(self.led.item("mahler", 10)["state"], "done")

    def test_goal_label_candidate(self):
        self.led.upsert_item("mahler", 10, state="ready", labels=json.dumps(["type:goal"]))
        self.run_pass()
        self.assertEqual(self.led.item("mahler", 10)["state"], "done")

    def test_checklist_and_empty_set_blockers(self):
        for body in ("", "## Done when\n- prose", "## Done when\n- [ ] Works"):
            with self.subTest(body=body):
                self.bodies[10] = body
                self.run_pass()
                self.gh.close_issue.assert_not_called()
        self.bodies[10] = "## Done when\n- [x] Works"
        self.led.upsert_item("mahler", 11, parent=None)
        self.run_pass()
        self.gh.close_issue.assert_not_called()
        self.assertIn("No child relationships", self.comments[10][-1]["body"])

    def test_blockers_deduplicate_after_restart_and_resolve(self):
        self.bodies[10] = "## Done when\n- [ ] exact criterion"
        self.run_pass()
        self.ctx = scheduler.Ctx(self.cfg, self.led)
        self.ctx._gh["mkny13/mahler"] = self.gh
        self.ctx.ping = mock.Mock()
        self.run_pass()
        self.gh.comment.assert_called_once()
        self.ctx.ping.assert_not_called()
        self.bodies[10] = "## Done when\n- [ ] different criterion"
        self.run_pass()
        self.ctx.ping.assert_called_once()
        self.bodies[10] = "## Done when\n- [x] different criterion"
        self.run_pass()
        self.assertEqual(self.led.item("mahler", 10)["state"], "done")

    def test_dry_run_has_no_writes(self):
        self.ctx.dry_run = True
        before = list(self.led.con.iterdump())
        self.run_pass()
        self.bodies[10] = ""
        self.run_pass()
        self.assertEqual(list(self.led.con.iterdump()), before)
        self.gh.comment.assert_not_called()
        self.gh.close_issue.assert_not_called()
        self.ctx.ping.assert_not_called()

    def test_discovery_failures_are_not_success(self):
        for method in ("parent_issue_inventory", "sub_issues", "issue_body", "issue_state"):
            with self.subTest(method=method):
                with mock.patch.object(self.gh, method, side_effect=GHError("incomplete")):
                    self.run_pass()
                self.gh.close_issue.assert_not_called()

    def test_partial_writes_recover_once(self):
        for operation in ("comment", "close_issue", "set_state"):
            with self.subTest(operation=operation):
                self.setUp()
                obj = self.led if operation == "set_state" else self.gh
                original = getattr(obj, operation)
                def ambiguous(*args, **kwargs):
                    original(*args, **kwargs)
                    raise GHError("response lost")
                with mock.patch.object(obj, operation, side_effect=ambiguous):
                    self.run_pass()
                self.run_pass()
                self.run_pass()
                self.assertEqual(len(self.comments[10]), 1)
                self.assertEqual(self.led.item("mahler", 10)["state"], "done")
                self.assertEqual(len(self.led.q("SELECT * FROM events WHERE number=10 AND kind='state'")), 1)

    def test_failed_parent_does_not_stop_second_parent(self):
        self.led.upsert_item("mahler", 20, state="parent")
        self.led.upsert_item("mahler", 12, parent=20, state="done")
        self.bodies[20] = self.bodies.pop(10)
        self.states[20] = "OPEN"
        self.run_pass()
        self.assertEqual(self.led.item("mahler", 20)["state"], "done")
        self.assertEqual(self.led.item("mahler", 10)["state"], "parent")

    def test_nested_and_cyclic_trackers_are_bounded(self):
        self.led.upsert_item("mahler", 20, state="parent", parent=10)
        self.led.upsert_item("mahler", 11, parent=20)
        self.bodies[20] = self.bodies[10]
        self.states[20] = "OPEN"
        self.run_pass()
        self.run_pass()
        self.assertEqual(self.led.item("mahler", 10)["state"], "done")
        self.assertIn("#20: recorded parent completion", self.comments[10][0]["body"])
        self.led.upsert_item("mahler", 10, state="parent", parent=20)
        self.led.upsert_item("mahler", 20, state="parent", parent=10)
        self.states.update({10: "OPEN", 20: "OPEN"})
        self.run_pass()
        self.assertEqual(self.led.item("mahler", 10)["state"], "parent")
        self.assertEqual(self.led.item("mahler", 20)["state"], "parent")


class SyncStoresParentTests(unittest.TestCase):
    def setUp(self):
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)
        self.cfg = {"defaults": {}, "projects": {"mahler": proj()}}
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
        self.gh_mock = mock.Mock()
        self.gh_mock.issues_changed.return_value = (True, None)
        self.gh_mock.blocked_by_of.return_value = []
        self.ctx._gh["mkny13/mahler"] = self.gh_mock

    def test_sync_stores_parent_from_body(self):
        """sync stores parent from 'Part of #N' body line."""
        # Mock open_issues to return an issue with "Part of #10" in body
        self.gh_mock.open_issues.return_value = [
            {
                "number": 11,
                "title": "Child issue",
                "labels": [],
                "body": "Part of #10\n\n## Plan\n...\n## Done when\n...",
                "createdAt": "2026-09-13T10:00:00Z",
                "updatedAt": "2026-09-13T10:00:00Z",
                "comments": [],
            }
        ]

        sync.sync(self.ctx, "mahler")

        item = self.led.item("mahler", 11)
        self.assertEqual(item["parent"], 10)

    def test_sync_handles_bold_part_of(self):
        """sync handles '**Part of #N**' (bold) variant."""
        self.gh_mock.open_issues.return_value = [
            {
                "number": 11,
                "title": "Child issue",
                "labels": [],
                "body": "**Part of #10**\n\n## Plan\n...\n## Done when\n...",
                "createdAt": "2026-09-13T10:00:00Z",
                "updatedAt": "2026-09-13T10:00:00Z",
                "comments": [],
            }
        ]

        sync.sync(self.ctx, "mahler")

        item = self.led.item("mahler", 11)
        self.assertEqual(item["parent"], 10)

    def test_sync_handles_blockquoted_part_of(self):
        """sync handles a 'Part of #N' line quoted under an '> **Original
        request:**' preamble (mahler#14/#64/#83's actual shape — this is what
        silently broke close_finished_parents for them)."""
        self.gh_mock.open_issues.return_value = [
            {
                "number": 11,
                "title": "Child issue",
                "labels": [],
                "body": "> **Original request:**\n"
                        "> Part of #10 (Some Goal)\n\n## Plan\n...\n## Done when\n...",
                "createdAt": "2026-09-13T10:00:00Z",
                "updatedAt": "2026-09-13T10:00:00Z",
                "comments": [],
            }
        ]

        sync.sync(self.ctx, "mahler")

        item = self.led.item("mahler", 11)
        self.assertEqual(item["parent"], 10)

    def test_sync_no_parent_when_no_part_of(self):
        """sync stores None when no 'Part of' line present."""
        self.gh_mock.open_issues.return_value = [
            {
                "number": 11,
                "title": "Standalone issue",
                "labels": [],
                "body": "Just a regular issue",
                "createdAt": "2026-09-13T10:00:00Z",
                "updatedAt": "2026-09-13T10:00:00Z",
                "comments": [],
            }
        ]

        sync.sync(self.ctx, "mahler")

        item = self.led.item("mahler", 11)
        self.assertIsNone(item["parent"])


class ParentColumnMigrationTests(unittest.TestCase):
    def test_parent_column_added_to_legacy_database(self):
        """Migration adds parent column to existing DB without it."""
        import os
        import sqlite3
        import tempfile
        from mahler.ledger import SCHEMA

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.db")
            # Create DB without parent column
            con = sqlite3.connect(path)
            # Use SCHEMA but filter out parent column
            schema_without_parent = "\n".join(
                l for l in SCHEMA.splitlines() if "parent" not in l.lower()
            )
            con.executescript(schema_without_parent)
            con.execute("INSERT INTO items (project, number, state) VALUES ('p', 1, 'ready')")
            con.commit()
            con.close()

            # Opening with Ledger should add the column
            led = Ledger(path)
            item = led.item("p", 1)
            self.assertEqual(item["state"], "ready")
            # Column should exist and be nullable
            led.upsert_item("p", 1, parent=5)
            item = led.item("p", 1)
            self.assertEqual(item["parent"], 5)


class MaintenanceQueueAfterParentCloseTests(unittest.TestCase):
    """Test that queue_maintenance files a pass again after parent closes and cooldown passes."""

    def setUp(self):
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)
        self.cfg = {"defaults": {}, "projects": {"mahler": proj()}}
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
        self.gh_mock = mock.Mock()
        self.ctx._gh["mkny13/mahler"] = self.gh_mock

        # Make all passes NOT due initially
        for pass_name in config.MAINTENANCE_PASSES:
            self.led.set_maintenance_checkpoint("mahler", pass_name, last_filed_at=NOW)
        # Make security due
        self.led.set_maintenance_checkpoint("mahler", "security",
                                            last_filed_at=NOW - timedelta(days=40))

    def test_queue_maintenance_after_parent_closes_and_cooldown(self):
        """After parent of a pass:security item closes and cooldown passes,
        queue_maintenance files that pass again."""
        # Create a pass:security item that's a child of a parent
        self.led.upsert_item("mahler", 10, title="Parent pass", state="parent")
        self.led.upsert_item("mahler", 11, title="Security pass", parent=10,
                            labels=json.dumps(["pass:security"]), state="done")
        self.led.upsert_item("mahler", 11, state_changed_at=iso(NOW - timedelta(days=20)))

        # Parent is still open - queue_maintenance should skip
        tick.queue_maintenance(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_not_called()

        # Now close the parent (simulate close_finished_parents)
        self.led.set_state("mahler", 10, "done", "all sub-issues done")

        # queue_maintenance should now file the pass
        tick.queue_maintenance(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_called_once()
        args, kwargs = self.gh_mock.create_issue.call_args
        self.assertIn("pass:security", args[2])

    def test_queue_maintenance_skips_if_cooldown_not_passed(self):
        """If cooldown hasn't passed, queue_maintenance doesn't file."""
        self.led.upsert_item("mahler", 10, title="Parent pass", state="parent")
        self.led.upsert_item("mahler", 11, title="Security pass", parent=10,
                            labels=json.dumps(["pass:security"]), state="done")
        # Closed only 5 days ago (cooldown default 14)
        self.led.upsert_item("mahler", 11, state_changed_at=iso(NOW - timedelta(days=5)))

        # Close parent
        self.led.set_state("mahler", 10, "done", "all sub-issues done")

        tick.queue_maintenance(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
