"""Tests for the read-only status page (`mahler serve`, DESIGN D10)."""

import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from mahler import serve
from mahler.ledger import Ledger


def make_cfg():
    # same shape config.load() returns: defaults + platforms + projects
    return {
        "defaults": {},
        "burst": {"enabled": True, "weekly_lead_hours": 5,
                  "session_lead_minutes": 60, "soft": 90, "hard": 97},
        "platforms": {
            "claude": {"enabled": True, "kind": "claude",
                       "soft": {"5h": 60, "weekly": 70},
                       "hard": {"5h": 70, "weekly": 80},
                       "stale_minutes": 15},
            "cline-free": {"enabled": True, "kind": "cline", "metered": False,
                           "soft": {"5h": 100, "weekly": 100},
                           "hard": {"5h": 100, "weekly": 100},
                           "stale_minutes": 60},
        },
        "projects": {"mahler": {"repo": "mkny13/mahler"}},
    }


def make_led():
    # thread_safe=True: request threads read this connection, exactly like
    # cmd_serve re-opens the real DB thread-safe
    led = Ledger(":memory:", thread_safe=True)
    led.upsert_item("mahler", 5, title="Read-only status page",
                    state="working", priority=2)
    led.upsert_item("mahler", 9, title="A needs-you item <script>",
                    state="needs_you", priority=1)
    led.claim("mahler", 5, "run-32", "auto", 10, platform="cline-free", run_id=1)
    led.create_run(project="mahler", number=5, role="build",
                   platform="cline-free", epoch=1)
    led.record_usage("claude", "5h", 42.0)
    return led


def render(led, cfg=None):
    cfg = cfg or make_cfg()
    return serve.render_page(serve.snapshot(cfg, led), cfg)


