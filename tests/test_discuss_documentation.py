"""Regression checks for the shipped D43 discussion documentation."""

from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class DiscussDocumentationTests(unittest.TestCase):
    def test_d43_describes_delivered_and_approved_feature(self):
        design = (REPO_ROOT / "DESIGN.md").read_text(encoding="utf-8")
        d43 = design.split("### D43 ", 1)[1].split("### D44 ", 1)[0]

        self.assertNotIn("not shipped execution", d43)
        self.assertNotIn("approval of the artifact commit revision", d43)
        self.assertIn("shipped", d43.lower())
        self.assertIn("approved", d43.lower())

    def test_roadmap_describes_delivered_and_approved_feature(self):
        roadmap = (REPO_ROOT / "ROADMAP.md").read_text(encoding="utf-8")
        section = roadmap.split("## Talk it through ", 1)[1].split("## Current state ", 1)[0]

        self.assertNotIn("approval of an exact revision remains pending", section)
        self.assertNotIn("does not ship a chat panel", section)
        self.assertIn("shipped", section.lower())
        self.assertIn("approved", section.lower())


if __name__ == "__main__":
    unittest.main()
