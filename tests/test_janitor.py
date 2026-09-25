"""The janitor prunes only what Mahler created, and only when the ledger says
it is safe (mahler#7, DESIGN D12). Like tests/test_runner.py, this runs
against a temporary bare remote and an in-memory ledger — never prod state."""

import copy
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from mahler import config, janitor, runner
from mahler.ledger import Ledger, iso
from mahler.scheduler import Ctx

OLD = "2020-01-01T00:00:00+00:00"


def _no_git_config_overrides(env):
    # Strip any inherited GIT_CONFIG_* overrides (e.g. a runner-imposed
    # safe.bareRepository=explicit) so commands run in a *bare* remote here
    # — like remote_heads() below — aren't refused; this test's own trust
    # boundary is the temp dir it creates, not the ambient environment.
    return {k: v for k, v in env.items()
           if not (k == "GIT_CONFIG_COUNT" or k.startswith("GIT_CONFIG_KEY_")
                   or k.startswith("GIT_CONFIG_VALUE_"))}


def sh(cwd, *args, env=None):
    full = _no_git_config_overrides({**os.environ, **(env or {})})
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True,
                          text=True, env=full).stdout.strip()


def write(path, text):
    with open(path, "w") as fh:
        fh.write(text)


class Base(unittest.TestCase):
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
        self.wtroot = os.path.join(t, "worktrees")
        self.led = Ledger(":memory:")
        self.addCleanup(self.led.close)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["projects"]["t"] = {
            "enabled": True, "repo": "mkny13/t", "path": self.repo,
            "worktree_root": self.wtroot,
        }
        self.ctx = Ctx(self.cfg, self.led)

    def tearDown(self):
        self.tmp.cleanup()

    # ---------- helpers ----------

    def make_run(self, number, *, status="ended", ended_hours_ago=48,
                 branch=None, with_wt=True):
        """A run row plus, optionally, its real registered worktree."""
        led, now = self.led, self.led.now()
        run_id = led.create_run(project="t", number=number, role="build",
                                platform="claude", epoch=1, status="running",
                                started_at=iso(now))
        branch = branch or f"mahler/{number}-slug"
        wt = None
        if with_wt:
            wt = os.path.join(self.wtroot, "t", f"{number}-run{run_id}")
            os.makedirs(os.path.dirname(wt), exist_ok=True)
            sh(self.repo, "git", "worktree", "add", "--quiet", "-b", branch, wt, "main")
        led.update_run(run_id, worktree=wt, branch=branch if with_wt else None,
                       status=status,
                       ended_at=iso(now - timedelta(hours=ended_hours_ago))
                       if status == "ended" and ended_hours_ago is not None else None)
        return run_id

    def push_branch(self, name, when=OLD):
        env = dict(os.environ, GIT_COMMITTER_DATE=when, GIT_AUTHOR_DATE=when)
        sha = sh(self.repo, "git", "commit-tree", "main^{tree}", "-m", "old", env=env)
        sh(self.repo, "git", "push", "-q", "origin", f"{sha}:refs/heads/{name}")
        return sha

    def remote_heads(self):
        return sh(self.remote, "git", "for-each-ref", "refs/heads",
                  "--format=%(refname)").splitlines()

    def worktrees(self):
        out = sh(self.repo, "git", "worktree", "list", "--porcelain")
        return [l.split()[1] for l in out.splitlines() if l.startswith("worktree ")]


