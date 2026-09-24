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

from mahler import config, platforms, presence, router, scheduler, ship, tick, usage
from mahler.ledger import Ledger, RoutedLedger, iso
from mahler.gh import depends_of

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
    with mock.patch.object(platforms, "available", return_value=True):
        tick.schedule(ctx, list(config.enabled_projects(ctx.cfg)))
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


class DependencyTests(unittest.TestCase):
    def setup_dependencies(self, body, extra=None):
        projects = {"a": proj(repo="mkny13/mahler"),
                    "ground": proj(repo="mkny13/groundwork"),
                    "tour": proj(repo="mkny13/couch-tour")}
        projects.update(extra or {})
        ctx, led = mk_ctx(projects)
        self.addCleanup(led.close)
        item(led, "a", 292)
        led.upsert_item("a", 292, depends=json.dumps(depends_of(body)))
        seed(led, **{"agy-claude": (10, 10)})
        return ctx, led

    def test_cross_repo_build_waits_for_both_target_issues(self):
        ctx, led = self.setup_dependencies(
            "Depends on: mkny13/groundwork#125, couch-tour#258")
        item(led, "a", 125, state="done")
        item(led, "a", 258, state="done")
        item(led, "ground", 125, state="parked")
        item(led, "tour", 258, state="shipped")
        self.assertEqual(plan(ctx, led), [])
        self.assertEqual(ctx.holds[0]["on"], [
            {"repo": "mkny13/groundwork", "number": 125},
            {"repo": "couch-tour", "number": 258}])
        led.set_state("ground", 125, "done", "test")
        self.assertEqual(plan(ctx, led), [])
        led.set_state("tour", 258, "done", "test")
        self.assertEqual(plan(ctx, led), ["a#292: would build on agy-claude"])

    def test_unknown_disabled_and_ambiguous_refs_remain_blocked(self):
        for ref, extra in [
            ("unknown/groundwork#125", {}),
            ("missing#125", {}),
            ("mkny13/groundwork#125", {"ground": proj(
                repo="mkny13/groundwork", enabled=False)}),
            ("groundwork#125", {"other": proj(repo="else/groundwork")}),
        ]:
            with self.subTest(ref=ref, extra=extra):
                ctx, led = self.setup_dependencies("Depends on: " + ref, extra)
                for name in ("a", "ground", "other"):
                    item(led, name, 125, state="done")
                self.assertEqual(plan(ctx, led), [])
                self.assertEqual(ctx.holds[0]["on"], depends_of("Depends on: " + ref))

    def test_bare_dependency_only_uses_current_project(self):
        ctx, led = self.setup_dependencies("Depends on: #125")
        item(led, "ground", 125, state="done")
        self.assertEqual(plan(ctx, led), [])
        item(led, "a", 125, state="done")
        self.assertEqual(plan(ctx, led), ["a#292: would build on agy-claude"])

    def test_qualified_local_ref_and_case_insensitive_matching(self):
        ctx, led = self.setup_dependencies("Depends on: MKNY13/MAHLER#125")
        self.assertEqual(plan(ctx, led), [])
        item(led, "a", 125, state="done")
        self.assertEqual(plan(ctx, led), ["a#292: would build on agy-claude"])

    def test_disabled_duplicate_does_not_make_shorthand_ambiguous(self):
        ctx, led = self.setup_dependencies("Depends on: groundwork#125", {
            "other": proj(repo="else/groundwork", enabled=False)})
        item(led, "ground", 125, state="done")
        self.assertEqual(plan(ctx, led), ["a#292: would build on agy-claude"])


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
        with mock.patch.object(platforms, "available", return_value=True), \
                mock.patch.object(tick, "start") as start:
            tick.schedule(ctx, list(config.enabled_projects(cfg)))
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
        with mock.patch.object(presence, "human_claude_active", return_value=True):
            item(led, "a", 1, age_minutes=10)
            lines = plan(ctx, led)
        self.assertEqual(lines, ["a#1: would build on agy-claude"])

    def test_burst_lets_claude_continue_past_normal_hard_line(self):
        """A running Claude run at 85% (normally hard) is not stopped mid-burst."""
        ctx, led = mk_ctx({"a": proj()}, total=1)
        seed_burst(led)
        usage.compute_burst(ctx, list(config.enabled_projects(ctx.cfg)))
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
    def test_opted_in_codex_route_is_scheduled(self):
        ctx, led = mk_ctx({"a": proj()}, total=1)
        ctx.cfg["routing"]["build"] = ["codex"]
        seed(led, codex=(10, 10))
        item(led, "a", 1, age_minutes=10)
        self.assertEqual(plan(ctx, led), ["a#1: would build on codex"])

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
        with mock.patch.object(platforms, "available", return_value=True):
            busy = tick.busy_platforms(ctx.cfg, led.active_runs())
        self.assertIn("claude", busy)
        self.assertIn("claude-opus", busy)
        with mock.patch.object(platforms, "available", return_value=True):
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
        with mock.patch.object(platforms, "available", return_value=True):
            busy = tick.busy_platforms(ctx.cfg, led.active_runs())
        # agy-gemini has max_runs=2, 1 run → NOT at capacity
        self.assertNotIn("agy-gemini", busy)
        # claude is different group → NOT blocked by agy-gemini
        self.assertNotIn("claude", busy)
        # max_runs=2 for agy-gemini → second run on same platform is fine
        led.create_run(project="a", number=10, role="build", platform="agy-gemini",
                        epoch=2)
        with mock.patch.object(platforms, "available", return_value=True):
            busy2 = tick.busy_platforms(ctx.cfg, led.active_runs())
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
        with mock.patch.object(ship, "start", side_effect=fake_start):
            ship._red_ci(ctx, "a", ctx.led.item("a", 2), 88,
                                {"headRefName": "fix/2", "headRefOid": "red1",
                                 "state": "FAILURE", "mergeable": "MERGEABLE",
                                 "statusCheckRollup": [{"state": "FAILURE"}]})
        # start should NOT be called with claude — group is busy
        for call in fake_start.call_args_list:
            platform = call.args[3]
            self.assertNotEqual(platform, "claude",
                                "fix run started on claude while claude-opus is busy")


    def test_orphan_working_item_reclaimed_on_tick(self):
        ctx, led = mk_ctx({"a": proj()})
        # mahler#162: item stuck in 'working' with no lease and no run
        item(led, "a", 124, state="working")
        self.assertEqual(led.item("a", 124)["state"], "working")
        self.assertIsNone(led.lease("a", 124))

        # On the next tick, expire/sweep_orphans returns it to ready
        tick.expire(ctx)
        self.assertEqual(led.item("a", 124)["state"], "ready")
        self.assertTrue(any("orphan working item (no lease, no active run) returned to ready" in line
                            for line in ctx.lines))
        evs = led.q("SELECT * FROM events WHERE project='a' AND number=124 AND kind='orphan_recovered'")
        self.assertEqual(len(evs), 1)

    def test_orphan_lease_on_ready_and_done_items_reaped_on_tick(self):
        ctx, led = mk_ctx({"a": proj()})
        item(led, "a", 10, state="ready")
        led.claim("a", 10, "stale:holder", "auto", 30)
        item(led, "a", 20, state="done")
        led.claim("a", 20, "stale:holder2", "auto", 30)
        self.assertIsNotNone(led.lease("a", 10))
        self.assertIsNotNone(led.lease("a", 20))

        tick.expire(ctx)
        self.assertIsNone(led.lease("a", 10))
        self.assertIsNone(led.lease("a", 20))
        self.assertEqual(led.item("a", 10)["state"], "ready")
        self.assertEqual(led.item("a", 20)["state"], "done")
        self.assertTrue(any("orphan lease by stale:holder on ready item released" in line
                            for line in ctx.lines))
        self.assertTrue(any("orphan lease by stale:holder2 on done item released" in line
                            for line in ctx.lines))


