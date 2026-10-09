"""Ready to test has always shown 0 items (mahler#285).

The conductor's own merge (ship.py's _shipped) already records a UAT row
from the PR body's "Needs a human to check" section — tested in
test_ship.py. But most real merges in this repo land by hand, following
CLAUDE.md's own merge protocol, or land in the same tick sync() (which
runs before ship() every tick, per scheduler.py) notices the issue closed
before ship.py's own watch gets to it. Either way, sync.py's "closed on
GitHub" fallback used to mark the item done without ever looking at the
merged PR, so the UAT queue never got the entry — Ready to test stayed
empty no matter how many shipped issues carried a checklist.
"""

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler import config, gh as gh_module, scheduler, sync
from mahler.ledger import Ledger, iso

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)

NEEDS_BODY = ("<!-- mahler:agent -->\nFixes #5\n\nwired the exporter\n\n"
              "## Needs a human to check\n- the new ping arrives\n")
PLAIN_BODY = "<!-- mahler:agent -->\nFixes #5\n\nplain ship, nothing to check.\n"


class FakeGH:
    """Only what sync()'s closed-issue path touches: the issue is already
    gone from open_issues() (closed), issue_state() reports CLOSED, and
    pr_view() answers as gh.pr_view would for the merged PR."""

    def __init__(self):
        self.state = "CLOSED"
        self.labels = ["mahler:verifying", "type:feature"]
        self.pr_state = "MERGED"
        self.pr_body = NEEDS_BODY
        self.merge_sha = "4c1f0abfeed5"
        self.fail_pr_view = False
        self.comments = []

    def issue_comments(self, number):
        return self.comments

    def comment(self, number, body):
        pass

    def issue_labels(self, number):
        return self.labels

    def set_state_label(self, number, state, current):
        self.labels = [x for x in current if x not in gh_module.LABEL_STATES]
        if state in gh_module.STATE_LABELS:
            self.labels.append(gh_module.STATE_LABELS[state])

    def open_issues(self):
        return []

    def issues_changed(self, etag=None):
        return (True, None)

    def issue_state(self, number):
        return self.state

    def pr_view(self, number):
        if self.fail_pr_view:
            raise gh_module.GHError("github down")
        return {"state": self.pr_state, "body": self.pr_body,
                "mergeCommit": {"oid": self.merge_sha}}


class ClosedOnGitHubUATTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["projects"]["x"] = {"path": tmp.name, "repo": "x/y"}
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)
        self.led.upsert_item("x", 5, title="Wired the exporter", state="verifying", pr=88)
        self.ctx = scheduler.Ctx(self.cfg, self.led)
        self.gh = FakeGH()

    def sync(self):
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), mock.patch.object(self.ctx, "ping"):
            sync.sync(self.ctx, "x")

    def test_hand_merged_pr_with_needs_human_lands_in_uat_queue(self):
        self.sync()
        self.assertEqual(self.led.item("x", 5)["state"], "shipped")
        self.assertEqual(self.gh.labels, ["type:feature", "mahler:shipped"])
        self.sync()
        self.assertEqual(self.led.item("x", 5)["state"], "shipped")
        self.assertEqual(len(self.led.q("SELECT * FROM events WHERE kind='shipped'")), 1)
        row = self.led.uat("x", 5)
        self.assertIsNotNone(row)
        self.assertEqual((row["pr"], row["sha"], row["title"]),
                         (88, "4c1f0abfeed5", "Wired the exporter"))
        self.assertEqual(row["needs"], "- the new ping arrives")
        self.assertIsNone(row["verdict"])

    def test_hand_merged_pr_without_needs_human_gets_default_check(self):
        self.gh.pr_body = PLAIN_BODY
        self.sync()
        self.assertEqual(self.led.item("x", 5)["state"], "shipped")
        self.assertIn("passing evidence", self.led.uat("x", 5)["needs"])

    def test_unmerged_manual_closure_is_not_shipped(self):
        for state in ("OPEN", "CLOSED"):
            with self.subTest(state=state):
                self.led.set_state("x", 5, "verifying")
                self.gh.pr_state = state
                self.sync()
                self.assertEqual(self.led.item("x", 5)["state"], "done")
                self.assertNotIn("mahler:shipped", self.gh.labels)
                self.assertIsNone(self.led.uat("x", 5))
                self.assertEqual(self.led.unreleased_items("x"), [])
                self.assertEqual(self.led.q("SELECT * FROM events WHERE kind='shipped'"), [])

    def test_closed_without_a_pr_never_looks_one_up(self):
        self.led.upsert_item("x", 5, pr=None)
        with mock.patch.object(self.gh, "pr_view",
                               side_effect=AssertionError("pr_view should not be called")):
            self.sync()
        self.assertEqual(self.led.item("x", 5)["state"], "done")
        self.assertIsNone(self.led.uat("x", 5))

    def test_pr_lookup_failure_retries_without_losing_shipment(self):
        self.gh.fail_pr_view = True
        self.sync()   # must not raise
        self.assertEqual(self.led.item("x", 5)["state"], "verifying")
        self.assertIsNone(self.led.uat("x", 5))


    def test_evidence_closed_poll_even_on_304_is_idempotent(self):
        self.sync()
        self.gh.comments = [dict(body="Verified: exporter works", author={"login": "owner"},
                                 authorAssociation="OWNER", id=10, url="https://example/comment/10",
                                 createdAt="2026-09-15T12:01:00Z")]
        self.gh.issues_changed = lambda etag: (False, None)
        self.sync()
        self.sync()
        self.assertEqual(self.led.item("x", 5)["state"], "done")
        self.assertEqual(self.gh.labels, ["type:feature"])
        self.assertEqual(len(self.led.q("SELECT * FROM completion_evidence")), 1)
        self.assertEqual(len(self.led.q("SELECT * FROM events WHERE kind='uat_verdict'")), 1)
        self.assertEqual(self.gh.state, "CLOSED")

    def test_parent_completes_before_child_post_merge_evidence(self):
        self.sync()
        self.led.upsert_item("x", 10, state="parent")
        self.led.upsert_item("x", 5, parent=10)
        self.gh.close_issue = mock.Mock()
        self.gh.parent_issue_inventory = mock.Mock(return_value=[])
        self.gh.sub_issues = mock.Mock(return_value=[])
        self.gh.issue_body = mock.Mock(return_value="## Done when\n- [x] Works")
        self.gh.pr_merge_info = mock.Mock(return_value={"state": "MERGED"})
        with mock.patch.object(self.ctx, "gh", return_value=self.gh):
            sync.close_finished_parents(self.ctx, [self.ctx.policy("x")])
        self.assertEqual(self.led.item("x", 10)["state"], "done")
        self.assertEqual(self.led.item("x", 5)["state"], "shipped")
        self.gh.comments = [dict(body="Verified: works", author={"login": "owner"},
                                 authorAssociation="OWNER", id=12,
                                 createdAt="2026-09-15T12:01:00Z")]
        with mock.patch.object(self.ctx, "gh", return_value=self.gh):
            sync.reconcile_shipped(self.ctx, "x")
            sync.close_finished_parents(self.ctx, [self.ctx.policy("x")])
        self.assertEqual(self.led.item("x", 10)["state"], "done")

    def test_label_failure_retries_after_local_completion(self):
        self.sync()
        self.gh.comments = [dict(body="Smoke: PASS tag=v1.2", author={"login": "bot"},
                                 id=11, createdAt="2026-09-15T12:01:00Z")]
        with mock.patch.object(self.gh, "set_state_label", side_effect=gh_module.GHError("offline")):
            self.sync()
        self.assertEqual(self.led.item("x", 5)["state"], "done")
        self.assertIn("mahler:shipped", self.gh.labels)
        self.sync()
        self.assertNotIn("mahler:shipped", self.gh.labels)

    def test_done_label_retries_when_shipped_write_succeeded_ambiguously(self):
        real_set_state_label = self.gh.set_state_label

        def set_then_fail(*args):
            real_set_state_label(*args)
            raise gh_module.GHError("response lost")

        with mock.patch.object(self.gh, "set_state_label", side_effect=set_then_fail):
            self.sync()
        self.assertEqual(self.led.item("x", 5)["state"], "shipped")
        self.assertIsNone(self.led.item("x", 5)["mirror"])
        self.assertIn("mahler:shipped", self.gh.labels)

        self.gh.comments = [dict(body="Smoke: PASS tag=v1.2", author={"login": "bot"},
                                 id=11, createdAt="2026-09-15T12:01:00Z")]
        self.sync()
        self.assertEqual(self.led.item("x", 5)["state"], "done")
        self.assertIsNone(self.led.item("x", 5)["mirror"])
        self.assertNotIn("mahler:shipped", self.gh.labels)


