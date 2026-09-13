"""Accounts (DESIGN D25): work logins beside this machine's own, never crossed.

A project spends only its own account's platforms, pins included; each
account's Claude login is its own run slot and quota; the burst and the
human-use flag stay with this machine's own Claude account.
"""

import os
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler import config, router, runner, scheduler
from mahler.gh import GH
from mahler.ledger import Ledger, iso

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
LATER = iso(NOW + timedelta(hours=6))


def work_cfg(**projects):
    user = {
        "concurrency": {"total": 3},
        "accounts": {"work": {
            "env": {"CLAUDE_CONFIG_DIR": "~/.claude-work", "COPILOT_HOME": "~/.copilot-work",
                    "CODEX_HOME": "~/.codex-work"},
            "routing": {"sort": ["claude-work"], "plan": ["claude-opus-work"],
                        "build": ["copilot-work", "codex-work", "claude-opus-work",
                                  "claude-work"]},
        }},
        "platforms": {
            "claude-work": {"from": "claude", "account": "work"},
            "claude-opus-work": {"from": "claude-opus", "account": "work"},
            "codex-work": {"from": "codex", "account": "work"},
            "copilot-work": {"from": "copilot", "account": "work", "metered": False},
        },
        "projects": projects or {
            "acme": {"enabled": True, "repo": "acme/app", "path": "/tmp/acme",
                     "account": "work", "hot_hold": False},
            "home": {"enabled": True, "repo": "me/home", "path": "/tmp/home",
                     "hot_hold": False},
        },
    }
    return config.resolve_platforms(config._merge(config.DEFAULTS, user))


def seed(led, **usage):
    for name, (five, weekly) in usage.items():
        led.record_usage(name, "5h", five, LATER)
        led.record_usage(name, "weekly", weekly, LATER)


class ConfigTests(unittest.TestCase):
    def test_from_inherits_the_base_platform(self):
        p = work_cfg()["platforms"]["claude-work"]
        self.assertEqual(p["kind"], "claude")
        self.assertEqual(p["soft"], config.DEFAULTS["platforms"]["claude"]["soft"])
        self.assertEqual(p["account"], "work")

    def test_another_account_gets_its_own_quota_group(self):
        plats = work_cfg()["platforms"]
        self.assertEqual(plats["claude-work"]["quota_group"], "claude@work")
        self.assertEqual(plats["claude-opus-work"]["quota_group"], "claude@work")
        self.assertEqual(plats["claude"]["quota_group"], "claude")

    def test_bad_from_disables_the_platform_instead_of_raising(self):
        cfg = config._merge(config.DEFAULTS,
                            {"platforms": {"x": {"from": "nope", "account": "work"},
                                           "y": {"from": "y"}}})
        plats = config.resolve_platforms(cfg)["platforms"]
        self.assertFalse(plats["x"]["enabled"])
        self.assertFalse(plats["y"]["enabled"])

    def test_own_account_inherits_the_environment_as_is(self):
        self.assertIsNone(config.run_env(work_cfg(), "personal"))

    def test_undefined_account_fails_closed(self):
        with self.assertRaises(ValueError):
            config.run_env(work_cfg(), "wrok")

    def test_work_env_drops_inherited_logins_and_sets_its_own(self):
        base = {"PATH": "/bin", "GH_TOKEN": "personal", "ANTHROPIC_API_KEY": "personal",
                "CLAUDE_CONFIG_DIR": "/Users/me/.claude"}
        env = config.run_env(work_cfg(), "work", base=base)
        self.assertEqual(env["PATH"], "/bin")
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], os.path.expanduser("~/.claude-work"))
        self.assertEqual(env["CODEX_HOME"], os.path.expanduser("~/.codex-work"))


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.cfg = work_cfg()
        self.led = Ledger(":memory:", clock=lambda: NOW)
        seed(self.led, **{"claude": (5, 5), "claude-opus": (5, 5), "agy-claude": (10, 10),
                          "agy-gemini": (10, 10), "claude-work": (5, 5),
                          "claude-opus-work": (5, 5)})

    def test_work_project_routes_only_to_work_platforms(self):
        for role in ("sort", "plan", "build", "fix"):
            names = router.candidates(self.cfg, role, account="work")
            self.assertTrue(names)
            self.assertTrue(all(n.endswith("-work") for n in names), (role, names))
        self.assertEqual(router.pick(self.cfg, self.led, "sort", account="work")[0],
                         "claude-work")
        self.assertEqual(router.pick(self.cfg, self.led, "plan", account="work")[0],
                         "claude-opus-work")

    def test_personal_project_never_gets_a_work_platform(self):
        self.cfg["routing"]["build"] = ["claude-work", "codex-work", "agy-claude"]
        self.assertEqual(router.candidates(self.cfg, "build"), ["agy-claude"])

    def test_pin_across_accounts_is_refused(self):
        name, reasons = router.pick(self.cfg, self.led, "build", pin="claude",
                                    account="work")
        self.assertIsNone(name)
        self.assertIn("claude: pinned, but it spends the personal account, not work",
                      reasons)
        name, _ = router.pick(self.cfg, self.led, "build", pin="claude-work", account="work")
        self.assertEqual(name, "claude-work")

    def test_account_without_routing_gets_nothing(self):
        self.assertEqual(router.candidates(self.cfg, "build", account="other"), [])

    def test_burst_never_lifts_work_claude(self):
        seed(self.led, **{"claude": (85, 85), "claude-work": (85, 85)})
        burst = {"5h": (90, 97), "weekly": (90, 97)}
        pc = self.cfg["platforms"]
        self.assertEqual(router.usage_state(self.led, "claude", pc["claude"], burst)[0], "ok")
        self.assertEqual(
            router.usage_state(self.led, "claude-work", pc["claude-work"], burst)[0], "hard")


