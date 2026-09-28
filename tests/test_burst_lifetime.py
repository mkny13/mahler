"""Account presence and durable launch allowances (mahler#544)."""
import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mahler import config, presence, runner, usage, watchdog
from mahler.ledger import Ledger, iso
from test_accounts import NOW, work_cfg


class BurstLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = work_cfg()
        self.cfg['claude_peak']['enabled'] = False
        self.cfg['accounts']['work']['env']['CLAUDE_CONFIG_DIR'] = str(self.root / 'work')
        self.personal = self.root / 'personal' / 'projects'
        self.patch = patch.object(presence, 'CLAUDE_PROJECTS', str(self.personal))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.led = Ledger(':memory:', clock=lambda: NOW)
        self.addCleanup(self.led.close)
        self.pol = config.project_policy(self.cfg, 'x')
        self.ctx = SimpleNamespace(cfg=self.cfg, led=self.led, burst_lines=None,
                                   hot_hold=True, say=Mock(), ping=Mock(), policy=lambda p: self.pol)
        self.projects = [{'path': str(self.root / 'repo'), 'hot_hold_minutes': 5}]
        for name in ('claude', 'claude-work'):
            self.seed(name, 9, 66)

    def seed(self, name, session, weekly):
        for window, pct in [('5h', session), ('weekly', weekly)]:
            self.led.record_usage(name, window, pct, iso(NOW + timedelta(hours=2)))

    def transcript(self, root, path=None, age=0):
        directory = root / presence.encode(path or self.projects[0]['path'])
        directory.mkdir(parents=True, exist_ok=True)
        transcript = directory / 'session.jsonl'
        transcript.write_text('{}')
        import time
        stamp = time.time() - age * 60
        os.utime(transcript, (stamp, stamp))

    def test_transcripts_defer_only_their_login(self):
        for group, root in [('claude', self.personal),
                            ('claude@work', self.root / 'work' / 'projects')]:
            with self.subTest(group=group):
                self.transcript(root)
                self.assertEqual(presence.human_claude_groups(self.cfg, self.projects), {group})
                self.ctx.burst_lines = None
                lines = usage.compute_burst(self.ctx, self.projects)
                self.assertEqual(set(lines), {'claude', 'claude@work'} - {group})
                self.assertIn(group, self.ctx.say.call_args_list[-2].args[0])
                self.transcript(root, age=6)
                self.assertEqual(presence.human_claude_groups(self.cfg, self.projects), set())
        self.transcript(self.personal, self.root / '.mahler' / 'worktrees' / 'repo')
        self.assertEqual(presence.human_claude_groups(self.cfg, self.projects), set())

    def launch(self, lines):
        self.ctx.burst_lines = lines
        rid = self.led.create_run(project='x', number=1, role='build', platform='claude-work', epoch=1)
        prep = dict(worktree=str(self.root), run_dir=str(self.root), branch='test', base_ref='main')
        with patch.object(runner.platforms, 'argv_for', return_value=['claude']), \
                patch.object(runner, 'run_env', return_value={}), patch.object(runner, 'spawn', return_value=123):
            meta = runner.launch(self.ctx, 'x', {'number': 1}, 'build', 'claude-work', rid, 1, 'prompt', prep)
        self.led.update_run(rid, **meta)
        Path(meta['log_path']).write_text('{}\n')
        return rid

    def health(self, rid):
        return watchdog._health(self.ctx, self.led.run(rid), self.pol, NOW)

    def test_launch_limits_survive_burst_ending_and_still_stop_at_hard_line(self):
        self.transcript(self.personal)
        lines = usage.compute_burst(self.ctx, self.projects)
        self.cfg['platforms']['claude-work'].update(soft={'5h': 0, 'weekly': 0},
                                                   hard={'5h': 0, 'weekly': 0})
        rid = self.launch(lines)
        self.assertEqual(json.loads(self.led.run(rid)['burst_lines']), {'5h': [90, 97], 'weekly': [90, 97]})
        self.assertIsNone(self.health(rid))  # today's 9% / 66% case
        self.ctx.burst_lines = None
        self.assertIsNone(self.health(rid))
        for window in ('5h', 'weekly'):
            self.seed('claude-work', 9, 66)
            self.led.record_usage('claude-work', window, 97, iso(NOW + timedelta(hours=2)))
            self.assertEqual(self.health(rid), 'quota')
        self.seed('claude-work', 9, 66)
        for info in ({'isUsingOverage': True}, {'status': 'rejected'}):
            Path(self.led.run(rid)['log_path']).write_text(json.dumps(
                {'type': 'rate_limit_event', 'rate_limit_info': info}))
            self.assertEqual(self.health(rid), 'quota')

    def test_session_stamp_preserves_weekly_line_and_current_higher_limits(self):
        rid = self.launch({'claude@work': {'5h': (90, 97)}})
        self.ctx.burst_lines = None
        self.seed('claude-work', 80, 80)
        self.assertEqual(self.health(rid), 'quota')
        self.ctx.burst_lines = {'claude@work': {'5h': (90, 97), 'weekly': (90, 97)}}
        self.assertIsNone(self.health(rid))

    def test_ordinary_launch_has_no_stamp(self):
        rid = self.launch(None)
        self.assertIsNone(self.led.run(rid)['burst_lines'])
