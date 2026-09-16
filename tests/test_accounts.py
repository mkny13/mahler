"""Accounts (DESIGN D25): work logins beside this machine's own, never crossed.

A project spends only its own account's platforms, pins included; each
account's Claude login is its own run slot and quota; bursts and usage-rise
human flags use each login's own quota group.
"""

import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler import config, platforms, presence, router, runner, scheduler, tick, usage
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

    def test_account_mode_defaults_to_order(self):
        self.assertEqual(config.account_mode_of(config.project_policy(work_cfg(), "home")),
                         "order")

    def test_account_mode_equal_is_read_through(self):
        cfg = work_cfg(both={"enabled": True, "repo": "b/oth", "path": "/tmp/both",
                             "accounts": ["personal", "work"], "account_mode": "equal"})
        self.assertEqual(config.account_mode_of(config.project_policy(cfg, "both")), "equal")

    def test_bad_account_mode_fails_closed(self):
        with self.assertRaises(ValueError):
            config.account_mode_of({"account_mode": "first"})
        cfg = config._merge(config.DEFAULTS, {"projects": {"x": {
            "enabled": True, "account_mode": "first"}}})
        with self.assertRaises(ValueError):
            config.validate_accounts(cfg)

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
                          "claude-opus-work": (5, 5), "codex-work": (10, 10)})

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

    def test_candidates_for_accounts_round_robin_interleaves(self):
        # personal build: agy-claude, agy-gemini, cline-free, copilot,
        # copilot-high, kilo, claude-opus, claude
        # work build: copilot-work, codex-work, claude-opus-work, claude-work
        merged = router.candidates_for_accounts(self.cfg, "build", ["personal", "work"])
        self.assertEqual(merged, [
            "agy-claude", "copilot-work", "agy-gemini", "codex-work",
            "cline-free", "claude-opus-work", "copilot", "claude-work",
            "copilot-high", "kilo", "claude-opus", "claude"])

    def test_candidates_for_accounts_preserves_order_when_one_account_runs_out(self):
        merged = router.candidates_for_accounts(self.cfg, "sort", ["personal", "work"])
        self.assertEqual(merged, ["claude", "claude-work", "agy-gemini", "agy-claude"])

    def test_pick_with_accounts_merges_instead_of_falling_back_by_order(self):
        # agy-claude and agy-gemini (personal's first two picks) are busy;
        # copilot-work is size-blocked at the default size:m, so the next
        # merged candidate is codex-work — equal-mode reaches it without
        # ever exhausting the rest of personal's own list first
        name, _ = router.pick(self.cfg, self.led, "build",
                              busy=["agy-claude", "agy-gemini"],
                              accounts=["personal", "work"])
        self.assertEqual(name, "codex-work")

    def test_pick_with_accounts_pin_reason_names_every_declared_account(self):
        cfg = work_cfg()
        cfg["accounts"]["other"] = {"env": {"CLAUDE_CONFIG_DIR": "~/.claude-other"}}
        cfg["platforms"]["claude-other"] = {"from": "claude", "account": "other"}
        cfg = config.resolve_platforms(cfg)
        name, reasons = router.pick(cfg, self.led, "build", pin="claude-other",
                                    accounts=["personal", "work"])
        self.assertIsNone(name)
        self.assertIn("claude-other: pinned, but it spends the other account, "
                      "not personal, work", reasons)

    def test_personal_burst_never_lifts_work_claude(self):
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
        with mock.patch.object(platforms, "available", return_value=True):
            busy = tick.busy_platforms(self.cfg, self.led.active_runs())
        self.assertIn("claude-opus", busy)
        self.assertNotIn("claude-work", busy)
        self.assertNotIn("claude-opus-work", busy)

    def test_work_readings_land_only_on_work_platforms(self):
        seed(self.led, claude=(30, 30), **{"claude-work": (30, 30)})
        usage.record_claude_usage(self.ctx, [("5h", 50.0, LATER), ("weekly", 60.0, LATER)],
                                       check_human=True, platform="claude-work")
        self.assertEqual(self.led.usage("claude-work")["5h"]["used_pct"], 50.0)
        self.assertEqual(self.led.usage("claude-opus-work")["weekly"]["used_pct"], 60.0)
        self.assertEqual(self.led.usage("claude")["5h"]["used_pct"], 30.0)
        self.assertIsNone(self.led.get_kv("human:claude"))

    def test_personal_readings_never_land_on_work_platforms(self):
        usage.record_claude_usage(self.ctx, [("5h", 50.0, LATER)])
        self.assertEqual(self.led.usage("claude")["5h"]["used_pct"], 50.0)
        self.assertEqual(self.led.usage("claude-work"), {})

    def test_refresh_probes_the_work_login_with_its_own_env(self):
        self.led.upsert_item("acme", 1, state="ready", priority=2)
        projects = [config.project_policy(self.cfg, "acme")]
        with mock.patch.object(usage, "refresh_codex"), \
                mock.patch.object(platforms, "oauth_usage",
                               return_value=[("5h", 11.0, LATER), ("weekly", 12.0, LATER)]) as oauth, \
                mock.patch.object(platforms, "probe_claude",
                                  return_value=[("5h", 21.0, LATER), ("weekly", 22.0, LATER)]) as probe, \
                mock.patch.object(platforms, "probe_copilot", return_value=[]) as copilot, \
                mock.patch.object(platforms, "probe_agy", return_value={}) as agy, \
                mock.patch.object(router, "peak_state", return_value=(False, None)):
            usage.refresh_usage(self.ctx, projects)
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
        with mock.patch.object(usage, "refresh_codex"), \
                mock.patch.object(platforms, "oauth_usage",
                               return_value=[("5h", 11.0, LATER), ("weekly", 12.0, LATER)]) as oauth, \
                mock.patch.object(platforms, "probe_claude") as probe, \
                mock.patch.object(platforms, "probe_copilot", return_value=[]), \
                mock.patch.object(router, "peak_state", return_value=(False, None)):
            usage.refresh_usage(self.ctx, projects)
        oauth.assert_called_with(keychain_service="Claude Code-credentials-abc")
        probe.assert_not_called()
        self.assertEqual(self.led.usage("claude-opus-work")["weekly"]["used_pct"], 12.0)


