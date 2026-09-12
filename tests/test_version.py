"""Tests for mahler version (version_info / format_version)."""

import os
import subprocess
import tempfile
import unittest

from mahler.version import format_version, version_info


def sh(cwd, *args):
    return subprocess.run(
        args, cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def write(path, text):
    with open(path, "w") as fh:
        fh.write(text)


class VersionInfoTests(unittest.TestCase):
    """Test version_info against real (temp) git repos."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.home = os.path.join(t, "home")
        os.makedirs(self.home)
        # Create a bare remote and a working clone.
        self.remote = os.path.join(t, "remote.git")
        self.repo = os.path.join(t, "repo")
        sh(t, "git", "init", "-q", "--bare", "-b", "main", self.remote)
        sh(t, "git", "clone", "-q", self.remote, self.repo)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            sh(self.repo, "git", "config", k, v)
        write(os.path.join(self.repo, "f.txt"), "init\n")
        sh(self.repo, "git", "add", ".")
        sh(self.repo, "git", "commit", "-qm", "init")
        sh(self.repo, "git", "push", "-q", "origin", "main")

    def tearDown(self):
        self.tmp.cleanup()

    def test_commit_is_reported(self):
        info = version_info(self.repo, self.home)
        self.assertTrue(info["is_git"])
        expected = sh(self.repo, "git", "rev-parse", "--short", "HEAD")
        self.assertEqual(info["commit"], expected)

    def test_no_known_good_file(self):
        info = version_info(self.repo, self.home)
        self.assertIsNone(info["known_good"])
        self.assertIsNone(info["kg_match"])

    def test_known_good_matches(self):
        sha = sh(self.repo, "git", "rev-parse", "--short", "HEAD")
        write(os.path.join(self.home, "known_good"), sha)
        info = version_info(self.repo, self.home)
        self.assertTrue(info["kg_match"])

    def test_known_good_does_not_match(self):
        write(os.path.join(self.home, "known_good"), "abc1234")
        info = version_info(self.repo, self.home)
        self.assertFalse(info["kg_match"])
        self.assertEqual(info["known_good"], "abc1234")

    def test_known_good_full_sha_matches_short(self):
        full = sh(self.repo, "git", "rev-parse", "HEAD")
        short = sh(self.repo, "git", "rev-parse", "--short", "HEAD")
        write(os.path.join(self.home, "known_good"), full)
        info = version_info(self.repo, self.home)
        # The short commit starts with part of the full known_good.
        self.assertTrue(info["kg_match"], f"{short} should prefix-match {full}")

    def test_behind_zero_when_up_to_date(self):
        info = version_info(self.repo, self.home)
        self.assertEqual(info["behind"], 0)

    def test_behind_nonzero(self):
        # Push a new commit to the remote via a second clone, then don't
        # pull in self.repo.  version_info should report behind > 0.
        clone2 = os.path.join(self.tmp.name, "clone2")
        sh(self.tmp.name, "git", "clone", "-q", self.remote, clone2)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            sh(clone2, "git", "config", k, v)
        write(os.path.join(clone2, "g.txt"), "ahead\n")
        sh(clone2, "git", "add", ".")
        sh(clone2, "git", "commit", "-qm", "ahead")
        sh(clone2, "git", "push", "-q", "origin", "main")
        info = version_info(self.repo, self.home)
        self.assertEqual(info["behind"], 1)

    def test_not_a_git_repo(self):
        plain = os.path.join(self.tmp.name, "plain")
        os.makedirs(plain)
        info = version_info(plain, self.home)
        self.assertFalse(info["is_git"])
        self.assertIsNone(info["commit"])
        self.assertIsNone(info["behind"])


class FormatVersionTests(unittest.TestCase):
    """Test format_version with synthetic dicts (no subprocess)."""

    def test_all_good(self):
        out = format_version({
            "commit": "abc1234", "is_git": True,
            "known_good": "abc1234", "kg_match": True,
            "behind": 0,
        })
        self.assertIn("abc1234", out)
        self.assertIn("matches", out)
        self.assertIn("up to date", out)

    def test_behind(self):
        out = format_version({
            "commit": "abc1234", "is_git": True,
            "known_good": "abc1234", "kg_match": True,
            "behind": 3,
        })
        self.assertIn("3 commits behind", out)

    def test_behind_one(self):
        out = format_version({
            "commit": "abc1234", "is_git": True,
            "known_good": "abc1234", "kg_match": True,
            "behind": 1,
        })
        self.assertIn("1 commit behind", out)

    def test_no_known_good(self):
        out = format_version({
            "commit": "abc1234", "is_git": True,
            "known_good": None, "kg_match": None,
            "behind": 0,
        })
        self.assertIn("no known-good recorded yet", out)

    def test_mismatch(self):
        out = format_version({
            "commit": "abc1234", "is_git": True,
            "known_good": "def5678", "kg_match": False,
            "behind": None,
        })
        self.assertIn("does not match", out)
        self.assertIn("def5678", out)
        self.assertIn("unknown", out)

    def test_not_git(self):
        out = format_version({
            "commit": None, "is_git": False,
            "known_good": None, "kg_match": None,
            "behind": None,
        })
        self.assertIn("not a git checkout", out)


if __name__ == "__main__":
    unittest.main()
