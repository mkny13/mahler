"""Capacity intervals (mahler#734): coalescing, splitting, gaps and retention.

Everything uses in-memory ledgers and a fake clock; nothing touches ~/.mahler.
"""

import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from mahler import capacity, config
from mahler.ledger import Ledger

T0 = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


class IntervalTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.led = Ledger(":memory:", clock=self.clock)
        self.addCleanup(self.led.close)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.lines = []
        self.ctx = SimpleNamespace(cfg=self.cfg, led=self.led, dry_run=False,
                                   say=self.lines.append)

    def tick(self, *entries, dry_run=False):
        """One tick's observations: (project, number, role, blockers[, diag])."""
        self.ctx.dry_run = dry_run
        obs = capacity.Observer(self.ctx)
        for e in entries:
            project, number, role, blockers, *rest = e
            obs.observe(project, number, role, blockers=blockers,
                        diag=rest[0] if rest else None, size="m", required_tier=1)
        obs.flush()
        return obs

    def rows(self):
        return [dict(r) for r in self.led.capacity_intervals()]

    def test_unchanged_ticks_keep_one_interval(self):
        for _ in range(5):
            self.tick(("a", 1, "build", ["no_platform"]))
            self.clock.advance(seconds=60)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["first_seen"], T0.isoformat())
        self.assertEqual(rows[0]["last_seen"], (T0 + timedelta(seconds=240)).isoformat())
        self.assertEqual(rows[0]["open"], 1)

    def test_changed_blockers_split_the_interval(self):
        self.tick(("a", 1, "build", ["no_platform"]))
        self.clock.advance(seconds=60)
        self.tick(("a", 1, "build", ["area"]))
        rows = self.rows()
        self.assertEqual([r["end_reason"] for r in rows], ["changed", None])
        self.assertEqual(json.loads(rows[1]["blockers"]), ["area"])

    def test_platform_reason_change_splits_but_config_snapshot_is_kept(self):
        name = next(iter(self.cfg["platforms"]))
        self.tick(("a", 1, "build", ["no_platform"], {name: ["quota"]}))
        self.clock.advance(seconds=60)
        self.cfg["platforms"][name]["tier"] = 9           # later config change
        self.tick(("a", 1, "build", ["no_platform"], {name: ["quota"]}))
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        meta = json.loads(rows[0]["platforms"])[name]
        self.assertNotEqual(meta["tier"], 9)               # history is not rewritten
        self.tick(("a", 1, "build", ["no_platform"], {name: ["no_credit"]}))
        self.assertEqual(len(self.rows()), 2)

    def test_disappearance_closes_at_last_seen(self):
        self.tick(("a", 1, "build", ["no_platform"]))
        self.clock.advance(seconds=60)
        self.tick(("a", 1, "build", ["no_platform"]))
        self.clock.advance(seconds=60)
        self.tick()
        (row,) = self.rows()
        self.assertEqual((row["open"], row["end_reason"]), (0, "gone"))
        self.assertEqual(row["last_seen"], (T0 + timedelta(seconds=60)).isoformat())

    def test_gap_over_120_seconds_is_unknown_not_waiting(self):
        self.tick(("a", 1, "build", ["no_platform"]))
        self.clock.advance(hours=3)                        # daemon was down
        self.tick(("a", 1, "build", ["no_platform"]))
        first, second = self.rows()
        self.assertEqual((first["first_seen"], first["last_seen"]),
                         (T0.isoformat(), T0.isoformat()))   # zero observed span
        self.assertEqual(first["end_reason"], "gap")
        self.assertEqual(second["first_seen"], (T0 + timedelta(hours=3)).isoformat())

    def test_exactly_120_seconds_still_coalesces(self):
        self.tick(("a", 1, "build", ["no_platform"]))
        self.clock.advance(seconds=120)
        self.tick(("a", 1, "build", ["no_platform"]))
        self.assertEqual(len(self.rows()), 1)

    def test_launch_closes_the_interval_with_the_run(self):
        self.tick(("a", 1, "build", ["no_platform"]))
        self.clock.advance(seconds=60)
        obs = capacity.Observer(self.ctx)
        obs.observe("a", 1, "build", blockers=["no_platform"])
        obs.launched("a", 1, "build", 77, "kilo")
        obs.flush()
        (row,) = self.rows()
        self.assertEqual((row["open"], row["end_reason"], row["launch_run_id"],
                          row["launched_platform"]), (0, "launched", 77, "kilo"))
        self.assertEqual(row["last_seen"], (T0 + timedelta(seconds=60)).isoformat())

    def test_launch_on_first_sight_records_a_closed_row(self):
        obs = capacity.Observer(self.ctx)
        obs.launched("a", 1, "fix", 5, "claude")
        obs.flush()
        (row,) = self.rows()
        self.assertEqual((row["role"], row["open"], row["launch_run_id"]), ("fix", 0, 5))

    def test_pause_is_its_own_closing_reason(self):
        self.tick(("a", 1, "build", ["no_platform"]))
        self.clock.advance(seconds=60)
        self.tick(("a", 1, "build", ["paused"]))
        self.assertEqual(self.rows()[0]["end_reason"], "paused")

    def test_dry_run_writes_nothing(self):
        self.tick(("a", 1, "build", ["no_platform"]), dry_run=True)
        self.assertEqual(self.rows(), [])

    def test_prune_drops_old_and_clamps_the_crossing_interval(self):
        self.tick(("a", 1, "build", ["x"]), ("a", 2, "build", ["x"]))
        self.clock.advance(seconds=60)
        self.tick(("a", 2, "build", ["x"]))           # #1 ends at T0, #2 at T0+60s
        self.clock.now = T0 + timedelta(days=90, seconds=30)
        self.led.capacity_prune(90)
        rows = self.rows()
        self.assertEqual([r["number"] for r in rows], [2])
        cutoff = (self.clock.now - timedelta(days=90)).isoformat()
        self.assertEqual(rows[0]["first_seen"], cutoff)
        self.assertEqual(rows[0]["last_seen"], (T0 + timedelta(seconds=60)).isoformat())

    def test_prune_keeps_an_interval_ending_exactly_on_the_cutoff(self):
        self.tick(("a", 1, "build", ["x"]))
        self.clock.now = T0 + timedelta(days=90)
        self.led.capacity_prune(90)
        self.assertEqual(len(self.rows()), 1)

    def test_flush_failure_is_reported_not_raised(self):
        obs = capacity.Observer(self.ctx)
        obs.observe("a", 1, "build", blockers=["x"])
        self.led.close()
        obs.flush()
        self.assertTrue(any("couldn't record capacity history" in l for l in self.lines))
        self.led = Ledger(":memory:", clock=self.clock)    # let cleanup close something

    def test_no_secrets_or_prompts_in_the_row(self):
        self.tick(("a", 1, "build", ["no_platform"]))
        self.assertEqual(set(self.rows()[0]), {
            "id", "project", "number", "role", "routing_role", "size", "effective_size",
            "required_tier", "first_seen", "last_seen", "open", "end_reason", "signature",
            "blockers", "platforms", "launch_run_id", "launched_platform"})


if __name__ == "__main__":
    unittest.main()
