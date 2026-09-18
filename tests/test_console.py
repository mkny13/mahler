"""The operator console (DESIGN D27): its state, its copy, its page, its writes."""

import copy
import json
import os
import re
import tempfile
import tomllib
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from local_timezone import local_timezone

from mahler import config, router, scheduler
from mahler.console import actions, page, state
from mahler.ledger import Ledger, iso

SAT_NOON = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)   # a Saturday: no peak window
MON_PEAK = datetime(2026, 9, 14, 14, 0, tzinfo=timezone.utc)   # Monday 07:00 PT: in the window


class Clock:
    def __init__(self, t=SAT_NOON):
        self.t = t

    def __call__(self):
        return self.t


def make_cfg(**over):
    """What config.load() returns for a small install: defaults, two projects."""
    user = {"projects": {
        "mahler": {"enabled": True, "repo": "mkny13/mahler", "path": "/nonexistent/mahler",
                   "hot_hold": False},
        "groundwork": {"enabled": True, "repo": "mkny13/groundwork",
                       "path": "/nonexistent/groundwork", "hot_hold": False},
        "old": {"enabled": False, "repo": "mkny13/old"},
    }}
    user = config._merge(user, over)
    return config.resolve_platforms(config._merge(copy.deepcopy(config.DEFAULTS), user))


def make_led(t=SAT_NOON):
    return Ledger(":memory:", clock=Clock(t))


def fresh(led, platform, window, pct, resets_in=timedelta(hours=3)):
    led.record_usage(platform, window, pct, iso(led.now() + resets_in))


def all_fresh(led, pct=10):
    """Every metered default platform has a fresh reading under its lines,
    with resets far enough out that no D23 burst raises Claude's lines."""
    for p in ("claude", "claude-opus", "agy-claude", "agy-gemini"):
        fresh(led, p, "5h", pct)
        fresh(led, p, "weekly", pct, resets_in=timedelta(days=3))
    for p in ("copilot", "copilot-high"):
        fresh(led, p, "monthly", pct, resets_in=timedelta(days=10))


class SystemStateTests(unittest.TestCase):
    def test_idle_running_paused(self):
        cfg, led = make_cfg(), make_led()
        self.assertEqual(state.build(cfg, led)["system"], {"label": "IDLE", "tone": "mut"})
        led.upsert_item("mahler", 1, title="t", state="working")
        led.create_run(project="mahler", number=1, role="build", platform="agy-claude",
                       epoch=1, est_mins=26)
        self.assertEqual(state.build(cfg, led)["system"]["label"], "RUNNING · 1")
        led.set_kv("paused", "1")
        self.assertEqual(state.build(cfg, led)["system"], {"label": "PAUSED", "tone": "warn"})


class SettingsConfigTests(unittest.TestCase):
    def test_projection_never_exposes_account_environment(self):
        cfg = make_cfg(accounts={"work": {
            "env": {"ANTHROPIC_AUTH_TOKEN": "secret"},
            "routing": {"sort": ["claude"], "plan": [], "build": ["agy-claude"]},
        }})
        view = config.settings(cfg)
        self.assertNotIn("secret", json.dumps(view))
        self.assertEqual([r["key"] for r in view["routing"]], ["default", "account:work"])

    def test_validation_and_toml_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('[projects.mahler]\nenabled = true\n'
                         '[[projects.mahler.backups]]\nname = "db"\nkind = "postgres"\n')
            form = config.settings(config.load(path))
            form["platforms"][0]["model"] = 'model-"one"'
            form["routing"][0]["build"] = list(reversed(form["routing"][0]["build"]))
            form["concurrency"] = {"total": 4, "by_tier": {"1": 3, "2": 2}}
            form["projects"][0]["max_parallel"] = 2
            form["scheduler"]["settle_minutes"] = 12
            saved = config.save_settings(form, path)
            with open(path, "rb") as fh:
                raw = tomllib.load(fh)
            loaded = config.load(path)

        self.assertEqual(raw["projects"]["mahler"]["backups"][0]["name"], "db")
        self.assertEqual(loaded["concurrency"], {"total": 4, "by_tier": {"1": 3, "2": 2}})
        self.assertEqual(loaded["defaults"]["settle_minutes"], 12)
        self.assertEqual(saved["platforms"][0]["model"], 'model-"one"')

    def test_invalid_settings_do_not_touch_file(self):
        cfg = make_cfg()
        form = config.settings(cfg)
        form["routing"][0]["build"] = ["not-a-platform"]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.toml")
            original = '[projects.mahler]\nenabled = true\n'
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(original)
            with self.assertRaisesRegex(ValueError, "configured platforms"):
                config.save_settings(form, path)
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), original)


class SettingsPageTests(unittest.TestCase):
    def setUp(self):
        self.cfg, self.led = make_cfg(), make_led()
        self.addCleanup(self.led.close)

    def test_desktop_and_phone_render_complete_settings_forms(self):
        doc = page.document(state.build(self.cfg, self.led))

        self.assertIn('<button class="rail-i" data-go="settings">', doc)
        self.assertIn('class="btn settings-head-link" data-go="settings"', doc)
        self.assertIn('data-tab-go="settings">Settings</button>', doc)
        self.assertIn('<section class="view view-settings">', doc)
        self.assertIn('<section class="tabv tabv-settings">', doc)
        self.assertEqual(doc.count('<form class="settings-form" data-settings-form>'), 2)
        self.assertIn('data-setting-platform="claude"', doc)
        self.assertIn('data-platform-field="provider"', doc)
        self.assertIn('data-platform-field="build_model"', doc)
        self.assertIn('data-route-scope="default"', doc)
        self.assertIn('data-route-move="up"', doc)
        self.assertIn('data-setting="concurrency.total"', doc)
        self.assertIn('data-setting="scheduler.settle_minutes"', doc)
        self.assertIn('data-setting="project.mahler"', doc)

    def test_settings_page_and_state_never_render_account_secrets(self):
        cfg = make_cfg(accounts={"work": {
            "env": {"ANTHROPIC_AUTH_TOKEN": "do-not-render-this"},
            "routing": {"sort": ["claude"], "plan": [], "build": ["agy-claude"]},
        }})
        doc = page.document(state.build(cfg, self.led))

        self.assertNotIn("do-not-render-this", doc)
        self.assertIn("Account · work", doc)
        self.assertIn("Account login environment variables are never displayed", doc)

    def test_browser_serializes_and_preserves_unsaved_settings(self):
        self.assertIn('post("settings", settingsPayload(form))', page.JS)
        self.assertIn('if (settingsDirty)', page.JS)
        self.assertIn('data-route-platform', page.JS)
        self.assertIn(':root[data-view="settings"] .view-settings', page.CSS)
        self.assertIn(':root[data-tab="settings"] .tabv-settings', page.CSS)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.cfg, self.led = make_cfg(), make_led()
        self.led.upsert_item("mahler", 41, title="Split runner yield", state="working")

    def run_for(self, mins, est):
        rid = self.led.create_run(project="mahler", number=41, role="build",
                                  platform="agy-claude", epoch=3, est_mins=est,
                                  worktree="/w/mahler/41-118",
                                  started_at=iso(self.led.now() - timedelta(minutes=mins)))
        self.led.claim("mahler", 41, f"run:{rid}", "auto", 10, platform="agy-claude", run_id=rid)
        return state.build(self.cfg, self.led)["runs"][0]

    def test_under_estimate(self):
        r = self.run_for(18, 26)
        self.assertEqual(r["timing"], "18m of ~26m")
        self.assertEqual(r["tone"], "acc")
        self.assertEqual(r["progress"], 69)
        self.assertIn("worktree 41-118", r["meta"])
        self.assertIn("lease held", r["meta"])
        self.assertEqual(r["url"], "https://github.com/mkny13/mahler/issues/41")

    def test_past_estimate(self):
        r = self.run_for(34, 22)
        self.assertEqual(r["timing"], "34m · 12m past estimate")
        self.assertEqual(r["tone"], "warn")
        self.assertEqual(r["progress"], 100)


class NeedsTests(unittest.TestCase):
    def test_question_comes_from_the_needs_you_transition(self):
        cfg, led = make_cfg(), make_led()
        led.upsert_item("mahler", 9, title="Staging key", state="ready", priority=2)
        led.set_state("mahler", 9, "needs_you", "Create one, or reuse prod read-only?")
        led.upsert_item("groundwork", 81, title="Date format", state="ready", priority=1)
        led.set_state("groundwork", 81, "failed", "3 attempts failed")
        led.upsert_item("old", 2, title="disabled project", state="needs_you")
        needs = state.build(cfg, led)["needs"]
        self.assertEqual([n["ref"] for n in needs], ["groundwork#81", "mahler#9"])   # p1 first
        self.assertEqual(needs[1]["question"], "Create one, or reuse prod read-only?")
        self.assertEqual(needs[0]["question"], "3 attempts failed")
        self.assertIn("waiting 0m", needs[1]["meta"])

    def test_question_and_options_come_from_the_item_when_set(self):
        """mahler#248: finalize's OPTIONS split stores both on the item; the
        console prefers them over the raw state-transition reason."""
        cfg, led = make_cfg(), make_led()
        led.upsert_item("mahler", 9, title="Staging key", state="ready", priority=2)
        led.set_state("mahler", 9, "needs_you", "Create a staging key or reuse prod?",
                      question="Create a staging key or reuse prod?",
                      options=json.dumps(["I'll create it", "Reuse prod"]))
        needs = state.build(cfg, led)["needs"]
        need = next(n for n in needs if n["ref"] == "mahler#9")
        self.assertEqual(need["question"], "Create a staging key or reuse prod?")
        self.assertEqual(need["options"], [{"label": "I'll create it", "text": "I'll create it"},
                                           {"label": "Reuse prod", "text": "Reuse prod"}])

    def test_legacy_options_are_removed_from_question_and_recovered_as_choices(self):
        cfg, led = make_cfg(), make_led()
        led.upsert_item("mahler", 9, title="Staging key", state="ready")
        led.set_state("mahler", 9, "needs_you",
                      "Use staging? [OPTIONS: Create one | Reuse prod]")
        need = state.build(cfg, led)["needs"][0]
        self.assertEqual(need["question"], "Use staging?")
        self.assertEqual([o["text"] for o in need["options"]], ["Create one", "Reuse prod"])

    def test_failed_question_ignores_a_stale_item_question_column(self):
        """A leftover 'question' column from an earlier needs_you round must not
        leak into a later failed item's displayed reason."""
        cfg, led = make_cfg(), make_led()
        led.upsert_item("groundwork", 81, title="Date format", state="ready", priority=1,
                        question="stale question from a past needs_you")
        led.set_state("groundwork", 81, "failed", "3 attempts failed")
        needs = state.build(cfg, led)["needs"]
        need = next(n for n in needs if n["ref"] == "groundwork#81")
        self.assertEqual(need["question"], "3 attempts failed")

    def test_landing_prefers_triage_when_something_needs_you(self):
        cfg, led = make_cfg(), make_led()
        self.assertEqual(state.build(cfg, led)["landing"], {"tab": "now", "view": "now"})
        led.upsert_item("mahler", 9, title="q", state="needs_you")
        self.assertEqual(state.build(cfg, led)["landing"], {"tab": "triage", "view": "needs"})


class BacklogTests(unittest.TestCase):
    def test_groups_counts_and_order(self):
        cfg, led = make_cfg(), make_led()
        led.upsert_item("mahler", 1, title="a", state="ready", priority=2)
        led.upsert_item("mahler", 2, title="b", state="inbox", priority=3)
        led.upsert_item("mahler", 3, title="c", state="working", priority=1)
        led.upsert_item("mahler", 4, title="d", state="needs_you", priority=2)
        led.upsert_item("mahler", 5, title="e", state="done")
        s = state.build(cfg, led)
        g = s["backlog"][0]
        self.assertEqual(g["project"], "mahler")
        self.assertEqual(g["counts"], "4 · 2 ready · 1 live · 1 you")
        self.assertEqual([i["state"] for i in g["items"]], ["needs-you", "working", "ready", "inbox"])
        self.assertEqual(s["backlog"][1]["counts"], "0 · 0 ready · 0 live")   # no "you" when 0
        self.assertEqual(s["backlog_total"], 4)
        self.assertNotIn("old", [g["project"] for g in s["backlog"]])


