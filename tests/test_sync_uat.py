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
import tempfile
import unittest
from datetime import datetime, timezone
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

    def test_parent_and_dependent_unblock_only_after_evidence(self):
        self.sync()
        self.led.upsert_item("x", 10, state="parent")
        self.led.upsert_item("x", 5, parent=10)
        self.gh.close_issue = mock.Mock()
        with mock.patch.object(self.ctx, "gh", return_value=self.gh):
            sync.close_finished_parents(self.ctx, [self.ctx.policy("x")])
        self.assertEqual(self.led.item("x", 10)["state"], "parent")
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


class EvidenceGrammarTests(unittest.TestCase):
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
