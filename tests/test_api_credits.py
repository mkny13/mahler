"""Team-plan Claude API credits (DESIGN D41, mahler#903).

No real keychain, network or ~/.mahler: subprocess and urllib are faked, the
ledger is in memory and config comes from a temporary TOML file.
"""
import http.client
import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

from mahler import api_credits, config, finalize, platforms, redact, router, runner
from mahler.ledger import Ledger, iso

UTC = timezone.utc


def _no_real_calls(*a, **k):
    raise AssertionError("tests must never read the real keychain or network")


_GUARDS = []


def setUpModule():
    # Belt and braces: anything not explicitly faked fails instead of reaching
    # the real login keychain or api.anthropic.com.
    for target in (mock.patch.object(api_credits.subprocess, "run", _no_real_calls),
                   mock.patch.object(api_credits.urllib.request, "urlopen", _no_real_calls)):
        target.start()
        _GUARDS.append(target)


def tearDownModule():
    while _GUARDS:
        _GUARDS.pop().stop()

BASE_TOML = """
[accounts.work]
env = {}
routing = { build = ["work-claude-api", "work-cline"] }

[platforms.work-claude-api]
from = "claude-api"
account = "work"
enabled = true

[platforms.work-cline]
from = "cline-free"
account = "work"

[api_credits]
enabled = true
pool_usd = 260
allowance_usd = 20
cycle_anchor_day = 15
burst_lead_hours = 48
safety_margin_usd = 10
run_reserve_usd = 5
workspace_id = "wrkspc_mahler"
api_key_expires = "2027-10-16"
admin_key_expires = "2027-10-16"

[[api_credits.grants]]
granted_at = "2026-09-15"
expires_at = "2026-10-15T00:00:00Z"
pool_usd = 260

[[api_credits.grants]]
granted_at = "2026-10-15"
expires_at = "2026-11-15T00:00:00Z"
pool_usd = 260

[[api_credits.grants]]
granted_at = "2026-11-15"
expires_at = "2026-12-15T00:00:00Z"
pool_usd = 260

[projects.personal-only]
enabled = false

[projects.work-only]
enabled = false
account = "work"
"""


def load(extra="", base=BASE_TOML):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "config.toml")
        with open(path, "w") as fh:
            fh.write(base + extra)
        return config.load(path)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class Ctx:
    def __init__(self, cfg, led):
        self.cfg, self.led, self.dry_run = cfg, led, False
        self.lines, self.pings = [], []
        self.burst_lines = None

    def say(self, msg):
        self.lines.append(msg)

    def ping(self, title, message="", project=None, number=None, priority="default",
             tags="", console=False):
        self.pings.append((title, message, priority))

    def policy(self, project):
        return config.project_policy(self.cfg, project)


def security(stdout="", stderr="", code=0):
    """A fake `security` subprocess.run that records its argv."""
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        assert kw.get("stdin") == subprocess.DEVNULL     # never waits on a prompt
        return subprocess.CompletedProcess(argv, code, stdout, stderr)
    run.calls = calls
    return run


def keys(api="sk-ant-api03-WORKSPACEKEYxxxxxxxxxxxx", admin="sk-ant-admin01-ADMINKEYxxxxxxxxxxxx"):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        service = argv[argv.index("-s") + 1]
        secret = admin if "admin" in service else api
        return subprocess.CompletedProcess(argv, 0, secret + "\n", "")
    run.calls = calls
    return run


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def opener_for(pages, record=None):
    """Serve cost_report pages in order (and 200 for /v1/models)."""
    pages = list(pages)

    def opener(req, timeout=None):
        if record is not None:
            record.append(req)
        if req.full_url.startswith(api_credits.MODELS_URL):
            return FakeResponse(b'{"data": []}')
        page = pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return FakeResponse(json.dumps(page).encode())
    return opener


def page(amounts, has_more=False, next_page=None, ws="wrkspc_mahler"):
    return {"data": [{"starting_at": "2026-10-15T00:00:00Z", "ending_at": "2026-10-16T00:00:00Z",
                      "results": [{"currency": "USD", "amount": a, "workspace_id": w}
                                  for a, w in amounts]}],
            "has_more": has_more, "next_page": next_page}


class Base(unittest.TestCase):
    NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)   # cycle 10-15 → 11-15

    def setUp(self):
        self.cfg = load()
        self.clock = Clock(self.NOW)
        self.led = Ledger(":memory:", clock=self.clock)
        self.addCleanup(self.led.close)
        self.addCleanup(api_credits.shutdown_gateways)   # never leak a gateway thread/port
        self.ctx = Ctx(self.cfg, self.led)
        self.name = "work-claude-api"
        self.pconf = self.cfg["platforms"][self.name]
        self.group = self.pconf["credit_pool"]["group"]

    def key_ok(self):
        self.led.set_kv(f"api_credits:key:{self.group}",
                        json.dumps({"status": "ok", "at": iso(self.led.now())}))

    def org(self, org_usd, ws_usd=0.0, age=timedelta(minutes=1), start=None):
        start = start or api_credits.cycle_bounds(self.led.now(), 15)[0]
        self.led.set_kv(f"api_credits:cost:{self.group}", json.dumps({
            "fetched_at": iso(self.led.now() - age), "cycle_start": iso(start),
            "org_usd": org_usd, "workspace_usd": ws_usd}))

    def spent(self, cost, started=None, status="ended", outcome="DONE"):
        rid = self.led.create_run(project="work-only", number=1, role="build",
                                  platform=self.name, status=status, epoch=0,
                                  started_at=iso(started or self.led.now() - timedelta(hours=1)))
        self.led.update_run(rid, cost_usd=cost, outcome=outcome)
        return rid

    def state(self):
        return router.usage_state(self.led, self.name, self.pconf)


