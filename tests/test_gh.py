import json
import unittest
from unittest.mock import patch, MagicMock
from mahler.gh import GH, GHError, project_client, pr_body, HELP_FOOTER, AGENT_NOTE

class TestGH(unittest.TestCase):
    def setUp(self):
        self.gh = GH("mkny13/mahler")
        self.gh._gh = MagicMock()

    def test_parent_inventory_paginates_and_excludes_prs(self):
        self.gh._gh.side_effect = [json.dumps([{"number": n} for n in range(100)]),
                                  json.dumps([{"number": 101}, {"number": 102, "pull_request": {}}])]
        self.assertEqual(len(self.gh.parent_issue_inventory()), 101)
        self.assertIn("page=2", self.gh._gh.call_args.args[1])

    def test_native_children_pagination_and_cross_repository_guard(self):
        def row(n, repo="mkny13/mahler"):
            return {"number": n, "repository_url": f"https://api.github.com/repos/{repo}"}
        self.gh._gh.side_effect = [json.dumps([row(n) for n in range(100)]), json.dumps([row(101)])]
        self.assertEqual(self.gh.sub_issues(10), [*range(100), 101])
        self.gh._gh.side_effect = None
        self.gh._gh.return_value = json.dumps([row(1, "other/repo")])
        with self.assertRaises(GHError):
            self.gh.sub_issues(10)
        self.gh._gh.side_effect = [json.dumps([row(n) for n in range(100)]), GHError("offline")]
        with self.assertRaises(GHError):
            self.gh.sub_issues(10)

    def test_closing_prs_include_historical_and_exhaust_pagination(self):
        def page(more, cursor, state):
            return json.dumps({"data": {"repository": {"issue": {"closedByPullRequestsReferences": {
                "nodes": [{"number": 9, "state": state, "repository": {"nameWithOwner": "mkny13/mahler"}}],
                "pageInfo": {"hasNextPage": more, "endCursor": cursor}}}}}})
        self.gh._gh.side_effect = [page(True, "next", "CLOSED"), page(False, None, "MERGED")]
        rows = self.gh.closing_prs(10)
        self.assertEqual([r["state"] for r in rows], ["CLOSED", "MERGED"])
        self.assertIn("includeClosedPrs:true", self.gh._gh.call_args.args[3])
        self.assertIn("cursor=next", self.gh._gh.call_args.args)
        self.gh._gh.side_effect = [page(True, "next", "CLOSED"), page(True, "next", "MERGED")]
        with self.assertRaisesRegex(GHError, "incomplete"):
            self.gh.closing_prs(10)
        self.gh._gh.side_effect = [json.dumps({"errors": [{"message": "failed"}]})]
        with self.assertRaises(GHError):
            self.gh.closing_prs(10)

    def test_dependency_rebase_requests_use_project_credentials(self):
        env = {"GH_CONFIG_DIR": "/test/work-account"}
        client = GH("work/repo", env=env)
        with patch("mahler.gh._gh", return_value="") as call:
            client.request_dependency_rebase(10, "dependabot")
            self.assertEqual(call.call_args.args,
                             ("issue", "comment", "10", "-R", "work/repo", "--body-file", "-"))
            self.assertTrue(call.call_args.kwargs["input"].startswith("<!-- mahler:agent -->"))
            self.assertIn("@dependabot rebase", call.call_args.kwargs["input"])
            self.assertEqual(call.call_args.kwargs["env"], env)
            call.reset_mock()
            call.side_effect = ['[[]]', '', '']
            client.request_dependency_rebase(11, "renovate")
            self.assertEqual([c.args for c in call.call_args_list], [
                ("api", "repos/work/repo/labels?per_page=100", "--paginate", "--slurp"),
                ("label", "create", "rebase", "-R", "work/repo"),
                ("issue", "edit", "11", "-R", "work/repo", "--add-label", "rebase")])
            self.assertTrue(all(c.kwargs["env"] == env for c in call.call_args_list))
            call.reset_mock()
            call.side_effect = ['[[{"name": "rebase", "color": "abcdef"}]]', '']
            client.request_dependency_rebase(11, "renovate")
            self.assertEqual(call.call_count, 2)
            self.assertEqual(call.call_args.args[0:2], ("issue", "edit"))
            call.reset_mock()
            with self.assertRaises(ValueError):
                client.request_dependency_rebase(12, "unknown")
            call.assert_not_called()

    def test_dependabot_rebase_bypasses_app_using_project_user(self):
        cfg = {"github_app": {"app_id": 123, "installation_id": 456,
                              "private_key_path": "/test/key.pem"}}
        for env in (None, {"GH_CONFIG_DIR": "/test/work-account",
                           "GH_TOKEN": "test-user-token"}):
            with self.subTest(env=env), \
                    patch("mahler.gh.config.run_env", return_value=env), \
                    patch("mahler.github_app.Installation") as installation, \
                    patch("mahler.gh._gh", return_value="") as call:
                app = installation.return_value
                app.token.return_value = "test-app-token"
                client = project_client(cfg, {"repo": "owner/repo"})
                client.request_dependency_rebase(10, "dependabot")
                app.token.assert_not_called()
                self.assertEqual(call.call_args.kwargs["env"], env)
                self.assertEqual(call.call_args.kwargs["input"],
                                 AGENT_NOTE + "\n@dependabot rebase" + HELP_FOOTER)
                # Other conductor comments still use the configured App.
                client.comment(10, "ordinary comment")
                self.assertEqual(call.call_args.kwargs["env"]["GH_TOKEN"],
                                 "test-app-token")
                app.token.reset_mock()
                call.side_effect = GHError("user login unavailable")
                with self.assertRaisesRegex(GHError, "user login unavailable"):
                    client.request_dependency_rebase(10, "dependabot")
                app.token.assert_not_called()

    def test_ci_retry_exact_head_workflow_job_and_failed_only_endpoint(self):
        failed = {"id": 10, "head_sha": "head", "workflow_id": 8, "run_attempt": 1,
                  "status": "completed", "conclusion": "failure"}
        base = {**failed, "id": 20, "head_sha": "tip", "conclusion": "success", "event": "push"}
        def reply(method, path, payload=None):
            if path.startswith("actions/runs?head_sha=head"):
                rows = [failed, {**failed, "id": 11, "head_sha": "other"},
                        {**failed, "id": 12, "workflow_id": 9, "conclusion": "success"}]
                return {"total_count": len(rows), "workflow_runs": rows}
            if path.startswith("actions/runs?head_sha=tip"):
                return {"total_count": 1, "workflow_runs": [base]}
            conclusion = "failure" if "/10/" in path else "success"
            return {"total_count": 1, "jobs": [{"name": "verify", "conclusion": conclusion}]}
        with patch.object(self.gh, "_api_json", side_effect=reply), \
                patch.object(self.gh, "branch_sha", return_value="tip") as tip:
            self.assertEqual(self.gh.ci_retry_runs("head", "release"), [{"id": 10, "attempt": 1}])
            tip.assert_called_with("release")
            base["workflow_id"] = 99
            self.assertEqual(self.gh.ci_retry_runs("head", "release"), [])
            self.assertEqual(self.gh.ci_retry_runs("head", "release", reported=True), [{"id": 10, "attempt": 1}])
        with patch.object(self.gh, "_api_json") as api:
            self.gh.rerun_failed_jobs(10)
            api.assert_called_once_with("POST", "actions/runs/10/rerun-failed-jobs")

    def test_ci_retry_rejects_incomplete_ambiguous_or_different_jobs(self):
        head = {"id": 10, "head_sha": "head", "workflow_id": 8, "run_attempt": 1,
                "status": "completed", "conclusion": "failure"}
        base = {**head, "id": 20, "head_sha": "tip", "event": "push", "conclusion": "success"}
        for case in ("missing", "ambiguous", "job", "pagination", "non-actions", "duplicate-job"):
            with self.subTest(case=case):
                rows = [] if case == "non-actions" else [head]
                bases = [] if case == "missing" else [base, base] if case == "ambiguous" else [base]
                replies = [
                    {"total_count": len(rows), "workflow_runs": rows},
                    {"total_count": len(bases), "workflow_runs": bases},
                    {"total_count": 1, "jobs": [{"name": "verify", "conclusion": "failure"}]},
                    {"total_count": 2 if case == "pagination" else 1,
                     "jobs": [{"name": "other" if case == "job" else "verify", "conclusion": "success"}]},
                ]
                if case == "duplicate-job":
                    replies[-1]["jobs"].append({"name": "verify", "conclusion": "failure"})
                    replies[-1]["total_count"] = 2
                with patch.object(self.gh, "_api_json", side_effect=replies), \
                        patch.object(self.gh, "branch_sha", return_value="tip"):
                    self.assertEqual(self.gh.ci_retry_runs("head", "main"), [])

    def test_exact_preview_fixtures(self):
        sha = "a" * 40
        deployment = {"id": 100, "sha": sha, "environment": "Preview",
                      "production_environment": False,
                      "repository_url": "https://api.github.com/repos/mkny13/mahler"}
        success = {"id": 200, "state": "success", "environment": "Preview",
                   "environment_url": "https://preview.example/build-10"}
        cases = [
            ([deployment], [success], success["environment_url"]),
            ([{**deployment, "sha": "b" * 40}], [success], None),
            ([deployment, {**deployment, "id": 101}], [success], None),
            ([deployment, {**deployment, "id": 101, "sha": "b" * 40}], [success], None),
            ([{**deployment, "production_environment": True}], [success], None),
            ([{**deployment, "environment": "production"}], [success], None),
            ([{**deployment, "repository_url": "https://api.github.com/repos/other/repo"}], [success], None),
            ([deployment], [success, {**success, "state": "failure"}], None),
            ([deployment], [success, {**success, "id": 201, "state": "pending"}], None),
            ([deployment], [{**success, "id": 199, "state": "pending"}, success], success["environment_url"]),
            ([deployment], [], None),
            ([deployment] * 100, [success], None),
            ([deployment], [success] * 100, None),
            ([deployment, *[{**deployment, "id": 100 - i, "sha": f"{i:040x}"} for i in range(1, 100)]], [success], success["environment_url"]),
            ([deployment], [success, *[{**success, "id": 200 - i, "state": "pending"} for i in range(1, 100)]], success["environment_url"]),
        ]
        for field, value in (("state", "failure"), ("state", "inactive"),
                             ("environment", "production"), ("environment_url", ""),
                             ("environment_url", "https://user:secret@preview.example"),
                             ("environment_url", "https://preview.example?token=secret"),
                             ("environment_url", "https://preview.example/#secret"),
                             ("environment_url", "http://preview.example")):
            cases.append(([deployment], [{**success, field: value}], None))
        for deployments, statuses, expected in cases:
            with self.subTest(deployments=deployments, statuses=statuses):
                self.gh._gh.side_effect = [json.dumps(deployments), json.dumps(statuses)]
                self.assertEqual(self.gh.exact_preview(sha, "Preview"), expected)
        self.gh._gh.reset_mock()
        self.gh._gh.side_effect = [json.dumps([deployment]), json.dumps([success])]
        self.gh.exact_preview(sha, "Preview", timeout=2)
        calls = self.gh._gh.call_args_list
        self.assertIn("repos/mkny13/mahler/deployments?environment=Preview", calls[0].args[-1])
        self.assertNotIn("sha=", calls[0].args[-1])
        self.assertTrue(all(c.kwargs["timeout"] == 2 for c in calls))
        self.gh._gh.reset_mock()
        self.assertIsNone(self.gh.exact_preview(sha, "Production"))
        self.gh._gh.assert_not_called()

    def test_screenshot_preview_filters_and_newest_status(self):
        sha = 'a' * 40
        deployment = {"id": 1, "sha": sha, "environment": "Preview",
                      "production_environment": False}
        for changes in ({"sha": "wrong"}, {"environment": "Production"},
                        {"production_environment": True}):
            self.gh._gh.reset_mock()
            self.gh._gh.return_value = json.dumps([{**deployment, **changes}])
            self.assertIsNone(self.gh.screenshot_preview(sha, "Preview"))
            self.assertEqual(self.gh._gh.call_count, 1)
        for status in ([], [{"state": "pending"}],
                       [{"state": "success", "log_url": "https://example.com"}],
                       [{"state": "success", "environment_url": "https://user:pass@example.com"}],
                       [{"state": "success", "environment_url": "http://example.com"}]):
            self.gh._gh.side_effect = [json.dumps([deployment]), json.dumps(status)]
            self.assertIsNone(self.gh.screenshot_preview(sha, "Preview"))
        self.gh._gh.side_effect = [json.dumps([{**deployment, "id": 2}, deployment]),
                                 json.dumps([{"state": "pending"}]),
                                 json.dumps([{"state": "success", "environment_url": "https://preview.example"}])]
        self.assertEqual(self.gh.screenshot_preview(sha, "Preview"), "https://preview.example")
        self.assertIn("per_page=1", self.gh._gh.call_args.args[-1])
        self.gh._gh.reset_mock()
        self.assertIsNone(self.gh.screenshot_preview(sha, "pRoDuCtIoN"))
        self.gh._gh.assert_not_called()

    def test_publish_artifacts_creates_orphan_branch_without_other_refs(self):
        calls = []

        def api(*args, input=None, **kw):
            calls.append((args[2], args[3].split("/", 3)[-1], json.loads(input) if input else None))
            path = args[3]
            if args[2] == "GET":
                raise GHError("gh api: HTTP 404: Not Found")
            return json.dumps({"sha": "s" * 40})
        from mahler.gh import GHError
        self.gh._gh.side_effect = api
        commit = self.gh.publish_artifacts("mahler-screenshots", "pr-1/" + "a" * 40,
                                           {"a.png": b"png", "manifest.json": b"{}"}, "msg")
        self.assertEqual(commit, "s" * 40)
        methods = [(m, p) for m, p, _ in calls]
        self.assertEqual(methods[-1], ("POST", "git/refs"))
        self.assertTrue(all(p.startswith(("git/ref/heads/mahler-screenshots", "git/blobs",
                                          "git/trees", "git/commits", "git/refs"))
                            for _, p in methods))
        self.assertEqual(calls[-1][2]["ref"], "refs/heads/mahler-screenshots")
        commit_body = [b for m, p, b in calls if p == "git/commits"][0]
        self.assertEqual(commit_body["parents"], [])
        tree_body = [b for m, p, b in calls if p == "git/trees"][0]
        self.assertNotIn("base_tree", tree_body)
        self.assertEqual(len(tree_body["tree"]), 2)

    def test_publish_artifacts_advances_without_force_and_reuses_head(self):
        calls = []
        directory = "pr-1/" + "a" * 40

        def api(*args, input=None, **kw):
            calls.append((args[2], args[3].split("/", 3)[-1], json.loads(input) if input else None))
            path = args[3]
            if "git/ref/heads" in path:
                return json.dumps({"object": {"sha": "p" * 40}})
            if "git/commits/" in path:
                return json.dumps({"tree": {"sha": "t" * 40}})
            if "git/trees/" in path:
                return json.dumps({"tree": existing})
            return json.dumps({"sha": "n" * 40})
        self.gh._gh.side_effect = api
        existing = []
        self.gh.publish_artifacts("mahler-screenshots", directory, {"a.png": b"x"}, "m")
        patch_call = calls[-1]
        self.assertEqual(patch_call[0], "PATCH")
        self.assertEqual(patch_call[2], {"sha": "n" * 40, "force": False})
        self.assertEqual([b for m, p, b in calls if p == "git/trees"][0]["base_tree"], "t" * 40)
        calls.clear()
        existing.append({"path": directory + "/a.png"})
        self.assertEqual(self.gh.publish_artifacts("mahler-screenshots", directory,
                                                   {"a.png": b"x"}, "m"), "p" * 40)
        self.assertFalse([c for c in calls if c[0] in ("POST", "PATCH")])

    def test_publish_artifacts_refuses_application_branches(self):
        from mahler.gh import GHError
        for branch in ("main", "master", "", "a b", "../x"):
            with self.assertRaises(GHError):
                self.gh.publish_artifacts(branch, "pr-1/x", {}, "m")
        self.gh._gh.assert_not_called()

    def test_artifact_url_is_commit_pinned(self):
        self.assertEqual(self.gh.artifact_url("c" * 40, "pr-1/x/a b.png"),
                         f"https://github.com/mkny13/mahler/blob/{'c' * 40}/pr-1/x/a%20b.png")

    def test_screenshot_discovery_bound_and_exact_query(self):
        sha = 'a' * 40
        deployments = [{"id": i, "sha": sha, "environment": "Preview",
                        "production_environment": False} for i in range(30)]
        self.gh._gh.side_effect = [json.dumps(deployments)] + ['[]'] * 20
        self.assertIsNone(self.gh.screenshot_preview(sha, "Preview"))
        self.assertEqual(self.gh._gh.call_count, 21)
        query = self.gh._gh.call_args_list[0].args[-1]
        self.assertIn(f"sha={sha}&environment=Preview&per_page=20", query)

    def test_shipped_label_replaces_closed_issue_state_and_exists_on_old_repos(self):
        self.gh._gh.return_value = json.dumps({"labels": [
            {"name": "mahler:verifying"}, {"name": "type:feature"}]})
        labels = self.gh.issue_labels(123)
        self.gh.set_state_label(123, "shipped", labels)
        self.assertEqual(labels, ["mahler:verifying", "type:feature"])
        self.gh._gh.assert_any_call("label", "create", "mahler:shipped", "-R",
                                  "mkny13/mahler", "--color", "0e8a16", "--force")
        self.gh._gh.assert_called_with("issue", "edit", "123", "-R", "mkny13/mahler",
                                      "--remove-label", "mahler:verifying",
                                      "--add-label", "mahler:shipped")

    def test_refresh_mergeability_scoped_get_and_errors(self):
        from mahler.gh import GHError
        self.gh.pr_refresh_mergeability(88)
        self.gh._gh.assert_called_once_with("api", "--method", "GET", "repos/mkny13/mahler/pulls/88")
        self.gh._gh.side_effect = GHError("offline")
        with self.assertRaises(GHError):
            self.gh.pr_refresh_mergeability(88)

    def test_comment_appends_footer_for_agent(self):
        self.gh.comment(123, "Test body")
        expected_body = f"{AGENT_NOTE}\nTest body{HELP_FOOTER}"
        self.gh._gh.assert_called_once_with("issue", "comment", "123", "-R", "mkny13/mahler", "--body-file", "-", input=expected_body)

    def test_comment_does_not_append_footer_if_already_present(self):
        self.gh.comment(123, f"Test body{HELP_FOOTER}")
        expected_body = f"{AGENT_NOTE}\nTest body{HELP_FOOTER}"
        self.gh._gh.assert_called_once_with("issue", "comment", "123", "-R", "mkny13/mahler", "--body-file", "-", input=expected_body)

    def test_comment_does_not_append_footer_for_human(self):
        self.gh.comment(123, "Test body", agent=False)
        self.gh._gh.assert_called_once_with("issue", "comment", "123", "-R", "mkny13/mahler", "--body-file", "-", input="Test body")

    def test_pr_body_appends_footer(self):
        body = pr_body(123, "Test summary")
        self.assertTrue(body.endswith(HELP_FOOTER))
        self.assertIn("Test summary", body)

    def test_pr_body_with_needs(self):
        body = pr_body(123, "Test summary", needs="- [ ] check this")
        self.assertTrue(body.endswith(HELP_FOOTER))
        self.assertIn("## Needs a human to check\n- [ ] check this", body)

    def relationship_result(self, *, blocking=(), blocked_by=()):
        return json.dumps({
            "blocking": {"nodes": [{"number": n} for n in blocking],
                         "totalCount": len(blocking)},
            "blockedBy": {"nodes": [{"number": n} for n in blocked_by],
                          "totalCount": len(blocked_by)},
        })

    def test_blocking_of_empty_single_and_multiple(self):
        for numbers in ([], [12], [12, 34, 56]):
            with self.subTest(numbers=numbers):
                self.gh._relationships.clear()
                self.gh._gh.reset_mock()
                self.gh._gh.return_value = self.relationship_result(blocking=numbers)
                self.assertEqual(self.gh.blocking_of(7), numbers)
                self.gh._gh.assert_called_once_with(
                    "issue", "view", "7", "-R", "mkny13/mahler",
                    "--json", "blocking,blockedBy")

    def test_blocked_by_of_empty_single_and_multiple(self):
        for numbers in ([], [12], [12, 34, 56]):
            with self.subTest(numbers=numbers):
                self.gh._relationships.clear()
                self.gh._gh.reset_mock()
                self.gh._gh.return_value = self.relationship_result(blocked_by=numbers)
                self.assertEqual(self.gh.blocked_by_of(7), numbers)
                self.gh._gh.assert_called_once_with(
                    "issue", "view", "7", "-R", "mkny13/mahler",
                    "--json", "blocking,blockedBy")

    def test_open_issues_grows_limit_until_complete(self):
        issues = [{"number": n} for n in range(601)]
        self.gh._gh.side_effect = [json.dumps(issues[:300]),
                                  json.dumps(issues[:600]), json.dumps(issues)]
        self.assertEqual(self.gh.open_issues(), issues)
        limits = [call.args[call.args.index("--limit") + 1]
                  for call in self.gh._gh.call_args_list]
        self.assertEqual(limits, ["300", "600", "1200"])
        self.assertEqual(self.gh.blocked_by_of(600), [])

    def test_open_issues_batches_relationships_for_both_directions(self):
        issue = {"number": 7, "blocking": {"nodes": [{"number": 8}]},
                 "blockedBy": {"nodes": [{"number": 6}]}}
        self.gh._gh.return_value = json.dumps([issue])
        self.assertEqual(self.gh.open_issues(), [issue])
        self.assertEqual(self.gh.blocking_of(7), [8])
        self.assertEqual(self.gh.blocked_by_of(7), [6])
        self.gh._gh.assert_called_once()

    def test_branch_sha(self):
        self.gh._gh.return_value = json.dumps({"sha": "c0ffee1234567890"})
        sha = self.gh.branch_sha("main")
        self.assertEqual(sha, "c0ffee1234567890")
        self.gh._gh.assert_called_once_with("api", "repos/mkny13/mahler/commits/main")

    def test_pr_ci_toolchain_files_filters_changed_paths(self):
        self.gh._gh.return_value = (
            ".github/workflows/macos-tests.yml\n"
            "app/project.yml\n"
            "android/gradle/wrapper/gradle-wrapper.properties\n"
            "package.json\n"
            "ios/Package.resolved\n"
            ".swift-version\n"
            "src/Player.swift\n"
            "docs/locksmith.md\n"
            "yarn.lock\n"
        )
        self.assertEqual(self.gh.pr_ci_toolchain_files(519), [
            ".github/workflows/macos-tests.yml",
            "app/project.yml",
            "android/gradle/wrapper/gradle-wrapper.properties",
            "package.json",
            "ios/Package.resolved",
            ".swift-version",
            "yarn.lock",
        ])
        self.gh._gh.assert_called_once_with(
            "pr", "diff", "519", "-R", "mkny13/mahler", "--name-only")

    def test_get_release_found(self):
        rel_data = {
            "tagName": "v0.1.0",
            "targetCommitish": "c0ffee",
            "body": "Release notes",
            "url": "https://github.com/mkny13/mahler/releases/tag/v0.1.0"
        }
        self.gh._gh.return_value = json.dumps(rel_data)
        rel = self.gh.get_release("v0.1.0")
        self.assertEqual(rel, rel_data)
        self.gh._gh.assert_called_once_with("release", "view", "v0.1.0", "-R", "mkny13/mahler",
                                           "--json", "tagName,targetCommitish,body,url")

    def test_get_release_not_found(self):
        from mahler.gh import GHError
        self.gh._gh.side_effect = GHError("gh release view: release not found")
        rel = self.gh.get_release("v0.1.0")
        self.assertIsNone(rel)

    def test_get_release_other_error_raises(self):
        from mahler.gh import GHError
        self.gh._gh.side_effect = GHError("gh release view: network timeout")
        with self.assertRaises(GHError):
            self.gh.get_release("v0.1.0")

    def test_latest_release_resolves_checkpoint_sha(self):
        rel_data = {
            "tagName": "v0.83", "targetCommitish": "main", "body": "Release notes",
            "url": "https://github.com/mkny13/couch-tour/releases/tag/v0.83",
            "publishedAt": "2026-09-10T12:00:00Z",
        }
        self.gh._gh.return_value = json.dumps(rel_data)
        with patch.object(self.gh, "get_tag_sha", return_value="c0ffee") as get_tag:
            release = self.gh.latest_release()
        self.assertEqual(release["checkpointSha"], "c0ffee")
        self.assertEqual(release["tagName"], "v0.83")
        get_tag.assert_called_once_with("v0.83")
        self.gh._gh.assert_called_once_with(
            "release", "view", "-R", "mkny13/mahler",
            "--json", "tagName,targetCommitish,body,url,publishedAt")

    def test_latest_release_missing_returns_none(self):
        from mahler.gh import GHError
        self.gh._gh.side_effect = GHError("gh release view: no releases found")
        self.assertIsNone(self.gh.latest_release())

    def test_get_tag_sha_commit(self):
        self.gh._gh.return_value = json.dumps({
            "object": {"type": "commit", "sha": "commit_sha_123"}
        })
        sha = self.gh.get_tag_sha("v0.1.0")
        self.assertEqual(sha, "commit_sha_123")
        self.gh._gh.assert_called_once_with("api", "repos/mkny13/mahler/git/ref/tags/v0.1.0")

    def test_get_tag_sha_annotated_tag(self):
        self.gh._gh.side_effect = [
            json.dumps({"object": {"type": "tag", "sha": "tag_obj_sha"}}),
            json.dumps({"object": {"type": "commit", "sha": "target_commit_sha"}}),
        ]
        sha = self.gh.get_tag_sha("v0.1.0")
        self.assertEqual(sha, "target_commit_sha")
        self.assertEqual(self.gh._gh.call_count, 2)

    def test_get_tag_sha_not_found(self):
        from mahler.gh import GHError
        self.gh._gh.side_effect = GHError("gh api: Not Found (HTTP 404)")
        sha = self.gh.get_tag_sha("v0.1.0")
        self.assertIsNone(sha)

    def test_release_create(self):
        self.gh._gh.return_value = "https://github.com/mkny13/mahler/releases/tag/v0.1.0\n"
        url = self.gh.release_create("v0.1.0", "target_sha", "v0.1.0", "Notes content")
        self.assertEqual(url, "https://github.com/mkny13/mahler/releases/tag/v0.1.0")
        self.gh._gh.assert_called_once_with(
            "release", "create", "v0.1.0", "-R", "mkny13/mahler",
            "--target", "target_sha", "--title", "v0.1.0",
            "--notes-file", "-", input="Notes content"
        )

