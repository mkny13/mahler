"""Keep the sorter's planning safeguards in the rendered prompt (mahler#238)."""

import sqlite3
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


    def test_build_accepts_a_sqlite_row(self):
        # The scheduler hands prompt.build a sqlite3.Row, which has no .get
        # (mahler#409: every launch failed for ~18h while this test used dicts).
        class DummyCtx:
            def policy(self, proj):
                return {"repo": "a/b", "rules": ""}
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        prep = {"worktree": "/tmp", "branch": "b", "replayed": False, "kept": None}
        for pr in (None, 7):
            item = con.execute("SELECT 1 AS number, 'T' AS title, ? AS pr", (pr,)).fetchone()
            for role in ("sort", "build"):
                with self.subTest(pr=pr, role=role):
                    self.assertIn("a/b", prompt.build(DummyCtx(), "p", item, role, "claude", prep))
        con.close()

class BuildRecipeTests(unittest.TestCase):
    def setUp(self):
        self.values = dict(
            number=358,
            repo="mkny13/example",
            title="Define What's New contract",
            platform="agy-gemini",
            worktree="/tmp/worktree",
            branch="mahler/358-whats-new",
            handoff="",
            verify="python3 -m unittest",
            base="main",
            rules="",
        )
        self.rendered = prompt.render("build", **self.values)
        self.text = " ".join(self.rendered.split())

    def test_whats_new_contract_guidance_present(self):
        # Explicit trigger and scoping rule
        self.assertIn("When (and only when) the issue explicitly asks for an in-app What's New surface or release feed", self.text)
        self.assertIn("Do not add What's New UI or feed consumption to tasks that do not explicitly request it", self.text)

        # Versioning and schema
        self.assertIn("Follow DESIGN D31's schema v1 JSON contract", self.text)
        self.assertIn("schema_version", self.text)
        self.assertIn("SemVer-based", self.text)
        self.assertIn("store the highest acknowledged version, show newer releases", self.text)

        # Read-only transport and local acknowledgement
        self.assertIn("Keep transport strictly read-only: apps consume the feed; they never publish releases or write read/acknowledgement state back to Mahler", self.text)
        self.assertIn("Client acknowledgement is local to each app installation", self.text)
        self.assertIn("marked read only after the user views or dismisses the surface", self.text)

        # Fault tolerance: must not block startup
        self.assertIn("Missing, unreachable, or malformed feed responses must degrade gracefully and never block app startup", self.text)

        # Native design conventions
        self.assertIn("Preserve the app's native design conventions", self.text)

        # Maintenance visibility collapsed/hidden by default
        self.assertIn("Hide maintenance details initially: render features and fixes prominently; keep maintenance collapsed or secondary", self.text)

        # Operational privacy boundaries
        self.assertIn("Exclude operational data: the feed provides release metadata only; never consume or display issue comments, run logs, credentials, or UAT notes", self.text)

        # Automated testing requirements
        self.assertIn("Add automated tests in the app covering JSON payload parsing, SemVer comparison, offline fallback, and local acknowledgement read-state persistence", self.text)

    def test_unrelated_status_output_contract_unaffected(self):
        # The STATUS line contract must remain exact and untampered
        status_lines = [line.strip() for line in self.rendered.splitlines() if line.startswith("STATUS:")]
        self.assertEqual(status_lines, [
            "STATUS: DONE <one-line summary of what changed>",
            "STATUS: NEEDS-YOU <the question, on one line> [OPTIONS: <choice> | <choice>]",
            "STATUS: BLOCKED <reason>",
            "STATUS: YIELDED <handoff summary>",
        ])
        for line in status_lines:
            self.assertNotIn("What's New", line)
            self.assertNotIn("release", line)

    def test_template_values_substituted_without_leftover_variables(self):
        for k, v in self.values.items():
            if k in ("handoff", "rules"):
                continue
            with self.subTest(variable=k):
                self.assertNotIn(f"${k}", self.rendered)
                self.assertIn(str(v), self.rendered)
        self.assertNotIn("$", self.rendered)


if __name__ == "__main__":
    unittest.main()

