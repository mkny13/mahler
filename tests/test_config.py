"""Named route groups stay raw in settings and expand at dispatch time."""
import copy
import io
from pathlib import Path
import tempfile
import tomllib
import unittest
from contextlib import closing, redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from mahler import cli, config, platforms, router
from mahler.console import actions, page, state
from mahler.ledger import Ledger


# Claude rows captured from `agy models` on 2026-10-11 (exit 0, no inference).
# A dated catalog snapshot; recurring availability detection belongs to #972.
AGY_CLAUDE_MODELS = """\
claude-opus-5-5-low	Claude Opus 5.5 (Low)
claude-opus-5-5-medium	Claude Opus 5.5 (Medium)
claude-opus-5-5-high	Claude Opus 5.5 (High)
claude-sonnet-5-5-low	Claude Sonnet 5.5 (Low)
claude-sonnet-5-5-medium	Claude Sonnet 5.5 (Medium)
claude-sonnet-5-5-high	Claude Sonnet 5.5 (High)
"""


class AgyClaudeDefaultTests(unittest.TestCase):
    def test_default_in_captured_catalog_and_price_basis(self):
        model = config.DEFAULTS["platforms"]["agy-claude"]["model"]
        self.assertEqual(model, "claude-opus-5-5-medium")
        self.assertIn(model, {row.split("\t")[0]
                              for row in AGY_CLAUDE_MODELS.splitlines()})
        basis = config.DEFAULTS["prices"]["claude-opus-5-5"]
        self.assertEqual(config.DEFAULTS["prices"][model], basis)
        example = Path(__file__).resolve().parents[1] / "config.example.toml"
        self.assertEqual(tomllib.loads(example.read_text())["prices"][model], basis)

    def test_operator_model_override_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('[platforms.agy-claude]\nmodel = "claude-sonnet-5-5-high"\n')
            cfg = config.load(path)
        self.assertEqual(cfg["platforms"]["agy-claude"]["model"],
                         "claude-sonnet-5-5-high")