class QuotaTests(unittest.TestCase):
    def rows(self, cfg, led):
        return {q["name"]: q for q in state.build(cfg, led)["quota"]}

    def test_one_gauge_per_quota_group(self):
        rows = self.rows(make_cfg(), make_led())
        self.assertIn("claude", rows)
        self.assertNotIn("claude-opus", rows)          # shares claude's quota
        self.assertEqual(rows["claude"]["members"], ["claude", "claude-opus"])
        self.assertNotIn("codex", rows)                # in no route by default

    def test_labels_and_tones(self):
        cfg, led = make_cfg(), make_led()
        all_fresh(led)
        fresh(led, "claude", "5h", 63)
        for w in ("5h", "weekly"):
            led.record_usage("kilo", w, 100.0, iso(led.now() + timedelta(minutes=18)))
        rows = self.rows(cfg, led)
        self.assertEqual((rows["claude"]["label"], rows["claude"]["tone"]), ("63%", "warn"))
        self.assertIn("soft line 60%", rows["claude"]["detail"])
        self.assertEqual((rows["kilo"]["label"], rows["kilo"]["tone"], rows["kilo"]["width"]),
                         ("off", "bad", 100))
        self.assertEqual(rows["cline-free"]["label"], "unmetered")
        self.assertEqual(rows["agy-claude"]["label"], "10%")
        self.assertIn("resets", rows["agy-claude"]["detail"])

    def test_no_reading_is_stale(self):
        rows = self.rows(make_cfg(), make_led())
        self.assertEqual(rows["agy-claude"]["label"], "stale")
        self.assertFalse(rows["agy-claude"]["available"])

    def test_model_line_carries_the_plan(self):
        cfg = make_cfg(accounts={"work": {"routing": {"build": ["codex-work"]}}},
                       platforms={"codex-work": {"from": "codex", "account": "work"}},
                       routing={"build": ["codex", "cline-free", "kilo"]})
        rows = self.rows(cfg, make_led())
        self.assertEqual(rows["codex"]["model"], "free tier")
        self.assertEqual(rows["cline-free"]["model"], "free tier · size s only")
        self.assertEqual(rows["kilo"]["model"], "kilo-auto/free · size s only")
        # a plan belongs to a login: another account doesn't inherit codex's
        self.assertEqual(rows["codex-work"]["model"], "work account")
        cfg["platforms"]["codex-work"]["plan"] = "business plan"
        self.assertEqual(self.rows(cfg, make_led())["codex-work"]["model"], "business plan")

    def test_capacity_line(self):
        cfg, led = make_cfg(routing={"sort": ["claude"], "plan": ["claude-opus"],
                                     "build": ["agy-claude", "kilo"]}), make_led()
        all_fresh(led)
        for w in ("5h", "weekly"):
            led.record_usage("kilo", w, 100.0, iso(led.now() + timedelta(minutes=18)))
        line = state.build(cfg, led)["capacity"]
        self.assertTrue(line.startswith("2 of 3 platforms available — claude, agy-claude."), line)
        self.assertIn("kilo is backing off until", line)

    def test_windows_carry_soft_line_and_reset(self):
        """mahler#335: the Capacity view needs every window, not just the worst."""
        cfg, led = make_cfg(), make_led()
        all_fresh(led)
        fresh(led, "claude", "5h", 63)
        rows = self.rows(cfg, led)
        wins = {w["window"]: w for w in rows["claude"]["windows"]}
        self.assertEqual(set(wins), {"5h", "weekly"})
        self.assertEqual(wins["5h"]["pct"], 63)
        self.assertEqual(wins["5h"]["soft"], 60)
        self.assertTrue(wins["5h"]["resets_txt"])
        self.assertEqual(wins["weekly"]["pct"], 10)


class CapacityPageTests(unittest.TestCase):
    """mahler#335: Capacity is a top-level view with full quota detail."""

    def setUp(self):
        self.cfg, self.led = make_cfg(), make_led()
        all_fresh(self.led)
        fresh(self.led, "claude", "5h", 63)
        self.s = state.build(self.cfg, self.led)

    def html(self):
        return page.app(self.s)

    def test_capacity_is_a_rail_item_and_a_view(self):
        html = self.html()
        self.assertIn('<button class="rail-i" data-go="capacity">', html)
        self.assertIn('<section class="view view-capacity">', html)
        self.assertIn(':root[data-view="capacity"] .view-capacity', page.CSS)
        self.assertIn('.vt-capacity', page.CSS)

    def test_every_page_links_to_it(self):
        html = self.html()
        # the left rail, the Now view's capacity line, and the sidebar block
        self.assertEqual(html.count('data-go="capacity"'), 3)

    def test_one_card_per_quota_group_with_its_windows(self):
        html = self.html()
        for q in self.s["quota"]:
            self.assertIn(f'data-quota="{q["name"]}"', html)
        claude = next(q for q in self.s["quota"] if q["name"] == "claude")
        self.assertIn("claude, claude-opus", html)          # members share one gauge
        self.assertIn("soft 60%", html)                     # per-window soft line
        self.assertIn("resets", html)                       # per-window reset time
        self.assertEqual(html.count('<div class="capwin">'),
                         sum(len(q["windows"]) for q in self.s["quota"]))

    def test_unmetered_groups_say_so(self):
        html = self.html()
        self.assertIn("unmetered — no quota signal", html)
        self.assertIn('<span class="meta t-mut">unmetered</span>', html)


class StatsTests(unittest.TestCase):
    """#352: closed-item productivity for selectable equal-length windows."""

    def setUp(self):
        self.cfg, self.led = make_cfg(), make_led()

    def done(self, project, number, ago):
        self.led.upsert_item(project, number, state="done",
                             state_changed_at=iso(self.led.now() - ago))

    def test_counts_current_previous_and_delta_by_enabled_project(self):
        self.done("mahler", 1, timedelta(days=1))
        self.done("mahler", 2, timedelta(days=3))
        self.done("mahler", 3, timedelta(days=8))
        self.done("mahler", 4, timedelta(days=20))
        self.done("groundwork", 1, timedelta(days=9))
        self.done("old", 1, timedelta(days=1))

        rows = state.build(self.cfg, self.led, "last7")["stats"]

        self.assertEqual(rows["mahler"], {"closed": 2, "prev_closed": 1, "delta": 1})
        self.assertEqual(rows["groundwork"], {"closed": 0, "prev_closed": 1, "delta": -1})
        self.assertNotIn("old", rows)

    def test_custom_dates_are_inclusive_and_compare_equal_length(self):
        today = self.led.now().astimezone().date()
        self.done("mahler", 1, timedelta(hours=1))
        self.done("mahler", 2, timedelta(days=1, hours=1))

        rows = state.stats(self.led, (today.isoformat(), today.isoformat()), ["mahler"])

        self.assertEqual(rows["mahler"], {"closed": 1, "prev_closed": 1, "delta": 0})

    def test_page_has_desktop_and_mobile_stats_with_range_controls(self):
        doc = page.document(state.build(self.cfg, self.led))

        self.assertIn('<button class="rail-i" data-go="stats">', doc)
        self.assertIn('<section class="view view-stats">', doc)
        self.assertIn('<button class="tab" data-tab-go="stats">', doc)
        self.assertIn('class="tabv tabv-stats"', doc)
        for key, label in state.STATS_RANGES:
            self.assertIn(f'data-stats-range="{key}">{label}</button>', doc)
        self.assertIn('localStorage.getItem("mahler.stats.range")', doc)
        self.assertIn('store("local", "mahler.stats.range", value)', doc)
        self.assertIn(':root[data-view="stats"] .view-stats', page.CSS)


class IdleReasonTests(unittest.TestCase):
    def idle(self, cfg, led):
        return state.build(cfg, led)["idle"]

    def test_quota_holds_nothing_when_nothing_is_queued(self):
        cfg, led = make_cfg(), make_led(MON_PEAK)     # peak on, no readings at all
        led.upsert_item("mahler", 39, title="probe", state="verifying", pr=112)
        idle = self.idle(cfg, led)
        self.assertEqual([r["text"] for r in idle["reasons"]],
                         ["Nothing is waiting to start — finished changes are waiting on CI."])

    def test_empty_backlog(self):
        idle = self.idle(make_cfg(), make_led())
        self.assertEqual(idle["headline"], "Nothing is running.")
        self.assertEqual(idle["reasons"][0]["text"], "The backlog is empty — nothing to work on.")

    def test_no_reasons_while_running(self):
        cfg, led = make_cfg(), make_led()
        led.upsert_item("mahler", 1, title="t", state="working")
        led.create_run(project="mahler", number=1, role="build", platform="kilo", epoch=1)
        self.assertIsNone(self.idle(cfg, led))

    @local_timezone("America/Los_Angeles")
    def test_peak_hours_with_an_override(self):
        cfg, led = make_cfg(), make_led(MON_PEAK)
        all_fresh(led)
        led.upsert_item("mahler", 1, title="t", state="ready")
        peak = self.idle(cfg, led)["reasons"][0]
        self.assertEqual(peak["text"], "Claude is in your peak hours — it plans but doesn't "
                                       "build until 11:00 PT. Free tiers are unaffected.")
        self.assertEqual(peak["countdown"], "4h 0m left")
        self.assertEqual((peak["action"], peak["act"]), ("Let Claude build", "peak_override"))

    def test_platforms_over_the_soft_line_are_grouped(self):
        cfg, led = make_cfg(), make_led()
        all_fresh(led)
        fresh(led, "agy-claude", "weekly", 91, resets_in=timedelta(days=2))
        fresh(led, "agy-gemini", "weekly", 88, resets_in=timedelta(days=2))
        led.upsert_item("mahler", 1, title="t", state="ready")
        texts = [r["text"] for r in self.idle(cfg, led)["reasons"]]
        self.assertIn("agy-gemini and agy-claude are both past 85% on the weekly window, so the "
                      "scheduler won't start there.", texts)

    def test_backoff_offers_to_clear(self):
        cfg, led = make_cfg(), make_led()
        all_fresh(led)
        for p in ("kilo", "cline-free"):
            for w in ("5h", "weekly"):
                led.record_usage(p, w, 100.0, iso(led.now() + timedelta(minutes=47)))
        led.upsert_item("mahler", 1, title="t", state="ready")
        r = [x for x in self.idle(cfg, led)["reasons"] if x.get("act") == "clear_backoff"][0]
        self.assertEqual(r["text"], "cline-free and kilo are backing off after quota errors.")
        self.assertEqual(r["countdown"], "cline-free 47m · kilo 47m")
        self.assertEqual(r["platforms"], ["cline-free", "kilo"])

    def test_verifying_item_holds_the_only_slot(self):
        cfg, led = make_cfg(), make_led()
        all_fresh(led)
        led.upsert_item("mahler", 39, title="probe", state="verifying", pr=112,
                        state_changed_at=iso(led.now() - timedelta(minutes=6)))
        led.upsert_item("mahler", 40, title="next", state="ready")
        idle = self.idle(cfg, led)
        self.assertEqual(idle["headline"], "Nothing is running. One thing is holding it:")
        r = idle["reasons"][0]
        self.assertEqual(r["text"], "mahler#39 holds the project's only parallel slot until its "
                                    "PR merges — CI has been pending 6 minutes.")
        self.assertEqual(r["countdown"], "verify timeout in 54m")
        self.assertEqual(r["action"], "Open PR #112")
        self.assertEqual(r["href"], "https://github.com/mkny13/mahler/pull/112")

    def test_number_words_in_the_headline(self):
        cfg, led = make_cfg(), make_led(MON_PEAK)
        led.set_kv("paused", "1")
        led.upsert_item("mahler", 1, title="t", state="ready")
        self.assertTrue(self.idle(cfg, led)["headline"].startswith("Nothing is running. "))
        self.assertRegex(self.idle(cfg, led)["headline"], r"(Two|Three|Four) things are holding it:$")


class BannerTests(unittest.TestCase):
    def kinds(self, cfg, led):
        return [b["kind"] for b in state.build(cfg, led)["banners"]]

    def test_none_when_all_is_well(self):
        cfg, led = make_cfg(), make_led()
        all_fresh(led)
        self.assertEqual(self.kinds(cfg, led), [])

    def test_severity_order(self):
        cfg = make_cfg(projects={"mahler": {"hot_hold": True}})
        led = make_led()                      # nothing fresh: every builder is over
        led.set_kv("paused", "1")
        led.record_usage("kilo", router.HOLD, 100.0, iso(led.now() + timedelta(minutes=40)))
        for w in ("5h", "weekly"):
            led.record_usage("cline-free", w, 100.0, iso(led.now() + timedelta(minutes=40)))
        led.create_run(project="mahler", number=3, role="build", platform="kilo", epoch=1,
                       status="ended", stop_reason="silent")
        with mock.patch.object(state.presence, "last_claude_activity",
                               return_value=led.now() - timedelta(minutes=4)):
            banners = state.build(cfg, led)["banners"]
        self.assertEqual([b["kind"] for b in banners],
                         ["PAUSED BY YOU", "ALL PLATFORMS OVER SOFT LINE", "HOT HOLD · MAHLER",
                          "RUN SAT SILENT · KILO"])
        self.assertEqual(banners[2]["text"], "You were working in mahler with Claude Code 4 "
                                             "minutes ago. No new builds start there until 20 "
                                             "minutes after you stop. Work in flight continues.")
        self.assertEqual(banners[0]["act"], "resume")
        self.assertIn("printed nothing for 10 minutes", banners[3]["text"])


class EventTests(unittest.TestCase):
    def test_stream_skips_bookkeeping_and_duplicates(self):
        cfg, led = make_cfg(), make_led()
        led.upsert_item("mahler", 5, title="t")
        led.set_state("mahler", 5, "ready", "sorted")
        led.event("run_start", "mahler", 5, {"run": 7, "role": "build", "platform": "kilo"})
        led.set_state("mahler", 5, "working", "kilo run 7")     # repeats run_start
        led.event("lease", "mahler", 5, "run:7 epoch 1")        # bookkeeping
        led.set_state("mahler", 5, "verifying", "build done")
        led.event("shipped", "mahler", 5, {"pr": 12})
        led.set_state("mahler", 6, "needs_you", "Which one?")
        ev = state.build(cfg, led)["events"]
        self.assertEqual([(e["kind"], e["text"]) for e in ev], [
            ("needs_you", "mahler#6 Which one?"),
            ("pr_merged", "mahler#5 squash-merged #12"),
            ("run_started", "mahler#5 build on kilo, run 7"),
            ("ready", "mahler#5 sorted"),
        ])
        self.assertTrue(ev[0]["attention"])
        self.assertTrue(ev[1]["undoable"])
        self.assertFalse(ev[2]["attention"])

    def test_digest_counts_what_you_have_not_seen(self):
        cfg, led = make_cfg(), make_led()
        led.event("run_start", "mahler", 5, {"run": 7, "role": "build", "platform": "kilo"})
        led.event("shipped", "mahler", 5, {"pr": 12})
        d = state.build(cfg, led)["digest"]
        self.assertEqual(d["count"], 2)
        actions.run(cfg, led, "digest_seen", {"upto": d["upto"]})
        self.assertEqual(state.build(cfg, led)["digest"]["count"], 0)
        led.event("shipped", "mahler", 6, {"pr": 13})
        self.assertEqual(state.build(cfg, led)["digest"]["count"], 1)


