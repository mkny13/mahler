"""Routing policy (DESIGN D8) and platform output parsing."""

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler import config, platforms, router
from mahler.ledger import Ledger, iso

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


def led_with(**usage):
    led = Ledger(":memory:", clock=lambda: NOW)
    later = iso(NOW + timedelta(hours=3))
    for name, (five, weekly) in usage.items():
        led.record_usage(name, "5h", five, later)
        led.record_usage(name, "weekly", weekly, later)
    return led


class RouterTests(unittest.TestCase):
    cfg = config.DEFAULTS

    def test_free_tiers_build_first(self):
        led = led_with(**{"agy-claude": (10, 10), "agy-gemini": (10, 10), "claude": (5, 5)})
        self.assertEqual(router.pick(self.cfg, led, "build")[0], "agy-claude")

    def test_claude_plans(self):
        led = led_with(**{"agy-claude": (10, 10), "claude": (5, 5)})
        self.assertEqual(router.pick(self.cfg, led, "sort")[0], "claude")

    def test_claude_builds_only_under_its_reserve(self):
        led = led_with(**{"agy-claude": (95, 20), "agy-gemini": (20, 92), "claude": (59, 10)})
        self.assertEqual(router.pick(self.cfg, led, "build")[0], "claude")
        led = led_with(**{"agy-claude": (95, 20), "agy-gemini": (20, 92), "claude": (61, 10)})
        name, reasons = router.pick(self.cfg, led, "build")
        self.assertIsNone(name)
        self.assertTrue(any("claude: soft" in r for r in reasons))

    def test_fix_routes_like_a_build(self):
        """Red CI's fix runs use the build routing, not a separate table (D18, #18)."""
        led = led_with(**{"agy-claude": (10, 10), "agy-gemini": (10, 10), "claude": (5, 5)})
        self.assertEqual(router.pick(self.cfg, led, "fix")[0], "agy-claude")

    def test_weekly_reserve_applies_too(self):
        led = led_with(**{"agy-claude": (95, 95), "agy-gemini": (95, 95), "claude": (10, 71)})
        self.assertIsNone(router.pick(self.cfg, led, "build")[0])

    def test_unknown_usage_counts_as_over_the_line(self):
        led = led_with(**{"agy-claude": (10, 10)})
        led.q("DELETE FROM usage WHERE platform='agy-claude' AND window='weekly'")
        name, _ = router.pick(self.cfg, led, "build")
        self.assertNotEqual(name, "agy-claude")

    def test_stale_samples_are_unknown(self):
        led = Ledger(":memory:", clock=lambda: NOW)
        old = iso(NOW - timedelta(minutes=30))
        for w in ("5h", "weekly"):
            led.record_usage("agy-claude", w, 1, iso(NOW + timedelta(hours=2)), sampled_at=old)
        self.assertEqual(router.usage_state(led, "agy-claude",
                                            self.cfg["platforms"]["agy-claude"])[0], "stale")

    def test_hard_line(self):
        led = led_with(**{"agy-gemini": (91, 10)})
        self.assertEqual(router.usage_state(led, "agy-gemini",
                                            self.cfg["platforms"]["agy-gemini"])[0], "hard")

    def test_cline_is_unmetered_but_small_items_only(self):
        led = led_with(**{"agy-claude": (95, 95), "agy-gemini": (95, 95), "claude": (5, 5)})
        self.assertEqual(router.pick(self.cfg, led, "build", size="s")[0], "cline-free")
        self.assertEqual(router.pick(self.cfg, led, "build", size="m")[0], "claude")

    def test_cline_backs_off_after_a_quota_error(self):
        led = led_with(**{"agy-claude": (95, 95), "agy-gemini": (95, 95), "claude": (80, 5)})
        for name in ("cline-free", "kilo"):
            led.record_usage(name, "5h", 100.0, iso(NOW + timedelta(minutes=30)))
        led.record_usage("copilot", "monthly", 100.0, iso(NOW + timedelta(minutes=30)))
        name, reasons = router.pick(self.cfg, led, "build", size="s")
        self.assertIsNone(name)
        self.assertTrue(any("backing off" in r for r in reasons))

    def test_pin_overrides_order(self):
        led = led_with(**{"agy-claude": (10, 10), "agy-gemini": (10, 10)})
        self.assertEqual(router.pick(self.cfg, led, "build", pin="agy-gemini")[0], "agy-gemini")

    def test_claude_opus_is_pinnable_but_never_beats_claude_unpinned(self):
        # same account/quota as "claude" (real Claude Code CLI, forced to
        # Opus), so it's last in routing order and only reached via a pin
        led = led_with(**{"agy-claude": (95, 95), "agy-gemini": (95, 95),
                           "claude": (5, 5), "claude-opus": (5, 5)})
        self.assertEqual(router.pick(self.cfg, led, "build")[0], "claude")
        self.assertEqual(router.pick(self.cfg, led, "build", pin="claude-opus")[0],
                          "claude-opus")

    def test_claude_opus_model_flag(self):
        argv = platforms.claude_argv(self.cfg["platforms"]["claude-opus"], "hi", "wt", "build")
        self.assertIn("opus", argv)

    def test_kilo_is_unmetered_last_resort_builder(self):
        led = led_with(**{"agy-claude": (95, 95), "agy-gemini": (95, 95), "claude": (5, 5)})
        led.record_usage("cline-free", "5h", 100.0, iso(NOW + timedelta(minutes=30)))
        led.record_usage("copilot", "monthly", 100.0, iso(NOW + timedelta(minutes=30)))
        self.assertEqual(router.pick(self.cfg, led, "build", size="s")[0], "kilo")

    def test_copilot_is_metered_by_monthly_ai_credits(self):
        led = led_with(**{"agy-claude": (95, 95), "agy-gemini": (95, 95), "claude": (5, 5)})
        led.record_usage("cline-free", "5h", 100.0, iso(NOW + timedelta(minutes=30)))
        later = iso(NOW + timedelta(days=5))
        led.record_usage("copilot", "monthly", 10.0, later)
        self.assertEqual(router.pick(self.cfg, led, "build", size="s")[0], "copilot")
        led.record_usage("copilot", "monthly", 96.0, later)     # over its hard line
        name, reasons = router.pick(self.cfg, led, "build", size="s")
        self.assertEqual(name, "kilo")     # next in order now that copilot's ahead of kilo
        self.assertTrue(any("copilot: hard" in r for r in reasons))

    def test_kilo_defaults_to_a_free_model_route(self):
        # mahler#29: without an explicit :free route, every kilo run 402s on credits.
        self.assertTrue(self.cfg["platforms"]["kilo"]["model"].endswith("/free"))