class SmokePolicyTests(unittest.TestCase):
    def load(self, user):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(config.dumps_toml(user))
            return config.load(path)

    def test_dependency_pr_policy(self):
        pol = config.project_policy(self.load({}), "app")
        self.assertTrue(pol["dependency_prs"])
        self.assertEqual(pol["dependency_prs_daily_cap"], 3)
        cfg = self.load({"projects": {"app": {"dependency_prs": False,
                                             "dependency_prs_daily_cap": 7}}})
        pol = config.project_policy(cfg, "app")
        self.assertFalse(pol["dependency_prs"])
        self.assertEqual(pol["dependency_prs_daily_cap"], 7)
        for key, values in (("dependency_prs", (0, "false", [])),
                            ("dependency_prs_daily_cap", (0, -1, True, 1.5, "3"))):
            for value in values:
                for section in ({"defaults": {key: value}},
                                {"projects": {"app": {key: value}}}):
                    with self.subTest(section=section), self.assertRaises(ValueError):
                        self.load(section)

    def test_gui_gate_defaults_overrides_and_validation(self):
        pol = config.project_policy(self.load({}), "app")
        self.assertEqual(pol["gui_idle_minutes"], 15)
        self.assertEqual(pol["gui_window"], "00:00-06:00, Mon-Fri 11:30-13:30")
        cfg = self.load({"defaults": {"gui_idle_minutes": 20, "gui_window": ""},
                         "projects": {"app": {"gui_idle_minutes": 2.5,
                                              "gui_window": "22:00-02:00"}}})
        self.assertEqual(config.project_policy(cfg, "app")["gui_idle_minutes"], 2.5)
        self.assertEqual(cfg["defaults"]["gui_idle_minutes"], 20)
        self.assertIsNone(config.parse_gui_window(cfg["defaults"]["gui_window"]))
        for key, values in (("gui_idle_minutes", (0, -1, True, "15", float("inf"), float("nan"))),
                            ("gui_window", ("0-6", "24:00-01:00", "00:60-01:00", "06:00-06:00",
                                            "00:00-6:00", 5,
                                            "Foo 11:30-13:30", "Fri-Mon 11:30-13:30",
                                            "Mon-Mon 11:30-13:30", "00:00-06:00, ",
                                            "00:00-06:00,,11:30-13:30", ","))):
            for value in values:
                for section in ({"defaults": {key: value}},
                                {"projects": {"app": {key: value}}}):
                    with self.subTest(section=section, value=value), self.assertRaises(ValueError):
                        self.load(section)

    def test_parse_gui_window_multiple_ranges_and_weekdays(self):
        from datetime import time
        self.assertIsNone(config.parse_gui_window(""))
        # Single range without weekday
        self.assertEqual(config.parse_gui_window("00:00-06:00"),
                         ((None, time(0, 0), time(6, 0)),))
        # Weekday range
        self.assertEqual(config.parse_gui_window("Mon-Fri 11:30-13:30"),
                         ((frozenset({0, 1, 2, 3, 4}), time(11, 30), time(13, 30)),))
        # Weekend range
        self.assertEqual(config.parse_gui_window("Sat-Sun 00:00-06:00"),
                         ((frozenset({5, 6}), time(0, 0), time(6, 0)),))
        # Single day
        self.assertEqual(config.parse_gui_window("Tue 12:00-14:00"),
                         ((frozenset({1}), time(12, 0), time(14, 0)),))
        # Slash-separated days
        self.assertEqual(config.parse_gui_window("Mon/Wed/Fri 12:00-13:00"),
                         ((frozenset({0, 2, 4}), time(12, 0), time(13, 0)),))
        # Multiple comma-separated ranges with whitespace
        res = config.parse_gui_window(" 00:00-06:00 , Mon-Fri 11:30-13:30 ")
        self.assertEqual(res,
                         ((None, time(0, 0), time(6, 0)),
                          (frozenset({0, 1, 2, 3, 4}), time(11, 30), time(13, 30))))
        # Formatter produces expected representation
        self.assertEqual(config.format_gui_window(res), "00:00-06:00, Mon-Fri 11:30-13:30")

    def test_parse_gui_window_bad_input_error_messages(self):
        cases = [
            ("Foo 11:30-13:30", "unknown day"),
            ("Mon-Bar 11:30-13:30", "unknown day"),
            ("Fri-Mon 11:30-13:30", "reversed day range"),
            ("Sun-Sat 11:30-13:30", "reversed day range"),
            ("Mon-Mon 11:30-13:30", "equal endpoints"),
            ("06:00-06:00", "equal endpoints"),
            ("Mon 06:00-06:00", "equal endpoints"),
            ("24:00-01:00", "invalid time"),
            ("00:60-01:00", "invalid time"),
            ("0-6", "must look like HH:MM-HH:MM"),
            ("00:00-6:00", "must look like HH:MM-HH:MM"),
            ("00:00-06:00, ", "empty item"),
            (",00:00-06:00", "empty item"),
            ("00:00-06:00,,11:30-13:30", "empty item"),
            (",", "empty item"),
            ("  ", "empty item"),
        ]
        for val, expected_phrase in cases:
            with self.subTest(val=val), self.assertRaises(ValueError) as cm:
                config.parse_gui_window(val)
            self.assertIn(expected_phrase, str(cm.exception))

    def test_post_merge_defaults_and_nested_overlays(self):
        cfg = self.load({})
        self.assertEqual(config.project_policy(cfg, "app")["post_merge"],
                         config.DEFAULT_POST_MERGE)
        cfg = self.load({"defaults": {"smoke": "./smoke", "post_merge": {
            "enabled": True, "live_command": "./live"}}, "projects": {
                "app": {"post_merge": {"deploy_strategy": "command",
                                       "deploy_command": "./deploy", "auto_revert": True}},
                "off": {"post_merge": {"enabled": False}}}})
        pol = config.project_policy(cfg, "app")
        self.assertEqual(pol["smoke"], "./smoke")
        self.assertEqual(pol["post_merge"]["live_command"], "./live")
        self.assertEqual(pol["post_merge"]["environment"], "staging")
        self.assertTrue(pol["post_merge"]["auto_revert"])
        self.assertFalse(config.project_policy(cfg, "off")["post_merge"]["enabled"])
        self.assertFalse(config.DEFAULT_POST_MERGE["enabled"])
        for timeout in (1, 86400):
            self.load({"projects": {"app": {"post_merge": {"timeout_seconds": timeout}}}})

    def test_post_merge_builtin_strategies(self):
        for strategy in ("mahler", "github"):
            cfg = self.load({"projects": {"app": {"post_merge": {
                "enabled": True, "live_strategy": strategy}}}})
            self.assertEqual(config.project_policy(cfg, "app")["post_merge"]["live_strategy"], strategy)
            with self.assertRaisesRegex(ValueError, "live_command"):
                self.load({"projects": {"app": {"post_merge": {
                    "live_strategy": strategy, "live_command": "./live"}}}})
        for environment in ("prod", "Production"):
            cfg = self.load({"projects": {"app": {"post_merge": {
                "live_strategy": "github", "environment": environment}}}})
            self.assertEqual(config.project_policy(cfg, "app")["post_merge"]["environment"], environment)
            with self.assertRaisesRegex(ValueError, "live_command"):
                self.load({"projects": {"app": {"post_merge": {
                    "live_strategy": "github", "environment": environment,
                    "live_command": "./live"}}}})

    def test_post_merge_invalid_contracts_are_project_scoped(self):
        cases = [("enabled", "yes"), ("auto_revert", 1),
                 ("deploy_strategy", "provider"), ("live_strategy", "none"),
                 ("live_command", 1), ("deploy_command", False), ("environment", []),
                 ("smoke", "./duplicate"), ("token", "not-allowed")]
        cases += [("timeout_seconds", v) for v in (0, -1, 86401, True, 1.5, "30")]
        for key, value in cases:
            with self.subTest(key=key, value=value), self.assertRaisesRegex(
                    ValueError, f"project 'app': post_merge.*{key}"):
                self.load({"projects": {"app": {"post_merge": {key: value}}}})
        for pm, field in (({"enabled": True}, "live_command"),
                          ({"enabled": True, "live_command": "./live", "environment": " "},
                           "environment"),
                          ({"enabled": True, "live_command": "./live",
                            "deploy_strategy": "command"}, "deploy_command"),
                          ({"enabled": True, "live_command": "./live",
                            "deploy_command": "./deploy"}, "deploy_command")):
            with self.subTest(pm=pm), self.assertRaisesRegex(
                    ValueError, f"project 'app': post_merge.*{field}"):
                self.load({"projects": {"app": {"post_merge": pm}}})
        with self.assertRaisesRegex(ValueError, "project 'app': post_merge must be a table"):
            self.load({"projects": {"app": {"post_merge": False}}})
        with self.assertRaisesRegex(ValueError, "project 'app': post_merge.live_command"):
            self.load({"defaults": {"post_merge": {"enabled": True, "live_command": "./live"}},
                       "projects": {"app": {"post_merge": {"live_command": ""}}}})

    def test_screenshot_defaults_inheritance_and_disable(self):
        defaults = {"screenshot": "", "screenshot_environment": "", "screenshot_path": [],
                    "screenshot_preview_non_personal": False,
                    "screenshot_timeout_seconds": 45}
        policy = config.project_policy(self.load({}), "app")
        self.assertEqual({k: policy[k] for k in defaults}, defaults)
        enabled = dict(screenshot="./capture.sh", screenshot_environment="staging",
                       screenshot_path=["/opt/capture/bin"],
                       screenshot_preview_non_personal=True, screenshot_timeout_seconds=60)
        cfg = self.load({"defaults": enabled, "projects": {
            "disabled": {"screenshot": ""}, "override": {
                "screenshot": "./other.sh", "screenshot_environment": "preview",
                "screenshot_path": [],
                "screenshot_preview_non_personal": False, "screenshot_timeout_seconds": 1}}})
        self.assertEqual(config.project_policy(cfg, "inherited")["screenshot"], "./capture.sh")
        self.assertEqual(config.project_policy(cfg, "disabled")["screenshot"], "")
        for key, value in enabled.items():
            self.assertEqual(config.project_policy(cfg, "inherited")[key], value)
        self.assertEqual(config.project_policy(cfg, "override")["screenshot_timeout_seconds"], 1)
        self.assertFalse(config.project_policy(cfg, "override")["screenshot_preview_non_personal"])
        self.assertEqual(config.project_policy(cfg, "override")["screenshot_path"], [])

    def test_screenshot_invalid_types(self):
        cases = {"screenshot": [True, 1, [], {}],
                 "screenshot_path": [True, 1, "/opt/bin", {}, [1], [""], ["."],
                                     ["bin"], ["/opt/bin", ""], ["/opt/bin:"],
                                     ["/opt/bin:.:/usr/bin"]],
                 "screenshot_environment": [False, 1, [], {}],
                 "screenshot_preview_non_personal": ["true", 0, 1, [], {}],
                 "screenshot_timeout_seconds": [True, 0, 61, 1.5, "45", [], {}]}
        for key, values in cases.items():
            for value in values:
                for user in ({"defaults": {key: value}}, {"projects": {"app": {key: value}}}):
                    with self.subTest(user=user), self.assertRaisesRegex(ValueError, key):
                        self.load(user)

    def test_review_context_default_override_and_clear(self):
        default = config.project_policy(self.load({}), "app")["review_context"]
        self.assertEqual(default, "Personal project: a single user owns and controls all "
                         "devices and upgrades them together.")
        for inherited in (default, "Shared service with independent upgrades.", ""):
            cfg = self.load({"defaults": {"review_context": inherited}, "projects": {
                "inherited": {}, "override": {"review_context": "  Custom context.\n"},
                "cleared": {"review_context": ""}}})
            for name, expected in (("inherited", inherited), ("other", inherited),
                                   ("override", "  Custom context.\n"), ("cleared", "")):
                with self.subTest(inherited=inherited, project=name):
                    self.assertEqual(config.project_policy(cfg, name)["review_context"], expected)

    def test_review_context_rejects_non_strings(self):
        for value in (False, 1, 1.5, [], {"text": "personal"}):
            for user in ({"defaults": {"review_context": value}},
                         {"projects": {"app": {"review_context": value}}}):
                with self.subTest(user=user), self.assertRaisesRegex(ValueError, "review_context"):
                    self.load(user)

    def test_green_review_round_policy(self):
        self.assertEqual(config.project_policy(self.load({}), "app")["review_green_rounds"], 2)
        cfg = self.load({"defaults": {"review_green_rounds": 3},
                         "projects": {"app": {"review_green_rounds": 1}}})
        self.assertEqual(config.project_policy(cfg, "app")["review_green_rounds"], 1)
        self.assertEqual(config.project_policy(cfg, "other")["review_green_rounds"], 3)
        for value in (0, -1, True, 1.5, "2"):
            for user in ({"defaults": {"review_green_rounds": value}},
                         {"projects": {"app": {"review_green_rounds": value}}}):
                with self.subTest(user=user), self.assertRaisesRegex(ValueError, "review_green_rounds"):
                    self.load(user)

    def test_capacity_wait_default_and_project_override(self):
        cfg = self.load({"projects": {"app": {"capacity_wait_max_hours": 48}}})
        self.assertEqual(config.project_policy(cfg, "app")["capacity_wait_max_hours"], 48)
        self.assertEqual(config.project_policy(cfg, "unconfigured")["capacity_wait_max_hours"], 24)

    def test_smoke_defaults_to_disabled_without_changing_verify(self):
        cfg = self.load({"projects": {"app": {"verify": "python3 -m unittest"}}})
        policy = config.project_policy(cfg, "app")
        self.assertEqual(policy["smoke"], "")
        self.assertEqual(policy["verify"], "python3 -m unittest")
        self.assertEqual(config.project_policy(cfg, "unconfigured")["verify"], "")

    def test_project_can_set_smoke_without_a_global_command(self):
        cfg = self.load({"projects": {"app": {"smoke": "./scripts/smoke.sh"}}})
        policy = config.project_policy(cfg, "app")
        self.assertEqual(policy["smoke"], "./scripts/smoke.sh")
        self.assertEqual(policy["verify"], "")
        self.assertEqual(cfg["defaults"]["smoke"], "")

    def test_merging_inherits_overrides_and_disables_smoke_per_project(self):
        cfg = self.load({
            "defaults": {"smoke": "./default-smoke.sh", "verify": "./verify.sh"},
            "projects": {
                "inherited": {},
                "override": {"smoke": "./project-smoke.sh", "verify": "./check.sh"},
                "disabled": {"smoke": ""},
            },
        })
        for name, smoke, verify in (
            ("inherited", "./default-smoke.sh", "./verify.sh"),
            ("override", "./project-smoke.sh", "./check.sh"),
            ("disabled", "", "./verify.sh"),
        ):
            with self.subTest(project=name):
                policy = config.project_policy(cfg, name)
                self.assertEqual(policy["smoke"], smoke)
                self.assertEqual(policy["verify"], verify)
        self.assertEqual(cfg["defaults"]["smoke"], "./default-smoke.sh")
        self.assertEqual(config.DEFAULTS["defaults"]["smoke"], "")


