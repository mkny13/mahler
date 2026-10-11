"""Work discussion routing/accounting with a fake provider; no real state or keys."""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mahler import api_credits, config, discuss, discuss_credits
from mahler.ledger import Ledger, iso
from test_api_credits import BASE_TOML, load, FakeUpstream, MESSAGE


class WorkDiscussionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.now = datetime(2026, 10, 20, tzinfo=timezone.utc)
        self.led = Ledger(str(self.root / 'ledger.db'), clock=lambda: self.now)
        self.addCleanup(self.led.close)
        self.cfg = load()
        self.cfg['projects']['p'] = dict(repo='o/r', account='work', enabled=True)
        self.name = 'work-claude-api'
        self.pconf = self.cfg['platforms'][self.name]
        self.pool = self.pconf['credit_pool']
        self.group = self.pool['group']
        self.led.set_kv(f'api_credits:key:{self.group}', json.dumps({'status': 'ok'}))
        self.led.set_kv(f'api_credits:cost:{self.group}', json.dumps(dict(
            fetched_at=iso(self.now), cycle_start=iso(datetime(2026, 10, 15, tzinfo=timezone.utc)), org_usd=0, workspace_usd=0)))
        self.binding = dict(project='p', repo='o/r', account='work', engine='agent-sdk')
        self.sid = self.led.create_chat_session(**self.binding)['id']
        with patch.object(api_credits, 'api_key_for_run', return_value='upstream-secret'):
            self.gw = api_credits._build_gateway(self.led, self.name, self.pconf, lambda *a, **k: None)
        self.upstream = FakeUpstream(200, b'{"usage":{"input_tokens":100,"output_tokens":50}}')
        self.gw.connect = lambda: self.upstream
        self.state = dict(port=12345, started='one')
        for patcher in (
                patch.object(api_credits, '_gateway_files', return_value=(str(self.root/'gateway.json'), 'unused')),
                patch.object(api_credits, '_read_state', side_effect=lambda path: dict(self.state)),
                patch.object(api_credits, 'gateway_healthy', return_value=True),
                patch('mahler.platforms.sdk_run_exe', return_value='/fake/mahler-sdk-run'),
                patch('mahler.platforms.oauth_usage', side_effect=AssertionError('no personal fallback')),
                patch.object(api_credits, 'api_key_for_run', side_effect=AssertionError('worker cannot read key'))):
            patcher.start(); self.addCleanup(patcher.stop)
        self.calls = []

    def turn(self, key='one', **kwargs):
        return self.led.submit_chat_turn(self.sid, key, 'Hello', **self.binding, **kwargs)

    def request(self, token):
        return self.gw.handle('POST', '/v1/messages?beta=true', {'x-api-key': token}, json.dumps(MESSAGE).encode())[0]

    def fake(self, argv, env, cwd, prompt, deadline, heartbeat, lock_fd):
        heartbeat()
        self.calls.append((argv, env, prompt))
        for _ in range(2):  # helper and main each reserve independently
            if self.request(env['ANTHROPIC_API_KEY']) != 200:
                raise discuss.TurnError('quota_unavailable', 'Gateway refused the request.')
        return 'Reply', 'sdk-session'

    def run_turn(self, turn, runner=None):
        discuss.process_turn(self.cfg, self.led, turn, runner=runner or self.fake)
        return json.loads(self.led.chat_turn(turn['id'])['result'])

    def test_helper_main_resume_duplicate_completion_and_secret_boundary(self):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'upstream-secret', 'ADMIN_KEY': 'admin-secret',
                                      'CLAUDE_CODE_OAUTH_TOKEN': 'personal-secret'}):
            first = self.turn()
            self.assertEqual(self.run_turn(first)['status'], 'success')
            self.assertEqual(self.run_turn(first)['status'], 'success')
            self.assertEqual(self.run_turn(self.turn('two'))['status'], 'success')
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[1][0][-2:], ['--resume', 'sdk-session'])
        rows = self.led.q('SELECT * FROM credit_reservations')
        self.assertEqual(len(rows), 4)
        for row in rows:
            self.assertIsNone(row['run_id'])
            self.assertTrue(row['attempt_key'].startswith('discussion:' + self.sid + ':'))
            self.assertEqual(row['status'], 'settled')
            self.led.settle_credit(self.group, row['grant_id'], row['attempt_key'], 999)
        self.assertEqual(self.led.q('SELECT * FROM runs'), [])
        self.assertAlmostEqual(self.led.credit_exposure(self.group, rows[0]['grant_id']), .0028)
        stored = repr([dict(r) for r in self.led.q('SELECT * FROM chat_sessions')]) + repr(
            [dict(r) for r in self.led.chat_turns(self.sid)])
        for argv, env, prompt in self.calls:
            self.assertEqual(self.request(env['ANTHROPIC_API_KEY']), 401)
            self.assertEqual(env['HOME'], str(self.root.resolve()/'discussion-sessions'/self.sid/'home'))
            for secret in ('upstream-secret', 'admin-secret', 'personal-secret'):
                self.assertNotIn(secret, repr((argv, env, prompt)) + stored)
            self.assertNotIn(env['ANTHROPIC_API_KEY'], repr(argv) + stored)

    def test_mcp_resolves_work_github_identity_outside_sdk_home(self):
        home = self.root / 'worker-home'
        identity = home / '.config' / 'gh-work'
        identity.mkdir(parents=True)
        (identity / 'hosts.yml').write_text('fake work login')
        config_path = self.root / 'config.toml'
        config_path.write_text(BASE_TOML.replace(
            'env = {}', 'env = { GH_CONFIG_DIR = "~/.config/gh-work" }') +
            '\n[projects.p]\nrepo = "o/r"\naccount = "work"\nenabled = true\n')
        fake_bin = self.root / 'bin'
        fake_bin.mkdir()
        gh = fake_bin / 'gh'
        gh.write_text(f'#!{sys.executable}\n' +
                      'import os\nfrom pathlib import Path\n' +
                      f'assert os.environ["GH_CONFIG_DIR"] == {str(identity)!r}\n' +
                      'assert (Path(os.environ["GH_CONFIG_DIR"]) / "hosts.yml").read_text() == "fake work login"\n' +
                      'assert "ANTHROPIC_API_KEY" not in os.environ\n' +
                      'print("[]")\n')
        gh.chmod(0o700)
        calls = []

        def runner(argv, env, cwd, prompt, deadline, heartbeat, lock_fd):
            self.assertNotEqual(env['HOME'], str(home))
            server = json.loads(argv[argv.index('--discussion-mcp') + 1])['mcpServers']['discussion']
            # Keep the real MCP process on the parent ledger's test clock.
            server['args'][1] = server['args'][1].replace('; main()',
                '; from mahler.ledger import Ledger, parse; '
                f'Ledger.now = lambda self: parse({iso(self.now)!r}); main()')
            child_env = {**env, **server.get('env', {})}
            child_env['PATH'] = str(fake_bin)
            result = subprocess.run([server['command'], *server['args']],
                env=child_env, cwd=cwd, input=json.dumps({
                    'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                    'params': {'name': 'list_items', 'arguments': {}}}) + '\n',
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            response = json.loads(result.stdout)['result']
            self.assertFalse(response.get('isError'), response)
            self.assertEqual(json.loads(response['content'][0]['text']), [])
            calls.append(response)
            return 'Reply', 'sdk-session'

        failures = []

        def checked_runner(*args):
            try:
                return runner(*args)
            except Exception as exc:
                failures.append(exc)
                raise

        with patch.object(config, 'HOME', str(home)):
            for key in ('first', 'resumed'):
                turn = self.turn(key)
                discuss.process_turn(self.cfg, self.led, turn, runner=checked_runner, config_path=config_path)
                if failures:
                    raise failures[0]
                self.assertEqual(json.loads(self.led.chat_turn(turn['id'])['result'])['status'], 'success')
        self.assertEqual(len(calls), 2)

    def test_project_policy_matrix(self):
        for i, (accounts, enabled, account, engine, expected) in enumerate([
                (['personal'], True, 'personal', 'claude', 'success'),
                (['work'], True, 'work', 'agent-sdk', 'success'),
                (['personal', 'work'], True, 'work', 'agent-sdk', 'binding_mismatch'),
                ([], True, 'work', 'agent-sdk', 'binding_mismatch'),
                (['work'], False, 'work', 'agent-sdk', 'binding_mismatch'),
                (['personal'], True, 'work', 'agent-sdk', 'binding_mismatch')]):
            with self.subTest(accounts=accounts, enabled=enabled):
                self.cfg['projects']['p'] = dict(repo='o/r', accounts=accounts, enabled=enabled)
                self.binding.update(account=account, engine=engine)
                self.sid = self.led.create_chat_session(**self.binding)['id']
                runner = self.fake
                if account == 'personal':
                    def runner(*args):
                        self.assertNotIn('ANTHROPIC_API_KEY', args[1])
                        self.assertNotIn('--discussion-mcp', args[0])
                        return 'Personal', 'personal-session'
                with patch('mahler.usage.discussion_capacity', return_value=('claude', [])), patch('mahler.usage.record_discussion_probe_usage'), patch(
                        'mahler.platforms.claude_exe', return_value='/fake/claude'):
                    result = self.run_turn(self.turn(str(i)), runner)
                self.assertEqual(result['status'] if not result['error'] else result['error']['code'], expected)

    def test_unavailable_pool_and_platforms_never_dispatch(self):
        for i, change in enumerate(('disabled', 'stale', 'auth', 'exhausted', 'ambiguous')):
            original = copy.deepcopy(self.cfg)
            with self.subTest(change=change):
                if change == 'disabled':
                    self.pool['enabled'] = False
                elif change == 'stale':
                    self.now += timedelta(hours=2)
                elif change == 'auth':
                    self.led.set_kv(f'api_credits:auth:{self.group}', json.dumps({'since': iso(self.now)}))
                elif change == 'exhausted':
                    grant = api_credits.current_grant(self.pool, self.now)
                    self.led.set_kv(f'api_credits:exhausted:{self.group}', json.dumps({'grant_id': grant['id']}))
                else:
                    self.cfg['platforms']['duplicate'] = copy.deepcopy(self.pconf)
                result = self.run_turn(self.turn(str(i)))
                self.assertEqual(result['error']['code'], 'quota_unavailable')
            self.cfg = original
            self.pconf = self.cfg['platforms'][self.name]; self.pool = self.pconf['credit_pool']
            self.now = datetime(2026, 10, 20, tzinfo=timezone.utc)
            for key in ('auth', 'exhausted'):
                self.led.set_kv(f'api_credits:{key}:{self.group}', '')
        self.assertEqual(self.calls, [])

    def test_restart_is_retryable_and_token_revoked(self):
        def restart(argv, env, cwd, prompt, deadline, heartbeat, lock_fd):
            self.local_token = env['ANTHROPIC_API_KEY']
            heartbeat()
            self.state['started'] = 'replacement'
            # Force the next periodic health check without a real sleep.
            with patch.object(discuss_credits.time, 'monotonic', return_value=deadline - 1):
                heartbeat()
            self.fail('gateway restart must terminate the turn')
        result = self.run_turn(self.turn(), restart)
        self.assertEqual(result['error']['code'], 'engine_error')
        self.assertTrue(result['error']['retryable'])
        self.assertEqual(self.request(self.local_token), 401)

    def test_uncertain_dispatch_retry_never_releases_or_bypasses_cap(self):
        def uncertain(argv, env, cwd, prompt, deadline, heartbeat, lock_fd):
            self.upstream.raises = OSError('upstream-secret admin-secret')
            self.assertEqual(self.request(env['ANTHROPIC_API_KEY']), 502)
            raise OSError('upstream-secret admin-secret')
        first = self.turn()
        result = self.run_turn(first, uncertain)
        self.assertNotIn('secret', json.dumps(result))
        row = self.led.q1('SELECT * FROM credit_reservations')
        self.assertEqual(row['status'], 'reserved')
        self.pool['allowance_usd'] = row['amount_usd'] * 1.5
        self.upstream.raises = None
        result = self.run_turn(self.turn('retry', retry_of=first['id']))
        self.assertEqual(result['error']['code'], 'quota_unavailable')
        self.assertEqual(len(self.led.q('SELECT * FROM credit_reservations')), 1)
        self.assertEqual(self.led.credit_exposure(self.group, row['grant_id']), row['amount_usd'])

    def test_gateway_checks_live_turn_account_and_fresh_usage(self):
        turn = self.turn()
        worker = self.led.claim_chat_turn(turn['id'])
        token = self.gw.tokens.register_discussion(self.sid, turn['id'], worker, 'work')
        with self.assertRaises(ValueError):
            self.gw.tokens.register_discussion(self.sid, turn['id'], worker, 'personal')
        self.assertEqual(self.request(token), 200)
        self.now += timedelta(seconds=121)
        self.assertEqual(self.request(token), 401)
        self.led.recover_chat_turns()
        turn = self.turn('retry', retry_of=turn['id'])
        worker = self.led.claim_chat_turn(turn['id'])
        token = self.gw.tokens.register_discussion(self.sid, turn['id'], worker, 'work')
        self.led.set_kv(f'api_credits:cost:{self.group}', '')
        self.assertEqual(self.request(token), 402)
        self.assertEqual(len(self.led.q('SELECT * FROM credit_reservations')), 1)

    def test_gateway_auth_and_exhaustion_reject_future_requests(self):
        for i, (status, message, key) in enumerate([
                (401, 'invalid x-api-key', 'auth'),
                (400, 'Your credit balance is too low', 'exhausted')]):
            with self.subTest(status=status):
                turn = self.turn(str(i))
                worker = self.led.claim_chat_turn(turn['id'])
                token = self.gw.tokens.register_discussion(self.sid, turn['id'], worker, 'work')
                self.upstream.status = status
                self.upstream.body = json.dumps({'error': {'message': message}}).encode()
                self.assertEqual(self.request(token), status)
                self.assertEqual(self.request(token), 402)
                rows = self.led.q('SELECT * FROM credit_reservations')
                self.assertEqual(len(rows), i + 1)
                self.assertEqual(rows[-1]['status'], 'released')
                with self.assertRaises(discuss.TurnError):
                    discuss_credits.check_capacity(self.led, self.name, self.pconf)
                self.led.complete_chat_turn(turn['id'], worker, assistant_text='Rejected')
                self.led.set_kv(f'api_credits:{key}:{self.group}', '')

    def test_shared_twenty_dollar_cap_and_persisted_authorization(self):
        turn = self.turn()
        worker = self.led.claim_chat_turn(turn['id'])
        token = self.gw.tokens.register_discussion(self.sid, turn['id'], worker, 'work')
        # Another process reads the same hashed authorization from the ledger.
        from contextlib import closing
        with closing(Ledger(self.led.path, clock=lambda: self.now)) as other:
            self.assertEqual(api_credits._TokenStore(other, self.group).lookup(token)['session_id'], self.sid)
        grant = api_credits.current_grant(self.pool, self.now)
        self.led.reserve_credit(self.group, grant['id'], 'other-client', 19.999, 20)
        self.assertEqual(self.request(token), 402)
        self.assertIsNone(self.upstream.sent)
        self.assertEqual(self.led.credit_exposure(self.group, grant['id']), 19.999)

    def test_work_retry_preserves_mutation_context_and_partial_links(self):
        def failed(argv, env, cwd, prompt, deadline, heartbeat, lock_fd):
            turn = self.led.q1("SELECT * FROM chat_turns WHERE status='running'")
            self.led.begin_chat_mutation(turn['id'], turn['worker_token'], 'create-one', 'add_item', {'title': 'x'})
            self.led.confirm_chat_mutation(self.sid, 'create-one', 'https://github.com/o/r/issues/1')
            raise discuss.TurnError('engine_error', 'Connection lost')
        first = self.turn()
        self.assertEqual(self.run_turn(first, failed)['confirmed_issue_urls'], ['https://github.com/o/r/issues/1'])
        self.assertEqual(self.run_turn(self.turn('retry', retry_of=first['id']))['status'], 'success')
        self.assertEqual(json.loads(self.calls[-1][2])['existing_mutations'][0]['operation_id'], 'create-one')


class SDKDiscussionTests(unittest.TestCase):
    def test_real_wrapper_with_fake_sdk_restricts_tools_and_returns_terminal_session(self):
        path = Path(__file__).resolve().parents[1]/'sdk_runner'/'mahler_sdk_run.py'
        spec = importlib.util.spec_from_file_location('test_discussion_sdk', path)
        sdk = importlib.util.module_from_spec(spec); spec.loader.exec_module(sdk)
        captured = []
        class Result:
            is_error = False
            errors = []
            usage = {}
            total_cost_usd = .001
            subtype = 'success'
            result = 'Reply'
            session_id = 'resumed'
        async def query(prompt, options):
            captured.append((prompt, options))
            yield Result()
        module = SimpleNamespace(ClaudeAgentOptions=lambda **kw: kw, ResultMessage=Result, query=query)
        recipe = path.parents[1]/'recipes'/'discuss.md'
        args = sdk._parse_args(['--prompt-file', '-', '--cwd', '.', '--model', 'approved-model',
                               '--discussion-mcp', '{"mcpServers":{"discussion":{"command":"fake"}}}',
                               '--discussion-system', str(recipe), '--resume', 'prior'])
        output = io.StringIO()
        with patch.dict('sys.modules', claude_agent_sdk=module), patch('sys.stdin', io.StringIO('message')), patch('sys.stdout', output):
            self.assertEqual(asyncio.run(sdk._run(args)), 0)
        opts = captured[0][1]
        self.assertEqual(opts['tools'], [])
        self.assertEqual(opts['permission_mode'], 'dontAsk')
        self.assertEqual(opts['setting_sources'], [])
        self.assertEqual(opts['resume'], 'prior')
        self.assertEqual(set(opts['mcp_servers']), {'discussion'})
        self.assertEqual(len(opts['allowed_tools']), 7)
        self.assertTrue(opts['strict_mcp_config'])
        self.assertIn('restricted', opts['extra_args'])
        self.assertEqual(discuss.engine_result(output.getvalue(), 0), ('Reply', 'resumed'))
