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
        self.pr_body = NEEDS_BODY
        self.merge_sha = "4c1f0abfeed5"
        self.fail_pr_view = False

    def open_issues(self):
        return []

    def issues_changed(self, etag=None):
        return (True, None)

    def issue_state(self, number):
        return self.state

    def pr_view(self, number):
        if self.fail_pr_view:
            raise gh_module.GHError("github down")
        return {"state": "MERGED", "body": self.pr_body,
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
        with mock.patch.object(self.ctx, "gh", return_value=self.gh):
            sync.sync(self.ctx, "x")

    def test_hand_merged_pr_with_needs_human_lands_in_uat_queue(self):
        self.sync()
        self.assertEqual(self.led.item("x", 5)["state"], "done")
        row = self.led.uat("x", 5)
        self.assertIsNotNone(row)
        self.assertEqual((row["pr"], row["sha"], row["title"]),
                         (88, "4c1f0abfeed5", "Wired the exporter"))
        self.assertEqual(row["needs"], "- the new ping arrives")
        self.assertIsNone(row["verdict"])

    def test_hand_merged_pr_without_needs_human_skips_the_uat_queue(self):
        self.gh.pr_body = PLAIN_BODY
        self.sync()
        self.assertEqual(self.led.item("x", 5)["state"], "done")
        self.assertIsNone(self.led.uat("x", 5))

    def test_closed_without_a_pr_never_looks_one_up(self):
        self.led.upsert_item("x", 5, pr=None)
        with mock.patch.object(self.gh, "pr_view",
                               side_effect=AssertionError("pr_view should not be called")):
            self.sync()
        self.assertEqual(self.led.item("x", 5)["state"], "done")
        self.assertIsNone(self.led.uat("x", 5))

    def test_pr_lookup_failure_still_marks_the_item_done(self):
        self.gh.fail_pr_view = True
        self.sync()   # must not raise
        self.assertEqual(self.led.item("x", 5)["state"], "done")
        self.assertIsNone(self.led.uat("x", 5))


if __name__ == "__main__":
    unittest.main()