class WorktreeTests(Base):
    def test_ended_run_worktree_removed_with_its_branch(self):
        run_id = self.make_run(12)
        wt = os.path.join(self.wtroot, "t", f"12-run{run_id}")
        self.assertTrue(os.path.isdir(wt))
        self.assertTrue(janitor.sweep(self.ctx, self.ctx.policy("t")))
        self.assertFalse(os.path.isdir(wt))
        self.assertNotIn(wt, self.worktrees())
        self.assertEqual(sh(self.repo, "git", "branch", "--list", "mahler/12-slug"), "")

    def test_recently_ended_run_kept(self):          # grace: kept worktrees (needs-you)
        self.make_run(13, ended_hours_ago=1)
        self.assertFalse(janitor.sweep(self.ctx, self.ctx.policy("t")))
        self.assertTrue(self.worktrees())

    def test_active_run_worktree_never_touched(self):
        self.make_run(14, status="running")
        self.assertFalse(janitor.sweep(self.ctx, self.ctx.policy("t")))
        self.assertTrue(self.worktrees())

    def test_dirs_without_an_ended_run_row_are_kept(self):
        os.makedirs(os.path.join(self.wtroot, "t", "9-run99999"))
        os.makedirs(os.path.join(self.wtroot, "t", "scratch"))
        self.assertFalse(janitor.sweep(self.ctx, self.ctx.policy("t")))
        self.assertTrue(os.path.isdir(os.path.join(self.wtroot, "t", "scratch")))

    def test_stale_admin_entry_is_pruned(self):
        run_id = self.make_run(15)
        wt = os.path.join(self.wtroot, "t", f"15-run{run_id}")
        shutil.rmtree(wt)                       # dir gone by hand, admin entry stale
        janitor.sweep(self.ctx, self.ctx.policy("t"))
        self.assertNotIn(wt, self.worktrees())

    def test_worktree_whose_ledger_path_does_not_match_is_kept(self):
        run_id = self.make_run(17)
        wt = os.path.join(self.wtroot, "t", f"17-run{run_id}")
        self.led.update_run(run_id, worktree=wt + "-elsewhere")
        self.assertFalse(janitor.sweep(self.ctx, self.ctx.policy("t")))
        self.assertTrue(os.path.isdir(wt))

    def test_launch_failed_run_without_ledger_worktree_is_removed_with_only_own_branch(self):
        run_id = self.make_run(18, with_wt=False)
        wt = os.path.join(self.wtroot, "t", f"18-run{run_id}")
        os.makedirs(wt)
        own = f"mahler/18-slug-r{run_id}"
        self.led.update_run(run_id, branch=own, outcome="launch failed: boom")
        sh(self.repo, "git", "branch", own, "main")

        self.assertTrue(janitor.sweep(self.ctx, self.ctx.policy("t")))
        self.assertFalse(os.path.exists(wt))
        self.assertEqual(sh(self.repo, "git", "branch", "--list", own), "")

    def test_launch_failed_run_never_deletes_canonical_branch(self):
        run_id = self.make_run(19, with_wt=False)
        wt = os.path.join(self.wtroot, "t", f"19-run{run_id}")
        os.makedirs(wt)
        canonical = "mahler/19-slug"
        self.led.update_run(run_id, branch=canonical, outcome="launch failed: boom")
        sh(self.repo, "git", "branch", canonical, "main")

        self.assertTrue(janitor.sweep(self.ctx, self.ctx.policy("t")))
        self.assertFalse(os.path.exists(wt))
        self.assertEqual(sh(self.repo, "git", "branch", "--list", canonical), canonical)

class BranchTests(Base):
    def test_old_snapshot_branch_of_closed_item_deleted(self):
        self.push_branch("mahler/snapshot/12-run5")
        self.led.upsert_item("t", 12, state="done")
        self.assertTrue(janitor.sweep(self.ctx, self.ctx.policy("t")))
        self.assertNotIn("refs/heads/mahler/snapshot/12-run5", self.remote_heads())

    def test_old_snapshot_branch_of_open_item_kept(self):
        self.push_branch("mahler/snapshot/12-run5")
        self.led.upsert_item("t", 12, state="ready")
        self.assertFalse(janitor.sweep(self.ctx, self.ctx.policy("t")))
        self.assertIn("refs/heads/mahler/snapshot/12-run5", self.remote_heads())

    def test_recent_snapshot_branch_kept(self):
        when = iso(self.led.now() - timedelta(days=1))
        self.push_branch("mahler/snapshot/12-run5", when=when)
        self.led.upsert_item("t", 12, state="done")
        self.assertFalse(janitor.sweep(self.ctx, self.ctx.policy("t")))
        self.assertIn("refs/heads/mahler/snapshot/12-run5", self.remote_heads())

    def test_snapshot_branch_without_a_ledger_item_kept(self):
        self.push_branch("mahler/snapshot/99-run5")
        self.assertFalse(janitor.sweep(self.ctx, self.ctx.policy("t")))
        self.assertIn("refs/heads/mahler/snapshot/99-run5", self.remote_heads())

    def test_old_abandoned_branch_of_closed_item_deleted(self):
        self.push_branch("mahler/abandoned/12-run7")
        self.led.upsert_item("t", 12, state="done")
        janitor.sweep(self.ctx, self.ctx.policy("t"))
        self.assertNotIn("refs/heads/mahler/abandoned/12-run7", self.remote_heads())

    def test_non_mahler_branches_are_strictly_untouched(self):
        self.push_branch("feature/keep-forever")
        self.push_branch("mahlerx/snapshot/12-run5")
        self.push_branch("mahler/not-a-snapshot-branch")
        janitor.sweep(self.ctx, self.ctx.policy("t"))
        heads = self.remote_heads()
        for ref in ("refs/heads/feature/keep-forever",
                    "refs/heads/mahlerx/snapshot/12-run5",
                    "refs/heads/mahler/not-a-snapshot-branch"):
            self.assertIn(ref, heads)


