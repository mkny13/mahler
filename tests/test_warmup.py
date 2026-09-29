from contextlib import closing
import copy
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch, Mock

from mahler import config, platforms, scheduler, usage, warmup
from mahler.ledger import Ledger, iso
from mahler.scheduler import Ctx

from local_timezone import local_timezone


class WarmupTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 28, 5, tzinfo=timezone.utc)
        self.led = Ledger(':memory:', clock=lambda: self.now)
        self.addCleanup(self.led.close)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.target = dict(name='personal', kind='claude', account='personal',
                           platform='claude-low', start='05:00', end='24:00', days='weekdays')
        self.cfg['warmup']['targets'] = [self.target]
        self.ctx = Ctx(self.cfg, self.led)
        self.read = self.enterContext(patch('mahler.warmup.read_usage', return_value=[('5h', 0, None)]))
        self.nudge = self.enterContext(patch('mahler.warmup.nudge'))
        # Start/end assertions below use local clock times, without changing TZ.
        self.now = self.now.replace(tzinfo=None).astimezone()

    def run_pass(self, **kwargs):
        warmup.warmup_pass(self.ctx, **kwargs)
        return self.ctx.lines[-1]

    def test_first_and_chained_window(self):
        self.run_pass()
        self.assertEqual(self.nudge.call_count, 1)
        self.led.record_usage('claude-low', '5h', 1, iso(self.now + timedelta(hours=5)))
        self.now += timedelta(hours=4, minutes=59)
        self.run_pass()
        self.assertEqual(self.nudge.call_count, 1)
        self.now += timedelta(minutes=1)
        self.run_pass()
        self.assertEqual(self.nudge.call_count, 2)

    def test_tick_relearn_uses_context_ledger(self):
        ctx = Ctx(self.cfg, self.led, dry_run=True)
        with patch('mahler.scheduler.config.enabled_projects', return_value=[]), \
                patch('mahler.scheduler.outbox.drain'), \
                patch('mahler.scheduler.compute_burst'), \
                patch('mahler.scheduler.watchdog'), \
                patch('mahler.scheduler.expire'), \
                patch('mahler.scheduler.close_finished_parents'), \
                patch('mahler.scheduler.refresh_usage'), \
                patch('mahler.scheduler.resets.spend_banked'), \
                patch('mahler.scheduler.warmup_pass'), \
                patch('mahler.scheduler.queue_maintenance'), \
                patch('mahler.scheduler.platform_audit.queue'), \
                patch('mahler.scheduler.schedule'), \
                patch('mahler.scheduler.ship'), \
                patch('mahler.scheduler.digest.maybe_send'), \
                patch('mahler.scheduler.janitor.maybe_run'), \
                patch('mahler.scheduler.relearn_due', return_value=True) as due, \
                patch('mahler.scheduler.relearn') as relearn, \
                patch.object(self.led, 'paused', return_value=False):
            scheduler.tick(ctx)

        due.assert_called_once_with(ctx.led)
        relearn.assert_called_once_with(ctx)

    def test_hours_weekends_and_off(self):
        for day, hour in [(28, 4), (27, 10), (29, 0)]:
            self.now = self.now.replace(day=day, hour=hour)
            self.assertIn('skipped-hours', self.run_pass())
        self.target['days'] = 'off'
        self.assertIn('skipped-disabled', self.run_pass(manual=True))
        self.nudge.assert_not_called()
        self.read.assert_not_called()

    def test_daily_and_manual(self):
        self.now = self.now.replace(day=27)
        self.target['days'] = 'daily'
        self.run_pass()
        self.nudge.assert_called_once()

    def test_active_avoids_even_free_read(self):
        self.led.record_usage('claude-low', '5h', 10, iso(self.now + timedelta(hours=1)))
        self.assertIn('skipped-active', self.run_pass())
        self.read.assert_not_called()
        self.nudge.assert_not_called()

    def test_free_read_detects_human_window(self):
        self.read.return_value = [('5h', 10, iso(self.now + timedelta(hours=5)))]
        self.assertIn('skipped-active', self.run_pass())
        self.nudge.assert_not_called()

    def test_zero_used_future_reset_is_idle(self):
        self.read.return_value = [('5h', 0, iso(self.now + timedelta(hours=5)))]
        self.run_pass()
        self.nudge.assert_called_once()

    def test_unknown_skips_without_spending(self):
        self.read.return_value = []
        self.assertIn('skipped-unknown', self.run_pass())
        self.nudge.assert_not_called()

    def test_pause_exhaustion_and_block(self):
        self.led.set_kv('paused', '1')
        self.assertIn('skipped-paused', self.run_pass())
        self.led.set_kv('paused', '')
        self.led.record_usage('claude-low', 'weekly', 100, iso(self.now + timedelta(days=1)))
        self.assertIn('skipped-exhausted', self.run_pass())
        self.led.clear_usage('claude-low', ['weekly'])
        self.read.return_value = platforms.CodexUsage([('5h', 0, None)], {'blocked': True})
        self.assertIn('skipped-exhausted', self.run_pass())
        self.nudge.assert_not_called()

    def test_four_hour_guard_and_restart(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'test.db')
            with closing(Ledger(path, clock=lambda: self.now)) as led:
                ctx = Ctx(self.cfg, led)
                warmup.warmup_pass(ctx)
            self.now += timedelta(hours=3)
            with closing(Ledger(path, clock=lambda: self.now)) as led:
                ctx = Ctx(self.cfg, led)
                warmup.warmup_pass(ctx)
                self.assertIn('skipped-guard', ctx.lines[-1])
            self.assertEqual(self.nudge.call_count, 1)

    def test_failure_backoff(self):
        self.nudge.side_effect = RuntimeError('private detail')
        self.assertIn('failed', self.run_pass())
        self.now += timedelta(minutes=29)
        self.assertIn('skipped-backoff', self.run_pass())
        self.now += timedelta(minutes=1)
        self.nudge.side_effect = None
        self.assertIn('fired', self.run_pass())
        self.assertEqual(self.nudge.call_count, 2)
        self.assertNotIn('private detail', str(self.led.q('SELECT * FROM events')))

    def test_per_target_exception_isolation(self):
        second = dict(self.target, name='second')
        self.cfg['warmup']['targets'].append(second)
        self.read.side_effect = [RuntimeError('bad'), [('5h', 0, None)], [('5h', 1, None)]]
        self.run_pass()
        self.nudge.assert_called_once_with(self.ctx, second)

    def test_dry_run_does_not_write(self):
        self.ctx.dry_run = True
        self.assertIn('would-fire', self.run_pass())
        self.nudge.assert_not_called()
        self.assertFalse(self.led.usage('claude-low'))
        self.assertFalse(self.led.q('SELECT * FROM events'))
        self.assertIsNone(self.led.get_kv('warmup:personal:last'))

    def test_failed_free_read_preserves_previous_fire_guard(self):
        self.run_pass()
        fired = self.led.get_kv('warmup:personal:last')
        self.now += timedelta(minutes=31)
        self.read.side_effect = OSError('offline')
        self.assertIn('failed', self.run_pass())
        self.assertEqual(self.led.get_kv('warmup:personal:last'), fired)
        self.now += timedelta(minutes=31)
        self.read.side_effect = None
        self.assertIn('skipped-guard', self.run_pass())
        self.assertEqual(self.nudge.call_count, 1)

    def test_peer_exhaustion_prevents_spending(self):
        self.led.record_usage('claude', 'weekly', 100, iso(self.now + timedelta(days=1)))
        self.assertIn('skipped-exhausted', self.run_pass())
        self.nudge.assert_not_called()
        self.read.assert_not_called()

    def test_end_exclusive_and_next_weekday(self):
        self.target['end'] = '20:00'
        self.now = self.now.replace(day=25, hour=20)  # Friday
        self.assertIn('skipped-hours', self.run_pass())
        expected = warmup.next_active(self.target, self.now)
        self.assertEqual((expected.day, expected.hour), (28, 5))

    def test_failed_nudge_backoff_survives_restart(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'test.db')
            self.nudge.side_effect = TimeoutError()
            with closing(Ledger(path, clock=lambda: self.now)) as led:
                warmup.warmup_pass(Ctx(self.cfg, led))
            self.now += timedelta(minutes=15)
            with closing(Ledger(path, clock=lambda: self.now)) as led:
                ctx = Ctx(self.cfg, led)
                warmup.warmup_pass(ctx)
                self.assertIn('skipped-backoff', ctx.lines[-1])
            self.assertEqual(self.nudge.call_count, 1)

    def test_account_environment(self):
        self.cfg['accounts']['work'] = {'env': {'CODEX_HOME': '/isolated/work'}}
        t = dict(self.target, kind='codex', account='work')
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'secret', 'GH_TOKEN': 'secret',
                                     'CLAUDE_CONFIG_DIR': '/personal', 'CODEX_HOME': '/personal'}):
            env = warmup.environment(self.cfg, t)
            self.assertEqual(env['CODEX_HOME'], '/isolated/work')
            for key in ('OPENAI_API_KEY', 'GH_TOKEN', 'CLAUDE_CONFIG_DIR'):
                self.assertNotIn(key, env)
            personal = warmup.environment(self.cfg, self.target)
            self.assertNotIn('CODEX_HOME', personal)
            self.assertNotIn('CLAUDE_CONFIG_DIR', personal)
        self.cfg['accounts']['work']['env'] = {}
        with self.assertRaises(ValueError):
            warmup.environment(self.cfg, t)


