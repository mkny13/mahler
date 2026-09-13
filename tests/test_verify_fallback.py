"""No status line but green verify counts as DONE (mahler#17, DESIGN D18).

Tests the three branches:
  1. Fallback DONE: no status line, commits ahead, verify passes → verifying
  2. No commits ahead → failed attempt
  3. Verify fails → failed attempt
Plus the Cline resume-once nudge.
"""

import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from mahler import config, platforms, runner, scheduler
from mahler.ledger import Ledger, iso

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


class FakeGH:
    def __init__(self, state="OPEN"):
        self.state, self.comments = state, []

    def issue_state(self, number):
        return self.state

    def comment(self, number, body):
        self.comments.append(body)


class VerifyFallbackTests(unittest.TestCase):
    """D18 verify-green fallback: no STATUS line, commits ahead + verify → DONE."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["projects"]["x"] = {
            "path": self.tmp, "repo": "x/y",
            "verify": "python3 -m unittest discover -s tests",
        }
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.led.upsert_item("x", 5, state="working", priority=2, sorted_at=iso(NOW))
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
        self.gh = FakeGH()
        self.run_id = self.led.create_run(project="x", number=5, role="build",
                                          platform="agy-claude", epoch=1, status="running")
        self.led.claim("x", 5, f"run:{self.run_id}", "auto",
                       self.cfg["defaults"]["auto_lease_minutes"],
                       platform="agy-claude", run_id=self.run_id)
        self.log = os.path.join(self.tmp, "agent.log")
        wt = os.path.join(self.tmp, "wt")
        os.makedirs(wt, exist_ok=True)
        self.run = {"id": self.run_id, "project": "x", "number": 5, "role": "build",
                    "platform": "agy-claude", "epoch": 1, "pid": None, "nudged": 0,
                    "worktree": wt, "branch": "mahler/5-x",
                    "log_path": self.log, "status_path": os.path.join(self.tmp, "exit"),
                    "started_at": iso(NOW), "stop_reason": None}

    def _write_log_no_status(self):
        """An agy log with no STATUS line — the fallback's starting point."""
        with open(self.log, "w") as fh:
            fh.write(json.dumps({"event": "result", "result": {"status": "SUCCESS",
                                 "response": "All tests pass, code pushed."}}) + "\n")

    def finalize(self, commits=1, verify_ok=True, saved_ahead=None):
        if saved_ahead is None:
            saved_ahead = commits
        saved = {"ref": "mahler/snapshot/5-run7", "sha": "abc123", "ahead": saved_ahead, "stat": None} if saved_ahead > 0 else None
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(runner, "snapshot", return_value=saved), \
                mock.patch.object(runner, "remove_worktree"), \
                mock.patch.object(runner, "commits_ahead", return_value=commits), \
                mock.patch.object(runner, "verify_in_worktree", return_value=verify_ok), \
                mock.patch.object(self.ctx, "ping"):
            scheduler.finalize(self.ctx, self.run)

    def last_event(self):
        rows = self.led.q("SELECT detail FROM events WHERE project='x' AND number=5 "
                          "AND kind='state' ORDER BY at DESC LIMIT 1")
        return rows[0]["detail"] if rows else ""

    def test_fallback_done_commits_ahead_and_verify_green(self):
        """No status line, commits ahead, verify passes → verifying (the conductor ships it)."""
        self._write_log_no_status()
        self.finalize(commits=3, verify_ok=True)
        item = self.led.item("x", 5)
        self.assertEqual(item["state"], "verifying")
        self.assertEqual(item["attempts"], 0)          # not a failed attempt
        self.assertIn("verify-green fallback", self.last_event())
        # The kv flag tells the conductor to write 'unconfirmed' in the PR body
        self.assertEqual(self.led.get_kv("unconfirmed:x#5"), "1")

    def test_fallback_done_uncommitted_snapshot_and_verify_green(self):
        """No status line, 0 commits ahead in worktree, but snapshot has uncommitted
        work and verify passes → verifying (mahler#145)."""
        self._write_log_no_status()
        self.finalize(commits=0, verify_ok=True, saved_ahead=2)
        item = self.led.item("x", 5)
        self.assertEqual(item["state"], "verifying")
        self.assertEqual(item["attempts"], 0)
        self.assertIn("verify-green fallback", self.last_event())
        self.assertEqual(self.led.get_kv("unconfirmed:x#5"), "1")

    def test_no_commits_ahead_is_failed_attempt(self):
        """No status line, 0 commits ahead and no uncommitted snapshot → failed attempt, as today."""
        self._write_log_no_status()
        self.finalize(commits=0, verify_ok=True, saved_ahead=0)
        item = self.led.item("x", 5)
        self.assertEqual(item["state"], "ready")
        self.assertEqual(item["attempts"], 1)
        self.assertIn("attempt 1 failed", self.last_event())

    def test_verify_fails_is_failed_attempt(self):
        """No status line, commits ahead, verify fails → failed attempt."""
        self._write_log_no_status()
        self.finalize(commits=2, verify_ok=False)
        item = self.led.item("x", 5)
        self.assertEqual(item["state"], "ready")
        self.assertEqual(item["attempts"], 1)
        self.assertIn("attempt 1 failed", self.last_event())

    def test_no_verify_command_skips_fallback(self):
        """When the project has no verify command, the fallback is skipped."""
        self.cfg["projects"]["x"]["verify"] = ""
        self._write_log_no_status()
        self.finalize(commits=3, verify_ok=True)
        item = self.led.item("x", 5)
        self.assertEqual(item["state"], "ready")
        self.assertEqual(item["attempts"], 1)

    def test_fallback_with_timeout_fires_when_verify_green(self):
        """A run stopped for timeout gets fallback if verify passes (mahler#145)."""
        self._write_log_no_status()
        self.run["stop_reason"] = "timeout"
        self.finalize(commits=3, verify_ok=True)
        item = self.led.item("x", 5)
        self.assertEqual(item["state"], "verifying")
        self.assertEqual(item["attempts"], 0)
        self.assertIn("verify-green fallback", self.last_event())

    def test_fallback_with_timeout_and_uncommitted_snapshot_fires(self):
        """A timed-out run with only uncommitted changes snapshotted gets fallback if verify passes."""
        self._write_log_no_status()
        self.run["stop_reason"] = "timeout"
        self.finalize(commits=0, verify_ok=True, saved_ahead=2)
        item = self.led.item("x", 5)
        self.assertEqual(item["state"], "verifying")
        self.assertEqual(item["attempts"], 0)

    def test_fallback_with_timeout_fails_when_verify_red(self):
        """A run stopped for timeout with failing verify fails the attempt."""
        self._write_log_no_status()
        self.run["stop_reason"] = "timeout"
        self.finalize(commits=3, verify_ok=False)
        item = self.led.item("x", 5)
        self.assertEqual(item["state"], "ready")
        self.assertEqual(item["attempts"], 1)
        self.assertIn("attempt 1 failed", self.last_event())

    def test_non_timeout_stop_reason_doesnt_fire_verify_fallback(self):
        """A run stopped for e.g. parked or quota doesn't fire verify fallback."""
        self._write_log_no_status()
        self.run["stop_reason"] = "parked"
        self.finalize(commits=3, verify_ok=True)
        item = self.led.item("x", 5)
        self.assertEqual(item["state"], "parked")
        self.assertEqual(item["attempts"], 0)


