"""Scheduler fairness (mahler#9): one global candidate list across projects,
builds before sorts when headroom is scarce, round-robin slots by project.

Everything runs against an in-memory Ledger with a dry-run context: no GitHub,
no subprocesses, and `platforms.available` is mocked so the tests don't depend
on which agent CLIs are installed on the machine.
"""

import copy
import json
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler import config, router, scheduler
from mahler.ledger import Ledger, RoutedLedger, iso

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
    """(5h, weekly) percentages per platform, sampled now, resetting later.
    Reset is 6h out: outside both burst lead windows (weekly_lead 5h,
    session_lead 60m) so the default test scenario is not in a burst."""
    later = iso(NOW + timedelta(hours=6))
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


def seed_burst(led, claude_pct=(85, 85), free_pct=(10, 10)):
    """Seed usage so a weekly burst window is active for claude (D23).
    Resets: 5h in 30min, weekly in 2h — both within burst leads.
    Free tiers reset 6h out (no burst for them)."""
    five_reset = iso(NOW + timedelta(minutes=30))
    weekly_reset = iso(NOW + timedelta(hours=2))
    for name in ("claude", "claude-opus"):
        led.record_usage(name, "5h", claude_pct[0], five_reset)
        led.record_usage(name, "weekly", claude_pct[1], weekly_reset)
    later6 = iso(NOW + timedelta(hours=6))
    for name in ("agy-claude", "agy-gemini"):
        led.record_usage(name, "5h", free_pct[0], later6)
        led.record_usage(name, "weekly", free_pct[1], later6)


class RemoteLeaseFailureTests(unittest.TestCase):
    def test_unreachable_canonical_host_skips_project_without_starting(self):
        cfg = mk_cfg({"a": proj(remote_ledger={"host": "mini.example"})})
        local = Ledger(":memory:", clock=lambda: NOW)
        item(local, "a", 1, age_minutes=20)
        seed(local, **{"agy-claude": (10, 10)})

        def failed(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 255, "", "unreachable")

        led = RoutedLedger(local, cfg, run=failed)
        ctx = scheduler.Ctx(cfg, led, dry_run=True)
        with mock.patch.object(scheduler.platforms, "available", return_value=True), \
                mock.patch.object(scheduler, "start") as start:
            scheduler.schedule(ctx, list(config.enabled_projects(cfg)))
        start.assert_not_called()
        self.assertIn("canonical lease host unavailable — project skipped this tick",
                      "\n".join(ctx.lines))


