"""Launch availability must not consume an item's implementation budget."""

import unittest
from datetime import timedelta
from unittest import mock

from mahler import scheduler, tick
from mahler.ledger import Ledger
from tests.test_schedule import NOW, item, mk_cfg, proj


class LaunchCapacityTests(unittest.TestCase):
    def test_three_launch_failures_preserve_budget_and_retry_after_backoff(self):
        led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(led.close)
        cfg = mk_cfg({"a": proj(max_parallel=1)}, total=1)
        ctx = scheduler.Ctx(cfg, led)
        ctx.ping = mock.Mock()
        item(led, "a", 1)
        led.upsert_item("a", 1, attempts=2, esc_tier=1, esc_fails=1)
        with mock.patch("mahler.tick.runner.prepare", side_effect=OSError("temporarily unavailable")) as prepare, \
                mock.patch("mahler.launch_health.version._short_head", return_value="abc1234"):
            for _ in range(3):
                self.assertFalse(tick.start(ctx, "a", led.item("a", 1), "build", "claude"))
            current = led.item("a", 1)
            self.assertEqual((current["state"], current["attempts"], current["esc_tier"],
                              current["esc_fails"]), ("ready", 2, 1, 1))
            self.assertIsNone(led.lease("a", 1))
            self.assertFalse(tick.start(ctx, "a", current, "build", "claude"))
            self.assertEqual(prepare.call_count, 3)
            self.assertIsNotNone(led.get_kv("launch_broken:a"))
            led.now = lambda: NOW + timedelta(minutes=30)
            self.assertFalse(tick.start(ctx, "a", current, "build", "claude"))
            self.assertEqual(prepare.call_count, 4)
            self.assertEqual(led.item("a", 1)["attempts"], 2)
