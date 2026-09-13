"""Scheduler fairness (mahler#9): one global candidate list across projects,
builds before sorts when headroom is scarce, round-robin slots by project.

Everything runs against an in-memory Ledger with a dry-run context: no GitHub,
no subprocesses, and `platforms.available` is mocked so the tests don't depend
on which agent CLIs are installed on the machine.
"""

import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler import config, scheduler
from mahler.ledger import Ledger, iso

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


def proj(**over):
    pol = {"enabled": True, "repo": "x/y", "path": "/tmp/x", "hot_hold": False}
    pol.update(over)
    return pol


def mk_cfg(projects, total=2, max_runs=2):
    cfg = copy.deepcopy(config.DEFAULTS)
    cfg["concurrency"]["total"] = total
    for pc in cfg["platforms"].values():
        pc["max_runs"] = max_runs
    for name, pol in projects.items():
        cfg["projects"][name] = pol
    return cfg


def mk_ctx(projects, **kw):
    cfg = mk_cfg(projects, **kw)
    led = Ledger(":memory:", clock=lambda: NOW)
    ctx = scheduler.Ctx(cfg, led, dry_run=True)
    return ctx, led


def seed(led, **usage):
    """(5h, weekly) percentages per platform, sampled now, resetting later."""
    later = iso(NOW + timedelta(hours=3))
    for name, (five, weekly) in usage.items():
        led.record_usage(name, "5h", five, later)
        led.record_usage(name, "weekly", weekly, later)


def item(led, project, number, state="ready", priority=2, age_minutes=0):
    led.upsert_item(project, number, state=state, priority=priority,
                    state_changed_at=iso(NOW - timedelta(minutes=age_minutes)),
                    sorted_at=iso(NOW - timedelta(days=1)) if state == "ready" else None)


def plan(ctx, led):
    """One schedule pass with platform availability faked to True."""
    with mock.patch.object(scheduler.platforms, "available", return_value=True):
        scheduler.schedule(ctx, list(config.enabled_projects(ctx.cfg)))
    return [line for line in ctx.lines if ": would " in line]


