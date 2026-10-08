"""Tests for review adjudication and reviewer precision (D33)."""
from datetime import datetime, timedelta, timezone
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from mahler import cli, review, scorecard
from mahler.ledger import Ledger, iso


class ReviewAdjudicationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.led = Ledger(":memory:", clock=lambda: self.now)
        self.addCleanup(self.led.close)
        self.cfg = {
            "projects": {"p": {"path": "/tmp/repo"}},
            "platforms": {"slot": {"quota_group": "g"}},
        }

    def advance(self, minutes=10):
        self.now += timedelta(minutes=minutes)

    def create_ended_review(self, number=1, outcome="REVIEW-FAIL", sha="sha123",
                            project="p", platform="slot", model="model-a", effort="high"):
        self.led.upsert_item(project, number, pr=50)
        run_id = self.led.create_run(
            project=project, number=number, role="review", platform=platform,
            model=model, effort=effort, size="s", epoch=1, status="ended",
            outcome=outcome, exit_code=0, started_at=iso(self.now), ended_at=iso(self.now),
        )
        if outcome == "REVIEW-FAIL":
            self.led.event("review_verdict", project, number, {
                "verdict": "fail", "review_run": run_id, "reviewed_sha": sha,
            })
        elif outcome == "REVIEW-PASS":
            self.led.event("review_verdict", project, number, {
                "verdict": "pass", "review_run": run_id, "reviewed_sha": sha,
            })
        return run_id

    def create_ended_fix(self, number=1, outcome="DONE", project="p", started_at=None):
        self.led.upsert_item(project, number, pr=50)
        start = started_at or iso(self.now)
        return self.led.create_run(
            project=project, number=number, role="fix", platform="slot",
            model="model-a", effort="high", size="s", epoch=1, status="ended",
            outcome=outcome, exit_code=0, started_at=start, ended_at=iso(self.now),
        )

    def test_validation_missing_item(self):
        with self.assertRaises(ValueError) as ctx:
            review.adjudicate(
                self.led, "nonexistent", 99, review_run=1, sha="sha123",
                classification="false", evidence="https://example.com/issue/1",
                reason="false positive",
            )
        self.assertIn("item nonexistent#99 not found", str(ctx.exception))

    def test_validation_invalid_classification(self):
        self.led.upsert_item("p", 1)
        with self.assertRaises(ValueError) as ctx:
            review.adjudicate(
                self.led, "p", 1, review_run=1, sha="sha123",
                classification="bogus", evidence="https://example.com/issue/1",
                reason="false positive",
            )
        self.assertIn("invalid classification", str(ctx.exception))

    def test_validation_missing_or_malformed_evidence(self):
        rev_id = self.create_ended_review()
        for bad_url in ("", "not-a-url", "ftp://example.com/file", "http:///no-host"):
            with self.subTest(url=bad_url):
                with self.assertRaises(ValueError) as ctx:
                    review.adjudicate(
                        self.led, "p", 1, review_run=rev_id, sha="sha123",
                        classification="false", evidence=bad_url,
                        reason="false positive",
                    )
                self.assertTrue("evidence" in str(ctx.exception).lower())
        # Assert no adjudication events written
        self.assertEqual(len(self.led.q("SELECT * FROM events WHERE kind='review_adjudication'")), 0)

    def test_validation_missing_reason(self):
        rev_id = self.create_ended_review()
        with self.assertRaises(ValueError) as ctx:
            review.adjudicate(
                self.led, "p", 1, review_run=rev_id, sha="sha123",
                classification="false", evidence="https://example.com/1",
                reason="",
            )
        self.assertIn("missing reason", str(ctx.exception).lower())
        self.assertEqual(len(self.led.q("SELECT * FROM events WHERE kind='review_adjudication'")), 0)

    def test_validation_review_run_not_found_or_wrong_item_or_role(self):
        self.led.upsert_item("p", 1)
        # Run not found
        with self.assertRaises(ValueError) as ctx:
            review.adjudicate(
                self.led, "p", 1, review_run=999, sha="sha123",
                classification="false", evidence="https://example.com/1",
                reason="reason",
            )
        self.assertIn("review run 999 not found", str(ctx.exception))

        # Run on wrong item
        self.advance()
        rev_other = self.create_ended_review(number=2)
        with self.assertRaises(ValueError) as ctx:
            review.adjudicate(
                self.led, "p", 1, review_run=rev_other, sha="sha123",
                classification="false", evidence="https://example.com/1",
                reason="reason",
            )
        self.assertIn("belongs to p#2", str(ctx.exception))

        # Run with wrong role
        self.advance()
        build_id = self.led.create_run(
            project="p", number=1, role="build", platform="slot", epoch=1,
            status="ended", outcome="DONE", exit_code=0, started_at=iso(self.now),
        )
        with self.assertRaises(ValueError) as ctx:
            review.adjudicate(
                self.led, "p", 1, review_run=build_id, sha="sha123",
                classification="false", evidence="https://example.com/1",
                reason="reason",
            )
        self.assertIn("expected 'review'", str(ctx.exception))

    def test_validation_review_run_not_ended_or_not_review_fail(self):
        self.led.upsert_item("p", 1)
        # Not ended
        running_id = self.led.create_run(
            project="p", number=1, role="review", platform="slot", epoch=1,
            status="running", started_at=iso(self.now),
        )
        with self.assertRaises(ValueError) as ctx:
            review.adjudicate(
                self.led, "p", 1, review_run=running_id, sha="sha123",
                classification="false", evidence="https://example.com/1",
                reason="reason",
            )
        self.assertIn("has not ended", str(ctx.exception))

        # Outcome not REVIEW-FAIL
        pass_id = self.create_ended_review(number=1, outcome="REVIEW-PASS")
        with self.assertRaises(ValueError) as ctx:
            review.adjudicate(
                self.led, "p", 1, review_run=pass_id, sha="sha123",
                classification="false", evidence="https://example.com/1",
                reason="reason",
            )
        self.assertIn("expected 'REVIEW-FAIL'", str(ctx.exception))

    def test_validation_missing_review_verdict_or_mismatched_sha(self):
        # Run has outcome REVIEW-FAIL, but no review_verdict event
        self.led.upsert_item("p", 1)
        no_event_id = self.led.create_run(
            project="p", number=1, role="review", platform="slot", epoch=1,
            status="ended", outcome="REVIEW-FAIL", exit_code=0, started_at=iso(self.now),
        )
        with self.assertRaises(ValueError) as ctx:
            review.adjudicate(
                self.led, "p", 1, review_run=no_event_id, sha="sha123",
                classification="false", evidence="https://example.com/1",
                reason="reason",
            )
        self.assertIn("no matching review_verdict event", str(ctx.exception))

        # Review verdict event exists, but sha mismatches
        rev_id = self.create_ended_review(sha="actual-sha")
        with self.assertRaises(ValueError) as ctx:
            review.adjudicate(
                self.led, "p", 1, review_run=rev_id, sha="wrong-sha",
                classification="false", evidence="https://example.com/1",
                reason="reason",
            )
        self.assertIn("mismatched SHA", str(ctx.exception))
        self.assertEqual(len(self.led.q("SELECT * FROM events WHERE kind='review_adjudication'")), 0)

    def test_append_only_idempotent_and_later_correction(self):
        rev_id = self.create_ended_review(sha="sha123")
        # 1. Successful adjudication
        created = review.adjudicate(
            self.led, "p", 1, review_run=rev_id, sha="sha123",
            classification="justified", evidence="https://example.com/1",
            reason="Confirmed valid finding",
        )
        self.assertTrue(created)
        events = self.led.q("SELECT * FROM events WHERE kind='review_adjudication'")
        self.assertEqual(len(events), 1)
        detail = json.loads(events[0]["detail"])
        self.assertEqual(detail["classification"], "justified")
        self.assertEqual(detail["review_run"], rev_id)
        self.assertEqual(detail["sha"], "sha123")
        self.assertEqual(detail["version"], 1)

        # 2. Identical retry is idempotent
        retried = review.adjudicate(
            self.led, "p", 1, review_run=rev_id, sha="sha123",
            classification="justified", evidence="https://example.com/1",
            reason="Confirmed valid finding",
        )
        self.assertFalse(retried)
        events = self.led.q("SELECT * FROM events WHERE kind='review_adjudication'")
        self.assertEqual(len(events), 1)

        # 3. Later correction appends a new event without modifying the earlier event
        self.advance()
        corrected = review.adjudicate(
            self.led, "p", 1, review_run=rev_id, sha="sha123",
            classification="false", evidence="https://example.com/1",
            reason="Actually a false positive upon further inspection",
        )
        self.assertTrue(corrected)
        events = self.led.q("SELECT * FROM events WHERE kind='review_adjudication' ORDER BY id")
        self.assertEqual(len(events), 2)
        # Earlier event unchanged
        self.assertEqual(json.loads(events[0]["detail"])["classification"], "justified")
        # Latest event wins
        self.assertEqual(json.loads(events[1]["detail"])["classification"], "false")

    def test_cli_review_adjudicate_command(self):
        rev_id = self.create_ended_review(sha="sha123")
        parser = cli.build_parser(self.cfg)
        args = parser.parse_args([
            "review-adjudicate", "p#1",
            "--review-run", str(rev_id),
            "--sha", "sha123",
            "--classification", "false",
            "--evidence", "https://github.com/example/repo/pull/50#issuecomment-1",
            "--reason", "False claim about concurrency",
        ])
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            rc = args.fn(args, self.cfg, self.led)
        self.assertEqual(rc, 0)
        self.assertIn("Recorded review adjudication", buf.getvalue())

        # Retry CLI
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            rc = args.fn(args, self.cfg, self.led)
        self.assertEqual(rc, 0)
        self.assertIn("Adjudication already recorded", buf.getvalue())

        # CLI validation failure returns code 2
        bad_args = parser.parse_args([
            "review-adjudicate", "p#1",
            "--review-run", str(rev_id),
            "--sha", "wrong-sha",
            "--classification", "false",
            "--evidence", "https://github.com/example/repo/pull/50#issuecomment-1",
            "--reason", "False claim",
        ])
        err_buf = io.StringIO()
        with mock.patch("sys.stderr", err_buf):
            rc = bad_args.fn(bad_args, self.cfg, self.led)
        self.assertEqual(rc, 2)
        self.assertIn("mismatched SHA", err_buf.getvalue())

    def test_historical_fix_run_attestation_validation(self):
        rev_id = self.create_ended_review(sha="sha123")
        self.advance(10)
        fix_id = self.create_ended_fix()

        # 1. Wrong item
        self.advance(5)
        fix_wrong_item = self.create_ended_fix(number=2)
        with self.assertRaises(ValueError) as ctx:
            review.adjudicate(
                self.led, "p", 1, review_run=rev_id, sha="sha123",
                classification="false", evidence="https://example.com/1",
                reason="reason", fix_runs=[fix_wrong_item],
            )
        self.assertIn("item mismatch", str(ctx.exception).lower())
        self.assertEqual(len(self.led.q("SELECT * FROM events WHERE kind='review_adjudication'")), 0)
        self.assertEqual(len(self.led.q("SELECT * FROM events WHERE kind='review_fix_trigger'")), 0)

        # 2. Wrong role
        build_id = self.led.create_run(
            project="p", number=1, role="build", platform="slot", epoch=1,
            status="ended", outcome="DONE", exit_code=0, started_at=iso(self.now),
        )
        with self.assertRaises(ValueError) as ctx:
            review.adjudicate(
                self.led, "p", 1, review_run=rev_id, sha="sha123",
                classification="false", evidence="https://example.com/1",
                reason="reason", fix_runs=[build_id],
            )
        self.assertIn("expected 'fix'", str(ctx.exception))
        self.assertEqual(len(self.led.q("SELECT * FROM events WHERE kind='review_adjudication'")), 0)

        # 3. Invalid chronology (fix started before review ended)
        early_time = iso(self.now - timedelta(hours=5))
        fix_early = self.led.create_run(
            project="p", number=1, role="fix", platform="slot", epoch=1,
            status="ended", outcome="DONE", exit_code=0, started_at=early_time, ended_at=early_time,
        )
        with self.assertRaises(ValueError) as ctx:
            review.adjudicate(
                self.led, "p", 1, review_run=rev_id, sha="sha123",
                classification="false", evidence="https://example.com/1",
                reason="reason", fix_runs=[fix_early],
            )
        self.assertIn("invalid chronology", str(ctx.exception))
        self.assertEqual(len(self.led.q("SELECT * FROM events WHERE kind='review_adjudication'")), 0)

        # 4. Conflicting existing link
        self.advance(10)
        other_rev_id = self.create_ended_review(number=1, sha="sha456")
        self.advance(10)
        fix_linked = self.create_ended_fix()
        self.led.event("review_fix_trigger", "p", 1, {
            "version": 1, "fix_run": fix_linked, "review_run": other_rev_id,
        })
        with self.assertRaises(ValueError) as ctx:
            review.adjudicate(
                self.led, "p", 1, review_run=rev_id, sha="sha123",
                classification="false", evidence="https://example.com/1",
                reason="reason", fix_runs=[fix_linked],
            )
        self.assertIn("conflicting existing link", str(ctx.exception))
        self.assertEqual(len(self.led.q("SELECT * FROM events WHERE kind='review_adjudication'")), 0)

        # 5. Valid attestation succeeds and records durably
        created = review.adjudicate(
            self.led, "p", 1, review_run=rev_id, sha="sha123",
            classification="false", evidence="https://example.com/1",
            reason="Linked fix was solely for this false finding", fix_runs=[fix_id],
        )
        self.assertTrue(created)
        triggers = self.led.q("SELECT * FROM events WHERE kind='review_fix_trigger'")
        # One from conflicting link setup + one new
        matching_triggers = [
            t for t in triggers
            if json.loads(t["detail"])["fix_run"] == fix_id
        ]
        self.assertEqual(len(matching_triggers), 1)
        td = json.loads(matching_triggers[0]["detail"])
        self.assertEqual(td["review_run"], rev_id)
        self.assertTrue(td["attested"])

    def test_historical_attestation_survives_reopen(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "test.db")
            led1 = Ledger(db_path, clock=lambda: self.now)
            try:
                led1.upsert_item("p", 1, pr=50)
                rev_id = led1.create_run(
                    project="p", number=1, role="review", platform="slot",
                    model="model-a", effort="high", size="s", epoch=1, status="ended",
                    outcome="REVIEW-FAIL", exit_code=0, started_at=iso(self.now), ended_at=iso(self.now),
                )
                led1.event("review_verdict", "p", 1, {
                    "verdict": "fail", "review_run": rev_id, "reviewed_sha": "sha123",
                })
                self.now += timedelta(minutes=15)
                fix_id = led1.create_run(
                    project="p", number=1, role="fix", platform="slot",
                    model="model-a", effort="high", size="s", epoch=1, status="ended",
                    outcome="DONE", exit_code=0, started_at=iso(self.now), ended_at=iso(self.now),
                )
                review.adjudicate(
                    led1, "p", 1, review_run=rev_id, sha="sha123",
                    classification="false", evidence="https://example.com/issue/1",
                    reason="false finding", fix_runs=[fix_id],
                )
            finally:
                led1.close()

            # Reopen ledger
            led2 = Ledger(db_path, clock=lambda: self.now)
            try:
                events = led2.q("SELECT * FROM events WHERE kind IN ('review_adjudication', 'review_fix_trigger')")
                self.assertEqual(len(events), 2)
                adj = next(e for e in events if e["kind"] == "review_adjudication")
                trig = next(e for e in events if e["kind"] == "review_fix_trigger")
                self.assertEqual(json.loads(adj["detail"])["classification"], "false")
                self.assertEqual(json.loads(trig["detail"])["fix_run"], fix_id)
            finally:
                led2.close()

    def test_review_precision_fixtures_and_filters(self):
        # Fixture requirement in Done When #6:
        # Two justified, one false, one unresolved, and one unadjudicated verdict
        # Precision 2/3 and false-fail share 1/3, with both excluded counts visible.
        # Grouped by platform/model/effort.
        # Preserve missing model identity.
        # Duplicate finalization events count once.
        # Zero denominator is unknown.
        # Late corrections recompute.
        # Project/window filters hold.

        # Setup 5 review runs in project 'p' within window
        r1 = self.create_ended_review(number=1, sha="sha1", platform="plat-a", model="m1", effort="high")
        self.advance()
        # Duplicate review_verdict event for r1 to test deduplication
        self.led.event("review_verdict", "p", 1, {
            "verdict": "fail", "review_run": r1, "reviewed_sha": "sha1",
        })

        r2 = self.create_ended_review(number=1, sha="sha2", platform="plat-a", model="m1", effort="high")
        self.advance()
        r3 = self.create_ended_review(number=1, sha="sha3", platform="plat-a", model="m1", effort="high")
        self.advance()
        r4 = self.create_ended_review(number=1, sha="sha4", platform="plat-a", model="m1", effort="high")
        self.advance()
        r5 = self.create_ended_review(number=1, sha="sha5", platform="plat-a", model="m1", effort="high")
        self.advance()

        # Adjudicate r1 (justified), r2 (justified), r3 (false), r4 (unresolved), r5 (unadjudicated)
        review.adjudicate(self.led, "p", 1, review_run=r1, sha="sha1", classification="justified",
                          evidence="https://example.com/1", reason="ok")
        review.adjudicate(self.led, "p", 1, review_run=r2, sha="sha2", classification="justified",
                          evidence="https://example.com/2", reason="ok")
        review.adjudicate(self.led, "p", 1, review_run=r3, sha="sha3", classification="false",
                          evidence="https://example.com/3", reason="ok")
        review.adjudicate(self.led, "p", 1, review_run=r4, sha="sha4", classification="unresolved",
                          evidence="https://example.com/4", reason="ok")
        # r5 remains unadjudicated

        # Also add a run for a different project 'other'
        self.create_ended_review(number=10, sha="shax", project="other", platform="plat-a", model="m1", effort="high")

        # Also add a run with missing model identity and zero denominator
        r_nomodel = self.create_ended_review(number=1, sha="sha-nomodel", platform="plat-b", model=None, effort="low")
        review.adjudicate(self.led, "p", 1, review_run=r_nomodel, sha="sha-nomodel", classification="unresolved",
                          evidence="https://example.com/nomodel", reason="ok")

        # 1. Test precision function for project 'p'
        rows = scorecard.review_precision(self.led, project="p")
        self.assertEqual(len(rows), 2)  # plat-a/m1/high and plat-b/None/low

        row_a = next(r for r in rows if r["platform"] == "plat-a")
        self.assertEqual(row_a["justified"], 2)
        self.assertEqual(row_a["false"], 1)
        self.assertEqual(row_a["unresolved"], 1)
        self.assertEqual(row_a["unadjudicated"], 1)
        self.assertAlmostEqual(row_a["precision"], 2 / 3)
        self.assertAlmostEqual(row_a["false_fail_share"], 1 / 3)

        # Zero denominator case
        row_b = next(r for r in rows if r["platform"] == "plat-b")
        self.assertIsNone(row_b["model"])  # preserved None
        self.assertEqual(row_b["justified"], 0)
        self.assertEqual(row_b["false"], 0)
        self.assertEqual(row_b["unresolved"], 1)
        self.assertEqual(row_b["unadjudicated"], 0)
        self.assertIsNone(row_b["precision"])
        self.assertIsNone(row_b["false_fail_share"])

        # 2. Text formatting
        text = scorecard.format_review_precision([row_a, row_b])
        self.assertIn("precision 2/3 (66.7%)", text)
        self.assertIn("false-fail 1/3 (33.3%)", text)
        self.assertIn("1 unresolved, 1 unadjudicated", text)
        self.assertIn("precision unknown · false-fail unknown", text)
        self.assertIn("Unknown model", text)

        # 3. CLI scorecard --review-precision and --json
        parser = cli.build_parser(self.cfg)
        args_text = parser.parse_args(["scorecard", "--project", "p", "--review-precision"])
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            rc = args_text.fn(args_text, self.cfg, self.led)
        self.assertEqual(rc, 0)
        self.assertIn("precision 2/3", buf.getvalue())

        args_json = parser.parse_args(["scorecard", "--project", "p", "--review-precision", "--json"])
        buf_json = io.StringIO()
        with mock.patch("sys.stdout", buf_json):
            rc = args_json.fn(args_json, self.cfg, self.led)
        self.assertEqual(rc, 0)
        parsed_json = json.loads(buf_json.getvalue())
        self.assertEqual(len(parsed_json), 2)
        self.assertEqual(parsed_json[0]["justified"], 2)
        self.assertEqual(parsed_json[0]["false"], 1)
        self.assertEqual(parsed_json[0]["unresolved"], 1)
        self.assertEqual(parsed_json[0]["unadjudicated"], 1)

        # 4. Late correction recomputes
        # Change r3 from false to justified
        review.adjudicate(self.led, "p", 1, review_run=r3, sha="sha3", classification="justified",
                          evidence="https://example.com/3", reason="reconsidered")
        recomputed = scorecard.review_precision(self.led, project="p")
        row_recomputed = next(r for r in recomputed if r["platform"] == "plat-a")
        self.assertEqual(row_recomputed["justified"], 3)
        self.assertEqual(row_recomputed["false"], 0)
        self.assertAlmostEqual(row_recomputed["precision"], 1.0)
        self.assertAlmostEqual(row_recomputed["false_fail_share"], 0.0)


if __name__ == "__main__":
    unittest.main()
