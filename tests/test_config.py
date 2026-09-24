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


if __name__ == '__main__':
    unittest.main()
