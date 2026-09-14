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

from mahler import config, finalize, runner, scheduler, tick
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

    def test_done_build_is_not_a_schedule_candidate(self):
        with open(self.log, "w") as fh:
            fh.write("STATUS: DONE everything green\n")
        self.finalize()
        cands = tick._candidates(self.ctx, [config.project_policy(self.cfg, "x")])
        self.assertEqual([(p["name"], r, it["number"]) for p, r, it in cands], [])

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

    def test_handoff_sets_ready_and_drops_lease_atomically(self):
        with open(self.log, "w") as fh:
            fh.write("reached quota\n")
        self.run["stop_reason"] = "quota"
        self.finalize()
        item = self.led.item("x", 5)
        self.assertEqual(item["state"], "ready")
        self.assertIsNone(self.led.lease("x", 5))

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


if __name__ == "__main__":
    unittest.main()