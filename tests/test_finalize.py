"""finalize: a build run that ends STATUS: DONE is a completed build (mahler#15).

DESIGN D18: the agent's job ends at commit+push+DONE; the conductor (code)
opens the PR and ships. Everything here runs against an in-memory Ledger with
gh, snapshot and worktree removal mocked: no GitHub, no subprocesses.
"""

import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler import config, finalize, router, runner, scheduler, tick
from mahler import gh as gh_module
from mahler.ledger import Ledger, iso

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


class FakeGH:
    def __init__(self, state="OPEN"):
        self.state, self.comments = state, []

    def issue_state(self, number):
        return self.state

    def comment(self, number, body):
        self.comments.append(body)


class RunTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["projects"]["x"] = {"path": self.tmp, "repo": "x/y"}
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)
        self.led.upsert_item("x", 5, state="working", priority=2, sorted_at=iso(NOW))
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
        self.gh = FakeGH()
        self.run_id = self.led.create_run(project="x", number=5, role="build",
                                          platform="cline-free", epoch=1, status="running")
        self.led.claim("x", 5, f"run:{self.run_id}", "auto",
                       self.cfg["defaults"]["auto_lease_minutes"],
                       platform="cline-free", run_id=self.run_id)
        self.log = os.path.join(self.tmp, "agent.log")
        self.run = {"id": self.run_id, "project": "x", "number": 5, "role": "build",
                    "platform": "cline-free", "epoch": 1, "pid": None,
                    "worktree": os.path.join(self.tmp, "wt"), "branch": "mahler/5-x",
                    "log_path": self.log, "status_path": os.path.join(self.tmp, "exit"),
                    "started_at": iso(NOW), "stop_reason": None}

    def test_finalization_persists_raw_accounting(self):
        self.cfg["platforms"]["cline-free"]["build_model"] = "gpt-6-sol"
        self.cfg["platforms"]["cline-free"]["cost_weight"] = 99
        with open(self.log, "w") as fh:
            json.dump({"type": "run_result", "finishReason": "completed",
                       "text": "STATUS: DONE implemented",
                       "aggregateUsage": {"inputTokens": 100, "outputTokens": 20,
                                          "cacheReadTokens": 50}}, fh)
        self.finalize()
        row = self.led.run(self.run_id)
        self.assertEqual(row["status"], "ended")
        self.assertEqual(row["model"], "gpt-6-sol")
        self.assertEqual(row["tokens_in"], 100)
        self.assertEqual(row["tokens_cached"], 50)
        self.assertEqual(row["tokens_out"], 20)
        self.assertEqual(row["tokens_reasoning"], 0)
        self.assertAlmostEqual(row["cost_usd"], .0005)
        self.assertEqual(row["cost_source"], "priced")

    def finalize(self):
        saved = {"ref": "mahler/snapshot/5-run7", "sha": "abc123", "ahead": 1, "stat": None}
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(runner, "snapshot", return_value=saved) as snap, \
                mock.patch.object(runner, "remove_worktree") as rm:
            finalize.finalize(self.ctx, self.run)
        return snap, rm

    def last_event(self):
        rows = self.led.q("SELECT detail FROM events WHERE project='x' AND number=5 "
                          "AND kind='state' ORDER BY at DESC LIMIT 1")
        return rows[0]["detail"] if rows else ""

    def test_done_is_a_completed_build_not_a_failed_attempt(self):
        with open(self.log, "w") as fh:
            fh.write("implemented it, tests pass\nSTATUS: DONE wired the exporter\n")
        with mock.patch.object(self.ctx, "ping") as ping:
            snap, rm = self.finalize()
        item = self.led.item("x", 5)
        self.assertEqual(item["state"], "verifying")   # the conductor ships it next (D18)
        self.assertEqual(item["summary"], "wired the exporter")
        self.assertEqual(item["attempts"], 0)          # not a failed attempt
        run = self.led.q("SELECT outcome, status FROM runs WHERE id=?",
                         (self.run_id,))[0]
        self.assertEqual((run["outcome"], run["status"]), ("DONE", "ended"))
        snap.assert_called_once()      # the work is saved before every handoff
        rm.assert_called_once()        # and the worktree is cleaned up
        ping.assert_called_once()
        # D18/D19: the item's canonical lease transfers to the conductor —
        # it is not simply released back to the pool.
        lease = self.led.lease("x", 5)
        self.assertIsNotNone(lease)
        self.assertEqual(lease["holder"], "conductor")

    def test_done_build_is_not_a_schedule_candidate(self):
        with open(self.log, "w") as fh:
            fh.write("STATUS: DONE everything green\n")
        self.finalize()
        cands = tick._candidates(self.ctx, [config.project_policy(self.cfg, "x")])
        self.assertEqual([(p["name"], r, it["number"]) for p, r, it in cands], [])

    def test_failed_exploration_preserves_attempt_and_escalation_budget(self):
        self.run["explore"] = 1
        self.led.update_run(self.run_id, explore=1)
        self.led.upsert_item("x", 5, attempts=2, esc_tier=1, esc_fails=1)
        with open(self.log, "w") as fh:
            fh.write("could not finish\n")
        self.finalize()
        item = self.led.item("x", 5)
        self.assertEqual((item["state"], item["attempts"], item["esc_tier"], item["esc_fails"]),
                         ("ready", 2, 1, 1))

    def test_no_status_line_is_still_a_failed_attempt(self):
        with open(self.log, "w") as fh:
            fh.write("Now opening the PR:\n")          # the mahler#8 failure mode
        self.finalize()
        item = self.led.item("x", 5)
        self.assertEqual(item["state"], "ready")       # retried, as today
        self.assertEqual(item["attempts"], 1)
        self.assertIn("attempt 1 failed", self.last_event())

    def test_needs_you_still_beats_done(self):
        with open(self.log, "w") as fh:
            fh.write("STATUS: NEEDS-YOU which licence key?\n")
        with mock.patch.object(self.ctx, "ping") as ping:
            self.finalize()
        item = self.led.item("x", 5)
        self.assertEqual(item["state"], "needs_you")
        self.assertIn("which licence key?", self.last_event())
        ping.assert_called_once()
        # mahler#257: a needs-you ping opens the console on that item
        self.assertEqual(ping.call_args.kwargs["priority"], "high")
        self.assertEqual(ping.call_args.kwargs["tags"], "question")
        self.assertTrue(ping.call_args.kwargs["console"])

    def test_needs_you_without_options_stores_the_whole_rest_as_the_question(self):
        with open(self.log, "w") as fh:
            fh.write("STATUS: NEEDS-YOU which licence key?\n")
        self.finalize()
        item = self.led.item("x", 5)
        self.assertEqual(item["question"], "which licence key?")
        self.assertEqual(json.loads(item["options"]), [])

    def test_needs_you_options_split_from_the_question(self):
        with open(self.log, "w") as fh:
            fh.write("STATUS: NEEDS-YOU Create a staging key or reuse prod? "
                     "OPTIONS: I'll create it | Reuse prod\n")
        with mock.patch.object(self.ctx, "ping") as ping:
            self.finalize()
        item = self.led.item("x", 5)
        self.assertEqual(item["state"], "needs_you")
        self.assertEqual(item["question"], "Create a staging key or reuse prod?")
        self.assertEqual(json.loads(item["options"]), ["I'll create it", "Reuse prod"])
        # the stored question and the event both exclude the OPTIONS suffix
        self.assertIn("Create a staging key or reuse prod?", self.last_event())
        self.assertNotIn("OPTIONS", self.last_event())
        # the ping carries the question, not the raw STATUS line
        ping.assert_called_once()
        self.assertEqual(ping.call_args.args[1], "Create a staging key or reuse prod?")

    def test_needs_you_options_case_insensitive_marker(self):
        with open(self.log, "w") as fh:
            fh.write("STATUS: NEEDS-YOU pick one options: A | B\n")
        self.finalize()
        item = self.led.item("x", 5)
        self.assertEqual(item["question"], "pick one")
        self.assertEqual(json.loads(item["options"]), ["A", "B"])

    def test_needs_you_options_capped_at_three_and_blanks_dropped(self):
        with open(self.log, "w") as fh:
            fh.write("STATUS: NEEDS-YOU pick one OPTIONS: A ||  B  | | C | D\n")
        self.finalize()
        item = self.led.item("x", 5)
        self.assertEqual(json.loads(item["options"]), ["A", "B", "C"])

    def test_needs_you_option_text_cut_to_forty_characters(self):
        long_choice = "x" * 60
        with open(self.log, "w") as fh:
            fh.write(f"STATUS: NEEDS-YOU pick one OPTIONS: {long_choice} | short\n")
        self.finalize()
        item = self.led.item("x", 5)
        options = json.loads(item["options"])
        self.assertEqual(options[0], "x" * 40)
        self.assertEqual(options[1], "short")

    def test_a_real_stop_keeps_its_own_handoff(self):
        with open(self.log, "w") as fh:
            fh.write("STATUS: DONE almost\n")
        self.led.claim("x", 5, "interactive:you", "interactive", 30)
        self.run["stop_reason"] = "preempted"
        self.finalize()
        item = self.led.item("x", 5)
        self.assertEqual(item["state"], "working")     # handed to your session
        self.assertEqual(item["attempts"], 0)          # a pre-emption is not a failure either
        self.assertNotIn("conductor ships it", self.last_event())

    def test_preempted_without_active_session_returns_to_ready(self):
        with open(self.log, "w") as fh:
            fh.write("STATUS: DONE almost\n")
        self.run["stop_reason"] = "preempted"
        self.finalize()
        item = self.led.item("x", 5)
        self.assertEqual(item["state"], "ready")       # no active session holding lease; returned to ready
        self.assertEqual(item["attempts"], 0)
        self.assertNotIn("conductor ships it", self.last_event())

    def test_yielded_returns_to_ready_without_counting_failure(self):
        for reason in (None, "preempted", "parked", "quota", "lost-lease", "timeout"):
            with self.subTest(reason=reason):
                self.run["stop_reason"] = reason
                self.led.upsert_item("x", 5, state="working", attempts=1,
                                     esc_fails=1, esc_tier=2)
                with open(self.log, "w") as fh:
                    fh.write("STATUS: YIELDED handed over\n")
                with mock.patch.object(self.ctx, "ping") as ping:
                    self.finalize()
                item = self.led.item("x", 5)
                self.assertEqual(item["state"], "ready")
                self.assertEqual((item["attempts"], item["esc_fails"], item["esc_tier"]),
                                 (1, 1, 2))
                run = self.led.q("SELECT outcome, status FROM runs WHERE id=?",
                                 (self.run_id,))[0]
                self.assertEqual((run["outcome"], run["status"]), ("YIELDED", "ended"))
                self.assertIsNone(self.led.lease("x", 5))
                self.assertIn("mahler/snapshot/5-run7", ping.call_args.args[1])
                self.assertEqual(ping.call_args.kwargs["priority"], "low")

    def test_yielded_hands_to_live_interactive_session(self):
        for reason in (None, "preempted", "parked", "quota", "lost-lease", "timeout"):
            with self.subTest(reason=reason):
                self.run["stop_reason"] = reason
                self.led.upsert_item("x", 5, state="working", attempts=1, esc_fails=1)
                self.led.claim("x", 5, "interactive:you", "interactive", 30)
                with open(self.log, "w") as fh:
                    fh.write("STATUS: YIELDED handed over\n")
                self.finalize()
                item = self.led.item("x", 5)
                self.assertEqual(item["state"], "working")
                self.assertEqual((item["attempts"], item["esc_fails"]), (1, 1))
                self.assertEqual(self.led.lease("x", 5)["holder"], "interactive:you")

    def test_blocked_counts_attempt_and_preserves_reason(self):
        with open(self.log, "w") as fh:
            fh.write("STATUS: BLOCKED needs a database password\n")
        self.finalize()
        item = self.led.item("x", 5)
        self.assertEqual(item["attempts"], 1)
        self.assertEqual(item["state"], "ready")
        outcome = "BLOCKED needs a database password"
        self.assertIn(outcome, self.last_event())
        run = self.led.q("SELECT outcome, status FROM runs WHERE id=?", (self.run_id,))[0]
        self.assertEqual((run["outcome"], run["status"]), (outcome, "ended"))
        self.assertIn(outcome, self.gh.comments[-1])

    def test_handoff_sets_ready_and_drops_lease_atomically(self):
        with open(self.log, "w") as fh:
            fh.write("reached quota\n")
        self.run["stop_reason"] = "quota"
        self.finalize()
        item = self.led.item("x", 5)
        self.assertEqual(item["state"], "ready")
        self.assertIsNone(self.led.lease("x", 5))

    def test_lost_lease_also_returns_to_ready(self):
        """A run that lost its lease (D6: stolen or reaped elsewhere) hands
        the item back to the queue exactly like a quota stop — reason
        'lost-lease' is its own branch, not covered by the quota case."""
        with open(self.log, "w") as fh:
            fh.write("someone else holds the lease now\n")
        self.run["stop_reason"] = "lost-lease"
        self.finalize()
        item = self.led.item("x", 5)
        self.assertEqual(item["state"], "ready")
        self.assertEqual(item["attempts"], 0)          # not a failed attempt
        self.assertIn("handoff (lost-lease)", self.last_event())

    def test_stop_and_hand_off_returns_to_ready_with_no_attempt(self):
        """A run stopped from the console (mahler#252) is a handoff, not a
        failed attempt: the item goes back to ready and the owner is pinged,
        exactly like a quota or lost-lease stop."""
        with open(self.log, "w") as fh:
            fh.write("stopped for a console handoff\n")
        self.run["stop_reason"] = "handoff"
        with mock.patch.object(self.ctx, "ping") as ping:
            self.finalize()
        item = self.led.item("x", 5)
        self.assertEqual(item["state"], "ready")
        self.assertEqual(item["attempts"], 0)          # not a failed attempt
        self.assertIn("handoff (handoff)", self.last_event())
        ping.assert_called_once()

    def test_claude_usage_and_quota_mirrors_to_opus_on_finalize(self):
        self.run["platform"] = "claude"
        ev = {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "unifiedWindows": {
                    "five_hour": {"utilization": 0.45, "resetsAt": 1789200000},
                    "seven_day": {"utilization": 0.65, "resetsAt": 1789200000},
                }
            }
        }
        res = {"type": "result", "result": "STATUS: DONE shipped it", "subtype": "success"}
        with open(self.log, "w") as fh:
            fh.write(json.dumps(ev) + "\n" + json.dumps(res) + "\n")
        self.finalize()
        # Both claude and claude-opus should have received the mirrored usage
        claude_5h = self.led.q("SELECT used_pct FROM usage WHERE platform='claude' AND window='5h' ORDER BY sampled_at DESC LIMIT 1")
        opus_5h = self.led.q("SELECT used_pct FROM usage WHERE platform='claude-opus' AND window='5h' ORDER BY sampled_at DESC LIMIT 1")
        self.assertTrue(claude_5h)
        self.assertTrue(opus_5h)
        self.assertEqual(claude_5h[0]["used_pct"], 45.0)
        self.assertEqual(opus_5h[0]["used_pct"], 45.0)

    def test_snapshot_failure_keeps_the_worktree_and_still_ends_the_run(self):
        with open(self.log, "w") as fh:
            fh.write("STATUS: DONE wired the exporter\n")
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(runner, "snapshot",
                                  side_effect=runner.GitError("no space left on device")), \
                mock.patch.object(runner, "remove_worktree") as rm, \
                mock.patch.object(self.ctx, "ping"):
            finalize.finalize(self.ctx, self.run)
        rm.assert_not_called()               # the worktree is kept, not removed
        run = self.led.q("SELECT status, outcome FROM runs WHERE id=?", (self.run_id,))[0]
        self.assertEqual((run["status"], run["outcome"]), ("ended", "DONE"))
        self.assertIn("snapshot failed, keeping worktree", "\n".join(self.ctx.lines))
        self.assertIn("Couldn't push a snapshot", self.gh.comments[-1])

    def test_handoff_comment_failure_does_not_break_finalize(self):
        with open(self.log, "w") as fh:
            fh.write("reached quota\n")
        self.run["stop_reason"] = "quota"
        saved = {"ref": "mahler/snapshot/5-run7", "sha": "abc123", "ahead": 1, "stat": None}
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(runner, "snapshot", return_value=saved), \
                mock.patch.object(runner, "remove_worktree"), \
                mock.patch.object(self.gh, "comment",
                                  side_effect=gh_module.GHError("rate limited")):
            finalize.finalize(self.ctx, self.run)   # must not raise
        item = self.led.item("x", 5)
        self.assertEqual(item["state"], "ready")
        self.assertIn("couldn't post handoff comment", "\n".join(self.ctx.lines))

    def test_cline_daily_cap_waits_until_the_named_reset_not_the_flat_backoff(self):
        # mahler#124: Cline's free model names its own reset time in the
        # error text ("Try again in 9h 41m") — honor it instead of always
        # retrying after backoff_minutes (60).
        with open(self.log, "w") as fh:
            fh.write(json.dumps({"error": {
                "code": "INFERENCE_CAP_ERROR",
                "message": "Error 429: Daily free limit reached on model "
                           "z-ai/glm-5.3-flash. Try again in 9h 41m"}}) + "\n")
        self.finalize()
        usage = self.led.q("SELECT resets_at, used_pct FROM usage WHERE platform='cline-free' "
                           "AND window='5h' ORDER BY sampled_at DESC LIMIT 1")
        self.assertTrue(usage)
        self.assertEqual(usage[0]["used_pct"], 100.0)
        resets_at = datetime.fromisoformat(usage[0]["resets_at"].replace("Z", "+00:00"))
        expected = NOW + timedelta(hours=9, minutes=41)
        self.assertLess(abs((resets_at - expected).total_seconds()), 60)
        run = self.led.q("SELECT stop_reason FROM runs WHERE id=?", (self.run_id,))[0]
        self.assertEqual(run["stop_reason"], "quota")

    def write_exit(self, code, when=NOW):
        with open(self.run["status_path"], "w") as fh:
            fh.write(str(code))
        os.utime(self.run["status_path"], (when.timestamp(), when.timestamp()))

    def test_successful_model_error_fix_ships_without_a_hold(self):
        self.write_exit(0)
        with open(self.log, "w") as fh:
            fh.write("Error: invalid model during an earlier retry\n")
            fh.write("STATUS: DONE Fixed invalid model handling\n")
        self.finalize()
        self.assertEqual(self.led.item("x", 5)["state"], "verifying")
        self.assertNotIn(router.HOLD, self.led.usage("cline-free"))

    def test_model_rejection_requires_failure_and_known_exit_time(self):
        log = {"model_unavailable": True}
        self.assertFalse(finalize._model_unavailable_fast(self.run, log, 1, None))
        self.write_exit(1)
        for code, ok, verb in ((0, None, None), (1, True, None), (1, None, "DONE")):
            with self.subTest(code=code, ok=ok, verb=verb):
                self.assertFalse(finalize._model_unavailable_fast(
                    self.run, {**log, "ok": ok}, code, verb))
        for seconds, expected in ((-1, False), (119, True), (120, False), (121, False)):
            self.write_exit(1, NOW + timedelta(seconds=seconds))
            self.assertEqual(finalize._model_unavailable_fast(self.run, log, 1, None), expected)

    def test_model_rejection_within_two_minutes_holds_without_spending_an_attempt(self):
        """Issue #420: a fast model-rejection error is excluded, not a failed
        attempt — the item's attempts/esc_fails are untouched, and the variant
        goes on a 24h hold with its own ping instead."""
        with open(self.log, "w") as fh:
            fh.write(json.dumps({"type": "error",
                                 "error": {"message": "model not found: bogus-model"}}) + "\n")
        # Exit at 90s, only observed at 130s.
        self.run["started_at"] = iso(NOW - timedelta(seconds=130))
        self.write_exit(1, NOW - timedelta(seconds=40))
        with mock.patch.object(self.ctx, "ping") as ping:
            self.finalize()
        item = self.led.item("x", 5)
        self.assertEqual(item["state"], "ready")
        self.assertEqual(item["attempts"], 0)
        self.assertEqual(item["esc_fails"], 0)
        ping.assert_called_once()
        self.assertIn("rejected its configured model", ping.call_args.args[1])
        hold = self.led.usage("cline-free").get(router.HOLD)
        self.assertIsNotNone(hold)
        until = datetime.fromisoformat(hold["resets_at"].replace("Z", "+00:00"))
        self.assertLess(abs((until - (NOW + timedelta(hours=24))).total_seconds()), 60)
        self.assertEqual(self.led.get_kv("hold_reason:cline-free"), "model_unavailable")

    def test_codex_named_model_rejection_does_not_spend_attempts(self):
        self.run["platform"] = "codex"
        self.write_exit(1)
        with open(self.log, "w") as fh:
            fh.write(json.dumps({"type": "turn.failed",
                                 "error": {"message": "Model 'foo' is not supported"}}) + "\n")
        with mock.patch.object(self.ctx, "ping") as ping:
            self.finalize()
        item = self.led.item("x", 5)
        self.assertEqual((item["attempts"], item["esc_fails"]), (0, 0))
        self.assertEqual(self.led.get_kv("hold_reason:codex"), "model_unavailable")
        hold = self.led.usage("codex")[router.HOLD]
        self.assertEqual(datetime.fromisoformat(hold["resets_at"]), NOW + timedelta(hours=24))
        ping.assert_called_once()

    def test_model_rejection_past_the_grace_period_is_a_normal_attempt(self):
        """A model that ran a while before erroring is a different problem —
        it must not spend the 24h hold on what might be a mid-run fluke."""
        self.run["started_at"] = iso(NOW - timedelta(minutes=5))
        self.write_exit(1)
        with open(self.log, "w") as fh:
            fh.write(json.dumps({"type": "error",
                                 "error": {"message": "model not found: bogus-model"}}) + "\n")
        self.finalize()
        item = self.led.item("x", 5)
        self.assertEqual(item["state"], "ready")
        self.assertEqual(item["attempts"], 1)
        self.assertNotIn(router.HOLD, self.led.usage("cline-free"))