class FixBranchTests(Base):
    def fix_branch(self, *, status="ended", tip="main", number=12):
        run_id = self.make_run(number, status=status, with_wt=False)
        name = f"mahler/{number}-slug-r{run_id}"
        sh(self.repo, "git", "push", "-q", "origin", f"{tip}:refs/heads/{name}")
        return name, run_id

    def test_ended_contained_in_main_deleted(self):
        name, _ = self.fix_branch()
        self.assertTrue(janitor.sweep(self.ctx, self.ctx.policy("t")))
        self.assertNotIn(f"refs/heads/{name}", self.remote_heads())

    def test_ended_contained_in_item_branch_deleted(self):
        tip = self.push_branch("mahler/12-current")
        self.led.upsert_item("t", 12, state="verifying", branch="mahler/12-current")
        name, _ = self.fix_branch(tip=tip)
        janitor.sweep(self.ctx, self.ctx.policy("t"))
        self.assertNotIn(f"refs/heads/{name}", self.remote_heads())
        self.assertIn("refs/heads/mahler/12-current", self.remote_heads())

    def test_pr_head_contains_ancestor_and_equal_tips(self):
        tip = self.push_branch("mahler/12-current")
        head = sh(self.repo, "git", "commit-tree", "main^{tree}", "-p", tip,
                  "-m", "PR head")
        sh(self.repo, "git", "push", "-q", "origin", f"{head}:refs/pull/325/head")
        self.led.upsert_item("t", 12, state="verifying", pr=325,
                             branch="mahler/12-current")
        names = [self.fix_branch(tip=sha)[0] for sha in (tip, head)]
        with mock.patch.object(runner, "git", wraps=runner.git) as git:
            janitor.sweep(self.ctx, self.ctx.policy("t"))
        for name in names:
            self.assertNotIn(f"refs/heads/{name}", self.remote_heads())
        self.assertEqual(sum("refs/pull/325/head" in c.args
                             for c in git.call_args_list), 1)

    def test_unique_commits_kept_and_reported(self):
        tip = self.push_branch("keep")
        name, _ = self.fix_branch(tip=tip)
        self.assertFalse(janitor.sweep(self.ctx, self.ctx.policy("t")))
        self.assertIn(f"refs/heads/{name}", self.remote_heads())
        self.assertTrue(any(name in line and "unique commits" in line
                            for line in self.ctx.lines))

    def test_unended_unknown_and_mismatched_runs_kept(self):
        names = []
        for status in ("running", "starting"):
            names.append(self.fix_branch(status=status)[0])
        name, run_id = self.fix_branch()
        self.led.update_run(run_id, project="other")
        names.append(name)
        name, run_id = self.fix_branch()
        self.led.update_run(run_id, number=99)
        names.append(name)
        unknown = "mahler/12-slug-r99999"
        sh(self.repo, "git", "push", "-q", "origin", f"main:refs/heads/{unknown}")
        names.append(unknown)
        self.assertFalse(janitor.sweep(self.ctx, self.ctx.policy("t")))
        for name in names:
            self.assertIn(f"refs/heads/{name}", self.remote_heads())

    def test_current_item_branch_kept(self):
        name, _ = self.fix_branch()
        self.led.upsert_item("t", 12, branch=name, pr=325)
        self.assertFalse(janitor.sweep(self.ctx, self.ctx.policy("t")))
        self.assertIn(f"refs/heads/{name}", self.remote_heads())

    def test_deletion_cap_and_subsequent_sweep(self):
        # Seed the temporary bare remote directly to keep 101 fixtures cheap.
        tip = sh(self.repo, "git", "rev-parse", "main")
        for _ in range(101):
            run_id = self.make_run(12, with_wt=False)
            sh(self.remote, "git", "update-ref", f"refs/heads/mahler/12-slug-r{run_id}", tip)
        janitor.sweep(self.ctx, self.ctx.policy("t"))
        self.assertEqual(len(self.remote_heads()), 2)  # main + one deferred fix
        self.assertTrue(any("cap reached (100)" in line for line in self.ctx.lines))
        janitor.sweep(self.ctx, self.ctx.policy("t"))
        self.assertEqual(self.remote_heads(), ["refs/heads/main"])

    def test_dry_run_keeps_fix_branch(self):
        name, _ = self.fix_branch()
        self.ctx.dry_run = True
        self.assertTrue(janitor.sweep(self.ctx, self.ctx.policy("t")))
        self.assertIn(f"refs/heads/{name}", self.remote_heads())
        self.assertTrue(any(f"would delete remote branch {name}" in line
                            for line in self.ctx.lines))

    def test_failed_fetch_does_not_use_stale_refs(self):
        name, _ = self.fix_branch()
        real_git = runner.git

        def fail_fetch(repo, *args, **kwargs):
            if args[0] == "fetch":
                raise runner.GitError("offline")
            return real_git(repo, *args, **kwargs)

        with mock.patch.object(runner, "git", side_effect=fail_fetch):
            self.assertFalse(janitor.sweep(self.ctx, self.ctx.policy("t")))
        self.assertIn(f"refs/heads/{name}", self.remote_heads())

    def test_failed_pr_fetch_does_not_fall_back_to_old_branch(self):
        tip = self.push_branch("mahler/12-old-head")
        name, _ = self.fix_branch(tip=tip)
        self.led.upsert_item("t", 12, branch="mahler/12-old-head", pr=325)
        self.assertFalse(janitor.sweep(self.ctx, self.ctx.policy("t")))
        self.assertIn(f"refs/heads/{name}", self.remote_heads())

    def test_remote_tip_race_is_rejected(self):
        name, _ = self.fix_branch()
        tip = sh(self.repo, "git", "rev-parse", "main")
        new = sh(self.repo, "git", "commit-tree", "main^{tree}", "-p", tip,
                 "-m", "new work")
        sh(self.repo, "git", "push", "-q", "origin", f"{new}:refs/heads/{name}")
        with self.assertRaises(runner.GitError):
            janitor._delete_remote_branch(self.repo, name, tip=tip, target="main")
        self.assertIn(f"refs/heads/{name}", self.remote_heads())

    def test_delete_guard_rejects_unproven_and_unrelated_refs(self):
        name, _ = self.fix_branch()
        for branch in (name, "mahler/revert-12-r1", "feature/12-slug-r1"):
            with self.subTest(branch=branch), self.assertRaises(RuntimeError):
                janitor._delete_remote_branch(self.repo, branch)
        tip = self.push_branch("unique")
        with self.assertRaises(RuntimeError):
            janitor._delete_remote_branch(self.repo, name, tip=tip, target="main")


