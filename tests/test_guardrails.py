"""Guardrails: destructive-command deny lists reach every argv that can run one.

mahler#77. CLAUDE_DENY must ride along on every claude-kind invocation
(claude, claude-opus, and any `from:` derived platform — they all share
claude_argv via argv_for), and Copilot's --deny-tool wiring mirrors the same
stems. agy/cline/codex/kilo have no deny-list flag; that gap is documented at
their argv builders and in DESIGN.md D12.
"""
import unittest

from mahler import platforms

REQUIRED_STEMS = [
    "git push --force", "git push -f", "git push --force-with-lease",
    "git push --delete", "git push -d", "git push --mirror",
    "git filter-branch", "git filter-repo", "git reset --hard",
    "git worktree remove",
    "gh repo delete", "gh repo archive", "gh release delete", "gh issue delete",
    "rm -rf /", "rm -rf ~", "rm -fr /", "rm -fr ~",
]


class TestGuardrails(unittest.TestCase):
    def test_deny_stems_cover_the_destructive_list(self):
        self.assertEqual(platforms.DENY_STEMS, REQUIRED_STEMS)

    def test_claude_and_copilot_lists_render_the_same_stems(self):
        self.assertEqual(platforms.CLAUDE_DENY,
                         [f"Bash({s}:*)" for s in platforms.DENY_STEMS])
        self.assertEqual(platforms.COPILOT_DENY,
                         [f"shell({s}:*)" for s in platforms.DENY_STEMS])

    def test_claude_argv_carries_the_full_deny_list(self):
        # claude and claude-opus (kind "claude") share claude_argv via argv_for.
        for pconf in ({"kind": "claude", "sort_model": "", "build_model": ""},
                      {"kind": "claude", "sort_model": "opus", "build_model": "opus"}):
            argv = platforms.argv_for(pconf, "prompt", "/tmp/wt", "build", 60)
            i = argv.index("--disallowedTools")
            self.assertEqual(argv[i + 1:i + 1 + len(platforms.CLAUDE_DENY)],
                             platforms.CLAUDE_DENY)

    def test_copilot_argv_denies_the_same_stems(self):
        argv = platforms.argv_for({"kind": "copilot", "model": ""},
                                  "prompt", "/tmp/wt", "build", 60)
        deny_values = [argv[i + 1] for i, a in enumerate(argv) if a == "--deny-tool"]
        self.assertEqual(deny_values, platforms.COPILOT_DENY)
        self.assertIn("--allow-all-tools", argv)  # non-interactive mode stays intact

    def test_flagless_platforms_still_build_argv(self):
        # The documented-gap platforms must keep building argv after any
        # guardrail change (they have nothing to wire, but must not break).
        for kind in ("agy", "cline", "codex", "kilo"):
            argv = platforms.argv_for({"kind": kind, "model": ""},
                                      "prompt", "/tmp/wt", "build", 60)
            self.assertTrue(argv)


if __name__ == "__main__":
    unittest.main()
