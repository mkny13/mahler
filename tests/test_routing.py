"""Routing policy (DESIGN D8) and platform output parsing."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

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
        led.record_usage("cline-free", "5h", 100.0, iso(NOW + timedelta(minutes=30)))
        name, reasons = router.pick(self.cfg, led, "build", size="s")
        self.assertIsNone(name)
        self.assertTrue(any("backing off" in r for r in reasons))

    def test_pin_overrides_order(self):
        led = led_with(**{"agy-claude": (10, 10), "agy-gemini": (10, 10)})
        self.assertEqual(router.pick(self.cfg, led, "build", pin="agy-gemini")[0], "agy-gemini")


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


if __name__ == "__main__":
    unittest.main()