class AccountEnvTests(Base):
    """A work project's own repo is fetched/pruned with its own git-hosting
    identity (D25/D26), not this machine's default — the sweep's counterpart
    to test_accounts.py's GH-client coverage (mahler#296)."""

    def setUp(self):
        super().setUp()
        self.cfg["accounts"] = {"work": {"env": {"GH_CONFIG_DIR": "~/.config/gh-work"}}}
        self.cfg["projects"]["t"]["account"] = "work"
        self.ctx = Ctx(self.cfg, self.led)

    def test_sweep_fetches_and_deletes_with_the_project_s_account_env(self):
        self.push_branch("mahler/snapshot/12-run5")
        self.led.upsert_item("t", 12, state="done")
        run_id = self.make_run(13, with_wt=False)
        name = f"mahler/13-slug-r{run_id}"
        sh(self.repo, "git", "push", "-q", "origin", f"main:refs/heads/{name}")
        sh(self.repo, "git", "push", "-q", "origin", "main:refs/pull/325/head")
        self.led.upsert_item("t", 13, pr=325, branch="mahler/13-current")
        with mock.patch.object(runner, "git", wraps=runner.git) as git:
            self.assertTrue(janitor.sweep(self.ctx, self.ctx.policy("t")))
        network_calls = [c for c in git.call_args_list if c.args[1] in ("fetch", "push")]
        self.assertTrue(network_calls)
        for call in network_calls:
            self.assertEqual(call.kwargs.get("env", {}).get("GH_CONFIG_DIR"),
                             os.path.expanduser("~/.config/gh-work"))


