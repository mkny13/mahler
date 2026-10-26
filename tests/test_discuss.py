"""Personal worker checks: fake engine plus real bounded child processes."""
from contextlib import closing
from datetime import timedelta
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from mahler import config, discuss, usage
from mahler.ledger import Ledger, iso


class DiscussionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = str(Path(self.directory.name) / 'ledger.db')
        self.led = Ledger(self.db)
        self.addCleanup(self.led.close)
        self.cfg = {'defaults': {}, 'projects': {'p': {'repo': 'o/r', 'account': 'personal'}},
                    'platforms': {'claude': {'kind': 'claude', 'quota_group': 'claude'}}}
        self.binding = dict(project='p', repo='o/r', account='personal', engine='claude')
        self.sid = self.led.create_chat_session(**self.binding)['id']
        self.calls = []
        self.samples = [('5h', 90, iso(self.led.now() + timedelta(hours=1))),
                        ('weekly', 90, iso(self.led.now() + timedelta(days=1)))]
        probe = patch('mahler.platforms.oauth_usage', return_value=self.samples)
        self.probe = probe.start(); self.addCleanup(probe.stop)
        exe = patch('mahler.platforms.claude_exe', return_value='/fake/claude')
        exe.start(); self.addCleanup(exe.stop)

    def turn(self, key='one', **kwargs):
        return self.led.submit_chat_turn(self.sid, key, 'Hello', **self.binding, **kwargs)

    def fake(self, argv, env, cwd, prompt, deadline, heartbeat, lock_fd):
        heartbeat()
        self.calls.append((argv, env, prompt))
        return 'A reply', 'engine-session'

    def run_turn(self, turn, runner=None):
        discuss.process_turn(self.cfg, self.led, turn, runner=runner or self.fake)
        return json.loads(self.led.chat_turn(turn['id'])['result'])

    def test_console_item_context_reaches_worker(self):
        self.led.set_kv('chat-item:' + self.sid, '9')
        self.run_turn(self.turn())
        self.assertEqual(json.loads(self.calls[0][2])['issue_number'], 9)

    def test_two_turn_resume_operator_usage(self):
        self.assertEqual(self.run_turn(self.turn())['status'], 'success')
        self.assertEqual(self.run_turn(self.turn('two'))['status'], 'success')
        self.assertNotIn('--resume', self.calls[0][0])
        self.assertEqual(self.calls[1][0][-2:], ['--resume', 'engine-session'])
        self.assertEqual(self.led.q('SELECT * FROM runs'), [])
        self.assertEqual(self.led.q('SELECT * FROM items'), [])
        self.assertIsNotNone(self.led.get_kv('human:claude'))
        self.assertEqual(len(self.led.q("SELECT * FROM events WHERE kind='discussion-usage'")), 2)

    def test_changed_binding(self):
        turn = self.turn()
        for key, value in [('repo', 'other/repo'), ('account', 'work')]:
            with self.subTest(key=key):
                original = self.cfg['projects']['p'][key]
                self.cfg['projects']['p'][key] = value
                result = self.run_turn(turn)
                self.assertEqual(result['error']['code'], 'binding_mismatch')
                self.cfg['projects']['p'][key] = original
                turn = self.turn(key)
        self.assertEqual(self.calls, [])

    def test_work_unavailable(self):
        self.cfg['projects']['p']['account'] = 'work'
        self.binding['account'] = 'work'
        self.binding['engine'] = 'agent-sdk'
        self.sid = self.led.create_chat_session(**self.binding)['id']
        self.assertEqual(self.run_turn(self.turn())['error']['code'], 'quota_unavailable')
        self.probe.assert_not_called()

    def test_unknown_exhausted_and_expired_quota(self):
        for i, samples in enumerate([[], [('5h', 10, self.samples[0][2])],
                [('5h', 100, self.samples[0][2]), self.samples[1]],
                [('5h', float('nan'), self.samples[0][2]), self.samples[1]],
                [('5h', 20, iso(self.led.now() - timedelta(seconds=1))), self.samples[1]]]):
            self.probe.return_value = samples
            self.assertEqual(self.run_turn(self.turn(str(i)))['error']['code'], 'quota_unavailable')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.led.q('SELECT * FROM runs'), [])

    def test_launch_boundary(self):
        base = {key: 'secret' for key in config.CREDENTIAL_VARS}
        base.update(HOME='/work', CLAUDE_CODE_USE_BEDROCK='1', AWS_SECRET_ACCESS_KEY='secret',
                    CLAUDE_CODE_SIMPLE='1', PATH='/bin')
        env = config.discussion_env(base)
        self.assertEqual(env, {'HOME': config.HOME, 'PATH': '/bin'})
        self.run_turn(self.turn())
        argv = self.calls[0][0]
        self.assertEqual(argv[argv.index('--tools') + 1], '')
        self.assertIn('--restricted', argv)
        self.assertIn('--disable-slash-commands', argv)
        self.assertIn('--strict-mcp-config', argv)
        self.assertEqual(argv[argv.index('--setting-sources') + 1], '')
        self.assertEqual(set(json.loads(argv[argv.index('--mcp-config') + 1])['mcpServers']), {'discussion'})
        self.assertNotIn('--bare', argv)
        self.assertNotIn('--fallback-model', argv)

    def test_partial_receipts_and_retry_identity(self):
        def failed(*args):
            turn = self.led.q1("SELECT * FROM chat_turns WHERE status='running'")
            self.led.begin_chat_mutation(turn['id'], turn['worker_token'], 'same', 'add_item', {'title': 'a'})
            self.led.confirm_chat_mutation(self.sid, 'same', 'https://github.com/o/r/issues/1')
            raise discuss.TurnError('engine_error', 'Lost reply')
        first = self.turn()
        result = self.run_turn(first, failed)
        self.assertEqual(result['confirmed_issue_urls'], ['https://github.com/o/r/issues/1'])
        self.run_turn(self.turn('retry', retry_of=first['id']))
        context = json.loads(self.calls[-1][2])
        self.assertEqual(context['existing_mutations'][0]['operation_id'], 'same')

    def test_restart_and_expired_queue(self):
        turn = self.turn()
        self.led.claim_chat_turn(turn['id'])
        stopped = threading.Event(); stopped.set()
        discuss.worker(self.db, lambda: self.cfg, stopped)
        result = json.loads(self.led.chat_turn(turn['id'])['result'])
        self.assertEqual(result['error']['code'], 'worker_lost')
        turn = self.turn('expired')
        self.led.con.execute('UPDATE chat_turns SET deadline_at=? WHERE id=?',
                             (iso(self.led.now() - timedelta(seconds=1)), turn['id']))
        self.assertEqual(self.run_turn(turn)['error']['code'], 'timeout')

    def test_lock_file_removed_on_clean_exit(self):
        stopped = threading.Event(); stopped.set()
        self.assertEqual(discuss.worker(self.db, lambda: self.cfg, stopped), 0)
        self.assertFalse(os.path.exists(self.db + '.discussion.lock'))

    def test_duplicate_lock(self):
        with open(self.db + '.discussion.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(discuss.worker(self.db), 75)

    def test_real_child_output_death_timeout_and_reaping(self):
        scripts = [('print("malformed")', 'invalid_result'),
                   ('import sys; sys.exit(7)', 'engine_error'),
                   ('import time; time.sleep(60)', 'timeout'),
                   ('print("x" * 300000)', 'invalid_result')]
        real_popen = subprocess.Popen
        for script, code in scripts:
            children = []
            def spawn(*args, **kwargs):
                p = real_popen(*args, **kwargs); children.append(p); return p
            with patch('mahler.discuss.subprocess.Popen', side_effect=spawn):
                with self.assertRaises(discuss.TurnError) as caught:
                    discuss.run_engine([sys.executable, '-c', script], dict(os.environ),
                        self.directory.name, '', time.monotonic() + .3, lambda: None)
            self.assertEqual(caught.exception.error['code'], code)
            self.assertIsNotNone(children[0].returncode)
            with self.assertRaises(ProcessLookupError):
                os.kill(children[0].pid, 0)

    def test_successful_real_child(self):
        value = dict(type='result', subtype='success', is_error=False, result='Reply', session_id='id')
        self.assertEqual(discuss.run_engine([sys.executable, '-c', 'print(' + repr(json.dumps(value)) + ')'],
            dict(os.environ), self.directory.name, '', time.monotonic() + 3, lambda: None), ('Reply', 'id'))

    def test_supervisor_start_failure_is_visible_and_retries(self):
        supervisor = discuss.Supervisor(self.db)
        calls = []
        def fail(*args, **kwargs):
            calls.append(1)
            self.assertNotIn('ANTHROPIC_API_KEY', kwargs['env'])
            self.assertNotIn('ANTHROPIC_ADMIN_KEY', kwargs['env'])
            if len(calls) == 2:
                supervisor.stop.set()
            raise OSError('Cannot start')
        with patch.dict(os.environ, ANTHROPIC_API_KEY='upstream-secret', ANTHROPIC_ADMIN_KEY='admin-secret'), \
                patch('mahler.discuss.subprocess.Popen', side_effect=fail):
            supervisor.start()
            supervisor.thread.join(timeout=4)
            supervisor.close()
        self.assertEqual(len(calls), 2)
        self.assertIn('start failed', self.led.get_kv('discussion:worker'))

    def test_supervisor_gives_up_after_max_retries(self):
        supervisor = discuss.Supervisor(self.db, max_retries=3)
        calls = []
        def fail(*args, **kwargs):
            calls.append(1)
            raise OSError('Cannot start')
        with patch('mahler.discuss.subprocess.Popen', side_effect=fail):
            supervisor.start()
            supervisor.thread.join(timeout=10)
            self.assertFalse(supervisor.thread.is_alive())
            supervisor.close()
        self.assertEqual(len(calls), 3)
        self.assertIn('permanently', self.led.get_kv('discussion:worker'))

    def test_supervisor_shutdown_reaps_child(self):
        # Real worker on an empty temporary ledger; no engine or account probe.
        supervisor = discuss.Supervisor(self.db)
        supervisor.start()
        limit = time.monotonic() + 3
        while supervisor.child is None and time.monotonic() < limit:
            time.sleep(.01)
        supervisor.close()
        self.assertIsNotNone(supervisor.child)
        self.assertIsNotNone(supervisor.child.returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(supervisor.child.pid, 0)

    def test_supervisor_removes_orphan_engine_after_worker_death(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
        try:
            supervisor = discuss.Supervisor(self.db)
            supervisor.child = type('Worker', (), {'pid': 12345})()
            Path(self.db + '.discussion-process').write_text(json.dumps({'worker': 12345, 'engine': child.pid}))
            supervisor.clean_engine()
            self.assertEqual(child.wait(timeout=3), -signal.SIGKILL)
        finally:
            if child.poll() is None:
                discuss.stop_process(child)

    def test_failed_engine_result_is_persisted(self):
        for code in ('timeout', 'invalid_result', 'engine_error'):
            def fail(*args):
                raise discuss.TurnError(code, 'Failed')
            result = self.run_turn(self.turn(code), fail)
            self.assertEqual(result['status'], 'error')
            self.assertEqual(result['error']['code'], code)
            self.assertTrue(result['error']['retryable'])

    def test_supervisor_shutdown_fences_interrupted_turn(self):
        supervisor = discuss.Supervisor(self.db)
        supervisor.start()
        try:
            limit = time.monotonic() + 3
            while self.led.get_kv('discussion:worker') != 'Running' and time.monotonic() < limit:
                time.sleep(.01)
            # Binding rejection would be processed on the next poll; stop promptly.
            turn = self.turn()
            self.led.claim_chat_turn(turn['id'])
        finally:
            supervisor.close()
        self.assertEqual(json.loads(self.led.chat_turn(turn['id'])['result'])['error']['code'], 'worker_lost')
