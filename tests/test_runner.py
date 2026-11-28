"""Snapshots must save everything a run left without touching its worktree (DESIGN D6/D9)."""

import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

from mahler import config, prompt, runner, no_change, lessons
from mahler.ledger import Ledger
from mahler.gh import depends_of, parse_command


def write(path, text, mode="w"):
    with open(path, mode) as fh:
        fh.write(text)


def _no_git_config_overrides(env):
    return {k: v for k, v in env.items()
            if not (k == "GIT_CONFIG_COUNT" or k.startswith("GIT_CONFIG_KEY_")
                    or k.startswith("GIT_CONFIG_VALUE_"))}


def sh(cwd, *args, env=None):
    full = _no_git_config_overrides({**os.environ, **(env or {})})
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True,
                          text=True, env=full).stdout.strip()


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

    def test_no_work_probe_fetches_configured_base(self):
        sh(self.repo, "git", "branch", "release")
        sh(self.repo, "git", "push", "-q", "origin", "release")
        sh(self.repo, "git", "update-ref", "-d", "refs/remotes/origin/release")
        self.assertEqual(runner.no_work_base(self.repo, "release"),
                         sh(self.repo, "git", "rev-parse", "HEAD"))

    def test_no_work_probe_distinguishes_dirty_untracked_and_committed_work(self):
        for filename in ("a.txt", "new.txt"):
            with self.subTest(filename=filename):
                write(os.path.join(self.repo, filename), "changed\n")
                before = sh(self.repo, "git", "status", "--porcelain")
                self.assertIsNone(runner.no_work_base(self.repo, "main"))
                self.assertEqual(sh(self.repo, "git", "status", "--porcelain"), before)
        sh(self.repo, "git", "add", ".")
        sh(self.repo, "git", "commit", "-qm", "work")
        self.assertIsNone(runner.no_work_base(self.repo, "main"))

    def test_no_work_probe_errors_are_not_clean_evidence(self):
        for wt, base in ((None, "main"), (self.repo + "-missing", "main"),
                         (self.repo, "missing-base")):
            with self.subTest(wt=wt, base=base), self.assertRaises(runner.GitError):
                runner.no_work_base(wt, base)

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

    def _load_snapshot_lesson(self, suppressed=True):
        rel = ".agents/skills/lesson-a/SKILL.md"
        path = Path(self.repo) / rel
        path.parent.mkdir(parents=True)
        text = ("---\nname: lesson-a\ndescription: Use when the build fails on X.\n"
                "metadata:\n  mahler-id: 58d1fc1f8ac8de24\n"
                "  created: 2026-10-01\n  updated: 2026-10-02\n"
                "  source: '#12'\n---\nRun the formatter first.\n")
        path.write_text("invalid frontmatter\n" if suppressed else text)
        sh(self.repo, "git", "add", ".")
        sh(self.repo, "git", "commit", "-qm", "lesson")
        sh(self.repo, "git", "push", "-q", "origin", "main")
        result = lessons.load(self.repo, self.repo, "origin/main", "p")
        self.assertEqual(bool(result["rejected"]), suppressed)
        self.assertEqual(path.exists(), not suppressed)
        self.assertEqual(sh(self.repo, "git", "status", "--porcelain"), "")
        return rel

    def test_snapshot_preserves_suppressed_lessons_when_clean(self):
        self._load_snapshot_lesson()
        self.assertIsNone(runner.snapshot(self.repo, self.repo, 5, 12, "main"))

    def test_snapshot_preserves_suppressed_lessons_with_uncommitted_work(self):
        rel = self._load_snapshot_lesson()
        write(os.path.join(self.repo, "a.txt"), "changed\n")
        write(os.path.join(self.repo, "new.txt"), "new\n")
        index = Path(sh(self.repo, "git", "rev-parse", "--absolute-git-dir")) / "index"
        before = index.read_bytes()
        saved = runner.snapshot(self.repo, self.repo, 5, 12, "main")
        self.assertEqual(sh(self.repo, "git", "rev-parse", f"{saved['sha']}:{rel}"),
                         sh(self.repo, "git", "rev-parse", f"HEAD:{rel}"))
        self.assertEqual(sh(self.repo, "git", "show", f"{saved['sha']}:a.txt"), "changed")
        self.assertEqual(sh(self.repo, "git", "show", f"{saved['sha']}:new.txt"), "new")
        self.assertEqual(index.read_bytes(), before)
        self.assertFalse((Path(self.repo) / rel).exists())

    def test_snapshot_captures_edits_to_active_lessons(self):
        rel = self._load_snapshot_lesson(suppressed=False)
        path = Path(self.repo) / rel
        path.write_text(path.read_text() + "Run tests afterward.\n")
        saved = runner.snapshot(self.repo, self.repo, 5, 12, "main")
        self.assertEqual(sh(self.repo, "git", "show", f"{saved['sha']}:{rel}"),
                         path.read_text().strip())

    def test_unpushed_commits_are_saved(self):
        write(os.path.join(self.repo, "b.txt"), "b\n")
        sh(self.repo, "git", "add", ".")
        sh(self.repo, "git", "commit", "-qm", "local only")
        saved = runner.snapshot(self.repo, self.repo, 5, 12, "main")
        self.assertEqual(saved["ahead"], 1)

    def test_push_carries_the_given_account_env(self):
        # mahler#296: a work project's snapshot push must go out under its own
        # git-hosting identity, not this machine's default git credentials.
        write(os.path.join(self.repo, "b.txt"), "b\n")
        sh(self.repo, "git", "add", ".")
        sh(self.repo, "git", "commit", "-qm", "local only")
        account_env = dict(os.environ, GH_CONFIG_DIR=os.path.expanduser("~/.config/gh-work"))
        with mock.patch.object(runner, "git", wraps=runner.git) as git:
            runner.snapshot(self.repo, self.repo, 5, 12, "main", env=account_env)
        push = next(c for c in git.call_args_list if c.args[1] == "push")
        self.assertEqual(push.kwargs["env"]["GH_CONFIG_DIR"],
                         os.path.expanduser("~/.config/gh-work"))


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

    def test_pushes_carry_the_given_account_env(self):
        self.main_moves("b.txt", "unrelated\n")
        account_env = dict(os.environ, GH_CONFIG_DIR=os.path.expanduser("~/.config/gh-work"))
        with mock.patch.object(runner, "git", wraps=runner.git) as git:
            runner.catch_up(self.wt, "mahler/7-x", "main", 7, 9, env=account_env)
        pushes = [c for c in git.call_args_list if c.args[1] == "push"]
        self.assertTrue(pushes)
        for push in pushes:
            self.assertEqual(push.kwargs["env"]["GH_CONFIG_DIR"],
                             os.path.expanduser("~/.config/gh-work"))


class ParsingTests(unittest.TestCase):
    def test_commands(self):
        self.assertEqual(parse_command("/mahler go"), ("go", None))
        self.assertEqual(parse_command("thanks!\n/mahler platform agy-gemini"),
                         ("platform", "agy-gemini"))
        self.assertIsNone(parse_command("just a reply"))

    def test_depends(self):
        self.assertEqual(depends_of("Blah\nDepends on: #3, #7\n"), [3, 7])
        self.assertEqual(depends_of(None), [])

    def test_qualified_dependencies_preserve_repository(self):
        expected = [3, {"repo": "mkny13/groundwork", "number": 125},
                    {"repo": "couch-tour", "number": 258}, 7]
        for prefix in ("", "> "):
            with self.subTest(prefix=prefix):
                self.assertEqual(depends_of(
                    prefix + "Depends on: #3, mkny13/groundwork#125, "
                    "`couch-tour#258`, #7\n"), expected)

    def test_depends_inside_a_blockquote(self):
        # a "Depends on:" line quoted under "> **Original request:**" must
        # still parse — same class of bug as part_of's blockquote handling
        self.assertEqual(
            depends_of("> **Original request:**\n> Depends on: #3, #7\n"), [3, 7])

    def test_slug(self):
        self.assertEqual(runner.slug("Add dark mode (Settings)!"), "add-dark-mode-settings")

    def test_slug_of_empty_title_falls_back_to_item(self):
        self.assertEqual(runner.slug(""), "item")
        self.assertEqual(runner.slug(None), "item")
        self.assertEqual(runner.slug("!!!"), "item")     # nothing survives the strip

    def test_slug_drops_non_ascii(self):
        self.assertEqual(runner.slug("Fix café préférence — now"),
                         "fix-caf-pr-f-rence-now")

    def test_slug_is_truncated_to_n_chars(self):
        self.assertEqual(runner.slug("a" * 45), "a" * 40)

    def test_slug_cut_never_ends_in_a_hyphen(self):
        self.assertEqual(runner.slug("ab cd", n=3), "ab")     # "ab-"[:3].rstrip("-")

    def test_slug_of_already_hyphenated_title_is_unchanged(self):
        self.assertEqual(runner.slug("already-hyphenated-title"), "already-hyphenated-title")


class ExitCodeTests(unittest.TestCase):
    def test_normal_exit_code_is_an_int(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "exit")
            write(path, "0\n")
            self.assertEqual(runner.exit_code({"status_path": path}), 0)
            write(path, "137\n")
            self.assertEqual(runner.exit_code({"status_path": path}), 137)

    def test_missing_status_file_is_none(self):
        self.assertIsNone(runner.exit_code({"status_path": "/nonexistent/path/exit"}))

    def test_non_numeric_content_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "exit")
            write(path, "not a number\n")
            self.assertIsNone(runner.exit_code({"status_path": path}))


class CommitsAheadTests(unittest.TestCase):
    def test_missing_worktree_is_zero(self):
        self.assertEqual(runner.commits_ahead("/nonexistent/worktree", "main"), 0)

    def test_empty_worktree_path_is_zero(self):
        self.assertEqual(runner.commits_ahead(None, "main"), 0)
        self.assertEqual(runner.commits_ahead("", "main"), 0)


