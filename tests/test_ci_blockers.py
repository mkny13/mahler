"""Parked queue blockers recover or explain their hold, using isolated state."""
import copy
import json
import unittest
from unittest.mock import Mock

from mahler import ci_blockers, config, scheduler, ship, tick
from mahler.console import state
from mahler.gh import GHError
from mahler.ledger import Ledger


class ParkedBlockerTests(unittest.TestCase):
    def setUp(self):
        self.led = Ledger(':memory:')
        self.addCleanup(self.led.close)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg['projects'] = {'x': {'repo': 'x/y', 'path': '/unused'}}
        self.ctx = scheduler.Ctx(self.cfg, self.led)
        self.ctx.ping = Mock()
        self.ctx.say = Mock()
        self.gh = Mock()
        self.ctx.gh = Mock(return_value=self.gh)
        self.gh.required_status_checks.return_value = {'checks'}
        self.gh.base_checks.return_value = self.rollup('FAILURE')
        self.gh.open_prs.return_value = []
        self.gh.create_issue.return_value = 'https://github.com/x/y/issues/300'

    @staticmethod
    def rollup(result):
        return [{'name': 'checks', 'status': 'COMPLETED', 'conclusion': result}]

    def blocker(self, parked='needs_you', **fields):
        self.led.upsert_item('x', 379, state=parked, title='Fix checks')
        record = {'number': 379, 'reason': 'required check checks fails repo-wide', **fields}
        self.led.set_kv('ci_blocker:x', json.dumps(record))

    def candidates(self):
        return tick._candidates(self.ctx, [self.ctx.policy('x')])

    def test_ci_blocker_refresh_clears_on_green_base_without_check_field(self):
        for fields in ({}, {'check': 'checks'}):
            with self.subTest(fields=fields):
                self.blocker(**fields)
                self.gh.base_checks.return_value = self.rollup('SUCCESS')
                self.assertIsNone(ship.refresh_ci_blocker(self.ctx, 'x'))
                self.assertIsNone(self.led.get_kv('ci_blocker:x'))
                self.assertEqual(self.led.item('x', 379)['state'], 'needs_you')

    def test_unknown_pending_or_red_base_keeps_hold(self):
        for rollup in ([], self.rollup('FAILURE'), [{'name': 'checks', 'status': 'IN_PROGRESS'}]):
            self.blocker()
            self.gh.base_checks.return_value = rollup
            self.assertIsNotNone(ship.refresh_ci_blocker(self.ctx, 'x'))
        self.gh.base_checks.side_effect = GHError('unavailable')
        self.assertIsNotNone(ship.refresh_ci_blocker(self.ctx, 'x'))

    def test_ci_blocker_adopts_ready_fix_item_when_blocker_parked(self):
        for parked in ('needs_you', 'failed', 'parked'):
            for fields in ({'check': 'checks'}, {'reason': 'repo-wide tests/github-contract.test.ts failure'}):
                with self.subTest(parked=parked, fields=fields):
                    self.blocker(parked, **fields)
                    self.led.upsert_item('x', 378, state='ready', title='Review follow-up: tests/github-contract.test.ts:135-163', issue_body='Repair checks')
                    self.led.upsert_item('x', 263, state='ready', title='Unrelated feature')
                    blocker = ship.refresh_ci_blocker(self.ctx, 'x')
                    self.assertEqual(blocker['number'], 378)
                    self.assertEqual([i['number'] for _, _, i in self.candidates()], [378])
                    self.assertEqual(self.led.item('x', 379)['state'], parked)

    def test_generic_agent_blocker_adopts_path_named_review_follow_up(self):
        self.blocker()
        self.led.upsert_item(
            'x', 378, state='ready',
            title='Review follow-up: tests/github-contract.test.ts:135-163',
            issue_body='Revisit the test failure.')
        blocker = ship.refresh_ci_blocker(self.ctx, 'x')
        self.assertEqual(blocker['number'], 378)
        self.assertEqual([i['number'] for _, _, i in self.candidates()], [378])

    def test_parked_ci_blocker_notifies_owner(self):
        self.blocker()
        self.led.upsert_item('x', 263, state='ready')
        ship.refresh_ci_blocker(self.ctx, 'x')
        self.assertEqual(self.candidates(), [])
        self.candidates()
        self.ctx.ping.assert_called_once()
        self.assertIn('parked CI blocker #379', self.ctx.ping.call_args.args[0])
        self.assertIn('263', str(self.ctx.ping.call_args))

    def test_parked_dependency_notifies_owner(self):
        self.led.upsert_item('x', 748, state='failed')
        self.led.upsert_item('x', 1038, state='ready', depends='[748]')
        self.assertEqual(self.candidates(), [])
        self.candidates()
        self.ctx.ping.assert_called_once()
        self.assertIn('x #1038 held behind parked dependency #748', self.ctx.ping.call_args.args[0])

    def test_dry_run_does_not_consume_notification(self):
        self.blocker()
        self.led.upsert_item('x', 263, state='ready')
        self.ctx.dry_run = True
        self.candidates()
        self.ctx.ping.assert_not_called()
        self.assertIsNone(self.led.get_kv('ci_blocker_parked_ping:x:379'))

    def test_detect_ci_blocker_skips_parked_and_needs_you_items(self):
        for n, parked in enumerate(('needs_you', 'failed', 'parked'), 1):
            self.led.upsert_item('x', n, state=parked, title='Fix checks')
        ship.detect_ci_blocker(self.ctx, 'x')
        self.assertEqual(ci_blockers.active(self.led, 'x')['number'], 300)

    def test_parked_blocker_can_resume_or_finish(self):
        for resumed in ('ready', 'shipped'):
            with self.subTest(resumed=resumed):
                self.blocker()
                self.led.upsert_item('x', 263, state='ready')
                self.candidates()
                self.led.set_state('x', 379, resumed)
                numbers = [i['number'] for _, _, i in self.candidates()]
                self.assertEqual(numbers, [379] if resumed == 'ready' else [263])

    def test_console_names_parked_ci_and_dependency(self):
        self.blocker('failed')
        self.led.upsert_item('x', 263, state='ready', depends='[379]')
        pending = {'x': [dict(i) for i in self.led.items('x')]}
        holds = [{'kind': 'ci_blocker', 'project': 'x', 'number': 263, 'on': [379]}]
        reasons = state._hold_reasons(self.cfg, holds, pending, [], self.led.now(), self.led)
        self.assertIn('failed', str(reasons))
        self.assertIn('required check checks', str(reasons))
        holds[0]['kind'] = 'deps'
        reasons = state._hold_reasons(self.cfg, holds, pending, [], self.led.now(), self.led)
        self.assertIn('(failed)', str(reasons))


