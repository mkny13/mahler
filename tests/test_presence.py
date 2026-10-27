"""Presence fixtures are synthetic and never inspect the user's Claude history."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler.ledger import Ledger
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
        self.led = Ledger(":memory:")
        self.addCleanup(self.led.close)
        presence._ACTIVITY_CACHE.clear()
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
                holds = state._hot_holds(self.led, [{'name': 'mahler', 'path': self.project,
                                                'hot_hold': True, 'hot_hold_minutes': 20}], self.now)
                self.assertEqual(holds[0]['directory'], self.cwd)
                self.assertIn('ops-session (last edit 3m ago)', state._hot_hold_text(holds[0]))

    def test_routine_metadata_preserves_read_only_and_edit_signals(self):
        for kind in ('agent-name', 'ai-title', 'atis-latch', 'attachment',
                     'bridge-session', 'cost-state', 'custom-title',
                     'file-history-delta', 'file-history-snapshot', 'last-prompt',
                     'mode', 'permission-mode', 'pr-link', 'progress',
                     'queue-operation', 'relocated', 'summary', 'system',
                     'worktree-state'):
            metadata = {'type': kind, 'timestamp': self.now.isoformat()}
            for tool, minutes in [('Read', 0), ('Edit', 3), ('Edit', 30)]:
                with self.subTest(kind=kind, tool=tool, minutes=minutes):
                    presence._ACTIVITY_CACHE.clear()  # same size and mtime each pass
                    self.write([metadata, self.message(tool, minutes=minutes),
                                self.message('Bash', command='mahler status'),
                                metadata])
                    activity = presence.last_claude_edit(self.project)
                    if tool == 'Read':
                        self.assertIsNone(activity)
                    else:
                        self.assertFalse(activity.fallback)
                        self.assertEqual(activity.at,
                                         self.now - timedelta(minutes=minutes))
                    self.assertEqual(self.active(), tool == 'Edit' and minutes < 20)
                    self.assertFalse(self.active(2))

    def test_metadata_does_not_disable_unknown_transcript_fallback(self):
        for rows in [[{'type': 'custom-title'}],
                     [self.message('Read'), {'type': 'future-record'}]]:
            with self.subTest(rows=rows):
                self.write(rows)
                self.assertTrue(self.active())
                self.assertTrue(presence.last_claude_edit(self.project).fallback)

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

    def test_console_names_unexempt_writer_and_uses_its_edit_age(self):
        self.led.now = lambda: self.now
        self.write([self.message('Edit', minutes=1)], name='ended')
        self.led.set_kv(presence.HOT_HOLD_END_PREFIX + 'mahler',
                        presence.end_marker(self.led, 'mahler', self.project))
        row = self.message('Edit', minutes=5)
        row['cwd'] = self.project + '/new-writer'
        self.write([row], name='new')
        window = timedelta(minutes=20)
        self.assertEqual(presence.hot_hold_activity(
            self.led, 'mahler', self.project, window), self.now - timedelta(minutes=5))
        holds = state._hot_holds(self.led, [{'name': 'mahler', 'path': self.project,
                                'hot_hold': True, 'hot_hold_minutes': 20}], self.now)
        self.assertEqual(holds[0]['directory'], row['cwd'])
        self.assertEqual(holds[0]['ago'], 5)

    def test_read_only_heredoc_with_quotes_does_not_hold(self):
        commands = [
            "python3 - <<'EOF'\nimport sys\n# It's an ops query\nprint(\"Don't hold\")\nEOF",
            "cat <<'EOF'\nPart of issue #292. Mike's feedback was that we shouldn't hold.\nEOF",
            "cat <<-EOF\n\t\tDon't fail on tabbed heredoc\n\t\tEOF",
            'cat << "EOF"\nHere is a double-quoted delimiter with Mike\'s apostrophe\nEOF',
            "cat <<\\EOF\nBackslash delimiter: Mike's bug\nEOF",
            'mahler add mahler "title" --body "$(cat <<\'EOF\'\nMike\'s bug\nEOF\n)"',
            "diff -u <(cat <<EOF1) <(cat <<EOF2)\nMike's 1\nEOF1\nMike's 2\nEOF2",
            'echo "<<EOF"\n# Not a heredoc: git commit in quotes should not hold',
        ]
        for command in commands:
            with self.subTest(command=command):
                self.write([self.message('Bash', command=command)])
                self.assertIsNone(presence.last_claude_activity(self.project))
                self.assertFalse(self.active())

    def test_git_writes_with_heredocs_hold(self):
        commands = [
            "git commit -F - <<'EOF'\nFix Mike's bug\nEOF",
            'git commit -m "$(cat <<\'EOF\'\nFix Mike\'s bug\nEOF\n)"',
            "python3 - <<'EOF'\nprint('test')\nEOF\ngit push origin HEAD",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.write([self.message('Bash', command=command)])
                self.assertTrue(self.active())
                self.assertFalse(presence.last_claude_edit(self.project).fallback)

    def test_unclosed_heredoc_retains_fallback(self):
        self.write([self.message('Bash', command="cat <<'EOF'\nmissing delimiter")])
        self.assertTrue(self.active())
        self.assertTrue(presence.last_claude_edit(self.project).fallback)

    def test_transcript_read_is_memoised_until_it_grows(self):
        self.write([self.message('Edit')])
        with mock.patch.object(presence, '_transcript_activity',
                               wraps=presence._transcript_activity) as read:
            first = presence.last_claude_edit(self.project)
            self.assertEqual(presence.last_claude_edit(self.project), first)
            self.assertEqual(read.call_count, 1)
            self.write([self.message('Edit'), self.message('Edit', minutes=1)])
            presence.last_claude_edit(self.project)
            self.assertEqual(read.call_count, 2)

    def test_hash_in_arguments_does_not_truncate_command(self):
        self.write([self.message('Bash', command='mahler ship mahler#625')])
        self.assertIsNone(presence.last_claude_activity(self.project))
        self.assertFalse(self.active())