class SlotAndUsageTests(unittest.TestCase):
    def setUp(self):
        self.cfg = work_cfg()
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=True)

    def test_each_claude_login_has_its_own_run_slot(self):
        self.led.create_run(project="home", number=1, role="build", platform="claude", epoch=1)
        with mock.patch.object(scheduler.platforms, "available", return_value=True):
            busy = scheduler.busy_platforms(self.cfg, self.led.active_runs())
        self.assertIn("claude-opus", busy)
        self.assertNotIn("claude-work", busy)
        self.assertNotIn("claude-opus-work", busy)

    def test_work_readings_land_only_on_work_platforms(self):
        seed(self.led, claude=(30, 30), **{"claude-work": (30, 30)})
        scheduler._record_claude_usage(self.ctx, [("5h", 50.0, LATER), ("weekly", 60.0, LATER)],
                                       check_human=True, platform="claude-work")
        self.assertEqual(self.led.usage("claude-work")["5h"]["used_pct"], 50.0)
        self.assertEqual(self.led.usage("claude-opus-work")["weekly"]["used_pct"], 60.0)
        self.assertEqual(self.led.usage("claude")["5h"]["used_pct"], 30.0)
        self.assertIsNone(self.led.get_kv("human:claude"))

    def test_personal_readings_never_land_on_work_platforms(self):
        scheduler._record_claude_usage(self.ctx, [("5h", 50.0, LATER)])
        self.assertEqual(self.led.usage("claude")["5h"]["used_pct"], 50.0)
        self.assertEqual(self.led.usage("claude-work"), {})

    def test_refresh_probes_the_work_login_with_its_own_env(self):
        self.led.upsert_item("acme", 1, state="ready", priority=2)
        projects = [config.project_policy(self.cfg, "acme")]
        with mock.patch.object(scheduler.platforms, "oauth_usage",
                               return_value=[("5h", 11.0, LATER), ("weekly", 12.0, LATER)]) as oauth, \
                mock.patch.object(scheduler.platforms, "probe_claude",
                                  return_value=[("5h", 21.0, LATER), ("weekly", 22.0, LATER)]) as probe, \
                mock.patch.object(scheduler.platforms, "probe_copilot", return_value=[]) as copilot, \
                mock.patch.object(scheduler.platforms, "probe_agy", return_value={}) as agy, \
                mock.patch.object(scheduler.router, "peak_state", return_value=(False, None)):
            scheduler.refresh_usage(self.ctx, projects)
        oauth.assert_not_called()          # no credentials source configured for work yet
        env = probe.call_args.kwargs["env"]
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], os.path.expanduser("~/.claude-work"))
        self.assertEqual(self.led.usage("claude-work")["5h"]["used_pct"], 21.0)
        self.assertEqual(self.led.usage("claude"), {})
        copilot.assert_not_called()        # no work GitHub login: never read the personal one
        agy.assert_not_called()

    def test_work_oauth_reads_the_configured_keychain_entry(self):
        self.cfg["accounts"]["work"]["claude_keychain_service"] = "Claude Code-credentials-abc"
        self.led.upsert_item("acme", 1, state="ready", priority=2)
        projects = [config.project_policy(self.cfg, "acme")]
        with mock.patch.object(scheduler.platforms, "oauth_usage",
                               return_value=[("5h", 11.0, LATER), ("weekly", 12.0, LATER)]) as oauth, \
                mock.patch.object(scheduler.platforms, "probe_claude") as probe, \
                mock.patch.object(scheduler.platforms, "probe_copilot", return_value=[]), \
                mock.patch.object(scheduler.router, "peak_state", return_value=(False, None)):
            scheduler.refresh_usage(self.ctx, projects)
        oauth.assert_called_with(keychain_service="Claude Code-credentials-abc")
        probe.assert_not_called()
        self.assertEqual(self.led.usage("claude-opus-work")["weekly"]["used_pct"], 12.0)


