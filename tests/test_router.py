"""Explicit review tier floors agree across routing and capacity diagnostics."""
import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler import config, platforms, router
from mahler.ledger import Ledger, iso


class ReviewTierTests(unittest.TestCase):
    def test_review_floor_default_and_explicit_pin(self):
        now = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg['routing']['review'] = ['agy-gemini', 'agy-claude']
        cfg['platforms']['agy-gemini']['tier'] = 1
        cfg['platforms']['agy-claude']['tier'] = 2
        pol = config.project_policy(cfg, 'x')
        led = Ledger(':memory:', clock=lambda: now)
        self.addCleanup(led.close)
        with mock.patch.object(platforms, 'available', return_value=True):
            for name in cfg['routing']['review']:
                for window in ('5h', 'weekly'):
                    led.record_usage(name, window, 10, iso(now + timedelta(days=3)))
            for tier, pin, expected in ((0, None, 'agy-gemini'),
                                        (2, None, 'agy-claude'),
                                        (2, 'agy-gemini', 'agy-gemini')):
                with self.subTest(tier=tier, pin=pin):
                    kwargs = dict(min_tier=tier, pin=pin)
                    chosen, _ = router.pick_for_project(cfg, led, pol, 'review', **kwargs)
                    self.assertEqual(chosen, expected)
                    diag = router.diagnose(cfg, led, pol, 'review', **kwargs)
                    names, _, _ = router.capacity_recovery(cfg, led, pol, 'review', **kwargs)
                    self.assertIn(expected, names)
                    self.assertEqual(diag[expected], ['eligible'])
                    if tier and not pin:
                        self.assertNotIn('agy-gemini', names)
                        self.assertIn('tier', diag['agy-gemini'])


class StaleCandidatesTests(unittest.TestCase):
    def test_stale_candidates_skip_fresh_and_excluded_slots(self):
        now = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg['routing']['review'] = ['agy-gemini', 'agy-claude', 'claude']
        pol = config.project_policy(cfg, 'x')
        led = Ledger(':memory:', clock=lambda: now)
        self.addCleanup(led.close)
        for window in ('5h', 'weekly'):
            led.record_usage('agy-gemini', window, 10, iso(now + timedelta(days=1)))
        with mock.patch.object(platforms, 'available', return_value=True):
            got = router.stale_candidates(cfg, led, pol, 'review', exclude={'claude'})
        self.assertEqual(got, ['agy-claude'])    # fresh skipped, builder slot never widened in


class GhWritesTests(unittest.TestCase):
    def _setup(self, route):
        now = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg['routing']['sort'] = route
        cfg['routing']['build'] = route
        pol = config.project_policy(cfg, 'x')
        led = Ledger(':memory:', clock=lambda: now)
        self.addCleanup(led.close)
        for name, windows in (('copilot', ('monthly',)), ('agy-gemini', ('5h', 'weekly'))):
            for window in windows:
                led.record_usage(name, window, 10, iso(now + timedelta(days=3)))
        return cfg, led, pol

    def test_sort_skips_copilot_but_build_keeps_it(self):
        cfg, led, pol = self._setup(['copilot', 'agy-gemini'])
        with mock.patch.object(platforms, 'available', return_value=True):
            for role in ('sort', 'plan'):
                cfg['routing'][role] = ['copilot', 'agy-gemini']
                chosen, reasons = router.pick_for_project(cfg, led, pol, role)
                self.assertEqual(chosen, 'agy-gemini', role)
                self.assertTrue(any('no GitHub write' in r for r in reasons))
            chosen, _ = router.pick_for_project(cfg, led, pol, 'build', size='s')
            self.assertEqual(chosen, 'copilot')

    def test_only_copilot_holds_with_reason(self):
        cfg, led, pol = self._setup(['copilot'])
        with mock.patch.object(platforms, 'available', return_value=True):
            chosen, reasons = router.pick_for_project(cfg, led, pol, 'sort')
            self.assertIsNone(chosen)
            self.assertTrue(any('copilot' in r and 'no GitHub write' in r for r in reasons))
            self.assertIn('no_gh_writes', router.diagnose(cfg, led, pol, 'sort')['copilot'])

    def test_declared_gh_writes_allows_copilot(self):
        cfg, led, pol = self._setup(['copilot'])
        cfg['platforms']['copilot']['gh_writes'] = True
        with mock.patch.object(platforms, 'available', return_value=True):
            chosen, _ = router.pick_for_project(cfg, led, pol, 'sort')
            self.assertEqual(chosen, 'copilot')


