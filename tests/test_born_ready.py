"""D21: children discovered during planning must not get a second sort."""

import copy
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from mahler import config, finalize, scheduler, sync, tick
from mahler.gh import GH
from mahler.ledger import Ledger, iso

NOW = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)
BODY = "Part of #5\n\n## Plan\nChange one file.\n\n## Done when\nTests pass."


class FakeGH:
    def __init__(self):
        self.issues = [{"number": 6, "title": "Child", "body": BODY,
                        "labels": [{"name": "size:s"}], "createdAt": iso(NOW)}]
        self.polls = []
        self.fetches = 0

    def issues_changed(self, etag=None):
        self.polls.append(etag)
        return etag != "cached", "cached"

    def open_issues(self):
        self.fetches += 1
        return copy.deepcopy(self.issues)

    def issue_state(self, number):
        return "OPEN"

    def blocked_by_of(self, number):
        return []


class BornReadyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["projects"]["x"] = {"path": tmp.name, "repo": "x/y"}
        cfg["defaults"]["settle_minutes"] = 0
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)
        self.ctx = scheduler.Ctx(cfg, self.led)
        self.gh = FakeGH()
        patch = mock.patch.object(self.ctx, "gh", return_value=self.gh)
        patch.start()
        self.addCleanup(patch.stop)
        self.led.upsert_item("x", 5, state="inbox", title="Planner")

    def discover_child(self):
        sync.sync(self.ctx, "x")
        self.assertEqual(self.led.item("x", 6)["state"], "inbox")

    def resync(self):
        self.led.set_kv("etag:x", None)
        sync.sync(self.ctx, "x")

    def candidates(self):
        return [(it["number"], role) for _, role, it in
                tick._candidates(self.ctx, [self.ctx.policy("x")])]

    def test_existing_child_promoted_after_parent_finishes(self):
        self.discover_child()
        self.led.set_state("x", 5, "parent")
        self.resync()
        child = self.led.item("x", 6)
        self.assertEqual((child["state"], child["sorted_at"]), ("ready", iso(NOW)))
        events = self.led.q("SELECT detail FROM events WHERE number=6 AND kind='state'")
        self.assertIn("born ready (planned under #5)", events[-1]["detail"])
        self.assertEqual(self.candidates(), [(6, "build")])
        self.assertEqual(self.led.active_runs(), [])

    def test_leased_child_is_not_promoted(self):
        self.discover_child()
        self.led.claim("x", 6, "human", "interactive", 30)
        self.led.set_state("x", 5, "parent")
        self.resync()
        self.assertEqual(self.led.item("x", 6)["state"], "inbox")
        self.assertIsNone(self.led.item("x", 6)["sorted_at"])

    def test_incomplete_children_are_not_promoted(self):
        for body, size in [(BODY.replace("## Plan", "Notes"), "s"),
                           (BODY.replace("## Done when", "Checks"), "s"),
                           (BODY, "l"), (BODY, None)]:
            with self.subTest(body=body, size=size):
                self.led.set_state("x", 5, "inbox")
                self.gh.issues[0].update(body=body, labels=[{"name": f"size:{size}"}])
                self.resync()
                self.led.set_state("x", 5, "parent")
                self.resync()
                self.assertEqual(self.led.item("x", 6)["state"], "inbox")
                self.assertIsNone(self.led.item("x", 6)["sorted_at"])

    def test_other_child_states_are_untouched(self):
        self.discover_child()
        self.led.set_state("x", 5, "parent")
        for state in ("ready", "working", "verifying", "needs_you", "parked", "done", "parent", "failed"):
            with self.subTest(state=state):
                self.led.set_state("x", 6, state, sorted_at=None)
                self.resync()
                self.assertEqual(self.led.item("x", 6)["state"], state)
                self.assertIsNone(self.led.item("x", 6)["sorted_at"])

    def test_split_forces_same_tick_fetch_and_promotion(self):
        self.discover_child()
        sync.sync(self.ctx, "x")
        self.assertEqual(self.gh.fetches, 1)  # unchanged GitHub takes the 304 path
        self.led.set_kv("etag:other", "other-cache")
        ending = finalize.Ending(self.ctx, {"project": "x", "number": 5},
                                 self.led.item("x", 5), self.ctx.policy("x"),
                                 "", "", "SPLIT", "", "", "SPLIT")
        finalize._sorted_split(ending)
        sync.sync(self.ctx, "x")
        self.assertEqual(self.gh.polls, [None, "cached", None])
        self.assertEqual(self.gh.fetches, 2)
        self.assertEqual(self.led.get_kv("etag:other"), "other-cache")
        self.assertEqual(self.candidates(), [(6, "build")])

    def test_child_waits_for_active_parent_sort_then_sorts_if_unplanned(self):
        self.gh.issues[0]["body"] = "Part of #5"
        self.discover_child()
        run = self.led.create_run(project="x", number=5, role="sort",
                                  platform="claude-opus", epoch=1, status="running")
        self.assertNotIn((6, "sort"), self.candidates())
        self.led.update_run(run, status="ended")
        self.assertIn((6, "sort"), self.candidates())

    def test_other_project_sort_or_parent_build_does_not_hold_child(self):
        self.discover_child()
        for project, role in [("other", "sort"), ("x", "build")]:
            with self.subTest(project=project, role=role):
                run = self.led.create_run(project=project, number=5, role=role,
                                          platform="codex", epoch=1, status="running")
                self.assertIn((6, "sort"), self.candidates())
                self.led.update_run(run, status="ended")

    def test_empty_etag_omits_conditional_request_header(self):
        gh = GH("x/y")
        with mock.patch.object(gh, "_gh", return_value="HTTP/2 200\n") as request:
            self.assertTrue(gh.issues_changed(None)[0])
        self.assertFalse(any("If-None-Match" in arg for arg in request.call_args.args))
