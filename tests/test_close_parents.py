"""Tests for closing parent issues when all sub-issues are done."""

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler import config, scheduler, sync, tick
from mahler.ledger import Ledger, iso
from mahler.gh import GHError

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


def proj(**over):
    pol = {"name": "mahler", "enabled": True, "repo": "mkny13/mahler", "path": "/tmp"}
    pol.update(over)
    return pol


class CloseFinishedParentsTests(unittest.TestCase):
    def setUp(self):
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)
        self.cfg = {"defaults": {}, "projects": {"mahler": proj()}}
        self.ctx = scheduler.Ctx(self.cfg, self.led)
        self.ctx.ping = mock.Mock()
        self.gh = mock.Mock()
        self.ctx._gh["mkny13/mahler"] = self.gh
        self.bodies = {10: "## Done when\n- [x] Works"}
        self.states = {10: "OPEN", 11: "CLOSED", 12: "CLOSED"}
        self.comments = {}
        self.gh.issue_body.side_effect = lambda n: self.bodies[n]
        self.remote = []
        self.next_issue = 30
        self.gh.create_issue.side_effect = self.fake_create
        self.gh.edit_issue_body.side_effect = self.fake_edit
        self.gh.issue_state.side_effect = lambda n: self.states[n]
        self.gh.issue_comments.side_effect = lambda n: self.comments.get(n, [])
        self.gh.comment.side_effect = lambda n, body: self.comments.setdefault(n, []).append({"body": body})
        self.gh.close_issue.side_effect = lambda n: self.states.update({n: "CLOSED"})
        self.gh.parent_issue_inventory.return_value = self.remote
        self.gh.sub_issues.return_value = []
        self.gh._parent_pages.return_value = []
        self.gh.closing_prs.return_value = [{"number": 99, "state": "MERGED"}]
        self.gh.pr_merge_info.return_value = {"state": "MERGED"}
        self.led.upsert_item("mahler", 10, state="parent")
        self.led.upsert_item("mahler", 11, parent=10, state="shipped", pr=99)

    def fake_create(self, title, body, labels=()):
        n = self.next_issue
        self.next_issue += 1
        self.remote.append({"number": n, "state": "open", "title": title, "body": body,
                            "labels": [{"name": l} for l in labels],
                            "html_url": f"https://github.com/mkny13/mahler/issues/{n}"})
        return f"https://github.com/mkny13/mahler/issues/{n}"

    def fake_edit(self, n, body):
        self.bodies[n] = body
        for row in self.remote:
            if row["number"] == n:
                row["body"] = body

    def add_remote(self, n, state="open", body="sorted plan", parent=10, title=None, label=True):
        row = {"number": n, "state": state, "body": body,
               "title": title or f"Verify remaining completion criteria for #{parent}",
               "labels": [{"name": "pass:verify"}] if label else [],
               "html_url": f"https://github.com/mkny13/mahler/issues/{n}"}
        self.remote.append(row)
        return row

    def run_pass(self):
        sync.close_finished_parents(self.ctx, [proj()])

    def test_merged_shipped_child_completes_checked_parent(self):
        before = dict(self.led.item("mahler", 11))
        self.run_pass()
        self.assertEqual(self.led.item("mahler", 10)["state"], "done")
        self.assertEqual(dict(self.led.item("mahler", 11)), before)
        self.assertIn("#11: merged PR #99", self.comments[10][0]["body"])
        self.run_pass()
        self.gh.comment.assert_called_once()
        self.assertEqual(len(self.led.q("SELECT * FROM events WHERE number=10 AND kind='state'")), 1)

    def test_child_criteria_tick_and_close_in_one_pass(self):
        for criterion in ("#11 closed", "#11 is merged", "#11 merged"):
            with self.subTest(criterion=criterion):
                self.setUp()
                self.bodies[10] = f"## Done when\n- [ ] {criterion}\n"
                self.run_pass()
                self.assertEqual(self.bodies[10], f"## Done when\n- [x] {criterion}\n")
                self.assertEqual(self.led.get_kv("parent_tick:mahler:10#11"), "1")
                self.assertEqual(self.led.item("mahler", 10)["state"], "done")
                self.ctx.ping.assert_not_called()
                self.gh.create_issue.assert_not_called()
                self.run_pass()
                self.gh.edit_issue_body.assert_called_once()
                self.gh.comment.assert_called_once()

    def test_mixed_criteria_tick_and_file_one_verify_issue(self):
        self.bodies[10] = "## Done when\n- [ ] #11 closed\n- [ ] approve security review"
        self.run_pass()
        self.assertIn("- [x] #11 closed", self.bodies[10])
        self.assertIn("- [ ] approve security review", self.bodies[10])
        self.gh.close_issue.assert_not_called()
        self.run_pass()
        self.gh.edit_issue_body.assert_called_once()
        self.gh.comment.assert_called_once()
        self.ctx.ping.assert_called_once()
        self.gh.create_issue.assert_called_once()
        _, body, labels = self.gh.create_issue.call_args.args
        self.assertIn("approve security review", body)
        self.assertIn("pass:verify", labels)
        # A different merged PR must not change the free-text dedupe identity.
        self.led.upsert_item("mahler", 11, pr=100)
        self.run_pass()
        self.gh.create_issue.assert_called_once()
        self.ctx.ping.assert_called_once()
        self.gh.comment.assert_called_once()

    def test_child_criterion_requires_closed_merged_relationship(self):
        for condition in ("open", "unmerged", "unrelated", "unknown"):
            with self.subTest(condition=condition):
                self.setUp()
                self.bodies[10] = "## Done when\n- [ ] #11 closed"
                if condition == "open":
                    self.states[11] = "OPEN"
                elif condition == "unknown":
                    self.states[11] = "UNKNOWN"
                elif condition == "unrelated":
                    self.led.upsert_item("mahler", 11, parent=None)
                else:
                    self.gh.pr_merge_info.return_value = {"state": "CLOSED"}
                    self.gh.closing_prs.return_value = []
                self.run_pass()
                self.gh.edit_issue_body.assert_not_called()
                self.gh.close_issue.assert_not_called()
                if condition == "unmerged":
                    self.gh.create_issue.assert_called_once()
                else:
                    self.gh.create_issue.assert_not_called()

    def test_ticks_preserve_other_sections_quotes_and_fences(self):
        self.bodies[10] = ("## Context\n- [ ] #11 closed\n## Done when\n"
                           "> - [ ] #11 closed\n```\n- [ ] #11 closed\n```\n"
                           "  * [ ] #11 closed\n")
        expected = self.bodies[10].replace("  * [ ]", "  * [x]")
        self.run_pass()
        self.assertEqual(self.bodies[10], expected)
        self.gh.close_issue.assert_called_once()

    def test_qualified_or_extended_criteria_are_not_ticked(self):
        for criterion in ("other/repo#11 closed", "#11 closed and deployed"):
            with self.subTest(criterion=criterion):
                self.setUp()
                self.bodies[10] = f"## Done when\n- [ ] {criterion}"
                self.run_pass()
                self.gh.edit_issue_body.assert_not_called()
                self.gh.close_issue.assert_not_called()

    def test_body_edit_failure_and_readback_fail_closed(self):
        self.bodies[10] = "## Done when\n- [ ] #11 closed"
        with mock.patch.object(self.gh, "edit_issue_body", side_effect=GHError("offline")):
            self.run_pass()
        self.gh.close_issue.assert_not_called()
        self.assertIsNone(self.led.get_kv("parent_tick:mahler:10#11"))
        # An edit that is not reflected in the readback cannot prove completion.
        with mock.patch.object(self.gh, "edit_issue_body"):
            self.run_pass()
        self.gh.close_issue.assert_not_called()
        self.run_pass()
        self.gh.close_issue.assert_called_once()

    FREE = "## Done when\n- [ ] approve security review"

    def test_ambiguous_verify_creation_reuses_open_item(self):
        self.bodies[10] = self.FREE
        real = self.fake_create
        def created_but_response_lost(*args):
            real(*args)
            raise GHError("response lost")
        self.gh.create_issue.side_effect = created_but_response_lost
        self.run_pass()
        self.run_pass()
        self.gh.create_issue.assert_called_once()
        self.assertEqual(len(self.remote), 1)

    def test_verify_lookup_failure_does_not_create(self):
        self.bodies[10] = self.FREE
        self.gh.parent_issue_inventory.side_effect = GHError("offline")
        self.run_pass()
        self.gh.create_issue.assert_not_called()
        self.gh.close_issue.assert_not_called()

    def test_markerless_open_item_reused_across_fingerprint_change(self):
        for state, labels in (("open", ()), ("closed", ("mahler:needs-you",))):
            with self.subTest(state=state, labels=labels):
                self.setUp()
                self.bodies[10] = self.FREE
                row = self.add_remote(
                    40, state=state,
                    body="> **Original request:** x\n\n## Plan\nsorted")
                row["labels"] += [{"name": l} for l in labels]
                before_labels = list(row["labels"])
                self.run_pass()
                self.gh.create_issue.assert_not_called()
                self.assertIn("approve security review", row["body"])
                self.assertIn("## Plan\nsorted", row["body"])
                self.assertEqual(row["labels"], before_labels)
                edits = self.gh.edit_issue_body.call_count
                self.run_pass()
                self.assertEqual(self.gh.edit_issue_body.call_count, edits)
                # Blockers change: same item updated, still no new issue.
                self.bodies[10] = self.FREE + "\n- [ ] confirm rollout"
                self.run_pass()
                self.gh.create_issue.assert_not_called()
                self.assertIn("confirm rollout", row["body"])
                self.assertEqual(row["body"].count("mahler:verify-blockers:start"), 1)
                self.assertIn("## Plan\nsorted", row["body"])

    def test_two_legacy_open_items_create_no_third(self):
        self.bodies[10] = self.FREE
        a = self.add_remote(41)
        b = self.add_remote(42)
        self.run_pass()
        self.run_pass()
        self.gh.create_issue.assert_not_called()
        self.assertIn("approve security review", a["body"])
        self.assertEqual(b["body"], "sorted plan")
        self.assertEqual(self.led.get_kv("parent_verify_dups:mahler:10"), "41,42")

    def test_closed_original_with_stale_kv_gets_one_followup(self):
        self.bodies[10] = self.FREE
        self.add_remote(43, state="closed")
        self.led.set_kv("parent_verify:mahler:10:stale", "x")
        self.run_pass()
        self.gh.create_issue.assert_called_once()
        body = self.gh.create_issue.call_args.args[1]
        self.assertIn("#43", body)
        self.assertIn("approve security review", body)
        self.run_pass()
        self.gh.create_issue.assert_called_once()
        # Restart: a fresh ledger sees the open follow-up in the inventory.
        self.ctx.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.ctx.led.close)
        self.ctx.led.upsert_item("mahler", 10, state="parent")
        self.ctx.led.upsert_item("mahler", 11, parent=10, state="shipped", pr=99)
        self.run_pass()
        self.gh.create_issue.assert_called_once()

    def test_closed_followup_same_blockers_diagnoses_once(self):
        self.bodies[10] = self.FREE
        self.add_remote(43, state="closed")
        self.run_pass()
        self.remote[-1]["state"] = "closed"
        for _ in range(3):
            self.run_pass()
        self.gh.create_issue.assert_called_once()
        diag = [c for c in self.comments[10] if "parent-verify-diagnostic" in c["body"]]
        self.assertEqual(len(diag), 1)
        self.assertIn("#43", diag[0]["body"])
        self.assertIn("#30", diag[0]["body"])
        self.assertEqual(sum("verification closed" in c.args[0]
                             for c in self.ctx.ping.call_args_list), 1)

    def test_checked_parent_closes_without_followup(self):
        self.add_remote(43, state="closed")
        self.run_pass()
        self.gh.create_issue.assert_not_called()
        self.assertEqual(self.led.item("mahler", 10)["state"], "done")

    def test_update_failure_retries_and_dry_run_writes_nothing(self):
        self.bodies[10] = self.FREE
        row = self.add_remote(44)
        with mock.patch.object(self.gh, "edit_issue_body", side_effect=GHError("offline")):
            self.run_pass()
        self.gh.create_issue.assert_not_called()
        self.assertEqual(row["body"], "sorted plan")
        self.run_pass()
        self.assertIn("approve security review", row["body"])
        self.setUp()
        self.bodies[10] = self.FREE
        self.add_remote(43, state="closed")
        self.ctx.dry_run = True
        self.run_pass()
        self.gh.create_issue.assert_not_called()
        self.gh.edit_issue_body.assert_not_called()

    def test_verify_identity_is_exact_parent_number(self):
        self.bodies[10] = self.FREE
        self.add_remote(45, parent=100)
        self.add_remote(46, parent=1, body="<!-- mahler:parent-verify:1:abc -->")
        self.add_remote(47, label=False)
        self.add_remote(48, title="Verify remaining completion criteria for #10 extra")
        self.run_pass()
        self.gh.create_issue.assert_called_once()
        self.assertEqual(self.remote[0]["body"], "sorted plan")

    def test_done_child_also_requires_merge(self):
        self.led.upsert_item("mahler", 11, state="done", pr=None)
        self.gh.closing_prs.return_value = []
        self.run_pass()
        self.gh.close_issue.assert_not_called()
        self.ctx.ping.assert_called_once()

    def test_all_done_children_with_merge_evidence(self):
        self.led.upsert_item("mahler", 11, state="done")
        self.led.upsert_item("mahler", 12, parent=10, state="done")
        self.run_pass()
        self.assertEqual(self.led.item("mahler", 10)["state"], "done")

    def test_closed_unmerged_recorded_pr_is_not_shipment(self):
        self.gh.pr_merge_info.return_value = {"state": "CLOSED"}
        self.gh.closing_prs.return_value = [{"number": 99, "state": "CLOSED"}]
        self.run_pass()
        self.gh.close_issue.assert_not_called()
        self.assertIn("closed without confirmed merged PR", self.comments[10][0]["body"])

    def _unmerged_with_reason(self, reason, body="## Done when\n- [x] Works"):
        self.bodies[10] = body
        self.gh.pr_merge_info.return_value = {"state": "CLOSED"}
        self.gh.closing_prs.return_value = []
        self.led.upsert_item("mahler", 11, parent=10, state="done", pr=None)
        self.remote.append({"number": 11, "state": "closed", "state_reason": reason,
                            "title": "child", "body": "Part of #10"})

    def test_disposition_matrix(self):
        for reason, resolves in (("not_planned", True), ("duplicate", True),
                                 ("completed", False), (None, False), ("weird", False)):
            with self.subTest(reason=reason):
                self.setUp()
                self._unmerged_with_reason(reason)
                self.run_pass()
                self.assertEqual(self.gh.close_issue.called, resolves)
                if not resolves:
                    self.assertIn("closed without confirmed merged PR evidence",
                                  self.comments[10][0]["body"])

    def test_not_planned_child_705_shape_closes_once_idempotent(self):
        self._unmerged_with_reason("not_planned")
        self.run_pass()
        self.run_pass()
        self.gh.close_issue.assert_called_once_with(10)
        self.gh.create_issue.assert_not_called()
        text = "\n".join(c["body"] for c in self.comments[10])
        self.assertIn("#11: closed not planned without a PR", text)
        self.assertNotIn("#11: merged", text)

    def test_not_planned_child_cannot_tick_merge_criterion(self):
        self._unmerged_with_reason("not_planned", "## Done when\n- [ ] #11 merged")
        self.run_pass()
        self.assertNotIn("[x]", self.bodies[10])
        self.gh.close_issue.assert_not_called()

    def test_closed_unmerged_child_feeds_parent_verify(self):
        self.bodies[10] = self.FREE
        self.gh.pr_merge_info.return_value = {"state": "CLOSED"}
        self.gh.closing_prs.return_value = []
        self.run_pass()
        self.run_pass()
        self.gh.create_issue.assert_called_once()
        body = self.gh.create_issue.call_args.args[1]
        self.assertIn("#11: closed without confirmed merged PR evidence", body)
        self.assertIn("supersession", body)
        self.gh.close_issue.assert_not_called()

    def test_verify_item_with_part_of_does_not_block_parent(self):
        for state in ("open", "closed"):
            for marked in (False, True):
                with self.subTest(state=state, marked=marked):
                    self.setUp()
                    body = "Part of #10"
                    if marked:
                        body += "\n<!-- mahler:parent-verify:10:abc -->"
                    self.add_remote(31, state=state, body=body, label=not marked,
                                    title="changed title" if marked else None)
                    self.led.upsert_item("mahler", 31, parent=10, state="done")
                    self.run_pass()
                    self.gh.close_issue.assert_called_once_with(10)
                    self.assertNotIn(mock.call(31), self.gh.closing_prs.call_args_list)

    def test_newly_merged_pr_reopens_diagnostic_parent_verify(self):
        for target in (10, 76):
            with self.subTest(target=target):
                self.setUp()
                self.bodies[10] = self.FREE
                self.add_remote(43, state="closed")
                self.run_pass()
                self.remote[-1]["state"] = "closed"
                self.comments[30] = [{"body": "Follow-up fix #76, prior PR #77"}]
                self.remote.append({"number": 76, "body": "Fix", "state": "closed"})
                self.run_pass()  # diagnostic
                # Exercise migration of the old permanent diagnostic flag.
                diag_key = self.led.q("SELECT key FROM kv WHERE key LIKE 'parent_verify_diag:%'")[0]["key"]
                self.led.set_kv(diag_key, "1")
                for comment in self.comments[10]:
                    if "parent-verify-diagnostic" in comment["body"]:
                        comment["created_at"] = iso(NOW)
                self.run_pass()  # no new evidence
                self.assertEqual(self.gh.create_issue.call_count, 1)
                event = {"event": "cross-referenced", "source": {"issue": {
                    "number": 78, "pull_request": {},
                    "repository_url": "https://api.github.com/repos/mkny13/mahler"}}}
                event["source"]["issue"]["pull_request"] = {"url": "pr"}
                self.gh._parent_pages.side_effect = lambda endpoint: (
                    [event] if endpoint == f"repos/mkny13/mahler/issues/{target}/timeline?" else [])
                self.gh.pr_merge_info.return_value = {
                    "state": "MERGED", "mergedAt": iso(NOW + timedelta(hours=1))}
                self.run_pass()
                self.assertEqual(self.gh.create_issue.call_count, 2)
                self.assertIn("PR #78", self.gh.create_issue.call_args.args[1])
                self.run_pass()  # reuse open verification
                self.remote[-1]["state"] = "closed"
                self.run_pass()  # the same merge cannot create a chain
                self.assertEqual(self.gh.create_issue.call_count, 2)

    def test_legacy_diagnostic_migration_accepts_merge_before_comment(self):
        self.bodies[10] = self.FREE
        self.add_remote(43, state="closed")
        self.run_pass()
        self.remote[-1]["state"] = "closed"
        self.comments[30] = [{"body": "Follow-up fix #76"}]
        self.remote.append({"number": 76, "body": "Fix", "state": "closed"})
        self.run_pass()  # diagnostic
        diag_key = self.led.q(
            "SELECT key FROM kv WHERE key LIKE 'parent_verify_diag:%'")[0]["key"]
        self.led.set_kv(diag_key, "1")
        for comment in self.comments[10]:
            if "parent-verify-diagnostic" in comment["body"]:
                comment["created_at"] = "2026-10-14T09:36:34Z"
        event = {"event": "cross-referenced", "source": {"issue": {
            "number": 78, "pull_request": {"url": "pr"},
            "repository_url": "https://api.github.com/repos/mkny13/mahler"}}}
        self.gh._parent_pages.side_effect = lambda endpoint: (
            [event] if endpoint == "repos/mkny13/mahler/issues/76/timeline?" else [])
        # PR #1078 merged four minutes before #1054's diagnostic comment.
        self.gh.pr_merge_info.return_value = {
            "state": "MERGED", "mergedAt": "2026-10-14T09:32:05Z"}
        self.run_pass()
        self.assertEqual(self.gh.create_issue.call_count, 2)
        self.assertIn("PR #78", self.gh.create_issue.call_args.args[1])
        self.run_pass()
        self.assertEqual(self.gh.create_issue.call_count, 2)

    def test_newly_created_diagnostic_accepts_merge_after_closure_before_recording(self):
        self.bodies[10] = self.FREE
        self.add_remote(43, state="closed")
        self.run_pass()
        self.remote[-1]["state"] = "closed"
        self.remote[-1]["closed_at"] = "2026-10-14T09:00:00Z"
        self.comments[30] = [{"body": "Follow-up fix #76"}]
        self.remote.append({"number": 76, "body": "Fix", "state": "closed"})
        event = {"event": "cross-referenced", "source": {"issue": {
            "number": 78, "pull_request": {"url": "pr"},
            "repository_url": "https://api.github.com/repos/mkny13/mahler"}}}
        self.gh._parent_pages.side_effect = lambda endpoint: (
            [event] if endpoint == "repos/mkny13/mahler/issues/76/timeline?" else [])
        # Merged after the final attempt closed, before any diagnostic exists.
        self.gh.pr_merge_info.return_value = {
            "state": "MERGED", "mergedAt": "2026-10-14T09:30:00Z"}
        self.run_pass()
        self.assertEqual(self.gh.create_issue.call_count, 2)
        self.assertIn("PR #78", self.gh.create_issue.call_args.args[1])
        self.assertFalse(any("parent-verify-diagnostic" in c["body"]
                             for c in self.comments.get(10, [])))
        # The merge-triggered retry closes unresolved with no further merge:
        # the diagnostic must still be posted once.
        self.remote[-1]["state"] = "closed"
        self.remote[-1]["closed_at"] = "2026-10-14T10:00:00Z"
        for _ in range(3):
            self.run_pass()
        diag = [c for c in self.comments.get(10, []) if "parent-verify-diagnostic" in c["body"]]
        self.assertEqual(len(diag), 1)

    def test_supported_list_syntax_and_cross_repository_unknown(self):
        body = "## Children\n- #11: child\n1. #12 child\n- 2. #13: child\n- [x] #14 child\n"
        body += "- mkny13/mahler#15 child\n- https://github.com/mkny13/mahler/issues/16 child"
        self.assertEqual(sync._parent_children(body, "mkny13/mahler"), set(range(11, 17)))
        with self.assertRaises(GHError):
            sync._parent_children(body + "\n- other/repo#12", "mkny13/mahler")

    def test_open_or_reopened_child_waits_silently(self):
        self.states[11] = "OPEN"
        self.led.upsert_item("mahler", 12, parent=10, state="done")
        self.run_pass()
        self.gh.close_issue.assert_not_called()
        self.ctx.ping.assert_not_called()

    def test_relationship_union_and_exclusions(self):
        self.bodies[10] += "\n## Plan\nSteps:\n- 1. #12: native overlap\n- 2. #13: historical\n" + \
            "- #10: self\n> - #90: original\n## Context\n- #91 incidental\n## Dependencies\n- #92 dep"
        self.gh.sub_issues.return_value = [12]
        self.gh.parent_issue_inventory.return_value = [
            {"number": 13, "body": "Part of #10"},
            {"number": 90, "body": "> Part of #10"}]
        self.states[13] = "CLOSED"
        self.run_pass()
        self.assertEqual(self.led.item("mahler", 10)["state"], "done")
        comment = self.comments[10][0]["body"]
        for n in (11, 12, 13):
            self.assertEqual(comment.count(f"#{n}:"), 1)
        self.assertNotIn("#90", comment)

    def test_part_of_only_historical_child(self):
        self.led.upsert_item("mahler", 11, parent=None)
        self.gh.parent_issue_inventory.return_value = [{"number": 12, "body": "Part of #10"}]
        self.run_pass()
        self.assertIn("#12: merged", self.comments[10][0]["body"])
        self.assertEqual(self.led.item("mahler", 10)["state"], "done")

    def test_native_only_child(self):
        self.led.upsert_item("mahler", 11, parent=None)
        self.gh.sub_issues.return_value = [12]
        self.run_pass()
        self.assertEqual(self.led.item("mahler", 10)["state"], "done")

    def test_goal_label_candidate(self):
        self.led.upsert_item("mahler", 10, state="ready", labels=json.dumps(["type:goal"]))
        self.run_pass()
        self.assertEqual(self.led.item("mahler", 10)["state"], "done")

    def test_goal_directly_built_no_children_not_a_parent(self):
        self.led.upsert_item("mahler", 11, parent=None)
        for state in ("ready", "working", "verifying", "needs_you"):
            with self.subTest(state=state):
                self.led.upsert_item("mahler", 10, state=state,
                                     labels=json.dumps(["type:goal"]))
                before = dict(self.led.item("mahler", 10))
                self.run_pass()
                self.assertEqual(dict(self.led.item("mahler", 10)), before)
                self.gh.comment.assert_not_called()
                self.gh.close_issue.assert_not_called()
                self.gh.create_issue.assert_not_called()
                self.ctx.ping.assert_not_called()

    def test_explicit_goal_parent_without_children_is_processed(self):
        for marker in ("state", "label", "pending"):
            with self.subTest(marker=marker):
                self.setUp()
                self.led.upsert_item("mahler", 11, parent=None)
                labels = ["type:goal"]
                if marker == "label":
                    labels.append("mahler:parent")
                self.led.upsert_item("mahler", 10,
                                     state="parent" if marker == "state" else "ready",
                                     labels=json.dumps(labels))
                if marker == "pending":
                    self.led.set_kv("parent_pending:mahler:10", "pending")
                self.run_pass()
                self.gh.comment.assert_called_once()
                self.ctx.ping.assert_called_once()
                self.assertIn("No child relationships", self.comments[10][0]["body"])

    def test_goal_child_relationship_sources(self):
        for source in ("body", "native", "ledger", "part_of"):
            with self.subTest(source=source):
                self.setUp()
                self.led.upsert_item("mahler", 10, state="ready",
                                     labels=json.dumps(["type:goal"]))
                self.led.upsert_item("mahler", 11, parent=None)
                if source == "body":
                    self.bodies[10] += "\n## Children\n- #11: child"
                elif source == "native":
                    self.gh.sub_issues.return_value = [11]
                elif source == "ledger":
                    self.led.upsert_item("mahler", 11, parent=10)
                else:
                    self.gh.parent_issue_inventory.return_value = [
                        {"number": 11, "body": "Part of #10"}]
                self.run_pass()
                self.gh.close_issue.assert_called_once_with(10)
                self.assertEqual(self.led.item("mahler", 10)["state"], "done")

    def test_goal_with_children_blocked_pings_once(self):
        self.led.upsert_item("mahler", 10, state="ready",
                             labels=json.dumps(["type:goal"]))
        self.bodies[10] = "## Done when\n- [ ] exact criterion"
        # A second child without merge evidence exercises the non-free-text branch.
        self.led.upsert_item("mahler", 12, parent=10, state="done")
        self.gh.closing_prs.return_value = []
        self.run_pass()
        self.ctx.ping.assert_called_once()
        self.bodies[10] += "\n## Context\nReworded explanation"
        self.led.upsert_item("mahler", 11, pr=100)
        self.run_pass()
        self.ctx.ping.assert_called_once()
        self.gh.close_issue.assert_not_called()
        self.assertEqual(self.led.item("mahler", 10)["state"], "ready")

    def test_checklist_and_empty_set_blockers(self):
        for body in ("", "## Done when\n- prose", "## Done when\n- [ ] Works"):
            with self.subTest(body=body):
                self.bodies[10] = body
                self.run_pass()
                self.gh.close_issue.assert_not_called()
        self.bodies[10] = "## Done when\n- [x] Works"
        self.led.upsert_item("mahler", 11, parent=None)
        self.run_pass()
        self.gh.close_issue.assert_not_called()
        self.assertIn("No child relationships", self.comments[10][-1]["body"])

    def test_wrapped_checklist_item_is_parsed(self):
        body = "## Done when\n- [ ] A long criterion that\n  wraps onto a second line"
        self.assertEqual(sync._parent_checklist(body),
                         ["Unchecked criterion: A long criterion that wraps onto a second line"])
        self.assertEqual(sync._parent_checklist("## Done when\n- [x] wrapped\n  more"), [])
        self.assertEqual(sync._parent_checklist("## Done when\n- prose\n- [ ] x"),
                         ["Done-when checklist absent or unparseable"])

    def test_357_shape_wrapped_items_nested_bullets_and_trailing_section(self):
        body = ("## Done when\n"
                "- [x] First criterion that is long and\n  wraps onto a second line\n"
                "- [x] Second criterion with lazy\ncontinuation\n"
                "  - nested detail bullet\n"
                "- [x] #11 closed\n\n"
                "## Needs a human to check\n- [ ] eyeball the console\n")
        self.assertEqual(len(sync._parent_checks(body)), 3)
        self.assertEqual(sync._parent_checklist(body), [])
        self.assertEqual(sync._parent_checklist(body.replace("[x]", "[ ]", 1)),
                         ["Unchecked criterion: First criterion that is long and wraps onto a second line"])

    def test_unparseable_done_when_files_one_verify_item(self):
        self.bodies[10] = "## Done when\n- just prose, no checkbox\n"
        self.run_pass()
        self.run_pass()
        self.gh.close_issue.assert_not_called()
        self.gh.create_issue.assert_called_once()

    def test_357_shape_completes_parent(self):
        self.bodies[10] = ("## Done when\n- [ ] #11 closed\n"
                           "- [x] Wrapped criterion that\n  continues here\n"
                           "  - nested detail\n\n"
                           "## Needs a human to check\n- [ ] eyeball it\n")
        self.run_pass()
        self.assertIn("- [x] #11 closed", self.bodies[10])
        self.gh.close_issue.assert_called_once()

    def test_blockers_deduplicate_after_restart_and_resolve(self):
        self.bodies[10] = "## Done when\n- [ ] exact criterion"
        self.run_pass()
        self.ctx = scheduler.Ctx(self.cfg, self.led)
        self.ctx._gh["mkny13/mahler"] = self.gh
        self.ctx.ping = mock.Mock()
        self.run_pass()
        self.gh.comment.assert_called_once()
        self.ctx.ping.assert_not_called()
        self.bodies[10] += "\n## Context\nReworded explanation"
        self.run_pass()
        self.ctx.ping.assert_not_called()
        self.bodies[10] = "## Done when\n- [ ] different criterion"
        self.run_pass()
        self.ctx.ping.assert_called_once()
        self.bodies[10] = "## Done when\n- [x] different criterion"
        self.run_pass()
        self.assertEqual(self.led.item("mahler", 10)["state"], "done")

    def test_deduplication_survives_ledger_reopen(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "ledger.db")
            self.led.con.backup((disk := Ledger(path)).con)
            disk.close()
            self.led = Ledger(path, clock=lambda: NOW)
            self.ctx.led = self.led
            self.bodies[10] = "## Done when\n- [ ] #11 closed\n- [ ] not yet"
            self.run_pass()
            self.led.close()
            self.led = Ledger(path, clock=lambda: NOW)
            self.ctx.led = self.led
            try:
                self.run_pass()
                self.gh.comment.assert_called_once()
                self.ctx.ping.assert_called_once()
                self.gh.edit_issue_body.assert_called_once()
                self.gh.create_issue.assert_called_once()
                self.assertEqual(self.led.get_kv("parent_tick:mahler:10#11"), "1")
            finally:
                self.led.close()

    def test_failure_before_local_state_write_reconciles(self):
        with mock.patch.object(self.led, "set_state", side_effect=RuntimeError("write failed")):
            self.run_pass()
        self.run_pass()
        self.assertEqual(self.led.item("mahler", 10)["state"], "done")
        self.gh.comment.assert_called_once()
        self.gh.close_issue.assert_called_once()

    def test_failed_project_does_not_stop_next_project(self):
        second = proj(name="other", repo="other/repo")
        self.cfg["projects"]["other"] = second
        other = mock.Mock()
        other.parent_issue_inventory.side_effect = GHError("offline")
        self.ctx._gh["other/repo"] = other
        self.led.upsert_item("other", 1, state="parent")
        sync.close_finished_parents(self.ctx, [second, proj()])
        self.assertEqual(self.led.item("mahler", 10)["state"], "done")

    def test_dry_run_has_no_writes(self):
        self.ctx.dry_run = True
        before = list(self.led.con.iterdump())
        self.run_pass()
        self.bodies[10] = "## Done when\n- [ ] #11 closed\n- [ ] review"
        self.run_pass()
        self.gh.edit_issue_body.assert_not_called()
        self.gh.create_issue.assert_not_called()
        self.assertEqual(list(self.led.con.iterdump()), before)
        self.gh.comment.assert_not_called()
        self.gh.close_issue.assert_not_called()
        self.ctx.ping.assert_not_called()

    def test_discovery_failures_are_not_success(self):
        for method in ("parent_issue_inventory", "sub_issues", "issue_body", "issue_state"):
            with self.subTest(method=method):
                with mock.patch.object(self.gh, method, side_effect=GHError("incomplete")):
                    self.run_pass()
                self.gh.close_issue.assert_not_called()

    def test_partial_writes_recover_once(self):
        for operation in ("comment", "close_issue", "set_state"):
            with self.subTest(operation=operation):
                self.setUp()
                obj = self.led if operation == "set_state" else self.gh
                original = getattr(obj, operation)
                def ambiguous(*args, **kwargs):
                    original(*args, **kwargs)
                    raise GHError("response lost")
                with mock.patch.object(obj, operation, side_effect=ambiguous):
                    self.run_pass()
                self.run_pass()
                self.run_pass()
                self.assertEqual(len(self.comments[10]), 1)
                self.assertEqual(self.led.item("mahler", 10)["state"], "done")
                self.assertEqual(len(self.led.q("SELECT * FROM events WHERE number=10 AND kind='state'")), 1)

    def test_failed_parent_does_not_stop_second_parent(self):
        self.led.upsert_item("mahler", 20, state="parent")
        self.led.upsert_item("mahler", 12, parent=20, state="done")
        self.bodies[20] = self.bodies.pop(10)
        self.states[20] = "OPEN"
        self.run_pass()
        self.assertEqual(self.led.item("mahler", 20)["state"], "done")
        self.assertEqual(self.led.item("mahler", 10)["state"], "parent")

    def test_nested_and_cyclic_trackers_are_bounded(self):
        self.led.upsert_item("mahler", 20, state="parent", parent=10)
        self.led.upsert_item("mahler", 11, parent=20)
        self.bodies[20] = self.bodies[10]
        self.states[20] = "OPEN"
        self.run_pass()
        self.run_pass()
        self.assertEqual(self.led.item("mahler", 10)["state"], "done")
        self.assertIn("#20: recorded parent completion", self.comments[10][0]["body"])
        self.led.upsert_item("mahler", 10, state="parent", parent=20)
        self.led.upsert_item("mahler", 20, state="parent", parent=10)
        self.states.update({10: "OPEN", 20: "OPEN"})
        self.run_pass()
        self.assertEqual(self.led.item("mahler", 10)["state"], "parent")
        self.assertEqual(self.led.item("mahler", 20)["state"], "parent")


