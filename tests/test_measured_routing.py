import copy
import unittest
from unittest.mock import patch
from contextlib import ExitStack

from mahler import config, router, scorecard
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

    def test_burst_is_stable_after_measurement(self):
        for name in ("cheap", "expensive"):
            self.cfg["platforms"][name]["kind"] = "claude"
        bursts = {"cheap": {"5h": (90, 97)}, "expensive": {"5h": (90, 97)}}
        self.assertEqual(self.pick(burst_lines=bursts), "cheap")
        self.assertEqual(self.pick(burst_lines={"expensive": {"5h": (90, 97)}}), "expensive")

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
        result = measured_routes(self.cfg, self.led, [dict(self.pol, name="test")], self.rows)
        self.assertIn("cheap → expensive → unknown → bad", result[0])
        self.assertIn("9/10 first try, $0.05 per success", result[0])
        self.assertEqual(measured_routes(self.cfg, self.led, [{"name": "test"}], self.rows), [])

    def test_mode_validation(self):
        self.cfg["routing_mode"] = "typo"
        with self.assertRaisesRegex(ValueError, "routing_mode"):
            config.validate_accounts(self.cfg)


if __name__ == "__main__":
    unittest.main()
