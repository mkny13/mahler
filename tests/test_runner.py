"""Snapshots must save everything a run left without touching its worktree (DESIGN D6/D9)."""

import os
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from mahler import config, runner
from mahler.gh import depends_of, parse_command


def write(path, text, mode="w"):
    with open(path, mode) as fh:
        fh.write(text)


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.remote = os.path.join(t, "remote.git")
        self.repo = os.path.join(t, "repo")
        sh(t, "git", "init", "-q", "--bare", "-b", "main", self.remote)
        sh(t, "git", "clone", "-q", self.remote, self.repo)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            sh(self.repo, "git", "config", k, v)
        write(os.path.join(self.repo, "a.txt"), "one\n")
        sh(self.repo, "git", "add", ".")
        sh(self.repo, "git", "commit", "-qm", "init")
        sh(self.repo, "git", "push", "-q", "origin", "main")
        self.runs = os.path.join(t, "runs")
        os.makedirs(os.path.join(self.runs, "5"))
        self.patch = mock.patch.object(config, "RUNS_DIR", self.runs)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_nothing_new_means_no_snapshot(self):
        self.assertIsNone(runner.snapshot(self.repo, self.repo, 5, 12, "main"))

    def test_dirty_and_untracked_work_is_saved_untouched(self):
        write(os.path.join(self.repo, "a.txt"), "two\n", "a")
        write(os.path.join(self.repo, "new.txt"), "fresh\n")
        before = sh(self.repo, "git", "status", "--porcelain")
        saved = runner.snapshot(self.repo, self.repo, 5, 12, "main")
        self.assertEqual(saved["ref"], "mahler/snapshot/12-run5")
        self.assertEqual(sh(self.repo, "git", "status", "--porcelain"), before)   # untouched
        files = sh(self.remote, "git", "ls-tree", "-r", "--name-only", "mahler/snapshot/12-run5")
        self.assertIn("new.txt", files.split())
        content = sh(self.remote, "git", "show", "mahler/snapshot/12-run5:a.txt")
        self.assertEqual(content, "one\ntwo")

    def test_unpushed_commits_are_saved(self):
        write(os.path.join(self.repo, "b.txt"), "b\n")
        sh(self.repo, "git", "add", ".")
        sh(self.repo, "git", "commit", "-qm", "local only")
        saved = runner.snapshot(self.repo, self.repo, 5, 12, "main")
        self.assertEqual(saved["ahead"], 1)


