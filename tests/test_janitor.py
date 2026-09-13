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

from mahler import config, janitor, runner
from mahler.ledger import Ledger, iso
from mahler.scheduler import Ctx

OLD = "2020-01-01T00:00:00+00:00"


def sh(cwd, *args, env=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


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
        self.cfg["projects"]["t2"] = {"enabled": True, "repo": "x/y",
                                      "path": os.path.join(self.tmp.name, "gone")}
        self.cfg["projects"]["t3"] = {"enabled": True, "repo": "x/y",
                                      "path": os.path.join(self.tmp.name, "plain")}
        os.makedirs(os.path.join(self.tmp.name, "plain"))
        janitor.maybe_run(self.ctx)
        self.assertEqual([l for l in self.ctx.lines if "janitor failed" in l], [])


if __name__ == "__main__":
    unittest.main()