class VariantQuotaGroupTests(unittest.TestCase):
    """Issue #420 (D33): several model x effort variants on one platform
    slot share its quota_group and its one run slot — max_runs must be
    counted across every variant together, not per variant name."""

    def variant_cfg(self, variants, max_runs):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["concurrency"]["total"] = 10
        cfg["platforms"] = {"kilo": cfg["platforms"]["kilo"]}
        cfg["platforms"]["kilo"]["variants"] = variants
        cfg["platforms"]["kilo"]["max_runs"] = max_runs
        cfg["platforms"]["kilo"].pop("max_size", None)
        cfg = config.resolve_platforms(cfg)
        names = [n for n in cfg["platforms"] if n != "kilo"]
        cfg["routing"]["build"] = names
        cfg["projects"]["a"] = proj(max_parallel=10)
        return cfg, names

    def test_group_max_runs_caps_total_starts_across_variants_in_one_tick(self):
        """Three variants, max_runs=2 on their shared slot, each item pinned
        to a different variant: a naive count per variant name (each
        individually under 2) would let all three start; the group total
        across variants must stop at 2."""
        cfg, names = self.variant_cfg(["model-a", "model-b", "model-c"], max_runs=2)
        led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(led.close)
        ctx = scheduler.Ctx(cfg, led, dry_run=True)
        for i, name in zip(range(1, 4), names):
            item(led, "a", i, age_minutes=5 - i)
            led.upsert_item("a", i, pin=name)
        with mock.patch.object(platforms, "available", return_value=True):
            tick.schedule(ctx, list(config.enabled_projects(ctx.cfg)))
        started = [line for line in ctx.lines if ": would " in line]
        self.assertEqual(len(started), 2, started)

    def test_a_running_variant_blocks_a_sibling_variant_at_max_runs_one(self):
        cfg, names = self.variant_cfg(["model-a", "model-b"], max_runs=1)
        led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(led.close)
        led.create_run(project="a", number=99, role="build", platform=names[0], epoch=1)
        with mock.patch.object(platforms, "available", return_value=True):
            busy = tick.busy_platforms(cfg, led.active_runs())
        self.assertIn(names[0], busy)
        self.assertIn(names[1], busy)          # the sibling shares the group

    def test_group_under_max_runs_leaves_the_sibling_free(self):
        cfg, names = self.variant_cfg(["model-a", "model-b"], max_runs=2)
        led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(led.close)
        led.create_run(project="a", number=99, role="build", platform=names[0], epoch=1)
        with mock.patch.object(platforms, "available", return_value=True):
            busy = tick.busy_platforms(cfg, led.active_runs())
        self.assertNotIn(names[1], busy)