class CatchUpTests(unittest.TestCase):
    """Resumed work starts on current base (DESIGN D19, mahler#27)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.remote = os.path.join(t, "remote.git")
        self.repo = os.path.join(t, "repo")
        sh(t, "git", "init", "-q", "--bare", "-b", "main", self.remote)
        sh(t, "git", "clone", "-q", self.remote, self.repo)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            sh(self.repo, "git", "config", k, v)
        self.commit("a.txt", "one\n", "init")
        sh(self.repo, "git", "push", "-q", "origin", "main")
        # an earlier run's saved work on the item's branch...
        sh(self.repo, "git", "checkout", "-qb", "mahler/7-x")
        self.commit("a.txt", "one\nitem seven\n", "item work")
        sh(self.repo, "git", "push", "-q", "origin", "mahler/7-x")
        self.old = sh(self.repo, "git", "rev-parse", "HEAD")
        sh(self.repo, "git", "checkout", "-q", "main")
        self.wt = os.path.join(t, "wt")

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, name, text, msg):
        write(os.path.join(self.repo, name), text)
        sh(self.repo, "git", "add", ".")
        sh(self.repo, "git", "commit", "-qm", msg)

    def main_moves(self, name, text):
        """...and then main moves on without it."""
        self.commit(name, text, "someone else's change")
        sh(self.repo, "git", "push", "-q", "origin", "main")
        sh(self.repo, "git", "fetch", "-q", "origin")
        sh(self.repo, "git", "worktree", "add", "-q", "-B", "mahler/7-x", self.wt,
           "origin/mahler/7-x")

    def test_saved_work_is_replayed_onto_current_main(self):
        self.main_moves("b.txt", "unrelated\n")
        self.assertIsNone(runner.catch_up(self.wt, "mahler/7-x", "main", 7, 9))
        tip = sh(self.remote, "git", "rev-parse", "mahler/7-x")
        self.assertEqual(tip, sh(self.wt, "git", "rev-parse", "HEAD"))
        sh(self.remote, "git", "merge-base", "--is-ancestor", "main", "mahler/7-x")
        self.assertEqual(sh(self.wt, "git", "show", "HEAD:a.txt"), "one\nitem seven")
        self.assertEqual(sh(self.wt, "git", "show", "HEAD:b.txt"), "unrelated")

    def test_work_already_on_main_drops_the_branch(self):
        self.main_moves("a.txt", "one\nitem seven\n")          # the same change landed
        self.assertIsNone(runner.catch_up(self.wt, "mahler/7-x", "main", 7, 9))
        self.assertEqual(sh(self.wt, "git", "rev-parse", "HEAD"),
                         sh(self.remote, "git", "rev-parse", "main"))
        self.assertEqual(sh(self.remote, "git", "branch", "--list", "mahler/7-x"), "")

    def test_work_that_no_longer_applies_starts_fresh_and_is_kept(self):
        self.main_moves("a.txt", "one\nsomething else\n")
        kept = runner.catch_up(self.wt, "mahler/7-x", "main", 7, 9)
        self.assertEqual(kept, "mahler/snapshot/7-stale-run9")
        self.assertEqual(sh(self.remote, "git", "rev-parse", kept), self.old)
        main = sh(self.remote, "git", "rev-parse", "main")
        self.assertEqual(sh(self.wt, "git", "rev-parse", "HEAD"), main)
        # a head at main's tip would read as merged on an open PR: the branch is dropped
        self.assertEqual(sh(self.remote, "git", "branch", "--list", "mahler/7-x"), "")
        self.assertEqual(sh(self.wt, "git", "status", "--porcelain"), "")
        # the agent can read the old work from its worktree, as the prompt says
        self.assertIn("item work", sh(self.wt, "git", "log", f"origin/main..origin/{kept}"))


class ParsingTests(unittest.TestCase):
    def test_commands(self):
        self.assertEqual(parse_command("/mahler go"), ("go", None))
        self.assertEqual(parse_command("thanks!\n/mahler platform agy-gemini"),
                         ("platform", "agy-gemini"))
        self.assertIsNone(parse_command("just a reply"))

    def test_depends(self):
        self.assertEqual(depends_of("Blah\nDepends on: #3, #7\n"), [3, 7])
        self.assertEqual(depends_of(None), [])

    def test_slug(self):
        self.assertEqual(runner.slug("Add dark mode (Settings)!"), "add-dark-mode-settings")


class SetupTailTests(unittest.TestCase):
    def test_setup_tail_reads_the_last_lines(self):
        with tempfile.TemporaryDirectory() as d:
            run = {"log_path": os.path.join(d, "agent.log")}
            self.assertEqual(runner.setup_tail(run), "")
            with open(os.path.join(d, "setup.log"), "w") as fh:
                fh.write("\n".join(f"line{i}" for i in range(30)))
            tail = runner.setup_tail(run)
            self.assertEqual(tail.splitlines()[0], "line10")
            self.assertEqual(len(tail.splitlines()), 20)
            self.assertEqual(runner.setup_tail(run, lines=2), "line28\nline29")


class PlatformLaunchTests(unittest.TestCase):
    def test_codex_launch_keeps_worktree_and_lease_fencing(self):
        with tempfile.TemporaryDirectory() as d:
            runs = os.path.join(d, "runs")
            repo = os.path.join(d, "repo")
            worktrees = os.path.join(d, "worktrees")
            os.makedirs(repo)
            policy = {
                "path": repo, "repo": "x/y", "base": "main", "link": [],
                "rules": "", "run_timeout_minutes": 60,
                "worktree_root": worktrees,
            }
            ctx = SimpleNamespace(
                cfg={"platforms": {"codex": {"kind": "codex"}}},
                policy=lambda project: policy,
            )
            item = {"number": 157, "title": "Add Codex", "branch": None}

            with mock.patch.object(config, "RUNS_DIR", runs), \
                    mock.patch.object(runner, "git"), \
                    mock.patch.object(runner, "remote_has", return_value=False), \
                    mock.patch.object(runner, "render", return_value="prompt"), \
                    mock.patch.object(runner, "fence_hooks", return_value="/tmp/hooks"), \
                    mock.patch.object(runner.platforms, "argv_for",
                                      return_value=["/usr/bin/true"]) as argv_for, \
                    mock.patch.object(runner.subprocess, "Popen",
                                      return_value=SimpleNamespace(pid=321)) as popen:
                launched = runner.launch(ctx, "mahler", item, "build", "codex", 9, 4)

            worktree = os.path.join(worktrees, "mahler", "157-run9")
            argv_for.assert_called_once_with(ctx.cfg["platforms"]["codex"], "prompt",
                                             worktree, "build", 60)
            self.assertEqual(popen.call_args.kwargs["cwd"], worktree)
            env = popen.call_args.kwargs["env"]
            self.assertEqual(env["MAHLER_EPOCH"], "4")
            self.assertEqual(env["GIT_CONFIG_VALUE_0"], "/tmp/hooks")
            self.assertEqual(launched["worktree"], worktree)


if __name__ == "__main__":
    unittest.main()