class QuietWindowTests(unittest.TestCase):
    """A shipment completes on smoke evidence or 14x24h after its merge."""

    MERGED = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["projects"]["x"] = {"path": tmp.name, "repo": "x/y"}
        self.now = self.MERGED
        self.led = Ledger(":memory:", clock=lambda: self.now)
        self.addCleanup(self.led.close)
        self.ctx = scheduler.Ctx(self.cfg, self.led)
        self.gh = FakeGH()
        self.gh.opened = []
        self.gh.priorities = []
        self.gh.reopen_issue = lambda n: (self.gh.opened.append(n), setattr(self.gh, "state", "OPEN"))
        self.gh.set_priority_label = lambda n, p, cur: self.gh.priorities.append(p)
        self.gh.comment = mock.Mock()
        gh_patch = mock.patch.object(self.ctx, "gh", return_value=self.gh)
        gh_patch.start()
        self.addCleanup(gh_patch.stop)
        self.gh.issue_reopens = lambda n: [iso(self.now)]
        self.gh.pr_merged_at = iso(self.MERGED)
        self.pr_view = self.gh.pr_view
        self.gh.pr_view = lambda n: dict(self.pr_view(n), mergedAt=self.gh.pr_merged_at)
        self.led.upsert_item("x", 5, title="Wired", state="shipped", pr=88,
                             mirror="mahler:shipped")
        self.led.add_uat("x", 5, 88, "abc", "Wired", "- check", shipped_at=iso(self.MERGED))

    def reconcile(self):
        with mock.patch.object(self.ctx, "gh", return_value=self.gh):
            sync.reconcile_shipped(self.ctx, "x")

    def after(self, **delta):
        self.now = self.MERGED + timedelta(**delta)
        self.reconcile()

    def state(self):
        return self.led.item("x", 5)["state"]

    def bug(self, number, created, body="Regression of #5", labels=("type:bug",)):
        self.led.upsert_item("x", number, state="ready", issue_body=body,
                             labels=json.dumps(list(labels)), created_at=iso(created))

    def test_boundary_waits_then_completes_once_without_comment(self):
        self.after(days=13, hours=23, minutes=59)
        self.assertEqual(self.state(), "shipped")
        self.after(days=14)
        self.assertEqual(self.state(), "done")
        self.after(days=15)
        self.after(days=16)
        ev = self.led.q("SELECT * FROM completion_evidence")
        self.assertEqual((len(ev), ev[0]["kind"], ev[0]["body"]),
                         (1, "quiet", "quiet period, no defect reported"))
        self.assertEqual(len(self.led.q("SELECT * FROM events WHERE kind='uat_verdict'")), 1)
        self.gh.comment.assert_not_called()
        self.assertEqual(self.gh.labels, ["type:feature"])

    def test_uses_merge_time_not_discovery_time(self):
        self.led.con.execute("UPDATE uat SET shipped_at=?", (iso(self.MERGED - timedelta(days=30)),))
        self.reconcile()
        self.assertEqual(self.state(), "done")

    def test_sync_ingests_last_interval_bug_before_quiet_completion(self):
        for ref in (5, 88):
            with self.subTest(ref=ref):
                self.led.con.execute("DELETE FROM items WHERE number=20")
                self.now = self.MERGED + timedelta(days=14)
                bug = dict(number=20, title="Regression", body=f"Broke #{ref}",
                           labels=[{"name": "type:bug"}], comments=[],
                           createdAt=iso(self.now - timedelta(seconds=30)))
                with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                        mock.patch.object(self.gh, "open_issues", return_value=[bug]), \
                        mock.patch.object(self.gh, "blocked_by_of", return_value=[], create=True):
                    sync.sync(self.ctx, "x")
                    sync.sync(self.ctx, "x")
                self.assertIsNotNone(self.led.item("x", 20))
                self.assertEqual(self.state(), "shipped")
                self.assertEqual(self.led.q("SELECT * FROM completion_evidence"), [])

    def test_quiet_completion_still_runs_on_unchanged_poll(self):
        self.now = self.MERGED + timedelta(days=14)
        self.led.set_kv("depends_format:x", "4")
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(self.gh, "issues_changed", return_value=(False, None)), \
                mock.patch.object(self.gh, "open_issues") as fetch:
            sync.sync(self.ctx, "x")
        fetch.assert_not_called()
        self.assertEqual(self.state(), "done")

    def test_failed_issue_fetch_does_not_complete_from_stale_evidence(self):
        self.now = self.MERGED + timedelta(days=14)
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(self.gh, "open_issues", side_effect=gh_module.GHError("offline")):
            with self.assertRaises(gh_module.GHError):
                sync.sync(self.ctx, "x")
        self.assertEqual(self.state(), "shipped")

    def test_legacy_rows_old_complete_young_wait_in_bounded_batches(self):
        for n in range(10, 10 + sync.QUIET_BATCH + 3):
            self.led.upsert_item("x", n, state="shipped", mirror="mahler:shipped")
            self.led.add_uat("x", n, None, "s", "t", "n",
                             shipped_at=iso(self.MERGED - timedelta(days=20)))
        young = 99
        self.led.upsert_item("x", young, state="shipped", mirror="mahler:shipped")
        self.led.add_uat("x", young, None, "s", "t", "n", shipped_at=iso(self.MERGED))
        self.reconcile()
        done = [i["number"] for i in self.led.items("x", ["done"])]
        self.assertEqual(len(done), sync.QUIET_BATCH)
        self.reconcile()
        self.assertEqual(self.led.item("x", young)["state"], "shipped")
        self.assertEqual(len(self.led.items("x", ["done"])), sync.QUIET_BATCH + 3)
        self.assertEqual(self.state(), "shipped")

    def test_adverse_evidence_blocks_quiet_completion(self):
        inside = self.MERGED + timedelta(days=2)
        cases = {
            "issue link": lambda: self.bug(20, inside),
            "pr link": lambda: self.bug(20, inside, body="Broke by PR #88"),
            "revert": lambda: self.led.event("revert_requested", "x", 5, {"pr": 88}),
            "reopen": lambda: self.led.event("source_reopened", "x", 5, {}),
        }
        for name, make in cases.items():
            with self.subTest(name):
                self.led.con.execute("DELETE FROM events")
                self.led.con.execute("DELETE FROM items WHERE number=20")
                if name in ("revert", "reopen"):
                    self.now = inside
                make()
                self.after(days=30)
                self.assertEqual(self.state(), "shipped")

    def test_unlinked_late_or_non_bug_items_do_not_block(self):
        self.bug(20, self.MERGED + timedelta(days=1), body="mentions #50 and x#5 and #55")
        self.bug(21, self.MERGED + timedelta(days=15))
        self.bug(22, self.MERGED + timedelta(days=1), labels=("type:feature",))
        self.after(days=16)
        self.assertEqual(self.state(), "done")

    def test_source_reopen_detected_from_open_issue(self):
        item = self.led.item("x", 5)
        self.now = self.MERGED + timedelta(days=1)
        sync._note_source_reopen(self.ctx, "x", item)
        sync._note_source_reopen(self.ctx, "x", item)
        self.assertEqual(len(self.led.q("SELECT * FROM events WHERE kind='source_reopened'")), 1)
        self.after(days=20)
        self.assertEqual(self.state(), "shipped")

    def test_reopen_updated_before_boundary_blocks_late_poll_completion(self):
        reopened_at = self.MERGED + timedelta(days=13, hours=23, minutes=59)
        self.now = self.MERGED + timedelta(days=14, minutes=1)
        issue = dict(number=5, title="Wired", body="",
                     labels=[{"name": "mahler:shipped"}], comments=[],
                     createdAt=iso(self.MERGED), updatedAt=iso(self.now))
        self.gh.issue_reopens = lambda n: [iso(reopened_at)]
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(self.gh, "open_issues", return_value=[issue]), \
                mock.patch.object(self.gh, "blocked_by_of", return_value=[], create=True):
            sync.sync(self.ctx, "x")

        event = self.led.q("SELECT at FROM events WHERE kind='source_reopened'")[0]
        self.assertEqual(event["at"], iso(reopened_at))
        self.assertEqual(self.state(), "shipped")
        self.assertEqual(self.led.q("SELECT * FROM completion_evidence"), [])

    def test_source_reopen_detected_when_shipped_label_write_failed(self):
        self.led.upsert_item("x", 5, mirror=None)
        item = self.led.item("x", 5)
        self.now = self.MERGED + timedelta(days=1)
        sync._note_source_reopen(self.ctx, "x", item)   # closure never confirmed
        self.assertEqual(self.led.q("SELECT * FROM events WHERE kind='source_reopened'"), [])
        self.led.set_kv("shipped_closed:x:5", "1")      # closed, label write failed
        sync._note_source_reopen(self.ctx, "x", item)
        self.assertEqual(len(self.led.q("SELECT * FROM events WHERE kind='source_reopened'")), 1)
        self.after(days=20)
        self.assertEqual(self.state(), "shipped")

    def smoke(self, at):
        self.gh.comments = [dict(body="Smoke: PASS tag=v1", author={"login": "bot"},
                                 id=1, createdAt=iso(at))]

    def test_smoke_completes_immediately_with_attributable_evidence(self):
        self.smoke(self.MERGED + timedelta(minutes=5))
        self.after(hours=1)
        self.assertEqual(self.state(), "done")
        ev = self.led.q("SELECT * FROM completion_evidence")[0]
        self.assertEqual((ev["kind"], ev["author"]), ("smoke", "bot"))

    def test_smoke_after_failed_close_is_not_a_source_reopen(self):
        self.led.upsert_item("x", 5, mirror=None)
        self.gh.state = "OPEN"
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(self.gh, "close_issue", create=True,
                                  side_effect=gh_module.GHError("offline")) as close:
            sync.mirror_shipped(self.ctx, "x", self.led.item("x", 5))
        close.assert_called_once_with(5)
        self.assertIsNone(self.led.get_kv("shipped_closed:x:5"))
        self.smoke(self.MERGED + timedelta(minutes=5))
        self.after(hours=1)
        self.assertEqual(self.state(), "done")

        issue = dict(number=5, title="Wired", body="", labels=[], comments=[],
                     createdAt=iso(self.MERGED))
        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
                mock.patch.object(self.gh, "open_issues", return_value=[issue]), \
                mock.patch.object(self.gh, "blocked_by_of", return_value=[], create=True):
            sync.sync(self.ctx, "x")
            sync.sync(self.ctx, "x")
        self.assertEqual(self.state(), "done")
        self.assertEqual(self.led.q("SELECT * FROM events WHERE kind IN "
                                    "('source_reopened', 'shipment_reopened')"), [])
        self.assertEqual(self.gh.priorities, [])
        self.assertEqual(len(self.led.q("SELECT * FROM completion_evidence")), 1)

    def test_source_reopen_after_smoke_uses_retained_closure_confirmation(self):
        with mock.patch.object(self.ctx, "gh", return_value=self.gh):
            sync.mirror_shipped(self.ctx, "x", self.led.item("x", 5))
        self.smoke(self.MERGED + timedelta(minutes=5))
        self.after(hours=1)
        self.assertIsNone(self.led.item("x", 5)["mirror"])
        self.gh.state = "OPEN"
        sync._note_source_reopen(self.ctx, "x", self.led.item("x", 5))
        self.reconcile()
        self.assertEqual(self.state(), "ready")
        self.assertEqual(self.gh.priorities, [1])
        self.assertEqual(len(self.led.q("SELECT * FROM events WHERE kind='source_reopened'")), 1)

    def test_boundary_smoke_wins_after_transient_comment_fetch_failure(self):
        self.now = self.MERGED + timedelta(days=13, hours=23, minutes=59)
        with mock.patch.object(self.gh, "issue_comments",
                               side_effect=gh_module.GHError("offline")):
            self.reconcile()
        self.assertEqual(self.state(), "shipped")

        self.smoke(self.now + timedelta(seconds=30))
        self.after(days=14)

        ev = self.led.q("SELECT * FROM completion_evidence")[0]
        self.assertEqual((self.state(), ev["kind"], ev["author"]),
                         ("done", "smoke", "bot"))

    def test_defect_inside_window_after_smoke_reopens_once(self):
        self.smoke(self.MERGED + timedelta(minutes=5))
        self.after(hours=1)
        self.bug(20, self.MERGED + timedelta(days=3))
        self.after(days=4)
        self.after(days=4, hours=1)
        item = self.led.item("x", 5)
        self.assertEqual((item["state"], item["priority"]), ("ready", 1))
        self.assertEqual(self.gh.priorities, [1])
        self.assertEqual(self.gh.opened, [5])
        self.assertEqual(self.led.get_kv("reopen_mirror:x:5"), "done")
        self.assertEqual(len(self.led.q("SELECT * FROM events WHERE kind='shipment_reopened'")), 1)
        self.assertEqual(len(self.led.q("SELECT * FROM completion_evidence")), 1)
        self.led.set_state("x", 5, "done", "again")
        self.after(days=5)
        self.assertEqual(len(self.led.q("SELECT * FROM events WHERE kind='shipment_reopened'")), 1)

    def test_replacement_shipment_requires_fresh_evidence_and_can_reopen(self):
        self.smoke(self.MERGED + timedelta(minutes=5))
        self.after(hours=1)
        self.bug(20, self.MERGED + timedelta(days=3))
        self.after(days=4)
        self.assertEqual(self.state(), "ready")
        replacement = self.MERGED + timedelta(days=5)
        self.led.add_uat("x", 5, 99, "replacement", "Fixed", "- check",
                         shipped_at=iso(replacement))
        self.led.upsert_item("x", 5, state="shipped", pr=99)
        self.after(days=6)
        self.assertEqual(self.state(), "shipped")  # old smoke is before this merge
        self.assertIsNone(self.led.uat("x", 5)["verdict"])
        self.smoke(replacement + timedelta(hours=1))
        self.after(days=7)
        self.assertEqual(self.state(), "done")
        self.bug(21, replacement + timedelta(days=3), body="Regression of #99")
        self.after(days=9)
        self.assertEqual(self.state(), "ready")
        self.assertEqual(len(self.led.q("SELECT * FROM events WHERE kind='shipment_reopened'")), 2)

    def test_replacement_gets_its_own_quiet_window(self):
        self.after(days=14)
        replacement = self.MERGED + timedelta(days=20)
        self.led.add_uat("x", 5, 99, "replacement", "Fixed", "- check",
                         shipped_at=iso(replacement))
        self.led.upsert_item("x", 5, state="shipped", pr=99)
        self.after(days=33, hours=23, minutes=59)
        self.assertEqual(self.state(), "shipped")
        self.after(days=34)
        self.assertEqual(self.state(), "done")
        evidence = self.led.q("SELECT * FROM completion_evidence")[0]
        self.assertEqual(evidence["created_at"], iso(replacement + timedelta(days=14)))

    def test_reopen_fetch_failure_retries_without_quiet_completion(self):
        self.now = self.MERGED + timedelta(days=14, minutes=1)
        issue = dict(number=5, title="Wired", body="", labels=[], comments=[],
                     createdAt=iso(self.MERGED), updatedAt=iso(self.now))
        with mock.patch.object(self.gh, "open_issues", return_value=[issue]), \
                mock.patch.object(self.gh, "blocked_by_of", return_value=[], create=True):
            with mock.patch.object(self.gh, "issue_reopens", side_effect=gh_module.GHError("offline")):
                with self.assertRaises(gh_module.GHError):
                    sync.sync(self.ctx, "x")
            self.assertEqual(self.state(), "shipped")
            self.assertIsNone(self.led.get_kv("source_reopened:x:5"))
            self.gh.issue_reopens = lambda n: [iso(self.MERGED + timedelta(days=13))]
            sync.sync(self.ctx, "x")
        self.assertEqual(self.state(), "shipped")

    def test_defect_after_window_does_not_undo_completions(self):
        self.smoke(self.MERGED + timedelta(minutes=5))
        self.after(hours=1)
        self.bug(20, self.MERGED + timedelta(days=15))
        self.after(days=16)
        self.assertEqual(self.state(), "done")

    def test_defect_after_quiet_completion_never_reopens(self):
        self.after(days=14)
        self.bug(20, self.MERGED + timedelta(days=14, hours=1))
        self.after(days=20)
        self.assertEqual(self.state(), "done")

    def test_reopen_label_failure_retries(self):
        self.smoke(self.MERGED + timedelta(minutes=5))
        self.after(hours=1)
        self.bug(20, self.MERGED + timedelta(days=3))
        with mock.patch.object(self.gh, "set_state_label", side_effect=gh_module.GHError("x")):
            self.after(days=4)
        self.assertEqual(self.state(), "ready")
        self.assertEqual(self.led.get_kv("reopen_mirror:x:5"), "pending")
        self.after(days=4, hours=1)
        self.assertEqual(self.led.get_kv("reopen_mirror:x:5"), "done")

    def test_done_label_retry_after_quiet_completion(self):
        self.gh.labels = ["mahler:shipped", "type:feature"]
        with mock.patch.object(self.gh, "set_state_label", side_effect=gh_module.GHError("x")):
            self.after(days=14)
        self.assertEqual(self.state(), "done")
        self.assertIn("mahler:shipped", self.gh.labels)
        self.after(days=14, hours=1)
        self.assertNotIn("mahler:shipped", self.gh.labels)


