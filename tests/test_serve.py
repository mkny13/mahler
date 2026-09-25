"""`mahler serve`: the console's HTTP surface (DESIGN D10, D27)."""

from datetime import timedelta
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from mahler import config, serve
from mahler.ledger import Ledger


def make_cfg():
    """What config.load() returns: the defaults plus one enabled project."""
    user = {"projects": {"mahler": {"enabled": True, "repo": "mkny13/mahler",
                                    "path": "/nonexistent/mahler", "hot_hold": False}}}
    return config.resolve_platforms(config._merge(config.DEFAULTS, user))


def make_led():
    # thread_safe=True: request threads read this connection, exactly like
    # cmd_serve re-opens the real DB thread-safe
    led = Ledger(":memory:", thread_safe=True)
    led.upsert_item("mahler", 5, title="Console", state="working", priority=2)
    led.upsert_item("mahler", 9, title="A needs-you item <script>", state="needs_you",
                    issue_body="Full body <b>must be escaped</b>", priority=1)
    led.create_run(project="mahler", number=5, role="build", platform="cline-free", epoch=1)
    return led


class _Served(unittest.TestCase):
    """A real server on an ephemeral localhost port."""

    load_cfg = None

    def setUp(self):
        self.cfg = make_cfg()
        self.led = make_led()
        load = self.load_cfg or (lambda: self.cfg)
        handler = type("Handler", (serve._Handler,),
                       {"led": self.led, "lock": threading.Lock(),
                        "load_cfg": staticmethod(load)})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        # A short poll_interval keeps shutdown() fast (mahler#95).
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def request(self, path, method="GET", data=None, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, dict(r.headers), r.read().decode()
        except urllib.error.HTTPError as e:
            try:
                return e.code, dict(e.headers), e.read().decode()
            finally:
                e.close()

    def post(self, action, body=None, headers=None):
        h = {"Content-Type": "application/json", "X-Mahler-Console": "1"}
        h.update(headers or {})
        return self.request(f"/api/{action}", "POST", json.dumps(body or {}).encode(), h)


class TestPages(_Served):
    def test_binds_loopback_only(self):
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")

    def test_root_is_the_console(self):
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("<title>Mahler</title>", body)
        self.assertIn("A needs-you item &lt;script&gt;", body)
        self.assertIn("Full body &lt;b&gt;must be escaped&lt;/b&gt;", body)
        self.assertIn('data-need-details="mahler#9"', body)
        self.assertIn('href="https://github.com/mkny13/mahler/issues/9"', body)

    def test_fragment_and_state(self):
        status, _, frag = self.request("/fragment")
        self.assertEqual(status, 200)
        self.assertNotIn("<html", frag)
        status, headers, body = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        s = json.loads(body)
        self.assertEqual(s["system"]["label"], "RUNNING · 1")
        self.assertEqual([n["ref"] for n in s["needs"]], ["mahler#9"])

    def test_stats_range_query_reaches_server_rendered_state(self):
        status, _, body = self.request("/api/state?range=last7")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["stats_range"], "last7")

        status, _, body = self.request(
            "/api/state?range=custom&start=2026-09-01&end=2026-09-12")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["stats_range"], "custom:2026-09-01:2026-09-12")

    def test_unknown_path_404(self):
        self.assertEqual(self.request("/status")[0], 404)

    def test_other_methods_rejected(self):
        for method in ("PUT", "DELETE", "PATCH"):
            self.assertEqual(self.request("/", method=method, data=b"{}")[0], 405, method)
        self.assertEqual(self.request("/", "POST", b"{}")[0], 405)
        self.assertEqual(self.post("drop_tables")[0], 404)