class ProjectBriefTests(unittest.TestCase):
    def ship(self, led, project, number, title, labels=(), summary="", pr=None):
        led.snapshot_release_item(project, number, pr=pr or number + 100,
                                  title=title, summary=summary, labels=list(labels))
        led.event("shipped", project, number, {"pr": pr or number + 100})
        return led.q1("SELECT max(id) AS id FROM events")["id"]

    def test_first_use_groups_captured_changes_and_collapses_maintenance(self):
        cfg, led = make_cfg(), make_led()
        first = self.ship(led, "mahler", 1, "New dashboard", ["type:feature"],
                          "Show the whole system at a glance")
        self.ship(led, "mahler", 2, "Fix stale count", ["type:bug"])
        last = self.ship(led, "mahler", 3, "Refresh audit", ["type:chore"])

        brief = state.build(cfg, led)["briefs"][0]
        self.assertEqual((brief["project"], brief["seen"], brief["upto"], brief["count"]),
                         ("mahler", 0, last, 3))
        self.assertEqual([i["number"] for i in brief["features"]], [1])
        self.assertEqual([i["number"] for i in brief["fixes"]], [2])
        self.assertEqual([i["number"] for i in brief["maintenance"]], [3])
        self.assertEqual(brief["features"][0]["summary"],
                         "Show the whole system at a glance")
        self.assertEqual(brief["maintenance_count"], 1)
        self.assertEqual(brief["oldest_shipped_at"], iso(led.now()))
        self.assertEqual(brief["newest_shipped_at"], iso(led.now()))
        self.assertLess(first, last)

        rendered = page._project_brief(brief)
        # This exact shared renderer is called by both responsive layouts.
        self.assertIn("New dashboard", rendered)
        self.assertIn("Maintenance (1)", rendered)
        self.assertIn('data-act="brief_seen"', rendered)

    def test_acknowledgement_is_per_project_idempotent_and_race_safe(self):
        cfg, led = make_cfg(), make_led()
        old_cursor = self.ship(led, "mahler", 1, "First", ["type:feature"])
        self.ship(led, "groundwork", 7, "Other project", ["type:bug"])
        displayed = state.build(cfg, led)
        self.assertEqual(next(b for b in displayed["briefs"] if b["project"] == "mahler")["upto"],
                         old_cursor)

        # This shipment races the acknowledgement of the already-rendered cursor.
        new_cursor = self.ship(led, "mahler", 2, "Arrived later", ["type:bug"])
        result = actions.run(cfg, led, "brief_seen",
                             {"project": "mahler", "upto": old_cursor})
        self.assertEqual(result, {"upto": old_cursor})
        briefs = {b["project"]: b for b in state.build(cfg, led)["briefs"]}
        self.assertEqual([i["number"] for i in briefs["mahler"]["fixes"]], [2])
        self.assertEqual(briefs["mahler"]["upto"], new_cursor)
        self.assertEqual(briefs["groundwork"]["count"], 1)

        again = actions.run(cfg, led, "brief_seen",
                            {"project": "mahler", "upto": old_cursor})
        self.assertEqual(again, {"upto": old_cursor, "deduplicated": True})
        self.assertEqual(led.q1("SELECT count(*) AS n FROM events "
                                "WHERE kind='console_brief_seen'")["n"], 1)

    def test_cursor_must_be_a_shipment_for_the_enabled_project(self):
        cfg, led = make_cfg(), make_led()
        other_cursor = self.ship(led, "groundwork", 7, "Other")
        with self.assertRaises(actions.ActionError):
            actions.run(cfg, led, "brief_seen",
                        {"project": "mahler", "upto": other_cursor})
        with self.assertRaises(actions.ActionError):
            actions.run(cfg, led, "brief_seen", {"project": "old", "upto": other_cursor})
        with self.assertRaises(actions.ActionError):
            actions.run(cfg, led, "brief_seen", {"project": "groundwork", "upto": True})

    def test_brief_spans_zero_one_and_multiple_releases_without_mutating_them(self):
        cfg, led = make_cfg(), make_led()
        self.assertTrue(state.build(cfg, led)["briefs"][0]["up_to_date"])
        first = self.ship(led, "mahler", 1, "Released once", ["type:feature"])
        led.create_release("mahler", "0.1.0", "sha1", item_numbers=[1])
        self.ship(led, "mahler", 2, "Released twice", ["type:bug"])
        led.create_release("mahler", "0.1.1", "sha2", item_numbers=[2])
        self.ship(led, "mahler", 3, "Still unreleased", ["type:feature"])

        brief = state.build(cfg, led)["briefs"][0]
        self.assertEqual(brief["release_versions"], ["0.1.0", "0.1.1"])
        self.assertEqual(brief["count"], 3)
        before_releases = [(r["id"], r["state"]) for r in led.list_releases("mahler")]
        before_items = [(r["number"], r["release_id"])
                        for r in led.q("SELECT * FROM release_items ORDER BY number")]
        actions.run(cfg, led, "brief_seen", {"project": "mahler", "upto": first})
        self.assertEqual([(r["id"], r["state"]) for r in led.list_releases("mahler")],
                         before_releases)
        self.assertEqual([(r["number"], r["release_id"])
                          for r in led.q("SELECT * FROM release_items ORDER BY number")],
                         before_items)


class PeakOverrideTests(unittest.TestCase):
    @local_timezone("America/Los_Angeles")
    def test_manual_override_holds_until_restored(self):
        cfg, led = make_cfg(), make_led(MON_PEAK)
        self.assertEqual(router.peak_state(cfg, led)[0], True)
        actions.run(cfg, led, "peak_override", {})
        self.assertEqual(router.peak_state(cfg, led), (False, None))
        self.assertEqual(router.peak_status_line(cfg, led),
                         "peak hours: overridden until you switch back")
        peak = state.build(cfg, led)["peak"]
        self.assertEqual((peak["line"], peak["action"], peak["header"]),
                         ("Peak hours overridden — Claude may build until you switch back",
                          "Restore", True))
        actions.run(cfg, led, "peak_restore", {})
        self.assertTrue(router.peak_state(cfg, led)[0])
        peak = state.build(cfg, led)["peak"]
        self.assertEqual(peak["short"], "Peak hours until 11:00 · Claude plans only")

    @local_timezone("America/Los_Angeles")
    def test_header_only_inside_the_window(self):
        peak = state.build(make_cfg(), make_led(SAT_NOON))["peak"]
        self.assertFalse(peak["header"])
        self.assertEqual(peak["line"], "Claude peak hours 05:00–11:00 PT — planning only, "
                                       "free tiers build")

    def test_peak_times_use_system_timezone(self):
        for zone, label, window, until in (
            ("America/New_York", "ET", "08:00–14:00", "14:00"),
            ("America/Los_Angeles", "PT", "05:00–11:00", "11:00"),
            ("UTC", "UTC", "12:00–18:00", "18:00"),
        ):
            with self.subTest(zone=zone), local_timezone(zone):
                led = make_led(MON_PEAK)
                self.addCleanup(led.close)
                peak = state._peak(make_cfg(), led)
                self.assertTrue(peak["active"])
                self.assertEqual((peak["until"], peak["tz"]), (until, label))
                self.assertIn(f"{window} {label}", peak["line"])
                self.assertEqual(peak["left"], "4h 0m left")

    @local_timezone("America/New_York")
    def test_window_uses_schedule_date_and_dst_at_endpoints(self):
        # 'Now' precedes the Pacific DST change: localize each endpoint
        # using its own offset, rather than the offset at the frozen 'now'.
        for now in (datetime(2026, 3, 8, 9, tzinfo=timezone.utc),
                    datetime(2026, 11, 1, 8, tzinfo=timezone.utc),
                    datetime(2026, 1, 5, 16, tzinfo=timezone.utc)):
            with self.subTest(now=now):
                led = make_led(now)
                self.addCleanup(led.close)
                peak = state._peak(make_cfg(), led)
                self.assertIn("08:00–14:00 ET", peak["line"])

    def test_refused_when_the_window_is_off(self):
        cfg = make_cfg(claude_peak={"enabled": False})
        with self.assertRaises(actions.ActionError):
            actions.run(cfg, make_led(), "peak_override", {})
        self.assertIsNone(state.build(cfg, make_led())["peak"])

    @local_timezone("America/Los_Angeles")
    def test_page_renders_peak_banner_only_when_active(self):
        cfg = make_cfg()
        off_peak_doc = page.document(state.build(cfg, make_led(SAT_NOON)))
        self.assertNotIn('class="peakline"', off_peak_doc)
        self.assertNotIn('class="peakrow"', off_peak_doc)
        self.assertNotIn("Claude peak hours 05:00", off_peak_doc)

        peak_doc = page.document(state.build(cfg, make_led(MON_PEAK)))
        self.assertIn('class="peakline"', peak_doc)
        self.assertIn('class="peakrow"', peak_doc)
        self.assertIn("Claude peak hours 05:00", peak_doc)


class ActionTests(unittest.TestCase):
    def test_pause_and_resume_write_events(self):
        cfg, led = make_cfg(), make_led()
        actions.run(cfg, led, "pause", {})
        self.assertTrue(led.paused())
        actions.run(cfg, led, "resume", {})
        self.assertFalse(led.paused())
        kinds = [r["kind"] for r in led.q("SELECT kind FROM events ORDER BY id")]
        self.assertEqual(kinds, ["pause", "resume"])

    def test_clear_backoff_keeps_real_readings(self):
        cfg, led = make_cfg(), make_led()
        until = iso(led.now() + timedelta(minutes=30))
        led.record_usage("kilo", "5h", 100.0, until)
        led.record_usage("kilo", router.HOLD, 100.0, until)
        led.record_usage("agy-claude", "weekly", 62.0)
        led.record_usage("agy-claude", router.HOLD, 100.0, until)
        actions.run(cfg, led, "clear_backoff", {"platforms": ["kilo", "agy-claude"]})
        self.assertEqual(led.usage("kilo"), {})
        self.assertEqual(set(led.usage("agy-claude")), {"weekly"})    # metered reading stays
        ev = led.q1("SELECT * FROM events WHERE kind='backoff_cleared'")
        self.assertIn("kilo", ev["detail"])

    def test_bad_input_is_refused(self):
        cfg, led = make_cfg(), make_led()
        for name, body in (("clear_backoff", {}), ("clear_backoff", {"platforms": ["nope"]}),
                           ("digest_seen", {"upto": "7"}), ("digest_seen", {"upto": True}),
                           ("pause", [])):
            with self.assertRaises(actions.ActionError, msg=(name, body)):
                actions.run(cfg, led, name, body)
        with self.assertRaises(KeyError):
            actions.run(cfg, led, "delete_everything", {})

    def test_digest_seen_never_goes_backwards(self):
        cfg, led = make_cfg(), make_led()
        actions.run(cfg, led, "digest_seen", {"upto": 9})
        actions.run(cfg, led, "digest_seen", {"upto": 3})
        self.assertEqual(led.get_kv(state.SEEN_KEY), "9")


class StopRunTests(unittest.TestCase):
    """Stop & hand off from the run detail (mahler#252, DESIGN D27)."""

    def setUp(self):
        from mahler.console import outbox
        self.outbox = outbox
        self.cfg, self.led = make_cfg(), make_led()
        self.addCleanup(self.led.close)
        self.led.upsert_item("mahler", 41, title="Split runner yield", state="working")
        self.run_id = self.led.create_run(project="mahler", number=41, role="build",
                                          platform="agy-claude", epoch=1)
        self.ctx = scheduler.Ctx(self.cfg, self.led)

    def test_bad_or_stale_run_is_refused(self):
        with self.assertRaisesRegex(actions.ActionError, "positive run id"):
            actions.run(self.cfg, self.led, "stop_run", {"run": "x"})
        with self.assertRaisesRegex(actions.ActionError, "already ended"):
            actions.run(self.cfg, self.led, "stop_run", {"run": self.run_id + 1})
        self.led.update_run(self.run_id, status="ended")
        with self.assertRaisesRegex(actions.ActionError, "already ended"):
            actions.run(self.cfg, self.led, "stop_run", {"run": self.run_id})

    def test_queuing_is_idempotent(self):
        first = actions.run(self.cfg, self.led, "stop_run", {"run": self.run_id})["id"]
        second = actions.run(self.cfg, self.led, "stop_run", {"run": self.run_id})["id"]
        self.assertEqual(first, second)
        self.assertEqual(len(self.led.pending_actions("stop_run")), 1)
        ev = self.led.q1("SELECT * FROM events WHERE kind='console_stop_queued'")
        self.assertEqual(json.loads(ev["detail"])["run"], self.run_id)

    def test_active_run_gets_yield_at_and_stop_reason(self):
        actions.run(self.cfg, self.led, "stop_run", {"run": self.run_id})
        self.outbox.drain(self.ctx)
        run = self.led.run(self.run_id)
        self.assertEqual(run["stop_reason"], "handoff")
        self.assertIsNotNone(run["yield_at"])
        row = self.led.q1("SELECT * FROM console_actions WHERE kind='stop_run'")
        self.assertEqual(row["status"], "done")

    def test_a_run_that_already_ended_is_skipped(self):
        actions.run(self.cfg, self.led, "stop_run", {"run": self.run_id})
        self.led.update_run(self.run_id, status="ended", stop_reason=None)
        self.outbox.drain(self.ctx)
        run = self.led.run(self.run_id)
        self.assertIsNone(run["stop_reason"])
        self.assertIsNone(run["yield_at"])
        row = self.led.q1("SELECT * FROM console_actions WHERE kind='stop_run'")
        self.assertEqual(row["status"], "skipped")

    def test_run_shows_stopping_once_queued(self):
        self.led.claim("mahler", 41, f"run:{self.run_id}", "auto", 10,
                       platform="agy-claude", run_id=self.run_id)
        actions.run(self.cfg, self.led, "stop_run", {"run": self.run_id})
        run = state.build(self.cfg, self.led)["runs"][0]
        self.assertEqual(run["status"], "stopping")