class SyncStoresParentTests(unittest.TestCase):
    def setUp(self):
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)
        self.cfg = {"defaults": {}, "projects": {"mahler": proj()}}
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
        self.gh_mock = mock.Mock()
        self.gh_mock.issues_changed.return_value = (True, None)
        self.gh_mock.blocked_by_of.return_value = []
        self.ctx._gh["mkny13/mahler"] = self.gh_mock

    def test_sync_stores_parent_from_body(self):
        """sync stores parent from 'Part of #N' body line."""
        # Mock open_issues to return an issue with "Part of #10" in body
        self.gh_mock.open_issues.return_value = [
            {
                "number": 11,
                "title": "Child issue",
                "labels": [],
                "body": "Part of #10\n\n## Plan\n...\n## Done when\n...",
                "createdAt": "2026-09-13T10:00:00Z",
                "updatedAt": "2026-09-13T10:00:00Z",
                "comments": [], "authorAssociation": "OWNER",
            }
        ]

        sync.sync(self.ctx, "mahler")

        item = self.led.item("mahler", 11)
        self.assertEqual(item["parent"], 10)

    def test_sync_handles_bold_part_of(self):
        """sync handles '**Part of #N**' (bold) variant."""
        self.gh_mock.open_issues.return_value = [
            {
                "number": 11,
                "title": "Child issue",
                "labels": [],
                "body": "**Part of #10**\n\n## Plan\n...\n## Done when\n...",
                "createdAt": "2026-09-13T10:00:00Z",
                "updatedAt": "2026-09-13T10:00:00Z",
                "comments": [], "authorAssociation": "OWNER",
            }
        ]

        sync.sync(self.ctx, "mahler")

        item = self.led.item("mahler", 11)
        self.assertEqual(item["parent"], 10)

    def test_sync_handles_blockquoted_part_of(self):
        """sync handles a 'Part of #N' line quoted under an '> **Original
        request:**' preamble (mahler#14/#64/#83's actual shape — this is what
        silently broke close_finished_parents for them)."""
        self.gh_mock.open_issues.return_value = [
            {
                "number": 11,
                "title": "Child issue",
                "labels": [],
                "body": "> **Original request:**\n"
                        "> Part of #10 (Some Goal)\n\n## Plan\n...\n## Done when\n...",
                "createdAt": "2026-09-13T10:00:00Z",
                "updatedAt": "2026-09-13T10:00:00Z",
                "comments": [], "authorAssociation": "OWNER",
            }
        ]

        sync.sync(self.ctx, "mahler")

        item = self.led.item("mahler", 11)
        self.assertEqual(item["parent"], 10)

    def test_sync_no_parent_when_no_part_of(self):
        """sync stores None when no 'Part of' line present."""
        self.gh_mock.open_issues.return_value = [
            {
                "number": 11,
                "title": "Standalone issue",
                "labels": [],
                "body": "Just a regular issue",
                "createdAt": "2026-09-13T10:00:00Z",
                "updatedAt": "2026-09-13T10:00:00Z",
                "comments": [], "authorAssociation": "OWNER",
            }
        ]

        sync.sync(self.ctx, "mahler")

        item = self.led.item("mahler", 11)
        self.assertIsNone(item["parent"])


