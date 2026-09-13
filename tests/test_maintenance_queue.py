import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler import config, scheduler
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

    def test_files_due_pass_and_resets(self):
        scheduler.queue_maintenance(self.ctx, [proj()])
        
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
        scheduler.queue_maintenance(self.ctx, [proj()])
        
        self.gh_mock.create_issue.assert_not_called()
        self.gh_mock.ensure_pass_label.assert_not_called()
        
        # Checkpoint NOT reset
        cp = self.led.maintenance_checkpoint("mahler", "security")
        self.assertNotEqual(iso(NOW), cp["last_filed_at"])

    def test_due_but_already_open(self):
        # Open issue with pass:security
        self.led.upsert_item("mahler", 99, labels=json.dumps(["pass:security"]), state="ready")
        scheduler.queue_maintenance(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_not_called()

    def test_due_but_in_cooldown(self):
        # Closed issue with pass:security, closed 5 days ago
        self.led.upsert_item("mahler", 99, labels=json.dumps(["pass:security"]))
        self.led.set_state("mahler", 99, "done")
        # set_state overwrites state_changed_at, fix it:
        self.led.upsert_item("mahler", 99, state_changed_at=iso(NOW - timedelta(days=5)))
        
        scheduler.queue_maintenance(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_not_called()

    def test_due_and_clear_of_cooldown(self):
        # Closed issue with pass:security, closed 20 days ago (cooldown default 14)
        self.led.upsert_item("mahler", 99, labels=json.dumps(["pass:security"]))
        self.led.set_state("mahler", 99, "done")
        self.led.upsert_item("mahler", 99, state_changed_at=iso(NOW - timedelta(days=20)))
        
        scheduler.queue_maintenance(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_called_once()

    def test_not_due(self):
        self.led.set_maintenance_checkpoint("mahler", "security", 
                                            last_filed_at=NOW - timedelta(days=10))
        scheduler.queue_maintenance(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_not_called()

if __name__ == "__main__":
    unittest.main()