class PageTests(unittest.TestCase):
    def setUp(self):
        self.cfg, self.led = make_cfg(), make_led()
        self.led.upsert_item("mahler", 9, title="A <script>alert(1)</script> title",
                             issue_body="Body <img src=x onerror=alert(2)>\nSecond line",
                             state="ready", options=json.dumps(["First choice", "Second choice"]))
        self.led.set_state("mahler", 9, "needs_you", "Pick <b>one</b>?")

    def test_document_has_both_layouts_and_lands_on_needs(self):
        doc = page.document(state.build(self.cfg, self.led))
        self.assertIn('<div class="dk">', doc)
        self.assertIn('<div class="ph">', doc)
        self.assertIn('data-view="needs" data-tab="triage"', doc)
        self.assertIn("Pick &lt;b&gt;one&lt;/b&gt;?", doc)
        self.assertNotIn("<script>alert", doc)
        self.assertIn('href="https://github.com/mkny13/mahler/issues/9"', doc)

    def test_needs_details_and_choices_render_safely_in_both_layouts(self):
        doc = page.document(state.build(self.cfg, self.led))
        self.assertEqual(doc.count('data-need-details="mahler#9"'), 2)
        self.assertGreaterEqual(doc.count("A &lt;script&gt;alert(1)&lt;/script&gt; title"), 2)
        self.assertEqual(doc.count("Body &lt;img src=x onerror=alert(2)&gt;"), 2)
        self.assertNotIn("<img src=x", doc)
        self.assertGreaterEqual(doc.count('data-text="First choice"'), 2)
        self.assertIn('store("session", "mahler.needDetails"', doc)

    def test_fragment_is_just_the_app(self):
        frag = page.app(state.build(self.cfg, self.led))
        self.assertNotIn("<html", frag)
        self.assertIn('id="counts"', frag)

    def test_design_non_negotiables(self):
        for prop in ("transition", "animation", "box-shadow"):
            self.assertNotRegex(page.CSS, prop + r"[\w-]*\s*:")
        for gold in ("#8a5b00", "#e0b155"):          # rejected as unreadable
            self.assertNotIn(gold, page.CSS.lower())
        self.assertNotIn("serif", page.CSS.replace("sans-serif", ""))

    def test_releases_card_renders_a_pending_cut_release_and_a_published_release(self):
        # Reproduces the 500 from a pending cut_release console_action (no
        # done_at yet — state._releases read the nonexistent "updated_at"
        # column) and a published release with published_at set (page.py's
        # published-history block called an unimported ledger.parse/_hhmm).
        self.led.snapshot_release_item("mahler", 10, pr=100, title="Add dark mode",
                                       merge_sha="sha10", labels=["type:feature"])
        self.led.create_release("mahler", version="1.0.0", checkpoint_sha="sha_rel1",
                                published_at="2026-09-14T12:00:00Z",
                                remote_url="https://github.com/mkny13/mahler/releases/tag/v1.0.0")
        self.led.queue_action("cut_release", project="mahler",
                              payload={"version": "1.1.0", "checkpoint_sha": "sha_head"})

        doc = page.document(state.build(self.cfg, self.led))
        self.assertIn("v1.0.0", doc)


class UatStateTests(unittest.TestCase):
    """Ready to test (mahler#250): what shipped lands there, until a verdict."""

    def setUp(self):
        self.cfg, self.led = make_cfg(), make_led()
        self.led.add_uat('mahler', 9, 88, '4c1f0abfeed5', 'Wired the exporter',
                         '- the new ping arrives\n- [x] no errors in the log')

    def uat(self, cfg=None):
        return state.build(cfg or self.cfg, self.led)["uat"]

    def test_rows_show_ref_meta_check_and_pr_link(self):
        u = self.uat()[0]
        self.assertEqual(u["ref"], "mahler#9")
        self.assertEqual(u["url"], "https://github.com/mkny13/mahler/issues/9")
        self.assertEqual(u["title"], "Wired the exporter")
        self.assertIn("merged", u["meta"])
        self.assertTrue(u["meta"].endswith("sha 4c1f0ab"))
        self.assertEqual(u["check"], "the new ping arrives; no errors in the log")
        self.assertEqual((u["link"], u["link_label"]),
                         ("https://github.com/mkny13/mahler/pull/88", "PR #88"))
        self.assertIsNone(u["pending"])

    def test_the_check_is_the_markers_stripped_and_capped(self):
        long = "\n".join(f"- thing {i} that goes on" for i in range(20))
        self.led.add_uat('mahler', 10, 89, 'abc', 'Later', long)
        self.assertEqual(len(self.uat()[0]["check"]), 200)   # capped

    def test_newest_first_and_disabled_projects_hidden(self):
        self.led.add_uat('mahler', 10, 89, 'abc', 'Later', '- x')
        self.led.add_uat('old', 3, 1, 'abc', 'disabled', '- x')
        self.assertEqual([u["number"] for u in self.uat()], [10, 9])
        self.assertNotIn("old", [u["project"] for u in self.uat()])

    def test_a_recorded_verdict_removes_the_row(self):
        self.led.set_uat_verdict('mahler', 9, 'pass')
        self.assertEqual(self.uat(), [])

    def test_uat_url_config_beats_the_pr_link(self):
        cfg = make_cfg(projects={'mahler': {
            'uat_url': 'https://staging.example.com/build/{number}',
            'uat_url_label': 'Open build'}})
        u = self.uat(cfg)[0]
        self.assertEqual(u["link"], 'https://staging.example.com/build/9')
        self.assertEqual(u["link_label"], 'Open build')

    def test_counts_exclude_a_queued_verdict(self):
        actions.run(self.cfg, self.led, 'uat_pass', {'project': 'mahler', 'number': 9})
        s = state.build(self.cfg, self.led)
        self.assertEqual((s["uat_count"], len(s["uat"])), (0, 1))
        self.assertEqual(s["uat"][0]["pending"], "uat_pass")
        self.assertEqual(s["landing"], {"tab": "now", "view": "now"})   # nothing to act on

    def test_landing_lands_on_ready_to_test(self):
        self.assertEqual(state.build(self.cfg, self.led)["landing"],
                         {"tab": "triage", "view": "test"})


class UatPageTests(unittest.TestCase):
    """The Ready-to-test view: Pass, Fail, and the bug sheet."""

    def setUp(self):
        self.cfg, self.led = make_cfg(), make_led()
        self.led.add_uat('mahler', 9, 88, '4c1f0abfeed5', 'Wired the exporter',
                         '- the new ping arrives')

    def frag(self):
        return page.app(state.build(self.cfg, self.led))

    def doc(self):
        return page.document(state.build(self.cfg, self.led))

    def test_renders_the_row_with_pass_and_fail(self):
        frag = self.frag()
        self.assertIn('data-uat="mahler#9"', frag)
        self.assertIn("Wired the exporter", frag)
        self.assertIn("the new ping arrives", frag)
        self.assertIn('href="https://github.com/mkny13/mahler/pull/88"', frag)
        self.assertIn("PR #88 ↗", frag)
        self.assertIn('data-act="uat_pass"', frag)
        self.assertIn('data-open-bug="mahler#9"', frag)
        self.assertIn(">Pass</button>", frag)
        self.assertIn(">Fail</button>", frag)
        self.assertIn('data-view="test" data-tab="triage"', self.doc())
        self.assertIn('<span>Ready to test</span><span class="mono t-mut">1</span>',
                      self.doc())                                # the rail badge

    def test_the_bug_sheet_is_in_the_page_and_wired(self):
        frag = self.frag()
        self.assertIn('data-bug-detail="mahler#9"', frag)
        self.assertIn("What went wrong?", frag)
        self.assertIn("File p1 bug", frag)
        self.assertIn('data-keep="bug:mahler#9"', frag)
        self.assertIn('data-act="uat_fail"', frag)

    def test_a_queued_verdict_shows_its_copy(self):
        actions.run(self.cfg, self.led, 'uat_pass', {'project': 'mahler', 'number': 9})
        self.assertIn("Passed — UAT recorded.", self.frag())
        self.led2 = make_led()
        self.led2.add_uat('mahler', 9, 88, '4c1f0ab', 'Wired the exporter', '- x')
        actions.run(self.cfg, self.led2, 'uat_fail',
                    {'project': 'mahler', 'number': 9, 'note': 'nope'})
        self.assertIn("Failed — p1 bug filed and routed.", page.app(state.build(self.cfg, self.led2)))

    def test_phone_has_a_ready_to_test_section(self):
        frag = self.frag()
        self.assertIn("Ready to test · 1", frag)
        self.assertIn('class="puat"', frag)

    def test_phone_ready_to_test_card_glues_ref_to_meta(self):
        s = {"needs": [], "needs_count": 0,
             "uat": [{"ref": "mahler#239", "url": "https://github.com/mkny13/mahler/issues/239",
                       "meta": "merged 20:01 · sha f259e84", "title": "t", "check": "c",
                       "link": "", "link_label": "", "project": "mahler", "number": 239,
                       "pending": None}],
             "uat_count": 1, "banners": [],
             "capture": {"recent": [], "projects": []}}
        html = page._p_triage(s)
        self.assertIn('mahler#239</a> · merged 20:01', html)
        self.assertNotIn('mahler#239merged', html)

    def test_desktop_ready_to_test_meta_line_join(self):
        """Desktop _d_test via _uat_left joins ref and meta with ' · '."""
        s = {"needs": [], "needs_count": 0,
             "uat": [{"ref": "mahler#239", "url": "https://github.com/mkny13/mahler/issues/239",
                       "meta": "merged 20:01 · sha f259e84", "title": "t", "check": "c",
                       "link": "", "link_label": "", "project": "mahler", "number": 239,
                       "pending": None}],
             "uat_count": 1, "banners": [],
             "capture": {"recent": [], "projects": []}}
        html = page._d_test(s)
        self.assertIn('mahler#239</a> · merged 20:01', html)
        self.assertNotIn('mahler#239merged', html)

    def test_an_empty_queue_renders_no_section(self):
        self.led.set_uat_verdict('mahler', 9, 'pass')
        frag = self.frag()
        self.assertNotIn("Ready to test ·", frag)
        self.assertNotIn('class="puat"', frag)

    def test_adjacent_inline_content_spans_have_separators(self):
        """Guard against glued adjacent content spans (.t/.meta/.check/.lnk/.q/.pchip).

        Scans the full phone+desktop app fragment for </span><span where both spans
        carry a content-bearing inline class. Legitimate chrome adjacencies (rail
        buttons, theme toggle, tabs, backlog group headers, quota rows) are
        explicitly allowlisted; any other adjacency is a regression.
        """
        cfg, led = make_cfg(), make_led()
        led.add_uat('mahler', 9, 88, '4c1f0abfeed5', 'Wired the exporter',
                     '- the new ping arrives')
        led.upsert_item('mahler', 10, title='Needs you item', state='needs_you')
        led.set_state('mahler', 10, 'needs_you', 'Pick one?')
        led.upsert_item('mahler', 11, title='Ready item', state='ready')
        led.upsert_item('groundwork', 5, title='Backlog item', state='inbox')
        s = state.build(cfg, led)
        html = page.app(s)

        # Content-bearing inline span classes that must not sit directly adjacent
        # without a text separator. Chrome/structural classes are excluded.
        content_classes = (
            't', 'meta', 'check', 'lnk', 'q', 'pchip', 'p1', 'ref', 'title',
            'why-item', 'more', 'lbl', 'val', 'detail', 'model', 'name', 'kind',
            'when', 'txt', 'sh-show', 'sh-hide', 'c', 'o', 'dot'
        )
        pattern = re.compile(
            r'</span><span\s+class="([^"]*(?:' + '|'.join(content_classes) + ')[^"]*)"'
        )

        # Known-safe adjacent pairs: (first_span_class_substring, second_span_class_substring)
        # These occur in chrome/navigation and are structurally intentional.
        allowlist = {
            # rail buttons: <span>Label</span><span class="mono t-tone">count</span>
            ('', 'mono t-'),
            # theme toggle: <span class="tl ...">Auto</span><span class="tl ...">Light</span>
            ('tl ', 'tl '),
            # view title tabs: <span class="vt vt-x">...</span><span class="vt vt-y">...</span>
            ('vt vt-', 'vt vt-'),
            # backlog group header: <span class="name">...</span><span class="mono">...</span>
            ('name', 'mono'),
            # backlog group header (phone): same
            # quota rows: <span class="name mono">...</span><span class="bar">...
            #            <span class="val mono t-tone">...</span></div><span class="model mono">...
            ('name mono', 'val mono t-'),
            ('val mono t-', 'model mono'),
            # quota bar internals: <span class="f-tone">...</span><span class="tick">...
            ('f-', 'tick'),
            ('tick', 'detail'),
            ('f-', 'detail'),
            # phone tabs: <span>Tab</span><span class="mono t-tone">count</span>
            # (same as rail buttons pattern)
            # need card: <span class="lbl">...</span><span class="q">...</span>
            ('lbl', 'q'),
            # need card meta: <span class="meta">...</span><span class="meta">... (the fixed case)
            # This used to be the bug; now it's one span. Keep allowlisted in case it reappears.
            ('meta', 'meta'),
            # phone needs row: <span class="meta">...</span><span class="pchip">...</span><span class="meta">...
            ('meta', 'pchip'),
            ('pchip', 'meta'),
            # phone uat row: <span class="meta">...</span> (single span now)
            # backlog items: <span class="pchip">...</span><span class="title">...</span><span class="st mono t-tone">...
            ('pchip', 'title'),
            ('title', 'st mono t-'),
            # phone backlog items: <span class="pbl">...<span class="pchip">...</span><span class="title">...</span><span class="st mono t-">...
            # event stream: <span class="when mono">...</span><span class="kind mono">...</span><span class="txt t-tone">...
            ('when mono', 'kind mono'),
            ('kind mono', 'txt t-'),
            # side backlog buttons: <span>project</span><span class="mono">counts</span>
            # (covered by name/mono above)
            # run timing: <span class="mono t-tone">...</span><span class="mono t-mut">...
            ('mono t-', 'mono t-'),
        }

        def is_allowed(first_class, second_class):
            for a, b in allowlist:
                if a in first_class and b in second_class:
                    return True
            return False

        violations = []
        for m in pattern.finditer(html):
            # Get the full class attribute of the second span
            second_class = m.group(1)
            # Find the first span's class by looking backwards
            before = html[max(0, m.start() - 200):m.start()]
            first_match = re.search(r'<span\s+class="([^"]*)"[^>]*></span>$', before)
            if not first_match:
                # Might be a span without class, or different structure; skip
                continue
            first_class = first_match.group(1)
            if not is_allowed(first_class, second_class):
                context_start = max(0, m.start() - 80)
                context_end = min(len(html), m.end() + 80)
                violations.append(
                    f'First span class="{first_class}", second span class="{second_class}"\n'
                    f'Context: ...{html[context_start:context_end]}...'
                )

        if violations:
            self.fail('Found adjacent content spans without separator:\n' + '\n\n'.join(violations))