class PracticesAuditPolicyTests(unittest.TestCase):
    def test_minimal_and_loaded_defaults(self):
        expected = {"enabled": True, "project": "mahler", "cadence_days": 30,
                    "merged_threshold": 20, "cooldown_days": 14}
        self.assertEqual(config.practices_audit_policy({}), expected)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("")
            cfg = config.load(path)
        self.assertEqual(cfg["practices_audit"], expected)
        self.assertEqual(config.practices_audit_policy(cfg), expected)

    def test_partial_overrides_and_disabled_round_trip(self):
        for overrides in ({"cadence_days": 7}, {"enabled": False},
                          {"project": "other", "cadence_days": 60,
                           "merged_threshold": 40, "cooldown_days": 21}):
            with self.subTest(overrides=overrides):
                cfg = {"practices_audit": overrides}
                before = copy.deepcopy(cfg)
                expected = {**config.DEFAULT_PRACTICES_AUDIT, **overrides}
                pol = config.practices_audit_policy(cfg)
                self.assertEqual(pol, expected)
                pol["enabled"] = not pol["enabled"]
                self.assertEqual(cfg, before)
                self.assertEqual(config.practices_audit_policy({})["enabled"], True)
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "config.toml"
                    path.write_text(config.dumps_toml(cfg))
                    loaded = config.load(path)
                self.assertEqual(config.practices_audit_policy(loaded), expected)

    def test_fixed_checklist_is_separate_from_configurable_maintenance(self):
        expected = ("ci-tests", "agent-instructions", "mahler-guidance",
                    "verify-command", "tracked-secrets", "branch-protection")
        self.assertEqual(config.PRACTICES_AUDIT_CHECKS, expected)
        self.assertEqual(config.PRACTICES_AUDIT_PASS, "practices-audit")
        self.assertNotIn(config.PRACTICES_AUDIT_PASS, config.MAINTENANCE_PASSES)
        cfg = {"practices_audit": {"enabled": False},
               "defaults": {"maintenance": {"passes": []}}}
        config.practices_audit_policy(cfg)
        self.assertEqual(config.PRACTICES_AUDIT_CHECKS, expected)
        self.assertNotIn("checks", config.DEFAULT_PRACTICES_AUDIT)