class TestWrites(_Served):
    def test_pause_then_resume(self):
        status, _, body = self.post("pause")
        self.assertEqual((status, json.loads(body)), (200, {"ok": True}))
        self.assertTrue(self.led.paused())
        self.post("resume")
        self.assertFalse(self.led.paused())

    def test_project_brief_acknowledgement(self):
        self.led.snapshot_release_item("mahler", 5, title="Console",
                                       labels=["type:feature"])
        self.led.event("shipped", "mahler", 5, {"pr": 12})
        cursor = self.led.q1("SELECT max(id) AS id FROM events")["id"]
        status, _, body = self.post("brief_seen", {"project": "mahler", "upto": cursor})
        self.assertEqual((status, json.loads(body)), (200, {"ok": True, "upto": cursor}))
        status, _, body = self.request("/api/state")
        brief = json.loads(body)["briefs"][0]
        self.assertEqual((brief["count"], brief["seen"]), (0, cursor))

    def test_end_session_records_override(self):
        self.cfg["projects"]["mahler"]["hot_hold"] = True
        with mock.patch("mahler.presence.last_claude_activity",
                        return_value=self.led.now() - timedelta(minutes=4)) as activity:
            before = json.loads(self.request("/api/state")[2])
            self.assertIn('"end_session"', json.dumps(before["banners"]))
            status, _, body = self.post("end_session", {"project": "mahler"})
            self.assertEqual((status, json.loads(body)), (200, {"ok": True}))
            self.assertIsNotNone(self.led.get_kv("hot_hold_end:mahler"))
            event = self.led.q1("SELECT * FROM events WHERE kind='hot_hold_end'")
            self.assertEqual(event["project"], "mahler")
            after = json.loads(self.request("/api/state")[2])
            self.assertNotIn('"end_session"', json.dumps(after["banners"]))
            self.assertNotIn('"end_session"', json.dumps(after["idle"]))
            self.assertNotIn('data-act="end_session"', self.request("/fragment")[2])
            activity.return_value = self.led.now() + timedelta(seconds=1)
            self.assertIn('data-act="end_session"', self.request("/fragment")[2])
        self.assertEqual(self.post("end_session", {"project": "unknown"})[0], 400)

    def test_same_origin_is_allowed(self):
        status, _, _ = self.post("pause", headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)

    def test_guard(self):
        cases = [
            ({"X-Mahler-Console": ""}, 403),                         # no header
            ({"Origin": "https://evil.example"}, 403),               # cross-site
            ({"Content-Type": "application/x-www-form-urlencoded"}, 415),
        ]
        for headers, code in cases:
            status, _, body = self.post("pause", headers=headers)
            self.assertEqual(status, code, headers)
            self.assertFalse(json.loads(body)["ok"])
        self.assertFalse(self.led.paused())

    def test_bad_json_and_bad_input(self):
        h = {"Content-Type": "application/json", "X-Mahler-Console": "1"}
        self.assertEqual(self.request("/api/pause", "POST", b"{not json", h)[0], 400)
        status, _, body = self.post("clear_backoff", {"platforms": []})
        self.assertEqual(status, 400)
        self.assertIn("platforms", json.loads(body)["error"])

    def test_only_this_machine_and_the_tailnet_may_write(self):
        for addr in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "100.101.2.3", "fd7a:115c:a1e0::1"):
            self.assertTrue(serve.write_allowed_from(addr), addr)
        for addr in ("192.168.1.20", "10.0.0.2", "8.8.8.8", "not an ip"):
            self.assertFalse(serve.write_allowed_from(addr), addr)