class ScheduleTests(unittest.TestCase):
    def test_work_and_personal_items_each_get_their_own_accounts_platform(self):
        cfg = work_cfg()
        led = Ledger(":memory:", clock=lambda: NOW)
        seed(led, **{"agy-claude": (10, 10), "agy-gemini": (10, 10), "claude": (5, 5),
                     "claude-work": (5, 5), "claude-opus-work": (5, 5), "codex-work": (10, 10)})
        for project in ("acme", "home"):
            led.upsert_item(project, 1, state="ready", priority=2,
                            state_changed_at=iso(NOW - timedelta(minutes=30)),
                            sorted_at=iso(NOW - timedelta(days=1)))
        ctx = scheduler.Ctx(cfg, led, dry_run=True)
        with mock.patch.object(platforms, "available", return_value=True), \
                mock.patch.object(presence, "human_claude_active", return_value=False):
            tick.schedule(ctx, list(config.enabled_projects(cfg)))
        lines = "\n".join(ctx.lines)
        self.assertIn("acme#1: would build on codex-work", lines)   # copilot takes size:s only
        self.assertIn("home#1: would build on agy-claude", lines)


class MultiAccountTests(unittest.TestCase):
    """A project may name accounts = [...] instead of a single account (D26):
    it tries its accounts in declared order and spends the first with
    headroom; pins and the runner check generalize to membership; GitHub
    identity stays singular (gh_account, default the first account)."""

    def setUp(self):
        self.cfg = work_cfg(
            acme={"enabled": True, "repo": "acme/app", "path": "/tmp/acme",
                  "account": "work", "hot_hold": False},
            home={"enabled": True, "repo": "me/home", "path": "/tmp/home",
                  "hot_hold": False},
            both={"enabled": True, "repo": "b/oth", "path": "/tmp/both",
                  "accounts": ["personal", "work"], "hot_hold": False},
        )
        self.cfg["accounts"]["other"] = {"env": {"CLAUDE_CONFIG_DIR": "~/.claude-other"}}
        self.cfg["platforms"]["claude-other"] = {"from": "claude", "account": "other"}
        self.cfg = config.resolve_platforms(self.cfg)
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)
        seed(self.led, **{"codex-work": (10, 10)})

    def item(self, project, number, state="ready", age_minutes=30, **kw):
        fields = dict(priority=2,
                      state_changed_at=iso(NOW - timedelta(minutes=age_minutes)))
        if state == "ready":
            fields["sorted_at"] = iso(NOW - timedelta(days=1))
        fields.update(kw)
        self.led.upsert_item(project, number, state=state, **fields)

    def plan(self, total=3):
        self.cfg["concurrency"]["total"] = total
        ctx = scheduler.Ctx(self.cfg, self.led, dry_run=True)
        with mock.patch.object(platforms, "available", return_value=True), \
                mock.patch.object(presence, "human_claude_active", return_value=False):
            tick.schedule(ctx, list(config.enabled_projects(self.cfg)))
        return ctx.lines

    def test_single_account_projects_read_as_one_account(self):
        self.assertEqual(config.accounts_of(config.project_policy(self.cfg, "home")),
                         ["personal"])
        self.assertEqual(config.accounts_of(config.project_policy(self.cfg, "acme")),
                         ["work"])
        self.assertEqual(config.accounts_of(config.project_policy(self.cfg, "both")),
                         ["personal", "work"])

    def test_project_setting_both_account_and_accounts_is_a_config_error(self):
        cfg = config._merge(config.DEFAULTS, {"projects": {"x": {
            "enabled": True, "account": "work", "accounts": ["personal"]}}})
        with self.assertRaises(ValueError) as cm:
            config.validate_accounts(cfg)
        self.assertIn("both 'account' and 'accounts'", str(cm.exception))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.toml")
            with open(path, "w") as fh:
                fh.write('[projects.x]\nenabled = true\naccount = "work"\n'
                         'accounts = ["personal"]\n')
            with self.assertRaises(ValueError):
                config.load(path)

    def test_gh_account_defaults_to_the_first_account_and_explicit_wins(self):
        self.assertEqual(config.gh_account_of(config.project_policy(self.cfg, "home")),
                         "personal")
        self.assertEqual(config.gh_account_of(config.project_policy(self.cfg, "acme")),
                         "work")
        self.assertEqual(config.gh_account_of(config.project_policy(self.cfg, "both")),
                         "personal")
        ctx = scheduler.Ctx(self.cfg, self.led, dry_run=True)
        self.assertIsNone(ctx.gh("both").env)          # personal: inherit as-is
        self.cfg["projects"]["both"]["gh_account"] = "work"
        ctx = scheduler.Ctx(self.cfg, self.led, dry_run=True)
        self.assertEqual(ctx.gh("both").env["CODEX_HOME"],
                         os.path.expanduser("~/.codex-work"))


    def test_multi_account_builds_on_its_first_account_with_headroom(self):
        seed(self.led, **{"agy-claude": (10, 10), "agy-gemini": (10, 10),
                          "claude-work": (5, 5), "claude-opus-work": (5, 5), "codex-work": (10, 10)})
        self.item("both", 1)
        self.assertIn("both#1: would build on agy-claude", self.plan())

    def test_multi_account_falls_through_to_its_next_account(self):
        # every personal build platform is busy; work has copilot-work free
        for platform in ("agy-claude", "agy-gemini", "cline-free", "copilot",
                         "kilo", "claude"):     # claude also busy-blocks claude-opus
            self.led.create_run(project="zz", number=1, role="build",
                                platform=platform, epoch=1)
        self.item("both", 1)
        self.assertIn("both#1: would build on codex-work", self.plan(total=10))

    def test_order_account_mode_still_exhausts_personal_first_by_default(self):
        # agy-claude and agy-gemini (personal's first two picks) are busy;
        # every other personal candidate is size-blocked at the default
        # size:m except claude itself, which has headroom. Default "order"
        # mode reaches all the way down to it rather than ever trying work,
        # even though work's codex-work sits idle with full quota headroom.
        for platform in ("agy-claude", "agy-gemini"):
            self.led.create_run(project="zz", number=1, role="build",
                                platform=platform, epoch=1)
        seed(self.led, **{"claude": (30, 30)})
        self.item("both", 1)
        self.assertIn("both#1: would build on claude", self.plan(total=10))

    def test_equal_account_mode_interleaves_instead_of_exhausting_one_account(self):
        # same setup as above, but account_mode = "equal": work's codex-work
        # (round-robin's second work turn) gets picked well before personal's
        # own fallback-of-last-resort (claude) is ever reached
        self.cfg["projects"]["both"]["account_mode"] = "equal"
        for platform in ("agy-claude", "agy-gemini"):
            self.led.create_run(project="zz", number=1, role="build",
                                platform=platform, epoch=1)
        seed(self.led, **{"claude": (30, 30)})
        self.item("both", 1)
        self.assertIn("both#1: would build on codex-work", self.plan(total=10))

    def test_pick_for_project_honors_account_mode_for_fix_runs_too(self):
        # ship.py routes CI-red fix runs through the same helper (D26): both
        # modes should behave for "fix" exactly as they do for "build"
        pol = config.project_policy(self.cfg, "both")
        for platform in ("agy-claude", "agy-gemini"):
            self.led.create_run(project="zz", number=1, role="build",
                                platform=platform, epoch=1)
        seed(self.led, **{"claude": (30, 30)})
        name, _ = router.pick_for_project(self.cfg, self.led, pol, "fix")
        self.assertEqual(name, "claude")
        pol = {**pol, "account_mode": "equal"}
        name, _ = router.pick_for_project(self.cfg, self.led, pol, "fix")
        self.assertEqual(name, "codex-work")

    def test_equal_account_mode_leaves_single_account_projects_unaffected(self):
        self.cfg["projects"]["acme"]["account_mode"] = "equal"
        seed(self.led, **{"codex-work": (10, 10)})
        self.item("acme", 1)
        self.assertIn("acme#1: would build on codex-work", self.plan())

    def test_multi_account_pin_on_either_declared_account_works(self):
        seed(self.led, **{"agy-claude": (10, 10), "claude-work": (5, 5)})
        self.item("both", 1, pin="claude-work")
        self.assertIn("both#1: would build on claude-work", self.plan())
        self.led.upsert_item("both", 1, state="ready", priority=2, pin="agy-claude",
                             state_changed_at=iso(NOW - timedelta(minutes=30)),
                             sorted_at=iso(NOW - timedelta(days=1)))
        self.assertIn("both#1: would build on agy-claude", self.plan())

    def test_multi_account_pin_on_an_undeclared_account_is_refused(self):
        self.item("both", 1, pin="claude-other")
        line = next(l for l in self.plan() if l.startswith("both#1: no platform"))
        self.assertIn("claude-other: pinned, but it spends the other account, "
                      "not personal", line)
        self.assertIn("claude-other: pinned, but it spends the other account, "
                      "not work", line)

    def test_multi_account_sort_is_not_deferred_while_any_account_has_a_builder(self):
        # personal has plenty of sort headroom, work has none: the sort still
        # goes first — it only waits when *every* account lacks a builder
        seed(self.led, **{"claude": (10, 10), "agy-claude": (10, 10),
                          "agy-gemini": (10, 10), "claude-work": (95, 95)})
        self.item("both", 1, state="inbox", age_minutes=60)
        self.item("both", 2, age_minutes=10)
        self.assertEqual([l for l in self.plan(total=1) if ": would " in l],
                         ["both#1: would sort on claude"])

    def test_multi_account_sort_is_deferred_when_every_account_lacks_a_builder(self):
        seed(self.led, **{"claude": (95, 95), "agy-claude": (95, 95),
                          "agy-gemini": (95, 95), "claude-work": (95, 95)})
        self.item("both", 1, state="inbox", age_minutes=60)
        self.item("both", 2, age_minutes=10)
        self.assertEqual([l for l in self.plan(total=1) if ": would " in l],
                         ["both#2: would build on codex-work"])

    def test_launch_accepts_a_platform_on_any_declared_account(self):
        ctx = scheduler.Ctx(self.cfg, self.led, dry_run=True)
        runner.check_account(ctx, "both", "agy-claude")    # personal
        runner.check_account(ctx, "both", "codex-work")    # work

    def test_launch_still_refuses_a_platform_on_an_undeclared_account(self):
        ctx = scheduler.Ctx(self.cfg, self.led, dry_run=True)
        with self.assertRaises(RuntimeError) as cm:
            runner.check_account(ctx, "both", "claude-other")
        self.assertIn("claude-other spends the other account; "
                      "both is on personal, work", str(cm.exception))