class AreaLabelTests(unittest.TestCase):
    """mahler#197: items sharing an `area:` label aren't run concurrently
    (DESIGN Layer 3, D6) — soft mutual exclusion, distinct from `depends`."""

    def test_same_area_serializes_only_one_starts(self):
        ctx, led = mk_ctx({"a": proj(max_parallel=2)}, total=2, max_runs=2)
        seed(led, **{"agy-claude": (10, 10)})
        led.upsert_item("a", 1, state="ready", priority=2, labels='["area:router"]',
                        state_changed_at=iso(NOW - timedelta(minutes=30)),
                        sorted_at=iso(NOW - timedelta(days=1)))
        led.upsert_item("a", 2, state="ready", priority=2, labels='["area:router"]',
                        state_changed_at=iso(NOW - timedelta(minutes=10)),
                        sorted_at=iso(NOW - timedelta(days=1)))
        lines = plan(ctx, led)
        self.assertEqual(lines, ["a#1: would build on agy-claude"])
        self.assertIn("a#2: waiting — area:router already in progress", ctx.lines)

    def test_different_areas_both_start(self):
        ctx, led = mk_ctx({"a": proj(max_parallel=2)}, total=2, max_runs=2)
        seed(led, **{p: (10, 10) for p in ("claude", "agy-claude", "agy-gemini")})
        led.upsert_item("a", 1, state="ready", priority=2, labels='["area:router"]',
                        state_changed_at=iso(NOW - timedelta(minutes=30)),
                        sorted_at=iso(NOW - timedelta(days=1)))
        led.upsert_item("a", 2, state="ready", priority=2, labels='["area:scheduler"]',
                        state_changed_at=iso(NOW - timedelta(minutes=10)),
                        sorted_at=iso(NOW - timedelta(days=1)))
        lines = plan(ctx, led)
        self.assertEqual(len(lines), 2)
        self.assertTrue(any("a#1: would build" in l for l in lines))
        self.assertTrue(any("a#2: would build" in l for l in lines))

    def test_verifying_item_holds_its_area_against_a_ready_item(self):
        ctx, led = mk_ctx({"a": proj(max_parallel=2)}, total=2, max_runs=2)
        seed(led, **{"agy-claude": (10, 10)})
        led.upsert_item("a", 1, state="verifying", priority=2, labels='["area:router"]',
                        state_changed_at=iso(NOW - timedelta(minutes=40)))
        led.upsert_item("a", 2, state="ready", priority=2, labels='["area:router"]',
                        state_changed_at=iso(NOW - timedelta(minutes=10)),
                        sorted_at=iso(NOW - timedelta(days=1)))
        lines = plan(ctx, led)
        self.assertEqual(lines, [])
        self.assertIn("a#2: waiting — area:router already in progress", ctx.lines)

    def test_unlabeled_item_is_never_blocked(self):
        ctx, led = mk_ctx({"a": proj(max_parallel=2)}, total=2, max_runs=2)
        seed(led, **{"agy-claude": (10, 10)})
        led.upsert_item("a", 1, state="verifying", priority=2, labels='["area:router"]',
                        state_changed_at=iso(NOW - timedelta(minutes=40)))
        led.upsert_item("a", 2, state="ready", priority=2, labels="[]",
                        state_changed_at=iso(NOW - timedelta(minutes=10)),
                        sorted_at=iso(NOW - timedelta(days=1)))
        lines = plan(ctx, led)
        self.assertEqual(lines, ["a#2: would build on agy-claude"])