class GrantModelTests(unittest.TestCase):
    """Confirmed grants (D41 step 3): spending is authorized only inside a
    grant Mike has actually seen in Console — never inferred from
    cycle_anchor_day/time, which are forecast-only."""

    def test_current_grant_picks_the_window_containing_now(self):
        pol = {"pool_usd": 100.0, "grants": [
            {"granted_at": "2026-10-09", "expires_at": "2026-10-15T00:00:00Z"},
            {"granted_at": "2026-10-15", "expires_at": "2026-11-15T00:00:00Z", "pool_usd": 300.0}]}
        g = api_credits.current_grant(pol, datetime(2026, 10, 20, tzinfo=UTC))
        self.assertEqual(g["id"], "2026-10-15")
        self.assertEqual(g["pool_usd"], 300.0)
        self.assertEqual(g["start"], datetime(2026, 10, 15, tzinfo=UTC))

    def test_no_confirmed_grant_covers_an_unlisted_date(self):
        pol = {"pool_usd": 100.0, "grants": [
            {"granted_at": "2026-10-09", "expires_at": "2026-10-15T00:00:00Z"}]}
        self.assertIsNone(api_credits.current_grant(pol, datetime(2026, 12, 1, tzinfo=UTC)))
        self.assertIsNone(api_credits.current_grant({"pool_usd": 1.0, "grants": []},
                                                     datetime(2026, 10, 10, tzinfo=UTC)))

    def test_grant_falls_back_to_the_pool_default_price(self):
        pol = {"pool_usd": 260.0, "grants": [
            {"granted_at": "2026-10-09", "expires_at": "2026-10-15T00:00:00Z"}]}
        g = api_credits.current_grant(pol, datetime(2026, 10, 10, tzinfo=UTC))
        self.assertEqual(g["pool_usd"], 260.0)

    def test_date_only_boundaries_floor_to_utc_midnight(self):
        self.assertEqual(api_credits.parse_grant_dt("2026-10-09"),
                         datetime(2026, 10, 9, tzinfo=UTC))
        self.assertEqual(api_credits.parse_grant_dt("2026-10-09T20:00:00Z"),
                         datetime(2026, 10, 9, 20, tzinfo=UTC))

    def test_validate_grants_requires_a_list_of_tables(self):
        with self.assertRaisesRegex(ValueError, "must be a list"):
            api_credits.validate_grants({"granted_at": "x"})
        with self.assertRaisesRegex(ValueError, "must be tables"):
            api_credits.validate_grants(["not-a-table"])

    def test_validate_grants_requires_both_dates(self):
        with self.assertRaisesRegex(ValueError, "need granted_at and expires_at"):
            api_credits.validate_grants([{"granted_at": "2026-10-09"}])

    def test_validate_grants_rejects_bad_ordering(self):
        with self.assertRaisesRegex(ValueError, "expires_at must be after granted_at"):
            api_credits.validate_grants([{"granted_at": "2026-10-15", "expires_at": "2026-10-09"}])

    def test_validate_grants_rejects_overlap(self):
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            api_credits.validate_grants([
                {"granted_at": "2026-10-09", "expires_at": "2026-10-20T00:00:00Z"},
                {"granted_at": "2026-10-15", "expires_at": "2026-11-15T00:00:00Z"}])

    def test_validate_grants_accepts_back_to_back_non_overlapping_windows(self):
        self.assertIsNone(api_credits.validate_grants([
            {"granted_at": "2026-10-09", "expires_at": "2026-10-15T00:00:00Z"},
            {"granted_at": "2026-10-15", "expires_at": "2026-11-15T00:00:00Z"}]))

    def test_validate_grants_rejects_unknown_fields_and_bad_pool(self):
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            api_credits.validate_grants([{"granted_at": "2026-10-09", "expires_at": "2026-10-15",
                                          "extra": 1}])
        with self.assertRaisesRegex(ValueError, "positive number"):
            api_credits.validate_grants([{"granted_at": "2026-10-09", "expires_at": "2026-10-15",
                                          "pool_usd": -5}])


