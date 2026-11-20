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

    def workflow_setup(self):
        self.cfg["projects"]["p"].pop("post_merge", None)
        self.led.con.execute("DELETE FROM post_merge_checks")
        self.gh.default_branch_settings.return_value = {"default_branch": "main"}
        self.gh.pr_for_commit.return_value = None
        self.gh.workflow_runs_for_commit.return_value = []
        self.register()

    def workflow(self, **changes):
        return dict({"id": 10, "workflow_id": 7, "name": "Deploy sync backend",
                     "head_sha": SHA, "head_branch": "main", "event": "push",
                     "status": "completed", "conclusion": "failure", "run_attempt": 1,
                     "html_url": "https://github.com/x/y/actions/runs/10"}, **changes)

    @mock.patch("mahler.notify.send", return_value=True)
    def test_default_branch_workflow_failure_noticed_without_post_merge_config(self, ping):
        self.workflow_setup()
        self.gh.workflow_runs_for_commit.return_value = [self.workflow()]
        self.tick()
        self.tick()
        self.assertIsNone(self.row())
        self.gh.create_issue.assert_called_once()
        title, body, labels = self.gh.create_issue.call_args.args
        self.assertIn("<!-- mahler:post-merge-failure", body)
        self.assertIn("Deploy sync backend", body)
        self.assertIn("https://github.com/x/y/actions/runs/10", body)
        self.assertIn("type:bug", labels)
        self.assertIn("p1", labels)
        ping.assert_called_once()
        self.assertEqual(ping.call_args.kwargs["priority"], "high")

    @mock.patch("mahler.notify.send", return_value=True)
    def test_workflow_green_rerun_or_active_attempt_suppresses_failure(self, ping):
        self.workflow_setup()
        for newer in (self.workflow(run_attempt=2, conclusion="success"),
                      self.workflow(id=11, conclusion="success"),
                      self.workflow(run_attempt=2, status="in_progress", conclusion=None)):
            self.gh.workflow_runs_for_commit.return_value = [newer, self.workflow()]
            self.tick()
        self.gh.create_issue.assert_not_called()
        ping.assert_not_called()

    @mock.patch("mahler.notify.send", return_value=True)
    def test_workflow_scope_and_observation_window(self, ping):
        self.workflow_setup()
        self.gh.workflow_runs_for_commit.return_value = [
            self.workflow(head_branch="feature"), self.workflow(head_sha="b" * 40),
            self.workflow(event="pull_request"), self.workflow(event="workflow_dispatch")]
        self.tick()
        self.gh.create_issue.assert_not_called()
        self.now += timedelta(days=15)
        self.gh.workflow_runs_for_commit.reset_mock()
        self.tick()
        self.gh.workflow_runs_for_commit.assert_not_called()

    @mock.patch("mahler.notify.send", return_value=True)
    def test_workflow_recovery_reuse_and_restart(self, ping):
        self.workflow_setup()
        self.led.con.execute("DELETE FROM kv WHERE key LIKE 'workflow-watch:%'")
        self.led.add_uat("p", 1, 4, SHA, "shipped", "verify", shipped_at=iso(NOW))
        self.gh.issue_by_marker.return_value = "https://github.com/x/y/issues/99"
        self.gh.workflow_runs_for_commit.return_value = [self.workflow(conclusion="timed_out")]
        self.tick()
        self.led.close()
        self.led = Ledger(str(self.root / "ledger.db"), clock=lambda: self.now)
        self.ctx = scheduler.Ctx(self.cfg, self.led)
        self.ctx.gh = mock.Mock(return_value=self.gh)
        self.tick()
        self.gh.create_issue.assert_not_called()
        ping.assert_called_once()
        events = self.led.q("SELECT * FROM events WHERE kind='post_merge_failed'")
        self.assertEqual(len(events), 1)
        self.assertIn("Deploy sync backend", events[0]["detail"])

    @mock.patch("mahler.notify.send", return_value=True)
    def test_workflow_watch_survives_phased_check_pause(self, ping):
        self.gh.default_branch_settings.return_value = {"default_branch": "main"}
        self.gh.pr_for_commit.return_value = None
        self.gh.workflow_runs_for_commit.return_value = [self.workflow(conclusion="startup_failure")]
        self.led.set_kv("post_merge_paused:p", "1")
        self.tick()
        self.gh.create_issue.assert_called_once()
        ping.assert_called_once()

    @mock.patch("mahler.notify.send", return_value=True)
    def test_workflow_provider_failure_retries_and_disabled_project_is_quiet(self, ping):
        self.workflow_setup()
        self.gh.workflow_runs_for_commit.side_effect = RuntimeError("offline")
        self.tick()
        self.gh.create_issue.assert_not_called()
        self.gh.workflow_runs_for_commit.side_effect = None
        self.gh.workflow_runs_for_commit.return_value = [self.workflow()]
        self.cfg["projects"]["p"]["enabled"] = False
        self.tick()
        self.gh.create_issue.assert_not_called()
        self.cfg["projects"]["p"]["enabled"] = True
        self.tick()
        ping.assert_called_once()

    @mock.patch("mahler.notify.send", return_value=True)
    def test_workflow_failure_dedupes_against_existing_open_issue(self, ping):
        self.workflow_setup()
        self.led.upsert_item("p", 83, title="Deploy sync backend on main fails npm audit")
        self.gh.issue_state.return_value = "OPEN"
        self.gh.workflow_runs_for_commit.return_value = [self.workflow()]
        self.tick()
        self.tick()
        self.gh.create_issue.assert_not_called()
        self.gh.comment_create.assert_called_once()
        number, body = self.gh.comment_create.call_args.args
        self.assertEqual(number, 83)
        self.assertIn("<!-- mahler:post-merge-failure", body)
        self.assertIn("/actions/runs/10", body)
        self.assertEqual(self.led.get_kv("open-post-merge-bug:p:workflow:7:actions_failed"), "83")

    @mock.patch("mahler.notify.send", return_value=True)
    def test_workflow_failure_migrates_legacy_open_bug_key(self, ping):
        self.workflow_setup()
        old_body = (
            f"<!-- mahler:post-merge-failure project=p number=1 sha={SHA} -->\n"
            "**Post-merge workflow check failed** for #1 (p#1).\n\n"
            "- **Failed phase:** `workflow`\n"
            "- **Failure code:** `actions_failed`\n"
            "- **Diagnostic:** Deploy sync backend: failure")
        self.led.upsert_item("p", 83, title="Post-merge workflow fail: p#1",
                             issue_body=old_body)
        self.led.set_kv("open-post-merge-bug:p:workflow:actions_failed", "83")
        self.gh.issue_state.return_value = "OPEN"
        self.gh.workflow_runs_for_commit.return_value = [self.workflow(
            id=11, html_url="https://github.com/x/y/actions/runs/11")]

        self.tick()

        self.gh.create_issue.assert_not_called()
        self.gh.comment_create.assert_called_once()
        self.assertEqual(self.gh.comment_create.call_args.args[0], 83)
        self.assertIn("/actions/runs/11", self.gh.comment_create.call_args.args[1])
        self.assertEqual(
            self.led.get_kv("open-post-merge-bug:p:workflow:7:actions_failed"), "83"
        )
        self.assertFalse(
            self.led.get_kv("open-post-merge-bug:p:workflow:actions_failed")
        )

    @mock.patch("mahler.notify.send", return_value=True)
    def test_workflow_failure_matches_reordered_issue_title_and_recovers(self, ping):
        self.workflow_setup()
        self.led.upsert_item(
            "p", 83,
            title="Sync backend deploy on main fails npm audit (picomatch high advisory)",
            issue_body="The main deployment fails npm audit.",
        )
        self.led.set_state(
            "p", 83, "needs_you",
            question="Wait for upstream, replace vitest/vite, or vendor picomatch?",
            sorted_at=iso(NOW),
        )
        self.gh.issue_state.return_value = "OPEN"
        self.gh.workflow_runs_for_commit.return_value = [self.workflow(id=44166202813)]

        self.tick()

        self.gh.create_issue.assert_not_called()
        self.gh.comment_create.assert_called_once()
        self.assertEqual(self.gh.comment_create.call_args.args[0], 83)
        self.assertEqual(
            self.led.get_kv("open-post-merge-bug:p:workflow:7:actions_failed"), "83"
        )

        self.gh.workflow_runs_for_commit.return_value = [
            self.workflow(id=44166202813, conclusion="success")
        ]
        self.tick()

        item = self.led.item("p", 83)
        self.assertEqual(item["state"], "inbox")
        self.assertIsNone(item["sorted_at"])
        recovery = self.gh.comment_create.call_args.args[1]
        self.assertIn("44166202813", recovery)
        self.assertIn(SHA, recovery)

    @mock.patch("mahler.notify.send", return_value=True)
    def test_workflow_failure_matches_item_with_only_run_id(self, ping):
        self.workflow_setup()
        self.led.upsert_item("p", 83, title="Audit remediation",
                             issue_body="Blocked by Actions run 44166202813.")
        self.led.set_state("p", 83, "needs_you", sorted_at=iso(NOW))
        self.gh.issue_state.return_value = "OPEN"
        self.gh.workflow_runs_for_commit.return_value = [self.workflow(id=44166202813)]

        self.tick()

        self.gh.create_issue.assert_not_called()
        self.gh.comment_create.assert_called_once()
        self.assertEqual(self.gh.comment_create.call_args.args[0], 83)
        self.assertEqual(
            self.led.get_kv("open-post-merge-bug:p:workflow:7:actions_failed"), "83"
        )
        self.gh.comment_create.reset_mock()
        self.gh.workflow_runs_for_commit.return_value = [
            self.workflow(id=44166202813, conclusion="success")
        ]

        self.tick()

        item = self.led.item("p", 83)
        self.assertEqual(item["state"], "inbox")
        self.assertIsNone(item["sorted_at"])
        self.gh.comment_create.assert_called_once()
        self.assertIn("44166202813", self.gh.comment_create.call_args.args[1])

    @mock.patch("mahler.notify.send", return_value=True)
    def test_workflow_green_rechecks_and_resumes_needs_you_item(self, ping):
        self.workflow_setup()
        self.led.upsert_item("p", 83, title="Deploy sync backend on main fails npm audit")
        self.led.set_state("p", 83, "needs_you", sorted_at=iso(NOW))
        self.gh.issue_state.return_value = "OPEN"
        self.gh.workflow_runs_for_commit.return_value = [self.workflow(conclusion="success")]
        self.tick()
        self.tick()
        item = self.led.item("p", 83)
        self.assertEqual(item["state"], "inbox")
        self.assertIsNone(item["sorted_at"])
        self.gh.comment_create.assert_called_once()
        body = self.gh.comment_create.call_args.args[1]
        for evidence in (SHA, "/actions/runs/10", "main", "success"):
            self.assertIn(evidence, body)
        self.assertEqual(len(self.led.q("SELECT * FROM events WHERE kind='workflow_recovered'")), 1)

    @mock.patch("mahler.notify.send", return_value=True)
    def test_workflow_matching_excludes_unrelated_and_closed_items(self, ping):
        self.workflow_setup()
        for n, title in ((81, "Add Deploy sync backend documentation"),
                         (82, "Other workflow on main fails"),
                         (83, "Deploy sync backend on main fails")):
            self.led.upsert_item("p", n, title=title)
            self.led.set_state("p", n, "needs_you")
        self.gh.issue_state.return_value = "CLOSED"
        self.gh.workflow_runs_for_commit.return_value = [self.workflow(conclusion="success")]
        self.tick()
        self.assertTrue(all(self.led.item("p", n)["state"] == "needs_you" for n in (81, 82, 83)))
        self.gh.comment_create.assert_not_called()
        self.gh.workflow_runs_for_commit.return_value = [self.workflow()]
        self.tick()
        self.gh.create_issue.assert_called_once()

    @mock.patch("mahler.notify.send", return_value=True)
    def test_different_workflows_on_same_commit_have_separate_bugs(self, ping):
        self.workflow_setup()
        self.gh.create_issue.side_effect = ["https://github.com/x/y/issues/98", "https://github.com/x/y/issues/99"]
        self.gh.issue_state.return_value = "OPEN"
        self.gh.workflow_runs_for_commit.return_value = [self.workflow(), self.workflow(id=11, workflow_id=8, name="Publish", html_url="https://github.com/x/y/actions/runs/11")]
        self.tick()
        self.assertEqual(self.gh.create_issue.call_count, 2)

    @mock.patch("mahler.notify.send", return_value=True)
    def test_newer_red_workflow_prevents_old_green_recovery(self, ping):
        self.workflow_setup()
        self.register("b" * 40)
        self.led.upsert_item("p", 83, title="Deploy sync backend on main fails")
        self.led.set_state("p", 83, "needs_you")
        self.gh.issue_state.return_value = "OPEN"
        self.gh.workflow_runs_for_commit.side_effect = lambda sha: [
            self.workflow(conclusion="success") if sha == SHA else
            self.workflow(id=11, head_sha=sha)]
        self.tick()
        self.assertEqual(self.led.item("p", 83)["state"], "needs_you")

    @mock.patch("mahler.notify.send", return_value=True)
    def test_workflow_link_recovers_on_new_commit_and_retries_comment(self, ping):
        self.workflow_setup()
        self.led.upsert_item("p", 83, title="Audit failure",
                             issue_body="https://github.com/x/y/actions/runs/10")
        self.gh.issue_state.return_value = "OPEN"
        self.gh.workflow_runs_for_commit.return_value = [self.workflow()]
        self.tick()
        self.gh.create_issue.assert_not_called()
        self.led.set_state("p", 83, "needs_you", sorted_at=iso(NOW))
        self.register("b" * 40)
        self.gh.workflow_runs_for_commit.side_effect = lambda sha: [
            self.workflow() if sha == SHA else self.workflow(
                id=12, head_sha=sha, conclusion="success",
                html_url="https://github.com/x/y/actions/runs/12")]
        self.gh.comment_create.reset_mock()
        self.gh.comment_create.side_effect = RuntimeError("offline")
        self.tick()
        self.assertEqual(self.led.item("p", 83)["state"], "needs_you")
        self.assertEqual(self.led.get_kv("open-post-merge-bug:p:workflow:7:actions_failed"), "83")
        self.gh.comment_create.side_effect = None
        self.tick()
        self.assertEqual(self.led.item("p", 83)["state"], "inbox")
        self.assertIn("b" * 40, self.gh.comment_create.call_args.args[1])
        self.assertFalse(self.led.get_kv("open-post-merge-bug:p:workflow:7:actions_failed"))

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

    @mock.patch("mahler.notify.send", return_value=True)
    def test_paused_permission_incident_retry_restart_and_recovery(self, ping):
        self.cfg["projects"]["p"]["post_merge"].update(live_strategy="github")
        self.led.set_kv("post_merge_paused:p", "1")
        self.led.set_kv("post_merge_pause_alert:p", '{"status":"sent"}')
        self.gh.app = object()
        self.gh.app_login.return_value = "app/mahler-conductor"
        self.gh.exact_preview.return_value = (None, "HTTP 403")
        ping.side_effect = [False, True, True]
        self.tick()
        detail = post_merge.pause_diagnostic(self.led, "p")
        self.assertIn("HTTP 403", detail["reason"])
        request = detail["permission_request"]
        for expected in ("Deployments: Read-only", "settings/apps/mahler-conductor/permissions",
                         "settings/installations", "x/y", "access was denied"):
            self.assertIn(expected, request)
        self.assertEqual(detail["observed_at"], iso(self.now))
        self.led.close()
        self.led = Ledger(str(self.root / "ledger.db"), clock=lambda: self.now)
        self.ctx = scheduler.Ctx(self.cfg, self.led)
        self.ctx.gh = mock.Mock(return_value=self.gh)
        self.tick()
        self.tick()
        self.assertEqual(ping.call_count, 2)
        self.assertEqual(len(self.led.q("SELECT * FROM events WHERE kind='post_merge_probe_diagnostic'")), 1)
        self.assertEqual(len(self.led.q("SELECT * FROM events WHERE kind='post_merge_permission_delivered'")), 1)
        for observation in ("merge SHA not found", "deployment lookup timed out"):
            self.gh.exact_preview.return_value = (None, observation)
            self.tick()
            self.assertEqual(post_merge.pause_diagnostic(self.led, "p")["permission_request"], request)
        self.gh.exact_preview.assert_called_with(SHA, self.row()["target"], timeout=5, return_reason=True)
        self.now += timedelta(days=2)
        self.gh.exact_preview.return_value = ("https://preview.example", "")
        self.tick()
        self.assertEqual(post_merge.pause_diagnostic(self.led, "p"), {})
        self.assertEqual(self.row()["failure_code"], "paused_expired")
        self.assertNotEqual(self.row()["status"], "PASS")
        self.led.set_kv("post_merge_paused:p", "1")
        self.register("b" * 40)
        self.gh.exact_preview.return_value = (None, "HTTP 403")
        self.tick()
        self.assertEqual(ping.call_count, 3)
        post_merge.resume(self.ctx, "p")
        self.assertEqual(post_merge.pause_diagnostic(self.led, "p"), {})

    @mock.patch("mahler.notify.send", return_value=True)
    def test_paused_exception_is_safe_and_project_scoped(self, ping):
        self.cfg["projects"]["p"]["post_merge"].update(live_strategy="github")
        self.cfg["projects"]["q"] = copy.deepcopy(self.cfg["projects"]["p"])
        post_merge.register(self.ctx, "q", 1, {"state": "MERGED", "mergeCommit": {"oid": SHA}})
        for name in ("p", "q"):
            self.led.set_kv(f"post_merge_paused:{name}", "1")
        self.gh.app = object()
        self.gh.app_login.side_effect = RuntimeError("SENTINEL_SECRET")
        self.gh.exact_preview.side_effect = RuntimeError("HTTP 403 SENTINEL_SECRET")
        other = mock.Mock()
        other.exact_preview.return_value = ("https://preview.example", "")
        self.ctx.gh = lambda name: self.gh if name == "p" else other
        post_merge.advance(self.ctx, [{"name": "p"}, {"name": "q"}])
        detail = post_merge.pause_diagnostic(self.led, "p")
        self.assertIn("HTTP 403", detail["reason"])
        self.assertIn("https://github.com/settings/apps", detail["permission_request"])
        self.assertNotIn("mahler-conductor", detail["permission_request"])
        self.assertEqual(post_merge.pause_diagnostic(self.led, "q"), {})
        self.assertEqual(self.led.get_kv("post_merge_paused:q"), "0")
        self.assertNotIn("SENTINEL_SECRET", str(detail) + str(ping.call_args_list) +
                         str([dict(r) for r in self.led.q("SELECT * FROM events")]))
        self.ctx.dry_run = True
        calls = self.gh.exact_preview.call_count
        self.tick()
        self.assertEqual(self.gh.exact_preview.call_count, calls)

    @mock.patch("mahler.notify.send", return_value=True)
    def test_non_app_denial_does_not_invent_app_permission_request(self, ping):
        self.cfg["projects"]["p"]["post_merge"].update(live_strategy="github")
        self.led.set_kv("post_merge_paused:p", "1")
        self.gh.app = None
        self.gh.exact_preview.return_value = (None, "HTTP 403 SENTINEL_SECRET")
        self.tick()
        detail = post_merge.pause_diagnostic(self.led, "p")
        self.assertIn("HTTP 403", detail["reason"])
        self.assertNotIn("permission_request", detail)
        self.gh.app_login.assert_not_called()
        ping.assert_not_called()
        self.assertNotIn("SENTINEL_SECRET", post_merge.pause_text(self.led, "p"))

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

    def failed_mahler_live(self):
        self.cfg["projects"]["p"]["post_merge"].update(
            deploy_strategy="watch", live_strategy="mahler", live_command="")
        self.tick()
        self.led.update_post_merge_check(
            "p", 1, SHA, expected_phase="live", phase="live",
            status="FAIL", failure_code="timeout", summary="late launch")
        self.now += timedelta(days=1)

    def test_failed_live_recovers_once_through_normal_smoke_after_restart(self):
        self.failed_mahler_live()
        old_deadline = self.row()["deadline_at"]
        with mock.patch("mahler.launch_health.exact_live", return_value="mahler:" + SHA):
            self.tick()
            self.assertEqual((self.row()["status"], self.row()["phase"]), ("pending", "live"))
            self.assertGreater(self.row()["deadline_at"], old_deadline)
            self.restart()
            self.tick()
            self.assertEqual(self.row()["phase"], "smoke")
            self.tick()
            self.assertEqual(self.row()["status"], "PASS")
            self.tick()
        self.assertTrue(Path(self.row()["evidence_ref"]).is_file())
        self.assertEqual(len(self.led.q(
            "SELECT * FROM events WHERE kind='post_merge_live_reopened'")), 1)

    def test_failed_live_recovers_when_proven_head_contains_merge(self):
        self.failed_mahler_live()
        live = "b" * 40
        with mock.patch("mahler.launch_health.exact_live", return_value="mahler:" + live):
            self.tick()
            self.assertEqual((self.row()["status"], self.row()["phase"]), ("pending", "live"))
            self.tick()
            self.assertEqual((self.row()["phase"], self.row()["artifact_ref"]),
                             ("smoke", "mahler:" + live))
            self.tick()
            self.assertEqual(self.row()["status"], "PASS")
            self.assertEqual(self.row()["summary"], "Descendant live identity PASS (mahler self-host)")
            self.tick()
        self.assertIn(f"Live: {live}\nIdentity: descendant\n",
                      Path(self.row()["evidence_ref"]).read_text())
        self.assertEqual(len(self.led.q(
            "SELECT * FROM events WHERE kind='post_merge_live_reopened'")), 1)

    def test_failed_live_without_exact_proof_keeps_remediation(self):
        self.failed_mahler_live()
        original = dict(self.row())
        with mock.patch.object(post_merge, "_remediate") as remediate:
            for result in (None, "", RuntimeError("unavailable")):
                with mock.patch("mahler.launch_health.exact_live",
                                side_effect=[result]):
                    self.tick()
                self.assertEqual(dict(self.row()), original)
            self.assertEqual(remediate.call_count, 3)

    def test_recovered_live_cannot_reopen_after_second_timeout(self):
        self.failed_mahler_live()
        with mock.patch("mahler.launch_health.exact_live", return_value="mahler:" + SHA):
            self.tick()
            self.assertEqual(self.row()["status"], "pending")
            self.now += timedelta(days=1)
            with mock.patch.object(post_merge, "_remediate"):
                self.tick()
                self.restart()
                self.tick()
        self.assertEqual(self.row()["status"], "FAIL")
        self.assertEqual(len(self.led.q(
            "SELECT * FROM events WHERE kind='post_merge_live_reopened'")), 1)

    def mahler_smoke(self, live):
        self.cfg["projects"]["p"]["post_merge"].update(
            deploy_strategy="watch", live_strategy="mahler", live_command="")
        self.tick()
        with mock.patch("mahler.launch_health.exact_live", return_value="mahler:" + live):
            self.tick()
        self.assertEqual(self.row()["phase"], "smoke")

    def restart(self):
        self.led.close()
        self.led = Ledger(str(self.root / "ledger.db"), clock=lambda: self.now)
        self.ctx = scheduler.Ctx(self.cfg, self.led)
        self.ctx.gh = mock.Mock(return_value=self.gh)

    def test_mahler_exact_report_and_single_delivery_after_restart(self):
        self.assert_mahler_report_and_delivery(SHA, "exact")

    def test_mahler_descendant_report_and_single_delivery_after_restart(self):
        self.assert_mahler_report_and_delivery("b" * 40, "descendant")

    def assert_mahler_report_and_delivery(self, live, identity):
        self.mahler_smoke(live)
        self.restart()
        with (
            mock.patch("mahler.launch_health.exact_live",
                       return_value="mahler:" + live) as observe,
            mock.patch.object(post_merge.subprocess, "Popen") as launch,
        ):
            self.tick()
        observe.assert_called_once_with(SHA, timeout=5, env=None)
        launch.assert_not_called()
        row = self.row()
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["live_sha"], SHA)
        self.assertEqual(row["artifact_ref"], "mahler:" + live)
        self.assertEqual(row["summary"],
                         f"{identity.title()} live identity PASS (mahler self-host)")
        report = Path(row["evidence_ref"])
        self.assertEqual(report.read_bytes(),
            f"Tag: {row['tag']}\nMerge: {SHA}\nLive: {live}\n"
            f"Identity: {identity}\nSmoke: PASS\n".encode())
        self.led.add_uat("p", 1, 4, SHA, "test", "verify")
        self.restart()
        self.tick()
        self.restart()
        self.tick()
        self.assertEqual(len(list(self.root.rglob("report.md"))), 1)
        self.gh.comment_create.assert_called_once_with(
            1, "Smoke: PASS report=" + report.as_uri())
        self.assertEqual(len(self.led.q(
            "SELECT * FROM kv WHERE key LIKE 'post-merge-evidence:%'")), 1)

    @mock.patch("mahler.notify.send")
    def test_mahler_smoke_reobserves_until_original_deadline(self, ping):
        self.mahler_smoke(SHA)
        deadline = self.row()["deadline_at"]
        from mahler.ledger import parse
        self.now = parse(deadline) - timedelta(seconds=4)
        with (
            mock.patch("mahler.launch_health.exact_live") as observe,
            mock.patch.object(post_merge.subprocess, "Popen") as launch,
        ):
            for result in (None, "mahler:" + "b" * 40, RuntimeError("private output")):
                observe.side_effect = result if isinstance(result, Exception) else None
                observe.return_value = result
                self.restart()
                self.tick()
                observe.assert_called_with(SHA, timeout=2, env=None)
                row = self.row()
                self.assertEqual((row["status"], row["phase"]), ("pending", "smoke"))
                self.assertEqual(row["summary"], "exact live identity not yet verified")
                self.assertEqual(row["deadline_at"], deadline)
                self.assertFalse(row["evidence_ref"])
                self.assertFalse(list(self.root.rglob("report.md")))
            self.now = parse(deadline)
            self.tick()
            self.assertEqual(self.row()["failure_code"], "timeout")
            launch.assert_not_called()
        self.assertFalse(self.led.q(
            "SELECT * FROM kv WHERE key LIKE 'post-merge-evidence:%'"))

    def test_mahler_smoke_observation_cannot_pass_after_deadline(self):
        self.mahler_smoke(SHA)
        from mahler.ledger import parse
        def observe(*args, **kwargs):
            self.now = parse(self.row()["deadline_at"])
            return "mahler:" + SHA
        with mock.patch("mahler.launch_health.exact_live", side_effect=observe):
            self.tick()
        self.assertEqual(self.row()["failure_code"], "timeout")
        self.assertFalse(list(self.root.rglob("report.md")))

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

    @mock.patch("mahler.notify.send")
    def test_live_lookup_http_403_recorded_in_summary_and_failure_event_without_payload(self, ping):
        from mahler.gh import GH, GHError
        self.cfg["projects"]["p"]["post_merge"].update(
            deploy_strategy="watch", live_strategy="github", live_command="")
        self.tick()
        self.gh.pr_for_commit.return_value = 42
        provider = GH("x/y")
        self.gh.exact_preview.side_effect = provider.exact_preview
        secret_payload = "gh: Resource not accessible by integration (HTTP 403) authorization_token=SECRET_123"
        with mock.patch.object(provider, "_gh", side_effect=GHError(secret_payload)):
            self.tick()
        self.assertEqual(self.row()["status"], "pending")
        self.assertIn("HTTP 403", self.row()["summary"])
        self.assertNotIn("SECRET_123", self.row()["summary"])
        self.assertNotIn("authorization_token", self.row()["summary"])

        self.ctx = scheduler.Ctx(self.cfg, self.led)
        self.ctx.gh = mock.Mock(return_value=self.gh)
        self.now += timedelta(seconds=1800)
        self.tick()
        self.assertEqual(self.row()["status"], "FAIL")
        self.assertEqual(self.row()["failure_code"], "timeout")
        self.assertIn("HTTP 403", self.row()["summary"])
        self.assertNotIn("SECRET_123", self.row()["summary"])
        self.assertNotIn("authorization_token", self.row()["summary"])
        self.assertIn("HTTP 403", self.gh.create_issue.call_args.args[1])
        self.assertNotIn("SECRET_123", self.gh.create_issue.call_args.args[1])
        event = self.led.q1("SELECT detail FROM events WHERE kind='post_merge_failed'")
        detail = json.loads(event["detail"])
        self.assertIn("HTTP 403", detail["diagnostic"])
        self.assertNotIn("SECRET_123", detail["diagnostic"])

    @mock.patch("mahler.notify.send")
    def test_live_lookup_timeout_recorded_in_summary_and_failure_event(self, ping):
        from mahler.gh import GH, GHError
        self.cfg["projects"]["p"]["post_merge"].update(
            deploy_strategy="watch", live_strategy="github", live_command="")
        self.tick()
        self.gh.pr_for_commit.return_value = 42
        provider = GH("x/y")
        self.gh.exact_preview.side_effect = provider.exact_preview
        with mock.patch.object(provider, "_gh", side_effect=GHError("gh api: Command '['gh']' timed out after 5 seconds")):
            self.tick()
        self.assertEqual(self.row()["status"], "pending")
        self.assertIn("timed out", self.row()["summary"])
        self.ctx = scheduler.Ctx(self.cfg, self.led)
        self.ctx.gh = mock.Mock(return_value=self.gh)
        self.now += timedelta(seconds=1800)
        self.tick()
        self.assertEqual(self.row()["status"], "FAIL")
        self.assertEqual(self.row()["failure_code"], "timeout")
        self.assertIn("timed out", self.row()["summary"])
        self.assertIn("timed out", self.gh.create_issue.call_args.args[1])
        event = self.led.q1("SELECT detail FROM events WHERE kind='post_merge_failed'")
        detail = json.loads(event["detail"])
        self.assertIn("timed out", detail["diagnostic"])

    def test_observe_live_direct_exception_preserves_failure_class(self):
        from mahler.gh import GHError
        self.cfg["projects"]["p"]["post_merge"].update(
            deploy_strategy="watch", live_strategy="github", live_command="")
        self.tick()
        # Direct exception from exact_preview with HTTP 403
        self.gh.exact_preview.side_effect = GHError("HTTP 403 Forbidden secret=leak")
        self.tick()
        self.assertEqual(self.row()["status"], "pending")
        self.assertIn("HTTP 403", self.row()["summary"])
        self.assertNotIn("secret=leak", self.row()["summary"])

        # Direct exception from exact_preview with timeout
        import subprocess
        self.gh.exact_preview.side_effect = subprocess.TimeoutExpired(["gh"], 5)
        self.tick()
        self.assertEqual(self.row()["status"], "pending")
        self.assertIn("timed out", self.row()["summary"])

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
        self.gh.exact_preview.return_value = (None, "merge SHA not found")
        self.tick()
        self.assertEqual(self.led.get_kv("post_merge_paused:p"), "1")
        self.gh.exact_preview.return_value = ("https://preview.example", "")
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
