"""Hermetic guard for commands documented in DESIGN.md."""

from pathlib import Path
import re
import shlex
import subprocess
import unittest

from tests.verify_hermes_contract import EXPECTED_OUTPUT, PINNED_COMMIT

REPO_ROOT = Path(__file__).resolve().parent.parent
DESIGN_PATH = REPO_ROOT / "DESIGN.md"
SHELL_LANGS = {"", "bash", "sh", "shell", "console"}


def extract_python_script_paths(markdown_text: str) -> list[str]:
    """Extract script paths executed via `python3 <path>` in shell code blocks."""
    blocks = re.findall(r"```([a-zA-Z0-9_-]*)\n(.*?)```", markdown_text, re.DOTALL)
    paths = []
    for lang, code in blocks:
        if lang.strip().lower() not in SHELL_LANGS:
            continue
        for line in code.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                tokens = shlex.split(line)
            except ValueError:
                tokens = line.split()
            for i, token in enumerate(tokens):
                if token in ("python3", "python"):
                    for arg in tokens[i + 1 :]:
                        if arg.startswith("-"):
                            continue
                        if "/" in arg or arg.endswith(".py"):
                            paths.append(arg)
                        break
    return paths


def extract_hermes_verification_block(markdown_text: str) -> tuple[str, list[str]]:
    """Extract the pinned commit SHA and sanitized output lines from DESIGN.md."""
    match_section = re.search(
        r"\*\*Executable pinned-source verification:\*\*.*?(?=\n\n\*\*|\Z)",
        markdown_text,
        re.DOTALL,
    )
    if not match_section:
        raise ValueError("Executable pinned-source verification section not found in DESIGN.md")
    section_text = match_section.group(0)

    commit_match = re.search(r"commit\s+`?([0-9a-f]{40})`?", section_text)
    commit_sha = commit_match.group(1) if commit_match else ""

    output_match = re.search(
        r"Sanitized output:\s*```(?:text)?\n(.*?)```", section_text, re.DOTALL
    )
    if not output_match:
        raise ValueError("Sanitized output block not found in DESIGN.md")
    output_lines = [
        line.strip()
        for line in output_match.group(1).splitlines()
        if line.strip()
    ]
    return commit_sha, output_lines


class DesignCommandsTests(unittest.TestCase):
    def test_documented_python_commands_exist_and_not_ignored(self):
        content = DESIGN_PATH.read_text(encoding="utf-8")
        paths = extract_python_script_paths(content)
        self.assertTrue(len(paths) > 0, "Expected at least one python3 command in DESIGN.md")
        for rel_path_str in paths:
            path = REPO_ROOT / rel_path_str
            self.assertTrue(
                path.is_file(),
                f"Documented command path does not exist in repo: {rel_path_str}",
            )
            res = subprocess.run(
                ["git", "check-ignore", "-q", rel_path_str],
                cwd=REPO_ROOT,
            )
            self.assertNotEqual(
                res.returncode,
                0,
                f"Documented command path is gitignored: {rel_path_str}",
            )

    def test_sanitized_output_matches_hermes_contract(self):
        content = DESIGN_PATH.read_text(encoding="utf-8")
        _, output_lines = extract_hermes_verification_block(content)
        self.assertEqual(output_lines, list(EXPECTED_OUTPUT))

    def test_pinned_commit_matches_hermes_contract(self):
        content = DESIGN_PATH.read_text(encoding="utf-8")
        commit_sha, _ = extract_hermes_verification_block(content)
        self.assertEqual(commit_sha, PINNED_COMMIT)
