"""Codex quota protocol and per-account routing (#276); no real login or server."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler import config, platforms, resets, router, scheduler, usage
from mahler.console import state
from mahler.ledger import Ledger


def epoch(dt):
    return int(dt.timestamp())


def response(blocked=False, personal=False):
    return {
        "ordinaryUsageAllowed": not blocked,
        "rateLimits": {
            "primary": {"usedPercent": 100 if blocked else 20,
                        "windowDurationMins": 43200 if personal else 300,
                        "resetsAt": 1900000000},
            "secondary": None if personal else {
                "usedPercent": 58, "windowDurationMins": 10080, "resetsAt": None}},
        "rateLimitResetCredits": {"availableCount": 3, "credits": [
            {"id": "secret-credit-id", "status": "available", "expiresAt": 1900000000},
            {"status": "consumed", "expiresAt": 1800000000}]}}


class CodexProbeTests(unittest.TestCase):
    def probe(self, body=None, mode="ok"):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "codex"
            exe.write_text("#!/usr/bin/env python3\n" + '''
import json, os, sys, time
assert sys.argv[1:] == ["app-server"]
assert os.environ["CODEX_HOME"] == "isolated"
request = json.loads(input())
assert request["method"] == "initialize"
print(json.dumps({"method": "notification"}), flush=True)
print(json.dumps({"id": 1, "result": {}}), flush=True)
assert json.loads(input())["method"] == "initialized"
assert json.loads(input())["method"] == "account/rateLimits/read"
mode = ''' + repr(mode) + '''
if mode == "timeout": time.sleep(5)
if mode == "bad": print("not json", flush=True)
elif mode == "error": print('{"id":2,"error":{}}', flush=True)
elif mode == "partial":
    sys.stdout.write('{"id":2')
    sys.stdout.flush()
    time.sleep(5)
elif mode == "eof": sys.exit(1)
else: print(json.dumps({"id": 2, "result": ''' + repr(body or response()) + '''}), flush=True)
time.sleep(5)
''')
            exe.chmod(0o755)
            # Only the stalling modes need a short deadline; a loaded machine can
            # take over 0.5 s just to start a fresh fake, which reads as a timeout.
            timeout = 0.5 if mode in ("timeout", "partial") else 10
            with mock.patch.object(platforms, "codex_exe", return_value=str(exe)):
                return platforms.probe_codex(env={**os.environ, "CODEX_HOME": "isolated"},
                                             timeout=timeout)

    def test_windows_null_reset_and_sanitized_credits(self):
        result = self.probe()
        self.assertEqual([(w, p) for w, p, _ in result], [("5h", 20), ("weekly", 58)])
        self.assertIsNone(result[1][2])
        self.assertEqual(result.metadata["reset_credits"], 3)
        self.assertEqual(len(result.metadata["credit_expiries"]), 1)
        self.assertNotIn("secret-credit-id", json.dumps(result.metadata))

    def test_personal_nonstandard_window_is_not_a_weekly_sample(self):
        result = self.probe(response(blocked=True, personal=True))
        self.assertEqual([(w, p) for w, p, _ in result], [("43200m", 100)])
        self.assertNotIn("weekly", [w for w, _, _ in result])
        self.assertTrue(result.metadata["blocked"])
        self.assertEqual(result.metadata["windows"][0]["window"], "43200m")

    def test_failures_and_partial_lines_are_bounded(self):
        for mode in ("timeout", "partial", "bad", "error", "eof"):
            with self.subTest(mode=mode):
                self.assertEqual(self.probe(mode=mode), [])

    def test_malformed_shapes_fail_closed(self):
        # Malformed or unsupported shapes emit no routing samples
        incomplete = {"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 20}}}
        unsupported_multi = {"rateLimits": {
            "primary": {"windowDurationMins": 300, "usedPercent": 20},
            "secondary": {"windowDurationMins": 43200, "usedPercent": 20}}}
        short_single = {"rateLimits": {"primary": {"windowDurationMins": 10, "usedPercent": 20}}}
        for body in ([1], {"rateLimits": []}, {"rateLimits": {"primary": {}}},
                     incomplete, unsupported_multi, short_single):
            with self.subTest(body=body):
                self.assertEqual(self.probe(body), [])


class CodexRefreshTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 16, tzinfo=timezone.utc)
        self.led = Ledger(":memory:", clock=lambda: self.now)
        self.addCleanup(self.led.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.personal = str(Path(self.tmp.name) / "personal")
        self.work = str(Path(self.tmp.name) / "work")
        for home in (self.personal, self.work):
            Path(home).mkdir()
            (Path(home) / "auth.json").write_text("{}")
        self.cfg = config.resolve_platforms(config._merge(copy.deepcopy(config.DEFAULTS), {
            "routing": {"sort": [], "build": ["codex", "codex-high"], "plan": []},
            "platforms": {"codex-work": {"from": "codex", "account": "work"}},
            "accounts": {
                "personal": {"env": {"CODEX_HOME": self.personal}},
                "work": {"env": {"CODEX_HOME": self.work},
                         "routing": {"build": ["codex-work"]}}},
            "projects": {"p": {"accounts": ["personal", "work"]}}}))
        self.led.upsert_item("p", 1, state="ready", priority=2)
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=True)
        self.projects = [config.project_policy(self.cfg, "p")]

    def refresh(self, result=None):
        with mock.patch.object(platforms, "probe_codex", return_value=(
                platforms._codex_usage(response()) if result is None else result)) as probe:
            usage.refresh_usage(self.ctx, self.projects)
        return probe

    def test_accounts_peers_throttle_and_retry(self):
        probe = self.refresh()
        self.assertEqual(probe.call_count, 2)
        self.assertEqual({c.kwargs["env"]["CODEX_HOME"] for c in probe.call_args_list},
                         {self.personal, self.work})
        for name in ("codex", "codex-high", "codex-work"):
            self.assertEqual(self.led.usage(name)["weekly"]["used_pct"], 58)
            self.assertEqual(router.usage_state(self.led, name, self.cfg["platforms"][name])[0], "ok")
        self.refresh().assert_not_called()
        self.now += timedelta(minutes=16)
        self.assertEqual(self.refresh([]).call_count, 2)
        self.refresh([]).assert_not_called()
        self.assertEqual(router.usage_state(self.led, "codex", self.cfg["platforms"]["codex"])[0], "stale")

    def test_missing_login_or_home_and_unmetered_are_skipped(self):
        (Path(self.work) / "auth.json").unlink()
        self.refresh().assert_called_once()
        self.cfg["accounts"]["work"]["env"] = {}
        self.refresh().assert_not_called()
        self.cfg["platforms"]["codex"]["metered"] = False
        with mock.patch.object(platforms, "probe_codex") as probe:
            usage.refresh_codex(self.cfg, self.led, "codex", force=True)
        probe.assert_not_called()

    def test_blocked_personal_and_reset_credits_visible_then_recover(self):
        self.refresh(platforms._codex_usage(response(blocked=True, personal=True)))
        pc = self.cfg["platforms"]["codex"]
        status, detail = router.usage_state(self.led, "codex", pc)
        self.assertEqual(status, "hard")
        self.assertIn("43200m 100%", detail)
        self.assertIn("1 banked reset (next expires", detail)
        self.assertIn("2 other reset credits", detail)
        rows = state._quota(self.cfg, self.led, None)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertIn("1 banked reset (next expires", row["detail"])
            self.assertFalse(row["available"])
        self.now += timedelta(minutes=16)
        self.refresh()
        self.assertEqual(router.usage_state(self.led, "codex", pc)[0], "ok")

    def test_blocked_with_low_usage_still_hard(self):
        body = response()
        body["ordinaryUsageAllowed"] = False
        self.refresh(platforms._codex_usage(body))
        self.assertEqual(router.usage_state(self.led, "codex", self.cfg["platforms"]["codex"])[0], "hard")

    def test_personal_does_not_inherit_work_home(self):
        del self.cfg["accounts"]["personal"]
        with mock.patch.dict(os.environ, {"CODEX_HOME": self.work, "OPENAI_API_KEY": "secret"}), \
                mock.patch.object(usage.os.path, "isfile", return_value=True), \
                mock.patch.object(platforms, "probe_codex", return_value=[]) as probe:
            usage.refresh_codex(self.cfg, self.led, "codex")
        env = probe.call_args.kwargs["env"]
        self.assertEqual(env["CODEX_HOME"], os.path.expanduser("~/.codex"))
        self.assertNotIn("OPENAI_API_KEY", env)

    def test_notify_when_codex_exhausted_and_cleared_on_recovery(self):
        with mock.patch("mahler.notify.send") as notify_send:
            # 1. Exhausted with 3 reset credits -> sends high priority notification
            exhausted_usage = platforms._codex_usage(response(blocked=True))
            with mock.patch.object(platforms, "probe_codex", return_value=exhausted_usage):
                usage.refresh_codex(self.cfg, self.led, "codex-work", force=True)
            notify_send.assert_called_once()
            args, kwargs = notify_send.call_args
            self.assertEqual(args[1], "Codex (work) quota exhausted")
            self.assertIn("3 reset credits available", args[2])
            self.assertIn("Hit reset in ChatGPT to refresh", args[2])
            self.assertEqual(kwargs.get("priority"), "high")
            self.assertEqual(kwargs.get("tags"), "warning,hourglass")
            self.assertTrue(self.led.get_kv("notified:codex-exhausted:work"))

            # 2. Duplicate probe while still exhausted does not send again
            notify_send.reset_mock()
            with mock.patch.object(platforms, "probe_codex", return_value=exhausted_usage):
                usage.refresh_codex(self.cfg, self.led, "codex-work", force=True)
            notify_send.assert_not_called()

            # 3. Recovers -> clears the notified flag
            ok_usage = platforms._codex_usage(response(blocked=False))
            with mock.patch.object(platforms, "probe_codex", return_value=ok_usage):
                usage.refresh_codex(self.cfg, self.led, "codex-work", force=True)
            notify_send.assert_not_called()
            self.assertEqual(self.led.get_kv("notified:codex-exhausted:work"), "")

    def test_notify_when_codex_exhausted_zero_credits(self):
        with mock.patch("mahler.notify.send") as notify_send:
            body = response(blocked=True)
            body["rateLimitResetCredits"]["availableCount"] = 0
            exhausted_usage = platforms._codex_usage(body)
            with mock.patch.object(platforms, "probe_codex", return_value=exhausted_usage):
                usage.refresh_codex(self.cfg, self.led, "codex-work", force=True)
            notify_send.assert_called_once()
            args, _ = notify_send.call_args
            self.assertEqual(args[1], "Codex (work) quota exhausted")
            self.assertIn("no reset credits remaining", args[2])

    def test_single_long_window_routed_and_thresholds(self):
        # 1. Unblocked 43200m at 20%: ok and routed
        self.refresh(platforms._codex_usage(response(blocked=False, personal=True)))
        pc = self.cfg["platforms"]["codex"]
        status, detail = router.usage_state(self.led, "codex", pc)
        self.assertEqual(status, "ok")
        self.assertIn("43200m 20%", detail)
        name, _ = router.pick(self.cfg, self.led, "build")
        self.assertEqual(name, "codex")

        # 2. At 70%: soft, not picked
        self.now += timedelta(minutes=16)
        body = response(blocked=False, personal=True)
        body["rateLimits"]["primary"]["usedPercent"] = 70
        self.refresh(platforms._codex_usage(body))
        status, detail = router.usage_state(self.led, "codex", pc)
        self.assertEqual(status, "soft")
        self.assertIn("43200m 70%", detail)
        name, reasons = router.pick(self.cfg, self.led, "build")
        self.assertNotEqual(name, "codex")
        self.assertTrue(any("codex: soft" in r for r in reasons))

        # 3. At 90%: hard, not picked
        self.now += timedelta(minutes=16)
        body["rateLimits"]["primary"]["usedPercent"] = 90
        self.refresh(platforms._codex_usage(body))
        status, detail = router.usage_state(self.led, "codex", pc)
        self.assertEqual(status, "hard")
        self.assertIn("43200m 90%", detail)
        name, reasons = router.pick(self.cfg, self.led, "build")
        self.assertNotEqual(name, "codex")
        self.assertTrue(any("codex: hard" in r for r in reasons))

        # 4. Explicitly blocked with low usage: still hard
        self.now += timedelta(minutes=16)
        body = response(blocked=True, personal=True)
        body["rateLimits"]["primary"]["usedPercent"] = 20
        self.refresh(platforms._codex_usage(body))
        status, _ = router.usage_state(self.led, "codex", pc)
        self.assertEqual(status, "hard")

    def test_unsupported_and_incomplete_shapes_remain_stale(self):
        pc = self.cfg["platforms"]["codex"]
        # Incomplete shape: only 5h window
        body_inc = {"ordinaryUsageAllowed": True,
                    "rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 20}}}
        self.refresh(platforms._codex_usage(body_inc))
        status, detail = router.usage_state(self.led, "codex", pc)
        self.assertEqual(status, "stale")
        self.assertIn("unrecognized or incomplete", detail)

        # Unsupported multi-window: 5h + 43200m
        body_multi = {"ordinaryUsageAllowed": True, "rateLimits": {
            "primary": {"windowDurationMins": 300, "usedPercent": 20},
            "secondary": {"windowDurationMins": 43200, "usedPercent": 20}}}
        self.refresh(platforms._codex_usage(body_multi))
        status, detail = router.usage_state(self.led, "codex", pc)
        self.assertEqual(status, "stale")
        self.assertIn("unrecognized or incomplete", detail)

        # Short single window: 10m
        body_short = {"ordinaryUsageAllowed": True,
                      "rateLimits": {"primary": {"windowDurationMins": 10, "usedPercent": 20}}}
        self.refresh(platforms._codex_usage(body_short))
        status, detail = router.usage_state(self.led, "codex", pc)
        self.assertEqual(status, "stale")
        self.assertIn("unrecognized or incomplete", detail)

    def test_unsupported_shape_alerts_after_four_hours_and_cleared(self):
        unsupported = platforms._codex_usage({
            "ordinaryUsageAllowed": True,
            "rateLimits": {"primary": {"windowDurationMins": 10, "usedPercent": 20}}})

        # 1. Four hours of continuous fresh probes triggers exactly one alert; no repeats
        with mock.patch("mahler.notify.send") as notify_send:
            with mock.patch.object(platforms, "probe_codex", return_value=unsupported):
                # t = 0
                usage.refresh_codex(self.cfg, self.led, "codex-work", force=True)
                notify_send.assert_not_called()
                # t = 2h (8 * 15m) -> no early alert
                for _ in range(8):
                    self.now += timedelta(minutes=15)
                    usage.refresh_codex(self.cfg, self.led, "codex-work", force=True)
                notify_send.assert_not_called()
                # t = 4h (another 8 * 15m) -> exactly one alert
                for _ in range(8):
                    self.now += timedelta(minutes=15)
                    usage.refresh_codex(self.cfg, self.led, "codex-work", force=True)
                notify_send.assert_called_once()
                args, kwargs = notify_send.call_args
                self.assertIn("work", args[1] + args[2])
                self.assertIn("codex-work", args[1] + args[2])
                self.assertIn("10m", args[1] + args[2])
                self.assertEqual(kwargs.get("priority"), "high")

                # Duplicate refresh after 4h does not send again
                notify_send.reset_mock()
                self.now += timedelta(minutes=15)
                usage.refresh_codex(self.cfg, self.led, "codex-work", force=True)
                notify_send.assert_not_called()

        # 2. Failed probe clears pending condition
        self.led.set_kv("codex:unsupported-shape:first:work", "")
        self.led.set_kv("codex:unsupported-shape:shape:work", "")
        self.led.set_kv("codex:unsupported-shape:last:work", "")
        self.led.set_kv("notified:codex-unsupported:work", "")
        with mock.patch("mahler.notify.send") as notify_send:
            for _ in range(8):
                self.now += timedelta(minutes=15)
                with mock.patch.object(platforms, "probe_codex", return_value=unsupported):
                    usage.refresh_codex(self.cfg, self.led, "codex-work", force=True)
            # Failed probe at 2h
            self.now += timedelta(minutes=15)
            with mock.patch.object(platforms, "probe_codex", return_value=[]):
                usage.refresh_codex(self.cfg, self.led, "codex-work", force=True)
            # Another 2h: no alert because counter was cleared
            for _ in range(8):
                self.now += timedelta(minutes=15)
                with mock.patch.object(platforms, "probe_codex", return_value=unsupported):
                    usage.refresh_codex(self.cfg, self.led, "codex-work", force=True)
            notify_send.assert_not_called()

        # 3. Supported shape clears pending condition
        self.led.set_kv("codex:unsupported-shape:first:work", "")
        self.led.set_kv("codex:unsupported-shape:shape:work", "")
        self.led.set_kv("codex:unsupported-shape:last:work", "")
        self.led.set_kv("notified:codex-unsupported:work", "")
        with mock.patch("mahler.notify.send") as notify_send:
            for _ in range(8):
                self.now += timedelta(minutes=15)
                with mock.patch.object(platforms, "probe_codex", return_value=unsupported):
                    usage.refresh_codex(self.cfg, self.led, "codex-work", force=True)
            # Supported shape returns
            self.now += timedelta(minutes=15)
            with mock.patch.object(platforms, "probe_codex", return_value=platforms._codex_usage(response())):
                usage.refresh_codex(self.cfg, self.led, "codex-work", force=True)
            # Another 2h: no alert
            for _ in range(8):
                self.now += timedelta(minutes=15)
                with mock.patch.object(platforms, "probe_codex", return_value=unsupported):
                    usage.refresh_codex(self.cfg, self.led, "codex-work", force=True)
            notify_send.assert_not_called()

        # 4. Shape change clears pending condition
        self.led.set_kv("codex:unsupported-shape:first:work", "")
        self.led.set_kv("codex:unsupported-shape:shape:work", "")
        self.led.set_kv("codex:unsupported-shape:last:work", "")
        self.led.set_kv("notified:codex-unsupported:work", "")
        unsupported_20 = platforms._codex_usage({
            "ordinaryUsageAllowed": True,
            "rateLimits": {"primary": {"windowDurationMins": 20, "usedPercent": 20}}})
        with mock.patch("mahler.notify.send") as notify_send:
            for _ in range(8):
                self.now += timedelta(minutes=15)
                with mock.patch.object(platforms, "probe_codex", return_value=unsupported):
                    usage.refresh_codex(self.cfg, self.led, "codex-work", force=True)
            # Shape changes to 20m for 2h
            for _ in range(8):
                self.now += timedelta(minutes=15)
                with mock.patch.object(platforms, "probe_codex", return_value=unsupported_20):
                    usage.refresh_codex(self.cfg, self.led, "codex-work", force=True)
            notify_send.assert_not_called()


class CodexResetSpendTests(unittest.TestCase):
    """Banked (expiring) reset credits are spent by the tick, never purchased
    ones (mahler#557, D35); no real login or server."""

    def setUp(self):
        self.now = datetime(2026, 9, 16, tzinfo=timezone.utc)
        self.led = Ledger(":memory:", clock=lambda: self.now)
        self.addCleanup(self.led.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.personal = str(Path(self.tmp.name) / "personal")
        self.work = str(Path(self.tmp.name) / "work")
        for home in (self.personal, self.work):
            Path(home).mkdir()
            (Path(home) / "auth.json").write_text("{}")
        self.cfg = config.resolve_platforms(config._merge(copy.deepcopy(config.DEFAULTS), {
            "routing": {"sort": [], "build": ["codex", "codex-high"], "plan": []},
            "platforms": {"codex-work": {"from": "codex", "account": "work"}},
            "accounts": {
                "personal": {"env": {"CODEX_HOME": self.personal}},
                "work": {"env": {"CODEX_HOME": self.work},
                         "routing": {"build": ["codex-work"]}}},
            "projects": {"p": {"accounts": ["personal", "work"]}}}))
        # Isolate the work login: the pass decides per login, so the tests
        # route the ready project to the work account only.
        self.cfg["projects"]["p"]["accounts"] = ["work"]
        self.led.upsert_item("p", 1, state="ready", priority=2)
        self.projects = [config.project_policy(self.cfg, "p")]

    def banked(self, weekly_pct=58, weekly_reset=None, credit=None):
        if credit is None:
            credit = {"id": "credit-1", "status": "available",
                      "expiresAt": epoch(self.now + timedelta(days=7))}
        return {
            "ordinaryUsageAllowed": True,
            "rateLimits": {
                "primary": {"usedPercent": 20, "windowDurationMins": 300,
                            "resetsAt": epoch(self.now + timedelta(hours=5))},
                "secondary": {"usedPercent": weekly_pct,
                              "windowDurationMins": 10080,
                              "resetsAt": epoch(weekly_reset) if weekly_reset else None}},
            "rateLimitResetCredits": {"availableCount": 1, "credits": [credit]}}

    def seed(self, work=None, personal=None):
        bodies = {self.personal: personal if personal is not None else response(),
                  self.work: work if work is not None else response()}

        def probe(*args, **kwargs):
            return platforms._codex_usage(bodies[kwargs["env"]["CODEX_HOME"]])

        with mock.patch.object(platforms, "probe_codex", side_effect=probe):
            usage.refresh_usage(self.ctx(), self.projects)

    def ctx(self, dry_run=False):
        return scheduler.Ctx(self.cfg, self.led, dry_run=dry_run)

    def run_pass(self, read=None, consume_result="reset", dry_run=False):
        body = read if read is not None else response()
        # The pass's targeting read is made with keep_ids=True; a raw body is
        # parsed here the same way (credit ids present, never persisted).
        read = platforms._codex_usage(body, keep_ids=True) if isinstance(body, dict) else body
        with mock.patch.object(platforms, "probe_codex", return_value=read) as probe, \
                mock.patch.object(platforms, "consume_codex_credit",
                                  return_value=consume_result) as consume, \
                mock.patch("mahler.notify.send") as ping:
            resets.spend_banked(self.ctx(dry_run), self.projects)
        return consume, probe, ping

    def test_dry_run_describes_the_spend_and_never_spends(self):
        # A diagnostic tick must not consume a real reset: the guard fires
        # before attempt state is written or the consume RPC is called.
        self.seed(work=self.banked(weekly_pct=95,
                                   weekly_reset=self.now + timedelta(hours=72)))
        consume, probe, ping = self.run_pass(dry_run=True)
        probe.assert_called_once()      # the targeting read still runs
        probe.assert_called_with(keep_ids=True, env=mock.ANY)
        consume.assert_not_called()
        ping.assert_not_called()
        self.assertIsNone(self.led.get_kv("reset:attempt:work"))
        self.assertEqual(self.led.q(
            "SELECT detail FROM events WHERE kind='reset_spent'"), [])
        # The standing weekly reading is untouched, too.
        self.assertEqual(self.led.usage("codex-work")["weekly"]["used_pct"], 95)

    def test_rule_a_spends_when_full_with_ready_work(self):
        self.seed(work=self.banked(weekly_pct=95,
                                   weekly_reset=self.now + timedelta(hours=72)))
        consume, probe, ping = self.run_pass(
            read=self.banked(weekly_pct=0, weekly_reset=self.now + timedelta(days=7)))
        consume.assert_called_once()
        kwargs = consume.call_args.kwargs
        self.assertEqual(kwargs["credit_id"], "credit-1")
        self.assertEqual(kwargs["env"]["CODEX_HOME"], self.work)
        self.assertTrue(kwargs["idempotency_key"])
        # The targeting read may carry credit ids; the standing probe may not.
        self.assertTrue(probe.call_args_list[0].kwargs.get("keep_ids"))
        ping.assert_called_once()
        self.assertEqual(ping.call_args.args[1], "Codex (work): banked reset spent")
        self.assertIn("rule a", ping.call_args.args[2])
        self.assertIn("weekly window now resets", ping.call_args.args[2])
        # The fresh reading is fanned out across the login's quota group.
        self.assertEqual(self.led.usage("codex-work")["weekly"]["used_pct"], 0)
        self.assertTrue(self.led.get_kv("notified:reset-spent:work:credit-1"))
        self.assertEqual(len(self.led.q(
            "SELECT detail FROM events WHERE kind='reset_spent'")), 1)

    def test_rule_b_spends_when_credit_expiring_into_a_used_week(self):
        self.seed(work=self.banked(
            weekly_pct=70, weekly_reset=self.now + timedelta(hours=72),
            credit={"id": "credit-1", "status": "available",
                    "expiresAt": epoch(self.now + timedelta(hours=24))}))
        consume, _, ping = self.run_pass(
            read=self.banked(weekly_pct=0, weekly_reset=self.now + timedelta(days=7)))
        consume.assert_called_once()
        self.assertIn("rule b", ping.call_args.args[2])

    def test_credits_without_expiry_are_never_spent(self):
        # availableCount > 0 but no expiring row: the banked kind cannot be
        # proven, so nothing is spent and nothing is pinged.
        self.seed(work=self.banked(
            weekly_pct=95, weekly_reset=self.now + timedelta(hours=72),
            credit={"id": "secret", "status": "available", "expiresAt": None}))
        consume, probe, ping = self.run_pass()
        consume.assert_not_called()
        ping.assert_not_called()
        self.assertEqual(self.led.q(
            "SELECT detail FROM events WHERE kind LIKE 'reset_%'"), [])

    def test_stale_reading_decides_nothing(self):
        self.seed(work=self.banked(weekly_pct=95,
                                   weekly_reset=self.now + timedelta(hours=72)))
        self.now += timedelta(minutes=16)
        consume, _, ping = self.run_pass()
        consume.assert_not_called()
        ping.assert_not_called()

    def test_expiring_unspent_pings_once(self):
        body = self.banked(
            weekly_pct=30, weekly_reset=self.now + timedelta(hours=72),
            credit={"id": "credit-1", "status": "available",
                    "expiresAt": epoch(self.now + timedelta(hours=24))})
        self.seed(work=body)
        consume, _, ping = self.run_pass()
        consume.assert_not_called()
        ping.assert_called_once()
        self.assertIn("banked reset expiring", ping.call_args.args[1])
        self.assertEqual(len(self.led.q(
            "SELECT detail FROM events WHERE kind='reset_expiring'")), 1)
        # A later tick in the same window stays silent.
        self.run_pass()[2].assert_not_called()

    def test_failed_call_backs_off_then_retries_with_same_key(self):
        self.seed(work=self.banked(weekly_pct=95,
                                   weekly_reset=self.now + timedelta(hours=72)))
        consume, _, _ = self.run_pass(consume_result=None)
        consume.assert_called_once()
        key = consume.call_args.kwargs["idempotency_key"]
        # A retry within the back-off window is skipped entirely.
        self.run_pass(consume_result=None)[0].assert_not_called()
        self.now += timedelta(minutes=31)
        self.seed(work=self.banked(weekly_pct=95,
                                   weekly_reset=self.now + timedelta(hours=72)))
        consume2, _, _ = self.run_pass(consume_result=None)
        consume2.assert_called_once()
        self.assertEqual(consume2.call_args.kwargs["idempotency_key"], key)

    def test_consume_requests_named_credit_and_parses_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "codex"
            exe.write_text("#!/usr/bin/env python3\n" + '''
import json, os, sys, time
assert os.environ["CODEX_HOME"] == "isolated"
assert json.loads(input())["method"] == "initialize"
print(json.dumps({"id": 1, "result": {}}), flush=True)
assert json.loads(input())["method"] == "initialized"
request = json.loads(input())
assert request["method"] == "account/rateLimitResetCredit/consume"
assert request["params"] == {"creditId": "credit-1", "idempotencyKey": "k-123"}
print(json.dumps({"id": 2, "result": {"outcome": "reset"}}), flush=True)
time.sleep(5)
''')
            exe.chmod(0o755)
            with mock.patch.object(platforms, "codex_exe", return_value=str(exe)):
                self.assertEqual(platforms.consume_codex_credit(
                    env={**os.environ, "CODEX_HOME": "isolated"},
                    credit_id="credit-1", idempotency_key="k-123"), "reset")
        # Fails closed: no credit named, no call is made.
        self.assertIsNone(platforms.consume_codex_credit(
            credit_id=None, idempotency_key="k-123"))

    def test_consume_error_response_is_not_a_spend(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "codex"
            exe.write_text("#!/usr/bin/env python3\n" + '''
import json, os, sys, time
assert json.loads(input())["method"] == "initialize"
print(json.dumps({"id": 1, "result": {}}), flush=True)
assert json.loads(input())["method"] == "initialized"
print(json.dumps({"id": 2, "error": {"code": -1}}), flush=True)
time.sleep(5)
''')
            exe.chmod(0o755)
            with mock.patch.object(platforms, "codex_exe", return_value=str(exe)):
                self.assertIsNone(platforms.consume_codex_credit(
                    env={**os.environ, "CODEX_HOME": "isolated"},
                    credit_id="credit-1", idempotency_key="k-123"))

    def test_banked_resets_surface_in_capacity_detail(self):
        self.seed(work=self.banked(
            weekly_pct=70, weekly_reset=self.now + timedelta(hours=72),
            credit={"id": "credit-1", "status": "available",
                    "expiresAt": epoch(self.now + timedelta(hours=24))}))
        detail = router.codex_detail(self.led, "codex-work",
                                     self.cfg["platforms"]["codex-work"])
        self.assertIn("1 banked reset (next expires 1d 0h)", detail)

