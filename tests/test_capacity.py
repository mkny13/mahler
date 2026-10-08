"""Compact capacity intervals, interval lifecycle, and pruning (mahler#734)."""

import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from mahler import capacity, config
from mahler.ledger import Ledger, iso

T0 = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


class IntervalTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.led = Ledger(":memory:", clock=self.clock)
        self.addCleanup(self.led.close)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.lines = []
        self.ctx = SimpleNamespace(cfg=self.cfg, led=self.led, dry_run=False,
                                   say=self.lines.append)

    def tick(self, *entries, dry_run=False):
        """One tick's observations: (project, number, role, blockers[, diag])."""
        self.ctx.dry_run = dry_run
        obs = capacity.Observer(self.ctx)
        for e in entries:
            project, number, role, blockers, *rest = e
            obs.observe(project, number, role, blockers=blockers,
                        diag=rest[0] if rest else None, size="m", required_tier=1)
        obs.flush()
        return obs

    def rows(self):
        return [dict(r) for r in self.led.capacity_intervals()]

    def test_unchanged_ticks_keep_one_interval(self):
        for _ in range(5):
            self.tick(("a", 1, "build", ["no_platform"]))
            self.clock.advance(seconds=60)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["first_seen"], T0.isoformat())
        self.assertEqual(rows[0]["last_seen"], (T0 + timedelta(seconds=240)).isoformat())
        self.assertEqual(rows[0]["open"], 1)

    def test_changed_blockers_split_the_interval(self):
        self.tick(("a", 1, "build", ["no_platform"]))
        self.clock.advance(seconds=60)
        self.tick(("a", 1, "build", ["area"]))
        rows = self.rows()
        self.assertEqual([r["end_reason"] for r in rows], ["changed", None])
        self.assertEqual(json.loads(rows[1]["blockers"]), ["area"])

    def test_platform_reason_change_splits_but_config_snapshot_is_kept(self):
        name = next(iter(self.cfg["platforms"]))
        self.tick(("a", 1, "build", ["no_platform"], {name: ["quota"]}))
        self.clock.advance(seconds=60)
        self.cfg["platforms"][name]["tier"] = 9           # later config change
        self.tick(("a", 1, "build", ["no_platform"], {name: ["quota"]}))
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        meta = json.loads(rows[0]["platforms"])[name]
        self.assertNotEqual(meta["tier"], 9)               # history is not rewritten
        self.tick(("a", 1, "build", ["no_platform"], {name: ["no_credit"]}))
        self.assertEqual(len(self.rows()), 2)

    def test_disappearance_closes_at_last_seen(self):
        self.tick(("a", 1, "build", ["no_platform"]))
        self.clock.advance(seconds=60)
        self.tick(("a", 1, "build", ["no_platform"]))
        self.clock.advance(seconds=60)
        self.tick()
        (row,) = self.rows()
        self.assertEqual((row["open"], row["end_reason"]), (0, "gone"))
        self.assertEqual(row["last_seen"], (T0 + timedelta(seconds=60)).isoformat())

    def test_gap_over_120_seconds_is_unknown_not_waiting(self):
        self.tick(("a", 1, "build", ["no_platform"]))
        self.clock.advance(hours=3)                        # daemon was down
        self.tick(("a", 1, "build", ["no_platform"]))
        first, second = self.rows()
        self.assertEqual((first["first_seen"], first["last_seen"]),
                         (T0.isoformat(), T0.isoformat()))   # zero observed span
        self.assertEqual(first["end_reason"], "gap")
        self.assertEqual(second["first_seen"], (T0 + timedelta(hours=3)).isoformat())

    def test_exactly_120_seconds_still_coalesces(self):
        self.tick(("a", 1, "build", ["no_platform"]))
        self.clock.advance(seconds=120)
        self.tick(("a", 1, "build", ["no_platform"]))
        self.assertEqual(len(self.rows()), 1)

    def test_launch_closes_the_interval_with_the_run(self):
        self.tick(("a", 1, "build", ["no_platform"]))
        self.clock.advance(seconds=60)
        obs = capacity.Observer(self.ctx)
        obs.observe("a", 1, "build", blockers=["no_platform"])
        obs.launched("a", 1, "build", 77, "kilo")
        obs.flush()
        (row,) = self.rows()
        self.assertEqual((row["open"], row["end_reason"], row["launch_run_id"],
                          row["launched_platform"]), (0, "launched", 77, "kilo"))
        self.assertEqual(row["last_seen"], (T0 + timedelta(seconds=60)).isoformat())

    def test_launch_after_gap_does_not_count_unobserved_time(self):
        self.tick(("a", 1, "build", ["no_platform"]))
        self.clock.advance(hours=3)
        obs = capacity.Observer(self.ctx)
        obs.launched("a", 1, "build", 77, "kilo")
        obs.flush()
        first, launched = self.rows()
        self.assertEqual((first["open"], first["end_reason"], first["last_seen"]),
                         (0, "gap", T0.isoformat()))
        self.assertEqual((launched["open"], launched["end_reason"],
                          launched["first_seen"], launched["last_seen"],
                          launched["launch_run_id"], launched["launched_platform"]),
                         (0, "launched", (T0 + timedelta(hours=3)).isoformat(),
                          (T0 + timedelta(hours=3)).isoformat(), 77, "kilo"))

    def test_launch_on_first_sight_records_a_closed_row(self):
        obs = capacity.Observer(self.ctx)
        obs.launched("a", 1, "fix", 5, "claude")
        obs.flush()
        (row,) = self.rows()
        self.assertEqual((row["role"], row["open"], row["launch_run_id"]), ("fix", 0, 5))

    def test_pause_is_its_own_closing_reason(self):
        self.tick(("a", 1, "build", ["no_platform"]))
        self.clock.advance(seconds=60)
        self.tick(("a", 1, "build", ["paused"]))
        self.assertEqual(self.rows()[0]["end_reason"], "paused")

    def test_observe_paused_preserves_shipping_waits(self):
        self.led.upsert_item("a", 10, state="verifying", labels='["size:s"]')
        self.led.set_kv("reviewfix-status:a#10", json.dumps({"state": "capacity_wait", "tier": 2}))
        obs = capacity.Observer(self.ctx)
        obs.observe_paused([{"name": "a"}])
        obs.flush()
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["project"], row["number"], row["role"]), ("a", 10, "fix"))
        self.assertEqual((row["size"], row["effective_size"], row["required_tier"]), ("s", "m", 2))
        self.assertEqual(json.loads(row["blockers"]), ["paused"])

    def test_dry_run_writes_nothing(self):
        self.tick(("a", 1, "build", ["no_platform"]), dry_run=True)
        self.assertEqual(self.rows(), [])

    def test_prune_drops_old_and_clamps_the_crossing_interval(self):
        self.tick(("a", 1, "build", ["x"]), ("a", 2, "build", ["x"]))
        self.clock.advance(seconds=60)
        self.tick(("a", 2, "build", ["x"]))           # #1 ends at T0, #2 at T0+60s
        self.clock.now = T0 + timedelta(days=90, seconds=30)
        self.led.capacity_prune(90)
        rows = self.rows()
        self.assertEqual([r["number"] for r in rows], [2])
        cutoff = (self.clock.now - timedelta(days=90)).isoformat()
        self.assertEqual(rows[0]["first_seen"], cutoff)
        self.assertEqual(rows[0]["last_seen"], (T0 + timedelta(seconds=60)).isoformat())

    def test_prune_keeps_an_interval_ending_exactly_on_the_cutoff(self):
        self.tick(("a", 1, "build", ["x"]))
        self.clock.now = T0 + timedelta(days=90)
        self.led.capacity_prune(90)
        self.assertEqual(len(self.rows()), 1)

    def test_flush_failure_is_reported_not_raised(self):
        obs = capacity.Observer(self.ctx)
        obs.observe("a", 1, "build", blockers=["x"])
        self.led.close()
        obs.flush()
        self.assertTrue(any("couldn't record capacity history" in l for l in self.lines))
        self.led = Ledger(":memory:", clock=self.clock)    # let cleanup close something

    def test_no_secrets_or_prompts_in_the_row(self):
        self.tick(("a", 1, "build", ["no_platform"]))
        self.assertEqual(set(self.rows()[0]), {
            "id", "project", "number", "role", "routing_role", "size", "effective_size",
            "required_tier", "first_seen", "last_seen", "open", "end_reason", "signature",
            "blockers", "platforms", "launch_run_id", "launched_platform"})


class CapacityReportTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.led = Ledger(":memory:", clock=self.clock)
        self.addCleanup(self.led.close)
        self.cfg = copy.deepcopy(config.DEFAULTS)

    def insert_interval(self, project, number, role, first_seen, last_seen, *,
                        open=0, end_reason=None, blockers=None, platforms=None,
                        launch_run_id=None, launched_platform=None, tier=1, size="m"):
        if blockers is None:
            blockers = ["no_platform"]
        if platforms is None:
            platforms = {"kilo": {"reasons": ["quota"], "cost": "free", "tier": tier, "kind": "kilo"}}
        self.led.con.execute(
            "INSERT INTO capacity_intervals (project, number, role, size, effective_size, "
            "required_tier, first_seen, last_seen, open, end_reason, signature, blockers, "
            "platforms, launch_run_id, launched_platform) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (project, number, role, size, size, tier, iso(first_seen), iso(last_seen),
             open, end_reason, "sig", json.dumps(blockers), json.dumps(platforms),
             launch_run_id, launched_platform)
        )

    def insert_run(self, run_id, platform, started_at, ended_at=None, *,
                   requests=None, request_buckets=None, request_coverage=None,
                   rate_limited=0, limit_hit=0):
        self.led.con.execute(
            "INSERT INTO runs (id, project, number, role, platform, epoch, status, "
            "started_at, ended_at, requests, request_buckets, request_coverage, "
            "rate_limited, limit_hit) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, "p", 1, "build", platform, 0, "ended",
             iso(started_at), iso(ended_at) if ended_at else None,
             requests, json.dumps(request_buckets) if isinstance(request_buckets, dict) else request_buckets,
             request_coverage, rate_limited, limit_hit)
        )

    def test_two_fully_overlapping_one_hour_waits_reports_one_hour(self):
        # Fixture with two fully overlapping one-hour waits reports 1.0 h, not 2.0 h
        t_start = T0
        t_end = T0 + timedelta(hours=1)
        self.insert_interval("a", 1, "build", t_start, t_end, tier=0)
        self.insert_interval("b", 2, "build", t_start, t_end, tier=0)
        self.clock.now = t_end

        rep = capacity.capacity_report(self.led, days=30, now=t_end)
        self.assertEqual(rep["tiers"][0]["hours"], 1.0)
        formatted = capacity.format_capacity_report(rep)
        self.assertIn("1.0 h", formatted)
        self.assertNotIn("2.0 h", formatted)

    def test_window_clipping_bounds_intervals(self):
        # Window clipping: intervals extending past window start or window end are clipped
        now = T0 + timedelta(days=30)
        win_start = now - timedelta(days=30)  # T0

        # Interval starting 2 hours before window_start and ending 1 hour after window_start
        self.insert_interval("a", 1, "build", win_start - timedelta(hours=2), win_start + timedelta(hours=1), tier=1)
        # Interval starting 1 hour before window_end and ending 2 hours after window_end
        self.insert_interval("b", 2, "build", now - timedelta(hours=1), now + timedelta(hours=2), tier=1)

        rep = capacity.capacity_report(self.led, days=30, now=now)
        # Expected: exactly 1.0 h from first interval + 1.0 h from second interval = 2.0 h
        self.assertEqual(rep["tiers"][1]["hours"], 2.0)

    def test_separate_accounts_union_wall_clock(self):
        # Separate accounts / issues union wall-clock time
        t1 = T0
        t2 = T0 + timedelta(hours=1)
        t3 = T0 + timedelta(minutes=30)
        t4 = T0 + timedelta(hours=1, minutes=30)
        self.insert_interval("proj_a", 1, "build", t1, t2, tier=1)
        self.insert_interval("proj_b", 2, "build", t3, t4, tier=1)

        rep = capacity.capacity_report(self.led, days=30, now=t4)
        self.assertEqual(rep["tiers"][1]["hours"], 1.5)

    def test_multiple_free_candidates_union_once(self):
        # Multiple free candidates in the same interval count once for that tier
        plats = {
            "kilo": {"reasons": ["quota"], "cost": "free", "tier": 1},
            "agy-gemini": {"reasons": ["no_credit"], "cost": "free", "tier": 1},
        }
        self.insert_interval("a", 1, "build", T0, T0 + timedelta(hours=1), platforms=plats, tier=1)
        rep = capacity.capacity_report(self.led, days=30, now=T0 + timedelta(hours=1))
        self.assertEqual(rep["tiers"][1]["hours"], 1.0)

    def test_gaps_exclude_unobserved_time(self):
        # Gaps over 120s are excluded from waiting time
        t1 = T0
        t2 = T0 + timedelta(minutes=30)
        t3 = T0 + timedelta(hours=3, minutes=30)
        t4 = T0 + timedelta(hours=4)
        self.insert_interval("a", 1, "build", t1, t2, end_reason="gap", tier=1)
        self.insert_interval("a", 1, "build", t3, t4, tier=1)

        rep = capacity.capacity_report(self.led, days=30, now=t4)
        # 30 mins + 30 mins = 1.0 h (the 3 hour gap is not waiting time)
        self.assertEqual(rep["tiers"][1]["hours"], 1.0)

    def test_unchanged_state_interval_duration(self):
        # Interval duration equals last_seen - first_seen
        t1 = T0
        t2 = T0 + timedelta(minutes=15)
        self.insert_interval("a", 1, "build", t1, t2, tier=1)

        rep = capacity.capacity_report(self.led, days=30, now=t2)
        self.assertEqual(rep["tiers"][1]["hours"], 0.25)

    def test_negative_fixtures_contribute_zero_opportunity_hours(self):
        t1 = T0
        t2 = T0 + timedelta(hours=1)
        now = t2

        cases = [
            # Size mismatch
            ("size_mismatch", ["no_platform"], {"kilo": {"reasons": ["size", "quota"], "cost": "free", "tier": 1}}),
            # Tier mismatch
            ("tier_mismatch", ["no_platform"], {"kilo": {"reasons": ["tier", "quota"], "cost": "free", "tier": 0}}),
            # Stale quota
            ("stale_quota", ["no_platform"], {"kilo": {"reasons": ["unknown"], "cost": "free", "tier": 1}}),
            # Generic hold
            ("generic_hold", ["no_platform"], {"kilo": {"reasons": ["hold"], "cost": "free", "tier": 1}}),
            # Platform busy slots
            ("platform_busy_slots", ["no_platform"], {"kilo": {"reasons": ["slots_busy", "quota"], "cost": "free", "tier": 1}}),
            # Item busy slots (project_slots)
            ("item_project_slots", ["project_slots"], {"kilo": {"reasons": ["quota"], "cost": "free", "tier": 1}}),
            # Item busy slots (global_slots)
            ("item_global_slots", ["global_slots"], {"kilo": {"reasons": ["quota"], "cost": "free", "tier": 1}}),
            # Dependencies
            ("dependencies", ["deps"], {"kilo": {"reasons": ["quota"], "cost": "free", "tier": 1}}),
            # Pause
            ("pause", ["paused"], {"kilo": {"reasons": ["quota"], "cost": "free", "tier": 1}}),
            # Restricted accounts
            ("restricted_accounts", ["no_platform"], {"kilo": {"reasons": ["account", "quota"], "cost": "free", "tier": 1}}),
            # Already-eligible platform
            ("already_eligible", ["no_platform"], {
                "kilo": {"reasons": ["quota"], "cost": "free", "tier": 1},
                "other": {"reasons": ["eligible"], "cost": "paid", "tier": 1},
            }),
        ]

        for name, blockers, platforms in cases:
            with self.subTest(case=name):
                led = Ledger(":memory:", clock=self.clock)
                led.con.execute(
                    "INSERT INTO capacity_intervals (project, number, role, size, effective_size, "
                    "required_tier, first_seen, last_seen, open, end_reason, signature, blockers, "
                    "platforms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("a", 1, "build", "m", "m", 1, iso(t1), iso(t2), 0, None, "sig",
                     json.dumps(blockers), json.dumps(platforms))
                )
                rep = capacity.capacity_report(led, days=30, now=now)
                hours = sum(t["hours"] for t in rep["tiers"].values())
                self.assertEqual(hours, 0.0, f"Case {name} contributed non-zero opportunity hours: {hours}")
                led.close()

    def test_launch_fixtures_distinct_runs_and_separation(self):
        t1 = T0
        t2 = T0 + timedelta(minutes=30)
        t3 = T0 + timedelta(hours=1)
        t4 = T0 + timedelta(hours=1, minutes=30)
        t5 = T0 + timedelta(hours=2)

        # Episode 1 for a#1: two changed rows, then launched run 101 on kilo
        self.insert_interval("a", 1, "build", t1, t2, end_reason="changed", tier=1)
        self.insert_interval("a", 1, "build", t2, t3, end_reason="launched", launch_run_id=101,
                             launched_platform="kilo", tier=1)

        # Episode 2 for a#1: repeated attempt after later failure, launched run 102
        self.insert_interval("a", 1, "build", t3, t4, end_reason="launched", launch_run_id=102,
                             launched_platform="kilo", tier=1)

        # Episode 3 for b#2: still waiting (open=1)
        self.insert_interval("b", 2, "build", t4, t5, open=1, tier=1)

        # Episode 4 for c#3: Claude diversion (launched run 200 on claude while kilo was blocked solely by quota)
        div_plats = {
            "claude": {"reasons": ["launched"], "cost": "paid", "kind": "claude", "tier": 1},
            "kilo": {"reasons": ["quota"], "cost": "free", "kind": "kilo", "tier": 1},
        }
        self.insert_interval("c", 3, "build", t5, t5, end_reason="launched", launch_run_id=200,
                             launched_platform="claude", platforms=div_plats, tier=1)

        rep = capacity.capacity_report(self.led, days=30, now=t5 + timedelta(minutes=10))

        # Distinct run IDs for tier 1 waiting episodes: 101 and 102 -> 2 runs
        self.assertEqual(rep["tiers"][1]["runs"], 2)
        self.assertEqual(rep["tiers"][1]["run_ids"], [101, 102])

        # b#2 is still waiting
        self.assertEqual(rep["tiers"][1]["still_waiting"], 1)

        # Claude diversion: run 200 is in diversions, not still waiting
        self.assertEqual(len(rep["diversions"]), 1)
        div = rep["diversions"][0]
        self.assertEqual(div["run_id"], 200)
        self.assertEqual(div["platform"], "claude")
        self.assertEqual(div["project"], "c")
        self.assertEqual(div["number"], 3)
        self.assertEqual(div["blocked_candidates"], [("kilo", 1)])

    def test_config_changes_do_not_alter_historical_classifications(self):
        # Historical interval has snapshotted platform data
        t1 = T0
        t2 = T0 + timedelta(hours=1)
        snap_plats = {"kilo": {"reasons": ["quota"], "cost": "free", "tier": 1}}
        self.insert_interval("a", 1, "build", t1, t2, platforms=snap_plats, tier=1)

        # Even if current config has kilo as paid tier 9
        self.cfg["platforms"]["kilo"]["cost_class"] = "paid"
        self.cfg["platforms"]["kilo"]["tier"] = 9

        rep = capacity.capacity_report(self.led, days=30, now=t2)
        self.assertIn(1, rep["tiers"])
        self.assertEqual(rep["tiers"][1]["hours"], 1.0)

    def test_peak_limit_evidence_does_not_leak_between_dates_or_models(self):
        for flag, label in (("rate_limited", "429 observed"), ("limit_hit", "limit hit")):
            with self.subTest(flag=flag):
                self.led.con.execute("DELETE FROM runs")
                for run_id, day, model, count, limited in (
                    (1, 0, "model-a", 10, True),
                    (2, 1, "model-a", 25, False),
                    (3, 0, "model-b", 30, False),
                    (4, 0, "model-c", 15, True),
                ):
                    start = T0 + timedelta(days=day)
                    self.insert_run(
                        run_id, "kilo", start, start + timedelta(hours=1),
                        requests=count,
                        request_buckets={start.date().isoformat(): {model: count}},
                        request_coverage="complete", **{flag: int(limited)})

                rep = capacity.capacity_report(self.led, days=30, now=T0 + timedelta(days=2))
                peaks = {p["model"]: p for p in rep["requests"]["peaks"]}
                self.assertEqual(peaks["model-a"]["peak_date"], "2026-09-13")
                for model in ("model-a", "model-b"):
                    self.assertFalse(peaks[model]["rate_limited"])
                    self.assertFalse(peaks[model]["limit_hit"])
                self.assertTrue(peaks["model-c"][flag])
                text = capacity.format_capacity_report(rep)
                self.assertIn("model-a: peak 25 requests on 2026-09-13 (limit evidence: none observed)", text)
                self.assertIn("model-b: peak 30 requests on 2026-09-12 (limit evidence: none observed)", text)
                self.assertIn(f"model-c: peak 15 requests on 2026-09-12 (limit evidence: {label})", text)

    def test_request_accounting_peaks_and_visible_unknown(self):
        t_base = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
        # Run 1: kilo on 2026-09-07, 10 requests
        self.insert_run(1, "kilo", t_base, t_base + timedelta(hours=1),
                        requests=10, request_buckets={"2026-09-07": {"kilo-a:free": 10}},
                        request_coverage="complete")
        # Run 2: kilo on 2026-09-08, 25 requests, 429 observed
        self.insert_run(2, "kilo", t_base + timedelta(days=1), t_base + timedelta(days=1, hours=1),
                        requests=25, request_buckets={"2026-09-08": {"kilo-a:free": 25}},
                        request_coverage="complete", rate_limited=1, limit_hit=1)
        # Run 3: kilo partial coverage, 5 requests (should NOT contribute to peak complete daily requests)
        self.insert_run(3, "kilo", t_base + timedelta(days=2), t_base + timedelta(days=2, hours=1),
                        requests=5, request_buckets={"2026-09-09": {"kilo-a:free": 5}},
                        request_coverage="partial")
        # Run 4: kilo unknown date, 3 requests
        self.insert_run(4, "kilo", t_base + timedelta(days=3), t_base + timedelta(days=3, hours=1),
                        requests=3, request_buckets={"unknown": {"kilo-a:free": 3}},
                        request_coverage="complete")
        # Run 5: kilo unknown model, 2 requests
        self.insert_run(5, "kilo", t_base + timedelta(days=4), t_base + timedelta(days=4, hours=1),
                        requests=2, request_buckets={"2026-09-11": {"unknown": 2}},
                        request_coverage="complete")
        # Run 6: cline with unsupported stream (requests is None)
        self.insert_run(6, "cline", t_base + timedelta(days=5), t_base + timedelta(days=5, hours=1))

        rep = capacity.capacity_report(self.led, days=30, now=t_base + timedelta(days=6))
        req = rep["requests"]

        # Peaks: kilo / kilo-a:free peak is 25 on 2026-09-08 with 429
        self.assertEqual(len(req["peaks"]), 1)
        peak = req["peaks"][0]
        self.assertEqual(peak["platform"], "kilo")
        self.assertEqual(peak["model"], "kilo-a:free")
        self.assertEqual(peak["peak_requests"], 25)
        self.assertEqual(peak["peak_date"], "2026-09-08")
        self.assertTrue(peak["rate_limited"])
        self.assertTrue(peak["limit_hit"])

        # Unknown and partial breakdowns
        self.assertEqual(req["partial"].get("kilo"), 5)
        self.assertEqual(req["unknown_day"].get("kilo"), 3)
        self.assertEqual(req["unknown_model"].get("kilo"), 2)

        # Cline is unsupported
        self.assertIn("cline", req["unsupported_streams"])

        # Formatting
        text = capacity.format_capacity_report(rep)
        self.assertIn("peak 25 requests on 2026-09-08", text)
        self.assertIn("429 observed", text)
        self.assertIn("5 requests with partial coverage", text)
        self.assertIn("3 requests with unknown date", text)
        self.assertIn("2 requests with unknown model", text)
        self.assertIn("cline:", text)
        self.assertIn("unsupported request stream", text)


if __name__ == "__main__":
    unittest.main()
