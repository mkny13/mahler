"""One-time capacity-pause migration must also run on unchanged GitHub polls."""

import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler import config, scheduler, sync
from mahler.ledger import Ledger, iso


class CapacityMigrationTests(unittest.TestCase):
    def setUp(self):
        self.led = Ledger(":memory:", clock=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc))
        self.addCleanup(self.led.close)
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["projects"]["x"] = {"repo": "x/y", "path": "."}
        self.ctx = scheduler.Ctx(cfg, self.led, dry_run=False)
        self.gh = mock.Mock()
        self.gh.issues_changed.return_value = (False, "etag")
        self.ctx._gh["x/y"] = self.gh
        self.led.set_kv("depends_format:x", "4")
        self.led.set_kv("unmerged_reconciled:x", "1")
        self.led.set_kv("release_baseline_checked:x", "1")

    def test_304_migrates_only_known_capacity_questions_once(self):
        questions = {
            1: "No fix run could start for PR #88 after 120 minutes: no platform",
            2: "Please provide credentials",
            3: "Review waiting for your decision about the product",
            4: "No fix run could start for PR #99 after 120 minutes: no platform",
            5: "PR #88: CI still running after 120 min",
        }
        for n, question in questions.items():
            self.led.set_state("x", n, "needs_you", pr=88, question=question)
        sync.sync(self.ctx, "x")
        self.gh.open_issues.assert_not_called()
        self.assertEqual(json.loads(self.led.get_kv("capacity-stranded:x#1"))["role"], "fix")
        for n in (2, 3, 4, 5):
            self.assertFalse(self.led.get_kv(f"capacity-stranded:x#{n}"))
            self.assertEqual(self.led.item("x", n)["question"], questions[n])
        self.led.set_kv("capacity-stranded:x#1", "")
        sync.sync(self.ctx, "x")
        self.assertFalse(self.led.get_kv("capacity-stranded:x#1"))
        self.assertEqual(self.led.item("x", 1)["state"], "needs_you")

    def test_dry_run_does_not_consume_migration(self):
        self.ctx.dry_run = True
        sync.sync(self.ctx, "x")
        self.assertFalse(self.led.get_kv("capacity-stranded-migrated:x"))


class MissingScopeTests(unittest.TestCase):
    def setUp(self):
        CapacityMigrationTests.setUp(self)
        self.ctx.cfg["projects"]["x"].update(scope="label", scope_label="custom")
        self.gh.issues_changed.return_value = (True, "new-etag")
        self.gh.blocked_by_of.return_value = []

    def issue(self, number, hours, labels=()):
        return {"number": number, "title": "Example", "body": "",
                "createdAt": iso(self.led.now() - timedelta(hours=hours)),
                "labels": [{"name": label} for label in labels], "comments": [],
                "authorAssociation": "OWNER"}

    def test_counts_only_over_24_hours_without_configured_label(self):
        self.gh.open_issues.return_value = [
            self.issue(1, 25), self.issue(2, 23), self.issue(3, 24),
            self.issue(4, 25, ["custom"]), self.issue(5, 25, ["mahler"])]
        sync.sync(self.ctx, "x")
        self.assertEqual(self.led.get_kv("missing_scope:x"), "2")
        self.assertEqual([i["number"] for i in self.led.items("x")], [4])
        self.gh.add_label.assert_not_called()
        self.gh.open_issues.return_value = []
        self.gh.issue_state.return_value = "CLOSED"
        sync.sync(self.ctx, "x")
        self.assertEqual(self.led.get_kv("missing_scope:x"), "0")

    def test_all_scope_and_dry_run_do_not_record_counts(self):
        self.gh.open_issues.return_value = []
        self.ctx.dry_run = True
        sync.sync(self.ctx, "x")
        self.assertIsNone(self.led.get_kv("missing_scope:x"))
        self.ctx.dry_run = False
        self.ctx.cfg["projects"]["x"]["scope"] = "all"
        sync.sync(self.ctx, "x")
        self.assertIsNone(self.led.get_kv("missing_scope:x"))

    def test_failed_listing_preserves_latest_count(self):
        from mahler.gh import GHError
        self.led.set_kv("missing_scope:x", "7")
        self.gh.open_issues.side_effect = GHError("offline")
        with self.assertRaises(GHError):
            sync.sync(self.ctx, "x")
        self.assertEqual(self.led.get_kv("missing_scope:x"), "7")


