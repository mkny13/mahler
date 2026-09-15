"""The operator console (DESIGN D27): its state, its copy, its page, its writes."""

import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler import config, router
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
        self.assertEqual(banners[2]["text"], "You have uncommitted edits in mahler from 4 minutes "
                                             "ago. No new runs start there until 20 minutes after "
                                             "you stop. Work in flight continues.")
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


class PeakOverrideTests(unittest.TestCase):
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

    def test_header_only_inside_the_window(self):
        peak = state.build(make_cfg(), make_led(SAT_NOON))["peak"]
        self.assertFalse(peak["header"])
        self.assertEqual(peak["line"], "Claude peak hours 05:00–11:00 PT — planning only, "
                                       "free tiers build")

    def test_refused_when_the_window_is_off(self):
        cfg = make_cfg(claude_peak={"enabled": False})
        with self.assertRaises(actions.ActionError):
            actions.run(cfg, make_led(), "peak_override", {})
        self.assertIsNone(state.build(cfg, make_led())["peak"])


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


class PageTests(unittest.TestCase):
    def setUp(self):
        self.cfg, self.led = make_cfg(), make_led()
        self.led.upsert_item("mahler", 9, title="A <script>alert(1)</script> title",
                             state="ready")
        self.led.set_state("mahler", 9, "needs_you", "Pick <b>one</b>?")

    def test_document_has_both_layouts_and_lands_on_needs(self):
        doc = page.document(state.build(self.cfg, self.led))
        self.assertIn('<div class="dk">', doc)
        self.assertIn('<div class="ph">', doc)
        self.assertIn('data-view="needs" data-tab="triage"', doc)
        self.assertIn("Pick &lt;b&gt;one&lt;/b&gt;?", doc)
        self.assertNotIn("<script>alert", doc)
        self.assertIn('href="https://github.com/mkny13/mahler/issues/9"', doc)

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


if __name__ == "__main__":
    unittest.main()
