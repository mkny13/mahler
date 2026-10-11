import console_snapshot
"""Models state from synthetic run history."""
import json
import unittest
from datetime import datetime, timedelta, timezone

from mahler.console import state
from mahler.ledger import Ledger, iso


class ModelsTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
        self.led = Ledger(':memory:', clock=lambda: self.now)
        self.addCleanup(self.led.close)

    def test_models_copy_tone_and_drilldown_for_every_size(self):
        for number, size in enumerate((None, '', 's'), 1):
            self.led.upsert_item('p', number)
            self.led.create_run(project='p', number=number, role='build',
                                size=size, platform='slot', model='Luna',
                                effort='low', epoch=1, status='ended',
                                ended_at=iso(self.now), outcome='DONE',
                                exit_code=0, cost_usd=.04, actual_mins=6)
        data = state.models(self.led, {})
        self.assertEqual(state.MODELS_LABEL, 'Models')
        self.assertEqual([g['title'] for g in data['groups']],
                         ['Build · unknown size', 'Build · unknown size', 'Build · s'])
        for group in data['groups']:
            row, = group['rows']
            self.assertIn('Luna · low', row['text'])
            self.assertIn('1 of 1 done first try', row['text'])
            self.assertIn('~$0.04 each · $0.04 per success · 6 min', row['text'])
            self.assertIn('unproven', row['text'])
            self.assertEqual(row['tone'], 'mut')
            self.assertEqual(row['details_label'], 'Run outcomes')
            self.assertIn('success: DONE with no adverse evidence', row['details'][0])
        self.assertIn('Pending and excluded runs do not count.', data['note'])

    def test_empty_state(self):
        data = state.models(self.led, {})
        self.assertEqual(data['groups'], [])
        self.assertEqual(data['empty'], 'No attempts in this window.')

    def test_large_history_has_bounded_outcomes_and_fragment(self):
        from mahler.console import page
        from mahler.scorecard import table
        from test_console import make_cfg

        for number in range(1, 122):
            self.led.upsert_item('p', number)
            self.led.create_run(project='p', number=number, role='build',
                                size='s', platform='slot', model='Luna',
                                effort='low', epoch=1, status='ended',
                                ended_at=iso(self.now), outcome='DONE', exit_code=0)
        cfg = make_cfg()
        row = state.models(self.led, cfg)['groups'][0]['rows'][0]
        self.assertEqual(len(row['details']), 21)
        self.assertTrue(row['details'][0].startswith('Run 121 ·'))
        self.assertTrue(row['details'][19].startswith('Run 102 ·'))
        self.assertEqual(row['details'][20], '…and 101 earlier runs')
        self.assertIn('121 of 121 done first try', row['text'])
        self.assertEqual(len(table(self.led, cfg)[0]['attempts']), 121)
        fragment = console_snapshot.app(state.build(cfg, self.led))
        self.assertEqual(fragment.count('<p>Run '), 40)  # 20 per layout
        self.assertEqual(fragment.count('…and 101 earlier runs'), 2)
        self.assertLess(len(fragment.encode()), 300_000)

    def test_refresh_skips_identical_fragments_but_clears_submitted_drafts(self):
        import shutil
        import subprocess
        from mahler.console import page
        if not shutil.which('node'):
            self.skipTest('Node is needed for browser regression')
        script = page.JS[page.JS.index('  var lastFragment'):page.JS.index('  // refresh() swaps')]
        script += r'''
const assert = require('assert');
let swaps = 0, applies = 0, revisions = 0, html = 'first';
let app = {querySelector: () => null, querySelectorAll: () => [], set innerHTML(v) { swaps++; }};
let document = {hidden: false, activeElement: null, getElementById: () => null};
let settingsDirty = false, errorToastTimer = null, suppressKeep = [];
let window = {console};
function statsUrl() { return '/fragment'; }
function apply() { applies++; }
function noteRevision() { revisions++; }
function fetch() { return Promise.resolve({ok: true, text: () => Promise.resolve(html)}); }
(async () => {
  await refresh();
  await refresh();
  await refresh(true);
  assert.deepStrictEqual([swaps, applies, revisions], [1, 2, 1]);
  html = 'changed';
  await refresh();
  assert.deepStrictEqual([swaps, applies, revisions], [2, 3, 2]);
  suppressKeep = ['submitted'];
  await refresh(true);
  assert.deepStrictEqual([swaps, applies, revisions], [3, 4, 3]);
})().catch(err => { console.error(err); process.exit(1); });
'''
        subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)


class RecentProblemsTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
        self.led = Ledger(':memory:', clock=lambda: self.now)
        self.addCleanup(self.led.close)

    def test_window_kinds_and_descriptions(self):
        from datetime import timedelta
        for kind in state.PROBLEM_KINDS:
            self.led.event(kind, detail={'message': 'something failed'})
        self.led.event('console_ok')
        for hours in (24, 25, -1):
            self.led.con.execute('INSERT INTO events(at, kind, detail) VALUES(?,?,?)',
                                (iso(self.now - timedelta(hours=hours)), 'console_client_error', str(hours)))
        rows = state._recent_problems({}, self.led, self.now)
        self.assertEqual(len(rows), 4)
        self.assertIn('24', [r['text'] for r in rows])
        self.assertNotIn('25', [r['text'] for r in rows])
        self.assertNotIn('-1', [r['text'] for r in rows])
        self.assertEqual({r['kind'] for r in rows}, set(state.PROBLEM_KINDS))
        self.assertIn('message=something failed', [r['text'] for r in rows])

    def test_empty_bounded_and_newest_first(self):
        self.assertEqual(state._recent_problems({}, self.led, self.now), [])
        for i in range(12):
            self.led.event('console_action_failed', detail=f'{i}\n' + 'x' * 400)
        rows = state._recent_problems({}, self.led, self.now)
        self.assertEqual(len(rows), state.RECENT_PROBLEMS_SHOWN)
        self.assertTrue(rows[0]['text'].startswith('11 '))
        self.assertTrue(all(len(r['text']) <= 200 and '\n' not in r['text'] for r in rows))


class ScorecardCacheTests(unittest.TestCase):
    """mahler#532: the scorecard rescans all history, so pages that don't show
    it skip it and the rest reuse it until a run changes or it goes stale."""

    def setUp(self):
        from test_console import make_cfg
        self.now = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
        self.led = Ledger(':memory:', clock=lambda: self.now)
        self.addCleanup(self.led.close)
        self.cfg = make_cfg()

    def table(self):
        from unittest import mock
        from mahler import scorecard
        return mock.patch.object(scorecard, 'table', wraps=scorecard.table)

    def test_views_without_measurements_never_build_it(self):
        with self.table() as table:
            for section in ('now', 'needs', 'test', 'backlog', 'triage', ''):
                s = state.build(self.cfg, self.led, section=section)
                self.assertIsNone(s['models'])
                self.assertEqual(s['measured_routes'], [])
        table.assert_not_called()

    def test_models_reuses_rows_until_a_run_changes_or_ttl(self):
        from datetime import timedelta
        with self.table() as table:
            state.build(self.cfg, self.led, section='models')
            state.build(self.cfg, self.led, section='capacity')
            self.assertEqual(table.call_count, 1)
            self.led.upsert_item('p', 1)
            self.led.create_run(project='p', number=1, role='build', platform='slot',
                                epoch=1, status='ended', ended_at=iso(self.now), outcome='DONE')
            state.build(self.cfg, self.led, section='models')
            self.assertEqual(table.call_count, 2)
            self.now += timedelta(seconds=state.SCORECARD_TTL + 1)
            state.build(self.cfg, self.led, section='models')
            self.assertEqual(table.call_count, 3)

    def test_full_state_still_includes_measurements(self):
        s = state.build(self.cfg, self.led)
        self.assertIsNotNone(s['models'])

    def test_separate_ledgers_do_not_share_a_cache(self):
        other = Ledger(':memory:', clock=lambda: self.now)
        self.addCleanup(other.close)
        with self.table() as table:
            state.build(self.cfg, self.led, section='models')
            state.build(self.cfg, other, section='models')
        self.assertEqual(table.call_count, 2)