class TrustGatedIntakeTests(unittest.TestCase):
    def setUp(self):
        CapacityMigrationTests.setUp(self)
        self.gh.issues_changed.return_value = (True, "new-etag")
        self.gh.blocked_by_of.return_value = []
        self.gh.authenticated_login.return_value = "configured-user"

    def issue(self, number, *, association="NONE", login="outsider", labels=(),
              body=""):
        return {"number": number, "title": f"Issue {number}", "body": body,
                "createdAt": iso(self.led.now()),
                "labels": [{"name": label} for label in labels], "comments": [],
                "authorLogin": login, "authorAssociation": association}

    def test_trusted_associations_repository_owner_and_configured_login_are_admitted(self):
        self.ctx.cfg["projects"]["x"]["scope"] = "all"
        self.gh.open_issues.return_value = [
            self.issue(1, association="OWNER"),
            self.issue(2, association="MEMBER"),
            self.issue(3, association="COLLABORATOR"),
            self.issue(4, login="x"),
            self.issue(5, login="configured-user"),
        ]
        sync.sync(self.ctx, "x")
        self.assertEqual({item["number"]: item["state"] for item in self.led.items("x")},
                         {n: "inbox" for n in range(1, 6)})
        self.gh.authenticated_login.assert_called_once_with()

    def test_untrusted_issue_is_held_before_forged_parent_and_state_labels(self):
        self.ctx.cfg["projects"]["x"].update(scope="label", scope_label="custom")
        parent = self.issue(10, association="OWNER", labels=["custom"])
        child = self.issue(11, body="Part of #10", labels=["mahler:ready"])
        self.gh.open_issues.return_value = [parent, child]
        sync.sync(self.ctx, "x")

        held = self.led.item("x", 11)
        self.assertEqual(held["state"], "parked")
        self.assertIsNone(held["parent"])
        self.assertNotIn(held["state"], {"inbox", "ready", "working"})
        state_events = [row["detail"] for row in self.led.q(
            "SELECT detail FROM events WHERE project='x' AND number=11 AND kind='state'")]
        self.assertTrue(any("untrusted issue author @outsider (NONE)" in detail
                            for detail in state_events))
        self.assertEqual(self.led.q("SELECT * FROM runs WHERE project='x' AND number=11"), [])

        child["labels"].append({"name": "custom"})
        sync.sync(self.ctx, "x")
        admitted = self.led.item("x", 11)
        self.assertEqual(admitted["state"], "ready")
        self.assertEqual(admitted["parent"], 10)
        self.assertEqual(self.led.get_kv("intake_hold:x#11"), "")

    def test_mahler_state_label_is_never_an_opt_in_even_if_configured(self):
        self.ctx.cfg["projects"]["x"].update(scope="all", scope_label="mahler:ready")
        self.gh.open_issues.return_value = [
            self.issue(12, labels=["mahler:ready"]),
        ]
        sync.sync(self.ctx, "x")
        self.assertEqual(self.led.item("x", 12)["state"], "parked")


class RepositoryAccessAlertTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from contextlib import ExitStack
        from pathlib import Path
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.path = str(Path(tmp) / "ledger.db")
        self.led = Ledger(self.path)
        self.addCleanup(lambda: self.led.close())
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["projects"] = {
            name: {"enabled": True, "repo": f"org/{name}", "path": tmp}
            for name in ("work", "home")}
        self.clients = {name: mock.Mock() for name in self.cfg["projects"]}
        for name, client in self.clients.items():
            client.issues_changed.return_value = (False, "etag")
            self.led.set_kv(f"depends_format:{name}", "4")
            self.led.set_kv(f"unmerged_reconciled:{name}", "1")
            self.led.set_kv(f"release_baseline_checked:{name}", "1")
        # Exercise the real tick sync boundary and real sync (including 304),
        # isolating every unrelated pass from machine state and external services.
        for target in (
            "outbox.drain", "compute_burst", "watchdog", "failures.backfill",
            "expire", "close_finished_parents", "refresh_usage", "resets.spend_banked",
            "relearn", "warmup_pass", "queue_maintenance", "platform_audit.queue",
            "practices_audit.queue", "schedule", "record_holds", "ship",
            "capacity.Observer.observe_paused", "capacity.Observer.flush",
            "mirror_labels", "backup.run_ledger", "digest.maybe_send", "janitor.maybe_run",
        ):
            self.stack.enter_context(mock.patch("mahler.scheduler." + target))
        self.stack.enter_context(mock.patch("mahler.scheduler.relearn_due", return_value=False))
        self.stack.enter_context(mock.patch("mahler.scheduler._project_ok", return_value=True))
        self.send = self.stack.enter_context(mock.patch("mahler.scheduler.notify.send"))

    def tick(self, work=None, home=None, dry_run=False):
        from mahler.gh import GHError
        for name, error in (("work", work), ("home", home)):
            self.clients[name].issues_changed.side_effect = GHError(error) if error else None
        ctx = scheduler.Ctx(self.cfg, self.led, dry_run=dry_run)
        ctx._gh = {f"org/{name}": client for name, client in self.clients.items()}
        scheduler.tick(ctx)
        return ctx

    def test_outage_restart_recovery_and_project_isolation(self):
        self.tick("HTTP 404: Not Found")
        self.send.assert_not_called()
        self.tick("GraphQL: Could not resolve to a Repository named work")
        self.assertEqual(self.send.call_count, 1)
        args, kwargs = self.send.call_args
        self.assertIn("work", args[1])
        self.assertIn("org/work", args[2])
        self.assertIsNone(kwargs["click"])
        self.assertNotIn("GraphQL", args[2])
        self.tick("http 404", "NOT FOUND")
        self.assertEqual(self.send.call_count, 1)
        self.led.close()
        self.led = Ledger(self.path)
        self.tick("HTTP 404", "not found")
        self.assertEqual(self.send.call_count, 2)  # home's independent second failure
        self.assertEqual(self.clients["home"].issues_changed.call_count, 4)
        self.tick("timeout")
        self.tick("HTTP 404")
        self.tick("HTTP 404")
        self.assertEqual(self.send.call_count, 2)  # unrelated error does not re-arm
        ctx = self.tick()
        self.assertTrue(any("work: GitHub unchanged (304)" in line for line in ctx.lines))
        self.assertFalse(self.led.get_kv("sync_access:work"))
        self.tick("HTTP 404")
        self.assertEqual(self.send.call_count, 2)
        self.tick("HTTP 404")
        self.assertEqual(self.send.call_count, 3)

    def test_nonmatching_breaks_streak_and_dry_run_is_read_only(self):
        for error in ("timeout", "issue 404 failed", "HTTP 4040", "HTTP 500"):
            self.tick("HTTP 404")
            self.tick(error)
            self.assertEqual(json.loads(self.led.get_kv("sync_access:work"))["failures"], 0)
        self.send.assert_not_called()
        previous = self.led.get_kv("sync_access:work")
        for _ in range(3):
            self.tick("HTTP 404", "HTTP 404", dry_run=True)
        self.assertEqual(self.led.get_kv("sync_access:work"), previous)
        self.assertIsNone(self.led.get_kv("sync_access:home"))
        self.send.assert_not_called()
        self.tick("HTTP 404")
        self.send.assert_not_called()
        self.tick("HTTP 404")
        self.send.assert_called_once()

    def test_notification_exception_does_not_stop_sync_or_repeat(self):
        self.send.side_effect = RuntimeError("delivery unavailable")
        self.tick("HTTP 404")
        self.tick("HTTP 404")
        self.tick("HTTP 404")
        self.send.assert_called_once()
        self.assertEqual(self.clients["home"].issues_changed.call_count, 3)


