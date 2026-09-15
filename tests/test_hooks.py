import contextlib
import io
import unittest
import os
import json
import stat
import tempfile
import subprocess
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone
from mahler import cli, config, runner, scheduler, watchdog
from mahler.ledger import Ledger, iso, parse

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
        
        # Redirect the scheduler's runs dir into the temp tree (mahler#93):
        # patched rather than assigned, so the module global is restored
        # after each test instead of leaking to whichever test runs next.
        self.runs_dir = os.path.join(self.tmp.name, "runs")
        os.makedirs(self.runs_dir)
        runs_patch = patch.object(config, "RUNS_DIR", self.runs_dir)
        runs_patch.start()
        self.addCleanup(runs_patch.stop)

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
            
        # Claude Code hook schema (D6): event -> [{"matcher", "hooks":
        # [{"type": "command", "command"}]}]. A bare {"command": ...} entry
        # never fires, so the fence and heartbeats would silently do nothing.
        self.assertIn("hooks", d)
        self.assertIn("SessionStart", d["hooks"])
        self.assertIn("PreToolUse", d["hooks"])
        self.assertIn("PostToolUse", d["hooks"])
        self.assertIn("UserPromptSubmit", d["hooks"])

        def cmds(entries):
            return [h["command"] for grp in entries for h in grp.get("hooks", [])
                    if h.get("type") == "command"]
        self.assertEqual(cmds(d["hooks"]["SessionStart"]),
                         ["python3 .claude/hooks/session_start.py"])
        pt = d["hooks"]["PreToolUse"]
        self.assertEqual([grp.get("matcher") for grp in pt], ["Edit|Write|Bash"])
        self.assertEqual(cmds(pt), ["python3 .claude/hooks/pre_tool_use.py"])
        self.assertEqual(cmds(d["hooks"]["PostToolUse"]),
                         ["python3 .claude/hooks/heartbeat.py"])
        self.assertEqual(cmds(d["hooks"]["UserPromptSubmit"]),
                         ["python3 .claude/hooks/heartbeat.py"])
        
        # The yield check must honor MAHLER_RUNS_DIR so tests can keep
        # their hands off the real ~/.mahler state (mahler#93).
        script_path = os.path.join(self.repo_dir, ".claude", "hooks", "pre_tool_use.py")
        with open(script_path) as f:
            self.assertIn("MAHLER_RUNS_DIR", f.read())
        
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
            
        watchdog.watchdog(Ctx())

        yield_file = os.path.join(config.RUNS_DIR, "10", "yield")
        self.assertTrue(os.path.exists(yield_file))
        self.assertEqual(stat.S_IMODE(os.stat(yield_file).st_mode), 0o600)
        with open(yield_file) as f:
            self.assertEqual(f.read(), "")

        # The expired yield must also have moved the run to stopping, or the
        # yield file alone (with nothing acting on it) is a dead signal.
        row = self.led.q1("SELECT status, stop_reason FROM runs WHERE id=?", (10,))
        self.assertEqual(row["status"], "stopping")
        self.assertEqual(row["stop_reason"], "preempted")

    def test_yield_hook_logic(self):
        class Args:
            project = "testproj"
        cli.cmd_hooks(Args(), self.cfg, self.led)

        pre_tool_script = os.path.join(self.repo_dir, ".claude", "hooks", "pre_tool_use.py")

        # The hook must read the yield file from the runs dir it is told
        # about (MAHLER_RUNS_DIR): tests must never touch the real
        # ~/.mahler state (mahler#93).
        run_id = "123"
        with patch.dict(os.environ, {"MAHLER_RUN_ID": run_id,
                                     "MAHLER_RUNS_DIR": self.runs_dir}):
            yield_dir = os.path.join(self.runs_dir, run_id)
            os.makedirs(yield_dir, exist_ok=True)
            yield_file = os.path.join(yield_dir, "yield")
            with open(yield_file, "w") as f:
                pass

            res = subprocess.run(["python3", pre_tool_script], input="",
                                 capture_output=True, text=True)
            self.assertEqual(res.returncode, 1)
            self.assertIn("yield delivered", res.stdout)

            # Without the yield file the same tool call must go through: the
            # block above has to come from the yield check specifically, not
            # from the hook failing (or blocking) unconditionally.
            os.remove(yield_file)
            res = subprocess.run(["python3", pre_tool_script], input="",
                                 capture_output=True, text=True)
            self.assertEqual(res.returncode, 0)
            self.assertEqual(res.stdout, "")

    def test_merge_fence_logic(self):
        class Args:
            project = "testproj"
        cli.cmd_hooks(Args(), self.cfg, self.led)
        pre_tool_script = os.path.join(self.repo_dir, ".claude", "hooks", "pre_tool_use.py")
        bin_dir = os.path.abspath("bin")

        # A live lease at epoch 2: a run still claiming epoch 1 must be
        # fenced off from `gh pr merge|create`, and the current epoch must
        # not be. Asserting only "no item in the DB fails" (the old version
        # of this test) would pass even if the epoch comparison were
        # deleted entirely, since a missing item fails lease_check either way.
        self.led.set_state("testproj", 1, "ready")
        self.led.claim("testproj", 1, "run:124", "auto", 30)
        self.led.con.execute("UPDATE leases SET epoch = 2 WHERE project='testproj' AND number=1")
        self.led.con.commit()

        def run_hook(command, epoch):
            env = os.environ.copy()
            env.update({
                "MAHLER_RUN_ID": "124",
                # The hook's `mahler lease-check` runs in its own process:
                # MAHLER_HOME must point it at the temp ledger, or it falls
                # back to the real ~/.mahler state (mahler#93).
                "MAHLER_HOME": self.tmp.name,
                "MAHLER_PROJECT": "testproj",
                "MAHLER_ISSUE": "1",
                "MAHLER_EPOCH": epoch,
            })
            env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
            return subprocess.run(["python3", pre_tool_script],
                                  input=json.dumps({"command": command}),
                                  capture_output=True, text=True, env=env)

        # D6 fences both outward-facing steps: `gh pr create|merge`.
        for fenced in ("gh pr merge -s", "gh pr create --title x"):
            res = run_hook(fenced, "1")
            self.assertEqual(res.returncode, 1, fenced)
            self.assertIn("STALE", res.stdout)

        # The live epoch is not fenced off.
        res = run_hook("gh pr merge -s", "2")
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout, "")

        # Anything else a run does is not fenced on the epoch at all.
        res = run_hook("gh pr checks 5", "1")
        self.assertEqual(res.returncode, 0)


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


