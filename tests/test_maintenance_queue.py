import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler import config, scheduler, tick
from mahler.ledger import Ledger, iso
from mahler.gh import GHError

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)

def proj(**over):
    pol = {"name": "mahler", "enabled": True, "repo": "mkny13/mahler", "path": "/tmp"}
    pol.update(over)
    return pol

class MaintenanceQueueTests(unittest.TestCase):
    def setUp(self):
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)
        self.cfg = {"defaults": {}, "projects": {"mahler": proj()}}
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
        self.gh_mock = mock.Mock()
        self.ctx._gh["mkny13/mahler"] = self.gh_mock
        
        # Make all passes NOT due
        for pass_name in config.MAINTENANCE_PASSES:
            self.led.set_maintenance_checkpoint("mahler", pass_name, last_filed_at=NOW)
        # Make security due
        self.led.set_maintenance_checkpoint("mahler", "security", 
                                            last_filed_at=NOW - timedelta(days=40))

    def make_health_due(self):
        self.led.set_maintenance_checkpoint(
            "mahler", "health", last_filed_at=NOW - timedelta(days=40),
            merged_since=25)

    def test_only_one_due_pass_files_and_other_waits_until_closed(self):
        self.make_health_due()
        before = {name: self.led.maintenance_checkpoint("mahler", name)
                  for name in config.MAINTENANCE_PASSES}
        tick.queue_maintenance(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_called_once()
        changed = [name for name, cp in before.items()
                   if self.led.maintenance_checkpoint("mahler", name) != cp]
        self.assertEqual(changed, ["security"])
        self.assertTrue(self.led.maintenance_due("mahler", "health"))

        # Re-entry before sync must also respect this tick's filing.
        tick.queue_maintenance(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_called_once()

        # Next tick's sync sees the open issue; health remains due.
        self.led.upsert_item("mahler", 99, labels=json.dumps(["pass:security"]),
                             state="ready")
        next_ctx = scheduler.Ctx(self.cfg, self.led)
        next_ctx._gh["mkny13/mahler"] = self.gh_mock
        tick.queue_maintenance(next_ctx, [proj()])
        self.gh_mock.create_issue.assert_called_once()
        self.assertTrue(self.led.maintenance_due("mahler", "health"))

        self.led.set_state("mahler", 99, "done")
        tick.queue_maintenance(next_ctx, [proj()])
        self.assertEqual(self.gh_mock.create_issue.call_count, 2)
        self.assertIn("pass:health", self.gh_mock.create_issue.call_args.args[2])

    def test_multiple_due_dry_run_names_one_and_preserves_checkpoints(self):
        self.make_health_due()
        before = {name: self.led.maintenance_checkpoint("mahler", name)
                  for name in config.MAINTENANCE_PASSES}
        self.ctx.dry_run = True
        tick.queue_maintenance(self.ctx, [proj()])
        self.assertEqual(self.ctx.lines, ["mahler: queuing security pass"])
        self.gh_mock.create_issue.assert_not_called()
        self.assertEqual(before, {
            name: self.led.maintenance_checkpoint("mahler", name)
            for name in config.MAINTENANCE_PASSES})

    def test_filing_failure_allows_next_due_pass(self):
        self.make_health_due()
        before = self.led.maintenance_checkpoint("mahler", "security")
        self.gh_mock.create_issue.side_effect = [GHError("unavailable"), 100]
        tick.queue_maintenance(self.ctx, [proj()])
        self.assertEqual(self.gh_mock.create_issue.call_count, 2)
        self.assertEqual(before, self.led.maintenance_checkpoint("mahler", "security"))
        self.assertEqual(self.ctx.passes_filed, {"mahler"})
        self.assertIn("pass:health", self.gh_mock.create_issue.call_args.args[2])

    def test_one_filing_per_project(self):
        other = proj(name="other", repo="mkny13/other")
        self.cfg["projects"]["other"] = other
        other_gh = mock.Mock()
        self.ctx._gh["mkny13/other"] = other_gh
        tick.queue_maintenance(self.ctx, [proj(), other])
        self.gh_mock.create_issue.assert_called_once()
        other_gh.create_issue.assert_called_once()
        self.assertEqual(self.ctx.passes_filed, {"mahler", "other"})

    def test_every_pass_has_maintenance_text(self):
        # regression guard: tick.MAINTENANCE_TEXT[pass_name] is a plain dict
        # lookup with no default — a name in config.MAINTENANCE_PASSES without
        # a matching entry here would KeyError the first time it's due.
        for pass_name in config.MAINTENANCE_PASSES:
            self.assertIn(pass_name, tick.MAINTENANCE_TEXT)
            title, body = tick.MAINTENANCE_TEXT[pass_name]
            self.assertTrue(title)
            self.assertTrue(body)

    def test_files_due_pass_and_resets(self):
        tick.queue_maintenance(self.ctx, [proj()])
        
        self.gh_mock.ensure_pass_label.assert_called_once_with("security")
        self.gh_mock.create_issue.assert_called_once()
        args, kwargs = self.gh_mock.create_issue.call_args
        self.assertEqual(args[0], "Security & Surface Area Audit")
        self.assertIn("type:chore", args[2])
        self.assertIn("pass:security", args[2])
        
        # Checkpoint is reset
        cp = self.led.maintenance_checkpoint("mahler", "security")
        self.assertEqual(iso(NOW), cp["last_filed_at"])

    def test_dry_run_never_files(self):
        self.ctx.dry_run = True
        tick.queue_maintenance(self.ctx, [proj()])
        
        self.gh_mock.create_issue.assert_not_called()
        self.gh_mock.ensure_pass_label.assert_not_called()
        
        # Checkpoint NOT reset
        cp = self.led.maintenance_checkpoint("mahler", "security")
        self.assertNotEqual(iso(NOW), cp["last_filed_at"])

    def test_due_but_already_open(self):
        # Open issue with pass:security
        self.led.upsert_item("mahler", 99, labels=json.dumps(["pass:security"]), state="ready")
        tick.queue_maintenance(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_not_called()

    def test_due_but_in_cooldown(self):
        # Closed issue with pass:security, closed 5 days ago
        self.led.upsert_item("mahler", 99, labels=json.dumps(["pass:security"]))
        self.led.set_state("mahler", 99, "done")
        # set_state overwrites state_changed_at, fix it:
        self.led.upsert_item("mahler", 99, state_changed_at=iso(NOW - timedelta(days=5)))
        
        tick.queue_maintenance(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_not_called()

    def test_due_and_clear_of_cooldown(self):
        # Closed issue with pass:security, closed 20 days ago (cooldown default 14)
        self.led.upsert_item("mahler", 99, labels=json.dumps(["pass:security"]))
        self.led.set_state("mahler", 99, "done")
        self.led.upsert_item("mahler", 99, state_changed_at=iso(NOW - timedelta(days=20)))
        
        tick.queue_maintenance(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_called_once()

    def test_not_due(self):
        self.led.set_maintenance_checkpoint("mahler", "security", 
                                            last_filed_at=NOW - timedelta(days=10))
        tick.queue_maintenance(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_not_called()

    def test_pass_issue_carries_scope_label(self):
        """D20: when scope=label, the filed pass issue carries the project's
        scope_label so Mahler's sync recognises it as in-scope."""
        self.cfg = {"defaults": {}, "projects": {"mahler": proj(scope="label", scope_label="project-scope")}}
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
        self.gh_mock = mock.Mock()
        self.ctx._gh["mkny13/mahler"] = self.gh_mock

        tick.queue_maintenance(self.ctx, [proj(scope="label", scope_label="project-scope")])

        self.gh_mock.create_issue.assert_called_once()
        args, kwargs = self.gh_mock.create_issue.call_args
        self.assertIn("project-scope", args[2])

    def test_pass_issue_no_scope_label_when_scope_all(self):
        """When scope=all, no scope_label is added to the pass issue: the
        label list is exactly the base set, nothing extra appended."""
        tick.queue_maintenance(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_called_once()
        args, kwargs = self.gh_mock.create_issue.call_args
        self.assertEqual(args[2], ["type:chore", "size:l", "p2", "pass:security"])

    def test_pass_with_undone_parent_is_skipped(self):
        """A pass item whose parent (e.g. a sub-issue split) isn't done yet
        blocks re-filing that pass, even though the pass item itself is done."""
        self.led.upsert_item("mahler", 50, state="ready")   # parent: not done
        self.led.upsert_item("mahler", 99, labels=json.dumps(["pass:security"]),
                              parent=50)
        self.led.set_state("mahler", 99, "done")
        self.led.upsert_item("mahler", 99, state_changed_at=iso(NOW - timedelta(days=20)))

        tick.queue_maintenance(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_not_called()

    def test_one_pass_in_flight_blocks_all_passes(self):
        """D20: if any pass:* item is open, no new passes are filed."""
        self.led.upsert_item("mahler", 99, labels=json.dumps(["pass:health"]), state="working")
        tick.queue_maintenance(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_not_called()

    def test_open_pass_blocks_different_pass(self):
        """An open pass:health blocks filing the due pass:security."""
        self.led.upsert_item("mahler", 99, labels=json.dumps(["pass:health"]), state="ready")
        tick.queue_maintenance(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_not_called()

    def test_all_passes_done_allows_new_pass(self):
        """D20: only blocks while a pass is open; once all pass items are
        done, filing proceeds for due passes."""
        self.led.upsert_item("mahler", 99, labels=json.dumps(["pass:health"]))
        self.led.set_state("mahler", 99, "done")
        self.led.upsert_item("mahler", 99, state_changed_at=iso(NOW - timedelta(days=20)))

        tick.queue_maintenance(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_called_once()
        args, kwargs = self.gh_mock.create_issue.call_args
        self.assertIn("pass:security", args[2])

    def test_open_pass_blocks_dry_run_too(self):
        """The in-flight guard applies even in dry-run."""
        self.ctx.dry_run = True
        self.led.upsert_item("mahler", 99, labels=json.dumps(["pass:health"]), state="working")
        tick.queue_maintenance(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_not_called()

    def test_due_but_open_issue_with_matching_title_and_no_pass_label(self):
        """An open type:goal item with a title matching a pass title but no
        pass:* label blocks filing a duplicate pass (issue #204)."""
        self.led.upsert_item("mahler", 57, title="Security & Surface Area Audit",
                              labels=json.dumps(["type:goal"]), state="ready")
        tick.queue_maintenance(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_not_called()

    def test_open_issue_with_matching_title_case_and_whitespace_blocks_pass(self):
        """Title matching handles case and whitespace normalization."""
        self.led.upsert_item("mahler", 57, title="  security & surface area audit  ",
                              labels=json.dumps(["type:goal", "mahler:parent"]), state="working")
        tick.queue_maintenance(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_not_called()

    def test_open_issue_with_matching_title_of_another_pass_blocks_all_passes(self):
        """D20: an open manual issue matching Codebase Health blocks the due security pass."""
        self.led.upsert_item("mahler", 58, title="Codebase Health & Refactoring Pass",
                              labels=json.dumps(["type:goal"]), state="ready")
        tick.queue_maintenance(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_not_called()

    def test_closed_issue_with_matching_title_respects_cooldown(self):
        """A completed issue with a matching title respects the cooldown window."""
        self.led.upsert_item("mahler", 57, title="Security & Surface Area Audit",
                              labels=json.dumps(["type:goal"]))
        self.led.set_state("mahler", 57, "done")
        self.led.upsert_item("mahler", 57, state_changed_at=iso(NOW - timedelta(days=5)))

        tick.queue_maintenance(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_not_called()

    def test_closed_issue_with_matching_title_past_cooldown_allows_filing(self):
        """A completed issue with a matching title past cooldown allows filing."""
        self.led.upsert_item("mahler", 57, title="Security & Surface Area Audit",
                              labels=json.dumps(["type:goal"]))
        self.led.set_state("mahler", 57, "done")
        self.led.upsert_item("mahler", 57, state_changed_at=iso(NOW - timedelta(days=20)))

        tick.queue_maintenance(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_called_once()
        args, kwargs = self.gh_mock.create_issue.call_args
        self.assertIn("pass:security", args[2])

    def test_manual_pass_with_undone_parent_is_skipped(self):
        """A manual pass item whose parent is not done blocks re-filing that pass."""
        self.led.upsert_item("mahler", 50, state="ready")
        self.led.upsert_item("mahler", 57, title="Security & Surface Area Audit",
                              labels=json.dumps(["type:goal"]), parent=50)
        self.led.set_state("mahler", 57, "done")
        self.led.upsert_item("mahler", 57, state_changed_at=iso(NOW - timedelta(days=20)))

        tick.queue_maintenance(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_not_called()

if __name__ == "__main__":
    unittest.main()