class PricingTests(unittest.TestCase):
    MESSAGE = {"model": "claude-sonnet-5-5", "max_tokens": 100,
              "messages": [{"role": "user", "content": "hi there"}]}

    def test_estimate_scales_with_model_and_max_tokens(self):
        cheap = api_credits.estimate_request_cost(self.MESSAGE, api_credits.MODEL_PRICING, 8192)
        pricier = api_credits.estimate_request_cost(
            {**self.MESSAGE, "model": "claude-opus-5"}, api_credits.MODEL_PRICING, 8192)
        bigger = api_credits.estimate_request_cost(
            {**self.MESSAGE, "max_tokens": 10000}, api_credits.MODEL_PRICING, 20000)
        self.assertGreater(pricier, cheap)
        self.assertGreater(bigger, cheap)

    def test_estimate_rejects_missing_model(self):
        bad = {k: v for k, v in self.MESSAGE.items() if k != "model"}
        with self.assertRaises(ValueError):
            api_credits.estimate_request_cost(bad, api_credits.MODEL_PRICING, 8192)

    def test_estimate_rejects_missing_or_non_positive_max_tokens(self):
        for bad in ({k: v for k, v in self.MESSAGE.items() if k != "max_tokens"},
                   {**self.MESSAGE, "max_tokens": 0}, {**self.MESSAGE, "max_tokens": -1},
                   {**self.MESSAGE, "max_tokens": True}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                api_credits.estimate_request_cost(bad, api_credits.MODEL_PRICING, 8192)

    def test_estimate_rejects_max_tokens_over_the_ceiling(self):
        with self.assertRaisesRegex(ValueError, "exceeds the approved ceiling"):
            api_credits.estimate_request_cost({**self.MESSAGE, "max_tokens": 9000},
                                              api_credits.MODEL_PRICING, 8192)

    def test_price_usage_matches_hand_computed_cost(self):
        price = api_credits.MODEL_PRICING["claude-sonnet-5-5"]
        usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000,
                "cache_creation_input_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000}
        cost = api_credits.price_usage(usage, api_credits.MODEL_PRICING, "claude-sonnet-5-5")
        self.assertEqual(cost, round(price["input"] + price["output"] + price["cache_write"]
                                     + price["cache_read"], 6))

    def test_price_usage_rejects_malformed_usage(self):
        with self.assertRaises(ValueError):
            api_credits.price_usage("nope", api_credits.MODEL_PRICING, "claude-sonnet-5-5")

    def test_model_pricing_override_merges_with_builtin(self):
        merged = api_credits.model_pricing({"model_pricing": {"claude-sonnet-5-5": {"output": 1.0}}})
        self.assertEqual(merged["claude-sonnet-5-5"]["output"], 1.0)
        self.assertEqual(merged["claude-sonnet-5-5"]["input"],
                         api_credits.MODEL_PRICING["claude-sonnet-5-5"]["input"])

    def test_stream_usage_takes_the_max_per_field_across_events(self):
        raw = (b'data: {"usage": {"output_tokens": 5}}\n'
              b'data: {"usage": {"output_tokens": 40, "input_tokens": 100}}\n'
              b'data: [DONE]\n')
        self.assertEqual(api_credits.parse_stream_usage(raw),
                         {"output_tokens": 40, "input_tokens": 100})

    def test_stream_usage_reads_usage_nested_under_message(self):
        raw = b'data: {"type":"message_start","message":{"usage":{"input_tokens":500}}}\n'
        self.assertEqual(api_credits.parse_stream_usage(raw), {"input_tokens": 500})

    def test_stream_usage_with_no_usage_event_is_none(self):
        self.assertIsNone(api_credits.parse_stream_usage(b'data: {"type":"ping"}\n'))
        self.assertIsNone(api_credits.parse_stream_usage(b'not even sse\n'))


class FakeUpstream:
    """A stand-in for http.client.HTTPSConnection, scripted per test."""

    def __init__(self, status, body, headers=None, raises=None):
        self.status, self.body, self.headers, self.raises = status, body, headers or {}, raises
        self.sent = None

    def request(self, method, path, body=None, headers=None):
        if self.raises:
            raise self.raises
        self.sent = {"method": method, "path": path, "body": body, "headers": headers}

    def getresponse(self):
        return self

    def read(self):
        return self.body

    def getheaders(self):
        return list(self.headers.items())

    def close(self):
        pass


MESSAGE = {"model": "claude-sonnet-5-5", "max_tokens": 100,
          "messages": [{"role": "user", "content": "hi there"}]}


def make_gateway(led, grant=None, upstream=None, allowed_models=("claude-sonnet-5-5",),
                 max_output_tokens=8192, pricing=None, real_key="sk-ant-api03-REALKEYxxxxxxxx",
                 group="g"):
    conn = upstream if upstream is not None else FakeUpstream(
        200, json.dumps({"usage": {"input_tokens": 100, "output_tokens": 50}}).encode())
    gw = api_credits.Gateway(led, group, lambda: grant, pricing or api_credits.MODEL_PRICING,
                             allowed_models, max_output_tokens, real_key,
                             connect=lambda: conn, ping=lambda *a, **k: gw.pings.append((a, k)))
    gw.pings = []
    gw.upstream = conn
    return gw


class GatewayTests(unittest.TestCase):
    """The harness's enforceable hook (D41 step 2): the only thing that ever
    holds the real workspace key, reserving before forwarding."""

    def setUp(self):
        self.led = Ledger(":memory:", clock=Clock(datetime(2026, 10, 20, tzinfo=UTC)))
        self.addCleanup(self.led.close)
        self.grant = {"id": "grant-a", "ceiling_usd": 20.0,
                     "end": datetime(2026, 11, 15, tzinfo=UTC)}

    def test_denies_unsupported_methods_and_paths(self):
        gw = make_gateway(self.led, self.grant)
        self.assertEqual(gw.handle("GET", "/v1/messages", {}, b"")[0], 404)
        self.assertEqual(gw.handle("POST", "/v1/models", {}, b"{}")[0], 404)
        self.assertEqual(gw.handle("POST", "/", {}, b"")[0], 404)

    def test_denies_an_unrecognized_local_token(self):
        gw = make_gateway(self.led, self.grant)
        status, _, _ = gw.handle("POST", "/v1/messages", {"x-api-key": "wrong"},
                                 json.dumps(MESSAGE).encode())
        self.assertEqual(status, 401)

    def test_revoked_token_is_refused(self):
        gw = make_gateway(self.led, self.grant)
        token = gw.register(1)
        gw.revoke(1)
        status, _, _ = gw.handle("POST", "/v1/messages", {"x-api-key": token},
                                 json.dumps(MESSAGE).encode())
        self.assertEqual(status, 401)

    def test_denies_a_model_outside_the_allow_list(self):
        gw = make_gateway(self.led, self.grant)
        token = gw.register(1)
        status, _, body = gw.handle("POST", "/v1/messages", {"x-api-key": token},
                                    json.dumps({**MESSAGE, "model": "claude-opus-5"}).encode())
        self.assertEqual(status, 400)
        self.assertIn(b"not an approved", body)
        self.assertEqual(self.led.q("SELECT * FROM credit_reservations"), [])

    def test_denies_max_tokens_over_the_ceiling(self):
        gw = make_gateway(self.led, self.grant, max_output_tokens=50)
        token = gw.register(1)
        status, _, _ = gw.handle("POST", "/v1/messages", {"x-api-key": token},
                                 json.dumps(MESSAGE).encode())
        self.assertEqual(status, 400)

    def test_denies_malformed_json(self):
        gw = make_gateway(self.led, self.grant)
        token = gw.register(1)
        status, _, _ = gw.handle("POST", "/v1/messages", {"x-api-key": token}, b"{not json")
        self.assertEqual(status, 400)

    def test_reserves_forwards_with_the_real_key_and_settles(self):
        upstream = FakeUpstream(200, json.dumps(
            {"usage": {"input_tokens": 1000, "output_tokens": 50}}).encode())
        gw = make_gateway(self.led, self.grant, upstream=upstream,
                          real_key="sk-ant-api03-REALWORKSPACExxxx")
        token = gw.register(42)
        status, _, _ = gw.handle("POST", "/v1/messages", {"x-api-key": token},
                                 json.dumps(MESSAGE).encode())
        self.assertEqual(status, 200)
        self.assertEqual(upstream.sent["headers"]["x-api-key"], "sk-ant-api03-REALWORKSPACExxxx")
        self.assertNotEqual(token, "sk-ant-api03-REALWORKSPACExxxx")
        rows = self.led.q("SELECT * FROM credit_reservations")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "settled")
        self.assertEqual(rows[0]["run_id"], 42)
        expected = api_credits.price_usage({"input_tokens": 1000, "output_tokens": 50},
                                           api_credits.MODEL_PRICING, "claude-sonnet-5-5")
        self.assertEqual(rows[0]["settled_usd"], expected)

    def test_refuses_when_headroom_is_exhausted(self):
        self.led.reserve_credit("g", "grant-a", "already-spent", 20.0, 20.0)
        gw = make_gateway(self.led, self.grant)
        token = gw.register(1)
        status, _, body = gw.handle("POST", "/v1/messages", {"x-api-key": token},
                                    json.dumps(MESSAGE).encode())
        self.assertEqual(status, 402)
        self.assertIn(b"credit balance is too low", body)

    def test_refuses_with_no_confirmed_grant(self):
        gw = make_gateway(self.led, grant=None)
        token = gw.register(1)
        status, _, body = gw.handle("POST", "/v1/messages", {"x-api-key": token},
                                    json.dumps(MESSAGE).encode())
        self.assertEqual(status, 402)
        self.assertIn(b"no confirmed grant", body)

    def test_pre_dispatch_failure_releases_the_reservation(self):
        gw = make_gateway(self.led, self.grant, upstream=FakeUpstream(0, b"", raises=OSError("down")))
        token = gw.register(1)
        status, _, _ = gw.handle("POST", "/v1/messages", {"x-api-key": token},
                                 json.dumps(MESSAGE).encode())
        self.assertEqual(status, 502)
        self.assertEqual(self.led.credit_exposure("g", "grant-a"), 0.0)

    def test_credit_exhausted_response_releases_and_parks_the_pool(self):
        body = json.dumps({"type": "error", "error": {
            "message": "Your credit balance is too low to access the Anthropic API."}}).encode()
        gw = make_gateway(self.led, self.grant, upstream=FakeUpstream(400, body))
        token = gw.register(1)
        status, _, _ = gw.handle("POST", "/v1/messages", {"x-api-key": token},
                                 json.dumps(MESSAGE).encode())
        self.assertEqual(status, 400)
        self.assertEqual(self.led.credit_exposure("g", "grant-a"), 0.0)
        self.assertEqual(json.loads(self.led.get_kv("api_credits:exhausted:g"))["grant_id"], "grant-a")
        self.assertEqual(len(gw.pings), 1)
        gw.handle("POST", "/v1/messages", {"x-api-key": token}, json.dumps(MESSAGE).encode())
        self.assertEqual(len(gw.pings), 1)     # pings once, not on every subsequent hit

    def test_auth_error_response_releases_and_parks_the_pool(self):
        body = json.dumps({"type": "error", "error": {"type": "authentication_error",
                           "message": "invalid x-api-key"}}).encode()
        gw = make_gateway(self.led, self.grant, upstream=FakeUpstream(401, body))
        token = gw.register(1)
        gw.handle("POST", "/v1/messages", {"x-api-key": token}, json.dumps(MESSAGE).encode())
        self.assertEqual(self.led.credit_exposure("g", "grant-a"), 0.0)
        self.assertTrue(json.loads(self.led.get_kv("api_credits:auth:g")).get("since"))

    def test_generic_upstream_error_releases_without_parking(self):
        gw = make_gateway(self.led, self.grant, upstream=FakeUpstream(529, b'{"type":"error"}'))
        token = gw.register(1)
        gw.handle("POST", "/v1/messages", {"x-api-key": token}, json.dumps(MESSAGE).encode())
        self.assertEqual(self.led.credit_exposure("g", "grant-a"), 0.0)
        self.assertIsNone(self.led.get_kv("api_credits:exhausted:g"))
        self.assertIsNone(self.led.get_kv("api_credits:auth:g"))

    def test_ambiguous_2xx_with_no_usage_retains_exposure(self):
        gw = make_gateway(self.led, self.grant, upstream=FakeUpstream(200, b'{}'))
        token = gw.register(1)
        gw.handle("POST", "/v1/messages", {"x-api-key": token}, json.dumps(MESSAGE).encode())
        self.assertGreater(self.led.credit_exposure("g", "grant-a"), 0.0)
        row = self.led.q1("SELECT * FROM credit_reservations")
        self.assertEqual(row["status"], "reserved")

    def test_streaming_usage_is_accumulated_from_sse_events(self):
        sse = (b'data: {"type":"message_start","message":{"usage":{"input_tokens":500,'
              b'"cache_read_input_tokens":200}}}\n\n'
              b'data: {"type":"message_delta","usage":{"output_tokens":10}}\n\n'
              b'data: {"type":"message_delta","usage":{"output_tokens":42}}\n\n'
              b'data: {"type":"message_stop"}\n\n')
        gw = make_gateway(self.led, self.grant, upstream=FakeUpstream(200, sse))
        token = gw.register(1)
        gw.handle("POST", "/v1/messages", {"x-api-key": token},
                 json.dumps({**MESSAGE, "stream": True}).encode())
        row = self.led.q1("SELECT * FROM credit_reservations")
        self.assertEqual(row["status"], "settled")
        expected = api_credits.price_usage(
            {"input_tokens": 500, "cache_read_input_tokens": 200, "output_tokens": 42},
            api_credits.MODEL_PRICING, "claude-sonnet-5-5")
        self.assertEqual(row["settled_usd"], expected)

    def test_retry_reserves_separately_not_sharing_one_budget(self):
        gw = make_gateway(self.led, self.grant)
        token = gw.register(1)
        gw.handle("POST", "/v1/messages", {"x-api-key": token}, json.dumps(MESSAGE).encode())
        gw.handle("POST", "/v1/messages", {"x-api-key": token}, json.dumps(MESSAGE).encode())
        rows = self.led.q("SELECT * FROM credit_reservations")
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["attempt_key"], rows[1]["attempt_key"])

    def test_real_key_only_ever_goes_in_the_upstream_request_header(self):
        secret = "sk-ant-api03-SUPERSECRETxxxxxxxxxxxxxxx"
        for upstream in (FakeUpstream(200, json.dumps({"usage": {"output_tokens": 1}}).encode()),
                        FakeUpstream(400, b'{"type":"error","error":{"message":"bad request"}}'),
                        FakeUpstream(529, b'{"type":"error"}')):
            with self.subTest(status=upstream.status):
                led = Ledger(":memory:", clock=self.led.now)
                gw = make_gateway(led, self.grant, real_key=secret, upstream=upstream)
                token = gw.register(1)
                status, headers, body = gw.handle("POST", "/v1/messages", {"x-api-key": token},
                                                  json.dumps(MESSAGE).encode())
                self.assertEqual(upstream.sent["headers"]["x-api-key"], secret)
                self.assertNotIn(secret.encode(), body)
                self.assertNotIn(secret, json.dumps(headers))
                led.close()

    def test_start_stop_serves_real_http_and_enforces_the_token(self):
        """One true socket-level test: the handler wiring (not just handle())
        actually denies an unrecognized token over real HTTP."""
        led = Ledger(":memory:", thread_safe=True, clock=self.led.now)
        self.addCleanup(led.close)
        gw = api_credits.Gateway(led, "g2", lambda: None, api_credits.MODEL_PRICING,
                                 ["claude-sonnet-5-5"], 8192, "sk-ant-api03-x")
        gw.start()
        self.addCleanup(gw.stop)
        # http.client, not urllib.request.urlopen: the module guard above
        # patches urlopen itself (api_credits.urllib.request IS urllib.request),
        # and this test wants a real loopback socket, not a faked one.
        conn = http.client.HTTPConnection("127.0.0.1", gw.port, timeout=5)
        try:
            conn.request("POST", "/v1/messages", body=json.dumps(MESSAGE).encode(),
                        headers={"x-api-key": "wrong"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 401)
            resp.read()
        finally:
            conn.close()


class CycleTests(unittest.TestCase):
    def test_mid_cycle(self):
        start, end = api_credits.cycle_bounds(datetime(2026, 10, 20, tzinfo=UTC), 15)
        self.assertEqual((start, end), (datetime(2026, 10, 15, tzinfo=UTC),
                                        datetime(2026, 11, 15, tzinfo=UTC)))

    def test_before_anchor_rolls_back_a_month_and_year(self):
        start, end = api_credits.cycle_bounds(datetime(2026, 1, 3, tzinfo=UTC), 15)
        self.assertEqual(start, datetime(2025, 12, 15, tzinfo=UTC))
        self.assertEqual(end, datetime(2026, 1, 15, tzinfo=UTC))

    def test_exact_boundary_starts_the_new_cycle(self):
        at = datetime(2026, 11, 15, tzinfo=UTC)
        self.assertEqual(api_credits.cycle_bounds(at, 15)[0], at)
        self.assertEqual(api_credits.cycle_bounds(at - timedelta(seconds=1), 15)[1], at)

    def test_short_months_clamp(self):
        # day 31: Jan 31 → Feb 28 (2027) → Mar 31; leap 2028 → Feb 29; Apr 30
        self.assertEqual(api_credits.cycle_bounds(datetime(2027, 2, 10, tzinfo=UTC), 31),
                         (datetime(2027, 1, 31, tzinfo=UTC), datetime(2027, 2, 28, tzinfo=UTC)))
        self.assertEqual(api_credits.cycle_bounds(datetime(2027, 3, 1, tzinfo=UTC), 31),
                         (datetime(2027, 2, 28, tzinfo=UTC), datetime(2027, 3, 31, tzinfo=UTC)))
        self.assertEqual(api_credits.cycle_bounds(datetime(2028, 2, 29, 12, tzinfo=UTC), 30)[0],
                         datetime(2028, 2, 29, tzinfo=UTC))
        self.assertEqual(api_credits.cycle_bounds(datetime(2027, 4, 30, 1, tzinfo=UTC), 31),
                         (datetime(2027, 4, 30, tzinfo=UTC), datetime(2027, 5, 31, tzinfo=UTC)))

    def test_anchor_time(self):
        start, end = api_credits.cycle_bounds(datetime(2026, 10, 15, 3, tzinfo=UTC), 15, "04:30")
        self.assertEqual(end, datetime(2026, 10, 15, 4, 30, tzinfo=UTC))


class ConfigTests(unittest.TestCase):
    def test_defaults_are_disabled_and_unrouted(self):
        cfg = config.load("/nonexistent/config.toml")
        self.assertFalse(cfg["platforms"]["claude-api"]["enabled"])
        self.assertFalse(cfg["api_credits"]["enabled"] if "api_credits" in cfg
                         else api_credits.policy(cfg)["enabled"])
        self.assertNotIn("claude-api", json.dumps(config.DEFAULTS["routing"]))
        self.assertEqual(cfg["platforms"]["claude-api"]["kind"], "cline")
        self.assertEqual(cfg["platforms"]["claude-api"]["provider"], "anthropic")

    def test_work_platform_gets_its_own_quota_group_and_pool(self):
        cfg = load()
        pc = cfg["platforms"]["work-claude-api"]
        self.assertEqual(pc["quota_group"], "claude-api@work")
        self.assertEqual(pc["credit_pool"]["peers"], ["work-claude-api"])

    def test_enabling_on_personal_is_refused(self):
        with self.assertRaisesRegex(ValueError, "must spend account 'work'"):
            load('\n[platforms.claude-api]\nenabled = true\n')

    def test_personal_pool_account_refused(self):
        with self.assertRaisesRegex(ValueError, "non-personal"):
            load(base=BASE_TOML.replace('[api_credits]\n', '[api_credits]\naccount = "personal"\n'))

    def test_bad_money_and_fields(self):
        for bad, msg in (('allowance_usd = 300', "exceed pool"),
                         ('pool_usd = -1', "non-negative"),
                         ('cycle_anchor_day = 32', "1 to 31"),
                         ('cycle_anchor_time = "25:00"', "HH:MM"),
                         ('api_key_expires = "soon"', "YYYY-MM-DD"),
                         ('api_key = "sk-ant-x"', "unknown fields"),
                         ('allowed_models = []', "non-empty list"),
                         ('allowed_models = [1]', "non-empty list"),
                         ('max_output_tokens = 0', "positive integer"),
                         ('max_output_tokens = 1.5', "positive integer"),
                         ('gateway_host = "0.0.0.0"', "must be loopback"),
                         ('base_url_env = ""', "non-empty string")):
            key = bad.split(" =")[0]
            text = "\n".join(line for line in BASE_TOML.splitlines()
                             if not line.startswith(key + " "))
            text = text.replace("[api_credits]", "[api_credits]\n" + bad)
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, msg):
                load(base=text)

    def test_bad_model_pricing(self):
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            load(extra='\n[api_credits.model_pricing.claude-sonnet-5-5]\nbogus = 1\n')
        with self.assertRaisesRegex(ValueError, "non-negative number"):
            load(extra='\n[api_credits.model_pricing.claude-sonnet-5-5]\ninput = -1\n')

    def test_grants_validation(self):
        with self.assertRaisesRegex(ValueError, "expires_at must be after granted_at"):
            load(extra='\n[[api_credits.grants]]\ngranted_at = "2026-12-20"\n'
                 'expires_at = "2026-12-01"\n')
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            load(extra='\n[[api_credits.grants]]\ngranted_at = "2026-10-20"\n'
                 'expires_at = "2026-11-20T00:00:00Z"\n')
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            load(extra='\n[[api_credits.grants]]\ngranted_at = "2026-12-20"\n'
                 'expires_at = "2026-12-25"\nbogus = 1\n')
        with self.assertRaisesRegex(ValueError, "positive number"):
            load(extra='\n[[api_credits.grants]]\ngranted_at = "2026-12-20"\n'
                 'expires_at = "2026-12-25"\npool_usd = 0\n')

    def test_credits_need_the_cline_anthropic_harness(self):
        with self.assertRaisesRegex(ValueError, "Claude Code is not covered"):
            load('\n[platforms.work-claude-api2]\nfrom = "claude"\naccount = "work"\n'
                 'api_credits = true\n')

    def test_example_config_still_loads(self):
        cfg = config.load(os.path.join(config.REPO_ROOT, "config.example.toml"))
        self.assertFalse(api_credits.policy(cfg)["enabled"])


