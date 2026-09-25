"""Real squash reverts in isolated repositories, without GitHub or network."""

from pathlib import Path
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from mahler import config, janitor, ship
from mahler.console import actions, outbox, page, state
from mahler.gh import GH, GHError, _git
from mahler.ledger import Ledger
from test_console import make_cfg


class RevertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.write('before\n')
        self.git('commit', '-am', 'base')
        self.git('switch', '-c', 'feature')
        self.write('after\n')
        self.git('commit', '-am', 'feature')
        self.git('switch', 'main')
        self.git('merge', '--squash', 'feature')
        self.git('commit', '-m', 'squash')
        self.sha = self.git('rev-parse', 'HEAD')
        self.git('remote', 'add', 'origin', str(self.repo))
        self.cfg = make_cfg(projects={'mahler': {'path': str(self.repo),
                             'worktree_root': str(self.root / 'trees'),
                             'scope': 'label', 'scope_label': 'managed'}})
        self.led = Ledger(':memory:')
        self.addCleanup(self.led.close)
        self.gh = GH('test/repo')
        self.gh.pr_merge_info = Mock(return_value={'state': 'MERGED',
            'mergeCommit': {'oid': self.sha}, 'title': 'Feature', 'baseRefName': 'main'})
        self.gh.create_issue = Mock(return_value='https://github.com/test/repo/issues/99')
        self.gh.comment = Mock()
        self.ctx = SimpleNamespace(cfg=self.cfg, led=self.led, gh=lambda p: self.gh,
                                   dry_run=False, say=Mock())
        self.led.event('shipped', 'mahler', 1, {'pr': 2})
        self.event = self.led.q1("SELECT id FROM events WHERE kind='shipped'")['id']

    def git(self, *args):
        return _git(str(self.repo), *args)

    def write(self, text):
        (self.repo / 'file').write_text(text)
        self.git('add', 'file')

    def queue(self):
        return actions.run(self.cfg, self.led, 'revert', {'event': self.event})['id']

    def clean(self):
        self.assertEqual(self.git('worktree', 'list', '--porcelain').count('worktree '), 1)
        self.assertEqual(list((self.root / 'trees' / 'mahler').iterdir()), [])
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_revert_prepared_and_shipped_by_existing_pipeline(self):
        self.queue()
        outbox.drain(self.ctx)
        event = self.led.q1("SELECT * FROM events WHERE kind='revert_requested'")
        self.assertEqual((event['project'], event['number']), ('mahler', 1))
        self.assertEqual(json.loads(event['detail']), {
            'pr': 2, 'revert_issue': 99, 'sha': self.sha})
        item = self.led.item('mahler', 99)
        self.assertEqual(item['state'], 'verifying')
        self.assertEqual(item['branch'], 'mahler/revert-2')
        self.assertEqual(self.git('show', 'mahler/revert-2:file'), 'before')
        self.assertEqual(self.git('show', 'main:file'), 'after')
        self.clean()
        self.gh.create_issue.assert_called_once_with('Revert: Feature',
            f'Reverts PR #2 ({self.sha}), which shipped #1. Asked for from the console.',
            ['type:bug', 'p1', 'managed'])
        with self.assertRaises(actions.ActionError):
            self.queue()
        # Exercise the local-ref publishing path and normal PR opening; network
        # calls are fake, git pushes only to this test's local origin.
        self.gh.pr_for_head = Mock(return_value=None)
        self.gh.pr_create = Mock(return_value=100)
        self.gh.issue_body = Mock(return_value='')
        ship._open_pr(self.ctx, 'mahler', item, self.gh,
                      config.project_policy(self.cfg, 'mahler'))
        self.assertEqual(self.led.item('mahler', 99)['pr'], 100)
        self.assertEqual(self.git('show', 'mahler/99-revert-feature:file'), 'before')
        self.assertEqual(janitor._stale_branches(self.ctx,
                         config.project_policy(self.cfg, 'mahler'), str(self.repo)), [])

    def test_conflict_is_inbox_and_cleans_worktree(self):
        self.write('later change\n')
        self.git('commit', '-am', 'later')
        id = self.queue()
        outbox.drain(self.ctx)
        self.assertEqual(self.led.item('mahler', 99)['state'], 'inbox')
        self.assertEqual(self.led.q1('SELECT status FROM console_actions WHERE id=?', (id,))['status'], 'done')
        self.assertIn('conflicts', self.gh.comment.call_args_list[0].args[1])
        self.clean()

    def test_git_failure_is_failed_and_does_not_duplicate_issue(self):
        original = self.gh._git
        def fail(path, *args):
            if args[0] == 'revert':
                raise GHError('commit failed')
            return original(path, *args)
        self.gh._git = fail
        id = self.queue()
        outbox.drain(self.ctx)
        self.assertEqual(self.led.q1('SELECT status FROM console_actions WHERE id=?', (id,))['status'], 'failed')
        self.assertEqual(self.led.item('mahler', 99)['state'], 'inbox')
        self.clean()
        with self.assertRaises(actions.ActionError):
            self.queue()

    def test_validation_duplicate_queue_and_confirmation(self):
        for event in (True, '1', -1, 9999):
            with self.assertRaises(actions.ActionError):
                actions.run(self.cfg, self.led, 'revert', {'event': event})
        snapshot = state.build(self.cfg, self.led)
        doc = page.document(snapshot)
        self.assertIn('data-open-revert=', doc)
        self.assertIn('Revert mahler#1 squash-merged #2?', doc)
        self.assertIn('The branch is kept for 14 days.', doc)
        self.assertIn('Keep it', doc)
        self.queue()
        with self.assertRaises(actions.ActionError):
            self.queue()
        doc = page.document(state.build(self.cfg, self.led))
        self.assertIn('revert queued', doc)
        self.assertNotIn('data-open-revert=', doc)

    def test_nonmerged_pr_never_creates_issue(self):
        self.gh.pr_merge_info.return_value['state'] = 'CLOSED'
        self.queue()
        outbox.drain(self.ctx)
        self.gh.create_issue.assert_not_called()


if __name__ == '__main__':
    unittest.main()
