import json
import unittest
from unittest.mock import patch, MagicMock
from mahler.gh import GH, pr_body, HELP_FOOTER, AGENT_NOTE

class TestGH(unittest.TestCase):
    def setUp(self):
        self.gh = GH("mkny13/mahler")
        self.gh._gh = MagicMock()

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
