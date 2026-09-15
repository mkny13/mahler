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


if __name__ == "__main__":
    unittest.main()
