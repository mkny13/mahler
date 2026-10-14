import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SK = ".agents/skills"
SNAP = ".agents/skills-archive/_snapshots"


def git(repo, *a):
    subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True)


def put(repo, rel, data):
    p = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write(data)


def gate(repo, base, head):
    return subprocess.run([sys.executable, "-m", "mahler.consolidation_gate", "--repo", repo,
                           "--base", base, "--head", head], cwd=ROOT,
                          capture_output=True, text=True)


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.r = self.tmp.name
        git(self.r, "init", "-q")
        git(self.r, "config", "user.email", "t@t")
        git(self.r, "config", "user.name", "t")
        put(self.r, f"{SK}/lesson-a/SKILL.md", "old\n")
        self.commit("base")
        self.base = self.rev()

    def commit(self, m):
        git(self.r, "add", "-A")
        git(self.r, "commit", "-q", "-m", m)

    def rev(self):
        return subprocess.run(["git", "-C", self.r, "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()

    def test_rewrite_without_snapshot_fails(self):
        put(self.r, f"{SK}/lesson-a/SKILL.md", "new\n")
        self.commit("edit")
        self.assertEqual(gate(self.r, self.base, self.rev()).returncode, 1)

    def test_rewrite_with_snapshot_passes(self):
        put(self.r, f"{SK}/lesson-a/SKILL.md", "new\n")
        put(self.r, f"{SNAP}/t1/lesson-a/SKILL.md", "old\n")
        self.commit("edit")
        self.assertEqual(gate(self.r, self.base, self.rev()).returncode, 0)

    def test_delete_without_snapshot_fails(self):
        git(self.r, "rm", "-rq", f"{SK}/lesson-a")
        self.commit("rm")
        self.assertEqual(gate(self.r, self.base, self.rev()).returncode, 1)

    def test_intact_archive_passes(self):
        git(self.r, "mv", f"{SK}/lesson-a", ".agents/lesson-a-tmp")
        os.makedirs(os.path.join(self.r, ".agents/skills-archive"))
        git(self.r, "mv", ".agents/lesson-a-tmp", ".agents/skills-archive/lesson-a")
        self.commit("archive")
        self.assertEqual(gate(self.r, self.base, self.rev()).returncode, 0)

    def test_no_change_passes(self):
        self.assertEqual(gate(self.r, self.base, self.base).returncode, 0)

    def test_bad_ref_fails_closed(self):
        self.assertEqual(gate(self.r, "nonexistent", self.base).returncode, 2)


if __name__ == "__main__":
    unittest.main()
