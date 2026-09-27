import console_snapshot
"""Models state from synthetic run history."""
import unittest
from datetime import datetime, timezone

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
        from test_console import make_cfg
        self.now = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
        self.led = Ledger(':memory:', clock=lambda: self.now)
        self.addCleanup(self.led.close)
        self.cfg = make_cfg()

    def pool(self, name, days=None, window='weekly', claude=False, pct=42):
        from datetime import timedelta
        self.cfg['platforms'][name] = {'windows': [window]}
        return {'name': name, 'model': 'Model <test>', 'members': [name],
                'claude': claude, 'state': 'ok', 'windows': [
                    {'window': window, 'pct': pct, 'soft': 80,
                     'resets': self.now + timedelta(days=days) if days is not None else None}]}

    def view(self, *pools):
        return state._weekly_quota_view(self.cfg, self.led, pools, self.now)

    def test_positions_monthly_unknown_and_boundaries(self):
        pools = [self.pool('now', 0), self.pool('tomorrow', 1),
                 self.pool('monthly', 6.5, 'monthly'), self.pool('unknown'),
                 self.pool('past', -1), self.pool('later', 7, 'monthly')]
        v = self.view(*pools)
        self.assertEqual(len(v['days']), 7)
        self.assertEqual([r['day'] for r in v['rows']], [0, 1, 6, None, None, None])
        self.assertEqual(v['rows'][0]['position'], 0)
        self.assertAlmostEqual(v['rows'][2]['position'], 100 * 6.5 / 7)
        self.assertEqual(v['rows'][3]['reset_text'], 'unknown')
        self.assertIn('awaiting updated reset', v['rows'][4]['reset_text'])
        self.assertIn('beyond this week', v['rows'][5]['reset_text'])
        self.assertEqual(len(v['unscheduled']), 3)
        self.assertEqual(v['rows'][2]['used_pct'], 42)
        self.assertEqual(v['rows'][2]['soft'], 80)

    def test_missing_reading_and_no_long_window(self):
        missing = self.pool('missing')
        missing['windows'] = []
        missing['state'] = 'stale'
        v = self.view(missing, self.pool('short', .1, '5h'))
        self.assertEqual(len(v['rows']), 1)
        self.assertIsNone(v['rows'][0]['used_pct'])
        self.assertEqual(v['rows'][0]['reset_text'], 'unknown')
        self.assertTrue(v['rows'][0]['stale'])

    def test_claude_overlay_only_next_24_hours(self):
        from datetime import timedelta
        for hours, expected in [(-1, False), (0, True), (5, True), (24, False)]:
            with self.subTest(hours=hours):
                pool = self.pool('claude', 3, claude=True)
                pool['windows'].append({'window': '5h', 'pct': 20, 'soft': 75,
                                       'resets': self.now + timedelta(hours=hours)})
                self.assertEqual(bool(self.view(pool)['rows'][0]['overlay']), expected)
                pool['claude'] = False
                self.assertIsNone(self.view(pool)['rows'][0]['overlay'])

    def test_shared_groups_and_latest_ledger_reading(self):
        from datetime import timedelta
        from test_console import fresh
        # The consolidated snapshot is the same input used by existing Capacity.
        fresh(self.led, 'claude', 'weekly', 20, timedelta(days=2))
        fresh(self.led, 'claude', 'weekly', 60, timedelta(days=4))
        snapshot = state.build(self.cfg, self.led, section='capacity')
        quota = snapshot['quota']
        weekly = snapshot['weekly_quota']['rows']
        self.assertEqual(len({r['name'] for r in weekly}), len(weekly))
        claude = next(q for q in quota if 'claude' in q['members'])
        row = next(r for r in weekly if r['name'] == claude['name'])
        self.assertEqual(row['used_pct'], 60)
        self.assertEqual(row['day'], 4)

    def test_render_both_layouts_and_only_active_sections(self):
        from mahler.console import page
        snapshot = state.build(self.cfg, self.led)
        snapshot['weekly_quota'] = self.view(self.pool('Account <one>', 2),
                                           self.pool('unknown'))
        desktop = page.app(snapshot, view='capacity')
        phone = page.app(snapshot, layout='phone', tab='browse')
        self.assertIn('data-capacity-mode="weekly"', desktop)
        self.assertIn('weekly-grid', desktop)
        self.assertIn('weekly-reset', desktop)
        self.assertIn('weekly-now', desktop)
        self.assertIn('width:42.00%', desktop)
        self.assertEqual(phone.count('<details class="weekly-day">'), 7)
        for html in (desktop, phone):
            self.assertIn('Account &lt;one&gt;', html)
            self.assertIn('Model &lt;test&gt;', html)
            self.assertIn('unknown', html)
        self.assertNotIn('Weekly resets', page.app(snapshot, view='now'))
        self.assertIsNone(state.build(self.cfg, self.led, section='now')['weekly_quota'])

    def test_local_ranges_across_dst(self):
        from datetime import timedelta
        from local_timezone import local_timezone
        with local_timezone('America/New_York'):
            self.now = datetime(2026, 10, 31, 16, tzinfo=timezone.utc)
            row = self.pool('dst', 1)
            v = self.view(row)
            self.assertIn('12:00 EDT', v['days'][0]['range'])
            self.assertIn('11:00 EST', v['days'][0]['range'])
            self.assertEqual(v['rows'][0]['day'], 1)
            self.assertEqual(row['windows'][0]['resets'] - self.now, timedelta(hours=24))
