"""Read-only shipment history on both console layouts (mahler#750)."""
import unittest
from mahler.console import actions, outbox, page, state
from test_console import make_cfg, make_led


class ShipmentHistoryTests(unittest.TestCase):
    def setUp(self):
        self.led = make_led()
        self.addCleanup(self.led.close)
        self.cfg = make_cfg()

    def add(self, number, project="mahler", needs="Check it"):
        self.led.upsert_item(project, number, state="shipped")
        self.led.add_uat(project, number, number + 100, "abc123", f"Change {number}", needs)

    def snapshot(self):
        return state.build(self.cfg, self.led)

    def test_twenty_newest_per_enabled_project_in_both_layouts(self):
        for project in ("mahler", "groundwork", "old"):
            for n in range(1, 25):
                self.add(n, project)
        snapshot = self.snapshot()
        self.assertEqual([s["project"] for s in snapshot["sessions"]], ["groundwork", "mahler"])
        for session in snapshot["sessions"]:
            self.assertEqual([r["number"] for r in session["changes"]], list(range(24, 4, -1)))
        for html in (page._d_test(snapshot), page._p_triage(snapshot)):
            self.assertEqual(html.count('data-uat='), 40)
            self.assertEqual(html.count('class="uat-session"'), 2)
            self.assertNotIn('data-uat="old', html)
        self.assertEqual(self.led.q1("SELECT count(*) AS n FROM uat")["n"], 72)

    def test_completion_hints_failure_and_historical_evidence(self):
        for n in range(1, 6):
            self.add(n, needs="<script>optional</script>" if n == 1 else "")
        self.led.accept_evidence("mahler", 2, dict(source="report:x", author="ci", created_at="2026-09-12T12:00:00Z", kind="smoke", body="Smoke: PASS report:x"))
        self.led.complete_quiet("mahler", 3, "2026-08-01T00:00:00Z")
        self.led.set_uat_verdict("mahler", 4, "pass")
        self.led.report_shipment_failure("mahler", 5, 99, "broken")
        snapshot = self.snapshot()
        self.assertEqual(snapshot["landing"], {"tab": "now", "view": "now"})
        self.assertNotIn("uat_count", snapshot)
        for html in (page._d_test(snapshot), page._p_triage(snapshot)):
            for text in ("Automated completion", "Quiet period completed", "Historical pass recorded", "Defect reported", "If you notice: &lt;script&gt;optional&lt;/script&gt;"):
                self.assertIn(text, html)
            self.assertEqual(html.count("If you notice:"), 1)
            self.assertEqual(html.count('data-open-bug='), 4)
            for forbidden in ("Pass all", '>Pass<', 'awaiting', 'uat_pass'):
                self.assertNotIn(forbidden, html)
        self.assertNotIn("uat_pass", actions.ACTIONS)
        self.assertNotIn("uat_pass", outbox.HANDLERS)
        self.assertNotIn("uat_pass", page.JS)
        self.assertNotIn("passAll", page.JS)
        with self.assertRaises(KeyError):
            actions.run(self.cfg, self.led, "uat_pass", {"project": "mahler", "number": 1})

    def test_shared_and_per_item_configured_links(self):
        self.add(1)
        self.add(2)
        for template in ("https://staging.example/", "https://staging.example/{number}", "https://staging.example/{pr}"):
            self.cfg = make_cfg(projects={"mahler": {"uat_url": template, "uat_url_label": "Try app"}})
            snapshot = self.snapshot()
            for html in (page._d_test(snapshot), page._p_triage(snapshot)):
                self.assertIn('href="' + template.replace('{number}', '1').replace('{pr}', '101') + '"', html)
                self.assertEqual(html.count('>Try app'), 1 if template.endswith('/') else 2)

    def test_queued_failure_does_not_claim_bug_already_filed(self):
        self.add(1)
        actions.run(self.cfg, self.led, "uat_fail", dict(project="mahler", number=1, note="broken"))
        html = page._d_test(self.snapshot())
        self.assertIn("Failure report queued", html)
        self.assertNotIn("bug filed", html)
        with self.assertRaises(actions.ActionError):
            actions.run(self.cfg, self.led, "uat_fail", dict(project="mahler", number=1, note="again"))
