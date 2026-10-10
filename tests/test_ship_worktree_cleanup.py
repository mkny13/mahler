"""Interactive cleanup uses only recorded paths and the merged PR's topic head."""
import copy
import contextlib
import io
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from mahler import cli, config, runner, scheduler, ship
from mahler.ledger import Ledger


class CleanupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.wt = tmp.name
        self.listing = ("worktree /primary\0HEAD base\0branch refs/heads/main\0\0"
                        f"worktree {self.wt}\0HEAD topic\0branch refs/heads/topic\0\0")
        self.calls = []
        self.dirty = ""
        self.head = "topic"

    def git(self, repo, *args):
        self.calls.append((repo, *args))
        if args[:2] == ("worktree", "list"):
            return self.listing
        if args[0] == "status":
            return self.dirty
        if args[0] == "rev-parse":
            return self.head
        return ""

    def cleanup(self, **kw):
        with mock.patch.object(runner, "git", side_effect=self.git):
            return runner.cleanup_interactive_worktree(
                "/primary", kw.get("wt", self.wt), kw.get("branch", "topic"),
                kw.get("head", "topic"))

    def assert_no_removal(self):
        self.assertFalse(any(c[1:3] == ("worktree", "remove") or c[1] == "branch"
                             for c in self.calls))

    def test_success_removes_only_recorded_tree_without_force_then_branch(self):
        self.assertIn("removed interactive", self.cleanup())
        self.assertEqual(self.calls[-3:], [
            ("/primary", "worktree", "remove", self.wt),
            ("/primary", "rev-parse", "refs/heads/topic"),
            ("/primary", "branch", "-D", "--", "topic")])

    def test_dirty_untracked_and_ignored_files_are_preserved(self):
        for status in (" M file", "?? untracked", "!! .env"):
            self.dirty = status
            self.assertIn("uncommitted or ignored", self.cleanup())
            self.assertIn((self.wt, "status", "--porcelain", "--untracked-files=all", "--ignored"),
                          self.calls)
            self.assert_no_removal()

    def test_git_status_reports_ignored_local_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            runner.git(str(repo), "init")
            (repo / ".gitignore").write_text(".env\n")
            runner.git(str(repo), "add", ".gitignore")
            runner.git(str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                       "commit", "-m", "ignore local environment")
            (repo / ".env").write_text("local-only")
            status = runner.git(str(repo), "status", "--porcelain", "--untracked-files=all",
                                "--ignored")
            self.assertIn("!! .env", status)

    def test_changed_head_is_preserved(self):
        self.head = "new-work"
        self.assertIn("HEAD differs", self.cleanup())
        self.assert_no_removal()

    def test_missing_directory_or_evidence_is_preserved(self):
        for args in ({"wt": self.wt + "/missing"}, {"head": None}, {"branch": None}):
            self.assertIn("skipped", self.cleanup(**args))
            self.assert_no_removal()

    def test_reused_or_primary_worktree_is_preserved(self):
        for branch in ("different", "main"):
            self.assertIn("no longer holds", self.cleanup(branch=branch))
            self.assert_no_removal()

    def test_git_failure_is_a_notice(self):
        with mock.patch.object(runner, "git", side_effect=runner.GitError("locked")):
            notice = runner.cleanup_interactive_worktree("/primary", self.wt, "topic", "topic")
        self.assertIn("skipped", notice)
        self.assertIn("locked", notice)

    def test_failed_removal_does_not_delete_branch(self):
        def fail(repo, *args):
            if args[:2] == ("worktree", "remove"):
                raise runner.GitError("locked")
            return self.git(repo, *args)
        with mock.patch.object(runner, "git", side_effect=fail):
            self.assertIn("locked", runner.cleanup_interactive_worktree(
                "/primary", self.wt, "topic", "topic"))
        self.assertFalse(any(c[1] == "branch" for c in self.calls))

    def test_detection_handles_unusual_paths_and_excludes_primary(self):
        path = '/tmp/a space\nand "quote"'
        self.listing = self.listing.replace(self.wt, path)
        with mock.patch.object(runner, "git", side_effect=self.git):
            self.assertEqual(runner.find_worktree_for_branch("/primary", "topic"), path)
            self.assertIsNone(runner.find_worktree_for_branch("/primary", "main"))