class NeedsYouPingTests(unittest.TestCase):
    """mahler#257: a needs-you push deep-links into the console on that item."""

    def cfg(self, public_url=""):
        c = make_cfg()
        c["serve"]["public_url"] = public_url
        return c

    def ping(self, public_url="", console=False, project="mahler", number=9):
        cfg, led = self.cfg(public_url), make_led()
        ctx = scheduler.Ctx(cfg, led, dry_run=False)
        with mock.patch("mahler.scheduler.notify.send") as send:
            ctx.ping("Mahler needs you — mahler #9", "which key?",
                     project=project, number=number, priority="high",
                     tags="question", console=console)
        return send

    def test_without_public_url_the_click_is_the_github_issue(self):
        send = self.ping(public_url="")
        self.assertTrue(send.called)
        self.assertEqual(send.call_args.kwargs["click"],
                         "https://github.com/mkny13/mahler/issues/9")
        self.assertEqual(send.call_args.kwargs["priority"], "high")
        self.assertEqual(send.call_args.kwargs["tags"], "question")

    def test_with_public_url_a_needs_you_ping_opens_the_console(self):
        send = self.ping(public_url="https://mac-mini.example.ts.net", console=True)
        self.assertEqual(send.call_args.kwargs["click"],
                         "https://mac-mini.example.ts.net/#needs/mahler/9")

    def test_console_flag_is_ignored_when_public_url_is_unset(self):
        send = self.ping(public_url="", console=True)
        self.assertEqual(send.call_args.kwargs["click"],
                         "https://github.com/mkny13/mahler/issues/9")

    def test_console_flag_needs_a_project(self):
        send = self.ping(public_url="https://mac-mini.example.ts.net", console=True,
                         project=None)
        self.assertIsNone(send.call_args.kwargs["click"])

    def test_fyi_pings_never_open_the_console(self):
        send = self.ping(public_url="https://mac-mini.example.ts.net", console=False)
        self.assertEqual(send.call_args.kwargs["click"],
                         "https://github.com/mkny13/mahler/issues/9")

    def test_trailing_slash_on_public_url_is_tolerated(self):
        send = self.ping(public_url="https://mac-mini.example.ts.net/", console=True)
        self.assertEqual(send.call_args.kwargs["click"],
                         "https://mac-mini.example.ts.net/#needs/mahler/9")


class ConsoleHashTests(unittest.TestCase):
    """mahler#257: #needs/<project>/<n> lands the console on that item."""

    def page(self, needs):
        cfg, led = make_cfg(), make_led()
        for n in needs:
            led.upsert_item(n[0], n[1], title=n[2], state="needs_you")
        return page.document(state.build(cfg, led))

    def test_needs_items_carry_a_data_need_anchor(self):
        doc = self.page([("mahler", 9, "Which key?"), ("groundwork", 81, "Date format")])
        self.assertIn('data-need="mahler#9"', doc)
        self.assertIn('data-need="groundwork#81"', doc)
        self.assertIn('class="view view-needs"', doc)
        self.assertIn('class="tabv tabv-triage"', doc)

    def test_the_script_handles_the_needs_you_hash(self):
        doc = self.page([("mahler", 9, "Which key?")])
        self.assertIn("applyHash", doc)
        self.assertIn("#needs/", doc)
        self.assertIn("hashchange", doc)


class RecordedIdleTests(unittest.TestCase):
    def setUp(self):
        self.cfg, self.led = make_cfg(), make_led()
        self.addCleanup(self.led.close)
        for n in range(1, 6):
            self.led.upsert_item("mahler", n, state="ready")

    def idle(self, holds, age=0):
        self.led.set_kv("schedule_holds", json.dumps({
            "at": iso(self.led.now() - timedelta(seconds=age)), "holds": holds}))
        return state._idle(self.cfg, self.led, {"paused": False, "peak": None, "quota": []},
                           [], self.led.now())

    def hold(self, kind, number=1, **data):
        return {"kind": kind, "project": "mahler", "number": number, **data}

    def item(self, n, title=None):
        return {"ref": f"mahler#{n}",
                "url": f"https://github.com/mkny13/mahler/issues/{n}", "title": title}

    def test_route_grouping_and_size_only(self):
        holds = [self.hold("no_platform", n, role="build", size="m", blockers={"size": ["kilo"]})
                 for n in (1, 2)]
        holds += [self.hold("no_platform", 3, role="plan", size="l",
                            blockers={"busy": ["kilo"], "over": ["agy-gemini", "agy-claude"],
                                      "peak": ["claude"], "size": ["cline-free"]})]
        self.assertEqual(self.idle(holds)["reasons"], [
            {"text": "2 item(s) need a builder that takes size:m, and none in the route does.",
             "items": [self.item(1), self.item(2)]},
            {"text": "1 plan item(s) have no platform with headroom — busy: kilo; past the line: "
                     "agy-claude, agy-gemini; peak hours: claude; too small: cline-free.",
             "items": [self.item(3)]}])

    def test_route_items_carry_the_title(self):
        self.led.upsert_item("mahler", 1, title="Fix the thing", state="ready")
        holds = [self.hold("no_platform", 1, role="build", size="m", blockers={"busy": ["kilo"]})]
        self.assertEqual(self.idle(holds)["reasons"][0]["items"], [self.item(1, "Fix the thing")])

    def test_settling_dependencies_overlap_and_capacity(self):
        minutes = config.project_policy(self.cfg, "mahler")["settle_minutes"]
        holds = [self.hold("settling", until=iso(self.led.now() + timedelta(minutes=2))),
                 self.hold("deps", 2, on=[6, 7]), self.hold("area", 3, area="console"),
                 self.hold("files", 4, files=["a.py", "b.py"]),
                 {"kind": "capacity", "project": "mahler", "max_parallel": 0}]
        reasons = self.idle(holds)["reasons"]
        self.assertIn({"text": f"1 item(s) were just sorted and settle for {minutes} minutes "
                              "before a build starts.", "countdown": "first in 2m"}, reasons)
        for text in ("mahler#3 waits — area:console already in progress.",
                     "mahler#4 waits — a.py, b.py already in progress."):
            self.assertIn({"text": text}, reasons)
        self.assertIn({"text": "mahler#2 waits for mahler#6 and mahler#7 to close.",
                       "items": [self.item(2)]}, reasons)
        self.assertIn({"text": "mahler is at its limit of 0 run(s).",
                       "items": [self.item(n) for n in range(1, 6)]}, reasons)

    def test_capacity_lists_only_ready_items_and_lease_host_also_inbox(self):
        self.led.upsert_item("mahler", 6, state="inbox")
        holds = [{"kind": "capacity", "project": "mahler", "max_parallel": 0}]
        self.assertEqual(self.idle(holds)["reasons"], [
            {"text": "mahler is at its limit of 0 run(s).",
             "items": [self.item(n) for n in range(1, 6)]}])
        holds = [{"kind": "lease_host", "project": "mahler"}]
        self.assertEqual(self.idle(holds)["reasons"], [
            {"text": "mahler waits — its canonical lease host is unavailable.",
             "items": [self.item(n) for n in range(1, 7)]}])

    def test_qualified_dependency_diagnostic(self):
        holds = [self.hold("deps", 1, on=[
            {"repo": "mkny13/groundwork", "number": 125},
            {"repo": "couch-tour", "number": 258}])]
        self.assertEqual(self.idle(holds)["reasons"][0]["text"],
                         "mahler#1 waits for mkny13/groundwork#125 and "
                         "couch-tour#258 to close.")

    @local_timezone("America/Los_Angeles")
    def test_off_peak_hold_reason(self):
        self.led.close()
        self.led = make_led(MON_PEAK)
        for n in range(1, 4):
            self.led.upsert_item("mahler", n, state="ready")

        holds = [self.hold("no_platform", 1, role="build", size="m",
                           blockers={"peak": ["claude-work"], "size": ["copilot-work"]})]
        
        self.assertEqual(self.idle(holds)["reasons"], [
            {"text": "1 build item(s) (size:m) wait for off-peak hours: claude-work resumes at "
                     "11:00 PT (in 4h 0m)",
             "items": [self.item(1)]}])

        size_holds = [self.hold("no_platform", 2, role="build", size="m",
                                blockers={"size": ["copilot-work"]})]
        self.assertEqual(self.idle(size_holds)["reasons"], [
            {"text": "1 item(s) need a builder that takes size:m, and none in the route does.",
             "items": [self.item(2)]}])

        self.led.close()
        self.led = make_led(SAT_NOON)
        for n in range(1, 4):
            self.led.upsert_item("mahler", n, state="ready")
        self.assertEqual(self.idle(holds)["reasons"], [
            {"text": "1 build item(s) have no platform with headroom — peak hours: claude-work; "
                     "too small: copilot-work.",
             "items": [self.item(1)]}])

    def test_many_dependencies_are_grouped(self):
        holds = [self.hold("deps", n, on=[99]) for n in range(1, 5)]
        self.assertEqual(self.idle(holds)["reasons"], [
            {"text": "4 items wait for other issues to close.",
             "items": [self.item(n) for n in range(1, 5)]}])

    def test_more_than_twenty_dependencies_truncate_with_a_note(self):
        for n in range(1, 26):
            self.led.upsert_item("mahler", n, title=f"item {n}", state="ready")
        holds = [self.hold("deps", n, on=[99]) for n in range(1, 26)]
        reasons = self.idle(holds)["reasons"]
        self.assertEqual(reasons[0]["text"], "25 items wait for other issues to close.")
        self.assertEqual(len(reasons[0]["items"]), 20)
        self.assertEqual(reasons[0]["more"], 5)
        self.assertEqual(reasons[0]["items"][0], self.item(1, "item 1"))
        self.assertEqual(reasons[0]["items"][-1], self.item(20, "item 20"))

    def test_page_renders_the_toggle_but_only_when_there_are_items(self):
        idle = {"headline": "Nothing is running. Two things are holding it:",
                "reasons": [
                    {"text": "2 item(s) need a builder that takes size:m.",
                     "items": [self.item(1, "Fix the thing"), self.item(2)], "more": 1},
                    {"text": "claude is past its quota line."}]}
        doc = page._idle({"idle": idle}, phone=False)
        self.assertIn('data-why="why-0"', doc)
        self.assertIn('data-toggle="why-0"', doc)
        self.assertIn("▾ 3 items", doc)
        self.assertIn("+1 more", doc)
        self.assertIn('href="https://github.com/mkny13/mahler/issues/1"', doc)
        self.assertIn("Fix the thing", doc)
        self.assertNotIn('data-toggle="why-1"', doc)

    def test_freshness_empty_and_malformed_snapshots(self):
        holds = [self.hold("area", area="console")]
        self.assertIn("area:console", self.idle(holds, 180)["reasons"][0]["text"])
        for age in (181, -1):
            self.assertIn("queued, but no platform", self.idle(holds, age)["reasons"][0]["text"])
        self.assertEqual(self.idle([])["reasons"], [])
        for raw in ("broken", "{}", "null", '[1]', '{"at": "oops", "holds": []}'):
            self.led.set_kv("schedule_holds", raw)
            self.assertIsNone(state._schedule_holds(self.led, self.led.now()))

    def test_disabled_projects_and_items_no_longer_pending_are_ignored(self):
        holds = [self.hold("area", 99, area="console"),
                 {"kind": "capacity", "project": "old", "max_parallel": 0}]
        self.assertEqual(self.idle(holds)["reasons"], [])

    def test_existing_slot_and_hot_hold_explanations_are_not_duplicated(self):
        self.led.upsert_item("mahler", 10, state="verifying", pr=20)
        self.cfg["projects"]["mahler"]["max_parallel"] = 1
        holds = [{"kind": "slot", "project": "mahler", "verifying": [10]}]
        reasons = self.idle(holds)["reasons"]
        self.assertEqual(len(reasons), 1)
        self.assertEqual(reasons[0]["action"], "Open PR #20")


