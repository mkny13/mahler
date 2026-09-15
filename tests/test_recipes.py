"""Keep the sorter's planning safeguards in the rendered prompt (mahler#238)."""

import unittest

from mahler import prompt


class SortRecipeTests(unittest.TestCase):
    def setUp(self):
        self.values = dict(number=238, repo="example/project", title="Sort safeguards",
                           worktree="/tmp/example-worktree", rules="Sample project rules")
        self.rendered = prompt.render("sort", **self.values)
        self.text = " ".join(self.rendered.split())

    def test_preserves_relationships_and_planned_sections(self):
        self.assertIn("Keep existing `Part of #N` and `Depends on: #N` lines, "
                      "each on its own unquoted line", self.text)
        self.assertIn("never inside the original-request quote", self.text)
        self.assertIn("Quote only the owner's free-form words", self.text)
        self.assertIn("if the body already has this shape, edit its sections in place",
                      self.text)

    def test_sub_issues_require_something_to_ship(self):
        self.assertIn("Every sub-issue must end in a commit", self.text)
        self.assertIn("DONE with nothing to push counts as a failed attempt", self.text)
        self.assertIn("Do GitHub-only housekeeping or writing", self.text)
        self.assertIn("in this planning run, record each action in an issue comment, "
                      "and file no sub-issue for it", self.text)

    def test_template_values_and_status_contract(self):
        for name, value in self.values.items():
            with self.subTest(variable=name):
                self.assertNotIn(f"${name}", self.rendered)
                self.assertIn(str(value), self.rendered)
        self.assertNotIn("$", self.rendered)
        for status in ("READY", "SPLIT", "NEEDS-YOU"):
            with self.subTest(status=status):
                self.assertTrue(any(line == f"STATUS: {status}" or
                                    line.startswith(f"STATUS: {status} ")
                                    for line in self.rendered.splitlines()))


if __name__ == "__main__":
    unittest.main()
