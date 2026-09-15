"""Lease semantics (DESIGN D6) — the part of the kernel that must be right."""

import copy
import json
import os
import sqlite3
import subprocess
import tempfile
import tomllib
import unittest
from datetime import datetime, timedelta, timezone

from mahler import config
from mahler.ledger import Ledger, RoutedLedger, SCHEMA, iso, remote_lease_operation


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
        self.addCleanup(self.led.close)

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
        self.assertEqual(info["held_by"]["holder"], "interactive:you")
        self.assertEqual(info["held_by"]["kind"], "interactive")
        # Verify the interactive lease is still valid
        current = self.led.lease("p", 1)
        self.assertEqual(current["holder"], "interactive:you")
        self.assertEqual(current["kind"], "interactive")

    def test_interactive_vs_interactive_needs_steal(self):
        self.led.claim("p", 1, "interactive:mac", "interactive", 30)
        refused, _ = self.led.claim("p", 1, "interactive:phone", "interactive", 30)
        self.assertIsNone(refused)
        stolen, info = self.led.claim("p", 1, "interactive:phone", "interactive", 30, steal=True)
        self.assertIsNotNone(stolen)
        self.assertEqual(info["stolen_from"]["holder"], "interactive:mac")

    def test_primary_lease_preempts_auto(self):
        # D6 names `primary` as a holder kind alongside interactive and auto;
        # it is human and wins the item the same way (mahler#97).
        run_id = self.led.create_run(project="p", number=1, role="build",
                                     platform="agy-claude", epoch=0)
        auto, _ = self.led.claim("p", 1, f"run:{run_id}", "auto", 10, run_id=run_id)
        primary, info = self.led.claim("p", 1, "primary:checkout", "primary", 30)
        self.assertIsNotNone(primary)
        self.assertEqual(info["preempted"]["holder"], f"run:{run_id}")
        self.assertIsNotNone(self.led.run(run_id)["yield_at"])
        # ... and primary vs interactive needs a steal, like any human lease
        self.led.claim("p", 2, "interactive:you", "interactive", 30)
        refused, _ = self.led.claim("p", 2, "primary:checkout", "primary", 30)
        self.assertIsNone(refused)

    def test_handoff_with_stale_epoch_is_refused(self):
        # handoff_from is itself a compare-and-set: a stale holder/epoch pair
        # must not be able to hand the lease across (mahler#97).
        lease, _ = self.led.claim("p", 1, "run:1", "auto", 10)
        stale, info = self.led.claim("p", 1, "conductor", "auto", 10,
                                     handoff_from=("run:1", lease["epoch"] + 5))
        self.assertIsNone(stale)
        self.assertEqual(info["held_by"]["holder"], "run:1")
        wrong_holder, _ = self.led.claim("p", 1, "conductor", "auto", 10,
                                         handoff_from=("run:other", lease["epoch"]))
        self.assertIsNone(wrong_holder)

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
        original_expires = lease["expires_at"]
        for _ in range(5):
            self.clock.advance(minutes=8)
            self.assertTrue(self.led.heartbeat("p", 1, "run:1", lease["epoch"], 10))
            # Verify expires_at is actually extended
            current = self.led.lease("p", 1, live_only=False)
            self.assertGreater(current["expires_at"], original_expires)
            original_expires = current["expires_at"]
        self.assertTrue(self.led.lease_check("p", 1, lease["epoch"]))

    def test_heartbeat_fails_when_lease_taken(self):
        lease, _ = self.led.claim("p", 1, "run:1", "auto", 10)
        self.clock.advance(minutes=11)  # lease expires
        self.led.claim("p", 1, "run:2", "auto", 10)  # new holder takes it
        # Heartbeat with old epoch should fail
        self.assertFalse(self.led.heartbeat("p", 1, "run:1", lease["epoch"], 10))

    def test_heartbeat_fails_on_wrong_holder(self):
        lease, _ = self.led.claim("p", 1, "run:1", "auto", 10)
        self.assertFalse(self.led.heartbeat("p", 1, "run:other", lease["epoch"], 10))

    def test_heartbeat_fails_on_wrong_epoch(self):
        lease, _ = self.led.claim("p", 1, "run:1", "auto", 10)
        self.assertFalse(self.led.heartbeat("p", 1, "run:1", lease["epoch"] + 1, 10))

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
        # Verify epoch was incremented (fencing)
        self.assertGreater(conductor["epoch"], run["epoch"])
        # Verify old epoch is fenced
        self.assertFalse(self.led.lease_check("p", 1, run["epoch"]))
        self.assertTrue(self.led.lease_check("p", 1, conductor["epoch"]))

    def test_handoff_fails_on_wrong_epoch(self):
        run, _ = self.led.claim("p", 1, "run:1", "auto", 10,
                                max_parallel=1)
        # Try handoff with wrong epoch
        conductor, info = self.led.claim(
            "p", 1, "conductor", "auto", 10, max_parallel=1,
            handoff_from=("run:1", run["epoch"] + 1))
        self.assertIsNone(conductor)
        self.assertIn("held_by", info)

    def test_next_id_is_monotonic_and_respects_floor(self):
        self.assertEqual(self.led.next_id("p", "D", floor=227), 228)
        self.assertEqual(self.led.next_id("p", "D"), 229)
        self.assertEqual(self.led.next_id("q", "D"), 1)


