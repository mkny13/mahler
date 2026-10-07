"""CLI tests for mahler capacity (mahler#736)."""

import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from mahler import cli
from mahler.ledger import Ledger, iso

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


class CliCapacityTests(unittest.TestCase):
    def setUp(self):
        self.now = T0
        self.led = Ledger(":memory:", clock=lambda: self.now)
        self.addCleanup(self.led.close)

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(cli.config, "load", return_value={"platforms": {}}), \
             patch.object(cli, "Ledger", return_value=self.led), \
             patch.object(cli, "RoutedLedger", side_effect=lambda led, cfg: led), \
             patch.object(self.led, "close"), \
             patch("sys.stdout", out), patch("sys.stderr", err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_default_days_is_30(self):
        code, out, err = self.run_cli(["capacity"])
        self.assertEqual(code, 0)
        self.assertIn("Mahler Free Capacity Report (last 30 days)", out)
        self.assertIn("Window:", out)
        self.assertIn("Coverage: no recorded capacity history in this window.", out)

    def test_days_1_and_90_accepted(self):
        code1, out1, _ = self.run_cli(["capacity", "--days", "1"])
        self.assertEqual(code1, 0)
        self.assertIn("Mahler Free Capacity Report (last 1 days)", out1)

        code90, out90, _ = self.run_cli(["capacity", "--days", "90"])
        self.assertEqual(code90, 0)
        self.assertIn("Mahler Free Capacity Report (last 90 days)", out90)

    def test_invalid_days_rejected(self):
        for bad in ["0", "-1", "-10", "91", "100", "abc", "1.5", ""]:
            with self.subTest(days=bad):
                out, err = io.StringIO(), io.StringIO()
                with patch("sys.stdout", out), patch("sys.stderr", err):
                    with self.assertRaises(SystemExit) as ctx:
                        cli.main(["capacity", "--days", bad])
                    self.assertEqual(ctx.exception.code, 2)

    def test_empty_ledger_handles_cleanly(self):
        code, out, err = self.run_cli(["capacity"])
        self.assertEqual(code, 0)
        self.assertIn("Coverage: no recorded capacity history in this window.", out)
        self.assertIn("No free-platform capacity wait observed.", out)
        self.assertIn("None observed.", out)
        self.assertIn("No request accounting recorded in this window.", out)

    def test_read_only_makes_no_ledger_writes_or_schema_changes(self):
        # Insert a sample interval and sample run
        self.led.con.execute(
            "INSERT INTO capacity_intervals (project, number, role, size, effective_size, "
            "required_tier, first_seen, last_seen, open, end_reason, signature, blockers, "
            "platforms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("p", 1, "build", "m", "m", 1, iso(self.now - timedelta(hours=1)), iso(self.now), 0,
             None, "sig", json.dumps(["no_platform"]),
             json.dumps({"kilo": {"reasons": ["quota"], "cost": "free", "tier": 1}}))
        )
        self.led.con.execute(
            "INSERT INTO runs (id, project, number, role, platform, epoch, status, "
            "started_at, ended_at, requests, request_buckets, request_coverage) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (10, "p", 1, "build", "kilo", 0, "ended",
             iso(self.now - timedelta(hours=1)), iso(self.now),
             5, json.dumps({"2026-09-20": {"kilo-a:free": 5}}), "complete")
        )

        # Snapshot table row counts and contents
        def snapshot():
            counts = {}
            for row in self.led.q("SELECT name FROM sqlite_master WHERE type='table'"):
                t = row["name"]
                counts[t] = self.led.q(f"SELECT COUNT(*) AS c FROM {t}")[0]["c"]
            return counts

        before = snapshot()
        code, out, _ = self.run_cli(["capacity", "--days", "7"])
        self.assertEqual(code, 0)
        after = snapshot()
        self.assertEqual(before, after)

    def test_cli_output_contains_qualification_and_sections(self):
        self.led.con.execute(
            "INSERT INTO capacity_intervals (project, number, role, size, effective_size, "
            "required_tier, first_seen, last_seen, open, end_reason, signature, blockers, "
            "platforms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("proj", 42, "build", "m", "m", 1, iso(self.now - timedelta(hours=2)),
             iso(self.now - timedelta(hours=1)), 0, None, "sig", json.dumps(["no_platform"]),
             json.dumps({"kilo": {"reasons": ["quota"], "cost": "free", "tier": 1}}))
        )
        code, out, _ = self.run_cli(["capacity"])
        self.assertEqual(code, 0)
        self.assertIn("Summary:", out)
        self.assertIn("an extra free pool at tier 1 would have been usable for 1.0 h", out)
        self.assertIn("Qualification:", out)
        self.assertIn("Observed opportunity only; not guaranteed throughput or a purchase recommendation.", out)
        self.assertIn("X is observed union wall time with replaceable quota/credit blockers", out)
        self.assertIn("A replacement pool is assumed to match the exhausted candidate’s account eligibility", out)
        self.assertIn("Claude/paid diversions:", out)
        self.assertIn("Request accounting (UTC daily peaks):", out)


if __name__ == "__main__":
    unittest.main()