class PromptRenderTests(unittest.TestCase):
    """prompt.render (mahler#70 split runner and prompt apart): string.Template's
    safe_substitute leaves an unrecognized escape or a missing key untouched
    rather than raising — the recipes rely on that contract."""

    def render(self, text, **vars):
        with tempfile.TemporaryDirectory() as d:
            write(os.path.join(d, "t.md"), text)
            with mock.patch.object(prompt, "RECIPES", d):
                return prompt.render("t", **vars)

    def test_double_dollar_is_an_escaped_literal_dollar(self):
        self.assertEqual(self.render("cost: $$5 for ${name}", name="x"), "cost: $5 for x")

    def test_missing_key_is_left_as_the_literal_placeholder(self):
        self.assertEqual(self.render("hello ${missing}, ${present}", present="you"),
                         "hello ${missing}, you")


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


class RemoveWorktreeTests(unittest.TestCase):
    """DESIGN D12: rm -rf must never stray outside the worktree root."""

    def test_removes_a_worktree_inside_root(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "worktrees")
            wt = os.path.join(root, "proj", "1-run1")
            os.makedirs(wt)
            with mock.patch.object(runner, "git") as git:
                runner.remove_worktree("/repo", wt, "mahler/1-x", root=root)
            self.assertFalse(os.path.isdir(wt))
            git.assert_any_call("/repo", "worktree", "remove", "--force", wt, check=False)
            git.assert_any_call("/repo", "worktree", "prune", check=False)
            git.assert_any_call("/repo", "branch", "-D", "mahler/1-x", check=False)

    def test_refuses_a_sibling_directory_sharing_only_a_string_prefix(self):
        # root=".../worktrees", wt=".../worktrees-evil/x" — a bare startswith()
        # would wrongly treat this as "inside" root and delete it.
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "worktrees")
            evil = os.path.join(d, "worktrees-evil", "x")
            os.makedirs(evil)
            with mock.patch.object(runner, "git") as git:
                runner.remove_worktree("/repo", evil, root=root)
            self.assertTrue(os.path.isdir(evil))    # untouched
            self.assertNotIn(
                mock.call("/repo", "worktree", "remove", "--force", evil, check=False),
                git.call_args_list)
            git.assert_any_call("/repo", "worktree", "prune", check=False)

    def test_refuses_a_path_that_escapes_through_a_symlinked_ancestor(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "worktrees")
            outside = os.path.join(d, "outside")
            escaped = os.path.join(outside, "1-run1")
            os.makedirs(root)
            os.makedirs(escaped)
            os.symlink(outside, os.path.join(root, "project"))
            apparent = os.path.join(root, "project", "1-run1")
            with mock.patch.object(runner, "git") as git:
                runner.remove_worktree("/repo", apparent, root=root)
            self.assertTrue(os.path.isdir(escaped))
            self.assertNotIn(
                mock.call("/repo", "worktree", "remove", "--force", apparent,
                          check=False), git.call_args_list)
            git.assert_called_once_with("/repo", "worktree", "prune", check=False)

    def test_missing_worktree_is_a_noop_but_still_prunes(self):
        with mock.patch.object(runner, "git") as git:
            runner.remove_worktree("/repo", None, root="/tmp/nonexistent-mahler-root")
        git.assert_called_once_with("/repo", "worktree", "prune", check=False)


class PlatformLaunchTests(unittest.TestCase):
    def test_codex_launch_keeps_worktree_and_lease_fencing(self):
        with tempfile.TemporaryDirectory() as d:
            runs = os.path.join(d, "runs")
            repo = os.path.join(d, "repo")
            remote = os.path.join(d, "remote.git")
            worktrees = os.path.join(d, "worktrees")
            os.makedirs(repo)
            sh(d, "git", "init", "-q", "--bare", "-b", "main", remote)
            sh(d, "git", "clone", "-q", remote, repo)
            for k, v in (("user.email", "t@t"), ("user.name", "t")):
                sh(repo, "git", "config", k, v)
            sh(repo, "git", "commit", "--allow-empty", "-qm", "init")
            sh(repo, "git", "push", "-q", "origin", "main")
            policy = {
                "path": repo, "repo": "x/y", "base": "main", "link": [],
                "rules": "", "run_timeout_minutes": 60,
                "worktree_root": worktrees,
            }
            ctx = SimpleNamespace(
                cfg={"platforms": {"codex": {"kind": "codex", "model": "codex"}}},
                policy=lambda project: policy,
            )
            item = {"number": 157, "title": "Add Codex", "branch": None}

            # Mock Popen only for the agent launch (which uses /bin/sh -c),
            # let subprocess.run (used by git) work normally
            real_popen = subprocess.Popen
            def popen_side_effect(args, **kwargs):
                if args and args[0] == "/bin/sh" and args[1] == "-c":
                    return SimpleNamespace(pid=321)
                return real_popen(args, **kwargs)

            with mock.patch.object(config, "RUNS_DIR", runs), \
                    mock.patch.object(runner.sys, "platform", "linux"), \
                    mock.patch.object(runner.platforms, "argv_for",
                                      return_value=["/usr/bin/true"]) as argv_for, \
                    mock.patch.object(runner.subprocess, "Popen",
                                      side_effect=popen_side_effect) as popen:
                prep = runner.prepare(ctx, "mahler", item, "build", "codex", 9)
                launched = runner.launch(ctx, "mahler", item, "build", "codex", 9, 4,
                                         "the prompt", prep)

            worktree = os.path.join(worktrees, "mahler", "157-run9")
            argv_for.assert_called_once_with(ctx.cfg["platforms"]["codex"], mock.ANY,
                                             worktree, "build", 60)
            self.assertEqual(popen.call_args.kwargs["cwd"], worktree)
            self.assertEqual(launched["worktree"], worktree)
            # Verify worktree was actually created and is a valid git worktree
            self.assertTrue(os.path.isdir(worktree))
            self.assertTrue(os.path.isfile(os.path.join(worktree, ".git")))
            # Verify hooks directory was created with pre-push hook
            hooks_dir = os.path.join(runs, "9", "hooks")
            self.assertTrue(os.path.isdir(hooks_dir))
            self.assertTrue(os.path.isfile(os.path.join(hooks_dir, "pre-push")))
            # Verify the hooks path is passed via GIT_CONFIG_VALUE_0
            env = popen.call_args.kwargs["env"]
            self.assertEqual(env["MAHLER_EPOCH"], "4")
            self.assertEqual(env["GIT_CONFIG_VALUE_0"], hooks_dir)


class ResumeLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.led = Ledger(":memory:")
        self.addCleanup(self.led.close)
        self.conf = {"kind": "codex", "account": "work", "model": "chosen"}
        self.home = self.root / "account"
        self.cfg = {"platforms": {"work": self.conf}, "accounts": {
            "work": {"env": {"CODEX_HOME": str(self.home),
                             "CLAUDE_CONFIG_DIR": str(self.home),
                             "XDG_DATA_HOME": str(self.home), "MISTRAL_API_KEY": "test"}}}}
        self.ctx = SimpleNamespace(cfg=self.cfg, led=self.led, policy=lambda _: {
            "path": str(self.root), "account": "work", "run_timeout_minutes": 60})
        self.source = self.led.create_run(project="p", number=7, role="build",
            platform="work", epoch=2, status="ended", session_id="session-123")
        self.new = self.led.create_run(project="p", number=7, role="fix",
            platform="work", epoch=3, status="running")
        self.runs = self.root / "runs"
        self.run_dir = self.runs / str(self.new)
        self.run_dir.mkdir(parents=True)
        self.prep = dict(worktree=str(self.root / "new-wt"), run_dir=str(self.run_dir),
                         branch="mahler/pr-head", base_ref="origin/mahler/pr-head")
        self.item = dict(number=7, branch="mahler/pr-head")
        for patcher in (mock.patch.object(config, "RUNS_DIR", str(self.runs)),
                        mock.patch.object(runner, "fence_hooks", return_value="new-hooks"),
                        mock.patch.object(runner.platforms, "codex_exe", return_value="codex"),
                        mock.patch.object(runner.platforms, "vibe_exe", return_value="vibe")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def storage(self, relative):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n")
        return path

    def launch(self):
        return runner.launch(self.ctx, "p", self.item, "fix", "work", self.new, 3,
                             "Full current fix prompt: CI logs and review findings", self.prep,
                             resume_from=self.source)

    def test_resume_preserves_prompt_worktree_branch_and_new_fencing(self):
        self.storage("sessions/2026/10/07/rollout-session-123.jsonl")
        with mock.patch.object(runner, "spawn", return_value=123) as spawn:
            result = self.launch()
        argv, cwd, log, status = spawn.call_args.args
        self.assertEqual(cwd, self.prep["worktree"])
        self.assertEqual(result["branch"], "mahler/pr-head")
        self.assertEqual(log, str(self.run_dir / "agent.log"))
        self.assertEqual(status, str(self.run_dir / "exit"))
        self.assertEqual(argv[-2], "session-123")
        self.assertEqual(argv[-1], (self.run_dir / "prompt.md").read_text())
        env = spawn.call_args.kwargs["env"]
        self.assertEqual(env["CODEX_HOME"], str(self.home))
        self.assertEqual(env["MAHLER_RUN_ID"], str(self.new))
        self.assertEqual(env["MAHLER_EPOCH"], "3")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "new-hooks")
        self.assertEqual(self.led.run(self.source)["epoch"], 2)

    def test_missing_storage_does_not_spawn_or_select_latest(self):
        with mock.patch.object(runner, "spawn") as spawn:
            with self.assertRaises(runner.ResumeUnsupported):
                self.launch()
        spawn.assert_not_called()

    def test_rejects_foreign_account_kind_item_and_missing_identity(self):
        self.storage("sessions/rollout-session-123.jsonl")
        self.cfg["platforms"]["other"] = {"kind": "codex", "account": "personal"}
        for changes in ({"platform": "other"}, {"project": "other"}, {"number": 8},
                        {"session_id": None}, {"session_id": " "},
                        {"session_id": "--last"}, {"status": "running"}):
            with self.subTest(changes=changes):
                self.led.update_run(self.source, platform="work", project="p", number=7,
                                    session_id="session-123", status="ended")
                self.led.update_run(self.source, **changes)
                with mock.patch.object(runner, "spawn") as spawn:
                    with self.assertRaises(runner.ResumeUnsupported):
                        self.launch()
                    spawn.assert_not_called()
        self.led.update_run(self.source, platform="other", status="ended")
        self.cfg["platforms"]["other"] = {"kind": "claude", "account": "work"}
        with self.assertRaises(runner.ResumeUnsupported):
            self.launch()

    def test_vibe_reuses_only_source_session_storage(self):
        self.conf["kind"] = "vibe"
        source_home = self.runs / str(self.source) / "vibe_home"
        logs = source_home / "logs/session"
        logs.mkdir(parents=True)
        (source_home / ".env").write_text("must not copy")
        (source_home / "config.toml").write_text("must not copy")
        with mock.patch.object(runner, "spawn", return_value=123) as spawn:
            self.launch()
        env = spawn.call_args.kwargs["env"]
        target = Path(env["VIBE_HOME"])
        self.assertEqual(target, self.run_dir / "vibe_home")
        self.assertEqual((target / "logs/session").resolve(), logs.resolve())
        self.assertFalse((target / ".env").exists())
        self.assertIn('chosen', (target / "config.toml").read_text())
        self.assertEqual(env["MAHLER_EPOCH"], "3")
        self.assertEqual(spawn.call_args.args[0][-2:], ["--resume", "session-123"])

    def test_vibe_missing_source_location_is_unsupported(self):
        self.conf["kind"] = "vibe"
        with mock.patch.object(runner, "spawn") as spawn:
            with self.assertRaises(runner.ResumeUnsupported):
                self.launch()
            spawn.assert_not_called()


