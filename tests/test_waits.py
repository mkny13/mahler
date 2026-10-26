"""Conductor-side waits (DESIGN D52, mahler#1260)."""
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from mahler import config, sync, waits
from mahler.ledger import Ledger, parse

T0 = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


class WaitTests(unittest.TestCase):
    def setUp(self):
        self.now = T0
        self.led = Ledger(":memory:", clock=lambda: self.now)
        self.addCleanup(self.led.close)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["projects"] = {"p": {"enabled": True, "repo": "x/p", "path": "/unused"}}
        self.pings, self.lines = [], []
        self.ctx = SimpleNamespace(
            cfg=self.cfg, led=self.led, dry_run=False, say=self.lines.append,
            policy=lambda project: config.project_policy(self.cfg, project),
            ping=lambda *a, **kw: self.pings.append((a, kw)))
        self.led.upsert_item("p", 1, title="t", state="verifying")

    def advance(self, **kw):
        self.now += timedelta(**kw)

    def record(self):
        return json.loads(self.led.get_kv("wait:p#1"))

    def hold(self, cause="capacity", **kw):
        waits.hold(self.led, "p", 1, cause, "because", resume=kw.pop("resume", "verifying"), **kw)

    def test_hold_sets_state_and_record_without_ping(self):
        self.hold()
        item = self.led.item("p", 1)
        self.assertEqual(item["state"], "waiting")
        rec = self.record()
        self.assertEqual((rec["cause"], rec["resume"], rec["checks"]), ("capacity", "verifying", 0))
        self.assertEqual(rec["bound_at"], item["state_changed_at"])
        self.assertEqual(self.pings, [])

    def test_unknown_cause_raises(self):
        with self.assertRaises(ValueError):
            waits.hold(self.led, "p", 1, "bogus", "x", resume="ready")
        self.assertEqual(self.led.item("p", 1)["state"], "verifying")

    def test_recheck_before_next_at_does_nothing(self):
        self.hold()
        self.advance(minutes=5)
        waits.recheck(self.ctx, "p")
        self.assertEqual(self.led.item("p", 1)["state"], "waiting")
        self.assertEqual(self.record()["checks"], 0)

    def test_time_wake_resumes_when_next_at_reached(self):
        self.hold(resume="ready")
        self.advance(minutes=15)
        waits.recheck(self.ctx, "p")
        self.assertEqual(self.led.item("p", 1)["state"], "ready")
        self.assertFalse(self.led.get_kv("wait:p#1"))

    def test_ci_repo_wide_stays_then_resumes_when_blocker_clears(self):
        self.hold("ci_repo_wide")
        self.led.set_kv("ci_blocker:p", json.dumps({"reason": "x"}))
        self.advance(minutes=15)
        waits.recheck(self.ctx, "p")
        self.assertEqual(self.led.item("p", 1)["state"], "waiting")
        self.assertEqual(self.record()["checks"], 1)
        self.led.set_kv("ci_blocker:p", "")
        self.advance(minutes=30)
        waits.recheck(self.ctx, "p")
        self.assertEqual(self.led.item("p", 1)["state"], "verifying")
        self.assertFalse(self.led.get_kv("wait:p#1"))

    def test_backoff_sequence(self):
        self.assertEqual([waits.backoff(i) for i in range(1, 9)],
                         [timedelta(minutes=m) for m in (15, 30, 60, 120, 240, 360, 360, 360)])
        self.hold("ci_repo_wide")
        self.led.set_kv("ci_blocker:p", json.dumps({"reason": "x"}))
        for expected in (30, 60, 120):
            self.advance(minutes=int(
                (parse(self.record()["next_at"]) - self.now).total_seconds() // 60))
            waits.recheck(self.ctx, "p")
            rec = self.record()
            self.assertEqual(parse(rec["next_at"]) - self.now, timedelta(minutes=expected))

    def test_stale_bound_at_gets_fresh_other_record(self):
        self.hold("ci_repo_wide")
        self.advance(minutes=1)
        self.led.set_state("p", 1, "waiting", "re-entered", question=None)
        self.advance(minutes=1)
        waits.recheck(self.ctx, "p")
        self.assertEqual(self.led.item("p", 1)["state"], "waiting")
        rec = self.record()
        self.assertEqual(rec["cause"], "other")
        self.assertEqual(rec["bound_at"], self.led.item("p", 1)["state_changed_at"])

    def test_missing_record_gets_fresh_other_record(self):
        self.led.set_state("p", 1, "waiting", "legacy")
        waits.recheck(self.ctx, "p")
        self.assertEqual(self.record()["cause"], "other")

    def test_backstop_escalates_once_with_one_ping(self):
        self.hold("github")
        self.advance(hours=72)
        waits.recheck(self.ctx, "p")
        item = self.led.item("p", 1)
        self.assertEqual(item["state"], "needs_you")
        self.assertIn("Waiting on github for 72h: because", item["question"])
        self.assertIn("[OPTIONS: retry now | park]", item["question"])
        self.assertFalse(self.led.get_kv("wait:p#1"))
        self.assertEqual(len(self.pings), 1)
        self.advance(hours=1)
        waits.recheck(self.ctx, "p")
        self.assertEqual(len(self.pings), 1)

    def test_go_on_waiting_item_resumes_ready_and_clears_record(self):
        self.hold()
        sync._apply_instruction(self.ctx, "p", self.led.item("p", 1), "go", None)
        self.assertEqual(self.led.item("p", 1)["state"], "ready")
        self.assertFalse(self.led.get_kv("wait:p#1"))

    def test_park_on_waiting_item_clears_record(self):
        self.hold()
        sync._apply_instruction(self.ctx, "p", self.led.item("p", 1), "park", None)
        self.assertEqual(self.led.item("p", 1)["state"], "parked")
        self.assertFalse(self.led.get_kv("wait:p#1"))

    def test_waiting_has_github_label(self):
        from mahler import gh
        self.assertEqual(gh.STATE_LABELS["waiting"], "mahler:waiting")
        self.assertIn("mahler:waiting", gh.LABEL_COLORS)


if __name__ == "__main__":
    unittest.main()
