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
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
        self.gh_mock = mock.Mock()
        self.ctx._gh["mkny13/mahler"] = self.gh_mock

    def test_closes_parent_when_all_children_done(self):
        """Parent with two children, both done, is closed and becomes done."""
        # Parent issue
        self.led.upsert_item("mahler", 10, title="Parent goal", state="parent")
        # Children
        self.led.upsert_item("mahler", 11, title="Child 1", parent=10, state="done")
        self.led.upsert_item("mahler", 12, title="Child 2", parent=10, state="done")

        sync.close_finished_parents(self.ctx, [proj()])

        # Verify close_issue was called with correct comment
        self.gh_mock.close_issue.assert_called_once()
        args, kwargs = self.gh_mock.close_issue.call_args
        self.assertEqual(args[0], 10)
        self.assertIn("Sub-issues: #11, #12", kwargs["comment"])

        # Verify parent state is now done
        parent = self.led.item("mahler", 10)
        self.assertEqual(parent["state"], "done")

    def test_does_not_close_when_child_not_done(self):
        """With one child still ready, nothing happens."""
        self.led.upsert_item("mahler", 10, title="Parent goal", state="parent")
        self.led.upsert_item("mahler", 11, title="Child 1", parent=10, state="done")
        self.led.upsert_item("mahler", 12, title="Child 2", parent=10, state="ready")

        sync.close_finished_parents(self.ctx, [proj()])

        self.gh_mock.close_issue.assert_not_called()
        parent = self.led.item("mahler", 10)
        self.assertEqual(parent["state"], "parent")

    def test_does_not_close_when_no_children(self):
        """With no children, nothing happens."""
        self.led.upsert_item("mahler", 10, title="Parent goal", state="parent")

        sync.close_finished_parents(self.ctx, [proj()])

        self.gh_mock.close_issue.assert_not_called()
        parent = self.led.item("mahler", 10)
        self.assertEqual(parent["state"], "parent")

    def test_dry_run_only_reports(self):
        """In dry_run mode, only reports what it would do."""
        self.ctx.dry_run = True
        self.led.upsert_item("mahler", 10, title="Parent goal", state="parent")
        self.led.upsert_item("mahler", 11, title="Child 1", parent=10, state="done")
        self.led.upsert_item("mahler", 12, title="Child 2", parent=10, state="done")

        sync.close_finished_parents(self.ctx, [proj()])

        self.gh_mock.close_issue.assert_not_called()
        self.assertIn("would close", self.ctx.lines[0])

    def test_multiple_parents_handled_independently(self):
        """Multiple parents are handled independently."""
        self.led.upsert_item("mahler", 10, title="Parent 1", state="parent")
        self.led.upsert_item("mahler", 11, title="Child 1", parent=10, state="done")
        self.led.upsert_item("mahler", 12, title="Child 2", parent=10, state="done")

        self.led.upsert_item("mahler", 20, title="Parent 2", state="parent")
        self.led.upsert_item("mahler", 21, title="Child 3", parent=20, state="done")
        self.led.upsert_item("mahler", 22, title="Child 4", parent=20, state="ready")  # not done

        sync.close_finished_parents(self.ctx, [proj()])

        # Only parent 10 should be closed
        self.assertEqual(self.gh_mock.close_issue.call_count, 1)
        args, _ = self.gh_mock.close_issue.call_args
        self.assertEqual(args[0], 10)

        self.assertEqual(self.led.item("mahler", 10)["state"], "done")
        self.assertEqual(self.led.item("mahler", 20)["state"], "parent")

    def test_ignores_non_parent_items(self):
        """Items not in 'parent' state are ignored."""
        self.led.upsert_item("mahler", 10, title="Not a parent", state="ready")
        self.led.upsert_item("mahler", 11, title="Child", parent=10, state="done")

        sync.close_finished_parents(self.ctx, [proj()])

        self.gh_mock.close_issue.assert_not_called()

    def test_gh_error_does_not_break_other_parents(self):
        """GHError on one parent doesn't stop processing others."""
        self.led.upsert_item("mahler", 10, title="Parent 1", state="parent")
        self.led.upsert_item("mahler", 11, title="Child 1", parent=10, state="done")

        self.led.upsert_item("mahler", 20, title="Parent 2", state="parent")
        self.led.upsert_item("mahler", 21, title="Child 2", parent=20, state="done")

        self.gh_mock.close_issue.side_effect = [GHError("fail"), None]

        sync.close_finished_parents(self.ctx, [proj()])

        # Both should be attempted
        self.assertEqual(self.gh_mock.close_issue.call_count, 2)
        # First failed, second succeeded
        self.assertEqual(self.led.item("mahler", 10)["state"], "parent")
        self.assertEqual(self.led.item("mahler", 20)["state"], "done")


class SyncStoresParentTests(unittest.TestCase):
    def setUp(self):
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)
        self.cfg = {"defaults": {}, "projects": {"mahler": proj()}}
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
        self.gh_mock = mock.Mock()
        self.gh_mock.issues_changed.return_value = (True, None)
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