class FailedSortRoutingTests(unittest.TestCase):
    def test_failed_sort_routes_to_alternative_or_falls_back(self):
        now = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg['routing']['sort'] = ['platform_a', 'platform_b']
        cfg['platforms']['platform_a'] = {'kind': 'claude', 'enabled': True, 'account': 'personal', 'gh_writes': True}
        cfg['platforms']['platform_b'] = {'kind': 'claude', 'enabled': True, 'account': 'personal', 'gh_writes': True}
        pol = config.project_policy(cfg, 'x')
        led = Ledger(':memory:', clock=lambda: now)
        self.addCleanup(led.close)
        for window in ('5h', 'weekly'):
            led.record_usage('platform_a', window, 10, iso(now + timedelta(days=3)))
            led.record_usage('platform_b', window, 10, iso(now + timedelta(days=3)))

        item = led.upsert_item('x', 1, state='inbox', attempts=0)
        with mock.patch.object(platforms, 'available', return_value=True):
            chosen, _ = router.pick_for_project(cfg, led, pol, 'sort', item=item)
            self.assertEqual(chosen, 'platform_a')

            # Simulate a failed sort run on platform_a
            led.create_run(project='x', number=1, role='sort', platform='platform_a',
                           status='ended', outcome='no status line', exit_code=1, epoch=1)
            item = led.upsert_item('x', 1, state='inbox', attempts=1)

            # When platform_b is available, routes to platform_b
            chosen, _ = router.pick_for_project(cfg, led, pol, 'sort', item=item)
            self.assertEqual(chosen, 'platform_b')

            # When platform_a is the sole available route, falls back to platform_a
            chosen, _ = router.pick_for_project(cfg, led, pol, 'sort', busy={'platform_b'}, item=item)
            self.assertEqual(chosen, 'platform_a')

    def test_pinned_platform_stays_on_pin_even_after_failure(self):
        now = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg['routing']['sort'] = ['platform_a', 'platform_b']
        cfg['platforms']['platform_a'] = {'kind': 'claude', 'enabled': True, 'account': 'personal', 'gh_writes': True}
        cfg['platforms']['platform_b'] = {'kind': 'claude', 'enabled': True, 'account': 'personal', 'gh_writes': True}
        pol = config.project_policy(cfg, 'x')
        led = Ledger(':memory:', clock=lambda: now)
        self.addCleanup(led.close)
        for window in ('5h', 'weekly'):
            led.record_usage('platform_a', window, 10, iso(now + timedelta(days=3)))
            led.record_usage('platform_b', window, 10, iso(now + timedelta(days=3)))

        item = led.upsert_item('x', 1, state='inbox', attempts=1, pin='platform_a')
        led.create_run(project='x', number=1, role='sort', platform='platform_a',
                       status='ended', outcome='no status line', exit_code=1, epoch=1)
        with mock.patch.object(platforms, 'available', return_value=True):
            chosen, _ = router.pick_for_project(cfg, led, pol, 'sort', pin=item['pin'], item=item)
            self.assertEqual(chosen, 'platform_a')


class AccountBoundaryAndFailClosedTests(unittest.TestCase):
    """Protective guards for pre-existing account-boundary behavior.

    These four tests pass against the pre-D25 base
    c01dee624dd8e33983cc1397d8ca358bc2b95b74 by design; they do not by
    themselves detect the D25 credential fail-closed fix. The fix-detecting
    credential regression tests are ConfigTests.test_work_github_identity_requires_account_scoped_auth,
    ConfigTests.test_work_claude_account_requires_its_own_config_dir, and
    SlotAndUsageTests.test_work_claude_without_config_dir_is_never_probed in
    tests/test_accounts.py; those fail on c01dee6 and pass on HEAD.
    """

    def setUp(self):
        self.now = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg['accounts'] = {'work': {'env': {'CLAUDE_CONFIG_DIR': '/tmp/work'}}}
        self.cfg['platforms']['work-claude'] = copy.deepcopy(self.cfg['platforms']['claude'])
        self.cfg['platforms']['work-claude']['account'] = 'work'
        self.cfg['platforms']['work-claude']['quota_group'] = 'claude@work'
        self.led = Ledger(':memory:', clock=lambda: self.now)
        self.addCleanup(self.led.close)

    # Guards pre-existing refusal of pins that cross account boundaries.
    def test_cross_account_pin_refused_with_reason(self):
        pol = {'name': 'p1', 'account': 'personal'}
        with mock.patch.object(platforms, 'available', return_value=True):
            for window in ('5h', 'weekly'):
                self.led.record_usage('work-claude', window, 10, iso(self.now + timedelta(days=3)))
            chosen, reasons = router.pick_for_project(self.cfg, self.led, pol, 'build', pin='work-claude')
            self.assertIsNone(chosen)
            self.assertTrue(any('spends the work account, not personal' in r for r in reasons))

    # Guards pre-existing filtering of undeclared accounts in priority mode.
    def test_priority_mode_refuses_undeclared_account_platforms(self):
        cands = router.candidates_for_priority(
            self.cfg, 'build', ['personal'],
            {'build': ['work-claude', 'agy-claude']}
        )
        self.assertNotIn('work-claude', cands)
        self.assertIn('agy-claude', cands)

    # Guards pre-existing fail-closed behavior for stale quota samples.
    def test_fail_closed_on_stale_quota_sample(self):
        pol = {'name': 'p1', 'account': 'personal'}
        self.led.record_usage('claude', '5h', 10, iso(self.now - timedelta(hours=1)))
        chosen, reasons = router.pick_for_project(self.cfg, self.led, pol, 'build', pin='claude')
        self.assertIsNone(chosen)
        self.assertTrue(any('stale' in r for r in reasons))

    # Guards pre-existing max-size bounds for unpinned candidates.
    def test_max_size_refuses_oversized_candidate_without_pin(self):
        pol = {'name': 'p1', 'account': 'personal'}
        self.cfg['routing']['build'] = ['claude-low']
        with mock.patch.object(platforms, 'available', return_value=True):
            for window in ('5h', 'weekly'):
                self.led.record_usage('claude-low', window, 10, iso(self.now + timedelta(days=3)))
            chosen, reasons = router.pick_for_project(self.cfg, self.led, pol, 'build', pin='claude-low', size='m')
            # Pin overrides size limit
            self.assertEqual(chosen, 'claude-low')

            # Without pin, size:m is refused on claude-low (max_size: s)
            chosen, reasons = router.pick_for_project(self.cfg, self.led, pol, 'build', size='m')
            self.assertTrue(any('claude-low: only takes size:s' in r for r in reasons))