class RoutingBoundaryTests(Base):
    def test_never_routes_to_a_personal_project(self):
        self.assertNotIn(self.name, router.candidates(self.cfg, "build"))
        self.assertEqual(router.candidates(self.cfg, "build", pin=self.name), [])
        personal = config.project_policy(self.cfg, "personal-only")
        name, reasons = router.pick_for_project(self.cfg, self.led, personal, "build", pin=self.name)
        self.assertIsNone(name)
        self.assertTrue(any("spends the work account" in r for r in reasons))

    def test_work_project_can_use_it(self):
        self.key_ok()
        work = config.project_policy(self.cfg, "work-only")
        name, _ = router.pick_for_project(self.cfg, self.led, work, "build", size="s")
        self.assertEqual(name, self.name)


class LineTests(Base):
    def test_unchecked_key_is_unknown(self):
        self.assertEqual(self.state()[0], "stale")

    def test_disabled_pool_is_hard(self):
        self.pconf["credit_pool"]["enabled"] = False
        self.key_ok()
        self.assertEqual(self.state()[0], "hard")

    def test_allowance_line(self):
        self.key_ok()
        self.assertEqual(self.state()[0], "ok")
        self.spent(14.0)
        self.assertEqual(self.state()[0], "ok")       # $6 left ≥ $5 reserve
        self.spent(2.0)
        state, detail = self.state()
        self.assertEqual(state, "soft")               # $4 left: no new run
        self.assertIn("$16.00 of $20.00 allowance", detail)
        self.spent(4.0)
        self.assertEqual(self.state()[0], "hard")     # line reached: running work yields

    def test_live_runs_hold_their_reserve(self):
        self.key_ok()
        self.spent(8.0)
        self.spent(None, status="running", outcome=None)    # $12 left − $5 live
        self.assertEqual(self.state()[0], "ok")
        self.spent(None, status="running", outcome=None)    # − $10 live
        self.assertEqual(self.state()[0], "soft")

    def test_unpriced_runs_count_as_the_reserve(self):
        self.key_ok()
        for _ in range(3):
            self.spent(None)
        snap = api_credits.snapshot(self.led, self.name, self.pconf)
        self.assertEqual(snap["unpriced_runs"], 3)
        self.assertEqual(snap["mahler_spend_usd"], 15.0)
        self.spent(None, outcome="launch failed: no key")   # never ran: not counted
        self.assertEqual(api_credits.snapshot(self.led, self.name, self.pconf)["unpriced_runs"], 3)

    def test_previous_cycle_spend_does_not_count(self):
        self.key_ok()
        self.spent(19.0, started=datetime(2026, 10, 14, 23, tzinfo=UTC))
        self.assertEqual(api_credits.snapshot(self.led, self.name, self.pconf)["mahler_spend_usd"], 0)

    def test_workspace_cost_is_used_when_higher_than_the_ledger(self):
        self.key_ok()
        self.spent(2.0)
        self.org(50.0, ws_usd=17.5)
        snap = api_credits.snapshot(self.led, self.name, self.pconf)
        self.assertEqual(snap["mahler_spend_usd"], 17.5)
        self.assertEqual(self.state()[0], "soft")

    def test_fresh_org_reading_of_an_empty_pool_blocks_the_allowance(self):
        self.key_ok()
        self.org(255.0)            # 260 − 255 − 10 margin < 0
        self.assertEqual(self.state()[0], "hard")

    def test_burst_line_in_the_lead_window(self):
        self.clock.now = datetime(2026, 11, 14, 6, tzinfo=UTC)   # 18h before the reset
        self.key_ok()
        self.spent(20.0)                       # allowance used up
        self.org(100.0, ws_usd=20.0)
        snap = api_credits.snapshot(self.led, self.name, self.pconf)
        self.assertTrue(snap["burst"])
        self.assertEqual(snap["remaining_usd"], 150.0)
        self.assertEqual(snap["line_usd"], 250.0)
        self.assertEqual(self.state()[0], "ok")
        self.assertIn("burst: org $100.00 of $260 pool", self.state()[1])
        self.org(247.0)
        self.assertEqual(self.state()[0], "soft")     # $3 left, under the $5 reserve
        self.org(251.0)
        self.assertEqual(self.state()[0], "hard")

    def test_stale_or_unknown_org_reading_never_bursts(self):
        self.clock.now = datetime(2026, 11, 14, 6, tzinfo=UTC)
        self.key_ok()
        self.spent(20.0)
        for setup in (lambda: None,                                   # unknown
                      lambda: self.org(10.0, age=timedelta(hours=2)),  # stale
                      lambda: self.org(10.0, start=datetime(2026, 9, 15, tzinfo=UTC))):  # old cycle
            with self.subTest(setup=setup):
                setup()
                snap = api_credits.snapshot(self.led, self.name, self.pconf)
                self.assertTrue(snap["burst_window"])
                self.assertFalse(snap["burst"])
                self.assertEqual(self.state()[0], "hard")     # allowance only
                self.assertIn("no burst", self.state()[1])

    def test_burst_promotes_the_pool_in_build_routes(self):
        self.clock.now = datetime(2026, 11, 14, 6, tzinfo=UTC)
        self.key_ok()
        order = ["work-cline", self.name]
        self.assertEqual(api_credits.promote(self.cfg, self.led, order), order)   # no reading
        self.org(50.0)
        self.assertEqual(api_credits.promote(self.cfg, self.led, order), [self.name, "work-cline"])

    def test_cycle_rollover_resets_the_allowance(self):
        self.key_ok()
        self.spent(20.0)
        self.assertEqual(self.state()[0], "hard")
        self.clock.now = datetime(2026, 11, 15, 0, 1, tzinfo=UTC)
        self.key_ok()
        self.assertEqual(self.state()[0], "ok")