class OpenCompletionTests(unittest.TestCase):
    def setUp(self):
        CapacityMigrationTests.setUp(self)
        self.ctx.cfg['projects']['x']['scope'] = 'all'
        self.gh.issues_changed.return_value = (True, 'new-etag')
        self.gh.blocked_by_of.return_value = []
        self.gh.closing_prs.return_value = []
        self.gh.pr_view.return_value = {'state': 'CLOSED', 'mergedAt': None}
        self.issue = {'number': 1, 'title': 'Lost work', 'body': '',
                      'createdAt': iso(self.led.now()),
                      'labels': [{'name': 'mahler:working'}], 'comments': [],
                      'authorAssociation': 'OWNER'}
        self.gh.open_issues.return_value = [self.issue]

    def completed(self, state='done', pr=88):
        self.led.set_state('x', 1, state, pr=pr, branch='abandoned',
                           attempts=3, mirror='mahler:working')

    def test_closed_unmerged_recovers_both_states_once(self):
        for state in ('done', 'shipped'):
            with self.subTest(state=state):
                self.completed(state)
                self.gh.comment.reset_mock()
                sync.sync(self.ctx, 'x')
                item = self.led.item('x', 1)
                self.assertEqual(item['state'], 'ready')
                self.assertIsNone(item['pr'])
                self.assertIsNone(item['branch'])
                self.assertEqual(item['attempts'], 0)
                self.gh.comment.assert_called_once()
                self.assertIn('not shipped: PR #88 closed without merging',
                              self.gh.comment.call_args.args[1])
                count = len(self.led.q("SELECT * FROM events WHERE kind='completion_unmerged'"))
                sync.sync(self.ctx, 'x')
                self.gh.comment.assert_called_once()
                self.assertEqual(len(self.led.q("SELECT * FROM events WHERE kind='completion_unmerged'")), count)

    def test_merged_or_open_pr_is_not_requeued(self):
        for view in ({'state': 'MERGED'}, {'state': 'CLOSED', 'mergedAt': '2026-10-01T00:00:00Z'},
                     {'state': 'OPEN'}, {}):
            with self.subTest(view=view):
                self.completed()
                self.gh.pr_view.return_value = view
                sync.sync(self.ctx, 'x')
                self.assertEqual(self.led.item('x', 1)['state'], 'done')
        self.gh.comment.assert_not_called()

    def test_merged_replacement_is_not_requeued(self):
        self.completed()
        self.gh.closing_prs.return_value = [{'number': 99, 'state': 'MERGED'}]
        sync.sync(self.ctx, 'x')
        self.assertEqual(self.led.item('x', 1)['state'], 'done')
        self.gh.comment.assert_not_called()

    def test_missing_pr_reported_once_without_requeue(self):
        self.completed(pr=None)
        sync.sync(self.ctx, 'x')
        sync.sync(self.ctx, 'x')
        self.assertEqual(self.led.item('x', 1)['state'], 'done')
        self.assertEqual(len(self.led.q("SELECT * FROM events WHERE kind='completion_missing_pr'")), 1)
        self.gh.pr_view.assert_not_called()
        self.gh.comment.assert_not_called()

    def test_upgrade_forces_full_sync_on_304(self):
        self.completed()
        self.led.set_kv('unmerged_reconciled:x', '')
        self.gh.issues_changed.return_value = (False, 'etag')
        sync.sync(self.ctx, 'x')
        self.assertEqual(self.led.item('x', 1)['state'], 'ready')
        sync.sync(self.ctx, 'x')
        self.gh.open_issues.assert_called_once()

    def test_lookup_and_comment_errors_preserve_state_and_retry(self):
        from mahler.gh import GHError
        for method in (self.gh.pr_view, self.gh.closing_prs, self.gh.comment):
            with self.subTest(method=method):
                self.completed()
                method.side_effect = GHError('offline')
                with self.assertRaises(GHError):
                    sync.sync(self.ctx, 'x')
                self.assertEqual(self.led.item('x', 1)['state'], 'done')
                self.assertEqual(self.led.get_kv('etag:x'), '')
                method.side_effect = None
                sync.sync(self.ctx, 'x')
                self.assertEqual(self.led.item('x', 1)['state'], 'ready')

    def test_existing_comment_and_dry_run(self):
        self.completed()
        self.ctx.dry_run = True
        sync.sync(self.ctx, 'x')
        self.assertEqual(self.led.item('x', 1)['state'], 'done')
        self.gh.pr_view.assert_not_called()
        self.ctx.dry_run = False
        self.issue['comments'] = [{'body': '<!-- mahler:agent -->\n<!-- mahler:unmerged-completion:88 -->',
                                   'createdAt': iso(self.led.now())}]
        sync.sync(self.ctx, 'x')
        self.assertEqual(self.led.item('x', 1)['state'], 'ready')
        self.gh.comment.assert_not_called()


