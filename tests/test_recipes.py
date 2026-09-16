"""Keep the sorter's planning safeguards in the rendered prompt (mahler#238)."""

import unittest

from mahler import prompt, config


class SortRecipeTests(unittest.TestCase):
    def setUp(self):
        self.values = dict(number=238, repo="example/project", title="Sort safeguards",
                           worktree="/tmp/example-worktree", rules="Sample project rules",
                           sizing="")
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
                # We skip checking $sizing since it is explicitly empty for self.rendered
                if name == "sizing":
                    continue
                self.assertNotIn(f"${name}", self.rendered)
                self.assertIn(str(value), self.rendered)
        self.assertNotIn("$", self.rendered)
        for status in ("READY", "SPLIT", "NEEDS-YOU"):
            with self.subTest(status=status):
                self.assertTrue(any(line == f"STATUS: {status}" or
                                    line.startswith(f"STATUS: {status} ")
                                    for line in self.rendered.splitlines()))


class SizeTargetTests(unittest.TestCase):
    def test_size_target_of(self):
        # returns "s" for account = "work"
        self.assertEqual(config.size_target_of({"account": "work"}), "s")
        # "" for default personal project
        self.assertEqual(config.size_target_of({}), "")
        self.assertEqual(config.size_target_of({"account": "personal"}), "")
        # "" for accounts = ["personal", "work"]
        self.assertEqual(config.size_target_of({"accounts": ["personal", "work"]}), "")
        # explicit value when one is set
        self.assertEqual(config.size_target_of({"account": "personal", "size_target": "s"}), "s")
        self.assertEqual(config.size_target_of({"account": "work", "size_target": ""}), "")
        self.assertEqual(config.size_target_of({"account": "work", "size_target": "invalid"}), "")

    def test_prompts(self):
        class DummyCtx:
            def policy(self, proj):
                if proj == "work-proj":
                    return {"repo": "a/b", "rules": "", "account": "work"}
                return {"repo": "a/b", "rules": ""}
        ctx = DummyCtx()
        item = {"number": 1, "title": "T", "pr": 2}
        prep = {"worktree": "/tmp", "branch": "b", "replayed": False, "kept": None}
        
        work_sort = prompt.build(ctx, "work-proj", item, "sort", "claude", prep)
        pers_sort = prompt.build(ctx, "pers-proj", item, "sort", "claude", prep)
        
        self.assertIn("Sizing for this project (from Mahler's config)", work_sort)
        self.assertNotIn("Sizing for this project", pers_sort)
        self.assertNotIn("$sizing", work_sort)
        self.assertNotIn("$sizing", pers_sort)
        
        work_build = prompt.build(ctx, "work-proj", item, "build", "claude", prep)
        pers_build = prompt.build(ctx, "pers-proj", item, "build", "claude", prep)
        
        self.assertNotIn("Sizing for this project", work_build)
        self.assertNotIn("Sizing for this project", pers_build)


if __name__ == "__main__":
    unittest.main()
