"""Interactive surfaces share the conductor's App identity (mahler#609)."""

import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from mahler import cli, config, github_app, mcp, scheduler, sync
from mahler.gh import GHError
from mahler.ledger import Ledger


class InteractiveIdentityTests(unittest.TestCase):
    def exercise(self, operation, identity):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg['projects']['x'] = {'repo': 'owner/repo', 'path': '/fake'}
        if identity != 'personal':
            cfg['projects']['x']['gh_account'] = 'work'
            cfg['accounts'] = {'work': {'env': {'GH_TOKEN': 'fake-human-token'}}}
        if identity == 'app':
            cfg['github_app'] = {'app_id': 123, 'installation_id': 456,
                                 'private_key_path': '/fake/key.pem'}
            cfg['projects']['x']['github_app_installation_id'] = 789
        led = Ledger(':memory:')
        self.addCleanup(led.close)
        led.upsert_item('x', 7, title='Test')
        calls = []

        def github(*args, env=None, transport="api", **kwargs):
            calls.append((args, env, transport))
            if args[:2] == ('pr', 'view'):
                return json.dumps({'state': 'OPEN', 'body': '', 'headRefName': 'branch'})
            if args[:2] == ('pr', 'list'):
                return '[]'
            if args[:2] == ('release', 'view') or '/git/ref/tags/' in str(args):
                raise GHError('not found')
            if args[0] == 'api' and '/commits/' in args[1]:
                return json.dumps({'sha': 'abc123'})
            return 'https://github.com/owner/repo/issues/8'

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(config, 'STATE', tmp), \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(github_app.Installation, 'token', autospec=True,
                                  return_value='fake-app-token') as token, \
                mock.patch('mahler.gh._gh', side_effect=github), \
                mock.patch('mahler.gh._git', side_effect=lambda *args, **kwargs:
                           github(*args, transport='git', **kwargs)), \
                contextlib.redirect_stdout(io.StringIO()):
            if operation == 'add':
                rc = cli.cmd_add(SimpleNamespace(project='x', title='New', body='', label=[]), cfg, led)
            elif operation == 'labels':
                rc = cli.cmd_labels(SimpleNamespace(project='x'), cfg, led)
            elif operation in ('ship', 'ship_branch'):
                rc = cli.cmd_ship(SimpleNamespace(item=('x', 7), holder='me',
                    pr=9 if operation == 'ship' else None, branch='branch', summary=None), cfg, led)
            elif operation == 'release':
                rc = cli.cmd_release(SimpleNamespace(target='x', version='0.1.0', publish=True), cfg, led)
                self.assertTrue(any(args[:2] == ('release', 'create') for args, _, _ in calls))
            elif operation in ('claim', 'lease_release'):
                self.assertEqual(cli.cmd_claim(SimpleNamespace(
                    item=('x', 7), holder='me', steal=False), cfg, led), 0)
                if operation == 'lease_release':
                    cli.cmd_release(SimpleNamespace(target=('x', 7), holder='me'), cfg, led)
                ctx = scheduler.Ctx(cfg, led)
                ctx._labels[('x', 7)] = []
                sync.mirror_labels(ctx, 'x')
                rc = 0
            else:
                request = {'id': 1, 'method': 'tools/call', 'params': {
                    'name': operation, 'arguments': {'project': 'x', 'title': 'New',
                                                   'number': 7, 'comment': 'Notes'}}}
                out = io.StringIO()
                with mock.patch('sys.stdin', io.StringIO(json.dumps(request) + '\n')), \
                        contextlib.redirect_stdout(out):
                    mcp.serve(cfg, led)
                self.assertIn('result', json.loads(out.getvalue()))
                rc = 0
            self.assertEqual(rc, 0)
            self.assertTrue(calls)
            for _, env, transport in calls:
                if identity == 'personal':
                    self.assertIsNone(env)
                else:
                    self.assertEqual(env['GH_TOKEN'], 'fake-app-token' if identity == 'app' and transport == 'api'
                                     else 'fake-human-token')
            if identity == 'app':
                self.assertTrue(token.called)
                for call in token.call_args_list:
                    self.assertEqual(call.args[0].installation_id, '789')
            else:
                token.assert_not_called()

    def test_command_identity(self):
        for operation in ('add', 'labels', 'ship', 'ship_branch', 'release',
                          'add_item', 'handoff', 'claim', 'lease_release'):
            for identity in ('app', 'work', 'personal'):
                with self.subTest(operation=operation, identity=identity):
                    self.exercise(operation, identity)
