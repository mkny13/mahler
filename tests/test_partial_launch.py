"""Recovery of a run row left behind by a crash during launch (#1239)."""
import copy
import io
import json
import signal
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from mahler import cli, config, platforms, runner, scheduler, tick, watchdog
from mahler.ledger import Ledger, iso


class PartialLaunchTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 25, tzinfo=timezone.utc)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg['projects']['x'] = {'path': self.tmp.name, 'repo': 'x/y', 'max_parallel': 1}
        self.led = Ledger(':memory:', clock=lambda: self.now)
        self.addCleanup(self.led.close)
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
        self.pol = self.ctx.policy('x')
        self.led.upsert_item('x', 1, state='working', sorted_at=iso(self.now))
        self.rid = self.led.create_run(project='x', number=1, role='build',
                                      platform='cline-free', epoch=1, status='running')
        self.led.claim('x', 1, f'run:{self.rid}', 'auto', 60, run_id=self.rid)

    def test_abandoned_launch_frees_capacity_without_attempt(self):
        for role, state in [('build', 'ready'), ('review', 'verifying')]:
            with self.subTest(role=role):
                if role == 'review':
                    lease, _ = self.led.claim('x', 1, f'run:{self.rid}', 'auto', 60, run_id=self.rid)
                    self.led.update_run(self.rid, epoch=lease['epoch'])
                    self.led.set_kv('review:x#1', json.dumps({'run_id': self.rid, 'sha': 'abc', 'pr': 12}))
                self.led.update_run(self.rid, role=role, status='running', stop_reason=None,
                                    started_at=iso(self.now - timedelta(minutes=11)))
                self.led.upsert_item('x', 1, state='working', pr=12 if role == 'review' else None)
                with mock.patch.object(self.ctx, 'gh') as gh, \
                        mock.patch.object(self.ctx, 'ping'), \
                        mock.patch.object(self.ctx, 'say') as say:
                    gh.return_value.issue_state.return_value = 'OPEN'
                    watchdog.watchdog(self.ctx)
                self.assertFalse(any('skipped' in str(c) for c in say.call_args_list))
                self.assertEqual(self.led.run(self.rid)['status'], 'ended')
                self.assertEqual(self.led.run(self.rid)['stop_reason'], 'silent')
                self.assertEqual(self.led.item('x', 1)['state'], state)
                self.assertEqual(self.led.item('x', 1)['attempts'], 0)
                self.assertEqual(self.led.active_runs(), [])
                if role == 'review':
                    self.assertIsNone(self.led.get_kv('review:x#1'))
                    self.assertIsNone(self.led.get_kv('reviewinconclusive:x#1:abc'))
                lease = self.led.lease('x', 1)
                self.assertTrue(lease is None or lease['holder'] != f'run:{self.rid}')
                st = tick._init_schedule_state(self.ctx, [self.pol])
                self.assertLess(st['in_project'].get('x', 0), self.pol['max_parallel'])

    def test_launch_inside_grace_is_not_finalized(self):
        with mock.patch.object(watchdog, 'finalize') as finish, \
                mock.patch.object(runner, 'kill') as kill:
            watchdog.watchdog(self.ctx)
        finish.assert_not_called()
        kill.assert_not_called()

    def test_log_without_recorded_path(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result = cli.cmd_log(SimpleNamespace(run_id=self.rid), self.cfg, self.led)
        self.assertEqual(result, 0)
        self.assertIn('no log recorded', output.getvalue())

    def test_null_helpers(self):
        self.assertFalse(runner.alive(None))
        with mock.patch.object(runner.os, 'killpg') as killpg:
            runner.signal_group(None, signal.SIGKILL)
            runner.kill(None)
            runner.terminate(None)
        killpg.assert_not_called()
        self.assertIsNone(runner.exit_code(self.led.run(self.rid)))
        self.assertEqual(runner.setup_tail(self.led.run(self.rid)), '')

    def test_null_log_matches_missing_log(self):
        self.assertEqual(platforms.read_log(None, 'cline'),
                         platforms.read_log(self.tmp.name + '/missing', 'cline'))