class ShipIntegrationTests(unittest.TestCase):
    def test_ship_records_and_confirmed_merge_cleans_recorded_checkout(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.closing(Ledger(":memory:")) as led:
            repo, wt = Path(tmp) / "repo", Path(tmp) / "session"
            repo.mkdir()
            wt.mkdir()
            runner.git(str(repo), "init", "--initial-branch=main")
            runner.git(str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                       "commit", "--allow-empty", "-m", "initial")
            head = runner.git(str(repo), "rev-parse", "HEAD")
            runner.git(str(repo), "branch", "topic")
            # Construct a linked-worktree fixture without running worktree add/remove.
            admin = repo / ".git" / "worktrees" / "session"
            admin.mkdir(parents=True)
            (admin / "HEAD").write_text("ref: refs/heads/topic\n")
            (admin / "commondir").write_text("../..\n")
            (admin / "gitdir").write_text(str(wt / ".git") + "\n")
            (wt / ".git").write_text(f"gitdir: {admin}\n")
            runner.git(str(wt), "read-tree", "HEAD")
            cfg = copy.deepcopy(config.DEFAULTS)
            cfg["projects"]["x"] = {"repo": "x/y", "path": str(repo)}
            led.upsert_item("x", 7, title="topic")
            led.claim("x", 7, "interactive:me", "interactive", 30)
            view = {"state": "OPEN", "headRefName": "topic", "headRefOid": head,
                    "body": "Fixes #7", "title": "topic"}
            gh = mock.Mock()
            gh.pr_view.return_value = view
            args = SimpleNamespace(item=("x", 7), holder="me", pr=9, branch=None, summary=None)
            with mock.patch.object(cli, "project_client", return_value=gh), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.cmd_ship(args, cfg, led), 0)
            self.assertEqual(led.get_kv("ship_worktree:x#7"), str(wt))
            self.assertEqual(led.get_kv("ship_worktree_branch:x#7"), "topic")
            self.assertIsNone(led.lease("x", 7))
            ctx = scheduler.Ctx(cfg, led, dry_run=False)
            real_git = runner.git
            removals = []

            def remove_fixture(path, *cmd):
                if cmd[:2] == ("worktree", "remove"):
                    self.assertEqual(cmd, ("worktree", "remove", str(wt)))
                    removals.append(cmd)
                    shutil.rmtree(wt)
                    shutil.rmtree(admin)
                    return ""
                return real_git(path, *cmd)

            with mock.patch.object(runner, "git", side_effect=remove_fixture), \
                    mock.patch.object(ctx, "say"), mock.patch.object(ctx, "ping"), \
                    mock.patch.object(ctx, "gh", return_value=gh), \
                    mock.patch.object(ship, "record_uat_if_needed", return_value=None), \
                    mock.patch.object(ship, "record_release_item_if_needed"), \
                    mock.patch("mahler.post_merge.register"), \
                    mock.patch.object(ship, "mirror_shipped"):
                with self.assertRaises(ValueError):
                    ship._shipped(ctx, "x", 7, 9, led.item("x", 7), view)
                self.assertTrue(wt.exists())
                view["state"] = "MERGED"
                view["mergeCommit"] = {"oid": "different-squash-sha"}
                ship._shipped(ctx, "x", 7, 9, led.item("x", 7), view)
            self.assertEqual(len(removals), 1)
            self.assertFalse(wt.exists())
            self.assertEqual(real_git(str(repo), "branch", "--list", "topic"), "")
            self.assertIsNone(led.get_kv("ship_worktree:x#7"))
            self.assertEqual(led.item("x", 7)["state"], "shipped")

    def test_unrecorded_autonomous_work_is_untouched(self):
        with contextlib.closing(Ledger(":memory:")) as led, mock.patch.object(runner, "cleanup_interactive_worktree") as cleanup:
            ctx = SimpleNamespace(led=led)
            ship._cleanup_interactive_worktree(ctx, "x", 7, {"headRefOid": "head"})
            cleanup.assert_not_called()

    def test_skip_notice_clears_record(self):
        with contextlib.closing(Ledger(":memory:")) as led:
            led.set_kv("ship_worktree:x#7", "/missing")
            led.set_kv("ship_worktree_branch:x#7", "topic")
            ctx = SimpleNamespace(led=led, policy=lambda p: {"path": "/repo"}, say=mock.Mock())
            ship._cleanup_interactive_worktree(ctx, "x", 7, {"headRefOid": "head"})
            self.assertIn("no longer exists", ctx.say.call_args.args[0])
            self.assertIsNone(led.get_kv("ship_worktree:x#7"))
            self.assertIsNone(led.get_kv("ship_worktree_branch:x#7"))
