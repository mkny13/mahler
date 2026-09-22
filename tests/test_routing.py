"""Routing policy (DESIGN D8) and platform output parsing."""

import copy
import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from local_timezone import local_timezone

from mahler import cli, config, platforms, router, scheduler, tick, usage
from mahler.ledger import Ledger, iso

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


def led_with(**usage):
    led = Ledger(":memory:", clock=lambda: NOW)
    # reset 6h out: outside both burst lead windows (weekly_lead 5h, session_lead 60m)
    later = iso(NOW + timedelta(hours=6))
    for name, (five, weekly) in usage.items():
        led.record_usage(name, "5h", five, later)
        led.record_usage(name, "weekly", weekly, later)
    return led


class RouterTests(unittest.TestCase):
    cfg = config.DEFAULTS

    def test_free_tiers_build_first(self):
        led = led_with(**{"agy-claude": (10, 10), "agy-gemini": (10, 10), "claude": (5, 5)})
        self.assertEqual(router.pick(self.cfg, led, "build")[0], "agy-claude")

    def test_claude_plans(self):
        led = led_with(**{"agy-claude": (10, 10), "claude": (5, 5)})
        self.assertEqual(router.pick(self.cfg, led, "sort")[0], "claude")

    def test_claude_builds_only_under_its_reserve(self):
        led = led_with(**{"agy-claude": (95, 20), "agy-gemini": (20, 92), "claude": (59, 10)})
        self.assertEqual(router.pick(self.cfg, led, "build")[0], "claude")
        led = led_with(**{"agy-claude": (95, 20), "agy-gemini": (20, 92), "claude": (61, 10)})
        name, reasons = router.pick(self.cfg, led, "build")
        self.assertIsNone(name)
        self.assertTrue(any("claude: soft" in r for r in reasons))

    def test_fix_routes_like_a_build(self):
        """Red CI's fix runs use the build routing, not a separate table (D18, #18)."""
        led = led_with(**{"agy-claude": (10, 10), "agy-gemini": (10, 10), "claude": (5, 5)})
        self.assertEqual(router.pick(self.cfg, led, "fix")[0], "agy-claude")

    def test_weekly_reserve_applies_too(self):
        led = led_with(**{"agy-claude": (95, 95), "agy-gemini": (95, 95), "claude": (10, 71)})
        name, reasons = router.pick(self.cfg, led, "build")
        self.assertIsNone(name)
        self.assertTrue(any("claude: soft" in r for r in reasons),
                        f"claude should be rejected due to weekly reserve, reasons: {reasons}")

    def test_unknown_usage_counts_as_over_the_line(self):
        led = led_with(**{"agy-claude": (10, 10)})
        led.q("DELETE FROM usage WHERE platform='agy-claude' AND window='weekly'")
        name, reasons = router.pick(self.cfg, led, "build")
        self.assertNotEqual(name, "agy-claude")
        self.assertTrue(any("agy-claude: stale" in r for r in reasons),
                        f"agy-claude should be rejected as stale, reasons: {reasons}")

    def test_stale_samples_are_unknown(self):
        led = Ledger(":memory:", clock=lambda: NOW)
        old = iso(NOW - timedelta(minutes=30))
        for w in ("5h", "weekly"):
            led.record_usage("agy-claude", w, 1, iso(NOW + timedelta(hours=2)), sampled_at=old)
        self.assertEqual(router.usage_state(led, "agy-claude",
                                            self.cfg["platforms"]["agy-claude"])[0], "stale")

    def test_window_reset_since_sample_is_stale(self):
        # D8: a reading for a window that already rolled over says nothing
        # about the new window — re-probe rather than guess.
        led = Ledger(":memory:", clock=lambda: NOW)
        led.record_usage("agy-claude", "5h", 1, iso(NOW - timedelta(minutes=1)))
        led.record_usage("agy-claude", "weekly", 1, iso(NOW + timedelta(hours=2)))
        state, detail = router.usage_state(led, "agy-claude",
                                           self.cfg["platforms"]["agy-claude"])
        self.assertEqual(state, "stale")
        self.assertIn("5h: reset since sample", detail)

    def test_unreadable_sample_is_stale_not_a_crash(self):
        # D8: unknown counts as over the soft line. A malformed timestamp must
        # never raise out of the router — the tick must stay exception-safe.
        led = Ledger(":memory:", clock=lambda: NOW)
        for w in ("5h", "weekly"):
            led.record_usage("agy-claude", w, 1, iso(NOW + timedelta(hours=2)),
                             sampled_at="not-a-timestamp")
        state, detail = router.usage_state(led, "agy-claude",
                                           self.cfg["platforms"]["agy-claude"])
        self.assertEqual(state, "stale")
        self.assertIn("5h: unreadable sample", detail)

    def test_size_m_skips_s_only_platforms(self):
        # D8 rule 2: m fits Antigravity or Claude only; Copilot, Kilo and
        # Cline-free are capped at size s.
        led = led_with(**{"agy-claude": (95, 95), "agy-gemini": (95, 95),
                          "claude": (59, 10)})
        name, reasons = router.pick(self.cfg, led, "build", size="m")
        self.assertEqual(name, "claude")
        for small in ("cline-free", "copilot", "kilo"):
            self.assertTrue(any(f"{small}: only takes size:s" in r for r in reasons),
                            f"{small} should be size-capped, reasons: {reasons}")

    def test_size_m_build_skips_claude_opus(self):
        # claude-opus is min_size l (D21); a size:m build lands on plain
        # claude, not Opus — Opus builds only by escalation or on size:l.
        led = led_with(**{"agy-claude": (95, 95), "agy-gemini": (95, 95),
                          "claude": (59, 10)})
        name, reasons = router.pick(self.cfg, led, "build", size="m")
        self.assertEqual(name, "claude")
        self.assertTrue(any("claude-opus: requires size:l" in r for r in reasons),
                        f"claude-opus should require size:l, reasons: {reasons}")

    def test_size_l_skips_max_size_m_platforms(self):
        # D8 rule 2: l items are split first; nothing with max_size m takes
        # them — the first size-capable platform is the min_size l sibling.
        led = led_with(**{"claude": (5, 5)})
        led.record_usage("copilot-high", "monthly", 5.0,
                         iso(NOW + timedelta(hours=6)))
        name, reasons = router.pick(self.cfg, led, "build", size="l")
        self.assertEqual(name, "copilot-high")
        self.assertTrue(any("agy-claude: only takes size:m" in r for r in reasons))

    def test_hard_line(self):
        led = led_with(**{"agy-gemini": (91, 10)})
        self.assertEqual(router.usage_state(led, "agy-gemini",
                                            self.cfg["platforms"]["agy-gemini"])[0], "hard")

    def test_fmt_countdown_units(self):
        self.assertEqual(router.fmt_countdown(timedelta(minutes=35)), "35m")
        self.assertEqual(router.fmt_countdown(timedelta(hours=1, minutes=26)), "1h 26m")
        self.assertEqual(router.fmt_countdown(timedelta(days=2, hours=5)), "2d 5h")
        self.assertEqual(router.fmt_countdown(timedelta(minutes=-5)), "0m")   # already past

    def test_window_countdowns_reports_each_fresh_window(self):
        led = led_with(**{"claude": (55, 60)})   # led_with resets both windows 3h out
        chips = router.window_countdowns(led, "claude", self.cfg["platforms"]["claude"])
        self.assertEqual([label for label, _ in chips], ["5h", "wk"])
        self.assertTrue(all(cd.startswith("in ") for _, cd in chips))

    def test_window_countdowns_skips_rolled_over_or_missing_windows(self):
        led = Ledger(":memory:", clock=lambda: NOW)
        led.record_usage("claude", "5h", 50.0, iso(NOW - timedelta(minutes=1)))  # rolled over
        chips = router.window_countdowns(led, "claude", self.cfg["platforms"]["claude"])
        self.assertEqual(chips, [])   # weekly has no sample, 5h already reset

    def test_backing_off_detail_includes_countdown(self):
        led = Ledger(":memory:", clock=lambda: NOW)
        led.record_usage("cline-free", "5h", 100.0, iso(NOW + timedelta(minutes=35)))
        state, detail = router.usage_state(led, "cline-free", self.cfg["platforms"]["cline-free"])
        self.assertEqual(state, "hard")
        self.assertIn("(in 35m)", detail)

    def test_cline_is_unmetered_but_small_items_only(self):
        led = led_with(**{"agy-claude": (95, 95), "agy-gemini": (95, 95), "claude": (5, 5)})
        self.assertEqual(router.pick(self.cfg, led, "build", size="s")[0], "cline-free")
        self.assertEqual(router.pick(self.cfg, led, "build", size="m")[0], "claude")

    def test_cline_backs_off_after_a_quota_error(self):
        led = led_with(**{"agy-claude": (95, 95), "agy-gemini": (95, 95), "claude": (80, 5)})
        for name in ("cline-free", "kilo"):
            led.record_usage(name, "5h", 100.0, iso(NOW + timedelta(minutes=30)))
        led.record_usage("copilot", "monthly", 100.0, iso(NOW + timedelta(minutes=30)))
        name, reasons = router.pick(self.cfg, led, "build", size="s")
        self.assertIsNone(name)
        self.assertTrue(any("backing off" in r for r in reasons))

    def test_pin_overrides_order(self):
        led = led_with(**{"agy-claude": (10, 10), "agy-gemini": (10, 10)})
        self.assertEqual(router.pick(self.cfg, led, "build", pin="agy-gemini")[0], "agy-gemini")

    def test_claude_opus_routes_hard_tasks_unpinned(self):
        # size:l (hard task) skips agy-claude/gemini (max_size: m) and claude (max_size: m),
        # routing directly to claude-opus (min_size: l)
        led = led_with(**{"agy-claude": (10, 10), "agy-gemini": (10, 10),
                           "claude": (5, 5), "claude-opus": (5, 5)})
        self.assertEqual(router.pick(self.cfg, led, "build", size="l")[0], "claude-opus")

    def test_claude_opus_skips_small_and_medium_unpinned(self):
        # size:s and size:m skip claude-opus (min_size: l) to save expensive Opus quota;
        # agy-claude takes them first when available, and claude (Sonnet) builds when free tiers full
        led = led_with(**{"agy-claude": (10, 10), "agy-gemini": (10, 10),
                           "claude": (5, 5), "claude-opus": (5, 5)})
        self.assertEqual(router.pick(self.cfg, led, "build", size="m")[0], "agy-claude")
        self.assertEqual(router.pick(self.cfg, led, "build", size="s")[0], "agy-claude")

        # When free tiers exhausted, size:m routes to claude (Sonnet), not claude-opus
        led_spent = led_with(**{"agy-claude": (95, 95), "agy-gemini": (95, 95),
                                "claude": (5, 5), "claude-opus": (5, 5)})
        self.assertEqual(router.pick(self.cfg, led_spent, "build", size="m")[0], "claude")

    def test_claude_opus_pin_overrides_size_restrictions(self):
        # Explicit pin to claude-opus works even on a size:s or size:m item
        led = led_with(**{"agy-claude": (10, 10), "agy-gemini": (10, 10),
                           "claude": (5, 5), "claude-opus": (5, 5)})
        self.assertEqual(router.pick(self.cfg, led, "build", pin="claude-opus", size="s")[0],
                          "claude-opus")

    def test_claude_opus_model_flag(self):
        argv = platforms.claude_argv(self.cfg["platforms"]["claude-opus"], "hi", "wt", "build")
        self.assertIn("opus", argv)

    def test_kilo_is_unmetered_last_resort_builder(self):
        led = led_with(**{"agy-claude": (95, 95), "agy-gemini": (95, 95), "claude": (5, 5)})
        led.record_usage("cline-free", "5h", 100.0, iso(NOW + timedelta(minutes=30)))
        led.record_usage("copilot", "monthly", 100.0, iso(NOW + timedelta(minutes=30)))
        self.assertEqual(router.pick(self.cfg, led, "build", size="s")[0], "kilo")

    def test_copilot_is_metered_by_monthly_ai_credits(self):
        led = led_with(**{"agy-claude": (95, 95), "agy-gemini": (95, 95), "claude": (5, 5)})
        led.record_usage("cline-free", "5h", 100.0, iso(NOW + timedelta(minutes=30)))
        later = iso(NOW + timedelta(days=5))
        led.record_usage("copilot", "monthly", 10.0, later)
        self.assertEqual(router.pick(self.cfg, led, "build", size="s")[0], "copilot")
        led.record_usage("copilot", "monthly", 96.0, later)     # over its hard line
        name, reasons = router.pick(self.cfg, led, "build", size="s")
        self.assertEqual(name, "kilo")     # next in order now that copilot's ahead of kilo
        self.assertTrue(any("copilot: hard" in r for r in reasons))

    def test_codex_is_opt_in_and_metered(self):
        self.assertNotIn("codex", self.cfg["routing"]["build"])
        custom = copy.deepcopy(self.cfg)
        custom["routing"]["build"] = ["codex", "copilot"]
        led = Ledger(":memory:")
        self.addCleanup(led.close)
        self.assertEqual(router.usage_state(led, "codex", custom["platforms"]["codex"])[0],
                         "stale")

    def test_codex_argv_is_ephemeral_unattended_jsonl_in_worktree(self):
        with mock.patch.object(platforms, "codex_exe", return_value="/app/codex"):
            argv = platforms.codex_argv(
                self.cfg["platforms"]["codex"], "do it", "/tmp/wt", "build")
        self.assertEqual(argv[:2], ["/app/codex", "exec"])
        self.assertIn("--ephemeral", argv)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertIn("--json", argv)
        self.assertEqual(argv[argv.index("-C") + 1], "/tmp/wt")
        self.assertEqual(argv[-1], "do it")

    def test_kilo_defaults_to_a_free_model_route(self):
        # mahler#29: without an explicit :free route, every kilo run 402s on credits.
        self.assertTrue(self.cfg["platforms"]["kilo"]["model"].endswith("/free"))