class BurstScheduleTests(unittest.TestCase):
    """D23: burst before a Claude window resets — use the expiring reserve."""

    def test_burst_picks_claude_for_build(self):
        """During a burst, Claude builds first — its quota is expiring."""
        ctx, led = mk_ctx({"a": proj()}, total=1)
        seed_burst(led)  # claude 85%: hard normally, ok under burst soft 90
        item(led, "a", 1, age_minutes=10)
        self.assertEqual(plan(ctx, led), ["a#1: would build on claude"])
        self.assertTrue(any("burst" in l for l in ctx.lines))

    def test_no_burst_picks_free_tier_first(self):
        """Outside a burst, free tiers build before Claude (D8 reserve)."""
        ctx, led = mk_ctx({"a": proj()}, total=1)
        seed(led, **{"claude": (85, 85), "agy-claude": (10, 10)})
        item(led, "a", 1, age_minutes=10)
        self.assertEqual(plan(ctx, led), ["a#1: would build on agy-claude"])
        self.assertFalse(any("burst" in l for l in ctx.lines))

    def test_burst_suppressed_by_human_claude_flag(self):
        """5h usage rose while no Claude run was live → defer burst."""
        ctx, led = mk_ctx({"a": proj()}, total=1)
        seed_burst(led)
        led.set_kv("human:claude", iso(NOW))  # recent human usage signal
        item(led, "a", 1, age_minutes=10)
        self.assertEqual(plan(ctx, led), ["a#1: would build on agy-claude"])
        self.assertTrue(any("deferring" in l for l in ctx.lines))

    def test_burst_suppressed_by_transcript_activity(self):
        """Recent Claude Code transcript activity suppresses the burst."""
        ctx, led = mk_ctx({"a": proj()}, total=1)
        seed_burst(led)
        with mock.patch.object(scheduler.presence, "human_claude_active", return_value=True):
            item(led, "a", 1, age_minutes=10)
            lines = plan(ctx, led)
        self.assertEqual(lines, ["a#1: would build on agy-claude"])

    def test_burst_lets_claude_continue_past_normal_hard_line(self):
        """A running Claude run at 85% (normally hard) is not stopped mid-burst."""
        ctx, led = mk_ctx({"a": proj()}, total=1)
        seed_burst(led)
        scheduler._compute_burst(ctx, list(config.enabled_projects(ctx.cfg)))
        self.assertIsNotNone(ctx.burst_lines)  # burst detected and not suppressed
        pconf = config.DEFAULTS["platforms"]["claude"]
        # 85% is normally hard (>= 70)
        self.assertEqual(router.usage_state(led, "claude", pconf)[0], "hard")
        # 85% with burst lines (hard 97) → ok, run can continue
        self.assertEqual(router.usage_state(led, "claude", pconf,
                                            burst_lines=ctx.burst_lines)[0], "ok")

    def test_burst_pick_skips_exhausted_claude(self):
        """If Claude is already at 98% (hard even under burst), fall to free tiers."""
        ctx, led = mk_ctx({"a": proj()}, total=1)
        seed_burst(led, claude_pct=(98, 98))
        item(led, "a", 1, age_minutes=10)
        self.assertEqual(plan(ctx, led), ["a#1: would build on agy-claude"])

    def test_burst_disabled_turns_off_burst(self):
        """enabled = false → no burst, no burst note."""
        ctx, led = mk_ctx({"a": proj()}, total=1)
        ctx.cfg["burst"]["enabled"] = False
        seed_burst(led)
        item(led, "a", 1, age_minutes=10)
        self.assertEqual(plan(ctx, led), ["a#1: would build on agy-claude"])
        self.assertFalse(any("burst" in l for l in ctx.lines))


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

    def test_pinned_inbox_sort_uses_the_pin(self):
        """A platform pin on an inbox issue routes the sort to that platform."""
        ctx, led = mk_ctx({"a": proj()}, total=1)
        seed(led, **{"agy-claude": (10, 10), "claude": (10, 10)})
        led.upsert_item("a", 1, state="inbox", priority=2, pin="agy-claude",
                        state_changed_at=iso(NOW - timedelta(minutes=60)))
        self.assertEqual(plan(ctx, led), ["a#1: would sort on agy-claude"])

    def test_pinned_plan_sort_still_uses_the_pin(self):
        """A platform pin on a planning item overrides the default plan route."""
        ctx, led = mk_ctx({"a": proj()}, total=1)
        seed(led, **{"agy-claude": (10, 10), "claude-opus": (10, 10)})
        led.upsert_item("a", 1, state="inbox", priority=2, pin="agy-claude",
                        labels='["size:l"]',
                        state_changed_at=iso(NOW - timedelta(minutes=60)))
        self.assertEqual(plan(ctx, led), ["a#1: would sort on agy-claude"])

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