class TestDynamicConfigReload(_Served):
    """mahler#50: a project added to config.toml after `mahler serve` starts
    shows up without restarting the server."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.tmp.name, "config.toml")
        self.write_config(extra="")
        self.patcher = mock.patch.object(serve.config, "CONFIG_PATH", self.config_path)
        self.patcher.start()
        self.load_cfg = serve.config.load
        super().setUp()
        self.led.upsert_item("phish-in", 3, title="Some new-project item", state="ready")

    def tearDown(self):
        super().tearDown()
        self.patcher.stop()
        self.tmp.cleanup()

    def write_config(self, extra):
        with open(self.config_path, "w") as fh:
            fh.write('[projects.mahler]\nenabled = true\nrepo = "mkny13/mahler"\n'
                     'hot_hold = false\n' + extra)

    def test_project_added_after_start_shows_without_restart(self):
        body = self.request("/")[2]
        self.assertNotIn("phish-in", body)
        self.write_config('[projects.phish-in]\nenabled = true\nrepo = "mkny13/couch-tour"\n'
                          'hot_hold = false\n')
        body = self.request("/")[2]
        self.assertIn('href="https://github.com/mkny13/couch-tour/issues/3"', body)

    def test_settings_get_save_and_reload(self):
        status, headers, body = self.request("/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        form = json.loads(body)
        self.assertNotIn("env", body)
        form["concurrency"]["total"] = 5
        form["projects"][0]["max_parallel"] = 2
        status, _, body = self.post("settings", form)
        self.assertEqual((status, json.loads(body)["ok"]), (200, True))
        loaded = config.load(self.config_path)
        self.assertEqual(loaded["concurrency"]["total"], 5)
        self.assertEqual(config.project_policy(loaded, "mahler")["max_parallel"], 2)
        self.assertEqual(json.loads(self.request("/api/settings")[2])["concurrency"]["total"], 5)

    def test_settings_reject_malformed_before_write(self):
        form = json.loads(self.request("/api/settings")[2])
        form["concurrency"]["total"] = 0
        with open(self.config_path, encoding="utf-8") as fh:
            before = fh.read()
        status, _, body = self.post("settings", form)
        self.assertEqual(status, 400)
        self.assertIn("concurrency total", json.loads(body)["error"])
        with open(self.config_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)


class TestAutoRestart(unittest.TestCase):
    """mahler#256: serve restarts itself when its code updates."""

    def test_head_change_triggers_shutdown(self):
        httpd = self._make_server()
        get_head = self._make_get_head(["abc123", "def456"])
        old_head = ["abc123"]
        with mock.patch.object(httpd, "shutdown") as mock_shutdown:
            t = threading.Thread(
                target=serve._check_for_update,
                args=(httpd, get_head, old_head, 0.05),
                daemon=True,
            )
            t.start()
            t.join(timeout=5)
        mock_shutdown.assert_called_once()
        self.assertEqual(old_head[0], "def456")

    def test_git_error_never_shutdown(self):
        httpd = self._make_server()
        get_head = self._make_get_head([None])
        old_head = [None]
        with mock.patch.object(httpd, "shutdown") as mock_shutdown:
            t = threading.Thread(
                target=serve._check_for_update,
                args=(httpd, get_head, old_head, 0.05),
                daemon=True,
            )
            t.start()
            t.join(timeout=0.3)
        mock_shutdown.assert_not_called()

    @staticmethod
    def _make_get_head(values):
        i = [0]
        def get_head():
            if i[0] < len(values):
                v = values[i[0]]
                i[0] += 1
                return v
            return values[-1]
        return get_head

    @staticmethod
    def _make_server():
        handler = type("Handler", (serve._Handler,),
                       {"led": None, "lock": threading.Lock(),
                        "load_cfg": staticmethod(lambda: {})})
        return ThreadingHTTPServer(("127.0.0.1", 0), handler)


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



class TestRunLog(_Served):
    def test_log_returns_lines_and_status(self):
        # Insert a running run
        run_id = 9999
        self.led.con.execute("INSERT INTO runs (id, project, number, role, platform, epoch, status, started_at) VALUES (?, 'mahler', 1, 'build', 'agy-gemini', 1, 'running', 1)", (run_id,))
        
        status, headers, body = self.request(f"/api/run/{run_id}/log")
        self.assertEqual(status, 200)
        
        import json
        obj = json.loads(body)
        self.assertIn("lines", obj)
        self.assertIn("status", obj)

    def test_log_404_for_unknown_run(self):
        status, _, _ = self.request("/api/run/9998/log")
        self.assertEqual(status, 404)

    def test_log_404_for_ended_run(self):
        run_id = 9997
        self.led.con.execute("INSERT INTO runs (id, project, number, role, platform, epoch, status, started_at) VALUES (?, 'mahler', 1, 'build', 'agy-gemini', 1, 'ended', 1)", (run_id,))
        status, _, _ = self.request(f"/api/run/{run_id}/log")
        self.assertEqual(status, 404)
        
    def test_log_404_for_bad_id(self):
        status, _, _ = self.request("/api/run/abc/log")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()


class TestAnswers(_Served):
    def test_answer_returns_id_and_undo(self):
        status, _, body = self.post('answer', {'project': 'mahler', 'number': 9, 'text': 'Yes'})
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertTrue(result['ok'])
        self.assertIsInstance(result['id'], int)
        self.assertEqual(self.post('answer_undo', {'id': result['id']})[0], 200)
        self.assertEqual(self.post('answer_undo', {'id': result['id']})[0], 400)