class BurstTests(unittest.TestCase):
    cfg = config.DEFAULTS

    def _claude_usage(self, led, five_pct, weekly_pct, five_reset, weekly_reset):
        """Seed claude platform usage with given reset times (absolute datetimes)."""
        led.record_usage("claude", "5h", five_pct, iso(five_reset))
        led.record_usage("claude", "weekly", weekly_pct, iso(weekly_reset))
        # claude-opus mirrors the same account
        led.record_usage("claude-opus", "5h", five_pct, iso(five_reset))
        led.record_usage("claude-opus", "weekly", weekly_pct, iso(weekly_reset))
        return led

    def _led(self):
        return Ledger(":memory:", clock=lambda: NOW)

    def test_no_burst_when_resets_are_far_out(self):
        led = self._claude_usage(self._led(), 81, 81,
                                 NOW + timedelta(hours=4),
                                 NOW + timedelta(days=5))  # 4h > 1h session lead, 5d > 5h weekly
        self.assertIsNone(router.burst_status(self.cfg, led))

    def test_weekly_burst_raises_both_windows(self):
        # weekly reset in 2h (<= 5h lead), 5h reset in 30m (<= 1h session lead)
        led = self._claude_usage(self._led(), 81, 81,
                                 NOW + timedelta(minutes=30),
                                 NOW + timedelta(hours=2))
        lines = router.burst_status(self.cfg, led)
        self.assertIsNotNone(lines)
        self.assertEqual(lines["5h"], (90, 97))
        self.assertEqual(lines["weekly"], (90, 97))
        self.assertEqual(router.burst_kind(lines), "weekly")

    def test_session_burst_raises_only_5h(self):
        # 5h reset in 30m (<= 1h session lead), weekly reset in 8h (> 5h lead)
        led = self._claude_usage(self._led(), 81, 81,
                                 NOW + timedelta(minutes=30),
                                 NOW + timedelta(hours=8))
        lines = router.burst_status(self.cfg, led)
        self.assertIsNotNone(lines)
        self.assertIn("5h", lines)
        self.assertNotIn("weekly", lines)
        self.assertEqual(router.burst_kind(lines), "session")

    def test_burst_disabled_in_config(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["burst"]["enabled"] = False
        led = self._claude_usage(self._led(), 81, 81,
                                 NOW + timedelta(minutes=30),
                                 NOW + timedelta(hours=2))
        self.assertIsNone(router.burst_status(cfg, led))

    def test_burst_needs_fresh_samples(self):
        # sampled_at 20min ago → stale → no burst even though resets are close
        led = self._led()
        old = iso(NOW - timedelta(minutes=20))
        led.record_usage("claude", "5h", 81, iso(NOW + timedelta(minutes=30)), sampled_at=old)
        led.record_usage("claude", "weekly", 81, iso(NOW + timedelta(hours=2)), sampled_at=old)
        self.assertIsNone(router.burst_status(self.cfg, led))

    def test_burst_needs_known_reset_time(self):
        led = self._led()
        led.record_usage("claude", "5h", 81, None)  # no reset time
        led.record_usage("claude", "weekly", 81, None)
        self.assertIsNone(router.burst_status(self.cfg, led))

    def test_burst_lines_only_for_claude_kind(self):
        # At 85%, normal claude lines (soft 60, hard 70) → hard. With burst
        # lines (soft 90, hard 97) → ok.
        led = self._led()
        reset = iso(NOW + timedelta(minutes=30))
        for name in ("claude", "claude-opus"):
            led.record_usage(name, "5h", 85.0, reset)
            led.record_usage(name, "weekly", 85.0, reset)
        pconf = self.cfg["platforms"]["claude"]
        # without burst
        self.assertEqual(router.usage_state(led, "claude", pconf)[0], "hard")
        # with burst
        lines = {"5h": (90, 97), "weekly": (90, 97)}
        self.assertEqual(router.usage_state(led, "claude", pconf, burst_lines=lines)[0], "ok")

    def test_burst_does_not_affect_non_claude_platforms(self):
        # agy-claude soft=85, hard=90; 90% → hard. burst_lines must not change it.
        led = self._led()
        reset = iso(NOW + timedelta(minutes=30))
        led.record_usage("agy-claude", "5h", 90.0, reset)
        led.record_usage("agy-claude", "weekly", 90.0, reset)
        pconf = self.cfg["platforms"]["agy-claude"]
        lines = {"5h": (90, 97), "weekly": (90, 97)}
        self.assertEqual(router.usage_state(led, "agy-claude", pconf,
                                            burst_lines=lines)[0], "hard")

    def test_burst_hard_line_still_below_100(self):
        # 98% with burst hard 97 → hard (never into paid overage)
        led = self._led()
        reset = iso(NOW + timedelta(minutes=30))
        for name in ("claude", "claude-opus"):
            led.record_usage(name, "5h", 98.0, reset)
            led.record_usage(name, "weekly", 98.0, reset)
        lines = {"5h": (90, 97), "weekly": (90, 97)}
        self.assertEqual(router.usage_state(led, "claude",
                         self.cfg["platforms"]["claude"], burst_lines=lines)[0], "hard")

    def test_burst_build_order_moves_claude_first(self):
        self.assertEqual(router.burst_build_order(self.cfg),
                         ["claude-opus", "claude", "agy-claude", "agy-gemini",
                          "cline-free", "copilot", "copilot-high", "kilo"])

    def test_pick_without_burst_prefers_free_tier(self):
        led = led_with(**{"claude": (85, 85), "agy-claude": (10, 10), "agy-gemini": (10, 10)})
        self.assertEqual(router.pick(self.cfg, led, "build", size="m")[0], "agy-claude")

    def test_pick_with_burst_prefers_claude(self):
        # Claude at 85%: hard normally, ok under burst lines (soft 90).
        led = self._led()
        reset = iso(NOW + timedelta(minutes=30))
        for name in ("claude", "claude-opus"):
            led.record_usage(name, "5h", 85.0, reset)
            led.record_usage(name, "weekly", 85.0, reset)
        later6 = iso(NOW + timedelta(hours=6))
        for name in ("agy-claude", "agy-gemini"):
            led.record_usage(name, "5h", 10.0, later6)
            led.record_usage(name, "weekly", 10.0, later6)
        burst = {"5h": (90, 97), "weekly": (90, 97)}
        # burst put claude-opus first, but min_size:l skips it for size:m; then claude
        self.assertEqual(router.pick(self.cfg, led, "build", size="m",
                                     burst_lines=burst)[0], "claude")

    def test_burst_pick_skips_exhausted_claude(self):
        # Claude at 98% (hard even under burst), so falls through to free tiers.
        led = self._led()
        reset = iso(NOW + timedelta(minutes=30))
        for name in ("claude", "claude-opus"):
            led.record_usage(name, "5h", 98.0, reset)
            led.record_usage(name, "weekly", 98.0, reset)
        later6 = iso(NOW + timedelta(hours=6))
        for name in ("agy-claude", "agy-gemini"):
            led.record_usage(name, "5h", 10.0, later6)
            led.record_usage(name, "weekly", 10.0, later6)
        burst = {"5h": (90, 97), "weekly": (90, 97)}
        self.assertEqual(router.pick(self.cfg, led, "build", size="m",
                                     burst_lines=burst)[0], "agy-claude")

    def test_weekly_burst_at_92_soft(self):
        # In a weekly burst, 92% is under burst hard 97 → soft.
        led = self._led()
        reset = iso(NOW + timedelta(hours=2))
        for name in ("claude", "claude-opus"):
            led.record_usage(name, "5h", 92.0, reset)
            led.record_usage(name, "weekly", 92.0, reset)
        lines = router.burst_status(self.cfg, led)
        self.assertIsNotNone(lines)
        state = router.usage_state(led, "claude",
                                   self.cfg["platforms"]["claude"],
                                   burst_lines=lines)[0]
        self.assertEqual(state, "soft")

    def test_weekly_burst_at_97_hard(self):
        # In a weekly burst, 97% hits burst hard 97 → hard.
        led = self._led()
        reset = iso(NOW + timedelta(hours=2))
        for name in ("claude", "claude-opus"):
            led.record_usage(name, "5h", 97.0, reset)
            led.record_usage(name, "weekly", 97.0, reset)
        lines = router.burst_status(self.cfg, led)
        self.assertIsNotNone(lines)
        state = router.usage_state(led, "claude",
                                   self.cfg["platforms"]["claude"],
                                   burst_lines=lines)[0]
        self.assertEqual(state, "hard")

    def test_session_burst_45min_only_5h(self):
        # Session burst: 5h resets in 45min, weekly at 50% resets 8h out.
        # Only 5h gets burst lines; weekly stays at normal reserve.
        led = self._led()
        five_reset = iso(NOW + timedelta(minutes=45))
        weekly_reset = iso(NOW + timedelta(hours=8))
        for name in ("claude", "claude-opus"):
            led.record_usage(name, "5h", 20.0, five_reset)
            led.record_usage(name, "weekly", 50.0, weekly_reset)
        lines = router.burst_status(self.cfg, led)
        self.assertIsNotNone(lines)
        self.assertIn("5h", lines)
        self.assertNotIn("weekly", lines)
        self.assertEqual(router.burst_kind(lines), "session")
        # 5h with burst lines → ok; weekly at 50% under normal soft 70 → ok too
        claude_lines = {"5h": lines["5h"]}
        state, detail = router.usage_state(led, "claude",
                                            self.cfg["platforms"]["claude"],
                                            burst_lines=claude_lines)
        self.assertEqual(state, "ok")

    def test_plan_size_l_sorts_on_claude_opus_during_burst(self):
        # size:l planning normally hits hard (75% >= 70 hard). During a burst
        # claude-opus is under burst soft 90 → ok, so the plan picks it.
        led = self._led()
        reset = iso(NOW + timedelta(hours=2))
        for name in ("claude", "claude-opus"):
            led.record_usage(name, "5h", 75.0, reset)
            led.record_usage(name, "weekly", 75.0, reset)
        later6 = iso(NOW + timedelta(hours=6))
        for name in ("agy-claude", "agy-gemini"):
            led.record_usage(name, "5h", 10.0, later6)
            led.record_usage(name, "weekly", 10.0, later6)
        burst = {"5h": (90, 97), "weekly": (90, 97)}
        # Without burst, plan has no ok platform (claude-opus is hard)
        self.assertIsNone(router.pick(self.cfg, led, "plan", size="l")[0])
        # With burst lines, claude-opus is ok → plan picks it
        self.assertEqual(router.pick(self.cfg, led, "plan", size="l",
                                     burst_lines=burst)[0], "claude-opus")

    def test_burst_does_not_stop_running_run_below_burst_hard(self):
        # A running Claude run at 92% during weekly burst: soft (not stopped),
        # but hard without burst lines.
        led = self._led()
        reset = iso(NOW + timedelta(hours=2))
        for name in ("claude", "claude-opus"):
            led.record_usage(name, "5h", 92.0, reset)
            led.record_usage(name, "weekly", 92.0, reset)
        lines = router.burst_status(self.cfg, led)
        self.assertIsNotNone(lines)
        pconf = self.cfg["platforms"]["claude"]
        # 92% without burst → hard (70 hard, 80 weekly)
        self.assertEqual(router.usage_state(led, "claude", pconf)[0], "hard")
        # 92% with burst → soft (90 soft) — run continues
        state, _ = router.usage_state(led, "claude", pconf, burst_lines=lines)
        self.assertEqual(state, "soft")


class ParseTests(unittest.TestCase):
    def test_agy_usage(self):
        data = {"command": {"data": {"groups": [
            {"name": "Gemini Models", "buckets": [
                {"window": "weekly", "remaining_fraction": 0.74, "reset_time": "2026-09-18T21:25:17Z"},
                {"window": "5h", "remaining_fraction": 0.78, "reset_time": "2026-09-13T00:10:06Z"}]},
            {"name": "Claude and GPT models", "buckets": [
                {"window": "weekly", "remaining_fraction": 1}]}]}}}
        pools = platforms.parse_agy_usage(data)
        self.assertEqual(pools["Gemini Models"][0], ("weekly", 26.0, "2026-09-18T21:25:17Z"))
        self.assertEqual(pools["Claude and GPT models"][0][1], 0.0)

    def test_claude_rate_limit_event(self):
        ev = {"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed", "unifiedWindows": {
                "five_hour": {"utilization": 0.33, "resetsAt": 1789257600},
                "seven_day": {"utilization": 0.49, "resetsAt": 1789462800}}}}
        s = platforms.claude_samples_from_event(ev)
        self.assertEqual([x[:2] for x in s], [("5h", 33.0), ("weekly", 49.0)])

    def test_claude_overage_marks_the_window_exhausted(self):
        # mahler#136: isUsingOverage true means paid extra usage has started —
        # treat the reported window as exhausted, same as a rejection.
        ev = {"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed_warning", "rateLimitType": "seven_day", "utilization": 0.75,
            "isUsingOverage": True, "resetsAt": 1789462800, "unifiedWindows": {
                "seven_day": {"utilization": 0.75, "resetsAt": 1789462800}}}}
        s = platforms.claude_samples_from_event(ev)
        self.assertIn(("weekly", 100.0, "2026-09-15T09:00:00+00:00"), s)

        ev_false = {"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed_warning", "rateLimitType": "seven_day", "utilization": 0.75,
            "isUsingOverage": False, "resetsAt": 1789462800, "unifiedWindows": {
                "seven_day": {"utilization": 0.75, "resetsAt": 1789462800}}}}
        s_false = platforms.claude_samples_from_event(ev_false)
        self.assertNotIn(("weekly", 100.0, "2026-09-15T09:00:00+00:00"), s_false)

    def test_claude_overage_in_read_log(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "o.log")
            with open(p, "w") as fh:
                fh.write(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
                    "status": "allowed_warning", "rateLimitType": "five_hour", "utilization": 0.8,
                    "isUsingOverage": True, "resetsAt": 1789257600, "unifiedWindows": {
                        "five_hour": {"utilization": 0.8, "resetsAt": 1789257600}}}}) + "\n")
            r = platforms.read_log(p, "claude")
            self.assertTrue(r["overage"])
            self.assertTrue(r["quota_hit"])

            q = os.path.join(d, "n.log")
            with open(q, "w") as fh:
                fh.write(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
                    "status": "allowed", "rateLimitType": "five_hour", "utilization": 0.8,
                    "isUsingOverage": False, "resetsAt": 1789257600, "unifiedWindows": {
                        "five_hour": {"utilization": 0.8, "resetsAt": 1789257600}}}}) + "\n")
            r = platforms.read_log(q, "claude")
            self.assertFalse(r["overage"])
            self.assertFalse(r["quota_hit"])

    def test_logs_and_status_lines(self):
        with tempfile.TemporaryDirectory() as d:
            c = os.path.join(d, "c.log")
            with open(c, "w") as fh:
                fh.write(json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "working"}]}}) + "\n")
                fh.write(json.dumps({"type": "result", "subtype": "success",
                                     "result": "All done.\nSTATUS: MERGED #7"}) + "\n")
            r = platforms.read_log(c, "claude")
            self.assertTrue(r["ok"])
            self.assertEqual(platforms.status_line(r["final"]), ("MERGED", "#7"))

            g = os.path.join(d, "g.log")
            with open(g, "w") as fh:
                fh.write(json.dumps({"event": "step_update", "step_update": {
                    "text_delta": "STATUS: NEEDS-YOU "}}) + "\n")
                fh.write(json.dumps({"event": "result", "result": {
                    "status": "SUCCESS", "response": "STATUS: NEEDS-YOU Which colour?\n"}}) + "\n")
            r = platforms.read_log(g, "agy")
            self.assertEqual(platforms.status_line(r["final"]), ("NEEDS-YOU", "Which colour?"))
        self.assertEqual(platforms.status_line("no status here"), (None, None))
        self.assertEqual(platforms.status_line("**STATUS: READY**"), ("READY", ""))
        # mahler#15: a build run ends at STATUS: DONE <summary>
        self.assertEqual(platforms.status_line("STATUS: DONE wired the exporter"),
                         ("DONE", "wired the exporter"))
        self.assertEqual(platforms.status_line("all green\nSTATUS: DONE pushed; summary here"),
                         ("DONE", "pushed; summary here"))

    def test_copilot_log(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "p.log")
            with open(p, "w") as fh:
                fh.write(json.dumps({"type": "assistant.message",
                                     "data": {"content": "working on it"}}) + "\n")
                fh.write(json.dumps({"type": "assistant.message",
                                     "data": {"content": "STATUS: DONE shipped it"}}) + "\n")
                fh.write(json.dumps({"type": "result", "exitCode": 0,
                                     "usage": {"premiumRequests": 1}}) + "\n")
            r = platforms.read_log(p, "copilot")
            self.assertTrue(r["ok"])
            self.assertFalse(r["quota_hit"])
            self.assertEqual(platforms.status_line(r["final"]), ("DONE", "shipped it"))

    def test_copilot_quota_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "p.log")
            with open(p, "w") as fh:
                fh.write(json.dumps({"type": "error", "error": {
                    "message": "You have exceeded your premium request quota"}}) + "\n")
            r = platforms.read_log(p, "copilot")
            self.assertTrue(r["quota_hit"])

    def test_codex_log_and_quota_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "codex.log")
            with open(p, "w") as fh:
                fh.write(json.dumps({"type": "item.completed", "item": {
                    "type": "agent_message", "text": "STATUS: DONE shipped it"}}) + "\n")
                fh.write(json.dumps({"type": "turn.completed", "usage": {
                    "input_tokens": 100, "output_tokens": 10}}) + "\n")
            r = platforms.read_log(p, "codex")
            self.assertTrue(r["ok"])
            self.assertFalse(r["quota_hit"])
            self.assertEqual(platforms.status_line(r["final"]), ("DONE", "shipped it"))

            with open(p, "w") as fh:
                fh.write(json.dumps({"type": "turn.failed", "error": {
                    "message": "Model unavailable"}}) + "\n")
            r = platforms.read_log(p, "codex")
            self.assertFalse(r["ok"])
            self.assertFalse(r["quota_hit"])

            with open(p, "w") as fh:
                fh.write(json.dumps({"type": "turn.failed", "error": {
                    "message": "Usage limit exceeded; try again later"}}) + "\n")
            r = platforms.read_log(p, "codex")
            self.assertFalse(r["ok"])
            self.assertTrue(r["quota_hit"])

    def test_kilo_log(self):
        # Real shape from `kilo run ... --format json -m kilo/kilo-auto/free`
        # (mahler#29, verified after `kilo auth login`).
        with tempfile.TemporaryDirectory() as d:
            k = os.path.join(d, "k.log")
            with open(k, "w") as fh:
                fh.write(json.dumps({"type": "step_start", "part": {"type": "step-start"}}) + "\n")
                fh.write(json.dumps({"type": "text",
                                     "part": {"type": "text", "text": "STATUS: READY"}}) + "\n")
                fh.write(json.dumps({"type": "step_finish", "part": {
                    "type": "step-finish", "reason": "stop",
                    "model": {"providerID": "kilo", "modelID": "poolside/laguna-s-2.1:free"},
                    "cost": 0}}) + "\n")
            r = platforms.read_log(k, "kilo")
            self.assertFalse(r["quota_hit"])
            self.assertEqual(platforms.status_line(r["last_text"]), ("READY", ""))
            # mahler#141: kilo-auto/free is stateless per invocation — record
            # the underlying model actually used so a quota hit can be weighed
            # against which free model it reflects rather than the whole pool.
            self.assertEqual(r["model"], "poolside/laguna-s-2.1:free")

    def test_kilo_log_without_model(self):
        # A kilo log with no step_finish event (e.g. an error-only run) leaves
        # model as None instead of raising.
        with tempfile.TemporaryDirectory() as d:
            k = os.path.join(d, "k.log")
            with open(k, "w") as fh:
                fh.write(json.dumps({"type": "text",
                                     "part": {"type": "text", "text": "STATUS: READY"}}) + "\n")
            r = platforms.read_log(k, "kilo")
            self.assertIsNone(r["model"])

    def test_kilo_out_of_credits_is_a_quota_hit(self):
        # Real 402 shape hit on the default (non-:free) model (mahler#29): no
        # "quota" in the text, so this exercises the broader QUOTA_WORDS list.
        with tempfile.TemporaryDirectory() as d:
            k = os.path.join(d, "k.log")
            with open(k, "w") as fh:
                fh.write(json.dumps({"type": "error", "error": {"data": {
                    "message": "Add credits to continue, or switch to a free model",
                    "statusCode": 402,
                    "responseBody": '{"error_type":"usage_limit_exceeded"}'}}}) + "\n")
            r = platforms.read_log(k, "kilo")
            self.assertTrue(r["quota_hit"])

    def test_probe_copilot_sums_ai_credits_into_a_monthly_pct(self):
        payload = {"timePeriod": {"year": 2026, "month": 9}, "user": "mkny13", "usageItems": [
            {"product": "Copilot", "sku": "Copilot AI Credits",
             "model": "Auto: Claude Haiku 4.5", "grossQuantity": 5.632911},
            {"product": "Copilot", "sku": "Copilot AI Credits",
             "model": "Claude Sonnet 5", "grossQuantity": 6.9314}]}
        with mock.patch("subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout="mkny13\n"),
                subprocess.CompletedProcess([], 0, stdout=json.dumps(payload)),
            ]
            samples = platforms.probe_copilot(1500)
        self.assertEqual(len(samples), 1)
        window, pct, resets = samples[0]
        self.assertEqual(window, "monthly")
        self.assertAlmostEqual(pct, round(100 * (5.632911 + 6.9314) / 1500, 1))
        self.assertTrue(resets)

    def test_probe_copilot_needs_a_login(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout="\n")
            self.assertEqual(platforms.probe_copilot(1500), [])
        run.assert_called_once()      # never got to the billing call

    def test_next_month_start_rolls_over_the_year(self):
        dec = datetime(2026, 12, 15, tzinfo=timezone.utc)
        self.assertEqual(platforms._next_month_start(dec), datetime(2027, 1, 1, tzinfo=timezone.utc))

    def test_kilo_auth_error_is_not_a_quota_hit(self):
        with tempfile.TemporaryDirectory() as d:
            k = os.path.join(d, "k.log")
            with open(k, "w") as fh:
                fh.write(json.dumps({"type": "error", "error": {
                    "data": {"message": "You need to sign in to use this model.",
                             "statusCode": 401}}}) + "\n")
            r = platforms.read_log(k, "kilo")
            self.assertFalse(r["quota_hit"])

    def test_retry_after_minutes(self):
        # mahler#124: Cline's daily cap names its own reset time.
        self.assertEqual(platforms.retry_after_minutes("Try again in 17h 5m"), 17 * 60 + 5)
        self.assertEqual(platforms.retry_after_minutes("try again in 45m"), 45)
        self.assertEqual(platforms.retry_after_minutes("Try again in 2h"), 2 * 60)
        self.assertIsNone(platforms.retry_after_minutes("no reset time here"))
        self.assertIsNone(platforms.retry_after_minutes(""))
        self.assertIsNone(platforms.retry_after_minutes(None))

    def test_cline_daily_cap_gives_retry_after_from_the_error_text(self):
        # Real shape from the issue: every run fails at once when Cline's free
        # model (GLM-5.3-flash) hits its daily cap (mahler#124).
        with tempfile.TemporaryDirectory() as d:
            c = os.path.join(d, "c.log")
            with open(c, "w") as fh:
                fh.write(json.dumps({"error": {
                    "code": "INFERENCE_CAP_ERROR",
                    "message": "Error 429: Daily free limit reached on model "
                               "z-ai/glm-5.3-flash. Try again in 9h 41m"}}) + "\n")
            r = platforms.read_log(c, "cline")
            self.assertTrue(r["quota_hit"])
            self.assertEqual(r["retry_after"], 9 * 60 + 41)


class ClaudeUsageSharingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["projects"]["p"] = {"path": "/tmp/test", "repo": "o/r"}
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)
        # Ensure there is an inbox item so routing platforms are wanted
        self.led.upsert_item("p", 1, state="inbox", title="Task", priority=2)
        self.ctx = scheduler.Ctx(self.cfg, self.led)
        # refresh_usage also probes agy/copilot platforms pulled into `wanted`
        # by routing; these tests only care about Claude mirroring, and
        # leaving them unmocked shells out to the real `agy`/`gh` CLIs
        # (mahler#95 — multi-second execution time creep per test).
        self.addCleanup(mock.patch.stopall)
        mock.patch("mahler.platforms.probe_agy", return_value={}).start()
        mock.patch("mahler.platforms.probe_copilot", return_value=[]).start()

    def test_refresh_usage_mirrors_oauth_to_claude_and_opus(self):
        sample = [("5h", 35.0, iso(NOW + timedelta(hours=3))),
                  ("weekly", 55.0, iso(NOW + timedelta(days=5)))]
        with mock.patch("mahler.platforms.oauth_usage", return_value=sample):
            usage.refresh_usage(self.ctx, [config.project_policy(self.cfg, "p")])

        c_5h = self.led.q("SELECT used_pct FROM usage WHERE platform='claude' AND window='5h'")[0]["used_pct"]
        o_5h = self.led.q("SELECT used_pct FROM usage WHERE platform='claude-opus' AND window='5h'")[0]["used_pct"]
        self.assertEqual(c_5h, 35.0)
        self.assertEqual(o_5h, 35.0)

    def test_refresh_usage_mirrors_probe_to_both_and_sets_kv(self):
        sample = [("5h", 42.0, iso(NOW + timedelta(hours=2)))]
        with mock.patch("mahler.platforms.oauth_usage", return_value=[]), \
             mock.patch("mahler.platforms.probe_claude", return_value=sample):
            usage.refresh_usage(self.ctx, [config.project_policy(self.cfg, "p")])

        c_5h = self.led.q("SELECT used_pct FROM usage WHERE platform='claude' AND window='5h'")[0]["used_pct"]
        o_5h = self.led.q("SELECT used_pct FROM usage WHERE platform='claude-opus' AND window='5h'")[0]["used_pct"]
        self.assertEqual(c_5h, 42.0)
        self.assertEqual(o_5h, 42.0)
        self.assertIsNotNone(self.led.get_kv("probe:claude"))
        self.assertIsNotNone(self.led.get_kv("probe:claude-opus"))

    def test_refresh_usage_no_lockout_when_probe_fails(self):
        with mock.patch("mahler.platforms.oauth_usage", return_value=[]), \
             mock.patch("mahler.platforms.probe_claude", return_value=[]):
            usage.refresh_usage(self.ctx, [config.project_policy(self.cfg, "p")])
        self.assertIsNone(self.led.get_kv("probe:claude"))
        self.assertIsNone(self.led.get_kv("probe:claude-opus"))
        self.assertIsNone(self.led.get_kv("probe:oauth:claude"))
        self.assertIsNone(self.led.get_kv("probe:oauth:claude-opus"))

    def test_refresh_usage_sets_oauth_kv_on_success(self):
        sample = [("5h", 35.0, iso(NOW + timedelta(hours=3))),
                  ("weekly", 55.0, iso(NOW + timedelta(days=5)))]
        with mock.patch("mahler.platforms.oauth_usage", return_value=sample):
            usage.refresh_usage(self.ctx, [config.project_policy(self.cfg, "p")])
        self.assertIsNotNone(self.led.get_kv("probe:oauth:claude"))
        self.assertIsNotNone(self.led.get_kv("probe:oauth:claude-opus"))

    def test_usage_needs_refresh_when_stale(self):
        led = Ledger(":memory:", clock=lambda: NOW)
        old = iso(NOW - timedelta(minutes=30))
        for w in ("5h", "weekly"):
            led.record_usage("claude", w, 1, iso(NOW + timedelta(hours=2)), sampled_at=old)
        pconf = self.cfg["platforms"]["claude"]
        self.assertTrue(usage._usage_needs_refresh(led, "claude", pconf))

    def test_usage_needs_refresh_when_approaching_stale(self):
        # 13 minutes old — past stale_minutes - 3 (12) but not yet stale (15)
        led = Ledger(":memory:", clock=lambda: NOW)
        old = iso(NOW - timedelta(minutes=13))
        for w in ("5h", "weekly"):
            led.record_usage("claude", w, 1, iso(NOW + timedelta(hours=2)), sampled_at=old)
        pconf = self.cfg["platforms"]["claude"]
        self.assertTrue(usage._usage_needs_refresh(led, "claude", pconf))

    def test_usage_needs_refresh_when_oauth_overdue(self):
        # Fresh samples but oauth never checked — needs refresh
        led = Ledger(":memory:", clock=lambda: NOW)
        later = iso(NOW + timedelta(hours=3))
        for w in ("5h", "weekly"):
            led.record_usage("claude", w, 1, later)
        pconf = self.cfg["platforms"]["claude"]
        self.assertTrue(usage._usage_needs_refresh(led, "claude", pconf))

    def test_usage_needs_refresh_when_fresh(self):
        led = Ledger(":memory:", clock=lambda: NOW)
        later = iso(NOW + timedelta(hours=3))
        for name in ("claude", "claude-opus"):
            for w in ("5h", "weekly"):
                led.record_usage(name, w, 1, later)
        led.set_kv("probe:oauth:claude", iso(NOW - timedelta(minutes=1)))
        led.set_kv("probe:oauth:claude-opus", iso(NOW - timedelta(minutes=1)))
        pconf = self.cfg["platforms"]["claude"]
        self.assertFalse(usage._usage_needs_refresh(led, "claude", pconf))
        self.assertFalse(usage._usage_needs_refresh(led, "claude-opus", pconf))

    def test_cli_cmd_usage_probe_mirrors_to_both(self):
        from types import SimpleNamespace
        args = SimpleNamespace(probe=True)
        sample = [("5h", 25.0, iso(NOW + timedelta(hours=4)))]
        with mock.patch("mahler.platforms.probe_agy", return_value={}), \
             mock.patch("mahler.platforms.oauth_usage", return_value=sample), \
             mock.patch("mahler.usage.refresh_codex"), \
             mock.patch("mahler.platforms.probe_copilot", return_value=[]):
            cli.cmd_usage(args, self.cfg, self.led)

        c_5h = self.led.q("SELECT used_pct FROM usage WHERE platform='claude' AND window='5h'")[0]["used_pct"]
        o_5h = self.led.q("SELECT used_pct FROM usage WHERE platform='claude-opus' AND window='5h'")[0]["used_pct"]
        self.assertEqual(c_5h, 25.0)
        self.assertEqual(o_5h, 25.0)

    def test_tier_of(self):
        self.assertEqual(router.tier_of(self.cfg["platforms"]["kilo"]), 1)
        self.assertEqual(router.tier_of(self.cfg["platforms"]["cline-free"]), 1)
        self.assertEqual(router.tier_of(self.cfg["platforms"]["copilot"]), 2)
        self.assertEqual(router.tier_of(self.cfg["platforms"]["agy-claude"]), 2)
        self.assertEqual(router.tier_of(self.cfg["platforms"]["agy-gemini"]), 3)
        self.assertEqual(router.tier_of(self.cfg["platforms"]["claude"]), 3)
        self.assertEqual(router.tier_of(self.cfg["platforms"]["claude-opus"]), 4)

    def test_risk_min_tier(self):
        self.assertEqual(router.risk_min_tier("update recipes/sort.md"), 2)
        self.assertEqual(router.risk_min_tier("fix typo in AGENTS.md"), 2)
        self.assertEqual(router.risk_min_tier("prompt context size fix"), 2)
        self.assertEqual(router.risk_min_tier("database migration for users"), 2)
        self.assertEqual(router.risk_min_tier("just a normal bug fix"), 0)
        self.assertEqual(router.risk_min_tier(None), 0)

    def test_min_tier_filtering(self):
        # kilo is tier 1, claude is tier 3. Busy holds everything before kilo in routing order.
        busy = {"agy-claude", "agy-gemini", "cline-free", "copilot"}
        led = led_with(**{"kilo": (10, 10), "claude": (10, 10)})
        # Default min_tier=0 picks kilo for size:s
        p, _ = router.pick(self.cfg, led, "build", size="s", busy=busy)
        self.assertEqual(p, "kilo")
        # min_tier=2 skips kilo (tier 1 < 2) and picks claude (tier 3)
        p, reasons = router.pick(self.cfg, led, "build", size="s", min_tier=2, busy=busy)
        self.assertEqual(p, "claude")
        self.assertTrue(any("kilo: tier 1 below escalation tier 2" in r for r in reasons))
        # min_tier=4 skips kilo and claude (tier 3 < 4)
        p, reasons = router.pick(self.cfg, led, "build", size="s", min_tier=4, busy=busy)
        self.assertIsNone(p)
        self.assertTrue(any("claude: tier 3 below escalation tier 4" in r for r in reasons))

    def test_pinned_platform_bypasses_min_tier(self):
        led = led_with(**{"kilo": (10, 10), "agy-claude": (10, 10)})
        p, _ = router.pick(self.cfg, led, "build", pin="kilo", min_tier=2)
        self.assertEqual(p, "kilo")