class FileOverlapTests(unittest.TestCase):
    """mahler#210: the `## Plan` Files: list, parsed at sync time into the
    `files` column, gives mutual exclusion the same shape as `area:` labels
    but mechanically — no label, no judgment call needed from the sort
    agent."""

    def test_overlapping_files_serializes_only_one_starts(self):
        ctx, led = mk_ctx({"a": proj(max_parallel=2)}, total=2, max_runs=2)
        seed(led, **{"agy-claude": (10, 10)})
        led.upsert_item("a", 1, state="ready", priority=2,
                        files='["mahler/tick.py"]',
                        state_changed_at=iso(NOW - timedelta(minutes=30)),
                        sorted_at=iso(NOW - timedelta(days=1)))
        led.upsert_item("a", 2, state="ready", priority=2,
                        files='["mahler/tick.py", "mahler/gh.py"]',
                        state_changed_at=iso(NOW - timedelta(minutes=10)),
                        sorted_at=iso(NOW - timedelta(days=1)))
        lines = plan(ctx, led)
        self.assertEqual(lines, ["a#1: would build on agy-claude"])
        self.assertIn("a#2: waiting — files already in progress: mahler/tick.py", ctx.lines)

    def test_disjoint_files_both_start(self):
        ctx, led = mk_ctx({"a": proj(max_parallel=2)}, total=2, max_runs=2)
        seed(led, **{p: (10, 10) for p in ("claude", "agy-claude", "agy-gemini")})
        led.upsert_item("a", 1, state="ready", priority=2,
                        files='["mahler/tick.py"]',
                        state_changed_at=iso(NOW - timedelta(minutes=30)),
                        sorted_at=iso(NOW - timedelta(days=1)))
        led.upsert_item("a", 2, state="ready", priority=2,
                        files='["mahler/router.py"]',
                        state_changed_at=iso(NOW - timedelta(minutes=10)),
                        sorted_at=iso(NOW - timedelta(days=1)))
        lines = plan(ctx, led)
        self.assertEqual(len(lines), 2)
        self.assertTrue(any("a#1: would build" in l for l in lines))
        self.assertTrue(any("a#2: would build" in l for l in lines))

    def test_verifying_item_holds_its_files_against_a_ready_item(self):
        ctx, led = mk_ctx({"a": proj(max_parallel=2)}, total=2, max_runs=2)
        seed(led, **{"agy-claude": (10, 10)})
        led.upsert_item("a", 1, state="verifying", priority=2,
                        files='["mahler/tick.py"]',
                        state_changed_at=iso(NOW - timedelta(minutes=40)))
        led.upsert_item("a", 2, state="ready", priority=2,
                        files='["mahler/tick.py"]',
                        state_changed_at=iso(NOW - timedelta(minutes=10)),
                        sorted_at=iso(NOW - timedelta(days=1)))
        lines = plan(ctx, led)
        self.assertEqual(lines, [])
        self.assertIn("a#2: waiting — files already in progress: mahler/tick.py", ctx.lines)

    def test_item_with_no_files_is_never_blocked(self):
        ctx, led = mk_ctx({"a": proj(max_parallel=2)}, total=2, max_runs=2)
        seed(led, **{"agy-claude": (10, 10)})
        led.upsert_item("a", 1, state="verifying", priority=2,
                        files='["mahler/tick.py"]',
                        state_changed_at=iso(NOW - timedelta(minutes=40)))
        led.upsert_item("a", 2, state="ready", priority=2, files="[]",
                        state_changed_at=iso(NOW - timedelta(minutes=10)),
                        sorted_at=iso(NOW - timedelta(days=1)))
        lines = plan(ctx, led)
        self.assertEqual(lines, ["a#2: would build on agy-claude"])


