"""Launch failures must stop retrying even though the tick itself survives."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from mahler import config, launch_health, runner, scheduler, tick
from mahler.console.state import _idle
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
        patch('mahler.config.STATE', self.tmp.name).start()
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

    def test_launch_failure_cleans_prepared_worktree_and_run_branch(self):
        run_id = self.led.q1('SELECT MAX(id) AS id FROM runs')['id'] or 0
        prepared = {
            'worktree': os.path.join(self.tmp.name, 'worktree'),
            'branch': f'mahler/1-title-r{run_id + 1}',
            'replayed': False, 'kept': None,
        }
        with patch('mahler.tick.runner.prepare', return_value=prepared), \
             patch('mahler.tick.prompt.build', side_effect=RuntimeError('prompt failed')), \
             patch('mahler.tick.runner.remove_worktree') as remove:
            self.assertFalse(self.start())
        actual_run_id = self.led.q1('SELECT MAX(id) AS id FROM runs')['id']
        remove.assert_called_once_with(self.cfg['projects']['a']['path'],
                                       prepared['worktree'],
                                       f'mahler/1-title-r{actual_run_id}',
                                       runner.worktree_root(self.cfg['projects']['a']))

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

    def idle_reasons(self):
        return _idle(self.cfg, self.led,
                     {"paused": False, "peak": None, "quota": []}, [], self.now)["reasons"]

    def test_console_global_breaker_without_pending_candidates(self):
        # Shipping attempts review/fix launches after schedule_holds is saved.
        for project in ('a', 'b'):
            for row in self.led.items(project):
                self.led.set_state(project, row['number'], 'verifying')
        scheduler.record_holds(self.ctx)
        with patch('mahler.tick.prompt.build', side_effect=AttributeError('broken')):
            self.start()
            self.start('a', 2)
        for age in (0, 5, 31):
            with self.subTest(minutes=age):
                self.now = NOW + timedelta(minutes=age)
                reasons = self.idle_reasons()
                self.assertEqual(len(reasons), 1)
                self.assertIn('All projects: launches paused', reasons[0]['text'])
                self.assertIn('AttributeError: broken', reasons[0]['text'])
                self.assertEqual(reasons[0]['countdown'],
                                 f"next attempt in {max(0, 30 - age)}m")
        launch_health.succeeded(self.ctx, 'a', 100)
        self.assertIn('waiting on CI', self.idle_reasons()[0]['text'])

    def test_console_project_breaker_empty_backlog_and_recovery(self):
        for project in ('a', 'b'):
            for row in self.led.items(project):
                self.led.set_state(project, row['number'], 'done')
        for run in range(1, 4):
            launch_health.failed(self.ctx, 'a', 1, run, RuntimeError('git failed'))
        reasons = self.idle_reasons()
        self.assertEqual(len(reasons), 1)
        self.assertIn('a: launches paused — RuntimeError: git failed', reasons[0]['text'])
        self.assertIn('30m', reasons[0]['countdown'])
        launch_health.succeeded(self.ctx, 'a', 4)
        self.assertIn('backlog is empty', self.idle_reasons()[0]['text'])

    def test_console_breaker_not_duplicated_or_retained_by_snapshot(self):
        with patch('mahler.tick.prompt.build', side_effect=AttributeError('broken')):
            self.schedule()
        scheduler.record_holds(self.ctx)
        reasons = self.idle_reasons()
        self.assertEqual(len(reasons), 1)
        self.assertIn('All projects: launches paused', reasons[0]['text'])
        launch_health.succeeded(self.ctx, 'a', 100)
        self.assertFalse(any('launches paused' in r['text'] for r in self.idle_reasons()))


class LaunchRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        state = patch.object(config, 'STATE', self.tmp.name)
        state.start()
        self.addCleanup(state.stop)
        self.led = Ledger(':memory:')
        self.addCleanup(self.led.close)
        self.ctx = scheduler.Ctx(mk_cfg({}), self.led)
        self.ctx.say = Mock()
        self.ctx.ping = Mock()
        self.sha = 'a' * 40
        head = patch.object(launch_health.version, '_git', return_value=(True, self.sha))
        self.git = head.start()
        self.addCleanup(head.stop)

    def test_success_records_full_app_head_atomically_only_when_changed(self):
        marker = self.home / 'launch_ok'
        launch_health.succeeded(self.ctx, 'a', 1)
        self.assertEqual(marker.read_text(), self.sha + '\n')
        self.git.assert_called_with(['rev-parse', 'HEAD'], config.REPO_ROOT)
        with patch.object(launch_health.os, 'replace', wraps=launch_health.os.replace) as replace:
            launch_health.succeeded(self.ctx, 'a', 2)
            replace.assert_not_called()
            self.git.return_value = (True, 'b' * 40)
            launch_health.succeeded(self.ctx, 'a', 3)
            replace.assert_called_once()
        self.assertEqual(marker.read_text(), 'b' * 40 + '\n')
        self.assertEqual(list(self.home.iterdir()), [marker])

    def test_failed_write_preserves_marker_and_successful_launch_recovery(self):
        marker = self.home / 'launch_ok'
        marker.write_text('b' * 40 + '\n')
        self.led.set_kv('launch_broken', json.dumps({'signature': 'broken'}))
        with patch.object(launch_health.os, 'replace', side_effect=OSError('disk failure')):
            launch_health.succeeded(self.ctx, 'a', 1)
        self.assertEqual(marker.read_text(), 'b' * 40 + '\n')
        self.assertEqual(list(self.home.iterdir()), [marker])
        self.assertEqual(json.loads(self.led.get_kv('launch_successes'))['global'], 1)
        self.assertIsNone(json.loads(self.led.get_kv('launch_broken')))
        self.ctx.say.assert_called_once()

    def test_git_failure_does_not_write_or_raise(self):
        self.git.return_value = (False, '')
        launch_health.succeeded(self.ctx, 'a', 1)
        self.assertFalse((self.home / 'launch_ok').exists())
        self.ctx.say.assert_called_once()

    def test_exit_code_requires_global_breaker_and_different_valid_launch_head(self):
        marker = self.home / 'launch_ok'
        for scope in (None, 'launch_broken:a', 'launch_broken'):
            for good in (None, '', 'invalid', self.sha, 'b' * 40):
                with self.subTest(scope=scope, good=good):
                    for key in ('launch_broken', 'launch_broken:a'):
                        self.led.set_kv(key, json.dumps({'signature': 'broken'} if scope == key else None))
                    marker.unlink(missing_ok=True)
                    if good is not None:
                        marker.write_text(good + '\n')
                    expected = 3 if scope == 'launch_broken' and good == 'b' * 40 else 0
                    self.assertEqual(launch_health.tick_exit_code(self.led), expected)
        self.git.return_value = (False, '')
        self.assertEqual(launch_health.tick_exit_code(self.led), 0)

    def test_cli_propagates_breaker_created_during_tick_before_ledger_closes(self):
        from argparse import Namespace
        from mahler import cli
        (self.home / 'launch_ok').write_text('b' * 40 + '\n')

        def trip(ctx):
            ctx.led.set_kv('launch_broken', json.dumps({'signature': 'broken'}))

        with patch.object(scheduler, 'take_lock', return_value=object()), \
                patch.object(scheduler, 'tick', side_effect=trip):
            self.assertEqual(cli.cmd_tick(Namespace(dry_run=False, no_hot_hold=False),
                                          mk_cfg({}), self.led), 3)

    def test_state_default_honors_mahler_home_in_fresh_process(self):
        result = subprocess.run(
            [sys.executable, '-c',
             'from mahler import config; print(config.STATE)'],
            cwd=config.REPO_ROOT, env={**os.environ, 'MAHLER_HOME': self.tmp.name},
            capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), self.tmp.name)