class ParseTests(unittest.TestCase):
    def test_agy_usage(self):
        data = {"command": {"data": {"groups": [
            {"name": "Gemini Models", "buckets": [
                {"window": "weekly", "remaining_fraction": 0.74, "reset_time": "2026-09-18T21:25:17Z"},
                {"window": "5h", "remaining_fraction": 0.78, "reset_time": "2026-09-13T00:10:06Z"}]},
            {"name": "Claude and GPT models", "buckets": [
                {"window": "weekly", "remaining_fraction": 1}]}]}}}
        pools = platforms.parse_agy_usage(data)
        self.assertEqual(pools["Gemini Models"][0], ("weekly", 26.0, "2026-09-18T21:25:17Z"))
        self.assertEqual(pools["Claude and GPT models"][0][1], 0.0)

    def test_claude_rate_limit_event(self):
        ev = {"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed", "unifiedWindows": {
                "five_hour": {"utilization": 0.33, "resetsAt": 1789257600},
                "seven_day": {"utilization": 0.49, "resetsAt": 1789462800}}}}
        s = platforms.claude_samples_from_event(ev)
        self.assertEqual([x[:2] for x in s], [("5h", 33.0), ("weekly", 49.0)])

    def test_logs_and_status_lines(self):
        with tempfile.TemporaryDirectory() as d:
            c = os.path.join(d, "c.log")
            with open(c, "w") as fh:
                fh.write(json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "working"}]}}) + "\n")
                fh.write(json.dumps({"type": "result", "subtype": "success",
                                     "result": "All done.\nSTATUS: MERGED #7"}) + "\n")
            r = platforms.read_log(c, "claude")
            self.assertTrue(r["ok"])
            self.assertEqual(platforms.status_line(r["final"]), ("MERGED", "#7"))

            g = os.path.join(d, "g.log")
            with open(g, "w") as fh:
                fh.write(json.dumps({"event": "step_update", "step_update": {
                    "text_delta": "STATUS: NEEDS-YOU "}}) + "\n")
                fh.write(json.dumps({"event": "result", "result": {
                    "status": "SUCCESS", "response": "STATUS: NEEDS-YOU Which colour?\n"}}) + "\n")
            r = platforms.read_log(g, "agy")
            self.assertEqual(platforms.status_line(r["final"]), ("NEEDS-YOU", "Which colour?"))
        self.assertEqual(platforms.status_line("no status here"), (None, None))
        self.assertEqual(platforms.status_line("**STATUS: READY**"), ("READY", ""))
        # mahler#15: a build run ends at STATUS: DONE <summary>
        self.assertEqual(platforms.status_line("STATUS: DONE wired the exporter"),
                         ("DONE", "wired the exporter"))
        self.assertEqual(platforms.status_line("all green\nSTATUS: DONE pushed; summary here"),
                         ("DONE", "pushed; summary here"))

    def test_copilot_log(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "p.log")
            with open(p, "w") as fh:
                fh.write(json.dumps({"type": "assistant.message",
                                     "data": {"content": "working on it"}}) + "\n")
                fh.write(json.dumps({"type": "assistant.message",
                                     "data": {"content": "STATUS: DONE shipped it"}}) + "\n")
                fh.write(json.dumps({"type": "result", "exitCode": 0,
                                     "usage": {"premiumRequests": 1}}) + "\n")
            r = platforms.read_log(p, "copilot")
            self.assertTrue(r["ok"])
            self.assertFalse(r["quota_hit"])
            self.assertEqual(platforms.status_line(r["final"]), ("DONE", "shipped it"))

    def test_copilot_quota_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "p.log")
            with open(p, "w") as fh:
                fh.write(json.dumps({"type": "error", "error": {
                    "message": "You have exceeded your premium request quota"}}) + "\n")
            r = platforms.read_log(p, "copilot")
            self.assertTrue(r["quota_hit"])

    def test_kilo_log(self):
        # Real shape from `kilo run ... --format json -m kilo/kilo-auto/free`
        # (mahler#29, verified after `kilo auth login`).
        with tempfile.TemporaryDirectory() as d:
            k = os.path.join(d, "k.log")
            with open(k, "w") as fh:
                fh.write(json.dumps({"type": "step_start", "part": {"type": "step-start"}}) + "\n")
                fh.write(json.dumps({"type": "text",
                                     "part": {"type": "text", "text": "STATUS: READY"}}) + "\n")
                fh.write(json.dumps({"type": "step_finish", "part": {
                    "type": "step-finish", "reason": "stop",
                    "model": {"providerID": "kilo", "modelID": "poolside/laguna-s-2.1:free"},
                    "cost": 0}}) + "\n")
            r = platforms.read_log(k, "kilo")
            self.assertFalse(r["quota_hit"])
            self.assertEqual(platforms.status_line(r["last_text"]), ("READY", ""))

    def test_kilo_out_of_credits_is_a_quota_hit(self):
        # Real 402 shape hit on the default (non-:free) model (mahler#29): no
        # "quota" in the text, so this exercises the broader QUOTA_WORDS list.
        with tempfile.TemporaryDirectory() as d:
            k = os.path.join(d, "k.log")
            with open(k, "w") as fh:
                fh.write(json.dumps({"type": "error", "error": {"data": {
                    "message": "Add credits to continue, or switch to a free model",
                    "statusCode": 402,
                    "responseBody": '{"error_type":"usage_limit_exceeded"}'}}}) + "\n")
            r = platforms.read_log(k, "kilo")
            self.assertTrue(r["quota_hit"])

    def test_probe_copilot_sums_ai_credits_into_a_monthly_pct(self):
        payload = {"timePeriod": {"year": 2026, "month": 9}, "user": "mkny13", "usageItems": [
            {"product": "Copilot", "sku": "Copilot AI Credits",
             "model": "Auto: Claude Haiku 4.5", "grossQuantity": 5.632911},
            {"product": "Copilot", "sku": "Copilot AI Credits",
             "model": "Claude Sonnet 5", "grossQuantity": 6.9314}]}
        with mock.patch("subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout="mkny13\n"),
                subprocess.CompletedProcess([], 0, stdout=json.dumps(payload)),
            ]
            samples = platforms.probe_copilot(1500)
        self.assertEqual(len(samples), 1)
        window, pct, resets = samples[0]
        self.assertEqual(window, "monthly")
        self.assertAlmostEqual(pct, round(100 * (5.632911 + 6.9314) / 1500, 1))
        self.assertTrue(resets)

    def test_probe_copilot_needs_a_login(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout="\n")
            self.assertEqual(platforms.probe_copilot(1500), [])
        run.assert_called_once()      # never got to the billing call

    def test_next_month_start_rolls_over_the_year(self):
        dec = datetime(2026, 12, 15, tzinfo=timezone.utc)
        self.assertEqual(platforms._next_month_start(dec), datetime(2027, 1, 1, tzinfo=timezone.utc))

    def test_kilo_auth_error_is_not_a_quota_hit(self):
        with tempfile.TemporaryDirectory() as d:
            k = os.path.join(d, "k.log")
            with open(k, "w") as fh:
                fh.write(json.dumps({"type": "error", "error": {
                    "data": {"message": "You need to sign in to use this model.",
                             "statusCode": 401}}}) + "\n")
            r = platforms.read_log(k, "kilo")
            self.assertFalse(r["quota_hit"])


if __name__ == "__main__":
    unittest.main()
