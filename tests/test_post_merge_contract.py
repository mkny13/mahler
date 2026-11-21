"""The phish-in/couch-tour post-merge contract stays copy-pasteable."""
import re
import tomllib
from pathlib import Path
import unittest


DOC = Path(__file__).parents[1] / "docs" / "post-merge-validation.md"


class PostMergeContractDocumentationTests(unittest.TestCase):
    def test_parent_740_evidence_records_ordering_and_exactly_one_project(self):
        text = DOC.read_text()
        self.assertIn("## Parent #740 evidence (#1605)", text)
        self.assertIn("#748 merge `c70a0d88fe4dbbc5e93f573d995244357009bc40`", text)
        self.assertIn("after #741 audit commit `b6ae20e1fccaf91973c1b049552f4d74371deb51`", text)
        self.assertIn("Mahler: PASS", text)
        self.assertRegex(text, r"Groundwork: PASS \(target:\s+Production\)")
        self.assertIn("exactly one of Groundwork/Couch Tour", text)

    def test_phish_in_snippet_is_valid_toml_and_bounded(self):
        text = DOC.read_text()
        match = re.search(r"```toml\n(?P<snippet>.*?)\n```", text, re.DOTALL)
        self.assertIsNotNone(match, "documentation must contain a TOML operator snippet")
        uncommented = "\n".join(
            line[2:] if line.startswith("# ") else line
            for line in match.group("snippet").splitlines()
        )
        parsed = tomllib.loads(uncommented)
        contract = parsed["projects"]["phish-in"]["post_merge"]
        self.assertFalse(contract["enabled"])
        self.assertEqual(contract["live_strategy"], "github")
        self.assertNotIn(contract["environment"].casefold(), {"prod", "production"})
        self.assertGreaterEqual(contract["timeout_seconds"], 1)
        self.assertLessEqual(contract["timeout_seconds"], 86400)
        self.assertIn("couch-tour#357", text)
        self.assertIn("#748", text)
        self.assertIn("#1038", text)


if __name__ == "__main__":
    unittest.main()
