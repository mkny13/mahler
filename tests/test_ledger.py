"""Lease semantics (DESIGN D6) — the part of the kernel that must be right."""

import copy
import json
import os
import subprocess
import tempfile
import tomllib
import unittest
from datetime import datetime, timedelta, timezone

from mahler import config
from mahler.ledger import Ledger, RoutedLedger, remote_lease_operation


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

    def test_capacity_is_checked_atomically_across_items(self):
        first, _ = self.led.claim("p", 1, "run:1", "auto", 10,
                                  max_parallel=1, capacity=True)
        second, info = self.led.claim("p", 2, "run:2", "auto", 10,
                                      max_parallel=1, capacity=True)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(info["at_capacity"][0]["number"], 1)

    def test_non_capacity_sort_does_not_occupy_project_slot(self):
        self.led.claim("p", 1, "sort:1", "auto", 10,
                       max_parallel=None, capacity=False)
        build, _ = self.led.claim("p", 2, "run:2", "auto", 10,
                                  max_parallel=1, capacity=True)
        self.assertIsNotNone(build)

    def test_handoff_keeps_same_capacity_slot_without_release(self):
        run, _ = self.led.claim("p", 1, "run:1", "auto", 10,
                                max_parallel=1)
        conductor, info = self.led.claim(
            "p", 1, "conductor", "auto", 10, max_parallel=1,
            handoff_from=("run:1", run["epoch"]))
        other, blocked = self.led.claim("p", 2, "run:2", "auto", 10,
                                        max_parallel=1)
        self.assertEqual(info["handed_off_from"]["holder"], "run:1")
        self.assertEqual(conductor["holder"], "conductor")
        self.assertIsNone(other)
        self.assertIn("at_capacity", blocked)

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

    def test_capacity_column_is_added_to_legacy_databases(self):
        from mahler.ledger import SCHEMA
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.db")
            import sqlite3
            con = sqlite3.connect(path)
            con.executescript("\n".join(
                line for line in SCHEMA.splitlines()
                if "capacity     INTEGER" not in line))
            con.execute(
                "INSERT INTO leases (project, number, holder, kind, epoch, acquired_at, "
                "heartbeat_at, expires_at) VALUES ('p',1,'run:1','auto',1,'x','x','x')")
            con.commit()
            con.close()
            led = Ledger(path)
            self.assertEqual(led.lease("p", 1, live_only=False)["capacity"], 1)
            led.con.close()


class RemoteLedgerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.canonical = Ledger(":memory:", clock=self.clock)
        self.local = Ledger(":memory:", clock=self.clock)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["projects"] = {
            "mahler": {
                "enabled": True,
                "max_parallel": 1,
                "remote_ledger": {
                    "host": "mike@mini.example.ts.net",
                    "client_id": "work-laptop",
                    "connect_timeout_seconds": 3,
                },
            },
            "work": {"enabled": True, "max_parallel": 2},
        }
        self.canonical_cfg = copy.deepcopy(config.DEFAULTS)
        self.canonical_cfg["projects"] = {
            "mahler": {"enabled": True, "max_parallel": 1},
        }
        self.requests = []

        def transport(argv, **kwargs):
            request = json.loads(kwargs["input"])
            self.requests.append((argv, request, kwargs))
            try:
                result = remote_lease_operation(
                    request, self.canonical_cfg, self.canonical)
                body = {"version": 1, "ok": True, "result": result}
                return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")
            except Exception as exc:
                body = {"version": 1, "ok": False, "error": str(exc)}
                return subprocess.CompletedProcess(argv, 1, json.dumps(body), "")

        self.routed = RoutedLedger(self.local, self.cfg, run=transport)

    def test_remote_claim_uses_canonical_db_and_namespaced_holder(self):
        lease, _ = self.routed.claim(
            "mahler", 151, "run:7", "auto", 10,
            platform="copilot", run_id=7)
        self.assertIsNone(self.local.lease("mahler", 151))
        canonical = self.canonical.lease("mahler", 151)
        self.assertEqual(lease["holder"], "work-laptop/run:7")
        self.assertEqual(canonical["holder"], "work-laptop/run:7")
        self.assertIsNone(canonical["run_id"])
        argv, request, kwargs = self.requests[-1]
        self.assertNotIn("run:7", argv)
        self.assertEqual(request["holder"], "work-laptop/run:7")
        self.assertEqual(kwargs["timeout"], 8)

    def test_unconfigured_project_stays_fully_local(self):
        lease, _ = self.routed.claim("work", 2, "run:2", "auto", 10)
        self.assertIsNotNone(lease)
        self.assertIsNotNone(self.local.lease("work", 2))
        self.assertEqual(self.requests, [])

    def test_remote_capacity_blocks_a_different_issue(self):
        first, _ = self.routed.claim("mahler", 1, "run:1", "auto", 10)
        second, info = self.routed.claim("mahler", 2, "run:2", "auto", 10)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(info["at_capacity"][0]["number"], 1)

    def test_remote_handoff_preserves_slot_and_fencing(self):
        run, _ = self.routed.claim("mahler", 1, "run:1", "auto", 10)
        conductor, info = self.routed.claim(
            "mahler", 1, "conductor", "auto", 10,
            handoff_from=("run:1", run["epoch"]))
        self.assertEqual(info["handed_off_from"]["holder"], "work-laptop/run:1")
        self.assertEqual(conductor["holder"], "work-laptop/conductor")
        self.assertFalse(self.routed.lease_check("mahler", 1, run["epoch"]))
        self.assertTrue(self.routed.lease_check("mahler", 1, conductor["epoch"]))

    def test_remote_heartbeat_release_and_lease(self):
        lease, _ = self.routed.claim("mahler", 1, "run:1", "auto", 10)
        self.clock.advance(minutes=5)
        self.assertTrue(self.routed.heartbeat(
            "mahler", 1, "run:1", lease["epoch"], 10))
        self.assertEqual(self.routed.lease("mahler", 1)["epoch"], lease["epoch"])
        self.assertTrue(self.routed.release(
            "mahler", 1, holder="run:1", epoch=lease["epoch"]))
        self.assertIsNone(self.canonical.lease("mahler", 1))

    def test_status_rows_include_remote_working_lease(self):
        self.routed.claim("mahler", 1, "interactive:chat", "interactive", 30)
        self.local.upsert_item("mahler", 1, state="working")
        rows = self.routed.lease_rows("mahler")
        self.assertEqual([row["holder"] for row in rows],
                         ["work-laptop/interactive:chat"])

    def test_transport_failure_is_closed_for_every_operation(self):
        def failed(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 255, "", "host unreachable")

        routed = RoutedLedger(self.local, self.cfg, run=failed)
        lease, info = routed.claim("mahler", 1, "run:1", "auto", 10)
        self.assertIsNone(lease)
        self.assertIn("unavailable", info)
        self.assertIsNone(routed.lease("mahler", 1))
        self.assertFalse(routed.heartbeat("mahler", 1, "run:1", 1, 10))
        self.assertFalse(routed.release("mahler", 1, holder="run:1", epoch=1))
        self.assertFalse(routed.lease_check("mahler", 1, 1))
        self.assertIn("host unreachable", routed.remote_error("mahler"))

    def test_malformed_success_response_is_also_closed(self):
        def malformed(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, "not json", "")

        routed = RoutedLedger(self.local, self.cfg, run=malformed)
        lease, info = routed.claim("mahler", 1, "run:1", "auto", 10)
        self.assertIsNone(lease)
        self.assertIn("malformed JSON", info["unavailable"])

    def test_endpoint_rejects_unknown_operation_and_disabled_project(self):
        with self.assertRaisesRegex(ValueError, "unsupported"):
            remote_lease_operation(
                {"version": 1, "operation": "query", "project": "mahler", "number": 1},
                self.canonical_cfg, self.canonical)
        with self.assertRaisesRegex(ValueError, "not enabled"):
            remote_lease_operation(
                {"version": 1, "operation": "lease", "project": "work", "number": 1},
                self.canonical_cfg, self.canonical)




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
