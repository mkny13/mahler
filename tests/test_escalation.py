"""Tests for tier escalation on retries, overrun heuristics, and sizing calibration."""

import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler import config, finalize, platforms, router, scheduler, ship, sync, tick
from mahler.ledger import Ledger, iso


class FakeGH:
    def __init__(self):
        self.comments = []

    def issue_state(self, n):
        return "OPEN"

    def failed_run_log(self, branch, tail=30):
        return 1, "CI failed"

    def comment(self, n, body):
        self.comments.append(body)


class StubCtx:
    def __init__(self, cfg, led, gh=None):
        self.cfg, self.led, self._gh = cfg, led, gh or FakeGH()
        self.lines, self.pings = [], []
        self.dry_run = False
        self.burst_lines = None

    def policy(self, project):
        return {"path": "/nonexistent", "base": "main", "run_timeout_minutes": 30,
                "progress_timeout_minutes": 15, "auto_lease_minutes": 30,
                "yield_grace_seconds": 30, "max_attempts": 5,
                "verify_timeout_minutes": 30, "capacity_wait_max_hours": 2}

    def gh(self, project):
        return self._gh

    def say(self, msg):
        self.lines.append(msg)

    def ping(self, title, message="", project=None, number=None,
             priority="default", tags=""):
        self.pings.append((title, message, priority))


class TierEscalationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.log_path = os.path.join(d, "agent.log")
        with open(self.log_path, "w") as fh:
            fh.write("failed\n")
        self.status_path = os.path.join(d, "exit")
        with open(self.status_path, "w") as fh:
            fh.write("1")

        self.led = Ledger(":memory:")
        self.addCleanup(self.led.close)

        cfg = copy.deepcopy(config.DEFAULTS)
        self.ctx = StubCtx(cfg, self.led)

    def tearDown(self):
        self.tmp.cleanup()

    def _end_run(self, project, number, platform, started_at=None):
        now = self.led.now()
        started = iso(now - timedelta(minutes=started_at)) if started_at else iso(now)
        run_id = self.led.create_run(
            project=project, number=number, role="build", platform=platform, epoch=1,
            log_path=self.log_path, status_path=self.status_path,
            worktree=self.tmp.name)
        self.led.con.execute("UPDATE runs SET started_at=? WHERE id=?", (started, run_id))
        finalize.finalize(self.ctx, self.led.run(run_id))

    def test_capacity_interrupted_fixes_charge_each_head_only_once(self):
        for trigger, number in (("ci", 95), ("review", 96)):
            with self.subTest(trigger=trigger):
                self.led.upsert_item("p", number, state="verifying", title="fix me",
                                     labels='["size:s"]', branch="b", pr=10,
                                     attempts=1, esc_fails=0, esc_tier=0)

                def attempt(sha):
                    args = (self.ctx, "p", self.led.item("p", number), 10,
                            {"headRefName": "b", "headRefOid": sha})
                    if trigger == "ci":
                        ship._red_ci(*args)
                    else:
                        ship._review_triggered_fix(*args, "blocking findings")

                with mock.patch("mahler.ship.start", return_value=False) as start, \
                     mock.patch("mahler.router.pick_for_project",
                                return_value=("agy-claude", [])):
                    for _ in range(3):
                        attempt("same-head")
                    self.assertEqual(self.led.item("p", number)["attempts"], 1)
                    start.return_value = True
                    attempt("same-head")
                    for reason in (*finalize.CAPACITY_STOPS, "quota"):
                        run_id = self.led.create_run(
                            project="p", number=number, role="fix",
                            platform="agy-claude", epoch=1, status="ended")
                        self.led.update_run(run_id, stop_reason=reason)
                        attempt("same-head")
                    item = self.led.item("p", number)
                    self.assertEqual((item["state"], item["attempts"], item["esc_fails"]),
                                     ("verifying", 2, 1))
                    for sha in ("next-head", "third-head", "fourth-head"):
                        attempt(sha)
                    item = self.led.item("p", number)
                    self.assertEqual((item["state"], item["attempts"]), ("failed", 5))

    def test_completed_genuine_fix_clears_cycle_dedup(self):
        for reason in (None, "timeout"):
            with self.subTest(reason=reason):
                key = "red:p#97:10:same-head"
                self.led.set_kv(key, iso(self.led.now() - timedelta(minutes=1)))
                self.led.set_kv(f"{key}:charged", "1")
                run_id = self.led.create_run(
                    project="p", number=97, role="fix",
                    platform="agy-claude", epoch=1, status="ended",
                    outcome="exit 1")
                self.led.update_run(run_id, stop_reason=reason)
                ship._clear_charged_if_fix_completed(self.led, "p", 97, key)
                self.assertIsNone(self.led.get_kv(key))
                self.assertIsNone(self.led.get_kv(f"{key}:charged"))
                self.led.con.execute("DELETE FROM runs WHERE id=?", (run_id,))

    def test_fix_that_never_started_keeps_cycle_dedup(self):
        key = "red:p#97:10:same-head"
        self.led.set_kv(key, iso(self.led.now() - timedelta(minutes=1)))
        self.led.set_kv(f"{key}:charged", "1")
        for outcome in ("launch failed: temporarily unavailable", "not claimed", None):
            with self.subTest(outcome=outcome):
                run_id = self.led.create_run(
                    project="p", number=97, role="fix",
                    platform="agy-claude", epoch=1, status="ended",
                    outcome=outcome)
                ship._clear_charged_if_fix_completed(self.led, "p", 97, key)
                self.assertIsNotNone(self.led.get_kv(key))
                self.assertEqual(self.led.get_kv(f"{key}:charged"), "1")
                self.led.con.execute("DELETE FROM runs WHERE id=?", (run_id,))

    def test_genuine_fix_failures_on_unchanged_head_keep_escalating(self):
        for trigger, number in (("ci", 97), ("review", 98)):
            with self.subTest(trigger=trigger):
                self.led.upsert_item("p", number, state="verifying", title="fix me",
                                     labels='["size:s"]', branch="b", pr=10,
                                     attempts=1, esc_fails=0, esc_tier=0)
                now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]

                def attempt():
                    args = (self.ctx, "p", self.led.item("p", number), 10,
                            {"headRefName": "b", "headRefOid": "same-head"})
                    if trigger == "ci":
                        ship._red_ci(*args)
                    else:
                        ship._review_triggered_fix(*args, "blocking findings")

                def complete_fix():
                    now[0] += timedelta(minutes=1)
                    self.led.create_run(
                        project="p", number=number, role="fix", platform="kilo",
                        epoch=1, status="ended", stop_reason="timeout",
                        outcome="exit 1")
                    now[0] += timedelta(minutes=1)

                with mock.patch.object(self.led, "now", side_effect=lambda: now[0]), \
                     mock.patch("mahler.ship.start", return_value=True), \
                     mock.patch("mahler.router.pick_for_project",
                                return_value=("agy-claude", [])):
                    attempt()
                    self.assertEqual((self.led.item("p", number)["attempts"],
                                      self.led.item("p", number)["esc_fails"]), (2, 1))

                    complete_fix()
                    attempt()
                    item = self.led.item("p", number)
                    self.assertEqual((item["attempts"], item["esc_fails"], item["esc_tier"]),
                                     (3, 0, 2))

                    complete_fix()
                    attempt()
                    item = self.led.item("p", number)
                    self.assertEqual((item["attempts"], item["esc_fails"], item["esc_tier"]),
                                     (4, 1, 2))

                    complete_fix()
                    attempt()
                    item = self.led.item("p", number)
                    self.assertEqual((item["state"], item["attempts"], item["esc_fails"],
                                      item["esc_tier"]), ("failed", 5, 0, 3))

    def test_review_and_ci_escalate_from_builder_not_reviewer(self):
        for failure in ("review", "ci"):
            with self.subTest(failure=failure):
                self.led.upsert_item("p", 90, state="verifying", title="fix me",
                                     labels='["size:s"]', branch="b", pr=10,
                                     esc_fails=1, esc_tier=0)
                for role, platform in (("build", "kilo"), ("review", "claude")):
                    self.led.create_run(project="p", number=90, role=role,
                                        platform=platform, epoch=1, status="ended")
                with mock.patch("mahler.ship.start", return_value=True), \
                     mock.patch("mahler.router.pick_for_project", return_value=("agy-claude", [])):
                    args = (self.ctx, "p", self.led.item("p", 90), 10,
                            {"headRefName": "b", "headRefOid": failure})
                    if failure == "review":
                        ship._review_triggered_fix(*args, "blocking findings")
                    else:
                        ship._red_ci(*args)
                self.assertEqual(self.led.item("p", 90)["esc_tier"], 2)

    def test_all_failure_paths_stay_at_route_cap(self):
        for failure in ("review", "ci", "retry", "overrun"):
            with self.subTest(failure=failure):
                self.led.upsert_item("p", 91, state="verifying", title="fix me",
                                     labels='["size:s"]', branch="b", pr=10,
                                     attempts=0, esc_fails=1, esc_tier=3)
                self.led.create_run(project="p", number=91, role="build",
                                    platform="claude", epoch=1, status="ended")
                with mock.patch("mahler.ship.start", return_value=True), \
                     mock.patch("mahler.router.pick_for_project", return_value=("claude", [])):
                    item = self.led.item("p", 91)
                    args = (self.ctx, "p", item, 10,
                            {"headRefName": "b", "headRefOid": failure})
                    if failure == "review":
                        ship._review_triggered_fix(*args, "blocking findings")
                    elif failure == "ci":
                        ship._red_ci(*args)
                    else:
                        finalize.retry_or_fail(self.ctx, "p", 91, item, None, "failed",
                                               platform="kilo" if failure == "overrun" else "claude",
                                               duration_mins=12)
                self.assertEqual(self.led.item("p", 91)["esc_tier"], 3)

    def test_cap_respects_routes_accounts_sizes_and_enabled(self):
        cfg = self.ctx.cfg
        cfg["platforms"] = {
            "small": {"enabled": True, "tier": 1, "max_size": "s"},
            "medium": {"enabled": True, "tier": 3},
            "large": {"enabled": True, "tier": 4, "min_size": "l"},
            "disabled": {"enabled": False, "tier": 5},
            "work": {"enabled": True, "tier": 6, "account": "work"},
            "unrouted": {"enabled": True, "tier": 7},
        }
        cfg["routing"]["build"] = ["small", "medium", "large", "disabled", "work"]
        self.assertEqual(router.cap_escalation(cfg, {}, 8, "s"), 3)
        self.assertEqual(router.cap_escalation(cfg, {}, 8, "l"), 3)
        self.assertEqual(router.cap_escalation(cfg, {}, 8, "l", role="build"), 4)
        pol = {"accounts": ["personal", "work"], "account_mode": "priority",
               "routing": {"build": ["small", "work"]}}
        self.assertEqual(router.cap_escalation(cfg, pol, 8, "s"), 6)
        pol["routing"]["build"] = ["small"]
        self.assertEqual(router.cap_escalation(cfg, pol, 8, "s"), 1)
        self.assertEqual(router.cap_escalation(cfg, pol, 8, "m"), 0)

    def test_waiting_fix_uses_capacity_backstop_without_recounting(self):
        for failure in ("review", "ci"):
            with self.subTest(failure=failure):
                now = self.led.now()
                self.led.upsert_item("p", 92, state="verifying", title="fix me",
                                     labels='["size:m"]', branch="b", pr=10,
                                     attempts=0, esc_fails=0, esc_tier=4)
                view = {"headRefName": "b", "headRefOid": failure}
                def step():
                    self.led.claim("p", 92, "conductor", "auto", 10)
                    args = (self.ctx, "p", self.led.item("p", 92), 10, view)
                    if failure == "review":
                        ship._review_triggered_fix(*args, "findings")
                    else:
                        ship._red_ci(*args)
                with mock.patch("mahler.router.pick_for_project", return_value=(None, ["no quota"])):
                    step()
                    self.assertIsNone(self.led.lease("p", 92))
                    self.assertEqual(self.led.item("p", 92)["state"], "verifying")
                    self.assertEqual(self.led.item("p", 92)["esc_tier"], 3)
                    count = len(self.ctx.pings)
                    with mock.patch.object(self.led, "now", return_value=now + timedelta(minutes=31)):
                        step()
                    self.assertEqual(self.led.item("p", 92)["state"], "verifying")
                    self.assertEqual(len(self.ctx.pings), count)
                    with mock.patch.object(self.led, "now", return_value=now + timedelta(hours=2, seconds=1)):
                        step()
                item = self.led.item("p", 92)
                self.assertEqual(item["state"], "needs_you")
                self.assertEqual(item["esc_fails"], 1)
                self.assertEqual(item["attempts"], 0)
                self.assertIn("no quota", item["question"])
                self.assertIsNone(self.led.lease("p", 92))
                self.assertTrue(self.ctx.pings[-1][0].startswith("Fix capacity wait expired"))

    def test_waiting_review_fix_resumes_when_capacity_returns(self):
        self.led.upsert_item("p", 93, state="verifying", title="fix me",
                             labels='["size:m"]', branch="b", pr=10)
        view = {"headRefName": "b", "headRefOid": "sha"}
        with mock.patch("mahler.router.explore_for_project", return_value=None), \
             mock.patch("mahler.router.pick_for_project", return_value=(None, ["busy"])):
            ship._review_triggered_fix(self.ctx, "p", self.led.item("p", 93),
                                       10, view, "findings")
        with mock.patch("mahler.router.explore_for_project", return_value=None), \
             mock.patch("mahler.router.pick_for_project", return_value=("claude", [])), \
             mock.patch("mahler.ship.start", return_value=True) as start:
            ship._review_triggered_fix(self.ctx, "p", self.led.item("p", 93),
                                       10, view, "findings")
        start.assert_called_once()
        self.assertIn("findings", start.call_args.kwargs["context"])
        self.assertEqual(self.led.item("p", 93)["attempts"], 1)
        self.assertEqual(self.led.item("p", 93)["esc_fails"], 1)

    def test_two_failures_on_tier_1_escalates_to_tier_2(self):
        # item starts at esc_tier=0, esc_fails=0
        self.led.upsert_item("p", 1, title="test item", labels=json.dumps(["size:s"]))
        item = self.led.item("p", 1)
        self.assertEqual(item["esc_tier"], 0)
        self.assertEqual(item["esc_fails"], 0)

        # 1st failure on kilo (tier 1)
        self._end_run("p", 1, "kilo")
        item = self.led.item("p", 1)
        self.assertEqual(item["esc_tier"], 0)
        self.assertEqual(item["esc_fails"], 1)

        # 2nd failure on kilo (tier 1) -> escalates to tier 2
        self._end_run("p", 1, "kilo")
        item = self.led.item("p", 1)
        self.assertEqual(item["esc_tier"], 2)
        self.assertEqual(item["esc_fails"], 0)

    def test_two_failures_on_tier_2_escalates_to_tier_3(self):
        self.led.upsert_item("p", 2, title="test item", labels=json.dumps(["size:m"]))
        # Fail twice on agy-claude (tier 2)
        self._end_run("p", 2, "agy-claude")
        self._end_run("p", 2, "agy-claude")
        item = self.led.item("p", 2)
        self.assertEqual(item["esc_tier"], 3)
        self.assertEqual(item["esc_fails"], 0)

    def test_overrun_heuristic_escalates_size_s_after_10_minutes_on_tier_1(self):
        self.led.upsert_item("p", 3, title="overrun task", labels=json.dumps(["size:s"]))
        # Failed run on kilo (tier 1) that ran for 12 minutes
        self._end_run("p", 3, "kilo", started_at=12)
        item = self.led.item("p", 3)
        self.assertEqual(item["esc_tier"], 2)
        self.assertEqual(item["esc_fails"], 0)

    def test_no_attempt_does_not_increment_esc_fails(self):
        self.led.upsert_item("p", 4, title="quota task", labels=json.dumps(["size:s"]))
        run_id = self.led.create_run(
            project="p", number=4, role="build", platform="kilo", epoch=1,
            log_path=self.log_path, status_path=self.status_path,
            worktree=self.tmp.name)
        self.led.update_run(run_id, stop_reason="quota")
        finalize.finalize(self.ctx, self.led.run(run_id))
        item = self.led.item("p", 4)
        self.assertEqual(item["esc_tier"], 0)
        self.assertEqual(item["esc_fails"], 0)

    def test_go_resets_esc_tier_and_esc_fails(self):
        self.led.upsert_item("p", 5, title="retry task", esc_tier=3, esc_fails=1)
        sync._apply_instruction(self.ctx, "p", self.led.item("p", 5), "go", None)
        item = self.led.item("p", 5)
        self.assertEqual(item["esc_tier"], 0)
        self.assertEqual(item["esc_fails"], 0)

    def test_schedule_respects_esc_tier(self):
        now = self.led.now()
        later = iso(now + timedelta(hours=6))
        for p in ("kilo", "agy-claude", "claude"):
            self.led.record_usage(p, "5h", 10.0, later)
            self.led.record_usage(p, "weekly", 10.0, later)

        # Task 6 has esc_tier=2, size:s
        self.led.upsert_item("p", 6, title="escalated task", state="ready",
                             labels=json.dumps(["size:s"]), esc_tier=2)

        # Schedule should pick agy-claude (tier 2), not kilo (tier 1)
        projects = [{"name": "p", "repo": "o/p", "max_parallel": 1, "path": self.tmp.name}]
        started = []
        with mock.patch("mahler.tick.start", side_effect=lambda *args, **kw: started.append(args[4]) or True), \
             mock.patch.object(platforms, "available", return_value=True):
            tick.schedule(self.ctx, projects)
        self.assertEqual(started, ["agy-claude"])

    def test_schedule_elevates_meta_programming_to_size_m_and_tier_2(self):
        now = self.led.now()
        later = iso(now + timedelta(hours=6))
        for p in ("kilo", "agy-claude"):
            self.led.record_usage(p, "5h", 10.0, later)
            self.led.record_usage(p, "weekly", 10.0, later)

        # Title mentions recipes/sort.md, labeled size:s
        self.led.upsert_item("p", 7, title="fix recipes/sort.md prompt", state="ready",
                             labels=json.dumps(["size:s"]))

        projects = [{"name": "p", "repo": "o/p", "max_parallel": 1, "path": self.tmp.name}]
        started = []
        with mock.patch("mahler.tick.start", side_effect=lambda *args, **kw: started.append(args[4]) or True), \
             mock.patch.object(platforms, "available", return_value=True):
            tick.schedule(self.ctx, projects)
        # Even though labeled size:s, risk_min_tier elevates min_tier to 2 and size to m,
        # so kilo (tier 1, max_size s) cannot take it, agy-claude takes it.
        self.assertEqual(started, ["agy-claude"])

    def test_red_ci_escalates_tier_after_two_failures(self):
        # Item starts at esc_tier=0, esc_fails=0
        self.led.upsert_item("p", 8, title="ci test", labels=json.dumps(["size:s"]), branch="b")
        # Record a previous run on kilo (tier 1)
        self.led.create_run(project="p", number=8, role="build", platform="kilo", epoch=1, status="ended")
        view = {"headRefName": "b", "headRefOid": "sha1"}
        item = self.led.item("p", 8)

        # 1st red CI
        with mock.patch("mahler.tick.start", return_value=True):
            ship._red_ci(self.ctx, "p", item, 10, view)
        item = self.led.item("p", 8)
        self.assertEqual(item["esc_tier"], 0)
        self.assertEqual(item["esc_fails"], 1)

        # 2nd red CI -> escalates to tier 2
        view2 = {"headRefName": "b", "headRefOid": "sha2"}
        with mock.patch("mahler.tick.start", return_value=True):
            ship._red_ci(self.ctx, "p", item, 10, view2)
        item = self.led.item("p", 8)
        self.assertEqual(item["esc_tier"], 2)
        self.assertEqual(item["esc_fails"], 0)


if __name__ == "__main__":
    unittest.main()