class LearnTimeTests(unittest.TestCase):
    """Every branch of the pure learned-warm-up rule (mahler#536)."""

    def test_exhaustion_rule(self):
        # W = F + E − 5h, inside the clamps: 600 + 120 − 300 = 420 (07:00)
        self.assertEqual(warmup.learn_time([(600, 120)] * 5, False), 420)

    def test_lower_clamp(self):
        # 300 + 10 − 300 = 10 < F − 4h30 → clamped up to 30 (00:30)
        self.assertEqual(warmup.learn_time([(300, 10)] * 5, False), 30)

    def test_upper_clamp(self):
        # 900 + 300 − 300 = 900 > F − 30min → clamped down to 870 (14:30)
        self.assertEqual(warmup.learn_time([(900, 300)] * 5, False), 870)

    def test_no_exhaustion_fallback(self):
        # W = F − 3h: 600 − 180 = 420
        self.assertEqual(warmup.learn_time([(600, None)] * 5, False), 420)

    def test_too_few_days_keeps_configured(self):
        self.assertIsNone(warmup.learn_time([(600, 120)] * 4, False))
        self.assertIsNone(warmup.learn_time([], False))

    def test_weekend_with_no_use_is_off(self):
        self.assertEqual(warmup.learn_time([], True), 'off')

    def test_rounding_to_15_minutes(self):
        # median candidate 422 → 420
        days = [(600, e) for e in (122, 124, 121, 122, 123)]
        self.assertEqual(warmup.learn_time(days, False), 420)

    def test_median_combines_days(self):
        # candidates 360, 420, 480, 420, 420 → median 420
        days = [(540, 120), (600, 120), (660, 120), (600, 120), (600, 120)]
        self.assertEqual(warmup.learn_time(days, False), 420)

    def test_days_without_use_are_ignored(self):
        self.assertEqual(warmup.learn_time([(600, 120)] * 5 + [(None, None)], False), 420)

    def test_hhmm(self):
        self.assertEqual(warmup.hhmm(370), '06:10')
        self.assertEqual(warmup.hhmm(0), '00:00')
        self.assertEqual(warmup.hhmm('off'), 'off')


