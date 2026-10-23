"""Copilot keeps its own login under a project's GitHub identity (mahler#1211).

A work-copilot run on an App-backed personal project used to hand Copilot the
App's installation token through GH_TOKEN, and Copilot refused it ("Access
denied by policy settings") on every run. The token now reaches `gh` through
the run's own config dir instead, and a refusal holds the platform for that
project without charging the item an attempt.
"""

import copy
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mahler import config, finalize, platforms, router, runner, scheduler
from mahler.ledger import Ledger, iso

NOW = datetime(2026, 10, 23, 13, 0, tzinfo=timezone.utc)

DENIED_LOG = (
    json.dumps({"type": "session.mcp_server_status_changed",
                "data": {"serverName": "github-mcp-server", "status": "connected"}}) + "\n"
    "Error: Access denied by policy settings (Request ID: DE6E:3F9040)\n"
    "\n"
    "Your Copilot CLI policy setting may be preventing access. This can happen when:\n"
    "  • Your Copilot subscription does not include this feature\n"
)


def app_ctx(d, platform_conf, accounts=None):
    policy = {"path": os.path.join(d, "repo"), "repo": "mkny13/mahler", "base": "main",
              "accounts": ["personal", "work"], "link": [], "rules": "",
              "run_timeout_minutes": 60}
    return SimpleNamespace(
        cfg={"platforms": platform_conf, "accounts": accounts or {
            "work": {"env": {"COPILOT_HOME": "~/.copilot-work",
                             "GH_CONFIG_DIR": "~/.config/gh-work"}}}},
        policy=lambda project: policy, say=lambda msg: None)


class RunEnvTests(unittest.TestCase):
    def run_env(self, d, platform, platform_conf, ambient=None, app=True):
        app_mock = mock.MagicMock()
        app_mock.agent_token.return_value = "ghs_scoped_agent_token_1211"
        with mock.patch.object(config, "RUNS_DIR", os.path.join(d, "runs")), \
                mock.patch.object(runner, "fence_hooks", return_value="/runs/42/hooks"), \
                mock.patch.object(config, "project_github_app",
                                  return_value=app_mock if app else None), \
                mock.patch.dict(os.environ, ambient or {"PATH": os.environ.get("PATH", "")},
                                clear=True):
            return runner.run_env(app_ctx(d, platform_conf), "mahler", 1123, platform, 42, 7)

    def test_work_copilot_on_app_project_keeps_its_own_login(self):
        with tempfile.TemporaryDirectory() as d:
            env = self.run_env(d, "work-copilot", {
                "work-copilot": {"kind": "copilot", "account": "work"}})
            # Copilot sees no GitHub token, so it uses the login stored under
            # its own COPILOT_HOME — never the App token.
            for var in ("GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN"):
                self.assertNotIn(var, env)
            self.assertEqual(env["COPILOT_HOME"], os.path.expanduser("~/.copilot-work"))
            self.assertNotIn("ghs_scoped_agent_token_1211", env.values())
            # gh still acts as the project's App identity, from a private file.
            gh_dir = os.path.join(d, "runs", "42", "gh")
            self.assertEqual(env["GH_CONFIG_DIR"], gh_dir)
            hosts = os.path.join(gh_dir, "hosts.yml")
            self.assertEqual(os.stat(hosts).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(gh_dir).st_mode & 0o777, 0o700)
            self.assertIn('oauth_token: "ghs_scoped_agent_token_1211"', Path(hosts).read_text())
            if shutil.which("gh"):
                out = subprocess.run(["gh", "auth", "token"], env={**env, "HOME": d},
                                     capture_output=True, text=True, timeout=30)
                self.assertEqual(out.stdout.strip(), "ghs_scoped_agent_token_1211")

    def test_other_kinds_keep_the_app_token_in_the_environment(self):
        with tempfile.TemporaryDirectory() as d:
            env = self.run_env(d, "work-codex", {
                "work-codex": {"kind": "codex", "account": "work"}})
            self.assertEqual(env["GH_TOKEN"], "ghs_scoped_agent_token_1211")
            self.assertFalse(os.path.exists(os.path.join(d, "runs", "42", "gh", "hosts.yml")))

    def test_a_token_the_platform_account_itself_carries_is_left_alone(self):
        # personal copilot, non-App project on the personal identity: the
        # daemon's own GH_TOKEN is the account's login, not an overlay.
        with tempfile.TemporaryDirectory() as d:
            env = self.run_env(d, "copilot", {"copilot": {"kind": "copilot"}},
                               ambient={"GH_TOKEN": "gho_personal_login", "PATH": "/bin"},
                               app=False)
            self.assertEqual(env["GH_TOKEN"], "gho_personal_login")
            self.assertFalse(os.path.exists(os.path.join(d, "runs", "42", "gh")))

    def test_an_overlay_token_from_another_account_is_moved_too(self):
        with tempfile.TemporaryDirectory() as d:
            env_in = {"GH_TOKEN": "gho_old", "PATH": "/bin"}
            ctx = app_ctx(d, {"work-copilot": {"kind": "copilot", "account": "work"}},
                          accounts={"work": {"env": {"COPILOT_HOME": "~/.copilot-work"}},
                                    "personal": {"env": {"GH_TOKEN": "gho_personal_overlay"}}})
            with mock.patch.object(config, "RUNS_DIR", os.path.join(d, "runs")), \
                    mock.patch.object(runner, "fence_hooks", return_value="/hooks"), \
                    mock.patch.object(config, "project_github_app", return_value=None), \
                    mock.patch.dict(os.environ, env_in, clear=True):
                env = runner.run_env(ctx, "mahler", 1123, "work-copilot", 42, 7)
            self.assertNotIn("GH_TOKEN", env)
            hosts = Path(env["GH_CONFIG_DIR"], "hosts.yml").read_text()
            self.assertIn('"gho_personal_overlay"', hosts)


