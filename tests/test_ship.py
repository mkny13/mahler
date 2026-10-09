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
from types import SimpleNamespace
from unittest import mock

from mahler import cli, config, finalize, gh as gh_module, platforms, router, runner, scheduler, ship, sync
from mahler.ledger import CONDUCTOR, Ledger, iso

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

    def ci_retry_runs(self, head, base, *, reported=False):
        return []

    def issue_labels(self, number):
        return getattr(self, "labels", ["mahler:verifying", "type:feature"])

    def close_issue(self, number):
        self.closed = number

    def set_state_label(self, number, state, current):
        self.labels = [x for x in current if x not in gh_module.LABEL_STATES]
        self.labels.append(gh_module.STATE_LABELS[state])

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

    def pr_refresh_mergeability(self, number):
        pass

    def failed_run_log(self, branch, tail=150):
        return 123, "CI assertion failed"

    def issue_state(self, number):
        return "OPEN"

    def base_in_head(self, path, base, head):
        return True

    def pr_merge(self, number, head):
        assert head == self.head_sha
        self.merged.append(number)
        if not self.queue:                # a real merge (no queue) is synchronous
            self.view_state = "MERGED"

    sources = None    # {(sha, file, line): text}; None = every line reads the same

    def source_line(self, path, sha, file, line):
        if self.sources is None:
            return "x = f'{token}'"
        return self.sources.get((sha, file, line))

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

    def resume_builder(self):
        self.led.upsert_item("x", 5, pr=88, branch="mahler/5-x", labels='["size:m"]')
        return self.led.create_run(project="x", number=5, role="build", platform="claude",
            status="ended", outcome="DONE", epoch=1, branch="mahler/5-x", session_id="saved-123")

    def test_resume_all_fix_triggers_keep_current_feedback(self):
        source = self.resume_builder()
        for trigger in ("ci", "review", "conflict"):
            with self.subTest(trigger=trigger), mock.patch.object(ship, "start", return_value=True) as start, \
                    mock.patch.object(self.ctx, "ping"), mock.patch.object(ship, "_repeat_finding", return_value=None):
                view = self.gh.pr_view(88)
                view["headRefOid"] = trigger
                if trigger == "ci":
                    ship._red_ci(self.ctx, "x", self.item(), 88, view)
                else:
                    ship._review_triggered_fix(self.ctx, "x", self.item(), 88, view,
                        "current blocking feedback", base_conflict=trigger == "conflict")
                self.assertEqual(start.call_args.args[2]["branch"], "mahler/5-x")
                self.assertEqual(start.call_args.args[4], "claude")
                self.assertEqual(start.call_args.kwargs["resume_from"], source)
                if trigger != "ci":
                    self.assertIn("current blocking feedback", start.call_args.kwargs["context"])

    def test_ci_launch_rejection_falls_back_once(self):
        self.check_resume_fallback("ci", detached=False)

    def test_review_launch_rejection_falls_back_once(self):
        self.check_resume_fallback("review", detached=False)

    def test_conflict_launch_rejection_falls_back_once(self):
        self.check_resume_fallback("conflict", detached=False)

    def test_ci_detached_rejection_falls_back_once(self):
        self.check_resume_fallback("ci", detached=True)

    def test_review_detached_rejection_falls_back_once(self):
        self.check_resume_fallback("review", detached=True)

    def test_conflict_detached_rejection_falls_back_once(self):
        self.check_resume_fallback("conflict", detached=True)

    def check_resume_fallback(self, trigger, detached):
        source = self.resume_builder()
        self.led.claim("x", 5, CONDUCTOR, "auto", 60, capacity=False)
        view = self.gh.pr_view(88)
        prep = {"worktree": self.tmp, "run_dir": self.tmp, "branch": "mahler/5-x",
                "base_ref": "origin/mahler/5-x"}
        meta = {"pid": 123, "worktree": self.tmp, "branch": "mahler/5-x",
                "log_path": os.path.join(self.tmp, "agent.log"),
                "status_path": os.path.join(self.tmp, "exit")}

        def trigger_fix():
            if trigger == "ci":
                ship._red_ci(self.ctx, "x", self.item(), 88, view)
            else:
                ship._review_triggered_fix(self.ctx, "x", self.item(), 88, view,
                    "current blocking feedback", base_conflict=trigger == "conflict")

        def launch(*args, **kwargs):
            if "resume_from" in kwargs and not detached:
                raise runner.ResumeUnsupported("source session storage is inaccessible")
            return meta

        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(self.ctx, "ping"), \
                mock.patch.object(ship, "_repeat_finding", return_value=None), \
                mock.patch.object(runner, "prepare", return_value=prep), \
                mock.patch("mahler.tick.prompt.build", return_value="current full prompt"), \
                mock.patch.object(runner, "launch", side_effect=launch) as launched, \
                mock.patch.object(runner, "remove_worktree"), \
                mock.patch("mahler.launch_health.failed") as failed, \
                mock.patch("mahler.launch_health.succeeded"):
            trigger_fix()
            rejected = self.led.last_run("x", 5, roles=("fix",))
            self.assertEqual(launched.call_args.kwargs, {"resume_from": source})
            if detached:
                with open(meta["log_path"], "w") as stream:
                    stream.write("Error: session saved-123 expired\n")
                with open(meta["status_path"], "w") as stream:
                    stream.write("1")
                finalize.finalize(self.ctx, rejected)
                finalize.finalize(self.ctx, rejected)
            self.assertEqual(self.led.run(rejected["id"])["stop_reason"], "resume_rejected")
            self.assertEqual(self.led.lease("x", 5)["holder"], CONDUCTOR)
            self.assertEqual(ship._review_route(self.ctx, "x", self.item(), "abc123")[2], {"claude"})
            failed.assert_not_called()

            # Reopen persisted state, so fallback does not depend on process memory.
            persisted = os.path.join(self.tmp, "restart.db")
            with sqlite3.connect(persisted) as connection:
                self.led.con.backup(connection)
            connection.close()
            restarted = Ledger(persisted, clock=lambda: NOW + timedelta(seconds=1))
            self.addCleanup(restarted.close)
            self.led = restarted
            self.ctx.led = restarted
            # The normal route is free to pick a different platform.
            with mock.patch.object(router, "pick_for_project", return_value=("agy-gemini", [])):
                trigger_fix()
            self.assertEqual(launched.call_count, 2)
            self.assertEqual(launched.call_args.kwargs, {})
            self.assertEqual(launched.call_args.args[4], "agy-gemini")
            self.assertEqual(launched.call_args.args[2]["branch"], "mahler/5-x")
            expected = 0 if trigger == "conflict" else 1
            self.assertEqual((self.item()["attempts"], self.item()["esc_fails"]),
                             (expected, expected))
            # Ordinary shipping ticks cannot launch another fix while it is active.
            self.ship()
            self.ship()
            self.assertEqual(launched.call_count, 2)
            fresh = self.led.last_run("x", 5, roles=("fix",))
            self.led.update_run(fresh["id"], status="ended", outcome="DONE")
            self.cfg["platforms"]["gemini-peer"] = {
                **self.cfg["platforms"]["agy-gemini"], "slot": "agy-gemini"}
            pin, size, exclude = ship._review_route(self.ctx, "x", self.item(), "next")
            for candidate in ("agy-gemini", "gemini-peer"):
                chosen, _ = router.pick(self.cfg, self.led, "review", candidate,
                                        size=size, exclude=exclude)
                self.assertIsNone(chosen)

    def test_successful_resumed_fixer_is_source_and_review_excludes_its_slot(self):
        self.resume_builder()
        fixer = self.led.create_run(project="x", number=5, role="fix", platform="claude",
            status="ended", outcome="DONE", epoch=2, branch="mahler/5-x", session_id="continued")
        self.led.create_run(project="x", number=5, role="review", platform="agy-gemini",
            status="ended", outcome="REVIEW-FAIL", epoch=3, branch="mahler/5-x", session_id="review")
        source = ship._resume_source(self.ctx, "x", self.item(), "mahler/5-x", "new", (), "m", 0)
        self.assertEqual(source["id"], fixer)
        self.cfg["platforms"]["claude-peer"] = {**self.cfg["platforms"]["claude"], "slot": "claude"}
        _, size, exclude = ship._review_route(self.ctx, "x", self.item(), "new")
        for candidate in ("claude", "claude-peer"):
            self.assertIsNone(router.pick(self.cfg, self.led, "review", candidate,
                                         size=size, exclude=exclude)[0])

    def test_resume_source_gates_and_branch_identity(self):
        source = self.resume_builder()
        pol = self.ctx.policy("x")
        for gate in ("missing", "cline", "quota", "tier", "busy", "account", "pin", "branch", "reserved", "size", "disabled"):
            with self.subTest(gate=gate):
                cfg = copy.deepcopy(self.cfg)
                item = dict(self.item())
                busy, tier = set(), 0
                if gate == "size":
                    cfg["platforms"]["claude"]["max_size"] = "s"
                if gate == "disabled":
                    cfg["platforms"]["claude"]["enabled"] = False
                if gate == "missing":
                    self.led.update_run(source, session_id=None)
                if gate == "cline":
                    cfg["platforms"]["claude"]["kind"] = "cline"
                if gate == "quota":
                    self.led.record_usage("claude", router.HOLD, 100, iso(NOW + timedelta(hours=1)))
                if gate == "tier":
                    tier = 100
                if gate == "busy":
                    busy = {"claude"}
                if gate == "account":
                    cfg["platforms"]["claude"]["account"] = "other"
                if gate == "pin":
                    item["pin"] = "agy-gemini"
                if gate == "reserved":
                    self.led.set_kv("cycle:resume", "reserved")
                with mock.patch.object(self.ctx, "cfg", cfg):
                    self.assertIsNone(ship._resume_source(self.ctx, "x", item,
                        "different" if gate == "branch" else "mahler/5-x", "cycle", busy, "m", tier))
                self.led.update_run(source, session_id="saved-123")
                self.led.set_kv("cycle:resume", None)
                self.led.clear_usage("claude", [router.HOLD])

    def test_rejected_resume_does_not_reset_cycle_or_review_identity(self):
        source = self.resume_builder()
        key = "red:x#5:88:abc123"
        self.led.set_kv(key, iso(NOW - timedelta(seconds=1)))
        self.led.set_kv(key + ":charged", "1")
        self.led.set_kv(key + ":resume", "reserved")
        self.led.create_run(project="x", number=5, role="fix", platform="agy-gemini",
            status="ended", outcome="exit 1", stop_reason="resume_rejected", epoch=2,
            branch="mahler/5-x")
        ship._clear_charged_if_fix_completed(self.led, "x", 5, key)
        self.assertEqual(self.led.get_kv(key + ":charged"), "1")
        self.assertIsNone(ship._resume_source(self.ctx, "x", self.item(), "mahler/5-x", key, (), "m", 0))
        self.assertEqual(ship._review_route(self.ctx, "x", self.item(), "abc123")[2], {"claude"})

    def test_capture_after_acceptable_ci_before_review_even_low_risk(self):
        self.led.upsert_item("x", 5, pr=88, labels='["type:chore", "size:s"]')
        for rollup, expected in [([{"state": "SUCCESS"}], ["capture", "review"]),
                                 ([], ["capture", "review"]),
                                 ([{"state": "PENDING"}], []),
                                 ([{"state": "FAILURE"}], [])]:
            with self.subTest(rollup=rollup):
                self.gh.rollup = rollup
                calls = []
                with mock.patch.object(ship.screenshots, "capture", side_effect=lambda *a: calls.append("capture")), \
                        mock.patch.object(ship, "_review_gate", side_effect=lambda *a: calls.append("review")), \
                        mock.patch.object(ship, "_red_ci"):
                    self.ship()
                self.assertEqual(calls, expected)

    def test_capture_failure_still_reaches_review(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.head_sha = "a" * 40
        self.cfg["projects"]["x"].update(
            screenshot="capture", screenshot_environment="Preview")
        self.gh.screenshot_preview = mock.Mock(side_effect=RuntimeError("signed-secret"))
        with mock.patch.object(ship, "_review_gate") as review:
            self.ship()
        review.assert_called_once()
        result = json.loads(self.led.get_kv(f"screenshot:x:88:{self.gh.head_sha}"))
        self.assertEqual(result["state"], "unavailable")
        self.assertNotIn("signed-secret", json.dumps(result))

    def test_delivery_runs_between_capture_and_review_and_never_blocks(self):
        self.led.upsert_item("x", 5, pr=88)
        calls = []
        with mock.patch.object(ship.screenshots, "capture", side_effect=lambda *a: calls.append("capture")), \
                mock.patch.object(ship.screenshot_delivery, "deliver", side_effect=lambda *a: calls.append("deliver")), \
                mock.patch.object(ship, "_review_gate", side_effect=lambda *a: calls.append("review")):
            self.ship()
        self.assertEqual(calls, ["capture", "deliver", "review"])

    def test_delivery_records_final_head_beside_merge_sha(self):
        self.led.upsert_item("x", 5, pr=88)
        self.led.set_kv(f"screenshot:x:88:{self.gh.head_sha}", json.dumps(
            {"sha": self.gh.head_sha, "pr": 88, "state": "success", "reason": "captured"}))
        self.gh.view_state = "MERGED"
        self.ship()
        final = json.loads(self.led.get_kv("screenshot-final:x#5"))
        self.assertEqual((final["head"], final["merge_sha"]),
                         (self.gh.head_sha, "4c1f0abfeed5"))

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
        self.assertEqual(self.gh.closed, 5)
        self.assertEqual(self.gh.labels, ["type:feature", "mahler:shipped"])
        self.assertEqual(self.item()["state"], "shipped")
        self.assertIsNone(self.led.lease("x", 5))       # conductor lease released
        body = self.gh.comments[-1]
        self.assertTrue(body.startswith("<!-- mahler:agent -->"))
        self.assertIn("wired the exporter", body)
        self.assertIn("## Needs a human to check", body)
        self.assertIn("- the new ping arrives", body)
        ping.assert_called_once()
        self.assertEqual(ping.call_args[0][0], "Shipped — x #5")

    def test_confirmed_merge_registers_post_merge_after_shipped(self):
        from mahler import post_merge
        self.led.upsert_item("x", 5, pr=88)
        observed = []
        with mock.patch.object(post_merge, "register", side_effect=lambda *args:
                               observed.append(self.item()["state"])):
            self.ship()
        self.assertEqual(observed, ["shipped"])

    def test_disabled_contract_keeps_shipment_without_check(self):
        self.led.upsert_item("x", 5, pr=88)
        self.ship()
        self.assertEqual(self.item()["state"], "shipped")
        self.assertEqual(self.led.q("SELECT * FROM post_merge_checks"), [])

    def test_shipped_label_failure_retries_after_issue_leaves_open_poll(self):
        self.led.upsert_item("x", 5, pr=88)
        with mock.patch.object(self.gh, "set_state_label", side_effect=gh_module.GHError("down")):
            self.ship()
        self.assertEqual(self.item()["state"], "shipped")
        self.assertIsNone(self.led.lease("x", 5))
        self.assertNotEqual(self.item()["mirror"], "mahler:shipped")
        self.ctx._labels.clear()
        with mock.patch.object(self.ctx, "gh", return_value=self.gh):
            sync.mirror_labels(self.ctx, "x")
        self.assertEqual(self.gh.labels, ["type:feature", "mahler:shipped"])
        self.assertEqual(self.item()["mirror"], "mahler:shipped")

    def test_shipped_comment_failure_still_closes_the_loop(self):
        self.led.upsert_item("x", 5, pr=88)
        with mock.patch.object(self.gh, "comment",
                               side_effect=gh_module.GHError("rate limited")):
            ping = self.ship()   # must not raise
        self.assertEqual(self.gh.merged, [88])
        self.assertEqual(self.item()["state"], "shipped")
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
        self.assertFalse(any("**Shipped**" in c for c in self.gh.comments))
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
        self.assertEqual(self.gh.comments, [])
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
        self.assertEqual(self.item()["state"], "shipped")
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
        self.assertIn("class=pr_closed", self.gh.comments[-1])
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

    def test_shipped_without_a_needs_human_list_gets_default_check(self):
        self.gh.view_body = "plain ship, nothing to check."
        self.led.upsert_item("x", 5, pr=88)
        self.ship()
        self.assertIn("passing evidence", self.led.uat("x", 5)["needs"])

    def test_a_failing_uat_write_does_not_stop_the_ship(self):
        self.led.upsert_item("x", 5, pr=88)
        with mock.patch.object(self.led, "add_uat",
                               side_effect=sqlite3.OperationalError("db locked")):
            self.ship()   # must not raise
        self.assertEqual(self.item()["state"], "shipped")
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

    def test_ci_fix_records_its_exact_charge_for_finalize(self):
        self.led.upsert_item("x", 5, pr=88, attempts=1, esc_fails=1)
        self.gh.rollup = [{"state": "FAILURE"}]
        def launch(ctx, project, item, role, platform, **kwargs):
            self.fix_id = self.led.create_run(project=project, number=5, role=role,
                                             platform=platform, epoch=1, status="running")
            return True
        with mock.patch.object(ship, "start", side_effect=launch):
            self.ship()
        info = json.loads(self.led.get_kv(f"ci-fix:{self.fix_id}"))
        self.assertEqual(info["cycle"], "red:x#5:88:abc123")
        self.assertEqual(info["before"], {"attempts": 1, "esc_tier": 0, "esc_fails": 1})
        self.assertEqual(info["after"], {k: self.item()[k] for k in info["before"]})
        self.assertEqual(info["after"]["attempts"], 2)

    def retry_setup(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.rollup = [{"conclusion": "FAILURE",
                           "detailsUrl": f"https://github.com/o/r/actions/runs/{run}/job/100"}
                          for run in (10, 11)]
        self.gh.ci_retry_runs = mock.Mock(side_effect=lambda *a: [{"id": 10, "attempt": 1}, {"id": 11, "attempt": 2}])
        self.gh.rerun_failed_jobs = mock.Mock()
        self.gh.actions_run = mock.Mock(return_value={"head_sha": "abc123", "run_attempt": 2,
                                                      "status": "completed", "conclusion": "failure"})
        return "ci-rerun:x#5:88:abc123"

    def test_ci_rerun_stale_pending_green_keeps_review_gate(self):
        key = self.retry_setup()
        start = self.patch_start()
        self.ship()
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)  # restart: only durable state survives
        self.ship()  # second workflow still has its old attempt
        self.assertEqual(self.gh.rerun_failed_jobs.call_count, 2)
        self.gh.actions_run.return_value.update(run_attempt=3, status="in_progress")
        self.ship()
        self.gh.actions_run.return_value.update(status="completed", conclusion="success")
        self.ship()  # old rollup must not launch a fix
        start.assert_not_called()
        self.assertEqual((self.item()["attempts"], self.item()["esc_fails"]), (0, 0))
        self.gh.rollup = [{"state": "SUCCESS"}]
        with mock.patch.object(ship, "_review_gate") as gate:
            self.ship()
        gate.assert_called_once()
        self.assertTrue(json.loads(self.led.get_kv(key))["done"])
        self.assertEqual(self.gh.merged, [])

    def assert_unmatched_failure_starts_fix(self, unmatched):
        key = self.retry_setup()
        self.gh.rollup.append(unmatched)
        start = self.patch_start()
        self.ship()
        start.assert_not_called()
        self.gh.actions_run.return_value.update(run_attempt=3, conclusion="success")
        self.gh.rollup = [unmatched]
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
        self.ship()
        start.assert_called_once()
        self.assertEqual((self.item()["attempts"], self.item()["esc_fails"]), (1, 1))
        self.assertTrue(json.loads(self.led.get_kv(key))["done"])
        self.ship()
        start.assert_called_once()
        self.assertEqual(self.gh.rerun_failed_jobs.call_count, 2)
        self.assertEqual(self.gh.merged, [])

    def test_ci_rerun_success_with_unmatched_workflow_starts_normal_fix(self):
        self.assert_unmatched_failure_starts_fix({
            "conclusion": "FAILURE",
            "detailsUrl": "https://github.com/o/r/actions/runs/12/job/101"})

    def test_ci_rerun_success_with_external_failure_starts_normal_fix(self):
        self.assert_unmatched_failure_starts_fix({
            "state": "FAILURE", "targetUrl": "https://external.example/check"})

    def test_ci_rerun_terminal_red_dedup_and_new_head(self):
        key = self.retry_setup()
        start = self.patch_start()
        self.ship()
        self.gh.actions_run.return_value.update(run_attempt=3)
        self.ship()
        start.assert_called_once()
        self.assertEqual((self.item()["attempts"], self.item()["esc_fails"]), (1, 1))
        self.led.release("x", 5)
        self.led.set_state("x", 5, "verifying", "re-shipped")
        self.led.set_kv("red:x#5:88:abc123", None)
        self.led.set_kv("red:x#5:88:abc123:charged", None)
        with mock.patch.object(ship, "_red_ci"):
            self.ship()
        self.assertEqual(self.gh.rerun_failed_jobs.call_count, 2)
        self.gh.head_sha = "new-head"
        self.ship()
        self.assertEqual(self.gh.rerun_failed_jobs.call_count, 4)

    def test_ci_rerun_uncertain_post_reconciles_and_times_out(self):
        self.retry_setup()
        self.gh.rerun_failed_jobs.side_effect = gh_module.GHError("response lost")
        self.ship()
        self.gh.rerun_failed_jobs.side_effect = None
        self.ship()
        self.assertEqual(self.gh.rerun_failed_jobs.call_count, 2)
        self.ship()
        self.assertEqual(self.gh.rerun_failed_jobs.call_count, 2)
        self.led.clock = lambda: NOW + timedelta(days=1)
        self.ship()
        self.assertEqual(self.item()["state"], "needs_you")
        self.assertEqual(self.item()["attempts"], 0)

    def test_ci_rerun_lost_response_then_completed_attempt_is_not_reposted(self):
        key = self.retry_setup()
        self.gh.rerun_failed_jobs.side_effect = gh_module.GHError("response lost")
        self.ship()
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
        self.gh.rerun_failed_jobs.side_effect = None
        self.gh.actions_run.return_value.update(run_attempt=3, conclusion="success")
        self.ship()  # reconcile first request; request second workflow
        self.gh.rollup = [{"state": "SUCCESS"}]
        with mock.patch.object(ship, "_review_gate") as gate:
            self.ship()
        gate.assert_called_once()
        self.assertTrue(json.loads(self.led.get_kv(key))["done"])
        self.assertEqual(self.gh.rerun_failed_jobs.call_args_list, [mock.call(10), mock.call(11)])
        self.assertEqual(self.item()["attempts"], 0)

    def test_ci_rerun_dry_run_has_no_mutation(self):
        key = self.retry_setup()
        self.ctx.dry_run = True
        self.ship()
        self.gh.rerun_failed_jobs.assert_not_called()
        self.assertIsNone(self.led.get_kv(key))

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

    def test_legacy_capacity_pauses_recover_only_when_route_returns(self):
        for role, question in (
            ("fix", "No fix run could start for PR #88 after 120 minutes: no platform"),
            ("review", "Review waiting — PR #88 is CI-green but no review could start "
                       "for over 120 minutes: no platform"),
        ):
            with self.subTest(role=role):
                self.led.set_kv("capacity-stranded-migrated:x", "")
                self.led.set_state("x", 5, "needs_you", pr=88, question=question)
                with mock.patch.object(platforms, "available", return_value=False):
                    self.ship()
                self.assertEqual(self.item()["state"], "needs_you")
                marker = json.loads(self.led.get_kv("capacity-stranded:x#5"))
                self.assertEqual(marker["role"], role)
                # Recovery does not parse the user-facing text again.
                self.led.upsert_item("x", 5, question="Translated capacity explanation")
                with mock.patch.object(ship, "_ship_item") as ordinary:
                    self.ship()
                self.assertEqual(self.item()["state"], "verifying")
                self.assertIsNone(self.item()["question"])
                self.assertEqual(self.item()["options"], "[]")
                self.assertFalse(self.led.get_kv("capacity-stranded:x#5"))
                ordinary.assert_called_once()

    def test_new_capacity_escalation_persists_recoverable_marker(self):
        self.led.upsert_item("x", 5, pr=88)
        ship.migrate_capacity_waits(self.ctx, "x")
        with mock.patch.object(self.ctx, "ping"):
            ship._capacity_wait(self.ctx, "x", self.item(), "reviewfix-status:x#5",
                                "cycle", "no route", "fix", ([], None, None), 2)
        self.assertEqual(self.item()["state"], "needs_you")
        self.assertEqual(json.loads(self.led.get_kv("capacity-stranded:x#5"))["role"], "fix")
        with mock.patch.object(ship, "_ship_item"):
            self.ship()
        self.assertEqual(self.item()["state"], "verifying")
        self.assertFalse(self.led.get_kv("reviewfix-status:x#5"))

    def test_capacity_recovery_preserves_decisions_and_later_pauses(self):
        self.led.set_state("x", 5, "needs_you", pr=88, question="Which product should we build?")
        with mock.patch.object(ship, "_ship_item") as ordinary:
            self.ship()
        ordinary.assert_not_called()
        self.assertEqual(self.item()["question"], "Which product should we build?")
        ship._mark_capacity_wait(self.led, "x", self.item(), "fix")
        self.led.now = lambda: NOW + timedelta(seconds=1)
        self.led.set_state("x", 5, "needs_you", question="Please supply credentials")
        self.ship()
        self.assertEqual(self.item()["state"], "needs_you")
        self.assertEqual(self.item()["question"], "Please supply credentials")
        self.assertFalse(self.led.get_kv("capacity-stranded:x#5"))

    def test_capacity_recovery_respects_review_independence(self):
        self.led.set_state("x", 5, "needs_you", pr=88, labels='["size:m"]')
        ship._mark_capacity_wait(self.led, "x", self.item(), "review")
        self.cfg["routing"]["review"] = ["agy-gemini"]
        with mock.patch.object(ship, "_code_source", return_value={"platform": "agy-gemini"}):
            self.ship()
        self.assertEqual(self.item()["state"], "needs_you")
        self.cfg["routing"]["review"].append("agy-claude")
        with mock.patch.object(ship, "_code_source", return_value={"platform": "agy-gemini"}), \
                mock.patch.object(ship, "_ship_item"):
            self.ship()
        self.assertEqual(self.item()["state"], "verifying")

    def test_capacity_recovery_waits_for_quota_and_required_fix_tier(self):
        self.cfg["routing"]["build"] = ["agy-gemini"]
        self.led.set_state("x", 5, "needs_you", pr=88, labels='["size:s"]',
                           title="Fix authentication", esc_tier=2)
        ship._mark_capacity_wait(self.led, "x", self.item(), "fix")
        self.led.record_usage("agy-gemini", "weekly", 100, iso(NOW + timedelta(hours=2)))
        self.ship()
        self.assertEqual(self.item()["state"], "needs_you")
        self.led.record_usage("agy-gemini", "weekly", 10, iso(NOW + timedelta(hours=2)))
        with mock.patch.object(ship, "_ship_item"):
            self.ship()
        self.assertEqual(self.item()["state"], "verifying")

    def test_capacity_recovery_leaves_session_and_dry_run_alone(self):
        self.led.set_state("x", 5, "needs_you", pr=88)
        ship._mark_capacity_wait(self.led, "x", self.item(), "fix")
        self.ctx.dry_run = True
        self.ship()
        self.assertEqual(self.item()["state"], "needs_you")
        self.ctx.dry_run = False
        self.led.claim("x", 5, "human", "session", 30)
        self.ship()
        self.assertEqual(self.item()["state"], "needs_you")

    def test_review_capacity_wait_uses_its_own_backstop_and_resets_per_head(self):
        self.led.upsert_item("x", 5, pr=88, labels='["size:l"]')
        now = [NOW]
        self.led.now = lambda: now[0]
        with mock.patch("mahler.router.pick_for_project", return_value=(None, [])):
            self.ship().assert_not_called()
            now[0] += timedelta(hours=3)
            self.ship().assert_not_called()
            self.assertEqual(self.item()["state"], "verifying")
            self.gh.head_sha = "def456"
            self.ship().assert_not_called()
            now[0] += timedelta(hours=23)
            self.ship().assert_not_called()
            now[0] += timedelta(hours=1)
            ping = self.ship()
            ping.assert_called_once()
            self.assertIn("no known recovery", ping.call_args.args[1])
            self.assertEqual(self.item()["state"], "needs_you")

    def test_fix_capacity_waits_retry_and_only_unknown_backstop_escalates(self):
        for review in (False, True):
            with self.subTest(review=review):
                self.led.upsert_item("x", 5, pr=88, state="verifying", labels='["size:m"]')
                self.gh.rollup = [{"state": "SUCCESS" if review else "FAILURE"}]
                self.led.set_kv("review:x#5", json.dumps({"sha": "abc123", "verdict": "fail", "pr": 88})
                                if review else None)
                self.led.set_kv("reviewfix-status:x#5", None)
                now = [NOW]
                self.led.now = lambda: now[0]
                with mock.patch("mahler.router.pick_for_project", return_value=(None, ["stale usage"])):
                    self.ship()
                    now[0] += timedelta(hours=3)
                    self.ship().assert_not_called()
                    self.assertEqual(self.item()["state"], "verifying")
                    now[0] += timedelta(hours=21)
                    ping = self.ship()
                    self.assertIn("no known recovery", ping.call_args.args[1])
                    self.assertEqual(self.item()["state"], "needs_you")

    def test_fix_start_clears_wait_only_on_success(self):
        for review in (False, True):
            with self.subTest(review=review):
                self.led.upsert_item("x", 5, pr=88, attempts=0, labels='["size:m"]')
                self.gh.rollup = [{"state": "SUCCESS" if review else "FAILURE"}]
                verdict = {"sha": "abc123", "verdict": "fail", "pr": 88}
                self.led.set_kv("review:x#5", json.dumps(verdict) if review else None)
                key = f"{'reviewfix' if review else 'red'}:x#5:88:abc123"
                with mock.patch("mahler.router.pick_for_project", return_value=(None, [])):
                    self.ship()
                wait = self.led.get_kv("reviewfix-status:x#5")
                self.assertEqual(json.loads(wait)["state"], "capacity_wait")
                cycle = self.led.get_kv(key)
                with mock.patch("mahler.router.pick_for_project", return_value=("claude", [])), \
                        mock.patch.object(ship, "start", return_value=False) as start:
                    self.ship()
                    start.assert_called_once()
                self.assertEqual(self.led.get_kv("reviewfix-status:x#5"), wait)
                self.assertEqual(self.led.get_kv(key), cycle)
                self.assertEqual(self.item()["attempts"], 0)
                self.assertFalse(self.led.get_kv(f"{key}:charged"))

                def launch(ctx, project, item, role, platform, **kwargs):
                    self.led.create_run(project=project, number=item["number"],
                                        role=role, platform=platform, epoch=1)
                    return True

                with mock.patch("mahler.router.pick_for_project", return_value=("claude", [])), \
                        mock.patch.object(ship, "start", side_effect=launch) as start:
                    self.ship()
                    start.assert_called_once()
                self.assertFalse(self.led.get_kv("reviewfix-status:x#5"))
                self.assertEqual(self.led.get_kv(key), cycle)
                self.assertEqual(self.item()["attempts"], 1)
                self.assertEqual(self.led.get_kv(f"{key}:charged"), "1")
                run = self.led.last_run("x", 5, roles=("fix",))
                if review:
                    self.assertEqual(json.loads(self.led.get_kv(f"{key}:run")),
                                     {"run_id": run["id"], "verdict": verdict})
                self.led.update_run(run["id"], status="ended")

    def test_known_hold_skips_routing_then_recovers(self):
        self.cfg["routing"]["review"] = ["agy-claude"]
        self.led.upsert_item("x", 5, pr=88, labels='["size:m"]')
        now = [NOW]
        self.led.now = lambda: now[0]
        reset = NOW + timedelta(hours=30)
        self.led.record_usage("agy-claude", "hold", 100, iso(reset))
        with mock.patch("mahler.router.pick_for_project") as pick:
            self.ship().assert_not_called()
            now[0] += timedelta(hours=25)
            self.ship().assert_not_called()
            pick.assert_not_called()
        self.assertEqual(self.item()["state"], "verifying")
        self.assertEqual(json.loads(self.led.get_kv("review-wait:x#5"))["retry_at"], iso(reset))
        now[0] = reset
        with mock.patch("mahler.router.pick_for_project", return_value=("agy-claude", [])), \
                mock.patch.object(ship, "start", return_value=True) as start:
            self.ship()
            start.assert_called_once()
        self.assertFalse(self.led.get_kv("review-wait:x#5"))

    def test_missing_required_fix_tier_escalates_distinctly(self):
        self.cfg["routing"]["build"] = ["kilo"]
        self.led.upsert_item("x", 5, pr=88, title="credentials fix", labels='["size:s"]')
        self.gh.rollup = [{"state": "FAILURE"}]
        ping = self.ship()
        self.assertEqual(self.item()["state"], "needs_you")
        self.assertIn("No configured eligible platform", ping.call_args.args[1])
        self.assertIn("required tier 2", ping.call_args.args[1])

    def test_failed_review_launch_keeps_the_wait_clock(self):
        self.led.upsert_item("x", 5, pr=88, labels='["size:m"]')
        with mock.patch("mahler.router.pick_for_project", return_value=(None, [])):
            self.ship()
        before = self.led.get_kv("review-wait:x#5")
        self.assertTrue(before)
        with mock.patch("mahler.router.pick_for_project", return_value=("claude", [])), \
                mock.patch.object(ship, "start", return_value=False):
            self.ship()
        self.assertEqual(self.led.get_kv("review-wait:x#5"), before)

    def test_failed_review_releases_capacity_for_another_green_pr(self):
        self.led.upsert_item("x", 5, pr=88, labels='["size:m"]')
        self.led.set_kv("review:x#5", json.dumps({"sha": "abc123", "verdict": "fail", "pr": 88}))
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
            ctx.led.set_kv("review:x#6", json.dumps({"sha": "abc123", "verdict": "pass", "pr": 89}))
            return True
        with mock.patch.object(self.led, "claim", side_effect=bounded_claim), \
             mock.patch("mahler.router.pick_for_project",
                        side_effect=[(None, ["no quota"]), ("claude", [])]), \
             mock.patch.object(ship, "start", side_effect=review_start):
            self.ship()
        self.led.set_kv("review:x#6", json.dumps({"sha": "abc123", "verdict": "pass", "pr": 89}))
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

    def test_review_limit_comments_findings_once(self):
        self.led.upsert_item("x", 5, pr=88, attempts=2)
        with mock.patch.object(self.ctx, "gh", return_value=self.gh):
            ship._review_triggered_fix(self.ctx, "x", self.item(), 88,
                                      self.gh.pr_view(88), "app.py: broken authorization")
            from mahler.failures import backfill
            backfill(self.ctx, [{"name": "x"}])
        self.assertEqual(self.item()["state"], "failed")
        self.assertEqual(len(self.gh.comments), 1)
        self.assertIn("class=review_rejected", self.gh.comments[0])
        self.assertIn("app.py: broken authorization", self.gh.comments[0])
        self.assertIn("mahler/5-x", self.gh.comments[0])

    def test_red_ci_gives_up_after_max_attempts(self):
        self.led.upsert_item("x", 5, pr=88, attempts=2)
        self.gh.rollup = [{"state": "FAILURE"}]
        start = self.patch_start()
        ping = self.ship()
        start.assert_not_called()
        self.assertIn("class=ci_failed", self.gh.comments[-1])
        self.assertIn("CI assertion failed", self.gh.comments[-1])
        self.assertEqual(self.item()["state"], "failed")
        self.assertIsNone(self.led.lease("x", 5))               # the slot is given back
        ping.assert_called_once()
        self.assertIn("Stuck", ping.call_args[0][0])

    def test_deferred_exploration_failures_preserve_retry_and_escalation_budgets(self):
        """DONE is provisional: later CI/review failure is still the explored
        attempt's failure and must leave room for a normal fix at the cap."""
        max_attempts = self.cfg["defaults"]["max_attempts"]
        for trigger, n in (("ci", 5), ("review", 6)):
            with self.subTest(trigger=trigger):
                self.led.upsert_item(
                    "x", n, pr=88, state="verifying", title="Low risk change",
                    branch=f"mahler/{n}-change", sorted_at=iso(NOW),
                    labels='["size:s"]', attempts=max_attempts - 1,
                    esc_tier=1, esc_fails=1)
                self.led.create_run(
                    project="x", number=n, role="build", platform="agy-gemini",
                    size="s", epoch=1, status="ended", outcome="DONE", explore=1,
                    ended_at=iso(NOW))
                view = {"headRefName": f"mahler/{n}-change",
                        "headRefOid": f"failed-{trigger}"}
                with mock.patch.object(ship, "start", return_value=True) as start, \
                        mock.patch.object(self.ctx, "ping"):
                    item = self.led.item("x", n)
                    if trigger == "ci":
                        ship._red_ci(self.ctx, "x", item, 88, view)
                    else:
                        ship._review_triggered_fix(
                            self.ctx, "x", item, 88, view, "blocking finding")
                item = self.led.item("x", n)
                self.assertEqual((item["state"], item["attempts"], item["esc_tier"],
                                  item["esc_fails"]),
                                 ("verifying", max_attempts - 1, 1, 1))
                start.assert_called_once()
                self.assertNotIn("explore", start.call_args.kwargs)

    def test_exploration_attribution_survives_a_failed_fix_launch(self):
        """A fix that never launched (or died on quota) leaves a newer run row
        with explore=0; the failing head still came from the explored DONE
        run, so the next tick must not spend an attempt on it."""
        max_attempts = self.cfg["defaults"]["max_attempts"]
        for trigger, n in (("ci", 7), ("review", 8)):
            with self.subTest(trigger=trigger):
                self.led.upsert_item(
                    "x", n, pr=88, state="verifying", title="Low risk change",
                    branch=f"mahler/{n}-change", sorted_at=iso(NOW),
                    labels='["size:s"]', attempts=max_attempts - 1,
                    esc_tier=1, esc_fails=1)
                self.led.create_run(
                    project="x", number=n, role="build", platform="agy-gemini",
                    size="s", epoch=1, status="ended", outcome="DONE", explore=1,
                    ended_at=iso(NOW))
                for outcome in ("launch failed: boom", "exit 1"):
                    self.led.create_run(
                        project="x", number=n, role="fix", platform="agy-gemini",
                        size="s", epoch=0, status="ended", outcome=outcome, explore=0,
                        ended_at=iso(NOW))
                view = {"headRefName": f"mahler/{n}-change",
                        "headRefOid": f"failed-{trigger}"}
                with mock.patch.object(ship, "start", return_value=True) as start, \
                        mock.patch.object(self.ctx, "ping"):
                    item = self.led.item("x", n)
                    if trigger == "ci":
                        ship._red_ci(self.ctx, "x", item, 88, view)
                    else:
                        ship._review_triggered_fix(
                            self.ctx, "x", item, 88, view, "blocking finding")
                item = self.led.item("x", n)
                self.assertEqual((item["state"], item["attempts"]),
                                 ("verifying", max_attempts - 1))
                start.assert_called_once()

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
        self.assertEqual(info, {"sha": "greensha1", "pr": 88, "run_id": None})

    def test_review_start_delivers_effective_operating_context_in_prompt(self):
        from mahler import prompt
        default = config.DEFAULTS["defaults"]["review_context"]
        for value in (None, "Multiple users upgrade independently.", ""):
            with self.subTest(value=value):
                if value is not None:
                    self.cfg["projects"]["x"]["review_context"] = value
                self.led.set_kv("review:x#5", None)
                with mock.patch.object(ship, "start", return_value=True) as start:
                    ship._start_review_run(self.ctx, "x", self.item(), 88,
                                           self.gh.pr_view(88), self.gh.head_sha)
                context = start.call_args.kwargs["context"]
                rendered = prompt.build(self.ctx, "x", self.item(), "review", "copilot",
                                        {"worktree": "/tmp/review", "branch": "b"},
                                        context=context)
                if value == "":
                    self.assertIn("review_context is empty", rendered)
                    self.assertNotIn(default, rendered)
                else:
                    self.assertIn(default if value is None else value, rendered)
                    if value is not None:
                        self.assertNotIn(default, rendered)
                self.assertIn("Green review round: 1", rendered)
                self.assertIn("Security, normal-flow data loss", rendered)

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

    def test_review_retry_still_excludes_the_builder_not_the_failed_reviewer(self):
        """A prior review did not produce the PR head and cannot replace the
        builder as the platform that DESIGN D11 requires us to exclude."""
        self.led.upsert_item("x", 5, pr=88, labels=json.dumps(["size:m"]))
        build = self.led.create_run(project="x", number=5, role="build",
                                    platform="agy-claude", epoch=1, status="running")
        self.led.update_run(build, status="ended")
        review = self.led.create_run(project="x", number=5, role="review",
                                     platform="agy-gemini", epoch=2, status="running")
        self.led.update_run(review, status="ended")
        calls = []
        self.patch_review_start(calls)
        self.ship()
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(calls[0][1], "agy-claude")

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

    def test_claude_variant_never_pins_its_own_slot_for_review(self):
        """A D33 variant is still the Claude platform that built the PR.

        High-risk review must fall back to another platform rather than pin
        the base Claude slot or deadlock after slot-wide exclusion.
        """
        variant = "claude/claude-sonnet-5/high"
        self.cfg["platforms"][variant] = dict(
            self.cfg["platforms"]["claude"], slot="claude",
            build_model="claude-sonnet-5", effort="high")
        self.cfg["projects"]["x"]["routing_mode"] = "measured"
        self.led.upsert_item("x", 5, pr=88, title="database migration for users",
                             labels="[]")
        rid = self.led.create_run(project="x", number=5, role="build",
                                  platform=variant, epoch=1, status="running")
        self.led.update_run(rid, status="ended")
        calls = []
        self.patch_review_start(calls)

        self.ship()

        self.assertEqual(len(calls), 1)
        _, platform, _ = calls[0]
        self.assertNotEqual(router.platform_slot(self.cfg, platform), "claude")

    def test_review_in_flight_just_waits(self):
        self.led.upsert_item("x", 5, pr=88, labels=json.dumps(["size:m"]))
        self.led.create_run(project="x", number=5, role="review",
                            platform="agy-gemini", epoch=1, status="running")
        calls = []
        self.patch_review_start(calls)
        self.ship()
        self.assertEqual(calls, [])
        self.assertEqual(self.gh.merged, [])

    def reviewed_pr(self):
        self.led.upsert_item("x", 5, pr=88, labels=json.dumps(["size:m"]))
        self.led.set_kv("review:x#5", json.dumps({"sha": self.gh.head_sha, "verdict": "pass", "pr": 88}))

    def test_reviewed_stale_pr_updates_then_waits_for_ci_and_fresh_base(self):
        self.reviewed_pr()
        with mock.patch.object(self.gh, "base_in_head", return_value=False), \
                mock.patch.object(self.gh, "update_reviewed_branch", create=True,
                                  return_value=("updated", True)) as update, \
                mock.patch("mahler.ship.start") as start:
            self.ship()
        self.assertEqual(self.item()["pr"], 88)
        self.assertEqual(self.item()["state"], "verifying")
        self.assertEqual(self.item()["attempts"], 0)
        self.assertEqual(self.gh.merged, [])
        start.assert_not_called()
        self.assertEqual(update.call_args.args[1:4], ("mahler/5-x", "main", "abc123"))
        self.assertEqual(json.loads(self.led.get_kv("review:x#5"))["sha"], "updated")
        self.gh.head_sha = "updated"
        self.gh.rollup = [{"state": "PENDING"}]
        self.ship()
        self.assertEqual(self.gh.merged, [])
        self.gh.rollup = [{"state": "SUCCESS"}]
        with mock.patch.object(self.gh, "base_in_head", return_value=True) as ancestry:
            self.ship()
        ancestry.assert_called_once()
        self.assertEqual(self.gh.merged, [88])

    def test_reviewed_update_changed_patch_requires_review(self):
        self.reviewed_pr()
        with mock.patch.object(self.gh, "base_in_head", return_value=False), \
                mock.patch.object(self.gh, "update_reviewed_branch", create=True,
                                  return_value=("updated", False)):
            self.ship()
        self.gh.head_sha = "updated"
        calls = []
        self.patch_review_start(calls)
        self.ship()
        self.assertEqual(calls[0][0], "review")
        self.assertEqual(self.item()["pr"], 88)
        self.assertEqual(self.gh.merged, [])

    def test_reviewed_conflict_fixes_same_pr_and_reviews_resolved_head(self):
        self.reviewed_pr()
        self.gh.mergeable = "CONFLICTING"
        calls = []
        self.patch_review_start(calls)
        with mock.patch.object(self.gh, "update_reviewed_branch", create=True,
                               return_value=None):
            self.ship()
        self.assertEqual(calls[0][0], "fix")
        self.assertIn("Merge origin/main", calls[0][2])
        self.assertEqual(self.item()["pr"], 88)
        self.assertEqual(self.item()["branch"], "mahler/5-x")
        self.gh.head_sha = "resolved"
        self.gh.mergeable = "MERGEABLE"
        self.led.release("x", 5, holder="run:14")
        self.led.set_state("x", 5, "verifying", "fix completed")
        self.ship()
        self.assertEqual(calls[-1][0], "review")
        self.assertEqual(self.gh.merged, [])

    def test_reviewed_update_error_preserves_pr_and_pass(self):
        self.reviewed_pr()
        with mock.patch.object(self.gh, "base_in_head", return_value=False), \
                mock.patch.object(self.gh, "update_reviewed_branch", create=True,
                                  side_effect=gh_module.GHError("push refused")):
            self.ship()
        self.assertEqual(self.item()["pr"], 88)
        self.assertEqual(self.item()["state"], "verifying")
        self.assertEqual(json.loads(self.led.get_kv("review:x#5"))["sha"], "abc123")
        self.assertEqual(self.gh.merged, [])

    def test_reviewed_conflict_rechecks_head_before_starting_fix(self):
        self.reviewed_pr()
        self.gh.mergeable = "CONFLICTING"
        def changed_head(*args):
            self.gh.head_sha = "new-head"
            return None
        with mock.patch.object(self.gh, "update_reviewed_branch", create=True,
                               side_effect=changed_head), \
                mock.patch("mahler.ship.start") as start:
            self.ship()
        start.assert_not_called()
        self.assertEqual(self.item()["pr"], 88)
        self.assertEqual(self.item()["state"], "verifying")
        self.assertEqual(self.gh.merged, [])

    def test_review_pass_verdict_merges(self):
        self.led.upsert_item("x", 5, pr=88, labels=json.dumps(["size:m"]))
        self.led.set_kv("review:x#5", json.dumps({"sha": self.gh.head_sha, "verdict": "pass", "pr": 88}))
        self.ship()
        self.assertEqual(self.gh.merged, [88])

    def test_manual_reship_review_budget_is_fresh_once_and_still_bounded(self):
        self.led.upsert_item("x", 5, state="failed", pr=88, attempts=6,
                             labels='["size:m"]')
        key = "reviewfix:x#5:88:abc123"
        self.led.set_kv(key, iso(NOW))
        self.led.set_kv(key + ":charged", "1")
        args = SimpleNamespace(item=("x", 5), pr=88, branch=None, summary=None, holder="me")
        with mock.patch.object(cli, "project_client", return_value=self.gh):
            self.assertEqual(cli.cmd_ship(args, self.cfg, self.led), 0)
        with mock.patch.object(ship, "start", return_value=True) as start, \
                mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(self.ctx, "ping"):
            for _ in range(2):
                ship._review_triggered_fix(self.ctx, "x", self.item(), 88,
                                           self.gh.pr_view(88), "blocking finding")
                self.assertEqual(self.item()["attempts"], 1)
            self.assertEqual(start.call_count, 2)
            # A new failing head consumes the next attempt; the third exhausts it.
            for attempt in (2, 3):
                self.gh.head_sha = f"head-{attempt}"
                ship._review_triggered_fix(self.ctx, "x", self.item(), 88,
                                           self.gh.pr_view(88), "blocking finding")
                self.assertEqual(self.item()["attempts"], attempt)
            self.assertEqual(start.call_count, 3)
        self.assertEqual(self.item()["state"], "failed")
        self.assertIn("attempt 3 of 3", self.gh.comments[-1])
        self.assertIn("cycle=" + self.led.get_kv("attempt_cycle:x#5"), self.gh.comments[-1])

    def test_review_fail_verdict_starts_a_fix_round_with_findings(self):
        self.led.upsert_item("x", 5, pr=88, labels=json.dumps(["size:m"]))
        self.led.set_kv("review:x#5", json.dumps({
            "sha": self.gh.head_sha, "verdict": "fail",
            "findings": "auth.py: missing null check on session token", "pr": 88}))
        calls = []
        self.patch_review_start(calls)
        self.ship()
        self.assertEqual(self.gh.merged, [])
        self.assertEqual(len(calls), 1)
        role, platform, context = calls[0]
        self.assertEqual(role, "fix")
        self.assertIn("auth.py: missing null check on session token", context)
        self.assertEqual(self.item()["attempts"], 1)

    def test_review_fix_trigger_links_verdict_and_distinguishes_cases(self):
        # 1. Successful launch records review_fix_trigger with exact verdict and fix ID
        self.led.upsert_item("x", 5, pr=88, labels=json.dumps(["size:m"]))
        rev1 = self.led.create_run(project="x", number=5, role="review",
                                   platform="agy-claude", epoch=1, status="ended",
                                   outcome="REVIEW-FAIL")
        self.led.set_kv("review:x#5", json.dumps({
            "pr": 88, "sha": "head-sha-1", "run_id": rev1, "verdict": "fail",
            "findings": "blocking finding 1"}))
        view = {"headRefOid": "head-sha-1", "headRefName": "feat", "baseRefName": "main",
                "statusCheckRollup": [{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}]}

        fix_runs_created = []
        def fake_start(ctx, project, item, role, platform, **kwargs):
            frid = self.led.create_run(project=project, number=item["number"],
                                       role=role, platform=platform, epoch=1,
                                       status="running")
            fix_runs_created.append(frid)
            return True

        with mock.patch.object(ship, "start", side_effect=fake_start), \
                mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(self.ctx, "ping"):
            ship._review_triggered_fix(self.ctx, "x", self.item(), 88, view, "blocking finding 1")

        triggers = [dict(r) for r in self.led.q("SELECT * FROM events WHERE kind='review_fix_trigger'")]
        self.assertEqual(len(triggers), 1)
        t1 = json.loads(triggers[0]["detail"])
        self.assertEqual(t1["fix_run"], fix_runs_created[0])
        self.assertEqual(t1["review_run"], rev1)
        self.assertEqual(t1["reviewed_sha"], "head-sha-1")
        self.assertEqual(t1["pr"], 88)

        # 2. Retry with same fix run does not duplicate link
        with mock.patch.object(ship, "start", return_value=True), \
                mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(self.ctx, "ping"):
            ship._review_triggered_fix(self.ctx, "x", self.item(), 88, view, "blocking finding 1")

        triggers_after_retry = self.led.q("SELECT * FROM events WHERE kind='review_fix_trigger'")
        self.assertEqual(len(triggers_after_retry), 1)

        # 3. Failed launch does not record review_fix_trigger
        with mock.patch.object(ship, "start", return_value=False), \
                mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(self.ctx, "ping"):
            ship._review_triggered_fix(self.ctx, "x", self.item(), 88, view, "blocking finding 1")

        triggers_after_fail = self.led.q("SELECT * FROM events WHERE kind='review_fix_trigger'")
        self.assertEqual(len(triggers_after_fail), 1)

        # 4. Same-SHA different reviewer links to that reviewer's run ID
        rev2 = self.led.create_run(project="x", number=5, role="review",
                                   platform="claude", epoch=1, status="ended",
                                   outcome="REVIEW-FAIL")
        self.led.set_kv("review:x#5", json.dumps({
            "pr": 88, "sha": "head-sha-1", "run_id": rev2, "verdict": "fail",
            "findings": "blocking finding 2"}))
        self.led.set_kv("reviewdup:x#5", None)
        with mock.patch.object(ship, "start", side_effect=fake_start), \
                mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(self.ctx, "ping"):
            ship._review_triggered_fix(self.ctx, "x", self.item(), 88, view, "blocking finding 2")

        triggers_after_rev2 = [dict(r) for r in self.led.q("SELECT * FROM events WHERE kind='review_fix_trigger' ORDER BY id")]
        self.assertEqual(len(triggers_after_rev2), 2)
        t2 = json.loads(triggers_after_rev2[1]["detail"])
        self.assertEqual(t2["fix_run"], fix_runs_created[1])
        self.assertEqual(t2["review_run"], rev2)
        self.assertEqual(t2["reviewed_sha"], "head-sha-1")

        # 5. Base-conflict fix does not record review_fix_trigger
        with mock.patch.object(ship, "start", side_effect=fake_start), \
                mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(self.ctx, "ping"):
            ship._review_triggered_fix(self.ctx, "x", self.item(), 88, view, "conflict", base_conflict=True)

        triggers_after_conflict = self.led.q("SELECT * FROM events WHERE kind='review_fix_trigger'")
        self.assertEqual(len(triggers_after_conflict), 2)

        # 6. CI fix does not record review_fix_trigger
        ci_view = {"headRefOid": "head-sha-1", "headRefName": "feat", "baseRefName": "main",
                   "statusCheckRollup": [{"name": "ci", "status": "COMPLETED", "conclusion": "FAILURE"}]}
        with mock.patch.object(ship, "start", side_effect=fake_start), \
                mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(self.ctx, "ping"):
            ship._red_ci(self.ctx, "x", self.item(), 88, ci_view)

        triggers_after_ci = self.led.q("SELECT * FROM events WHERE kind='review_fix_trigger'")
        self.assertEqual(len(triggers_after_ci), 2)

    def same_head_setup(self):
        self.led.upsert_item("x", 5, pr=88, labels=json.dumps(["size:m"]))
        self.cfg["routing"]["review"] = ["agy-claude", "claude", "agy-gemini"]
        rid = self.led.create_run(project="x", number=5, role="review",
                                  platform="agy-claude", epoch=1)
        self.led.update_run(rid, status="ended", outcome="REVIEW-FAIL")
        self.led.set_kv("review:x#5", json.dumps({
            "pr": 88, "sha": "abc123", "run_id": rid, "verdict": "fail",
            "findings": "app.py:1: missing evidence"}))
        self.same_head_calls = []

        def start(ctx, project, item, role, platform, **kwargs):
            self.same_head_calls.append((role, platform, kwargs))
            rid = self.led.create_run(project=project, number=item["number"],
                                      role=role, platform=platform, epoch=1)
            # Fix routing is immaterial here; explicitly model an independent fixer.
            self.led.update_run(rid, started_at=iso(self.led.now() + timedelta(seconds=1)),
                                **({"platform": "agy-gemini"} if role == "fix" else {}))
            return True

        patcher = mock.patch.object(ship, "start", side_effect=start)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.same_head_gate()
        self.assertEqual(self.same_head_calls[-1][0], "fix")
        return self.led.last_run("x", 5, roles=("fix",))["id"]

    def same_head_gate(self):
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(self.ctx, "ping"):
            ship._review_gate(self.ctx, "x", self.item(), self.item()["pr"],
                              self.gh.pr_view(self.item()["pr"]))

    def same_head_counters(self):
        return tuple(self.item()[k] for k in ("attempts", "esc_fails", "esc_tier"))

    def same_head_verdict(self, verdict):
        info = json.loads(self.led.get_kv("review:x#5"))
        self.led.update_run(info["run_id"], status="ended", outcome="REVIEW-" + verdict.upper())
        info.update(verdict=verdict, findings="app.py:1: missing evidence")
        self.led.set_kv("review:x#5", json.dumps(info))

    def test_same_head_done_reviews_once_then_passes_normal_merge_gate(self):
        from mahler import review
        rid = self.same_head_setup()
        before = self.same_head_counters()
        self.led.update_run(rid, status="ended", outcome="DONE")
        self.same_head_gate()
        self.assertEqual(self.same_head_calls[-1][:2], ("review", "claude"))
        self.assertEqual(self.same_head_counters(), before)
        self.assertEqual(self.gh.merged, [])
        self.assertIn("missing evidence", self.same_head_calls[-1][2]["context"])
        self.same_head_gate()
        self.assertEqual(len(self.same_head_calls), 2)
        self.same_head_verdict("pass")
        self.same_head_gate()
        self.assertEqual(self.gh.merged, [88])
        self.assertEqual(len(review.window(self.led, "x", 5, 88)), 1)

    def test_same_head_second_done_is_charged_and_exhausts_budget(self):
        rid = self.same_head_setup()
        self.led.update_run(rid, status="ended", outcome="DONE")
        self.same_head_gate()
        self.same_head_verdict("fail")
        # Even a would-be repeat-finding second opinion cannot bypass the allowance.
        with mock.patch.object(ship, "_repeat_finding", return_value={"platform": "claude"}) as repeat:
            self.same_head_gate()
            repeat.assert_not_called()
        self.assertEqual(self.same_head_calls[-1][0], "fix")
        self.assertEqual(self.item()["attempts"], 2)
        rid = self.led.last_run("x", 5, roles=("fix",))["id"]
        self.led.update_run(rid, status="ended", outcome="DONE")
        self.same_head_gate()
        self.assertEqual(self.item()["state"], "failed")
        self.assertEqual(self.item()["attempts"], 3)
        self.assertEqual([c[0] for c in self.same_head_calls], ["fix", "review", "fix"])

    def test_same_head_done_at_last_attempt_still_gets_review(self):
        rid = self.same_head_setup()
        self.led.upsert_item("x", 5, attempts=2, esc_fails=1, esc_tier=2)
        before = self.same_head_counters()
        self.led.update_run(rid, status="ended", outcome="DONE")
        self.same_head_gate()
        self.assertEqual(self.same_head_calls[-1][0], "review")
        self.assertEqual(self.same_head_counters(), before)
        self.same_head_verdict("fail")
        self.same_head_gate()
        self.assertEqual(self.item()["state"], "failed")
        self.assertEqual(self.item()["attempts"], 3)

    def test_same_head_pending_survives_restart_capacity_and_failed_launch(self):
        rid = self.same_head_setup()
        self.led.update_run(rid, status="ended", outcome="DONE")
        before = self.same_head_counters()
        self.cfg["concurrency"]["total"] = 0
        self.same_head_gate()
        saved = self.led.get_kv("reviewdone:x#5:88:abc123")
        self.assertEqual(json.loads(saved)["fix_run"], rid)
        self.assertIsNone(self.led.get_kv("review:x#5"))
        # Reopen the persisted database, as a new conductor process would.
        path = os.path.join(self.tmp, "restarted.db")
        connection = sqlite3.connect(path)
        try:
            self.led.con.backup(connection)
        finally:
            connection.close()
        self.led = Ledger(path, clock=lambda: NOW)
        self.addCleanup(self.led.close)
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
        self.same_head_gate()
        self.cfg["concurrency"]["total"] = 4
        with mock.patch.object(ship, "start", return_value=False):
            self.same_head_gate()
        self.assertEqual(self.same_head_counters(), before)
        self.assertEqual(self.led.get_kv("reviewdone:x#5:88:abc123"), saved)
        self.same_head_gate()
        self.same_head_gate()
        self.assertEqual([c[0] for c in self.same_head_calls], ["fix", "review"])
        self.assertEqual(self.same_head_counters(), before)

    def test_same_head_replays_invalidation_after_interrupted_tick(self):
        rid = self.same_head_setup()
        self.led.update_run(rid, status="ended", outcome="DONE")
        original = self.led.set_kv

        def interrupted(key, value):
            if key == "review:x#5" and value is None:
                raise RuntimeError("interrupted")
            original(key, value)

        with mock.patch.object(self.led, "set_kv", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.same_head_gate()
        self.same_head_gate()
        self.assertEqual(self.same_head_calls[-1][0], "review")
        self.assertEqual(self.item()["attempts"], 1)

    def test_same_head_reviewer_fallback_keeps_fixer_slot_excluded(self):
        rid = self.same_head_setup()
        self.led.update_run(rid, status="ended", outcome="DONE")
        self.cfg["platforms"]["fixer-variant"] = {
            **self.cfg["platforms"]["agy-gemini"], "slot": "agy-gemini"}
        self.cfg["routing"]["review"] = ["fixer-variant", "agy-gemini", "agy-claude"]
        self.same_head_gate()
        self.assertEqual(self.same_head_calls[-1][:2], ("review", "agy-claude"))

    def test_same_head_eligibility_requires_exact_completed_review_fix(self):
        rid = self.same_head_setup()
        info = json.loads(self.led.get_kv("review:x#5"))
        for outcome, stop, status in (("BLOCKED failed", None, "ended"),
                ("DONE", "quota", "ended"), ("DONE", "preempted", "ended"),
                ("launch failed: no process", None, "ended"),
                ("not claimed", None, "ended"), ("DONE", None, "running")):
            with self.subTest(outcome=outcome, stop=stop, status=status):
                self.led.update_run(rid, status=status, outcome=outcome, stop_reason=stop)
                self.assertFalse(ship._unchanged_done_review(
                    self.ctx, "x", self.item(), 88, "abc123", info))
        self.led.update_run(rid, status="ended", outcome="DONE", stop_reason=None)
        self.led.set_kv("reviewfix:x#5:88:abc123:run", json.dumps({
            "run_id": rid - 1, "verdict": info}))
        self.assertFalse(ship._unchanged_done_review(
            self.ctx, "x", self.item(), 88, "abc123", info))
        self.assertIsNone(self.led.get_kv("reviewdone:x#5:88:abc123"))

    def test_same_head_allowance_is_scoped_to_pr_head_and_survives_reship(self):
        rid = self.same_head_setup()
        self.led.update_run(rid, status="ended", outcome="DONE")
        self.same_head_gate()
        self.same_head_verdict("fail")
        info = json.loads(self.led.get_kv("review:x#5"))
        self.assertFalse(ship._unchanged_done_review(
            self.ctx, "x", self.item(), 88, "abc123", info))
        for pr, sha in ((89, "abc123"), (88, "new-head")):
            with self.subTest(pr=pr, sha=sha):
                key = f"reviewfix:x#5:{pr}:{sha}"
                self.led.set_kv(key, iso(NOW))
                self.led.set_kv(key + ":run", json.dumps({"run_id": rid, "verdict": info}))
                self.assertTrue(ship._unchanged_done_review(
                    self.ctx, "x", self.item(), pr, sha, info))
        # A manual re-ship resets attempt accounting, not the per-head allowance.
        args = SimpleNamespace(item=("x", 5), pr=88, branch=None, holder="test", summary=None)
        with mock.patch.object(cli, "project_client", return_value=self.gh):
            self.assertEqual(cli.cmd_ship(args, self.cfg, self.led), 0)
        self.assertFalse(ship._unchanged_done_review(
            self.ctx, "x", self.item(), 88, "abc123", info))

    def test_fix_announcements_identify_the_trigger(self):
        """Exercise real start() announcements through both shipping paths."""
        for trigger in ("ci", "review"):
            with self.subTest(trigger=trigger):
                self.led.upsert_item("x", 5, state="verifying", pr=88,
                                     labels='["size:m"]', attempts=0)
                self.gh.rollup = [{"state": "FAILURE" if trigger == "ci" else "SUCCESS"}]
                self.led.set_kv("review:x#5", json.dumps({
                    "sha": self.gh.head_sha, "verdict": "fail",
                    "findings": "missing null check", "pr": 88}))
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
        self.led.set_kv("review:x#5", json.dumps({"sha": "oldsha", "verdict": "pass", "pr": 88}))
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

    def test_pr_review_ignores_old_no_change_claim(self):
        self.led.set_kv("no_change:x#5", json.dumps({"phase": "rejected", "mode": "claim"}))
        self.test_review_pass_posts_a_comment_and_records_the_verdict()
        self.assertEqual(json.loads(self.led.get_kv("no_change:x#5"))["phase"], "rejected")

    def test_review_pass_lists_non_blocking_notes(self):
        run = self.review_run()
        with open(self.log, "w") as fh:
            fh.write("STATUS: REVIEW-PASS a.py: odd filenames miss | b.py: add a test\n")
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(self.ctx, "ping"), mock.patch.object(self.ctx, "say"), \
                mock.patch.object(runner, "remove_worktree"):
            finalize.finalize(self.ctx, run)
        self.assertEqual(json.loads(self.led.get_kv("review:x#5"))["verdict"], "pass")
        body = self.gh.comments[-1]
        self.assertIn("no blocking issues", body)
        self.assertIn("- a.py: odd filenames miss", body)
        self.assertIn("- b.py: add a test", body)

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
            "platform": run["platform"], "pr": 88,
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

    def test_unknown_refresh_dispatches_normal_states(self):
        for result in ("CONFLICTING", "MERGEABLE", "pending", "red", "CLOSED", "MERGED"):
            with self.subTest(result=result):
                self.led.set_state("x", 5, "verifying", pr=88)
                self.gh.view_state = "OPEN"
                self.gh.mergeable = "UNKNOWN"
                self.gh.rollup = [{"state": "SUCCESS"}]
                self.led.set_kv("mergeability:x#5:88", "")
                self.ship()
                self.ship()
                def refresh(_):
                    if result in ("pending", "red"):
                        self.gh.rollup = [{"state": "PENDING" if result == "pending" else "FAILURE"}]
                    elif result in ("CLOSED", "MERGED"):
                        self.gh.view_state = result
                    else:
                        self.gh.mergeable = result
                with mock.patch.object(self.gh, "pr_refresh_mergeability", side_effect=refresh) as force, \
                        mock.patch.object(ship, "_red_ci") as red:
                    self.ship()
                force.assert_called_once_with(88)
                if result == "CONFLICTING":
                    self.assertEqual(self.item()["state"], "ready")
                    self.assertEqual(self.item()["attempts"], 0)
                elif result in ("MERGEABLE", "MERGED"):
                    self.assertEqual(self.item()["state"], "shipped")
                elif result == "CLOSED":
                    self.assertEqual(self.item()["state"], "ready")
                else:
                    self.assertEqual(self.item()["state"], "verifying")
                    self.assertEqual(red.called, result == "red")

    def test_refresh_uses_changed_target_for_conflict_and_keeps_merge_guards(self):
        self.led.upsert_item("x", 5, pr=88, attempts=2)
        self.gh.mergeable = "UNKNOWN"
        self.ship()
        self.ship()
        original = self.gh.pr_view(88)
        fresh = dict(original, mergeable="CONFLICTING", baseRefName="release", headRefOid="new")
        with mock.patch.object(self.gh, "pr_view", side_effect=[original, fresh]):
            self.ship()
        self.assertEqual(self.item()["attempts"], 2)
        self.assertIn("conflicts with release", self.last_event())
        self.assertFalse(self.led.get_kv("mergeability:x#5:88"))
        self.led.set_state("x", 5, "verifying", pr=88)
        self.ship()
        self.ship()
        fresh = dict(original, mergeable="MERGEABLE")
        changed = dict(fresh, headRefOid="changed-again")
        with mock.patch.object(self.gh, "pr_view", side_effect=[original, fresh, changed]), \
                mock.patch.object(self.gh, "base_in_head") as ancestry:
            self.ship()
        ancestry.assert_not_called()
        self.assertEqual(self.gh.merged, [])

    def test_refresh_reread_failure_waits_safely(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.mergeable = "UNKNOWN"
        self.ship()
        self.ship()
        original = self.gh.pr_view(88)
        with mock.patch.object(self.gh, "pr_view", side_effect=[original, gh_module.GHError("reread")]):
            self.ship()
        self.assertEqual(self.item()["state"], "verifying")
        self.assertIn("reread", self.led.get_kv("mergeability:x#5:88"))
        self.assertEqual(self.gh.merged, [])

    def test_unknown_refresh_backoff_survives_context_restart(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.mergeable = "UNKNOWN"
        for rollup in ([{"state": "SUCCESS"}], []):
            with self.subTest(rollup=rollup):
                self.gh.rollup = rollup
                self.led.set_kv("mergeability:x#5:88", "")
                with mock.patch.object(self.gh, "pr_refresh_mergeability") as force, \
                        mock.patch.object(self.ctx, "say") as say:
                    for tick in range(10):
                        self.ship()
                        self.assertEqual(force.call_count, min(max(tick - 1, 0), 3))
                    self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
                    with mock.patch.object(self.led, "now", return_value=NOW + timedelta(days=1)):
                        self.ship()
                        self.ship()
                    self.assertEqual(force.call_count, 4)
                    self.assertNotIn("CI still running", str(say.call_args_list))
                self.assertEqual(self.item()["state"], "verifying")
                self.assertEqual(self.gh.merged, [])
                ci = json.loads(self.led.get_kv("ci:x#5:88"))
                self.assertEqual(ci["state"], "green" if rollup else "none")

    def test_unknown_changed_identity_restarts_observations(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.mergeable = "UNKNOWN"
        with mock.patch.object(self.gh, "pr_refresh_mergeability") as force:
            self.ship()
            self.ship()
            self.gh.head_sha = "new"
            self.ship()
            force.assert_not_called()
            view = dict(self.gh.pr_view(88), baseRefName="release")
            with mock.patch.object(self.gh, "pr_view", return_value=view):
                self.ship()
                self.ship()
                force.assert_not_called()
                self.ship()
                force.assert_called_once()
        self.assertEqual(self.gh.merged, [])

    def test_unknown_refresh_error_is_rate_limited_and_other_items_continue(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.mergeable = "UNKNOWN"
        self.ship()
        self.ship()
        self.led.upsert_item("x", 6, state="verifying", pr=89)
        with mock.patch.object(self.gh, "pr_refresh_mergeability", side_effect=gh_module.GHError("offline")) as force:
            for _ in range(4):
                self.ship()
            self.assertEqual(force.call_count, 2)  # one attempt per item
            with mock.patch.object(self.led, "now", return_value=NOW + timedelta(minutes=5)):
                self.ship()
            self.assertEqual(force.call_count, 4)
        self.assertIn("offline", self.led.get_kv("mergeability:x#5:88"))
        self.assertEqual(self.item(6)["state"], "verifying")

    def test_unknown_refresh_rechecks_session_lease(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.mergeable = "UNKNOWN"
        self.ship()
        self.ship()
        def refresh(_):
            self.gh.mergeable = "CONFLICTING"
            self.led.claim("x", 5, "interactive:mike", "interactive", 30)
        with mock.patch.object(self.gh, "pr_refresh_mergeability", side_effect=refresh):
            self.ship()
        self.assertEqual(self.item()["state"], "working")
        self.assertEqual(self.item()["pr"], 88)
        self.assertEqual(self.gh.merged, [])

    def test_unknown_pending_checks_still_timeout(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.mergeable = "UNKNOWN"
        self.gh.rollup = [{"state": "PENDING"}]
        self.ship()
        with mock.patch.object(self.led, "now", return_value=NOW + timedelta(days=1)):
            self.ship()
        self.assertEqual(self.item()["state"], "needs_you")
        self.assertIn("CI still running", self.item()["question"])

    def test_capacity_recovery_resets_waits_before_real_shipping(self):
        for role in ("fix", "review"):
            for condition in ("pending", "UNKNOWN", "queued"):
                with self.subTest(role=role, condition=condition):
                    self.led.set_state("x", 5, "needs_you", pr=88, question="capacity")
                    self.led.set_kv("capacity-stranded:x#5", json.dumps({
                        "role": role, "pr": 88, "at": self.item()["state_changed_at"]}))
                    old = json.dumps({"sha": "abc123", "since": iso(NOW - timedelta(days=1)),
                                      "state": "pending"})
                    for prefix in ("ci", "mergeability", "queue"):
                        self.led.set_kv(f"{prefix}:x#5:88", old)
                        self.led.set_kv(f"{prefix}:x#6:89", old)
                    self.led.set_kv("review:x#5", json.dumps({"sha": "abc123", "verdict": "pass", "pr": 88}))
                    self.led.set_kv("fix:x#5:88", "evidence")
                    self.gh.mergeable = "UNKNOWN" if condition == "UNKNOWN" else "MERGEABLE"
                    self.gh.rollup = [{"state": "PENDING" if condition == "pending" else "SUCCESS"}]
                    self.ship()
                    self.assertEqual(self.item()["state"], "verifying")
                    self.assertEqual(self.gh.merged, [])
                    queue = json.loads(self.led.get_kv("queue:x#5:88"))
                    self.assertEqual(queue["sha"], "abc123")
                    self.assertEqual(queue["since"], iso(NOW))
                    self.assertEqual(self.led.get_kv("fix:x#5:88"), "evidence")
                    self.assertEqual(json.loads(self.led.get_kv("review:x#5"))["verdict"], "pass")
                    for prefix in ("ci", "mergeability", "queue"):
                        self.assertEqual(self.led.get_kv(f"{prefix}:x#6:89"), old)

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
        self.assertEqual(self.item()["state"], "shipped")

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

    def test_lookup_failure_has_separate_verification_deadline(self):
        self.led.upsert_item("x", 5, pr=88)
        with mock.patch.object(self.gh, "base_in_head", return_value=None):
            self.ship()
        self.led.now = lambda: NOW + timedelta(minutes=90)
        self.gh.fail_view = {88}
        self.ship()
        self.assertEqual(self.item()["state"], "verifying")
        self.led.now = lambda: NOW + timedelta(minutes=180)
        self.ship()
        self.assertEqual(self.item()["state"], "needs_you")
        self.assertEqual(self.gh.merged, [])

    def test_ci_pending_timeout_reports_launch_failure_not_transient_error(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.rollup = [{"state": "PENDING"}]
        self.ship()
        for minute in (10, 40):
            self.led.now = lambda m=minute: NOW + timedelta(minutes=m)
            self.led.create_run(project="x", number=5, role="fix", platform="claude",
                                epoch=1, status="ended", ended_at=iso(self.led.now()),
                                outcome="launch failed: branch held at /interactive/worktree")
        self.led.now = lambda: NOW + timedelta(minutes=90)
        with mock.patch.object(self.gh, "pr_view", side_effect=gh_module.GHError("TLS timeout")):
            self.ship()
            self.assertEqual(self.item()["state"], "verifying")
            self.led.now = lambda: NOW + timedelta(minutes=180)
            self.ship()
        self.assertIn("branch held at /interactive/worktree", self.item()["question"])
        self.assertNotIn("TLS", self.item()["question"])

    def test_successful_lookup_clears_lookup_failure_timer(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.rollup = [{"state": "PENDING"}]
        self.gh.fail_view = {88}
        self.ship()
        self.assertIsNotNone(self.led.get_kv("pr-lookup:x#5:88"))
        self.gh.fail_view = set()
        self.ship()
        self.assertIsNone(self.led.get_kv("pr-lookup:x#5:88"))

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
        self.assertEqual(self.item()["state"], "shipped")
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
        self.assertEqual(self.item()["state"], "shipped")

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

    def test_pr_merged_outside_mahler_is_shipped(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.view_state = "MERGED"
        ping = self.ship()
        self.assertEqual(self.gh.merged, [])
        self.assertEqual(self.item()["state"], "shipped")
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
        self.assertEqual(self.item(6)["state"], "shipped")         # 6: still shipped
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
        self.assertEqual(self.item()["state"], "shipped")
        self.assertIn("unowned-PR check failed", " ".join(self.ctx.lines))


class TestReviewConvergence(unittest.TestCase):
    setUp = ShipTests.setUp
    item = ShipTests.item
    ship = ShipTests.ship

    def rounds(self, findings):
        self.led.upsert_item("x", 5, state="verifying", pr=88, attempts=0,
                             labels='["size:m"]')
        history = [{"sha": f"head-{i}", "findings": finding,
                    "at": iso(NOW), "run_id": i}
                   for i, finding in enumerate(findings, 1)]
        self.gh.head_sha = history[-1]["sha"]
        self.led.set_kv("reviewfindings:x#5", json.dumps(history))
        self.led.set_kv("review:x#5", json.dumps({
            "sha": self.gh.head_sha, "verdict": "fail", "findings": findings[-1], "pr": 88}))
        return history

    def test_two_divergent_transitions_keep_blockers_in_bounded_fix_flow(self):
        findings = ["auth.py: null check", "db.py: query", "api.py: unsafe input"]
        self.rounds(findings)
        with mock.patch.object(ship, "start", return_value=True) as start:
            self.ship()
        self.assertEqual(start.call_args.args[3], "fix")
        self.assertEqual(self.item()["state"], "verifying")
        self.assertIsNotNone(self.led.get_kv("reviewdrift:x#5:88"))
        self.assertFalse(self.gh.merged)

    def test_one_divergence_then_overlap_or_repeat_starts_a_fix(self):
        sequences = [
            ["a.py: bug", "b.py: bug"],
            ["a.py: bug", "b.py: bug", "b.py: remaining bug"],
            ["a.py: bug"] * 5,
            ["a.py: bug | b.py: bug", "c.py: bug", "b.py: recurring bug"],
            ["a.py: bug", "unknown location", "c.py: bug"],
            ["a.py: bug", "b.py: bug", "b.py: bug", "c.py: bug"],
        ]
        for findings in sequences:
            with self.subTest(findings=findings):
                self.rounds(findings)
                with mock.patch.object(ship, "start", return_value=True) as start:
                    self.ship()
                self.assertEqual(self.item()["state"], "verifying")
                self.assertEqual(start.call_args.args[3], "fix")

    def test_repeated_extensionless_location_starts_a_fix(self):
        self.rounds([
            f"Dockerfile: missing runtime dependency | {name}.py: bug"
            for name in ("a", "b", "c")
        ])
        with mock.patch.object(ship, "start", return_value=True) as start:
            self.ship()
        self.assertEqual(self.item()["state"], "verifying")
        start.assert_called_once()
        self.assertEqual(start.call_args.args[3], "fix")

    def test_repeated_dotfile_location_starts_a_fix(self):
        self.assertEqual(
            ship._finding_files(".gitignore: blocker | config/.env.local: leak"),
            {".gitignore", "config/.env.local"})
        self.rounds([
            f".gitignore: same blocker | {name}.py: bug"
            for name in ("a", "b", "c")
        ])
        with mock.patch.object(ship, "start", return_value=True) as start:
            self.ship()
        self.assertEqual(self.item()["state"], "verifying")
        start.assert_called_once()
        self.assertEqual(start.call_args.args[3], "fix")

    def test_extensionless_locations_preserve_paths_and_ignore_prose(self):
        self.assertEqual(ship._finding_files(
            "Dockerfile: missing dependency | - `build/Makefile`:10 broken target"
            " | * deploy/Makefile:20 missing target using os.path"
            " | explanation mentions another/Dockerfile"),
            {"Dockerfile", "build/Makefile", "deploy/Makefile"})

    def test_full_paths_and_compound_filenames_remain_distinct(self):
        findings = ["src/auth/index.test.js:10 null check",
                    "src/db/index.test.ts:20 query",
                    "src/api/index.test.tsx:30 unsafe input"]
        self.assertEqual(ship._finding_files(" | ".join(findings)), {
            "src/auth/index.test.js", "src/db/index.test.ts", "src/api/index.test.tsx"})
        self.rounds(findings)
        with mock.patch.object(ship, "start") as start:
            self.ship()
        self.assertEqual(start.call_args.args[3], "fix")
        self.assertIsNotNone(self.led.get_kv("reviewdrift:x#5:88"))

    def test_shared_dotted_expression_does_not_mask_divergent_locations(self):
        findings = ["auth.py:10 missing validation before json.loads",
                    "db.py:20 transaction leaks when json.loads raises",
                    "api.py:30 unbounded input passed to json.loads"]
        self.assertEqual(ship._finding_files(" | ".join(findings)),
                         {"auth.py", "db.py", "api.py"})
        self.rounds(findings)
        with mock.patch.object(ship, "start") as start:
            self.ship()
        self.assertEqual(start.call_args.args[3], "fix")
        self.assertIsNotNone(self.led.get_kv("reviewdrift:x#5:88"))

    def test_retry_does_not_reescalate_the_same_history(self):
        history = self.rounds(["a.py: bug", "b.py: bug", "c.py: bug"])
        with mock.patch.object(ship, "start", return_value=False):
            self.ship()
        self.led.upsert_item("x", 5, state="verifying")
        with mock.patch.object(ship, "start", return_value=True) as start:
            self.ship()
        start.assert_called_once()
        self.assertEqual(json.loads(self.led.get_kv("reviewfindings:x#5")), history)

    def test_pass_breaks_streak_and_failed_finalization_is_idempotent(self):
        from types import SimpleNamespace

        history = self.rounds(["a.py: bug", "b.py: bug"])
        self.led.set_kv("review:x#5", json.dumps({"sha": "head-3", "verdict": "pending"}))
        ending = SimpleNamespace(
            led=self.led, project="x", number=5, item=self.item(),
            run={"id": 20, "platform": "agy-gemini"}, rest="c.py: bug",
            ctx=self.ctx, set_state=mock.Mock())
        with mock.patch.object(self.ctx, "gh", return_value=self.gh):
            finalize._review_failed(ending)
            finalize._review_failed(ending)
            recorded = json.loads(self.led.get_kv("reviewfindings:x#5"))
            self.assertEqual(recorded, history + [{
                "sha": "head-3", "findings": ending.rest,
                "at": iso(NOW), "run_id": 20, "platform": "agy-gemini", "pr": 88}])
            finalize._review_passed(ending)
        self.assertEqual(self.led.get_kv("reviewconvergence:x#5"), "3")


class TestRepeatReviewFinding(unittest.TestCase):
    setUp = ShipTests.setUp
    item = ShipTests.item
    ship = ShipTests.ship
    patch_review_start = ShipTests.patch_review_start

    FINDING = "mahler/ghapp.py:52 sends the literal ****** instead of the JWT"

    def failed_rounds(self, findings, reviewers=("copilot", "copilot")):
        self.led.upsert_item("x", 5, state="verifying", pr=88, attempts=0,
                             labels='["size:m"]')
        build = self.led.create_run(project="x", number=5, role="build",
                                    platform="agy-claude", epoch=1, status="running")
        self.led.update_run(build, status="ended")
        history = [{"sha": f"head-{i}", "findings": f, "at": iso(NOW), "run_id": i,
                    "platform": r}
                   for i, (f, r) in enumerate(zip(findings, reviewers), 1)]
        self.gh.head_sha = history[-1]["sha"]
        self.led.set_kv("reviewfindings:x#5", json.dumps(history))
        self.led.set_kv("review:x#5", json.dumps({
            "sha": self.gh.head_sha, "verdict": "fail", "findings": findings[-1], "pr": 88}))

    def test_repeat_on_unchanged_line_asks_another_platform_without_a_fix(self):
        self.failed_rounds([self.FINDING, self.FINDING],
                           reviewers=("copilot", "agy-gemini"))
        calls = []
        self.patch_review_start(calls)
        ping = self.ship()
        self.assertEqual(calls, [])
        ping.assert_not_called()
        self.assertEqual(self.item()["attempts"], 0)
        self.assertEqual(self.item()["esc_fails"], 0)
        self.assertIsNone(self.led.get_kv("review:x#5"))
        self.ship()
        self.assertEqual([c[0] for c in calls], ["review"])
        self.assertNotIn(calls[0][1], ("agy-gemini", "agy-claude"))

    def test_alternate_reviewer_confirming_the_finding_starts_the_fix(self):
        self.failed_rounds([self.FINDING, self.FINDING])
        self.patch_review_start([])
        self.ship()
        history = json.loads(self.led.get_kv("reviewfindings:x#5"))
        history.append({"sha": self.gh.head_sha, "findings": self.FINDING, "at": iso(NOW),
                        "run_id": 3, "platform": "agy-gemini"})
        self.led.set_kv("reviewfindings:x#5", json.dumps(history))
        self.led.set_kv("review:x#5", json.dumps({
            "sha": self.gh.head_sha, "verdict": "fail", "findings": self.FINDING, "pr": 88}))
        for run in self.led.active_runs():
            self.led.update_run(run["id"], status="ended")
        self.led.release("x", 5)
        calls = []
        self.patch_review_start(calls)
        ping = self.ship()
        self.assertEqual([c[0] for c in calls], ["fix"])
        ping.assert_called_once()

    def test_changed_source_line_or_new_finding_follows_the_fix_flow(self):
        cases = {
            "changed line": ([self.FINDING, self.FINDING],
                             {("head-1", "mahler/ghapp.py", 52): "a", ("head-2", "mahler/ghapp.py", 52): "b"}),
            "unreadable line": ([self.FINDING, self.FINDING], {}),
            "new file:line": ([self.FINDING, "mahler/ghapp.py:90 other bug"], None),
            "no location": ([self.FINDING, "something is wrong"], None),
            "extra finding": ([self.FINDING, f"{self.FINDING} | mahler/x.py:3 more"], None),
        }
        for name, (findings, sources) in cases.items():
            with self.subTest(name):
                self.gh.sources = sources
                self.failed_rounds(findings)
                calls = []
                self.patch_review_start(calls)
                self.ship()
                self.assertEqual([c[0] for c in calls], ["fix"])
                for run in self.led.active_runs():
                    self.led.update_run(run["id"], status="ended")
                self.led.release("x", 5)
                self.led.set_kv("reviewfix:x#5:88:" + self.gh.head_sha, None)
                self.led.set_kv("reviewdup:x#5", None)
                mock.patch.stopall()
                self.setUp()


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


class TestClassifiedReview(unittest.TestCase):
    setUp = ShipTests.setUp
    item = ShipTests.item

    def finding(self, severity="follow-up", **fields):
        return dict(severity=severity, category="behavior", location="app.py:12",
                    scenario="Retry after an interrupted request", consequence="Request is lost", **fields)

    def ending(self, findings, verdict="pass", rid=10):
        self.led.upsert_item("x", 5, pr=88, labels=json.dumps(["size:m"]))
        self.led.set_kv("review:x#5", json.dumps({"sha": "abc123", "run_id": rid, "pr": 88}))
        return SimpleNamespace(led=self.led, ctx=self.ctx, project="x", number=5,
            item=self.item(), run={"id": rid, "platform": "agy-gemini"},
            rest=json.dumps({"findings": findings}), outcome="completed", set_state=mock.Mock())

    def finish(self, ending, verdict="pass"):
        with mock.patch.object(self.ctx, "gh", return_value=self.gh):
            getattr(finalize, "_review_passed" if verdict == "pass" else "_review_failed")(ending)

    def gate(self):
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(ship, "_merge_queued") as merge, \
                mock.patch.object(ship, "_review_triggered_fix") as fix, \
                mock.patch.object(ship, "_start_review_run") as start:
            ship._review_gate(self.ctx, "x", self.item(), 88, self.gh.pr_view(88))
        return merge, fix, start

    def test_followups_merge_without_fix_and_retries_preserve_links(self):
        ending = self.ending([self.finding()])
        self.finish(ending)
        self.finish(ending)
        self.gh.issue_by_marker = mock.Mock(return_value=None)
        self.gh.create_issue = mock.Mock(return_value="https://github.com/x/y/issues/99")
        self.cfg["projects"]["x"].update(scope="label", scope_label="managed")
        merge, fix, _ = self.gate()
        merge.assert_called_once()
        fix.assert_not_called()
        args = self.gh.create_issue.call_args.args
        self.assertIn("managed", args[2])
        self.assertIn("mahler:inbox", args[2])
        self.assertIn("https://github.com/x/y/issues/5", args[1])
        self.assertIn("https://github.com/x/y/pull/88", args[1])
        self.assertNotIn("Part of", args[1])
        self.assertNotIn("Depends on", args[1])
        self.finish(ending)
        self.gate()
        self.gh.create_issue.assert_called_once()
        self.assertTrue(any("issues/99" in c for c in self.gh.comments))

    def test_mixed_only_hands_blockers_to_fix_and_retains_followups(self):
        blocker = self.finding("blocking")
        follow = {**self.finding(), "location": "extra.py:20"}
        self.finish(self.ending([blocker, follow]), "fail")
        merge, fix, _ = self.gate()
        merge.assert_not_called()
        self.assertIn("app.py:12", fix.call_args.args[-1])
        self.assertNotIn("extra.py", fix.call_args.args[-1])
        self.finish(self.ending([], rid=11))
        records = json.loads(self.led.get_kv("reviewresults:x#5"))
        self.assertEqual(len(records["10"]["findings"]), 2)
        self.assertEqual(records["10"]["sha"], "abc123")
        self.gh.issue_by_marker = mock.Mock(return_value="https://github.com/x/y/issues/99")
        self.gh.create_issue = mock.Mock()
        self.gate()[0].assert_called_once()
        self.gh.create_issue.assert_not_called()

    def test_malformed_inconsistent_and_missing_citations_never_approve(self):
        for text in ('{"findings":', '{"findings":{}}', '{"findings":[{}]}', '',
                     json.dumps({"findings": [self.finding("blocking")]})):
            with self.subTest(text=text):
                ending = self.ending([])
                ending.rest = text
                self.finish(ending)
                self.assertIsNone(self.led.get_kv("review:x#5"))
        from mahler import review
        f = {**self.finding("blocking"), "category": "spec"}
        body = "## Done when\n- [ ] Retries preserve requests\n## Context\nOther text"
        for citation in (None, "", "Other text", "Retries preserve requests"):
            f["done_when"] = citation
            with self.assertRaises(ValueError):
                review.parse("fail", json.dumps({"findings": [f]}), body)
        f["done_when"] = "- [ ] Retries preserve requests"
        self.assertEqual(review.parse("fail", json.dumps({"findings": [f]}), body), [f])
        with self.assertRaises(ValueError):
            review.parse("fail", '{"findings":[]}', body)

    def test_failure_retry_and_remote_recovery_after_lost_local_write(self):
        self.finish(self.ending([self.finding()]))
        self.gh.issue_by_marker = mock.Mock(side_effect=gh_module.GHError("offline"))
        self.gh.create_issue = mock.Mock(return_value="https://github.com/x/y/issues/99")
        merge, fix, _ = self.gate()
        merge.assert_not_called()
        fix.assert_not_called()
        self.gh.issue_by_marker.side_effect = None
        self.gh.issue_by_marker.return_value = None
        before = self.led.get_kv("reviewresults:x#5")
        original = self.led.set_kv
        def lose_write(key, value):
            if key == "reviewresults:x#5":
                raise RuntimeError("process died")
            original(key, value)
        with mock.patch.object(self.led, "set_kv", side_effect=lose_write):
            with self.assertRaises(RuntimeError):
                self.gate()
        self.assertEqual(self.led.get_kv("reviewresults:x#5"), before)
        self.ctx = scheduler.Ctx(self.cfg, self.led)  # restart, only ledger survives
        self.gh.issue_by_marker.return_value = "https://github.com/x/y/issues/99"
        self.gate()[0].assert_called_once()
        self.gh.create_issue.assert_called_once()

    def test_stale_head_and_dry_run_never_file(self):
        self.finish(self.ending([self.finding()]))
        self.gh.create_issue = mock.Mock()
        self.gh.issue_by_marker = mock.Mock()
        self.gh.head_sha = "new-head"
        merge, fix, start = self.gate()
        merge.assert_not_called()
        start.assert_called_once()
        self.gh.head_sha = "abc123"
        self.ctx.dry_run = True
        self.gate()[0].assert_not_called()
        self.gh.issue_by_marker.assert_not_called()
        self.gh.create_issue.assert_not_called()

    def test_old_finalization_does_not_replace_new_run(self):
        ending = self.ending([])
        newer = json.dumps({"sha": "new-head", "run_id": 11, "verdict": "pending"})
        self.led.set_kv("review:x#5", newer)
        self.finish(ending)
        self.assertEqual(self.led.get_kv("review:x#5"), newer)

    def test_create_failure_stays_pending_and_retries_without_fix(self):
        self.finish(self.ending([self.finding()]))
        self.gh.issue_by_marker = mock.Mock(return_value=None)
        self.gh.create_issue = mock.Mock(side_effect=gh_module.GHError("unavailable"))
        merge, fix, _ = self.gate()
        merge.assert_not_called()
        fix.assert_not_called()
        self.assertEqual(json.loads(self.led.get_kv("review:x#5"))["verdict"], "pass")
        self.gh.create_issue.side_effect = None
        self.gh.create_issue.return_value = "https://github.com/x/y/issues/99"
        self.gate()[0].assert_called_once()
        self.assertEqual(self.gh.issue_by_marker.call_count, 2)


class TestGreenReviewRounds(unittest.TestCase):
    setUp = ShipTests.setUp
    item = ShipTests.item
    finding = TestClassifiedReview.finding
    ending = TestClassifiedReview.ending
    finish = TestClassifiedReview.finish
    gate = TestClassifiedReview.gate

    def rounds(self):
        from mahler import review
        return review.window(self.led, "x", 5, self.item()["pr"])

    def complete_head(self, sha, findings, rid):
        ending = self.ending(findings, rid=rid)
        info = json.loads(self.led.get_kv("review:x#5"))
        info.update(sha=sha, pr=self.item()["pr"])
        self.led.set_kv("review:x#5", json.dumps(info))
        self.gh.head_sha = sha
        self.finish(ending, "fail" if any(f["severity"] == "blocking" for f in findings) else "pass")
        return ending

    def test_inclusive_threshold_and_different_file_followups_merge(self):
        from mahler import review
        for threshold in (2, 3):
            self.cfg["projects"]["x"]["review_green_rounds"] = threshold
            self.led.set_kv("reviewrounds:x#5:88", None)
            self.led.set_kv("reviewresults:x#5", None)
            self.gh.issue_by_marker = mock.Mock(return_value=None)
            self.gh.create_issue = mock.Mock(return_value="https://github.com/x/y/issues/99")
            for number in range(1, threshold + 2):
                with self.subTest(threshold=threshold, number=number):
                    sha = f"head-{number}"
                    self.led.upsert_item("x", 5, pr=88)
                    context = review.start_context(self.ctx, "x", self.item(), 88, sha)
                    self.assertIn(f"Green review round: {number};", context)
                    self.assertEqual("Convergence threshold reached" in context, number >= threshold)
                    finding = {**self.finding(), "location": f"file{number}.py:10"}
                    self.complete_head(sha, [finding], threshold * 10 + number)
                    merge, fix, _ = self.gate()
                    merge.assert_called_once()
                    fix.assert_not_called()
                    self.assertEqual(len(self.rounds()), number)
            self.assertIsNotNone(self.led.get_kv("reviewdrift:x#5:88"))
            self.assertEqual(self.gh.create_issue.call_count, threshold + 1)
            self.assertTrue(any("Classified as nonblocking" in c for c in self.gh.comments))

    def test_new_and_unresolved_blockers_remain_blocking_after_threshold(self):
        for i, category in enumerate(("security", "security", "data-loss", "behavior"), 1):
            f = {**self.finding("blocking"), "category": category}
            if i == 4:
                f.update(location="different.py:20", scenario="A normal save loses the update")
            self.complete_head(f"head-{i}", [f], i)
            merge, fix, _ = self.gate()
            merge.assert_not_called()
            fix.assert_called_once()
            self.assertEqual(len(self.rounds()), i)
        # Same file/line does not establish the same classified defect.
        from mahler import review
        a = self.finding("blocking")
        self.assertTrue(review.same_findings([a], [a]))
        self.assertFalse(review.same_findings([a], [{**a, "scenario": "New evidence"}]))

    def test_rounds_survive_restart_retries_and_manual_reship(self):
        ending = self.complete_head("abc123", [], 10)
        self.gate()
        self.finish(ending)
        self.gate()
        self.complete_head("abc123", [], 11)  # alternate reviewer, same head
        self.gate()
        self.assertEqual(len(self.rounds()), 1)
        self.assertEqual(len(self.rounds()[0]["reviews"]), 2)
        self.ctx = scheduler.Ctx(self.cfg, self.led)
        args = SimpleNamespace(item=("x", 5), pr=88, branch=None, holder="test", summary=None)
        with mock.patch.object(cli, "project_client", return_value=self.gh):
            self.assertEqual(cli.cmd_ship(args, self.cfg, self.led), 0)
        self.gate()
        self.assertEqual(len(self.rounds()), 1)
        self.complete_head("second-head", [], 12)
        self.gate()
        self.assertEqual(len(self.rounds()), 2)
        # Same SHA on a different PR cannot reuse the old verdict or round window.
        self.led.upsert_item("x", 5, pr=89)
        with mock.patch.object(ship, "_start_review_run") as start:
            ship._review_gate(self.ctx, "x", self.item(), 89, self.gh.pr_view(89))
        start.assert_called_once()
        self.assertEqual(self.rounds(), [])
        from mahler import review
        self.assertIn("Green review round: 1;",
                      review.start_context(self.ctx, "x", self.item(), 89, "second-head"))
        self.led.set_kv("review:x#5", json.dumps({
            "sha": "second-head", "pr": 89, "verdict": "pass", "run_id": 13,
            "classified": []}))
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(ship, "_merge_queued") as merge:
            ship._review_gate(self.ctx, "x", self.item(), 89, self.gh.pr_view(89))
        merge.assert_called_once()
        self.assertEqual(len(self.rounds()), 1)
        self.assertEqual(len(review.window(self.led, "x", 5, 88)), 2)

    def test_red_pending_stale_and_missing_verdicts_never_count(self):
        self.complete_head("abc123", [], 10)
        for rollup in ([{"state": "FAILURE"}], [{"state": "PENDING"}]):
            self.gh.rollup = rollup
            merge, fix, start = self.gate()
            merge.assert_not_called()
            fix.assert_not_called()
            start.assert_not_called()
            self.assertEqual(self.rounds(), [])
        self.gh.rollup = [{"state": "SUCCESS"}]
        self.gh.head_sha = "new-head"
        self.gate()[2].assert_called_once()
        self.assertEqual(self.rounds(), [])
        self.gh.head_sha = "abc123"
        for info in ({}, {"sha": "abc123", "verdict": "pending"}):
            self.led.set_kv("review:x#5", json.dumps(info))
            self.gate()[2].assert_called_once()
            self.assertEqual(self.rounds(), [])

    def test_review_start_receives_prior_classified_evidence(self):
        f = self.finding("blocking")
        self.complete_head("old-head", [f], 10)
        self.gate()
        self.gh.head_sha = "new-head"
        self.led.set_kv("review:x#5", None)
        with mock.patch.object(ship, "start", return_value=True) as start:
            ship._start_review_run(self.ctx, "x", self.item(), 88, self.gh.pr_view(88), "new-head")
        context = start.call_args.kwargs["context"]
        self.assertIn("Green review round: 2; review_green_rounds: 2", context)
        self.assertIn(f["scenario"], context)
        self.assertIn("Do not revive fixed findings without new evidence", context)

    def test_same_location_new_classified_evidence_is_not_repeat(self):
        previous = self.finding("blocking")
        current = {**previous, "scenario": "A newly demonstrated failure on the same line"}
        for i, f in enumerate((previous, current), 1):
            self.complete_head(f"head-{i}", [f], i)
            self.gate()
        with mock.patch.object(self.ctx, "gh", return_value=self.gh):
            self.assertIsNone(ship._repeat_finding(self.ctx, "x", 5, self.gh.pr_view(88)))
        current["scenario"] = previous["scenario"]
        history = json.loads(self.led.get_kv("reviewfindings:x#5"))
        history[-1]["classified"] = [current]
        self.led.set_kv("reviewfindings:x#5", json.dumps(history))
        with mock.patch.object(self.ctx, "gh", return_value=self.gh):
            self.assertIsNotNone(ship._repeat_finding(self.ctx, "x", 5, self.gh.pr_view(88)))

    def test_before_threshold_blocker_starts_fix_without_design(self):
        self.cfg["projects"]["x"]["review_green_rounds"] = 3
        finding = {**self.finding("blocking"), "location": "a.py:10"}
        self.complete_head("head-1", [finding], 1)
        merge, fix, _ = self.gate()
        merge.assert_not_called()
        fix.assert_called_once()
        self.assertIsNone(self.led.get_kv("design:x#5:88:head-1"))

    def test_design_starts_only_at_threshold_after_drift_is_established(self):
        self.cfg["projects"]["x"]["review_green_rounds"] = 4
        later = NOW + timedelta(hours=2)
        self.led.record_usage("claude-opus", "5h", 10, later)
        self.led.record_usage("claude-opus", "weekly", 10, later)
        self.led.claim("x", 5, CONDUCTOR, "auto", 30, capacity=False)

        for number in range(1, 4):
            finding = {**self.finding("blocking"),
                       "location": f"file-{number}.py:{number * 10}"}
            self.complete_head(f"head-{number}", [finding], number)
            merge, fix, _ = self.gate()
            merge.assert_not_called()
            fix.assert_called_once()
            self.assertIsNone(self.led.get_kv(f"design:x#5:88:head-{number}"))

        self.assertIsNotNone(self.led.get_kv("reviewdrift:x#5:88"))
        finding = {**self.finding("blocking"), "location": "file-4.py:40"}
        self.complete_head("head-4", [finding], 4)
        with mock.patch.object(ship, "start", return_value=True) as start_mock:
            merge, fix, _ = self.gate()
            merge.assert_not_called()
            fix.assert_not_called()
            design_calls = [call for call in start_mock.call_args_list
                            if len(call.args) > 3 and call.args[3] == "design"]
            self.assertEqual(len(design_calls), 1)
            self.assertFalse(any(call.args[3] == "fix"
                                 for call in start_mock.call_args_list
                                 if len(call.args) > 3))

    def test_threshold_with_disjoint_location_drift_starts_one_design_run_and_no_simultaneous_fix(self):
        self.cfg["projects"]["x"]["review_green_rounds"] = 2
        later = NOW + timedelta(hours=2)
        self.led.record_usage("claude-opus", "5h", 10, later)
        self.led.record_usage("claude-opus", "weekly", 10, later)
        self.led.claim("x", 5, CONDUCTOR, "auto", 30, capacity=False)
        self.complete_head("head-1", [{**self.finding("blocking"), "location": "a.py:10"}], 1)
        self.gate()
        self.complete_head("head-2", [{**self.finding("blocking"), "location": "b.py:20"}], 2)
        self.gate()
        self.complete_head("head-3", [{**self.finding("blocking"), "location": "c.py:30"}], 3)
        with mock.patch.object(ship, "start", return_value=True) as start_mock:
            merge, fix, _ = self.gate()
            merge.assert_not_called()
            fix.assert_not_called()
            start_mock.assert_called_once()
            self.assertEqual(start_mock.call_args.args[3], "design")
            self.assertEqual(start_mock.call_args.kwargs["handoff_from"][0], CONDUCTOR)

    def test_failed_design_start_retries_without_fix_or_competing_review(self):
        self.cfg["projects"]["x"]["review_green_rounds"] = 2
        later = NOW + timedelta(hours=2)
        self.led.record_usage("claude-opus", "5h", 10, later)
        self.led.record_usage("claude-opus", "weekly", 10, later)
        self.led.claim("x", 5, CONDUCTOR, "auto", 30, capacity=False)
        self.complete_head("head-1", [{**self.finding("blocking"), "location": "a.py:10"}], 1)
        self.gate()
        self.complete_head("head-2", [{**self.finding("blocking"), "location": "b.py:20"}], 2)
        self.gate()
        self.complete_head("head-3", [{**self.finding("blocking"), "location": "c.py:30"}], 3)
        with mock.patch.object(ship, "start", return_value=False) as start_mock:
            merge, fix, start_review = self.gate()
            merge.assert_not_called()
            fix.assert_not_called()
            start_review.assert_not_called()
            start_mock.assert_called_once()
            self.assertEqual(start_mock.call_args.args[3], "design")
        # Failed attempt is not recorded as completed, leaving it retryable
        self.assertIsNone(self.led.get_kv("design:x#5:88:head-3"))
        # Subsequent gate pass retries and succeeds when start returns True
        with mock.patch.object(ship, "start", return_value=True) as retry_mock:
            merge, fix, start_review = self.gate()
            merge.assert_not_called()
            fix.assert_not_called()
            start_review.assert_not_called()
            retry_mock.assert_called_once()
            self.assertEqual(retry_mock.call_args.args[3], "design")

    def test_design_capacity_wait_does_not_start_fix_run(self):
        self.cfg["projects"]["x"]["review_green_rounds"] = 2
        self.complete_head("head-1", [{**self.finding("blocking"), "location": "a.py:10"}], 1)
        self.gate()
        self.complete_head("head-2", [{**self.finding("blocking"), "location": "b.py:20"}], 2)
        self.gate()
        self.complete_head("head-3", [{**self.finding("blocking"), "location": "c.py:30"}], 3)
        self.gate()
        self.assertIsNotNone(self.led.get_kv("reviewdrift:x#5:88"))
        with mock.patch.object(router, "capacity_recovery", return_value=(["busy"], None, None)):
            merge, fix, _ = self.gate()
            merge.assert_not_called()
            fix.assert_not_called()
            wait = json.loads(self.led.get_kv("design-wait:x#5") or "{}")
            self.assertEqual(wait.get("state"), "capacity_wait")

    def test_one_design_only_across_ticks_and_restarts(self):
        self.cfg["projects"]["x"]["review_green_rounds"] = 2
        self.complete_head("head-1", [{**self.finding("blocking"), "location": "a.py:10"}], 1)
        self.gate()
        self.complete_head("head-2", [{**self.finding("blocking"), "location": "b.py:20"}], 2)
        self.gate()
        self.complete_head("head-3", [{**self.finding("blocking"), "location": "c.py:30"}], 3)
        # Active design run: waits across ticks without starting fix or another design
        self.led.create_run(project="x", number=5, role="design", platform="agy-gemini", status="running", epoch=1)
        with mock.patch.object(ship, "start") as start_mock:
            merge, fix, _ = self.gate()
            merge.assert_not_called()
            fix.assert_not_called()
            start_mock.assert_not_called()
        # Completed design run: duplicate ticks/restarts do not start a second design run
        self.led.q("DELETE FROM runs WHERE project='x' AND number=5 AND role='design'")
        self.led.set_kv("design:x#5:88:head-3", json.dumps({"disposition": "fix", "consumed": True}))
        with mock.patch.object(ship, "start") as start_mock:
            self.gate()
            self.assertFalse(any(c.args[3] == "design" for c in start_mock.call_args_list if len(c.args) > 3))

    def test_planned_fix_consumption_starts_one_fix_and_subsequent_head_reviewed(self):
        self.cfg["projects"]["x"]["review_green_rounds"] = 2
        plan = {
            "summary": "Fix all bugs",
            "files": ["a.py", "b.py"],
            "steps": ["step 1", "step 2"],
            "tests": "pytest"
        }
        self.complete_head("head-3", [{**self.finding("blocking"), "location": "c.py:30"}], 3)
        self.led.set_kv("design:x#5:88:head-3", json.dumps({
            "head": "head-3", "disposition": "fix", "plan": plan, "consumed": False
        }))
        with mock.patch.object(ship, "start", return_value=True) as start_mock:
            with mock.patch.object(self.ctx, "gh", return_value=self.gh):
                ship._review_gate(self.ctx, "x", self.item(), 88, self.gh.pr_view(88))
            start_mock.assert_called_once()
            self.assertEqual(start_mock.call_args.args[3], "fix")
            context = start_mock.call_args.kwargs["context"]
            self.assertIn("Fix all bugs", context)
            self.assertIn("step 1", context)
            self.assertIn("pytest", context)
        self.assertEqual(self.led.get_kv("design-consumed:x#5:88:head-3"), "1")
        record = json.loads(self.led.get_kv("design:x#5:88:head-3"))
        self.assertTrue(record.get("consumed"))
        # Subsequent new head triggers fresh independent review
        self.gh.head_sha = "head-4"
        self.led.set_kv("review:x#5", None)
        with mock.patch.object(ship, "_start_review_run") as start_review:
            with mock.patch.object(self.ctx, "gh", return_value=self.gh):
                ship._review_gate(self.ctx, "x", self.item(), 88, self.gh.pr_view(88))
            start_review.assert_called_once()

    def test_design_result_followups_files_deduped_issues_and_merges(self):
        self.cfg["projects"]["x"]["review_green_rounds"] = 2
        findings = [{
            "severity": "follow-up", "category": "behavior",
            "location": "a.py:10", "scenario": "edge case", "consequence": "minor glitch"
        }]
        self.complete_head("head-3", [{**self.finding("blocking"), "location": "c.py:30"}], 3)
        self.led.set_kv("design:x#5:88:head-3", json.dumps({
            "head": "head-3", "run_id": 42, "disposition": "followups",
            "justification": "Minor edge case acceptable for v1",
            "findings": findings
        }))
        self.gh.issue_by_marker = mock.Mock(return_value=None)
        self.gh.create_issue = mock.Mock(return_value="https://github.com/x/y/issues/101")
        merge, fix, _ = self.gate()
        merge.assert_called_once()
        fix.assert_not_called()
        self.gh.create_issue.assert_called_once()
        self.assertTrue(any("issues/101" in c for c in self.gh.comments))
        self.assertTrue(any("Minor edge case acceptable for v1" in c for c in self.gh.comments))

    def test_design_result_followups_refuses_protected_blocker(self):
        self.cfg["projects"]["x"]["review_green_rounds"] = 2
        for keyword in ("security", "Done-when", "data loss"):
            with self.subTest(keyword=keyword):
                findings = [{
                    "severity": "follow-up", "category": keyword,
                    "location": "a.py:10", "scenario": f"Explicit {keyword} blocker", "consequence": "critical"
                }]
                self.complete_head("head-3", [{**self.finding("blocking"), "location": "c.py:30"}], 3)
                self.led.set_kv("design:x#5:88:head-3", json.dumps({
                    "head": "head-3", "run_id": 42, "disposition": "followups",
                    "justification": f"Acceptable despite {keyword}",
                    "findings": findings
                }))
                merge, fix, _ = self.gate()
                merge.assert_not_called()
                fix.assert_called_once()
        with self.subTest(case="unsatisfied-done-when-field"):
            findings = [{
                "severity": "follow-up", "category": "spec", "done_when": "- [ ] Must verify credentials",
                "location": "a.py:10", "scenario": "Credentials not verified", "consequence": "acceptance criteria unsatisfied"
            }]
            self.complete_head("head-3", [{**self.finding("blocking"), "location": "c.py:30"}], 3)
            self.led.set_kv("design:x#5:88:head-3", json.dumps({
                "head": "head-3", "run_id": 42, "disposition": "followups",
                "justification": "Acceptable follow-up",
                "findings": findings
            }))
            merge, fix, _ = self.gate()
            merge.assert_not_called()
            fix.assert_called_once()

    def test_design_result_followups_refuses_blocking_classified_blocker(self):
        for category in ("security", "data-loss"):
            for severity in ("blocking", "follow-up"):
                with self.subTest(category=category, severity=severity):
                    self.led.set_kv("reviewresults:x#5", None)
                    sha = f"head-{category}-{severity}"
                    findings = [{
                        "severity": severity, "category": category,
                        "location": "a.py:10", "scenario": "Retry after interruption",
                        "consequence": "Stored information is exposed or lost",
                    }]
                    # Keep the review failing for the nonblocking control, too.
                    findings.append(self.finding("blocking"))
                    self.complete_head(sha, findings, 3)
                    info = json.loads(self.led.get_kv("review:x#5"))
                    self.assertEqual(info["classified"], findings)
                    self.assertEqual(info["verdict"], "fail")
                    # Isolate the classified guard from the redundant prose guard.
                    info["findings"] = "Review requires a fix."
                    self.led.set_kv("review:x#5", json.dumps(info))
                    self.led.set_kv(f"design:x#5:88:{sha}", json.dumps({
                        "head": sha, "run_id": 42, "disposition": "followups",
                        "justification": "Minor edge case acceptable for v1",
                        "findings": [self.finding("follow-up")],
                    }))
                    self.gh.issue_by_marker = mock.Mock(return_value=None)
                    self.gh.create_issue = mock.Mock(return_value="https://github.com/x/y/issues/101")
                    merge, fix, _ = self.gate()
                    if severity == "blocking":
                        merge.assert_not_called()
                        fix.assert_called_once()
                    else:
                        merge.assert_called_once()
                        fix.assert_not_called()

    def test_design_result_followups_accepts_incidental_prose_mentions(self):
        self.cfg["projects"]["x"]["review_green_rounds"] = 2
        for idx, keyword in enumerate(("security", "Done-when", "data loss"), 1):
            with self.subTest(keyword=keyword):
                self.led.set_kv("reviewresults:x#5", None)
                sha = f"head-incidental-{idx}"
                findings = [{
                    "severity": "follow-up", "category": "behavior",
                    "location": "a.py:10",
                    "scenario": f"Found nonblocking behavior issue mentioning {keyword} incidentally",
                    "consequence": f"Incidental mention of {keyword} in consequence"
                }]
                self.complete_head(sha, [{
                    **self.finding("blocking"),
                    "location": "c.py:30",
                    "scenario": f"Review prose incidentally mentioning {keyword}",
                }], idx)
                self.led.set_kv(f"design:x#5:88:{sha}", json.dumps({
                    "head": sha, "run_id": 40 + idx, "disposition": "followups",
                    "justification": f"Acceptable justification mentioning {keyword} incidentally",
                    "findings": findings
                }))
                self.gh.issue_by_marker = mock.Mock(return_value=None)
                self.gh.create_issue = mock.Mock(return_value=f"https://github.com/x/y/issues/10{idx}")
                merge, fix, _ = self.gate()
                merge.assert_called_once()
                fix.assert_not_called()
                self.gh.create_issue.assert_called_once()
                self.assertTrue(any(f"issues/10{idx}" in c for c in self.gh.comments))

    def test_design_result_followups_ignores_legacy_review_findings_marker(self):
        self.cfg["projects"]["x"]["review_green_rounds"] = 2
        self.led.set_kv("reviewresults:x#5", None)
        sha = "head-legacy-marker"
        findings = [{
            "severity": "follow-up", "category": "behavior",
            "location": "a.py:10", "scenario": "Minor", "consequence": "None"
        }]
        self.complete_head(sha, [{**self.finding("blocking"), "location": "c.py:30"}], 3)
        info = json.loads(self.led.get_kv("review:x#5"))
        info["findings"] = "[blocking/security] Something bad\n[blocking/spec](Done when: must verify)"
        info["classified"] = []
        self.led.set_kv("review:x#5", json.dumps(info))
        self.led.set_kv(f"design:x#5:88:{sha}", json.dumps({
            "head": sha, "run_id": 99, "disposition": "followups",
            "justification": "Acceptable",
            "findings": findings
        }))
        self.gh.issue_by_marker = mock.Mock(return_value=None)
        self.gh.create_issue = mock.Mock(return_value="https://github.com/x/y/issues/200")
        merge, fix, _ = self.gate()
        merge.assert_called_once()
        fix.assert_not_called()

    def test_overlapping_findings_reset_drift_and_missing_locations_inconclusive(self):
        self.cfg["projects"]["x"]["review_green_rounds"] = 2
        self.complete_head("head-1", [{**self.finding("blocking"), "location": "a.py:10"}], 1)
        self.gate()
        self.complete_head("head-2", [{**self.finding("blocking"), "location": "b.py:20"}], 2)
        self.gate()
        self.complete_head("head-3", [{**self.finding("blocking"), "location": "c.py:30"}], 3)
        self.gate()
        self.assertIsNotNone(self.led.get_kv("reviewdrift:x#5:88"))
        # Overlapping finding resets drift
        self.complete_head("head-4", [{**self.finding("blocking"), "location": "b.py:20"}], 4)
        merge, fix, _ = self.gate()
        self.assertIsNone(self.led.get_kv("reviewdrift:x#5:88"))
        fix.assert_called_once()
        # Location-less finding is inconclusive
        self.complete_head("head-5", [{**self.finding("blocking"), "location": "unknown"}], 5)
        merge, fix, _ = self.gate()
        self.assertIsNone(self.led.get_kv("reviewdrift:x#5:88"))
        fix.assert_called_once()

    def test_new_pr_starts_fresh_convergence_window(self):
        self.complete_head("head-1", [{**self.finding("blocking"), "location": "a.py:10"}], 1)
        self.complete_head("head-2", [{**self.finding("blocking"), "location": "b.py:20"}], 2)
        self.complete_head("head-3", [{**self.finding("blocking"), "location": "c.py:30"}], 3)
        self.gate()
        self.assertIsNotNone(self.led.get_kv("reviewdrift:x#5:88"))
        # Switch to PR 89
        self.led.upsert_item("x", 5, pr=89)
        self.assertIsNone(self.led.get_kv("reviewdrift:x#5:89"))
        self.assertIsNone(self.led.get_kv("design:x#5:89:head-3"))
        from mahler import review
        self.assertEqual(review.window(self.led, "x", 5, 89), [])

    def test_stale_head_does_not_start_design(self):
        self.cfg["projects"]["x"]["review_green_rounds"] = 2
        self.complete_head("head-1", [{**self.finding("blocking"), "location": "a.py:10"}], 1)
        self.gate()
        self.complete_head("head-2", [{**self.finding("blocking"), "location": "b.py:20"}], 2)
        self.gate()
        self.complete_head("head-3", [{**self.finding("blocking"), "location": "c.py:30"}], 3)
        self.gate()
        # Now PR head moves to new-head while review was on head-3
        self.gh.head_sha = "new-head"
        merge, fix, start = self.gate()
        merge.assert_not_called()
        fix.assert_not_called()
        start.assert_called_once()


class ShipCapacityTests(unittest.TestCase):
    """Fix and review waits in the shipping pass feed the capacity history (mahler#734)."""

    def setUp(self):
        ShipTests.setUp(self)

    item = ShipTests.item
    ship = ShipTests.ship

    def rows(self):
        self.ctx.capacity.flush()
        return {(r["role"]): dict(r) for r in self.led.capacity_intervals()}

    def test_ci_fix_wait_is_recorded_each_tick_as_one_interval(self):
        self.led.upsert_item("x", 5, pr=88, labels='["size:l"]')
        self.gh.rollup = [{"state": "FAILURE"}]
        now = [NOW]
        self.led.now = lambda: now[0]
        with mock.patch.object(platforms, "available", return_value=False):
            self.ship()
            self.ctx.capacity.flush()
            for _ in range(3):
                now[0] += timedelta(seconds=60)
                self.ship()
                self.ctx.capacity.flush()
        (row,) = [dict(r) for r in self.led.capacity_intervals()]
        self.assertEqual((row["role"], row["required_tier"]), ("fix", 0))
        self.assertEqual((row["size"], row["effective_size"]), ("l", "m"))
        self.assertEqual(json.loads(row["blockers"]), ["shipping_wait"])
        self.assertEqual(row["last_seen"], iso(NOW + timedelta(seconds=180)))
        self.assertEqual(json.loads(row["platforms"])["agy-claude"]["reasons"], ["unavailable"])

    def test_review_triggered_fix_wait_is_recorded(self):
        self.led.upsert_item("x", 5, pr=88, labels='["size:l"]')
        self.led.set_kv("review:x#5", json.dumps({"sha": "abc123", "verdict": "fail", "pr": 88}))
        with mock.patch("mahler.router.pick_for_project", return_value=(None, ["x: soft"])):
            self.ship()
        row = self.rows()["fix"]
        self.assertEqual((row["size"], row["effective_size"]), ("l", "m"))
        self.assertEqual(json.loads(row["blockers"]), ["shipping_wait"])

    def test_review_wait_is_recorded_and_retry_delay_stays_observable(self):
        self.cfg["routing"]["review"] = ["agy-claude"]
        self.led.upsert_item("x", 5, pr=88, labels='["size:m"]')
        self.led.record_usage("agy-claude", "hold", 100, iso(NOW + timedelta(hours=30)))
        with mock.patch("mahler.router.pick_for_project") as pick:
            self.ship()
            pick.assert_not_called()              # observation never forces a retry
        row = self.rows()["review"]
        self.assertEqual(json.loads(row["platforms"])["agy-claude"]["reasons"], ["hold"])

    def test_launch_after_a_wait_closes_the_interval(self):
        self.led.upsert_item("x", 5, pr=88, labels='["size:m"]')
        self.gh.rollup = [{"state": "FAILURE"}]
        with mock.patch.object(platforms, "available", return_value=False):
            self.ship()
        self.ctx.capacity.flush()
        self.ctx.capacity.launched("x", 5, "fix", 41, "agy-claude")
        self.ctx.capacity.flush()
        (row,) = [dict(r) for r in self.led.capacity_intervals()]
        self.assertEqual((row["open"], row["launch_run_id"]), (0, 41))

    def test_ci_fix_wait_when_global_slots_full_preserves_escalated_metadata_through_launch(self):
        self.cfg["concurrency"]["total"] = 1
        self.led.create_run(project="y", number=99, role="build", platform="claude", epoch=1)
        self.led.upsert_item("x", 5, pr=88, labels='["size:s"]', esc_tier=2)
        self.gh.rollup = [{"state": "FAILURE"}]
        self.ship()
        row = self.rows()["fix"]
        self.assertEqual((row["size"], row["effective_size"], row["required_tier"]),
                         ("s", "m", 2))
        self.assertEqual(json.loads(row["blockers"]), ["shipping_wait"])

        self.ctx.capacity.launched("x", 5, "fix", 55, "claude")
        self.ctx.capacity.flush()
        row = self.rows()["fix"]
        self.assertEqual((row["open"], row["end_reason"], row["launch_run_id"]),
                         (0, "launched", 55))
        self.assertEqual((row["size"], row["effective_size"], row["required_tier"]),
                         ("s", "m", 2))

    def test_review_triggered_fix_wait_when_global_slots_full_preserves_escalated_metadata(self):
        self.cfg["concurrency"]["total"] = 1
        self.led.create_run(project="y", number=99, role="build", platform="claude", epoch=1)
        self.led.upsert_item("x", 5, pr=88, labels='["size:s"]', esc_tier=2)
        self.led.set_kv("reviewed-pr:x#5", "88")
        self.led.set_kv("review:x#5", json.dumps({"sha": "abc123", "verdict": "fail", "pr": 88}))
        self.ship()
        row = self.rows()["fix"]
        self.assertEqual((row["size"], row["effective_size"], row["required_tier"]),
                         ("s", "m", 2))
        self.assertEqual(json.loads(row["blockers"]), ["shipping_wait"])

        self.ctx.capacity.launched("x", 5, "fix", 56, "claude")
        self.ctx.capacity.flush()
        row = self.rows()["fix"]
        self.assertEqual((row["open"], row["end_reason"], row["launch_run_id"]),
                         (0, "launched", 56))
        self.assertEqual((row["size"], row["effective_size"], row["required_tier"]),
                         ("s", "m", 2))

    def test_history_write_failure_does_not_stop_shipping(self):
        self.led.upsert_item("x", 5, pr=88)
        self.gh.rollup = [{"state": "SUCCESS"}]
        with mock.patch.object(Ledger, "capacity_record", side_effect=OSError("disk")), \
             mock.patch.object(ship, "_observe_wait", side_effect=RuntimeError("diag")):
            self.ship()
            self.ctx.capacity.flush()
        self.assertTrue(any("capacity history" in l for l in self.ctx.lines))

    def test_diagnosis_failure_does_not_change_the_wait(self):
        self.led.upsert_item("x", 5, pr=88, labels='["size:m"]')
        self.gh.rollup = [{"state": "FAILURE"}]
        with mock.patch.object(platforms, "available", return_value=False), \
             mock.patch.object(ship.router, "diagnose", side_effect=RuntimeError("boom")):
            self.ship()
        self.assertEqual(self.item()["state"], "verifying")
        self.assertTrue(self.led.get_kv("reviewfix-status:x#5"))
        self.assertTrue(any("capacity diagnosis failed" in l for l in self.ctx.lines))

    def test_dry_run_records_nothing(self):
        self.led.upsert_item("x", 5, pr=88, labels='["size:m"]')
        self.gh.rollup = [{"state": "FAILURE"}]
        self.ctx.dry_run = True
        with mock.patch.object(platforms, "available", return_value=False):
            self.ship()
        self.assertEqual(self.rows(), {})


class DependencyPRTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = NOW
        self.led = Ledger(self.tmp.name + '/ledger.db', clock=lambda: self.now)
        self.addCleanup(lambda: self.led.close())
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg['projects']['x'] = {'path': self.tmp.name, 'repo': 'x/y'}
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
        self.ctx.merge_requested = False
        self.gh = mock.Mock()
        self.views = {}
        self.gh.open_prs.side_effect = lambda: list(self.views.values())
        self.gh.dependency_pr_view.side_effect = lambda n: copy.deepcopy(self.views[n])
        self.gh.base_in_head.return_value = True
        self.gh.pr_merge.side_effect = self.merge
        self.gh.issue_by_marker.return_value = None
        self.gh.create_issue.return_value = 'https://github.com/x/y/issues/123'
        patcher = mock.patch.object(self.ctx, 'gh', return_value=self.gh)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.add(10)

    def add(self, n, before='1.2.3', after='1.2.4', bot='dependabot'):
        body = f'Bumps [package](https://example.com/package) from {before} to {after}.'
        if bot == 'renovate':
            kind = 'major' if before[0] != after[0] else 'minor' if before[2] != after[2] else 'patch'
            body = ('| Package | Type | Update | Change |\n|---|---|---|---|\n'
                    f'| package | dependencies | {kind} | `{before}` → `{after}` |\n')
        self.views[n] = dict(number=n, title='Update package', body=body,
            url=f'https://github.com/x/y/pull/{n}', author={'login': bot + '[bot]'},
            isCrossRepository=False, isDraft=False, state='OPEN', headRefName=bot + '/package',
            headRefOid=str(n).zfill(40), baseRefName='release', mergeable='MERGEABLE',
            statusCheckRollup=[{'state': 'SUCCESS'}], files=[{'path': 'package-lock.json'}])

    def merge(self, n, sha):
        self.assertEqual(self.views[n]['headRefOid'], sha)
        self.views[n].update(state='MERGED', mergedAt=iso(self.now))

    def run_pass(self):
        self.ctx.merge_requested = False
        ship._dependency_prs(self.ctx, 'x')

    def events(self):
        return self.led.q("SELECT * FROM events WHERE kind='dependency_adopted'")

    def reopen(self):
        self.led.close()
        self.led = Ledger(self.tmp.name + '/ledger.db', clock=lambda: self.now)
        self.ctx.led = self.led

    def requests(self):
        return [json.loads(r['value']) for r in self.led.q(
            "SELECT value FROM kv WHERE key LIKE 'dependency-rebase:x:%'")]

    def test_rebase_transition_to_confirmed_adoption(self):
        self.gh.base_in_head.return_value = False
        self.run_pass()
        self.run_pass()
        self.gh.request_dependency_rebase.assert_called_once_with(10, 'dependabot')
        self.gh.pr_merge.assert_not_called()
        self.assertIsNone(self.led.get_kv('dependency-pending:x'))
        self.assertFalse(self.events())
        self.views[10].update(headRefOid='b' * 40, statusCheckRollup=[{'state': 'PENDING'}])
        self.run_pass()
        self.gh.pr_merge.assert_not_called()
        self.gh.base_in_head.return_value = True
        self.views[10]['statusCheckRollup'] = [{'state': 'SUCCESS'}]
        self.run_pass()
        self.run_pass()
        self.gh.pr_merge.assert_called_once_with(10, 'b' * 40)
        self.assertEqual(len(self.events()), 1)
        self.assertTrue(self.requests()[0]['resolved_at'])

    def test_rebase_once_per_head_across_restart_and_base_movement(self):
        for n, bot in ((10, 'dependabot'), (11, 'renovate')):
            self.add(n, bot=bot)
        self.gh.base_in_head.return_value = False
        self.run_pass()
        self.run_pass()
        self.run_pass()
        self.reopen()
        self.views[10]['baseRefName'] = 'other'
        self.run_pass()
        self.assertEqual(self.gh.request_dependency_rebase.call_args_list,
                         [mock.call(10, 'dependabot'), mock.call(11, 'renovate')])
        self.views[10]['headRefOid'] = 'b' * 40
        self.run_pass()
        self.assertEqual(self.gh.request_dependency_rebase.call_count, 3)

    def test_rebase_timeout_boundary_dedup_and_admission_holds(self):
        self.gh.base_in_head.return_value = False
        self.run_pass()
        self.led.upsert_item('x', 5, state='working')
        self.led.claim('x', 5, 'session', 'interactive', 120)
        self.cfg['projects']['x']['dependency_prs_daily_cap'] = 0
        with mock.patch.object(self.ctx, 'ping') as ping:
            self.now += timedelta(minutes=59, seconds=59)
            self.run_pass()
            ping.assert_not_called()
            self.now += timedelta(seconds=1)
            self.run_pass()
            ping.assert_called_once()
            self.assertIn(self.views[10]['url'], ping.call_args.args[1])
            self.assertIn(self.views[10]['headRefOid'], ping.call_args.args[1])
            self.run_pass()
            self.run_pass()
            self.reopen()
            self.run_pass()
            ping.assert_called_once()

    def test_rebase_admission_and_timeout_during_each_hold(self):
        self.gh.base_in_head.return_value = False
        for hold in ('disabled', 'dry-run', 'cap', 'ordinary', 'capacity', 'merge'):
            with self.subTest(hold=hold):
                self.cfg['projects']['x'].update(dependency_prs=hold != 'disabled',
                                                dependency_prs_daily_cap=0 if hold == 'cap' else 3)
                self.ctx.dry_run = hold == 'dry-run'
                self.led.upsert_item('x', 5, state='verifying' if hold == 'ordinary' else 'ready')
                if hold == 'capacity':
                    self.led.claim('x', 5, 'session', 'interactive', 120)
                self.ctx.merge_requested = hold == 'merge'
                ship._dependency_prs(self.ctx, 'x')
                self.gh.request_dependency_rebase.assert_not_called()
                self.led.release('x', 5)
        self.ctx.merge_requested = False
        self.run_pass()
        self.now += timedelta(hours=1)
        for hold in ('cap', 'ordinary', 'merge'):
            with self.subTest(timeout_hold=hold), mock.patch.object(self.ctx, 'ping') as ping:
                request = self.requests()[0]
                request.pop('alerted_at', None)
                self.led.set_kv(f"dependency-rebase:x:10:{request['sha']}", json.dumps(request))
                self.cfg['projects']['x']['dependency_prs_daily_cap'] = 0 if hold == 'cap' else 3
                self.led.set_state('x', 5, 'verifying' if hold == 'ordinary' else 'ready')
                self.ctx.merge_requested = hold == 'merge'
                ship._dependency_prs(self.ctx, 'x')
                ping.assert_called_once()

    def test_rebase_resolution_suppresses_stale_alert(self):
        self.gh.base_in_head.return_value = False
        self.add(11)
        self.add(12)
        self.run_pass()
        self.views[10]['state'] = 'CLOSED'
        self.views[11]['state'] = 'MERGED'
        self.views[12].update(headRefOid='b' * 40, statusCheckRollup=[{'state': 'PENDING'}])
        self.now += timedelta(hours=1)
        with mock.patch.object(self.ctx, 'ping') as ping:
            self.run_pass()
            ping.assert_not_called()
        self.assertTrue(all(r.get('resolved_at') for r in self.requests()))

    def test_rebase_error_is_durable_and_does_not_block_other_pr(self):
        self.add(11)
        self.gh.base_in_head.side_effect = lambda path, base, sha: sha != '10'.zfill(40)
        def fail(n, bot):
            self.assertEqual(self.requests()[0]['outcome'], 'uncertain')
            raise gh_module.GHError('response lost')
        self.gh.request_dependency_rebase.side_effect = fail
        self.run_pass()
        self.gh.pr_merge.assert_called_once_with(11, '11'.zfill(40))
        self.reopen()
        self.run_pass()
        self.gh.request_dependency_rebase.assert_called_once()
        self.assertEqual(self.requests()[0]['error'], 'response lost')
        self.now += timedelta(hours=1)
        with mock.patch.object(self.ctx, 'ping') as ping:
            self.run_pass()
            self.assertIn('response lost', ping.call_args.args[1])
            self.run_pass()
            ping.assert_called_once()

    def test_rebase_reconciliation_api_and_notification_failures(self):
        self.gh.base_in_head.return_value = False
        self.run_pass()
        self.now += timedelta(hours=1)
        self.led.upsert_item('x', 5, state='verifying')
        with mock.patch.object(self.ctx, 'ping', side_effect=RuntimeError('offline')) as ping:
            self.gh.dependency_pr_view.side_effect = gh_module.GHError('unavailable')
            self.run_pass()
            ping.assert_not_called()
            self.gh.dependency_pr_view.side_effect = lambda n: copy.deepcopy(self.views[n])
            self.run_pass()
            self.reopen()
            self.run_pass()
            ping.assert_called_once()
        self.gh.request_dependency_rebase.assert_called_once()

    def test_interrupted_rebase_intent_is_not_retried(self):
        self.gh.base_in_head.return_value = False
        self.gh.request_dependency_rebase.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_pass()
        self.reopen()
        self.gh.request_dependency_rebase.side_effect = None
        self.run_pass()
        self.gh.request_dependency_rebase.assert_called_once()
        self.assertEqual(self.requests()[0]['outcome'], 'uncertain')

    def test_patch_minor_and_actions(self):
        for bot in ('dependabot', 'renovate'):
            for after in ('1.2.4', '1.3.0'):
                with self.subTest(bot=bot, after=after):
                    self.add(10, after=after, bot=bot)
                    self.assertEqual(ship._dependency_update(self.views[10])['classification'],
                                     'minor' if after == '1.3.0' else 'patch')
        self.run_pass()
        self.gh.base_in_head.assert_called_once_with(self.tmp.name, 'release', '10'.zfill(40))
        self.assertEqual(len(self.events()), 1)
        self.add(11)
        self.views[11].update(headRefName='dependabot/github_actions/actions/checkout-5',
                             body='Bumps [actions/checkout](https://example.com) from 4 to 5.',
                             files=[{'path': '.github/workflows/ci.yml'}])
        self.run_pass()
        self.assertEqual(json.loads(self.events()[-1]['detail'])['classification'], 'github-actions')
        self.add(12, bot='renovate')
        self.views[12]['body'] = self.views[12]['body'].replace('dependencies', 'action').replace('`1.2.3` → `1.2.4`', '`abc123` → `def456`')
        self.views[12]['files'] = [{'path': '.github/workflows/ci.yml'}]
        self.run_pass()
        self.assertEqual(len(self.events()), 3)

    def test_dependabot_body_without_blank_line_before_details(self):
        """Dependabot emits <details> immediately after Bumps without empty line."""
        body = (
            "Bumps [actions/checkout](https://github.com/actions/checkout) from 4 to 7.\n"
            "<details>\n"
            "<summary>Release notes</summary>\n"
            "</details>\n"
        )
        view = {
            "headRefName": "dependabot/github_actions/actions/checkout-7",
            "author": {"login": "dependabot[bot]"},
            "isCrossRepository": False,
            "body": body,
            "files": [{"path": ".github/workflows/ci.yml"}],
        }
        res = ship._dependency_update(view)
        self.assertEqual(res, {
            "classification": "github-actions",
            "dependencies": ["[actions/checkout](https://github.com/actions/checkout)"],
        })

        npm_body = (
            "Bumps [@vitejs/plugin-react](https://github.com/vitejs/vite-plugin-react) from 5.2.0 to 6.1.1.\n"
            "<details>\n"
            "<summary>Release notes</summary>\n"
            "</details>\n"
        )
        npm_view = {
            "headRefName": "dependabot/npm_and_yarn/vitejs/plugin-react-6.1.1",
            "author": {"login": "dependabot[bot]"},
            "isCrossRepository": False,
            "body": npm_body,
            "files": [{"path": "package.json"}, {"path": "package-lock.json"}],
        }
        res_npm = ship._dependency_update(npm_view)
        self.assertEqual(res_npm, {
            "classification": "major",
            "dependencies": ["[@vitejs/plugin-react](https://github.com/vitejs/vite-plugin-react)"],
        })

        patch_body = (
            "Bumps foo from 1.2.3 to 1.2.4.\n"
            "<details>\n"
            "<summary>Release notes</summary>\n"
            "</details>\n"
        )
        patch_view = {
            "headRefName": "dependabot/pip/foo-1.2.4",
            "author": {"login": "dependabot[bot]"},
            "isCrossRepository": False,
            "body": patch_body,
            "files": [{"path": "requirements.txt"}],
        }
        res_patch = ship._dependency_update(patch_view)
        self.assertEqual(res_patch, {
            "classification": "patch",
            "dependencies": ["foo"],
        })

        comment_body = (
            "<!-- comment -->\n"
            "Bumps foo from 1.2.3 to 1.3.0.\n"
            "<details>\n"
        )
        comment_view = {
            "headRefName": "dependabot/pip/foo-1.3.0",
            "author": {"login": "dependabot[bot]"},
            "isCrossRepository": False,
            "body": comment_body,
            "files": [{"path": "requirements.txt"}],
        }
        res_comment = ship._dependency_update(comment_view)
        self.assertEqual(res_comment, {
            "classification": "minor",
            "dependencies": ["foo"],
        })

    def test_each_bot_patch_and_minor_merge(self):
        self.cfg['projects']['x']['dependency_prs_daily_cap'] = 10
        n = 10
        for bot in ('dependabot', 'renovate'):
            for after in ('1.2.4', '1.3.0'):
                with self.subTest(bot=bot, after=after):
                    self.add(n, after=after, bot=bot)
                    self.run_pass()
                    self.assertEqual(self.views[n]['state'], 'MERGED')
                    n += 1
        self.assertEqual(len(self.events()), 4)

    def test_dependency_metadata_uses_account_environment(self):
        env = {"GH_CONFIG_DIR": "/test/work-account"}
        with mock.patch.object(gh_module, "_gh", return_value="{}") as call:
            gh_module.GH("work/repo", env=env).dependency_pr_view(10)
        self.assertEqual(call.call_args.kwargs['env'], env)
        self.assertEqual(call.call_args.args[:6], ('pr', 'view', '10', '-R', 'work/repo', '--json'))
        for field in ('author', 'files', 'body', 'headRefOid', 'baseRefName', 'isDraft'):
            self.assertIn(field, call.call_args.args[-1].split(','))

    def test_rejected_request_retries_after_timeout_with_fresh_gates(self):
        self.gh.pr_merge.side_effect = gh_module.GHError('rejected')
        self.run_pass()
        self.now += timedelta(minutes=61)
        self.gh.pr_merge.side_effect = self.merge
        self.gh.base_in_head.return_value = False
        self.run_pass()
        self.assertEqual(self.gh.pr_merge.call_count, 1)
        self.gh.base_in_head.return_value = True
        self.run_pass()
        self.assertEqual(self.gh.pr_merge.call_count, 2)
        self.assertEqual(len(self.events()), 1)

    def test_refusals(self):
        original = copy.deepcopy(self.views[10])
        for changes in ({'isDraft': True}, {'state': 'CLOSED'}, {'mergeable': 'CONFLICTING'},
                {'mergeable': 'UNKNOWN'}, {'statusCheckRollup': [{'state': 'FAILURE'}]},
                {'statusCheckRollup': [{'state': 'PENDING'}]}, {'statusCheckRollup': []},
                {'body': 'Bumps package to latest'}, {'body': 'Bumps package from 1 to 2.'},
                {'body': 'Bumps package from 1.2.3 to 1.3.0-beta.'},
                {'author': {'login': 'someone'}}, {'isCrossRepository': True}):
            with self.subTest(changes=changes):
                self.views[10] = {**original, **changes}
                self.run_pass()
                self.gh.pr_merge.assert_not_called()
                self.gh.request_dependency_rebase.assert_not_called()
        self.views[10] = original
        self.gh.base_in_head.return_value = None
        self.run_pass()
        self.gh.pr_merge.assert_not_called()
        self.gh.base_in_head.side_effect = gh_module.GHError('fetch failed')
        self.run_pass()
        self.gh.pr_merge.assert_not_called()
        self.gh.request_dependency_rebase.assert_not_called()

    def test_changed_exact_head_base_checks_and_metadata(self):
        original = self.views[10]
        for changes in ({'headRefOid': 'b' * 40}, {'baseRefName': 'other'},
                        {'isDraft': True}, {'body': 'unknown'},
                        {'statusCheckRollup': [{'state': 'PENDING'}]}):
            with self.subTest(changes=changes):
                self.gh.dependency_pr_view.side_effect = [original, {**original, **changes}]
                self.run_pass()
                self.gh.pr_merge.assert_not_called()
        self.gh.request_dependency_rebase.assert_not_called()

    def test_major_dedup_and_scope(self):
        self.cfg['projects']['x'].update(scope='label', scope_label='custom')
        self.add(10, after='2.0.0')
        self.run_pass()
        self.run_pass()
        self.gh.create_issue.assert_called_once()
        title, body, labels = self.gh.create_issue.call_args.args
        self.assertIn('Update package', title)
        self.assertIn('/pull/10', body)
        self.assertEqual(labels, ['type:chore', 'size:m', 'p2', 'custom'])
        self.gh.pr_merge.assert_not_called()
        self.led.set_kv('dependency-issue:x:10', None)
        self.gh.issue_by_marker.return_value = 'https://github.com/x/y/issues/123'
        self.run_pass()
        self.gh.create_issue.assert_called_once()

    def test_daily_cap_restart_next_day_override_and_idempotence(self):
        for n in range(11, 15):
            self.add(n)
        for _ in range(4):
            self.run_pass()
        self.assertEqual(self.gh.pr_merge.call_count, 3)
        self.led.close()
        self.led = Ledger(self.tmp.name + '/ledger.db', clock=lambda: self.now)
        self.ctx.led = self.led
        self.run_pass()
        self.assertEqual(self.gh.pr_merge.call_count, 3)
        detail = json.loads(self.events()[0]['detail'])
        self.led.set_kv('dependency-pending:x', json.dumps(detail))
        self.run_pass()
        self.assertEqual(len(self.events()), 3)
        self.cfg['projects']['x']['dependency_prs_daily_cap'] = 4
        self.run_pass()
        self.assertEqual(self.gh.pr_merge.call_count, 4)
        self.now += timedelta(days=1)
        self.run_pass()
        self.assertEqual(self.gh.pr_merge.call_count, 5)

    def test_opt_out_capacity_precedence_dry_run_and_one_per_tick(self):
        self.cfg['projects']['x']['dependency_prs'] = False
        self.run_pass()
        self.gh.open_prs.assert_not_called()
        self.cfg['projects']['x']['dependency_prs'] = True
        self.ctx.dry_run = True
        self.run_pass()
        self.gh.open_prs.assert_not_called()
        self.ctx.dry_run = False
        self.led.upsert_item('x', 5, state='working')
        self.led.claim('x', 5, 'session', 'interactive', 30)
        self.run_pass()
        self.gh.pr_merge.assert_not_called()
        self.led.release('x', 5)
        self.led.set_state('x', 5, 'verifying')
        self.run_pass()
        self.gh.pr_merge.assert_not_called()
        self.led.set_state('x', 5, 'ready')
        self.ctx.merge_requested = True
        ship._dependency_prs(self.ctx, 'x')
        self.gh.pr_merge.assert_not_called()
        self.add(11)
        self.run_pass()
        self.gh.pr_merge.assert_called_once()

    def test_queue_and_uncertain_response_recovery(self):
        self.gh.pr_merge.side_effect = gh_module.GHError('response lost')
        self.run_pass()
        self.assertTrue(self.ctx.merge_requested)
        self.assertFalse(self.events())
        self.run_pass()
        self.gh.pr_merge.assert_called_once()
        self.merge(10, self.views[10]['headRefOid'])
        self.run_pass()
        self.assertEqual(len(self.events()), 1)

    def test_account_client_and_project_exception_isolation(self):
        with mock.patch.object(ship, '_ship_project'), mock.patch.object(ship, '_unowned_prs'):
            self.gh.open_prs.side_effect = gh_module.GHError('unavailable')
            ship.ship(self.ctx, [{'name': 'x'}, {'name': 'other'}])
        self.assertIn(mock.call('x'), self.ctx.gh.call_args_list)
        self.assertIn(mock.call('other'), self.ctx.gh.call_args_list)

    def test_renovate_ambiguous_group_and_conflicting_type(self):
        self.add(10, bot='renovate')
        body = self.views[10]['body']
        for change in (body.replace('patch', 'minor'), body + '| other | deps | minor | unknown |\n'):
            self.views[10]['body'] = change
            self.assertIsNone(ship._dependency_update(self.views[10]))
