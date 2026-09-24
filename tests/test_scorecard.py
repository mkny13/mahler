"""Synthetic history tests: no production state or external services."""
from datetime import datetime, timedelta, timezone
import unittest

from mahler.ledger import Ledger, iso
from mahler.scorecard import attempts


class AttemptTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.led = Ledger(':memory:', clock=lambda: self.now)
        self.addCleanup(self.led.close)

    def advance(self, days=1):
        self.now += timedelta(days=days)

    def run_attempt(self, number=1, role='build', outcome='DONE', project='p', **cols):
        self.led.upsert_item(project, number)
        args = dict(project=project, number=number, role=role, platform='slot',
                    model='model', effort='medium', size='s', epoch=1,
                    status='ended', outcome=outcome, exit_code=0,
                    ended_at=iso(self.now))
        args.update(cols)
        return self.led.create_run(**args)

    def result(self, rid, result, why=None):
        row = next(r for r in attempts(self.led, None) if r['run'] == rid)
        self.assertEqual(row['result'], result, row)
        if why:
            self.assertEqual(row['why'], why)
        return row

    def ship(self):
        self.advance()
        self.led.upsert_item('p', 1, pr=50)
        self.led.event('shipped', 'p', 1, {'pr': 50})

    def bug(self, days=1, body='Breaks #1.', project='p', labels='["type:bug"]'):
        self.advance(days)
        self.led.upsert_item(project, 100, issue_body=body, labels=labels,
                             created_at=iso(self.now))

    def test_clean_and_metadata(self):
        rid = self.run_attempt()
        row = self.result(rid, 'success')
        self.assertEqual({k: row[k] for k in ('project', 'number', 'role', 'size',
                         'platform', 'model', 'effort')}, dict(project='p', number=1,
                         role='build', size='s', platform='slot', model='model', effort='medium'))
        self.ship()
        self.advance(30)
        self.result(rid, 'success')

    def test_later_fix_including_running_fix(self):
        rid = self.run_attempt()
        self.advance()
        fix = self.run_attempt(role='fix', status='running', ended_at=None)
        self.result(rid, 'failure', 'later fix run')
        self.assertNotIn(fix, [r['run'] for r in attempts(self.led, None)])
        self.led.update_run(fix, status='ended', ended_at=iso(self.now))
        self.result(fix, 'success')

    def test_review_fail_persists_after_kv_overwrite(self):
        rid = self.run_attempt()
        self.advance()
        self.led.event('review_verdict', 'p', 1,
                       {'verdict': 'fail', 'review_run': 2, 'reviewed_sha': 'sha'})
        self.led.set_kv('review:p#1', '{"verdict":"pass"}')
        self.result(rid, 'failure', 'failed review')

    def test_legacy_review_failure(self):
        rid = self.run_attempt()
        self.advance()
        self.run_attempt(role='review', outcome='REVIEW-FAIL')
        self.result(rid, 'failure', 'failed review')

    def test_revert(self):
        rid = self.run_attempt()
        self.advance()
        self.led.event('revert_requested', 'p', 1, {'pr': 50})
        self.result(rid, 'failure', 'revert')

    def test_uat_fail(self):
        rid = self.run_attempt()
        self.ship()
        self.led.add_uat('p', 1, 50, 'sha', 'Title', 'Check')
        self.advance()
        self.led.set_uat_verdict('p', 1, 'fail')
        self.result(rid, 'failure', 'UAT fail')

    def test_bug_window_and_whole_tokens(self):
        for days, body, project, labels, expected in (
            (1, 'Breaks #1.', 'p', '["type:bug"]', 'failure'),
            (14, 'Breaks (#50)', 'p', '["type:bug"]', 'failure'),
            (15, 'Breaks #1', 'p', '["type:bug"]', 'success'),
            (-1, 'Breaks #1', 'p', '["type:bug"]', 'success'),
            (1, 'Breaks #10 #500 #1abc abc#1', 'p', '["type:bug"]', 'success'),
            (1, 'Breaks #1', 'other', '["type:bug"]', 'success'),
            (1, 'Breaks #1', 'p', '["type:feature"]', 'success'),
        ):
            with self.subTest(days=days, body=body, project=project, labels=labels):
                self.led.con.execute('DELETE FROM items')
                self.led.con.execute('DELETE FROM runs')
                self.led.con.execute('DELETE FROM events')
                rid = self.run_attempt()
                self.ship()
                self.bug(days, body, project, labels)
                self.result(rid, expected)

    def test_linked_uat_bug_without_label_or_reference(self):
        rid = self.run_attempt(role='review', outcome='REVIEW-PASS')
        self.ship()
        self.led.add_uat('p', 1, 50, 'sha', 'Title', 'Check')
        self.bug(body='', labels='[]')
        self.led.set_uat_verdict('p', 1, 'fail', bug=100)
        self.result(rid, 'failure', 'bug within 14 days')

    def test_review_clean_and_missed_defects(self):
        for outcome in ('REVIEW-PASS', 'REVIEW-FAIL'):
            rid = self.run_attempt(role='review', outcome=outcome)
            self.result(rid, 'success')
        self.ship()
        self.bug()
        for row in attempts(self.led, None):
            self.assertEqual(row['result'], 'failure')

    def test_crashes(self):
        for cols in ({'outcome': None}, {'outcome': 'no status line'},
                     {'exit_code': 1}, {'outcome': 'YIELDED', 'stop_reason': 'timeout'}):
            with self.subTest(cols=cols):
                self.result(self.run_attempt(**cols), 'failure')

    def test_each_exclusion(self):
        for outcome, reason, why in (
            ('launch failed: executable missing', None, 'launch failed'),
            ('setup failed', 'setup-failed', 'setup failed'),
            ('not claimed', None, 'not claimed'),
            ('BLOCKED missing credentials', None, 'BLOCKED'),
            ('NEEDS-YOU', None, 'NEEDS-YOU'),
            *[('YIELDED', reason, reason) for reason in ('quota', 'preempted', 'closed', 'parked')],
        ):
            with self.subTest(why=why):
                rid = self.run_attempt(outcome=outcome, stop_reason=reason, exit_code=1)
                self.result(rid, 'excluded', why)

    def test_ready_first_build_success_failure_pending_and_exclusions(self):
        for role in ('sort', 'plan'):
            rid = self.run_attempt(role=role, outcome='READY')
            self.result(rid, 'pending')
        self.advance()
        self.run_attempt(outcome='launch failed: missing')
        self.result(rid, 'pending')
        build = self.run_attempt(status='running', ended_at=None)
        self.result(rid, 'pending')
        self.led.update_run(build, status='ended', ended_at=iso(self.now))
        self.result(rid, 'success')
        self.led.update_run(build, outcome='exit 1', exit_code=1)
        self.advance()
        self.run_attempt()
        self.result(rid, 'failure')

    def test_ready_escalated_or_split(self):
        rid = self.run_attempt(role='sort', outcome='READY')
        self.advance()
        self.run_attempt()
        self.led.upsert_item('p', 1, esc_tier=1)
        self.result(rid, 'failure', 'item escalated')
        self.led.upsert_item('p', 1, esc_tier=0)
        self.run_attempt(role='sort', outcome='SPLIT')
        self.result(rid, 'failure', 'item split again')

    def test_split_threshold_pending_and_resplit(self):
        rid = self.run_attempt(role='plan', outcome='SPLIT')
        self.result(rid, 'pending')
        builds = []
        for n in range(2, 6):
            self.led.upsert_item('p', n, parent=1)
        for n in range(2, 6):
            self.result(rid, 'pending')
            self.advance()
            builds.append(self.run_attempt(number=n))
        self.result(rid, 'success')
        self.led.update_run(builds[0], outcome='no status line')
        self.result(rid, 'success')  # exactly 75%
        self.led.update_run(builds[1], outcome='no status line')
        self.result(rid, 'failure')
        self.run_attempt(number=2, role='sort', outcome='SPLIT')
        self.result(rid, 'failure', 'child split again')

    def test_bounds_only_limit_report_not_evidence(self):
        rid = self.run_attempt()
        start = self.now
        self.advance()
        end = self.now
        fix = self.run_attempt(role='fix')
        rows = attempts(self.led, iso(start), end)
        self.assertEqual([r['run'] for r in rows], [rid])
        self.assertEqual(rows[0]['result'], 'failure')
        self.assertEqual([r['run'] for r in attempts(self.led, end)], [fix])

    def test_old_and_other_project_evidence_does_not_blame_new_run(self):
        self.led.event('review_verdict', 'p', 1, {'verdict': 'fail'})
        self.led.event('revert_requested', 'p', 1)
        self.advance()
        rid = self.run_attempt()
        self.advance()
        self.led.event('revert_requested', 'other', 1)
        self.run_attempt(project='other', role='fix')
        self.result(rid, 'success')

    def test_historical_revert_outbox_and_unexecuted_actions(self):
        rid = self.run_attempt()
        self.advance()
        self.led.con.execute(
            "INSERT INTO console_actions(kind,project,number,created_at,due_at) "
            "VALUES ('revert','p',1,?,?)", (iso(self.now), iso(self.now)))
        self.result(rid, 'success')
        self.led.con.execute("UPDATE console_actions SET status='cancelled'")
        self.result(rid, 'success')
        self.led.con.execute("UPDATE console_actions SET status='done',done_at=?",
                             (iso(self.now),))
        self.result(rid, 'failure', 'revert')

    def test_excluded_fix_remains_defect_evidence(self):
        for number, (outcome, reason, why) in enumerate((
            ('launch failed: missing', None, 'launch failed'),
            ('setup failed', 'setup-failed', 'setup failed'),
            ('not claimed', None, 'not claimed'),
            ('BLOCKED missing credentials', None, 'BLOCKED'),
            ('NEEDS-YOU', None, 'NEEDS-YOU'),
            *[('YIELDED', reason, reason)
              for reason in ('quota', 'preempted', 'closed', 'parked')],
        ), start=1):
            with self.subTest(why=why):
                for role in ('build', 'fix'):
                    with self.subTest(role=role):
                        rid = self.run_attempt(number=number, role=role)
                        self.result(rid, 'success')
                        self.advance()
                        fix = self.run_attempt(number=number, role='fix',
                                               status='running', ended_at=None)
                        self.result(rid, 'failure', 'later fix run')
                        self.led.update_run(fix, status='ended', ended_at=iso(self.now),
                                            outcome=outcome, stop_reason=reason, exit_code=1)
                        self.result(fix, 'excluded', why)
                        self.result(rid, 'failure', 'later fix run')
                        self.advance()

    def test_uat_shipping_timestamp_supports_bug_evidence(self):
        rid = self.run_attempt()
        self.advance()
        self.led.add_uat('p', 1, 50, 'sha', 'Title', 'Check')
        self.bug(body='Regression in #50')
        self.result(rid, 'failure', 'bug within 14 days')

    def test_release_shipping_timestamp_supports_bug_evidence(self):
        rid = self.run_attempt()
        self.advance()
        self.led.con.execute(
            "INSERT INTO release_items(project,number,pr,shipped_at) VALUES ('p',1,50,?)",
            (iso(self.now),))
        self.bug(body='Regression in #50')
        self.result(rid, 'failure', 'bug within 14 days')

    def test_delayed_merge_discovery(self):
        rid = self.run_attempt()
        self.advance()  # Monday
        merge_time = self.now
        self.advance()  # Tuesday
        self.bug(body='Regression in #50', days=0)
        self.advance()  # Wednesday
        # Sync discovers merge on Wednesday but records actual merge time (Monday)
        self.led.add_uat('p', 1, 50, 'sha', 'Title', 'Check', shipped_at=iso(merge_time))
        self.result(rid, 'failure', 'bug within 14 days')

    def test_read_only_repeatable_and_late_evidence_reclassifies(self):
        rid = self.run_attempt()
        before = self.led.con.total_changes
        self.assertEqual(attempts(self.led, None), attempts(self.led, None))
        self.assertEqual(self.led.con.total_changes, before)
        self.result(rid, 'success')
        self.ship()
        self.bug()
        self.result(rid, 'failure')
