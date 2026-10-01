"""Bounded Ready-to-test sessions and the browser's two-tap bulk verdict."""
import json
import shutil
import subprocess
import unittest

from mahler.console import actions, page, state
from test_console import make_cfg, make_led


class SessionsTests(unittest.TestCase):
    def setUp(self):
        self.led = make_led()
        self.addCleanup(self.led.close)
        self.cfg = make_cfg()

    def add(self, number, project="mahler", labels=(), parent=None):
        self.led.upsert_item(project, number, labels=json.dumps(labels), parent=parent)
        self.led.add_uat(project, number, number + 100, "abc123", f"Change {number}", "Check it")

    def snapshot(self):
        return state.build(self.cfg, self.led)

    def test_group_precedence_projects_and_deterministic_area(self):
        self.add(1, labels=["area:z", "area:a"], parent=90)
        self.add(2, parent=90)
        self.add(3)
        self.add(4, project="groundwork", labels=["area:a"])
        self.add(5, project="old")
        sessions = self.snapshot()["sessions"]
        self.assertEqual({(s["project"], s["group"]) for s in sessions}, {
            ("mahler", "area:a"), ("mahler", "Part of #90"),
            ("mahler", "Other changes"), ("groundwork", "area:a")})
        self.assertEqual(sum(len(s["changes"]) for s in sessions), 4)

    def test_split_counts_order_and_flat_queue_preserved(self):
        for n in range(1, 24):
            self.add(n, labels=["area:console"])
        self.add(24, parent=90)
        snapshot = self.snapshot()
        sessions = snapshot["sessions"]
        self.assertEqual([s["pending_count"] for s in sessions], [10, 10, 3, 1])
        self.assertEqual([s["parts"] for s in sessions], [3, 3, 3, 1])
        self.assertIn("Part 3 of 3", sessions[2]["title"])
        self.assertEqual(len(snapshot["uat"]), 24)
        self.assertEqual({u["ref"] for u in snapshot["uat"]},
                         {u["ref"] for s in sessions for u in s["changes"]})

    def test_verdicts_skipped_and_sessions_sorted_by_actionable_count(self):
        for n in range(1, 4):
            self.add(n, labels=["area:a"])
        self.add(4, labels=["area:b"])
        self.led.set_uat_verdict("mahler", 1, "fail")
        actions.run(self.cfg, self.led, "uat_fail", {"project": "mahler", "number": 2, "note": "broken"})
        actions.run(self.cfg, self.led, "uat_pass", {"project": "mahler", "number": 3})
        sessions = self.snapshot()["sessions"]
        self.assertEqual([s["group"] for s in sessions], ["area:b", "area:a"])
        self.assertEqual([s["pending_count"] for s in sessions], [1, 0])
        self.assertTrue(sessions[1]["pass_all"]["disabled"])
        self.assertFalse(sessions[0]["pass_all"]["confirmed"])
        html = page._d_test(self.snapshot())
        self.assertIn("Failed — p1 bug", html)
        self.assertIn(' disabled>Pass all 0', html)
        # A stale browser cannot overwrite either a queued or a recorded failure.
        for number in (2, 3):
            with self.assertRaises(actions.ActionError):
                actions.run(self.cfg, self.led, "uat_pass", {"project": "mahler", "number": number})

    def test_shipped_failure_stays_visible_with_age_until_pass(self):
        self.add(1)
        self.led.upsert_item("mahler", 1, state="shipped")
        self.led.con.execute("UPDATE uat SET shipped_at='2026-09-01T00:00:00Z'")
        self.led.set_uat_verdict("mahler", 1, "fail", bug=99)
        row = self.snapshot()["uat"][0]
        self.assertIn("d ago", row["meta"])
        self.assertIn("PR #101", row["meta"])
        self.assertIn("sha abc123", row["meta"])
        self.assertIn("awaiting passing evidence", row["meta"])
        actions.run(self.cfg, self.led, "uat_pass", {"project": "mahler", "number": 1})

    def test_shared_link_and_both_layouts(self):
        self.cfg = make_cfg(projects={"mahler": {"uat_url": "https://staging.example/", "uat_url_label": "Try app"}})
        self.add(1)
        self.add(2)
        snapshot = self.snapshot()
        for html in (page._d_test(snapshot), page._p_triage(snapshot)):
            self.assertEqual(html.count('class="uat-session"'), 1)
            self.assertEqual(html.count('href="https://staging.example/"'), 1)
            self.assertEqual(html.count('data-act="uat_pass"'), 2)
            self.assertEqual(html.count('data-open-bug='), 2)
            self.assertIn("Pass all 2", html)
            self.assertIn("2 awaiting verdict", html)
            self.assertIn("mahler · Other changes", html)

    def test_change_specific_template_falls_back_to_pr_in_session(self):
        for template in ("https://staging.example/{number}", "https://staging.example/{pr}"):
            self.cfg = make_cfg(projects={"mahler": {"uat_url": template}})
            self.add(1)
            snapshot = self.snapshot()
            self.assertIsNone(snapshot["sessions"][0]["link"])
            self.assertIn('href="https://github.com/mkny13/mahler/pull/101"', page._d_test(snapshot))
            self.assertIn("staging.example", snapshot["uat"][0]["link"])

    def test_missing_item_metadata_and_empty_queue(self):
        self.assertEqual(self.snapshot()["sessions"], [])
        self.led.add_uat("mahler", 1, None, "abc", "Orphan", "Check")
        self.assertEqual(self.snapshot()["sessions"][0]["group"], "Other changes")


class BrowserPassAllTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node needed for browser regression")
    def test_two_taps_pending_requests_membership_changes_and_failure(self):
        js = page.JS[page.JS.index("  // Shared across both layouts"):page.JS.index("  function numberValue")]
        script = js + r'''
const assert = require("assert");
let calls = [], rows = [1, 2, 3], fail = false;
function payloadFor(n) { return {project: "mahler", number: n}; }
function post(action, payload) {
  calls.push([action, payload.number]);
  return fail ? Promise.reject(Error("offline")) : Promise.resolve();
}
function button() {
  let attrs = {};
  return {disabled: false, textContent: "Pass all 3",
    getAttribute: k => attrs[k], setAttribute: (k, v) => attrs[k] = v,
    closest: () => ({querySelectorAll: () => rows})};
}
(async () => {
  let b = button();
  verdictRequests["mahler#2"] = true; // per-change Fail still in flight
  await passAll(b);
  assert.equal(b.textContent, "Tap again to pass all 2");
  assert.deepEqual(calls, []);
  let pending = passAll(b);
  assert.equal(b.disabled, true);
  await passAll(button()); // second layout cannot double-submit
  await pending;
  assert.deepEqual(calls, [["uat_pass", 1], ["uat_pass", 3]]);
  assert.equal(verdictRequests["mahler#2"], true);
  assert.equal(passAllBusy, false);
  calls = []; b = button(); rows = [1, 3];
  await passAll(b);
  rows = [3]; // membership change needs another confirmation
  await passAll(b);
  assert.deepEqual(calls, []);
  assert.equal(b.textContent, "Tap again to pass all 1");
  await passAll(b);
  assert.deepEqual(calls, [["uat_pass", 3]]);
  calls = []; b = button(); rows = [4, 5]; fail = true;
  await passAll(b);
  await assert.rejects(passAll(b), /offline/);
  assert.equal(passAllBusy, false);
  assert.equal(verdictRequests["mahler#4"], undefined);
  assert.deepEqual(calls, [["uat_pass", 4]]); // stop on transport failure
  b = button(); rows = [];
  await passAll(b);
  assert.equal(b.disabled, true);
})().catch(err => { console.error(err); process.exitCode = 1; });
'''
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
