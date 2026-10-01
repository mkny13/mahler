"""Issue intake preserves project scope and caller-supplied routing labels."""

import contextlib
import copy
import io
import unittest
from unittest import mock

from mahler import cli, config


class AddCommandTests(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["projects"]["x"] = {"repo": "owner/repo", "scope": "all"}
        patcher = mock.patch("mahler.cli.GH")
        self.gh_class = patcher.start()
        self.addCleanup(patcher.stop)
        self.gh = self.gh_class.return_value
        self.gh.create_issue.return_value = "https://github.com/owner/repo/issues/1"

    def run_add(self, *options):
        out, err = io.StringIO(), io.StringIO()
        with (mock.patch.object(config, "load", return_value=self.cfg),
              mock.patch.object(config, "DB_PATH", ":memory:"),
              contextlib.redirect_stdout(out), contextlib.redirect_stderr(err)):
            rc = cli.main(["add", "x", "A new issue", *options])
        return rc, out.getvalue(), err.getvalue()

    def test_unscoped_project_without_labels(self):
        rc, out, err = self.run_add()
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        self.assertEqual(out.strip(), self.gh.create_issue.return_value)
        self.gh.create_issue.assert_called_once_with("A new issue", "", [])

    def test_scoped_project_without_user_labels(self):
        self.cfg["projects"]["x"]["scope"] = "label"
        rc, _, _ = self.run_add()
        self.assertEqual(rc, 0)
        self.gh.create_issue.assert_called_once_with("A new issue", "", ["mahler"])

    def test_custom_scope_label_precedes_user_labels(self):
        self.cfg["projects"]["x"].update(scope="label", scope_label="managed")
        rc, _, _ = self.run_add("--label", "type:goal", "--label", "size:m")
        self.assertEqual(rc, 0)
        self.gh.create_issue.assert_called_once_with(
            "A new issue", "", ["managed", "type:goal", "size:m"])

    def test_unscoped_project_forwards_labels_in_order_and_body_unchanged(self):
        body = "The goal\n\nMore detail."
        rc, _, _ = self.run_add("--label", "type:goal", "--body", body,
                                "--label", "size:m", "--label", "p2")
        self.assertEqual(rc, 0)
        self.gh.create_issue.assert_called_once_with(
            "A new issue", body, ["type:goal", "size:m", "p2"])

    def test_reserved_labels_are_rejected_before_github(self):
        for label in ("mahler:ready", "mahler:working", "mahler:future-state"):
            with self.subTest(label=label):
                rc, _, err = self.run_add("--label", "type:goal", "--label", label)
                self.assertNotEqual(rc, 0)
                self.assertIn(label, err)
                self.assertIn("reserved", err)
                self.gh_class.assert_not_called()
                self.gh.create_issue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