if __name__ == '__main__':
    unittest.main()


class TestReviewMarkerLookup(unittest.TestCase):
    def test_exact_marker_across_pages_and_closed_issues(self):
        gh = GH("owner/repo")
        marker = "<!-- mahler:review-follow-up:abc -->"
        with patch.object(gh, "_gh", return_value=json.dumps([
                [{"body": marker, "pull_request": {}, "html_url": "pr"},
                 {"body": marker[:-4], "html_url": "partial"}],
                [{"body": marker, "state": "closed", "html_url": "found"}]])) as call:
            self.assertEqual(gh.issue_by_marker(marker), "found")
            call.assert_called_once_with("api", "--paginate", "--slurp",
                "repos/owner/repo/issues?state=all&per_page=100")
        with patch.object(gh, "_gh", return_value='[[{"body": null}]]'):
            self.assertIsNone(gh.issue_by_marker(marker))


class ReviewedBranchUpdateTests(unittest.TestCase):
    """Real git updates against a temporary remote; no GitHub or live state."""

    def setUp(self):
        import os
        import tempfile
        from tests.test_runner import sh, write
        self.sh, self.write = sh, write
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo, self.remote = os.path.join(tmp.name, 'repo'), os.path.join(tmp.name, 'remote.git')
        sh(tmp.name, 'git', 'init', '-q', '--bare', self.remote)
        sh(tmp.name, 'git', 'init', '-q', '-b', 'release', self.repo)
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.com')
        self.git('remote', 'add', 'origin', self.remote)
        self.file = os.path.join(self.repo, 'work.txt')
        write(self.file, 'original\n')
        self.commit('initial')
        self.base = self.git('rev-parse', 'HEAD')
        self.git('push', '-q', 'origin', 'release')
        self.git('checkout', '-qb', 'topic')
        write(self.file, 'reviewed\n')
        self.commit('reviewed work')
        self.head = self.git('rev-parse', 'HEAD')
        self.git('push', '-q', 'origin', 'topic')
        self.git('checkout', '-q', 'release')
        self.client = GH('test/repo')

    def git(self, *args):
        return self.sh(self.repo, 'git', *args)

    def commit(self, message):
        self.git('add', '.')
        self.git('commit', '-qm', message)

    def advance(self, content=None):
        import os
        self.write(self.file if content else os.path.join(self.repo, 'base.txt'), content or 'base change\n')
        self.commit('advance target')
        self.git('push', '-q', 'origin', 'release')

    def update(self, authorize=lambda: True):
        return self.client.update_reviewed_branch(self.repo, 'topic', 'release', self.head, authorize)

    def remote_head(self):
        return self.git('ls-remote', 'origin', 'refs/heads/topic').split()[0]

    def test_clean_update_preserves_exact_patch_and_checkout(self):
        self.advance()
        before = self.git('rev-parse', 'HEAD')
        updated, unchanged = self.update()
        self.assertTrue(unchanged)
        self.assertNotEqual(updated, self.head)
        self.assertEqual(self.remote_head(), updated)
        self.assertEqual(self.git('rev-parse', 'HEAD'), before)
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertTrue(self.client.base_in_head(self.repo, 'release', updated))

    def test_conflict_keeps_remote_head(self):
        self.advance('conflicting base\n')
        self.assertIsNone(self.update())
        self.assertEqual(self.remote_head(), self.head)
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_clean_merge_with_changed_patch_does_not_preserve_review(self):
        self.advance('reviewed\n')
        updated, unchanged = self.update()
        self.assertFalse(unchanged)
        self.assertEqual(self.remote_head(), updated)

    def test_lost_lease_and_concurrent_push_leave_remote_untouched(self):
        from mahler.gh import GHError
        self.advance()
        with self.assertRaises(GHError):
            self.update(lambda: False)
        self.assertEqual(self.remote_head(), self.head)
        def concurrent_push():
            self.git('push', '-q', '--force', 'origin', 'release:topic')
            return True
        with self.assertRaises(GHError):
            self.update(concurrent_push)
        self.assertEqual(self.remote_head(), self.git('rev-parse', 'release'))

    def test_missing_target_and_changed_head_fail_closed(self):
        from mahler.gh import GHError
        with self.assertRaises(GHError):
            self.client.update_reviewed_branch(self.repo, 'topic', 'missing', self.head, lambda: True)
        with self.assertRaises(GHError):
            self.client.update_reviewed_branch(self.repo, 'topic', 'release', self.base, lambda: True)
        self.assertEqual(self.remote_head(), self.head)


class TestCommitPRLookup(unittest.TestCase):
    def test_pr_for_commit_finds_merged_pr(self):
        gh = GH("owner/repo")
        with patch.object(gh, "_gh", return_value=json.dumps([
                {"number": 10, "merged_at": None},
                {"number": 12, "merged_at": "2026-10-08T00:00:00Z"}])):
            self.assertEqual(gh.pr_for_commit("a" * 40), 12)

    def test_pr_for_commit_fallback_first_or_none(self):
        gh = GH("owner/repo")
        with patch.object(gh, "_gh", return_value=json.dumps([{"number": 15}])):
            self.assertEqual(gh.pr_for_commit("a" * 40), 15)
        with patch.object(gh, "_gh", return_value="[]"):
            self.assertIsNone(gh.pr_for_commit("a" * 40))
        from mahler.gh import GHError
        with patch.object(gh, "_gh", side_effect=GHError("offline")):
            self.assertIsNone(gh.pr_for_commit("a" * 40))
