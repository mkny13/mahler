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
                "labels": [{"name": label} for label in labels], "comments": []}

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