class LaunchAndGitHubTests(unittest.TestCase):
    def test_launch_refuses_a_platform_from_another_account(self):
        ctx = scheduler.Ctx(work_cfg(), Ledger(":memory:", clock=lambda: NOW), dry_run=True)
        with mock.patch.object(runner, "git") as git:
            with self.assertRaises(RuntimeError):
                runner.prepare(ctx, "acme", {"number": 1, "title": "t", "branch": None},
                               "build", "claude", 7)
        git.assert_not_called()

    def test_work_project_github_calls_carry_the_work_env(self):
        ctx = scheduler.Ctx(work_cfg(), Ledger(":memory:", clock=lambda: NOW), dry_run=True)
        self.assertEqual(ctx.gh("acme").env["CODEX_HOME"], os.path.expanduser("~/.codex-work"))
        self.assertIsNone(ctx.gh("home").env)
        done = subprocess.CompletedProcess([], 0, '{"state": "OPEN"}', "")
        with mock.patch("mahler.gh.subprocess.run", return_value=done) as run:
            GH("acme/app", env={"GH_CONFIG_DIR": "/w"}).issue_state(3)
        self.assertEqual(run.call_args.kwargs["env"], {"GH_CONFIG_DIR": "/w"})

    @mock.patch.dict(os.environ, {"GH_TOKEN": "system-token", "GH_CONFIG_DIR": "/sys/gh"})
    def test_run_environment_github_identity_overlay(self):
        cfg = work_cfg(both={"enabled": True, "repo": "b/oth", "path": "/tmp/both",
                             "accounts": ["personal", "work"]},
                       acme={"enabled": True, "repo": "acme/app", "path": "/tmp/acme",
                             "account": "work"},
                       custom={"enabled": True, "repo": "c/c", "path": "/tmp/c",
                               "accounts": ["work", "personal"], "gh_account": "other"})
        cfg["accounts"]["work"]["env"]["GH_CONFIG_DIR"] = "~/.gh-work"
        cfg["accounts"]["work"]["env"]["GH_TOKEN"] = "work-token"
        cfg["accounts"]["other"] = {"env": {"GH_CONFIG_DIR": "~/.gh-other"}}
        cfg["platforms"]["claude-other"] = {"from": "claude", "account": "other"}
        cfg = config.resolve_platforms(cfg)
        
        ctx = scheduler.Ctx(cfg, Ledger(":memory:", clock=lambda: NOW), dry_run=True)
        item = {"number": 1, "title": "t", "branch": None}
        prep = {"worktree": "/tmp", "run_dir": "/tmp", "branch": "b", "base_ref": "main", "replayed": False, "kept": None}

        # 1. Project whose gh_account resolves to 'personal', run on a platform with 'account' = 'work':
        # The built env has no GH_CONFIG_DIR from the work account, and every non-GitHub work variable is present.
        with mock.patch("mahler.runner.spawn", return_value=123) as spawn, \
             mock.patch("mahler.runner.fence_hooks", return_value="/hooks"), \
             mock.patch("mahler.platforms.argv_for", return_value=["/usr/bin/true"]):
            runner.launch(ctx, "both", item, "build", "claude-work", 7, 1, "prompt", prep)
            
        env = spawn.call_args.kwargs["env"]
        self.assertNotIn("GH_CONFIG_DIR", env)
        self.assertNotIn("GH_TOKEN", env)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], os.path.expanduser("~/.claude-work"))
        
        # 2. The same project on a personal platform: env unchanged from today
        with mock.patch("mahler.runner.spawn", return_value=123) as spawn, \
             mock.patch("mahler.runner.fence_hooks", return_value="/hooks"), \
             mock.patch("mahler.platforms.argv_for", return_value=["/usr/bin/true"]):
            runner.launch(ctx, "both", item, "build", "claude", 7, 1, "prompt", prep)
            
        env = spawn.call_args.kwargs["env"]
        self.assertEqual(env["GH_CONFIG_DIR"], "/sys/gh")
        self.assertEqual(env["GH_TOKEN"], "system-token")
        
        # 3. A work-account project on a work platform keeps the work GH_CONFIG_DIR
        with mock.patch("mahler.runner.spawn", return_value=123) as spawn, \
             mock.patch("mahler.runner.fence_hooks", return_value="/hooks"), \
             mock.patch("mahler.platforms.argv_for", return_value=["/usr/bin/true"]):
            runner.launch(ctx, "acme", item, "build", "claude-work", 7, 1, "prompt", prep)
            
        env = spawn.call_args.kwargs["env"]
        self.assertEqual(env["GH_CONFIG_DIR"], os.path.expanduser("~/.gh-work"))
        self.assertEqual(env["GH_TOKEN"], "work-token")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], os.path.expanduser("~/.claude-work"))
        
        # 4. An explicit gh_account that is neither the platform's account nor the first declared account is honoured.
        with mock.patch("mahler.runner.spawn", return_value=123) as spawn, \
             mock.patch("mahler.runner.fence_hooks", return_value="/hooks"), \
             mock.patch("mahler.platforms.argv_for", return_value=["/usr/bin/true"]):
            runner.launch(ctx, "custom", item, "build", "claude-work", 7, 1, "prompt", prep)
            
        env = spawn.call_args.kwargs["env"]
        self.assertEqual(env["GH_CONFIG_DIR"], os.path.expanduser("~/.gh-other"))
        self.assertNotIn("GH_TOKEN", env)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], os.path.expanduser("~/.claude-work"))


if __name__ == "__main__":
    unittest.main()
