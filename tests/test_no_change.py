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
        self.gh.issue_body.return_value = ""
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

    def test_optional_observation_is_durable_and_non_gating(self):
        self.verdict(checks=["Verify on owner's phone"])
        self.advance()
        self.advance()
        self.assertEqual(self.read()["human_checks"], ["Verify on owner's phone"])
        self.assertEqual(self.read()["phase"], "done")
        self.assertEqual(self.led.item("x", 5)["state"], "done")
        self.assertEqual(self.led.item("x", 5)["attempts"], 0)
        self.gh.close_issue.assert_called_once()

    def test_invalid_verdicts_are_bounded_without_build_attempts(self):
        for text in ('', 'no findings', '{}', '[]', '{"mode":"claim","evidence":[],"human_checks":[]}',
                     '{"mode":"claim","evidence":["x"],"human_checks":"none"}'):
            with self.subTest(text=text):
                e = self.ending(text)
                e.run['id'] = 20 + self.record['inconclusive']
                self.record['run_id'] = e.run['id']
                self.save()
                no_change.finish(e)
                self.record = self.read()
                self.assertEqual(self.record["phase"], "waiting")
        self.advance()
        self.advance()
        self.assertEqual(self.read()["phase"], "exhausted")
        self.assertEqual(self.led.item("x", 5)["state"], "needs_you")
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

    def practices(self, state="pass", question=None):
        from mahler import practices_audit as audit
        self.verdict()
        self.record.update(pending_practices_checks=["ci-tests"], owner_question=question)
        self.save()
        self.gh.issue_body.return_value = audit.proposal_marker("x", "ci-tests")
        patch = mock.patch.object(audit, "verify_proposal", return_value=
                                 audit.Finding("ci-tests", state, "scanner evidence", ()))
        scanner = patch.start()
        self.addCleanup(patch.stop)
        return scanner

    def test_mechanical_pass_and_mixed_owner_question(self):
        for question in (None, "[credentials] Which account should own this integration?"):
            with self.subTest(question=question):
                self.closed = False
                self.comments.clear()
                self.led.set_state("x", 5, "verifying", "test")
                scanner = self.practices(question=question)
                self.advance()
                self.advance()
                scanner.assert_called_once()
                self.assertEqual(self.led.item("x", 5)["state"],
                                 "needs_you" if question else "done")
                self.assertIn("Practices ci-tests: pass at base", self.comments[0])
                if question:
                    self.assertEqual(self.led.item("x", 5)["question"], question.split("] ", 1)[1])
                    self.assertFalse(self.closed)
                    self.ctx.ping.assert_called_once()

    def test_owner_question_without_mechanical_check(self):
        self.verdict()
        self.record["owner_question"] = "[credentials] Which account should own this integration?"
        self.save()
        self.advance()
        self.advance()
        self.assertEqual(self.led.item("x", 5)["state"], "needs_you")
        self.ctx.ping.assert_called_once()
        self.gh.close_issue.assert_not_called()

    def test_non_owner_question_becomes_a_human_check_not_a_park(self):
        self.verdict()
        self.record["owner_question"] = "Should I file a follow-up?"
        self.save()
        self.advance()
        self.assertNotEqual(self.led.item("x", 5)["state"], "needs_you")
        self.assertIn("Needs a human to check:\n- Should I file a follow-up?", self.comments[0])
        self.assertTrue(self.closed)

    def test_mechanical_gap_rejects_once(self):
        self.practices("gap")
        self.advance()
        self.advance()
        self.assertEqual(self.led.item("x", 5)["attempts"], 1)
        self.assertEqual(self.read()["phase"], "rejected")
        self.assertIn("ci-tests: gap", self.comments[0])
        self.gh.close_issue.assert_not_called()

    def test_mechanical_unknown_and_exception_are_inconclusive(self):
        for error in (None, GHError("API unavailable")):
            with self.subTest(error=error):
                scanner = self.practices("unknown")
                scanner.side_effect = error
                self.advance()
                self.assertEqual(self.read()["phase"], "waiting")
                self.assertTrue(self.read()["practices_error"])
                self.gh.close_issue.assert_not_called()
        self.record = self.read()
        self.record["inconclusive"] = finalize.REVIEW_INCONCLUSIVE_CAP + 1
        self.save()
        self.advance()
        self.assertEqual(self.read()["phase"], "exhausted")

    def test_scan_lease_loss_and_base_drift_never_close(self):
        scanner = self.practices()
        with mock.patch.object(no_change, "fetched_base", side_effect=["base", "new"]):
            self.advance()
        self.gh.close_issue.assert_not_called()
        result = scanner.return_value
        def preempt(*args):
            self.led.claim("x", 5, "human", "interactive", 10)
            return result
        scanner.side_effect = preempt
        self.advance()
        self.gh.close_issue.assert_not_called()

    def test_legacy_scheduler_recovery_and_confirmed_closure(self):
        scanner = self.practices()
        self.record.update(phase="manual", human_checks=["Run unavailable Mahler command"])
        self.save()
        self.led.set_state("x", 5, "parked", "legacy manual")
        with mock.patch.object(no_change.router, "capacity_recovery", return_value=(True, None, None)), \
                mock.patch.object(no_change.router, "pick_for_project", return_value=("reviewer", [])), \
                mock.patch.object(ship, "start") as start, \
                mock.patch.object(ship, "_open_pr") as pr:
            ship._ship_project(self.ctx, "x")
            start.assert_called_once()
            self.assertNotIn("verdict", self.read())
            self.assertEqual(self.read()["legacy_evidence"]["human_checks"],
                             ["Run unavailable Mahler command"])
            payload = json.dumps({"mode": "claim", "evidence": ["All other criteria satisfied"],
                                  "human_checks": [], "pending_practices_checks": ["ci-tests"]})
            no_change.finish(self.ending(payload))
            action = self.gh.close_issue.side_effect
            def lost_response(n):
                action(n)
                raise GHError("response lost")
            self.gh.close_issue.side_effect = lost_response
            ship._ship_project(self.ctx, "x")
            self.assertEqual(self.read()["phase"], "verdict")
            self.gh.close_issue.side_effect = action
            ship._ship_project(self.ctx, "x")
            ship._ship_project(self.ctx, "x")
            pr.assert_not_called()
        self.assertEqual(self.read()["phase"], "done")
        self.gh.close_issue.assert_called_once()
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(self.led.con.execute("SELECT count(*) FROM release_items").fetchone()[0], 0)

    def test_recovery_skips_dry_run_active_human_and_unrelated(self):
        for skip in ("dry", "active", "human", "unrelated", "pr"):
            with self.subTest(skip=skip):
                self.verdict()
                self.record["phase"] = "other" if skip == "unrelated" else "manual"
                self.save()
                self.led.set_state("x", 5, "parked", "test")
                self.ctx.dry_run = skip == "dry"
                if skip == "active":
                    rid = self.led.create_run(project="x", number=5, role="review", platform="claude", epoch=1)
                if skip == "human":
                    self.led.claim("x", 5, "human", "interactive", 10)
                if skip == "pr":
                    self.led.upsert_item("x", 5, pr=10)
                ship._ship_project(self.ctx, "x")
                self.assertEqual(self.led.item("x", 5)["state"], "parked")
                if skip == "active":
                    self.led.update_run(rid, status="ended")
                if skip == "human":
                    self.led.release("x", 5, holder="human")

    def test_optional_fields_are_validated_and_unmatched_checks_do_not_close(self):
        base = {"mode": "claim", "evidence": ["source"], "human_checks": []}
        for extra in ({"pending_practices_checks": ["unknown"]},
                      {"pending_practices_checks": "ci-tests"}, {"owner_question": ""},
                      {"owner_question": []}):
            with self.assertRaises(ValueError):
                no_change.parse_payload(json.dumps({**base, **extra}))
        self.practices()
        self.gh.issue_body.return_value = "unmatched"
        self.advance()
        self.assertEqual(self.read()["phase"], "waiting")
        self.gh.close_issue.assert_not_called()

    def test_inconclusive_rotates_and_escalates_before_parking(self):
        for index in range(4):
            e = self.ending('')
            e.run['id'] = index + 20
            e.run['platform'] = f'reviewer-{index}'
            self.record.update(run_id=e.run['id'])
            self.save()
            no_change.finish(e)
            no_change.finish(e)  # replay is not another attempt
            self.record = self.read()
            seen = json.loads(self.led.get_kv('reviewinconclusive:x#5:base') or '[]')
            self.assertEqual(len(seen), index + 1)
            with mock.patch.object(no_change.router, 'capacity_recovery', return_value=(True, None, None)) as capacity, \
                    mock.patch.object(no_change.router, 'pick_for_project', return_value=('next-reviewer', [])) as pick, \
                    mock.patch.object(ship, 'start') as start:
                self.advance()
            if index < 3:
                self.assertEqual(pick.call_args.kwargs['exclude'], {f'reviewer-{i}' for i in range(index + 1)})
                self.assertEqual(pick.call_args.kwargs['min_tier'], 2 if index == 2 else 0)
                self.assertEqual(capacity.call_args.kwargs['min_tier'], 2 if index == 2 else 0)
                start.assert_called_once()
            else:
                start.assert_not_called()
                self.assertEqual(self.led.item('x', 5)['state'], 'needs_you')
            if index == 2:
                self.assertEqual(self.led.get_kv('reviewescalate:x#5:base'), '1')

    def test_begin_preserves_full_final_evidence(self):
        e = self.ending('done', verb='DONE')
        e.no_work_base = 'base'
        evidence = 'https://github.com/x/y/issues/653#issuecomment-1\n' + 'output\n' * 400
        e.log.update(final=evidence, last_text=evidence[-1500:])
        no_change.begin(e)
        self.assertEqual(self.read()['build_evidence'], evidence)

    def test_parent_verify_github_only_claim_closes_without_historical_diff(self):
        from mahler import prompt
        self.led.upsert_item('x', 653, issue_body='- [x] Six completion criteria')
        self.led.upsert_item('x', 5, issue_body='<!-- mahler:parent-verify:653:abc -->')
        self.gh.authenticated_login.return_value = 'owner'
        self.gh.issue_body.return_value = '- [x] Six completion criteria'
        url = 'https://github.com/x/y/issues/653#issuecomment-1'
        self.gh.issue_comments.side_effect = lambda n: [
            {'body': 'Evidence: all six checks passed', 'author': {'login': 'owner'}, 'url': url}]
        context = prompt.issue_context(self.ctx, 'x', self.led.item('x', 5), 'x/y')
        self.assertIn('- [x] Six completion criteria', context)
        self.assertIn(url, context)
        payload = json.dumps({'mode': 'claim', 'evidence': [
            f'Parent #653 checklist complete; evidence comment verified at {url}; '
            'GitHub-only task exempt from historical diff sensitivity'], 'human_checks': []})
        no_change.finish(self.ending(payload))
        self.advance()
        self.assertEqual(self.led.item('x', 5)['state'], 'done')
        self.assertIn(url, self.comments[0])
