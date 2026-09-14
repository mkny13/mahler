"""Tests for tier escalation on retries, overrun heuristics, and sizing calibration."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler import config, finalize, platforms, router, scheduler, ship, sync, tick
from mahler.ledger import Ledger, iso


class FakeGH:
    def __init__(self):
        self.comments = []

    def issue_state(self, n):
        return "OPEN"

    def comment(self, n, body):
        self.comments.append(body)


class StubCtx:
    def __init__(self, cfg, led, gh=None):
        self.cfg, self.led, self._gh = cfg, led, gh or FakeGH()
        self.lines, self.pings = [], []
        self.dry_run = False
        self.burst_lines = None

    def policy(self, project):
        return {"path": "/nonexistent", "base": "main", "run_timeout_minutes": 30,
                "progress_timeout_minutes": 15, "auto_lease_minutes": 30,
                "yield_grace_seconds": 30, "max_attempts": 5}

    def gh(self, project):
        return self._gh

    def say(self, msg):
        self.lines.append(msg)

    def ping(self, title, message="", project=None, number=None,
             priority="default", tags=""):
        self.pings.append((title, message, priority))


class TierEscalationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.log_path = os.path.join(d, "agent.log")
        with open(self.log_path, "w") as fh:
            fh.write("failed\n")
        self.status_path = os.path.join(d, "exit")
        with open(self.status_path, "w") as fh:
            fh.write("1")

        self.led = Ledger(":memory:")
        self.addCleanup(self.led.close)

        cfg = copy = dict(config.DEFAULTS)
        self.ctx = StubCtx(cfg, self.led)

    def tearDown(self):
        self.tmp.cleanup()

    def _end_run(self, project, number, platform, started_at=None):
        now = self.led.now()
        started = iso(now - timedelta(minutes=started_at)) if started_at else iso(now)
        run_id = self.led.create_run(
            project=project, number=number, role="build", platform=platform, epoch=1,
            log_path=self.log_path, status_path=self.status_path,
            worktree=self.tmp.name)
        self.led.con.execute("UPDATE runs SET started_at=? WHERE id=?", (started, run_id))
        finalize.finalize(self.ctx, self.led.run(run_id))

    def test_two_failures_on_tier_1_escalates_to_tier_2(self):
        # item starts at esc_tier=0, esc_fails=0
        self.led.upsert_item("p", 1, title="test item", labels=json.dumps(["size:s"]))
        item = self.led.item("p", 1)
        self.assertEqual(item["esc_tier"], 0)
        self.assertEqual(item["esc_fails"], 0)

        # 1st failure on kilo (tier 1)
        self._end_run("p", 1, "kilo")
        item = self.led.item("p", 1)
        self.assertEqual(item["esc_tier"], 0)
        self.assertEqual(item["esc_fails"], 1)

        # 2nd failure on kilo (tier 1) -> escalates to tier 2
        self._end_run("p", 1, "kilo")
        item = self.led.item("p", 1)
        self.assertEqual(item["esc_tier"], 2)
        self.assertEqual(item["esc_fails"], 0)

    def test_two_failures_on_tier_2_escalates_to_tier_3(self):
        self.led.upsert_item("p", 2, title="test item", labels=json.dumps(["size:m"]))
        # Fail twice on agy-claude (tier 2)
        self._end_run("p", 2, "agy-claude")
        self._end_run("p", 2, "agy-claude")
        item = self.led.item("p", 2)
        self.assertEqual(item["esc_tier"], 3)
        self.assertEqual(item["esc_fails"], 0)

    def test_overrun_heuristic_escalates_size_s_after_10_minutes_on_tier_1(self):
        self.led.upsert_item("p", 3, title="overrun task", labels=json.dumps(["size:s"]))
        # Failed run on kilo (tier 1) that ran for 12 minutes
        self._end_run("p", 3, "kilo", started_at=12)
        item = self.led.item("p", 3)
        self.assertEqual(item["esc_tier"], 2)
        self.assertEqual(item["esc_fails"], 0)

    def test_no_attempt_does_not_increment_esc_fails(self):
        self.led.upsert_item("p", 4, title="quota task", labels=json.dumps(["size:s"]))
        run_id = self.led.create_run(
            project="p", number=4, role="build", platform="kilo", epoch=1,
            log_path=self.log_path, status_path=self.status_path,
            worktree=self.tmp.name)
        self.led.update_run(run_id, stop_reason="quota")
        finalize.finalize(self.ctx, self.led.run(run_id))
        item = self.led.item("p", 4)
        self.assertEqual(item["esc_tier"], 0)
        self.assertEqual(item["esc_fails"], 0)

    def test_go_resets_esc_tier_and_esc_fails(self):
        self.led.upsert_item("p", 5, title="retry task", esc_tier=3, esc_fails=1)
        sync._apply_instruction(self.ctx, "p", self.led.item("p", 5), "go", None)
        item = self.led.item("p", 5)
        self.assertEqual(item["esc_tier"], 0)
        self.assertEqual(item["esc_fails"], 0)

    def test_schedule_respects_esc_tier(self):
        now = self.led.now()
        later = iso(now + timedelta(hours=6))
        for p in ("kilo", "agy-claude", "claude"):
            self.led.record_usage(p, "5h", 10.0, later)
            self.led.record_usage(p, "weekly", 10.0, later)

        # Task 6 has esc_tier=2, size:s
        self.led.upsert_item("p", 6, title="escalated task", state="ready",
                             labels=json.dumps(["size:s"]), esc_tier=2)

        # Schedule should pick agy-claude (tier 2), not kilo (tier 1)
        projects = [{"name": "p", "repo": "o/p", "max_parallel": 1, "path": self.tmp.name}]
        started = []
        with mock.patch("mahler.tick.start", side_effect=lambda *args, **kw: started.append(args[4]) or True), \
             mock.patch.object(platforms, "available", return_value=True):
            tick.schedule(self.ctx, projects)
        self.assertEqual(started, ["agy-claude"])

    def test_schedule_elevates_meta_programming_to_size_m_and_tier_2(self):
        now = self.led.now()
        later = iso(now + timedelta(hours=6))
        for p in ("kilo", "agy-claude"):
            self.led.record_usage(p, "5h", 10.0, later)
            self.led.record_usage(p, "weekly", 10.0, later)

        # Title mentions recipes/sort.md, labeled size:s
        self.led.upsert_item("p", 7, title="fix recipes/sort.md prompt", state="ready",
                             labels=json.dumps(["size:s"]))

        projects = [{"name": "p", "repo": "o/p", "max_parallel": 1, "path": self.tmp.name}]
        started = []
        with mock.patch("mahler.tick.start", side_effect=lambda *args, **kw: started.append(args[4]) or True), \
             mock.patch.object(platforms, "available", return_value=True):
            tick.schedule(self.ctx, projects)
        # Even though labeled size:s, risk_min_tier elevates min_tier to 2 and size to m,
        # so kilo (tier 1, max_size s) cannot take it, agy-claude takes it.
        self.assertEqual(started, ["agy-claude"])

    def test_red_ci_escalates_tier_after_two_failures(self):
        # Item starts at esc_tier=0, esc_fails=0
        self.led.upsert_item("p", 8, title="ci test", labels=json.dumps(["size:s"]), branch="b")
        # Record a previous run on kilo (tier 1)
        self.led.create_run(project="p", number=8, role="build", platform="kilo", epoch=1, status="ended")
        view = {"headRefName": "b", "headRefOid": "sha1"}
        item = self.led.item("p", 8)

        # 1st red CI
        with mock.patch("mahler.tick.start", return_value=True):
            ship._red_ci(self.ctx, "p", item, 10, view)
        item = self.led.item("p", 8)
        self.assertEqual(item["esc_tier"], 0)
        self.assertEqual(item["esc_fails"], 1)

        # 2nd red CI -> escalates to tier 2
        view2 = {"headRefName": "b", "headRefOid": "sha2"}
        with mock.patch("mahler.tick.start", return_value=True):
            ship._red_ci(self.ctx, "p", item, 10, view2)
        item = self.led.item("p", 8)
        self.assertEqual(item["esc_tier"], 2)
        self.assertEqual(item["esc_fails"], 0)


if __name__ == "__main__":
    unittest.main()