class CapabilitySlotTests(unittest.TestCase):
    """Low/medium/high slots share an account's quota and run slot."""

    cfg = config.DEFAULTS

    def test_quota_groups_are_shared_with_the_base_platform(self):
        self.assertEqual(self.cfg["platforms"]["claude-low"]["quota_group"], "claude")
        self.assertEqual(self.cfg["platforms"]["codex"]["quota_group"], "codex")
        self.assertEqual(self.cfg["platforms"]["codex-low"]["quota_group"], "codex")
        self.assertEqual(self.cfg["platforms"]["codex-high"]["quota_group"], "codex")
        self.assertEqual(self.cfg["platforms"]["copilot"]["quota_group"], "copilot")
        self.assertEqual(self.cfg["platforms"]["copilot-high"]["quota_group"], "copilot")
        # same login -> quota readings and run slots are shared (D21)
        self.assertEqual(usage.quota_peers(self.cfg, "codex"),
                         ["codex-low", "codex", "codex-high"])
        self.assertEqual(usage.quota_peers(self.cfg, "copilot"),
                         ["copilot", "copilot-high"])

    def test_route_placement(self):
        # copilot-high joins the default build route right after copilot;
        # codex-high stays opt-in like codex itself.
        build = self.cfg["routing"]["build"]
        self.assertLess(build.index("copilot"), build.index("copilot-high"))
        self.assertNotIn("codex-high", build)
        self.assertNotIn("codex", build)

    def test_copilot_high_takes_size_l_but_not_smaller(self):
        # size:l: copilot is capped at max_size s, so the first size-capable
        # platform in route order is copilot-high (min_size l, tier 3) — it
        # needs a fresh monthly sample, since copilot-high is metered
        led = led_with(**{"agy-claude": (10, 10), "agy-gemini": (10, 10)})
        led.record_usage("copilot-high", "monthly", 5.0,
                         iso(NOW + timedelta(hours=6)))
        self.assertEqual(router.pick(self.cfg, led, "build", size="l")[0],
                         "copilot-high")
        # size:s/m: copilot-high is skipped (min_size l) like claude-opus;
        # copilot (max_size s) still takes size:s
        self.assertEqual(router.pick(self.cfg, led, "build", size="s")[0],
                         "agy-claude")
        led_spent = led_with(**{"agy-claude": (95, 95), "agy-gemini": (95, 95),
                                "claude": (5, 5), "claude-opus": (5, 5)})
        self.assertEqual(router.pick(self.cfg, led_spent, "build", size="m")[0],
                         "claude")

    def test_copilot_high_usage_shares_the_monthly_cap(self):
        # copilot-high is metered on the same monthly AI-credits window; a
        # hard reading on either platform reflects the shared account
        led = Ledger(":memory:", clock=lambda: NOW)
        led.record_usage("copilot-high", "monthly", 96.0,
                         iso(NOW + timedelta(hours=6)))
        state, _ = router.usage_state(led, "copilot-high",
                                      self.cfg["platforms"]["copilot-high"])
        self.assertEqual(state, "hard")
        # the run slot is shared: an active run on copilot-high holds copilot
        # and vice versa (busy_platforms groups by quota_group)
        active = [{"platform": "copilot-high"}]
        self.assertIn("copilot", tick.busy_platforms(self.cfg, active))

    def test_sibling_models_reach_the_argv(self):
        self.assertIn("gpt-5.3-codex",
                      platforms.copilot_argv(self.cfg["platforms"]["copilot-high"],
                                             "hi", "wt", "build"))
        self.assertIn("gpt-5.6-sol",
                      platforms.codex_argv(self.cfg["platforms"]["codex-high"],
                                           "hi", "wt", "build"))

    def test_low_slots_are_small_and_select_the_low_models(self):
        self.assertEqual(self.cfg["platforms"]["claude-low"]["max_size"], "s")
        self.assertEqual(self.cfg["platforms"]["codex-low"]["max_size"], "s")
        self.assertEqual(self.cfg["platforms"]["codex"]["max_size"], "m")
        self.assertEqual(self.cfg["platforms"]["claude-low"]["build_model"], "haiku")
        self.assertIn("gpt-5.6-luna",
                      platforms.codex_argv(self.cfg["platforms"]["codex-low"],
                                           "hi", "wt", "build"))

    def test_copilot_uses_auto_with_the_configured_tier_but_high_stays_pinned(self):
        # copilot: "auto" + --auto-tier gets the 10% discount for routine
        # size:s work; copilot-high stays pinned — its only job is a
        # capability guarantee, and auto is turn-complexity-adaptive (a live
        # check with --auto-tier intelligence still picked claude-haiku-4.5
        # for a trivial prompt, mahler#192's follow-up research).
        argv = platforms.copilot_argv(self.cfg["platforms"]["copilot"], "hi", "wt", "build")
        self.assertEqual(argv[argv.index("--model") + 1], "auto")
        self.assertEqual(argv[argv.index("--auto-tier") + 1], "balance")
        high_argv = platforms.copilot_argv(self.cfg["platforms"]["copilot-high"],
                                           "hi", "wt", "build")
        self.assertNotIn("--auto-tier", high_argv)


