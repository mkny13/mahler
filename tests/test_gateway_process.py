"""The credits gateway outlives the tick that launches a run (mahler#951).

Trial 3 (run 11765): the gateway was a thread in the one-shot `mahler tick`
process, so it vanished seconds after the run started and every later request
got ECONNREFUSED. These tests use a temp MAHLER_HOME and never touch the real
keychain, network or ~/.mahler.
"""
import http.client
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

from mahler import api_credits, config, finalize
from mahler.ledger import Ledger

from test_api_credits import Ctx, load

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.dirname(os.path.abspath(__file__))


class TokenStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "mahler.db")
        self.a, self.b = Ledger(self.path), Ledger(self.path)   # two processes' connections
        self.addCleanup(self.a.close)
        self.addCleanup(self.b.close)

    def test_a_token_registered_on_one_connection_is_honoured_on_another(self):
        token = api_credits._TokenStore(self.a, "g").register(7)
        other = api_credits._TokenStore(self.b, "g")
        self.assertEqual(other.lookup(token), 7)
        self.assertIsNone(other.lookup("not-a-token"))

    def test_revoke_forgets_every_token_of_the_run_and_only_that_run(self):
        store = api_credits._TokenStore(self.a, "g")
        first, second, other = store.register(7), store.register(7), store.register(8)
        store.revoke(7)
        reader = api_credits._TokenStore(self.b, "g")
        self.assertIsNone(reader.lookup(first))
        self.assertIsNone(reader.lookup(second))
        self.assertEqual(reader.lookup(other), 8)

    def test_only_a_hash_of_the_token_is_stored(self):
        token = api_credits._TokenStore(self.a, "g").register(7)
        rows = self.a.con.execute("SELECT key, value FROM kv").fetchall()
        self.assertFalse(any(token in f"{r['key']}{r['value']}" for r in rows))

    def test_tokens_are_namespaced_by_group(self):
        token = api_credits._TokenStore(self.a, "g1").register(7)
        self.assertIsNone(api_credits._TokenStore(self.b, "g2").lookup(token))


class EnsureGatewayProcessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(config, "STATE", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = load()
        self.led = Ledger(":memory:")
        self.addCleanup(self.led.close)
        self.name = "work-claude-api"
        self.pconf = self.cfg["platforms"][self.name]
        self.group = self.pconf["credit_pool"]["group"]
        self.state_path, _ = api_credits._gateway_files(self.group)

    def write_state(self, port=4321, started="s1"):
        with open(self.state_path, "w") as fh:
            json.dump({"pid": 1, "port": port, "started": started}, fh)

    def test_reuses_a_gateway_that_answers_for_this_group(self):
        self.write_state()
        spawned = []
        gw = api_credits.ensure_gateway_process(
            Ctx(self.cfg, self.led), self.name, self.pconf,
            spawn=lambda *a: spawned.append(a), healthy=lambda h, p, g: p == 4321)
        self.assertEqual(gw.port, 4321)
        self.assertEqual(spawned, [])

    def test_starts_a_new_gateway_when_the_recorded_one_is_dead(self):
        self.write_state(port=4321, started="old")
        up = {"on": False}

        def spawn(name, log):
            self.write_state(port=5555, started="new")
            up["on"] = True

        gw = api_credits.ensure_gateway_process(
            Ctx(self.cfg, self.led), self.name, self.pconf, spawn=spawn,
            healthy=lambda h, p, g: up["on"] and p == 5555, sleep=lambda s: None)
        self.assertEqual(gw.port, 5555)

    def test_a_gateway_that_never_comes_up_fails_the_launch(self):
        with self.assertRaisesRegex(RuntimeError, "did not start"):
            api_credits.ensure_gateway_process(
                Ctx(self.cfg, self.led), self.name, self.pconf, spawn=lambda *a: None,
                healthy=lambda *a: False, sleep=lambda s: None, timeout=0.01)

    def test_a_stale_state_file_is_not_trusted_without_a_health_answer(self):
        self.write_state(port=4321, started="old")
        with self.assertRaises(RuntimeError):
            api_credits.ensure_gateway_process(
                Ctx(self.cfg, self.led), self.name, self.pconf, spawn=lambda *a: None,
                healthy=lambda *a: False, sleep=lambda s: None, timeout=0.01)

    def test_in_memory_ledger_keeps_the_in_process_gateway(self):
        self.addCleanup(api_credits.shutdown_gateways)
        with mock.patch.object(api_credits, "api_key_for_run", return_value="k"):
            gw = api_credits.gateway_for_run(Ctx(self.cfg, self.led), self.name, self.pconf)
        self.assertIsInstance(gw, api_credits.Gateway)


class GatewayOutlivesLauncherTest(unittest.TestCase):
    """The regression itself: a real separate process, a launcher that exits."""

    DRIVER = textwrap.dedent('''
        import json, os, subprocess, sys
        sys.path.insert(0, {repo!r})
        sys.path.insert(0, {tests!r})
        from mahler import api_credits, config
        from mahler.ledger import Ledger
        from test_api_credits import Ctx, load

        def spawn(name, log):
            # the same detachment _spawn_gateway uses, minus the keychain read
            subprocess.Popen([sys.executable, {serve!r}], stdin=subprocess.DEVNULL,
                             stdout=open(log, "ab"), stderr=subprocess.STDOUT,
                             start_new_session=True, close_fds=True)

        cfg = load()
        led = Ledger(config.DB_PATH)
        name = "work-claude-api"
        gw = api_credits.ensure_gateway_process(Ctx(cfg, led), name, cfg["platforms"][name],
                                                spawn=spawn)
        print(json.dumps({{"port": gw.port, "token": gw.register(41)}}))
        led.close()
    ''')

    SERVE = textwrap.dedent('''
        import sys
        sys.path.insert(0, {repo!r})
        sys.path.insert(0, {tests!r})
        from unittest import mock
        from mahler import api_credits
        from test_api_credits import load
        cfg = load()
        with mock.patch.object(api_credits, "api_key_for_run", return_value="not-a-real-key"):
            api_credits.run_gateway_process(cfg, "work-claude-api", idle_seconds=3, poll=0.2)
    ''')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = dict(os.environ, MAHLER_HOME=self.tmp.name)
        self.serve = os.path.join(self.tmp.name, "serve.py")
        self.driver = os.path.join(self.tmp.name, "driver.py")
        with open(self.serve, "w") as fh:
            fh.write(self.SERVE.format(repo=REPO, tests=TESTS))
        with open(self.driver, "w") as fh:
            fh.write(self.DRIVER.format(repo=REPO, tests=TESTS, serve=self.serve))
        self.group = load()["platforms"]["work-claude-api"]["credit_pool"]["group"]
        with mock.patch.object(config, "STATE", self.tmp.name):
            self.state_path, _ = api_credits._gateway_files(self.group)
        self.addCleanup(self.stop_gateway)

    def launch(self):
        out = subprocess.run([sys.executable, self.driver], env=self.env, capture_output=True,
                             text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout.strip().splitlines()[-1])   # the launcher has now exited

    def stop_gateway(self):
        try:
            with open(self.state_path) as fh:
                os.kill(json.load(fh)["pid"], 15)
        except (OSError, ValueError, KeyError):
            pass

    def wait_until(self, check, timeout=20):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if check():
                return True
            time.sleep(0.1)
        return False

    def post(self, port, token):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            body = json.dumps({"model": "not-an-approved-model", "max_tokens": 10,
                               "messages": [{"role": "user", "content": "hi"}]})
            conn.request("POST", "/v1/messages", body=body,
                         headers={"x-api-key": token, "content-type": "application/json"})
            resp = conn.getresponse()
            resp.read()
            return resp.status
        finally:
            conn.close()

    def test_the_gateway_keeps_serving_after_the_launching_process_exits(self):
        info = self.launch()
        self.assertTrue(api_credits.gateway_healthy("127.0.0.1", info["port"], self.group))
        # The registered token is honoured (400: well-formed but model not approved), a
        # forged one is not (401). Neither reaches Anthropic.
        self.assertEqual(self.post(info["port"], info["token"]), 400)
        self.assertEqual(self.post(info["port"], "forged"), 401)

    def test_an_idle_gateway_with_no_active_run_exits_and_removes_its_state(self):
        self.launch()
        self.assertTrue(os.path.exists(self.state_path))
        self.assertTrue(self.wait_until(lambda: not os.path.exists(self.state_path)),
                        "the idle gateway should have exited")


class GatewayFailureEndingTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load()
        self.led = Ledger(":memory:")
        self.addCleanup(self.led.close)
        self.pconf = self.cfg["platforms"]["work-claude-api"]
        self.ctx = Ctx(self.cfg, self.led)

    def run_row(self):
        rid = self.led.create_run(project="work-only", number=1, role="build",
                                  platform="work-claude-api", status="ended", epoch=0)
        return {"id": rid, "platform": "work-claude-api", "log_path": None}

    def test_a_refused_connection_is_flagged_and_pinged_once(self):
        log = {"last_text": "API Error: Connection refused — a firewall (ECONNREFUSED)"}
        api_credits.after_run(self.ctx, self.run_row(), self.pconf, log)
        self.assertTrue(log.get("gateway_failed"))
        self.assertEqual(len(self.ctx.pings), 1)
        self.assertIn("gateway unreachable", self.ctx.pings[0][0])

    def test_an_ordinary_ending_is_not_flagged(self):
        log = {"last_text": "STATUS: DONE added the docs"}
        api_credits.after_run(self.ctx, self.run_row(), self.pconf, log)
        self.assertFalse(log.get("gateway_failed"))
        self.assertEqual(self.ctx.pings, [])

    def test_the_verify_green_fallback_is_skipped_for_a_gateway_failure(self):
        with open(finalize.__file__) as fh:
            source = fh.read()
        self.assertIn('handler is _ended_unconfirmed and not log.get("gateway_failed")', source)


if __name__ == "__main__":
    unittest.main()
