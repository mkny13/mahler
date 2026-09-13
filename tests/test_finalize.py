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
from datetime import datetime, timezone
from unittest import mock

from mahler import config, runner, scheduler
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
            scheduler.finalize(self.ctx, self.run)
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
        cands = scheduler._candidates(self.ctx, [config.project_policy(self.cfg, "x")])
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
        self.run["stop_reason"] = "preempted"
        self.finalize()
        item = self.led.item("x", 5)
        self.assertEqual(item["state"], "working")     # handed to your session, as before
        self.assertEqual(item["attempts"], 0)          # a pre-emption is not a failure either
        self.assertNotIn("conductor ships it", self.last_event())

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


if __name__ == "__main__":
    unittest.main()