class CopilotQuotaFanOutTests(unittest.TestCase):
    """copilot-high shares copilot's AI-credits quota_group, so the periodic
    probe (usage.refresh_usage) must fan its one reading out to both — same
    account, same credits, one `gh api` call — the way record_claude_usage
    already does for claude/claude-opus."""

    def setUp(self):
        self.cfg = config.resolve_platforms(config._merge(config.DEFAULTS, {
            "projects": {"acme": {"enabled": True, "repo": "x/acme", "path": "/tmp/acme"}},
        }))
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=True)
        self.led.upsert_item("acme", 1, state="ready", priority=2)
        self.projects = [config.project_policy(self.cfg, "acme")]

    def test_a_single_probe_reading_lands_on_both_platforms(self):
        later = iso(NOW + timedelta(days=5))
        with mock.patch.object(platforms, "probe_copilot",
                               return_value=[("monthly", 42.0, later)]) as probe, \
                mock.patch.object(platforms, "probe_claude", return_value=[]), \
                mock.patch.object(platforms, "oauth_usage", return_value=[]), \
                mock.patch.object(platforms, "probe_agy", return_value={}), \
                mock.patch.object(router, "peak_state", return_value=(False, None)):
            usage.refresh_usage(self.ctx, self.projects)
        probe.assert_called_once()     # not called twice for copilot and copilot-high
        self.assertEqual(self.led.usage("copilot")["monthly"]["used_pct"], 42.0)
        self.assertEqual(self.led.usage("copilot-high")["monthly"]["used_pct"], 42.0)


