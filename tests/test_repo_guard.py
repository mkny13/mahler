"""Repo guard (mahler#588): tracked browser-profile state fails CI.

The guard sees tracked *names* only — every test below feeds synthetic path
lists for files that do not exist, which is only possible because the guard
never opens or stats what it inspects.
"""
import io
import os
import unittest
from contextlib import redirect_stdout

from mahler import repo_guard

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_guard(paths):
    """main() over a synthetic tracked-path list; returns (code, output)."""
    out = io.StringIO()
    with redirect_stdout(out):
        code = repo_guard.main(paths=paths)
    return code, out.getvalue()


class TestRepoGuard(unittest.TestCase):
    def test_clean_tracked_list_passes(self):
        clean = [
            "README.md",
            "mahler/repo_guard.py",
            "tests/test_repo_guard.py",
            "docs/console/index.html",
            ".github/workflows/ci.yml",
        ]
        code, out = run_guard(clean)
        self.assertEqual(code, 0)
        self.assertEqual(repo_guard.violations(clean), [])

    def test_each_prohibited_basename_fails_anywhere(self):
        for name in repo_guard.PROHIBITED_BASENAMES:
            for path in (name, f"some/profile/dir/{name}"):
                with self.subTest(path=path):
                    self.assertTrue(repo_guard._prohibited(path))

    def test_journal_and_sqlite_companions_fail(self):
        for path in (
            "Cookies-journal",
            "profile/Default/Login Data-journal",
            "profile/Default/History-wal",
            "profile/Default/Local State-shm",
            "profile/Default/Device Bound Sessions-journal",
        ):
            with self.subTest(path=path):
                self.assertTrue(repo_guard._prohibited(path))

    def test_similar_but_distinct_names_pass(self):
        for path in (
            "Cookiecutter",
            "docs/history.md",
            "tests/test_history.py",
            "browsing-history-2026.md",
            "session-cookies.txt",
            "Login Data (archived).txt",
        ):
            with self.subTest(path=path):
                self.assertFalse(repo_guard._prohibited(path))

    def test_root_calendar_paths_fail_whatever_they_are(self):
        for path in (
            ".calendar-chrome3",                      # the profile directory
            ".calendar-chrome3/Default/Cookies",      # nested profile state
            ".calendar-chrome3/Default/History",
            ".calendar-chrome3.log",
            ".calendar-other/Local State-journal",
        ):
            with self.subTest(path=path):
                self.assertTrue(repo_guard._prohibited(path))

    def test_nested_calendar_state_deep_in_the_tree_fails_by_basename(self):
        # A profile copied under a non-ignored directory is still caught by
        # the basename rule.
        self.assertTrue(repo_guard._prohibited("snapshots/.calendar-x/History"))

    def test_main_fails_nonzero_and_names_only_paths(self):
        paths = [
            ".calendar-chrome3/Default/Cookies",
            ".calendar-chrome3/Default/Cookies-journal",
            "README.md",
        ]
        code, out = run_guard(paths)
        self.assertEqual(code, 1)
        for path in paths[:2]:
            self.assertIn(path, out)
        self.assertIn("never open or quote their contents", out)

    def test_main_fails_on_each_prohibited_class(self):
        for path in (
            ".calendar-chrome3",
            ".calendar-chrome3/Local State",
            "profile/Login Data",
            "profile/History-journal",
            "profile/Device Bound Sessions",
        ):
            with self.subTest(path=path):
                self.assertEqual(run_guard([path])[0], 1)

    def test_guard_never_touches_the_filesystem(self):
        # Synthetic names of files that do not exist anywhere: if the guard
        # opened or even stat'ed candidates, these would fail.
        self.assertEqual(
            run_guard(["no/such/dir/Cookies", "no/such/Local State"])[0], 1
        )

    def test_tracked_paths_reads_the_real_index(self):
        # Integration: the same command CI runs, against this checkout.
        paths = repo_guard.tracked_paths(REPO_ROOT)
        self.assertIn("tests/test_ledger.py", paths)
        self.assertEqual(repo_guard.violations(paths), [])

    def test_main_root_outside_a_repo_exits_two(self):
        import tempfile
        from contextlib import redirect_stderr

        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with redirect_stderr(err):
                code = repo_guard.main(root=tmp)
            self.assertEqual(code, 2)
            self.assertIn("git ls-files failed", err.getvalue())


if __name__ == "__main__":
    unittest.main()