class CustomMaintenanceTests(unittest.TestCase):
    def load(self, maintenance):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(config.dumps_toml({"projects": {
                "app": {"maintenance": maintenance}, "other": {"maintenance": {
                    "custom": {"parity": {"title": "Other parity", "text": "Other text"}}}}
            }}))
            return config.load(path)

    def test_custom_passes_append_in_order_without_duplicates(self):
        cfg = self.load({"passes": ["health", "parity", "health", "security"],
                         "custom": {"parity": {"title": "Parity", "text": "Scan"},
                                    "extra": {"title": "Extra", "text": "More"}}})
        pol = config.maintenance_policy(cfg, "app")
        self.assertEqual(pol["passes"], ["health", "parity", "security", "extra"])
        self.assertEqual(cfg["projects"]["app"]["maintenance"], pol)
        self.assertEqual(config.maintenance_policy(cfg, "other")["passes"],
                         [*config.MAINTENANCE_PASSES, "parity"])
        self.assertEqual(config.DEFAULT_MAINTENANCE["passes"], list(config.MAINTENANCE_PASSES))

    def test_bad_entries_are_logged_and_skipped_individually(self):
        bad = {"security": {"title": "Override", "text": "Override"},
               "missing-title": {"text": "Scan"}, "missing-text": {"title": "Scan"},
               "bad-title": {"title": 42, "text": "Scan"},
               "bad-text": {"title": "Scan", "text": False},
               "empty-title": {"title": "  ", "text": "Scan"},
               "empty-text": {"title": "Scan", "text": ""}, "scalar": "bad"}
        with self.assertLogs("mahler.config", level="WARNING") as logs:
            cfg = self.load({"passes": ["health", *bad], "custom": {
                **bad, "valid": {"title": "Good", "text": "Scan"}}})
        self.assertEqual(len(logs.output), len(bad))
        for key in bad:
            self.assertTrue(any(repr(key) in line for line in logs.output))
        pol = config.maintenance_policy(cfg, "app")
        self.assertEqual(pol["passes"], ["health", "security", "valid"])
        self.assertEqual(list(pol["custom"]), ["valid"])
        self.assertEqual(config.maintenance_policy(cfg, "other")["custom"]["parity"]["title"],
                         "Other parity")

    def test_malformed_custom_table_does_not_break_loading(self):
        with self.assertLogs("mahler.config", level="WARNING"):
            cfg = self.load({"custom": "bad"})
        self.assertEqual(config.maintenance_policy(cfg, "app")["passes"],
                         list(config.MAINTENANCE_PASSES))