class WeeklyQuotaTests(unittest.TestCase):
    def setUp(self):
        from local_timezone import local_timezone
        self.tz = local_timezone('America/New_York')
        self.tz.__enter__()
        self.addCleanup(self.tz.__exit__, None, None, None)
        self.now = datetime(2026, 9, 23, 16, tzinfo=timezone.utc)
        self.led = Ledger(':memory:', clock=lambda: self.now)
        self.addCleanup(self.led.close)
        self.cfg = {'platforms': {}, 'routing': {'build': []}}

    def pool(self, name, reset=None, window='weekly', **conf):
        self.cfg['platforms'][name] = {'windows': [window], **conf}
        self.cfg['routing']['build'].append(name)
        if reset is not None:
            self.led.record_usage(name, window, 42, reset)

    def view(self):
        return state.week_calendar(self.cfg, self.led, self.now)

    def test_week_boundary_grouping_and_routing_order(self):
        self.pool('sat', '2026-09-27T00:00:00Z')
        self.pool('z-first', '2026-09-27T17:40:00Z')
        self.pool('a-second', '2026-09-27T17:40:00Z')
        self.pool('earlier', '2026-09-27T16:00:00Z')
        v = self.view()
        self.assertEqual(v['lines'], [
            {'day': 'Sun', 'time': '12:00', 'names': ['earlier']},
            {'day': 'Sun', 'time': '13:40', 'names': ['z-first', 'a-second']},
            {'day': 'Sat', 'time': '20:00', 'names': ['sat']}])
        for key in ('days', 'hours', 'markers', 'summary', 'legend'):
            self.assertNotIn(key, v)

    def test_past_weekly_reset_rolls_to_next_occurrence(self):
        self.pool('past-sunday', '2026-09-20T17:40:00Z')
        past = self.view()['lines']
        self.led.record_usage('past-sunday', 'weekly', 42, '2026-09-27T17:40:00Z')
        self.assertEqual(past, self.view()['lines'])
        self.assertEqual(past, [{'day': 'Sun', 'time': '13:40', 'names': ['past-sunday']}])

    def test_unknown_unmetered_and_monthly(self):
        self.pool('unknown')
        self.pool('malformed', 'bad timestamp')
        self.pool('free', metered=False)
        self.pool('short-window', window='5h')
        self.pool('monthly', '2026-09-24T01:30:00Z', 'monthly')
        self.pool('next-month', '2026-10-01T00:00:00Z', 'monthly')
        self.pool('past-monthly', '2026-09-21T00:00:00Z', 'monthly')
        v = self.view()
        self.assertEqual(v['unknown'], ['unknown', 'malformed'])
        self.assertEqual(v['unmetered'], ['free', 'short-window'])
        self.assertEqual(v['lines'], [{'day': 'Wed', 'time': '21:30', 'names': ['monthly']}])
        self.assertEqual(v['outside'], ['next-month · monthly · Wed 30 Sep 20:00'])

    def test_latest_non_routed_alias_reading_counts_shared_group_once(self):
        self.pool('work-low', '2026-09-21T00:00:00Z', quota_group='work')
        self.pool('work-medium', quota_group='work')
        self.cfg['platforms']['work-alias'] = {'quota_group': 'work'}
        self.now += timedelta(seconds=1)
        self.led.record_usage('work-alias', 'weekly', 42, '2026-09-24T01:30:00Z')
        self.assertEqual(self.view()['lines'], [
            {'day': 'Wed', 'time': '21:30', 'names': ['work']}])

    def test_empty(self):
        self.assertEqual(self.view()['lines'], [])

    def test_render_layouts_escape_names_and_scope_computation(self):
        from mahler.console import page
        from test_console import make_cfg
        self.pool('Account <one>', '2026-09-24T01:30:00Z')
        self.pool('unknown')
        self.pool('free', metered=False)
        self.pool('later', '2026-10-01T00:00:00Z', 'monthly')
        snapshot = state.build(make_cfg(), self.led)
        snapshot['weekly_quota'] = self.view()
        desktop = page.app(snapshot, view='capacity')
        phone = page.app(snapshot, layout='phone', tab='browse')
        rendered = page._weekly_quota(snapshot)
        self.assertIn('data-capacity-mode="weekly"', desktop)
        for html in (desktop, phone):
            self.assertIn(rendered, html)
            self.assertIn('Wed 21:30</span> · Account &lt;one&gt;', html)
            self.assertNotIn('Account <one>', html)
            for label in ('Reset unknown', 'No weekly cycle', 'Monthly resets outside this week'):
                self.assertIn(label, html)
            for removed in ('weekly-cell', 'weekly-hour', 'weekly-now', 'weekly-grid',
                            'weekly-bar', 'weekly-legend', 'Freshest:', 'Leanest:'):
                self.assertNotIn(removed, html)
        self.assertNotIn('Weekly resets', page.app(snapshot, view='now'))
        from unittest.mock import patch
        with patch.object(state, 'week_calendar', side_effect=AssertionError('inactive')):
            self.assertIsNone(state.build(make_cfg(), self.led, section='now')['weekly_quota'])

    def test_dst_week_localizes_next_reset_individually(self):
        self.now = datetime(2026, 11, 4, 16, tzinfo=timezone.utc)
        self.pool('dst', '2026-11-01T04:00:00Z')  # Sunday 00:00 EDT
        self.assertEqual(self.view()['lines'], [
            {'day': 'Sat', 'time': '23:00', 'names': ['dst']}])
        self.now = datetime(2026, 3, 11, 16, tzinfo=timezone.utc)
        self.led.record_usage('dst', 'weekly', 42, '2026-03-08T05:00:00Z')
        self.assertEqual(self.view()['lines'], [
            {'day': 'Sun', 'time': '01:00', 'names': ['dst']}])