class LogTests(unittest.TestCase):
    def read(self, text):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "agent.log")
            Path(path).write_text(text)
            return platforms.read_log(path, "copilot")

    def test_policy_denial_is_an_auth_failure_not_quota(self):
        res = self.read(DENIED_LOG)
        self.assertTrue(res["auth_denied"])
        self.assertFalse(res["quota_hit"])
        self.assertIn("Access denied by policy settings", res["last_error"])

    def test_structured_denial_event_counts_too(self):
        res = self.read(json.dumps({"type": "session.error", "data": {
            "message": "Access denied by policy settings"}}) + "\n")
        self.assertTrue(res["auth_denied"])

    def test_agent_prose_quoting_the_phrase_does_not(self):
        res = self.read(json.dumps({"type": "assistant.message", "data": {
            "content": "The old error was 'Access denied by policy settings'.\n"
                       "STATUS: DONE fixed"}}) + "\n")
        self.assertFalse(res["auth_denied"])


class FinalizeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["projects"]["x"] = {"path": self.tmp, "repo": "x/y"}
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)

    def run_once(self, text, exit_code="1"):
        self.led.upsert_item("x", 5, state="working", priority=2, sorted_at=iso(NOW))
        run_id = self.led.create_run(project="x", number=5, role="build",
                                     platform="copilot", epoch=1, status="running")
        self.led.claim("x", 5, f"run:{run_id}", "auto",
                       self.cfg["defaults"]["auto_lease_minutes"],
                       platform="copilot", run_id=run_id)
        d = os.path.join(self.tmp, str(run_id))
        os.makedirs(d)
        run = {"id": run_id, "project": "x", "number": 5, "role": "build",
               "platform": "copilot", "epoch": 1, "pid": None,
               "worktree": os.path.join(d, "wt"), "branch": "mahler/5-x",
               "log_path": os.path.join(d, "agent.log"),
               "status_path": os.path.join(d, "exit"),
               "started_at": iso(NOW), "stop_reason": None}
        Path(run["log_path"]).write_text(text)
        Path(run["status_path"]).write_text(exit_code)
        with mock.patch.object(self.ctx, "gh"), \
                mock.patch.object(runner, "snapshot", return_value=None), \
                mock.patch.object(runner, "remove_worktree"), \
                mock.patch.object(self.ctx, "ping") as ping:
            finalize.finalize(self.ctx, run)
        return run_id, ping

    def test_denial_holds_the_platform_for_the_project_without_an_attempt(self):
        run_id, ping = self.run_once(DENIED_LOG)
        item = self.led.item("x", 5)
        self.assertEqual((item["state"], item["attempts"]), ("ready", 0))
        self.assertEqual(self.led.run(run_id)["stop_reason"], "auth_denied")
        self.assertEqual(router.project_hold(self.led, "x", "copilot"),
                         NOW + timedelta(minutes=finalize.AUTH_DENIED_HOLD_MINUTES))
        # project-scoped: the platform itself is not on a global hold
        self.assertIsNone(router.project_hold(self.led, "other", "copilot"))
        self.assertNotIn(router.HOLD, self.led.usage("copilot"))
        self.assertEqual(ping.call_count, 1)
        self.assertIn("refused on x", ping.call_args.args[0])
        # a second denial while held refreshes the hold but doesn't ping again
        _, ping = self.run_once(DENIED_LOG)
        self.assertEqual(ping.call_count, 0)
        self.assertEqual(self.led.item("x", 5)["attempts"], 0)

    def test_a_run_that_reported_is_judged_on_its_report(self):
        self.assertFalse(finalize._auth_denied({"auth_denied": True}, 0, None))
        self.assertFalse(finalize._auth_denied({"auth_denied": True}, 1, "DONE"))
        self.assertFalse(finalize._auth_denied({"auth_denied": True, "ok": True}, 1, None))
        self.assertTrue(finalize._auth_denied({"auth_denied": True}, 1, None))


