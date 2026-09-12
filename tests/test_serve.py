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

    def test_items_by_state_with_github_links(self):
        html = render(self.led, self.cfg)
        self.assertIn('href="https://github.com/mkny13/mahler/issues/5"', html)
        self.assertIn("Read-only status page", html)
        self.assertIn("held by", html)
        self.assertIn("run-32", html)

    def test_title_is_escaped(self):
        html = render(self.led, self.cfg)
        self.assertIn("A needs-you item &lt;script&gt;", html)
        self.assertNotIn("<script>", html)

    def test_quota_gauges(self):
        html = render(self.led, self.cfg)
        self.assertIn("claude", html)
        self.assertIn("42%", html)          # gauge label
        self.assertIn("width:42%", html)    # gauge fill
        self.assertIn("unmetered", html)    # cline-free

    def test_paused_banner(self):
        self.led.set_kv("paused", "1")
        html = render(self.led, self.cfg)
        self.assertIn("PAUSED", html)

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


class TestServer(unittest.TestCase):
    """The real HTTP surface, on an ephemeral localhost port."""

    def setUp(self):
        self.cfg = make_cfg()
        self.led = make_led()
        handler = type("Handler", (serve._Handler,),
                       {"cfg": self.cfg, "led": self.led,
                        "lock": threading.Lock()})
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
            return e.code, dict(e.headers), e.read().decode()

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


class TestCliWiring(unittest.TestCase):
    def test_serve_subcommand_passes_port(self):
        from mahler import cli
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(cli.config, "DB_PATH",
                                   os.path.join(tmp, "mahler.db")), \
                 mock.patch("mahler.serve.serve", return_value=0) as mock_serve:
                rc = cli.main(["serve", "--port", "9111"])
        self.assertEqual(rc, 0)
        self.assertEqual(mock_serve.call_args.args[2], 9111)


if __name__ == "__main__":
    unittest.main()

