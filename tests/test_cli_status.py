"""Tests for mahler status output formatting (including item_url on sqlite3.Row)."""

import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from mahler import cli
from mahler.ledger import Ledger


class StatusCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        state = patch.object(cli.config, "STATE", self.tmp.name)
        state.start()
        self.addCleanup(state.stop)
        self.db_path = os.path.join(self.tmp.name, "mahler.db")
        self.led = Ledger(self.db_path)
        self.addCleanup(self.led.close)
        self.cfg = {
            "defaults": {},
            "platforms": {},
            "projects": {
                "proj": {
                    "enabled": True,
                    "repo": "owner/proj",
                },
            },
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_scope_counts_text_json_and_project_filter(self):
        self.cfg["projects"].update({
            "a": {"enabled": True, "scope": "label", "scope_label": "custom"},
            "b": {"enabled": True, "scope": "label", "scope_label": "mahler"}})
        self.led.set_kv("missing_scope:a", "2")
        self.led.set_kv("missing_scope:b", "3")
        self.led.set_kv("missing_scope:proj", "99")
        for project, expected in ((None, {"a": 2, "b": 3}), ("a", {"a": 2})):
            for as_json in (False, True):
                with self.subTest(project=project, json=as_json):
                    buf = io.StringIO()
                    with patch("sys.stdout", buf):
                        cli.cmd_status(SimpleNamespace(json=as_json, project=project),
                                       self.cfg, self.led)
                    if as_json:
                        self.assertEqual(json.loads(buf.getvalue())["missing_scope_counts"], expected)
                    else:
                        self.assertIn("a: 2 (custom)", buf.getvalue())
                        self.assertEqual("b: 3 (mahler)" in buf.getvalue(), project is None)
                        self.assertNotIn("proj: 99", buf.getvalue())

    def test_status_item_urls_with_sqlite_row(self):
        # Insert an item without PR
        self.led.upsert_item("proj", 1, title="Test Issue 1", state="ready")
        # Insert an item with PR
        self.led.upsert_item("proj", 2, title="Test Issue 2", state="working", pr=123)

        # Start a run
        run_id = self.led.create_run(
            project="proj", number=2, role="build", platform="platform1",
            epoch=1, worktree="/tmp/wt", branch="b1",
        )
        self.led.claim("proj", 2, f"run:{run_id}", "auto", 10, run_id=run_id, platform="platform1")

        # Record an event
        self.led.event("state", "proj", 2, "ready -> working")

        class Args:
            json = False
            project = None

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            ret = cli.cmd_status(Args(), self.cfg, self.led)

        self.assertEqual(ret, 0)
        output = buf.getvalue()
        self.assertIn(f"proj#2 — platform1 build run {run_id}, 0m", output)
        self.assertIn("https://github.com/owner/proj/issues/1", output)
        self.assertIn("https://github.com/owner/proj/pull/123", output)

    def test_interactive_claim_ignores_watch_lease_but_names_running_holder(self):
        from types import SimpleNamespace
        from mahler import config
        self.cfg["defaults"] = config.DEFAULTS["defaults"].copy()
        self.led.upsert_item("proj", 1, state="verifying", pr=91)
        self.led.upsert_item("proj", 2, state="ready")
        self.led.claim("proj", 1, "conductor", "auto", 10, capacity=False)
        claim = self.led.claim
        def bounded_claim(*args, **kwargs):
            return claim(*args, **kwargs, max_parallel=1)
        args = SimpleNamespace(item=("proj", 2), holder="test", steal=False)
        with patch.object(self.led, "claim", side_effect=bounded_claim), patch("sys.stdout", io.StringIO()):
            self.assertEqual(cli.cmd_claim(args, self.cfg, self.led), 0)
        self.led.release("proj", 2)
        self.led.release("proj", 1)
        run = self.led.create_run(project="proj", number=1, role="review",
                                  platform="claude", epoch=1)
        self.led.claim("proj", 1, f"run:{run}", "auto", 10,
                       platform="claude", run_id=run)
        buf = io.StringIO()
        with patch.object(self.led, "claim", side_effect=bounded_claim), patch("sys.stdout", buf):
            self.assertEqual(cli.cmd_claim(args, self.cfg, self.led), 1)
        self.assertIn(f"proj#1 — claude review run {run}", buf.getvalue())

    def test_status_capacity_retries_and_ordinary_items(self):
        import json
        from datetime import timedelta
        from types import SimpleNamespace
        from mahler.ledger import iso
        self.led.upsert_item("proj", 1, title="Waiting", state="verifying", pr=123)
        self.led.upsert_item("proj", 2, title="Ordinary", state="verifying", pr=124)
        retry = self.led.now() + timedelta(hours=2)
        for role, key, verdict in (("fix", "reviewfix-status", "fail"),
                                   ("review", "review-wait", "pending"),
                                   ("review", "review-wait", "fail")):
            self.led.set_kv("ci:proj#1:123", json.dumps({"state": "green"}))
            self.led.set_kv("review:proj#1", json.dumps({"verdict": verdict, "pr": 123}))
            if role == "review" and verdict == "fail":
                self.led.set_kv("ci:proj#1:123", json.dumps({"state": "green", "sha": "new"}))
                self.led.set_kv("review:proj#1", json.dumps({"verdict": "fail", "sha": "old", "pr": 123}))
                self.led.set_kv("reviewfix-status:proj#1", json.dumps({"state": "running"}))
            for retry_at, expected in (
                    (iso(retry), f"retry around {retry.astimezone():%b %d %H:%M %Z}"),
                    (None, "retry time unknown; will re-check")):
                with self.subTest(role=role, retry_at=retry_at):
                    self.led.set_kv(f"{key}:proj#1", json.dumps({
                        "state": "capacity_wait", "retry_at": retry_at}))
                    buf = io.StringIO()
                    with patch("sys.stdout", buf):
                        cli.cmd_status(SimpleNamespace(json=False, project=None), self.cfg, self.led)
                    output = buf.getvalue()
                    self.assertIn(f"{role} waiting for capacity; {expected}", output)
                    ordinary = next(line for line in output.splitlines() if "Ordinary" in line)
                    self.assertTrue(ordinary.endswith("https://github.com/owner/proj/pull/124"))
                    self.assertNotIn("capacity", ordinary)
            self.led.set_kv(f"{key}:proj#1", "")

    def test_status_quota_shows_reset_countdown_chips(self):
        # mahler#52: quota lines append `[5h in ...  · wk in ...]` chips
        # when a window has a fresh, still-future reset time.
        from datetime import timedelta
        from mahler.ledger import iso
        cfg = {
            "defaults": {},
            "platforms": {
                "claude": {"enabled": True, "kind": "claude",
                           "soft": {"5h": 60, "weekly": 70},
                           "hard": {"5h": 70, "weekly": 80},
                           "stale_minutes": 15},
            },
            "projects": {},
        }
        later = iso(self.led.now() + timedelta(hours=1, minutes=26))
        self.led.record_usage("claude", "5h", 42.0, later)

        class Args:
            json = False
            project = None

        buf = io.StringIO()
        with patch("sys.stdout", buf):
               ret = cli.cmd_status(Args(), cfg, self.led)

        self.assertEqual(ret, 0)
        output = buf.getvalue()
        self.assertRegex(output, r"\[5h in 1h 2[0-9]m\]")

    def test_status_shows_burst_note(self):
        """D23: during a burst, `mahler status` prints a burst note."""
        import copy
        from datetime import timedelta
        from mahler import config
        from mahler.ledger import iso
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["projects"] = {}
        # Claude usage within the weekly burst lead (2h), at 85%
        five_reset = iso(self.led.now() + timedelta(minutes=30))
        weekly_reset = iso(self.led.now() + timedelta(hours=2))
        self.led.record_usage("claude", "5h", 85.0, five_reset)
        self.led.record_usage("claude", "weekly", 85.0, weekly_reset)

        class Args:
            json = False
            project = None

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cli.cmd_status(Args(), cfg, self.led)
        output = buf.getvalue()
        self.assertIn("D23", output)
        self.assertIn("burst", output)

    def test_status_shows_api_credits_detail_without_secrets(self):
        """D41/mahler#903: text and JSON status show the confirmed grant,
        reserved/settled spend and blocking reason, never key material."""
        from mahler import config as config_mod
        from mahler.ledger import iso as iso_
        toml_text = """
[accounts.work]
env = {}
routing = { build = ["work-claude-api"] }

[platforms.work-claude-api]
from = "claude-api"
account = "work"
enabled = true

[api_credits]
enabled = true
workspace_id = "wrkspc_mahler"
api_key_expires = "2027-10-16"
admin_key_expires = "2027-10-16"

[[api_credits.grants]]
granted_at = "2026-10-09"
expires_at = "2026-10-15T00:00:00Z"
pool_usd = 260

[projects.work-only]
enabled = false
account = "work"
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.toml")
            with open(path, "w") as fh:
                fh.write(toml_text)
            cfg = config_mod.load(path)
        clock = lambda: datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            led = Ledger(os.path.join(tmp, "status.db"), clock=clock)
            try:
                led.set_kv("api_credits:key:claude-api@work",
                          json.dumps({"status": "ok", "at": iso_(clock())}))
                led.reserve_credit("claude-api@work", "2026-10-09", "attempt-1", 2.5, 20.0, run_id=1)

                class Args:
                    json = False
                    project = None

                buf = io.StringIO()
                with patch("sys.stdout", buf):
                    cli.cmd_status(Args(), cfg, led)
                output = buf.getvalue()
                self.assertIn("API credits (claude-api@work, D41)", output)
                self.assertIn("confirmed", output)
                self.assertIn("reserved/settled", output)
                self.assertNotIn("sk-ant", output)

                class JsonArgs:
                    json = True
                    project = None

                buf2 = io.StringIO()
                with patch("sys.stdout", buf2):
                    cli.cmd_status(JsonArgs(), cfg, led)
                payload = json.loads(buf2.getvalue())
                snap = payload["api_credits"]["work-claude-api"]
                self.assertTrue(snap["grant_confirmed"])
                self.assertEqual(snap["grant_id"], "2026-10-09")
                self.assertGreaterEqual(snap["reserved_exposure_usd"], 2.5)
                self.assertNotIn("sk-ant", buf2.getvalue())
            finally:
                led.close()


class DaemonUpdateStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.app = self.state / "app"
        self.app.mkdir()
        self.log = self.state / "logs" / "update.log"
        self.log.parent.mkdir()
        state = patch.object(cli.config, "STATE", self.tmp.name)
        state.start()
        self.addCleanup(state.stop)
        self.led = Ledger(":memory:")
        self.addCleanup(self.led.close)
        self.now = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
        clock = patch.object(self.led, "now", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.cfg = {"defaults": {}, "platforms": {}, "projects": {}}
        self.git("init", "-q", "-b", "main")
        self.git("commit", "-q", "--allow-empty", "-m", "installed")
        self.head = self.git("rev-parse", "HEAD")
        for i in range(2):
            self.git("commit", "-q", "--allow-empty", "-m", f"new {i}")
        self.candidate = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", self.candidate)
        self.git("checkout", "-q", "--detach", self.head)

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.app), "-c", "user.name=Test",
             "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", *args],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    def decision(self, hours, outcome="blocked", reason="checks_not_green",
                 head=None, candidate=None):
        at = (self.now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return (f"{at} head={head or self.head} candidate={candidate or self.candidate} "
                f"outcome={outcome} reason={reason}\n")

    def status(self, json_output=False):
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            result = cli.cmd_status(
                SimpleNamespace(json=json_output, project=None), self.cfg, self.led)
        self.assertEqual(result, 0)
        return buf.getvalue()

    def test_long_stall_reports_count_elapsed_latest_action_offline(self):
        self.log.write_text(self.decision(26.5, candidate=self.head)
                            + self.decision(1, reason="rollback_excluded"))
        before = self.log.read_bytes()
        with patch.object(cli.subprocess, "run", wraps=subprocess.run) as run:
            output = self.status()
        self.assertIn("2 commits behind fetched origin/main", output)
        self.assertIn("blocked for 26h 30m", output)
        self.assertIn("rollback_excluded: origin/main is the rolled-back bad_sha", output)
        self.assertEqual(self.log.read_bytes(), before)
        self.assertEqual([call.args[0][3] for call in run.call_args_list],
                         ["rev-parse", "rev-list"])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.head)
        data = json.loads(self.status(json_output=True))["daemon_update_stall"]
        self.assertEqual(data["commits_behind"], 2)
        self.assertEqual(data["blocked_seconds"], 26 * 3600 + 30 * 60)
        self.assertEqual(data["blocked_since"], "2026-10-02T09:30:00+00:00")
        self.assertEqual(data["reason"], "rollback_excluded")

    def test_threshold_and_recent_delay(self):
        for hours, warns in ((23.999, False), (24, True)):
            with self.subTest(hours=hours):
                self.log.write_text(self.decision(hours))
                self.assertEqual("WARNING: daemon update stalled" in self.status(), warns)

    def test_warning_clears_when_checkout_catches_up(self):
        self.log.write_text(self.decision(30))
        self.git("checkout", "-q", "--detach", self.candidate)
        self.assertNotIn("daemon update stalled", self.status())
        self.assertNotIn("daemon_update_stall", json.loads(self.status(True)))
        # Even diagnostics naming the now-current HEAD cannot produce a warning.
        self.log.write_text(self.decision(30, head=self.candidate))
        self.assertNotIn("daemon update stalled", self.status())

    def test_nonblocked_and_unproven_entries_break_streak(self):
        for interruption in (
                self.decision(25, outcome="updated", reason="ci_and_tests_passed"),
                self.decision(25, outcome="unchanged", reason="already_current"),
                self.decision(25, outcome="rolled_back", reason="tick_failed"),
                self.decision(25, head=self.candidate),
                self.decision(25, head="unknown"),
                self.decision(25, reason="unrecognized"),
                "legacy or malformed diagnostic\n",
                self.decision(25).replace("2026-10-02", "2026-99-02"),
                self.decision(-1),
                self.decision(31),
        ):
            with self.subTest(interruption=interruption):
                self.log.write_text(self.decision(30) + interruption + self.decision(1))
                self.assertNotIn("daemon update stalled", self.status())

    def test_rejected_tests_and_unknown_candidate_continue_streak(self):
        self.log.write_text(self.decision(25, outcome="rejected", reason="tests_failed")
                            + self.decision(1, reason="fetch_failed", candidate="unknown"))
        output = self.status()
        self.assertIn("blocked for 25h 0m", output)
        self.assertIn("fetch_failed: check origin access", output)

    def test_missing_malformed_and_unreadable_diagnostics(self):
        self.assertNotIn("daemon update stalled", self.status())
        for content in ("", "malformed\n", self.decision(30) + "partial",
                        self.decision(30) + "\N{SNOWMAN}\n"):
            with self.subTest(content=content):
                self.log.write_text(content)
                self.assertNotIn("daemon update stalled", self.status())
        with patch("builtins.open", side_effect=PermissionError("unreadable")):
            self.assertNotIn("daemon update stalled", self.status())

    def test_unavailable_git_evidence_omits_warning(self):
        self.log.write_text(self.decision(30))
        for error in (FileNotFoundError("git"), subprocess.TimeoutExpired("git", 5),
                      subprocess.CalledProcessError(128, "git")):
            with self.subTest(error=error), patch.object(cli.subprocess, "run", side_effect=error):
                self.assertNotIn("daemon update stalled", self.status())
        self.git("update-ref", "-d", "refs/remotes/origin/main")
        self.assertNotIn("daemon update stalled", self.status())


if __name__ == "__main__":
    unittest.main()