class ResumeNudgeTests(unittest.TestCase):
    """Free builder resume tests (mahler#426): shared env, stdin redirection,
    2-resume cap, network error resumption, and kilo session-not-found fallback."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["accounts"]["work"] = {"env": {"GH_CONFIG_DIR": "~/.gh-work"}}
        self.cfg["projects"]["x"] = {
            "path": self.tmp, "repo": "x/y", "base": "main",
            "account": "work", "link": [], "run_timeout_minutes": 60,
        }
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)
        self.led.upsert_item("x", 5, state="working", priority=2, sorted_at=iso(NOW))
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
        self.gh = FakeGH()
        self.runs_dir = os.path.join(self.tmp, "runs")
        self.patch_runs = mock.patch.object(config, "RUNS_DIR", self.runs_dir)
        self.patch_runs.start()
        self.addCleanup(self.patch_runs.stop)

    def _make_run(self, platform="cline-free", nudged=0):
        run_id = self.led.create_run(project="x", number=5, role="build",
                                     platform=platform, epoch=3, status="running")
        self.led.update_run(run_id, nudged=nudged)
        self.led.claim("x", 5, f"run:{run_id}", "auto",
                       self.cfg["defaults"]["auto_lease_minutes"],
                       platform=platform, run_id=run_id)
        run_dir = os.path.join(self.runs_dir, str(run_id))
        os.makedirs(run_dir, exist_ok=True)
        log_path = os.path.join(run_dir, "agent.log")
        status_path = os.path.join(run_dir, "exit")
        run = {"id": run_id, "project": "x", "number": 5, "role": "build",
               "platform": platform, "epoch": 3, "pid": None,
               "worktree": os.path.join(self.tmp, "wt"), "branch": "mahler/5-x",
               "log_path": log_path, "status_path": status_path,
               "started_at": iso(NOW), "stop_reason": None, "nudged": nudged}
        return run, run_id, log_path, status_path

    def test_resume_env_includes_fence_epoch_and_gh_overlay(self):
        run, run_id, log_path, status_path = self._make_run("cline-free", nudged=0)
        with open(log_path, "w") as fh:
            fh.write(json.dumps({"type": "run_result", "text": "partial work",
                                 "finishReason": "completed"}) + "\n")
        with open(status_path, "w") as fh:
            fh.write("0\n")

        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(runner, "snapshot", return_value=None), \
                mock.patch.object(runner, "remove_worktree"), \
                mock.patch.object(runner, "commits_ahead", return_value=0), \
                mock.patch.object(runner, "fence_hooks", return_value="/runs/hooks"), \
                mock.patch.object(runner, "spawn", return_value=777) as spawn:
            finalize.finalize(self.ctx, run)

        spawn.assert_called_once()
        env = spawn.call_args.kwargs["env"]
        self.assertEqual(env["MAHLER_EPOCH"], "3")
        self.assertEqual(env["MAHLER_RUN_ID"], str(run_id))
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "core.hooksPath")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "/runs/hooks")
        self.assertEqual(env["GH_CONFIG_DIR"], os.path.expanduser("~/.gh-work"))
        self.assertEqual(self.led.run(run_id)["nudged"], 1)

    def test_two_resume_cap(self):
        # 1st resume: nudged 0 -> 1
        run, run_id, log_path, status_path = self._make_run("cline-free", nudged=0)
        with open(log_path, "w") as fh:
            fh.write(json.dumps({"type": "run_result", "text": "partial", "finishReason": "completed"}) + "\n")
        with open(status_path, "w") as fh:
            fh.write("0\n")
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(runner, "snapshot", return_value=None), \
                mock.patch.object(runner, "remove_worktree"), \
                mock.patch.object(runner, "fence_hooks", return_value="/hooks"), \
                mock.patch.object(runner, "spawn", return_value=101):
            finalize.finalize(self.ctx, run)
        self.assertEqual(self.led.run(run_id)["status"], "running")
        self.assertEqual(self.led.run(run_id)["nudged"], 1)

        # 2nd resume: nudged 1 -> 2
        run["nudged"] = 1
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(runner, "snapshot", return_value=None), \
                mock.patch.object(runner, "remove_worktree"), \
                mock.patch.object(runner, "fence_hooks", return_value="/hooks"), \
                mock.patch.object(runner, "spawn", return_value=102):
            finalize.finalize(self.ctx, run)
        self.assertEqual(self.led.run(run_id)["status"], "running")
        self.assertEqual(self.led.run(run_id)["nudged"], 2)

        # 3rd attempt: nudged 2 -> capped, does not resume
        run["nudged"] = 2
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(runner, "snapshot", return_value=None), \
                mock.patch.object(runner, "remove_worktree"), \
                mock.patch.object(runner, "spawn") as spawn:
            finalize.finalize(self.ctx, run)
        spawn.assert_not_called()
        self.assertEqual(self.led.run(run_id)["status"], "ended")
        self.assertEqual(self.led.item("x", 5)["attempts"], 1)

    def test_network_error_resumes_cline_on_exit_1(self):
        run, run_id, log_path, status_path = self._make_run("cline-free", nudged=0)
        with open(log_path, "w") as fh:
            fh.write(json.dumps({"type": "error", "message": "Network connection lost"}) + "\n")
        with open(status_path, "w") as fh:
            fh.write("1\n")

        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(runner, "snapshot", return_value=None), \
                mock.patch.object(runner, "remove_worktree"), \
                mock.patch.object(runner, "fence_hooks", return_value="/hooks"), \
                mock.patch.object(runner, "spawn", return_value=201) as spawn:
            finalize.finalize(self.ctx, run)
        spawn.assert_called_once()
        self.assertEqual(self.led.run(run_id)["status"], "running")
        self.assertEqual(self.led.run(run_id)["nudged"], 1)

    def test_network_error_resumes_kilo_on_exit_1(self):
        run, run_id, log_path, status_path = self._make_run("kilo", nudged=0)
        with open(log_path, "w") as fh:
            fh.write(json.dumps({"type": "step_start", "sessionID": "ses_kilo999",
                                 "part": {"type": "step-start"}}) + "\n")
            fh.write(json.dumps({"type": "error", "sessionID": "ses_kilo999",
                                 "error": {"message": "The socket connection was closed unexpectedly"}}) + "\n")
        with open(status_path, "w") as fh:
            fh.write("1\n")

        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(runner, "snapshot", return_value=None), \
                mock.patch.object(runner, "remove_worktree"), \
                mock.patch.object(runner, "fence_hooks", return_value="/hooks"), \
                mock.patch.object(runner, "spawn", return_value=202) as spawn:
            finalize.finalize(self.ctx, run)
        spawn.assert_called_once()
        argv = spawn.call_args.args[0]
        self.assertIn("--session", argv)
        self.assertEqual(argv[argv.index("--session") + 1], "ses_kilo999")
        self.assertEqual(self.led.run(run_id)["status"], "running")

    def test_kilo_session_not_found_uses_fresh_run_fallback(self):
        run, run_id, log_path, status_path = self._make_run("kilo", nudged=0)
        with open(log_path, "w") as fh:
            fh.write(json.dumps({"type": "step_start", "sessionID": "ses_stale",
                                 "part": {"type": "step-start"}}) + "\n")
            fh.write(json.dumps({"type": "error", "error": {"message": "Error: Session not found"}}) + "\n")
        with open(status_path, "w") as fh:
            fh.write("1\n")

        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(runner, "snapshot", return_value=None), \
                mock.patch.object(runner, "remove_worktree"), \
                mock.patch.object(runner, "fence_hooks", return_value="/hooks"), \
                mock.patch.object(runner, "spawn", return_value=203) as spawn:
            finalize.finalize(self.ctx, run)
        spawn.assert_called_once()
        argv = spawn.call_args.args[0]
        self.assertNotIn("--session", argv)
        self.assertIn("--dir", argv)
        self.assertEqual(self.led.run(run_id)["status"], "running")

    def test_quota_error_does_not_resume(self):
        run, run_id, log_path, status_path = self._make_run("cline-free", nudged=0)
        with open(log_path, "w") as fh:
            fh.write(json.dumps({"type": "run_result", "finishReason": "error",
                                 "text": "rate limit exceeded: 429"}) + "\n")
        with open(status_path, "w") as fh:
            fh.write("1\n")

        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(runner, "snapshot", return_value=None), \
                mock.patch.object(runner, "remove_worktree"), \
                mock.patch.object(runner, "spawn") as spawn:
            finalize.finalize(self.ctx, run)
        spawn.assert_not_called()
        self.assertEqual(self.led.run(run_id)["status"], "ended")
        self.assertEqual(self.led.run(run_id)["stop_reason"], "quota")

    def test_resume_writes_prompt_file_with_stdin_redirection(self):
        run, run_id, log_path, status_path = self._make_run("cline-free", nudged=0)
        with open(log_path, "w") as fh:
            fh.write(json.dumps({"type": "run_result", "text": "stop", "finishReason": "completed"}) + "\n")
        with open(status_path, "w") as fh:
            fh.write("0\n")

        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(runner, "snapshot", return_value=None), \
                mock.patch.object(runner, "remove_worktree"), \
                mock.patch.object(runner, "fence_hooks", return_value="/hooks"), \
                mock.patch.object(runner, "spawn", return_value=204) as spawn:
            finalize.finalize(self.ctx, run)

        spawn.assert_called_once()
        stdin_path = spawn.call_args.kwargs["stdin_path"]
        expected_path = os.path.join(self.runs_dir, str(run_id), "resume-1.md")
        self.assertEqual(stdin_path, expected_path)
        self.assertTrue(os.path.isfile(expected_path))
        with open(expected_path) as fh:
            prompt_content = fh.read()
        self.assertIn("You stopped before finishing. Carry on with the next step of your instructions, and end with the STATUS line.", prompt_content)
        self.assertIn("If your work is done, commit and push it first.", prompt_content)


if __name__ == "__main__":
    unittest.main()