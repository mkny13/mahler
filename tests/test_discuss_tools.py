"""Restricted discussion boundary, with local git fixtures and mocked GitHub only."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mahler.discuss_tools import DiscussionTools, MAX_FILE_BYTES, TOOL_FIELDS
from mahler.gh import AGENT_NOTE, GH, GHError
from mahler.ledger import Ledger


class DiscussionToolsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        (self.repo / 'README.md').write_text('Repository context')
        (self.root / 'private').write_text('outside secret')
        (self.repo / 'escape').symlink_to(self.root / 'private')
        (self.repo / 'binary').write_bytes(b'\0binary')
        (self.repo / 'large').write_text('x' * (MAX_FILE_BYTES + 1))
        (self.repo / 'invalid').write_bytes(b'\xff')
        self.git('add', '.')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture')
        (self.repo / 'untracked').write_text('private')
        self.cfg = {'defaults': {}, 'projects': {'p': {
            'repo': 'owner/repo', 'path': str(self.repo), 'account': 'personal',
            'scope': 'label', 'scope_label': 'managed'}}}
        self.led = Ledger(':memory:')
        self.addCleanup(self.led.close)
        self.binding = dict(project='p', repo='owner/repo', account='personal', engine='claude')
        self.sid = self.led.create_chat_session(**self.binding)['id']
        self.turn = self.led.submit_chat_turn(self.sid, 'request', 'Hello', **self.binding)['id']
        self.token = self.led.claim_chat_turn(self.turn)
        client = patch('mahler.discuss_tools.project_client')
        self.client_factory = client.start()
        self.addCleanup(client.stop)
        self.gh = self.client_factory.return_value
        self.issue = {'number': 12, 'title': 'Original', 'body': '> **Original request:** Words\n\n'
                      'Part of #1\nDepends on: #2\n\n## Owner answers\nKeep it blue.',
                      'comments': [{'body': 'Owner says yes'}], 'labels': ['managed', 'mahler:needs-you'],
                      'url': 'https://github.com/owner/repo/issues/12', 'version': 'v1'}
        self.gh.discussion_issue.side_effect = lambda n: copy.deepcopy(self.issue)
        self.gh.issue_by_marker.return_value = None
        self.gh.create_issue.return_value = self.issue['url']
        self.gh.discussion_edit.return_value = self.issue['url']
        self.tools = DiscussionTools(self.cfg, self.led, self.sid, self.turn, self.token)

    def git(self, *args):
        subprocess.run(['git', '-C', str(self.repo), *args], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def call(self, name, **args):
        return self.tools.call(name, args)

    def test_allowed_reads_and_project_client(self):
        self.assertEqual(set(TOOL_FIELDS), {'read_repo_file', 'list_repo_files', 'list_items',
                         'get_item', 'add_item', 'edit_item', 'answer_item'})
        self.client_factory.assert_called_once()
        self.assertEqual(self.client_factory.call_args.args[1]['repo'], 'owner/repo')
        files = self.call('list_repo_files')
        self.assertIn('README.md', files)
        self.assertNotIn('escape', files)
        self.assertNotIn('untracked', files)
        self.assertEqual(self.call('read_repo_file', path='README.md'), 'Repository context')
        # Working-tree replacement cannot turn a tracked read into a home-file read.
        (self.repo / 'README.md').unlink()
        (self.repo / 'README.md').symlink_to(self.root / 'private')
        self.assertEqual(self.call('read_repo_file', path='README.md'), 'Repository context')
        self.gh.open_issues.return_value = [self.issue, {'number': 13, 'labels': []}]
        self.assertEqual(len(self.call('list_items')), 1)
        self.assertEqual(self.call('get_item', number=12)['comments'], self.issue['comments'])

    def test_denies_bypass_cross_project_and_bad_paths(self):
        for name in ('shell', 'write_file', 'merge', 'claim', 'release', 'heartbeat', 'handoff', 'next_id', 'http'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'not allowed'):
                self.call(name)
        for name in TOOL_FIELDS:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'cross-project'):
                self.call(name, project='other')
        for path in ('/etc/passwd', '../private', 'dir/../../private', 'escape', 'escape/child',
                     '.git/config', 'untracked', 'binary', 'large', 'invalid', 'a\\b', './README.md'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.call('read_repo_file', path=path)
        with self.assertRaisesRegex(ValueError, 'unexpected'):
            self.call('add_item', operation_id='a', title='x', body='', claim=True)
        with self.assertRaises(ValueError):
            self.call('get_item', number='../pulls/1')
        self.assertEqual(self.led.q('SELECT * FROM leases'), [])
        self.assertEqual(self.led.q('SELECT * FROM runs'), [])
        self.gh.create_issue.assert_not_called()

    def test_scope_and_configuration_changes_fail_closed(self):
        self.issue['labels'] = []
        for name, args in [('get_item', {}), ('answer_item', {'operation_id': 'a', 'answer': 'yes'}),
                           ('edit_item', {'operation_id': 'e', 'expected_version': 'v1', 'refinement': 'new'})]:
            with self.assertRaisesRegex(ValueError, 'scope'):
                self.call(name, number=12, **args)
        self.cfg['projects']['p']['account'] = 'work'
        with self.assertRaisesRegex(ValueError, 'binding_mismatch'):
            self.call('list_repo_files')

    def test_create_scope_duplicate_and_confirmed_link(self):
        result = self.call('add_item', operation_id='create', title='New', body='Request')
        self.assertTrue(result['confirmed'])
        self.assertEqual(result['issue_url'], self.issue['url'])
        args = self.gh.create_issue.call_args.args
        self.assertEqual(args[0], 'New')
        self.assertIn('<!-- mahler:discussion:', args[1])
        self.assertEqual(args[2], ['managed'])
        self.assertEqual(self.call('add_item', operation_id='create', title='New', body='Request'), result)
        self.assertEqual(self.gh.create_issue.call_count, 1)
        self.assertEqual(self.led.chat_receipts(self.turn), [self.issue['url']])

    def test_create_ambiguous_response_reconciles_without_recreating(self):
        markers = []
        def create(title, body, labels):
            markers.append(body.split('\n\n')[-1])
            raise GHError('response lost')
        self.gh.create_issue.side_effect = create
        with self.assertRaisesRegex(GHError, 'response lost'):
            self.call('add_item', operation_id='create', title='New', body='')
        self.assertEqual(self.led.chat_receipts(self.turn), [])
        with self.assertRaisesRegex(GHError, 'uncertain'):
            self.call('add_item', operation_id='create', title='New', body='')
        self.gh.issue_by_marker.return_value = self.issue['url']
        result = self.call('add_item', operation_id='create', title='New', body='')
        self.assertTrue(result['confirmed'])
        self.gh.issue_by_marker.assert_called_with(markers[0])
        self.assertEqual(self.gh.create_issue.call_count, 1)

    def test_answer_ambiguous_response_and_retry_turn(self):
        def comment(number, body):
            self.assertTrue(body.startswith(AGENT_NOTE + '\n'))
            self.issue['comments'].append({'body': body})
            raise GHError('response lost')
        self.gh.comment.side_effect = comment
        result = self.call('answer_item', number=12, operation_id='answer', answer='Yes')
        self.led.complete_chat_turn(self.turn, self.token, assistant_text='Failed later',
            error={'code': 'engine_error', 'message': 'lost', 'retryable': True})
        turn = self.led.submit_chat_turn(self.sid, 'retry', 'Retry', retry_of=self.turn, **self.binding)['id']
        token = self.led.claim_chat_turn(turn)
        self.tools = DiscussionTools(self.cfg, self.led, self.sid, turn, token)
        self.assertEqual(self.call('answer_item', number=12, operation_id='answer', answer='Yes'), result)
        self.assertEqual(self.gh.comment.call_count, 1)
        self.assertEqual(self.led.chat_receipts(turn), [self.issue['url']])
        with self.assertRaisesRegex(ValueError, 'fenced'):
            DiscussionTools(self.cfg, self.led, self.sid, self.turn, self.token)

    def test_answer_success_and_edit_conflict(self):
        self.call('answer_item', number=12, operation_id='answer', answer='Yes')
        body = self.gh.comment.call_args.args[1]
        self.assertTrue(body.startswith(AGENT_NOTE + '\nYes\n'))
        self.call('edit_item', number=12, operation_id='edit', expected_version='v1', refinement='Acceptance')
        self.gh.discussion_edit.assert_called_once()
        self.gh.discussion_edit.side_effect = GHError('conflict: owner changed content')
        with self.assertRaisesRegex(GHError, 'conflict'):
            self.call('edit_item', number=12, operation_id='edit2', expected_version='old', refinement='Acceptance')

    def test_untrusted_links_never_become_receipts(self):
        self.gh.create_issue.return_value = 'https://github.com/other/repo/issues/1'
        with self.assertRaisesRegex(ValueError, 'URL'):
            self.call('add_item', operation_id='create', title='New', body='')
        self.assertEqual(self.led.chat_receipts(self.turn), [])

    def test_session_turn_mismatch(self):
        other = self.led.create_chat_session(**self.binding)['id']
        with self.assertRaisesRegex(ValueError, 'binding_mismatch'):
            DiscussionTools(self.cfg, self.led, other, self.turn, self.token)


class DiscussionGitHubTests(unittest.TestCase):
    @patch('mahler.gh._gh')
    def test_get_comments_revision_and_append_preserves_everything(self, command):
        issue = {'number': 12, 'title': 'Original', 'body': '> **Original request:** Keep all this\n'
                 'Part of #1\nDepends on: #2\n## Owner answer\nBlue',
                 'labels': [{'name': 'managed'}, {'name': 'mahler:needs-you'}],
                 'html_url': 'https://github.com/owner/repo/issues/12', 'updated_at': 'now'}
        comment = {'body': 'Yes', 'id': 1, 'created_at': 'now', 'user': {'login': 'owner'}}
        def respond(*args, **kw):
            if args[0] == 'api' and 'comments?' in args[1]:
                return json.dumps([[comment]])
            if args[0] == 'api':
                return json.dumps(issue)
            return ''
        command.side_effect = respond
        gh = GH('owner/repo')
        original = gh.discussion_issue(12)
        self.assertEqual(original['comments'][0]['body'], 'Yes')
        gh.discussion_edit(12, original['version'], 'New acceptance check', '<!-- marker -->')
        write = command.call_args
        self.assertEqual(write.args[:2], ('issue', 'edit'))
        self.assertTrue(write.kwargs['input'].startswith(issue['body'] + '\n\n'))
        self.assertIn('New acceptance check', write.kwargs['input'])
        self.assertNotIn('--remove-label', write.args)
        self.assertNotIn('--add-label', write.args)
        command.reset_mock()
        def fenced(issue):
            raise ValueError('worker fenced after remote read')
        with self.assertRaisesRegex(ValueError, 'worker fenced'):
            gh.discussion_edit(12, original['version'], 'New', '<!-- marker -->', before_write=fenced)
        self.assertFalse(any(c.args[0] == 'issue' for c in command.call_args_list))
        command.reset_mock()
        # Even a new owner answer without a body update invalidates the version.
        comment['body'] = 'Actually no'
        with self.assertRaisesRegex(GHError, 'conflict'):
            gh.discussion_edit(12, original['version'], 'New', '<!-- marker -->')
        self.assertFalse(any(c.args[0] == 'issue' for c in command.call_args_list))

    @patch('mahler.gh._gh')
    def test_pull_request_rejected(self, command):
        command.return_value = json.dumps({'pull_request': {}})
        with self.assertRaisesRegex(GHError, 'not pull requests'):
            GH('owner/repo').discussion_issue(1)
