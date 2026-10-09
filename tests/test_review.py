"""Tests for effective review status derivation and agreement (mahler#758)."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from mahler import config, finalize, review, runner, scheduler, ship
from mahler.console import state
from mahler.ledger import Ledger, iso

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


class TestEffectiveReviewPure(unittest.TestCase):
    """Pure helper tests: identity, launch binding, run lifecycle, and verdict evidence."""

    def test_active_and_stopping_runs_are_pending(self):
        binding = {"sha": "head1", "pr": 10, "run_id": 1}
        for status in ("running", "stopping"):
            with self.subTest(status=status):
                run = {"id": 1, "role": "review", "status": status, "project": "x", "number": 5}
                self.assertEqual(
                    review.effective(binding, pr=10, sha="head1", run=run, project="x", number=5),
                    "pending",
                )

    def test_ended_and_missing_runs_are_not_pending(self):
        binding = {"sha": "head1", "pr": 10, "run_id": 1}
        ended_run = {"id": 1, "role": "review", "status": "ended", "project": "x", "number": 5}
        self.assertEqual(
            review.effective(binding, pr=10, sha="head1", run=ended_run, project="x", number=5),
            "unknown",
        )
        self.assertEqual(
            review.effective(binding, pr=10, sha="head1", run=None, project="x", number=5),
            "unknown",
        )
        self.assertEqual(
            review.effective(binding, pr=10, sha="head1", run=ended_run, project="x", number=5, required=True),
            "required",
        )

    def test_wrong_role_is_not_pending(self):
        binding = {"sha": "head1", "pr": 10, "run_id": 1}
        for role in ("build", "fix", "sort"):
            with self.subTest(role=role):
                run = {"id": 1, "role": role, "status": "running", "project": "x", "number": 5}
                self.assertEqual(
                    review.effective(binding, pr=10, sha="head1", run=run, project="x", number=5),
                    "unknown",
                )

    def test_wrong_item_or_project_is_not_pending(self):
        binding = {"sha": "head1", "pr": 10, "run_id": 1}
        run_wrong_num = {"id": 1, "role": "review", "status": "running", "project": "x", "number": 99}
        self.assertEqual(
            review.effective(binding, pr=10, sha="head1", run=run_wrong_num, project="x", number=5),
            "unknown",
        )
        run_wrong_proj = {"id": 1, "role": "review", "status": "running", "project": "other", "number": 5}
        self.assertEqual(
            review.effective(binding, pr=10, sha="head1", run=run_wrong_proj, project="x", number=5),
            "unknown",
        )

    def test_mismatched_run_id_is_not_pending(self):
        binding = {"sha": "head1", "pr": 10, "run_id": 1}
        run = {"id": 2, "role": "review", "status": "running", "project": "x", "number": 5}
        self.assertEqual(
            review.effective(binding, pr=10, sha="head1", run=run, project="x", number=5),
            "unknown",
        )

    def test_head_replacement_invalidates_pending_and_terminal_verdicts(self):
        run = {"id": 1, "role": "review", "status": "running", "project": "x", "number": 5}
        pending_binding = {"sha": "old_head", "pr": 10, "run_id": 1}
        self.assertEqual(
            review.effective(pending_binding, pr=10, sha="new_head", run=run, project="x", number=5),
            "unknown",
        )

        pass_binding = {"sha": "old_head", "pr": 10, "verdict": "pass", "run_id": 1}
        self.assertEqual(
            review.effective(pass_binding, pr=10, sha="new_head", run=None, project="x", number=5),
            "unknown",
        )

        fail_binding = {"sha": "old_head", "pr": 10, "verdict": "fail", "run_id": 1}
        self.assertEqual(
            review.effective(fail_binding, pr=10, sha="new_head", run=None, project="x", number=5),
            "unknown",
        )

    def test_pr_replacement_invalidates_pending_and_terminal_verdicts(self):
        run = {"id": 1, "role": "review", "status": "running", "project": "x", "number": 5}
        pending_binding = {"sha": "head1", "pr": 10, "run_id": 1}
        self.assertEqual(
            review.effective(pending_binding, pr=11, sha="head1", run=run, project="x", number=5),
            "unknown",
        )

        pass_binding = {"sha": "head1", "pr": 10, "verdict": "pass"}
        self.assertEqual(
            review.effective(pass_binding, pr=11, sha="head1", run=None, project="x", number=5),
            "unknown",
        )

    def test_terminal_verdict_missing_bound_sha_is_not_accepted(self):
        for verdict in ("pass", "fail"):
            with self.subTest(verdict=verdict):
                binding = {"pr": 10, "verdict": verdict}
                # When current SHA is supplied, missing bound SHA must not match as wildcard
                self.assertEqual(
                    review.effective(binding, pr=10, sha="head1", run=None, project="x", number=5),
                    "unknown",
                )
                self.assertEqual(
                    review.effective(binding, pr=10, sha="head1", run=None, project="x", number=5, required=True),
                    "required",
                )
                self.assertNotIn(
                    review.effective(binding, pr=10, sha="head1", run=None, project="x", number=5),
                    ("pass", "fail"),
                )
                # Explicit None sha in binding must also not match
                binding_none = {"sha": None, "pr": 10, "verdict": verdict}
                self.assertEqual(
                    review.effective(binding_none, pr=10, sha="head1", run=None, project="x", number=5),
                    "unknown",
                )

    def test_terminal_verdict_missing_bound_pr_is_not_accepted(self):
        for verdict in ("pass", "fail"):
            with self.subTest(verdict=verdict):
                binding = {"sha": "head1", "verdict": verdict}
                # When current PR is supplied, missing bound PR must not match as wildcard
                self.assertEqual(
                    review.effective(binding, pr=10, sha="head1", run=None, project="x", number=5),
                    "unknown",
                )
                self.assertEqual(
                    review.effective(binding, pr=10, sha="head1", run=None, project="x", number=5, required=True),
                    "required",
                )
                self.assertNotIn(
                    review.effective(binding, pr=10, sha="head1", run=None, project="x", number=5),
                    ("pass", "fail"),
                )
                # Explicit None pr in binding must also not match
                binding_none = {"sha": "head1", "pr": None, "verdict": verdict}
                self.assertEqual(
                    review.effective(binding_none, pr=10, sha="head1", run=None, project="x", number=5),
                    "unknown",
                )

    def test_terminal_verdict_missing_both_bound_sha_and_pr_is_not_accepted(self):
        for verdict in ("pass", "fail"):
            with self.subTest(verdict=verdict):
                binding = {"verdict": verdict}
                self.assertEqual(
                    review.effective(binding, pr=10, sha="head1", run=None, project="x", number=5),
                    "unknown",
                )
                self.assertNotIn(
                    review.effective(binding, pr=10, sha="head1", run=None, project="x", number=5),
                    ("pass", "fail"),
                )

    def test_active_run_with_unbound_identity_remains_pending(self):
        run = {"id": 1, "role": "review", "status": "running", "project": "x", "number": 5}
        # Pending runs with missing bound SHA or PR retain existing pending behavior
        binding_no_sha = {"pr": 10, "run_id": 1}
        self.assertEqual(
            review.effective(binding_no_sha, pr=10, sha="head1", run=run, project="x", number=5),
            "pending",
        )
        binding_no_pr = {"sha": "head1", "run_id": 1}
        self.assertEqual(
            review.effective(binding_no_pr, pr=10, sha="head1", run=run, project="x", number=5),
            "pending",
        )

    def test_terminal_verdicts_are_durable_across_run_lifecycle(self):
        for verdict in ("pass", "fail"):
            with self.subTest(verdict=verdict):
                binding = {"sha": "head1", "pr": 10, "verdict": verdict, "run_id": 1}
                # Active run
                active = {"id": 1, "role": "review", "status": "running", "project": "x", "number": 5}
                self.assertEqual(
                    review.effective(binding, pr=10, sha="head1", run=active, project="x", number=5),
                    verdict,
                )
                # Ended run
                ended = {"id": 1, "role": "review", "status": "ended", "project": "x", "number": 5}
                self.assertEqual(
                    review.effective(binding, pr=10, sha="head1", run=ended, project="x", number=5),
                    verdict,
                )
                # Absent/missing run
                self.assertEqual(
                    review.effective(binding, pr=10, sha="head1", run=None, project="x", number=5),
                    verdict,
                )

    def test_required_invalidation_and_empty_bindings(self):
        for empty in ({}, None, "{}", ""):
            with self.subTest(empty=empty):
                self.assertEqual(
                    review.effective(empty, pr=10, sha="head1", run=None, project="x", number=5),
                    "unknown",
                )
                self.assertEqual(
                    review.effective(empty, pr=10, sha="head1", run=None, project="x", number=5, required=True),
                    "required",
                )

    def test_legacy_pending_records_conservatively_checked(self):
        # Legacy record with verdict: "pending" and run_id
        legacy = {"sha": "head1", "pr": 10, "verdict": "pending", "run_id": 1}
        active_run = {"id": 1, "role": "review", "status": "running", "project": "x", "number": 5}
        ended_run = {"id": 1, "role": "review", "status": "ended", "project": "x", "number": 5}

        # Active run: pending
        self.assertEqual(
            review.effective(legacy, pr=10, sha="head1", run=active_run, project="x", number=5),
            "pending",
        )
        # Ended run: NEVER pending because a KV verdict says so
        self.assertEqual(
            review.effective(legacy, pr=10, sha="head1", run=ended_run, project="x", number=5),
            "unknown",
        )
        # Missing run: NEVER pending
        self.assertEqual(
            review.effective(legacy, pr=10, sha="head1", run=None, project="x", number=5),
            "unknown",
        )
        # Old head: NEVER pending
        self.assertEqual(
            review.effective(legacy, pr=10, sha="new_head", run=active_run, project="x", number=5),
            "unknown",
        )

        # Legacy record with verdict: "pending" without run_id
        legacy_no_rid = {"sha": "head1", "pr": 10, "verdict": "pending"}
        self.assertEqual(
            review.effective(legacy_no_rid, pr=10, sha="head1", run=active_run, project="x", number=5),
            "pending",
        )
        self.assertEqual(
            review.effective(legacy_no_rid, pr=10, sha="head1", run=ended_run, project="x", number=5),
            "unknown",
        )
        self.assertEqual(
            review.effective(legacy_no_rid, pr=10, sha="head1", run=None, project="x", number=5),
            "unknown",
        )


class TestEffectiveReviewWithLedger(unittest.TestCase):
    """Ledger integration tests: launch binding, finalization after preemption, console/ship agreement."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)
        self.cfg = config.load(os.devnull)
        self.cfg["projects"]["x"] = {
            "name": "x",
            "repo": "x/y",
            "path": self.tmp,
            "base": "main",
            "auto_lease_minutes": 30,
        }

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ledger_effective_for_item_active_stopping_ended_missing(self):
        self.led.upsert_item("x", 5, pr=88, state="verifying")
        rid = self.led.create_run(project="x", number=5, role="review", platform="claude", epoch=1)
        self.led.set_kv("review:x#5", json.dumps({"sha": "head1", "pr": 88, "run_id": rid}))

        # Running
        self.assertEqual(
            review.effective_for_item(self.led, "x", 5, pr=88, sha="head1"),
            "pending",
        )
        # Stopping
        self.led.update_run(rid, status="stopping")
        self.assertEqual(
            review.effective_for_item(self.led, "x", 5, pr=88, sha="head1"),
            "pending",
        )
        # Ended
        self.led.update_run(rid, status="ended")
        self.assertEqual(
            review.effective_for_item(self.led, "x", 5, pr=88, sha="head1"),
            "unknown",
        )
        # Missing run (unknown run_id)
        self.led.set_kv("review:x#5", json.dumps({"sha": "head1", "pr": 88, "run_id": 99999}))
        self.assertEqual(
            review.effective_for_item(self.led, "x", 5, pr=88, sha="head1"),
            "unknown",
        )

    def test_ledger_effective_for_item_terminal_verdict_missing_bound_sha_or_pr(self):
        self.led.upsert_item("x", 5, pr=88, state="verifying")
        # Missing bound sha in terminal record
        self.led.set_kv("review:x#5", json.dumps({"pr": 88, "verdict": "pass"}))
        self.assertEqual(
            review.effective_for_item(self.led, "x", 5, pr=88, sha="head1"),
            "unknown",
        )
        # Missing bound pr in terminal record
        self.led.set_kv("review:x#5", json.dumps({"sha": "head1", "verdict": "pass"}))
        self.assertEqual(
            review.effective_for_item(self.led, "x", 5, pr=88, sha="head1"),
            "unknown",
        )
        # Fully bound terminal record matches
        self.led.set_kv("review:x#5", json.dumps({"sha": "head1", "pr": 88, "verdict": "pass"}))
        self.assertEqual(
            review.effective_for_item(self.led, "x", 5, pr=88, sha="head1"),
            "pass",
        )

    def test_finalization_after_interactive_preemption_leaves_item_not_pending(self):
        self.led.upsert_item("x", 5, pr=88, branch="mahler/5-feat", state="verifying")
        rid = self.led.create_run(project="x", number=5, role="review", platform="claude",
                                  epoch=1, status="running")
        self.led.claim("x", 5, f"run:{rid}", "auto", 30, run_id=rid)
        # Launch binding recorded without verdict=pending
        self.led.set_kv("review:x#5", json.dumps({"sha": "head1", "pr": 88, "run_id": rid}))
        self.assertEqual(
            review.effective_for_item(self.led, "x", 5, pr=88, sha="head1"),
            "pending",
        )

        # Interactive session pre-empts the item
        self.led.claim("x", 5, "session:user", "interactive", 30)

        # Run ends and finalizes
        log_path = os.path.join(self.tmp, "run.log")
        status_path = os.path.join(self.tmp, "exit_code")
        with open(log_path, "w") as fh:
            fh.write("Agent was preempted\n")
        with open(status_path, "w") as fh:
            fh.write("0\n")

        run_dict = {
            "id": rid, "project": "x", "number": 5, "role": "review", "platform": "claude",
            "epoch": 1, "pid": None, "worktree": os.path.join(self.tmp, "wt"),
            "branch": "mahler/5-feat", "log_path": log_path, "status_path": status_path,
            "stop_reason": "preempted", "started_at": iso(NOW),
        }
        ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
        with mock.patch.object(ctx, "ping"), mock.patch.object(ctx, "say"), \
                mock.patch.object(runner, "remove_worktree"):
            finalize.finalize(ctx, run_dict)

        # The run is ended
        recorded_run = self.led.run(rid)
        self.assertEqual(recorded_run["status"], "ended")

        # review:x#5 is NOT effectively pending, even though the launch binding remains
        self.assertEqual(
            review.effective_for_item(self.led, "x", 5, pr=88, sha="head1"),
            "unknown",
        )

    def test_console_and_ship_gate_agree_on_pending_review(self):
        self.led.upsert_item("x", 5, pr=88, branch="mahler/5-feat", state="verifying",
                             labels=json.dumps(["size:m"]))
        rid = self.led.create_run(project="x", number=5, role="review", platform="claude",
                                  epoch=1, status="running")
        self.led.claim("x", 5, f"run:{rid}", "auto", 30, run_id=rid)
        self.led.set_kv("ci:x#5:88", json.dumps({"state": "green", "sha": "head1"}))
        self.led.set_kv("review:x#5", json.dumps({"sha": "head1", "pr": 88, "run_id": rid}))

        # 1. Console description: waiting for independent review
        text, _ = state._verification_wait(self.led, "x", self.led.item("x", 5), NOW)
        self.assertEqual(text, " — waiting for the independent review.")

        # 2. Ship gate: does NOT merge or duplicate launch; says in progress
        ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
        say_mock = mock.Mock()
        ctx.say = say_mock
        view = {"headRefOid": "head1", "statusCheckRollup": [{"state": "SUCCESS"}]}
        with mock.patch.object(ship, "_merge_queued") as merge, \
                mock.patch.object(ship, "_start_review_run") as start:
            ship._review_gate(ctx, "x", self.led.item("x", 5), 88, view)
        merge.assert_not_called()
        start.assert_not_called()
        self.assertTrue(any("independent review in progress" in str(c) for c in say_mock.call_args_list))

    def test_console_and_ship_gate_agree_on_ended_run_not_pending(self):
        self.led.upsert_item("x", 5, pr=88, branch="mahler/5-feat", state="verifying",
                             labels=json.dumps(["size:s"]))
        rid = self.led.create_run(project="x", number=5, role="review", platform="claude",
                                  epoch=1, status="ended")
        self.led.set_kv("ci:x#5:88", json.dumps({"state": "green", "sha": "head1"}))
        self.led.set_kv("review:x#5", json.dumps({"sha": "head1", "pr": 88, "run_id": rid}))

        # For a size:s item whose review run ended without verdict, review is not required
        # Console: green, waiting for its turn to merge
        text, _ = state._verification_wait(self.led, "x", self.led.item("x", 5), NOW)
        self.assertEqual(text, " — green, waiting for its turn to merge.")

        # Ship gate: skips review gate straight to merge queued
        ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
        view = {"headRefOid": "head1", "statusCheckRollup": [{"state": "SUCCESS"}]}
        with mock.patch.object(ship, "_merge_queued") as merge, \
                mock.patch.object(ship, "_start_review_run") as start:
            ship._review_gate(ctx, "x", self.led.item("x", 5), 88, view)
        merge.assert_called_once()
        start.assert_not_called()

    def test_console_and_ship_gate_agree_on_passed_review(self):
        self.led.upsert_item("x", 5, pr=88, branch="mahler/5-feat", state="verifying",
                             labels=json.dumps(["size:m"]))
        self.led.set_kv("ci:x#5:88", json.dumps({"state": "green", "sha": "head1"}))
        self.led.set_kv("review:x#5", json.dumps({
            "sha": "head1", "pr": 88, "verdict": "pass", "run_id": 1,
        }))

        # Console: green, waiting for its turn to merge
        text, _ = state._verification_wait(self.led, "x", self.led.item("x", 5), NOW)
        self.assertEqual(text, " — green, waiting for its turn to merge.")

        # Ship gate: queues merge
        ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
        view = {"headRefOid": "head1", "statusCheckRollup": [{"state": "SUCCESS"}]}
        with mock.patch.object(review, "file_followups", return_value=True), \
                mock.patch.object(ship, "_merge_queued") as merge:
            ship._review_gate(ctx, "x", self.led.item("x", 5), 88, view)
        merge.assert_called_once()

    def test_console_and_ship_gate_agree_on_failed_review(self):
        self.led.upsert_item("x", 5, pr=88, branch="mahler/5-feat", state="verifying",
                             labels=json.dumps(["size:m"]))
        self.led.set_kv("ci:x#5:88", json.dumps({"state": "green", "sha": "head1"}))
        self.led.set_kv("review:x#5", json.dumps({
            "sha": "head1", "pr": 88, "verdict": "fail", "run_id": 1, "findings": "defect found",
        }))

        # Console: review failed; waiting for a fix run
        text, _ = state._verification_wait(self.led, "x", self.led.item("x", 5), NOW)
        self.assertEqual(text, " — review failed; waiting for a fix run.")

        # Ship gate: triggers a fix run
        ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
        view = {"headRefOid": "head1", "statusCheckRollup": [{"state": "SUCCESS"}]}
        with mock.patch.object(ship, "_review_triggered_fix") as fix, \
                mock.patch.object(ship, "_unchanged_done_review", return_value=False):
            ship._review_gate(ctx, "x", self.led.item("x", 5), 88, view)
        fix.assert_called_once()
        self.assertIn("defect found", fix.call_args.args[-1])

    def test_console_capacity_wait_uses_effective_review(self):
        self.led.upsert_item("x", 5, pr=88, state="verifying", labels=json.dumps(["size:m"]))
        retry = NOW + timedelta(hours=2)

        # 1. Review capacity wait when review is pending/needed
        self.led.set_kv("ci:x#5:88", json.dumps({"state": "green", "sha": "head1"}))
        self.led.set_kv("review:x#5", json.dumps({"sha": "head1", "pr": 88, "run_id": 1}))
        self.led.set_kv("review-wait:x#5", json.dumps({
            "state": "capacity_wait", "retry_at": iso(retry),
        }))
        text = state.capacity_wait_text(self.led, "x", self.led.item("x", 5), NOW)
        self.assertIn("review waiting for capacity", text)

        # 2. Fix capacity wait when review failed
        self.led.set_kv("review:x#5", json.dumps({"sha": "head1", "pr": 88, "verdict": "fail"}))
        self.led.set_kv("reviewfix-status:x#5", json.dumps({
            "state": "capacity_wait", "retry_at": iso(retry),
        }))
        text = state.capacity_wait_text(self.led, "x", self.led.item("x", 5), NOW)
        self.assertIn("fix waiting for capacity", text)

        # 3. Old failed review for old SHA ignores fail verdict and checks review wait for new SHA
        self.led.set_kv("ci:x#5:88", json.dumps({"state": "green", "sha": "new_head"}))
        self.led.set_kv("review:x#5", json.dumps({"sha": "old_head", "pr": 88, "verdict": "fail"}))
        self.led.set_kv("review-wait:x#5", json.dumps({
            "state": "capacity_wait", "retry_at": iso(retry),
        }))
        text = state.capacity_wait_text(self.led, "x", self.led.item("x", 5), NOW)
        self.assertIn("review waiting for capacity", text)

        # 4. Review passed: no capacity wait for review or fix
        self.led.set_kv("ci:x#5:88", json.dumps({"state": "green", "sha": "head1"}))
        self.led.set_kv("review:x#5", json.dumps({"sha": "head1", "pr": 88, "verdict": "pass"}))
        text = state.capacity_wait_text(self.led, "x", self.led.item("x", 5), NOW)
        self.assertEqual(text, "")



