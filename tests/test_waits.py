"""Conductor-side waits (DESIGN D52, mahler#1260)."""
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from unittest import mock

from mahler import config, finalize, no_change, ship, sync, waits
from mahler.ledger import CONDUCTOR, Ledger, iso, parse

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
        self.hold("github", resume="ready")
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


class SiteGH:
    """Just enough GitHub for the escalation sites (mahler#1261)."""

    def __init__(self):
        self.base_rollup = []
        self.pr_rollup = [{"name": "test", "conclusion": "FAILURE"}]

    def base_checks(self, base):
        return self.base_rollup

    def required_status_checks(self, base):
        return set()

    def pr_view(self, pr):
        return {"state": "OPEN", "headRefOid": "abc", "headRefName": "b",
                "baseRefName": "main", "statusCheckRollup": self.pr_rollup}

    def failed_run_log(self, branch, tail=150):
        return 1, "red"

    def comment(self, n, body):
        pass

    def issue_state(self, n):
        return "OPEN"


class SiteTests(unittest.TestCase):
    """Each conductor-side escalation site waits instead of asking the owner (#1261)."""

    def setUp(self):
        self.now = T0
        self.led = Ledger(":memory:", clock=lambda: self.now)
        self.addCleanup(self.led.close)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["projects"] = {"p": {"enabled": True, "repo": "x/p", "path": "/unused"}}
        self.pings, self.lines = [], []
        self.gh = SiteGH()
        self.ctx = SimpleNamespace(
            cfg=self.cfg, led=self.led, dry_run=False, say=self.lines.append,
            policy=lambda project: config.project_policy(self.cfg, project),
            ping=lambda *a, **kw: self.pings.append((a, kw)), gh=lambda project: self.gh,
            burst_lines=None)
        self.led.upsert_item("p", 1, title="t", state="verifying", pr=88, branch="b",
                             sorted_at=iso(T0))

    def advance(self, **kw):
        self.now += timedelta(**kw)

    def item(self):
        return self.led.item("p", 1)

    def record(self):
        return json.loads(self.led.get_kv("wait:p#1"))

    def assert_waiting(self, cause, resume="verifying"):
        self.assertEqual(self.item()["state"], "waiting")
        self.assertIsNone(self.item()["question"])
        rec = self.record()
        self.assertEqual((rec["cause"], rec["resume"]), (cause, resume))
        self.assertEqual(self.pings, [])

    def recheck_after(self, **kw):
        self.advance(**kw)
        waits.recheck(self.ctx, "p")

    # site 1
    def test_ci_pending_timeout_waits_and_restarts_timer_on_wake(self):
        view = {"headRefOid": "abc"}
        ship._ci_pending(self.ctx, "p", self.item(), 88, view)
        self.assertEqual(self.item()["state"], "verifying")
        self.advance(minutes=61)
        ship._ci_pending(self.ctx, "p", self.item(), 88, view)
        self.assert_waiting("ci_stalled")
        self.assertIsNone(self.led.lease("p", 1))
        self.recheck_after(minutes=15)
        self.assertEqual(self.item()["state"], "verifying")
        self.assertFalse(self.led.get_kv("ci:p#1:88"))

    # site 2
    def test_merge_queue_timeout_waits_and_resets_queue_since(self):
        self.led.set_kv("queue:p#1:88", json.dumps({"sha": "abc", "since": iso(self.now)}))
        self.advance(minutes=61)
        ship._merge_queued(self.ctx, "p", self.item(), 88, {"headRefOid": "abc"})
        self.assert_waiting("merge_queue")
        self.recheck_after(minutes=15)
        self.assertEqual(self.item()["state"], "verifying")
        self.assertEqual(json.loads(self.led.get_kv("queue:p#1:88"))["since"], iso(self.now))

    # site 3
    def test_missing_tier_waits_and_resumes_when_a_route_appears(self):
        ship._capacity_wait(self.ctx, "p", self.item(), "reviewfix-status:p#1", "abc",
                            "no route", "fix", ([], None, None), 0)
        self.assert_waiting("capacity")
        self.assertEqual(self.record()["wake"], {"role": "fix"})
        with mock.patch("mahler.router.pick_for_project", return_value=(None, ["x"])):
            self.recheck_after(minutes=15)
        self.assertEqual(self.item()["state"], "waiting")
        with mock.patch("mahler.router.pick_for_project", return_value=("claude", [])) as pick:
            self.recheck_after(minutes=30)
        self.assertEqual(pick.call_args.args[3], "fix")
        self.assertEqual(self.item()["state"], "verifying")
        self.assertFalse(self.led.get_kv("reviewfix-status:p#1"))

    def test_expired_capacity_wait_waits(self):
        status = "review-wait:p#1"
        self.led.set_kv(status, json.dumps({"sha": "abc", "at": iso(self.now - timedelta(hours=30))}))
        ship._capacity_wait(self.ctx, "p", self.item(), status, "abc", "busy", "review",
                            (["claude"], None, None))
        self.assert_waiting("capacity")
        self.assertEqual(self.record()["wake"], {"role": "review"})

    def test_legacy_capacity_marker_becomes_capacity_wait(self):
        self.led.set_state("p", 1, "needs_you", "old", question="old pause")
        ship._mark_capacity_wait(self.led, "p", self.item(), "review")
        ship.recover_capacity_waits(self.ctx, "p")
        self.assert_waiting("capacity")
        self.assertEqual(self.record()["wake"], {"role": "review"})
        self.assertFalse(self.led.get_kv("capacity-stranded:p#1"))

    # site 5 wake (the launch site is covered in test_tick_capacity)
    def test_worktree_conflict_wakes_when_branch_is_free_or_lease_gone(self):
        waits.hold(self.led, "p", 1, "worktree_conflict", "x", resume="verifying",
                   wake={"branch": "b", "path": "/elsewhere", "holder": None})
        with mock.patch("mahler.runner.find_worktree_for_branch", return_value="/elsewhere"):
            self.recheck_after(minutes=15)
        self.assertEqual(self.item()["state"], "waiting")
        with mock.patch("mahler.runner.find_worktree_for_branch", return_value=None) as find:
            self.recheck_after(minutes=30)
        find.assert_called_once_with("/unused", "b")
        self.assertEqual(self.item()["state"], "verifying")
        self.led.claim("p", 1, "human:mike", "interactive", 30)
        waits.hold(self.led, "p", 1, "worktree_conflict", "x", resume="verifying",
                   wake={"branch": "b", "path": "/elsewhere", "holder": "human:mike"})
        with mock.patch("mahler.runner.find_worktree_for_branch", return_value="/elsewhere"):
            self.recheck_after(minutes=15)
            self.assertEqual(self.item()["state"], "waiting")
            self.led.release("p", 1, holder="human:mike")
            self.recheck_after(minutes=30)
        self.assertEqual(self.item()["state"], "verifying")

    # site 6
    def red_ci(self, attempts=3):
        self.led.upsert_item("p", 1, attempts=attempts - 1, esc_tier=1, esc_fails=1)
        view = self.gh.pr_view(88)
        with mock.patch("mahler.ship.start", return_value=False):
            ship._red_ci(self.ctx, "p", self.item(), 88, view)

    def test_red_ci_at_max_attempts_repo_wide_waits_without_charge(self):
        self.gh.base_rollup = [{"name": "test", "conclusion": "FAILURE"}]
        self.red_ci()
        self.assert_waiting("ci_repo_wide")
        item = self.item()
        self.assertEqual((item["attempts"], item["esc_tier"], item["esc_fails"]), (2, 1, 1))
        self.assertFalse(self.led.get_kv("red:p#1:88:abc"))
        self.assertIsNone(self.led.lease("p", 1))

    def test_red_ci_at_max_attempts_with_active_blocker_waits_then_resumes(self):
        self.led.set_kv("ci_blocker:p", json.dumps({"reason": "x"}))
        self.red_ci()
        self.assert_waiting("ci_repo_wide")
        self.assertEqual(self.item()["attempts"], 2)
        self.recheck_after(minutes=15)
        self.assertEqual(self.item()["state"], "waiting")
        self.led.set_kv("ci_blocker:p", "")
        self.recheck_after(minutes=30)
        self.assertEqual(self.item()["state"], "verifying")

    def test_red_ci_at_max_attempts_on_own_failure_still_fails(self):
        self.gh.base_rollup = [{"name": "lint", "conclusion": "FAILURE"}]
        self.red_ci()
        self.assertEqual(self.item()["state"], "failed")
        self.assertEqual(self.item()["attempts"], 3)

    # site 7
    def test_inconclusive_review_cap_waits_six_hours_then_prefers_others(self):
        sha = "abc"
        seen = [{"run_id": i, "platform": f"p{i}"} for i in (1, 2, 3)]
        self.led.set_kv(f"reviewinconclusive:p#1:{sha}", json.dumps(seen))
        self.led.set_kv(finalize._review_kv_key("p", 1), json.dumps({"sha": sha, "run_id": 4}))
        e = SimpleNamespace(led=self.led, project="p", number=1, reason=None, outcome="x",
                            run={"id": 4, "platform": "p4"},
                            set_state=lambda state, why, **cols: self.led.set_state(
                                "p", 1, state, why, **cols))
        e.ping = lambda *a, **kw: self.pings.append((a, kw))
        finalize._review_inconclusive(e)
        self.assert_waiting("review_inconclusive")
        self.assertEqual(parse(self.record()["next_at"]) - self.now, timedelta(hours=6))
        self.recheck_after(minutes=15)
        self.assertEqual(self.item()["state"], "waiting")
        self.recheck_after(hours=6)
        self.assertEqual(self.item()["state"], "verifying")
        self.assertFalse(self.led.get_kv(f"reviewinconclusive:p#1:{sha}"))
        avoid = json.loads(self.led.get_kv(f"reviewinconclusive-avoid:p#1:{sha}"))
        self.assertEqual({r["platform"] for r in avoid}, {"p1", "p2", "p3", "p4"})

    def test_exhausted_no_change_claim_wakes_with_reset_counter(self):
        no_change.save(self.led, "p", 1, {"phase": "exhausted", "base_sha": "base",
                                          "inconclusive": 4})
        waits.hold(self.led, "p", 1, "review_inconclusive", "x", resume="verifying",
                   wake={"sha": "base"})
        self.recheck_after(hours=6)
        self.assertEqual(self.item()["state"], "verifying")
        record = no_change.read(self.led, "p", 1)
        self.assertEqual((record["phase"], record["inconclusive"]), ("waiting", 0))


