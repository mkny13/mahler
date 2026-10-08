"""Daily digest (mahler#6): the text builder is pure; the send is kv-gated."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from mahler import digest
from mahler.ledger import Ledger, iso


class Clock:
    def __init__(self, t=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)):
        self.t = t

    def __call__(self):
        return self.t


def seeded_ledger(clock=None):
    """A ledger with one completed item, one needs_you, one handoff, one usage row."""
    clock = clock or Clock()
    led = Ledger(":memory:", clock=clock)
    led.upsert_item("mahler", 6, title="Daily digest ping")
    led.upsert_item("mahler", 8, title="Setup errors")
    led.set_state("mahler", 8, "needs_you", "you asked")
    old = clock.t - timedelta(hours=30)           # outside the 24h window
    led.q("INSERT INTO events (at, project, number, kind, detail) VALUES (?,?,?,?,?)",
          (iso(old), "mahler", 9, "state", "working -> done (old news)"))
    led.set_state("mahler", 6, "done", "merged")
    led.event("state", "mahler", 8, "ready -> ready (handoff (quota))")
    led.record_usage("claude", "5h", 42.0)
    led.record_usage("claude", "weekly", 10.5)
    return led


class FormatTests(unittest.TestCase):
    def data(self):
        led = seeded_ledger()
        self.addCleanup(led.close)
        return digest.gather(led, {"platforms": {"claude": {"enabled": True}}},
                             since=datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc),
                             now=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc))

    def test_shipments_never_create_owner_waits_or_nags(self):
        clock = Clock()
        led = Ledger(":memory:", clock=clock)
        self.addCleanup(led.close)
        for n, state in enumerate(("shipped", "shipped", "needs_you", "failed"), 1):
            led.upsert_item("p", n, title=f"Change {n}")
            led.set_state("p", n, state)
        for n in (1, 2):
            led.add_uat("p", n, n + 10, "sha", f"Change {n}", "Legacy checklist")
        data = digest.gather(led, {}, since=clock.t)
        self.assertEqual([(w["number"], w["state"]) for w in data["waiting"]],
                         [(3, "needs_you"), (4, "failed")])
        self.assertEqual(data["completed"], [])
        text = digest.format_digest(data)
        self.assertIn("Waiting on you (2):", text)
        for absent in ("Change 1", "Change 2", "UAT", "Ready to test", "verdict", "backlog"):
            self.assertNotIn(absent, text)

    def test_automated_and_quiet_completion_roll_up_as_completed(self):
        clock = Clock()
        led = Ledger(":memory:", clock=clock)
        self.addCleanup(led.close)
        merged = iso(clock.t)
        for n in (1, 2):
            led.upsert_item("p", n, title=f"Change {n}")
            led.set_state("p", n, "shipped")
            led.add_uat("p", n, n + 10, "sha", f"Change {n}", "")
        clock.t += timedelta(days=14)
        self.assertTrue(led.complete_quiet("p", 1, merged))
        self.assertTrue(led.accept_evidence("p", 2, dict(
            source="report:2", author="bot", created_at=iso(clock.t),
            kind="smoke", body="Smoke: PASS report=2")))
        data = digest.gather(led, {}, since=clock.t - timedelta(days=1))
        self.assertEqual({r["number"] for r in data["completed"]}, {1, 2})
        text = digest.format_digest(data)
        self.assertIn("Completed in the last 24h (2):", text)
        self.assertIn("Waiting on you (0):", text)
        self.assertNotIn("Shipped", text)

    def test_missing_scope_is_one_aggregate_line(self):
        led = Ledger(":memory:")
        self.addCleanup(led.close)
        cfg = {"defaults": {}, "projects": {
            "a": {"scope": "label", "scope_label": "custom"},
            "b": {"scope": "label", "scope_label": "mahler"},
            "all": {"scope": "all"},
            "disabled": {"scope": "label", "enabled": False}}}
        for name, count in (("a", 2), ("b", 3), ("all", 99), ("disabled", 99)):
            led.set_kv(f"missing_scope:{name}", str(count))
        data = digest.gather(led, cfg, since=led.now(),
                             local_now=datetime(2026, 10, 7))
        self.assertEqual(data["missing_scope_counts"], {"a": 2, "b": 3})
        text = digest.format_digest(data)
        self.assertEqual([line for line in text.splitlines() if "missing scope label" in line],
                         ["Open issues older than 24h missing scope label: 5 (latest full sync)"])
        self.assertNotIn("#", text)

    def test_includes_completed_with_titles(self):
        text = digest.format_digest(self.data())
        self.assertIn("Completed in the last 24h (1):", text)
        self.assertIn("- mahler#6 Daily digest ping", text)

    def test_includes_waiting_on_you(self):
        text = digest.format_digest(self.data())
        self.assertIn("Waiting on you (1):", text)
        self.assertIn("- mahler#8 [needs_you] Setup errors", text)

    def test_includes_handoffs(self):
        text = digest.format_digest(self.data())
        self.assertIn("Handoffs (1):", text)
        self.assertIn("handoff (quota)", text)

    def test_usage_lines(self):
        text = digest.format_digest(self.data())
        self.assertIn("- claude: 5h 42%, week 10%", text)

    def test_empty_day(self):
        data = {"completed": [], "waiting": [], "handoffs": [],
                "usage": [{"platform": "claude", "5h": None, "weekly": None}]}
        text = digest.format_digest(data)
        self.assertIn("- none", text)
        self.assertIn("- nothing", text)
        self.assertIn("claude: 5h no data, week no data", text)

    def test_gather_ignores_out_of_window_events(self):
        data = self.data()
        self.assertEqual([s["number"] for s in data["completed"]], [6])
        self.assertNotIn(9, [s["number"] for s in data["completed"]])


class GatingTests(unittest.TestCase):
    def setUp(self):
        self.led = Ledger(":memory:")
        self.addCleanup(self.led.close)
        self.nine = datetime(2026, 9, 12, 9, 0)          # local wall time

    def test_not_due_before_the_hour(self):
        early = datetime(2026, 9, 12, 7, 59)
        self.assertFalse(digest.should_send(self.led, early, hour=8))

    def test_due_once_then_not_again_the_same_day(self):
        self.assertTrue(digest.should_send(self.led, self.nine, hour=8))
        self.led.set_kv(digest.KV_KEY, "2026-09-12")
        self.assertFalse(digest.should_send(self.led, self.nine, hour=8))

    def test_due_again_the_next_day(self):
        self.led.set_kv(digest.KV_KEY, "2026-09-11")
        self.assertTrue(digest.should_send(self.led, self.nine, hour=8))


class Ctx:
    def __init__(self, cfg, led, dry_run=False):
        self.cfg, self.led, self.dry_run = cfg, led, dry_run
        self.lines = []

    def say(self, msg):
        self.lines.append(msg)


class MaybeSendTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(datetime(2026, 9, 12, 9, 30, tzinfo=timezone.utc))
        self.led = seeded_ledger(self.clock)
        self.cfg = {"platforms": {"claude": {"enabled": True}},
                    "digest": {"hour": 8}}
        self.ctx = Ctx(self.cfg, self.led)
        self.local = patch.object(digest, "_local_now",
                                  return_value=datetime(2026, 9, 12, 10, 30))
        self.local.start()
        self.addCleanup(self.local.stop)

    def test_sends_once_and_records_the_kv_key(self):
        with patch("mahler.digest.notify.send", return_value=True) as send:
            digest.maybe_send(self.ctx)
        self.assertEqual(send.call_count, 1)
        title, body = send.call_args[0][1:]
        self.assertEqual(title, digest.TITLE)
        self.assertIn("mahler#6 Daily digest ping", body)
        self.assertEqual(self.led.get_kv(digest.KV_KEY), "2026-09-12")
        # second tick the same day: no repeat
        with patch("mahler.digest.notify.send", return_value=True) as send:
            digest.maybe_send(self.ctx)
        self.assertEqual(send.call_count, 0)

    def test_failed_send_does_not_set_the_kv_key(self):
        with patch("mahler.digest.notify.send", return_value=False) as send:
            digest.maybe_send(self.ctx)
        self.assertEqual(send.call_count, 1)
        self.assertIsNone(self.led.get_kv(digest.KV_KEY))
        self.assertIn("will retry next tick", self.ctx.lines[-1])

    def test_dry_run_never_sends(self):
        self.ctx.dry_run = True
        with patch("mahler.digest.notify.send", return_value=True) as send:
            digest.maybe_send(self.ctx)
        self.assertEqual(send.call_count, 0)
        self.assertIsNone(self.led.get_kv(digest.KV_KEY))

    def test_a_crash_never_breaks_the_tick(self):
        with patch("mahler.digest.gather", side_effect=RuntimeError("boom")):
            digest.maybe_send(self.ctx)          # must not raise
        self.assertTrue(any("digest failed" in l for l in self.ctx.lines))




class WeeklyModelsTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
        self.led = Ledger(":memory:", clock=lambda: self.now)
        self.addCleanup(self.led.close)
        self.ctx = Ctx({}, self.led)
        self.number = 0

    def seed(self, size="s", role="build", outcome="DONE", cost=.04, model="Luna"):
        self.number += 1
        self.led.upsert_item("p", self.number)
        self.led.create_run(
            project="p", number=self.number, role=role, size=size, platform="slot",
            model=model, effort="low", epoch=1, status="ended",
            ended_at=iso(self.now), outcome=outcome, exit_code=0, cost_usd=cost)

    def test_all_unknown_size_representations_deliver(self):
        for size in ("", None, "s"):
            for role in ("build", "plan"):
                self.seed(size=size, role=role, outcome="READY" if role == "plan" else "DONE")
        with patch.object(digest, "_local_now", return_value=self.now), patch(
                "mahler.digest.notify.send", return_value=True) as send:
            digest.maybe_send(self.ctx)
        send.assert_called_once()
        body = send.call_args.args[2]
        self.assertEqual(body.count("- Top "), 6)
        self.assertIn("Top build/unknown size", body)
        self.assertIn("Top plan/unknown size", body)
        self.assertNotIn("Unpriced models", body)
        self.assertEqual(self.led.get_kv(digest.KV_KEY), "2026-09-21")

    def test_only_local_monday_includes_weekly_section(self):
        self.seed()
        for day in range(7):
            local = self.now + timedelta(days=day)
            # Deliberately hold the UTC ledger clock at Monday to test local gating.
            with self.subTest(day=day), patch.object(
                    digest, "_local_now", return_value=local), patch(
                    "mahler.digest.notify.send", return_value=True) as send:
                digest.maybe_send(self.ctx)
                send.assert_called_once()
                self.assertEqual("Weekly model scorecard:" in send.call_args.args[2], day == 0)

    def test_pending_excluded_only_warn_when_cost_missing(self):
        self.seed(role="plan", outcome="READY", model="priced-pending")
        self.seed(outcome="BLOCKED", model="priced-excluded")
        self.seed(role="plan", outcome="READY", cost=None, model="missing-pending")
        self.seed(outcome="BLOCKED", cost=None, model="missing-excluded")
        lines = digest.weekly_models(self.led, {})["lines"]
        self.assertEqual([line for line in lines if "Unpriced models:" in line],
                         ["- Unpriced models: missing-excluded, missing-pending"])

    def test_changes_snapshot_advances_only_after_successful_delivery(self):
        for _ in range(10):
            self.seed(model="winner", cost=.02)
            self.seed(model="loser", cost=.04)
        self.seed(model="loser", outcome="no status line", cost=.04)
        with patch.object(digest, "_local_now", return_value=self.now), patch(
                "mahler.digest.notify.send", return_value=False) as send:
            digest.maybe_send(self.ctx)
            self.assertIn("Newly good:", send.call_args.args[2])
            self.assertIn("Newly dominated:", send.call_args.args[2])
            self.assertIsNone(self.led.get_kv("weekly_model_scorecard"))
            self.assertIsNone(self.led.get_kv(digest.KV_KEY))
            send.return_value = True
            digest.maybe_send(self.ctx)
            self.assertIn("Newly good:", send.call_args.args[2])
            self.assertIsNotNone(self.led.get_kv("weekly_model_scorecard"))
        lines = digest.weekly_models(self.led, {})["lines"]
        self.assertFalse(any("Newly" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