class ParentColumnMigrationTests(unittest.TestCase):
    def test_parent_column_added_to_legacy_database(self):
        """Migration adds parent column to existing DB without it."""
        import os
        import sqlite3
        import tempfile
        from mahler.ledger import SCHEMA

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.db")
            # Create DB without parent column
            con = sqlite3.connect(path)
            # Use SCHEMA but filter out parent column
            schema_without_parent = "\n".join(
                l for l in SCHEMA.splitlines() if "parent" not in l.lower()
            )
            con.executescript(schema_without_parent)
            con.execute("INSERT INTO items (project, number, state) VALUES ('p', 1, 'ready')")
            con.commit()
            con.close()

            # Opening with Ledger should add the column
            led = Ledger(path)
            item = led.item("p", 1)
            self.assertEqual(item["state"], "ready")
            # Column should exist and be nullable
            led.upsert_item("p", 1, parent=5)
            item = led.item("p", 1)
            self.assertEqual(item["parent"], 5)


class MaintenanceQueueAfterParentCloseTests(unittest.TestCase):
    """Test that queue_maintenance files a pass again after parent closes and cooldown passes."""

    def setUp(self):
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)
        self.cfg = {"defaults": {}, "projects": {"mahler": proj()}}
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
        self.gh_mock = mock.Mock()
        self.ctx._gh["mkny13/mahler"] = self.gh_mock

        # Make all passes NOT due initially
        for pass_name in config.MAINTENANCE_PASSES:
            self.led.set_maintenance_checkpoint("mahler", pass_name, last_filed_at=NOW)
        # Make security due
        self.led.set_maintenance_checkpoint("mahler", "security",
                                            last_filed_at=NOW - timedelta(days=40))

    def test_queue_maintenance_after_parent_closes_and_cooldown(self):
        """After parent of a pass:security item closes and cooldown passes,
        queue_maintenance files that pass again."""
        # Create a pass:security item that's a child of a parent
        self.led.upsert_item("mahler", 10, title="Parent pass", state="parent")
        self.led.upsert_item("mahler", 11, title="Security pass", parent=10,
                            labels=json.dumps(["pass:security"]), state="done")
        self.led.upsert_item("mahler", 11, state_changed_at=iso(NOW - timedelta(days=20)))

        # Parent is still open - queue_maintenance should skip
        tick.queue_maintenance(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_not_called()

        # Now close the parent (simulate close_finished_parents)
        self.led.set_state("mahler", 10, "done", "all sub-issues done")

        # queue_maintenance should now file the pass
        tick.queue_maintenance(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_called_once()
        args, kwargs = self.gh_mock.create_issue.call_args
        self.assertIn("pass:security", args[2])

    def test_queue_maintenance_skips_if_cooldown_not_passed(self):
        """If cooldown hasn't passed, queue_maintenance doesn't file."""
        self.led.upsert_item("mahler", 10, title="Parent pass", state="parent")
        self.led.upsert_item("mahler", 11, title="Security pass", parent=10,
                            labels=json.dumps(["pass:security"]), state="done")
        # Closed only 5 days ago (cooldown default 14)
        self.led.upsert_item("mahler", 11, state_changed_at=iso(NOW - timedelta(days=5)))

        # Close parent
        self.led.set_state("mahler", 10, "done", "all sub-issues done")

        tick.queue_maintenance(self.ctx, [proj()])
        self.gh_mock.create_issue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