class TestConvergencePayload(unittest.TestCase):
    def test_evidence_validation_and_legacy_readability(self):
        import copy
        criterion = "- [ ] Questions remain parked"
        finding = dict(severity="blocking", category="spec", location="app.py:12",
                       scenario="New timing case", consequence="Question changes", done_when=criterion)
        body = "## Done when\n" + criterion
        def parse(f):
            return review.parse("fail", json.dumps({"findings": [f]}), body)
        self.assertEqual(parse(finding), [finding])
        evidence = dict(relation="new-edge-case", prior_sha="head-1", prior_scenario="Original case",
                        fixed=dict(location="app.py:12", evidence="Original case now passes"),
                        acceptance_tests=[dict(test="tests/test_app.py::test_parked", result="pass",
                                               evidence="Current head passes stated case")],
                        outside_tests="New timing not in stated cases")
        finding["convergence"] = evidence
        self.assertEqual(parse(finding), [finding])
        for key in evidence:
            bad = copy.deepcopy(finding)
            del bad["convergence"][key]
            with self.subTest(missing=key), self.assertRaises(ValueError):
                parse(bad)
        for relation in ("unresolved", "regression", "acceptance-failure"):
            protected = copy.deepcopy(finding)
            protected["convergence"]["relation"] = relation
            with self.assertRaises(ValueError):
                parse(protected)
            protected["convergence"]["defect"] = dict(location="app.py:12",
                test="tests/test_app.py::test_parked", evidence="First-fix test fails again")
            self.assertEqual(parse(protected), [protected])
        for bad_evidence in (None, [], {}, {**evidence, "relation": "guess"},
                             {**evidence, "acceptance_tests": []},
                             {**evidence, "acceptance_tests": [dict(test="case", result="maybe", evidence="unknown")]}):
            with self.subTest(evidence=bad_evidence), self.assertRaises(ValueError):
                parse({**finding, "convergence": bad_evidence})
