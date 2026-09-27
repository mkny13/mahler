import copy
import unittest
from unittest.mock import patch
from contextlib import ExitStack

from mahler import config, router, scorecard, tick
from mahler.ledger import Ledger
from mahler.scheduler import Ctx


class MeasuredRoutingTests(unittest.TestCase):
    def setUp(self):
        self.led = Ledger(":memory:")
        self.addCleanup(self.led.close)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["claude_peak"] = {"enabled": False}
        self.cfg["platforms"] = {
            name: {"enabled": True, "metered": False, "kind": "codex",
                   "model": name, "effort": "medium", "tier": tier}
            for name, tier in (("bad", 1), ("unknown", 1), ("expensive", 2), ("cheap", 1))}
        self.cfg["routing"] = {"build": list(self.cfg["platforms"])}
        self.pol = {"routing_mode": "measured"}
        self.rows = [
            dict(role=role, size=size, platform=name, model=name, effort="medium",
                 status=status, cost_per_success=cost, n=10, successes=9)
            for role in ("build", "fix", "review", "sort", "plan")
            for size in ("s", "m")
            for name, status, cost in (("bad", "below", .01),
                                       ("expensive", "good", .5), ("cheap", "good", .05))]

    def pick(self, **kwargs):
        return router.pick_for_project(self.cfg, self.led, self.pol,
                                       kwargs.pop("role", "build"),
                                       scorecard_rows=self.rows, **kwargs)[0]

    def test_all_roles_and_fallback_order(self):
        for role in ("build", "fix", "review", "sort", "plan"):
            with self.subTest(role=role):
                self.assertEqual(self.pick(role=role), "cheap")
                self.assertEqual(self.pick(role=role, busy={"cheap"}), "expensive")
                self.assertEqual(self.pick(role=role, busy={"cheap", "expensive"}), "unknown")
                self.assertEqual(self.pick(role=role, busy={"cheap", "expensive", "unknown"}), "bad")

    def test_unproven_free_precedes_proven_cheap_paid(self):
        self.cfg["routing"]["build"] = ["cheap", "unknown"]
        self.cfg["platforms"]["cheap"]["kind"] = "claude"
        self.assertEqual(self.pick(), "unknown")

    def test_proven_free_variants_still_order_by_cost_per_success(self):
        self.cfg["routing"]["build"] = ["expensive", "cheap"]
        self.assertEqual(self.pick(), "cheap")

    def test_cost_class_defaults_and_explicit_override(self):
        personal_claude = {"kind": "claude"}
        work_claude = {"kind": "claude", "account": "work"}
        self.assertEqual(router.cost_class(personal_claude), "paid")
        self.assertEqual(router.cost_class(work_claude), "free")
        self.assertEqual(router.cost_class({"kind": "codex"}), "free")
        self.assertEqual(router.cost_class(dict(personal_claude, cost_class="free")), "free")
        self.assertEqual(router.cost_class(dict(work_claude, cost_class="paid")), "paid")

    def test_dry_run_routes_free_ahead_of_paid_at_every_size(self):
        self.cfg["platforms"] = {
            "paid": {"enabled": True, "metered": False, "kind": "claude",
                     "model": "paid", "effort": "medium"},
            "free-s": {"enabled": True, "metered": False, "kind": "cline",
                       "model": "free-s", "effort": "medium", "max_size": "s"},
            "free-m": {"enabled": True, "metered": False, "kind": "agy",
                       "model": "free-m", "effort": "medium", "min_size": "m",
                       "max_size": "m"},
            "free-l": {"enabled": True, "metered": False, "kind": "codex",
                       "model": "free-l", "effort": "medium", "min_size": "l"},
        }
        self.cfg["routing"] = {"build": ["paid", "free-s", "free-m", "free-l"]}
        self.cfg["concurrency"]["total"] = 1
        self.cfg["projects"] = {"p": {
            "enabled": True, "repo": "x/p", "path": "/tmp/p", "hot_hold": False,
            "max_parallel": 1, "routing_mode": "measured", "explore": False,
        }}
        rows = [dict(role="build", size=size, platform="paid", model="paid",
                     effort="medium", status="good", cost_per_success=.01,
                     n=10, successes=10)
                for size in ("s", "m", "l")]
        for number, (size, expected) in enumerate(
                (("s", "free-s"), ("m", "free-m"), ("l", "free-l")), 1):
            with self.subTest(size=size):
                led = Ledger(":memory:")
                self.addCleanup(led.close)
                led.upsert_item("p", number, state="ready", title="Build it",
                                labels=f'["size:{size}"]', sorted_at="2000-01-01T00:00:00Z")
                ctx = Ctx(self.cfg, led, dry_run=True)
                ctx._scorecard_rows = rows
                with patch("mahler.platforms.available", return_value=True):
                    tick.schedule(ctx, list(config.enabled_projects(self.cfg)))
                self.assertIn(f"p#{number}: would build on {expected}", ctx.lines)

    def test_list_mode_and_project_override(self):
        self.pol = {}
        self.assertEqual(self.pick(), "bad")
        self.cfg["routing_mode"] = "measured"
        self.assertEqual(self.pick(), "cheap")
        self.pol["routing_mode"] = "list"
        self.assertEqual(self.pick(), "bad")

    def test_filters_pin_and_effective_size(self):
        self.assertEqual(self.pick(min_tier=2), "expensive")
        self.assertEqual(self.pick(role="review", exclude={"cheap"}), "expensive")
        self.assertEqual(self.pick(pin="bad"), "bad")
        self.assertEqual(self.pick(role="review", pin="cheap", exclude={"cheap"}), None)
        self.assertEqual(self.pick(size="l"), "bad")
        self.cfg["platforms"]["cheap"]["model"] = "replacement"
        self.assertEqual(self.pick(), "expensive")

    def test_review_excludes_every_variant_on_the_builder_slot(self):
        builder = "cheap/model-a/low"
        sibling = "cheap/model-b/medium"
        base = self.cfg["platforms"]["cheap"]
        self.cfg["platforms"][builder] = dict(base, slot="cheap", model="model-a",
                                               effort="low")
        self.cfg["platforms"][sibling] = dict(base, slot="cheap", model="model-b",
                                               effort="medium")
        self.cfg["routing"]["build"] = [sibling, "expensive"]
        rows = [dict(r, platform=sibling, model="model-b", cost_per_success=.01)
                for r in self.rows if r["platform"] == "cheap"]
        rows += [r for r in self.rows if r["platform"] == "expensive"]

        platform, reasons = router.pick_for_project(
            self.cfg, self.led, self.pol, "review", exclude={builder},
            scorecard_rows=rows)

        self.assertEqual(platform, "expensive")
        self.assertIn(f"{sibling}: excluded (same platform as the builder)", reasons)

    def test_burst_is_stable_after_measurement(self):
        for name in ("cheap", "expensive"):
            self.cfg["platforms"][name]["kind"] = "claude"
        bursts = {"cheap": {"5h": (90, 97)}, "expensive": {"5h": (90, 97)}}
        self.assertEqual(self.pick(burst_lines=bursts), "cheap")
        self.assertEqual(self.pick(burst_lines={"expensive": {"5h": (90, 97)}}), "expensive")

    def test_burst_still_promotes_paid_claude_ahead_of_free_class(self):
        self.cfg["routing"]["build"] = ["cheap", "unknown"]
        self.cfg["platforms"]["cheap"]["kind"] = "claude"
        self.assertEqual(self.pick(), "unknown")
        self.assertEqual(self.pick(burst_lines={"cheap": {"5h": (90, 97)}}), "cheap")

    def test_direct_pick_promotes_burst_after_account_merge(self):
        self.cfg["routing_mode"] = "measured"
        self.cfg["routing"]["build"] = ["bad", "cheap"]
        self.cfg["accounts"] = {"work": {"routing": {"build": ["unknown", "expensive"]}}}
        for name in ("unknown", "expensive"):
            self.cfg["platforms"][name]["account"] = "work"
        self.cfg["platforms"]["cheap"]["kind"] = "claude"
        picked, reasons = router.pick(
            self.cfg, self.led, "build", accounts=["personal", "work"],
            busy={"cheap"}, burst_lines={"cheap": {"5h": (90, 97)}},
            scorecard_rows=[])
        self.assertEqual(picked, "bad")
        self.assertEqual(reasons, ["cheap: busy"])

    def test_context_reuses_empty_scorecard(self):
        ctx = Ctx(self.cfg, self.led)
        with patch.object(scorecard, "table", return_value=[]) as table:
            self.assertEqual(ctx.scorecard_rows, [])
            self.assertEqual(ctx.scorecard_rows, [])
            table.assert_called_once_with(self.led, self.cfg)

    def test_unproven_and_below_keep_list_order(self):
        rows = [dict(r, status="unproven") for r in self.rows]
        order = list(self.cfg["platforms"])
        self.assertEqual(router.measured_order(self.cfg, order, rows, "build", "m"), order)
        rows = [dict(r, status="below") for r in rows]
        self.assertEqual(router.measured_order(self.cfg, order, rows, "build", "m"),
                         ["unknown", "bad", "expensive", "cheap"])

    def test_tick_reuses_snapshot_across_schedule_ship_and_digest(self):
        from mahler import scheduler
        ctx = Ctx(self.cfg, self.led)
        ctx.dry_run = True
        snapshots = []
        def consume(c, *args):
            snapshots.append(c.scorecard_rows)
        with ExitStack() as stack:
            table = stack.enter_context(patch.object(scorecard, "table", return_value=[]))
            stack.enter_context(patch.object(scheduler, "_project_ok", return_value=True))
            for name in ("compute_burst", "watchdog", "expire", "close_finished_parents",
                         "refresh_usage", "queue_maintenance", "platform_audit.queue",
                         "janitor.maybe_run", "outbox.drain"):
                stack.enter_context(patch("mahler.scheduler." + name))
            for name in ("schedule", "ship", "digest.maybe_send"):
                stack.enter_context(patch("mahler.scheduler." + name, side_effect=consume))
            scheduler.tick(ctx)
            self.assertEqual(table.call_count, 1)
            self.assertEqual(len(snapshots), 3)
            scheduler.tick(ctx)
            self.assertEqual(table.call_count, 2)

    def test_console_explains_measured_routes(self):
        from mahler.console.state import measured_routes
        # Console routing checks installed CLIs; CI need not have Codex installed.
        with patch("mahler.platforms.available", return_value=True):
            result = measured_routes(self.cfg, self.led, [dict(self.pol, name="test")], self.rows)
        self.assertIn("cheap → expensive → unknown → bad", result[0])
        self.assertIn("9/10 first try, $0.05 per success", result[0])
        self.assertEqual(measured_routes(self.cfg, self.led, [{"name": "test"}], self.rows), [])

    def test_console_measured_routes_without_installed_clis(self):
        from mahler.console.state import measured_routes
        with patch("mahler.platforms.available", return_value=False):
            result = measured_routes(self.cfg, self.led, [dict(self.pol, name="test")], self.rows)
        self.assertTrue(result)
        self.assertIn("cheap → expensive → unknown → bad", result[0])
        self.assertTrue(all(row.endswith("Unpinned preference: no headroom") for row in result))

    def test_mode_validation(self):
        self.cfg["routing_mode"] = "typo"
        with self.assertRaisesRegex(ValueError, "routing_mode"):
            config.validate_accounts(self.cfg)

    def test_cost_class_validation(self):
        self.cfg["platforms"]["cheap"]["cost_class"] = "metered"
        with self.assertRaisesRegex(ValueError, "cost_class must be free or paid"):
            config.validate_accounts(self.cfg)


if __name__ == "__main__":
    unittest.main()