class MigrationTests(SiteTests):
    """waits.migrate_legacy converts conductor-generated escalations once (#1261)."""

    CASES = (
        ("needs_you", "PR #88: CI still running after 61 min", "ci_stalled"),
        ("needs_you", "No fix run could start for PR #88 after 90 minutes: no route",
         "capacity"),
        ("needs_you", "setup failed 2 times in a row — the environment, not the task", "setup"),
        ("needs_you", "PR #88 passed checks but hasn't merged in 70 min — check the merge queue",
         "merge_queue"),
        ("needs_you", "Independent no-change reviews exhausted inconclusive attempts. "
                      "Review manually or retry?", "review_inconclusive"),
        ("needs_you", "4 reviews of this PR head ended without a verdict (platforms: a, b). "
                      "Review it yourself, or re-run it?", "review_inconclusive"),
    )

    def test_each_question_pattern_converts(self):
        for state, text, cause in self.CASES:
            with self.subTest(cause=cause, text=text):
                self.led.set_kv("waits-migrated:p", "")
                self.led.set_state("p", 1, state, text, question=text)
                self.assertEqual(waits.migrate_legacy(self.ctx, "p"), 1)
                self.assert_waiting(cause, "ready" if cause == "setup" else "verifying")

    def test_failed_state_event_detail_converts(self):
        self.led.set_state("p", 1, "failed", "PR #88: CI still running after 61 min")
        waits.migrate_legacy(self.ctx, "p")
        self.assert_waiting("ci_stalled")

    def test_ci_still_red_converts_only_when_repo_wide_now(self):
        self.led.set_state("p", 1, "failed", "CI still red on PR #88 after 3 attempts", attempts=3)
        waits.migrate_legacy(self.ctx, "p")
        self.assertEqual(self.item()["state"], "failed")
        self.led.set_kv("waits-migrated:p", "")
        self.led.set_kv("red:p#1:88:abc:before", json.dumps(
            {"attempts": 2, "esc_tier": 0, "esc_fails": 0}))
        self.gh.base_rollup = [{"name": "test", "conclusion": "FAILURE"}]
        waits.migrate_legacy(self.ctx, "p")
        self.assert_waiting("ci_repo_wide")
        self.assertEqual(self.item()["attempts"], 2)

    def test_legacy_inconclusive_pr_item_wakes_with_reset_and_avoid_list(self):
        seen = [{"run_id": i, "platform": f"p{i}"} for i in (1, 2, 3, 4)]
        self.led.set_kv("reviewinconclusive:p#1:abc", json.dumps(seen))
        text = "4 reviews of this PR head ended without a verdict (platforms: a, b). Re-run?"
        self.led.set_state("p", 1, "needs_you", text, question=text)
        waits.migrate_legacy(self.ctx, "p")
        self.assert_waiting("review_inconclusive")
        self.assertEqual(self.record()["wake"], {"sha": "abc"})
        self.recheck_after(hours=6)
        self.assertEqual(self.item()["state"], "verifying")
        self.assertFalse(self.led.get_kv("reviewinconclusive:p#1:abc"))
        avoid = json.loads(self.led.get_kv("reviewinconclusive-avoid:p#1:abc"))
        self.assertEqual({r["platform"] for r in avoid}, {"p1", "p2", "p3", "p4"})

    def test_legacy_inconclusive_migration_retries_after_head_lookup_outage(self):
        seen = [{"run_id": i, "platform": f"p{i}"} for i in (1, 2, 3, 4)]
        self.led.set_kv("reviewinconclusive:p#1:abc", json.dumps(seen))
        text = "4 reviews of this PR head ended without a verdict (platforms: a, b). Re-run?"
        self.led.set_state("p", 1, "needs_you", text, question=text)

        with mock.patch.object(self.gh, "pr_view", side_effect=RuntimeError("temporary outage")):
            self.assertEqual(waits.migrate_legacy(self.ctx, "p"), 0)
        self.assertEqual(self.item()["state"], "needs_you")
        self.assertFalse(self.led.get_kv("wait:p#1"))
        self.assertFalse(self.led.get_kv("waits-migrated:p"))

        self.assertEqual(waits.migrate_legacy(self.ctx, "p"), 1)
        self.assert_waiting("review_inconclusive")
        self.assertEqual(self.record()["wake"], {"sha": "abc"})
        self.recheck_after(hours=6)
        self.assertEqual(self.item()["state"], "verifying")
        self.assertFalse(self.led.get_kv("reviewinconclusive:p#1:abc"))
        avoid = json.loads(self.led.get_kv("reviewinconclusive-avoid:p#1:abc"))
        self.assertEqual({r["platform"] for r in avoid}, {"p1", "p2", "p3", "p4"})

    def test_legacy_no_change_item_resumes_review_not_build(self):
        no_change.save(self.led, "p", 1, {"phase": "exhausted", "base_sha": "base",
                                          "inconclusive": 4})
        self.led.upsert_item("p", 1, pr=None)
        text = ("Independent no-change reviews exhausted inconclusive attempts. "
                "Review manually or retry?")
        self.led.set_state("p", 1, "needs_you", text, question=text)
        waits.migrate_legacy(self.ctx, "p")
        self.assert_waiting("review_inconclusive")
        self.recheck_after(hours=6)
        self.assertEqual(self.item()["state"], "verifying")
        record = no_change.read(self.led, "p", 1)
        self.assertEqual((record["phase"], record["inconclusive"]), ("waiting", 0))

    def test_owner_question_is_left_alone(self):
        owner = ("[product] Should the weekly digest go out on Monday or Friday? "
                 "PR #88: the copy differs after 60 min of review")
        for text in ("Should the weekly digest go out on Monday or Friday?", owner,
                     "Which Stripe account should PR #88 charge against?"):
            with self.subTest(text=text):
                self.led.set_kv("waits-migrated:p", "")
                self.led.set_state("p", 1, "needs_you", text, question=text)
                self.assertEqual(waits.migrate_legacy(self.ctx, "p"), 0)
                self.assertEqual(self.item()["state"], "needs_you")
                self.assertEqual(self.item()["question"], text)

    def test_migration_runs_once(self):
        waits.migrate_legacy(self.ctx, "p")
        text = "PR #88: CI still running after 61 min"
        self.led.set_state("p", 1, "needs_you", text, question=text)
        self.assertEqual(waits.migrate_legacy(self.ctx, "p"), 0)
        self.assertEqual(self.item()["state"], "needs_you")


if __name__ == "__main__":
    unittest.main()