class ProjectExclusionTests(unittest.TestCase):
    """mahler#231: area and file exclusions belong to one project."""

    def test_exclusions_are_project_scoped_for_every_seed_path(self):
        for field, value, waiting in (
            ("labels", '["area:router"]', "area:router already in progress"),
            ("files", '["README.md"]', "files already in progress: README.md"),
        ):
            for state in ("working", "verifying", "ready"):
                for candidate_project in ("a", "b"):
                    with self.subTest(field=field, state=state,
                                      candidate_project=candidate_project):
                        ctx, led = mk_ctx({
                            "a": proj(repo="x/a", path="/tmp/a", max_parallel=2),
                            "b": proj(repo="x/b", path="/tmp/b", max_parallel=2),
                        }, total=3, max_runs=3)
                        self.addCleanup(led.close)
                        seed(led, **{"agy-claude": (10, 10)})
                        item(led, "a", 1, state=state, age_minutes=30)
                        led.upsert_item("a", 1, **{field: value})
                        if state == "working":
                            led.create_run(project="a", number=1, role="build",
                                           platform="agy-claude", epoch=1)
                        item(led, candidate_project, 2, age_minutes=10)
                        led.upsert_item(candidate_project, 2, **{field: value})

                        lines = plan(ctx, led)

                        expected = (["a#1: would build on agy-claude"]
                                    if state == "ready" else [])
                        if candidate_project == "b":
                            expected.append("b#2: would build on agy-claude")
                            self.assertFalse(any("waiting —" in line for line in ctx.lines))
                        else:
                            self.assertIn(f"a#2: waiting — {waiting}", ctx.lines)
                        self.assertEqual(lines, expected)


class TierBudgetUnitTests(unittest.TestCase):
    """mahler#200: tick.tier_budget_busy() in isolation — the pure function
    behind concurrency.by_tier, before it's wired into a full schedule()
    pass. Tiers here are config.py's current cline-free/kilo=1, agy-claude/
    codex/copilot=2, agy-gemini/claude/codex-high/copilot-high=3, claude-opus=4."""

    def test_absent_by_tier_blocks_nothing(self):
        cfg = mk_cfg({"a": proj()})
        self.assertEqual(tick.tier_budget_busy(cfg, [1, 1, 3, 4]), set())

    def test_below_cap_blocks_nothing(self):
        cfg = mk_cfg({"a": proj()})
        cfg["concurrency"]["by_tier"] = {1: 3}
        self.assertEqual(tick.tier_budget_busy(cfg, [1, 1]), set())

    def test_at_cap_blocks_its_own_tier(self):
        cfg = mk_cfg({"a": proj()})
        cfg["concurrency"]["by_tier"] = {3: 1}
        busy = tick.tier_budget_busy(cfg, [3])
        self.assertIn("claude", busy)
        self.assertIn("agy-gemini", busy)

    def test_at_cap_also_blocks_tiers_above_it(self):
        """'At or above': a budget keyed at tier 3 also blocks tier 4
        (claude-opus) — a scarcer run can't dodge a laxer cap."""
        cfg = mk_cfg({"a": proj()})
        cfg["concurrency"]["by_tier"] = {3: 1}
        busy = tick.tier_budget_busy(cfg, [3])
        self.assertIn("claude-opus", busy)

    def test_at_cap_leaves_lower_tiers_free(self):
        cfg = mk_cfg({"a": proj()})
        cfg["concurrency"]["by_tier"] = {3: 1}
        busy = tick.tier_budget_busy(cfg, [3])
        self.assertNotIn("cline-free", busy)
        self.assertNotIn("kilo", busy)
        self.assertNotIn("agy-claude", busy)

    def test_a_scarcer_run_counts_against_a_laxer_budget(self):
        """A tier-4 (claude-opus) run also counts against a tier-'2 and up'
        budget — it can't evade the cap by running at an even scarcer tier."""
        cfg = mk_cfg({"a": proj()})
        cfg["concurrency"]["by_tier"] = {2: 1}
        busy = tick.tier_budget_busy(cfg, [4])
        self.assertIn("claude-opus", busy)   # tier 4
        self.assertIn("claude", busy)        # tier 3, >= 2
        self.assertIn("agy-claude", busy)    # tier 2
        self.assertNotIn("cline-free", busy)  # tier 1, below the budget's floor