class FairnessTests(unittest.TestCase):
    def test_round_robin_across_projects(self):
        """Three items in project a and one newer in project b, three slots:
        b's item is not left waiting behind a's whole queue."""
        ctx, led = mk_ctx({"a": proj(max_parallel=3), "b": proj(max_parallel=3)},
                          total=3, max_runs=3)
        seed(led, **{p: (10, 10) for p in ("claude", "agy-claude", "agy-gemini")})
        item(led, "a", 1, age_minutes=30)
        item(led, "b", 7, age_minutes=25)
        item(led, "a", 2, age_minutes=20)
        item(led, "a", 3, age_minutes=10)
        self.assertEqual(plan(ctx, led),
                         ["a#1: would build on agy-claude",
                          "b#7: would build on agy-claude",
                          "a#2: would build on agy-claude"])

    def test_builds_beat_sorts_when_one_platform_is_left(self):
        """The live failure (mahler#9): one free builder, an older inbox sort
        and a younger ready build — the build gets the slot, not the sort."""
        ctx, led = mk_ctx({"a": proj()}, total=1)
        seed(led, **{"claude": (75, 85), "agy-claude": (95, 95), "agy-gemini": (10, 10)})
        item(led, "a", 1, state="inbox", age_minutes=60)
        item(led, "a", 2, age_minutes=10)          # younger than the sort
        self.assertEqual(plan(ctx, led),
                         ["a#2: would build on agy-gemini"])

    def test_sorts_are_not_deferred_when_headroom_is_plenty(self):
        """With room everywhere the tiebreak is age again: the older sort
        still goes first."""
        ctx, led = mk_ctx({"a": proj(max_parallel=2)}, total=2, max_runs=2)
        seed(led, **{p: (10, 10) for p in ("claude", "agy-claude", "agy-gemini")})
        item(led, "a", 1, state="inbox", age_minutes=60)
        item(led, "a", 2, age_minutes=10)
        self.assertEqual(plan(ctx, led),
                         ["a#1: would sort on claude",
                          "a#2: would build on agy-claude"])

    def test_priority_comes_first_across_projects(self):
        ctx, led = mk_ctx({"a": proj(), "b": proj()}, total=1)
        seed(led, **{"agy-claude": (10, 10)})
        item(led, "a", 1, age_minutes=60)                    # default priority 2
        item(led, "b", 9, priority=1, age_minutes=10)        # p1, but newer
        self.assertEqual(plan(ctx, led),
                         ["b#9: would build on agy-claude"])

    def test_max_parallel_still_applies(self):
        ctx, led = mk_ctx({"a": proj(max_parallel=1), "b": proj()}, total=4, max_runs=2)
        seed(led, **{"agy-claude": (10, 10)})
        item(led, "a", 1, age_minutes=30)
        item(led, "b", 5, age_minutes=20)
        item(led, "a", 2, age_minutes=10)
        self.assertEqual(plan(ctx, led),
                         ["a#1: would build on agy-claude",
                          "b#5: would build on agy-claude"])
        self.assertIn("a: at capacity (2 running)", ctx.lines)

    def test_unmerged_change_holds_the_build_slot(self):
        """The live failure (mahler#27): with max_parallel 1, three builds ran
        back to back while none had merged, each on a base missing the others."""
        ctx, led = mk_ctx({"a": proj(max_parallel=1), "b": proj()}, total=4, max_runs=2)
        seed(led, **{"agy-claude": (10, 10)})
        item(led, "a", 1, state="verifying", age_minutes=40)
        item(led, "a", 2, age_minutes=30)
        item(led, "b", 5, age_minutes=20)
        self.assertEqual(plan(ctx, led), ["b#5: would build on agy-claude"])
        self.assertIn("a: builds wait — 1 finished change(s) not merged yet", ctx.lines)

    def test_unmerged_change_does_not_hold_sorts(self):
        ctx, led = mk_ctx({"a": proj(max_parallel=1)}, total=2, max_runs=2)
        seed(led, **{p: (10, 10) for p in ("claude", "agy-claude", "agy-gemini")})
        item(led, "a", 1, state="verifying", age_minutes=40)
        item(led, "a", 2, age_minutes=30)
        item(led, "a", 3, state="inbox", age_minutes=20)
        self.assertEqual(plan(ctx, led), ["a#3: would sort on claude"])

    def test_room_beside_an_unmerged_change(self):
        ctx, led = mk_ctx({"a": proj(max_parallel=2)}, total=2, max_runs=2)
        seed(led, **{"agy-claude": (10, 10)})
        item(led, "a", 1, state="verifying", age_minutes=40)
        item(led, "a", 2, age_minutes=30)
        item(led, "a", 3, age_minutes=20)
        self.assertEqual(plan(ctx, led), ["a#2: would build on agy-claude"])

    def test_per_platform_max_runs_still_applies(self):
        ctx, led = mk_ctx({"a": proj(), "b": proj()}, total=4, max_runs=1)
        seed(led, **{"agy-claude": (10, 10), "agy-gemini": (10, 10)})
        item(led, "a", 1, age_minutes=30)
        item(led, "b", 5, age_minutes=20)
        self.assertEqual(plan(ctx, led),
                         ["a#1: would build on agy-claude",
                          "b#5: would build on agy-gemini"])

    def test_settle_and_dependencies_still_gate_builds(self):
        ctx, led = mk_ctx({"a": proj()}, total=4, max_runs=4)
        seed(led, **{"agy-claude": (10, 10)})
        led.upsert_item("a", 1, state="ready", priority=2,          # freshly sorted
                        state_changed_at=iso(NOW), sorted_at=iso(NOW))
        led.upsert_item("a", 2, state="ready", state_changed_at=iso(NOW),
                        sorted_at=iso(NOW - timedelta(days=1)), depends="[3]")
        item(led, "a", 4, age_minutes=10)
        self.assertEqual(plan(ctx, led),
                         ["a#4: would build on agy-claude"])

    def test_mahler_project_prioritized_over_other_projects_all_else_equal(self):
        """All else being equal (same priority, both builds), mahler work is
        prioritized over other projects even if the other project has older items."""
        ctx, led = mk_ctx({"other": proj(), "mahler": proj()}, total=1)
        seed(led, **{"agy-claude": (10, 10)})
        item(led, "other", 1, priority=2, age_minutes=60)   # older item in other project
        item(led, "mahler", 42, priority=2, age_minutes=10)  # younger item in mahler
        self.assertEqual(plan(ctx, led),
                         ["mahler#42: would build on agy-claude"])

    def test_higher_priority_in_other_project_still_beats_mahler(self):
        """When not equal (p1 vs p2), issue priority still wins."""
        ctx, led = mk_ctx({"other": proj(), "mahler": proj()}, total=1)
        seed(led, **{"agy-claude": (10, 10)})
        item(led, "other", 1, priority=1, age_minutes=10)   # p1 in other project
        item(led, "mahler", 42, priority=2, age_minutes=60)  # p2 in mahler
        self.assertEqual(plan(ctx, led),
                         ["other#1: would build on agy-claude"])

    def test_custom_priority_projects_configuration(self):
        ctx, led = mk_ctx({"other": proj(), "mahler": proj(), "custom": proj()}, total=1)
        ctx.cfg["scheduling"]["priority_projects"] = ["custom", "mahler"]
        seed(led, **{"agy-claude": (10, 10)})
        item(led, "other", 1, priority=2, age_minutes=60)
        item(led, "mahler", 2, priority=2, age_minutes=40)
        item(led, "custom", 3, priority=2, age_minutes=10)
        self.assertEqual(plan(ctx, led),
                         ["custom#3: would build on agy-claude"])


if __name__ == "__main__":
    unittest.main()
