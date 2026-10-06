"""Launch availability must not consume an item's implementation budget."""

import json
import unittest
from datetime import timedelta
from unittest import mock

from mahler import scheduler, tick
from mahler.ledger import Ledger, iso
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
        with mock.patch("mahler.tick.runner.prepare",
                        side_effect=OSError("temporarily unavailable")) as prepare, \
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


from datetime import datetime, timezone  # noqa: E402
from mahler import capacity, platforms  # noqa: E402
from tests.test_schedule import seed  # noqa: E402


class CapacityObservationTests(unittest.TestCase):
    """The tick records what waits and why, without changing what it launches."""

    def setUp(self):
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)

    def ctx(self, projects=None, total=3, dry_run=False):
        cfg = mk_cfg(projects or {"a": proj(max_parallel=3)}, total=total)
        ctx = scheduler.Ctx(cfg, self.led, dry_run=dry_run)
        ctx.ping = mock.Mock()
        return ctx

    def schedule(self, ctx, start=False):
        projects = [p for p in scheduler.config.enabled_projects(ctx.cfg)]
        with mock.patch.object(platforms, "available", return_value=True), \
             mock.patch("mahler.tick.start", return_value=start) as started:
            tick.schedule(ctx, projects)
        ctx.capacity.flush()
        return started

    def rows(self):
        return {(r["project"], r["number"], r["role"]): r
                for r in map(dict, self.led.capacity_intervals())}

    def test_sort_plan_and_build_waits_are_recorded_with_platform_reasons(self):
        ctx = self.ctx()
        item(self.led, "a", 1, state="inbox")
        item(self.led, "a", 2, state="inbox")
        self.led.upsert_item("a", 2, labels=json.dumps(["type:goal"]))
        item(self.led, "a", 3)
        self.led.upsert_item("a", 3, labels=json.dumps(["size:m"]))
        for name in ("claude", "claude-opus", "agy-gemini", "agy-claude"):
            self.led.record_usage(name, "5h", 100, iso(NOW + timedelta(hours=3)))
            self.led.record_usage(name, "weekly", 100, iso(NOW + timedelta(hours=3)))
        self.schedule(ctx)
        rows = self.rows()
        self.assertEqual(rows[("a", 2, "sort")]["routing_role"], "plan")
        self.assertEqual(rows[("a", 1, "sort")]["routing_role"], "sort")
        build = rows[("a", 3, "build")]
        self.assertEqual((build["size"], build["effective_size"]), ("m", "m"))
        self.assertEqual(json.loads(build["blockers"]), ["no_platform"])
        plats = json.loads(build["platforms"])
        self.assertEqual(plats["agy-claude"]["reasons"], ["quota"])
        self.assertEqual(plats["copilot"]["reasons"], ["size", "unknown"])     # stale, not quota
        self.assertEqual(plats["jetstream"]["reasons"], ["disabled", "route", "size"])
        self.assertEqual(plats["agy-claude"]["cost"], "free")

    def test_launch_is_recorded_only_after_start_succeeds(self):
        ctx = self.ctx()
        item(self.led, "a", 3)
        seed(self.led, **{"agy-claude": (10, 10)})
        self.schedule(ctx, start=False)
        row = self.rows()[("a", 3, "build")]
        self.assertEqual(json.loads(row["blockers"]), ["start_failed"])
        self.assertEqual(json.loads(row["platforms"])["agy-claude"]["reasons"], ["unknown"])
        self.assertIsNone(row["launch_run_id"])

    def test_real_start_marks_the_interval_launched(self):
        ctx = self.ctx()
        item(self.led, "a", 3)
        seed(self.led, **{"agy-claude": (10, 10)})
        self.schedule(ctx)            # one waiting tick first
        ctx.capacity.obs.clear()
        it = self.led.item("a", 3)
        gh = mock.Mock()
        with mock.patch.object(ctx, "gh", return_value=gh), \
             mock.patch("mahler.tick.runner.prepare", return_value={"branch": "b"}), \
             mock.patch("mahler.tick.prompt.build", return_value="p"), \
             mock.patch("mahler.tick.runner.launch", return_value={"branch": "b"}):
            self.assertTrue(tick.start(ctx, "a", it, "build", "agy-claude", size="m"))
        ctx.capacity.flush()
        row = self.rows()[("a", 3, "build")]
        self.assertEqual((row["open"], row["end_reason"], row["launched_platform"]),
                         (0, "launched", "agy-claude"))
        self.assertIsNotNone(row["launch_run_id"])

    def test_paused_tick_stays_observable(self):
        ctx = self.ctx()
        item(self.led, "a", 3)
        item(self.led, "a", 1, state="inbox")
        capacity.of(ctx).observe_paused([{"name": "a"}])
        ctx.capacity.flush()
        rows = self.rows()
        self.assertEqual(json.loads(rows[("a", 3, "build")]["blockers"]), ["paused"])
        self.assertEqual(json.loads(rows[("a", 1, "sort")]["blockers"]), ["paused"])

    def test_full_global_slots_still_records_blocked_candidates(self):
        ctx = self.ctx(total=1)
        seed(self.led, **{"agy-claude": (10, 10)})
        item(self.led, "a", 3)
        self.led.create_run(project="a", number=9, role="build", platform="agy-gemini",
                            epoch=0, status="running")
        self.schedule(ctx)
        row = self.rows()[("a", 3, "build")]
        self.assertEqual(json.loads(row["blockers"]), ["global_slots"])
        self.assertEqual(json.loads(row["platforms"])["agy-claude"]["reasons"], ["eligible"])

    def test_item_blockers_are_recorded(self):
        ctx = self.ctx({"a": proj(max_parallel=1), "b": proj(max_parallel=2, settle_minutes=30)})
        seed(self.led, **{"agy-claude": (10, 10)})
        item(self.led, "a", 1)
        self.led.create_run(project="a", number=9, role="build", platform="agy-gemini",
                            epoch=0, status="running")
        item(self.led, "b", 2)
        self.led.upsert_item("b", 2, sorted_at=iso(NOW))
        item(self.led, "b", 3)
        self.led.upsert_item("b", 3, depends=json.dumps([{"repo": "x/y", "number": 99}]))
        self.schedule(ctx)
        rows = self.rows()
        self.assertEqual(json.loads(rows[("a", 1, "build")]["blockers"]), ["project_slots"])
        self.assertEqual(json.loads(rows[("b", 2, "build")]["blockers"]), ["settling"])
        self.assertEqual(json.loads(rows[("b", 3, "build")]["blockers"]), ["deps"])

    def test_launch_breaker_is_an_item_blocker(self):
        ctx = self.ctx()
        item(self.led, "a", 3)
        with mock.patch("mahler.tick.launch_health.allowed", return_value=False):
            self.schedule(ctx)
        self.assertEqual(json.loads(self.rows()[("a", 3, "build")]["blockers"]),
                         ["launch_breaker"])

    def test_dry_run_writes_nothing(self):
        ctx = self.ctx(dry_run=True)
        item(self.led, "a", 3)
        self.schedule(ctx)
        self.assertEqual(self.rows(), {})

    def test_recording_failure_never_breaks_the_tick(self):
        ctx = self.ctx()
        item(self.led, "a", 3)
        with mock.patch.object(Ledger, "capacity_record", side_effect=OSError("disk full")):
            scheduler.capacity.of(ctx).observe("a", 3, "build", blockers=["x"])
            ctx.capacity.flush()
        self.assertTrue(any("couldn't record capacity history" in l for l in ctx.lines))

    def test_diagnosis_failure_never_changes_the_choice(self):
        ctx = self.ctx()
        item(self.led, "a", 3)
        seed(self.led, **{"agy-claude": (10, 10)})
        with mock.patch.object(tick.router, "diagnose", side_effect=RuntimeError("boom")):
            started = self.schedule(ctx, start=True)
        self.assertEqual(started.call_args[0][4], "agy-claude")

    def test_full_tick_flushes_even_when_the_ship_pass_raises(self):
        ctx = self.ctx()
        item(self.led, "a", 3)
        with mock.patch.object(scheduler, "ship", side_effect=RuntimeError("x")), \
             self.assertRaises(RuntimeError):
            scheduler.tick(ctx)
