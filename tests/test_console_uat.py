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


def capture_fixture(led, base, number=1, count=1, head="a" * 40, pr=None):
    """A local-only capture and a confirmed squash merge, with no public links."""
    import json
    from pathlib import Path
    from mahler import screenshots
    pr = pr or number + 100
    merge = "f" * 40
    led.upsert_item("mahler", number, state="shipped")
    led.add_uat("mahler", number, pr, merge, f"Change {number}", "")
    source = Path(base) / f"source-{number}-{head}"
    source.mkdir()
    entries = [{"route": f'/page-{i}<aside>', "file": f"{i}.png"} for i in range(count)]
    for entry in entries:
        (source / entry["file"]).write_bytes(screenshots.PNG_SIGNATURE + b"fixture")
    (source / "manifest.json").write_text(json.dumps(
        {"version": 1, "sha": head, "screenshots": entries}))
    directory = screenshots.store(source, "mahler", pr, head)
    led.set_kv(f"screenshot:mahler:{pr}:{head}",
               json.dumps({"pr": pr, "sha": head, "state": "success"}))
    led.set_kv(f"screenshot-final:mahler#{number}",
               json.dumps({"pr": pr, "head": head, "merge_sha": merge}))
    return directory


class ShipmentScreenshotTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from unittest.mock import patch
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch("mahler.config.STATE", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.led = make_led()
        self.addCleanup(self.led.close)
        self.cfg = make_cfg(projects={"mahler": {"screenshot": "/operator/capture"}})

    def snapshot(self):
        return state.build(self.cfg, self.led)

    def test_one_and_ten_private_images_on_both_layouts(self):
        capture_fixture(self.led, self.tmp.name)
        capture_fixture(self.led, self.tmp.name, number=2, count=10, head="b" * 40)
        snapshot = self.snapshot()
        rows = {r["number"]: r for r in snapshot["uat"]}
        self.assertEqual(len(rows[1]["screenshots"]), 1)
        self.assertEqual(len(rows[2]["screenshots"]), 10)
        self.assertEqual(rows[1]["screenshot_head"], "a" * 40)
        self.assertEqual(rows[1]["sha"], "f" * 40)
        self.assertTrue(set(x["id"] for x in rows[1]["screenshots"]).isdisjoint(
            x["id"] for x in rows[2]["screenshots"]))
        for html in (page._d_test(snapshot), page._p_triage(snapshot)):
            self.assertEqual(html.count('<img '), 11)
            self.assertEqual(html.count('class="screenshot-gallery"'), 2)
            self.assertEqual(html.count('>Fail</button>'), 2)
            self.assertIn('alt="Screenshot of /page-0&lt;aside&gt;"', html)
            self.assertIn('loading="lazy"', html)
            self.assertNotIn('>Pass<', html)
            for row in rows.values():
                for image in row["screenshots"]:
                    self.assertIn(f'href="{image["url"]}"', html)
                    self.assertIn(f'src="{image["url"]}"', html)
                    self.assertTrue(image["url"].startswith("/screenshots/"))

    def test_disabled_no_capture_and_wrong_attribution_have_no_gallery(self):
        import json
        capture_fixture(self.led, self.tmp.name)
        key = "screenshot-final:mahler#1"
        original = json.loads(self.led.get_kv(key))
        cases = [{}, dict(original, pr=999), dict(original, head="b" * 40),
                 dict(original, merge_sha="c" * 40)]
        for final in cases:
            self.led.set_kv(key, json.dumps(final))
            for html in (page._d_test(self.snapshot()), page._p_triage(self.snapshot())):
                self.assertNotIn('screenshot-gallery', html)
                self.assertNotIn('<img ', html)
        self.led.set_kv(key, json.dumps(original))
        self.cfg = make_cfg()
        self.assertNotIn("screenshots", self.snapshot()["uat"][0])

    def test_failed_missing_and_malformed_artifacts_are_unavailable(self):
        import json
        directory = capture_fixture(self.led, self.tmp.name)
        image = self.snapshot()["uat"][0]["screenshots"][0]
        (directory / "0.png").unlink()
        self.assertIsNone(state.screenshot_image(self.cfg, self.led, image["id"]))
        for html in (page._d_test(self.snapshot()), page._p_triage(self.snapshot())):
            self.assertIn("Screenshots unavailable.", html)
            self.assertNotIn('<img ', html)
            self.assertIn('>Fail</button>', html)
        self.led.set_kv("screenshot:mahler:101:" + "a" * 40,
                       json.dumps({"pr": 101, "sha": "a" * 40, "state": "failed"}))
        self.assertEqual(self.snapshot()["uat"][0]["screenshot_status"],
                         "Screenshots unavailable.")
        self.led.set_kv("screenshot-final:mahler#1", "[]")
        self.assertNotIn("screenshots", self.snapshot()["uat"][0])

    def test_superseded_head_never_appears(self):
        import json
        capture_fixture(self.led, self.tmp.name)
        capture_fixture(self.led, self.tmp.name, head="b" * 40)
        row = self.snapshot()["uat"][0]
        self.assertEqual(row["screenshot_head"], "b" * 40)
        from mahler.screenshots import artifact_id
        old = artifact_id("mahler", 101, "a" * 40, 0)
        self.assertIsNone(state.screenshot_image(self.cfg, self.led, old))
        self.assertNotEqual(row["screenshots"][0]["id"], old)