class TestAttachments(_Served):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.patcher = mock.patch.object(serve.config, "ATTACHMENTS_DIR", self.tmp.name)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()
        super().tearDown()

    def test_upload(self):
        import base64
        data = b"\x89PNG\r\n\x1a\n" + b"some_data"
        status, _, body = self.post("attach", {"name": "test.png", "type": "image/png", "data": base64.b64encode(data).decode()})
        self.assertEqual(status, 200)
        res = json.loads(body)
        self.assertTrue(res["ok"])
        self.assertIn("id", res)
        self.assertEqual(res["name"], "test.png")
        filename = res["id"]
        self.assertTrue(filename.endswith(".png"))
        
        path = os.path.join(self.tmp.name, filename)
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(oct(os.stat(path).st_mode)[-3:], "600")

    def test_upload_magic_bytes_check(self):
        import base64
        data = b"wrong_magic_bytes"
        status, _, body = self.post("attach", {"name": "test.png", "type": "image/png", "data": base64.b64encode(data).decode()})
        self.assertEqual(status, 400)
        self.assertIn("magic bytes", json.loads(body)["error"])
        
    def test_download(self):
        import base64
        data = b"\x89PNG\r\n\x1a\n" + b"some_data"
        status, _, body = self.post("attach", {"name": "test.png", "type": "image/png", "data": base64.b64encode(data).decode()})
        filename = json.loads(body)["id"]

        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/attachments/{filename}", method="GET")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(dict(r.headers)["Content-Type"], "image/png")
            self.assertEqual(dict(r.headers)["Cache-Control"], "private, max-age=86400")
            self.assertEqual(dict(r.headers)["X-Content-Type-Options"], "nosniff")
            self.assertEqual(r.read(), data)

    def test_download_path_traversal(self):
        status, _, _ = self.request("/attachments/../config.toml")
        self.assertEqual(status, 404)


class TestReleasesFeed(_Served):
    def test_releases_feed_empty_state_and_headers(self):
        status, headers, body = self.request("/api/releases/mahler")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        feed = json.loads(body)
        self.assertEqual(feed["schema_version"], 1)
        self.assertEqual(feed["project"], "mahler")
        self.assertTrue(feed["generated_at"].endswith("Z"))
        self.assertEqual(feed["releases"], [])

    def test_releases_feed_url_variants(self):
        for path in ("/api/releases/mahler", "/api/releases/mahler.json", "/api/projects/mahler/releases.json"):
            status, _, body = self.request(path)
            self.assertEqual(status, 200)
            feed = json.loads(body)
            self.assertEqual(feed["project"], "mahler")

    def test_releases_feed_unknown_or_disabled_project(self):
        status, _, _ = self.request("/api/releases/nonexistent")
        self.assertEqual(status, 404)

        # Disabled project returns 404
        self.cfg["projects"]["mahler"]["enabled"] = False
        status, _, _ = self.request("/api/releases/mahler")
        self.assertEqual(status, 404)

    def test_releases_feed_limit_query_param(self):
        status, _, _ = self.request("/api/releases/mahler?limit=0")
        self.assertEqual(status, 400)
        status, _, _ = self.request("/api/releases/mahler?limit=-5")
        self.assertEqual(status, 400)
        status, _, _ = self.request("/api/releases/mahler?limit=abc")
        self.assertEqual(status, 400)

    def test_releases_feed_populated_release(self):
        self.led.snapshot_release_item("mahler", 42, pr=43, title="New feature", summary="Added awesome feature",
                                       merge_sha="sha_feat", labels=["type:feature"])
        self.led.create_release("mahler", "1.0.0", checkpoint_sha="sha_feat",
                                published_at="2026-09-17T01:00:00Z", remote_url="https://github.com/mkny13/mahler/releases/tag/v1.0.0")

        status, _, body = self.request("/api/releases/mahler?limit=5")
        self.assertEqual(status, 200)
        feed = json.loads(body)
        self.assertEqual(len(feed["releases"]), 1)
        rel = feed["releases"][0]
        self.assertEqual(rel["version"], "1.0.0")
        self.assertEqual(rel["checkpoint_sha"], "sha_feat")
        self.assertEqual(rel["published_at"], "2026-09-17T01:00:00Z")
        self.assertEqual(len(rel["sections"]["features"]), 1)
        self.assertEqual(rel["sections"]["features"][0]["number"], 42)
        self.assertEqual(rel["sections"]["features"][0]["pr"], 43)
        self.assertEqual(rel["sections"]["features"][0]["title"], "New feature")
        self.assertEqual(rel["sections"]["features"][0]["summary"], "Added awesome feature")
        # Ensure no internal fields leaked
        self.assertNotIn("merge_sha", rel["sections"]["features"][0])
        self.assertNotIn("labels", rel["sections"]["features"][0])
