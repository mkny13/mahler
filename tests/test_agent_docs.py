"""Contract test for instruction-file identity, commands, links, and escalation categories."""

from pathlib import Path
import re
import unittest

from mahler.finalize import OWNER_CATEGORIES
from mahler.gh import parse_command

REPO_ROOT = Path(__file__).resolve().parent.parent

NAMED_DOCS = [
    REPO_ROOT / "AGENTS.md",
    REPO_ROOT / "CLAUDE.md",
    REPO_ROOT / "docs" / "commands.md",
    REPO_ROOT / "docs" / "agent-best-practices.md",
]


class AgentDocsContractTests(unittest.TestCase):
    def test_instruction_files_are_byte_identical_and_within_budget(self):
        agents_path = REPO_ROOT / "AGENTS.md"
        claude_path = REPO_ROOT / "CLAUDE.md"
        self.assertTrue(agents_path.is_file(), "AGENTS.md must exist")
        self.assertTrue(claude_path.is_file(), "CLAUDE.md must exist")

        agents_bytes = agents_path.read_bytes()
        claude_bytes = claude_path.read_bytes()
        self.assertEqual(
            agents_bytes,
            claude_bytes,
            "AGENTS.md and CLAUDE.md must be byte-identical for tool compatibility",
        )

        line_count = len(agents_bytes.decode("utf-8").splitlines())
        self.assertLessEqual(
            line_count,
            150,
            f"Instruction files must not exceed 150 lines (got {line_count})",
        )

    def test_instruction_files_state_all_required_boundaries(self):
        raw_content = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        content = " ".join(raw_content.split())

        # 1. Autonomous-run boundary
        self.assertIn("MAHLER_ISSUE", content)
        self.assertIn("implement, verify and commit locally, then end with a STATUS line", content)
        self.assertIn("admitted issue context", content)

        # 2. Conductor-owned shipping boundary
        self.assertIn("agents build, the conductor ships", content)
        self.assertIn("snapshots and pushes the branch, opens the PR, watches CI", content)

        # 3. Interactive-session & by-hand handoff boundary
        self.assertIn("docs/by-hand.md", content)
        self.assertIn("mahler claim", content)
        self.assertIn("mahler heartbeat", content)
        self.assertIn("mahler ship", content)
        self.assertIn("mahler release", content)
        self.assertIn("Fixes #N", content)
        self.assertIn("shared primary checkout", content)

        # 4. Desktop-gate boundary
        self.assertIn("mahler desktop run -- <command>", content)
        self.assertIn("GATE-DENIED", content)
        self.assertIn("D40", content)
        self.assertIn("D50", content)

        # 5. Verification boundary
        for cmd in (
            "python3 -m mahler.repo_guard",
            "python3 -m unittest discover -s tests",
            "python3 tests/run_random.py",
            "python3 -X dev tests/run_strict.py",
        ):
            self.assertIn(cmd, content)

        # 6. Test-isolation boundary
        self.assertIn("Tests must be fully isolated", content)
        self.assertIn("Ledger(':memory:')", content)

        # 7. Lessons boundary
        self.assertIn(".agents/skills/lesson-*/SKILL.md", content)
        self.assertIn("D44", content)

        # 8. Five owner-only escalation categories
        for category in OWNER_CATEGORIES:
            self.assertIn(f"`{category}`", content)

    def test_commands_doc_describes_six_forms_and_plain_answers_matching_parser(self):
        # Mechanical parser checks
        self.assertEqual(parse_command("/mahler go"), ("go", None))
        self.assertEqual(parse_command("/mahler park"), ("park", None))
        self.assertEqual(parse_command("/mahler stop"), ("stop", None))
        self.assertEqual(parse_command("/mahler platform agy-gemini"), ("platform", "agy-gemini"))
        self.assertEqual(parse_command("/mahler platform auto"), ("platform", "auto"))
        self.assertEqual(parse_command("/mahler platform none"), ("platform", "none"))
        self.assertIsNone(parse_command("Plain answer without command"))

        commands_text = (REPO_ROOT / "docs" / "commands.md").read_text(encoding="utf-8")

        # Six comment forms documented
        for form in (
            "`/mahler go`",
            "`/mahler park`",
            "`/mahler stop`",
            "`/mahler platform <name>`",
            "`/mahler platform auto`",
            "`/mahler platform none`",
        ):
            self.assertIn(form, commands_text)

        # Effects matching current implementation
        self.assertIn("`ready`", commands_text)
        self.assertIn("`verifying`", commands_text)
        self.assertIn("`parked`", commands_text)
        self.assertIn("`platform:<name>`", commands_text)
        self.assertIn("`inbox`", commands_text)
        self.assertIn("`failed`", commands_text)
        self.assertIn("merge with follow-ups", commands_text)

    def test_repository_relative_links_in_named_docs_exist(self):
        link_pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
        for doc in NAMED_DOCS:
            self.assertTrue(doc.is_file(), f"{doc} must exist")
            text = doc.read_text(encoding="utf-8")
            matches = link_pattern.findall(text)
            relative_links = [
                target for _, target in matches
                if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", target)
                and not target.startswith("mailto:")
            ]
            self.assertTrue(
                len(relative_links) > 0,
                f"Expected repository-relative links in {doc.relative_to(REPO_ROOT)}",
            )
            for target in relative_links:
                # Strip anchor fragment
                file_target = target.split("#")[0].strip()
                if not file_target:
                    continue  # Anchor in the same file
                resolved = (doc.parent / file_target).resolve()
                self.assertTrue(
                    resolved.exists(),
                    f"Broken repository link in {doc.relative_to(REPO_ROOT)}: {target} -> {resolved}",
                )

    def test_agent_best_practices_adoption_claims_and_line_count(self):
        text = (REPO_ROOT / "docs" / "agent-best-practices.md").read_text(encoding="utf-8")

        # Stale line count must not appear
        self.assertNotIn("174 lines", text)

        # Adoption candidates 1, 3, 5, 6 adopted; 2 and 4 remaining
        self.assertIn("Adopted in mahler#1741", text)
        self.assertIn("Adopted in mahler#1747", text)
        self.assertIn("Adopted in mahler#1750", text)
        self.assertIn("Adopted in mahler#1746", text)

        # Escalation categories documented in rule 13
        for category in OWNER_CATEGORIES:
            self.assertIn(category.replace("-", " "), text.lower())


if __name__ == "__main__":
    unittest.main()
