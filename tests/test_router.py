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
