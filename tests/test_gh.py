import json
import unittest
from unittest.mock import patch, MagicMock
from mahler.gh import GH, pr_body, HELP_FOOTER, AGENT_NOTE

class TestGH(unittest.TestCase):
    def setUp(self):
        self.gh = GH("mkny13/mahler")
        self.gh._gh = MagicMock()

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

if __name__ == '__main__':
    unittest.main()
