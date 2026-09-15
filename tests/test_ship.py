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

    def pr_merge(self, number):
        self.merged.append(number)
        if not self.queue:                # a real merge (no queue) is synchronous
            self.view_state = "MERGED"

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
        self.addCleanup(self.led.close)
        later = iso(NOW + timedelta(hours=2))
        for name in ("agy-claude", "agy-gemini", "claude"):   # live usage samples,
            self.led.record_usage(name, "5h", 10, later)   # so routing has something
            self.led.record_usage(name, "weekly", 10, later)
        self.led.upsert_item("x", 5, state="verifying", priority=2,
                             title="Wired the exporter",
                             branch="mahler/snapshot/5-run7",
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

        def fake_start(ctx, project, it, role, platform, handoff_from=None, size=None):
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
        self.assertEqual(self.led.lease("x", 5)["holder"], "conductor")

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
        self.assertEqual(item["branch"], "mahler/snapshot/5-run7")   # the rebuild resumes it
        self.assertIsNone(self.led.lease("x", 5))
        self.assertEqual(ping.call_args[0][0], "Rebuilding — x #5")

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
                view, gh_module.GHError("github down")]):
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
            ship.ship(self.ctx, [{"name": "x"}])
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