class ReasonGroupsTests(unittest.TestCase):
    def test_all_router_reason_categories_and_duplicate_accounts(self):
        reasons = ["kilo: busy", "cline-free: only takes size:s", "copilot: requires size:m",
                   "small: tier 1 below escalation tier 3", "claude: peak hours until 11:00",
                   "agy-claude: soft (weekly 90%)", "agy-gemini: hard (weekly 99%)",
                   "codex: stale (5h: no sample)",
                   "work: pinned, but it spends the work account, not personal",
                   "unknown: disabled", "kilo: busy"]
        self.assertEqual(router.reason_groups(reasons), {
            "busy": ["kilo"], "size": ["cline-free", "copilot"], "tier": ["small"],
            "peak": ["claude"], "over": ["agy-claude", "agy-gemini"], "stale": ["codex"],
            "account": ["work"], "other": ["unknown"]})
        self.assertEqual(router.reason_groups([]), {})


class PeakLocalTimeTests(unittest.TestCase):
    def test_status_and_override_use_system_timezone(self):
        for month, hour in ((9, 14), (1, 15)):
            for zone, label, end in (("America/New_York", "ET", "14:00"),
                                     ("America/Los_Angeles", "PT", "11:00"),
                                     ("Asia/Kolkata", "IST", "23:30" if month == 9 else "00:30")):
                with self.subTest(month=month, zone=zone), local_timezone(zone):
                    now = datetime(2026, month, 14 if month == 9 else 5,
                                   hour, tzinfo=timezone.utc)
                    led = Ledger(":memory:", clock=lambda: now)
                    self.addCleanup(led.close)
                    self.assertEqual(router.peak_status_line(config.DEFAULTS, led),
                                     f"peak hours: Claude paused until {end} {label} (in 4h 0m)")
                    out = io.StringIO()
                    with mock.patch("sys.stdout", out):
                        result = cli.cmd_peak(mock.Mock(off=True, for_duration=None),
                                              config.DEFAULTS, led)
                    self.assertEqual(result, 0)
                    self.assertEqual(out.getvalue().strip(),
                                     f"peak override on — Claude runs allowed until {end} {label} (in 4h 0m)")
                    self.assertEqual(router.peak_status_line(config.DEFAULTS, led),
                                     f"peak hours: overridden until {end} {label} (4h 0m)")
                    self.assertEqual(led.get_kv(router.PEAK_OVERRIDE),
                                     iso(now + timedelta(hours=4)))


