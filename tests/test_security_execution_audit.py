"""Finite execution-boundary probes; no agent, shell, Git or network is run."""
from contextlib import closing
from pathlib import Path
import shlex
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mahler import gh, platforms, runner, serve
from mahler.discuss_tools import DiscussionTools
from mahler.ledger import Ledger


class ExecutionAuditTests(unittest.TestCase):
    def test_launch_and_resume_metacharacters_remain_shell_data(self):
        prompt = "audit 'quoted'; $(synthetic_probe) `synthetic_probe`\nnext line"
        wt = "/synthetic/work tree; $(synthetic_probe)"
        paths = [wt + suffix for suffix in ("/agent log", "/exit'", "/stdin;")]
        for kind in ('claude', 'copilot', 'agy', 'cline', 'codex', 'kilo',
                     'agent-sdk', 'kiro', 'vibe'):
            conf = {'kind': kind, 'model': 'synthetic'}
            with self.subTest(kind=kind), patch.object(platforms, 'which', return_value='/synthetic/cli'):
                variants = [platforms.argv_for(conf, prompt, wt, 'build', 60, run_dir=wt)]
                if kind in ('claude', 'codex', 'cline', 'kilo', 'vibe'):
                    variants.append(platforms.resume_argv_for(
                        conf, prompt, wt, 'build', 60, session_id="session; $(synthetic_probe)"))
                for argv in variants:
                    if kind != 'agent-sdk':
                        self.assertEqual(argv.count(prompt), 1)
                    with patch.object(runner.subprocess, 'Popen') as popen:
                        runner.spawn(argv, wt, paths[0], paths[1], env={}, stdin_path=paths[2])
                    command = popen.call_args.args[0]
                    self.assertEqual(command[:2], ['/bin/sh', '-c'])
                    # A shell lexer must recover each original operand, including
                    # redirects' filenames; no payload creates a shell operator.
                    self.assertEqual(shlex.split(command[2]),
                                     argv + ['<', paths[2], '>', paths[0], '2>&1;',
                                             'echo', '$?', '>', paths[1]])
                    self.assertEqual(popen.call_args.kwargs['cwd'], wt)

    def test_option_like_pr_head_is_refused_before_execution(self):
        client = gh.GH('owner/repo')
        with patch.object(gh.subprocess, 'run') as run:
            for head in ('--upload-pack=synthetic', '-c', 'a' * 39, 'a' * 40 + ';probe'):
                with self.subTest(head=head), self.assertRaisesRegex(gh.GHError, 'invalid PR head'):
                    client.base_in_head('/synthetic/repo', 'main', head)
            run.assert_not_called()

    def test_option_like_tracked_filename_resolves_to_blob_operand(self):
        tool = object.__new__(DiscussionTools)
        filename = '--help; $(synthetic_probe)'
        oid = 'a' * 40
        with patch.object(tool, '_git', side_effect=[
                f'100644 blob {oid}\t{filename}\0'.encode(), b'contents']) as git:
            self.assertEqual(tool._read_file(filename), 'contents')
        self.assertEqual(git.call_args_list[0].args,
                         ('ls-tree', '-r', '-z', '--full-tree', 'HEAD'))
        self.assertEqual(git.call_args_list[1].args, ('cat-file', 'blob', oid))

    def test_discussion_unauthorized_names_and_project_do_not_dispatch(self):
        with closing(Ledger(':memory:')) as led:
            binding = dict(project='p', repo='owner/repo', account='personal', engine='claude')
            sid = led.create_chat_session(**binding)['id']
            tid = led.submit_chat_turn(sid, 'request', 'Hello', **binding)['id']
            token = led.claim_chat_turn(tid)
            cfg = {'defaults': {}, 'projects': {'p': {
                'repo': 'owner/repo', 'path': '/synthetic/repo', 'account': 'personal'}}}
            with patch('mahler.discuss_tools.project_client') as client:
                tool = DiscussionTools(cfg, led, sid, tid, token)
                with patch.object(tool, '_git') as git:
                    for name in ('shell; pause', '__getattribute__', '_mutate', 'claim', 'gh pr merge'):
                        with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'not allowed'):
                            tool.call(name, {})
                    with self.assertRaisesRegex(ValueError, 'cross-project'):
                        tool.call('list_repo_files', {'project': 'other'})
                    git.assert_not_called()
                self.assertEqual(client.return_value.mock_calls, [])
                self.assertEqual(led.q('SELECT * FROM leases'), [])

    def test_console_refusal_precedes_body_read_and_action(self):
        for address, headers in (
            ('203.0.113.10', {'X-Forwarded-For': '127.0.0.1'}),
            ('::ffff:203.0.113.10', {}),
            ('127.0.0.1', {'Origin': 'https://console.invalid.evil'}),
        ):
            with self.subTest(address=address, headers=headers):
                handler = object.__new__(serve._Handler)
                handler.client_address = (address, 1234)
                handler.command, handler.path = 'POST', '/api/pause'
                handler.headers = {'X-Mahler-Console': '1', 'Content-Type': 'application/json',
                                   'Host': 'console.invalid', 'Content-Length': '2', **headers}
                handler.rfile = Mock()
                handler.log_message, handler._json = Mock(), Mock()
                with patch.dict(serve.actions.ACTIONS, {'pause': Mock()}) as actions:
                    handler.do_POST()
                    actions['pause'].assert_not_called()
                handler.rfile.read.assert_not_called()
                self.assertEqual(handler._json.call_args.args[0], 403)

    def test_fence_quotes_hook_paths_and_checks_lease_before_chaining(self):
        with tempfile.TemporaryDirectory() as directory:
            original = "/synthetic/hooks 'quoted'; $(synthetic_probe)"
            binary = "/synthetic/mahler 'quoted'; $(synthetic_probe)"
            with patch.object(runner, 'git', return_value=original), \
                    patch.object(runner.config, 'MAHLER_BIN', binary):
                hooks = runner.fence_hooks('/synthetic/repo', directory)
            lines = (Path(hooks) / 'pre-push').read_text().splitlines()
            self.assertTrue(lines[1].startswith(shlex.quote(binary) + ' lease-check || {'))
            self.assertIn('exit 1;', lines[1])
            self.assertEqual(shlex.split(lines[2]),
                             ['[', '-x', original + '/pre-push', ']', '&&', 'exec',
                              original + '/pre-push', '$@'])


if __name__ == '__main__':
    unittest.main()