class TestRender(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()
        self.led = make_led()

    def test_running_shows_minutes_and_platform(self):
        html = render(self.led, self.cfg)
        self.assertIn("mahler#5", html)
        self.assertIn("cline-free", html)
        self.assertIn("min", html)
        self.assertIn("build", html)

    def test_running_links_to_github_issue(self):
        # mahler#50: the Running section should link project#number too,
        # consistent with the Items section.
        html = render(self.led, self.cfg)
        self.assertIn('<a href="https://github.com/mkny13/mahler/issues/5">mahler#5</a>',
                      html)

    def test_running_without_repo_renders_without_link(self):
        html = render(self.led, {"defaults": {}, "platforms": self.cfg["platforms"],
                                 "projects": {}})
        self.assertIn("mahler#5", html)
        self.assertNotIn("https://github.com", html)

    def test_items_by_state_with_github_links(self):
        html = render(self.led, self.cfg)
        self.assertIn('href="https://github.com/mkny13/mahler/issues/5"', html)
        self.assertIn("Read-only status page", html)
        self.assertIn("held by", html)
        self.assertIn("run-32", html)

    def test_title_is_escaped(self):
        html = render(self.led, self.cfg)
        self.assertIn("A needs-you item &lt;script&gt;", html)
        # mahler#134: the page now has its own <script> (collapse-state), so
        # assert on the raw unescaped title rather than the bare tag.
        self.assertNotIn("A needs-you item <script>", html)

    def test_quota_gauges(self):
        html = render(self.led, self.cfg)
        self.assertIn("claude", html)
        self.assertIn("42%", html)          # gauge label
        self.assertIn("width:42%", html)    # gauge fill
        self.assertIn("unknown limit", html)    # cline-free

    def test_quota_reset_countdown_chips(self):
        # mahler#52: badge chips show reset countdowns on the quota card header
        from datetime import timedelta
        from mahler.ledger import iso
        led = make_led()
        later = iso(led.now() + timedelta(hours=2, minutes=5))
        led.record_usage("claude", "weekly", 10.0, later)
        html = render(led, self.cfg)
        self.assertRegex(html, r'class="chip">wk in 2h [0-5]?\dm</span>')

    def test_paused_banner(self):
        self.led.set_kv("paused", "1")
        html = render(self.led, self.cfg)
        self.assertIn("PAUSED", html)

    def test_burst_indicator_in_quota(self):
        """D23: during a burst, the status page shows a burst banner."""
        from datetime import timedelta
        from mahler.ledger import iso
        led = make_led()
        five_reset = iso(led.now() + timedelta(minutes=30))
        weekly_reset = iso(led.now() + timedelta(hours=2))
        led.record_usage("claude", "5h", 85.0, five_reset)
        led.record_usage("claude", "weekly", 85.0, weekly_reset)
        html = render(led, self.cfg)
        self.assertIn("burst active", html)
        snap = serve.snapshot(self.cfg, led)
        self.assertEqual(snap["burst"], "weekly")

    def test_events_capped_at_30(self):
        for j in range(40):
            self.led.event("tick", detail=f"event {j}")
        snap = serve.snapshot(self.cfg, self.led)
        self.assertEqual(len(snap["events"]), serve.EVENTS_SHOWN)
        self.assertEqual(snap["events"][0]["detail"], "event 10")   # oldest kept
        self.assertEqual(snap["events"][-1]["detail"], "event 39")  # newest
        html = render(self.led, self.cfg)
        self.assertIn("event 39", html)
        self.assertNotIn("event 0<", html)

    def test_page_contract(self):
        html = render(self.led, self.cfg)
        self.assertIn('http-equiv="refresh" content="30"', html)   # auto-refresh
        self.assertIn('name="viewport"', html)                     # phone-friendly
        self.assertIn("prefers-color-scheme: dark", html)          # dark theme
        self.assertIn("color-scheme", html)
        self.assertIn("<!DOCTYPE html>", html)

    def test_items_without_repo_render_without_link(self):
        html = render(self.led, {"defaults": {}, "platforms": self.cfg["platforms"],
                                 "projects": {}})
        self.assertNotIn("https://github.com", html)
        self.assertIn("mahler#5", html)     # still listed, just not linked

    def test_child_items_show_parent(self):
        self.led.upsert_item("mahler", 10, title="Child issue", state="ready",
                            priority=2, parent=5)
        html = render(self.led, self.cfg)
        self.assertIn("mahler#10", html)
        self.assertIn("part of", html)
        self.assertIn("mahler#5", html)

    def test_running_child_shows_parent(self):
        self.led.upsert_item("mahler", 12, title="Another child", state="working",
                            priority=2, parent=5)
        self.led.claim("mahler", 12, "run-33", "auto", 10, platform="cline-free", run_id=2)
        self.led.create_run(project="mahler", number=12, role="build",
                           platform="cline-free", epoch=1)
        html = render(self.led, self.cfg)
        self.assertIn("mahler#12", html)
        self.assertIn("part of", html)
        self.assertIn("mahler#5", html)

    def test_ui_order_is_running_quota_items_events(self):
        # Issue: UI should show Running, then Quota, then the rest
        html = render(self.led, self.cfg)
        running_pos = html.find("<h2>Running")
        quota_pos = html.find("<h2>Quota")
        items_pos = html.find("<h2>Items")
        events_pos = html.find("<h2>Events")
        self.assertLess(running_pos, quota_pos, "Running should appear before Quota")
        self.assertLess(quota_pos, items_pos, "Quota should appear before Items")
        self.assertLess(items_pos, events_pos, "Events should appear after Items")

    def test_header_has_jump_links(self):
        # mahler#134: compact header with anchors to each section
        html = render(self.led, self.cfg)
        self.assertIn('<header class="topbar">', html)
        self.assertIn('<nav class="jump"', html)
        for sid in ("running", "quota", "items", "events"):
            self.assertIn(f'href="#{sid}"', html)

    def test_sections_are_collapsible_details(self):
        # mahler#134: each section wraps in details/summary, open by default
        html = render(self.led, self.cfg)
        for sid in ("running", "quota", "items", "events"):
            self.assertIn(f'<details class="section" id="{sid}" open>', html)
        self.assertEqual(html.count("<summary>"), 4)
        self.assertIn("<summary><h2>Running", html)
        self.assertIn("<summary><h2>Events", html)

    def test_collapse_state_persists_via_local_storage(self):
        # mahler#134: open/closed state survives the 30s meta-refresh
        html = render(self.led, self.cfg)
        self.assertIn("localStorage", html)
        self.assertIn("mahler.section.", html)
        self.assertIn('http-equiv="refresh" content="30"', html)  # refresh unchanged


class TestServer(unittest.TestCase):
    """The real HTTP surface, on an ephemeral localhost port."""

    def setUp(self):
        self.cfg = make_cfg()
        self.led = make_led()
        handler = type("Handler", (serve._Handler,),
                       {"led": self.led, "lock": threading.Lock(),
                        "load_cfg": staticmethod(lambda: self.cfg)})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def get(self, path, method="GET", data=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     method=method, data=data)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, dict(r.headers), r.read().decode()
        except urllib.error.HTTPError as e:
            try:
                return e.code, dict(e.headers), e.read().decode()
            finally:
                e.close()

    def test_binds_loopback_only(self):
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")

    def test_get_root_is_html(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertIn("Mahler", body)

    def test_unknown_path_404(self):
        status, _, _ = self.get("/status")
        self.assertEqual(status, 404)

    def test_write_methods_rejected(self):
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            status, _, _ = self.get("/", method=method, data=b"{}")
            self.assertEqual(status, 405, method)


class TestDynamicConfigReload(unittest.TestCase):
    """mahler#50: a project added to config.toml after `mahler serve` starts
    must get GitHub links without restarting the server."""

    def setUp(self):
        self.led = make_led()
        self.led.upsert_item("phish-in", 3, title="Some new-project item",
                              state="ready", priority=2)
        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.tmp.name, "config.toml")
        with open(self.config_path, "w") as fh:
            fh.write('[projects.mahler]\nrepo = "mkny13/mahler"\n')
        self.patcher = mock.patch.object(serve.config, "CONFIG_PATH", self.config_path)
        self.patcher.start()

        handler = type("Handler", (serve._Handler,),
                       {"led": self.led, "lock": threading.Lock(),
                        "load_cfg": staticmethod(serve.config.load)})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.patcher.stop()
        self.tmp.cleanup()

    def get(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=5) as r:
            return r.read().decode()

    def test_project_added_after_start_gets_links_without_restart(self):
        # phish-in isn't in config.toml yet: item renders, but no link.
        body = self.get()
        self.assertIn("phish-in#3", body)
        self.assertNotIn('href="https://github.com/mkny13/phish-in', body)

        # Add phish-in to config.toml while the server is already running.
        with open(self.config_path, "w") as fh:
            fh.write('[projects.mahler]\nrepo = "mkny13/mahler"\n'
                      '[projects.phish-in]\nrepo = "mkny13/phish-in"\n')

        body = self.get()
        self.assertIn('href="https://github.com/mkny13/phish-in/issues/3"', body)


class TestCliWiring(unittest.TestCase):
    def test_serve_subcommand_passes_args(self):
        from mahler import cli
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(cli.config, "DB_PATH",
                                   os.path.join(tmp, "mahler.db")), \
                 mock.patch("mahler.serve.serve", return_value=0) as mock_serve:
                rc = cli.main(["serve", "--host", "0.0.0.0", "--port", "9111"])
        self.assertEqual(rc, 0)
        self.assertEqual(mock_serve.call_args.args[2], "0.0.0.0")
        self.assertEqual(mock_serve.call_args.args[3], 9111)

    def test_serve_subcommand_defaults(self):
        from mahler import cli
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(cli.config, "DB_PATH",
                                   os.path.join(tmp, "mahler.db")), \
                 mock.patch.object(cli.config, "CONFIG_PATH",
                                   os.path.join(tmp, "nonexistent.toml")), \
                 mock.patch("mahler.serve.serve", return_value=0) as mock_serve:
                rc = cli.main(["serve"])
        self.assertEqual(rc, 0)
        self.assertEqual(mock_serve.call_args.args[2], "127.0.0.1")
        self.assertEqual(mock_serve.call_args.args[3], 8787)



if __name__ == "__main__":
    unittest.main()

