"""Explicit review tier floors agree across routing and capacity diagnostics."""
import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler import config, platforms, router
from mahler.ledger import Ledger, iso


class ReviewTierTests(unittest.TestCase):
    def test_review_floor_default_and_explicit_pin(self):
        now = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg['routing']['review'] = ['agy-gemini', 'agy-claude']
        cfg['platforms']['agy-gemini']['tier'] = 1
        cfg['platforms']['agy-claude']['tier'] = 2
        pol = config.project_policy(cfg, 'x')
        led = Ledger(':memory:', clock=lambda: now)
        self.addCleanup(led.close)
        with mock.patch.object(platforms, 'available', return_value=True):
            for name in cfg['routing']['review']:
                for window in ('5h', 'weekly'):
                    led.record_usage(name, window, 10, iso(now + timedelta(days=3)))
            for tier, pin, expected in ((0, None, 'agy-gemini'),
                                        (2, None, 'agy-claude'),
                                        (2, 'agy-gemini', 'agy-gemini')):
                with self.subTest(tier=tier, pin=pin):
                    kwargs = dict(min_tier=tier, pin=pin)
                    chosen, _ = router.pick_for_project(cfg, led, pol, 'review', **kwargs)
                    self.assertEqual(chosen, expected)
                    diag = router.diagnose(cfg, led, pol, 'review', **kwargs)
                    names, _, _ = router.capacity_recovery(cfg, led, pol, 'review', **kwargs)
                    self.assertIn(expected, names)
                    self.assertEqual(diag[expected], ['eligible'])
                    if tier and not pin:
                        self.assertNotIn('agy-gemini', names)
                        self.assertIn('tier', diag['agy-gemini'])


class StaleCandidatesTests(unittest.TestCase):
    def test_stale_candidates_skip_fresh_and_excluded_slots(self):
        now = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg['routing']['review'] = ['agy-gemini', 'agy-claude', 'claude']
        pol = config.project_policy(cfg, 'x')
        led = Ledger(':memory:', clock=lambda: now)
        self.addCleanup(led.close)
        for window in ('5h', 'weekly'):
            led.record_usage('agy-gemini', window, 10, iso(now + timedelta(days=1)))
        with mock.patch.object(platforms, 'available', return_value=True):
            got = router.stale_candidates(cfg, led, pol, 'review', exclude={'claude'})
        self.assertEqual(got, ['agy-claude'])    # fresh skipped, builder slot never widened in


class GhWritesTests(unittest.TestCase):
    def _setup(self, route):
        now = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg['routing']['sort'] = route
        cfg['routing']['build'] = route
        pol = config.project_policy(cfg, 'x')
        led = Ledger(':memory:', clock=lambda: now)
        self.addCleanup(led.close)
        for name, windows in (('copilot', ('monthly',)), ('agy-gemini', ('5h', 'weekly'))):
            for window in windows:
                led.record_usage(name, window, 10, iso(now + timedelta(days=3)))
        return cfg, led, pol

    def test_sort_skips_copilot_but_build_keeps_it(self):
        cfg, led, pol = self._setup(['copilot', 'agy-gemini'])
        with mock.patch.object(platforms, 'available', return_value=True):
            for role in ('sort', 'plan'):
                cfg['routing'][role] = ['copilot', 'agy-gemini']
                chosen, reasons = router.pick_for_project(cfg, led, pol, role)
                self.assertEqual(chosen, 'agy-gemini', role)
                self.assertTrue(any('no GitHub write' in r for r in reasons))
            chosen, _ = router.pick_for_project(cfg, led, pol, 'build')
            self.assertEqual(chosen, 'copilot')

    def test_only_copilot_holds_with_reason(self):
        cfg, led, pol = self._setup(['copilot'])
        with mock.patch.object(platforms, 'available', return_value=True):
            chosen, reasons = router.pick_for_project(cfg, led, pol, 'sort')
            self.assertIsNone(chosen)
            self.assertTrue(any('copilot' in r and 'no GitHub write' in r for r in reasons))
            self.assertIn('no_gh_writes', router.diagnose(cfg, led, pol, 'sort')['copilot'])

    def test_declared_gh_writes_allows_copilot(self):
        cfg, led, pol = self._setup(['copilot'])
        cfg['platforms']['copilot']['gh_writes'] = True
        with mock.patch.object(platforms, 'available', return_value=True):
            chosen, _ = router.pick_for_project(cfg, led, pol, 'sort')
            self.assertEqual(chosen, 'copilot')