class HumanRiseTests(unittest.TestCase):
    """Rise detection is per quota group, never our own nudges (D25, #536)."""

    def setUp(self):
        self.now = datetime(2026, 9, 28, 6, tzinfo=timezone.utc)
        self.led = Ledger(':memory:', clock=lambda: self.now)
        self.addCleanup(self.led.close)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg['platforms']['codex-work'] = dict(kind='codex', account='work',
                                                   quota_group='codex@work', model='m')
        self.cfg['accounts']['work'] = {'env': {'CODEX_HOME': '/work/codex'}}
        self.cfg['warmup']['targets'] = [dict(name='codexw', kind='codex', account='work',
                                              platform='codex-work', start='05:00',
                                              end='24:00', days='weekdays')]

    def events(self):
        return [(r['kind'], r['detail']) for r in self.led.q(
            "SELECT kind, detail FROM events WHERE project IS NULL AND "
            "kind IN ('human-use', 'human-out')")]

    def test_rise_sets_flag_and_event_per_group(self):
        usage.note_human_rise(self.cfg, self.led, 'codex-work', 15)
        self.assertTrue(self.led.get_kv('human:codex@work'))
        self.assertIn(('human-use', 'codex@work'), self.events())
        self.assertNotIn(('human-out', 'codex@work'), self.events())

    def test_exhaustion_event_at_90_pct(self):
        usage.note_human_rise(self.cfg, self.led, 'codex-work', 95)
        self.assertIn(('human-out', 'codex@work'), self.events())

    def test_suppressed_while_a_run_is_live(self):
        with patch.object(self.led, 'active_runs',
                          return_value=[{'platform': 'codex-work'}]):
            usage.note_human_rise(self.cfg, self.led, 'codex-work', 95)
        self.assertIsNone(self.led.get_kv('human:codex@work'))
        self.assertEqual(self.events(), [])

    def test_suppressed_after_our_own_warmup_nudge(self):
        self.led.set_kv('warmup:codexw:last', iso(self.now - timedelta(minutes=30)))
        usage.note_human_rise(self.cfg, self.led, 'codex-work', 15)
        self.assertIsNone(self.led.get_kv('human:codex@work'))
        self.assertEqual(self.events(), [])

    def test_no_cross_group_contamination(self):
        self.cfg['platforms']['claude-low'] = dict(kind='claude', account='personal',
                                                   quota_group='claude')
        usage.note_human_rise(self.cfg, self.led, 'claude-low', 15)
        self.assertTrue(self.led.get_kv('human:claude'))
        self.assertIsNone(self.led.get_kv('human:codex@work'))
        self.assertEqual([d for _, d in self.events()], ['claude'])

    def test_refresh_codex_detects_a_rise(self):
        self.led.record_usage('codex-work', '5h', 10, iso(self.now + timedelta(hours=5)))
        samples = platforms.CodexUsage(
            [('5h', 30, iso(self.now + timedelta(hours=5)))], {'blocked': False})
        with patch('mahler.platforms.probe_codex', return_value=samples), \
             patch('mahler.usage.codex_env', return_value={}):
            usage.refresh_codex(self.cfg, self.led, 'codex-work')
        self.assertTrue(self.led.get_kv('human:codex@work'))
        self.assertIn(('human-use', 'codex@work'), self.events())

    def test_agy_refresh_detects_a_rise(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg['platforms']['agy-gemini'] = dict(kind='agy', account='personal',
                                              quota_group='agy-gemini', pool='gemini')
        led = Ledger(':memory:', clock=lambda: self.now)
        self.addCleanup(led.close)
        led.record_usage('agy-gemini', '5h', 10, iso(self.now + timedelta(hours=5)))
        # first active_runs call puts the platform in `wanted` (as a live run
        # would); the second is note_human_rise's own suppression check
        runs = Mock(side_effect=[[{'platform': 'agy-gemini'}], []])
        with patch('mahler.platforms.probe_agy',
                   return_value={'gemini': [('5h', 40, iso(self.now + timedelta(hours=5)))]}), \
             patch.object(config, 'DEFAULT_ACCOUNT', 'personal'), \
             patch('mahler.usage.router.usage_state', return_value=('stale', None)), \
             patch.object(led, 'active_runs', runs):
            usage.refresh_usage(Ctx(cfg, led), [])
        self.assertTrue(led.get_kv('human:agy-gemini'))
        self.assertIn(('human-use', 'agy-gemini'),
                      [(r['kind'], r['detail']) for r in led.q(
                          "SELECT kind, detail FROM events WHERE project IS NULL")])


class TranscriptTests(unittest.TestCase):
    """Transcript scanning is read-only, per day, and never counts Mahler's
    own worktrees or nudge temp dirs."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.root = os.path.join(self.td.name, 'projects')
        os.makedirs(self.root)
        self.now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
        self.since = self.now - timedelta(days=10)
        self.excluded = ('/Users/mike/.mahler/worktrees', '/tmp/mahler-warmup')

    def write(self, name, lines):
        d = os.path.join(self.root, name)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, 's.jsonl')
        with open(path, 'w') as f:
            f.writelines(line + '\n' for line in lines)
        stamp = self.since.timestamp() + 3600
        os.utime(path, (stamp, stamp))

    def test_first_use_per_day(self):
        self.write('Users-mike-Mahler', [
            '{"type":"user","cwd":"/Users/mike/Mahler","timestamp":"2026-09-25T09:10:00Z"}',
            '{"type":"user","cwd":"/Users/mike/Mahler","timestamp":"2026-09-25T15:00:00Z"}'])
        with local_timezone('UTC'):
            out = warmup.transcript_first_use(self.root, self.since, self.now,
                                              excluded=self.excluded)
        self.assertEqual(out, {date(2026, 9, 25): datetime(2026, 9, 25, 9, 10,
                                                           tzinfo=timezone.utc)})

    def test_mahler_worktree_and_tempdir_lines_are_excluded(self):
        self.write('Users-mike-.mahler-worktrees-mahler-536-run10059', [
            '{"cwd":"/Users/mike/.mahler/worktrees/mahler/536-run10059",'
            '"timestamp":"2026-09-25T07:00:00Z"}'])
        self.write('private-tmp-xyz', [
            '{"cwd":"/tmp/mahler-warmup-abc","timestamp":"2026-09-25T07:30:00Z"}'])
        self.write('Users-mike-Mahler', [
            '{"type":"user","cwd":"/Users/mike/Mahler",'
            '"timestamp":"2026-09-25T09:10:00Z"}'])
        with local_timezone('UTC'):
            out = warmup.transcript_first_use(self.root, self.since, self.now,
                                              excluded=self.excluded)
        self.assertEqual(list(out), [date(2026, 9, 25)])
        self.assertEqual(out[date(2026, 9, 25)].hour, 9)

    def test_worktree_transcript_excluded_whole(self):
        # A worktree transcript's queue-operation records carry no cwd, so the
        # file must be dropped as a whole, not line by line.
        self.write('Users-mike-.mahler-worktrees-mahler-536-run10061', [
            '{"cwd":"/Users/mike/.mahler/worktrees/mahler/536-run10061",'
            '"timestamp":"2026-09-25T07:00:00Z"}',
            '{"timestamp":"2026-09-25T06:00:00Z"}'])
        self.write('Users-mike-Mahler', [
            '{"type":"user","cwd":"/Users/mike/Mahler",'
            '"timestamp":"2026-09-25T09:10:00Z"}'])
        with local_timezone('UTC'):
            out = warmup.transcript_first_use(self.root, self.since, self.now,
                                              excluded=self.excluded)
        self.assertEqual(out, {date(2026, 9, 25): datetime(2026, 9, 25, 9, 10,
                                                           tzinfo=timezone.utc)})

    def test_only_user_message_timestamps_count(self):
        # Outside worktrees, non-user records without cwd (queue operations)
        # must not count either: only user messages say when Mike started.
        self.write('Users-mike-Mahler', [
            '{"timestamp":"2026-09-25T06:00:00Z"}',
            '{"type":"assistant","cwd":"/Users/mike/Mahler",'
            '"timestamp":"2026-09-25T08:00:00Z"}',
            '{"type":"user","cwd":"/Users/mike/Mahler",'
            '"timestamp":"2026-09-25T09:10:00Z"}'])
        self.write('Users-mike-.codex-sessions', [
            '{"timestamp":"2026-09-25T07:00:00Z","type":"response_item",'
            '"payload":{"type":"message","role":"assistant","content":[]}}',
            '{"timestamp":"2026-09-25T07:30:00Z","type":"response_item",'
            '"payload":{"type":"message","role":"user","content":[]}}'])
        with local_timezone('UTC'):
            out = warmup.transcript_first_use(self.root, self.since, self.now,
                                              excluded=self.excluded)
        self.assertEqual(out, {date(2026, 9, 25): datetime(2026, 9, 25, 7, 30,
                                                           tzinfo=timezone.utc)})

    def test_stale_files_are_skipped(self):
        d = os.path.join(self.root, 'Users-mike-Mahler')
        os.makedirs(d)
        path = os.path.join(d, 'old.jsonl')
        with open(path, 'w') as f:
            f.write('{"timestamp":"2026-09-20T09:00:00Z"}\n')
        stamp = (self.since - timedelta(days=1)).timestamp()
        os.utime(path, (stamp, stamp))
        self.assertEqual(warmup.transcript_first_use(
            self.root, self.since, self.now, excluded=self.excluded), {})


class LearnCollectTests(unittest.TestCase):
    """collect_days merges transcript and usage signals into (F, E) days; the
    relearn pass persists report-only kv and gates itself to once per day."""

    def setUp(self):
        self.now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
        self.led = Ledger(':memory:', clock=lambda: self.now)
        self.addCleanup(self.led.close)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg['platforms']['claude-low'] = dict(kind='claude', account='personal',
                                                   quota_group='claude')
        self.target = dict(name='claude', kind='claude', account='personal',
                           platform='claude-low', start='05:00', end='24:00',
                           days='weekdays')
        self.cfg['warmup']['targets'] = [self.target]
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)

    def test_collect_days_merges_transcript_and_events(self):
        root = os.path.join(self.td.name, 'projects')
        os.makedirs(root)
        path = os.path.join(root, 's.jsonl')
        with open(path, 'w') as f:
            f.write('{"type":"user","cwd":"/Users/mike/Mahler",'
                    '"timestamp":"2026-09-25T09:10:00Z"}\n')
        os.utime(path, (self.now.timestamp() - 3600,) * 2)
        self.now = datetime(2026, 9, 25, 11, 10, tzinfo=timezone.utc)
        self.led.event('human-out', detail='claude')
        with patch('mahler.warmup.transcript_roots', return_value=[root]), \
             local_timezone('UTC'):
            days = warmup.collect_days(self.cfg, self.led, self.target,
                                       self.now - timedelta(days=10), self.now)
        self.assertEqual(days, {date(2026, 9, 25): (550, 120)})

    def test_collect_days_ignores_events_outside_window(self):
        # Ledger events outside the 30-day window must age out of the report:
        # an old human-use stays out, and so does one after `now`.
        root = os.path.join(self.td.name, 'projects')
        os.makedirs(root)
        self.now = datetime(2026, 9, 25, 11, 10, tzinfo=timezone.utc)
        self.led.event('human-use', detail='claude')            # inside the window
        self.now = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)
        self.led.event('human-use', detail='claude')            # older than the window
        self.now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
        self.led.event('human-use', detail='claude')            # after the report's now
        with patch('mahler.warmup.transcript_roots', return_value=[root]), \
             local_timezone('UTC'):
            days = warmup.collect_days(self.cfg, self.led, self.target,
                                       datetime(2026, 9, 15, tzinfo=timezone.utc),
                                       datetime(2026, 9, 26, tzinfo=timezone.utc))
        self.assertEqual(list(days), [date(2026, 9, 25)])
        self.assertEqual(days[date(2026, 9, 25)], (670, None))

    def test_relearn_persists_kv_and_gates_daily(self):
        days = {date(2026, 9, 25): (550, 120), date(2026, 9, 24): (560, 130),
                date(2026, 9, 23): (545, 110), date(2026, 9, 22): (555, None),
                date(2026, 9, 21): (550, None)}
        self.now = datetime(2026, 9, 28, 4, tzinfo=timezone.utc)
        ctx = Ctx(self.cfg, self.led)
        with patch('mahler.warmup.collect_days', return_value=days), \
             local_timezone('UTC'):
            self.assertTrue(warmup.relearn_due(self.led))
            warmup.relearn(ctx)
            payload = json.loads(self.led.get_kv('warmup:learned:claude:weekday'))
            # exhaustion median E=120, F median 550 → 550+120-300 = 370 → 375 (06:15)
            self.assertEqual(payload['learned'], 375)
            self.assertEqual(payload['n'], 5)
            self.assertEqual(self.led.get_kv('warmup:learned:at'), '2026-09-28')
            self.assertFalse(warmup.relearn_due(self.led))

    def test_relearn_dry_run_writes_nothing(self):
        self.now = datetime(2026, 9, 28, 4, tzinfo=timezone.utc)
        ctx = Ctx(self.cfg, self.led, dry_run=True)
        with patch('mahler.warmup.collect_days', return_value={}), \
             local_timezone('UTC'):
            warmup.relearn(ctx)
        self.assertIsNone(self.led.get_kv('warmup:learned:at'))
        self.assertIsNone(self.led.get_kv('warmup:learned:claude:weekday'))

    def test_relearn_waits_for_03_00_local(self):
        self.now = datetime(2026, 9, 28, 2, tzinfo=timezone.utc)
        with local_timezone('UTC'):
            self.assertFalse(warmup.relearn_due(self.led))

    def test_learned_lines_fallback_and_learned(self):
        self.now = datetime(2026, 9, 28, 4, tzinfo=timezone.utc)
        with patch('mahler.warmup.collect_days', return_value={}), \
             local_timezone('UTC'):
            lines = warmup.learned_lines(self.cfg, self.led)
        self.assertEqual(lines, [
            'claude weekdays: not enough data yet (0 of 5 days with use) — keeping 05:00',
            'claude weekends: no use seen — warm-up would be off'])
        self.led.set_kv('warmup:learned:claude:weekday',
                        json.dumps({'learned': 370, 'F': 550, 'E': 120, 'n': 5}))
        lines = warmup.console_lines(self.cfg, self.led)
        self.assertIn('claude weekdays: learned warm-up 06:10', lines[0])
        self.assertIn('(5 days)', lines[0])
        self.assertIn('no learned warm-up yet', lines[1])


class WarmupConfigTests(unittest.TestCase):
    def test_defaults_and_validation(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'config.toml')
            cfg = config.load(path)
            self.assertEqual(len(cfg['warmup']['targets']), 1)
            with open(path, 'w') as f:
                f.write('[[warmup.targets]]\nname="bad"\nkind="claude"\naccount="typo"\nplatform="claude"\n')
            with self.assertRaisesRegex(ValueError, 'unknown account'):
                config.load(path)

    def test_invalid_hours_and_mismatched_account(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        t = dict(name='test', kind='claude', account='personal', platform='claude',
                 start='25:00', end='24:00')
        cfg['warmup']['targets'] = [t]
        with self.assertRaisesRegex(ValueError, 'start < end'):
            config.configure_warmup(cfg, cfg)
        t.update(start='05:00', account='work')
        cfg['accounts']['work'] = {}
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            config.configure_warmup(cfg, cfg)


class WarmupAdapterTests(unittest.TestCase):
    def setUp(self):
        self.led = Ledger(':memory:')
        self.addCleanup(self.led.close)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg['accounts']['work'] = {'env': {'CODEX_HOME': '/work/codex',
                                                'CLAUDE_CONFIG_DIR': '/work/claude'}}
        self.cfg['platforms']['work-low'] = dict(kind='codex', account='work',
                                                model='test-model', quota_group='codex@work')
        self.ctx = Ctx(self.cfg, self.led)
        self.target = dict(name='work', kind='codex', account='work', platform='work-low')

    @patch('mahler.platforms.codex_exe', return_value='/bin/codex')
    @patch('mahler.warmup.subprocess.run')
    def test_codex_command_timeout_and_environment(self, run, exe):
        run.return_value = Mock(returncode=0, stdout='{"type":"turn.completed"}\n')
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'secret', 'GH_TOKEN': 'secret'}):
            warmup.nudge(self.ctx, self.target)
        args, kwargs = run.call_args
        self.assertIn('--ephemeral', args[0])
        self.assertIn('test-model', args[0])
        self.assertIn('read-only', args[0])
        self.assertIn('model_reasoning_effort="low"', args[0])
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', args[0])
        self.assertEqual(kwargs['timeout'], 90)
        self.assertEqual(kwargs['env']['CODEX_HOME'], '/work/codex')
        self.assertNotIn('GH_TOKEN', kwargs['env'])
        self.assertNotIn('OPENAI_API_KEY', kwargs['env'])
        self.assertFalse(os.path.exists(kwargs['cwd']))

    @patch('mahler.platforms.agy_exe', return_value='/bin/agy')
    @patch('mahler.warmup.subprocess.run')
    def test_agy_failed_status_is_not_success(self, run, exe):
        run.return_value = Mock(returncode=0, stdout='{"status":"ERROR"}')
        target = dict(name='agy', kind='agy', account='personal', platform='agy-gemini')
        with self.assertRaises(RuntimeError):
            warmup.nudge(self.ctx, target)
        self.assertEqual(run.call_args.kwargs['timeout'], 90)

    @patch('mahler.platforms.probe_codex')
    def test_free_codex_reader_uses_work_login(self, probe):
        warmup.read_usage(self.ctx, self.target)
        self.assertEqual(probe.call_args.kwargs['env']['CODEX_HOME'], '/work/codex')


if __name__ == '__main__':
    unittest.main()
