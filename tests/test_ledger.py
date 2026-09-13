"""Lease semantics (DESIGN D6) — the part of the kernel that must be right."""

import copy
import os
import tempfile
import tomllib
import unittest
from datetime import datetime, timedelta, timezone

from mahler import config
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


class ConnectionTests(unittest.TestCase):
    """The status page (mahler serve) opens the ledger with thread_safe=True
    so a server thread can query the connection made on the main thread
    (mahler.serve serialises access with a lock)."""

    def test_thread_safe_ledger_usable_from_another_thread(self):
        import threading
        led = Ledger(":memory:", thread_safe=True)
        result = {}

        def work():
            led.upsert_item("p", 1, title="x")
            result["title"] = led.item("p", 1)["title"]

        t = threading.Thread(target=work)
        t.start()
        t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertEqual(result.get("title"), "x")

    def test_default_ledger_stays_main_thread_only(self):
        # without the flag sqlite3 still guards the connection: the guard is
        # the default, thread_safe must be a deliberate opt-in
        import sqlite3
        import threading
        led = Ledger(":memory:")
        outcome = []

        def work():
            try:
                led.item("p", 1)
                outcome.append("unguarded")
            except sqlite3.ProgrammingError:
                outcome.append("guarded")

        t = threading.Thread(target=work)
        t.start()
        t.join(timeout=5)
        self.assertEqual(outcome, ["guarded"])




class StateTests(unittest.TestCase):
    def test_set_state_logs_transition(self):
        led = Ledger(":memory:")
        led.upsert_item("p", 3, title="x")
        led.set_state("p", 3, "ready", "sorted")
        self.assertEqual(led.item("p", 3)["state"], "ready")
        ev = led.q("SELECT * FROM events WHERE kind='state'")[-1]
        self.assertIn("inbox -> ready", ev["detail"])


class SetupFailCounterTests(unittest.TestCase):
    def test_bump_counts_consecutively_and_reset_clears(self):
        led = Ledger(":memory:")
        led.upsert_item("p", 8, title="x")
        self.assertEqual(led.item("p", 8)["setup_fails"], 0)
        self.assertEqual(led.bump_setup_fails("p", 8), 1)
        self.assertEqual(led.bump_setup_fails("p", 8), 2)
        led.reset_setup_fails("p", 8)
        self.assertEqual(led.item("p", 8)["setup_fails"], 0)

    def test_setup_fails_column_is_added_to_legacy_databases(self):
        import os
        import sqlite3
        import tempfile
        from mahler.ledger import SCHEMA
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.db")
            con = sqlite3.connect(path)
            con.executescript("\n".join(l for l in SCHEMA.splitlines()
                                        if "setup_fails" not in l))
            con.execute("INSERT INTO items (project, number, state) VALUES ('p', 1, 'ready')")
            con.commit()
            con.close()
            led = Ledger(path)
            self.assertEqual(led.item("p", 1)["setup_fails"], 0)


class MaintenanceConfigTests(unittest.TestCase):
    def test_defaults_cover_all_passes(self):
        maintenance = config.DEFAULTS["defaults"]["maintenance"]
        self.assertTrue(maintenance["enabled"])
        self.assertEqual(maintenance["cadence_days"], 30)
        self.assertEqual(maintenance["merged_threshold"], 20)
        self.assertEqual(maintenance["cooldown_days"], 14)
        self.assertEqual(maintenance["passes"], list(config.MAINTENANCE_PASSES))

    def test_project_maintenance_partially_overrides_defaults(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["projects"]["p"] = {
            "maintenance": {"enabled": False, "passes": ["security"], "cadence_days": 45},
        }
        maintenance = config.maintenance_policy(cfg, "p")
        self.assertFalse(maintenance["enabled"])
        self.assertEqual(maintenance["passes"], ["security"])
        self.assertEqual(maintenance["cadence_days"], 45)
        self.assertEqual(maintenance["merged_threshold"], 20)
        self.assertEqual(maintenance["cooldown_days"], 14)

    def test_project_maintenance_table_loads_from_toml(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.toml")
            with open(path, "w") as fh:
                fh.write("""
[projects.p]
enabled = true
repo = "owner/project"
path = "/tmp/project"

[projects.p.maintenance]
enabled = false
passes = ["health", "guidance"]
""")
            with open(path, "rb") as fh:
                loaded = tomllib.load(fh)
            cfg = config.load(path)
        maintenance = loaded["projects"]["p"]["maintenance"]
        self.assertFalse(maintenance["enabled"])
        self.assertEqual(maintenance["passes"], ["health", "guidance"])
        loaded_policy = config.maintenance_policy(cfg, "p")
        self.assertFalse(loaded_policy["enabled"])
        self.assertEqual(loaded_policy["passes"], ["health", "guidance"])
        self.assertEqual(loaded_policy["cadence_days"], 30)


class MaintenanceCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.led = Ledger(":memory:", clock=self.clock)

    def test_threshold_due(self):
        self.led.set_maintenance_checkpoint("p", "security", merged_since=20)
        self.assertTrue(self.led.maintenance_due("p", "security"))

    def test_cadence_due(self):
        self.led.reset_maintenance("p", "security")
        self.clock.advance(days=30)
        self.assertTrue(self.led.maintenance_due("p", "security"))

    def test_not_due(self):
        self.led.reset_maintenance("p", "security")
        self.clock.advance(days=29, hours=23, minutes=59)
        self.led.set_maintenance_checkpoint("p", "security", merged_since=19)
        self.assertFalse(self.led.maintenance_due("p", "security"))

    def test_first_run_is_due(self):
        self.assertTrue(self.led.maintenance_due("p", "security"))

    def test_reset_then_not_due(self):
        self.led.set_maintenance_checkpoint(
            "p", "security", last_filed_at="2020-01-01T00:00:00+00:00",
            merged_since=99)
        self.led.reset_maintenance("p", "security")
        self.assertFalse(self.led.maintenance_due("p", "security"))

    def test_project_and_pass_counters_do_not_leak(self):
        self.led.reset_maintenance("p", "security")
        self.led.record_shipped("p", ["security"])
        self.assertEqual(
            self.led.maintenance_checkpoint("p", "security")["merged_since"], 1)
        self.assertEqual(
            self.led.maintenance_checkpoint("p", "health")["merged_since"], 0)
        self.assertEqual(
            self.led.maintenance_checkpoint("q", "security")["merged_since"], 0)
        self.led.reset_maintenance("p", "health")
        self.led.reset_maintenance("q", "security")
        self.assertTrue(self.led.maintenance_due("p", "security", merged_threshold=1))
        self.assertFalse(self.led.maintenance_due("p", "health", merged_threshold=1))
        self.assertFalse(self.led.maintenance_due("q", "security", merged_threshold=1))

    def test_shipped_event_increments_only_selected_passes(self):
        self.led.event("shipped", "p", 1, {"pr": 2}, passes=["security", "tests"])
        self.assertEqual(
            self.led.maintenance_checkpoint("p", "security")["merged_since"], 1)
        self.assertEqual(
            self.led.maintenance_checkpoint("p", "tests")["merged_since"], 1)
        self.assertEqual(
            self.led.maintenance_checkpoint("p", "health")["merged_since"], 0)


if __name__ == "__main__":
    unittest.main()
