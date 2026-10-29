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


if __name__ == '__main__':
    unittest.main()