class TierBudgetScheduleTests(unittest.TestCase):
    """mahler#200: concurrency.by_tier wired into tick.schedule() — a
    finer-grained cap layered under the existing concurrency.total ceiling,
    never a replacement for it."""

    def test_absent_by_tier_reproduces_current_behavior(self):
        """Regression guard: with no by_tier configured, two active
        cline-free (tier 1) runs don't stop a third from starting."""
        ctx, led = mk_ctx({"a": proj(max_parallel=6)}, total=6, max_runs=5)
        led.create_run(project="a", number=1, role="build", platform="cline-free", epoch=1)
        led.create_run(project="a", number=2, role="build", platform="cline-free", epoch=2)
        led.upsert_item("a", 3, state="ready", priority=2, labels='["size:s"]',
                        state_changed_at=iso(NOW - timedelta(minutes=10)),
                        sorted_at=iso(NOW - timedelta(days=1)))
        self.assertEqual(plan(ctx, led), ["a#3: would build on cline-free"])

    def test_by_tier_budget_allows_up_to_its_cap(self):
        """by_tier = {1: 3}: two active tier-1 runs (cline-free) leave room
        for a third — the budget isn't reached yet."""
        ctx, led = mk_ctx({"a": proj(max_parallel=6)}, total=6, max_runs=5)
        ctx.cfg["concurrency"]["by_tier"] = {1: 3}
        led.create_run(project="a", number=1, role="build", platform="cline-free", epoch=1)
        led.create_run(project="a", number=2, role="build", platform="cline-free", epoch=2)
        led.upsert_item("a", 3, state="ready", priority=2, labels='["size:s"]',
                        state_changed_at=iso(NOW - timedelta(minutes=10)),
                        sorted_at=iso(NOW - timedelta(days=1)))
        self.assertEqual(plan(ctx, led), ["a#3: would build on cline-free"])

    def test_by_tier_budget_blocks_once_its_cap_is_reached(self):
        """by_tier = {1: 3}: three active tier-1 runs (cline-free + kilo)
        block a fourth, even though each platform's own max_runs has
        headroom left."""
        ctx, led = mk_ctx({"a": proj(max_parallel=6)}, total=6, max_runs=5)
        ctx.cfg["concurrency"]["by_tier"] = {1: 3}
        led.create_run(project="a", number=1, role="build", platform="cline-free", epoch=1)
        led.create_run(project="a", number=2, role="build", platform="cline-free", epoch=2)
        led.create_run(project="a", number=3, role="build", platform="kilo", epoch=1)
        led.upsert_item("a", 4, state="ready", priority=2, labels='["size:s"]',
                        state_changed_at=iso(NOW - timedelta(minutes=10)),
                        sorted_at=iso(NOW - timedelta(days=1)))
        self.assertEqual(plan(ctx, led), [])
        self.assertTrue(any("a#4: no platform for build" in l for l in ctx.lines))

    def test_by_tier_budget_blocks_a_second_concurrent_scarce_run(self):
        """by_tier = {4: 1}: a size:l item that would otherwise route to
        claude-opus (the only size:l-fitting platform seeded here) is
        blocked while one claude-opus run is already active."""
        ctx, led = mk_ctx({"a": proj(max_parallel=4)}, total=4, max_runs=5)
        ctx.cfg["concurrency"]["by_tier"] = {4: 1}
        seed(led, **{"claude": (10, 10), "claude-opus": (10, 10)})
        led.create_run(project="a", number=1, role="build", platform="claude-opus", epoch=1)
        led.upsert_item("a", 2, state="ready", priority=2, labels='["size:l"]',
                        state_changed_at=iso(NOW - timedelta(minutes=10)),
                        sorted_at=iso(NOW - timedelta(days=1)))
        self.assertEqual(plan(ctx, led), [])

    def test_total_still_wins_even_when_by_tier_allows_more(self):
        """concurrency.total stays the hard outer ceiling: a generous
        by_tier budget (10) doesn't let a second run start past total=2."""
        ctx, led = mk_ctx({"a": proj(max_parallel=6)}, total=2, max_runs=5)
        ctx.cfg["concurrency"]["by_tier"] = {1: 10}
        led.create_run(project="a", number=1, role="build", platform="cline-free", epoch=1)
        led.upsert_item("a", 2, state="ready", priority=2, labels='["size:s"]',
                        state_changed_at=iso(NOW - timedelta(minutes=20)),
                        sorted_at=iso(NOW - timedelta(days=1)))
        led.upsert_item("a", 3, state="ready", priority=2, labels='["size:s"]',
                        state_changed_at=iso(NOW - timedelta(minutes=10)),
                        sorted_at=iso(NOW - timedelta(days=1)))
        lines = plan(ctx, led)
        self.assertEqual(len(lines), 1)


