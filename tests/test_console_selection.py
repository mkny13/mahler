"""Selection budgets and navigation regressions for #521."""
import copy
import json
import re
import shutil
import subprocess
import unittest
from unittest import mock

from mahler.console import actions, page, state
from test_console import make_cfg, make_led
from test_serve import _Served


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.led = make_led()
        self.addCleanup(self.led.close)
        self.cfg = make_cfg()
        # Seven projects and dozens of model slots, comparable to the console
        # that triggered this issue, without reading the real daemon config.
        template = self.cfg["platforms"]["cline-free"]
        for slot in range(30):
            self.cfg["platforms"][f"work-model-{slot}"] = dict(template, model=f"model-{slot}")
        for project in range(5):
            self.cfg["projects"][f"project-{project}"] = dict(
                self.cfg["projects"]["mahler"], routing={"build": ["cline-free"]})
        for number in range(1, 101):
            self.led.upsert_item("mahler", number, title=f"Realistic backlog item {number}",
                                 state="ready", issue_body="Context and acceptance criteria. " * 20)
            self.led.create_run(project="mahler", number=number, role="build",
                                platform="cline-free", epoch=1, status="ended",
                                outcome="DONE", ended_at=self.led.now().isoformat())
            if number <= 40:
                self.led.add_uat("mahler", number, number + 100, "abc123",
                                 f"Shipped improvement {number}", "- Verify the changed behavior")
                self.led.snapshot_release_item("mahler", number, pr=number+100,
                    title=f"Shipped improvement {number}", merge_sha="abc123",
                    labels=["type:feature"])
            if number <= 10:
                self.led.set_state("mahler", number, "needs_you", "Choose an option")
        self.s = state.build(self.cfg, self.led)

    def test_every_fragment_contains_only_selected_section_and_meets_budget(self):
        sizes = {}
        for layout, names in (("desktop", dict(page.VIEWS)), ("phone", page.TABS)):
            for name in names:
                fragment = page.app(self.s, layout, view=name, tab=name)
                sizes[f"{layout}/{name}"] = len(fragment.encode())
                sections = re.findall(r'<section class="(?:view|tabv) (?:view|tabv)-([^" ]+)', fragment)
                self.assertEqual(sections, [name])
                self.assertNotIn('data-run-detail=', fragment)
                self.assertNotIn('data-bug-detail=', fragment)
                self.assertNotIn('data-release-detail=', fragment)
                self.assertNotIn('class="ph"' if layout == "desktop" else 'class="dk"', fragment)
                json.loads(re.search(r'id="counts">(.*?)</script>', fragment)[1])
        self.assertLess(max(sizes.values()), 250_000, json.dumps(sizes, indent=2))

    def test_invalid_selection_falls_back_and_open_overlay_is_scoped(self):
        fragment = page.app(self.s, "garbage", view="<script>")
        self.assertIn('data-rendered-layout="desktop"', fragment)
        self.assertNotIn("<script>", fragment)
        fragment = page.app(self.s, overlays={"capture": "1"})
        self.assertIn('data-capture-detail="1"', fragment)
        self.assertNotIn('data-release-detail=', fragment)

    def test_long_tasks_are_validated_and_recorded(self):
        report = dict(kind="longtask", duration_ms=1200, layout="phone", view="triage",
                      elements=100, fragment_bytes=12000, heap_bytes=50000,
                      minutes_since_load=45)
        actions.client_log({}, self.led, report)
        rows = self.led.con.execute("SELECT detail FROM events WHERE kind='console_client_longtask'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0][0])["duration_ms"], 1200)
        for extra in ({"duration_ms": 999}, {"elements": float("nan")},
                      {"layout": "bad"}, {"view": "<script>"}, {"heap_bytes": None}):
            actions.client_log({}, self.led, dict(report, **extra))
        self.assertEqual(self.led.con.execute(
            "SELECT count(*) FROM events WHERE kind='console_client_longtask'").fetchone()[0], 2)


class SelectionHTTPTests(_Served):
    def test_fragment_and_reload_use_selected_layout(self):
        status, _, html = self.request("/fragment?layout=phone&tab=models")
        self.assertEqual(status, 200)
        self.assertIn('data-rendered-section="models"', html)
        self.assertNotIn('class="dk"', html)
        status, _, html = self.request("/", headers={
            "Cookie": "mahler_layout=desktop; mahler_view=settings"})
        self.assertEqual(status, 200)
        # Inspect app markup only; embedded CSS/JS names all views.
        fragment = html.split('<div id="app">', 1)[1].split('<script>')[0]
        self.assertIn('data-rendered-section="settings"', fragment)
        self.assertNotIn('class="ph"', fragment)
        self.assertNotIn('section class="view view-now"', fragment)

    def test_long_task_uses_existing_endpoint_and_limiter(self):
        payload = dict(kind="longtask", duration_ms=1500, layout="desktop", view="now",
                       elements=120, fragment_bytes=15000, minutes_since_load=60)
        with mock.patch("mahler.serve.CLIENT_LOG_MAX_EVENTS", 3):
            for _ in range(5):
                self.assertEqual(self.post("client_log", payload)[0], 204)
        count = self.led.con.execute(
            "SELECT count(*) FROM events WHERE kind='console_client_longtask'").fetchone()[0]
        self.assertGreater(count, 0)
        self.assertEqual(count, 3)


class BrowserSelectionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node needed for browser regression")
    def test_navigation_drafts_resize_and_telemetry(self):
        result = subprocess.run(["node", "tests/console_selection_test.js"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
