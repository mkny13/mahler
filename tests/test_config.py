"""Named route groups stay raw in settings and expand at dispatch time."""
import copy
import io
from pathlib import Path
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from mahler import cli, config, router
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

    def test_quota_group_defaults_to_the_slots_name_when_unset(self):
        cfg = self.cfg()
        del cfg['platforms']['codex']['quota_group']
        cfg = config.resolve_platforms(cfg)
        self.assertEqual(cfg['platforms']['codex/gpt-6-luna/low']['quota_group'], 'codex')

    def test_no_variants_declared_is_unchanged(self):
        before = set(config.DEFAULTS['platforms'])
        after = set(config.resolve_platforms(copy.deepcopy(config.DEFAULTS))['platforms'])
        self.assertEqual(before, after)

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