class HotHoldTests(unittest.TestCase):
    """D6 layer 2: recent Claude transcript activity in a project holds its
    new builds (not sorts, not running work) until hot_hold_minutes pass.
    The hold lifts `hot_hold_minutes` after the last activity; a project can
    opt out with hot_hold: false, and --no-hot-hold lifts every hold."""

    def hold(self, minutes_ago=5, hot_hold=True, **over):
        ctx, led = mk_ctx({"a": proj(hot_hold=hot_hold, **over)})
        seed(led, **{"agy-claude": (10, 10)})
        item(led, "a", 1)
        last = None if minutes_ago is None else NOW - timedelta(minutes=minutes_ago)
        with mock.patch.object(presence, "last_claude_activity", return_value=last):
            lines = plan(ctx, led)
        return lines, ctx.lines

    def test_recent_activity_holds_new_builds(self):
        lines, said = self.hold(minutes_ago=5)
        self.assertEqual(lines, [])
        self.assertIn("a#1: hot hold — a Claude session is active in this project", said)

    def test_activity_older_than_the_window_lets_builds_start(self):
        lines, _ = self.hold(minutes_ago=25)          # default window is 20 min
        self.assertEqual(lines, ["a#1: would build on agy-claude"])

    def test_no_activity_at_all_lets_builds_start(self):
        lines, _ = self.hold(minutes_ago=None)
        self.assertEqual(lines, ["a#1: would build on agy-claude"])

    def test_a_raised_window_holds_longer(self):
        lines, _ = self.hold(minutes_ago=25, hot_hold_minutes=30)
        self.assertEqual(lines, [])

    def test_a_project_can_opt_out_of_the_hot_hold(self):
        lines, _ = self.hold(hot_hold=False)
        self.assertEqual(lines, ["a#1: would build on agy-claude"])

    def test_no_hot_hold_let_starts_through_everywhere(self):
        cfg = mk_cfg({"a": proj(hot_hold=True)})
        led = Ledger(":memory:", clock=lambda: NOW)
        ctx = scheduler.Ctx(cfg, led, dry_run=True, hot_hold=False)
        seed(led, **{"agy-claude": (10, 10)})
        item(led, "a", 1)
        with mock.patch.object(presence, "last_claude_activity",
                               return_value=NOW - timedelta(minutes=1)):
            lines = plan(ctx, led)
        self.assertEqual(lines, ["a#1: would build on agy-claude"])

    def test_sorts_are_not_gated_by_the_hot_hold(self):
        # sorts are read-only triage: no worktree, no files, so a human
        # session in the project never collides with one
        ctx, led = mk_ctx({"a": proj(hot_hold=True)})
        seed(led, **{"agy-claude": (10, 10)})
        item(led, "a", 1, state="inbox")
        with mock.patch.object(presence, "last_claude_activity",
                               return_value=NOW - timedelta(minutes=1)):
            lines = plan(ctx, led)
        self.assertEqual(lines, ["a#1: would sort on agy-claude"])


class InteractiveLeaseRenewalTests(unittest.TestCase):
    """D6: an interactive lease whose TTL lapses is renewed from Claude
    transcript activity (presence stands in for the hooks), and released
    back to ready when the activity is gone."""

    def setUp(self):
        self.t = {"now": NOW}
        self.led = Ledger(":memory:", clock=lambda: self.t["now"])
        cfg = mk_cfg({"a": proj()})
        self.ctx = scheduler.Ctx(cfg, self.led, dry_run=True)

    def expired_lease(self):
        self.led.upsert_item("a", 1, state="working")
        self.led.claim("a", 1, "interactive:you", "interactive", 30)
        self.t["now"] = NOW + timedelta(minutes=45)     # past the 30-min TTL

    def test_recent_claude_activity_renews_the_lease(self):
        self.expired_lease()
        with mock.patch.object(presence, "last_claude_activity",
                               return_value=self.t["now"] - timedelta(minutes=5)):
            tick.expire(self.ctx)
        lease = self.led.lease("a", 1)
        self.assertIsNotNone(lease)
        self.assertEqual(lease["holder"], "interactive:you")
        self.assertEqual(self.led.item("a", 1)["state"], "working")

    def test_no_activity_releases_the_expired_lease(self):
        self.expired_lease()
        with mock.patch.object(presence, "last_claude_activity", return_value=None):
            tick.expire(self.ctx)
        self.assertIsNone(self.led.lease("a", 1))
        self.assertEqual(self.led.item("a", 1)["state"], "ready")


