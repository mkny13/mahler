"""Backups (DESIGN D12): dump, verify, prune — with fake pg tools, no database."""

import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta

from mahler import backup
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


if __name__ == "__main__":
    unittest.main()
