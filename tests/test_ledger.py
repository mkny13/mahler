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
from mahler.ledger import Ledger, RoutedLedger, SCHEMA, remote_lease_operation


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

        orphans = self.led.orphan_lease_rows()
        self.assertEqual(sorted(o["number"] for o in orphans), [3, 4])

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


if __name__ == "__main__":
    unittest.main()

if __name__ == "__main__":
    unittest.main()