class FenceHookTests(unittest.TestCase):
    def test_repository_hooks_path_is_shell_quoted(self):
        with tempfile.TemporaryDirectory() as d:
            marker = os.path.join(d, "injected")
            configured = f"hooks-$(touch {marker})"
            with mock.patch.object(runner, "git", return_value=configured), \
                    mock.patch.object(config, "MAHLER_BIN", "/usr/bin/true"):
                hooks = runner.fence_hooks("/repo", os.path.join(d, "run"))
            subprocess.run(["/bin/sh", os.path.join(hooks, "pre-push")],
                           env={**os.environ, "MAHLER_ISSUE": "594"}, check=True)
            self.assertFalse(os.path.exists(marker))


class PrepareAccountEnvTests(unittest.TestCase):
    """A work project's repo is fetched with its own git-hosting identity
    (D25/D26's gh_account), not this machine's default git credentials —
    otherwise a private work repo 404s as "Repository not found" against the
    wrong account's credential helper (mahler#296)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.remote = os.path.join(t, "remote.git")
        self.repo = os.path.join(t, "repo")
        sh(t, "git", "init", "-q", "--bare", "-b", "main", self.remote)
        sh(t, "git", "clone", "-q", self.remote, self.repo)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            sh(self.repo, "git", "config", k, v)
        sh(self.repo, "git", "commit", "--allow-empty", "-qm", "init")
        sh(self.repo, "git", "push", "-q", "origin", "main")
        self.worktrees = os.path.join(t, "worktrees")
        policy = {
            "path": self.repo, "repo": "acme/x", "base": "main", "link": [],
            "account": "work", "worktree_root": self.worktrees,
            "rules": "", "run_timeout_minutes": 60,
        }
        self.ctx = SimpleNamespace(
            cfg={"platforms": {"claude-work": {"account": "work", "kind": "claude",
                                                "sort_model": "planning-model",
                                                "build_model": "builder-model"}},
                 "accounts": {"work": {"env": {"GH_CONFIG_DIR": "~/.config/gh-work"}}}},
            policy=lambda project: policy,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_claim_review_checks_out_exact_base_not_saved_branch(self):
        sha = sh(self.repo, "git", "rev-parse", "HEAD")
        sh(self.repo, "git", "push", "-q", "origin", "HEAD:release")
        self.ctx.policy("acme")["base"] = "release"
        sh(self.repo, "git", "commit", "--allow-empty", "-qm", "newer saved work")
        sh(self.repo, "git", "push", "-q", "origin", "main")
        self.assertEqual(no_change.fetched_base(self.ctx, "acme"), sha)
        sh(self.repo, "git", "push", "-q", "origin", "HEAD:mahler/snapshot/old")
        self.ctx.led = Ledger(":memory:")
        self.addCleanup(self.ctx.led.close)
        self.ctx.led.upsert_item("acme", 3)
        rid = self.ctx.led.create_run(project="acme", number=3, role="review",
                                      platform="claude-work", epoch=1)
        lease, _ = self.ctx.led.claim("acme", 3, f"run:{rid}", "auto", 10, run_id=rid)
        no_change.save(self.ctx.led, "acme", 3, {"base_sha": sha, "phase": "waiting"})
        item = {"number": 3, "title": "t", "branch": "mahler/snapshot/old", "claim_base_sha": sha}
        with mock.patch.object(config, "RUNS_DIR", os.path.join(self.tmp.name, "runs")), \
                mock.patch.object(runner, "catch_up") as replay:
            prep = runner.prepare(self.ctx, "acme", item, "review", "claude-work", rid)
        self.assertEqual(sh(prep["worktree"], "git", "rev-parse", "HEAD"), sha)
        self.assertEqual(sh(prep["worktree"], "git", "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")
        self.assertIsNone(prep["branch"])
        self.assertFalse(prep["replayed"])
        replay.assert_not_called()
        record = no_change.read(self.ctx.led, "acme", 3)
        self.assertEqual((record["run_id"], record["epoch"]), (rid, lease["epoch"]))
        text = prompt.build(self.ctx, "acme", item, "review", "claude-work", prep)
        self.assertIn(sha, text)
        self.assertIn("claim mode", text)
        self.assertIn("Claim mode has no PR or branch diff", text)
        self.assertIn("exact checked-base ancestry", text)
        self.assertNotIn("gh pr diff", text)
        self.assertNotIn("already-green", text)
        normal = prompt.build(self.ctx, "acme", {**item, "claim_base_sha": None, "pr": 8},
                              "review", "claude-work", prep)
        self.assertIn("gh pr diff 8", normal)
        self.assertIn("already-green", normal)
        self.assertNotIn("claim mode", normal)

    def test_review_worktree_detached_when_branch_held(self):
        item = {"number": 3, "title": "t", "branch": "main"}
        sha = sh(self.repo, "git", "rev-parse", "HEAD")
        with mock.patch.object(config, "RUNS_DIR", os.path.join(self.tmp.name, "runs")):
            prep = runner.prepare(self.ctx, "acme", item, "review", "claude-work", 1)
        self.assertEqual(sh(prep["worktree"], "git", "rev-parse", "HEAD"), sha)
        self.assertEqual(sh(prep["worktree"], "git", "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")
        self.assertIsNone(prep["branch"])
        self.assertEqual(sh(self.repo, "git", "rev-parse", "--abbrev-ref", "HEAD"), "main")

    def test_fix_external_conflict_identifies_untouched_worktree(self):
        item = {"number": 3, "title": "t", "branch": "main"}
        with mock.patch.object(config, "RUNS_DIR", os.path.join(self.tmp.name, "runs")):
            with self.assertRaises(runner.WorktreeConflict) as error:
                runner.prepare(self.ctx, "acme", item, "fix", "claude-work", 1)
        self.assertEqual(Path(error.exception.path).resolve(), Path(self.repo).resolve())
        self.assertEqual(error.exception.branch, "main")
        self.assertEqual(sh(self.repo, "git", "rev-parse", "--abbrev-ref", "HEAD"), "main")

    def test_fix_reclaims_inactive_managed_worktree(self):
        sh(self.repo, "git", "branch", "pr-head")
        sh(self.repo, "git", "push", "-q", "origin", "pr-head")
        item = {"number": 3, "title": "t", "branch": "pr-head"}
        self.ctx.led = mock.Mock()
        self.ctx.led.active_runs.return_value = []
        with mock.patch.object(config, "RUNS_DIR", os.path.join(self.tmp.name, "runs")):
            old = runner.prepare(self.ctx, "acme", item, "fix", "claude-work", 1)
            prep = runner.prepare(self.ctx, "acme", item, "fix", "claude-work", 2)
        self.assertFalse(os.path.exists(old["worktree"]))
        self.assertEqual(prep["branch"], "pr-head")

    def test_fix_retained_checkout_fallback_pushes_to_pr_head(self):
        sh(self.repo, "git", "branch", "pr-head")
        sh(self.repo, "git", "push", "-q", "origin", "pr-head")
        item = {"number": 3, "title": "t", "branch": "pr-head"}
        with mock.patch.object(config, "RUNS_DIR", os.path.join(self.tmp.name, "runs")):
            old = runner.prepare(self.ctx, "acme", item, "fix", "claude-work", 1)
            with mock.patch.object(runner, "_fix_worktree_conflict", return_value=False):
                prep = runner.prepare(self.ctx, "acme", item, "fix", "claude-work", 2)
        self.assertEqual(prep["branch"], "pr-head-r2")
        self.assertEqual(prep["push_branch"], "pr-head")
        rendered = prompt.render("fix", branch=prep["branch"], push_branch=prep["push_branch"])
        self.assertIn("(PR head: `pr-head`)", " ".join(rendered.split()))
        self.assertNotIn("git push", rendered)
        sh(prep["worktree"], "git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
           "commit", "--allow-empty", "-qm", "fix")
        sh(prep["worktree"], "git", "push", "origin", "HEAD:" + prep["push_branch"])
        self.assertEqual(sh(self.repo, "git", "ls-remote", "origin", "refs/heads/pr-head").split()[0],
                         sh(prep["worktree"], "git", "rev-parse", "HEAD"))
        self.assertTrue(os.path.isdir(old["worktree"]))

    def test_fix_preserves_pr_branch_without_replaying_saved_work(self):
        self.git_branch = "mahler/3-reviewed"
        sh(self.repo, "git", "branch", self.git_branch)
        sh(self.repo, "git", "push", "-q", "origin", self.git_branch)
        item = {"number": 3, "title": "t", "branch": self.git_branch}
        with mock.patch.object(config, "RUNS_DIR", os.path.join(self.tmp.name, "runs")), \
                mock.patch.object(runner, "catch_up") as replay:
            prep = runner.prepare(self.ctx, "acme", item, "fix", "claude-work", 1)
            self.assertEqual(prep["branch"], self.git_branch)
            self.assertFalse(prep["replayed"])
            replay.assert_not_called()
            # An active checkout stays intact; the fallback still targets the PR.
            self.ctx.led = mock.Mock()
            self.ctx.led.active_runs.return_value = [{"worktree": prep["worktree"]}]
            other = runner.prepare(self.ctx, "acme", item, "fix", "claude-work", 2)
            self.assertEqual(other["push_branch"], self.git_branch)
            self.assertEqual(other["branch"], self.git_branch + "-r2")
            self.assertTrue(os.path.isdir(prep["worktree"]))

    def test_design_checks_out_pr_head_without_replaying_and_uses_planning_model(self):
        branch = "mahler/3-reviewed"
        sh(self.repo, "git", "branch", branch)
        sh(self.repo, "git", "push", "-q", "origin", branch)
        item = {"number": 3, "title": "t", "branch": branch, "pr": 88}
        runs = os.path.join(self.tmp.name, "runs")
        with mock.patch.object(config, "RUNS_DIR", runs), \
                mock.patch.object(runner, "catch_up") as replay:
            prep = runner.prepare(self.ctx, "acme", item, "design", "claude-work", 4)
            self.assertEqual(prep["branch"], branch)
            self.assertEqual(prep["head_sha"], sh(prep["worktree"], "git", "rev-parse", "HEAD"))
            self.assertFalse(prep["replayed"])
            replay.assert_not_called()
            self.assertEqual(sh(prep["worktree"], "git", "status", "--porcelain"), "")
            with mock.patch.object(runner.platforms, "argv_for",
                                   return_value=["/bin/agent"]) as argv_for, \
                    mock.patch.object(runner, "run_env", return_value={}), \
                    mock.patch.object(runner, "spawn", return_value=123):
                runner.launch(self.ctx, "acme", item, "design", "claude-work", 4, 1,
                              "design prompt", prep)
        argv_for.assert_called_once_with(self.ctx.cfg["platforms"]["claude-work"],
                                         mock.ANY, prep["worktree"], "sort", 60)

    def test_fetch_carries_the_project_s_account_env(self):
        item = {"number": 3, "title": "t", "branch": None}
        with mock.patch.object(runner, "git", wraps=runner.git) as git:
            runner.prepare(self.ctx, "acme", item, "build", "claude-work", 1)
        fetch = next(c for c in git.call_args_list if c.args[1] == "fetch")
        self.assertEqual(fetch.kwargs["env"]["GH_CONFIG_DIR"],
                         os.path.expanduser("~/.config/gh-work"))


class ShellSanitizationTests(unittest.TestCase):
    """mahler#74: untrusted content (issue title/body, agent output) reaches
    runner.launch inside the prompt argv element. Before that argv lands in
    the `/bin/sh -c` string, every element must be shlex-quoted so nothing
    can break out and run as shell."""

    def test_launch_shell_string_quotes_adversarial_prompt(self):
        with tempfile.TemporaryDirectory() as d:
            policy = {
                "path": os.path.join(d, "repo"), "repo": "x/y", "base": "main",
                "link": [], "rules": "", "run_timeout_minutes": 60,
                "worktree_root": os.path.join(d, "worktrees"),
            }
            ctx = SimpleNamespace(
                cfg={"platforms": {"codex": {"kind": "codex"}}},
                policy=lambda project: policy,
            )
            evil = "ok'; rm -rf / #$(cat /etc/passwd) `id` \"quote\" \\eol"
            with mock.patch.object(config, "RUNS_DIR", os.path.join(d, "runs")), \
                    mock.patch.object(runner.sys, "platform", "linux"), \
                    mock.patch.object(runner, "git"), \
                    mock.patch.object(runner, "remote_has", return_value=False), \
                    mock.patch.object(runner, "fence_hooks", return_value="/tmp/hooks"), \
                    mock.patch.object(runner.platforms, "argv_for",
                                      return_value=["/bin/agent", evil]), \
                    mock.patch.object(runner.subprocess, "Popen",
                                      return_value=SimpleNamespace(pid=321)) as popen:
                item = {"number": 74, "title": evil, "branch": None}
                prep = runner.prepare(ctx, "mahler", item, "build", "codex", 11)
                runner.launch(ctx, "mahler", item, "build", "codex", 11, 1, evil, prep)
            shell = popen.call_args.args[0][2]
            # the prompt element survives intact, inside shlex quoting
            self.assertIn(shlex.quote(evil), shell)
            # the command part of the shell string re-parses to exactly the
            # intended argv — i.e. the metacharacters are data, never shell
            self.assertEqual(shlex.split(shell.split(" > ", 1)[0]),
                             ["/bin/agent", evil])


class RunEnvTests(unittest.TestCase):
    """runner.run_env constructs the shared environment for launches and resumes (mahler#426)."""

    def test_run_env_contains_fence_and_epoch(self):
        with tempfile.TemporaryDirectory() as d:
            policy = {
                "path": os.path.join(d, "repo"), "repo": "x/y", "base": "main",
                "link": [], "rules": "", "run_timeout_minutes": 60,
            }
            ctx = SimpleNamespace(
                cfg={"platforms": {"codex": {"kind": "codex"}}, "accounts": {}},
                policy=lambda project: policy,
            )
            with mock.patch.object(config, "RUNS_DIR", os.path.join(d, "runs")), \
                    mock.patch.object(runner, "fence_hooks", return_value="/runs/42/hooks"):
                env = runner.run_env(ctx, "myproj", 99, "codex", 42, 7)

            self.assertEqual(env["MAHLER_EPOCH"], "7")
            self.assertEqual(env["MAHLER_RUN_ID"], "42")
            self.assertEqual(env["MAHLER_PROJECT"], "myproj")
            self.assertEqual(env["MAHLER_ISSUE"], "99")
            self.assertEqual(env["MAHLER_HOME"], config.STATE)
            self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
            self.assertEqual(env["GIT_CONFIG_KEY_0"], "core.hooksPath")
            self.assertEqual(env["GIT_CONFIG_VALUE_0"], "/runs/42/hooks")

    def test_run_env_applies_gh_overlay(self):
        with tempfile.TemporaryDirectory() as d:
            policy = {
                "path": os.path.join(d, "repo"), "repo": "acme/y", "base": "main",
                "account": "work", "link": [],
            }
            ctx = SimpleNamespace(
                cfg={
                    "platforms": {"claude-work": {"kind": "claude", "account": "work"}},
                    "accounts": {"work": {"env": {"GH_CONFIG_DIR": "~/.gh-work", "CLAUDE_CONFIG_DIR": "~/.claude-work"}}},
                },
                policy=lambda project: policy,
            )
            with mock.patch.object(config, "RUNS_DIR", os.path.join(d, "runs")), \
                    mock.patch.object(runner, "fence_hooks", return_value="/runs/1/hooks"):
                env = runner.run_env(ctx, "acme", 10, "claude-work", 1, 2)
            self.assertEqual(env["CLAUDE_CONFIG_DIR"], os.path.expanduser("~/.claude-work"))
            self.assertEqual(env["GH_CONFIG_DIR"], os.path.expanduser("~/.gh-work"))

    def test_app_backed_run_env_drops_ambient_credentials_and_uses_scoped_token(self):
        with tempfile.TemporaryDirectory() as d:
            repo_dir = os.path.join(d, "repo")
            os.makedirs(repo_dir, exist_ok=True)
            policy = {
                "path": repo_dir, "repo": "mkny13/mahler", "base": "main",
                "link": [], "rules": "", "run_timeout_minutes": 60,
            }
            app_mock = mock.MagicMock()
            app_mock.agent_token.return_value = "ghs_scoped_token_xyz123"
            app_mock.app_id = "12345"
            app_mock.installation_id = "67890"
            app_mock._key_path = "/secret/app_key.pem"
            app_mock._cache_dir = Path("/secret/cache")

            ctx = SimpleNamespace(
                cfg={
                    "platforms": {"codex": {"kind": "codex"}},
                    "accounts": {},
                    "github_app": {"app_id": 12345, "installation_id": 67890,
                                  "private_key_path": "/secret/app_key.pem"},
                },
                policy=lambda project: policy,
            )
            runs_dir = os.path.join(d, "runs")
            ambient_env = {
                "GH_TOKEN": "ambient_broad_personal_token",
                "GITHUB_TOKEN": "ambient_broad_oauth_token",
                "GH_CONFIG_DIR": os.path.join(d, "ambient_gh"),
                "GH_HOST": "ambient.github.com",
                "GITHUB_APP_ID": "12345",
                "GITHUB_APP_PRIVATE_KEY_PATH": "/secret/ambient_key.pem",
                "PATH": "/usr/bin:/bin",
            }
            with mock.patch.object(config, "RUNS_DIR", runs_dir), \
                    mock.patch.object(runner, "fence_hooks", return_value="/runs/42/hooks"), \
                    mock.patch.object(config, "project_github_app", return_value=app_mock), \
                    mock.patch.dict(os.environ, ambient_env, clear=True):
                env = runner.run_env(ctx, "mahler", 1144, "codex", 42, 7)

            # Scoped token requested for repository
            app_mock.agent_token.assert_called_once_with("mahler")

            # Scoped token and isolated config dir present
            self.assertEqual(env["GH_TOKEN"], "ghs_scoped_token_xyz123")
            expected_gh_config = os.path.join(runs_dir, "42", "gh")
            self.assertEqual(env["GH_CONFIG_DIR"], expected_gh_config)
            self.assertTrue(os.path.isdir(expected_gh_config))
            self.assertEqual(os.stat(expected_gh_config).st_mode & 0o777, 0o700)

            # None of ambient GitHub identity survives
            self.assertNotIn("GITHUB_TOKEN", env)
            self.assertNotIn("GH_HOST", env)
            self.assertNotIn("GITHUB_APP_ID", env)
            self.assertNotIn("GITHUB_APP_PRIVATE_KEY_PATH", env)
            self.assertNotIn("/secret/app_key.pem", env.values())
            self.assertNotIn("12345", env.values())
            self.assertNotIn("67890", env.values())

    def test_app_backed_run_env_git_credential_fallback_disabled(self):
        with tempfile.TemporaryDirectory() as d:
            repo_dir = os.path.join(d, "repo")
            os.makedirs(repo_dir, exist_ok=True)
            policy = {
                "path": repo_dir, "repo": "mkny13/mahler", "base": "main",
                "link": [], "rules": "", "run_timeout_minutes": 60,
            }
            app_mock = mock.MagicMock()
            app_mock.agent_token.return_value = "ghs_scoped_token_xyz123"

            ctx = SimpleNamespace(
                cfg={
                    "platforms": {"codex": {"kind": "codex"}},
                    "accounts": {},
                    "github_app": {"app_id": 12345, "installation_id": 67890,
                                  "private_key_path": "/secret/key.pem"},
                },
                policy=lambda project: policy,
            )
            runs_dir = os.path.join(d, "runs")
            global_config = os.path.join(d, "operator.gitconfig")
            with open(global_config, "w") as f:
                f.write('[credential]\nhelper = "!f() { echo operator-broad-secret; }; f"\n')
            askpass = os.path.join(d, "operator-askpass")
            with open(askpass, "w") as f:
                f.write("#!/bin/sh\nprintf '%s\\n' operator-broad-secret\n")
            os.chmod(askpass, 0o700)
            with open(global_config, "a") as f:
                f.write(f"\n[core]\n\taskPass = {askpass}\n")
            with mock.patch.object(config, "RUNS_DIR", runs_dir), \
                    mock.patch.object(runner, "fence_hooks", return_value="/runs/42/hooks"), \
                    mock.patch.object(config, "project_github_app", return_value=app_mock), \
                    mock.patch.dict(os.environ, {
                        "GIT_ASKPASS": askpass,
                        "SSH_ASKPASS": askpass,
                        "SSH_ASKPASS_REQUIRE": "force",
                        "GIT_CONFIG_GLOBAL": global_config,
                    }, clear=True):
                env = runner.run_env(ctx, "mahler", 1144, "codex", 42, 7)

            self.assertEqual(env["GIT_CONFIG_COUNT"], "3")
            self.assertEqual(env["GIT_CONFIG_KEY_0"], "core.hooksPath")
            self.assertEqual(env["GIT_CONFIG_VALUE_0"], "/runs/42/hooks")
            self.assertEqual(env["GIT_CONFIG_KEY_1"], "credential.helper")
            self.assertEqual(env["GIT_CONFIG_VALUE_1"], "")
            self.assertEqual(env["GIT_CONFIG_KEY_2"], "core.askPass")
            self.assertEqual(env["GIT_CONFIG_VALUE_2"], "")
            self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
            self.assertNotIn("GIT_ASKPASS", env)
            self.assertNotIn("SSH_ASKPASS", env)
            self.assertNotIn("SSH_ASKPASS_REQUIRE", env)

            # Negative/abuse test: git credential fill with ambient operator helper configured
            # must fail closed (code 128) and cannot select or return ambient credentials.
            global_config = os.path.join(d, "operator.gitconfig")
            with open(global_config, "w") as f:
                f.write('[credential]\nhelper = "!f() { echo password=operator-broad-secret; }; f"\n')
            askpass = os.path.join(d, "operator-askpass")
            with open(askpass, "w") as f:
                f.write("#!/bin/sh\nprintf '%s\\n' operator-broad-secret\n")
            os.chmod(askpass, 0o700)
            with open(global_config, "a") as f:
                f.write(f"\n[core]\n\taskPass = {askpass}\n")
            out = subprocess.run(["git", "credential", "fill"],
                                 input="protocol=https\nhost=github.com\n\n",
                                 env=env, capture_output=True, text=True)
            self.assertNotEqual(out.returncode, 0)
            self.assertNotIn("operator-broad-secret", out.stdout)

    def test_scoped_run_token_least_privilege_permissions(self):
        from mahler import github_app
        # Must not contain write permissions for contents or pull requests
        perms = github_app.AGENT_PERMISSIONS
        self.assertEqual(perms.get("contents"), "read")
        self.assertEqual(perms.get("pull_requests"), "read")
        self.assertNotIn("workflows", perms)
        self.assertNotIn("administration", perms)
        self.assertNotIn("secrets", perms)

    def test_explicit_provider_repo_mismatch_refuses_run_env(self):
        with tempfile.TemporaryDirectory() as d:
            repo_dir = os.path.join(d, "repo")
            os.makedirs(repo_dir, exist_ok=True)
            policy = {
                "path": repo_dir, "repo": "makastel_ncstate/sit-stand-walk", "base": "main",
                "account": "work", "github_provider": "work", "link": [], "rules": "",
                "run_timeout_minutes": 60,
            }
            ctx = SimpleNamespace(
                cfg={
                    "platforms": {"claude-work": {"kind": "claude", "account": "work"}},
                    "accounts": {"work": {"env": {"GH_CONFIG_DIR": "~/.config/gh-work"}}},
                    "github_apps": {"work": {
                        "app_id": 789, "installation_id": 1011,
                        "private_key_path": "/secret/work.pem",
                        "repositories": ["google-chat-nav"],
                    }},
                },
                policy=lambda project: policy,
                say=lambda msg: None,
            )
            with mock.patch.object(config, "RUNS_DIR", os.path.join(d, "runs")), \
                    mock.patch.object(runner, "fence_hooks", return_value="/runs/42/hooks"):
                with self.assertRaises(ValueError) as caught:
                    runner.run_env(ctx, "acme", 1144, "claude-work", 42, 7)
            for secret in ("/secret", "789", "1011"):
                self.assertNotIn(secret, str(caught.exception))

    def test_non_app_project_emits_warning_and_uses_transitional_path(self):
        with tempfile.TemporaryDirectory() as d:
            repo_dir = os.path.join(d, "repo")
            os.makedirs(repo_dir, exist_ok=True)
            policy = {
                "path": repo_dir, "repo": "acme/work-project", "base": "main",
                "account": "work", "link": [], "rules": "", "run_timeout_minutes": 60,
            }
            said = []
            ctx = SimpleNamespace(
                cfg={
                    "platforms": {"claude-work": {"kind": "claude", "account": "work"}},
                    "accounts": {"work": {"env": {"GH_CONFIG_DIR": "~/.config/gh-work"}}},
                },
                policy=lambda project: policy,
                say=lambda msg: said.append(msg),
            )
            runs_dir = os.path.join(d, "runs")
            with mock.patch.object(config, "RUNS_DIR", runs_dir), \
                    mock.patch.object(runner, "fence_hooks", return_value="/runs/42/hooks"), \
                    mock.patch.object(config, "project_github_app", return_value=None):
                env = runner.run_env(ctx, "acme", 1144, "claude-work", 42, 7)

            self.assertEqual(env["GH_CONFIG_DIR"], os.path.expanduser("~/.config/gh-work"))
            self.assertTrue(any("transitional" in s.lower() and "warning" in s.lower() for s in said),
                            f"Expected transitional warning in {said}")

    def test_undefined_account_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            repo_dir = os.path.join(d, "repo")
            os.makedirs(repo_dir, exist_ok=True)
            policy = {
                "path": repo_dir, "repo": "acme/work-project", "base": "main",
                "account": "undefined_work", "gh_account": "undefined_work",
                "link": [], "rules": "", "run_timeout_minutes": 60,
            }
            ctx = SimpleNamespace(
                cfg={
                    "platforms": {"claude": {"kind": "claude", "account": "personal"}},
                    "accounts": {},
                },
                policy=lambda project: policy,
            )
            runs_dir = os.path.join(d, "runs")
            with mock.patch.object(config, "RUNS_DIR", runs_dir), \
                    mock.patch.object(runner, "fence_hooks", return_value="/runs/42/hooks"), \
                    mock.patch.object(config, "project_github_app", return_value=None):
                with self.assertRaises(ValueError):
                    runner.run_env(ctx, "acme", 1144, "claude", 42, 7)

    def test_require_scoped_identity_blocks_ambient_credentials_and_personal_app(self):
        """Negative/abuse test: requiring scoped run identity refuses ambient credentials."""
        for project_name, repo_name in (
            ("google-chat-nav", "makastel_ncstate/google-chat-nav"),
            ("google-workspace-connector", "makastel_ncstate/google-workspace-connector"),
            ("sit-stand-walk", "makastel_ncstate/sit-stand-walk"),
        ):
            with self.subTest(project=project_name), tempfile.TemporaryDirectory() as d:
                repo_dir = os.path.join(d, "repo")
                os.makedirs(repo_dir, exist_ok=True)
                policy = {
                    "path": repo_dir,
                    "repo": repo_name,
                    "base": "main",
                    "account": "work",
                    "link": [],
                    "rules": "",
                    "run_timeout_minutes": 60,
                    "require_scoped_identity": True,
                }
                ctx = SimpleNamespace(
                    cfg={
                        "platforms": {"work-claude": {"kind": "claude", "account": "work"}},
                        "accounts": {"work": {"env": {"GH_CONFIG_DIR": "~/.config/gh-work"}}},
                        # Personal App is configured, but should NEVER be used for work repo
                        "github_app": {
                            "app_id": 12345,
                            "installation_id": 67890,
                            "private_key_path": "/secret/personal.pem",
                        },
                    },
                    policy=lambda p, pol=policy: pol,
                )
                runs_dir = os.path.join(d, "runs")
                ambient_env = {
                    "GH_TOKEN": "ambient_broad_personal_token",
                    "GITHUB_TOKEN": "ambient_broad_oauth_token",
                    "GH_CONFIG_DIR": os.path.join(d, "ambient_gh"),
                    "PATH": "/usr/bin:/bin",
                }
                with mock.patch.object(config, "RUNS_DIR", runs_dir), \
                        mock.patch.object(runner, "fence_hooks", return_value="/runs/42/hooks"), \
                        mock.patch.dict(os.environ, ambient_env, clear=True):
                    with self.assertRaises(ValueError) as caught:
                        runner.run_env(ctx, project_name, 1145, "work-claude", 42, 7)
                    self.assertIn(project_name, str(caught.exception))
                    self.assertNotIn("/secret", str(caught.exception))
                    self.assertNotIn("ambient_broad", str(caught.exception))

    def test_work_project_with_approved_work_provider_launches_scoped(self):
        with tempfile.TemporaryDirectory() as d:
            repo_dir = os.path.join(d, "repo")
            os.makedirs(repo_dir, exist_ok=True)
            policy = {
                "path": repo_dir,
                "repo": "makastel_ncstate/google-chat-nav",
                "base": "main",
                "account": "work",
                "github_provider": "work",
                "require_scoped_identity": True,
                "link": [],
                "rules": "",
                "run_timeout_minutes": 60,
            }
            app_mock = mock.MagicMock()
            app_mock.agent_token.return_value = "ghs_work_scoped_token"

            ctx = SimpleNamespace(
                cfg={
                    "platforms": {"work-claude": {"kind": "claude", "account": "work"}},
                    "accounts": {"work": {"env": {}}},
                    "github_apps": {
                        "work": {
                            "app_id": 9999,
                            "installation_id": 8888,
                            "private_key_path": "/secret/work.pem",
                        },
                    },
                },
                policy=lambda p: policy,
            )
            runs_dir = os.path.join(d, "runs")
            with mock.patch.object(config, "RUNS_DIR", runs_dir), \
                    mock.patch.object(runner, "fence_hooks", return_value="/runs/42/hooks"), \
                    mock.patch.object(config, "project_github_app", return_value=app_mock):
                env = runner.run_env(ctx, "google-chat-nav", 1145, "work-claude", 42, 7)

            app_mock.agent_token.assert_called_once_with("google-chat-nav")
            self.assertEqual(env["GH_TOKEN"], "ghs_work_scoped_token")
            self.assertTrue(env["GH_CONFIG_DIR"].endswith("gh"))
            self.assertEqual(env.get("GIT_CONFIG_KEY_1"), "credential.helper")
            self.assertEqual(env.get("GIT_CONFIG_VALUE_1"), "")

    def test_provider_selection_follows_project_not_platform_account(self):
        with tempfile.TemporaryDirectory() as d:
            repo_dir = os.path.join(d, "repo")
            os.makedirs(repo_dir, exist_ok=True)
            # Project uses work github identity, but runs on personal AI platform
            policy = {
                "path": repo_dir,
                "repo": "makastel_ncstate/google-chat-nav",
                "base": "main",
                "accounts": ["personal", "work"],
                "github_provider": "work",
                "require_scoped_identity": True,
                "link": [],
                "rules": "",
                "run_timeout_minutes": 60,
            }
            work_app_mock = mock.MagicMock()
            work_app_mock.agent_token.return_value = "ghs_work_scoped_token"

            ctx = SimpleNamespace(
                cfg={
                    "platforms": {"codex": {"kind": "codex", "account": "personal"}},
                    "accounts": {"personal": {"env": {}}, "work": {"env": {}}},
                    "github_app": {"app_id": 111, "installation_id": 222, "private_key_path": "/secret/p.pem"},
                    "github_apps": {"work": {"app_id": 999, "installation_id": 888, "private_key_path": "/secret/w.pem"}},
                },
                policy=lambda p: policy,
            )
            runs_dir = os.path.join(d, "runs")
            with mock.patch.object(config, "RUNS_DIR", runs_dir), \
                    mock.patch.object(runner, "fence_hooks", return_value="/runs/42/hooks"), \
                    mock.patch.object(config, "project_github_app", return_value=work_app_mock):
                env = runner.run_env(ctx, "google-chat-nav", 1145, "codex", 42, 7)

            work_app_mock.agent_token.assert_called_once_with("google-chat-nav")
            self.assertEqual(env["GH_TOKEN"], "ghs_work_scoped_token")


class SpawnStdinTests(unittest.TestCase):
    """runner.spawn supports stdin_path redirection (mahler#426)."""

    def test_spawn_with_stdin_path_quotes_and_redirects(self):
        evil_path = "/tmp/resume; rm -rf /; 'quote'.md"
        with mock.patch("subprocess.Popen", return_value=SimpleNamespace(pid=123)) as popen:
            pid = runner.spawn(["/bin/cmd", "arg1"], "/cwd", "/log", "/exit",
                               stdin_path=evil_path)
        self.assertEqual(pid, 123)
        shell = popen.call_args.args[0][2]
        self.assertIn(f"< {shlex.quote(evil_path)}", shell)
        self.assertIn("> /log 2>&1; echo $? > /exit", shell)

    def test_spawn_without_stdin_path_omits_redirection(self):
        with mock.patch("subprocess.Popen", return_value=SimpleNamespace(pid=123)) as popen:
            pid = runner.spawn(["/bin/cmd", "arg1"], "/cwd", "/log", "/exit")
        self.assertEqual(pid, 123)
        shell = popen.call_args.args[0][2]
        self.assertNotIn("<", shell)

    def test_ambient_run_marker_does_not_claim_an_unrelated_subprocess(self):
        with tempfile.TemporaryDirectory() as temp:
            os.makedirs(os.path.join(temp, "runs", "42"))
            env = dict(os.environ, MAHLER_RUN_ID="42",
                       MAHLER_RUNS_DIR=os.path.join(temp, "runs"))
            with mock.patch.object(runner.sys, "platform", "darwin"), \
                    mock.patch("subprocess.Popen",
                               return_value=SimpleNamespace(pid=123)) as popen:
                runner.spawn(["/bin/cmd"], "/cwd", "/log",
                             os.path.join(temp, "unrelated", "exit"), env=env)
        self.assertEqual(popen.call_count, 1)
        self.assertIs(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)


class MarkerDescriptorTests(unittest.TestCase):
    """runner.spawn marker descriptor lifecycle and fault-injection tests (mahler#1656)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runs_dir = Path(self.tmp.name) / "runs"
        self.run_id = "165601"
        self.run_dir = self.runs_dir / self.run_id
        self.run_dir.mkdir(parents=True)
        self.log_path = self.run_dir / "agent.log"
        self.status_path = self.run_dir / "exit"
        self.env = {
            "MAHLER_RUN_ID": self.run_id,
            "MAHLER_RUNS_DIR": str(self.runs_dir),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def _track_descriptors(self):
        opened_marker_fds = []
        closed_marker_fds = []
        real_open = os.open
        real_close = os.close
        marker_inodes = {}

        def tracking_open(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if "process-marker" in str(path):
                opened_marker_fds.append(fd)
                marker_inodes[fd] = os.fstat(fd).st_ino
            return fd

        def tracking_close(fd):
            if fd in opened_marker_fds:
                # The fd number may be reused later; count only the marker file.
                try:
                    same = os.fstat(fd).st_ino == marker_inodes[fd]
                except OSError:
                    same = False
                if same:
                    closed_marker_fds.append(fd)
            return real_close(fd)

        def is_open(fd):
            try:
                os.fstat(fd)
                return True
            except OSError:
                return False

        def cleanup_leaked():
            for fd in opened_marker_fds:
                if is_open(fd):
                    try:
                        real_close(fd)
                    except OSError:
                        pass

        self.addCleanup(cleanup_leaked)
        return opened_marker_fds, closed_marker_fds, tracking_open, tracking_close, is_open

    def test_marker_fchmod_failure_closes_raw_descriptor(self):
        opened, closed, tr_open, tr_close, is_open = self._track_descriptors()

        def fault_fchmod(fd, mode):
            raise OSError(13, "fchmod permission denied fault")

        with mock.patch.object(runner.sys, "platform", "darwin"), \
                mock.patch("os.open", side_effect=tr_open), \
                mock.patch("os.close", side_effect=tr_close), \
                mock.patch("os.fchmod", side_effect=fault_fchmod):
            with self.assertRaises(OSError) as ctx:
                runner.spawn(["/bin/cmd"], self.tmp.name, str(self.log_path),
                             str(self.status_path), env=self.env)

        self.assertIn("fchmod permission denied fault", str(ctx.exception))
        self.assertEqual(len(opened), 1, "Expected marker file to be opened")
        marker_fd = opened[0]
        self.assertFalse(is_open(marker_fd), f"Marker descriptor {marker_fd} was leaked after fchmod failure")
        self.assertEqual(closed.count(marker_fd), 1, "Expected raw marker descriptor to be closed exactly once")

    def test_marker_fdopen_failure_closes_raw_descriptor(self):
        opened, closed, tr_open, tr_close, is_open = self._track_descriptors()

        def fault_fdopen(fd, *args, **kwargs):
            raise OSError(24, "fdopen EMFILE fault")

        with mock.patch.object(runner.sys, "platform", "darwin"), \
                mock.patch("os.open", side_effect=tr_open), \
                mock.patch("os.close", side_effect=tr_close), \
                mock.patch("os.fdopen", side_effect=fault_fdopen):
            with self.assertRaises(OSError) as ctx:
                runner.spawn(["/bin/cmd"], self.tmp.name, str(self.log_path),
                             str(self.status_path), env=self.env)

        self.assertIn("fdopen EMFILE fault", str(ctx.exception))
        self.assertEqual(len(opened), 1, "Expected marker file to be opened")
        marker_fd = opened[0]
        self.assertFalse(is_open(marker_fd), f"Marker descriptor {marker_fd} was leaked after fdopen failure")
        self.assertEqual(closed.count(marker_fd), 1, "Expected raw marker descriptor to be closed exactly once")

    def test_marker_popen_failure_closes_stream_without_double_close(self):
        opened, closed, tr_open, tr_close, is_open = self._track_descriptors()

        with mock.patch.object(runner.sys, "platform", "darwin"), \
                mock.patch("os.open", side_effect=tr_open), \
                mock.patch("os.close", side_effect=tr_close), \
                mock.patch("subprocess.Popen", side_effect=OSError(2, "Popen ENOENT fault")):
            with self.assertRaises(OSError) as ctx:
                runner.spawn(["/bin/cmd"], self.tmp.name, str(self.log_path),
                             str(self.status_path), env=self.env)

        self.assertIn("Popen ENOENT fault", str(ctx.exception))
        self.assertEqual(len(opened), 1, "Expected marker file to be opened")
        marker_fd = opened[0]
        self.assertFalse(is_open(marker_fd), f"Marker descriptor {marker_fd} was leaked after Popen failure")
        self.assertEqual(closed.count(marker_fd), 0, "Double-close: os.close was called in addition to stream.close()")

    def test_marker_success_closes_parent_copy_and_passes_fds(self):
        opened, closed, tr_open, tr_close, is_open = self._track_descriptors()
        popen_calls = []
        ready = self.run_dir / "process-tracker-ready"

        def mock_popen(*args, **kwargs):
            p = mock.MagicMock()
            p.pid = 98765
            p.poll.return_value = None
            popen_calls.append((args, kwargs))
            ready.touch()
            return p

        with mock.patch.object(runner.sys, "platform", "darwin"), \
                mock.patch("os.open", side_effect=tr_open), \
                mock.patch("os.close", side_effect=tr_close), \
                mock.patch("subprocess.Popen", side_effect=mock_popen):
            pid = runner.spawn(["/bin/cmd"], self.tmp.name, str(self.log_path),
                               str(self.status_path), env=self.env)

        self.assertEqual(pid, 98765)
        self.assertEqual(len(opened), 1)
        marker_fd = opened[0]
        self.assertFalse(is_open(marker_fd), "Parent marker copy was not closed after spawn")
        self.assertEqual(closed.count(marker_fd), 0, "Double-close on success path")

        child_args, child_kwargs = popen_calls[0]
        self.assertEqual(child_kwargs.get("pass_fds"), (marker_fd,))
        self.assertIsNotNone(child_kwargs.get("stdin"))
        self.assertNotEqual(child_kwargs.get("stdin"), subprocess.DEVNULL)

    def test_marker_inheritance_real_subprocess(self):
        if sys.platform != "darwin":
            self.skipTest("Darwin marker inheritance test")
        out_file = self.run_dir / "child_marker_info.txt"
        processes = []
        real_popen = subprocess.Popen

        def tracked_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process

        child_code = (
            "import os, pathlib, sys\n"
            f"out = pathlib.Path({str(out_file)!r})\n"
            "stdin_ino = os.fstat(0).st_ino\n"
            "out.write_text(f'{stdin_ino}')\n"
            "sys.exit(0)\n"
        )
        with mock.patch.object(runner.subprocess, "Popen", side_effect=tracked_popen):
            runner.spawn([sys.executable, "-c", child_code], self.tmp.name,
                         str(self.log_path), str(self.status_path), env=self.env)
        deadline = time.monotonic() + 5
        while not self.status_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.status_path.exists(), "Child process did not finish")
        for process in processes:
            process.wait(timeout=5)
        marker_path = self.run_dir / "process-marker"
        self.assertTrue(marker_path.exists())
        self.assertTrue(out_file.exists())
        child_stdin_ino = int(out_file.read_text().strip())
        self.assertEqual(child_stdin_ino, marker_path.stat().st_ino)


class DetachedProcessReapingTests(unittest.TestCase):
    def test_fast_exit_reaps_detached_native_helper(self):
        """A native helper must not escape between its fork and the next poll."""
        with tempfile.TemporaryDirectory() as temp:
            run_id = "91597001"
            run_dir = os.path.join(temp, run_id)
            os.makedirs(run_dir)
            helper_path = os.path.join(run_dir, "helper.pid")
            log_path = os.path.join(run_dir, "agent.log")
            status_path = os.path.join(run_dir, "exit")
            code = (
                "import os, subprocess; "
                f"p=subprocess.Popen(['/usr/bin/tail','-f','/dev/null'], "
                "start_new_session=True, stdout=subprocess.DEVNULL, "
                "stderr=subprocess.DEVNULL); "
                f"open({helper_path!r}, 'w').write(str(p.pid))"
            )
            env = dict(os.environ, MAHLER_RUN_ID=run_id, MAHLER_RUNS_DIR=temp)
            helper_pid = None
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", ResourceWarning)
                    runner.spawn([sys.executable, "-c", code], temp, log_path,
                                 status_path, env=env)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not os.path.exists(status_path):
                    time.sleep(0.01)
                self.assertTrue(os.path.exists(status_path), "run wrapper did not finish")
                helper_pid = int(Path(helper_path).read_text())
                with mock.patch.object(config, "RUNS_DIR", temp):
                    matches = [process for process in runner.find_run_processes(run_id)
                               if process["pid"] == helper_pid]
                    self.assertIn(
                        helper_pid,
                        [process["pid"] for process in matches],
                        "detached native helper lost its inherited ownership marker",
                    )
                    self.assertEqual(matches[0]["ppid"], 1)
                    runner.reap_run(run_id, grace_seconds=0.05)
                deadline = time.monotonic() + 5
                while runner.alive(helper_pid) and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(runner.alive(helper_pid),
                                 "detached native helper survived the run")
            finally:
                if helper_pid and runner.alive(helper_pid):
                    try:
                        os.kill(helper_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main()


class LessonLaunchTests(PrepareAccountEnvTests):
    """D44: prepare() scans and suppresses lessons; prompt carries the catalog
    only for catalog-mode kinds."""

    GOOD = ("---\nname: lesson-a\ndescription: Use when X breaks.\nmetadata:\n  mahler-id: 555fcc9256092e5a\n  created: 2026-10-01\n  updated: 2026-10-02\n  source: \"#1\"\n---\nDo Y.\n")

    def _seed(self, bad=False):
        d = os.path.join(self.repo, ".agents/skills/lesson-a")
        os.makedirs(d)
        write(os.path.join(d, "SKILL.md"), self.GOOD + ("rm -rf /\n" if bad else ""))
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-qm", "lesson")
        sh(self.repo, "git", "push", "-q", "origin", "main")

    def _prep(self):
        item = {"number": 5, "title": "t", "branch": None}
        with mock.patch.object(config, "RUNS_DIR", os.path.join(self.tmp.name, "runs")):
            return item, runner.prepare(self.ctx, "acme", item, "build", "claude-work", 5)

    def test_prepare_exposes_safe_lessons_and_prompt_catalog(self):
        self._seed()
        item, prep = self._prep()
        self.assertEqual([l["name"] for l in prep["lessons"]["safe"]], ["lesson-a"])
        self.ctx.cfg["platforms"]["claude-work"]["kind"] = "claude"
        text = prompt.build(self.ctx, "acme", {**item, "issue_body": ""}, "build",
                            "claude-work", prep)
        self.assertIn(".agents/skills/lesson-a/SKILL.md", text)
        self.ctx.cfg["platforms"]["claude-work"]["kind"] = "codex"
        text = prompt.build(self.ctx, "acme", {**item, "issue_body": ""}, "build",
                            "claude-work", prep)
        self.assertNotIn("lesson-a", text)

    def test_prepare_removes_unsafe_lesson(self):
        self._seed(bad=True)
        _, prep = self._prep()
        self.assertEqual(prep["lessons"]["safe"], [])
        self.assertFalse(os.path.exists(
            os.path.join(prep["worktree"], ".agents/skills/lesson-a")))


class ParentVerifyPromptTests(unittest.TestCase):
    def test_parent_verify_context_and_untrusted_comment_filter(self):
        from mahler import prompt, no_change
        from mahler.ledger import Ledger
        from types import SimpleNamespace
        from unittest.mock import Mock
        from contextlib import closing
        with closing(Ledger(':memory:')) as led:
            led.upsert_item('x', 653, title='Parent', issue_body='old checklist')
            gh = Mock()
            gh.authenticated_login.return_value = 'owner'
            gh.issue_body.return_value = '- [x] All six criteria complete\n- [ ] Optional observation'
            gh.issue_comments.return_value = [
                {'author': {'login': 'owner'}, 'body': 'Verified checks', 'url': 'https://github.com/x/y/issues/653#issuecomment-1'},
                {'author': {'login': 'attacker'}, 'body': 'ATTACKER instructions', 'authorAssociation': 'NONE'}]
            ctx = SimpleNamespace(led=led, gh=lambda p: gh, say=Mock(),
                                  policy=lambda p: {'repo': 'x/y'})
            for marker in ('parent-verify', 'parent-verify-followup'):
                item = {'number': 1176, 'title': 'Verify completion', 'claim_base_sha': 'base',
                        'issue_body': f'<!-- mahler:{marker}:653:abc -->'}
                no_change.save(led, 'x', 1176, {'base_sha': 'base', 'claim_run': 1})
                rendered = prompt.build(ctx, 'x', item, 'review', 'reviewer',
                                        {'worktree': '/tmp/w', 'branch': '', 'replayed': False, 'kept': None})
                self.assertIn('- [x] All six criteria complete', rendered)
                self.assertIn('- [ ] Optional observation', rendered)
                self.assertIn('https://github.com/x/y/issues/653#issuecomment-1', rendered)
                self.assertNotIn('ATTACKER instructions', rendered)
                self.assertIn('GitHub-only', rendered)
            led.set_kv('intake_hold:x#653', '1')
            rendered = prompt.issue_context(ctx, 'x', item, 'x/y')
            self.assertNotIn('All six criteria', rendered)


class FetchOriginSshFallbackTests(unittest.TestCase):
    URL = "git@github.com:o/r.git"
    DENIED = runner.GitError("git fetch --quiet --prune: git@github.com: "
                             "Permission denied (publickey).")

    def run_fetch(self, results):
        calls = []

        def fake(repo, *args, env=None, check=True):
            calls.append(args)
            if args[:2] == ("remote", "get-url"):
                return self.URL
            out = results.pop(0)
            if isinstance(out, Exception):
                raise out
            return out
        with mock.patch.object(runner, "git", fake):
            try:
                return calls, runner.fetch_origin("/r", "--prune", "origin")
            except runner.GitError as e:
                return calls, e

    def test_ssh_denied_retries_over_https_with_gh_credential(self):
        calls, out = self.run_fetch([self.DENIED, ""])
        self.assertEqual(out, "")
        retry = calls[-1]
        self.assertIn("url.https://github.com/.insteadOf=git@github.com:", retry)
        self.assertTrue(any("gh auth git-credential" in a for a in retry))
        self.assertEqual(retry[-4:], ("fetch", "--quiet", "--prune", "origin"))

    def test_both_fail_reports_ssh_and_https(self):
        _, err = self.run_fetch([self.DENIED, runner.GitError("git fetch: 401")])
        self.assertIsInstance(err, runner.GitError)
        self.assertIn("publickey", str(err))
        self.assertIn("HTTPS fallback failed", str(err))

    def test_other_errors_and_non_ssh_remotes_do_not_fall_back(self):
        calls, err = self.run_fetch([runner.GitError("git fetch: timed out")])
        self.assertEqual(str(err), "git fetch: timed out")
        self.assertEqual(len(calls), 1)
        self.URL = "https://github.com/o/r.git"
        calls, err = self.run_fetch([self.DENIED])
        self.assertIs(err, self.DENIED)

class DetachedRunTests(unittest.TestCase):
    def test_detached_native_child_after_leader_exit_is_reaped_on_darwin(self):
        import signal
        import sys
        import time
        import uuid
        import warnings
        if sys.platform != "darwin":
            self.skipTest("Darwin native executables can hide inherited environment")
        marker = str(uuid.uuid4().int)
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp) / "runs"
            run_dir = runs_dir / marker
            worktree = Path(tmp) / "project" / f"1-run{marker}"
            run_dir.mkdir(parents=True)
            worktree.mkdir(parents=True)
            pid_path = run_dir / "pid"
            release_path = run_dir / "release"
            log_path = run_dir / "agent.log"
            status_path = run_dir / "exit"
            child_code = ("import os,pathlib,subprocess,time\n"
                          "p=subprocess.Popen(['/usr/bin/tail','-f','/dev/null'], "
                          "start_new_session=True, stdout=subprocess.DEVNULL, "
                          "stderr=subprocess.DEVNULL)\n"
                          f"pathlib.Path({str(pid_path)!r}).write_text(str(p.pid))\n"
                          f"release=pathlib.Path({str(release_path)!r})\n"
                          "while not release.exists():\n    time.sleep(.01)\n"
                          "os._exit(0)\n")
            env = dict(os.environ, MAHLER_RUN_ID=marker,
                       MAHLER_RUNS_DIR=str(runs_dir))
            with mock.patch.object(config, "RUNS_DIR", str(runs_dir)), \
                    warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)
                leader = runner.spawn([sys.executable, "-c", child_code], str(worktree),
                                      str(log_path), str(status_path), env=env)
            pid = None
            try:
                deadline = time.monotonic() + 5
                while not pid_path.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(pid_path.exists())
                pid = int(pid_path.read_text())
                with mock.patch.object(config, "RUNS_DIR", str(runs_dir)):
                    while (pid not in [p["pid"] for p in runner.find_run_processes(marker)]
                           and time.monotonic() < deadline):
                        time.sleep(.02)
                    self.assertIn(pid, [p["pid"] for p in runner.find_run_processes(marker)])
                release_path.touch()
                while not status_path.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(status_path.exists())
                os.waitpid(leader, 0)
                self.assertFalse(runner.alive(leader))
                with mock.patch.object(config, "RUNS_DIR", str(runs_dir)):
                    found = runner.find_run_processes(marker)
                    self.assertIn(pid, [p["pid"] for p in found])
                    self.assertEqual(next(p for p in found if p["pid"] == pid)["ppid"], 1)
                    runner.reap_run(marker, grace_seconds=.05)
                deadline = time.monotonic() + 5
                while runner.alive(pid) and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertFalse(runner.alive(pid))
            finally:
                release_path.touch(exist_ok=True)
                try:
                    os.killpg(leader, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(leader, 0)
                except ChildProcessError:
                    pass
                if pid is not None:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_detached_child_after_leader_exit_is_reaped(self):
        import sys
        import signal
        import time
        import uuid
        marker = str(uuid.uuid4().int)
        with tempfile.TemporaryDirectory() as tmp:
            ready = Path(tmp) / "ready"
            child_code = ("import signal,time,pathlib; "
                          "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                          f"pathlib.Path({str(ready)!r}).touch(); time.sleep(60)")
            leader_code = ("import subprocess,sys; "
                           f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}], "
                           "start_new_session=True, stdout=subprocess.DEVNULL, "
                           "stderr=subprocess.DEVNULL); print(p.pid)")
            result = subprocess.run([sys.executable, "-c", leader_code],
                                    env=dict(os.environ, MAHLER_RUN_ID=marker),
                                    capture_output=True, text=True, check=True)
            pid = int(result.stdout)
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(ready.exists())
                self.assertIn(pid, [p["pid"] for p in runner.find_run_processes(marker)])
                runner.reap_run(marker, grace_seconds=.05)
                deadline = time.monotonic() + 5
                while runner.find_run_processes(marker) and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertEqual(runner.find_run_processes(marker), [])
            finally:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

class RunProcessSafetyTests(unittest.TestCase):
    def test_darwin_ownership_records_require_matching_uid_and_start_identity(self):
        marker = "123"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / marker
            run_dir.mkdir()
            path = run_dir / runner.OWNERSHIP_FILE
            records = {
                900007: dict(pid=900007, uid=os.getuid(), identity="start-a"),
                900008: dict(pid=900008, uid=os.getuid(), identity="old-start"),
                900009: dict(pid=900009, uid=os.getuid() + 1, identity="start-c"),
            }
            runner._write_ownership(path, marker, records)
            rows = {
                900007: dict(pid=900007, ppid=1, uid=os.getuid(), identity="start-a",
                             command="/usr/bin/tail -f /dev/null", run_id=None),
                900008: dict(pid=900008, ppid=1, uid=os.getuid(), identity="new-start",
                             command="/usr/bin/tail -f /dev/null", run_id=None),
                900009: dict(pid=900009, ppid=1, uid=os.getuid(), identity="start-c",
                             command="/usr/bin/tail -f /dev/null", run_id=None),
                900010: dict(pid=900010, ppid=1, uid=os.getuid(), identity="start-d",
                             command="/usr/bin/tail -f /dev/null", run_id=None),
            }
            with mock.patch.object(config, "RUNS_DIR", tmp), \
                    mock.patch.object(runner.sys, "platform", "darwin"), \
                    mock.patch.object(runner, "_process_snapshot", return_value=rows):
                self.assertEqual([p["pid"] for p in runner.find_run_processes(marker)],
                                 [900007])

    def test_darwin_unmarked_process_in_run_worktree_is_not_owned_by_run(self):
        import signal
        import sys
        import uuid
        if sys.platform != "darwin":
            self.skipTest("Darwin-specific worktree ownership regression")
        marker = str(uuid.uuid4().int)
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp) / "runs"
            run_dir = runs_dir / marker
            worktree = Path(tmp) / "project" / f"1-run{marker}"
            run_dir.mkdir(parents=True)
            worktree.mkdir(parents=True)
            # Reproduce the stale provenance file created by the unsafe
            # implementation; CWD is not evidence that this process belongs
            # to the run.
            (run_dir / "process-root").write_text(str(worktree) + "\n")
            env = dict(os.environ)
            env.pop("MAHLER_RUN_ID", None)
            process = subprocess.Popen(
                ["/usr/bin/tail", "-f", "/dev/null"], cwd=worktree, env=env,
                start_new_session=True, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            try:
                with mock.patch.object(config, "RUNS_DIR", str(runs_dir)):
                    self.assertNotIn(
                        process.pid,
                        [p["pid"] for p in runner.find_run_processes(marker)])
            finally:
                try:
                    os.kill(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)

    def test_excludes_ancestors_services_other_users_and_unmarked(self):
        def row(pid, **kw):
            return dict(pid=pid, ppid=kw.get("ppid", 1), uid=kw.get("uid", os.getuid()),
                        identity="start", command=kw.get("command", "python helper.py"),
                        run_id=kw.get("run_id", "123"))
        rows = {p["pid"]: p for p in [
            row(1), row(os.getpid()), row(os.getppid(), ppid=900001),
            row(900001), row(900002, command="python -m mahler serve"),
            row(900003, command="/usr/local/bin/mahlerd"),
            row(900004, uid=os.getuid()+1), row(900005, run_id=None),
            row(900006, run_id="1234"), row(900007),
            row(900008, command="/opt/bin/mahler-launcher"),
            row(900009, command="python -m mahler tick"),
        ]}
        with mock.patch.object(runner, "_process_snapshot", return_value=rows):
            self.assertEqual([p["pid"] for p in runner.find_run_processes(123)], [900007])
            self.assertEqual(runner.find_run_processes(123, ppid=8), [])

    def test_pid_reuse_never_gets_signalled(self):
        original = dict(pid=900007, identity="old")
        replacement = dict(pid=900007, identity="new")
        with mock.patch.object(runner, "find_run_processes",
                               side_effect=[[original], [replacement], []]), \
                mock.patch.object(runner.os, "kill") as kill:
            runner.reap_run(123, grace_seconds=0)
        kill.assert_not_called()

    def test_term_precedes_kill_even_with_zero_grace(self):
        import signal
        process = dict(pid=900007, identity="start")
        with mock.patch.object(runner, "find_run_processes", return_value=[process]), \
                mock.patch.object(runner.os, "kill") as kill:
            runner.reap_run(123, grace_seconds=0)
        self.assertEqual(kill.call_args_list,
                         [mock.call(900007, signal.SIGTERM),
                          mock.call(900007, signal.SIGKILL)])

    def test_darwin_marker_in_argv_is_not_environment(self):
        prefix = "900007 1 501 Wed Nov 18 13:00:00 2026 "
        command = "python helper.py MAHLER_RUN_ID=123"
        plain = prefix + command + "\n"
        expanded = prefix + command + " HOME=/tmp MAHLER_RUN_ID=456 PATH=/bin\n"
        with mock.patch.object(runner.sys, "platform", "darwin"), \
                mock.patch.object(runner.subprocess, "run", side_effect=[
                    SimpleNamespace(returncode=0, stdout=plain),
                    SimpleNamespace(returncode=0, stdout=expanded)]):
            self.assertEqual(runner._process_snapshot()[900007]["run_id"], "456")

    def test_linux_reads_nul_environment_and_start_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "900007"
            path.mkdir()
            (path / "stat").write_text("900007 (name with ) parens) S 1 " +
                                      " ".join(["0"] * 17) + " 999 0")
            (path / "environ").write_bytes(b"OTHER=MAHLER_RUN_ID=456\0MAHLER_RUN_ID=123\0")
            (path / "cmdline").write_bytes(b"python\0helper.py\0")
            with mock.patch.object(runner.sys, "platform", "linux"), \
                    mock.patch.object(runner.Path, "glob", return_value=[path]):
                row = runner._process_snapshot()[900007]
            self.assertEqual(row["run_id"], "123")
            self.assertEqual(row["identity"], "999")
            self.assertEqual(row["ppid"], 1)
