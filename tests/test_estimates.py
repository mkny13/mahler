"""Tests for time estimate tracking and periodic calibration (mahler#59)."""

import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from mahler.ledger import Ledger, iso
from mahler.finalize import _check_estimate_calibration


class Clock:
    def __init__(self):
        self.t = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


class EstimateTrackingTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.led = Ledger(":memory:", clock=self.clock)

    def test_schema_migration_adds_est_and_actual_mins(self):
        # Create an in-memory DB with older schema missing est_mins and actual_mins
        con = sqlite3.connect(":memory:")
        con.execute("""
            CREATE TABLE runs (
                id INTEGER PRIMARY KEY, project TEXT NOT NULL, number INTEGER NOT NULL,
                role TEXT NOT NULL, platform TEXT NOT NULL, epoch INTEGER NOT NULL,
                status TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT
            )
        """)
        con.close()

    def test_size_column_is_added_to_legacy_databases(self):
        # A database predating mahler#207 has no `size` column on runs.
        import os
        import tempfile
        from mahler.ledger import SCHEMA
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.db")
            con = sqlite3.connect(path)
            con.executescript("\n".join(
                l for l in SCHEMA.splitlines()
                if not l.strip().startswith("size ")))
            con.execute(
                "INSERT INTO runs (project, number, role, platform, epoch, status, started_at) "
                "VALUES ('p', 1, 'build', 'claude', 0, 'ended', '2026-09-12T12:00:00+00:00')")
            con.commit()
            con.close()
            led = Ledger(path)
            self.addCleanup(led.close)
            r = led.q1("SELECT * FROM runs WHERE project='p' AND number=1")
            self.assertIsNone(r["size"])

    def test_create_run_records_predicted_est_mins(self):
        # Default global fallback is 15.0 mins when no runs exist
        run_id = self.led.create_run(project="p", number=1, role="build", platform="claude", epoch=0)
        r = self.led.run(run_id)
        self.assertIsNotNone(r["est_mins"])
        self.assertEqual(r["est_mins"], 15.0)

        # Explicit est_mins passed
        run_id2 = self.led.create_run(project="p", number=2, role="sort", platform="claude",
                                      epoch=0, est_mins=4.5)
        r2 = self.led.run(run_id2)
        self.assertEqual(r2["est_mins"], 4.5)

    def test_create_run_stamps_size(self):
        run_id = self.led.create_run(project="p", number=1, role="build", platform="claude",
                                     epoch=0, size="m")
        r = self.led.run(run_id)
        self.assertEqual(r["size"], "m")

        run_id2 = self.led.create_run(project="p", number=2, role="build", platform="claude",
                                      epoch=0)
        r2 = self.led.run(run_id2)
        self.assertIsNone(r2["size"])

    def test_estimates_group_by_platform_role_size(self):
        t0 = self.clock()

        def seed(n, size, mins):
            self.led.create_run(
                project="p", number=n, role="build", platform="claude", epoch=0, size=size,
                status="ended", started_at=iso(t0), ended_at=iso(t0 + timedelta(minutes=mins)))

        # Three size:s runs averaging 5m, three size:l runs averaging 50m —
        # both buckets clear the count>=3 guard.
        for i, mins in enumerate((4, 5, 6)):
            seed(i, "s", mins)
        for i, mins in enumerate((45, 50, 55)):
            seed(100 + i, "l", mins)

        ests = self.led.estimates()
        self.assertAlmostEqual(ests["run_avg_size"][("claude", "build", "s")], 5.0, places=2)
        self.assertAlmostEqual(ests["run_avg_size"][("claude", "build", "l")], 50.0, places=2)

        self.assertAlmostEqual(self.led.run_estimate(ests, "claude", "build", "s"), 5.0, places=2)
        self.assertAlmostEqual(self.led.run_estimate(ests, "claude", "build", "l"), 50.0, places=2)
        # (platform, role) blend still reflects every run regardless of size
        self.assertAlmostEqual(self.led.run_estimate(ests, "claude", "build"),
                               (4 + 5 + 6 + 45 + 50 + 55) / 6, places=2)

    def test_thin_size_bucket_falls_back_to_platform_role(self):
        t0 = self.clock()
        # Only two size:s runs — short of the count>=3 guard used elsewhere
        # (by_pr/by_p), so the size bucket must not be trusted.
        for i, mins in enumerate((4, 6)):
            self.led.create_run(
                project="p", number=i, role="build", platform="claude", epoch=0, size="s",
                status="ended", started_at=iso(t0), ended_at=iso(t0 + timedelta(minutes=mins)))

        ests = self.led.estimates()
        self.assertNotIn(("claude", "build", "s"), ests["run_avg_size"])
        self.assertAlmostEqual(self.led.run_estimate(ests, "claude", "build", "s"),
                               self.led.run_estimate(ests, "claude", "build"))

    def test_run_estimate_falls_back_without_size(self):
        # No size given at all (e.g. an older run, or a sort/plan role) still
        # resolves through the (platform, role) / platform / global chain.
        ests = self.led.estimates()
        self.assertEqual(self.led.run_estimate(ests, "claude", "build"),
                         self.led.run_estimate(ests, "claude", "build", None))

    def test_update_run_computes_actual_mins(self):
        run_id = self.led.create_run(project="p", number=1, role="build", platform="claude", epoch=0)
        self.clock.advance(minutes=18, seconds=30)
        self.led.update_run(run_id, status="ended", outcome="done", ended_at=iso(self.clock()))
        r = self.led.run(run_id)
        self.assertEqual(r["actual_mins"], 18.5)

    def test_calibrate_estimates_computes_factor_and_mae(self):
        # Seed 3 completed runs with known predictions and actuals
        # Run 1: predicted 10m, actual 12m (diff 2)
        # Run 2: predicted 20m, actual 24m (diff 4)
        # Total predicted = 30m, Total actual = 36m -> ratio = 1.2, MAE = 3.0
        t0 = self.clock()
        self.led.create_run(id=1, project="p", number=1, role="build", platform="claude",
                            epoch=0, est_mins=10.0, actual_mins=12.0, status="ended",
                            started_at=iso(t0), ended_at=iso(t0 + timedelta(minutes=12)))
        self.led.create_run(id=2, project="p", number=2, role="build", platform="claude",
                            epoch=0, est_mins=20.0, actual_mins=24.0, status="ended",
                            started_at=iso(t0), ended_at=iso(t0 + timedelta(minutes=24)))

        stats = self.led.calibrate_estimates(window=10)
        self.assertIsNotNone(stats)
        self.assertEqual(stats["factor"], 1.2)
        self.assertEqual(stats["mae"], 3.0)
        self.assertEqual(stats["samples"], 2)

        # Check that calibration state is saved
        self.assertEqual(self.led.calibration_factor(), 1.2)

        # Future run estimates scale by the calibration factor
        ests = self.led.estimates()
        self.assertEqual(ests["calibration_factor"], 1.2)
        est = self.led.run_estimate(ests, "claude", "build")
        # Global or PR avg is multiplied by 1.2
        self.assertAlmostEqual(est, (12.0 + 24.0) / 2.0 * 1.2, places=1)

    def test_calibrate_clamps_extreme_outliers(self):
        t0 = self.clock()
        # Extreme run taking 10x longer (e.g. hung run)
        self.led.create_run(id=1, project="p", number=1, role="build", platform="claude",
                            epoch=0, est_mins=10.0, actual_mins=100.0, status="ended",
                            started_at=iso(t0), ended_at=iso(t0 + timedelta(minutes=100)))

        stats = self.led.calibrate_estimates()
        self.assertEqual(stats["raw_factor"], 10.0)
        self.assertEqual(stats["factor"], 2.0)  # clamped at max 2.0

        # Extreme fast run (0.1x)
        self.led.update_run(1, actual_mins=1.0)
        stats2 = self.led.calibrate_estimates()
        self.assertEqual(stats2["raw_factor"], 0.1)
        self.assertEqual(stats2["factor"], 0.5)  # clamped at min 0.5

    def test_issue_done_records_accuracy_event(self):
        t0 = self.clock()
        self.led.upsert_item("p", 1, state="working")
        self.led.create_run(id=1, project="p", number=1, role="build", platform="claude",
                            epoch=0, est_mins=15.0, actual_mins=22.5, status="ended",
                            started_at=iso(t0), ended_at=iso(t0 + timedelta(minutes=22, seconds=30)))

        self.led.set_state("p", 1, "done", "shipped")
        evs = self.led.q("SELECT * FROM events WHERE kind='issue_done_stats'")
        self.assertEqual(len(evs), 1)
        detail = json.loads(evs[0]["detail"])
        self.assertEqual(detail["total_actual_mins"], 22.5)
        self.assertEqual(detail["runs_count"], 1)

    def test_scheduler_periodic_calibration(self):
        class DummyCtx:
            def __init__(self, led):
                self.led = led
                self.cfg = {"estimates": {"calibration_interval": 3, "calibration_window": 10}}
                self.messages = []

            def say(self, msg):
                self.messages.append(msg)

        ctx = DummyCtx(self.led)

        # Seed completed runs with predictions
        t0 = self.clock()
        for i in range(1, 5):
            self.led.create_run(id=i, project="p", number=i, role="build", platform="claude",
                                epoch=0, est_mins=10.0, actual_mins=15.0, status="ended",
                                started_at=iso(t0), ended_at=iso(t0 + timedelta(minutes=15)))

        # Run 1
        _check_estimate_calibration(ctx)
        self.assertEqual(self.led.get_kv("runs_since_calibration"), "1")
        self.assertEqual(len(ctx.messages), 0)

        # Run 2
        _check_estimate_calibration(ctx)
        self.assertEqual(self.led.get_kv("runs_since_calibration"), "2")
        self.assertEqual(len(ctx.messages), 0)

        # Run 3 -> triggers calibration!
        _check_estimate_calibration(ctx)
        self.assertEqual(self.led.get_kv("runs_since_calibration"), "0")
        self.assertEqual(len(ctx.messages), 1)
        self.assertIn("Calibrated time estimates", ctx.messages[0])
        self.assertEqual(self.led.calibration_factor(), 1.5)


if __name__ == "__main__":
    unittest.main()