class EndingTests(Base):
    def run_row(self, text=""):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        log = os.path.join(tmp, "agent.log")
        with open(log, "w") as fh:
            fh.write(text)
        rid = self.led.create_run(project="work-only", number=1, role="build",
                                  platform=self.name, log_path=log, epoch=0)
        return self.led.run(rid), log

    def test_credit_balance_too_low_exhausts_until_the_cycle_resets(self):
        self.key_ok()
        msg = "Your credit balance is too low to access the Anthropic API."
        self.assertTrue(platforms.is_credit_exhausted(msg))
        run, path = self.run_row(json.dumps({"type": "error", "message": msg}) + "\n")
        log = platforms.read_log(path, "cline")
        self.assertTrue(log["credit_exhausted"])
        api_credits.after_run(self.ctx, run, self.pconf, log)
        api_credits.after_run(self.ctx, run, self.pconf, log)
        self.assertEqual(len(self.ctx.pings), 1)
        self.assertEqual(self.state()[0], "no_credit")
        self.clock.now = datetime(2026, 11, 15, 1, tzinfo=UTC)
        self.key_ok()
        self.assertEqual(self.state()[0], "ok")

    def test_auth_error_parks_and_pings_once(self):
        self.key_ok()
        err = {"type": "error", "error": {"type": "authentication_error",
                                          "message": "invalid x-api-key"}}
        run, path = self.run_row(json.dumps(err) + "\n")
        log = platforms.read_log(path, "cline")
        api_credits.after_run(self.ctx, run, self.pconf, log)
        self.assertTrue(log["auth_failed"])
        api_credits.after_run(self.ctx, run, self.pconf, dict(log))
        self.assertEqual(len(self.ctx.pings), 1)
        self.assertIn("rejected", self.ctx.pings[0][0])
        state, detail = self.state()
        self.assertEqual(state, "hard")
        self.assertIn("parked", detail)

    def test_parked_key_rechecks_for_free_and_never_loops(self):
        self.key_ok()
        self.led.set_kv(f"api_credits:auth:{self.group}",
                        json.dumps({"since": iso(self.led.now())}))
        calls = []
        api_credits.refresh(self.ctx, run=keys(), opener=opener_for([page([])], calls))
        self.assertFalse([c for c in calls if c.full_url == api_credits.MODELS_URL])
        self.clock.now += timedelta(hours=6, minutes=1)
        api_credits.refresh(self.ctx, run=keys(), opener=opener_for([page([])] * 3, calls))
        self.assertEqual(len([c for c in calls if c.full_url == api_credits.MODELS_URL]), 1)
        self.assertEqual(self.state()[0], "ok")

    def test_finalize_treats_auth_failure_as_no_attempt_and_skips_the_nudge(self):
        self.assertIn("no_credit", finalize.NO_ATTEMPT)
        self.key_ok()
        self.spent(20.0)
        run, _ = self.run_row("")
        with mock.patch.object(finalize.runner, "exit_code", return_value=0), \
                mock.patch.object(finalize.runner, "spawn") as spawn:
            self.assertFalse(finalize._try_cline_nudge(self.ctx, run, "cline", {"ok": True},
                                                       {"run_timeout_minutes": 60}))
            spawn.assert_not_called()