class ScheduleTests(unittest.TestCase):
    def test_work_and_personal_items_each_get_their_own_accounts_platform(self):
        cfg = work_cfg()
        led = Ledger(":memory:", clock=lambda: NOW)
        seed(led, **{"agy-claude": (10, 10), "agy-gemini": (10, 10), "claude": (5, 5),
                     "claude-work": (5, 5), "claude-opus-work": (5, 5)})
        for project in ("acme", "home"):
            led.upsert_item(project, 1, state="ready", priority=2,
                            state_changed_at=iso(NOW - timedelta(minutes=30)),
                            sorted_at=iso(NOW - timedelta(days=1)))
        ctx = scheduler.Ctx(cfg, led, dry_run=True)
        with mock.patch.object(scheduler.platforms, "available", return_value=True), \
                mock.patch.object(scheduler.presence, "human_claude_active", return_value=False):
            scheduler.schedule(ctx, list(config.enabled_projects(cfg)))
        lines = "\n".join(ctx.lines)
        self.assertIn("acme#1: would build on codex-work", lines)   # copilot takes size:s only
        self.assertIn("home#1: would build on agy-claude", lines)


class LaunchAndGitHubTests(unittest.TestCase):
    def test_launch_refuses_a_platform_from_another_account(self):
        ctx = scheduler.Ctx(work_cfg(), Ledger(":memory:", clock=lambda: NOW), dry_run=True)
        with mock.patch.object(runner, "git") as git:
            with self.assertRaises(RuntimeError):
                runner.launch(ctx, "acme", {"number": 1, "title": "t", "branch": None},
                              "build", "claude", 7, 1)
        git.assert_not_called()

    def test_work_project_github_calls_carry_the_work_env(self):
        ctx = scheduler.Ctx(work_cfg(), Ledger(":memory:", clock=lambda: NOW), dry_run=True)
        self.assertEqual(ctx.gh("acme").env["CODEX_HOME"], os.path.expanduser("~/.codex-work"))
        self.assertIsNone(ctx.gh("home").env)
        done = subprocess.CompletedProcess([], 0, '{"state": "OPEN"}', "")
        with mock.patch("mahler.gh.subprocess.run", return_value=done) as run:
            GH("acme/app", env={"GH_CONFIG_DIR": "/w"}).issue_state(3)
        self.assertEqual(run.call_args.kwargs["env"], {"GH_CONFIG_DIR": "/w"})


if __name__ == "__main__":
    unittest.main()