class PeakOverrideCommandTests(unittest.TestCase):
    def test_peak_hold_reason_mentions_flag_override(self):
        now = datetime(2026, 9, 14, 14, 0, tzinfo=timezone.utc)
        led = Ledger(":memory:", clock=lambda: now)
        self.addCleanup(led.close)
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["routing"]["build"] = ["claude"]
        name, reasons = router.pick(cfg, led, "build")
        self.assertIsNone(name)
        self.assertEqual(len(reasons), 1)
        self.assertIn("— mahler peak --off to override", reasons[0])
        self.assertNotIn("mahler peak off", reasons[0])

    def test_peak_cli_help_documents_correct_flags(self):
        buf = io.StringIO()
        with self.assertRaises(SystemExit) as cm, mock.patch("sys.stdout", buf):
            cli.main(["peak", "--help"])
        self.assertEqual(cm.exception.code, 0)
        help_out = buf.getvalue()
        self.assertIn("--off", help_out)
        self.assertIn("allow Claude through the peak hold", help_out)
        self.assertIn("--on", help_out)
        self.assertIn("restore normal scheduling", help_out)
        self.assertNotIn("pause new Claude runs", help_out)
        self.assertNotIn("peak off", help_out)

    def test_peak_cmd_on_clears_override_and_restores_scheduling(self):
        now = datetime(2026, 9, 14, 14, 0, tzinfo=timezone.utc)
        led = Ledger(":memory:", clock=lambda: now)
        self.addCleanup(led.close)
        cli.cmd_peak(mock.Mock(off=True, on=False, for_duration=None), config.DEFAULTS, led)
        self.assertIsNotNone(led.get_kv(router.PEAK_OVERRIDE))

        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            ret = cli.cmd_peak(mock.Mock(off=False, on=True, for_duration=None),
                               config.DEFAULTS, led)
        self.assertEqual(ret, 0)
        self.assertEqual(out.getvalue().strip(), "peak override cleared")
        self.assertIsNone(led.get_kv(router.PEAK_OVERRIDE))


if __name__ == "__main__":
    unittest.main()
