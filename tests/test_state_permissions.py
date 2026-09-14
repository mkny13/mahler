"""State on disk is user-only (issue #75): 0700 directories, 0600 db and tick
lock — the pattern backup.py already uses for dumps. Every creation site is
exercised under a deliberately permissive umask (0o022) to prove the modes are
explicit, not inherited."""

import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from mahler import config, scheduler
from mahler.ledger import Ledger


def mode_of(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class EnsurePrivateDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Point STATE at the tmp dir so the climb-up-and-tighten behaviour is
        # exercised without ever touching the real ~/.mahler.
        self.patch = mock.patch.object(config, "STATE", self.tmp.name)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.old_umask = os.umask(0o022)
        self.addCleanup(os.umask, self.old_umask)

    def test_new_nested_dirs_under_state_are_user_only(self):
        target = os.path.join(self.tmp.name, "runs", "42")
        config.ensure_private_dir(target)
        self.assertEqual(mode_of(target), 0o700)
        self.assertEqual(mode_of(os.path.dirname(target)), 0o700)
        self.assertEqual(mode_of(self.tmp.name), 0o700)

    def test_preexisting_loose_dirs_are_tightened(self):
        for d in (self.tmp.name, os.path.join(self.tmp.name, "loose")):
            os.makedirs(d, exist_ok=True)
        os.chmod(self.tmp.name, 0o755)
        os.chmod(os.path.join(self.tmp.name, "loose"), 0o755)
        config.ensure_private_dir(os.path.join(self.tmp.name, "loose"))
        self.assertEqual(mode_of(self.tmp.name), 0o700)
        self.assertEqual(mode_of(os.path.join(self.tmp.name, "loose")), 0o700)

    def test_paths_outside_state_get_the_leaf_only(self):
        outside = tempfile.mkdtemp(dir=os.path.dirname(self.tmp.name))
        self.addCleanup(lambda: shutil.rmtree(outside, ignore_errors=True))
        os.chmod(outside, 0o755)
        target = os.path.join(outside, "wt")
        config.ensure_private_dir(target)
        self.assertEqual(mode_of(target), 0o700)
        # ancestors outside STATE are not ours to chmod
        self.assertEqual(mode_of(outside), 0o755)


class StateFilePermissionsTests(unittest.TestCase):
    """The Done-when checks from issue #75."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "state")
        self.db = os.path.join(self.home, "mahler.db")
        self.patches = [
            mock.patch.object(config, "STATE", self.home),
            mock.patch.object(config, "DB_PATH", self.db),
            mock.patch.object(config, "LOCK_PATH", os.path.join(self.home, "tick.lock")),
            mock.patch.object(config, "RUNS_DIR", os.path.join(self.home, "runs")),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        # Deliberately permissive: the modes must be explicit, not umask luck.
        self.old_umask = os.umask(0o022)
        self.addCleanup(os.umask, self.old_umask)

    def test_state_dir_created_0700_by_take_lock_and_lock_0600(self):
        fh = scheduler.take_lock()
        try:
            self.assertIsNotNone(fh)
            self.assertEqual(mode_of(self.home), 0o700)
            self.assertEqual(mode_of(os.path.join(self.home, "tick.lock")), 0o600)
        finally:
            if fh:
                fh.close()

    def test_state_dir_and_lock_tightened_when_preexisting_loose(self):
        os.makedirs(self.home, exist_ok=True)
        os.chmod(self.home, 0o755)
        with open(os.path.join(self.home, "tick.lock"), "w") as fh:
            fh.write("old")
        os.chmod(os.path.join(self.home, "tick.lock"), 0o644)
        fh = scheduler.take_lock()
        try:
            self.assertEqual(mode_of(self.home), 0o700)
            self.assertEqual(mode_of(os.path.join(self.home, "tick.lock")), 0o600)
        finally:
            if fh:
                fh.close()

    def test_db_dir_0700_and_db_file_0600_on_creation(self):
        led = Ledger(self.db)
        try:
            led.set_kv("x", "1")     # forces the WAL sidecar into existence
        finally:
            led.close()
        self.assertEqual(mode_of(self.home), 0o700)
        self.assertEqual(mode_of(self.db), 0o600)

    def test_loose_preexisting_db_is_tightened(self):
        os.makedirs(self.home, exist_ok=True)
        with open(self.db, "wb"):
            pass
        os.chmod(self.db, 0o644)
        os.chmod(self.home, 0o755)
        led = Ledger(self.db)
        led.close()
        self.assertEqual(mode_of(self.db), 0o600)
        self.assertEqual(mode_of(self.home), 0o700)


if __name__ == "__main__":
    unittest.main()
