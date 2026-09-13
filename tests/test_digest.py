"""Daily digest (mahler#6): the text builder is pure; the send is kv-gated."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from mahler import digest
from mahler.ledger import Ledger, iso


class Clock:
    def __init__(self, t=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)):
        self.t = t

    def __call__(self):
        return self.t


def seeded_ledger(clock=None):
    """A ledger with one shipped item, one needs_you, one handoff, one usage row."""
    clock = clock or Clock()
    led = Ledger(":memory:", clock=clock)
    led.upsert_item("mahler", 6, title="Daily digest ping")
    led.upsert_item("mahler", 8, title="Setup errors")
    led.set_state("mahler", 8, "needs_you", "you asked")
    old = clock.t - timedelta(hours=30)           # outside the 24h window
    led.q("INSERT INTO events (at, project, number, kind, detail) VALUES (?,?,?,?,?)",
          (iso(old), "mahler", 9, "state", "working -> done (old news)"))
    led.set_state("mahler", 6, "done", "merged")
    led.event("state", "mahler", 8, "ready -> ready (handoff (quota))")
    led.record_usage("claude", "5h", 42.0)
    led.record_usage("claude", "weekly", 10.5)
    return led


class FormatTests(unittest.TestCase):
    def data(self):
        return digest.gather(seeded_ledger(), {"platforms": {"claude": {"enabled": True}}},
                             since=datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc),
                             now=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc))

    def test_includes_shipped_with_titles(self):
        text = digest.format_digest(self.data())
        self.assertIn("Shipped in the last 24h (1):", text)
        self.assertIn("- mahler#6 Daily digest ping", text)

    def test_includes_waiting_on_you(self):
        text = digest.format_digest(self.data())
        self.assertIn("Waiting on you (1):", text)
        self.assertIn("- mahler#8 [needs_you] Setup errors", text)

    def test_includes_handoffs(self):
        text = digest.format_digest(self.data())
        self.assertIn("Handoffs (1):", text)
        self.assertIn("handoff (quota)", text)

    def test_usage_lines(self):
        text = digest.format_digest(self.data())
        self.assertIn("- claude: 5h 42%, week 10%", text)

    def test_empty_day(self):
        data = {"shipped": [], "waiting": [], "handoffs": [],
                "usage": [{"platform": "claude", "5h": None, "weekly": None}]}
        text = digest.format_digest(data)
        self.assertIn("- none", text)
        self.assertIn("- nothing", text)
        self.assertIn("claude: 5h no data, week no data", text)

    def test_gather_ignores_out_of_window_events(self):
        data = self.data()
        self.assertEqual([s["number"] for s in data["shipped"]], [6])
        self.assertNotIn(9, [s["number"] for s in data["shipped"]])


class GatingTests(unittest.TestCase):
    def setUp(self):
        self.led = Ledger(":memory:")
        self.addCleanup(self.led.close)
        self.nine = datetime(2026, 9, 12, 9, 0)          # local wall time

    def test_not_due_before_the_hour(self):
        early = datetime(2026, 9, 12, 7, 59)
        self.assertFalse(digest.should_send(self.led, early, hour=8))

    def test_due_once_then_not_again_the_same_day(self):
        self.assertTrue(digest.should_send(self.led, self.nine, hour=8))
        self.led.set_kv(digest.KV_KEY, "2026-09-12")
        self.assertFalse(digest.should_send(self.led, self.nine, hour=8))

    def test_due_again_the_next_day(self):
        self.led.set_kv(digest.KV_KEY, "2026-09-11")
        self.assertTrue(digest.should_send(self.led, self.nine, hour=8))


class Ctx:
    def __init__(self, cfg, led, dry_run=False):
        self.cfg, self.led, self.dry_run = cfg, led, dry_run
        self.lines = []

    def say(self, msg):
        self.lines.append(msg)


class MaybeSendTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(datetime(2026, 9, 12, 9, 30, tzinfo=timezone.utc))
        self.led = seeded_ledger(self.clock)
        self.cfg = {"platforms": {"claude": {"enabled": True}},
                    "digest": {"hour": 8}}
        self.ctx = Ctx(self.cfg, self.led)
        self.local = patch.object(digest, "_local_now",
                                  return_value=datetime(2026, 9, 12, 10, 30))
        self.local.start()
        self.addCleanup(self.local.stop)

    def test_sends_once_and_records_the_kv_key(self):
        with patch("mahler.digest.notify.send", return_value=True) as send:
            digest.maybe_send(self.ctx)
        self.assertEqual(send.call_count, 1)
        title, body = send.call_args[0][1:]
        self.assertEqual(title, digest.TITLE)
        self.assertIn("mahler#6 Daily digest ping", body)
        self.assertEqual(self.led.get_kv(digest.KV_KEY), "2026-09-12")
        # second tick the same day: no repeat
        with patch("mahler.digest.notify.send", return_value=True) as send:
            digest.maybe_send(self.ctx)
        self.assertEqual(send.call_count, 0)

    def test_failed_send_does_not_set_the_kv_key(self):
        with patch("mahler.digest.notify.send", return_value=False) as send:
            digest.maybe_send(self.ctx)
        self.assertEqual(send.call_count, 1)
        self.assertIsNone(self.led.get_kv(digest.KV_KEY))
        self.assertIn("will retry next tick", self.ctx.lines[-1])

    def test_dry_run_never_sends(self):
        self.ctx.dry_run = True
        with patch("mahler.digest.notify.send", return_value=True) as send:
            digest.maybe_send(self.ctx)
        self.assertEqual(send.call_count, 0)
        self.assertIsNone(self.led.get_kv(digest.KV_KEY))

    def test_a_crash_never_breaks_the_tick(self):
        with patch("mahler.digest.gather", side_effect=RuntimeError("boom")):
            digest.maybe_send(self.ctx)          # must not raise
        self.assertTrue(any("digest failed" in l for l in self.ctx.lines))


if __name__ == "__main__":
    unittest.main()
