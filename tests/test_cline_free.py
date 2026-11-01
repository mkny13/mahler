"""cline-free runs only on Cline's published free models (mahler#1507)."""

import copy
import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler import cline_free, config, finalize, router, scheduler
from mahler.ledger import Ledger, iso

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
URL = config.DEFAULTS["platforms"]["cline-free"]["free_models_url"]


class _Res(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_for(free):
    return lambda req, timeout=None: _Res(json.dumps({"free": free}).encode())


def broken(req, timeout=None):
    raise OSError("offline")


class Base(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.clock = [NOW]
        self.led = Ledger(":memory:", clock=lambda: self.clock[0])
        self.addCleanup(self.led.close)

    def choose(self, opener, **kw):
        return cline_free.choose(self.cfg, self.led, refresh_list=True, opener=opener, **kw)


class FreeListTests(Base):
    def test_default_is_a_current_free_model_with_a_list_url(self):
        pconf = self.cfg["platforms"]["cline-free"]
        self.assertEqual(pconf["model"], "cline-free/solar-mini4")
        self.assertTrue(pconf["free_models_url"].startswith("https://api.cline.bot/"))

    def test_fetch_parses_ids_and_returns_none_when_unreachable(self):
        self.assertEqual(cline_free.fetch_free_models(URL, opener=opener_for(
            [{"id": "a/free"}, {"name": "no id"}, "b/free"])), ["a/free", "b/free"])
        self.assertIsNone(cline_free.fetch_free_models(URL, opener=broken))

    def test_list_change_switches_away_from_a_model_that_left(self):
        self.assertEqual(self.choose(opener_for([{"id": "a/free"}, {"id": "b/free"}])), "a/free")
        self.clock[0] += timedelta(hours=1)           # cache stale
        self.assertEqual(self.choose(opener_for([{"id": "b/free"}, {"id": "c/free"}])), "b/free")

    def test_unreachable_list_keeps_last_known_good_model(self):
        self.assertEqual(self.choose(opener_for([{"id": "a/free"}])), "a/free")
        self.clock[0] += timedelta(hours=1)
        self.assertEqual(self.choose(broken), "a/free")

    def test_unreachable_with_no_history_uses_configured_default(self):
        self.assertEqual(self.choose(broken), "cline-free/solar-mini4")

    def test_measurements_prefer_proven_models(self):
        rows = [{"role": "build", "size": "s", "platform": "cline-free", "model": "b/free", "effort": "",
                 "status": "good", "cost_per_success": 0.0},
                {"role": "build", "size": "s", "platform": "cline-free", "model": "a/free", "effort": "",
                 "status": "below", "cost_per_success": 0.0}]
        with mock.patch.object(cline_free.scorecard, "table", return_value=rows):
            self.assertEqual(self.choose(opener_for([{"id": "a/free"}, {"id": "b/free"},
                                                     {"id": "c/free"}])), "b/free")


class BilledTests(Base):
    def test_rejected_model_is_skipped_and_none_left_means_no_model(self):
        self.choose(opener_for([{"id": "a/free"}, {"id": "b/free"}]))
        cline_free.reject(self.led, "a/free")
        self.assertEqual(self.choose(opener_for([{"id": "a/free"}, {"id": "b/free"}])), "b/free")
        cline_free.reject(self.led, "b/free")
        self.assertIsNone(self.choose(opener_for([{"id": "a/free"}, {"id": "b/free"}])))

    def test_router_holds_platform_when_no_free_model_remains(self):
        pconf = self.cfg["platforms"]["cline-free"]
        self.choose(opener_for([{"id": "a/free"}]))
        self.assertEqual(router.usage_state(self.led, "cline-free", pconf)[0], "ok")
        cline_free.reject(self.led, "a/free")
        self.assertEqual(router.usage_state(self.led, "cline-free", pconf)[0], "hard")


class InsufficientBalanceTests(Base):
    def run_ended(self, free):
        self.cfg["projects"]["x"] = {"path": "/tmp", "repo": "x/y"}
        self.led.upsert_item("x", 5, state="working", priority=2, sorted_at=iso(NOW))
        self.choose(opener_for([{"id": m} for m in free]))
        rid = self.led.create_run(project="x", number=5, role="build", platform="cline-free",
                                  epoch=1, status="running", model="a/free")
        ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
        return ctx, self.led.run(rid)

    def test_billing_model_is_rejected_without_a_platform_hold(self):
        ctx, run = self.run_ended(["a/free", "b/free"])
        with mock.patch.object(ctx, "ping") as ping:
            finalize._model_billed(ctx, run)
        self.assertEqual(cline_free.candidates(self.led, self.cfg["platforms"]["cline-free"]), ["b/free"])
        self.assertNotIn(router.HOLD, self.led.usage("cline-free"))
        ping.assert_not_called()

    def test_last_free_model_billing_holds_platform_and_pings(self):
        ctx, run = self.run_ended(["a/free"])
        with mock.patch.object(ctx, "ping") as ping:
            finalize._model_billed(ctx, run)
        self.assertIn(router.HOLD, self.led.usage("cline-free"))
        self.assertEqual(self.led.get_kv("hold_reason:cline-free"), "no_free_models")
        ping.assert_called_once()

    def test_audit_flags_model_absent_from_free_list(self):
        from mahler import platform_audit
        self.choose(opener_for([{"id": "other/free"}]))
        body = platform_audit.build_body(self.cfg, self.led, config.platform_audit_policy(self.cfg))
        section = body.split("### Configured models flagged by availability evidence", 1)[1]
        self.assertIn("| `cline-free` | `cline-free/solar-mini4` |", section)
        self.assertIn("free list", section)


if __name__ == "__main__":
    unittest.main()