class TestInteractiveLeaseCommands(unittest.TestCase):
    """D6 layer 1 pinned at the CLI: `claim/heartbeat/release` go through the
    ledger's compare-and-set, interactive claims grant the policy TTL (30 min
    of inactivity, mahler#33-style per-session holders), heartbeats are
    fenced on holder+epoch, and `lease-check` works from the run env and
    allows anything outside a run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.led = Ledger(os.path.join(self.tmp.name, "mahler.db"))
        self.addCleanup(self.led.close)
        self.cfg = {"defaults": config.DEFAULTS["defaults"], "platforms": {},
                    "projects": {"testproj": {"enabled": True}}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_claim_grants_interactive_lease_with_policy_ttl(self):
        class Args:
            item = ("testproj", 7)
            holder = "aaaaaaaa"
            steal = False
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.cmd_claim(Args(), self.cfg, self.led), 0)
        lease = self.led.lease("testproj", 7)
        self.assertEqual(lease["kind"], "interactive")
        self.assertEqual(lease["holder"], "interactive:aaaaaaaa")
        self.assertEqual(lease["epoch"], 1)
        ttl = (parse(lease["expires_at"]) - parse(lease["heartbeat_at"])).total_seconds() / 60
        self.assertEqual(ttl, 30)  # interactive_lease_minutes default (D6)
        self.assertIn("(epoch 1)", out.getvalue())

    def test_heartbeat_renews_own_lease_and_refuses_a_taken_over_one(self):
        class Claim:
            item = ("testproj", 7)
            holder = "aaaaaaaa"
            steal = False
        class HB:
            item = ("testproj", 7)
            holder = "aaaaaaaa"
        with contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_claim(Claim(), self.cfg, self.led)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.cmd_heartbeat(HB(), self.cfg, self.led), 0)
            self.assertIn("renewed", out.getvalue())
            # a second session steals; the first session's heartbeat is refused
            Claim.holder = "bbbbbbbb"
            Claim.steal = True
            with contextlib.redirect_stdout(io.StringIO()):
                cli.cmd_claim(Claim(), self.cfg, self.led)
            Claim.holder = "aaaaaaaa"
            out2 = io.StringIO()
            with contextlib.redirect_stdout(out2):
                self.assertEqual(cli.cmd_heartbeat(HB(), self.cfg, self.led), 1)
            self.assertIn("you don't hold this item", out2.getvalue())

    def test_release_hands_the_item_back(self):
        class Args:
            item = ("testproj", 7)
            holder = "aaaaaaaa"
            steal = False
        class Rel:
            item = ("testproj", 7)
            holder = "aaaaaaaa"
        with contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_claim(Args(), self.cfg, self.led)
            self.assertEqual(cli.cmd_release(Rel(), self.cfg, self.led), 0)
        self.assertIsNone(self.led.lease("testproj", 7))
        self.assertEqual(self.led.item("testproj", 7)["state"], "ready")

    def test_lease_check_reads_the_run_env_and_allows_outside(self):
        class Args:
            item = ("testproj", 7)
            holder = "aaaaaaaa"
            steal = False
        with contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_claim(Args(), self.cfg, self.led)
        env = {"MAHLER_PROJECT": "testproj", "MAHLER_ISSUE": "7"}
        out = io.StringIO()
        with patch.dict(os.environ, {**env, "MAHLER_EPOCH": "1"}):
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.cmd_lease_check(SimpleNamespace(item=None, epoch=None),
                                                     self.cfg, self.led), 0)
        self.assertIn("ok — you still hold", out.getvalue())
        out = io.StringIO()
        with patch.dict(os.environ, {**env, "MAHLER_EPOCH": "2"}):  # stale epoch
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.cmd_lease_check(SimpleNamespace(item=None, epoch=None),
                                                     self.cfg, self.led), 1)
        self.assertIn("STALE", out.getvalue())
        out = io.StringIO()
        with patch.dict(os.environ, {}, clear=True):  # not inside a Mahler run
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.cmd_lease_check(SimpleNamespace(item=None, epoch=None),
                                                     self.cfg, self.led), 0)
        self.assertIn("allowed", out.getvalue())
