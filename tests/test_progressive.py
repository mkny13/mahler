"""Weekly pacing and account-scoped expiring reserves (mahler#283)."""
import copy
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from mahler import config, presence, router, usage
from mahler.ledger import Ledger, iso
from test_accounts import NOW, work_cfg


class ProgressiveTests(unittest.TestCase):
    def setUp(self):
        self.led = Ledger(':memory:', clock=lambda: NOW)
        self.addCleanup(self.led.close)
        self.pc = copy.deepcopy(config.DEFAULTS['platforms']['claude'])
        self.pc.update(progressive=['weekly'], soft={'5h': 60, 'weekly': 40},
                       hard={'5h': 70, 'weekly': 49})

    def sample(self, remaining, pct=0):
        self.led.record_usage('claude', 'weekly', pct, iso(NOW + remaining))
        self.led.record_usage('claude', '5h', 0, iso(NOW + timedelta(hours=4)))

    def test_all_days_and_exact_boundaries(self):
        for day in range(1, 8):
            for elapsed in (timedelta(days=day-1, microseconds=1), timedelta(days=day)):
                with self.subTest(day=day, elapsed=elapsed):
                    self.sample(timedelta(days=7) - elapsed)
                    soft, hard = router.effective_lines(self.led, 'claude', self.pc, 'weekly')
                    self.assertAlmostEqual(soft, 40 * day / 7)
                    self.assertAlmostEqual(hard, 49 * day / 7)

    def test_start_and_yield_thresholds(self):
        for pct, expected in ((5, 'ok'), (40/7, 'soft'), (7, 'hard')):
            self.sample(timedelta(days=7), pct)
            self.assertEqual(router.usage_state(self.led, 'claude', self.pc)[0], expected)

    def test_unknown_reset_conservatively_uses_first_day(self):
        for reset in (None, 'garbage'):
            self.led.record_usage('claude', 'weekly', 6, reset)
            self.assertAlmostEqual(router.effective_lines(self.led, 'claude', self.pc, 'weekly')[0], 40/7)

    def test_expired_or_stale_sample_still_blocks(self):
        self.sample(timedelta(0))
        self.assertEqual(router.usage_state(self.led, 'claude', self.pc)[0], 'stale')
        self.sample(timedelta(days=7))
        self.led.record_usage('claude', 'weekly', 0, iso(NOW + timedelta(days=7)),
                              sampled_at=iso(NOW - timedelta(minutes=16)))
        self.assertEqual(router.usage_state(self.led, 'claude', self.pc)[0], 'stale')

    def test_opt_in_and_session_burst_preserves_pacing(self):
        self.sample(timedelta(days=8))
        self.assertEqual(router.effective_lines(self.led, 'claude', self.pc, '5h'), (60, 70))
        self.assertAlmostEqual(router.effective_lines(self.led, 'claude', self.pc, 'weekly',
                                                    {'5h': (90, 97)})[0], 40/7)
        self.assertEqual(router.effective_lines(self.led, 'claude', self.pc, 'weekly',
                                               {'weekly': (90, 97), '5h': (90, 97)}), (90, 97))
        self.pc['progressive'] = []
        self.assertEqual(router.effective_lines(self.led, 'claude', self.pc, 'weekly'), (40, 49))


class WorkBurstTests(unittest.TestCase):
    def setUp(self):
        self.cfg = work_cfg()
        self.cfg['claude_peak']['enabled'] = False
        self.led = Ledger(':memory:', clock=lambda: NOW)
        self.addCleanup(self.led.close)
        self.ctx = SimpleNamespace(cfg=self.cfg, led=self.led, burst_lines=None,
                                   hot_hold=True, say=lambda line: None)

    def seed(self, name, weekly_hours, session_minutes=240, pct=85):
        self.led.record_usage(name, '5h', pct, iso(NOW + timedelta(minutes=session_minutes)))
        self.led.record_usage(name, 'weekly', pct, iso(NOW + timedelta(hours=weekly_hours)))

    def test_work_reset_does_not_lift_personal_and_prioritizes_work(self):
        self.seed('claude', 48)
        for name in ('claude-work', 'claude-opus-work'):
            self.seed(name, 2)
        lines = router.all_bursts(self.cfg, self.led)
        for name, expected in (('claude', 'hard'), ('claude-work', 'ok'), ('claude-opus-work', 'ok')):
            self.assertEqual(router.usage_state(self.led, name, self.cfg['platforms'][name], lines)[0], expected)
        self.assertEqual(router.pick(self.cfg, self.led, 'build', account='work', size='m',
                                     burst_lines=lines)[0], 'claude-work')
        self.seed('claude-work', 2, pct=98)
        self.assertEqual(router.usage_state(self.led, 'claude-work', self.cfg['platforms']['claude-work'], lines)[0], 'hard')

    def test_personal_reset_cannot_trigger_work_burst(self):
        self.seed('claude', 2)
        self.seed('claude-work', 48)
        lines = router.all_bursts(self.cfg, self.led)
        self.assertIsNone(router.platform_burst('claude-work', self.cfg['platforms']['claude-work'], lines))

    def test_work_session_only_and_stale_reset(self):
        self.seed('claude-work', 48, 30)
        self.assertEqual(router.all_bursts(self.cfg, self.led), {'claude@work': {'5h': (90, 97)}})
        self.led.record_usage('claude-work', 'weekly', 0, None)
        self.assertIsNone(router.all_bursts(self.cfg, self.led))

    def test_transcript_suppresses_work_burst(self):
        self.seed('claude-work', 2)
        with mock.patch.object(presence, 'human_claude_active', return_value=True):
            self.assertIsNone(usage.compute_burst(self.ctx, []))

    def test_usage_rise_suppresses_only_its_group(self):
        self.seed('claude-work', 2)
        self.seed('claude', 2)
        usage.record_claude_usage(self.ctx, [('5h', 86, iso(NOW + timedelta(minutes=30)))],
                                  check_human=True, platform='claude-work')
        with mock.patch.object(presence, 'human_claude_active', return_value=False):
            lines = usage.compute_burst(self.ctx, [])
        self.assertIn('claude', lines)
        self.assertNotIn('claude@work', lines)
