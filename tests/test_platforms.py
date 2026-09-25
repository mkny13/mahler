"""Tests for platform adapters and resume commands (mahler#426)."""

import json
import os
import tempfile
import unittest

from mahler import platforms


class NetworkErrorDetectionTests(unittest.TestCase):
    def test_matches_network_patterns_case_insensitively(self):
        patterns = [
            "Network connection lost",
            "The socket connection was closed unexpectedly",
            "The operation timed out",
            "Session not found",
            "Error: session not found: abc123",
            "NETWORK CONNECTION LOST",
            "The Socket Connection Was Closed Unexpectedly",
        ]
        for p in patterns:
            with self.subTest(pattern=p):
                self.assertTrue(platforms.is_network_error(p))

    def test_non_network_errors_return_false(self):
        non_network = [
            "429 rate limit exceeded",
            "Daily free limit reached. Try again in 2h",
            "Add credits to continue",
            "SyntaxError: invalid syntax",
            "Build failed",
            "",
            None,
        ]
        for p in non_network:
            with self.subTest(pattern=p):
                self.assertFalse(platforms.is_network_error(p))


class ReadLogResumeInfoTests(unittest.TestCase):
    def test_kilo_log_extracts_session_id_and_last_error(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = os.path.join(d, "kilo.log")
            with open(log_path, "w") as fh:
                fh.write(json.dumps({"type": "step_start", "sessionID": "ses_abc123",
                                     "part": {"type": "step-start"}}) + "\n")
                fh.write(json.dumps({"type": "error", "sessionID": "ses_abc123",
                                     "error": {"message": "Network connection lost"}}) + "\n")
            res = platforms.read_log(log_path, "kilo")
            self.assertEqual(res["session_id"], "ses_abc123")
            self.assertEqual(res["last_error"], "Network connection lost")
            self.assertTrue(platforms.is_network_error(res["last_error"]))

    def test_cline_log_extracts_last_error_from_error_event(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = os.path.join(d, "cline.log")
            with open(log_path, "w") as fh:
                fh.write(json.dumps({"type": "error",
                                     "message": "The socket connection was closed unexpectedly"}) + "\n")
            res = platforms.read_log(log_path, "cline")
            self.assertEqual(res["last_error"], "The socket connection was closed unexpectedly")
            self.assertTrue(platforms.is_network_error(res["last_error"]))

    def test_cline_log_extracts_last_error_from_run_result(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = os.path.join(d, "cline.log")
            with open(log_path, "w") as fh:
                fh.write(json.dumps({"type": "run_result", "finishReason": "error",
                                     "text": "The operation timed out"}) + "\n")
            res = platforms.read_log(log_path, "cline")
            self.assertEqual(res["last_error"], "The operation timed out")
            self.assertTrue(platforms.is_network_error(res["last_error"]))

    def test_raw_text_session_not_found_is_captured(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = os.path.join(d, "kilo.log")
            with open(log_path, "w") as fh:
                fh.write("Error: Session not found\n")
            res = platforms.read_log(log_path, "kilo")
            self.assertEqual(res["last_error"], "Error: Session not found")
            self.assertTrue(platforms.is_network_error(res["last_error"]))


class ModelUnavailableDetectionTests(unittest.TestCase):
    """Issue #420: a CLI rejecting the configured model is a permanent
    problem, distinct from a quota hit, matched across every kind's real
    error shape."""

    def test_matches_and_does_not_confuse_with_quota(self):
        for text in ("Model not found: bogus-model", "unsupported model",
                     "Error: invalid model 'foo'", "MODEL_NOT_FOUND",
                     "the API does not support model gpt-1",
                     "Model 'foo' not found", "Model 'foo' is not supported",
                     'Model "org/foo-1" is not allowed',
                     "Model foo is not enabled"):
            with self.subTest(text=text):
                self.assertTrue(platforms.is_model_unavailable(text))
        for text in ("429 rate limit exceeded", "Add credits to continue",
                     "Daily free limit reached. Try again in 2h",
                     "Model foo loaded but file not found",
                     "Model foo is supported", "", None):
            with self.subTest(text=text):
                self.assertFalse(platforms.is_model_unavailable(text))

    def _log(self, d, *lines):
        path = os.path.join(d, "agent.log")
        with open(path, "w") as fh:
            for line in lines:
                fh.write((json.dumps(line) if isinstance(line, dict) else line) + "\n")
        return path

    def test_claude_result_is_error_with_model_text(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._log(d, {"type": "result", "subtype": "error",
                                 "is_error": True, "result": "model not found: bogus"})
            res = platforms.read_log(path, "claude")
            self.assertTrue(res["model_unavailable"])
            self.assertFalse(res["quota_hit"])

    def test_codex_turn_failed(self):
        for message in ("unsupported model gpt-1", "Model 'foo' not found",
                        "Model 'foo' is not supported",
                        'Model "org/foo-1" is not allowed'):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as d:
                path = self._log(d, {"type": "turn.failed",
                                     "error": {"message": message}})
                res = platforms.read_log(path, "codex")
                self.assertTrue(res["model_unavailable"])
                self.assertFalse(res["quota_hit"])

    def test_copilot_error_event(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._log(d, {"type": "error", "message": "invalid model"})
            res = platforms.read_log(path, "copilot")
            self.assertTrue(res["model_unavailable"])

    def test_cline_run_result_not_ok(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._log(d, {"type": "run_result", "finishReason": "error",
                                 "text": "model not found"})
            res = platforms.read_log(path, "cline")
            self.assertTrue(res["model_unavailable"])

    def test_kilo_error_event(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._log(d, {"type": "error", "error": {"message": "model not allowed"}})
            res = platforms.read_log(path, "kilo")
            self.assertTrue(res["model_unavailable"])

    def test_agy_result_not_success(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._log(d, {"event": "result",
                                 "result": {"status": "FAILED", "response": "unknown model"}})
            res = platforms.read_log(path, "agy")
            self.assertTrue(res["model_unavailable"])

    def test_plain_text_requires_an_error_prefix(self):
        for text, expected in (
            ("STATUS: DONE Fixed invalid model handling", False),
            ("Tests cover model not found errors", False),
            ("Error: invalid model bogus", True),
        ):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as d:
                res = platforms.read_log(self._log(d, text), "cline")
                self.assertEqual(res["model_unavailable"], expected)

    def test_quota_error_is_not_flagged_as_model_unavailable(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._log(d, {"type": "error", "error": {"message": "429 rate limit exceeded"}})
            res = platforms.read_log(path, "cline")
            self.assertFalse(res["model_unavailable"])
            self.assertTrue(res["quota_hit"])


class ResumeArgvTests(unittest.TestCase):
    def test_cline_resume_argv_omits_id_and_uses_fresh_run(self):
        # Verified with Cline 3.0.64 (mahler#426):
        # cline --id in --json mode always fails; fresh run in worktree is the working form.
        pconf = {"kind": "cline", "model": "my-model"}
        prompt = "Resume instructions"
        argv = platforms.cline_resume_argv(pconf, prompt, "/path/to/wt", "build", timeout_minutes=30)
        self.assertNotIn("--id", argv)
        self.assertIn("--cwd", argv)
        self.assertEqual(argv[argv.index("--cwd") + 1], "/path/to/wt")
        self.assertIn("--json", argv)
        self.assertIn("--auto-approve", argv)
        self.assertIn("-t", argv)
        self.assertEqual(argv[argv.index("-t") + 1], "1800")
        self.assertIn("-m", argv)
        self.assertEqual(argv[argv.index("-m") + 1], "my-model")
        self.assertEqual(argv[-1], prompt)

    def test_kilo_resume_argv_with_session(self):
        # Verified with Kilo 7.6.2 (mahler#426):
        # kilo run [prompt] --session <id> --dir <wt> --auto --format json
        pconf = {"kind": "kilo", "model": "kilo/free"}
        prompt = "Resume instructions"
        argv = platforms.kilo_resume_argv(pconf, prompt, "/path/to/wt", "build",
                                          timeout_minutes=60, session_id="ses_abc123")
        self.assertEqual(argv[:2], [platforms.kilo_exe(), "run"])
        self.assertIn(prompt, argv)
        self.assertIn("--session", argv)
        self.assertEqual(argv[argv.index("--session") + 1], "ses_abc123")
        self.assertIn("--dir", argv)
        self.assertEqual(argv[argv.index("--dir") + 1], "/path/to/wt")
        self.assertIn("--auto", argv)
        self.assertIn("--format", argv)
        self.assertEqual(argv[argv.index("--format") + 1], "json")
        self.assertIn("-m", argv)
        self.assertEqual(argv[argv.index("-m") + 1], "kilo/free")

    def test_kilo_resume_argv_without_session_is_fresh_run_fallback(self):
        # When session_id is None (or "session not found" occurred), kilo falls back
        # to a fresh run without --session in the same worktree.
        pconf = {"kind": "kilo"}
        prompt = "Resume instructions"
        argv = platforms.kilo_resume_argv(pconf, prompt, "/path/to/wt", "build",
                                          timeout_minutes=60, session_id=None)
        self.assertEqual(argv[:2], [platforms.kilo_exe(), "run"])
        self.assertIn(prompt, argv)
        self.assertNotIn("--session", argv)
        self.assertIn("--dir", argv)
        self.assertEqual(argv[argv.index("--dir") + 1], "/path/to/wt")
        self.assertIn("--auto", argv)
        self.assertIn("--format", argv)
        self.assertEqual(argv[argv.index("--format") + 1], "json")

    def test_resume_argv_for_dispatch(self):
        cline_conf = {"kind": "cline"}
        kilo_conf = {"kind": "kilo"}
        self.assertEqual(platforms.resume_argv_for(cline_conf, "p", "/wt", "build", 60),
                         platforms.cline_resume_argv(cline_conf, "p", "/wt", "build", 60))
        self.assertEqual(platforms.resume_argv_for(kilo_conf, "p", "/wt", "build", 60, session_id="s1"),
                         platforms.kilo_resume_argv(kilo_conf, "p", "/wt", "build", 60, session_id="s1"))
        with self.assertRaises(ValueError):
            platforms.resume_argv_for({"kind": "claude"}, "p", "/wt", "build", 60)


if __name__ == "__main__":
    unittest.main()
