"""The conductor ships (DESIGN D18, mahler#16): a build that ended DONE is
pushed, opened as a PR, watched across ticks and squash-merged on green — but
only while Mahler still holds the lease (D6).

Everything runs against an in-memory Ledger with gh, git and ping mocked.
"""

import copy
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler import config, finalize, gh as gh_module, platforms, runner, scheduler, ship
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
        self.mergeable = "MERGEABLE"
        self.head_sha = "abc123"
        self.pushed, self.created, self.merged, self.comments = [], [], [], []
        self.fail_view = set()
        self.queue = False    # simulate a merge queue: pr_merge only enqueues

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
                "statusCheckRollup": self.rollup, "mergeable": self.mergeable,
                "headRefName": "mahler/5-x", "headRefOid": self.head_sha,
                "baseRefName": "main",
                "mergeCommit": {"oid": "4c1f0abfeed5"}}

    def issue_state(self, number):
        return "OPEN"

    def base_in_head(self, path, base, head):
        return True

    def pr_merge(self, number, head):
        assert head == self.head_sha
        self.merged.append(number)
        if not self.queue:                # a real merge (no queue) is synchronous
            self.view_state = "MERGED"

    def comment(self, number, body):
        if not body.startswith(gh_module.AGENT_MARK):
            body = gh_module.AGENT_NOTE + "\n" + body
        self.comments.append(body)

    prs = ()

    def open_prs(self):
        return list(self.prs)


class ShipTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["projects"]["x"] = {"path": self.tmp, "repo": "x/y"}
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)
        later = iso(NOW + timedelta(hours=2))
        for name in ("agy-claude", "agy-gemini", "claude"):   # live usage samples,
            self.led.record_usage(name, "5h", 10, later)   # so routing has something
            self.led.record_usage(name, "weekly", 10, later)
        self.led.upsert_item("x", 5, state="verifying", priority=2,
                             title="Wired the exporter",
                             branch="mahler/5-wired-the-exporter",
                             summary="wired the exporter", sorted_at=iso(NOW))
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
        self.gh = FakeGH()
        avail = mock.patch.object(platforms, "available", return_value=True)
        avail.start()
        self.addCleanup(avail.stop)

    def item(self, n=5):
        return self.led.item("x", n)

    def ship(self):
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(self.ctx, "ping") as ping:
            ship.ship(self.ctx, [{"name": "x"}])
        return ping

    # ---------- opening the PR ----------

    def test_pushes_the_branch_and_opens_the_pr(self):
        self.led.upsert_item("x", 5, branch="mahler/snapshot/5-run7")
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
        self.assertEqual(item["branch"], "mahler/5-wired-the-exporter")
        self.assertEqual(self.gh.merged, [])           # CI is watched from the next tick
        ping.assert_not_called()

    def test_pr_opening_is_idempotent(self):
        """A tick that died after `gh pr create` must not open a second PR."""
        self.led.upsert_item("x", 5, branch="mahler/snapshot/5-run7")
        self.gh.existing_pr = 88
        self.ship()
        self.assertEqual(self.gh.created, [])
        self.assertEqual(self.item()["pr"], 88)
        self.assertEqual(self.item()["branch"], "mahler/5-wired-the-exporter")

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

    def test_shipped_comment_failure_still_closes_the_loop(self):
        self.led.upsert_item("x", 5, pr=88)
        with mock.patch.object(self.gh, "comment",
                               side_effect=gh_module.GHError("rate limited")):
            ping = self.ship()   # must not raise
        self.assertEqual(self.gh.merged, [88])
        self.assertEqual(self.item()["state"], "done")
        self.assertIsNone(self.led.lease("x", 5))
        ping.assert_called_once()
        self.assertIn("couldn't post the shipped comment", " ".join(self.ctx.lines))

    def test_shipped_needs_human_lands_in_the_uat_queue(self):
        self.led.upsert_item("x", 5, pr=88)
        self.ship()
        row = self.led.uat("x", 5)
        self.assertEqual((row["pr"], row["sha"], row["title"]), (88, "4c1f0abfeed5",
                                                                "Wired the exporter"))
        self.assertEqual(row["needs"], "- the new ping arrives")
        self.assertIsNone(row["verdict"])
        self.assertEqual(row["shipped_at"], iso(NOW))

    def test_shipped_issue_lands_in_unreleased_draft(self):
        self.led.upsert_item("x", 5, pr=88, labels=json.dumps(["type:feature"]))
        self.ship()
        unrel = self.led.unreleased_items("x")
        self.assertEqual(len(unrel), 1)
        row = unrel[0]
        self.assertEqual(row["number"], 5)
        self.assertEqual(row["pr"], 88)
        self.assertEqual(row["title"], "Wired the exporter")
        self.assertEqual(row["summary"], "wired the exporter")
        self.assertEqual(row["merge_sha"], "4c1f0abfeed5")
        self.assertEqual(json.loads(row["labels"]), ["type:feature"])
        self.assertEqual(row["shipped_at"], iso(NOW))

    def assert_nothing_shipped(self, ping):
        self.assertEqual(self.led.unreleased_items("x"), [])
        self.assertIsNone(self.led.uat("x", 5))
        self.assertEqual(self.led.q("SELECT * FROM events WHERE kind='shipped'"), [])
        self.assertEqual(self.gh.comments, [])
        self.assertFalse(any(c.args[0].startswith("Shipped") for c in ping.call_args_list))

    def test_closed_unmerged_pr_returns_to_ready(self):
        self.gh.view_state = "CLOSED"
        self.led.upsert_item("x", 5, pr=88)
        branch = self.item()["branch"]
        ping = self.ship()
        self.assertEqual(self.item()["state"], "ready")
        self.assertEqual(self.item()["attempts"], 1)
        self.assertEqual(self.item()["branch"], branch)
        self.assertIsNone(self.item()["pr"])
        self.assertIsNone(self.led.lease("x", 5))
        self.assertIn("PR #88 was closed without merging", self.last_event())
        self.assert_nothing_shipped(ping)
        self.ship()  # Not counted again once it leaves verifying.
        self.assertEqual(self.item()["attempts"], 1)

    def test_closed_deleted_pr_branch_rebuilds_from_latest_snapshot(self):
        def git(*args):
            return subprocess.run(["git", "-C", self.tmp, *args], check=True,
                                  capture_output=True, text=True).stdout.strip()

        origin = os.path.join(self.tmp, "origin.git")
        git("init", "--bare", "-b", "main", origin)
        git("init", "-b", "main")
        git("remote", "add", "origin", origin)
        author = ["-c", "user.name=test", "-c", "user.email=test@example.com"]
        git(*author, "commit", "--allow-empty", "-m", "base")
        git("push", "origin", "main")
        for run in (7, 8):
            git(*author, "commit", "--allow-empty", "-m", f"completed work {run}")
            snapshot = f"mahler/snapshot/5-run{run}"
            git("push", "origin", f"HEAD:refs/heads/{snapshot}")
            self.led.upsert_item("x", 5, branch=snapshot)
            with mock.patch.object(self.gh, "push_branch",
                                   side_effect=gh_module.GH("x/y").push_branch):
                self.ship()
            self.gh.existing_pr = 88
        completed = git("rev-parse", "HEAD")
        head = self.item()["branch"]
        git("push", "origin", "--delete", head)
        self.gh.existing_pr = None
        self.gh.view_state = "CLOSED"
        ping = self.ship()
        self.assertEqual(self.item()["state"], "ready")
        self.assertEqual(self.item()["branch"], snapshot)
        self.assertIsNone(self.item()["pr"])
        git("fetch", "origin", "--prune")
        self.assertFalse(runner.remote_has(self.tmp, head))
        start = runner.start_ref(self.tmp, "main", self.item()["branch"], head)
        self.assertEqual(git("rev-parse", start), completed)
        self.assert_nothing_shipped(ping)

    def test_merged_pr_with_saved_snapshot_records_shipping(self):
        self.led.upsert_item("x", 5, pr=88, branch="mahler/snapshot/5-run7")
        self.gh.view_state = "MERGED"
        self.ship()
        self.assertEqual(self.item()["state"], "done")
        self.assertEqual(self.gh.pushed, [])
        self.assertEqual(self.gh.created, [])
        self.assertIsNotNone(self.led.uat("x", 5))
        self.assertEqual(len(self.led.unreleased_items("x")), 1)

    def test_closed_unmerged_pr_fails_at_attempt_limit(self):
        self.gh.view_state = "CLOSED"
        limit = self.ctx.policy("x")["max_attempts"]
        self.led.upsert_item("x", 5, pr=88, attempts=limit - 1)
        ping = self.ship()
        self.assertEqual(self.item()["state"], "failed")
        self.assertEqual(self.item()["attempts"], limit)
        self.assertIsNone(self.led.lease("x", 5))
        self.assertIn("PR #88 was closed without merging", self.last_event())
        ping.assert_called_once()
        self.assertIn("Stuck", ping.call_args.args[0])
        self.assert_nothing_shipped(ping)

    def test_closed_pr_adopts_replacement_on_actual_pr_head(self):
        self.gh.view_state = "CLOSED"
        self.led.upsert_item("x", 5, pr=88)
        with mock.patch.object(self.gh, "pr_for_head", return_value=99) as lookup:
            ping = self.ship()
        self.assertEqual(lookup.call_args_list[-1], mock.call("mahler/5-x"))
        self.assertEqual((self.item()["pr"], self.item()["state"]), (99, "verifying"))
        self.assertEqual(self.item()["attempts"], 0)
        self.assertIsNone(self.led.lease("x", 5))
        self.assert_nothing_shipped(ping)
        self.gh.view_state = "OPEN"
        self.gh.rollup = [{"state": "PENDING"}]
        with mock.patch.object(self.gh, "pr_view", wraps=self.gh.pr_view) as view:
            self.ship()
        view.assert_called_once_with(99)

    def test_rebuild_adopts_fresh_pr_before_watching_stale_pr(self):
        self.led.upsert_item("x", 5, pr=88, branch="mahler/snapshot/5-run7")
        self.gh.existing_pr = 99
        with mock.patch.object(self.gh, "pr_view", wraps=self.gh.pr_view) as view, mock.patch.object(
                self.gh, "pr_for_head", wraps=self.gh.pr_for_head) as lookup:
            ping = self.ship()
        view.assert_called_once_with(88)
        lookup.assert_called_once_with("mahler/5-x")
        self.assertEqual((self.item()["pr"], self.item()["state"]), (99, "verifying"))
        self.assertEqual(self.item()["branch"], "mahler/5-x")
        self.assertEqual(self.gh.pushed, [("mahler/5-x", "mahler/snapshot/5-run7")])
        self.assertEqual(self.led.lease("x", 5)["holder"], "conductor")
        self.assert_nothing_shipped(ping)

    def test_rebuild_with_snapshot_and_closed_pr_opens_replacement_pr(self):
        self.led.upsert_item("x", 5, pr=88, branch="mahler/snapshot/5-run7")
        self.gh.existing_pr = None
        self.gh.view_state = "CLOSED"
        ping = self.ship()
        self.assertEqual(self.gh.pushed, [("mahler/5-x", "mahler/snapshot/5-run7")])
        self.assertEqual(len(self.gh.created), 1)
        self.assertEqual(self.gh.created[0][0], "mahler/5-x")
        self.assertEqual((self.item()["pr"], self.item()["state"]), (88, "verifying"))
        self.assertEqual(self.item()["branch"], "mahler/5-x")
        self.assertEqual(self.item()["attempts"], 0)
        self.assertEqual(self.led.lease("x", 5)["holder"], "conductor")
        self.assert_nothing_shipped(ping)

    def test_closed_immediately_after_merge_request_is_not_shipped(self):
        self.led.upsert_item("x", 5, pr=88)
        with mock.patch.object(self.gh, "pr_merge", side_effect=lambda *a: setattr(
                self.gh, "view_state", "CLOSED")):
            ping = self.ship()
        self.assertEqual(self.item()["state"], "ready")
        self.assertIsNone(self.led.lease("x", 5))
        self.assert_nothing_shipped(ping)

    def test_closed_pr_dry_run_reports_retry_without_writes(self):
        self.ctx.dry_run = True
        self.gh.view_state = "CLOSED"
        for attempts, expected in ((0, "ready"), (self.ctx.policy("x")["max_attempts"] - 1, "failed")):
            with self.subTest(expected=expected):
                self.led.upsert_item("x", 5, pr=88, attempts=attempts)
                before = dict(self.item())
                ping = self.ship()
                self.assertIn(f"closed without merging — would go to {expected}", self.ctx.lines[-1])
                self.assertEqual(dict(self.item()), before)
                self.assertIsNone(self.led.lease("x", 5))
                self.assert_nothing_shipped(ping)
                ping.assert_not_called()

    def test_replacement_lookup_failure_does_not_consume_attempt(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.view_state = "CLOSED"
        with mock.patch.object(self.gh, "pr_for_head", side_effect=gh_module.GHError("down")):
            ping = self.ship()
        self.assertEqual((self.item()["state"], self.item()["attempts"]), ("verifying", 0))
        self.assert_nothing_shipped(ping)

    def test_shipped_issue_snapshot_is_idempotent_on_retry(self):
        self.led.upsert_item("x", 5, pr=88, labels=json.dumps(["type:bug"]))
        self.ship()
        self.assertEqual(len(self.led.unreleased_items("x")), 1)

        view = self.gh.pr_view(88)
        ship._shipped(self.ctx, "x", 5, 88, self.led.item("x", 5), view, merged=True)
        self.assertEqual(len(self.led.unreleased_items("x")), 1)

    def test_shipped_without_a_needs_human_list_skips_the_uat_queue(self):
        self.gh.view_body = "plain ship, nothing to check."
        self.led.upsert_item("x", 5, pr=88)
        self.ship()
        self.assertIsNone(self.led.uat("x", 5))

    def test_a_failing_uat_write_does_not_stop_the_ship(self):
        self.led.upsert_item("x", 5, pr=88)
        with mock.patch.object(self.led, "add_uat",
                               side_effect=sqlite3.OperationalError("db locked")):
            self.ship()   # must not raise
        self.assertEqual(self.item()["state"], "done")
        self.assertIn("couldn't record the UAT item", " ".join(self.ctx.lines))

    def test_shipped_event_tracks_enabled_maintenance_passes(self):
        self.cfg["projects"]["x"]["maintenance"] = {
            "enabled": True, "passes": ["security", "tests"],
        }
        self.led.upsert_item("x", 5, pr=88)
        self.ship()
        self.assertEqual(
            self.led.maintenance_checkpoint("x", "security")["merged_since"], 1)
        self.assertEqual(
            self.led.maintenance_checkpoint("x", "tests")["merged_since"], 1)
        self.assertEqual(
            self.led.maintenance_checkpoint("x", "health")["merged_since"], 0)

    def test_disabled_maintenance_does_not_track_shipments(self):
        self.cfg["projects"]["x"]["maintenance"] = {"enabled": False}
        self.led.upsert_item("x", 5, pr=88)
        self.ship()
        self.assertEqual(
            self.led.maintenance_checkpoint("x", "security")["merged_since"], 0)

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

    def patch_start(self):
        """A start() stand-in: claims the fix run's lease as the real one would,
        but launches nothing."""
        led = self.led

        def fake_start(ctx, project, it, role, platform, handoff_from=None, size=None,
                       fix_reason="ci"):
            led.claim(project, it["number"], "run:14", "auto", 30,
                      platform=platform, run_id=14, handoff_from=handoff_from)
            led.set_state(project, it["number"], "working")
            ctx.gh(project).comment(it["number"],
                                    f"🔁 **{platform}** started a fix run (run 14) on "
                                    f"branch `{it['branch']}` — CI was red.")
            return True

        patcher = mock.patch.object(ship, "start", side_effect=fake_start)
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def test_red_ci_pings_once_and_starts_a_fix_run(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.rollup = [{"state": "FAILURE"}]
        self.gh.head_sha = "red1"
        start = self.patch_start()
        ping = self.ship()
        start.assert_called_once()
        self.assertEqual(start.call_args.kwargs["fix_reason"], "ci")
        (it, role) = start.call_args[0][2:4]
        self.assertEqual((it["branch"], role), ("mahler/5-x", "fix"))
        self.assertEqual(self.item()["state"], "working")        # the fix run's lease
        self.assertEqual(self.item()["attempts"], 1)             # a red cycle counts
        self.assertEqual(self.led.lease("x", 5)["holder"], "run:14")
        body = self.gh.comments[-1]
        self.assertIn("fix run", body)
        self.assertTrue(body.startswith("<!-- mahler:agent -->"))
        ping.assert_called_once()
        self.assertEqual(ping.call_args[0][0], "CI red — x #5")

    def test_red_ci_ping_is_not_repeated_per_sha(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.rollup = [{"state": "FAILURE"}]
        self.gh.head_sha = "red1"
        self.ship()
        self.ship()
        self.gh.head_sha = "red2"          # the fix pushed, CI failed again
        p3 = self.ship()
        reds = [c for c in p3.call_args_list if c[0][0] == "CI red — x #5"]
        self.assertEqual(len(reds), 1)     # once per failing head SHA

    def test_red_ci_with_no_free_slot_waits_for_the_next_tick(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.rollup = [{"state": "FAILURE"}]
        with mock.patch.object(platforms, "available", return_value=False):
            self.ship()
        self.assertEqual(self.item()["state"], "verifying")     # unchanged, retried next tick
        self.assertIsNone(self.led.lease("x", 5))

    def test_failed_review_releases_capacity_for_another_green_pr(self):
        self.led.upsert_item("x", 5, pr=88, labels='["size:m"]')
        self.led.set_kv("review:x#5", json.dumps({"sha": "abc123", "verdict": "fail"}))
        self.led.upsert_item("x", 6, pr=89, state="verifying", title="small fix",
                             labels='["size:s"]', branch="mahler/6-fix")
        # Enforce canonical max_parallel=1 with the real ledger claim path.
        claim = self.led.claim
        def bounded_claim(*args, **kwargs):
            return claim(*args, **kwargs, max_parallel=1)
        with mock.patch.object(self.led, "claim", side_effect=bounded_claim), \
             mock.patch("mahler.router.pick_for_project", return_value=(None, ["no quota"])):
            self.ship()
        self.assertEqual(self.item()["state"], "verifying")
        self.assertIsNone(self.led.lease("x", 5))
        self.assertEqual(self.gh.merged, [89])

    def test_review_quota_wait_does_not_block_another_review_or_merge(self):
        self.led.upsert_item("x", 5, pr=88, labels='["size:m"]')
        self.led.upsert_item("x", 6, pr=89, state="verifying", title="another change",
                             labels='["size:m"]', branch="mahler/6-fix")
        # Upgrade an idle lease left by the previous daemon release.
        old, _ = self.led.claim("x", 5, "conductor", "auto", 10)
        claim = self.led.claim
        def bounded_claim(*args, **kwargs):
            return claim(*args, **kwargs, max_parallel=1)
        started = []
        def review_start(ctx, project, item, role, platform, **kwargs):
            lease, _ = ctx.led.claim(project, item["number"], "run:99", "auto", 10,
                                    handoff_from=kwargs["handoff_from"])
            self.assertIsNotNone(lease)
            started.append(item["number"])
            ctx.led.claim(project, item["number"], "conductor", "auto", 10,
                          capacity=False, handoff_from=("run:99", lease["epoch"]))
            ctx.led.set_kv("review:x#6", json.dumps({"sha": "abc123", "verdict": "pass"}))
            return True
        with mock.patch.object(self.led, "claim", side_effect=bounded_claim), \
             mock.patch("mahler.router.pick_for_project",
                        side_effect=[(None, ["no quota"]), ("claude", [])]), \
             mock.patch.object(ship, "start", side_effect=review_start):
            self.ship()
        self.led.set_kv("review:x#6", json.dumps({"sha": "abc123", "verdict": "pass"}))
        self.assertEqual(started, [6])
        lease = self.led.lease("x", 5)
        self.assertEqual(lease["capacity"], 0)
        self.assertEqual(lease["epoch"], old["epoch"])
        with mock.patch("mahler.router.pick_for_project", return_value=(None, ["no quota"])):
            self.ship()
        self.assertEqual(self.gh.merged, [89])
        self.assertEqual(self.item()["state"], "verifying")

    def test_only_one_merge_request_per_project_per_tick(self):
        self.led.upsert_item("x", 5, pr=88)
        self.led.upsert_item("x", 6, pr=89, state="verifying", title="small fix",
                             branch="mahler/6-fix")
        self.gh.queue = True
        self.ship()
        self.assertEqual(self.gh.merged, [88])
        self.ship()
        self.assertEqual(self.gh.merged, [88, 89])

    def test_second_merge_rechecks_base_after_first_tick(self):
        self.led.upsert_item("x", 5, pr=88)
        self.led.upsert_item("x", 6, pr=89, state="verifying", title="small fix",
                             branch="mahler/6-fix")
        self.gh.queue = True
        self.ship()
        with mock.patch.object(self.gh, "base_in_head", return_value=False):
            self.ship()
        self.assertEqual(self.gh.merged, [88])
        self.assertEqual(self.led.item("x", 6)["state"], "ready")

    def test_failed_review_launch_restores_watch_lease_without_capacity(self):
        from mahler import tick
        self.led.upsert_item("x", 5, pr=88)
        lease, _ = self.led.claim("x", 5, "conductor", "auto", 10, capacity=False)
        with mock.patch.object(runner, "prepare", side_effect=RuntimeError("setup failed")):
            self.assertFalse(tick.start(self.ctx, "x", self.item(), "review", "agy-claude",
                                        handoff_from=("conductor", lease["epoch"])))
        restored = self.led.lease("x", 5)
        self.assertEqual(restored["holder"], "conductor")
        self.assertEqual(restored["capacity"], 0)
        self.assertFalse(self.led.lease_check("x", 5, lease["epoch"]))

    def test_run_capacity_refuses_review_and_fix_until_holder_finishes(self):
        from mahler import tick
        self.cfg["projects"]["x"]["max_parallel"] = 1
        self.led.upsert_item("x", 6, state="verifying", branch="mahler/6-fix")
        run = self.led.create_run(project="x", number=5, role="review",
                                  platform="claude", epoch=1)
        self.led.claim("x", 5, f"run:{run}", "auto", 10,
                       platform="claude", run_id=run)
        claim = self.led.claim
        def bounded_claim(*args, **kwargs):
            return claim(*args, **kwargs, max_parallel=1)
        with mock.patch.object(self.led, "claim", side_effect=bounded_claim), \
             mock.patch.object(self.ctx, "say") as say, \
             mock.patch.object(runner, "prepare", return_value={}), \
             mock.patch("mahler.prompt.build", return_value="prompt"), \
             mock.patch.object(runner, "launch", return_value={}):
            for role in ("build", "review", "fix"):
                self.assertFalse(tick.start(self.ctx, "x", self.led.item("x", 6),
                                            role, "agy-claude"))
            self.assertIn(f"x#5 — claude review run {run}", str(say.call_args_list))
            self.led.update_run(run, status="ended")
            self.led.release("x", 5)
            self.assertTrue(tick.start(self.ctx, "x", self.led.item("x", 6),
                                       "review", "agy-claude"))

    def test_red_ci_does_not_reescalate_every_tick_on_the_same_sha(self):
        """mahler#232 — a tick that can't start a fix run (no free slot, no
        platform) must not re-count the same red CI cycle: esc_fails/esc_tier
        and attempts stay put across repeated ticks on one head sha, and only
        move again once a genuinely new cycle (a new sha) is observed."""
        self.led.upsert_item("x", 5, pr=88)
        self.gh.rollup = [{"state": "FAILURE"}]
        self.gh.head_sha = "red1"
        with mock.patch.object(platforms, "available", return_value=False):
            for _ in range(3):
                self.ship()
        item = self.item()
        self.assertEqual((item["state"], item["esc_fails"], item["esc_tier"], item["attempts"]),
                         ("verifying", 1, 0, 0))

        # a second failing sha (as if a fix run had pushed and failed again)
        # is one more cycle, not two more.
        self.gh.head_sha = "red2"
        with mock.patch.object(platforms, "available", return_value=False):
            for _ in range(3):
                self.ship()
        item = self.item()
        self.assertEqual((item["esc_fails"], item["esc_tier"]), (0, 2))

    def test_red_ci_gives_up_after_max_attempts(self):
        self.led.upsert_item("x", 5, pr=88, attempts=2)
        self.gh.rollup = [{"state": "FAILURE"}]
        start = self.patch_start()
        ping = self.ship()
        start.assert_not_called()
        self.assertEqual(self.item()["state"], "failed")
        self.assertIsNone(self.led.lease("x", 5))               # the slot is given back
        ping.assert_called_once()
        self.assertIn("Stuck", ping.call_args[0][0])

    # ---------- independent review before merge (DESIGN D11) ----------

    def patch_review_start(self, role_seen):
        """A start() stand-in for review/review-triggered-fix runs: records
        every call's (role, platform, context) and claims the run lease like
        the real one would, but launches nothing."""
        led = self.led

        def fake_start(ctx, project, it, role, platform, handoff_from=None,
                       size=None, context=None, fix_reason="ci"):
            role_seen.append((role, platform, context))
            led.claim(project, it["number"], "run:14", "auto", 30,
                      platform=platform, run_id=14, handoff_from=handoff_from)
            return True

        patcher = mock.patch.object(ship, "start", side_effect=fake_start)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_low_risk_item_merges_without_a_review(self):
        """No size:m/l label and nothing risk-flagged in the title: unchanged
        from before D11 existed."""
        self.led.upsert_item("x", 5, pr=88, labels="[]")
        self.ship()
        self.assertEqual(self.gh.merged, [88])

    def test_size_m_item_on_green_ci_starts_a_review_instead_of_merging(self):
        self.led.upsert_item("x", 5, pr=88, labels=json.dumps(["size:m"]))
        self.gh.head_sha = "greensha1"
        calls = []
        self.patch_review_start(calls)
        self.ship()
        self.assertEqual(self.gh.merged, [])            # not merged yet
        self.assertEqual(len(calls), 1)
        role, platform, context = calls[0]
        self.assertEqual(role, "review")
        info = json.loads(self.led.get_kv("review:x#5"))
        self.assertEqual(info, {"sha": "greensha1", "verdict": "pending"})

    def test_review_excludes_the_builder_platform(self):
        """DESIGN D11: the reviewer must be a different platform than
        whichever one produced the PR — even when that platform would
        otherwise be first in the review routing order."""
        self.led.upsert_item("x", 5, pr=88, labels=json.dumps(["size:m"]))
        rid = self.led.create_run(project="x", number=5, role="build",
                                  platform="agy-claude", epoch=1, status="running")
        self.led.update_run(rid, status="ended")
        calls = []
        self.patch_review_start(calls)
        self.ship()
        self.assertEqual(len(calls), 1)
        _, platform, _ = calls[0]
        self.assertNotEqual(platform, "agy-claude")

    def test_data_touching_item_pins_claude_for_review(self):
        self.led.upsert_item("x", 5, pr=88, title="database migration for users",
                             labels="[]")
        rid = self.led.create_run(project="x", number=5, role="build",
                                  platform="agy-claude", epoch=1, status="running")
        self.led.update_run(rid, status="ended")
        calls = []
        self.patch_review_start(calls)
        self.ship()
        self.assertEqual(len(calls), 1)
        _, platform, _ = calls[0]
        self.assertEqual(platform, "claude")

    def test_claude_never_pinned_as_its_own_reviewer(self):
        """Would otherwise deadlock: pin=claude plus exclude={claude} leaves
        no candidate at all."""
        self.led.upsert_item("x", 5, pr=88, title="database migration for users",
                             labels="[]")
        rid = self.led.create_run(project="x", number=5, role="build",
                                  platform="claude", epoch=1, status="running")
        self.led.update_run(rid, status="ended")
        calls = []
        self.patch_review_start(calls)
        self.ship()
        self.assertEqual(len(calls), 1)
        _, platform, _ = calls[0]
        self.assertNotEqual(platform, "claude")

    def test_review_in_flight_just_waits(self):
        self.led.upsert_item("x", 5, pr=88, labels=json.dumps(["size:m"]))
        self.led.create_run(project="x", number=5, role="review",
                            platform="agy-gemini", epoch=1, status="running")
        calls = []
        self.patch_review_start(calls)
        self.ship()
        self.assertEqual(calls, [])
        self.assertEqual(self.gh.merged, [])

    def test_review_pass_verdict_merges(self):
        self.led.upsert_item("x", 5, pr=88, labels=json.dumps(["size:m"]))
        self.led.set_kv("review:x#5", json.dumps({"sha": self.gh.head_sha, "verdict": "pass"}))
        self.ship()
        self.assertEqual(self.gh.merged, [88])

    def test_review_fail_verdict_starts_a_fix_round_with_findings(self):
        self.led.upsert_item("x", 5, pr=88, labels=json.dumps(["size:m"]))
        self.led.set_kv("review:x#5", json.dumps({
            "sha": self.gh.head_sha, "verdict": "fail",
            "findings": "auth.py: missing null check on session token"}))
        calls = []
        self.patch_review_start(calls)
        self.ship()
        self.assertEqual(self.gh.merged, [])
        self.assertEqual(len(calls), 1)
        role, platform, context = calls[0]
        self.assertEqual(role, "fix")
        self.assertIn("auth.py: missing null check on session token", context)
        self.assertEqual(self.item()["attempts"], 1)

    def test_fix_announcements_identify_the_trigger(self):
        """Exercise real start() announcements through both shipping paths."""
        for trigger in ("ci", "review"):
            with self.subTest(trigger=trigger):
                self.led.upsert_item("x", 5, state="verifying", pr=88,
                                     labels='["size:m"]', attempts=0)
                self.gh.rollup = [{"state": "FAILURE" if trigger == "ci" else "SUCCESS"}]
                self.led.set_kv("review:x#5", json.dumps({
                    "sha": self.gh.head_sha, "verdict": "fail",
                    "findings": "missing null check"}))
                with mock.patch("mahler.tick.runner.prepare", return_value={}), \
                        mock.patch("mahler.tick.prompt.build", return_value="fix prompt"), \
                        mock.patch("mahler.tick.runner.launch", return_value={"branch": "mahler/5-x"}), \
                        mock.patch("mahler.tick.launch_health.succeeded"):
                    self.ship()
                self.assertEqual(self.item()["state"], "working")
                event = self.led.q1("SELECT detail FROM events WHERE kind='state' ORDER BY id DESC")
                for text in (self.gh.comments[-1], event["detail"]):
                    if trigger == "review":
                        self.assertIn("review found blocking issues", text)
                        self.assertIn("review comment", text)
                        self.assertIn("https://github.com/x/y/pull/88", text)
                        self.assertNotIn("CI was red", text)
                    else:
                        self.assertIn("CI was red", text)
                        self.assertNotIn("review found blocking issues", text)
                for run in self.led.active_runs():
                    self.led.update_run(run["id"], status="ended")
                self.led.release("x", 5)

    def test_stale_review_verdict_on_a_new_sha_re_reviews(self):
        """A fix round (or any new push) changes the head sha: a verdict
        recorded for the old sha must not merge or re-fix on the new one."""
        self.led.upsert_item("x", 5, pr=88, labels=json.dumps(["size:m"]))
        self.led.set_kv("review:x#5", json.dumps({"sha": "oldsha", "verdict": "pass"}))
        self.gh.head_sha = "newsha"
        calls = []
        self.patch_review_start(calls)
        self.ship()
        self.assertEqual(self.gh.merged, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "review")

    # ---------- a review run's lifecycle in finalize (DESIGN D11) ----------

    def review_run(self):
        self.led.upsert_item("x", 5, state="verifying", pr=88, branch="mahler/5-wired")
        rid = self.led.create_run(project="x", number=5, role="review", platform="agy-gemini",
                                  epoch=1, status="running")
        self.led.claim("x", 5, f"run:{rid}", "auto", 30, platform="agy-gemini", run_id=rid)
        self.log = os.path.join(self.tmp, "review-agent.log")
        return {"id": rid, "project": "x", "number": 5, "role": "review", "platform": "agy-gemini",
                "epoch": 1, "pid": None, "worktree": os.path.join(self.tmp, "wt"),
                "branch": "mahler/5-wired", "log_path": self.log,
                "status_path": os.path.join(self.tmp, "exit"),
                "started_at": iso(NOW), "stop_reason": None}

    def test_review_pass_posts_a_comment_and_records_the_verdict(self):
        run = self.review_run()
        self.led.set_kv("review:x#5", json.dumps({"sha": "reviewed-head"}))
        with open(self.log, "w") as fh:
            fh.write("STATUS: REVIEW-PASS no findings\n")
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(self.ctx, "ping"), mock.patch.object(self.ctx, "say"), \
                mock.patch.object(runner, "remove_worktree"):
            finalize.finalize(self.ctx, run)
        self.assertEqual(self.led.item("x", 5)["state"], "verifying")
        self.assertEqual(self.led.lease("x", 5)["holder"], "conductor")
        self.assertEqual(self.led.lease("x", 5)["capacity"], 0)
        info = json.loads(self.led.get_kv("review:x#5"))
        self.assertEqual(info["verdict"], "pass")
        event = self.led.q1("SELECT detail FROM events WHERE kind='review_verdict'")
        self.assertEqual(json.loads(event['detail']), {
            'verdict': 'pass', 'review_run': run['id'], 'reviewed_sha': 'reviewed-head'})
        self.assertIn("no blocking issues", self.gh.comments[-1])

    def test_review_fail_posts_findings_and_records_the_verdict(self):
        run = self.review_run()
        self.led.set_kv("review:x#5", json.dumps({"sha": "reviewed-head"}))
        with open(self.log, "w") as fh:
            fh.write("STATUS: REVIEW-FAIL auth.py: missing null check | db.py: unindexed query\n")
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(self.ctx, "ping"), mock.patch.object(self.ctx, "say"), \
                mock.patch.object(runner, "remove_worktree"):
            finalize.finalize(self.ctx, run)
        self.assertEqual(self.led.item("x", 5)["state"], "verifying")
        info = json.loads(self.led.get_kv("review:x#5"))
        self.assertEqual(info["verdict"], "fail")
        event = self.led.q1("SELECT detail FROM events WHERE kind='review_verdict'")
        self.assertEqual(json.loads(event['detail']), {
            'verdict': 'fail', 'review_run': run['id'], 'reviewed_sha': 'reviewed-head'})
        history = json.loads(self.led.get_kv("reviewfindings:x#5"))
        self.assertEqual(history, [{
            "sha": "reviewed-head",
            "findings": "auth.py: missing null check | db.py: unindexed query",
            "at": iso(NOW),
            "run_id": run["id"],
        }])
        self.assertIn("missing null check", info["findings"])
        body = self.gh.comments[-1]
        self.assertIn("auth.py: missing null check", body)
        self.assertIn("db.py: unindexed query", body)

    def test_review_run_never_snapshots_the_worktree(self):
        """A review run is read-only (recipes/review.md): finalize must not
        try to save/commit anything from its worktree, unlike build/fix."""
        run = self.review_run()
        with open(self.log, "w") as fh:
            fh.write("STATUS: REVIEW-PASS no findings\n")
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(self.ctx, "ping"), mock.patch.object(self.ctx, "say"), \
                mock.patch.object(runner, "remove_worktree"), \
                mock.patch.object(runner, "snapshot") as snap:
            finalize.finalize(self.ctx, run)
        snap.assert_not_called()

    def test_review_run_with_no_status_line_clears_the_pending_verdict(self):
        self.led.set_kv("review:x#5", json.dumps({"sha": "abc123", "verdict": "pending"}))
        run = self.review_run()
        with open(self.log, "w") as fh:
            fh.write("the agent crashed before printing a status line\n")
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(self.ctx, "ping"), mock.patch.object(self.ctx, "say"), \
                mock.patch.object(runner, "remove_worktree"):
            finalize.finalize(self.ctx, run)
        self.assertIsNone(self.led.get_kv("review:x#5"))

    # ---------- a fix run's lifecycle in finalize (mahler#18) ----------

    def fix_run(self, role="fix"):
        """A live fix run on the item, like test_finalize's, with the run's
        lease held."""
        self.led.upsert_item("x", 5, state="working", pr=88, branch="mahler/5-wired")
        rid = self.led.create_run(project="x", number=5, role=role, platform="cline-free",
                                  epoch=1, status="running")
        self.led.claim("x", 5, f"run:{rid}", "auto", 30, platform="cline-free", run_id=rid)
        self.log = os.path.join(self.tmp, "agent.log")
        return {"id": rid, "project": "x", "number": 5, "role": role, "platform": "cline-free",
                "epoch": 1, "pid": None, "worktree": os.path.join(self.tmp, "wt"),
                "branch": "mahler/5-wired", "log_path": self.log,
                "status_path": os.path.join(self.tmp, "exit"),
                "started_at": iso(NOW), "stop_reason": None}

    def last_event(self):
        rows = self.led.q("SELECT detail FROM events WHERE project='x' AND number=5 "
                          "AND kind='state' ORDER BY at DESC LIMIT 1")
        return rows[0]["detail"] if rows else ""

    def test_a_fix_run_gets_the_pr_branch_in_its_prompt(self):
        run = self.fix_run()
        with open(self.log, "w") as fh:
            fh.write("fixed the failing import\nSTATUS: DONE done\n")
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(self.ctx, "ping"), \
                mock.patch.object(self.ctx, "say"), \
                mock.patch.object(runner, "remove_worktree"), \
                mock.patch.object(runner, "snapshot",
                                  return_value={"ref": "mahler/5-wired", "sha": "z",
                                                "ahead": 1, "stat": None}):
            finalize.finalize(self.ctx, run)
        item = self.led.item("x", 5)
        self.assertEqual(item["state"], "verifying")     # CI re-runs on the new SHA
        self.assertEqual(item["attempts"], 0)            # a DONE fix is not a failure
        self.assertEqual(self.led.lease("x", 5)["holder"], "conductor")
        self.assertIn("fix pushed", self.last_event())

    def test_a_preempted_fix_leaves_the_pr_alone(self):
        run = self.fix_run()
        self.led.claim("x", 5, "interactive:you", "interactive", 30)
        run["stop_reason"] = "preempted"
        with open(self.log, "w") as fh:
            fh.write("STATUS: DONE almost\n")
        with mock.patch.object(self.ctx, "ping"), mock.patch.object(self.ctx, "say"), \
                mock.patch.object(runner, "remove_worktree"):
            finalize.finalize(self.ctx, run)
        self.assertEqual(self.led.item("x", 5)["state"], "working")   # handoff to session

    def test_a_failed_fix_counts_and_retries_like_a_build(self):
        run = self.fix_run()
        with open(self.log, "w") as fh:
            fh.write("STATUS: BLOCKED need prod access\n")
        with mock.patch.object(self.ctx, "ping") as ping, \
                mock.patch.object(self.ctx, "say"), \
                mock.patch.object(runner, "remove_worktree"):
            finalize.finalize(self.ctx, run)
        item = self.led.item("x", 5)
        self.assertEqual((item["state"], item["attempts"]), ("ready", 1))
        self.assertIn("attempt 1 failed", self.last_event())
        ping.assert_not_called()

    # ---------- pending CI times out to needs-you (mahler#18, part of #14) ----------

    def test_pending_ci_within_the_timeout_just_waits(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.rollup = [{"state": "PENDING"}]
        self.gh.head_sha = "abc"
        self.ship()
        self.assertEqual(self.item()["state"], "verifying")
        self.assertEqual(self.led.lease("x", 5)["holder"], "conductor")

    def test_pending_ci_times_out_to_needs_you(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.rollup = [{"state": "PENDING"}]
        self.gh.head_sha = "abc"
        self.ship()                              # stamp "since" for abc
        later = lambda: NOW + timedelta(minutes=90)
        self.led.now = later
        try:
            ping = self.ship()
        finally:
            self.led.now = lambda: NOW
        ping.assert_called_once()
        self.assertIn("needs you", ping.call_args[0][0])
        self.assertEqual(ping.call_args.kwargs["priority"], "high")
        self.assertEqual(ping.call_args.kwargs["tags"], "question")
        self.assertTrue(ping.call_args.kwargs["console"])
        self.assertEqual(self.item()["state"], "needs_you")
        self.assertIsNone(self.led.lease("x", 5))               # the slot is released
        self.assertIn("90 min", self.last_event())
        # mahler#248: a structured question with no answer options
        self.assertIn("90 min", self.item()["question"])
        self.assertEqual(self.item()["options"], "[]")

    def test_pending_ci_timeout_restarts_on_a_new_sha(self):
        """A push to the PR (e.g. the fix run) restarts the wait: CI starts over."""
        self.led.upsert_item("x", 5, pr=88)
        self.gh.rollup = [{"state": "PENDING"}]
        self.gh.head_sha = "abc"
        self.ship()                              # stamp "since" for abc
        later = lambda: NOW + timedelta(minutes=90)
        self.led.now = later
        try:
            self.gh.head_sha = "def"             # the fix pushed; CI started over
            ping = self.ship()
        finally:
            self.led.now = lambda: NOW
        ping.assert_not_called()
        self.assertEqual(self.item()["state"], "verifying")

    # ---------- a base that moved (D19) ----------

    def test_conflicting_pr_goes_back_for_a_rebuild(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.mergeable = "CONFLICTING"
        self.gh.rollup = []                  # GitHub runs no CI on a conflicting PR
        ping = self.ship()
        self.assertEqual(self.gh.merged, [])
        item = self.item()
        self.assertEqual((item["state"], item["pr"], item["attempts"]), ("ready", None, 0))
        self.assertEqual(item["branch"], "mahler/5-wired-the-exporter")   # the rebuild resumes it
        self.assertIsNone(self.led.lease("x", 5))
        self.assertEqual(ping.call_args[0][0], "Rebuilding — x #5")

    def test_rebuild_pr_is_not_reported_as_unowned(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.mergeable = "CONFLICTING"
        self.gh.rollup = []
        self.gh.prs = [self._open(88, 5)]

        ping = self.ship()

        self.assertEqual(self.unowned_pings(ping), [])
        self.assertEqual(self.item()["pr"], None)
        self.assertIsNotNone(self.led.get_kv("unowned:x#88"))

    def test_mergeability_not_known_yet_waits(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.mergeable = "UNKNOWN"
        self.ship()
        self.assertEqual(self.gh.merged, [])
        self.assertEqual(self.item()["state"], "verifying")

    def test_no_ci_configured_counts_as_green(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.rollup = []
        self.ship()
        self.assertEqual(self.gh.merged, [88])

    def test_stale_base_rebuild_preserves_attempts_and_work(self):
        self.led.upsert_item("x", 5, pr=88, attempts=2)
        with mock.patch.object(self.gh, "base_in_head", return_value=False):
            self.ship()
        self.assertEqual(self.gh.merged, [])
        self.assertEqual((self.item()["state"], self.item()["pr"], self.item()["attempts"]),
                         ("ready", None, 2))
        self.assertEqual(self.item()["branch"], "mahler/5-wired-the-exporter")
        self.assertIsNone(self.led.lease("x", 5))
        self.assertIn("does not contain current main", self.last_event())

    def test_no_checks_still_requires_freshness(self):
        self.gh.rollup = []
        self.test_stale_base_rebuild_preserves_attempts_and_work()

    def test_changed_pr_observation_never_merges(self):
        self.led.upsert_item("x", 5, pr=88)
        view = self.gh.pr_view(88)
        for change in ({"headRefOid": "replacement"}, {"baseRefName": "release"},
                       {"headRefOid": None}, {"baseRefName": None},
                       {"statusCheckRollup": [{"state": "PENDING"}]},
                       {"statusCheckRollup": [{"state": "FAILURE"}]},
                       {"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}]},
                       {"mergeable": "UNKNOWN"}):
            with self.subTest(change=change), mock.patch.object(
                    self.gh, "pr_view", side_effect=[view, dict(view, **change)]), \
                    mock.patch.object(self.gh, "base_in_head") as guard:
                self.ship()
                guard.assert_not_called()
                self.assertEqual(self.gh.merged, [])
        self.gh.head_sha = "replacement"
        self.gh.rollup = [{"state": "PENDING"}]
        self.ship()
        self.assertEqual(self.gh.merged, [])
        self.gh.rollup = [{"state": "SUCCESS"}]
        self.ship()
        self.assertEqual(self.item()["state"], "done")

    def test_non_main_target_is_used(self):
        self.led.upsert_item("x", 5, pr=88)
        view = dict(self.gh.pr_view(88), baseRefName="release/v2")
        self.gh.queue = True
        with mock.patch.object(self.gh, "pr_view", return_value=view), \
                mock.patch.object(self.gh, "base_in_head", return_value=True) as guard:
            self.ship()
        guard.assert_called_once_with(self.tmp, "release/v2", self.gh.head_sha)
        self.assertEqual(self.gh.merged, [88])

    def test_unknown_freshness_retries_then_times_out_without_attempts(self):
        self.led.upsert_item("x", 5, pr=88)
        with mock.patch.object(self.gh, "base_in_head", side_effect=gh_module.GHError("fetch failed")):
            self.ship()
            self.assertEqual(self.item()["state"], "verifying")
            self.led.now = lambda: NOW + timedelta(minutes=90)
            self.ship()
        self.assertEqual(self.item()["state"], "needs_you")
        self.assertEqual(self.item()["attempts"], 0)
        self.assertEqual(self.gh.merged, [])
        self.assertIsNone(self.led.lease("x", 5))
        self.assertIn("fetch failed", self.last_event())

    def test_lookup_failure_keeps_existing_verification_deadline(self):
        self.led.upsert_item("x", 5, pr=88)
        with mock.patch.object(self.gh, "base_in_head", return_value=None):
            self.ship()
        self.led.now = lambda: NOW + timedelta(minutes=90)
        self.gh.fail_view = {88}
        self.ship()
        self.assertEqual(self.item()["state"], "needs_you")
        self.assertEqual(self.gh.merged, [])

    def test_missing_metadata_on_first_observation_waits(self):
        self.led.upsert_item("x", 5, pr=88)
        for key in ("headRefOid", "baseRefName"):
            view = self.gh.pr_view(88)
            view.pop(key)
            with self.subTest(key=key), mock.patch.object(self.gh, "pr_view", return_value=view), \
                    mock.patch.object(self.gh, "base_in_head") as guard:
                self.ship()
                guard.assert_not_called()
                self.assertEqual(self.item()["state"], "verifying")
                self.assertEqual(self.gh.merged, [])

    def test_unknown_ancestry_and_api_failure_wait(self):
        self.led.upsert_item("x", 5, pr=88)
        with mock.patch.object(self.gh, "base_in_head", return_value=None):
            self.ship()
        view = self.gh.pr_view(88)
        with mock.patch.object(self.gh, "pr_view", side_effect=[view, gh_module.GHError("API down")]):
            self.ship()
        self.assertEqual(self.gh.merged, [])
        self.assertEqual(self.item()["state"], "verifying")
        self.assertEqual(self.item()["attempts"], 0)

    def test_preemption_during_fetch_blocks_merge_and_rebuild(self):
        self.led.upsert_item("x", 5, pr=88)
        def preempt(*args):
            self.led.claim("x", 5, "interactive:mike", "interactive", 30)
            return False
        with mock.patch.object(self.gh, "base_in_head", side_effect=preempt):
            self.ship()
        self.assertEqual(self.gh.merged, [])
        self.assertEqual((self.item()["state"], self.item()["pr"]), ("working", 88))

    def test_preemption_during_failed_fetch_keeps_human_ownership(self):
        self.led.upsert_item("x", 5, pr=88)
        # Start the verification deadline, then fail after it expires.
        with mock.patch.object(self.gh, "base_in_head", return_value=None):
            self.ship()
        self.led.now = lambda: NOW + timedelta(minutes=90)

        def preempt(*args):
            self.led.claim("x", 5, "interactive:mike", "interactive", 30)
            raise gh_module.GHError("fetch failed")

        with mock.patch.object(self.gh, "base_in_head", side_effect=preempt):
            self.ship()
        self.assertEqual(self.gh.merged, [])
        self.assertEqual((self.item()["state"], self.item()["pr"]), ("working", 88))
        self.assertEqual(self.led.lease("x", 5)["holder"], "interactive:mike")

    def test_incomplete_check_does_not_authorize_merge(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.rollup = [{"status": "COMPLETED", "conclusion": None}]
        with mock.patch.object(self.gh, "base_in_head") as guard:
            self.ship()
        guard.assert_not_called()
        self.assertEqual(self.gh.merged, [])
        self.assertEqual(self.item()["state"], "verifying")

    def test_queued_request_does_not_recheck_freshness(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.queue = True
        self.ship()
        with mock.patch.object(self.gh, "base_in_head", return_value=False) as guard:
            self.ship()
        guard.assert_not_called()
        self.assertEqual(self.item()["state"], "verifying")
        self.assertEqual(self.gh.merged, [88])

    # ---------- a merge queue (mahler#211) ----------

    def test_merge_queue_waits_then_ships_once_it_lands(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.queue = True
        ping = self.ship()
        self.assertEqual(self.gh.merged, [88])            # merge requested once
        self.assertEqual(self.item()["state"], "verifying")
        self.assertEqual(self.led.lease("x", 5)["holder"], "conductor")
        ping.assert_not_called()
        self.gh.view_state = "MERGED"                      # the queue landed it
        ping = self.ship()
        self.assertEqual(self.gh.merged, [88])              # not requested again
        self.assertEqual(self.item()["state"], "done")
        self.assertIsNone(self.led.lease("x", 5))
        ping.assert_called_once()
        self.assertEqual(ping.call_args[0][0], "Shipped — x #5")

    def test_merge_queue_does_not_re_request_every_tick(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.queue = True
        self.ship()
        self.ship()
        self.ship()
        self.assertEqual(self.gh.merged, [88])

    def test_merge_queue_new_head_gets_a_fresh_request_and_timeout(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.queue = True
        self.ship()
        self.led.now = lambda: NOW + timedelta(minutes=90)
        self.gh.head_sha = "new-head"
        ping = self.ship()
        self.assertEqual(self.gh.merged, [88, 88])
        self.assertEqual(self.item()["state"], "verifying")
        ping.assert_not_called()
        self.ship()
        self.assertEqual(self.gh.merged, [88, 88])

    def test_merge_queue_confirmation_failure_does_not_repeat_request(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.queue = True
        view = self.gh.pr_view(88)
        with mock.patch.object(self.gh, "pr_view", side_effect=[
                view, view, gh_module.GHError("github down")]):
            ping = self.ship()
        self.assertEqual(self.item()["state"], "verifying")
        self.assertEqual(self.gh.comments, [])
        ping.assert_not_called()
        self.ship()
        self.assertEqual(self.gh.merged, [88])
        self.gh.view_state = "MERGED"
        self.ship()
        self.assertEqual(self.item()["state"], "done")

    def test_merge_queue_stuck_times_out_to_needs_you(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.queue = True
        self.ship()                              # requests the merge, stamps "since"
        later = lambda: NOW + timedelta(minutes=90)
        self.led.now = later
        try:
            ping = self.ship()
        finally:
            self.led.now = lambda: NOW
        ping.assert_called_once()
        self.assertIn("needs you", ping.call_args[0][0])
        self.assertTrue(ping.call_args.kwargs["console"])
        self.assertEqual(self.gh.comments, [])
        self.assertEqual(self.item()["state"], "needs_you")
        self.assertIsNone(self.led.lease("x", 5))
        self.assertIn("90 min", self.last_event())
        # mahler#248: a structured question with no answer options
        self.assertIn("90 min", self.item()["question"])
        self.assertEqual(self.item()["options"], "[]")

    def test_pr_resolved_outside_mahler_is_just_done(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.view_state = "MERGED"
        ping = self.ship()
        self.assertEqual(self.gh.merged, [])
        self.assertEqual(self.item()["state"], "done")
        self.assertIn("**Shipped**", self.gh.comments[-1])
        self.assertIsNotNone(self.led.uat("x", 5))
        self.assertEqual(self.led.unreleased_items("x")[0]["pr"], 88)
        self.assertEqual(ping.call_args.args[0], "Shipped — x #5")

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
                             branch="mahler/6-other-thing", summary="other",
                             pr=89, sorted_at=iso(NOW))
        self.gh.fail_view = {88}
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(self.ctx, "ping"):
            ship.ship(self.ctx, [{"name": "x"}])
        self.assertEqual(self.item()["state"], "verifying")     # 5: retried next tick
        self.assertEqual(self.item(6)["state"], "done")         # 6: still shipped
        self.assertIn("PR lookup failed", " ".join(self.ctx.lines))

    # ---------- open PRs no item tracks (mahler#407) ----------

    def _open(self, n, hours_old, branch="review-gate", draft=False):
        return {"number": n, "title": f"PR {n}", "headRefName": branch,
                "createdAt": iso(NOW - timedelta(hours=hours_old)), "isDraft": draft}

    def unowned_pings(self, ping):
        return [c for c in ping.call_args_list if c[0][0].startswith("PR nobody is shipping")]

    def test_old_untracked_pr_pings_once(self):
        self.led.upsert_item("x", 5, state="done", pr=88)
        self.gh.prs = [self._open(88, 5), self._open(395, 3)]
        ping = self.ship()
        pings = self.unowned_pings(ping)
        self.assertEqual(len(pings), 1)
        self.assertIn("PR #395", pings[0][0][0])
        self.assertIn("mahler ship x#<issue> --pr 395", pings[0][0][1])
        self.led.set_kv("unowned-scan:x", iso(NOW - timedelta(hours=1)))  # scan again
        self.assertEqual(self.unowned_pings(self.ship()), [])            # ...no repeat

    def test_young_draft_and_bot_prs_are_left_alone(self):
        self.gh.prs = [self._open(1, 1), self._open(2, 5, draft=True),
                       self._open(3, 5, branch="dependabot/pip/x")]
        self.assertEqual(self.unowned_pings(self.ship()), [])

    def test_scan_is_throttled(self):
        self.gh.prs = [self._open(395, 3)]
        self.led.set_kv("unowned-scan:x", iso(NOW - timedelta(minutes=5)))
        self.assertEqual(self.unowned_pings(self.ship()), [])

    def test_unowned_check_failure_does_not_break_the_ship_pass(self):
        self.led.upsert_item("x", 5, pr=88)
        with mock.patch.object(self.gh, "open_prs", side_effect=gh_module.GHError("down")):
            self.ship()
        self.assertEqual(self.item()["state"], "done")
        self.assertIn("unowned-PR check failed", " ".join(self.ctx.lines))


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
        
        # Placeholders should be stripped
        for p in ("Nothing", "Nothing.", "- Nothing", "* Nothing.", "Nothing to check",
                  "- none", "N/a", "Nothing known yet — if X, list it here"):
            self.assertEqual(gh_module.needs_human_of(f"## Needs a human to check\n{p}"), "")
            
        # Real checklist items are preserved
        self.assertEqual(gh_module.needs_human_of("## Needs a human to check\n- Confirm login"),
                         "- Confirm login")
        self.assertEqual(gh_module.needs_human_of("## Needs a human to check\n- Nothing\n- Also check login"),
                         "- Nothing\n- Also check login")

    def test_completed_check_uses_its_conclusion(self):
        self.assertEqual(gh_module.checks_state(
            [{"status": "COMPLETED", "conclusion": "FAILURE"}]), "red")
        self.assertEqual(gh_module.checks_state(
            [{"status": "COMPLETED", "conclusion": "SUCCESS"}]), "green")

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


class FreshnessAdapterTests(unittest.TestCase):
    def test_real_origin_drift_and_replacement_head(self):
        with tempfile.TemporaryDirectory() as d:
            repo, origin = os.path.join(d, "repo"), os.path.join(d, "origin.git")
            def git(*args):
                return subprocess.run(["git", "-C", repo, *args], check=True,
                                      capture_output=True, text=True).stdout.strip()
            subprocess.run(["git", "init", "-b", "release", repo], check=True,
                           capture_output=True)
            subprocess.run(["git", "init", "--bare", origin], check=True, capture_output=True)
            git("remote", "add", "origin", origin)
            def commit(message):
                git("-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "--allow-empty", "-m", message)
                return git("rev-parse", "HEAD")
            base = commit("base")
            git("push", "origin", "HEAD:release")
            head = commit("PR work")
            git("push", "origin", "HEAD:topic")
            gh = gh_module.GH("test/repo")
            self.assertTrue(gh.base_in_head(repo, "release", head))
            # Advance origin independently, leaving the checked PR head untouched.
            tree = git("rev-parse", f"{base}^{{tree}}")
            moved = git("-c", "user.name=Test", "-c", "user.email=test@example.com",
                        "commit-tree", tree, "-p", base, "-m", "independent base change")
            git("push", "origin", f"{moved}:release")
            self.assertFalse(gh.base_in_head(repo, "release", head))
            self.assertEqual(git("rev-parse", "HEAD"), head)
            with self.assertRaises(gh_module.GHError):
                gh.base_in_head(repo, "missing", head)
            git("-c", "user.name=Test", "-c", "user.email=test@example.com",
                "merge", "--no-edit", moved)
            replacement = git("rev-parse", "HEAD")
            git("push", "origin", "HEAD:topic")
            self.assertTrue(gh.base_in_head(repo, "release", replacement))

    def test_merge_pins_checked_sha_and_keeps_account(self):
        env = {"GH_CONFIG_DIR": "/test/account"}
        with mock.patch.object(gh_module, "_gh") as call:
            gh_module.GH("x/y", env=env).pr_merge(88, "a" * 40)
        call.assert_called_once_with("pr", "merge", "88", "-R", "x/y", "--squash",
                                     "--delete-branch", "--match-head-commit", "a" * 40,
                                     env=env)

    def test_fetch_order_and_git_account_are_preserved(self):
        env = {"GH_CONFIG_DIR": "/test/account"}
        head, base = "a" * 40, "b" * 40
        with mock.patch.object(gh_module, "_git", side_effect=[
                "", "", "", base, "false"]) as git, mock.patch.object(
                subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 0, "", "")) as ancestry:
            self.assertTrue(gh_module.GH("x/y", env=env).base_in_head(
                "/test/checkout", "release/v2", head))
        self.assertEqual(git.call_args_list, [
            mock.call("/test/checkout", "check-ref-format", "refs/heads/release/v2", env=env),
            mock.call("/test/checkout", "fetch", "--quiet", "--no-tags", "origin", head, env=env),
            mock.call("/test/checkout", "fetch", "--quiet", "--no-tags", "origin",
                      "+refs/heads/release/v2:refs/remotes/origin/release/v2", env=env),
            mock.call("/test/checkout", "rev-parse", "--verify",
                      "refs/remotes/origin/release/v2^{commit}", env=env),
            mock.call("/test/checkout", "rev-parse", "--is-shallow-repository", env=env),
        ])
        ancestry.assert_called_once_with(
            ["git", "-C", "/test/checkout", "merge-base", "--is-ancestor", base, head],
            capture_output=True, text=True, timeout=90, env=env)

    def test_shallow_history_is_unknown(self):
        gh = gh_module.GH("x/y")
        with mock.patch.object(gh, "_git", side_effect=["", "", "", "b" * 40, "true"]), \
                self.assertRaisesRegex(gh_module.GHError, "complete checkout"):
            gh.base_in_head("unused", "main", "a" * 40)

    def test_ancestry_timeout_is_unknown(self):
        gh = gh_module.GH("x/y")
        with mock.patch.object(gh, "_git", side_effect=["", "", "", "b" * 40, "false"]), \
                mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 90)), \
                self.assertRaisesRegex(gh_module.GHError, "ancestry check failed"):
            gh.base_in_head("unused", "main", "a" * 40)

    def test_missing_metadata_and_indeterminate_git_fail_closed(self):
        gh = gh_module.GH("x/y")
        for base, head in ((None, "a" * 40), ("main", None), ("main", "--option")):
            with self.subTest(base=base, head=head), self.assertRaises(gh_module.GHError):
                gh.base_in_head("unused", base, head)
        with mock.patch.object(gh, "_git", side_effect=["", "", "", "b" * 40, "false"]), \
                mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 128, "", "missing object")), self.assertRaises(gh_module.GHError):
            gh.base_in_head("unused", "main", "a" * 40)


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
