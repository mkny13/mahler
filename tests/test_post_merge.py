"""Exact identity, restart and per-tick execution against private temporary state."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler import config, post_merge, scheduler
from mahler.gh import completion_evidence
from mahler.ledger import Ledger, iso

SHA = "a" * 40
NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


class PostMergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.patch = mock.patch.object(config, "STATE", str(self.root / "state"))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.now = NOW
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["projects"] = {"p": {"enabled": True, "repo": "x/y", "path": str(self.root),
            "smoke": "printf 'Tag: %s\\nSmoke: PASS\\n' \"$MAHLER_TAG\"",
            "post_merge": {"enabled": True, "deploy_strategy": "command", "deploy_command": "true",
                "live_command": "printf '{\"sha\":\"%s\",\"tag\":\"%s\"}' \"$MAHLER_MERGE_SHA\" \"$MAHLER_TAG\""}}}
        self.led = Ledger(str(self.root / "ledger.db"), clock=lambda: self.now)
        self.addCleanup(lambda: self.led.close())
        self.ctx = scheduler.Ctx(self.cfg, self.led)
        self.gh = mock.Mock()
        self.gh.issue_comments.return_value = []
        self.gh.create_issue.return_value = "https://github.com/x/y/issues/99"
        self.gh.issue_by_marker.return_value = None
        self.ctx.gh = mock.Mock(return_value=self.gh)
        self.projects = [{"name": "p"}]
        self.register()

    def register(self, sha=SHA):
        post_merge.register(self.ctx, "p", 1, {"state": "MERGED", "mergeCommit": {"oid": sha}})

    def row(self, sha=SHA):
        return self.led.post_merge_check("p", 1, sha)

    def tick(self):
        post_merge.advance(self.ctx, self.projects)

    def wait_result(self, directory):
        deadline = time.monotonic() + 5
        while not (directory / "result.json").exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue((directory / "result.json").exists())

    def phase(self):
        row = dict(self.row())
        directory = post_merge._directory(row)
        self.tick()
        self.assertEqual(dict(self.row()), row)  # launch cannot also poll
        self.wait_result(directory)
        self.tick()

    def through_live(self):
        self.phase()
        self.phase()
        self.assertEqual(self.row()["phase"], "smoke")

    def test_builtin_live_strategies_feed_artifact_to_smoke(self):
        for strategy in ("github", "mahler"):
            with self.subTest(strategy=strategy):
                self.led.con.execute("DELETE FROM post_merge_checks")
                self.register()
                self.cfg["projects"]["p"]["post_merge"].update(
                    deploy_strategy="watch", live_strategy=strategy, live_command="")
                self.tick()
                artifact = "https://preview.example/build-10" if strategy == "github" else "mahler:" + SHA
                self.gh.exact_preview.return_value = (artifact, "")
                with mock.patch("mahler.launch_health.exact_live", return_value=artifact):
                    self.tick()
                self.assertEqual(self.row()["phase"], "smoke")
                self.assertEqual(self.row()["live_sha"], SHA)
                self.assertEqual(self.row()["artifact_ref"], artifact)
                if strategy == "mahler":
                    continue  # smoke is satisfied by the live proof; see the test below
                with mock.patch.object(post_merge.subprocess, "Popen") as launch:
                    # Use a fresh directory for each strategy's smoke intent.
                    with mock.patch.object(post_merge, "_directory", return_value=self.root / strategy):
                        self.tick()
                    self.assertEqual(launch.call_args.kwargs["env"]["MAHLER_ARTIFACT_REF"], artifact)

    def test_mahler_live_proof_is_its_smoke(self):
        self.cfg["projects"]["p"]["post_merge"].update(
            deploy_strategy="watch", live_strategy="mahler", live_command="")
        self.tick()
        with mock.patch("mahler.launch_health.exact_live", return_value="mahler:" + SHA):
            self.tick()
        self.assertEqual(self.row()["phase"], "smoke")
        with mock.patch.object(post_merge.subprocess, "Popen") as launch:
            self.tick()
        launch.assert_not_called()
        row = self.row()
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(Path(row["evidence_ref"]).read_text(),
                         f"Tag: {row['tag']}\nSmoke: PASS\n")
        self.gh.create_issue.assert_not_called()

    def test_provider_errors_retry_until_original_deadline_and_isolate_projects(self):
        self.cfg["projects"]["p"]["post_merge"].update(
            deploy_strategy="watch", live_strategy="github", live_command="")
        self.tick()
        self.cfg["projects"]["q"] = copy.deepcopy(self.cfg["projects"]["p"])
        self.projects.append({"name": "q"})
        post_merge.register(self.ctx, "q", 2, {"state": "MERGED", "mergeCommit": {"oid": SHA}})
        self.gh.exact_preview.side_effect = RuntimeError("private-provider-payload")
        deadline = self.row()["deadline_at"]
        self.tick()
        self.assertEqual(self.row()["status"], "pending")
        self.assertEqual(self.led.post_merge_check("q", 2, SHA)["phase"], "live")
        self.gh.exact_preview.side_effect = [(None, "no deployments found for environment"), ("https://preview.example/build", "")]
        self.tick()
        self.assertEqual(self.row()["phase"], "live")
        self.assertEqual(self.led.post_merge_check("q", 2, SHA)["phase"], "smoke")
        self.assertEqual(self.row()["deadline_at"], deadline)
        self.now += timedelta(seconds=1800)
        self.tick()
        self.assertEqual(self.row()["failure_code"], "timeout")
        self.assertNotIn("private-provider-payload", " ".join(self.ctx.lines))

    @mock.patch("mahler.notify.send")
    def test_live_timeout_reports_last_observation_after_restart(self, ping):
        from mahler.gh import GH
        self.cfg["projects"]["p"]["post_merge"].update(
            deploy_strategy="watch", live_strategy="github", live_command="")
        self.tick()
        self.gh.pr_for_commit.return_value = 42
        provider = GH("x/y")
        self.gh.exact_preview.side_effect = provider.exact_preview
        with mock.patch.object(provider, "_gh", return_value="[]"):
            self.tick()
        self.assertEqual(self.row()["status"], "pending")
        self.ctx = scheduler.Ctx(self.cfg, self.led)
        self.ctx.gh = mock.Mock(return_value=self.gh)
        self.now += timedelta(seconds=1800)
        self.tick()
        self.assertEqual(self.row()["failure_code"], "timeout")
        reason = "no deployments found for environment"
        self.assertIn(reason, self.row()["summary"])
        self.assertIn(reason, self.gh.create_issue.call_args.args[1])
        event = self.led.q1("SELECT detail FROM events WHERE kind='post_merge_failed'")
        self.assertIn(reason, json.loads(event["detail"])["diagnostic"])

    def test_pass_across_ticks_and_single_d10_evidence(self):
        self.led.upsert_item("p", 1, state="shipped")
        self.led.add_uat("p", 1, 4, SHA, "test", "verify", shipped_at=iso(NOW - timedelta(seconds=1)))
        self.through_live()
        self.phase()
        terminal = dict(self.row())
        self.assertEqual(terminal["status"], "PASS")
        self.assertEqual(self.led.item("p", 1)["state"], "shipped")
        self.gh.comment_create.assert_not_called()
        self.tick()
        body = self.gh.comment_create.call_args.args[1]
        evidence = completion_evidence({"body": body, "author": {"login": "bot"},
            "url": "https://example.test/comment/1", "createdAt": iso(NOW)},
            iso(NOW - timedelta(seconds=1)))
        self.assertEqual(evidence["kind"], "smoke")
        self.tick()
        self.register()
        self.assertEqual(dict(self.row()), terminal)
        self.gh.comment_create.assert_called_once()
        self.assertTrue(self.led.accept_evidence("p", 1, evidence))
        self.assertEqual(self.led.item("p", 1)["state"], "done")

    def test_restart_and_duplicate_poll_do_not_relaunch(self):
        with mock.patch.object(post_merge.subprocess, "Popen", side_effect=OSError("secret")):
            self.tick()
        self.assertEqual(self.row()["status"], "FAIL")
        self.assertNotIn("secret", self.row()["summary"])
        self.register("b" * 40)
        row = self.row("b" * 40)
        directory = post_merge._directory(row)
        directory.mkdir(parents=True)
        # Crash after the durable intent but before launch: never launch again.
        self.led.close()
        self.led = Ledger(str(self.root / "ledger.db"), clock=lambda: self.now)
        self.ctx.led = self.led
        with mock.patch.object(post_merge.subprocess, "Popen") as launch:
            self.tick()
            self.tick()
            launch.assert_not_called()
            self.assertEqual(self.row("b" * 40)["status"], "pending")
            self.now += timedelta(seconds=1800)
            self.tick()
            self.assertEqual(self.row("b" * 40)["failure_code"], "timeout")

    def test_running_restart_polls_same_result(self):
        self.cfg["projects"]["p"]["post_merge"]["deploy_command"] = "sleep .15"
        directory = post_merge._directory(self.row())
        self.tick()
        self.led.close()
        self.led = Ledger(str(self.root / "ledger.db"), clock=lambda: self.now)
        self.ctx.led = self.led
        with mock.patch.object(post_merge, "_launch") as launch:
            self.tick()
            self.assertEqual(self.row()["phase"], "deploy")
            self.wait_result(directory)
            self.tick()
            self.assertEqual(self.row()["phase"], "live")
            launch.assert_not_called()

    def test_live_identity_mismatch_and_nonzero(self):
        for command, code in (("printf '{\"sha\":\"wrong\",\"tag\":\"wrong\"}'", "identity_mismatch"),
                              ("exit 9", "command_failed"), ("printf invalid", "command_failed")):
            with self.subTest(command=command):
                self.led.con.execute("DELETE FROM post_merge_checks")
                self.register()
                self.cfg["projects"]["p"]["post_merge"].update(deploy_strategy="watch", live_command=command)
                self.tick()
                directory = post_merge._directory(self.row())
                if directory.exists():
                    import shutil
                    shutil.rmtree(directory)
                self.phase()
                self.assertEqual(self.row()["failure_code"], code)

    def test_smoke_grammar_rejects_malformed_duplicate_skips_and_wrong_tag(self):
        reports = ["", f"Tag: {SHA}\n", f"Tag: {SHA}\nSmoke: SKIP\n",
                   f"Tag: {SHA}\nWaived: journey - reason\n", "Tag: wrong\nSmoke: PASS\n",
                   f"Tag: {SHA}\nSmoke: PASS\nSmoke: FAIL\n",
                   f"Tag: {SHA}\nTag: {SHA}\nSmoke: PASS\n", f"Tag: {SHA}\nSmoke: FAIL\n"]
        self.through_live()
        for index, report in enumerate(reports):
            with self.subTest(report=report):
                directory = self.root / f"report-{index}"
                directory.mkdir()
                intent = {"identity": post_merge._identity(self.row()), "token": "new"}
                (directory / "intent.json").write_text(json.dumps(intent))
                # A real command emits the fixture through stdin-free env transport.
                with mock.patch.dict("os.environ", {"TEST_REPORT": report}):
                    post_merge._worker(directory, 3, 'printf "%s" "$TEST_REPORT"')
                result = json.loads((directory / "result.json").read_text())
                self.assertFalse(result.get("pass") and result.get("tag") == SHA)
                with mock.patch.object(post_merge, "_directory", return_value=directory):
                    post_merge._poll(self.ctx, self.row(), directory)
                self.assertEqual(self.row()["failure_code"], "invalid_report")
                self.led.con.execute("UPDATE post_merge_checks SET status='pending', failure_code='' ")

    def test_stale_result_rejected(self):
        directory = post_merge._directory(self.row())
        directory.mkdir(parents=True)
        identity = post_merge._identity(self.row())
        (directory / "intent.json").write_text(json.dumps({"identity": identity, "token": "new"}))
        (directory / "result.json").write_text(json.dumps({"identity": identity, "token": "old"}))
        self.tick()
        self.assertEqual(self.row()["failure_code"], "stale_result")

    def test_disabled_dry_run_and_smoke_disabled(self):
        for field in ("enabled", "contract", "dry_run"):
            with self.subTest(field=field), mock.patch.object(post_merge, "_advance") as advance:
                self.cfg["projects"]["p"]["enabled"] = field != "enabled"
                self.cfg["projects"]["p"]["post_merge"]["enabled"] = field != "contract"
                self.ctx.dry_run = field == "dry_run"
                self.tick()
                advance.assert_not_called()
        self.ctx.dry_run = False
        self.through_live()
        self.cfg["projects"]["p"]["smoke"] = ""
        self.tick()
        self.assertEqual(self.row()["failure_code"], "smoke_disabled")

    def test_project_and_check_errors_do_not_stop_later_projects(self):
        self.cfg["projects"]["q"] = copy.deepcopy(self.cfg["projects"]["p"])
        self.projects.append({"name": "q"})
        post_merge.register(self.ctx, "q", 2, {"state": "MERGED", "mergeCommit": {"oid": SHA}})
        with mock.patch.object(post_merge, "_advance", side_effect=[RuntimeError("private"), None]) as advance:
            self.tick()
            self.assertEqual(advance.call_count, 2)
        with mock.patch.object(self.ctx, "policy", side_effect=[RuntimeError("private"), config.project_policy(self.cfg, "q")]), \
             mock.patch.object(post_merge, "_advance") as advance:
            self.tick()
            advance.assert_called_once()
        self.assertNotIn("private", " ".join(self.ctx.lines))

    def test_uncertain_evidence_post_is_recovered_without_duplicate(self):
        self.led.add_uat("p", 1, 4, SHA, "test", "verify", shipped_at=iso(NOW - timedelta(seconds=1)))
        self.through_live()
        self.phase()
        def uncertain(number, body):
            self.gh.issue_comments.return_value = [{"body": body, "author": {"login": "bot"},
                "id": "123", "createdAt": iso(NOW)}]
            raise RuntimeError("uncertain")
        self.gh.comment_create.side_effect = uncertain
        self.tick()
        self.tick()
        self.gh.comment_create.assert_called_once()

    def test_newer_merge_cannot_receive_older_pass_evidence(self):
        self.led.add_uat("p", 1, 4, SHA, "test", "verify", shipped_at=iso(NOW - timedelta(seconds=1)))
        self.through_live()
        self.phase()
        self.register("b" * 40)
        with mock.patch.object(post_merge, "_advance"):
            self.tick()
        self.gh.comment_create.assert_not_called()

    def test_registration_recovery_uses_confirmed_merge_and_is_idempotent(self):
        self.led.con.execute("DELETE FROM post_merge_checks")
        post_merge.register(self.ctx, "p", 1, {"state": "CLOSED", "headRefOid": SHA})
        self.assertIsNone(self.row())
        self.led.upsert_item("p", 1, state="shipped")
        self.led.add_uat("p", 1, 4, SHA, "test", "verify")
        with mock.patch.object(post_merge, "_advance"):
            self.tick()
            original = dict(self.row())
            self.now += timedelta(seconds=60)
            self.tick()
        self.assertEqual(dict(self.row()), original)

    def test_account_environment_and_literal_metadata(self):
        self.cfg["accounts"] = {"work": {"env": {"DEPLOY_LOGIN": "work"}}}
        self.cfg["projects"]["p"]["account"] = "work"
        self.led.con.execute("UPDATE post_merge_checks SET artifact_ref=?", ("$(touch nope); `false`",))
        with mock.patch.dict("os.environ", {"GH_TOKEN": "personal-secret"}), \
             mock.patch.object(post_merge.subprocess, "Popen") as launch:
            self.tick()
            args, kwargs = launch.call_args
            self.assertNotIn("GH_TOKEN", kwargs["env"])
            self.assertEqual(kwargs["env"]["DEPLOY_LOGIN"], "work")
            self.assertEqual(kwargs["env"]["MAHLER_ARTIFACT_REF"], "$(touch nope); `false`")
            self.assertNotIn("$(touch nope)", " ".join(args[0]))
            self.assertTrue(kwargs["start_new_session"])

    def test_worker_timeout_and_large_output_are_terminal(self):
        for index, command in enumerate(("sleep 10", "yes output")):
            directory = self.root / f"worker-{index}"
            directory.mkdir()
            (directory / "intent.json").write_text(json.dumps({"identity": post_merge._identity(self.row()), "token": "x"}))
            post_merge._worker(directory, .05, command)
            self.assertEqual(json.loads((directory / "result.json").read_text())["error"], "command_failed")

    def test_post_merge_failure_files_linked_p1_bug_and_sends_ping(self):
        self.led.upsert_item("p", 1, state="shipped")
        self.led.add_uat("p", 1, 42, SHA, "test-feature", "needs nothing")
        self.cfg["ntfy"] = {"topic": "post-merge-alerts"}
        self.cfg["projects"]["p"]["post_merge"]["live_command"] = "exit 1"
        self.phase()  # deploy -> live
        self.gh.create_issue.reset_mock()
        with mock.patch("mahler.notify.send") as notify_mock:
            notify_mock.return_value = True
            self.phase()  # live runs and fails (exit 1)
            self.assertEqual(self.row()["status"], "FAIL")
            self.gh.create_issue.assert_called_once()
            args, _ = self.gh.create_issue.call_args
            title, body, labels = args
            self.assertIn("Post-merge live fail: p#1", title)
            self.assertEqual(labels, ["type:bug", "p1"])
            self.assertIn("#1", body)
            self.assertIn("PR #42", body)
            self.assertIn(SHA, body)
            self.assertIn("live", body)
            self.assertIn("<!-- mahler:post-merge-failure", body)
            notify_mock.assert_called_once()
            ping_args, ping_kwargs = notify_mock.call_args
            self.assertIn("Post-merge live failed: p#1", ping_args[1])
            self.assertIn("https://github.com/x/y/issues/99", ping_kwargs.get("click", ""))
            self.assertEqual(self.led.item("p", 99)["state"], "inbox")
            self.assertEqual(self.led.item("p", 99)["priority"], 1)

    def test_crash_recovery_after_each_point_does_not_duplicate_bug_or_ping(self):
        self.led.upsert_item("p", 1, state="shipped")
        self.led.add_uat("p", 1, 42, SHA, "test-feature", "needs nothing")
        self.cfg["ntfy"] = {"topic": "post-merge-alerts"}
        self.led.update_post_merge_check("p", 1, SHA, expected_phase="deploy",
                                         phase="deploy", status="FAIL", failure_code="command_failed")
        # Crash Point 1: Issue exists on GitHub via marker, but kv record lost
        self.gh.issue_by_marker.return_value = "https://github.com/x/y/issues/105"
        with mock.patch("mahler.notify.send") as notify_mock:
            notify_mock.return_value = True
            self.tick()
            self.gh.create_issue.assert_not_called()
            self.assertEqual(self.led.get_kv(f"post-merge-bug:p:1:{SHA}"), "105")
            self.assertEqual(notify_mock.call_count, 1)

        # Crash Point 2: Bug in kv, ping sent (in kv), retry tick does not duplicate either
        self.gh.create_issue.reset_mock()
        with mock.patch("mahler.notify.send") as notify_mock:
            self.tick()
            self.gh.create_issue.assert_not_called()
            notify_mock.assert_not_called()

    def report_failure(self, number, phase="live", code="timeout"):
        from mahler import failures
        return failures.report_post_merge(
            self.ctx, "p", number, f"{number:040x}", phase, code, pr=42,
            output="diagnostic tail ghp_0123456789abcdefghijklmn")

    @mock.patch("mahler.notify.send")
    def test_post_merge_failure_dedupes_against_open_bug_with_comment(self, ping):
        self.gh.issue_state.return_value = "OPEN"
        self.report_failure(2)
        original = dict(self.led.item("p", 99))
        self.report_failure(3)
        self.report_failure(3)  # remediation retry is not another failure
        self.gh.create_issue.assert_called_once()
        self.gh.comment_create.assert_called_once()
        number, body = self.gh.comment_create.call_args.args
        self.assertEqual(number, 99)
        for text in ("<!-- mahler:agent -->", f"{3:040x}", "#3", "PR #42",
                     "timeout", "live", "diagnostic tail"):
            self.assertIn(text, body)
        self.assertNotIn("ghp_0123456789abcdefghijklmn", body)
        self.assertEqual(self.led.get_kv(f"post-merge-bug:p:3:{3:040x}"), "99")
        self.assertEqual(self.led.item("p", 99)["title"], original["title"])
        self.assertEqual(json.loads(self.led.get_kv("post-merge-consecutive:p"))["count"], 2)

    @mock.patch("mahler.notify.send")
    def test_closed_post_merge_bug_allows_new_bug(self, ping):
        self.report_failure(2)
        self.gh.issue_state.return_value = "CLOSED"
        self.gh.create_issue.return_value = "https://github.com/x/y/issues/100"
        self.assertEqual(self.report_failure(3), 100)
        self.assertEqual(self.gh.create_issue.call_count, 2)
        self.gh.comment_create.assert_not_called()

    @mock.patch("mahler.notify.send")
    def test_consecutive_identical_failures_trigger_circuit_breaker_and_single_ping(self, ping):
        self.gh.issue_state.return_value = "OPEN"
        for number in (2, 3, 4, 4):
            self.report_failure(number)
        self.assertEqual(self.led.get_kv("post_merge_paused:p"), "1")
        alerts = [call for call in ping.call_args_list if "paused" in call.args[1].lower()]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].kwargs["priority"], "high")
        with mock.patch.object(post_merge, "_advance") as advance:
            self.tick()
            self.tick()
            advance.assert_not_called()

    def trip(self):
        self.gh.issue_state.return_value = "OPEN"
        for number in (2, 3, 4):
            self.report_failure(number)
        self.assertEqual(self.led.get_kv("post_merge_paused:p"), "1")

    def expire(self):
        self.now = self.now + timedelta(days=3)

    @mock.patch("mahler.notify.send")
    def test_post_merge_pause_records_timestamp_and_event(self, ping):
        self.trip()
        self.assertEqual(self.led.get_kv("post_merge_paused_at:p"), iso(self.now))
        events = self.led.q("SELECT * FROM events WHERE kind='post_merge_paused'")
        self.assertEqual(len(events), 1)
        detail = json.loads(events[0]["detail"])
        self.assertEqual((detail["phase"], detail["failure_code"], detail["count"]),
                         ("live", "timeout", 3))

    @mock.patch("mahler.notify.send")
    def test_resume_settles_overdue_checks_without_bug_or_streak_cascade(self, ping):
        self.trip()
        creates = self.gh.create_issue.call_count
        self.expire()
        self.assertEqual(post_merge.resume(self.ctx, "p", source="cli"), 1)
        self.assertNotEqual(self.led.get_kv("post_merge_paused:p"), "1")
        row = self.row()
        self.assertEqual((row["status"], row["failure_code"]), ("FAIL", "paused_expired"))
        self.assertIn("expired while paused", row["summary"])
        self.tick()
        self.assertEqual(self.gh.create_issue.call_count, creates)
        self.assertEqual(json.loads(self.led.get_kv("post-merge-consecutive:p"))["count"], 0)
        self.assertNotEqual(self.led.get_kv("post_merge_paused:p"), "1")
        event = self.led.q1("SELECT detail FROM events WHERE kind='post_merge_resumed'")
        self.assertEqual(json.loads(event["detail"]), {"source": "cli", "settled": 1})

    @mock.patch("mahler.notify.send")
    def test_re_probe_clears_pause_when_live_lookup_succeeds(self, ping):
        self.trip()
        self.cfg["projects"]["p"]["post_merge"].update(
            deploy_strategy="watch", live_strategy="github", live_command="")
        self.gh.exact_preview.return_value = None
        self.tick()
        self.assertEqual(self.led.get_kv("post_merge_paused:p"), "1")
        self.gh.exact_preview.return_value = "https://preview.example"
        self.tick()
        self.assertNotEqual(self.led.get_kv("post_merge_paused:p"), "1")
        event = self.led.q1("SELECT detail FROM events WHERE kind='post_merge_resumed'")
        self.assertEqual(json.loads(event["detail"])["source"], "re_probe")
        self.assertEqual(self.row()["phase"], "live")

    @mock.patch("mahler.notify.send")
    def test_cmd_resume_project_unpauses_post_merge(self, ping):
        from types import SimpleNamespace
        from mahler import cli
        for command, args in ((cli.cmd_resume, {"project": "p"}),
                              (cli.cmd_resume, {"project": None}),
                              (cli.cmd_unstick, {"project": "p"})):
            with self.subTest(command=command.__name__, args=args):
                self.led.set_kv("post_merge_paused:p", "1")
                with mock.patch("mahler.ship.unstick", return_value=0), \
                        mock.patch("sys.stdout"):
                    command(SimpleNamespace(**args), self.cfg, self.led)
                self.assertNotEqual(self.led.get_kv("post_merge_paused:p"), "1")

    @mock.patch("mahler.notify.send")
    def test_failed_circuit_breaker_ping_retries_while_project_is_paused(self, ping):
        self.gh.issue_state.return_value = "OPEN"
        pause_attempts = 0

        def send(_cfg, title, *_args, **_kwargs):
            nonlocal pause_attempts
            if "Post-merge checks paused" in title:
                pause_attempts += 1
                return pause_attempts > 1
            return True
        ping.side_effect = send

        for number in (2, 3, 4):
            self.report_failure(number)
        paused_alerts = lambda: [
            call for call in ping.call_args_list
            if "Post-merge checks paused" in call.args[1]
        ]
        self.assertEqual(len(paused_alerts()), 1)
        self.assertEqual(self.led.get_kv("post_merge_paused:p"), "1")

        self.tick()
        self.assertEqual(len(paused_alerts()), 2)
        self.tick()
        self.assertEqual(len(paused_alerts()), 2)
        self.assertEqual(self.led.get_kv("post_merge_paused:p"), "1")

    @mock.patch("mahler.notify.send")
    def test_different_failure_resets_streak_and_has_distinct_bug(self, ping):
        self.gh.issue_state.return_value = "OPEN"
        self.report_failure(2)
        self.report_failure(3)
        self.gh.create_issue.return_value = "https://github.com/x/y/issues/100"
        self.report_failure(4, code="command_failed")
        self.assertNotEqual(self.led.get_kv("post_merge_paused:p"), "1")
        self.assertEqual(json.loads(self.led.get_kv("post-merge-consecutive:p"))["count"], 1)
        self.assertEqual(self.gh.create_issue.call_count, 2)

    @mock.patch("mahler.notify.send")
    def test_post_merge_pass_resets_consecutive_failure_count(self, ping):
        self.report_failure(2)
        self.through_live()
        self.phase()
        self.assertEqual(self.row()["status"], "PASS")
        self.assertEqual(json.loads(self.led.get_kv("post-merge-consecutive:p"))["count"], 0)
        self.report_failure(3)
        self.tick()  # old PASS delivery must not erase a newer failure
        self.assertEqual(json.loads(self.led.get_kv("post-merge-consecutive:p"))["count"], 1)

    def test_post_merge_register_skips_post_merge_failure_bugs(self):
        self.led.con.execute("DELETE FROM post_merge_checks")
        for fields in ({"issue_body": "<!-- mahler:post-merge-failure project=p -->"},
                       {"title": "Post-merge live fail: p#2", "issue_body": "## Plan\nFix it"}):
            self.led.upsert_item("p", 1, **fields)
            self.register()
            self.assertIsNone(self.row())

    def test_raw_post_merge_bug_held_when_upstream_cause_open(self):
        from mahler import tick
        self.led.upsert_item("p", 2, state="ready")
        body = "<!-- mahler:post-merge-failure project=p -->\nFailure notice"
        policy = self.ctx.policy("p")
        for state in ("inbox", "ready"):
            with self.subTest(state=state):
                self.led.upsert_item("p", 99, state=state, issue_body=body, depends="[2]")
                candidates = tick._candidates(self.ctx, [policy])
                self.assertNotIn(99, [it["number"] for _, _, it in candidates])
                self.assertTrue(any(h["kind"] == "upstream_cause" and h["number"] == 99
                                    for h in self.ctx.holds))
        self.led.upsert_item("p", 2, state="done")
        self.assertIn(99, [it["number"] for _, _, it in tick._candidates(self.ctx, [policy])])
        self.led.upsert_item("p", 2, state="ready")
        self.led.upsert_item("p", 99, state="inbox", issue_body=body + "\n## Plan\nFix cause")
        self.assertIn(99, [it["number"] for _, _, it in tick._candidates(self.ctx, [policy])])

    def test_auto_revert_opt_in_prepares_revert_and_recovers_without_duplicates(self):
        self.led.upsert_item("p", 1, state="shipped")
        self.led.add_uat("p", 1, 42, SHA, "test-feature", "needs nothing")
        self.led.update_post_merge_check("p", 1, SHA, expected_phase="deploy",
                                         phase="deploy", status="FAIL", failure_code="timeout")
        # auto_revert = false: never calls revert
        self.cfg["projects"]["p"]["post_merge"]["auto_revert"] = False
        with mock.patch("mahler.console.revert.revert") as revert_mock:
            self.tick()
            revert_mock.assert_not_called()

        # auto_revert = true: calls revert with recover=True
        self.led.con.execute("DELETE FROM kv WHERE key LIKE 'post-merge-remediation%'")
        self.cfg["projects"]["p"]["post_merge"]["auto_revert"] = True
        with mock.patch("mahler.console.revert.revert") as revert_mock:
            revert_mock.return_value = ("done", "200")
            self.tick()
            revert_mock.assert_called_once_with(self.ctx, "p", 1, 42, source="post_merge", recover=True)

        # Crash recovery on next tick: does not duplicate
        with mock.patch("mahler.console.revert.revert") as revert_mock:
            self.tick()
            revert_mock.assert_not_called()

    def test_credentials_and_personal_captures_redacted_in_issue_event_and_ping(self):
        secret = "ghp_0123456789abcdefghijklmn"
        url_secret = "postgres://bob:s3cret@db.example.com:5432/app"
        self.led.upsert_item("p", 1, state="shipped")
        self.led.add_uat("p", 1, 42, SHA, "test-feature", "needs nothing")
        self.cfg["ntfy"] = {"topic": "alerts"}
        self.led.update_post_merge_check("p", 1, SHA, expected_phase="deploy",
                                         phase="deploy", status="FAIL", failure_code="command_failed")
        from mahler import failures
        with mock.patch("mahler.notify.send") as notify_mock:
            notify_mock.return_value = True
            failures.report_post_merge(self.ctx, "p", 1, SHA, "deploy", "command_failed",
                                       output=f"Error leaking {secret} and {url_secret}")
            self.gh.create_issue.assert_called_once()
            _, body, _ = self.gh.create_issue.call_args[0]
            self.assertNotIn(secret, body)
            self.assertNotIn("s3cret", body)
            self.assertIn("<redacted>", body)
            ping_args = notify_mock.call_args[0]
            self.assertNotIn(secret, ping_args[1])
            self.assertNotIn(secret, ping_args[2])
            events = self.led.q("SELECT * FROM events WHERE kind='post_merge_failed'")
            self.assertEqual(len(events), 1)
            self.assertNotIn(secret, events[0]["detail"])


if __name__ == "__main__":
    unittest.main()