class RouterTests(unittest.TestCase):
    def setUp(self):
        user = {
            "accounts": {"work": {"env": {"COPILOT_HOME": "~/.copilot-work"},
                                  "routing": {"build": ["work-copilot", "codex-work"]}}},
            "platforms": {"work-copilot": {"from": "copilot", "account": "work",
                                           "metered": False},
                          "codex-work": {"from": "codex", "account": "work",
                                         "metered": False}},
            "projects": {
                "acme": {"enabled": True, "repo": "acme/app", "path": "/tmp/acme",
                         "account": "work"},
                "mine": {"enabled": True, "repo": "me/mine", "path": "/tmp/mine",
                         "account": "work"}},
        }
        self.cfg = config.resolve_platforms(config._merge(config.DEFAULTS, user))
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)
        self.until = NOW + timedelta(hours=6)
        self.led.set_kv(router.project_hold_key("mine", "work-copilot"), iso(self.until))

    def test_hold_applies_to_its_project_only(self):
        acme = config.project_policy(self.cfg, "acme")
        mine = config.project_policy(self.cfg, "mine")
        self.assertEqual(router.pick_for_project(self.cfg, self.led, acme, "build", size="s")[0],
                         "work-copilot")
        name, reasons = router.pick_for_project(self.cfg, self.led, mine, "build", size="s")
        self.assertEqual(name, "codex-work")
        self.assertTrue(any(r.startswith("work-copilot: on hold for mine") for r in reasons))
        # pins don't override it
        name, _ = router.pick_for_project(self.cfg, self.led, mine, "build",
                                          pin="work-copilot", size="s")
        self.assertIsNone(name)
        self.assertIsNone(router.preferred_for_project(
            self.cfg, self.led, mine, "build", "work-copilot", size="s"))
        # diagnose also checks the CLI is installed; CI has no copilot binary
        with mock.patch.object(platforms, "available", return_value=True):
            self.assertIn("hold", router.diagnose(self.cfg, self.led, mine, "build", size="s")["work-copilot"])
            self.assertEqual(router.diagnose(self.cfg, self.led, acme, "build", size="s")["work-copilot"],
                             ["eligible"])

    def test_capacity_recovery_reports_when_the_hold_lifts(self):
        mine = config.project_policy(self.cfg, "mine")
        self.led.set_kv(router.project_hold_key("mine", "codex-work"), iso(self.until))
        eligible, recovery, retry = router.capacity_recovery(self.cfg, self.led, mine, "build", size="s")
        self.assertIn("work-copilot", eligible)
        self.assertEqual((recovery, retry), (self.until, self.until))

    def test_expired_hold_lifts(self):
        self.led.set_kv(router.project_hold_key("mine", "work-copilot"),
                        iso(NOW - timedelta(minutes=1)))
        mine = config.project_policy(self.cfg, "mine")
        self.assertEqual(router.pick_for_project(self.cfg, self.led, mine, "build", size="s")[0],
                         "work-copilot")


if __name__ == "__main__":
    unittest.main()
