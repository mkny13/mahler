"""Presence fixtures are synthetic and never inspect the user's Claude history."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler import presence
from mahler.console import state


class PresenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = '/projects/mahler'
        self.cwd = self.project + '/.claude/worktrees/ops-session'
        self.now = datetime.now(timezone.utc)
        self.patch = mock.patch.object(presence, 'CLAUDE_PROJECTS', str(self.root))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def write(self, rows, cwd=None, name='session'):
        folder = self.root / presence.encode(cwd or self.cwd)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (name + '.jsonl')
        path.write_text('\n'.join(json.dumps(row) for row in rows))
        os.utime(path, (self.now.timestamp(), self.now.timestamp()))
        return path

    def message(self, tool=None, minutes=3, command=None):
        content = [{'type': 'text', 'text': 'Checking the queue'}]
        if tool:
            content.append({'type': 'tool_use', 'name': tool,
                            'input': {'command': command} if command else {}})
        return {'type': 'assistant', 'cwd': self.cwd,
                'timestamp': (self.now - timedelta(minutes=minutes)).isoformat(),
                'message': {'content': content}}

    def active(self, minutes=20):
        return presence.human_claude_active([
            {'path': self.project, 'hot_hold_minutes': minutes}])

    def test_read_only_does_not_hold(self):
        self.write([self.message('Read'), self.message('Bash', command='mahler status'),
                    self.message('Bash', command='gh issue create --title bug'),
                    self.message('Bash', command='git status'), self.message()])
        self.assertIsNone(presence.last_claude_activity(self.project))
        self.assertFalse(self.active())

    def test_recent_writes_hold_and_name_session(self):
        for tool, command in [('Edit', None), ('Write', None), ('NotebookEdit', None),
                              ('Bash', 'git commit -m fix'),
                              ('Bash', 'git -C /projects/mahler push origin HEAD')]:
            with self.subTest(tool=tool, command=command):
                self.write([self.message(tool, command=command), self.message(minutes=0)])
                self.assertEqual(presence.last_claude_activity(self.project),
                                 self.now - timedelta(minutes=3))
                self.assertTrue(self.active())
                self.assertFalse(self.active(2))
                holds = state._hot_holds(None, [{'name': 'mahler', 'path': self.project,
                                                'hot_hold': True, 'hot_hold_minutes': 20}], self.now)
                self.assertEqual(holds[0]['directory'], self.cwd)
                self.assertIn('ops-session (last edit 3m ago)', state._hot_hold_text(holds[0]))

    def test_git_arguments_do_not_count_as_write_subcommands(self):
        for command in ['git log --grep=commit', 'git show HEAD:docs/push.md',
                        'git -C "/projects/commit repo" log --grep=push',
                        'git -c alias.example=commit status',
                        'git --git-dir push status',
                        'git diff -- commit push', 'echo "git commit"',
                        'git status # git push', 'git --help push']:
            with self.subTest(command=command):
                self.write([self.message('Bash', command=command)])
                self.assertIsNone(presence.last_claude_activity(self.project))
                self.assertFalse(self.active())

    def test_git_writes_with_options_and_shell_sequences_hold(self):
        for command in ['git -C "/projects/my repo" -c user.name=Test commit -m fix',
                        'git --git-dir=/projects/repo/.git push',
                        'git -C/projects/repo push',
                        'git status && git push origin HEAD',
                        'git log --grep=commit; git commit -m fix',
                        'git status\ngit push', '/usr/bin/git push']:
            with self.subTest(command=command):
                self.write([self.message('Bash', command=command)])
                self.assertTrue(self.active())
                self.assertFalse(presence.last_claude_edit(self.project).fallback)

    def test_unparseable_shell_command_retains_fallback(self):
        self.write([self.message('Bash', command="git commit -m 'unfinished")])
        self.assertTrue(self.active())
        self.assertTrue(presence.last_claude_edit(self.project).fallback)

    def test_old_edit_with_recent_chat_does_not_hold(self):
        self.write([self.message('Edit', minutes=30), self.message(minutes=0)])
        self.assertFalse(self.active())
        self.assertTrue(self.active(40))

    def test_malformed_unknown_empty_and_missing_timestamp_hold(self):
        missing = self.message('Edit')
        del missing['timestamp']
        for rows in [[{'unknown': True}], [], [missing], [None],
                     [{'type': 'assistant', 'message': None}]]:
            with self.subTest(rows=rows):
                self.write(rows)
                self.assertTrue(self.active())
                self.assertTrue(presence.last_claude_edit(self.project).fallback)
        path = self.write([self.message()])
        path.write_text('{broken json')
        self.assertTrue(self.active())

    def test_unreadable_transcript_holds(self):
        self.write([self.message()])
        with mock.patch('builtins.open', side_effect=PermissionError):
            self.assertTrue(self.active())

    def test_other_projects_and_mahler_runs_do_not_count(self):
        self.write([self.message('Edit')], cwd=self.project + 'X')
        self.write([self.message('Edit')], cwd='/other/.mahler/worktrees/mahler/453')
        self.assertFalse(self.active())

    def test_latest_writer_wins_over_newer_readonly_session(self):
        self.write([self.message('Edit', minutes=10)], name='writer')
        self.write([self.message(minutes=0)], name='reader')
        self.assertEqual(presence.last_claude_activity(self.project),
                         self.now - timedelta(minutes=10))
