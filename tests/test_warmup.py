from contextlib import closing
import copy
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, Mock

from mahler import config, platforms, warmup
from mahler.ledger import Ledger, iso
from mahler.scheduler import Ctx


class WarmupTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 28, 5, tzinfo=timezone.utc)
        self.led = Ledger(':memory:', clock=lambda: self.now)
        self.addCleanup(self.led.close)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.target = dict(name='personal', kind='claude', account='personal',
                           platform='claude-low', start='05:00', end='24:00', days='weekdays')
        self.cfg['warmup']['targets'] = [self.target]
        self.ctx = Ctx(self.cfg, self.led)
        self.read = patch('mahler.warmup.read_usage', return_value=[('5h', 0, None)]).start()
        self.addCleanup(patch.stopall)
        self.nudge = patch('mahler.warmup.nudge').start()
        # Start/end assertions below use local clock times, without changing TZ.
        self.now = self.now.replace(tzinfo=None).astimezone()

    def run_pass(self, **kwargs):
        warmup.warmup_pass(self.ctx, **kwargs)
        return self.ctx.lines[-1]

    def test_first_and_chained_window(self):
        self.run_pass()
        self.assertEqual(self.nudge.call_count, 1)
        self.led.record_usage('claude-low', '5h', 1, iso(self.now + timedelta(hours=5)))
        self.now += timedelta(hours=4, minutes=59)
        self.run_pass()
        self.assertEqual(self.nudge.call_count, 1)
        self.now += timedelta(minutes=1)
        self.run_pass()
        self.assertEqual(self.nudge.call_count, 2)

    def test_hours_weekends_and_off(self):
        for day, hour in [(28, 4), (27, 10), (29, 0)]:
            self.now = self.now.replace(day=day, hour=hour)
            self.assertIn('skipped-hours', self.run_pass())
        self.target['days'] = 'off'
        self.assertIn('skipped-disabled', self.run_pass(manual=True))
        self.nudge.assert_not_called()
        self.read.assert_not_called()

    def test_daily_and_manual(self):
        self.now = self.now.replace(day=27)
        self.target['days'] = 'daily'
        self.run_pass()
        self.nudge.assert_called_once()

    def test_active_avoids_even_free_read(self):
        self.led.record_usage('claude-low', '5h', 10, iso(self.now + timedelta(hours=1)))
        self.assertIn('skipped-active', self.run_pass())
        self.read.assert_not_called()
        self.nudge.assert_not_called()

    def test_free_read_detects_human_window(self):
        self.read.return_value = [('5h', 10, iso(self.now + timedelta(hours=5)))]
        self.assertIn('skipped-active', self.run_pass())
        self.nudge.assert_not_called()

    def test_zero_used_future_reset_is_idle(self):
        self.read.return_value = [('5h', 0, iso(self.now + timedelta(hours=5)))]
        self.run_pass()
        self.nudge.assert_called_once()

    def test_unknown_skips_without_spending(self):
        self.read.return_value = []
        self.assertIn('skipped-unknown', self.run_pass())
        self.nudge.assert_not_called()

    def test_pause_exhaustion_and_block(self):
        self.led.set_kv('paused', '1')
        self.assertIn('skipped-paused', self.run_pass())
        self.led.set_kv('paused', '')
        self.led.record_usage('claude-low', 'weekly', 100, iso(self.now + timedelta(days=1)))
        self.assertIn('skipped-exhausted', self.run_pass())
        self.led.clear_usage('claude-low', ['weekly'])
        self.read.return_value = platforms.CodexUsage([('5h', 0, None)], {'blocked': True})
        self.assertIn('skipped-exhausted', self.run_pass())
        self.nudge.assert_not_called()

    def test_four_hour_guard_and_restart(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'test.db')
            with closing(Ledger(path, clock=lambda: self.now)) as led:
                ctx = Ctx(self.cfg, led)
                warmup.warmup_pass(ctx)
            self.now += timedelta(hours=3)
            with closing(Ledger(path, clock=lambda: self.now)) as led:
                ctx = Ctx(self.cfg, led)
                warmup.warmup_pass(ctx)
                self.assertIn('skipped-guard', ctx.lines[-1])
            self.assertEqual(self.nudge.call_count, 1)

    def test_failure_backoff(self):
        self.nudge.side_effect = RuntimeError('private detail')
        self.assertIn('failed', self.run_pass())
        self.now += timedelta(minutes=29)
        self.assertIn('skipped-backoff', self.run_pass())
        self.now += timedelta(minutes=1)
        self.nudge.side_effect = None
        self.assertIn('fired', self.run_pass())
        self.assertEqual(self.nudge.call_count, 2)
        self.assertNotIn('private detail', str(self.led.q('SELECT * FROM events')))

    def test_per_target_exception_isolation(self):
        second = dict(self.target, name='second')
        self.cfg['warmup']['targets'].append(second)
        self.read.side_effect = [RuntimeError('bad'), [('5h', 0, None)], [('5h', 1, None)]]
        self.run_pass()
        self.nudge.assert_called_once_with(self.ctx, second)

    def test_dry_run_does_not_write(self):
        self.ctx.dry_run = True
        self.assertIn('would-fire', self.run_pass())
        self.nudge.assert_not_called()
        self.assertFalse(self.led.usage('claude-low'))
        self.assertFalse(self.led.q('SELECT * FROM events'))
        self.assertIsNone(self.led.get_kv('warmup:personal:last'))

    def test_account_environment(self):
        self.cfg['accounts']['work'] = {'env': {'CODEX_HOME': '/isolated/work'}}
        t = dict(self.target, kind='codex', account='work')
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'secret', 'GH_TOKEN': 'secret',
                                     'CLAUDE_CONFIG_DIR': '/personal', 'CODEX_HOME': '/personal'}):
            env = warmup.environment(self.cfg, t)
            self.assertEqual(env['CODEX_HOME'], '/isolated/work')
            for key in ('OPENAI_API_KEY', 'GH_TOKEN', 'CLAUDE_CONFIG_DIR'):
                self.assertNotIn(key, env)
            personal = warmup.environment(self.cfg, self.target)
            self.assertNotIn('CODEX_HOME', personal)
            self.assertNotIn('CLAUDE_CONFIG_DIR', personal)
        self.cfg['accounts']['work']['env'] = {}
        with self.assertRaises(ValueError):
            warmup.environment(self.cfg, t)


class WarmupConfigTests(unittest.TestCase):
    def test_defaults_and_validation(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'config.toml')
            cfg = config.load(path)
            self.assertEqual(len(cfg['warmup']['targets']), 2)
            with open(path, 'w') as f:
                f.write('[[warmup.targets]]\nname="bad"\nkind="claude"\naccount="typo"\nplatform="claude"\n')
            with self.assertRaisesRegex(ValueError, 'unknown account'):
                config.load(path)

    def test_invalid_hours_and_mismatched_account(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        t = dict(name='test', kind='claude', account='personal', platform='claude',
                 start='25:00', end='24:00')
        cfg['warmup']['targets'] = [t]
        with self.assertRaisesRegex(ValueError, 'start < end'):
            config.configure_warmup(cfg, cfg)
        t.update(start='05:00', account='work')
        cfg['accounts']['work'] = {}
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            config.configure_warmup(cfg, cfg)


if __name__ == '__main__':
    unittest.main()
