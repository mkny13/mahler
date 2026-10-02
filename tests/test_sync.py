"""One-time capacity-pause migration must also run on unchanged GitHub polls."""

import copy
import json
import unittest
from datetime import datetime, timezone
from unittest import mock

from mahler import config, scheduler, sync
from mahler.ledger import Ledger


class CapacityMigrationTests(unittest.TestCase):
    def setUp(self):
        self.led = Ledger(":memory:", clock=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc))
        self.addCleanup(self.led.close)
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["projects"]["x"] = {"repo": "x/y", "path": "."}
        self.ctx = scheduler.Ctx(cfg, self.led, dry_run=False)
        self.gh = mock.Mock()
        self.gh.issues_changed.return_value = (False, "etag")
        self.ctx._gh["x/y"] = self.gh
        self.led.set_kv("depends_format:x", "4")
        self.led.set_kv("release_baseline_checked:x", "1")

    def test_304_migrates_only_known_capacity_questions_once(self):
        questions = {
            1: "No fix run could start for PR #88 after 120 minutes: no platform",
            2: "Please provide credentials",
            3: "Review waiting for your decision about the product",
            4: "No fix run could start for PR #99 after 120 minutes: no platform",
            5: "PR #88: CI still running after 120 min",
        }
        for n, question in questions.items():
            self.led.set_state("x", n, "needs_you", pr=88, question=question)
        sync.sync(self.ctx, "x")
        self.gh.open_issues.assert_not_called()
        self.assertEqual(json.loads(self.led.get_kv("capacity-stranded:x#1"))["role"], "fix")
        for n in (2, 3, 4, 5):
            self.assertFalse(self.led.get_kv(f"capacity-stranded:x#{n}"))
            self.assertEqual(self.led.item("x", n)["question"], questions[n])
        self.led.set_kv("capacity-stranded:x#1", "")
        sync.sync(self.ctx, "x")
        self.assertFalse(self.led.get_kv("capacity-stranded:x#1"))
        self.assertEqual(self.led.item("x", 1)["state"], "needs_you")

    def test_dry_run_does_not_consume_migration(self):
        self.ctx.dry_run = True
        sync.sync(self.ctx, "x")
        self.assertFalse(self.led.get_kv("capacity-stranded-migrated:x"))
