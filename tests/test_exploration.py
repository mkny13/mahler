"""D33 exploration: deterministic selection without spending retry budgets."""
import copy
import hashlib
import json
import unittest
from datetime import timedelta
from unittest import mock

from mahler import config, router, scorecard, ship, tick, scheduler
from mahler.console import state
from mahler.ledger import Ledger, iso


class ExplorationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg['measure']['explore_share'] = dict(build=1, fix=1, sort=1, plan=1)
        self.cfg['claude_peak']['enabled'] = False
        self.cfg['platforms'] = {
            'cheap': dict(enabled=True, kind='kilo', metered=False, max_size='s',
                          tier=1, model='cheap-model'),
            'normal': dict(enabled=True, kind='cline', metered=False, tier=2,
                           model='normal-model'),
            'unknown': dict(enabled=True, kind='cline', metered=False, tier=1),
        }
        self.cfg['routing'] = {r: ['normal', 'unknown', 'cheap']
                               for r in ('build', 'fix', 'sort', 'plan')}
        self.cfg['prices'] = {'cheap-model': {'in': 1, 'out': 2},
                              'normal-model': {'in': 4, 'out': 8}}
        self.cfg['projects']['p'] = dict(enabled=True, hot_hold=False, path='/tmp/test', repo='x/y')
        self.pol = config.project_policy(self.cfg, 'p')
        self.led = Ledger(':memory:')
        self.addCleanup(self.led.close)
        self.led.upsert_item('p', 1, title='Add a button', state='ready',
                             sorted_at=iso(self.led.now() - timedelta(hours=1)),
                             labels=json.dumps(['size:s']))

    def pick(self, role='build', size='s', **kw):
        return router.explore_for_project(self.cfg, self.led, self.pol,
                                         self.led.item('p', 1), role, size=size, **kw)

    def test_deterministic_hash_and_role_share(self):
        self.cfg['measure']['explore_share']['build'] = .15
        for attempts in range(40):
            self.led.upsert_item('p', 1, attempts=attempts)
            selected = int(hashlib.sha1(f'p#1:build:{attempts}'.encode()).hexdigest(), 16) % 1000 < 150
            self.assertEqual(self.pick(), 'cheap' if selected else None)
            self.assertEqual(self.pick(), self.pick())
        self.assertIsNone(self.pick(role='review'))

    def test_pin_risk_and_opt_out(self):
        self.led.upsert_item('p', 1, pin='normal')
        self.assertIsNone(self.pick())
        self.led.upsert_item('p', 1, pin=None, title='Fix lease protocol')
        for role in ('build', 'sort', 'plan', 'fix'):
            self.assertIsNone(self.pick(role))
        self.led.upsert_item('p', 1, title='Button')
        self.pol['explore'] = False
        self.assertIsNone(self.pick())

    def test_account_defaults_and_boundary(self):
        self.cfg['projects']['p']['account'] = 'work'
        self.pol = config.project_policy(self.cfg, 'p')
        self.assertFalse(self.pol['explore'])
        self.assertIsNone(self.pick())
        self.pol['explore'] = True
        self.assertIsNone(self.pick())  # personal routes cannot spend work
        self.cfg['projects']['p'] = {'accounts': ['work', 'personal']}
        self.assertTrue(config.project_policy(self.cfg, 'p')['explore'])

    def test_headroom_busy_and_escalation(self):
        self.assertEqual(self.pick(), 'cheap')
        self.assertEqual(self.pick(busy={'cheap'}), 'normal')
        self.assertEqual(self.pick(min_tier=2), 'normal')
        self.led.record_usage('cheap', router.HOLD, 100,
                              iso(self.led.now() + timedelta(hours=1)))
        self.assertEqual(self.pick(), 'normal')
        self.cfg['platforms']['normal']['metered'] = True
        self.assertEqual(self.pick(), 'unknown')  # stale meter is ineligible

    def test_peak_min_size_and_priority_route_are_enforced(self):
        self.cfg['platforms']['cheap']['kind'] = 'claude'
        with mock.patch.object(router, 'peak_state', return_value=(True, self.led.now() + timedelta(hours=1))):
            self.assertEqual(self.pick(), 'normal')
        self.cfg['platforms']['cheap']['min_size'] = 'm'
        self.assertEqual(self.pick(), 'normal')
        self.pol.update(account_mode='priority', routing={'build': ['unknown']})
        self.assertEqual(self.pick(), 'unknown')

    def test_cost_ties_keep_route_order_and_efforts_have_separate_evidence(self):
        self.cfg['prices'] = {}
        self.assertEqual(self.pick(), 'normal')
        self.seed('normal')
        self.assertEqual(self.pick(), 'unknown')
        self.cfg['platforms']['normal'].update(kind='codex', effort='high')
        self.assertEqual(self.pick(), 'normal')

    def test_ci_and_review_fix_paths_can_explore(self):
        for review in (False, True):
            with self.subTest(review=review):
                self.led.upsert_item('p', 1, pr=12, state='verifying')
                ctx = scheduler.Ctx(self.cfg, self.led)
                view = {'headRefName': 'test', 'headRefOid': str(review)}
                with mock.patch('mahler.ship.start', return_value=True) as start, \
                        mock.patch.object(ctx, 'ping'):
                    if review:
                        ship._review_triggered_fix(ctx, 'p', self.led.item('p', 1), 12, view, 'fix button')
                    else:
                        ship._red_ci(ctx, 'p', self.led.item('p', 1), 12, view)
                self.assertTrue(start.call_args.kwargs['explore'])
                self.assertEqual(start.call_args.args[3], 'fix')

    def test_free_stream_one_size_up_never_two(self):
        self.assertEqual(self.pick(size='m'), 'cheap')
        self.assertEqual(self.pick(size='l'), 'normal')
        self.cfg['platforms']['cheap']['metered'] = True
        for window in router.WINDOWS:
            self.led.record_usage('cheap', window, 0, iso(self.led.now() + timedelta(hours=1)))
        self.assertEqual(self.pick(size='m'), 'normal')
        self.assertEqual(self.pick(size='s'), 'cheap')

    def seed(self, platform, count=8, **kw):
        for _ in range(count):
            self.led.create_run(project='p', number=1, role='build', size='s',
                                platform=platform, model=self.cfg['platforms'][platform].get('model'),
                                effort='default', epoch=1, status='ended', outcome='DONE',
                                ended_at=iso(self.led.now()), **kw)

    def test_no_unproven_falls_through(self):
        for name in self.cfg['platforms']:
            self.seed(name)
        self.assertIsNone(self.pick())
        self.assertEqual(router.pick_for_project(self.cfg, self.led, self.pol, 'build', size='s')[0], 'normal')

    def test_median_token_mix_drives_expected_cost(self):
        self.cfg['prices']['cheap-model'] = {'in': 10, 'out': 1}
        self.cfg['prices']['normal-model'] = {'in': 1, 'out': 10}
        self.seed('unknown', 1, tokens_in=10000, tokens_out=10)
        self.assertEqual(self.pick(), 'normal')

    def test_previous_exploration_forces_normal_retry(self):
        self.seed('cheap', 1, explore=1)
        self.assertIsNone(self.pick())
        self.seed('normal', 1)
        self.assertEqual(self.pick(), 'cheap')

    def test_schedule_marks_exploration_and_records_choice(self):
        ctx = scheduler.Ctx(self.cfg, self.led)
        with mock.patch('mahler.platforms.available', return_value=True), \
                mock.patch('mahler.tick.start', return_value=True) as start:
            tick.schedule(ctx, [self.pol])
        self.assertEqual(start.call_args.args[4], 'cheap')
        self.assertTrue(start.call_args.kwargs['explore'])
        self.assertTrue(any('trying cheap' in line for line in ctx.lines))

    def test_start_persists_real_size_and_console_copy(self):
        ctx = scheduler.Ctx(self.cfg, self.led)
        with mock.patch('mahler.tick.runner.prepare', return_value={}), \
                mock.patch('mahler.tick.prompt.build', return_value='test'), \
                mock.patch('mahler.tick.runner.launch', return_value={'branch': 'test'}), \
                mock.patch.object(ctx, 'gh'):
            self.assertTrue(tick.start(ctx, 'p', self.led.item('p', 1), 'build', 'cheap',
                                       size='m', explore=True))
        run = self.led.last_run('p', 1)
        self.assertEqual((run['explore'], run['size']), (1, 'm'))
        self.assertIn('trying cheap', state._runs(self.cfg, self.led, self.led.now())[0]['meta'])
        self.led.update_run(run['id'], status='ended', outcome='DONE', ended_at=iso(self.led.now()))
        self.assertEqual(scorecard.table(self.led, self.cfg)[0]['size'], 'm')

    def test_planning_record_uses_plan_scorecard(self):
        self.led.create_run(project='p', number=1, role='sort', routing_role='plan',
                            size='l', platform='normal', epoch=1, status='ended',
                            outcome='failure', ended_at=iso(self.led.now()))
        self.assertEqual(scorecard.table(self.led, self.cfg)[0]['role'], 'plan')