class QuotaGroupTests(unittest.TestCase):
    """DESIGN D21: claude and claude-opus share one run slot via
    a shared quota_group. A run on either platform blocks both."""

    def test_claude_run_blocks_claude_opus(self):
        """With a claude run active, claude-opus is marked busy
        (shared quota group at max_runs=1), so a size:l item skips it."""
        ctx, led = mk_ctx({"a": proj()}, total=2, max_runs=1)
        seed(led, **{"claude": (10, 10), "claude-opus": (10, 10)})
        led.create_run(project="a", number=1, role="build", platform="claude",
                       epoch=1)
        with mock.patch.object(scheduler.platforms, "available", return_value=True):
            busy = scheduler.busy_platforms(ctx.cfg, led.active_runs())
        self.assertIn("claude", busy)
        self.assertIn("claude-opus", busy)
        with mock.patch.object(scheduler.platforms, "available", return_value=True):
            platform, reasons = router.pick(ctx.cfg, led, "build", None, busy,
                                                size="l")
        self.assertNotEqual(platform, "claude-opus")
        self.assertIn("claude-opus: busy", reasons)

    def test_claude_opus_starts_when_free(self):
        """With nothing active, the size:l item starts on claude-opus."""
        ctx, led = mk_ctx({"a": proj()}, total=2)
        seed(led, **{"claude": (10, 10), "claude-opus": (10, 10)})
        led.upsert_item("a", 1, state="ready", priority=2,
                          state_changed_at=iso(NOW - timedelta(minutes=10)),
                          sorted_at=iso(NOW - timedelta(days=1)),
                          labels='["size:l"]')
        self.assertEqual(plan(ctx, led),
                         ["a#1: would build on claude-opus"])

    def test_different_groups_dont_block_each_other(self):
        """agy-gemini and claude are in different quota groups: with
        an agy-gemini run active, claude is still free (different group),
        and agy-gemini's own max_runs=2 allows a second agy-gemini run."""
        ctx, led = mk_ctx({"a": proj(max_parallel=4)}, total=4, max_runs=2)
        seed(led, **{"agy-gemini": (10, 10), "claude": (10, 10)})
        led.create_run(project="a", number=1, role="build", platform="agy-gemini",
                       epoch=1)
        with mock.patch.object(scheduler.platforms, "available", return_value=True):
            busy = scheduler.busy_platforms(ctx.cfg, led.active_runs())
        # agy-gemini has max_runs=2, 1 run → NOT at capacity
        self.assertNotIn("agy-gemini", busy)
        # claude is different group → NOT blocked by agy-gemini
        self.assertNotIn("claude", busy)
        # max_runs=2 for agy-gemini → second run on same platform is fine
        led.create_run(project="a", number=10, role="build", platform="agy-gemini",
                        epoch=2)
        with mock.patch.object(scheduler.platforms, "available", return_value=True):
            busy2 = scheduler.busy_platforms(ctx.cfg, led.active_runs())
        self.assertIn("agy-gemini", busy2)
        self.assertNotIn("claude", busy2)
        item(led, "a", 3, age_minutes=5)
        self.assertIn("a#3: would build on claude", plan(ctx, led))

    def test_red_ci_respects_quota_group(self):
        """_red_ci uses busy_platforms: a fix run doesn't start on
        claude while claude-opus is busy (same quota group)."""
        ctx, led = mk_ctx({"a": proj()}, total=4, max_runs=1)
        seed(led, **{"claude": (10, 10), "claude-opus": (10, 10)})
        led.create_run(project="a", number=1, role="build", platform="claude-opus",
                       epoch=1)
        # Simulate red CI on item 2
        ctx.led.upsert_item("a", 2, state="verifying", pr=88, branch="mahler/2",
                              labels='[]', title="fix")
        fake_start = mock.MagicMock(return_value=True)
        with mock.patch.object(scheduler, "start", side_effect=fake_start):
            scheduler._red_ci(ctx, "a", ctx.led.item("a", 2), 88,
                                {"headRefName": "fix/2", "headRefOid": "red1",
                                 "state": "FAILURE", "mergeable": "MERGEABLE",
                                 "statusCheckRollup": [{"state": "FAILURE"}]})
        # start should NOT be called with claude — group is busy
        for call in fake_start.call_args_list:
            platform = call.args[3]
            self.assertNotEqual(platform, "claude",
                                "fix run started on claude while claude-opus is busy")


if __name__ == "__main__":
    unittest.main()