class ScheduleHoldTests(unittest.TestCase):
    def test_candidate_holds(self):
        ctx, led = mk_ctx({"a": proj(settle_minutes=10)})
        self.addCleanup(led.close)
        item(led, "a", 1)
        led.upsert_item("a", 1, sorted_at=iso(NOW - timedelta(minutes=2)))
        item(led, "a", 2)
        led.upsert_item("a", 2, depends="[3, 4]")
        item(led, "a", 3, state="done")
        plan(ctx, led)
        self.assertEqual(ctx.holds, [
            {"kind": "settling", "project": "a", "number": 1,
             "until": iso(NOW + timedelta(minutes=8))},
            {"kind": "deps", "project": "a", "number": 2, "on": [4]}])

    def test_schedule_branches(self):
        for kind in ("capacity", "slot", "area", "files", "hot_hold", "lease_host", "no_platform"):
            with self.subTest(kind=kind):
                ctx, led = mk_ctx({"a": proj(max_parallel=2, hot_hold=kind == "hot_hold")}, total=4)
                self.addCleanup(led.close)
                item(led, "a", 1)
                if kind == "capacity":
                    for n in (2, 3):
                        led.create_run(project="a", number=n, role="build", platform="kilo", epoch=1)
                elif kind in ("slot", "area", "files"):
                    item(led, "a", 2, state="verifying")
                    if kind == "slot":
                        item(led, "a", 3, state="verifying")
                    else:
                        field, value = (("labels", '["area:console"]') if kind == "area"
                                        else ("files", '["mahler/tick.py"]'))
                        for n in (1, 2):
                            led.upsert_item("a", n, **{field: value})
                with mock.patch.object(presence, "last_claude_activity", return_value=NOW), \
                     mock.patch.object(led, "remote_error", create=True,
                                       return_value="offline" if kind == "lease_host" else None), \
                     mock.patch.object(router, "pick_for_project", return_value=(None, ["kilo: busy"])):
                    plan(ctx, led)
                expected = {"kind": kind, "project": "a"}
                expected.update({
                    "capacity": {"max_parallel": 2}, "slot": {"verifying": [2, 3]},
                    "area": {"number": 1, "area": "console"},
                    "files": {"number": 1, "files": ["mahler/tick.py"]},
                    "hot_hold": {"number": 1}, "lease_host": {},
                    "no_platform": {"number": 1, "role": "build", "size": "m",
                                    "blockers": {"busy": ["kilo"]}},
                }[kind])
                self.assertEqual(ctx.holds, [expected])

    def test_tick_records_paused_and_scheduled_holds_and_survives_failed_write(self):
        from contextlib import ExitStack
        for paused, fail in ((False, False), (True, False), (False, True)):
            with self.subTest(paused=paused, fail=fail):
                ctx, led = mk_ctx({})
                self.addCleanup(led.close)
                ctx.dry_run = False
                if paused:
                    led.set_kv("paused", "1")
                with ExitStack() as stack:
                    for name in ("compute_burst", "watchdog", "expire", "close_finished_parents",
                                 "refresh_usage", "queue_maintenance", "platform_audit.queue",
                                 "ship", "digest.maybe_send", "janitor.maybe_run"):
                        stack.enter_context(mock.patch("mahler.scheduler." + name))
                    stack.enter_context(mock.patch("mahler.scheduler.schedule", side_effect=lambda c, p:
                                                   c.hold("lease_host", project="a")))
                    if fail:
                        stack.enter_context(mock.patch.object(led, "set_kv", side_effect=RuntimeError("disk")))
                    scheduler.tick(ctx)
                if fail:
                    self.assertTrue(any("couldn't record schedule holds" in line for line in ctx.lines))
                else:
                    self.assertEqual(json.loads(led.get_kv("schedule_holds")), {
                        "at": iso(NOW), "holds": [{"kind": "paused"}] if paused else
                        [{"kind": "lease_host", "project": "a"}]})

    def test_snapshot_is_bounded_and_dry_run_does_not_write(self):
        ctx, led = mk_ctx({})
        self.addCleanup(led.close)
        for n in range(205):
            ctx.hold("deps", project="a", number=n, on=[300])
        scheduler.record_holds(ctx)
        self.assertIsNone(led.get_kv("schedule_holds"))
        ctx.dry_run = False
        scheduler.record_holds(ctx)
        self.assertEqual(len(json.loads(led.get_kv("schedule_holds"))["holds"]), 200)


if __name__ == "__main__":
    unittest.main()
