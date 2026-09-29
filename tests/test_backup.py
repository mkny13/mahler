"""Backups (DESIGN D12): dump, verify, prune — with fake pg tools, no database."""

import os
import stat
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch
from datetime import datetime, timedelta

from mahler import backup, scheduler
from mahler.ledger import Ledger

FAKE_DUMP = """#!/bin/sh
# records its environment so the test can check the password stayed off argv
env | grep '^PG' > "$(dirname "$0")/env.seen"
echo "$@" > "$(dirname "$0")/argv.seen"
while [ $# -gt 0 ]; do [ "$1" = "--file" ] && { shift; echo dumpdata > "$1"; }; shift; done
"""
FAKE_RESTORE = "#!/bin/sh\necho '; header'\necho '1; TABLE public set_logs'\n"


def script(d, name, body):
    p = os.path.join(d, name)
    with open(p, "w") as fh:
        fh.write(body)
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
    return p


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.env_file = os.path.join(d, ".env.prod")
        with open(self.env_file, "w") as fh:
            fh.write('DATABASE_URL_UNPOOLED="postgresql://owner:s3cret@db.example:5432/neondb?sslmode=require"\n')
        self.spec = {"name": "prod", "env_file": self.env_file, "url_var": "DATABASE_URL_UNPOOLED",
                     "pg_dump": script(d, "pg_dump", FAKE_DUMP),
                     "pg_restore": script(d, "pg_restore", FAKE_RESTORE)}
        self.root = os.path.join(d, "backups")

    def tearDown(self):
        self.tmp.cleanup()

    def test_dump_verified_private_and_password_off_argv(self):
        res = backup.backup_postgres("gw", self.spec, root=self.root)
        self.assertTrue(os.path.exists(res["path"]))
        self.assertEqual(res["entries"], 1)
        self.assertEqual(stat.S_IMODE(os.stat(res["path"]).st_mode), 0o600)
        # D12: dumps are 0600 in 0700 directories.
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.root, "gw")).st_mode), 0o700)
        argv = os.path.join(self.tmp.name, "argv.seen")
        env = os.path.join(self.tmp.name, "env.seen")
        with open(argv) as fh:
            argv = fh.read()
        self.assertNotIn("s3cret", argv)
        with open(env) as fh:
            seen = fh.read()
        self.assertIn("PGPASSWORD=s3cret", seen)
        self.assertIn("PGSSLMODE=require", seen)

    def test_failure_is_scrubbed(self):
        self.spec["pg_dump"] = script(self.tmp.name, "bad_dump",
                                      "#!/bin/sh\necho 'could not connect postgresql://owner:s3cret@x/db' >&2\nexit 1\n")
        with self.assertRaises(backup.BackupError) as cm:
            backup.backup_postgres("gw", self.spec, root=self.root)
        self.assertNotIn("s3cret", str(cm.exception))

    def test_pre_existing_loose_directory_is_tightened(self):
        dest = os.path.join(self.root, "gw")
        os.makedirs(dest, mode=0o755)
        os.chmod(dest, 0o755)
        res = backup.backup_postgres("gw", self.spec, root=self.root)
        self.assertEqual(stat.S_IMODE(os.stat(dest).st_mode), 0o700)
        self.assertTrue(os.path.exists(res["path"]))

    def test_retention(self):
        now = datetime(2026, 9, 12, 3, 0)
        stamps = [now - timedelta(days=i) for i in range(400)]
        keep = backup.keep_set(stamps)
        self.assertIn(stamps[0], keep)
        self.assertIn(stamps[13], keep)                 # 14 dailies
        self.assertLessEqual(len(keep), 14 + 8 + 12)
        self.assertNotIn(stamps[399], keep)

    def test_due_once_a_day_and_backs_off_after_failure(self):
        led = Ledger(":memory:")
        spec = {"name": "prod", "hour": 3}
        self.assertFalse(backup.due(led, "gw", spec, datetime(2026, 9, 12, 2, 59)))
        self.assertTrue(backup.due(led, "gw", spec, datetime(2026, 9, 12, 3, 1)))
        led.set_kv("backup:gw:prod", datetime(2026, 9, 12, 3, 1).isoformat())
        self.assertFalse(backup.due(led, "gw", spec, datetime(2026, 9, 12, 23, 0)))
        self.assertTrue(backup.due(led, "gw", spec, datetime(2026, 9, 13, 3, 5)))
        led.set_kv("backup:gw:prod:failed_at", datetime(2026, 9, 13, 3, 5).isoformat())
        self.assertFalse(backup.due(led, "gw", spec, datetime(2026, 9, 13, 3, 30)))
        self.assertTrue(backup.due(led, "gw", spec, datetime(2026, 9, 13, 4, 6)))


