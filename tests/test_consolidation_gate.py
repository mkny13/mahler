import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SK = ".agents/skills"
SNAP = ".agents/skills-archive/_snapshots"
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "ci.yml")


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

    def add_lesson(self, name, content):
        put(self.r, f"{SK}/{name}/SKILL.md", content)
        self.commit(f"add {name}")
        self.base = self.rev()

    def rev(self):
        return subprocess.run(["git", "-C", self.r, "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()

    def test_rewrite_without_snapshot_fails(self):
        put(self.r, f"{SK}/lesson-a/SKILL.md", "new\n")
        self.commit("edit")
        result = gate(self.r, self.base, self.rev())
        self.assertEqual(result.returncode, 1, result.stderr)

    def test_rewrite_with_snapshot_passes(self):
        put(self.r, f"{SK}/lesson-a/SKILL.md", "new\n")
        put(self.r, f"{SNAP}/t1/lesson-a/SKILL.md", "old\n")
        self.commit("edit")
        result = gate(self.r, self.base, self.rev())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_snapshot_missing_affected_lesson_fails(self):
        self.add_lesson("lesson-b", "old-b\n")
        put(self.r, f"{SK}/lesson-a/SKILL.md", "new-a\n")
        put(self.r, f"{SK}/lesson-b/SKILL.md", "new-b\n")
        put(self.r, f"{SNAP}/t1/lesson-a/SKILL.md", "old\n")
        self.commit("edit both with partial snapshot")
        result = gate(self.r, self.base, self.rev())
        self.assertEqual(result.returncode, 1, result.stderr)

    def test_non_verbatim_snapshot_fails(self):
        put(self.r, f"{SK}/lesson-a/SKILL.md", "new\n")
        put(self.r, f"{SNAP}/t1/lesson-a/SKILL.md", "not old\n")
        self.commit("edit with incorrect snapshot")
        result = gate(self.r, self.base, self.rev())
        self.assertEqual(result.returncode, 1, result.stderr)

    def test_affected_lessons_split_across_timestamps_fail(self):
        self.add_lesson("lesson-b", "old-b\n")
        put(self.r, f"{SK}/lesson-a/SKILL.md", "new-a\n")
        put(self.r, f"{SK}/lesson-b/SKILL.md", "new-b\n")
        put(self.r, f"{SNAP}/t1/lesson-a/SKILL.md", "old\n")
        put(self.r, f"{SNAP}/t2/lesson-b/SKILL.md", "old-b\n")
        self.commit("split snapshots")
        result = gate(self.r, self.base, self.rev())
        self.assertEqual(result.returncode, 1, result.stderr)

    def test_unchanged_historical_snapshot_does_not_pass(self):
        put(self.r, f"{SNAP}/old/lesson-a/SKILL.md", "old\n")
        self.commit("add historical snapshot")
        self.base = self.rev()
        put(self.r, f"{SK}/lesson-a/SKILL.md", "new\n")
        self.commit("edit without new snapshot")
        result = gate(self.r, self.base, self.rev())
        self.assertEqual(result.returncode, 1, result.stderr)

    def test_delete_without_snapshot_fails(self):
        git(self.r, "rm", "-rq", f"{SK}/lesson-a")
        self.commit("rm")
        result = gate(self.r, self.base, self.rev())
        self.assertEqual(result.returncode, 1, result.stderr)

    def test_intact_archive_passes(self):
        git(self.r, "mv", f"{SK}/lesson-a", ".agents/lesson-a-tmp")
        os.makedirs(os.path.join(self.r, ".agents/skills-archive"))
        git(self.r, "mv", ".agents/lesson-a-tmp", ".agents/skills-archive/lesson-a")
        self.commit("archive")
        result = gate(self.r, self.base, self.rev())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_change_passes(self):
        result = gate(self.r, self.base, self.base)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_bad_ref_fails_closed(self):
        result = gate(self.r, "nonexistent", self.base)
        self.assertEqual(result.returncode, 2, result.stderr)

    def test_initial_push_empty_tree_baseline_runs_gate(self):
        empty_tree = subprocess.run(
            ["git", "-C", self.r, "hash-object", "-t", "tree", "/dev/null"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        result = gate(self.r, empty_tree, self.base)
        self.assertEqual(result.returncode, 0, result.stderr)


class WorkflowContractTest(unittest.TestCase):
    def test_gate_is_in_test_job_with_explicit_event_refs(self):
        with open(WORKFLOW, encoding="utf-8") as fh:
            workflow = fh.read()
        self.assertRegex(
            workflow,
            r"(?ms)^on:\n  pull_request:\n  merge_group:\n  push:\n    branches: \[main\]\n",
        )
        test_job = re.search(r"(?ms)^  test:\n(.*?)(?=^  [A-Za-z0-9_-]+:|\Z)", workflow)
        self.assertIsNotNone(test_job)
        job = test_job.group(1)
        self.assertIn("fetch-depth: 0", job)
        gate_step = re.search(r"(?ms)^      - name: Lesson consolidation gate\n(.*?)(?=^      - |\Z)", job)
        self.assertIsNotNone(gate_step)
        gate = gate_step.group(1)
        self.assertRegex(
            gate,
            r"(?m)^        env:\n"
            r"          BASE_SHA: \$\{\{ github\.event\.pull_request\.base\.sha \|\| "
            r"github\.event\.merge_group\.base_sha \|\| github\.event\.before \}\}\n"
            r"          HEAD_SHA: \$\{\{ github\.event\.pull_request\.head\.sha \|\| "
            r"github\.event\.merge_group\.head_sha \|\| github\.event\.after \}\}$",
        )
        self.assertRegex(
            gate,
            r"(?ms)^        run: \|\n"
            r"          if echo \"\$BASE_SHA\" \| grep -qE '\^0\+\$'; then\n"
            r"            BASE_SHA=\"\$\(git hash-object -t tree /dev/null\)\"\n"
            r"          fi\n"
            r"          python -m mahler\.consolidation_gate --base \"\$BASE_SHA\" "
            r"--head \"\$HEAD_SHA\"$",
        )
        self.assertIn("python -m mahler.consolidation_gate --base \"$BASE_SHA\" --head \"$HEAD_SHA\"", gate)
        for event_ref in (
            "github.event.pull_request.base.sha",
            "github.event.merge_group.base_sha",
            "github.event.before",
            "github.event.pull_request.head.sha",
            "github.event.merge_group.head_sha",
            "github.event.after",
        ):
            self.assertIn(event_ref, gate)
        for command in (
            "python -m mahler.repo_guard",
            "python -m unittest discover -s tests -v",
            "python tests/run_random.py",
            "python -X dev tests/run_strict.py",
        ):
            self.assertIn(command, job)


if __name__ == "__main__":
    unittest.main()