class RoutingGroupsTests(unittest.TestCase):
    def cfg(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg['groups'] = {'free': ['agy-claude', 'agy-gemini'],
                         'build': ['@free', 'claude', '@free']}
        cfg['routing']['build'] = ['@build', 'claude']
        return cfg

    def test_nested_duplicates_and_order(self):
        cfg = self.cfg()
        expected = ['agy-claude', 'agy-gemini', 'claude']
        self.assertEqual(config.expand_route(cfg, cfg['routing']['build']), expected)
        self.assertEqual(router.candidates(cfg, 'build'), expected)
        self.assertEqual(router.burst_build_order(cfg), ['claude', 'agy-claude', 'agy-gemini'])
        self.assertEqual(state._routed(cfg, ('build',)), expected)
        self.assertEqual(cfg['routing']['build'], ['@build', 'claude'])

    def test_unknown_and_cycle_warn_once_and_keep_valid_siblings(self):
        cfg = self.cfg()
        cfg['groups']['free'] = ['@build', '@missing', 'agy-claude']
        cfg['routing'] = {'build': ['@build', '@missing'], 'sort': ['@missing']}
        warnings = config.routing_warnings(cfg)
        self.assertEqual(len(warnings), 2)
        self.assertIn('cycle', warnings[0])
        self.assertIn('unknown routing group @missing', warnings[1])
        self.assertEqual(router.candidates(cfg, 'build'), ['agy-claude', 'claude'])
        with closing(Ledger(':memory:')) as led, redirect_stdout(io.StringIO()) as output:
            cli.cmd_status(SimpleNamespace(json=False), cfg, led)
        for warning in warnings:
            self.assertEqual(output.getvalue().count(warning), 1)

    def test_deep_groups_do_not_recurse(self):
        cfg = {'groups': {str(i): [f'@{i+1}'] for i in range(1100)}}
        cfg['groups']['1100'] = ['claude']
        self.assertEqual(config.expand_route(cfg, ['@0']), ['claude'])

    def test_account_and_priority_routes_enforce_membership(self):
        cfg = self.cfg()
        cfg['platforms']['work-builder'] = {'enabled': True, 'account': 'work'}
        cfg['groups']['work'] = ['work-builder']
        cfg['accounts'] = {'work': {'routing': {'build': ['@work', '@free']}}}
        self.assertEqual(router.candidates(cfg, 'build', account='work'), ['work-builder'])
        cfg['projects'] = {'app': {'account': 'work', 'account_mode': 'priority',
                                   'routing': {'build': ['@work']}}}
        config.validate_accounts(cfg)
        self.assertEqual(router.candidates_for_priority(
            cfg, 'build', ['work'], cfg['projects']['app']['routing']), ['work-builder'])
        for entry, message in [('@missing', 'unknown routing group'),
                               ('@free', 'undeclared account'), ('@cycle', 'cycle'),
                               ('@malformed', 'routing entry must be'), (42, 'unknown platform')]:
            cfg['groups']['cycle'] = ['@cycle']
            cfg['groups']['malformed'] = [42]
            cfg['projects']['app']['routing']['build'] = [entry]
            with self.subTest(entry=entry), self.assertRaisesRegex(ValueError, message):
                config.validate_accounts(cfg)

    def test_console_action_round_trip_preserves_group_tokens(self):
        user = {'groups': {'shared': ['claude']},
                'routing': {'build': ['@shared']},
                'accounts': {'personal': {'routing': {'build': ['@shared']}}},
                'projects': {'app': {'routing': {'build': ['@shared']}}}}
        with tempfile.TemporaryDirectory() as tmp, closing(Ledger(':memory:')) as led:
            path = Path(tmp) / 'config.toml'
            path.write_text(config.dumps_toml(user))
            cfg = config.load(path)
            form = config.settings(cfg)
            html = page._settings_form(form, 'desktop')
            self.assertIn('data-route-platform="@shared"', html)
            self.assertIn('<option value="@shared">', html)
            with patch.object(config, 'CONFIG_PATH', path):
                saved = actions.save_settings(cfg, led, form)['settings']
            for route in saved['routing']:
                self.assertEqual(route['build'], ['@shared'])
            loaded = config.load(path)
            self.assertEqual(loaded['groups'], user['groups'])
            self.assertEqual(router.candidates(loaded, 'build'), ['claude'])


class PlatformVariantTests(unittest.TestCase):
    """Issue #420 (D33): a platform slot may declare several model x effort
    variants, expanded at load time into synthetic platform entries that
    share its quota_group and its one run slot."""

    def cfg(self, **platform_over):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg['platforms'] = {'codex': {
            'enabled': True, 'kind': 'codex', 'model': 'gpt-5.6-terra',
            'tier': 2, 'max_size': 'm', 'quota_group': 'codex', 'metered': False,
            'variants': ['gpt-5.6-luna@low', 'gpt-5.6-luna@medium', 'gpt-5.6-luna'],
            'variant_tiers': {'gpt-5.6-luna@medium': 4},
            **platform_over,
        }}
        return cfg

    def test_expansion_and_naming(self):
        cfg = config.resolve_platforms(self.cfg())
        plats = cfg['platforms']
        self.assertEqual(set(plats), {
            'codex', 'codex/gpt-5.6-luna/low', 'codex/gpt-5.6-luna/medium',
            'codex/gpt-5.6-luna/default',
        })
        low = plats['codex/gpt-5.6-luna/low']
        self.assertEqual(low['model'], 'gpt-5.6-luna')
        self.assertEqual(low['effort'], 'low')
        self.assertEqual(low['slot'], 'codex')
        self.assertEqual(low['quota_group'], 'codex')
        self.assertEqual(low['tier'], 2)          # inherits the slot's own tier
        self.assertEqual(low['max_size'], 'm')    # inherits everything else too
        no_effort = plats['codex/gpt-5.6-luna/default']
        self.assertEqual(no_effort['model'], 'gpt-5.6-luna')
        self.assertNotIn('effort', no_effort)     # no @effort in the spec, none inherited
        self.assertNotIn('variants', low)         # a variant does not itself re-expand
        self.assertNotIn('variant_tiers', low)

    def test_default_variant_unchanged(self):
        cfg = config.resolve_platforms(self.cfg())
        self.assertEqual(cfg['platforms']['codex']['model'], 'gpt-5.6-terra')
        self.assertEqual(cfg['platforms']['codex']['variants'],
                         ['gpt-5.6-luna@low', 'gpt-5.6-luna@medium', 'gpt-5.6-luna'])

    def test_variant_tiers_overrides_the_slots_tier(self):
        cfg = config.resolve_platforms(self.cfg())
        self.assertEqual(cfg['platforms']['codex/gpt-5.6-luna/medium']['tier'], 4)
        self.assertEqual(cfg['platforms']['codex/gpt-5.6-luna/low']['tier'], 2)

    def test_claude_kind_expands_sort_and_build_model(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg['platforms'] = {'claude': {
            'enabled': True, 'kind': 'claude', 'sort_model': 'sonnet', 'build_model': '',
            'tier': 3, 'quota_group': 'claude', 'metered': False,
            'variants': ['opus-5-5@high'],
        }}
        cfg = config.resolve_platforms(cfg)
        variant = cfg['platforms']['claude/opus-5-5/high']
        self.assertEqual(variant['sort_model'], 'opus-5-5')
        self.assertEqual(variant['build_model'], 'opus-5-5')
        self.assertEqual(variant['effort'], 'high')
        self.assertNotIn('model', variant)

    def test_explicit_claude_effort_overrides_all_inherited_role_efforts(self):
        cfg = self.cfg()
        slot = {"kind": "claude", "effort": "medium",
                "variants": ["opus-5-5@low", "opus-5-5"]}
        roles = ("sort", "plan", "build", "fix", "review")
        slot.update({f"{role}_effort": "high" for role in roles})
        cfg["platforms"] = {"claude": slot}
        cfg = config.resolve_platforms(cfg)
        for role in roles:
            with self.subTest(role=role):
                explicit = cfg["platforms"]["claude/opus-5-5/low"]
                self.assertEqual(platforms.effort_args(explicit, role), ["--effort", "low"])
                self.assertEqual(platforms.effort_value(cfg["platforms"]["claude"], role), "high")
                default = cfg["platforms"]["claude/opus-5-5/default"]
                self.assertEqual(platforms.effort_value(default, role), "high")

    def test_quota_group_defaults_to_the_slots_name_when_unset(self):
        cfg = self.cfg()
        del cfg['platforms']['codex']['quota_group']
        cfg = config.resolve_platforms(cfg)
        self.assertEqual(cfg['platforms']['codex/gpt-5.6-luna/low']['quota_group'], 'codex')

    def test_variants_inherit_the_slots_cost_class(self):
        cfg = config.resolve_platforms(self.cfg(cost_class='paid'))
        self.assertEqual(cfg['platforms']['codex/gpt-5.6-luna/low']['cost_class'], 'paid')

    def test_no_variants_declared_is_unchanged(self):
        before = set(config.DEFAULTS['platforms'])
        after = set(config.resolve_platforms(copy.deepcopy(config.DEFAULTS))['platforms'])
        # Issue #422 (D33): built-in slots declare incumbent-first candidates;
        # every declared entry is expanded into one synthetic platform.
        self.assertEqual(after - before, {
            "claude/claude-sonnet-5-5/default", "claude/claude-sonnet-5-5/low",
            "claude/claude-sonnet-5-5/high", "claude/claude-opus-5-5/low",
            "claude-opus/claude-opus-5-5/default",
            "claude-opus/claude-opus-5-5/medium",
            "claude-opus/claude-opus-5-5/high",
            "agy-gemini/gemini-3.1-pro-high/default",
            "agy-gemini/gemini-3.8-flash/low",
            "agy-gemini/gemini-3.8-flash/medium",
            "codex-low/gpt-5.6-luna/default", "codex-low/gpt-6-luna/low",
            "codex-low/gpt-6-luna/medium", "codex-low/gpt-6-luna/high",
            "codex/gpt-5.6-terra/default", "codex/gpt-6-luna/high",
            "codex/gpt-6-sol/low",
            "copilot/auto/default", "copilot/claude-sonnet-5.5/default",
        })
        self.assertEqual(before - after, set())

    def test_variant_tier_drives_escalation_routing(self):
        """D8 rule 4: escalation skips a candidate below min_tier. A variant's
        tier (from variant_tiers, or inherited from the slot) is what that
        check reads, so variant_tiers alone is enough to change which variant
        an escalated item lands on."""
        cfg = self.cfg()
        cfg['routing'] = {'build': ['codex/gpt-5.6-luna/low', 'codex/gpt-5.6-luna/medium']}
        cfg = config.resolve_platforms(cfg)
        with closing(Ledger(':memory:')) as led:
            platform, reasons = router.pick(cfg, led, 'build', min_tier=3)
        self.assertEqual(platform, 'codex/gpt-5.6-luna/medium')   # tier 4 (variant_tiers)
        self.assertIn('codex/gpt-5.6-luna/low: tier 2 below escalation tier 3', reasons)

    def test_routing_and_pin_can_target_a_variant(self):
        cfg = self.cfg()
        cfg['routing'] = {'build': ['codex/gpt-5.6-luna/low']}
        cfg = config.resolve_platforms(cfg)
        self.assertEqual(router.candidates(cfg, 'build'), ['codex/gpt-5.6-luna/low'])
        with closing(Ledger(':memory:')) as led:
            platform, reasons = router.pick(cfg, led, 'build', pin='codex/gpt-5.6-luna/low')
        self.assertEqual(platform, 'codex/gpt-5.6-luna/low')


if __name__ == '__main__':
    unittest.main()


class PinnedModelVersionsTests(unittest.TestCase):
    """Issue #421 (D33): the built-in Claude slots pin exact model IDs instead
    of the `haiku`/`sonnet`/`opus` aliases, which silently follow the newest
    release. `claude-opus` keeps the 5.5 effort levels as variant candidates."""

    def test_claude_low_pins_haiku_4_5(self):
        low = config.DEFAULTS["platforms"]["claude-low"]
        self.assertEqual(low["sort_model"], "claude-haiku-4-5-20251001")
        self.assertEqual(low["build_model"], "claude-haiku-4-5-20251001")

    def test_claude_pins_sonnet_5_5(self):
        med = config.DEFAULTS["platforms"]["claude"]
        self.assertEqual(med["sort_model"], "claude-sonnet-5-5")
        self.assertEqual(med["build_model"], "claude-sonnet-5-5")

    def test_claude_opus_pins_opus_5_5(self):
        opus = config.DEFAULTS["platforms"]["claude-opus"]
        self.assertEqual(opus["sort_model"], "claude-opus-5-5")
        self.assertEqual(opus["build_model"], "claude-opus-5-5")
        self.assertEqual(opus["variants"], [
            "claude-opus-5-5", "claude-opus-5-5@medium",
            "claude-opus-5-5@high"])
        resolved = config.resolve_platforms(copy.deepcopy(config.DEFAULTS))
        self.assertNotIn("claude-opus/claude-opus-5/default", resolved["platforms"])
        for variant in ("default", "medium", "high"):
            v = resolved["platforms"][f"claude-opus/claude-opus-5-5/{variant}"]
            self.assertEqual(v["sort_model"], "claude-opus-5-5")
            self.assertEqual(v["build_model"], "claude-opus-5-5")
            self.assertEqual(v["slot"], "claude-opus")
            self.assertEqual(v["quota_group"], "claude")
            self.assertEqual(v["tier"], 4)          # inherits the slot's tier

    def test_pinned_models_have_price_rows(self):
        prices = config.DEFAULTS["prices"]
        for name in ("claude-haiku-4-5-20251001", "claude-sonnet-5-5", "claude-opus-5-5"):
            self.assertIn(name, prices, f"{name} has no [prices] entry")
            for key in ("in", "out"):
                self.assertIsInstance(prices[name][key], (int, float))

    def test_aliases_are_not_used_for_routing(self):
        for slot in ("claude-low", "claude", "claude-opus"):
            for key in ("sort_model", "build_model"):
                self.assertNotIn(config.DEFAULTS["platforms"][slot][key],
                                 ("haiku", "sonnet", "opus"), slot)


class BuiltInVariantCandidatesTests(unittest.TestCase):
    """Issue #422: incumbents remain first while new candidates are opt-in."""

    def test_personal_slots_declare_incumbent_first_variants(self):
        platforms = config.DEFAULTS["platforms"]
        self.assertEqual(platforms["codex-low"]["variants"], [
            "gpt-5.6-luna", "gpt-6-luna@low", "gpt-6-luna@medium",
            "gpt-6-luna@high"])
        self.assertEqual(platforms["codex"]["variants"], [
            "gpt-5.6-terra", "gpt-6-luna@high", "gpt-6-sol@low"])
        self.assertEqual(platforms["claude"]["variants"], [
            "claude-sonnet-5-5", "claude-sonnet-5-5@low",
            "claude-sonnet-5-5@high", "claude-opus-5-5@low"])
        self.assertEqual(platforms["claude-opus"]["variants"], [
            "claude-opus-5-5", "claude-opus-5-5@medium",
            "claude-opus-5-5@high"])
        self.assertEqual(platforms["agy-gemini"]["variants"], [
            "gemini-3.1-pro-high", "gemini-3.8-flash@low",
            "gemini-3.8-flash@medium"])

    def test_default_route_order_is_unchanged_by_variants(self):
        cfg = config.resolve_platforms(copy.deepcopy(config.DEFAULTS))
        self.assertEqual(router.candidates(cfg, "build"), [
            "agy-claude", "agy-gemini", "cline-free", "copilot",
            "copilot-high", "kilo", "claude-opus", "claude"])
        self.assertEqual(router.candidates(cfg, "sort"),
                         ["claude", "agy-gemini", "agy-claude"])
        self.assertEqual(router.candidates(cfg, "plan"), ["claude-opus"])


class CopilotCandidatesTests(unittest.TestCase):
    def test_documented_overrides_and_inheritance(self):
        example = (Path(__file__).resolve().parents[1] / "config.example.toml").read_text()
        block = example.split('# [platforms.copilot]\n', 1)[1].split('# metered = false', 1)[0]
        documented = tomllib.loads('[platforms.copilot]\n' + '\n'.join(
            line.removeprefix('# ') for line in block.splitlines()
            if line.startswith(('# variants', '# [platforms.', '# from', '# account'))))
        personal = ["auto", "claude-sonnet-5.5"]
        work = ["auto", "gpt-6.1-sol", "claude-sonnet-5.5"]
        self.assertEqual(documented['platforms']['copilot']['variants'], personal)
        self.assertEqual(documented['platforms']['work-copilot']['variants'], work)
        self.assertNotIn('work-copilot', config.DEFAULTS['platforms'])
        for override in (False, True):
            cfg = copy.deepcopy(config.DEFAULTS)
            declaration = copy.deepcopy(documented['platforms']['work-copilot'])
            if not override:
                declaration.pop('variants')
            cfg['platforms']['work-copilot'] = declaration
            cfg = config.resolve_platforms(cfg)
            for slot, models, account, group in (
                    ('copilot', personal, 'personal', 'copilot'),
                    ('work-copilot', work if override else personal, 'work', 'copilot@work')):
                base = cfg['platforms'][slot]
                self.assertEqual(base['variants'], models)
                self.assertEqual(base['model'], 'auto')
                self.assertEqual(base['auto_tier'], 'balance')
                self.assertEqual([name for name in cfg['platforms'] if name.startswith(slot + '/')],
                                 [f'{slot}/{model}/default' for model in models])
                for model in models:
                    name = f'{slot}/{model}/default'
                    variant = cfg['platforms'][name]
                    self.assertEqual(variant['model'], model)
                    self.assertEqual(platforms.effort_value(variant, 'build') or 'default', 'default')
                    self.assertEqual(config.account_of(variant), account)
                    self.assertEqual(variant['quota_group'], group)
                    self.assertEqual(router.platform_slot(cfg, name), slot)
                    for key, value in dict(tier=2, max_size='s', metered=True,
                                           monthly_cap_credits=1500, windows=['monthly'],
                                           soft={'monthly': 80}, hard={'monthly': 95},
                                           stale_minutes=360, backoff_minutes=60).items():
                        self.assertEqual(variant[key], value)

    def test_dotted_scoring_prices_match_example(self):
        path = Path(__file__).resolve().parents[1] / 'config.example.toml'
        prices = tomllib.loads(path.read_text())['prices']
        for model, cached in [('gpt-6.1-sol', .10), ('claude-sonnet-5.5', .20)]:
            expected = {'in': 2., 'cached_in': cached, 'out': 10.}
            self.assertEqual(config.DEFAULTS['prices'][model], expected)
            self.assertEqual(prices[model], expected)
        self.assertIn('claude-sonnet-5-5', prices)
        self.assertIn('claude-sonnet-5-5', config.DEFAULTS['prices'])


class ProjectGitHubAppConfigTests(unittest.TestCase):
    def test_global_personal_installation_resolved(self):
        cfg = {"github_app": {"app_id": 123, "installation_id": 456,
                              "private_key_path": "/secret/key.pem"}}
        app = config.project_github_app(cfg, {})
        self.assertIsNotNone(app)
        self.assertEqual(app.app_id, "123")
        self.assertEqual(app.installation_id, "456")
        self.assertEqual(app._key_path, "/secret/key.pem")

    def test_project_override_installation_resolved(self):
        cfg = {"github_app": {"app_id": 123, "installation_id": 456,
                              "private_key_path": "/secret/key.pem"}}
        app = config.project_github_app(cfg, {"github_app_installation_id": 789})
        self.assertIsNotNone(app)
        self.assertEqual(app.installation_id, "789")

    def test_unavailable_app_returns_none(self):
        self.assertIsNone(config.project_github_app({}, {}))
        cfg = {"github_app": {"app_id": 123, "installation_id": 456,
                              "private_key_path": "/secret/key.pem"}}
        self.assertIsNone(config.project_github_app(cfg, {"account": "work"}))
        self.assertIsNone(config.project_github_app(cfg, {"gh_account": "work"}))

    def test_project_name_resolution(self):
        cfg = {
            "github_app": {"app_id": 123, "installation_id": 456, "private_key_path": "/secret/key.pem"},
            "defaults": {},
            "projects": {
                "proj_personal": {"repo": "org/repo"},
                "proj_override": {"github_app_installation_id": 999, "repo": "org/repo"},
                "proj_work": {"account": "work", "repo": "org/repo"},
            },
        }
        self.assertEqual(config.project_github_app(cfg, "proj_personal").installation_id, "456")
        self.assertEqual(config.project_github_app(cfg, "proj_override").installation_id, "999")
        self.assertIsNone(config.project_github_app(cfg, "proj_work"))

    def test_invalid_app_config_fails_secret_safe(self):
        for invalid in ({"app_id": "not-a-number"}, {"app_id": 123}, {"app_id": 123, "installation_id": 456}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError) as caught:
                config.project_github_app({"github_app": invalid}, {})
            self.assertNotIn("/secret", str(caught.exception))

    def test_named_work_provider_coexists_with_personal_app(self):
        cfg = {
            "github_app": {
                "app_id": 123,
                "installation_id": 456,
                "private_key_path": "/secret/personal.pem",
            },
            "github_apps": {
                "work": {
                    "app_id": 789,
                    "installation_id": 1011,
                    "private_key_path": "/secret/work.pem",
                },
            },
            "defaults": {},
            "projects": {
                "personal_proj": {"repo": "owner/personal-proj"},
                "work_proj": {
                    "account": "work",
                    "repo": "makastel_ncstate/google-chat-nav",
                    "github_provider": "work",
                },
            },
        }
        personal_app = config.project_github_app(cfg, "personal_proj")
        work_app = config.project_github_app(cfg, "work_proj")

        self.assertIsNotNone(personal_app)
        self.assertIsNotNone(work_app)
        self.assertEqual(personal_app.app_id, "123")
        self.assertEqual(personal_app.installation_id, "456")
        self.assertEqual(personal_app._key_path, "/secret/personal.pem")

        self.assertEqual(work_app.app_id, "789")
        self.assertEqual(work_app.installation_id, "1011")
        self.assertEqual(work_app._key_path, "/secret/work.pem")

        # Cache dirs must be isolated
        self.assertNotEqual(str(personal_app._cache_dir), str(work_app._cache_dir))
        self.assertTrue(str(work_app._cache_dir).endswith("work"))

    def test_provider_selection_independent_of_compute_account(self):
        cfg = {
            "github_app": {
                "app_id": 123,
                "installation_id": 456,
                "private_key_path": "/secret/personal.pem",
            },
            "github_apps": {
                "work": {
                    "app_id": 789,
                    "installation_id": 1011,
                    "private_key_path": "/secret/work.pem",
                },
            },
            "defaults": {},
            "projects": {
                # D26 multi-account project
                "multi_proj": {
                    "accounts": ["personal", "work"],
                    "repo": "org/multi",
                    "github_provider": "work",
                },
                # Compute account is personal, but github provider is work
                "personal_compute_work_gh": {
                    "account": "personal",
                    "repo": "org/cross",
                    "github_provider": "work",
                },
                # Work account with no github provider -> None (personal App never leaks)
                "unmigrated_work": {
                    "account": "work",
                    "repo": "makastel_ncstate/sit-stand-walk",
                },
            },
        }
        self.assertEqual(config.project_github_app(cfg, "multi_proj").app_id, "789")
        self.assertEqual(config.project_github_app(cfg, "personal_compute_work_gh").app_id, "789")
        self.assertIsNone(config.project_github_app(cfg, "unmigrated_work"))

    def test_provider_repository_coverage_restriction(self):
        cfg = {
            "github_apps": {
                "work": {
                    "app_id": 789,
                    "installation_id": 1011,
                    "private_key_path": "/secret/work.pem",
                    "repositories": ["google-chat-nav", "sit-stand-walk"],
                },
            },
            "defaults": {},
            "projects": {
                "covered": {
                    "account": "work",
                    "repo": "makastel_ncstate/google-chat-nav",
                    "github_provider": "work",
                },
                "uncovered": {
                    "account": "work",
                    "repo": "makastel_ncstate/google-workspace-connector",
                    "github_provider": "work",
                },
            },
        }
        self.assertIsNotNone(config.project_github_app(cfg, "covered"))
        with self.assertRaises(ValueError) as caught:
            config.project_github_app(cfg, "uncovered")
        self.assertNotIn("/secret", str(caught.exception))
        self.assertNotIn("789", str(caught.exception))
        # Selecting the provider explicitly never silently falls back to ambient credentials
        with self.assertRaises(ValueError):
            config.github_app_settings(cfg, "uncovered")

    def test_require_scoped_identity_validation_fails_closed(self):
        # 1. Selected provider absent
        cfg_absent = {
            "github_app": {"app_id": 123, "installation_id": 456, "private_key_path": "/secret/key.pem"},
            "projects": {
                "google-chat-nav": {
                    "account": "work",
                    "repo": "makastel_ncstate/google-chat-nav",
                    "require_scoped_identity": True,
                },
            },
        }
        with self.assertRaises(ValueError) as caught:
            config.validate_github_app(cfg_absent)
        self.assertIn("google-chat-nav", str(caught.exception))
        self.assertNotIn("/secret", str(caught.exception))

        # 2. Provider repository restriction does not cover project
        cfg_uncovered = {
            "github_apps": {
                "work": {
                    "app_id": 789,
                    "installation_id": 1011,
                    "private_key_path": "/secret/work.pem",
                    "repositories": ["sit-stand-walk"],
                },
            },
            "projects": {
                "google-chat-nav": {
                    "account": "work",
                    "repo": "makastel_ncstate/google-chat-nav",
                    "github_provider": "work",
                    "require_scoped_identity": True,
                },
            },
        }
        with self.assertRaises(ValueError) as caught:
            config.validate_github_app(cfg_uncovered)
        self.assertIn("google-chat-nav", str(caught.exception))
        self.assertNotIn("/secret", str(caught.exception))

    def test_negative_unauthorized_provider_access_refused(self):
        """Negative/abuse test: ambient or cross-account access refused."""
        cfg = {
            "github_app": {"app_id": 123, "installation_id": 456, "private_key_path": "/secret/personal.pem"},
            "defaults": {},
            "accounts": {"work": {"env": {"GH_CONFIG_DIR": "~/.config/gh-work"}}},
            "projects": {
                "work_repo": {
                    "account": "work",
                    "repo": "makastel_ncstate/google-chat-nav",
                },
            },
        }
        # Work project without approved provider must NEVER return personal App
        app = config.project_github_app(cfg, "work_repo")
        self.assertIsNone(app)
        settings = config.github_app_settings(cfg, "work_repo")
        self.assertIsNone(settings)


class SetProjectMaxParallelTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'config.toml'
        self.user = {'concurrency': {'total': 5},
                     'projects': {'app': {'enabled': True, 'repo': 'x/app', 'path': '/tmp/app',
                                          'max_parallel': 2},
                                  'off': {'enabled': False, 'repo': 'x/off', 'path': '/tmp/off'}}}
        self.path.write_text(config.dumps_toml(self.user))

    def test_changes_only_that_project_value(self):
        self.assertEqual(config.set_project_max_parallel('app', 7, self.path), 7)
        got = tomllib.loads(self.path.read_text())
        self.user['projects']['app']['max_parallel'] = 7
        self.assertEqual(got, self.user)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_rejections_leave_file_byte_identical(self):
        before = self.path.read_bytes()
        for project, value in (('app', 0), ('app', 65), ('app', True), ('app', '3'),
                               ('app', 2.0), ('off', 3), ('nope', 3)):
            with self.subTest(project=project, value=value):
                with self.assertRaises(ValueError):
                    config.set_project_max_parallel(project, value, self.path)
                self.assertEqual(self.path.read_bytes(), before)

    def test_boundaries_accepted(self):
        for value in (1, 64):
            self.assertEqual(config.set_project_max_parallel('app', value, self.path), value)