class AnswerTests(unittest.TestCase):
    def setUp(self):
        from mahler.console import outbox
        self.outbox = outbox
        self.cfg, self.led = make_cfg(), make_led()
        self.addCleanup(self.led.close)
        self.led.upsert_item('mahler', 9, title='Which?', state='needs_you')
        self.ctx = scheduler.Ctx(self.cfg, self.led)
        self.gh = mock.Mock()
        self.ctx._gh['mkny13/mahler'] = self.gh

    def answer(self, text='Yes', number=9):
        return actions.run(self.cfg, self.led, 'answer',
                           {'project': 'mahler', 'number': number, 'text': text})['id']

    def row(self, id):
        return self.led.q1('SELECT * FROM console_actions WHERE id=?', (id,))

    def test_delay_replace_undo_and_validation(self):
        first = self.answer(' Yes ')
        self.assertEqual(self.row(first)['due_at'], iso(self.led.now() + timedelta(seconds=60)))
        second = self.answer('No')
        self.assertEqual(self.row(first)['status'], 'cancelled')
        actions.run(self.cfg, self.led, 'answer_undo', {'id': second})
        self.led.clock.t += timedelta(seconds=60)
        self.outbox.drain(self.ctx)
        self.gh.comment.assert_not_called()
        with self.assertRaisesRegex(actions.ActionError, 'already sent'):
            actions.run(self.cfg, self.led, 'answer_undo', {'id': second})
        for text in ('', '  ', 'x' * 4001, None, '<!-- mahler:agent --> answer'):
            with self.subTest(text=str(text)[:25]), self.assertRaises(actions.ActionError):
                self.answer(text)
        self.led.set_state('mahler', 9, 'ready')
        with self.assertRaises(actions.ActionError):
            self.answer()
        with self.assertRaises(actions.ActionError):
            actions.run(self.cfg, self.led, 'answer', {'project': 'old', 'number': 1, 'text': 'yes'})

    def test_delivery_and_existing_reply_path(self):
        from mahler.sync import _process_comments
        id = self.answer('Choose <this>')
        self.outbox.drain(self.ctx)
        self.gh.comment.assert_not_called()
        self.led.clock.t += timedelta(seconds=60)
        self.ctx.dry_run = True
        self.outbox.drain(self.ctx)
        self.gh.comment.assert_not_called()
        self.ctx.dry_run = False
        self.outbox.drain(self.ctx)
        self.gh.comment.assert_called_once_with(9, 'Choose <this>', agent=False)
        self.assertEqual(self.row(id)['status'], 'done')
        with self.assertRaisesRegex(actions.ActionError, 'already sent'):
            actions.run(self.cfg, self.led, 'answer_undo', {'id': id})
        _process_comments(self.ctx, 'mahler', self.led.item('mahler', 9),
                          [{'createdAt': iso(self.led.now()), 'body': 'Choose <this>'}])
        self.assertEqual(self.led.item('mahler', 9)['state'], 'inbox')
        self.outbox.drain(self.ctx)
        self.gh.comment.assert_called_once()

    def test_moved_on_and_failures_do_not_stop_next_action(self):
        moved = self.answer()
        self.led.set_state('mahler', 9, 'ready')
        bad = self.led.queue_action('unknown')
        self.led.upsert_item('mahler', 10, state='failed')
        failed = self.answer(number=10)
        self.led.upsert_item('mahler', 11, state='needs_you')
        good = self.answer(number=11)
        self.gh.comment.side_effect = [RuntimeError('x' * 500), None]
        self.led.clock.t += timedelta(seconds=60)
        self.outbox.drain(self.ctx)
        self.assertEqual([self.row(i)['status'] for i in (moved, bad, failed, good)],
                         ['skipped', 'failed', 'failed', 'done'])
        self.assertLessEqual(len(self.row(failed)['result']), 300)
        self.assertEqual(len(self.led.q("SELECT * FROM events WHERE kind='console_action_failed'")), 2)
        with mock.patch.object(self.led, 'due_actions', side_effect=RuntimeError('db gone')):
            self.outbox.drain(self.ctx)

    def test_pending_state_counts_render_and_options(self):
        self.led.upsert_item('mahler', 10, title='Failed', state='failed')
        id = self.answer('<yes>')
        s = state.build(self.cfg, self.led)
        self.assertEqual(s['needs_count'], 1)
        need = next(n for n in s['needs'] if n['number'] == 9)
        self.assertEqual(need['pending'], {'id': id, 'text': '<yes>'})
        failed = next(n for n in s['needs'] if n['number'] == 10)
        self.assertEqual(failed['options'], [{'label': 'Retry', 'text': '/mahler go'},
                                            {'label': 'Park it', 'text': '/mahler park'}])
        doc = page.document(s)
        self.assertEqual(doc.count('You said: &lt;yes&gt;'), 2)
        self.assertIn('data-act="answer_undo"', doc)
        self.assertIn('data-keep="need:mahler#10"', doc)
        self.assertIn('or say something…', doc)
        self.assertIn('or type an answer…', doc)
        self.assertIn('Failed', page._d_side(s))
        self.assertNotIn('Which?', page._d_side(s))
        self.assertEqual(state._answer_options({'options': '["One", "Two"]'}),
                         [{'label': 'One', 'text': 'One'}, {'label': 'Two', 'text': 'Two'}])


    def test_real_gh_wrapper_keeps_human_answer_unmarked(self):
        from mahler.gh import GH, AGENT_MARK
        gh = GH('mkny13/mahler')
        self.ctx._gh['mkny13/mahler'] = gh
        self.answer('/mahler go')
        self.led.clock.t += timedelta(seconds=60)
        with mock.patch.object(gh, '_gh') as call:
            self.outbox.drain(self.ctx)
        self.assertEqual(call.call_args.kwargs['input'], '/mahler go')
        with mock.patch.object(gh, '_gh') as call:
            gh.comment(9, 'agent update')
        self.assertTrue(call.call_args.kwargs['input'].startswith(AGENT_MARK))

    def test_retry_and_park_use_existing_commands(self):
        from mahler.sync import _process_comments
        for text, expected in (('/mahler go', 'ready'), ('/mahler park', 'parked')):
            self.led.set_state('mahler', 9, 'failed', attempts=3)
            self.answer(text)
            self.led.clock.t += timedelta(seconds=60)
            self.outbox.drain(self.ctx)
            _process_comments(self.ctx, 'mahler', self.led.item('mahler', 9),
                              [{'createdAt': iso(self.led.now()), 'body': text}])
            self.assertEqual(self.led.item('mahler', 9)['state'], expected)

    def test_tick_drains_before_burst_even_while_paused(self):
        self.led.set_kv('paused', '1')
        order = []
        names = ('watchdog', 'expire', 'close_finished_parents', 'record_holds',
                 'mirror_labels', 'sync')
        from contextlib import ExitStack
        with ExitStack() as stack:
            for name in names:
                stack.enter_context(mock.patch.object(scheduler, name))
            for mod in (scheduler.digest, scheduler.janitor):
                stack.enter_context(mock.patch.object(mod, 'maybe_send' if mod is scheduler.digest else 'maybe_run'))
            stack.enter_context(mock.patch.object(scheduler, '_project_ok', return_value=False))
            stack.enter_context(mock.patch.object(self.outbox, 'drain', side_effect=lambda ctx: order.append('drain')))
            stack.enter_context(mock.patch.object(scheduler, 'compute_burst', side_effect=lambda *a: order.append('burst')))
            scheduler.tick(self.ctx)
        self.assertEqual(order, ['drain', 'burst'])

    def test_cancel_after_due_snapshot_prevents_send(self):
        id = self.answer()
        self.led.clock.t += timedelta(seconds=60)
        snapshot = self.led.due_actions()
        self.led.cancel_action(id)
        with mock.patch.object(self.led, 'due_actions', return_value=snapshot):
            self.outbox.drain(self.ctx)
        self.gh.comment.assert_not_called()
        self.assertEqual(self.row(id)['status'], 'cancelled')


class CaptureActionTests(unittest.TestCase):
    """Capture: type or dictate an idea, pick a project (mahler#251)."""

    def setUp(self):
        self.cfg, self.led = make_cfg(), make_led()
        self.addCleanup(self.led.close)

    def capture(self, text='Buy milk', project='mahler'):
        return actions.run(self.cfg, self.led, 'capture', {'text': text, 'project': project})['id']

    def test_bad_input_is_refused(self):
        for body in ({}, {'text': 'hi'}, {'project': 'mahler'}, {'text': 'hi', 'project': None},
                     {'text': 'hi', 'project': 7}, {'text': 'hi', 'project': 'old'},
                     {'text': 'hi', 'project': 'nope'}, {'text': None, 'project': 'mahler'},
                     {'text': '   ', 'project': 'mahler'}, {'text': 'x' * 8001, 'project': 'mahler'}):
            with self.assertRaises(actions.ActionError, msg=body):
                actions.run(self.cfg, self.led, 'capture', body)

    def test_accepts_the_full_range(self):
        self.capture(text='x')                  # 1 char
        self.capture(text='x' * 8000)            # 8000 chars

    def test_queues_with_no_delay_and_strips_the_text(self):
        id = self.capture(text='  Buy milk  ')
        row = self.led.q1('SELECT * FROM console_actions WHERE id=?', (id,))
        self.assertEqual(row['kind'], 'capture')
        self.assertEqual(row['project'], 'mahler')
        self.assertIsNone(row['number'])
        self.assertEqual(row['due_at'], row['created_at'])
        self.assertEqual(json.loads(row['payload'])['text'], 'Buy milk')
        ev = self.led.q1("SELECT * FROM events WHERE kind='console_capture_queued'")
        self.assertEqual(json.loads(ev['detail'])['id'], id)


class CaptureOutboxTests(unittest.TestCase):
    def setUp(self):
        from mahler.console import outbox
        self.outbox = outbox
        self.cfg, self.led = make_cfg(), make_led()
        self.addCleanup(self.led.close)
        self.ctx = scheduler.Ctx(self.cfg, self.led)
        self.gh = mock.Mock()
        self.ctx._gh['mkny13/mahler'] = self.gh

    def capture(self, text='Buy milk\nmore detail', project='mahler'):
        return actions.run(self.cfg, self.led, 'capture', {'text': text, 'project': project})['id']

    def row(self, id):
        return self.led.q1('SELECT * FROM console_actions WHERE id=?', (id,))

    def test_creates_an_issue_with_title_body_and_labels(self):
        self.gh.create_issue.return_value = 'https://github.com/mkny13/mahler/issues/42'
        id = self.capture()
        self.outbox.drain(self.ctx)
        title, body, labels = self.gh.create_issue.call_args[0]
        self.assertEqual(title, 'Buy milk')
        self.assertTrue(body.startswith('Buy milk\nmore detail'))
        self.assertTrue(body.endswith('\n\n— captured from the Mahler console'))
        self.assertEqual(labels, ['type:feature', 'p2', 'mahler:inbox'])
        row = self.row(id)
        self.assertEqual(row['status'], 'done')
        self.assertEqual(row['result'], '42')
        ev = self.led.q1("SELECT * FROM events WHERE kind='captured'")
        self.assertEqual(ev['number'], 42)

    def test_title_is_the_first_line_cut_at_a_word_boundary(self):
        self.assertEqual(self.outbox.capture_title('Title line\nrest of the idea'), 'Title line')
        long_word = 'x' * 90
        self.assertEqual(self.outbox.capture_title(long_word), long_word[:80])
        text = ('lorem ipsum ' * 10).strip()
        title = self.outbox.capture_title(text)
        self.assertLessEqual(len(title), 80)
        self.assertTrue(text.startswith(title))
        self.assertFalse(title.endswith(' '))

    def test_scope_label_is_added_when_the_project_scopes_by_label(self):
        cfg = make_cfg(projects={'mahler': {'scope': 'label', 'scope_label': 'triage'}})
        self.gh.create_issue.return_value = 'https://github.com/mkny13/mahler/issues/9'
        actions.run(cfg, self.led, 'capture', {'text': 'hi', 'project': 'mahler'})
        self.ctx.cfg = cfg
        self.outbox.drain(self.ctx)
        labels = self.gh.create_issue.call_args[0][2]
        self.assertIn('triage', labels)

    def test_a_github_failure_marks_the_action_failed(self):
        from mahler.gh import GHError
        self.gh.create_issue.side_effect = GHError('boom')
        id = self.capture()
        self.outbox.drain(self.ctx)
        self.assertEqual(self.row(id)['status'], 'failed')
        ev = self.led.q1("SELECT * FROM events WHERE kind='console_action_failed'")
        self.assertIsNotNone(ev)

    def test_a_disabled_project_is_skipped(self):
        id = self.capture()
        cfg = make_cfg()
        cfg['projects']['mahler']['enabled'] = False
        self.ctx.cfg = cfg
        self.outbox.drain(self.ctx)
        self.gh.create_issue.assert_not_called()
        self.assertEqual(self.row(id)['status'], 'skipped')

    def test_attachment_is_linked(self):
        self.gh.create_issue.return_value = 'https://github.com/mkny13/mahler/issues/43'
        self.ctx.cfg['serve']['public_url'] = 'https://console.example.com/'
        id = actions.run(self.cfg, self.led, 'capture', {'text': 'Idea', 'project': 'mahler', 'attachment': 'some-uuid.png'})['id']
        self.outbox.drain(self.ctx)
        body = self.gh.create_issue.call_args[0][1]
        self.assertIn('\n\nAttachment: [some-uuid.png](https://console.example.com/attachments/some-uuid.png) (`~/.mahler/attachments/some-uuid.png`)', body)