class KeychainTests(Base):
    def test_read_key_classifies_failures(self):
        self.assertEqual(api_credits.read_key("s", "a", run=security("sk-ant-x\n")),
                         ("sk-ant-x", api_credits.KEY_OK))
        locked = security(stderr="security: SecKeychainSearchCopyNext: User interaction is "
                                 "not allowed.", code=36)
        self.assertEqual(api_credits.read_key("s", "a", run=locked), (None, api_credits.KEY_LOCKED))
        self.assertEqual(api_credits.read_key("s", "a", run=security(stderr="(-25308)", code=1))[1],
                         api_credits.KEY_LOCKED)
        missing = security(stderr="The specified item could not be found in the keychain.",
                           code=44)
        self.assertEqual(api_credits.read_key("s", "a", run=missing)[1], api_credits.KEY_MISSING)

        def boom(*a, **k):
            raise subprocess.TimeoutExpired("security", 10)
        self.assertEqual(api_credits.read_key("s", "a", run=boom)[1], api_credits.KEY_ERROR)

    def test_locked_keychain_parks_the_platform_and_says_so_once(self):
        locked = security(stderr="User interaction is not allowed.", code=36)
        api_credits.refresh(self.ctx, run=locked, opener=opener_for([]))
        state, detail = self.state()
        self.assertEqual(state, "hard")
        self.assertIn("login keychain is locked", detail)
        titles = [p[0] + p[1] for p in self.ctx.pings]
        self.assertTrue(any("keychain is locked" in t for t in titles))
        self.assertFalse(any("rejected" in t or "missing" in t for t in titles))
        before = len(self.ctx.pings)
        self.clock.now += timedelta(minutes=20)      # one recheck, same state: no new ping
        api_credits.refresh(self.ctx, run=locked, opener=opener_for([]))
        self.assertEqual(len(self.ctx.pings), before)
        # one key check per 15 minutes plus one admin read per minute at most
        self.assertLessEqual(len(locked.calls), 4)

    def test_run_env_injects_only_a_local_token_never_the_real_key(self):
        self.ctx.cfg["projects"]["work-only"]["path"] = "/tmp/x"
        reader = keys()
        with mock.patch.object(runner, "fence_hooks", return_value="/tmp/hooks"), \
                mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-api03-DAEMONLEAKxxxxxxxx"}), \
                mock.patch.object(api_credits.subprocess, "run", reader):
            env = runner.run_env(self.ctx, "work-only", 1, self.name, 7, 1)
            other = runner.run_env(self.ctx, "work-only", 1, "work-cline", 8, 1)
        # D41 step 2: the real workspace key never reaches a run's environment
        # — only the gateway holds it. The run gets a fresh local token good
        # only for the gateway, plus its base URL.
        self.assertNotEqual(env["ANTHROPIC_API_KEY"], "sk-ant-api03-WORKSPACEKEYxxxxxxxxxxxx")
        self.assertRegex(env["ANTHROPIC_BASE_URL"], r"^http://127\.0\.0\.1:\d+$")
        self.assertNotIn("ANTHROPIC_API_KEY", other)
        self.assertNotIn("ANTHROPIC_BASE_URL", other)
        self.assertNotIn("WORKSPACEKEY", json.dumps(env))
        self.assertNotIn("ADMINKEY", json.dumps(env))
        self.assertTrue(all("mahler-anthropic-admin" not in c for c in reader.calls))

    def test_run_env_each_run_gets_a_distinct_token_and_gateway_knows_the_run_id(self):
        self.ctx.cfg["projects"]["work-only"]["path"] = "/tmp/x"
        with mock.patch.object(runner, "fence_hooks", return_value="/tmp/hooks"), \
                mock.patch.object(api_credits.subprocess, "run", keys()):
            env1 = runner.run_env(self.ctx, "work-only", 1, self.name, 7, 1)
            env2 = runner.run_env(self.ctx, "work-only", 1, self.name, 8, 1)
        self.assertNotEqual(env1["ANTHROPIC_API_KEY"], env2["ANTHROPIC_API_KEY"])
        gw = api_credits._GATEWAYS[self.pconf["credit_pool"]["group"]]
        self.assertEqual(gw._run_for_token(env1["ANTHROPIC_API_KEY"]), 7)
        self.assertEqual(gw._run_for_token(env2["ANTHROPIC_API_KEY"]), 8)
        api_credits.revoke_run_token(self.pconf, 7)
        self.assertIsNone(gw._run_for_token(env1["ANTHROPIC_API_KEY"]))
        self.assertEqual(gw._run_for_token(env2["ANTHROPIC_API_KEY"]), 8)

    def test_run_env_fails_closed_when_the_key_is_unreadable(self):
        self.ctx.cfg["projects"]["work-only"]["path"] = "/tmp/x"
        with mock.patch.object(runner, "fence_hooks", return_value="/tmp/hooks"), \
                mock.patch.object(api_credits.subprocess, "run",
                                  security(stderr="User interaction is not allowed.", code=36)):
            with self.assertRaisesRegex(RuntimeError, "keychain is locked"):
                runner.run_env(self.ctx, "work-only", 1, self.name, 7, 1)

    def test_cline_argv_uses_the_anthropic_provider(self):
        with mock.patch.object(platforms, "cline_exe", return_value="/x/cline"):
            argv = platforms.argv_for(self.pconf, "do it", "/wt", "build", 60)
        self.assertEqual(argv[argv.index("-P") + 1], "anthropic")
        self.assertEqual(argv[argv.index("-m") + 1], "claude-sonnet-5-5")
        self.assertNotIn("-k", argv)                  # the key never goes in argv


