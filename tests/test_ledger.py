"""Lease semantics (DESIGN D6) — the part of the kernel that must be right."""

import unittest
from datetime import datetime, timedelta, timezone

from mahler.ledger import Ledger


class Clock:
    def __init__(self):
        self.t = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.led = Ledger(":memory:", clock=self.clock)

    def test_auto_vs_auto_never_double_assigns(self):
        a, _ = self.led.claim("p", 1, "run:1", "auto", 10)
        b, info = self.led.claim("p", 1, "run:2", "auto", 10)
        self.assertIsNotNone(a)
        self.assertIsNone(b)
        self.assertEqual(info["held_by"]["holder"], "run:1")

    def test_interactive_preempts_auto_and_flags_the_run(self):
        run_id = self.led.create_run(project="p", number=1, role="build",
                                     platform="agy-claude", epoch=0)
        auto, _ = self.led.claim("p", 1, f"run:{run_id}", "auto", 10, run_id=run_id)
        you, info = self.led.claim("p", 1, "interactive:you", "interactive", 30)
        self.assertIsNotNone(you)
        self.assertEqual(info["preempted"]["holder"], f"run:{run_id}")
        self.assertGreater(you["epoch"], auto["epoch"])
        self.assertIsNotNone(self.led.run(run_id)["yield_at"])

    def test_auto_cannot_take_from_interactive(self):
        self.led.claim("p", 1, "interactive:you", "interactive", 30)
        lease, info = self.led.claim("p", 1, "run:9", "auto", 10)
        self.assertIsNone(lease)
        self.assertIn("held_by", info)

    def test_interactive_vs_interactive_needs_steal(self):
        self.led.claim("p", 1, "interactive:mac", "interactive", 30)
        refused, _ = self.led.claim("p", 1, "interactive:phone", "interactive", 30)
        self.assertIsNone(refused)
        stolen, info = self.led.claim("p", 1, "interactive:phone", "interactive", 30, steal=True)
        self.assertIsNotNone(stolen)
        self.assertEqual(info["stolen_from"]["holder"], "interactive:mac")

    def test_epoch_fences_a_zombie(self):
        old, _ = self.led.claim("p", 1, "run:1", "auto", 10)
        self.clock.advance(minutes=11)                        # presumed dead
        new, _ = self.led.claim("p", 1, "run:2", "auto", 10)
        self.assertFalse(self.led.lease_check("p", 1, old["epoch"]))
        self.assertTrue(self.led.lease_check("p", 1, new["epoch"]))
        self.assertFalse(self.led.heartbeat("p", 1, "run:1", old["epoch"], 10))

    def test_same_holder_renews_without_new_epoch(self):
        a, _ = self.led.claim("p", 1, "interactive:you", "interactive", 30)
        self.clock.advance(minutes=5)
        b, info = self.led.claim("p", 1, "interactive:you", "interactive", 30)
        self.assertEqual(a["epoch"], b["epoch"])
        self.assertTrue(info.get("renewed"))

    def test_expiry_frees_the_item(self):
        self.led.claim("p", 1, "run:1", "auto", 10)
        self.clock.advance(minutes=9)
        self.assertEqual(self.led.expired_leases(), [])
        self.clock.advance(minutes=2)
        self.assertEqual(len(self.led.expired_leases()), 1)
        self.assertIsNone(self.led.lease("p", 1))
        again, _ = self.led.claim("p", 1, "run:2", "auto", 10)
        self.assertIsNotNone(again)

    def test_heartbeat_keeps_it_alive(self):
        lease, _ = self.led.claim("p", 1, "run:1", "auto", 10)
        for _ in range(5):
            self.clock.advance(minutes=8)
            self.assertTrue(self.led.heartbeat("p", 1, "run:1", lease["epoch"], 10))
        self.assertTrue(self.led.lease_check("p", 1, lease["epoch"]))

    def test_release_only_by_matching_holder(self):
        lease, _ = self.led.claim("p", 1, "run:1", "auto", 10)
        self.assertFalse(self.led.release("p", 1, holder="run:2"))
        self.assertTrue(self.led.release("p", 1, holder="run:1", epoch=lease["epoch"]))
        self.assertIsNone(self.led.lease("p", 1))

    def test_next_id_is_monotonic_and_respects_floor(self):
        self.assertEqual(self.led.next_id("p", "D", floor=227), 228)
        self.assertEqual(self.led.next_id("p", "D"), 229)
        self.assertEqual(self.led.next_id("q", "D"), 1)


class StateTests(unittest.TestCase):
    def test_set_state_logs_transition(self):
        led = Ledger(":memory:")
        led.upsert_item("p", 3, title="x")
        led.set_state("p", 3, "ready", "sorted")
        self.assertEqual(led.item("p", 3)["state"], "ready")
        ev = led.q("SELECT * FROM events WHERE kind='state'")[-1]
        self.assertIn("inbox -> ready", ev["detail"])


if __name__ == "__main__":
    unittest.main()
