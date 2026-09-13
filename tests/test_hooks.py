import contextlib
import io
import unittest
import os
import json
import tempfile
import subprocess
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone
from mahler import cli, config, scheduler, runner
from mahler.ledger import Ledger, iso

class TestHooks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo_dir = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.repo_dir)
        self.db_path = os.path.join(self.tmp.name, "mahler.db")
        self.led = Ledger(self.db_path)
        self.addCleanup(self.led.close)
        self.cfg = {
            "defaults": config.DEFAULTS["defaults"],
            "platforms": config.DEFAULTS["platforms"],
            "projects": {"testproj": {"path": self.repo_dir, "enabled": True}}
        }
        
        # Override config.RUNS_DIR for watchdog tests
        config.RUNS_DIR = os.path.join(self.tmp.name, "runs")
        os.makedirs(config.RUNS_DIR)

    def tearDown(self):
        self.tmp.cleanup()
        
    def test_hooks_command(self):
        class Args:
            project = "testproj"
        
        cli.cmd_hooks(Args(), self.cfg, self.led)
        
        settings_path = os.path.join(self.repo_dir, ".claude", "settings.json")
        self.assertTrue(os.path.exists(settings_path))
        with open(settings_path) as f:
            d = json.load(f)
            
        self.assertIn("hooks", d)
        self.assertIn("SessionStart", d["hooks"])
        self.assertIn("PreToolUse", d["hooks"])
        
        pt = d["hooks"]["PreToolUse"][0]
        self.assertIn("pre_tool_use.py", pt["command"])
        self.assertEqual(pt["tools"], ["Edit", "Write", "Bash"])
        
    @patch("mahler.runner.alive", return_value=True)
    @patch("mahler.router.usage_state", return_value=("soft", ""))
    def test_watchdog_creates_yield_file(self, *mocks):
        self.led.set_state("testproj", 1, "ready")
        self.led.claim("testproj", 1, "run:10", "auto", 10)
        self.led.create_run(id=10, project="testproj", number=1, role="build", platform="claude", epoch=1, pid=9999, worktree="wt", branch="br", base_ref="main", log_path="log", status_path="exit", status="running")
        
        # Manually set yield_at (like request_stop does)
        self.led.update_run(10, yield_at="2026-09-12T00:00:00Z")
        
        class Ctx:
            led = self.led
            cfg = self.cfg
            dry_run = False
            def policy(self, proj):
                return config.project_policy(self.cfg, proj)
            def say(self, msg): pass
            
        scheduler.watchdog(Ctx())
        
        yield_file = os.path.join(config.RUNS_DIR, "10", "yield")
        self.assertTrue(os.path.exists(yield_file))

    def test_yield_hook_logic(self):
        class Args:
            project = "testproj"
        cli.cmd_hooks(Args(), self.cfg, self.led)
        
        pre_tool_script = os.path.join(self.repo_dir, ".claude", "hooks", "pre_tool_use.py")
        
        # Setup yield file
        run_id = "123"
        os.environ["MAHLER_RUN_ID"] = run_id
        yield_dir = os.path.expanduser(f"~/.mahler/runs/{run_id}")
        os.makedirs(yield_dir, exist_ok=True)
        yield_file = os.path.join(yield_dir, "yield")
        with open(yield_file, "w") as f:
            pass
            
        try:
            res = subprocess.run(["python3", pre_tool_script], capture_output=True, text=True)
            self.assertEqual(res.returncode, 1)
            self.assertIn("yield delivered", res.stdout)
        finally:
            os.remove(yield_file)
            del os.environ["MAHLER_RUN_ID"]

    def test_merge_fence_logic(self):
        class Args:
            project = "testproj"
        cli.cmd_hooks(Args(), self.cfg, self.led)
        pre_tool_script = os.path.join(self.repo_dir, ".claude", "hooks", "pre_tool_use.py")
        
        run_id = "124"
        os.environ["MAHLER_RUN_ID"] = run_id
        
        # Mock lease-check failure by setting up environment and running the script
        # The script calls `mahler lease-check`.
        # To fail `mahler lease-check`, we can set MAHLER_PROJECT, MAHLER_ISSUE, MAHLER_EPOCH and let it fail.
        os.environ["MAHLER_PROJECT"] = "testproj"
        os.environ["MAHLER_ISSUE"] = "1"
        os.environ["MAHLER_EPOCH"] = "1"
        
        # No item in DB, so lease-check fails.
        try:
            env = os.environ.copy()
            env["PATH"] = os.path.abspath("bin") + os.pathsep + env.get("PATH", "")
            # We mock the call by actually passing JSON to stdin
            stdin_data = json.dumps({"command": "gh pr merge -s"})
            res = subprocess.run(["python3", pre_tool_script], input=stdin_data, capture_output=True, text=True, env=env)
            self.assertEqual(res.returncode, 1)
            self.assertIn("STALE", res.stdout)
        finally:
            del os.environ["MAHLER_RUN_ID"]
            del os.environ["MAHLER_PROJECT"]
            del os.environ["MAHLER_ISSUE"]
            del os.environ["MAHLER_EPOCH"]