class BackupFailureTests(unittest.TestCase):
    def setUp(self):
        self.led = Ledger(":memory:")
        self.addCleanup(self.led.close)
        self.ctx = scheduler.Ctx({}, self.led)
        self.ctx.ping = Mock()
        self.now = datetime(2026, 9, 12, 12)
        clock = self.enterContext(patch("mahler.backup.datetime", wraps=datetime))
        clock.now.side_effect = lambda: self.now

    def assert_failure_backs_off(self, spec, name):
        key = f"backup:gw:{name}:failed_at"
        self.assertIsNone(backup.run(self.ctx, "gw", spec))
        self.assertEqual(self.led.get_kv(key), self.now.isoformat())
        self.assertIn("FAILED", self.ctx.lines[-1])
        self.assertIn(name, self.ctx.lines[-1])
        self.ctx.ping.assert_called_once()
        self.now += timedelta(minutes=59)
        self.assertIsNone(backup.run(self.ctx, "gw", spec))
        self.ctx.ping.assert_called_once()
        self.now += timedelta(minutes=1)
        self.assertIsNone(backup.run(self.ctx, "gw", spec))
        self.assertEqual(self.ctx.ping.call_count, 2)

    def test_missing_name_backs_off(self):
        self.assert_failure_backs_off({"hour": 0}, "<unnamed>")

    def test_corrupt_success_timestamp_backs_off(self):
        self.led.set_kv("backup:gw:prod", "not-a-date")
        self.assert_failure_backs_off({"name": "prod", "hour": 0}, "prod")

    def test_corrupt_failure_timestamp_backs_off(self):
        self.led.set_kv("backup:gw:prod:failed_at", "not-a-date")
        self.assert_failure_backs_off({"name": "prod", "hour": 0}, "prod")

    def test_invalid_hour_backs_off_before_validation(self):
        self.assert_failure_backs_off({"name": "prod", "hour": "noon"}, "prod")

    def test_non_table_spec_backs_off(self):
        self.assert_failure_backs_off("invalid", "<unnamed>")

    def test_forced_missing_name_is_reported(self):
        self.assertIsNone(backup.run(self.ctx, "gw", {}, force=True))
        self.ctx.ping.assert_called_once()

    def test_well_formed_run_then_skip_and_force(self):
        spec = {"name": "prod", "hour": 0}
        result = {"bytes": 2048, "entries": 1}
        with patch("mahler.backup.backup_postgres", return_value=result) as dump, \
                patch("mahler.backup.prune", return_value=[]) as prune:
            self.assertEqual(backup.run(self.ctx, "gw", spec), result)
            self.assertIsNone(backup.run(self.ctx, "gw", spec))
            dump.assert_called_once()
            prune.assert_called_once()
            self.assertEqual(backup.run(self.ctx, "gw", spec, force=True), result)
            self.assertEqual(dump.call_count, 2)
        self.ctx.ping.assert_not_called()

    def test_tick_continues_after_malformed_or_unexpected_backup_failure(self):
        for unexpected in (False, True):
            with self.subTest(unexpected=unexpected), ExitStack() as stack:
                project = {"name": "gw", "backups": [{"hour": 0}, {"name": "next"}]}
                stack.enter_context(patch("mahler.scheduler.config.enabled_projects",
                                         return_value=[project]))
                stack.enter_context(patch("mahler.scheduler._project_ok", return_value=True))
                for name in ("compute_burst", "watchdog", "sync", "expire",
                             "close_finished_parents", "refresh_usage", "queue_maintenance",
                             "platform_audit.queue", "schedule", "ship", "mirror_labels"):
                    stack.enter_context(patch(f"mahler.scheduler.{name}"))
                stack.enter_context(patch.object(self.led, "paused", return_value=True))
                digest = stack.enter_context(patch("mahler.scheduler.digest.maybe_send"))
                janitor = stack.enter_context(patch("mahler.scheduler.janitor.maybe_run"))
                stack.enter_context(patch("mahler.scheduler.backup.run_ledger",
                                          side_effect=RuntimeError("unexpected") if unexpected else None))
                if unexpected:
                    run = stack.enter_context(patch("mahler.scheduler.backup.run",
                                                    side_effect=[RuntimeError("unexpected"), None]))
                else:
                    run = stack.enter_context(patch("mahler.scheduler.backup.run", wraps=backup.run))
                    stack.enter_context(patch("mahler.backup.backup_postgres",
                                             return_value={"bytes": 1, "entries": 1}))
                    stack.enter_context(patch("mahler.backup.prune", return_value=[]))
                self.assertIs(scheduler.tick(self.ctx), self.ctx.lines)
                self.assertEqual(run.call_count, 2)
                digest.assert_called_once_with(self.ctx)
                janitor.assert_called_once_with(self.ctx)