class EvidenceGrammarTests(unittest.TestCase):
    def test_reopen_fetch_uses_event_times_across_pages(self):
        gh = gh_module.GH("owner/repo")
        pages = [[dict(event="closed", created_at="old")],
                 [dict(event="reopened", created_at="actual")]]
        with mock.patch.object(gh, "_gh", return_value=json.dumps(pages)) as call:
            self.assertEqual(gh.issue_reopens(5), ["actual"])
        self.assertIn("--paginate", call.call_args.args)
        self.assertIn("repos/owner/repo/issues/5/events?per_page=100", call.call_args.args)

    def test_comment_fetch_preserves_attribution_across_pages(self):
        gh = gh_module.GH("owner/repo")
        comment = dict(body="Verified: works", user={"login": "owner"},
                       author_association="OWNER", id=123,
                       created_at="2026-09-15T12:01:00Z", html_url="https://example/123")
        with mock.patch.object(gh, "_gh", return_value=json.dumps([[], [comment]])) as call:
            comments = gh.issue_comments(5)
        self.assertIn("--paginate", call.call_args.args)
        evidence = gh_module.completion_evidence(comments[0], iso(NOW))
        self.assertEqual(evidence["author"], "owner")
        self.assertEqual(evidence["source"], "https://example/123")

    def test_accepted_and_rejected_forms_and_metadata(self):
        base = dict(author={"login": "mike"}, authorAssociation="OWNER", id=123,
                    createdAt="2026-09-15T12:01:00Z")
        for body in (gh_module.UAT_PASS_COMMENT, "Verified: works on my phone",
                     "Smoke: PASS https://example/report", "Smoke: PASS tag=v1.2",
                     "Smoke: PASS artifact=build-123", "Smoke: PASS report=smoke.txt"):
            with self.subTest(body=body):
                self.assertIsNotNone(gh_module.completion_evidence(dict(base, body=body), iso(NOW)))
        for body in ("looks good", "Verified:", "Smoke: PASS", "Smoke: PASS looks good",
                     "Smoke: FAIL report=smoke.txt", "STATUS: DONE all passed",
                     "❌ **UAT failed** — filed #42.", "<!-- mahler:agent -->\nVerified: works"):
            with self.subTest(body=body):
                self.assertIsNone(gh_module.completion_evidence(dict(base, body=body), iso(NOW)))
        for change in (dict(author={}), dict(id=None), dict(createdAt=iso(NOW)),
                       dict(createdAt="2026-09-14T00:00:00Z"), dict(createdAt="bad"),
                       dict(authorAssociation="MEMBER")):
            self.assertIsNone(gh_module.completion_evidence(
                dict(base, body="Verified: works", **change), iso(NOW)))


if __name__ == "__main__":
    unittest.main()
