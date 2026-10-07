"""No-PR claim lifecycle: isolated ledger and fake external side effects."""

import copy
import json
import tempfile
import unittest
from unittest import mock

from mahler import config, finalize, no_change, scheduler, ship
from mahler.gh import GHError
from mahler.ledger import Ledger, iso


class ClaimTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["projects"]["x"] = {"repo": "x/y", "path": tmp.name, "base": "release"}
        self.led = Ledger(":memory:")
        self.addCleanup(self.led.close)
        self.led.upsert_item("x", 5, state="verifying", title="claim", sorted_at=iso(self.led.now()))
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
        self.comments = []
        self.closed = False
        self.gh = mock.Mock()
        self.gh.issue_comments.side_effect = lambda n: [{"body": c} for c in self.comments]
        self.gh.comment.side_effect = lambda n, body: self.comments.append(body)
        self.gh.issue_state.side_effect = lambda n: "CLOSED" if self.closed else "OPEN"
        self.gh.close_issue.side_effect = lambda n: setattr(self, "closed", True)
        for target, name, value in ((self.ctx, "gh", self.gh),
                                     (no_change, "fetched_base", "base"),
                                     (self.ctx, "ping", None)):
            patch = mock.patch.object(target, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        self.record = {"mode": "claim", "claim_run": 1, "base_sha": "base",
                       "evidence": "source already implements issue", "build_evidence": "tests pass",
                       "human_checks": [], "phase": "waiting", "inconclusive": 0}
        self.save()

    def save(self):
        no_change.save(self.led, "x", 5, self.record)

    def read(self):
        return no_change.read(self.led, "x", 5)

    def advance(self):
        ship._ship_item(self.ctx, "x", self.led.item("x", 5))

    def verdict(self, verdict="pass", checks=None):
        self.record.update(phase="verdict", verdict=verdict, run_id=2,
                           review_evidence=["module.py:12 satisfies acceptance; test suite passed"],
                           human_checks=checks or [])
        self.save()

    def ending(self, text, verb="REVIEW-PASS", epoch=1, run_id=2):
        self.record.update(phase="reviewing", run_id=2, epoch=1)
        self.save()
        return finalize.Ending(self.ctx, {"id": run_id, "epoch": epoch,
                    "project": "x", "number": 5, "platform": "claude"},
                    self.led.item("x", 5), self.ctx.policy("x"), {}, "claude", verb,
                    text, None, verb)

    def test_pass_closes_once_without_shipping_or_attempts(self):
        self.verdict()
        with mock.patch.object(ship, "_open_pr") as pr:
            self.advance()
            self.advance()
        pr.assert_not_called()
        self.assertEqual(self.led.item("x", 5)["state"], "done")
        self.assertEqual(self.led.item("x", 5)["attempts"], 0)
        self.assertEqual(len(self.comments), 1)
        self.gh.close_issue.assert_called_once_with(5)
        self.assertEqual(self.led.con.execute("SELECT count(*) FROM release_items").fetchone()[0], 0)

    def test_fail_charges_once_and_returns_to_build(self):
        self.verdict("fail")
        self.advance()
        self.advance()
        self.assertEqual(self.read()["phase"], "rejected")
        self.assertEqual(self.led.item("x", 5)["attempts"], 1)
        self.assertEqual(self.led.item("x", 5)["state"], "ready")
        self.assertIn("module.py:12", self.comments[0])
        self.gh.close_issue.assert_not_called()

    def test_manual_pass_parks_durable_checks(self):
        self.verdict(checks=["Verify on owner's phone"])
        self.advance()
        self.advance()
        self.assertEqual(self.read()["human_checks"], ["Verify on owner's phone"])
        self.assertEqual(self.read()["phase"], "manual")
        self.assertEqual(self.led.item("x", 5)["state"], "parked")
        self.assertEqual(self.led.item("x", 5)["attempts"], 0)
        self.gh.close_issue.assert_not_called()

    def test_invalid_verdicts_are_bounded_without_build_attempts(self):
        for text in ('', 'no findings', '{}', '[]', '{"mode":"claim","evidence":[],"human_checks":[]}',
                     '{"mode":"claim","evidence":["x"],"human_checks":"none"}'):
            with self.subTest(text=text):
                e = self.ending(text)
                no_change.finish(e)
                self.record = self.read()
                self.assertEqual(self.record["phase"], "waiting")
        self.advance()
        self.advance()
        self.assertEqual(self.read()["phase"], "exhausted")
        self.assertEqual(self.led.item("x", 5)["state"], "failed")
        self.assertEqual(self.led.item("x", 5)["attempts"], 0)
        self.ctx.ping.assert_called_once()

    def test_run_and_epoch_fence_and_replayed_verdict(self):
        payload = json.dumps({"mode": "claim", "evidence": ["a.py:4"], "human_checks": []})
        for run_id, epoch in ((3, 1), (2, 2)):
            e = self.ending(payload, run_id=run_id, epoch=epoch)
            no_change.finish(e)
            self.assertEqual(self.read()["phase"], "reviewing")
        e = self.ending(payload)
        no_change.finish(e)
        no_change.finish(e)
        self.assertEqual(self.read()["phase"], "verdict")
        self.assertEqual(self.read()["inconclusive"], 0)

    def test_base_drift_discards_verdict_and_launches_new_review(self):
        self.verdict()
        with mock.patch.object(no_change, "fetched_base", return_value="new-base"), \
                mock.patch.object(no_change.router, "capacity_recovery", return_value=(False, None, None)):
            self.advance()
        self.assertEqual(self.read()["phase"], "waiting")
        self.assertEqual(self.read()["base_sha"], "new-base")
        self.assertNotIn("verdict", self.read())
        self.gh.comment.assert_not_called()
        self.gh.close_issue.assert_not_called()

    def test_capacity_waits_and_launch_reuses_independent_route(self):
        with mock.patch.object(ship, "_review_route", return_value=(None, "m", {"builder"})), \
                mock.patch.object(no_change.router, "capacity_recovery", return_value=(False, None, None)), \
                mock.patch.object(ship, "start") as start:
            self.advance()
            self.advance()
            start.assert_not_called()
        self.assertEqual(self.read()["inconclusive"], 0)
        with mock.patch.object(ship, "_review_route", return_value=(None, "m", {"builder"})), \
                mock.patch.object(no_change.router, "capacity_recovery", return_value=(True, None, None)), \
                mock.patch.object(no_change.router, "pick_for_project", return_value=("reviewer", [])) as pick, \
                mock.patch.object(ship, "start") as start:
            self.advance()
        self.assertEqual(pick.call_args.kwargs["exclude"], {"builder"})
        self.assertEqual(start.call_args.args[2]["claim_base_sha"], "base")
        self.assertEqual(start.call_args.args[3:5], ("review", "reviewer"))
        self.assertEqual(self.led.item("x", 5)["attempts"], 0)

    def test_restart_after_comment_or_close_failure_is_retryable(self):
        for operation in ("comment", "close_issue"):
            with self.subTest(operation=operation):
                self.closed = False
                self.comments.clear()
                self.led.set_state("x", 5, "verifying", "test")
                self.verdict()
                action = getattr(self.gh, operation).side_effect

                def submitted_then_failed(*args):
                    action(*args)
                    raise GHError("response lost")

                with mock.patch.object(self.gh, operation, side_effect=submitted_then_failed):
                    with self.assertRaises(GHError):
                        self.advance()
                self.assertEqual(self.read()["phase"], "verdict")
                self.assertEqual(self.led.item("x", 5)["state"], "verifying")
                # No process-local receipt: a fresh context uses ledger + GitHub markers.
                with mock.patch.object(self.ctx, "gh", return_value=self.gh):
                    self.advance()
                self.assertEqual(len(self.comments), 1)
                self.assertEqual(self.led.item("x", 5)["state"], "done")

    def test_unconfirmed_closure_stays_retryable(self):
        self.verdict()
        self.gh.close_issue.side_effect = None
        self.advance()
        self.assertEqual(self.read()["phase"], "verdict")
        self.assertEqual(self.led.item("x", 5)["state"], "verifying")

    def test_lease_change_during_github_read_prevents_closure(self):
        self.verdict()

        def preempt(n):
            self.led.claim("x", 5, "human", "interactive", 10)
            return []

        self.gh.issue_comments.side_effect = preempt
        self.advance()
        self.gh.comment.assert_not_called()
        self.gh.close_issue.assert_not_called()

    def test_base_drift_during_comment_prevents_closure(self):
        self.verdict()
        with mock.patch.object(no_change, "fetched_base", side_effect=["base", "new"]):
            self.advance()
        self.gh.close_issue.assert_not_called()

    def test_interrupted_launch_is_bounded_and_active_run_is_not_relaunched(self):
        rid = self.led.create_run(project="x", number=5, role="review", platform="claude", epoch=1)
        self.record.update(phase="reviewing", run_id=rid, epoch=1)
        self.save()
        with mock.patch.object(ship, "start") as start:
            self.advance()
            start.assert_not_called()
        self.assertEqual(self.read()["inconclusive"], 0)
        self.led.update_run(rid, status="ended")
        with mock.patch.object(no_change.router, "capacity_recovery", return_value=(False, None, None)):
            self.advance()
            self.advance()
        self.assertEqual(self.read()["inconclusive"], 1)