class RepoWideStepTests(unittest.TestCase):
    """A PR red on the same job and step as the base branch is a repo-wide failure
    on any attempt, and a ci_failed item is re-queued once when base turns green
    (mahler#1593)."""

    PR_JOB = 'https://github.com/x/y/actions/runs/1/job/11'
    BASE_JOB = 'https://github.com/x/y/actions/runs/2/job/22'

    def setUp(self):
        self.led = Ledger(':memory:')
        self.addCleanup(self.led.close)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg['projects'] = {'x': {'repo': 'x/y', 'path': '/unused'}}
        self.ctx = scheduler.Ctx(self.cfg, self.led)
        self.ctx.ping = Mock()
        self.ctx.say = Mock()
        self.gh = Mock()
        self.ctx.gh = Mock(return_value=self.gh)
        self.gh.required_status_checks.return_value = {'checks'}
        self.gh.open_prs.return_value = []
        self.gh.create_issue.return_value = 'https://github.com/x/y/issues/300'
        self.gh.branch_sha.return_value = 'base1'
        self.gh.failed_run_log.return_value = (1, 'audit failed')
        self.steps = {self.PR_JOB: 'pnpm audit --audit-level high',
                      self.BASE_JOB: 'pnpm audit --audit-level high'}
        self.gh.failed_step.side_effect = lambda url: self.steps.get(url)
        self.base_green()
        self.led.upsert_item('x', 434, state='verifying', pr=435, title='Feature',
                             branch='mahler/434-x', sorted_at='2026-11-17T00:00:00Z')
        self.led.upsert_item('x', 436, state='ready', title='Next', depends='[434]',
                             sorted_at='2026-11-17T00:00:00Z')

    def base_green(self):
        self.gh.base_checks.return_value = [
            {'name': 'checks', 'status': 'COMPLETED', 'conclusion': 'SUCCESS'}]

    def base_red(self):
        self.gh.base_checks.return_value = [
            {'name': 'checks', 'status': 'COMPLETED', 'conclusion': 'FAILURE',
             'detailsUrl': self.BASE_JOB}]

    def view(self, sha='head1'):
        return {'state': 'OPEN', 'headRefName': 'mahler/434-x', 'headRefOid': sha,
                'baseRefName': 'main',
                'statusCheckRollup': [{'name': 'checks', 'status': 'COMPLETED',
                                       'conclusion': 'FAILURE', 'detailsUrl': self.PR_JOB}]}

    def red_ci(self, attempts=0, sha='head1'):
        self.led.upsert_item('x', 434, attempts=attempts)
        with unittest.mock.patch.object(ship, 'start', return_value=False):
            ship._red_ci(self.ctx, 'x', self.led.item('x', 434), 435, self.view(sha))

    def item(self):
        return self.led.item('x', 434)

    def test_pr_red_on_same_job_and_step_as_main_holds_without_spending_attempt(self):
        self.base_red()
        self.red_ci(attempts=0)
        item = self.item()
        self.assertEqual(item['state'], 'waiting')
        self.assertEqual(json.loads(self.led.get_kv('wait:x#434'))['cause'], 'ci_repo_wide')
        self.assertEqual((item['attempts'], item['esc_tier'], item['esc_fails']), (0, 0, 0))
        self.assertIsNotNone(ci_blockers.active(self.led, 'x'))

    def test_pr_red_on_different_step_does_not_hold_repo_wide(self):
        self.base_red()
        self.steps[self.BASE_JOB] = 'Run tests'
        self.red_ci(attempts=0)
        item = self.item()
        self.assertNotEqual(item['state'], 'waiting')
        self.assertEqual(item['esc_fails'], 1)
        self.assertTrue(self.led.get_kv('red:x#434:435:head1'))

    def fail_on_own(self, sha='head1'):
        """Exhaust attempts while base is green: the item parks in failed."""
        self.red_ci(attempts=2, sha=sha)
        self.assertEqual(self.item()['state'], 'failed')

    def test_detect_needs_two_prs_red_on_the_same_step(self):
        other = 'https://github.com/x/y/actions/runs/3/job/33'
        self.led.upsert_item('x', 437, state='verifying', pr=438, title='Other')
        for step, expected in (('Run tests', False), ('pnpm audit --audit-level high', True)):
            with self.subTest(step=step):
                self.led.set_kv('ci_blocker:x', None)
                self.steps[other] = step

                def view(pr):
                    v = self.view()
                    if pr == 438:
                        v['statusCheckRollup'][0]['detailsUrl'] = other
                    return v
                self.gh.pr_view.side_effect = view
                ship.detect_ci_blocker(self.ctx, 'x')
                self.assertEqual(ci_blockers.active(self.led, 'x') is not None, expected)

    def test_ci_failed_item_requeued_once_when_base_green(self):
        self.fail_on_own()
        ship.requeue_ci_failed(self.ctx, 'x')        # base unchanged: no new evidence
        self.assertEqual(self.item()['state'], 'failed')
        self.gh.branch_sha.return_value = 'base2'    # main moved on and is green
        ship.requeue_ci_failed(self.ctx, 'x')
        item = self.item()
        self.assertEqual(item['state'], 'verifying')
        self.assertEqual(item['attempts'], 0)

    def test_ci_failed_item_not_requeued_while_base_still_red(self):
        self.fail_on_own()
        self.gh.branch_sha.return_value = 'base2'
        self.base_red()
        self.steps[self.BASE_JOB] = 'Run tests'
        ship.requeue_ci_failed(self.ctx, 'x')
        self.assertEqual(self.item()['state'], 'failed')

    def test_ci_failed_item_not_requeued_repeatedly(self):
        self.fail_on_own()
        self.gh.branch_sha.return_value = 'base2'
        ship.requeue_ci_failed(self.ctx, 'x')
        self.assertEqual(self.item()['state'], 'verifying')
        self.gh.branch_sha.return_value = 'base2'
        self.fail_on_own()                            # same head red again
        self.gh.branch_sha.return_value = 'base3'
        for _ in range(3):
            ship.requeue_ci_failed(self.ctx, 'x')
        self.assertEqual(self.item()['state'], 'failed')

    def test_other_failure_class_is_not_requeued(self):
        self.fail_on_own()
        self.led.set_state('x', 434, 'ready', 'you said go')
        self.led.set_state('x', 434, 'failed', 'review rejected 3 times')
        self.gh.branch_sha.return_value = 'base2'
        ship.requeue_ci_failed(self.ctx, 'x')
        self.assertEqual(self.item()['state'], 'failed')


if __name__ == '__main__':
    unittest.main()