class UatActionTests(unittest.TestCase):
    """Ready to test (mahler#250): the Pass and Fail buttons queue a verdict."""

    def setUp(self):
        self.cfg, self.led = make_cfg(), make_led()
        self.addCleanup(self.led.close)
        self.led.add_uat('mahler', 9, 88, '4c1f0ab', 'Wired the exporter',
                         '- the new ping arrives')

    def row(self, id):
        return self.led.q1('SELECT * FROM console_actions WHERE id=?', (id,))

    def test_bad_input_is_refused(self):
        for body in ({}, {'project': 'mahler'}, {'number': 9},
                     {'project': 'mahler', 'number': 0},
                     {'project': 'mahler', 'number': True},
                     {'project': 'nope', 'number': 9},
                     {'project': 'old', 'number': 9},
                     {'project': 'mahler', 'number': 8}):
            with self.assertRaises(actions.ActionError, msg=body):
                actions.run(self.cfg, self.led, 'uat_pass', body)

    def test_pass_queues_with_no_delay_and_records_the_event(self):
        id = actions.run(self.cfg, self.led, 'uat_pass',
                         {'project': 'mahler', 'number': 9})['id']
        row = self.row(id)
        self.assertEqual((row['kind'], row['project'], row['number'],
                          json.loads(row['payload'])), ('uat_pass', 'mahler', 9, {}))
        self.assertEqual(self.led.due_actions()[0]['id'], id)   # due at once
        ev = self.led.q1("SELECT * FROM events WHERE kind='console_uat_queued'")
        self.assertEqual((ev['project'], ev['number'], json.loads(ev['detail'])['verdict']),
                         ('mahler', 9, 'pass'))

    def test_fail_needs_a_note_of_at_most_2000_chars(self):
        for body in ({'project': 'mahler', 'number': 9},
                     {'project': 'mahler', 'number': 9, 'note': 3},
                     {'project': 'mahler', 'number': 9, 'note': 'x' * 2001}):
            with self.assertRaises(actions.ActionError, msg=body):
                actions.run(self.cfg, self.led, 'uat_fail', body)
        id = actions.run(self.cfg, self.led, 'uat_fail',
                         {'project': 'mahler', 'number': 9, 'note': ''})['id']
        self.assertEqual(json.loads(self.row(id)['payload']), {'note': ''})

    def test_a_second_verdict_is_refused_while_one_is_queued(self):
        actions.run(self.cfg, self.led, 'uat_pass', {'project': 'mahler', 'number': 9})
        with self.assertRaises(actions.ActionError):
            actions.run(self.cfg, self.led, 'uat_fail',
                        {'project': 'mahler', 'number': 9, 'note': 'again'})

    def test_a_second_verdict_is_refused_after_one_is_recorded(self):
        self.led.set_uat_verdict('mahler', 9, 'pass')
        with self.assertRaises(actions.ActionError):
            actions.run(self.cfg, self.led, 'uat_pass', {'project': 'mahler', 'number': 9})


class UatOutboxTests(unittest.TestCase):
    """The tick applies the verdicts: a pass closes it, a fail files the bug."""

    def setUp(self):
        from mahler.console import outbox
        self.outbox = outbox
        self.cfg, self.led = make_cfg(), make_led()
        self.addCleanup(self.led.close)
        self.ctx = scheduler.Ctx(self.cfg, self.led)
        self.gh = mock.Mock()
        self.ctx._gh['mkny13/mahler'] = self.gh
        self.led.add_uat('mahler', 9, 88, '4c1f0ab', 'Wired the exporter',
                         '- the new ping arrives')

    def row(self, id):
        return self.led.q1('SELECT * FROM console_actions WHERE id=?', (id,))

    def uat(self):
        return self.led.uat('mahler', 9)

    def queue(self, kind, **body):
        return actions.run(self.cfg, self.led, kind,
                           {'project': 'mahler', 'number': 9, **body})['id']

    def drain(self):
        self.outbox.drain(self.ctx)

    def test_pass_comments_and_records_the_verdict(self):
        id = self.queue('uat_pass')
        self.drain()
        self.gh.comment.assert_called_once_with(
            9, "✅ **UAT passed** (from the console).", agent=False)
        row = self.uat()
        self.assertEqual(row['verdict'], 'pass')
        self.assertEqual(row['verdict_at'], iso(self.led.now()))
        self.assertEqual(self.row(id)['status'], 'done')
        ev = self.led.q1("SELECT * FROM events WHERE kind='uat_verdict'")
        self.assertEqual((ev['project'], ev['number'], json.loads(ev['detail'])['verdict']),
                         ('mahler', 9, 'pass'))

    def test_fail_files_a_p1_bug_and_routs_it(self):
        self.gh.create_issue.return_value = 'https://github.com/mkny13/mahler/issues/42'
        id = self.queue('uat_fail', note='the ping never arrived')
        self.drain()
        title, body, labels = self.gh.create_issue.call_args[0]
        self.assertEqual(title, 'UAT failed: Wired the exporter')
        self.assertIn('> the ping never arrived', body)
        self.assertIn('Found checking #9 — PR #88, build 4c1f0ab.', body)
        self.assertIn('## Needs a human to check\n- the new ping arrives', body)
        self.assertEqual(labels, ['type:bug', 'p1'])
        self.gh.comment.assert_called_once_with(9, "❌ **UAT failed** — filed #42.",
                                                agent=False)
        row = self.uat()
        self.assertEqual((row['verdict'], row['bug'], row['note']),
                         ('fail', 42, 'the ping never arrived'))
        self.assertEqual(self.row(id)['status'], 'done')
        self.assertEqual(self.row(id)['result'], 'filed #42')
        ev = self.led.q1("SELECT * FROM events WHERE kind='uat_verdict'")
        self.assertEqual((ev['project'], ev['number'], json.loads(ev['detail'])['bug']),
                         ('mahler', 9, 42))

    def test_fail_without_a_note_still_files_the_bug(self):
        self.gh.create_issue.return_value = 'https://github.com/mkny13/mahler/issues/7'
        self.queue('uat_fail', note='')
        self.drain()
        body = self.gh.create_issue.call_args[0][1]
        self.assertNotIn('>', body)
        self.assertIn('Found checking #9 — PR #88, build 4c1f0ab.', body)
        self.assertIsNone(self.uat()['note'])

    def test_fail_labels_the_project_scope(self):
        cfg = make_cfg(projects={'mahler': {'scope': 'label', 'scope_label': 'triage'}})
        self.ctx.cfg = cfg
        self.gh.create_issue.return_value = 'https://github.com/mkny13/mahler/issues/7'
        self.queue('uat_fail', note='nope')
        self.drain()
        self.assertIn('triage', self.gh.create_issue.call_args[0][2])

    def test_a_github_failure_marks_the_action_failed_and_leaves_it_pending(self):
        from mahler.gh import GHError
        self.gh.comment.side_effect = GHError('boom')
        id = self.queue('uat_pass')
        self.drain()
        self.assertEqual(self.row(id)['status'], 'failed')
        self.assertIsNone(self.uat()['verdict'])    # still in the queue

    def test_a_recorded_verdict_is_skipped(self):
        id = self.queue('uat_fail', note='late')
        self.led.set_uat_verdict('mahler', 9, 'pass')   # decided before the tick runs
        self.drain()
        self.gh.create_issue.assert_not_called()
        self.assertEqual((self.row(id)['status'], self.row(id)['result']),
                         ('skipped', 'the verdict is already recorded'))

    def test_a_disabled_project_is_skipped(self):
        self.ctx.cfg = make_cfg()
        self.ctx.cfg['projects']['mahler']['enabled'] = False
        id = self.queue('uat_pass')
        self.drain()
        self.gh.comment.assert_not_called()
        self.assertEqual((self.row(id)['status'], self.row(id)['result']),
                         ('skipped', 'the project is disabled'))

    def test_attachment_is_linked(self):
        self.gh.create_issue.return_value = 'https://github.com/mkny13/mahler/issues/43'
        self.ctx.cfg['serve']['public_url'] = 'https://console.example.com/'
        id = self.queue('uat_fail', note='Idea', attachment='some-uuid.png')
        self.drain()
        body = self.gh.create_issue.call_args[0][1]
        self.assertIn('\n\nAttachment: [some-uuid.png](https://console.example.com/attachments/some-uuid.png) (`~/.mahler/attachments/some-uuid.png`)', body)


class CaptureStateTests(unittest.TestCase):
    def setUp(self):
        self.cfg, self.led = make_cfg(), make_led()
        self.addCleanup(self.led.close)

    def test_dropdown_lists_enabled_projects(self):
        s = state.build(self.cfg, self.led)
        self.assertEqual(s['capture']['projects'], ['mahler', 'groundwork'])

    def test_pending_row_shows_at_the_top_of_the_backlog_group(self):
        actions.run(self.cfg, self.led, 'capture', {'text': 'New idea here', 'project': 'mahler'})
        self.led.upsert_item('mahler', 3, title='An older item', state='ready')
        s = state.build(self.cfg, self.led)
        g = next(g for g in s['backlog'] if g['project'] == 'mahler')
        self.assertEqual(g['items'][0], {'ref': None, 'url': None, 'number': None, 'pr': None, 'pr_url': None, 'title': 'New idea here',
                                         'p': 'p2', 'p1': False, 'priority': 2, 'state': 'inbox', 'tone': 'mut', 'parent': None, 'depends': []})
        self.assertEqual(g['items'][1]['ref'], 'mahler#3')

    def test_the_placeholder_is_gone_once_the_outbox_finishes_it(self):
        from mahler.console import outbox as ob
        actions.run(self.cfg, self.led, 'capture', {'text': 'New idea here', 'project': 'mahler'})
        ctx = scheduler.Ctx(self.cfg, self.led)
        ctx._gh['mkny13/mahler'] = mock.Mock(create_issue=mock.Mock(
            return_value='https://github.com/mkny13/mahler/issues/7'))
        ob.drain(ctx)
        g = next(g for g in state.build(self.cfg, self.led)['backlog'] if g['project'] == 'mahler')
        self.assertEqual(g['items'], [])   # sync() hasn't run yet — no placeholder, no real item

    def test_recent_captures_carry_their_status(self):
        id = self.capture_id()
        s = state.build(self.cfg, self.led)
        self.assertEqual(s['capture']['recent'],
                         [{'id': id, 'project': 'mahler', 'repo': 'mkny13/mahler', 'status': 'pending'}])

    def capture_id(self):
        return actions.run(self.cfg, self.led, 'capture', {'text': 'hi', 'project': 'mahler'})['id']

    def test_recent_drops_off_after_ten_minutes(self):
        self.capture_id()
        self.led.clock.t += timedelta(minutes=11)
        self.assertEqual(state.build(self.cfg, self.led)['capture']['recent'], [])

    def test_a_capture_for_a_now_disabled_project_is_ignored(self):
        self.capture_id()
        cfg = make_cfg()
        cfg['projects']['mahler']['enabled'] = False
        self.assertEqual(state.build(cfg, self.led)['capture']['recent'], [])


class CapturePageTests(unittest.TestCase):
    def setUp(self):
        self.cfg, self.led = make_cfg(), make_led()
        self.addCleanup(self.led.close)

    def test_composer_has_the_dropdown_and_starts_with_save_disabled(self):
        doc = page.document(state.build(self.cfg, self.led))
        self.assertIn('placeholder="Type or dictate."', doc)
        self.assertIn('<option value="" disabled>Project</option>', doc)
        self.assertIn('<option value="mahler">mahler</option>', doc)
        self.assertIn('<option value="groundwork">groundwork</option>', doc)
        self.assertIn('data-act="capture" data-capture-save disabled', doc)
        self.assertIn('class="view view-capture"', doc)

    def test_confirmation_note_after_saving(self):
        actions.run(self.cfg, self.led, 'capture', {'text': 'hi', 'project': 'mahler'})
        doc = page.document(state.build(self.cfg, self.led))
        self.assertIn('Saved to mkny13/mahler as a new issue', doc)
        self.assertIn('It settles 10 minutes before anything picks it up.', doc)

    def test_no_note_without_a_recent_capture(self):
        doc = page.document(state.build(self.cfg, self.led))
        self.assertNotIn('Saved to', doc)


if __name__ == "__main__":
    unittest.main()

    def test_backlog_row_ref_and_pr_links(self):
        self.led.upsert_item("mahler", 1, title="a", state="ready", priority=2, pr=42)
        self.led.upsert_item("mahler", 2, title="b", state="inbox", priority=2)
        self.led.queue_action("capture", project="mahler", payload={"text": "pending cap"})
        s = state.build(self.cfg, self.led)
        html = page.document(s)
        self.assertIn('<span class="ref mono t-mut"></span>', html)
        self.assertIn('<span class="ref mono t-mut"><a href="https://github.com/mkny13/mahler/issues/1" target="_blank">mahler#1</a> <span class="pr-link"><a href="https://github.com/mkny13/mahler/pull/42" target="_blank">PR #42</a></span></span>', html)
        self.assertIn('<span class="ref mono t-mut"><a href="https://github.com/mkny13/mahler/issues/2" target="_blank">mahler#2</a></span>', html)