class BacklogSnapshotSyncTests(unittest.TestCase):
    """sync records the net-backlog snapshot from data in hand (mahler#1104)."""

    def setUp(self):
        MissingScopeTests.setUp(self)

    issue = MissingScopeTests.issue

    def test_full_sync_records_in_scope_count_for_label_projects(self):
        self.gh.open_issues.return_value = [
            self.issue(1, 25, ["custom"]), self.issue(2, 25, ["custom"]),
            self.issue(3, 25), self.issue(4, 1)]
        sync.sync(self.ctx, "x")
        self.assertEqual(self.led.last_backlog("x")["open_count"], 2)

    def test_all_scope_counts_every_open_issue_once_per_hour(self):
        self.ctx.cfg["projects"]["x"]["scope"] = "all"
        self.gh.open_issues.return_value = [self.issue(1, 25), self.issue(2, 1)]
        sync.sync(self.ctx, "x")
        self.gh.open_issues.return_value = [self.issue(1, 25)]
        sync.sync(self.ctx, "x")                    # same hour: throttled
        rows = self.led.q("SELECT open_count FROM backlog_snapshots WHERE project='x'")
        self.assertEqual([r["open_count"] for r in rows], [2])

    def test_304_reuses_last_count_without_fetching(self):
        self.led.record_backlog("x", 7, at=self.led.now() - timedelta(hours=2))
        self.gh.issues_changed.return_value = (False, "etag")
        sync.sync(self.ctx, "x")
        self.gh.open_issues.assert_not_called()
        rows = self.led.q("SELECT at, open_count FROM backlog_snapshots WHERE project='x'"
                          " ORDER BY at")
        self.assertEqual([(r["at"], r["open_count"]) for r in rows][-1],
                         (iso(self.led.now()), 7))
        self.assertEqual(len(rows), 2)

    def test_304_without_history_records_nothing(self):
        self.gh.issues_changed.return_value = (False, "etag")
        sync.sync(self.ctx, "x")
        self.assertIsNone(self.led.last_backlog("x"))

    def test_dry_run_records_nothing(self):
        self.ctx.dry_run = True
        self.gh.open_issues.return_value = [self.issue(1, 25, ["custom"])]
        sync.sync(self.ctx, "x")
        self.assertIsNone(self.led.last_backlog("x"))

    def test_raising_record_backlog_does_not_break_sync(self):
        self.gh.open_issues.return_value = [self.issue(4, 25, ["custom"])]
        with mock.patch.object(self.led, "record_backlog", side_effect=RuntimeError("disk")):
            sync.sync(self.ctx, "x")
        self.assertEqual([i["number"] for i in self.led.items("x")], [4])
        self.assertTrue(any("backlog snapshot failed: disk" in line for line in self.ctx.lines))
        self.gh.issues_changed.return_value = (False, "etag")
        self.led.record_backlog("x", 1, at=self.led.now() - timedelta(hours=2))
        with mock.patch.object(self.led, "record_backlog", side_effect=RuntimeError("disk")):
            sync.sync(self.ctx, "x")                # the 304 path too
