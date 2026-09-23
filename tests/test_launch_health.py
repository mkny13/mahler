"""Launch failures must stop retrying even though the tick itself survives."""

import json
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from mahler import config, launch_health, scheduler, tick
from mahler.console.state import _hold_reasons
from mahler.ledger import Ledger
from test_schedule import NOW, item, mk_cfg, proj


class LaunchHealthTests(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'ledger.db')
        self.led = Ledger(self.path, clock=lambda: self.now)
        self.addCleanup(lambda: self.led.close())
        self.cfg = mk_cfg({'a': proj(max_parallel=10), 'b': proj(max_parallel=10)}, total=10)
        self.ctx = scheduler.Ctx(self.cfg, self.led)
        self.ctx.ping = Mock()
        self.ctx.gh = Mock()
        self.prep = patch('mahler.tick.runner.prepare', return_value={
            'worktree': self.tmp.name, 'branch': 'test', 'replayed': False, 'kept': None}).start()
        self.launch = patch('mahler.tick.runner.launch', return_value={'branch': 'test'}).start()
        self.addCleanup(patch.stopall)
        patch('mahler.launch_health.version._short_head', return_value='abc1234').start()
        patch('mahler.tick.router.pick_for_project', return_value=('claude', [])).start()
        for project, number in [('a', 1), ('a', 2), ('a', 3), ('b', 1)]:
            item(self.led, project, number)

    def start(self, project='a', number=1):
        return tick.start(self.ctx, project, self.led.item(project, number), 'build', 'claude')

    def schedule(self):
        tick.schedule(self.ctx, list(config.enabled_projects(self.cfg)))

    def state(self, key='launch_broken'):
        return json.loads(self.led.get_kv(key) or 'null')

    def test_real_row_runs_through_real_prompt(self):
        memory = Ledger(':memory:', clock=lambda: self.now)
        self.addCleanup(memory.close)
        self.ctx.led = memory
        item(memory, 'a', 1)
        row = memory.items('a', ['ready'])[0]
        self.assertIsInstance(row, sqlite3.Row)
        self.assertTrue(tick.start(self.ctx, 'a', row, 'build', 'claude'))
        self.assertIn('x/y', self.launch.call_args.args[-2])
        self.assertFalse(memory.q1("SELECT 1 FROM events WHERE kind='launch_failed'"))
        self.assertFalse(memory.q1("SELECT 1 FROM runs WHERE outcome LIKE 'launch failed%'"))

    def test_global_trip_blocks_same_tick_persists_and_recovers(self):
        with patch('mahler.tick.prompt.build', side_effect=AttributeError("'Row' has no get")) as build:
            self.schedule()
            self.assertEqual(build.call_count, 2)
            self.assertEqual(self.ctx.ping.call_count, 1)
            self.assertEqual(self.ctx.ping.call_args.kwargs['priority'], 'high')
            self.assertEqual(self.state()['sha'], 'abc1234')
            self.assertEqual(len(self.state('launch_failures')), 2)
            self.led.close()
            self.led = Ledger(self.path, clock=lambda: self.now)
            self.ctx.led = self.led
            self.now += timedelta(minutes=29, seconds=59)
            self.schedule()
            self.assertEqual(build.call_count, 2)
            self.assertTrue(any(h['kind'] == 'launch_broken' for h in self.ctx.holds))
            self.now += timedelta(seconds=1)
            self.schedule()
            self.assertEqual(build.call_count, 3)  # exactly one failing canary
            self.schedule()
            self.assertEqual(build.call_count, 3)
            self.assertEqual(self.ctx.ping.call_count, 1)
        self.now += timedelta(minutes=30)
        self.assertTrue(self.start())
        self.assertIsNone(self.state())
        self.assertEqual(self.ctx.ping.call_count, 2)
        self.assertIn('recovered', self.ctx.ping.call_args.args[0])
        self.assertTrue(self.start('b'))

    def test_project_failure_does_not_block_or_recover_from_other_project(self):
        error = subprocess.CalledProcessError(128, ['git', 'fetch', '/tmp/repo'])
        for attempt in range(3):
            with patch('mahler.tick.runner.prepare', side_effect=error):
                self.assertFalse(self.start())
            # Healthy activity elsewhere breaks the global failure streak.
            item(self.led, 'b', attempt + 10)
            self.assertTrue(self.start('b', attempt + 10))
        self.assertIsNone(self.state())
        self.assertIsNotNone(self.state('launch_broken:a'))
        self.assertEqual(self.ctx.ping.call_args.kwargs['priority'], 'default')
        attempts = self.prep.call_count
        self.assertFalse(self.start())
        self.assertEqual(self.prep.call_count, attempts)
        self.assertTrue(self.start('b'))
        self.assertIsNotNone(self.state('launch_broken:a'))
        self.now += timedelta(minutes=30)
        self.assertTrue(self.start())
        self.assertIsNone(self.state('launch_broken:a'))
        self.assertEqual(self.ctx.ping.call_count, 2)

    def test_success_breaks_global_failure_streak(self):
        with patch('mahler.tick.prompt.build', side_effect=AttributeError('broken')):
            self.assertFalse(self.start())
        self.assertTrue(self.start('b'))
        with patch('mahler.tick.prompt.build', side_effect=AttributeError('broken')):
            self.assertFalse(self.start('a', 2))
        self.assertIsNone(self.state())

    def test_direct_fix_start_is_held_without_claim_or_run(self):
        with patch('mahler.tick.prompt.build', side_effect=AttributeError('broken')):
            self.start()
            self.start('a', 2)
        before = self.led.q1('SELECT COUNT(*) AS n FROM runs')['n']
        self.assertFalse(tick.start(self.ctx, 'a', self.led.item('a', 3), 'fix', 'claude'))
        self.assertEqual(self.led.q1('SELECT COUNT(*) AS n FROM runs')['n'], before)
        self.assertIsNone(self.led.lease('a', 3))

    def test_history_bounded_and_signatures_normalized(self):
        for n in range(60):
            launch_health.failed(self.ctx, 'a', 1, n + 1,
                                 RuntimeError(f'failure {n} at /tmp/run{n}/file deadbeef\nmore'))
        self.assertEqual(len(self.state('launch_failures')), 50)
        self.assertEqual(len({f['signature'] for f in self.state('launch_failures')}), 1)
        self.assertEqual(self.ctx.ping.call_count, 1)
        self.assertEqual(launch_health.signature(RuntimeError('at src/alpha.py 123 abcdefab')),
                         launch_health.signature(RuntimeError('at lib/beta.py 456 deadbeef')))
        self.assertNotEqual(launch_health.signature(ValueError('oops')),
                            launch_health.signature(TypeError('oops')))

    def test_console_explains_hold(self):
        with patch('mahler.tick.prompt.build', side_effect=AttributeError('broken')):
            self.start()
            self.start('a', 2)
        reasons = _hold_reasons(self.cfg, self.ctx.holds,
                               {'a': self.led.items('a')}, [], self.now)
        self.assertIn('All projects: launches paused', reasons[0]['text'])
        self.assertIn('30m', reasons[0]['countdown'])