class ConnectionTests(unittest.TestCase):
    """The status page (mahler serve) opens the ledger with thread_safe=True
    so a server thread can query the connection made on the main thread
    (mahler.serve serialises access with a lock)."""

    def test_thread_safe_ledger_usable_from_another_thread(self):
        """thread_safe=True allows a connection to be used from another thread
        when access is serialized externally (e.g. by a lock in mahler.serve)."""
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

    def test_thread_safe_ledger_serialized_access(self):
        """Multiple threads can use the ledger when access is serialized with a lock."""
        import threading
        led = Ledger(":memory:", thread_safe=True)
        lock = threading.Lock()
        errors = []

        def writer(thread_id):
            try:
                for i in range(50):
                    with lock:
                        led.upsert_item("p", thread_id * 100 + i, title=f"item-{thread_id}-{i}")
            except Exception as e:
                errors.append(f"Thread {thread_id}: {e}")

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertFalse(any(t.is_alive() for t in threads), "Some threads did not complete")
        self.assertEqual(errors, [], f"Serialized access errors: {errors}")
        self.assertEqual(len(led.items("p")), 250)

    def test_default_ledger_stays_main_thread_only(self):
        # without the flag sqlite3 still guards the connection: the guard is
        # the default, thread_safe must be a deliberate opt-in
        import sqlite3
        import threading
        led = Ledger(":memory:")
        outcome = []

        def work():
            try:
                led.upsert_item("p", 1, title="x")  # write operation triggers thread check
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


class CloseTests(unittest.TestCase):
    """issue #68: Ledger owns a sqlite3 connection; close() is the explicit
    shutdown hook, and a discarded Ledger must not leak the handle."""

    def test_close_is_idempotent_and_fences_further_use(self):
        import sqlite3
        led = Ledger(":memory:")
        self.addCleanup(led.close)
        led.upsert_item("p", 1, title="x")
        led.close()
        led.close()                     # second close is a no-op
        with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
            led.q("SELECT 1")

    def test_routed_ledger_closes_its_local_ledger(self):
        import sqlite3
        local = Ledger(":memory:")
        routed = RoutedLedger(local, copy.deepcopy(config.DEFAULTS))
        routed.close()
        with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
            local.q("SELECT 1")


class RemoteLedgerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.canonical = Ledger(":memory:", clock=self.clock)
        self.local = Ledger(":memory:", clock=self.clock)
        self.addCleanup(self.canonical.close)
        self.addCleanup(self.local.close)
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

    def test_remote_command_accepts_safe_argv_for_an_explicit_python(self):
        self.cfg["projects"]["mahler"]["remote_ledger"]["command"] = [
            "/opt/homebrew/bin/python3", "~/.mahler/app/bin/mahler"]
        routed = RoutedLedger(self.local, self.cfg, run=self.routed._run)
        lease, _ = routed.claim("mahler", 151, "run:7", "auto", 10)
        self.assertIsNotNone(lease)
        argv = self.requests[-1][0]
        self.assertEqual(argv[-3:], [
            "/opt/homebrew/bin/python3", "~/.mahler/app/bin/mahler",
            "ledger-remote-op"])

    def test_remote_command_argv_rejects_shell_syntax_and_options(self):
        for command in (["python3", "-c"], ["python3", "x;touch-bad"]):
            with self.subTest(command=command):
                self.cfg["projects"]["mahler"]["remote_ledger"]["command"] = command
                routed = RoutedLedger(self.local, self.cfg, run=self.routed._run)
                lease, info = routed.claim("mahler", 151, "run:7", "auto", 10)
                self.assertIsNone(lease)
                self.assertIn("invalid remote_ledger", info["unavailable"])
        # Also test invalid host
        self.cfg["projects"]["mahler"]["remote_ledger"]["command"] = [
            "/opt/homebrew/bin/python3", "~/.mahler/app/bin/mahler"]
        self.cfg["projects"]["mahler"]["remote_ledger"]["host"] = "invalid host with spaces"
        routed = RoutedLedger(self.local, self.cfg, run=self.routed._run)
        lease, info = routed.claim("mahler", 151, "run:7", "auto", 10)
        self.assertIsNone(lease)
        self.assertIn("invalid remote_ledger", info["unavailable"])
        # Test valid host and command still works
        self.cfg["projects"]["mahler"]["remote_ledger"]["host"] = "mike@mini.example.ts.net"
        routed = RoutedLedger(self.local, self.cfg, run=self.routed._run)
        lease, _ = routed.claim("mahler", 151, "run:7", "auto", 10)
        self.assertIsNotNone(lease)

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
        # Verify heartbeat fails with wrong epoch
        lease2, _ = self.routed.claim("mahler", 2, "run:2", "auto", 10)
        self.assertFalse(self.routed.heartbeat("mahler", 2, "run:2", lease2["epoch"] + 1, 10))
        # Verify release fails with wrong epoch
        self.assertFalse(self.routed.release("mahler", 2, holder="run:2", epoch=lease2["epoch"] + 1))
        # Verify lease_check fails with wrong epoch
        self.assertFalse(self.routed.lease_check("mahler", 2, lease2["epoch"] + 1))

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
        # Verify local project still works
        local_lease, _ = routed.claim("work", 1, "run:1", "auto", 10)
        self.assertIsNotNone(local_lease)

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


class ClearUsageTests(unittest.TestCase):
    def test_clears_only_named_windows_of_one_platform(self):
        led = Ledger(":memory:")
        led.record_usage("kilo", "5h", 100.0, "2026-09-12T13:00:00+00:00")
        led.record_usage("kilo", "hold", 100.0, "2026-09-12T13:00:00+00:00")
        led.record_usage("kilo", "weekly", 40.0)
        led.record_usage("cline-free", "5h", 100.0, "2026-09-12T13:00:00+00:00")
        self.assertEqual(led.clear_usage("kilo", ["5h", "hold"]), 2)
        self.assertEqual(set(led.usage("kilo")), {"weekly"})
        self.assertEqual(set(led.usage("cline-free")), {"5h"})

    def test_no_windows_is_a_no_op(self):
        led = Ledger(":memory:")
        led.record_usage("kilo", "5h", 100.0)
        self.assertEqual(led.clear_usage("kilo", []), 0)
        self.assertEqual(set(led.usage("kilo")), {"5h"})


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


