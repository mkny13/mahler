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
                self.gh.exact_preview.return_value = artifact
                with mock.patch("mahler.launch_health.exact_live", return_value=artifact):
                    self.tick()
                self.assertEqual(self.row()["phase"], "smoke")
                self.assertEqual(self.row()["live_sha"], SHA)
                self.assertEqual(self.row()["artifact_ref"], artifact)
                with mock.patch.object(post_merge.subprocess, "Popen") as launch:
                    # Use a fresh directory for each strategy's smoke intent.
                    with mock.patch.object(post_merge, "_directory", return_value=self.root / strategy):
                        self.tick()
                    self.assertEqual(launch.call_args.kwargs["env"]["MAHLER_ARTIFACT_REF"], artifact)

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
        self.gh.exact_preview.side_effect = [None, "https://preview.example/build"]
        self.tick()
        self.assertEqual(self.row()["phase"], "live")
        self.assertEqual(self.led.post_merge_check("q", 2, SHA)["phase"], "smoke")
        self.assertEqual(self.row()["deadline_at"], deadline)
        self.now += timedelta(seconds=1800)
        self.tick()
        self.assertEqual(self.row()["failure_code"], "timeout")
        self.assertNotIn("private-provider-payload", " ".join(self.ctx.lines))

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


if __name__ == "__main__":
    unittest.main()