class BehaviorTests(Base):
    def test_dry_run_reports_without_deleting(self):
        run_id = self.make_run(12)
        self.push_branch("mahler/snapshot/13-run6")
        self.led.upsert_item("t", 13, state="done")
        self.ctx.dry_run = True
        janitor.maybe_run(self.ctx)
        self.assertTrue(any("would remove worktree" in l for l in self.ctx.lines))
        self.assertTrue(any("would delete remote branch mahler/snapshot/13-run6"
                            in l for l in self.ctx.lines))
        self.assertTrue(os.path.isdir(os.path.join(self.wtroot, "t", f"12-run{run_id}")))
        self.assertIn("refs/heads/mahler/snapshot/13-run6", self.remote_heads())
        self.assertIsNone(self.led.get_kv(janitor.KV_KEY))

    def test_runs_once_a_day(self):
        self.make_run(12)
        today = datetime.now().astimezone().date().isoformat()
        janitor.maybe_run(self.ctx)                 # sweeps, marks the day
        self.assertEqual(self.led.get_kv(janitor.KV_KEY), today)
        self.assertFalse(os.path.exists(os.path.join(self.wtroot, "t", "12-run1")))
        self.make_run(14)                           # another stale one, same day
        self.ctx.lines.clear()
        janitor.maybe_run(self.ctx)
        self.assertEqual(self.ctx.lines, [])        # gated: nothing swept again today
        self.assertTrue(os.path.isdir(os.path.join(self.wtroot, "t", "14-run2")))

    def test_never_raises_over_an_unreachable_project(self):
        # "t2" is skipped outright (unreachable path); "t3" is reachable but
        # its sweep blows up mid-tick — that one must be caught and logged
        # without stopping "t"'s own sweep or the daily gate from being set.
        run_id = self.make_run(12)
        wt = os.path.join(self.wtroot, "t", f"12-run{run_id}")
        self.cfg["projects"]["t2"] = {"enabled": True, "repo": "x/y",
                                      "path": os.path.join(self.tmp.name, "gone")}
        self.cfg["projects"]["t3"] = {"enabled": True, "repo": "x/y",
                                      "path": os.path.join(self.tmp.name, "plain")}
        os.makedirs(os.path.join(self.tmp.name, "plain"))
        real_sweep = janitor.sweep

        def flaky(ctx, pol):
            if pol["name"] == "t3":
                raise RuntimeError("boom")
            return real_sweep(ctx, pol)

        with mock.patch.object(janitor, "sweep", side_effect=flaky):
            janitor.maybe_run(self.ctx)      # a broken project must not stop the tick

        self.assertTrue(any("t3" in l and "boom" in l for l in self.ctx.lines))
        self.assertFalse(os.path.isdir(wt))                      # t's own sweep still ran
        today = datetime.now().astimezone().date().isoformat()
        self.assertEqual(self.led.get_kv(janitor.KV_KEY), today)  # day still marked swept


if __name__ == "__main__":
    unittest.main()
