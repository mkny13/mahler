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

if __name__ == '__main__':
    unittest.main()