class SchemaDriftTests(unittest.TestCase):
    """SCHEMA must describe the real runtime schema (mahler#97): the startup
    ALTER migrations exist for databases opened by older versions, not as a
    way to finish CREATE TABLE on fresh ones."""

    MIGRATED_ITEM_COLS = ("pr", "summary", "setup_fails", "parent",
                          "esc_tier", "esc_fails")

    def _cols(self, con, table):
        return {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}

    def test_fresh_schema_declares_every_migrated_column(self):
        led = Ledger(":memory:")
        self.addCleanup(led.close)
        items = self._cols(led.con, "items")
        for col in self.MIGRATED_ITEM_COLS:
            self.assertIn(col, items)
            self.assertIn(col, SCHEMA)          # declared, not ALTERed in
        self.assertIn("summary", SCHEMA)        # the column ship.py reads

    def test_legacy_items_table_without_summary_is_migrated(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.db")
            con = sqlite3.connect(path)
            con.executescript("\n".join(l for l in SCHEMA.splitlines()
                                        if "summary" not in l))
            con.execute("INSERT INTO items (project, number, state) VALUES ('p', 1, 'ready')")
            con.commit()
            con.close()
            led = Ledger(path)
            self.addCleanup(led.close)
            self.assertIn("summary", self._cols(led.con, "items"))
            self.assertIsNone(led.item("p", 1)["summary"])


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
        self.addCleanup(self.led.close)

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


class InvariantAndOrphanTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.led = Ledger(":memory:", clock=self.clock)
        self.addCleanup(self.led.close)

    def test_release_working_item_sets_ready_atomically(self):
        self.led.upsert_item("p", 1, state="ready")
        self.led.claim("p", 1, "run:1", "auto", 10)
        self.led.set_state("p", 1, "working", "run 1 started")
        self.assertEqual(self.led.item("p", 1)["state"], "working")

        ok = self.led.release("p", 1, holder="run:1")
        self.assertTrue(ok)
        self.assertIsNone(self.led.lease("p", 1))
        self.assertEqual(self.led.item("p", 1)["state"], "ready")
        ev = self.led.q("SELECT * FROM events WHERE kind='state'")[-1]
        self.assertIn("working -> ready", ev["detail"])

    def test_release_non_working_item_preserves_state(self):
        for state in ("verifying", "done", "needs_you", "failed", "parked"):
            self.led.upsert_item("p", 2, state="ready")
            self.led.claim("p", 2, "run:2", "auto", 10)
            self.led.set_state("p", 2, state, "reason")
            self.assertEqual(self.led.item("p", 2)["state"], state)

            self.led.release("p", 2, holder="run:2")
            self.assertEqual(self.led.item("p", 2)["state"], state)

    def test_release_explicit_to_state_none_preserves_working(self):
        self.led.upsert_item("p", 1, state="working")
        self.led.claim("p", 1, "run:1", "auto", 10)
        self.led.release("p", 1, holder="run:1", to_state=None)
        self.assertEqual(self.led.item("p", 1)["state"], "working")
        # Verify lease is still released
        self.assertIsNone(self.led.lease("p", 1))
        # Verify no state transition event was logged
        events = self.led.q("SELECT * FROM events WHERE kind='state' AND project='p' AND number=1")
        self.assertEqual(len(events), 0)

    def test_orphan_working_items_query(self):
        # 1. working with live lease -> not orphan
        self.led.upsert_item("p", 1, state="working")
        self.led.claim("p", 1, "run:1", "auto", 10)

        # 2. working with active run -> not orphan
        self.led.upsert_item("p", 2, state="working")
        self.led.create_run(project="p", number=2, role="build", platform="claude", epoch=1, status="running")

        # 3. working with no lease and no run -> orphan!
        self.led.upsert_item("p", 3, state="working")

        # 4. ready with no lease -> not orphan
        self.led.upsert_item("p", 4, state="ready")

        orphans = self.led.orphan_working_items()
        self.assertEqual([o["number"] for o in orphans], [3])
        self.assertEqual(self.led.orphan_working_items("other"), [])

    def test_orphan_lease_rows_query(self):
        # 1. lease on working item -> not orphan lease
        self.led.upsert_item("p", 1, state="working")
        self.led.claim("p", 1, "run:1", "auto", 10)

        # 2. lease on verifying item -> not orphan lease
        self.led.upsert_item("p", 2, state="verifying")
        self.led.claim("p", 2, "conductor", "auto", 10)

        # 3. lease on ready item with no run -> orphan lease!
        self.led.upsert_item("p", 3, state="ready")
        self.led.claim("p", 3, "stale:holder", "auto", 10)

        # 4. lease on done item with no run -> orphan lease!
        self.led.upsert_item("p", 4, state="done")
        self.led.claim("p", 4, "stale:holder2", "auto", 10)

        # 5. lease on ready item WITH an active run -> not orphan lease (watchdog owns)
        self.led.upsert_item("p", 5, state="ready")
        self.led.claim("p", 5, "run:5", "auto", 10)
        self.led.create_run(project="p", number=5, role="build", platform="claude", epoch=1, status="running")

        # 6. lease on ready item WITH a stopping run -> not orphan lease
        self.led.upsert_item("p", 6, state="ready")
        self.led.claim("p", 6, "run:6", "auto", 10)
        self.led.create_run(project="p", number=6, role="build", platform="claude", epoch=1, status="stopping")

        orphans = self.led.orphan_lease_rows()
        self.assertEqual(sorted(o["number"] for o in orphans), [3, 4])

    def test_orphan_lease_rows_excludes_stopping_run(self):
        # Verify that stopping runs also prevent orphan classification
        self.led.upsert_item("p", 10, state="ready")
        self.led.claim("p", 10, "run:10", "auto", 10)
        self.led.create_run(project="p", number=10, role="build", platform="claude", epoch=1, status="stopping")
        orphans = self.led.orphan_lease_rows()
        self.assertNotIn(10, [o["number"] for o in orphans])

class IndexTests(unittest.TestCase):
    """Query indexes (issue #89): created on fresh DBs and applied to existing
    ones at startup, without touching table structure."""

    EXPECTED = {
        "idx_items_project_state", "idx_items_state",
        "idx_runs_status_project", "idx_runs_item_status", "idx_runs_ended",
        "idx_events_kind_at", "idx_events_item",
    }

    def _indexes(self, con):
        return {r["name"] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")}

    def test_fresh_db_has_query_indexes(self):
        led = Ledger(":memory:")
        self.addCleanup(led.close)
        self.assertTrue(self.EXPECTED <= self._indexes(led.con))

    def test_existing_db_gets_indexes_at_startup(self):
        # Simulate a legacy database: same tables, no query indexes.
        legacy_lines, skipping = [], False
        for line in SCHEMA.splitlines():
            if line.lstrip().upper().startswith("CREATE INDEX"):
                skipping = ";" not in line
                continue
            if skipping:
                skipping = ";" not in line
                continue
            legacy_lines.append(line)
        legacy = "\n".join(legacy_lines)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mahler.db")
            con = sqlite3.connect(path)
            con.row_factory = sqlite3.Row
            con.executescript(legacy)
            con.execute("INSERT INTO items (project, number, state) VALUES ('p', 1, 'ready')")
            con.commit()
            con.close()

            led = Ledger(path)  # the startup schema update must add the indexes
            self.addCleanup(led.close)
            self.assertTrue(self.EXPECTED <= self._indexes(led.con))
            # ... and the pre-existing data survives untouched
            self.assertEqual(led.item("p", 1)["state"], "ready")

    def test_reopening_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mahler.db")
            led = Ledger(path)
            led.close()
            led = Ledger(path)
            self.addCleanup(led.close)
            self.assertEqual(self.EXPECTED, self.EXPECTED & self._indexes(led.con))

    def test_planner_uses_index_for_active_runs(self):
        led = Ledger(":memory:")
        self.addCleanup(led.close)
        led.create_run(project="p", number=1, role="build", platform="claude", epoch=1)
        plan = " ".join(r[3] for r in led.q(
            "EXPLAIN QUERY PLAN SELECT * FROM runs WHERE status IN ('running','stopping')"))
        # no full table scan: one of the runs-status indexes is chosen
        self.assertRegex(plan, r"USING INDEX idx_runs_\w+")


class PlatformOutcomeTests(unittest.TestCase):
    """mahler#206: the ledger-side data the platform-tier audit reads."""

    def setUp(self):
        self.clock = Clock()
        self.led = Ledger(":memory:", clock=self.clock)
        self.addCleanup(self.led.close)

    def _run(self, platform, outcome, role="build", started_ago_days=0):
        started = self.clock() - timedelta(days=started_ago_days)
        run_id = self.led.create_run(project="p", number=1, role=role, platform=platform,
                                     epoch=1, status="ended", outcome=outcome,
                                     started_at=iso(started))
        return run_id

    def test_platform_outcomes_counts_done_and_needs_you(self):
        self._run("kilo", "DONE")
        self._run("kilo", "DONE")
        self._run("kilo", "NEEDS-YOU")
        self._run("kilo", "exit 1")
        stats = self.led.platform_outcomes()
        self.assertEqual(stats["kilo"], {"runs": 4, "done": 2, "needs_you": 1})

    def test_platform_outcomes_ignores_sort_role(self):
        self._run("claude", "READY", role="sort")
        stats = self.led.platform_outcomes()
        self.assertNotIn("claude", stats)

    def test_platform_outcomes_respects_since(self):
        self._run("kilo", "DONE", started_ago_days=200)
        self._run("kilo", "DONE", started_ago_days=1)
        recent = self.led.platform_outcomes(since=self.clock() - timedelta(days=30))
        self.assertEqual(recent["kilo"]["runs"], 1)
        all_time = self.led.platform_outcomes()
        self.assertEqual(all_time["kilo"]["runs"], 2)

    def test_platform_escalations_reads_structured_detail(self):
        self.led.event("escalated", "p", 1,
                       {"tier_from": 0, "tier_to": 2, "platform": "kilo", "reason": "2 failures"})
        self.led.event("escalated", "p", 2,
                       {"tier_from": 2, "tier_to": 3, "platform": "kilo", "reason": "red CI"})
        self.led.event("escalated", "p", 3,
                       {"tier_from": 2, "tier_to": 3, "platform": "agy-claude", "reason": "red CI"})
        counts = self.led.platform_escalations()
        self.assertEqual(counts, {"kilo": 2, "agy-claude": 1})

    def test_platform_escalations_skips_legacy_string_detail(self):
        self.led.event("escalated", "p", 1, "tier 0 -> 2 (red CI)")
        self.assertEqual(self.led.platform_escalations(), {})

    def test_platform_escalations_respects_since(self):
        self.clock.advance(days=0)
        self.led.event("escalated", "p", 1, {"platform": "kilo"})
        self.clock.advance(days=10)
        cutoff = self.clock()
        self.clock.advance(days=1)
        self.led.event("escalated", "p", 2, {"platform": "kilo"})
        self.assertEqual(self.led.platform_escalations(since=cutoff), {"kilo": 1})


if __name__ == "__main__":
    unittest.main()



class ConsoleQueueTests(unittest.TestCase):
    def test_queue_due_cancel_finish(self):
        clock = Clock()
        led = Ledger(':memory:', clock=clock)
        self.addCleanup(led.close)
        first = led.queue_action('answer', 'p', 1, {'text': 'yes'}, 60)
        second = led.queue_action('other')
        self.assertEqual([r['id'] for r in led.due_actions()], [second])
        self.assertEqual(json.loads(led.pending_actions('answer')[0]['payload']), {'text': 'yes'})
        self.assertTrue(led.cancel_action(second))
        self.assertFalse(led.cancel_action(second))
        clock.advance(seconds=60)
        self.assertEqual([r['id'] for r in led.due_actions(limit=1)], [first])
        led.finish_action(first, 'done', 'posted')
        self.assertFalse(led.cancel_action(first))
        self.assertEqual(led.pending_actions(), [])
        row = led.q1('SELECT * FROM console_actions WHERE id=?', (first,))
        self.assertEqual((row['status'], row['result'], row['done_at']), ('done', 'posted', iso(clock())))
        with self.assertRaises(ValueError):
            led.finish_action(first, 'pending')