class GraphTests(unittest.TestCase):
    def setUp(self):
        self.maxDiff = None
        self.cfg, self.led = make_cfg(), make_led()
        
    def tearDown(self):
        self.led.close()

    def test_graph_produces_ranks_and_edges(self):
        self.led.upsert_item('mahler', 1, title='Parent', state='ready')
        self.led.upsert_item('mahler', 2, title='Child1', parent=1, state='ready')
        self.led.upsert_item('mahler', 3, title='Child2', parent=1, depends='[2]', state='ready')
        s = state.build(self.cfg, self.led)
        g = s['dep_graph']['mahler']
        self.assertEqual(len(g['nodes']), 3)
        ranks = {n['number']: n['rank'] for n in g['nodes']}
        self.assertEqual(ranks[1], 0)
        self.assertEqual(ranks[2], 1)
        self.assertEqual(ranks[3], 2)
        edges = sorted((e['from'], e['to'], e['kind']) for e in g['edges'])
        self.assertEqual(edges, [(1, 2, 'parent'), (1, 3, 'parent'), (2, 3, 'depends')])

    def test_qualified_refs_do_not_create_false_local_graph_edges(self):
        self.led.upsert_item("mahler", 1, state="ready")
        self.led.upsert_item("mahler", 2, state="ready", depends=json.dumps([
            {"repo": "else/other", "number": 1},
            {"repo": "mkny13/mahler", "number": 1}]))
        graph = state.build(self.cfg, self.led)["dep_graph"]["mahler"]
        self.assertEqual(graph["edges"], [{"from": 1, "to": 2, "kind": "depends"}])

    def test_graph_terminates_on_cycle(self):
        self.led.upsert_item('mahler', 1, depends='[2]', state='ready')
        self.led.upsert_item('mahler', 2, depends='[1]', state='ready')
        s = state.build(self.cfg, self.led)
        g = s['dep_graph']['mahler']
        self.assertEqual(len(g['nodes']), 2)
        self.assertEqual({n['rank'] for n in g['nodes']}, {0})

    def test_backlog_items_carry_parent_and_depends(self):
        self.led.upsert_item('mahler', 1, state='ready')
        self.led.upsert_item('mahler', 2, parent=1, depends='[1, 999]', state='ready')
        s = state.build(self.cfg, self.led)
        g = next(g for g in s['backlog'] if g['project'] == 'mahler')
        i1 = next(i for i in g['items'] if i['number'] == 1)
        i2 = next(i for i in g['items'] if i['number'] == 2)
        self.assertEqual(i1['parent'], None)
        self.assertEqual(i1['depends'], [])
        self.assertEqual(i2['parent'], 1)
        # 999 is not open
        self.assertEqual(i2['depends'], [1])

    def test_graph_produced_even_if_no_relationships(self):
        self.led.upsert_item('mahler', 1, state='ready')
        self.led.upsert_item('mahler', 2, state='ready')
        s = state.build(self.cfg, self.led)
        g = s['dep_graph']['mahler']
        self.assertEqual(len(g['nodes']), 2)
        self.assertEqual({n['rank'] for n in g['nodes']}, {0})
        self.assertEqual(g['edges'], [])


class ConsoleReleasesStateTests(unittest.TestCase):
    def setUp(self):
        self.cfg, self.led = make_cfg(), make_led()
        self.addCleanup(self.led.close)

    def test_state_build_includes_releases_for_enabled_projects(self):
        s = state.build(self.cfg, self.led)
        self.assertIn("releases", s)
        self.assertIn("releases_suggested", s)
        project_names = [p["project"] for p in s["releases"]]
        self.assertIn("mahler", project_names)
        self.assertIn("groundwork", project_names)
        self.assertNotIn("old", project_names)

    def test_rolling_draft_and_published_releases(self):
        self.led.snapshot_release_item("mahler", 101, pr=11, title="Feat A", summary="Added A",
                                       merge_sha="sha_a", labels=["type:feature"])
        self.led.snapshot_release_item("mahler", 102, pr=12, title="Fix B", summary="Fixed B",
                                       merge_sha="sha_b", labels=["type:bug"])
        self.led.snapshot_release_item("mahler", 103, pr=13, title="Chore C", summary="Cleaned C",
                                       merge_sha="sha_c", labels=["type:chore"])

        self.led.snapshot_release_item("mahler", 90, pr=1, title="Past feat", summary="Past",
                                       merge_sha="sha_past", labels=["type:feature"])
        self.led.create_release("mahler", "0.1.0", checkpoint_sha="sha_past",
                                published_at="2026-09-01T12:00:00Z", remote_url="https://github.com/mkny13/mahler/releases/tag/v0.1.0",
                                item_numbers=[90])

        s = state.build(self.cfg, self.led)
        m_rel = next(r for r in s["releases"] if r["project"] == "mahler")
        draft = m_rel["draft"]
        self.assertGreater(draft["count"], 0)
        self.assertEqual(draft["checkpoint_sha"], "sha_c")
        self.assertEqual(len(draft["features"]), 1)
        self.assertEqual(len(draft["fixes"]), 1)
        self.assertEqual(len(draft["other"]), 0)
        self.assertEqual(len(draft["maintenance"]), 1)
        self.assertEqual(len(m_rel["published"]), 1)
        self.assertEqual(m_rel["published"][0]["version"], "0.1.0")
        self.assertEqual(draft["version_options"]["patch"], "0.1.1")
        self.assertEqual(draft["version_options"]["minor"], "0.2.0")
        self.assertEqual(draft["version_options"]["major"], "1.0.0")

    def test_two_part_baseline_proposes_next_build_version(self):
        self.led.create_release("mahler", "0.83", checkpoint_sha="sha_old",
                                published_at="2026-09-01T12:00:00Z", item_numbers=[])
        self.led.snapshot_release_item("mahler", 101, merge_sha="sha_new",
                                       labels=["type:feature"])
        release = next(r for r in state.build(self.cfg, self.led)["releases"]
                       if r["project"] == "mahler")
        self.assertEqual(release["draft"]["proposed_version"], "0.84")
        self.assertEqual(release["draft"]["version_options"]["proposed"], "0.84")


class CutReleaseActionTests(unittest.TestCase):
    def setUp(self):
        self.cfg, self.led = make_cfg(), make_led()
        self.addCleanup(self.led.close)
        self.led.snapshot_release_item("mahler", 101, pr=11, title="Feat A", summary="Added A",
                                       merge_sha="sha_a", labels=["type:feature"])

    def test_unknown_or_disabled_project_refused(self):
        with self.assertRaisesRegex(actions.ActionError, "project must be enabled"):
            actions.run(self.cfg, self.led, "cut_release", {
                "project": "old", "version": "0.1.0", "checkpoint_sha": "sha_a", "item_numbers": [101]
            })

    def test_invalid_version_refused(self):
        with self.assertRaisesRegex(actions.ActionError, "invalid version"):
            actions.run(self.cfg, self.led, "cut_release", {
                "project": "mahler", "version": "invalid", "checkpoint_sha": "sha_a", "item_numbers": [101]
            })

    def test_version_not_greater_refused(self):
        self.led.create_release("mahler", "1.0.0", checkpoint_sha="sha_old", item_numbers=[])
        with self.assertRaisesRegex(actions.ActionError, "greater"):
            actions.run(self.cfg, self.led, "cut_release", {
                "project": "mahler", "version": "1.0.0", "checkpoint_sha": "sha_a", "item_numbers": [101]
            })
        with self.assertRaisesRegex(actions.ActionError, "greater"):
            actions.run(self.cfg, self.led, "cut_release", {
                "project": "mahler", "version": "0.9.0", "checkpoint_sha": "sha_a", "item_numbers": [101]
            })

    def test_stale_checkpoint_sha_refused(self):
        with self.assertRaisesRegex(actions.ActionError, "draft has changed since preview"):
            actions.run(self.cfg, self.led, "cut_release", {
                "project": "mahler", "version": "0.1.0", "checkpoint_sha": "sha_different", "item_numbers": [101]
            })

    def test_stale_item_numbers_refused(self):
        with self.assertRaisesRegex(actions.ActionError, "draft has changed since preview"):
            actions.run(self.cfg, self.led, "cut_release", {
                "project": "mahler", "version": "0.1.0", "checkpoint_sha": "sha_a", "item_numbers": [101, 102]
            })

    def test_successful_queuing_and_deduplication(self):
        res1 = actions.run(self.cfg, self.led, "cut_release", {
            "project": "mahler", "version": "0.1.0", "checkpoint_sha": "sha_a", "item_numbers": [101]
        })
        self.assertIn("id", res1)
        self.assertFalse(res1.get("deduplicated", False))

        # Double tap / repeat returns deduplicated: True
        res2 = actions.run(self.cfg, self.led, "cut_release", {
            "project": "mahler", "version": "0.1.0", "checkpoint_sha": "sha_a", "item_numbers": [101]
        })
        self.assertTrue(res2.get("deduplicated"))
        self.assertEqual(res1["id"], res2["id"])

        pending = self.led.pending_actions("cut_release")
        self.assertEqual(len(pending), 1)

        ev = self.led.q1("SELECT * FROM events WHERE kind='console_release_queued'")
        self.assertIsNotNone(ev)
        payload = json.loads(ev["detail"])
        self.assertEqual(payload["version"], "0.1.0")

    def test_two_part_version_is_accepted_and_preserved(self):
        self.led.create_release("mahler", "0.83", checkpoint_sha="sha_old", item_numbers=[])
        result = actions.run(self.cfg, self.led, "cut_release", {
            "project": "mahler", "version": "v0.84", "checkpoint_sha": "sha_a",
            "item_numbers": [101],
        })
        payload = json.loads(self.led.q1(
            "SELECT payload FROM console_actions WHERE id=?", (result["id"],))["payload"])
        self.assertEqual(payload["version"], "0.84")


class CutReleaseOutboxTests(unittest.TestCase):
    def setUp(self):
        from mahler.console import outbox
        self.outbox = outbox
        self.cfg, self.led = make_cfg(), make_led()
        self.addCleanup(self.led.close)
        self.led.snapshot_release_item("mahler", 101, pr=11, title="Feat A", summary="Added A",
                                       merge_sha="sha_a", labels=["type:feature"])
        self.ctx = scheduler.Ctx(self.cfg, self.led)

    def test_outbox_publish_release_success(self):
        act_id = self.led.queue_action("cut_release", project="mahler", payload={
            "version": "0.1.0",
            "checkpoint_sha": "sha_a",
            "item_numbers": [101],
            "notes": "Test notes",
        })

        gh_mock = mock.Mock()
        gh_mock.get_release.return_value = None
        gh_mock.get_tag_sha.return_value = None
        gh_mock.release_create.return_value = "https://github.com/mkny13/mahler/releases/tag/v0.1.0"

        with mock.patch.object(self.ctx, "gh", return_value=gh_mock):
            self.outbox.drain(self.ctx)

        # Action is done
        self.assertEqual(len(self.led.pending_actions("cut_release")), 0)
        # Release exists in DB
        rel = self.led.get_release("mahler", "0.1.0")
        self.assertIsNotNone(rel)
        self.assertEqual(rel["remote_url"], "https://github.com/mkny13/mahler/releases/tag/v0.1.0")
        # Event logged
        ev = self.led.q1("SELECT * FROM events WHERE kind='release_published'")
        self.assertIsNotNone(ev)

    def test_outbox_publish_release_conflict_fails_action_and_keeps_draft(self):
        act_id = self.led.queue_action("cut_release", project="mahler", payload={
            "version": "0.1.0",
            "checkpoint_sha": "sha_a",
            "item_numbers": [101],
            "notes": "Test notes",
        })

        # Mock remote tag exists with conflicting SHA
        gh_mock = mock.Mock()
        gh_mock.get_release.return_value = None
        gh_mock.get_tag_sha.return_value = "sha_conflicting"

        with mock.patch.object(self.ctx, "gh", return_value=gh_mock):
            self.outbox.drain(self.ctx)

        # Action is no longer pending (it failed)
        self.assertEqual(len(self.led.pending_actions("cut_release")), 0)
        row = self.led.q1("SELECT * FROM console_actions WHERE id=?", (act_id,))
        self.assertEqual(row["status"], "failed")
        self.assertIn("conflicting", row["result"])
        # Release not created, item 101 remains unsealed
        self.assertIsNone(self.led.get_release("mahler", "0.1.0"))
        items = self.led.unreleased_items("mahler")
        self.assertEqual(len(items), 1)


class ConsoleReleasesPageTests(unittest.TestCase):
    def setUp(self):
        self.cfg, self.led = make_cfg(), make_led()
        self.addCleanup(self.led.close)
        self.led.snapshot_release_item("mahler", 101, pr=11, title="Feat A", summary="Added A",
                                       merge_sha="sha_a", labels=["type:feature"])
        self.led.snapshot_release_item("mahler", 102, pr=12, title="Maint B", summary="Chore B",
                                       merge_sha="sha_b", labels=["type:chore"])

    def test_desktop_and_phone_render_releases(self):
        s = state.build(self.cfg, self.led)
        doc = page.document(s)
        # Desktop view
        self.assertIn('data-view="releases"', doc)
        self.assertIn('view-releases', doc)
        # Phone view
        self.assertIn('tabv-releases', doc)
        self.assertIn('data-tab="releases"', doc)
        # Item rendering
        self.assertIn('Feat A', doc)
        # Maintenance expandable details
        self.assertIn('<details class="rel-maint">', doc)
        self.assertIn('Maint B', doc)
        # Preview modal overlay
        self.assertIn('class="ov releaseov"', doc)
        self.assertIn('Cut release', doc)
        self.assertIn('data-set-ver=', doc)

    def test_cut_release_modal_displays_next_two_part_build_version(self):
        self.led.create_release("mahler", "0.83", checkpoint_sha="sha_old", item_numbers=[])
        doc = page.document(state.build(self.cfg, self.led))
        self.assertIn('Cut release · mahler', doc)
        self.assertIn('value="0.84"', doc)
        self.assertIn('Proposed v0.84', doc)
        self.assertNotIn('Patch v0.84', doc)
        self.assertNotIn('Minor v0.84', doc)