class ApiCreditsRowTests(unittest.TestCase):
    """D41/mahler#903: the console's quota row for a credits platform shows
    the confirmed grant and spend, built from the same api_credits.snapshot
    used by `mahler status`."""

    def setUp(self):
        from mahler import config as config_mod
        import os
        import tempfile
        toml_text = """
[accounts.work]
env = {}
routing = { build = ["work-claude-api"] }

[platforms.work-claude-api]
from = "claude-api"
account = "work"
enabled = true

[api_credits]
enabled = true
workspace_id = "wrkspc_mahler"

[[api_credits.grants]]
granted_at = "2026-10-09"
expires_at = "2026-10-15T00:00:00Z"
pool_usd = 260

[projects.work-only]
enabled = false
account = "work"
"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "config.toml")
        with open(path, "w") as fh:
            fh.write(toml_text)
        self.cfg = config_mod.load(path)
        self.now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
        self.led = Ledger(":memory:", clock=lambda: self.now)
        self.addCleanup(self.led.close)
        self.led.set_kv("api_credits:key:claude-api@work",
                        json.dumps({"status": "ok", "at": iso(self.now)}))

    def test_row_shows_grant_and_spend_without_secrets(self):
        self.led.reserve_credit("claude-api@work", "2026-10-09", "attempt-1", 3.0, 20.0, run_id=1)
        row = state._quota_row(self.cfg, self.led, {}, "work-claude-api",
                               ["work-claude-api"], {"work-claude-api"}, {})
        self.assertIn("grant ends", row["detail"])
        self.assertNotIn("sk-ant", row["detail"])

    def test_row_flags_no_confirmed_grant(self):
        self.now = datetime(2026, 12, 1, tzinfo=timezone.utc)
        row = state._quota_row(self.cfg, self.led, {}, "work-claude-api",
                               ["work-claude-api"], {"work-claude-api"}, {})
        self.assertIn("no confirmed grant", row["detail"])
