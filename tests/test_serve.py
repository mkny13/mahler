"""`mahler serve`: the console's HTTP surface (DESIGN D10, D27)."""

from datetime import datetime, timedelta
import io
import json
import os
import queue
import re
import tempfile
import threading
import time
import types
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



class TestClientLog(_Served):
    payload = {"action": "pause", "error": "network failed", "elapsed_ms": 12.5}

    def events(self):
        return [json.loads(row[0]) for row in self.led.con.execute(
            "SELECT detail FROM events WHERE kind='console_client_error'")]

    def test_records_redacted_detail_and_empty_response(self):
        payload = dict(self.payload, action="TOKEN=secret", error="PASSWORD=hidden")
        status, _, body = self.post("client_log", payload)
        self.assertEqual((status, body), (204, ""))
        self.assertEqual(self.events(), [{
            "action": "TOKEN=<redacted>", "error": "PASSWORD=<redacted>", "elapsed_ms": 12.5}])

    def test_malformed_reports_are_silently_discarded(self):
        bodies = [b"not JSON", b"[]", b"null", b"{}", b'"text"', bytes([255]),
                  b'{"action":1,"error":"x","elapsed_ms":1}',
                  b'{"action":"x","error":[],"elapsed_ms":1}']
        bodies += [json.dumps(dict(self.payload, elapsed_ms=value)).encode()
                   for value in ("1", None, True, float("nan"), float("inf"))]
        for body in bodies:
            with self.subTest(body=body):
                status, _, response = self.request("/api/client_log", "POST", body, {
                    "Content-Type": "application/json", "X-Mahler-Console": "1"})
                self.assertEqual((status, response), (204, ""))
        self.assertEqual(self.events(), [])

    def test_write_failure_returns_204(self):
        with mock.patch.object(self.led, "event", side_effect=RuntimeError("broken")):
            self.assertEqual(self.post("client_log", self.payload)[0], 204)

    def test_auth_still_applies(self):
        for headers, expected in [
            ({"X-Mahler-Console": ""}, 403),
            ({"Content-Type": "text/plain"}, 415),
            ({"Origin": "https://elsewhere.example"}, 403),
        ]:
            self.assertEqual(self.post("client_log", self.payload, headers)[0], expected)
        with mock.patch.object(serve, "write_allowed_from", return_value=False):
            self.assertEqual(self.post("client_log", self.payload)[0], 403)
        self.assertEqual(self.events(), [])

    def test_rate_cap_and_window_reset(self):
        with mock.patch.object(serve.time, "monotonic", return_value=100):
            for _ in range(serve.CLIENT_LOG_MAX_EVENTS + 2):
                self.assertEqual(self.post("client_log", self.payload)[0], 204)
        self.assertEqual(len(self.events()), serve.CLIENT_LOG_MAX_EVENTS)
        with mock.patch.object(serve.time, "monotonic",
                               return_value=100 + serve.CLIENT_LOG_WINDOW_SECONDS):
            self.assertEqual(self.post("client_log", self.payload)[0], 204)
        self.assertEqual(len(self.events()), serve.CLIENT_LOG_MAX_EVENTS + 1)

    def test_ip_budgets_are_independent(self):
        limiter = serve._ClientLogLimiter()
        for _ in range(serve.CLIENT_LOG_MAX_EVENTS):
            self.assertTrue(limiter.allow("127.0.0.1"))
        self.assertFalse(limiter.allow("127.0.0.1"))
        self.assertTrue(limiter.allow("100.64.0.2"))

    def test_does_not_wait_for_action_lock_or_load_config(self):
        results = queue.Queue()
        with mock.patch.object(self.httpd.RequestHandlerClass, "load_cfg",
                               side_effect=AssertionError("must not load config")):
            with self.httpd.RequestHandlerClass.lock:
                worker = threading.Thread(
                    target=lambda: results.put(self.post("client_log", self.payload)))
                worker.start()
                try:
                    status, _, _ = results.get(timeout=1)
                    self.assertEqual(status, 204)
                finally:
                    # Release the lock even if a regression leaves the request waiting.
                    pass
            worker.join(timeout=5)
        self.assertEqual(len(self.events()), 1)

    def test_browser_reports_without_waiting_for_telemetry(self):
        import shutil
        import subprocess
        from pathlib import Path
        if not shutil.which("node"):
            self.skipTest("Node is needed for browser logic regression")
        source = (Path(__file__).parents[1] / "mahler/console/console.js").read_text()
        post = source[source.index("  function post("):source.index("  function numberValue")]
        script = r"""
const assert = require("assert");
var POST_TIMEOUT_MS = 5, CAPTURE_TIMEOUT_MESSAGE = "capture timeout";
var window = {console: {warn() {}}};
var errors = [], reports = [], mode;
function showErrorToast(message) { errors.push(message); }
function refresh() { return Promise.resolve(); }
function fetch(url, options) {
  if (url === "/api/client_log") {
    reports.push(JSON.parse(options.body));
    assert.equal(options.headers["X-Mahler-Console"], "1");
    assert.equal(options.keepalive, true);
    if (mode === "network") return Promise.reject(new Error("logging failed"));
    if (mode === "non-json") throw new Error("sync logging failure");
    return new Promise(() => {}); // Must not delay retry/toast flow.
  }
  if (mode === "timeout") return new Promise((resolve, reject) => {
    options.signal.addEventListener("abort", () => {
      var err = new Error("aborted"); err.name = "AbortError"; reject(err);
    });
  });
  if (mode === "network") return Promise.reject(new Error("network failed"));
  return Promise.resolve({json: () => mode === "non-json" ?
    Promise.reject(new Error("bad JSON")) : Promise.resolve({ok: false, error: "refused"})});
}
""" + post + r"""
(async () => {
  for (mode of ["timeout", "network", "refused", "non-json"]) {
    reports = []; errors = [];
    await post("pause", {}).catch(() => {});
    assert.equal(reports.length, 1);
    assert.equal(reports[0].action, "pause");
    assert.equal(typeof reports[0].elapsed_ms, "number");
    assert.ok(reports[0].elapsed_ms >= 0);
    assert.match(reports[0].error, /timeout|network failed|refused|non-JSON/);
    assert.equal(errors.length, 1);
  }
})().catch(err => { console.error(err); process.exitCode = 1; });
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


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


class TestRequestLogging(_Served):
    def setUp(self):
        super().setUp()
        self.lines = queue.Queue()
        sink = mock.Mock()
        def capture(line):
            if "method=" in line:
                self.lines.put(line)
            return len(line)
        sink.write.side_effect = capture
        patcher = mock.patch.object(serve, "sys", mock.Mock(stderr=sink))
        patcher.start()
        self.addCleanup(patcher.stop)

    def next_line(self, event):
        line = self.lines.get(timeout=5)
        timestamp = line.split()[1]
        self.assertIsNotNone(datetime.fromisoformat(timestamp).tzinfo)
        self.assertIn(f'event="{event}"', line)
        self.assertRegex(line, r'duration_ms=\d+\.\d{3}')
        return line

    def test_start_is_visible_during_lock_wait_and_sleeping_action(self):
        entered = threading.Event()
        release = threading.Event()
        result = []

        def slow_action(*args):
            entered.set()
            release.wait(timeout=5)
            time.sleep(0.03)
            return {"message": "done"}

        lock = self.httpd.RequestHandlerClass.lock
        with mock.patch.object(serve.actions, "run", side_effect=slow_action):
            worker = threading.Thread(target=lambda: result.append(self.post("pause")))
            try:
                with lock:
                    worker.start()
                    start = self.next_line("start")
                    self.assertIn('method="POST" path="/api/pause" status=- size=0', start)
                    self.assertFalse(entered.is_set())
                    self.assertTrue(self.lines.empty())
                    time.sleep(0.03)
                self.assertTrue(entered.wait(timeout=5))
                self.assertTrue(self.lines.empty())
            finally:
                release.set()
                worker.join(timeout=5)
            end = self.next_line("end")
        self.assertEqual(result[0][0], 200)
        self.assertIn(f'status=200 size={len(result[0][2].encode())}', end)
        duration = float(re.search(r'duration_ms=([\d.]+)', end)[1])
        self.assertGreaterEqual(duration, 60)
        self.assertTrue(self.lines.empty())

    def test_get_and_error_response_sizes(self):
        for path, status in (("/api/state", 200), ("/missing", 404)):
            with self.subTest(path=path):
                actual, _, body = self.request(path)
                self.assertEqual(actual, status)
                if status == 404:
                    self.next_line("code 404, message Not Found")
                end = self.next_line("end")
                self.assertIn(f'method="GET" path="{path}"', end)
                self.assertIn(f'status={status} size={len(body.encode())}', end)

    def test_broken_logging_does_not_fail_request(self):
        def broken_write(line):
            if "method=" in line:
                raise OSError("broken")
            return len(line)
        with mock.patch.object(serve.sys.stderr, "write", side_effect=broken_write), \
                mock.patch("builtins.print", side_effect=OSError("also broken")) as fallback:
            self.assertEqual(self.post("pause")[0], 200)
            self.httpd.shutdown()
            self.httpd.server_close()
            self.assertTrue(fallback.called)


class TestSlowRequests(_Served):
    def setUp(self):
        super().setUp()
        self.cfg["serve"]["slow_request_ms"] = 10

    def slow_event(self):
        row = self.led.q1(
            "SELECT * FROM events WHERE kind='console_slow_request' ORDER BY id DESC LIMIT 1")
        self.assertIsNotNone(row)
        return json.loads(row["detail"])

    def test_lock_wait_over_threshold_records_event(self):
        held = threading.Event()
        lock = self.httpd.RequestHandlerClass.lock

        def hold_lock():
            with lock:
                held.set()
                time.sleep(0.08)

        holder = threading.Thread(target=hold_lock)
        holder.start()
        self.assertTrue(held.wait(timeout=5))
        try:
            self.assertEqual(self.post("pause")[0], 200)
        finally:
            holder.join(timeout=5)

        detail = self.slow_event()
        self.assertEqual(detail["path"], "/api/pause")
        self.assertGreaterEqual(detail["wait_ms"], 10)
        self.assertGreaterEqual(detail["action_ms"], 0)

    def test_action_over_threshold_records_event(self):
        run = serve.actions.run

        def slow_action(*args):
            time.sleep(0.05)
            return run(*args)

        with mock.patch.object(serve.actions, "run", side_effect=slow_action):
            self.assertEqual(self.post("pause")[0], 200)

        detail = self.slow_event()
        self.assertEqual(detail["path"], "/api/pause")
        self.assertGreaterEqual(detail["action_ms"], 10)
        self.assertGreaterEqual(detail["wait_ms"], 0)

    def test_event_write_failure_does_not_fail_request(self):
        self.cfg["serve"]["slow_request_ms"] = 0
        event = self.led.event

        def fail_slow_event(kind, *args, **kwargs):
            if kind == "console_slow_request":
                raise OSError("event sink unavailable")
            return event(kind, *args, **kwargs)

        with mock.patch.object(self.led, "event", side_effect=fail_slow_event) as write:
            status, _, body = self.post("pause")

        self.assertEqual((status, json.loads(body)), (200, {"ok": True}))
        self.assertTrue(self.led.paused())
        self.assertTrue(any(call.args[0] == "console_slow_request"
                            for call in write.call_args_list))


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
        for run in self.led.active_runs():
            self.led.update_run(run["id"], status="done")
        self.led.upsert_item("mahler", 10, title="Waiting", state="ready")
        with mock.patch("mahler.presence.last_claude_activity",
                        return_value=self.led.now() - timedelta(minutes=4)) as activity:
            before = json.loads(self.request("/api/state")[2])
            self.assertIn('"end_session"', json.dumps(before["banners"]))
            self.assertIn('"end_session"', json.dumps(before["idle"]))
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
            self.assertTrue(serve.write_allowed_from(addr), f"{addr}")
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
        body = self.request("/?view=backlog")[2]
        self.assertNotIn("phish-in", body)
        self.write_config('[projects.phish-in]\nenabled = true\nrepo = "mkny13/couch-tour"\n'
                          'hot_hold = false\n')
        body = self.request("/?view=backlog")[2]
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


class _EnoughRetries(Exception):
    """Ends serve's never-shutting-down update loop once it has retried."""


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
        sleeps = []
        err = io.StringIO()

        def fake_sleep(seconds):
            # The real loop retries forever, so a thread would outlive the test
            # and write to whatever sys.stderr a later test redirects to.
            sleeps.append(seconds)
            if len(sleeps) > 3:
                raise _EnoughRetries

        clock = types.SimpleNamespace(sleep=fake_sleep, monotonic=time.monotonic)
        with (mock.patch.object(httpd, "shutdown") as mock_shutdown,
              mock.patch.object(serve, "time", clock),
              mock.patch.object(serve.sys, "stderr", err)):
            with self.assertRaises(_EnoughRetries):
                serve._check_for_update(httpd, get_head, old_head, 0.05)
        mock_shutdown.assert_not_called()
        self.assertEqual(sleeps, [0.05] * 4)
        self.assertEqual(err.getvalue().count("cannot read HEAD, will retry"), 3)

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

    def _make_server(self):
        handler = type("Handler", (serve._Handler,),
                       {"led": None, "lock": threading.Lock(),
                        "load_cfg": staticmethod(lambda: {})})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(httpd.server_close)
        return httpd


class TestCliWiring(unittest.TestCase):
    def test_serve_subcommand_passes_args(self):
        from mahler import cli
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(cli.config, "DB_PATH",
                                   os.path.join(tmp, "mahler.db")), \
                 mock.patch.object(cli.config, "CONFIG_PATH",
                                   os.path.join(tmp, "nonexistent.toml")), \
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

    def test_releases_feed_decodes_project_name(self):
        project = "phish-in/couch-tour"
        self.cfg["projects"][project] = {"enabled": True, "repo": "mkny13/couch-tour"}

        status, _, body = self.request("/api/releases/phish-in%2Fcouch-tour.json")

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["project"], project)

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