class LedgerBackupTests(unittest.TestCase):
    """Backing up Mahler's own ledger (mahler#533) — temp-dir files only,
    never ~/.mahler."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.led_path = os.path.join(self.tmp.name, "mahler.db")
        self.led = Ledger(self.led_path)
        self.addCleanup(self.led.close)
        self.led.set_kv("hello", "world")
        self.root = os.path.join(self.tmp.name, "backups")

    def test_backup_is_verified_private_and_contains_live_data(self):
        res = backup.backup_ledger(self.led_path, root=self.root)
        self.assertTrue(os.path.exists(res["path"]))
        self.assertEqual(res["bytes"], os.path.getsize(res["path"]))
        self.assertEqual(stat.S_IMODE(os.stat(res["path"]).st_mode), 0o600)
        # D12: 0600 in 0700 directories.
        self.assertEqual(stat.S_IMODE(os.stat(self.root).st_mode), 0o700)
        self.assertEqual(backup.integrity_check(res["path"]), "ok")
        copy = Ledger(res["path"])
        try:
            self.assertEqual(copy.get_kv("hello"), "world")
        finally:
            copy.close()

    def test_pre_existing_loose_root_is_tightened(self):
        os.makedirs(self.root, mode=0o755)
        os.chmod(self.root, 0o755)
        backup.backup_ledger(self.led_path, root=self.root)
        self.assertEqual(stat.S_IMODE(os.stat(self.root).st_mode), 0o700)

    def test_missing_source_raises(self):
        with self.assertRaises(backup.BackupError):
            backup.backup_ledger(":memory:", root=self.root)
        with self.assertRaises(backup.BackupError):
            backup.backup_ledger(os.path.join(self.tmp.name, "nope.db"), root=self.root)

    def test_prune_keeps_policy_set(self):
        os.makedirs(self.root, exist_ok=True)
        now = datetime(2026, 9, 12, 3, 0)
        stamps = [now - timedelta(days=i) for i in range(40)]
        for s in stamps:
            open(os.path.join(self.root, f"mahler-{s.strftime(backup.STAMP)}.db"), "w").close()
        removed = backup.prune_ledger(root=self.root, daily=3, weekly=2, monthly=1)
        remaining = os.listdir(self.root)
        self.assertEqual(len(remaining) + len(removed), 40)
        self.assertLessEqual(len(remaining), 3 + 2 + 1)
        newest = f"mahler-{stamps[0].strftime(backup.STAMP)}.db"
        self.assertIn(newest, remaining)

    def test_optimize_and_checkpoint_does_not_raise(self):
        backup.optimize_and_checkpoint(self.led)

    def test_copy_off_disk_mirrors_file(self):
        dest_dir = os.path.join(self.tmp.name, "offsite")
        res = backup.backup_ledger(self.led_path, root=self.root)
        backup.copy_off_disk(res["path"], dest_dir)
        mirrored = os.path.join(dest_dir, os.path.basename(res["path"]))
        with open(res["path"], "rb") as fh:
            original = fh.read()
        with open(mirrored, "rb") as fh:
            self.assertEqual(fh.read(), original)


class RunLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.led_path = os.path.join(self.tmp.name, "mahler.db")
        self.led = Ledger(self.led_path)
        self.addCleanup(self.led.close)
        self.root = os.path.join(self.tmp.name, "backups")
        self.cfg = {"backup": {"hour": 0, "copy_to": ""}}
        self.ctx = scheduler.Ctx(self.cfg, self.led)
        self.ctx.ping = Mock()
        self.now = datetime(2026, 9, 12, 12)
        clock = self.enterContext(patch("mahler.backup.datetime", wraps=datetime))
        clock.now.side_effect = lambda: self.now

    def test_backs_up_once_a_day_and_records_kv(self):
        res = backup.run_ledger(self.ctx, root=self.root)
        self.assertIsNotNone(res)
        self.assertTrue(os.path.exists(res["path"]))
        self.assertEqual(self.led.get_kv("backup:mahler:ledger"), self.now.isoformat())
        self.ctx.ping.assert_not_called()
        self.assertIsNone(backup.run_ledger(self.ctx, root=self.root))  # already done today

    def test_checkpoints_the_live_connection(self):
        with patch.object(backup, "optimize_and_checkpoint") as opt:
            backup.run_ledger(self.ctx, root=self.root)
        opt.assert_called_once_with(self.led)

    def test_copy_to_mirrors_the_verified_backup(self):
        self.cfg["backup"]["copy_to"] = os.path.join(self.tmp.name, "offsite")
        res = backup.run_ledger(self.ctx, root=self.root)
        mirrored = os.path.join(self.cfg["backup"]["copy_to"], os.path.basename(res["path"]))
        self.assertTrue(os.path.exists(mirrored))

    def test_copy_to_failure_pings_but_keeps_the_backup(self):
        self.cfg["backup"]["copy_to"] = os.path.join(self.tmp.name, "offsite")
        with patch("mahler.backup.copy_off_disk", side_effect=OSError("disk full")):
            res = backup.run_ledger(self.ctx, root=self.root)
        self.assertIsNotNone(res)
        self.assertTrue(os.path.exists(res["path"]))
        self.ctx.ping.assert_called_once()
        self.assertEqual(self.led.get_kv("backup:mahler:ledger"), self.now.isoformat())

    def test_failed_integrity_check_pings_and_does_not_raise_or_record_success(self):
        with patch("mahler.backup.integrity_check", return_value="row 4 missing from index"):
            self.assertIsNone(backup.run_ledger(self.ctx, root=self.root))
        self.ctx.ping.assert_called_once()
        self.assertIsNone(self.led.get_kv("backup:mahler:ledger"))
        self.assertEqual(os.listdir(self.root) if os.path.isdir(self.root) else [], [])

    def test_missing_ledger_path_pings_without_raising(self):
        missing = os.path.join(self.tmp.name, "nope.db")
        self.assertIsNone(backup.run_ledger(self.ctx, led_path=missing, root=self.root, force=True))
        self.ctx.ping.assert_called_once()

    def test_tick_calls_run_ledger_once(self):
        with ExitStack() as stack:
            stack.enter_context(patch("mahler.scheduler.config.enabled_projects", return_value=[]))
            for name in ("compute_burst", "watchdog", "expire", "close_finished_parents",
                         "refresh_usage", "queue_maintenance", "platform_audit.queue",
                         "schedule", "ship", "mirror_labels", "digest.maybe_send", "janitor.maybe_run"):
                stack.enter_context(patch(f"mahler.scheduler.{name}"))
            stack.enter_context(patch.object(self.led, "paused", return_value=True))
            run_ledger = stack.enter_context(patch("mahler.scheduler.backup.run_ledger"))
            scheduler.tick(self.ctx)
        run_ledger.assert_called_once_with(self.ctx)


if __name__ == "__main__":
    unittest.main()