class TestSessionIdentity(unittest.TestCase):
    """mahler#33: two interactive sessions must not both show up as the same
    'interactive:you', and a session should learn another one is active."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.led = Ledger(os.path.join(self.tmp.name, "mahler.db"))
        self.addCleanup(self.led.close)
        self.cfg = {"defaults": config.DEFAULTS["defaults"], "platforms": {},
                    "projects": {"testproj": {"enabled": True}}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_holder_uses_the_session_id(self):
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "00a45724-4546-442f-a508-x"}):
            self.assertEqual(cli.default_holder(), "00a45724")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cli.default_holder(), "you")

    def test_other_interactive_holders_excludes_self_and_expired(self):
        now = self.led.now()
        self.led.con.execute(
            "INSERT INTO leases (project, number, holder, kind, epoch, acquired_at, "
            "heartbeat_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
            ("testproj", 1, "interactive:aaaaaaaa", "interactive", 1,
             iso(now), iso(now), iso(now + timedelta(minutes=30))))
        self.led.con.execute(
            "INSERT INTO leases (project, number, holder, kind, epoch, acquired_at, "
            "heartbeat_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
            ("testproj", 2, "interactive:bbbbbbbb", "interactive", 1,
             iso(now), iso(now), iso(now - timedelta(minutes=1))))  # expired
        others = cli.other_interactive_holders(self.led, "testproj", "interactive:aaaaaaaa")
        self.assertEqual(others, [])  # only the caller's own live lease exists
        others = cli.other_interactive_holders(self.led, "testproj", "interactive:zzzzzzzz")
        self.assertEqual(others, ["interactive:aaaaaaaa"])  # sees the other live one, not the expired one

    def test_claim_nudges_about_another_active_session(self):
        class Args:
            item = ("testproj", 1)
            holder = "aaaaaaaa"
            steal = False
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.cmd_claim(Args(), self.cfg, self.led)

        class Args2:
            item = ("testproj", 2)
            holder = "bbbbbbbb"
            steal = False
        out2 = io.StringIO()
        with contextlib.redirect_stdout(out2):
            cli.cmd_claim(Args2(), self.cfg, self.led)
        self.assertIn("interactive:aaaaaaaa", out2.getvalue())
        self.assertIn("own worktree", out2.getvalue())
        self.assertNotIn("Note:", out.getvalue())  # first claimant, nobody else active yet

    def test_session_start_script_flags_other_sessions(self):
        class Args:
            project = "testproj"
        with tempfile.TemporaryDirectory() as repo_dir:
            self.cfg["projects"]["testproj"]["path"] = repo_dir
            cli.cmd_hooks(Args(), self.cfg, self.led)
            script_path = os.path.join(repo_dir, ".claude", "hooks", "session_start.py")
            with open(script_path) as fh:
                script = fh.read()
            self.assertIn("CLAUDE_CODE_SESSION_ID", script)
            self.assertIn("Another interactive session", script)