class CostReportTests(Base):
    def test_cents_are_parsed_exactly(self):
        org, ws = api_credits.parse_cost_page(page([("1234.5", "wrkspc_mahler"),
                                                    ("100", None), ("0.25", "other")]),
                                              "wrkspc_mahler")
        self.assertEqual(org, Decimal("1334.75"))
        self.assertEqual(ws, Decimal("1234.5"))

    def test_malformed_pages_raise(self):
        for body in ({}, {"data": "x"}, {"data": [{"results": [{"currency": "USD"}]}]},
                     {"data": [{"results": [{"amount": "NaN"}]}]},
                     {"data": [{"results": [{"amount": "1", "currency": "EUR"}]}]},
                     {"data": [{"results": [{"amount": "abc"}]}]}):
            with self.subTest(body=body), self.assertRaises(api_credits.CostError):
                api_credits.parse_cost_page(body)

    def test_pagination_and_headers(self):
        calls = []
        opener = opener_for([page([("1000", "wrkspc_mahler")], True, "p2"),
                             page([("550", None)])], calls)
        reading = api_credits.fetch_cost("sk-ant-admin01-k", datetime(2026, 10, 15, 6, tzinfo=UTC),
                                         "wrkspc_mahler", opener=opener)
        self.assertEqual(reading, {"org_usd": 15.5, "workspace_usd": 10.0})
        self.assertEqual(len(calls), 2)
        self.assertIn("starting_at=2026-10-15T00%3A00%3A00Z", calls[0].full_url)
        self.assertIn("group_by%5B%5D=workspace_id", calls[0].full_url)
        self.assertIn("page=p2", calls[1].full_url)
        self.assertEqual(calls[0].get_header("X-api-key"), "sk-ant-admin01-k")
        self.assertEqual(calls[0].get_header("Anthropic-version"), "2023-06-01")

    def test_errors(self):
        with self.assertRaises(api_credits.CostError):
            api_credits.fetch_cost("k", self.NOW, opener=opener_for([page([], True, None)]))
        auth = urllib.error.HTTPError(api_credits.COST_URL, 401, "no", {}, io.BytesIO(b""))
        with self.assertRaises(api_credits.CostAuthError):
            api_credits.fetch_cost("k", self.NOW, opener=opener_for([auth]))
        with self.assertRaises(api_credits.CostError):
            api_credits.fetch_cost("k", self.NOW,
                                   opener=opener_for([urllib.error.URLError("down")]))
        with self.assertRaises(api_credits.CostError):
            api_credits.fetch_cost("k", self.NOW, max_pages=2,
                                   opener=opener_for([page([], True, "a"), page([], True, "b")]))

    def test_refresh_caches_and_rate_limits(self):
        calls = []
        api_credits.refresh(self.ctx, run=keys(), opener=opener_for([page([("2000", "wrkspc_mahler")])], calls))
        snap = api_credits.snapshot(self.led, self.name, self.pconf)
        self.assertEqual(snap["org_spend_usd"], 20.0)
        self.assertEqual(snap["workspace_spend_usd"], 20.0)
        self.assertEqual(self.led.usage(self.name)[api_credits.WINDOW]["used_pct"], 100.0)
        self.clock.now += timedelta(minutes=10)
        api_credits.refresh(self.ctx, run=keys(), opener=opener_for([], calls))
        self.assertEqual(len(calls), 1)                # cached for 15 minutes
        self.clock.now += timedelta(minutes=6)
        failing = opener_for([urllib.error.URLError("down")] * 5, calls)
        api_credits.refresh(self.ctx, run=keys(), opener=failing)
        api_credits.refresh(self.ctx, run=keys(), opener=failing)   # same minute: no retry
        self.assertEqual(len(calls), 2)
        self.assertEqual(api_credits.snapshot(self.led, self.name, self.pconf)["org_spend_usd"], 20.0)

    def test_admin_key_never_logged_or_persisted(self):
        api_credits.refresh(self.ctx, run=keys(), opener=opener_for([page([("1", None)])]))
        dump = json.dumps([dict(r) for r in self.led.q("SELECT * FROM kv")]) + "".join(self.ctx.lines)
        self.assertNotIn("ADMINKEY", dump)
        self.assertNotIn("WORKSPACEKEY", dump)


