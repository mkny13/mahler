"""Banked Claude rate-limit resets (mahler#1598, D35): cedar_ember parsing, the
spend rules, the reset_rate_limits call and the Capacity detail. No network,
no real ~/.claude, no real keychain."""
import copy
import io
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from mahler import config, platforms, resets, router, scheduler, usage
from mahler.console import state
from mahler.ledger import Ledger

ORG = "11111111-2222-3333-4444-555555555555"


def grant(gid="grant_1", left=1, ends=None, usable=True, paused=False, **extra):
    g = {"id": gid, "resets_left": left, "usable_now": usable, "paused": paused,
         "ends_at": ends, "clears": ["five_hour", "seven_day"], "type": "banked",
         "gives_back_weekly": True}
    g.update(extra)
    return g


def body(weekly_pct=50.0, weekly_reset=None, grants=None, cedar="default"):
    out = {"five_hour": {"utilization": 20.0, "resets_at": None},
           "seven_day": {"utilization": weekly_pct,
                         "resets_at": weekly_reset.isoformat() if weekly_reset else None}}
    if cedar == "default":
        out["cedar_ember"] = {"eligible": True, "grants": grants or [],
                              "next_grant_id": None}
    elif cedar != "missing":
        out["cedar_ember"] = cedar
    return out


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class CedarEmberParsingTests(unittest.TestCase):
    def test_usable_grants_are_counted_with_sorted_expiries_and_no_ids(self):
        u = platforms._claude_usage(body(grants=[
            grant("b", 2, "2026-09-20T00:00:00Z"),
            grant("a", 1, "2026-09-18T00:00:00+00:00"),
            grant("c", 3, None),
            grant("paused", 5, "2026-09-17T00:00:00Z", paused=True),
            grant("later", 5, "2026-09-17T00:00:00Z", usable=False),
            grant("empty", 0, "2026-09-17T00:00:00Z")]))
        self.assertIsInstance(u, platforms.ClaudeUsage)
        self.assertEqual(list(u), [("5h", 20.0, None), ("weekly", 50.0, None)])
        self.assertEqual(u.metadata["reset_credits"], 6)
        self.assertEqual(u.metadata["credit_expiries"],
                         ["2026-09-18T00:00:00+00:00", "2026-09-20T00:00:00+00:00"])
        self.assertNotIn("credit_rows", u.metadata)    # ids never leave the read
        rows = platforms._claude_usage(body(grants=[grant("a", 1)]), keep_ids=True)
        self.assertEqual(rows.metadata["credit_rows"][0]["id"], "a")

    def test_missing_null_and_ineligible_mean_no_credits(self):
        for b in (body(cedar="missing"), body(cedar=None),
                  body(cedar={"eligible": False, "grants": [grant()]}),
                  body(cedar={"grants": [grant()]}),
                  body(cedar={"eligible": True})):
            u = platforms._claude_usage(b, keep_ids=True)
            self.assertEqual(u.metadata["reset_credits"], 0)
            self.assertEqual(u.metadata["credit_expiries"], [])
            self.assertEqual(u.metadata["credit_rows"], [])
            self.assertEqual(len(u), 2)                # windows still read

    def test_malformed_cedar_ember_fails_closed(self):
        bad = [grant(gid="Has Caps"), grant(gid="x" * 41), grant(gid=7),
               grant(left=-1), grant(left=True), grant(left="1"),
               grant(ends="not a date"), grant(ends=12345),
               grant(paused="no"), grant(usable=1), grant(clears="all"),
               grant(gives_back_weekly="yes"), "not a grant"]
        for g in bad:
            u = platforms._claude_usage(body(grants=[grant("good", 4), g]))
            self.assertEqual(u.metadata["reset_credits"], 0, g)
        for cedar in ("yes", [], {"eligible": True, "grants": "many"}):
            self.assertEqual(platforms._claude_usage(
                body(cedar=cedar)).metadata["reset_credits"], 0)

    def test_oauth_usage_asks_for_cedar_ember_and_returns_metadata(self):
        payload = json.dumps(body(grants=[grant("g", 1, "2026-09-18T00:00:00Z")])).encode()
        with mock.patch.object(platforms, "claude_oauth_token", return_value="tok"), \
                mock.patch("urllib.request.urlopen",
                           return_value=FakeResponse(payload)) as urlopen:
            u = platforms.oauth_usage(keychain_service=None, credentials_file="x")
        req = urlopen.call_args.args[0]
        self.assertEqual(req.full_url,
                         "https://api.anthropic.com/api/oauth/usage?cedar_ember=1")
        self.assertEqual(req.get_header("Anthropic-beta"), "oauth-2025-04-20")
        self.assertEqual(u.metadata["reset_credits"], 1)
        with mock.patch.object(platforms, "claude_oauth_token", return_value=None), \
                mock.patch("urllib.request.urlopen") as urlopen:
            self.assertEqual(platforms.oauth_usage(), [])
        urlopen.assert_not_called()


class ClaudeOrgAndConsumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, where, data):
        where.mkdir(parents=True, exist_ok=True)
        (where / ".claude.json").write_text(json.dumps(data))

    def test_org_uuid_comes_from_the_logins_own_config(self):
        work, home = self.root / "work", self.root / "home"
        self.write(work, {"oauthAccount": {"organizationUuid": ORG}})
        self.write(home, {"oauthAccount": {"organizationUuid": ORG.replace("1", "9")}})
        cfg = {"accounts": {"work": {"env": {"CLAUDE_CONFIG_DIR": str(work)}},
                            "other": {"env": {}}}}
        self.assertEqual(platforms.claude_organization_uuid("work", cfg), ORG)
        self.assertEqual(platforms.claude_organization_uuid("personal", cfg, home=home),
                         ORG.replace("1", "9"))
        # D25: an account with no config dir of its own never reads personal's
        self.assertIsNone(platforms.claude_organization_uuid("other", cfg, home=home))

    def test_missing_or_malformed_org_uuid_is_none(self):
        cfg = {"accounts": {"work": {"env": {"CLAUDE_CONFIG_DIR": str(self.root / "w")}}}}
        self.assertIsNone(platforms.claude_organization_uuid("work", cfg))  # no file
        for data in ({}, {"oauthAccount": None}, {"oauthAccount": {}},
                     {"oauthAccount": {"organizationUuid": "../../etc"}}, ["x"]):
            self.write(self.root / "w", data)
            self.assertIsNone(platforms.claude_organization_uuid("work", cfg), data)

    def consume(self, response=None, error=None):
        side = error if error else None
        ret = FakeResponse(json.dumps(response).encode()) if response is not None else None
        with mock.patch("urllib.request.urlopen", return_value=ret,
                        side_effect=side) as urlopen:
            out = platforms.consume_claude_credit("tok", ORG, "grant_1", "req-1")
        return out, urlopen

    def test_consume_posts_the_grant_and_parses_each_result(self):
        for result in ("reset", "already_used", "not_limited", "cooldown",
                       "ineligible", "unavailable"):
            out, urlopen = self.consume({"result": result})
            self.assertEqual(out, result)
        req = urlopen.call_args.args[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.full_url, "https://api.anthropic.com/api/organizations/"
                                       f"{ORG}/reset_rate_limits")
        self.assertEqual(json.loads(req.data), {"program": "cedar_ember",
                                                "grant_id": "grant_1",
                                                "request_id": "req-1"})
        self.assertEqual(req.get_header("Authorization"), "Bearer tok")
        self.assertEqual(req.get_header("Anthropic-beta"), "oauth-2025-04-20")
        for unexpected in ({"result": "maybe"}, {}, ["reset"]):
            self.assertIsNone(self.consume(unexpected)[0])

    def test_consume_http_errors_and_bad_inputs(self):
        def http(code):
            return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(b""))
        self.assertEqual(self.consume(error=http(429))[0], "rateLimited")
        self.assertEqual(self.consume(error=http(401))[0], "signedOut")
        self.assertEqual(self.consume(error=http(403))[0], "signedOut")
        self.assertIsNone(self.consume(error=http(500))[0])
        self.assertIsNone(self.consume(error=urllib.error.URLError("down"))[0])
        with mock.patch("urllib.request.urlopen") as urlopen:
            for args in ((None, ORG, "g", "r"), ("t", None, "g", "r"),
                         ("t", "../x", "g", "r"), ("t", ORG, "Bad Id", "r"),
                         ("t", ORG, "g", None)):
                self.assertIsNone(platforms.consume_claude_credit(*args))
        urlopen.assert_not_called()


class ClaudeResetSpendTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 16, tzinfo=timezone.utc)
        self.led = Ledger(":memory:", clock=lambda: self.now)
        self.addCleanup(self.led.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name) / "work"
        self.work.mkdir()
        (self.work / ".credentials.json").write_text("{}")
        (self.work / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"organizationUuid": ORG}}))
        self.cfg = config.resolve_platforms(config._merge(copy.deepcopy(config.DEFAULTS), {
            "platforms": {"claude-work": {"from": "claude", "account": "work",
                                          "quota_group": "claude-work"}},
            "accounts": {"work": {"env": {"CLAUDE_CONFIG_DIR": str(self.work)},
                                  "routing": {"build": ["claude-work"]}}},
            "projects": {"p": {"accounts": ["work"]}}}))
        self.led.upsert_item("p", 1, state="ready", priority=2)
        self.projects = [config.project_policy(self.cfg, "p")]

    def ctx(self, dry_run=False):
        return scheduler.Ctx(self.cfg, self.led, dry_run=dry_run)

    def banked(self, weekly_pct=50.0, weekly_reset=None, grants=None):
        if grants is None:
            grants = [grant("grant_1", 1, (self.now + timedelta(days=7)).isoformat())]
        return body(weekly_pct, weekly_reset, grants)

    def seed(self, b, platform="claude-work"):
        usage.record_claude_usage(self.ctx(), platforms._claude_usage(b), platform=platform)

    def run_pass(self, read=None, consume_result="reset", dry_run=False, after=None):
        read = read if read is not None else self.banked(weekly_pct=95)
        after = after if after is not None else self.banked(
            weekly_pct=0, weekly_reset=self.now + timedelta(days=7))

        def oauth(**kwargs):
            return platforms._claude_usage(read if kwargs.get("keep_ids") else after,
                                           keep_ids=kwargs.get("keep_ids", False))
        with mock.patch.object(platforms, "oauth_usage", side_effect=oauth) as probe, \
                mock.patch.object(platforms, "claude_oauth_token", return_value="tok") as tok, \
                mock.patch.object(platforms, "consume_claude_credit",
                                  return_value=consume_result) as consume, \
                mock.patch("mahler.notify.send") as ping:
            resets.spend_banked(self.ctx(dry_run), self.projects)
        self.tok = tok
        return consume, probe, ping

    def events(self, kind):
        return self.led.q("SELECT detail FROM events WHERE kind=?", (kind,))

    def test_quota_metadata_fans_out_to_the_logins_peers_only(self):
        self.seed(self.banked(weekly_pct=70))
        quota = router.claude_quota(self.led, "claude-work",
                                    self.cfg["platforms"]["claude-work"])
        self.assertEqual(quota["reset_credits"], 1)
        self.assertEqual(quota["sampled_at"], self.now.isoformat())
        self.assertIsNone(self.led.get_kv("claude:quota:claude"))   # never personal
        self.assertNotIn("grant_1", self.led.get_kv("claude:quota:claude-work"))

    def test_rule_a_spends_when_full_with_ready_work(self):
        self.seed(self.banked(95, self.now + timedelta(hours=72)))
        consume, probe, ping = self.run_pass()
        consume.assert_called_once()
        token, org, gid, request_id = consume.call_args.args
        self.assertEqual((token, org, gid), ("tok", ORG, "grant_1"))
        self.assertTrue(request_id)
        # the work login's own credentials file, never the personal keychain
        self.tok.assert_called_with(keychain_service=None,
                                    credentials_file=str(self.work / ".credentials.json"))
        self.assertTrue(probe.call_args_list[0].kwargs.get("keep_ids"))
        ping.assert_called_once()
        self.assertEqual(ping.call_args.args[1], "Claude (work): banked reset spent")
        self.assertIn("rule a", ping.call_args.args[2])
        self.assertEqual(self.led.usage("claude-work")["weekly"]["used_pct"], 0)
        detail = json.loads(self.events("reset_spent")[0]["detail"])
        self.assertEqual((detail["kind"], detail["rule"], detail["credit"]),
                         ("claude", "a", "grant_1"))
        self.assertEqual(json.loads(self.led.get_kv(
            "reset:attempt:claude:work"))["outcome"], "reset")

    def test_rule_a_needs_ready_work_and_a_distant_weekly_reset(self):
        self.seed(self.banked(95, self.now + timedelta(hours=12)))
        self.run_pass()[0].assert_not_called()          # natural reset is near
        self.led.upsert_item("p", 1, state="done", priority=2)
        self.seed(self.banked(95, self.now + timedelta(hours=72)))
        self.run_pass()[0].assert_not_called()          # nothing waiting
        self.led.upsert_item("p", 1, state="ready", priority=2)
        self.seed(self.banked(70, self.now + timedelta(hours=72)))
        self.run_pass()[0].assert_not_called()          # below the hard line

    def test_rule_b_spends_an_expiring_grant_into_a_used_week(self):
        g = [grant("grant_1", 1, (self.now + timedelta(hours=24)).isoformat())]
        self.seed(self.banked(70, self.now + timedelta(hours=72), g))
        consume, _, ping = self.run_pass(read=self.banked(70, None, g))
        consume.assert_called_once()
        self.assertIn("rule b", ping.call_args.args[2])

    def test_rule_b_does_not_fire_when_the_week_rolls_over_first(self):
        g = [grant("grant_1", 1, (self.now + timedelta(hours=24)).isoformat())]
        self.seed(self.banked(70, self.now + timedelta(hours=12), g))
        consume, _, ping = self.run_pass(read=self.banked(70, None, g))
        consume.assert_not_called()
        self.assertIn("banked reset expiring", ping.call_args.args[1])

    def test_expiring_unspent_pings_once_per_grant(self):
        g = [grant("grant_1", 1, (self.now + timedelta(hours=24)).isoformat())]
        self.seed(self.banked(30, self.now + timedelta(hours=72), g))
        consume, _, ping = self.run_pass()
        consume.assert_not_called()
        ping.assert_called_once()
        self.assertEqual(ping.call_args.args[1],
                         "Claude (work): banked reset expiring unused")
        self.assertEqual(len(self.events("reset_expiring")), 1)
        self.run_pass()[2].assert_not_called()

    def test_paused_unusable_empty_or_undated_grants_are_never_spent(self):
        later = (self.now + timedelta(days=3)).isoformat()
        for g in (grant(ends=later, paused=True), grant(ends=later, usable=False),
                  grant(ends=later, left=0), grant(ends=None)):
            self.led.set_kv("reset:attempt:claude:work", "")
            self.seed(self.banked(95, self.now + timedelta(hours=72), [g]))
            consume, _, ping = self.run_pass(read=self.banked(95, None, [g]))
            consume.assert_not_called()
            ping.assert_not_called()

    def test_targets_only_what_the_same_tick_read_shows_usable(self):
        # the standing reading had a usable grant; the targeting read says
        # it is now paused — nothing is spent
        self.seed(self.banked(95, self.now + timedelta(hours=72)))
        paused = [grant("grant_1", 1, (self.now + timedelta(days=7)).isoformat(),
                        paused=True)]
        self.run_pass(read=self.banked(95, None, paused))[0].assert_not_called()

    def test_earliest_expiring_usable_grant_is_targeted(self):
        grants = [grant("late", 1, (self.now + timedelta(days=6)).isoformat()),
                  grant("soon", 1, (self.now + timedelta(days=2)).isoformat()),
                  grant("sooner", 1, (self.now + timedelta(days=1)).isoformat(),
                        usable=False)]
        self.seed(self.banked(95, self.now + timedelta(hours=72), grants))
        consume, _, _ = self.run_pass(read=self.banked(95, None, grants))
        self.assertEqual(consume.call_args.args[2], "soon")

    def test_stale_or_malformed_reading_decides_nothing(self):
        self.seed(self.banked(95, self.now + timedelta(hours=72)))
        self.now += timedelta(minutes=16)
        consume, probe, ping = self.run_pass()
        consume.assert_not_called()
        probe.assert_not_called()
        self.now -= timedelta(minutes=16)
        self.led.set_kv("claude:quota:claude-work", "{not json")
        self.run_pass()[0].assert_not_called()

    def test_dry_run_describes_and_never_spends(self):
        self.seed(self.banked(95, self.now + timedelta(hours=72)))
        consume, probe, ping = self.run_pass(dry_run=True)
        probe.assert_called_once()
        consume.assert_not_called()
        ping.assert_not_called()
        self.assertIsNone(self.led.get_kv("reset:attempt:claude:work"))
        self.assertEqual(self.events("reset_spent"), [])

    def test_non_reset_results_end_the_attempt_and_back_off(self):
        for result in ("already_used", "not_limited", "cooldown", "ineligible",
                       "unavailable", "rateLimited", "signedOut", None):
            self.led.set_kv("reset:attempt:claude:work", "")
            self.seed(self.banked(95, self.now + timedelta(hours=72)))
            consume, _, ping = self.run_pass(consume_result=result)
            consume.assert_called_once()
            ping.assert_not_called()
            self.assertEqual(self.events("reset_spent"), [])
            # no retry loop within the back-off window
            self.run_pass(consume_result=result)[0].assert_not_called()

    def test_retry_after_back_off_reuses_the_request_id(self):
        self.seed(self.banked(95, self.now + timedelta(hours=72)))
        consume, _, _ = self.run_pass(consume_result=None)
        key = consume.call_args.args[3]
        self.now += timedelta(minutes=31)
        self.seed(self.banked(95, self.now + timedelta(hours=72)))
        consume2, _, _ = self.run_pass(consume_result="reset")
        self.assertEqual(consume2.call_args.args[3], key)
        # a later spend from the same grant (resets left) gets a fresh id
        self.now += timedelta(minutes=31)
        self.seed(self.banked(95, self.now + timedelta(hours=72)))
        consume3, _, _ = self.run_pass(consume_result="reset")
        self.assertNotEqual(consume3.call_args.args[3], key)

    def test_codex_attempt_state_is_not_shared(self):
        self.led.set_kv("reset:attempt:work", json.dumps(
            {"credit_id": "codex-credit", "key": "k", "at": self.now.isoformat()}))
        self.seed(self.banked(95, self.now + timedelta(hours=72)))
        self.run_pass()[0].assert_called_once()
        self.assertEqual(json.loads(self.led.get_kv("reset:attempt:work"))["key"], "k")

    def test_missing_org_uuid_or_token_spends_nothing(self):
        (self.work / ".claude.json").unlink()
        self.seed(self.banked(95, self.now + timedelta(hours=72)))
        self.run_pass()[0].assert_not_called()
        (self.work / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"organizationUuid": ORG}}))
        with mock.patch.object(platforms, "claude_oauth_token", return_value=None), \
                mock.patch.object(platforms, "consume_claude_credit") as consume, \
                mock.patch.object(platforms, "oauth_usage") as probe:
            resets._spend_claude(self.ctx(), "claude-work",
                                 self.cfg["platforms"]["claude-work"], "work", "a")
        consume.assert_not_called()
        probe.assert_not_called()

    def test_work_login_never_uses_personal_org_or_token(self):
        # A work login with no config dir of its own has no source: it must
        # not fall back to the personal keychain or ~/.claude.json (D25).
        del self.cfg["accounts"]["work"]["env"]["CLAUDE_CONFIG_DIR"]
        self.seed(self.banked(95, self.now + timedelta(hours=72)))
        with mock.patch.object(platforms, "claude_organization_uuid",
                               wraps=platforms.claude_organization_uuid):
            consume, probe, _ = self.run_pass()
        consume.assert_not_called()
        probe.assert_not_called()
        self.tok.assert_not_called()

    def test_one_broken_login_does_not_block_others(self):
        self.seed(self.banked(95, self.now + timedelta(hours=72)))
        with mock.patch.object(resets, "_handle_login",
                               side_effect=[RuntimeError("boom")] * 50) as handle:
            resets.spend_banked(self.ctx(), self.projects)
        self.assertGreaterEqual(handle.call_count, 2)    # every login still tried

    def test_api_credit_platforms_are_not_logins(self):
        self.cfg["platforms"]["claude-work"]["api_credits"] = True
        self.assertNotIn("claude-work", [n for n, _ in resets._claude_logins(self.cfg)])

    def test_banked_resets_surface_in_capacity_detail(self):
        g = [grant("grant_1", 2, (self.now + timedelta(hours=24)).isoformat())]
        self.seed(self.banked(70, self.now + timedelta(hours=72), g))
        pconf = self.cfg["platforms"]["claude-work"]
        self.assertEqual(router.claude_detail(self.led, "claude-work", pconf),
                         "2 banked resets (next expires 1d 0h)")
        row = state._quota_row(self.cfg, self.led, {}, "claude-work", ["claude-work"],
                               {"claude-work"}, {})
        self.assertIn("2 banked resets (next expires 1d 0h)", row["detail"])
        self.seed(self.banked(70, None, []))
        self.assertEqual(router.claude_detail(self.led, "claude-work", pconf), "")


if __name__ == "__main__":
    unittest.main()
