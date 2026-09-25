"""Named route groups stay raw in settings and expand at dispatch time."""
import copy
import io
from pathlib import Path
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from mahler import cli, config, platforms, router
from mahler.console import actions, page, state
from mahler.ledger import Ledger


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
            'variants': ['gpt-6-luna@low', 'gpt-6-luna@medium', 'gpt-5.6-luna'],
            'variant_tiers': {'gpt-6-luna@medium': 4},
            **platform_over,
        }}
        return cfg

    def test_expansion_and_naming(self):
        cfg = config.resolve_platforms(self.cfg())
        plats = cfg['platforms']
        self.assertEqual(set(plats), {
            'codex', 'codex/gpt-6-luna/low', 'codex/gpt-6-luna/medium',
            'codex/gpt-5.6-luna/default',
        })
        low = plats['codex/gpt-6-luna/low']
        self.assertEqual(low['model'], 'gpt-6-luna')
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
                         ['gpt-6-luna@low', 'gpt-6-luna@medium', 'gpt-5.6-luna'])

    def test_variant_tiers_overrides_the_slots_tier(self):
        cfg = config.resolve_platforms(self.cfg())
        self.assertEqual(cfg['platforms']['codex/gpt-6-luna/medium']['tier'], 4)
        self.assertEqual(cfg['platforms']['codex/gpt-6-luna/low']['tier'], 2)

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
        self.assertEqual(cfg['platforms']['codex/gpt-6-luna/low']['quota_group'], 'codex')

    def test_no_variants_declared_is_unchanged(self):
        before = set(config.DEFAULTS['platforms'])
        after = set(config.resolve_platforms(copy.deepcopy(config.DEFAULTS))['platforms'])
        # Issue #422 (D33): built-in slots declare incumbent-first candidates;
        # every declared entry is expanded into one synthetic platform.
        self.assertEqual(after - before, {
            "claude/claude-sonnet-5/default", "claude/claude-sonnet-5/low",
            "claude/claude-sonnet-5/high", "claude/claude-opus-5-5/low",
            "claude-opus/claude-opus-5-5/default",
            "claude-opus/claude-opus-5-5/medium",
            "claude-opus/claude-opus-5-5/high",
            "claude-opus/claude-opus-5/default",
            "agy-gemini/gemini-3.1-pro-high/default",
            "agy-gemini/gemini-3.8-flash/low",
            "agy-gemini/gemini-3.8-flash/medium",
            "codex-low/gpt-5.6-luna/default", "codex-low/gpt-6-luna/low",
            "codex-low/gpt-6-luna/medium", "codex-low/gpt-6-luna/high",
            "codex/gpt-5.6-terra/default", "codex/gpt-6-luna/high",
            "codex/gpt-6-sol/low",
        })
        self.assertEqual(before - after, set())

    def test_variant_tier_drives_escalation_routing(self):
        """D8 rule 4: escalation skips a candidate below min_tier. A variant's
        tier (from variant_tiers, or inherited from the slot) is what that
        check reads, so variant_tiers alone is enough to change which variant
        an escalated item lands on."""
        cfg = self.cfg()
        cfg['routing'] = {'build': ['codex/gpt-6-luna/low', 'codex/gpt-6-luna/medium']}
        cfg = config.resolve_platforms(cfg)
        with closing(Ledger(':memory:')) as led:
            platform, reasons = router.pick(cfg, led, 'build', min_tier=3)
        self.assertEqual(platform, 'codex/gpt-6-luna/medium')   # tier 4 (variant_tiers)
        self.assertIn('codex/gpt-6-luna/low: tier 2 below escalation tier 3', reasons)

    def test_routing_and_pin_can_target_a_variant(self):
        cfg = self.cfg()
        cfg['routing'] = {'build': ['codex/gpt-6-luna/low']}
        cfg = config.resolve_platforms(cfg)
        self.assertEqual(router.candidates(cfg, 'build'), ['codex/gpt-6-luna/low'])
        with closing(Ledger(':memory:')) as led:
            platform, reasons = router.pick(cfg, led, 'build', pin='codex/gpt-6-luna/low')
        self.assertEqual(platform, 'codex/gpt-6-luna/low')


if __name__ == '__main__':
    unittest.main()


class PinnedModelVersionsTests(unittest.TestCase):
    """Issue #421 (D33): the built-in Claude slots pin exact model IDs instead
    of the `haiku`/`sonnet`/`opus` aliases, which silently follow the newest
    release. `claude-opus` additionally keeps `claude-opus-5` as a variant
    candidate, so the previous version isn't dropped when a newer Opus ships."""

    def test_claude_low_pins_haiku_4_5(self):
        low = config.DEFAULTS["platforms"]["claude-low"]
        self.assertEqual(low["sort_model"], "claude-haiku-4-5-20251001")
        self.assertEqual(low["build_model"], "claude-haiku-4-5-20251001")

    def test_claude_pins_sonnet_5(self):
        med = config.DEFAULTS["platforms"]["claude"]
        self.assertEqual(med["sort_model"], "claude-sonnet-5")
        self.assertEqual(med["build_model"], "claude-sonnet-5")

    def test_claude_opus_pins_opus_5_5_and_keeps_opus_5_as_variant(self):
        opus = config.DEFAULTS["platforms"]["claude-opus"]
        self.assertEqual(opus["sort_model"], "claude-opus-5-5")
        self.assertEqual(opus["build_model"], "claude-opus-5-5")
        self.assertEqual(opus["variants"], [
            "claude-opus-5-5", "claude-opus-5-5@medium",
            "claude-opus-5-5@high", "claude-opus-5"])
        resolved = config.resolve_platforms(copy.deepcopy(config.DEFAULTS))
        self.assertIn("claude-opus/claude-opus-5/default", resolved["platforms"])
        variant = resolved["platforms"]["claude-opus/claude-opus-5/default"]
        self.assertEqual(variant["sort_model"], "claude-opus-5")
        self.assertEqual(variant["build_model"], "claude-opus-5")
        self.assertEqual(variant["slot"], "claude-opus")
        self.assertEqual(variant["quota_group"], "claude")
        self.assertEqual(variant["tier"], 4)          # inherits the slot's tier

    def test_pinned_models_have_price_rows(self):
        prices = config.DEFAULTS["prices"]
        for name in ("claude-haiku-4-5-20251001", "claude-sonnet-5", "claude-opus-5-5"):
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
            "claude-sonnet-5", "claude-sonnet-5@low",
            "claude-sonnet-5@high", "claude-opus-5-5@low"])
        self.assertEqual(platforms["claude-opus"]["variants"], [
            "claude-opus-5-5", "claude-opus-5-5@medium",
            "claude-opus-5-5@high", "claude-opus-5"])
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
