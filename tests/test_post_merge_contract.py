"""The phish-in/couch-tour post-merge contract stays copy-pasteable."""
import re
import tomllib
from pathlib import Path
import unittest


DOC = Path(__file__).parents[1] / "docs" / "post-merge-validation.md"


class PostMergeContractDocumentationTests(unittest.TestCase):
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