class ClineNudgeTests(unittest.TestCase):
    """Cline resume-once: end with no STATUS and finishReason==completed → nudge."""

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
        self.exit_path = os.path.join(self.tmp, "exit")
        wt = os.path.join(self.tmp, "wt")
        os.makedirs(wt, exist_ok=True)
        self.run = {"id": self.run_id, "project": "x", "number": 5, "role": "build",
                    "platform": "cline-free", "epoch": 1, "pid": None, "nudged": 0,
                    "worktree": wt, "branch": "mahler/5-x",
                    "log_path": self.log, "status_path": self.exit_path,
                    "started_at": iso(NOW), "stop_reason": None}

    def _write_cline_log_completed_no_status(self):
        """A Cline log with finishReason=completed but no STATUS line."""
        with open(self.log, "w") as fh:
            fh.write(json.dumps({"type": "run_result",
                                 "text": "Now I'll run the backup...",
                                 "finishReason": "completed"}) + "\n")
        # Exit code 0
        with open(self.exit_path, "w") as fh:
            fh.write("0\n")

    def _write_cline_log_error_no_status(self):
        """A Cline log with a non-completed finish."""
        with open(self.log, "w") as fh:
            fh.write(json.dumps({"type": "run_result",
                                 "text": "Error happened",
                                 "finishReason": "error"}) + "\n")
        with open(self.exit_path, "w") as fh:
            fh.write("1\n")

    def test_cline_nudge_fires_on_completed_no_status(self):
        """Cline exit=0, finishReason=completed, no STATUS → nudge once."""
        self._write_cline_log_completed_no_status()
        fake_proc = mock.MagicMock()
        fake_proc.pid = 99999
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(runner, "snapshot", return_value=None), \
                mock.patch.object(runner, "remove_worktree"), \
                mock.patch.object(runner, "commits_ahead", return_value=0), \
                mock.patch("subprocess.Popen", return_value=fake_proc) as popen, \
                mock.patch.object(scheduler, "_cline_session_id", return_value="sess-42"):
            scheduler.finalize(self.ctx, self.run)
        # The run should be back to "running" (resumed), not ended
        db_run = self.led.run(self.run_id)
        self.assertEqual(db_run["status"], "running")
        self.assertEqual(db_run["nudged"], 1)
        self.assertEqual(db_run["pid"], 99999)
        # The item stays in working (not failed)
        self.assertEqual(self.led.item("x", 5)["state"], "working")
        self.assertEqual(self.led.item("x", 5)["attempts"], 0)
        # Popen was called to resume cline
        popen.assert_called_once()
        argv_str = popen.call_args[0][0][2]  # shell command
        self.assertIn("--id", argv_str)
        self.assertIn("sess-42", argv_str)
        self.assertIn("Carry on", argv_str)

    def test_cline_nudge_does_not_fire_twice(self):
        """A nudged cline run that ends again without STATUS is a failed attempt."""
        self._write_cline_log_completed_no_status()
        self.run["nudged"] = 1   # already nudged once
        self.led.update_run(self.run_id, nudged=1)
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(runner, "snapshot", return_value=None), \
                mock.patch.object(runner, "remove_worktree"):
            scheduler.finalize(self.ctx, self.run)
        item = self.led.item("x", 5)
        self.assertEqual(item["state"], "ready")
        self.assertEqual(item["attempts"], 1)

    def test_cline_nudge_skipped_on_non_zero_exit(self):
        """Cline exit=1 → no nudge, straight to failed attempt."""
        self._write_cline_log_error_no_status()
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(runner, "snapshot", return_value=None), \
                mock.patch.object(runner, "remove_worktree"):
            scheduler.finalize(self.ctx, self.run)
        item = self.led.item("x", 5)
        self.assertEqual(item["state"], "ready")
        self.assertEqual(item["attempts"], 1)

    def test_non_cline_platform_skips_nudge(self):
        """An agy run without STATUS doesn't get the Cline nudge."""
        self.run["platform"] = "agy-claude"
        with open(self.log, "w") as fh:
            fh.write(json.dumps({"event": "result", "result": {"status": "SUCCESS",
                                 "response": "Done."}}) + "\n")
        with open(self.exit_path, "w") as fh:
            fh.write("0\n")
        # No verify configured → fallback won't fire; nudge skipped for non-cline
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(runner, "snapshot", return_value=None), \
                mock.patch.object(runner, "remove_worktree"):
            scheduler.finalize(self.ctx, self.run)
        item = self.led.item("x", 5)
        self.assertEqual(item["state"], "ready")
        self.assertEqual(item["attempts"], 1)

    def test_verify_fallback_beats_cline_nudge(self):
        """When verify passes, the fallback fires — no nudge needed."""
        self.cfg["projects"]["x"]["verify"] = "true"
        self._write_cline_log_completed_no_status()
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(runner, "snapshot",
                                  return_value={"ref": "mahler/snapshot/5-run7",
                                                "sha": "abc", "ahead": 2, "stat": None}), \
                mock.patch.object(runner, "remove_worktree"), \
                mock.patch.object(runner, "commits_ahead", return_value=2), \
                mock.patch.object(runner, "verify_in_worktree", return_value=True), \
                mock.patch.object(self.ctx, "ping"):
            scheduler.finalize(self.ctx, self.run)
        item = self.led.item("x", 5)
        self.assertEqual(item["state"], "verifying")  # fallback DONE, not nudge
        self.assertEqual(item["attempts"], 0)


class PRBodyUnconfirmedTests(unittest.TestCase):
    """The PR body carries a note when the agent didn't confirm."""

    def test_pr_body_with_unconfirmed_note(self):
        from mahler.gh import pr_body
        body = pr_body(5, "wired the exporter", unconfirmed=True)
        self.assertIn("did not end with a STATUS line", body)
        self.assertIn("Fixes #5", body)
        self.assertIn("wired the exporter", body)

    def test_pr_body_without_unconfirmed_note(self):
        from mahler.gh import pr_body
        body = pr_body(5, "wired the exporter", unconfirmed=False)
        self.assertNotIn("did not end with a STATUS line", body)


if __name__ == "__main__":
    unittest.main()
