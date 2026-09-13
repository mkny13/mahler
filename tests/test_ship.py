"""The conductor ships (DESIGN D18, mahler#16): a build that ended DONE is
pushed, opened as a PR, watched across ticks and squash-merged on green — but
only while Mahler still holds the lease (D6).

Everything runs against an in-memory Ledger with gh, git and ping mocked.
"""

import copy
import os
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from mahler import config, gh as gh_module, scheduler
from mahler.ledger import Ledger, iso

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)

ISSUE_BODY = ("## Problem\n\nmake it so\n\n## Needs a human to check\n"
              "- the new ping arrives\n- nothing else\n")


class FakeGH:
    def __init__(self):
        self.existing_pr = None
        self.view_state = "OPEN"
        self.view_body = gh_module.pr_body(5, "wired the exporter",
                                           "- the new ping arrives")
        self.rollup = [{"state": "SUCCESS"}]
        self.pushed, self.created, self.merged, self.comments = [], [], [], []
        self.fail_view = set()

    def issue_body(self, number):
        return ISSUE_BODY

    def push_branch(self, path, branch, ref):
        self.pushed.append((branch, ref))
        return "abc123def"

    def pr_for_head(self, head):
        return self.existing_pr

    def pr_create(self, head, base, title, body):
        self.created.append((head, base, title, body))
        return 88

    def pr_view(self, number):
        if number in self.fail_view:
            raise gh_module.GHError("github down")
        return {"state": self.view_state, "body": self.view_body,
                "statusCheckRollup": self.rollup, "headRefName": "mahler/5-x",
                "baseRefName": "main"}

    def pr_merge(self, number):
        self.merged.append(number)

    def comment(self, number, body):
        if not body.startswith(gh_module.AGENT_MARK):
            body = gh_module.AGENT_NOTE + "\n" + body
        self.comments.append(body)


class ShipTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["projects"]["x"] = {"path": self.tmp, "repo": "x/y"}
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.led.upsert_item("x", 5, state="verifying", priority=2,
                             title="Wired the exporter",
                             branch="mahler/snapshot/5-run7",
                             summary="wired the exporter", sorted_at=iso(NOW))
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
        self.gh = FakeGH()

    def item(self, n=5):
        return self.led.item("x", n)

    def ship(self):
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(self.ctx, "ping") as ping:
            scheduler.ship(self.ctx, [{"name": "x"}])
        return ping

    # ---------- opening the PR ----------

    def test_pushes_the_branch_and_opens_the_pr(self):
        ping = self.ship()
        self.assertEqual(self.gh.pushed, [("mahler/5-wired-the-exporter",
                                           "mahler/snapshot/5-run7")])
        head, base, title, body = self.gh.created[0]
        self.assertEqual((head, base, title),
                         ("mahler/5-wired-the-exporter", "main", "Wired the exporter"))
        self.assertTrue(body.startswith("<!-- mahler:agent -->"))
        self.assertIn("Fixes #5", body)
        self.assertIn("wired the exporter", body)
        self.assertIn("## Needs a human to check", body)
        self.assertIn("- the new ping arrives", body)
        item = self.item()
        self.assertEqual((item["pr"], item["state"]), (88, "verifying"))
        self.assertEqual(self.gh.merged, [])           # CI is watched from the next tick
        ping.assert_not_called()

    def test_pr_opening_is_idempotent(self):
        """A tick that died after `gh pr create` must not open a second PR."""
        self.gh.existing_pr = 88
        self.ship()
        self.assertEqual(self.gh.created, [])
        self.assertEqual(self.item()["pr"], 88)

    def test_nothing_to_ship_is_a_failed_attempt(self):
        self.led.upsert_item("x", 5, branch=None)
        self.ship()
        item = self.item()
        self.assertEqual((item["state"], item["attempts"]), ("ready", 1))

    # ---------- watching CI, and merging on green ----------

    def test_green_ci_squash_merges_and_closes_the_loop(self):
        self.led.upsert_item("x", 5, pr=88)
        ping = self.ship()
        self.assertEqual(self.gh.merged, [88])
        self.assertEqual(self.item()["state"], "done")
        self.assertIsNone(self.led.lease("x", 5))       # conductor lease released
        body = self.gh.comments[-1]
        self.assertTrue(body.startswith("<!-- mahler:agent -->"))
        self.assertIn("wired the exporter", body)
        self.assertIn("## Needs a human to check", body)
        self.assertIn("- the new ping arrives", body)
        ping.assert_called_once()
        self.assertEqual(ping.call_args[0][0], "Shipped — x #5")

    def test_conductor_holds_the_lease_while_verifying(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.rollup = [{"state": "PENDING"}]
        self.ship()
        self.assertEqual(self.led.lease("x", 5)["holder"], "conductor")

    def test_pending_ci_waits_across_ticks(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.rollup = [{"state": "PENDING"}]
        ping = self.ship()
        self.assertEqual(self.gh.merged, [])
        self.assertEqual(self.item()["state"], "verifying")
        ping.assert_not_called()

    def test_red_ci_waits_for_the_fix_path(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.rollup = [{"state": "FAILURE"}]
        ping = self.ship()
        self.assertEqual(self.gh.merged, [])
        self.assertEqual(self.item()["state"], "verifying")
        ping.assert_not_called()

    def test_no_ci_configured_counts_as_green(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.rollup = []
        self.ship()
        self.assertEqual(self.gh.merged, [88])

    def test_pr_resolved_outside_mahler_is_just_done(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.view_state = "MERGED"
        self.ship()
        self.assertEqual(self.gh.merged, [])
        self.assertEqual(self.item()["state"], "done")

    # ---------- the lease fence (D6) ----------

    def test_lease_lost_never_merges(self):
        """A session took the item: Mahler steps aside, PR stays open."""
        self.led.upsert_item("x", 5, pr=88)
        self.led.claim("x", 5, "interactive:mike", "interactive", 30)
        ping = self.ship()
        self.assertEqual(self.gh.merged, [])
        item = self.item()
        self.assertEqual((item["state"], item["pr"]), ("working", 88))
        ping.assert_called_once()

    def test_lease_lost_before_the_pr_is_also_a_handoff(self):
        self.led.claim("x", 5, "interactive:mike", "interactive", 30)
        ping = self.ship()
        self.assertEqual((self.gh.pushed, self.gh.created), ([], []))
        self.assertEqual(self.item()["state"], "working")
        ping.assert_called_once()

    # ---------- exception safety ----------

    def test_one_failing_item_does_not_break_the_pass(self):
        self.led.upsert_item("x", 5, pr=88)
        self.led.upsert_item("x", 6, state="verifying", title="Other thing",
                             branch="mahler/snapshot/6-run9", summary="other",
                             pr=89, sorted_at=iso(NOW))
        self.gh.fail_view = {88}
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(self.ctx, "ping"):
            scheduler.ship(self.ctx, [{"name": "x"}])
        self.assertEqual(self.item()["state"], "verifying")     # 5: retried next tick
        self.assertEqual(self.item(6)["state"], "done")         # 6: still shipped
        self.assertIn("shipping failed", " ".join(self.ctx.lines))


class HelpersTests(unittest.TestCase):
    def test_pr_body_round_trip(self):
        body = gh_module.pr_body(5, "wired the exporter", "- the new ping arrives")
        self.assertTrue(body.startswith("<!-- mahler:agent -->"))
        self.assertIn("Fixes #5", body)
        self.assertEqual(gh_module.pr_summary_of(body), "wired the exporter")
        self.assertEqual(gh_module.needs_human_of(body), "- the new ping arrives")

    def test_needs_human_of_issue_body(self):
        self.assertEqual(gh_module.needs_human_of(ISSUE_BODY),
                         "- the new ping arrives\n- nothing else")
        self.assertEqual(gh_module.needs_human_of("no section here"), "")
        self.assertEqual(gh_module.needs_human_of(""), "")

    def test_checks_state(self):
        self.assertEqual(gh_module.checks_state(None), "none")
        self.assertEqual(gh_module.checks_state([]), "none")
        self.assertEqual(gh_module.checks_state([{"state": "SUCCESS"}]), "green")
        self.assertEqual(gh_module.checks_state(
            [{"state": "SUCCESS"}, {"state": "PENDING"}]), "pending")
        self.assertEqual(gh_module.checks_state(
            [{"state": "NEUTRAL"}, {"state": "FAILURE"}]), "red")


class MigrationTests(unittest.TestCase):
    def test_a_db_from_before_the_columns_gets_them(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "mahler.db")
            con = sqlite3.connect(path)
            con.execute(
                "CREATE TABLE items (project TEXT NOT NULL, number INTEGER NOT NULL,"
                " title TEXT, state TEXT NOT NULL DEFAULT 'inbox',"
                " labels TEXT NOT NULL DEFAULT '[]', priority INTEGER NOT NULL DEFAULT 2,"
                " depends TEXT NOT NULL DEFAULT '[]', pin TEXT, branch TEXT,"
                " attempts INTEGER NOT NULL DEFAULT 0, epoch INTEGER NOT NULL DEFAULT 0,"
                " created_at TEXT, sorted_at TEXT, state_changed_at TEXT,"
                " last_comment_at TEXT, mirror TEXT, PRIMARY KEY (project, number))")
            con.commit()
            con.close()
            led = Ledger(path)
            self.assertIsNone(led.item("x", 5))
            led.upsert_item("x", 5, pr=7, summary="s")
            self.assertEqual(led.item("x", 5)["pr"], 7)


class PushBranchTests(unittest.TestCase):
    def test_pushes_once_and_is_idempotent(self):
        def git(*a, cwd=None):
            return subprocess.run(["git", *a], cwd=cwd, check=True,
                                  capture_output=True, text=True)
        with tempfile.TemporaryDirectory() as d:
            origin = os.path.join(d, "origin.git")
            repo = os.path.join(d, "repo")
            git("init", "--bare", "-b", "main", origin)
            git("init", "-b", "main", repo)
            git("remote", "add", "origin", origin, cwd=repo)
            cfg = ["-c", "user.email=t@t", "-c", "user.name=t"]
            git(*cfg, "commit", "--allow-empty", "-m", "base", cwd=repo)
            git(*cfg, "commit", "--allow-empty", "-m", "the work", cwd=repo)
            git("push", "--quiet", origin, "HEAD:refs/heads/mahler/snapshot/5-run7",
                cwd=repo)
            gh = gh_module.GH("x/y")
            sha = gh.push_branch(repo, "mahler/5-thing", "mahler/snapshot/5-run7")
            out = git("ls-remote", origin, "refs/heads/mahler/5-thing")
            self.assertTrue(out.stdout.startswith(sha))
            self.assertEqual(gh.push_branch(repo, "mahler/5-thing",
                                            "mahler/snapshot/5-run7"), sha)


if __name__ == "__main__":
    unittest.main()
