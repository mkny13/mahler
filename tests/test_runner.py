"""Snapshots must save everything a run left without touching its worktree (DESIGN D6/D9)."""

import os
import shlex
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

from mahler import config, prompt, runner, no_change
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
        }
        self.ctx = SimpleNamespace(
            cfg={"platforms": {"claude-work": {"account": "work"}},
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
        self.assertNotIn("gh pr", text)
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
            # An active checkout must fail rather than give the fix a new branch.
            self.ctx.led = mock.Mock()
            self.ctx.led.active_runs.return_value = [{"worktree": prep["worktree"]}]
            with self.assertRaises(runner.GitError):
                runner.prepare(self.ctx, "acme", item, "fix", "claude-work", 2)

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


if __name__ == "__main__":
    unittest.main()