class ExpiryTests(Base):
    def test_cadence(self):
        due = api_credits.expiry_notice_due
        self.assertIsNone(due(15, {}))
        self.assertEqual(due(14, {}), "14d")
        self.assertIsNone(due(10, {"stage": "14d"}))
        self.assertEqual(due(3, {"stage": "14d"}), "daily")
        self.assertEqual(due(-1, {}), "daily")
        self.assertIsNone(due(None, {}))

    def test_refresh_pings_14_days_out_then_daily_from_3(self):
        def expiry_pings():
            return [p for p in self.ctx.pings if "key expires" in p[0] or "key has expired" in p[0]]
        start = datetime(2027, 9, 30, 12, tzinfo=UTC)          # 16 days before 2027-10-16
        for day in range(20):
            self.clock.now = start + timedelta(days=day)
            for hour in (0, 6):                                # two ticks a day
                self.clock.now += timedelta(hours=hour)
                api_credits.refresh(self.ctx, run=keys(), opener=opener_for([page([])] * 10))
        days = [p[0] for p in expiry_pings() if p[0].startswith("Mahler: Anthropic API")]
        # 14 days out once, then 3, 2, 1, today, then daily once expired
        self.assertEqual(days[0], "Mahler: Anthropic API key expires in 14 days")
        self.assertEqual(days[1:5], ["Mahler: Anthropic API key expires in 3 days",
                                     "Mahler: Anthropic API key expires in 2 days",
                                     "Mahler: Anthropic API key expires in 1 days",
                                     "Mahler: Anthropic API key expires today"])
        self.assertTrue(all("has expired" in d for d in days[5:]))
        self.assertEqual(len(days), 5 + 3)                     # 10-17, 10-18, 10-19
        self.assertTrue(any(p[0].startswith("Mahler: Anthropic admin") for p in expiry_pings()))

    def test_status_shows_expiry_within_30_days(self):
        self.key_ok()
        self.clock.now = datetime(2027, 10, 1, tzinfo=UTC)
        self.key_ok()
        self.assertIn("API key expires 2027-10-16 (in 15d)", self.state()[1])


class RedactTests(unittest.TestCase):
    def test_anthropic_keys_are_masked(self):
        for key in ("sk-ant-api03-AbCdEf0123456789_-xyzXYZ", "sk-ant-admin01-AbCdEf0123456789_-xyz"):
            with self.subTest(key=key):
                out = redact.redact(f"error with key {key} in it")
                self.assertNotIn(key, out)
                self.assertIn(redact.MARK, out)
        self.assertNotIn("sk-ant", redact.redact("ANTHROPIC_API_KEY=sk-ant-api03-zzzzzzzzzzzzzzzzzz"))


if __name__ == "__main__":
    unittest.main()
