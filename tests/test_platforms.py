"""Tests for platform adapters and resume commands (mahler#426)."""

import copy
import json
import os
import tempfile
import unittest
from unittest import mock

from mahler import config, platforms, router


def provider_error_events(message):
    """Synthetic failures in the adapters' established stream event shapes."""
    return (
        ('claude', {'type': 'result', 'subtype': 'error_during_execution',
                    'is_error': True, 'errors': [message]}),
        ('claude', {'type': 'error', 'error': {'message': message}}),
        ('cline', {'type': 'run_result', 'finishReason': 'error', 'text': message}),
        ('copilot', {'type': 'session.error', 'data': {'message': message}}),
        ('codex', {'type': 'turn.failed', 'error': {'message': message}}),
        ('kilo', {'type': 'error', 'error': {'data': {'message': message}}}),
        ('agy', {'event': 'result', 'result': {'status': 'FAILED', 'response': message}}),
        ('kiro', {'type': 'runError', 'data': {'message': message}}),
        ('vibe', {'type': 'error', 'message': message}),
    )


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


class CreditExhaustionTests(unittest.TestCase):
    def test_credit_phrases_are_distinct_from_rate_limits(self):
        for text in ("Insufficient balance. Your Cline Credits balance is $-0.07",
                     "Add credits to continue, or switch to a free model",
                     "credits balance is $-0.01"):
            with self.subTest(text=text):
                self.assertTrue(platforms.is_credit_exhausted(text))
        self.assertFalse(platforms.is_credit_exhausted("429 rate limit exceeded"))

    def test_logs_classify_cline_and_kilo_credit_failures(self):
        cases = (
            ("cline", {"type": "error", "message": "Insufficient balance. Your Cline Credits balance is $-0.07"}),
            ("kilo", {"type": "error", "error": {"data": {"statusCode": 402,
                    "message": "Add credits to continue"}}}),
        )
        for kind, event in cases:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "agent.log")
                with open(path, "w") as fh:
                    fh.write(json.dumps(event) + "\n")
                result = platforms.read_log(path, kind)
                self.assertTrue(result["credit_exhausted"])
                self.assertTrue(result["quota_hit"])

    def test_rate_limit_stays_quota(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "agent.log")
            with open(path, "w") as fh:
                fh.write(json.dumps({"type": "error", "message": "429 rate limit exceeded"}) + "\n")
            result = platforms.read_log(path, "cline")
            self.assertFalse(result["credit_exhausted"])
            self.assertTrue(result["quota_hit"])

    def test_status_line_strips_markdown_emphasis(self):
        self.assertEqual(platforms.status_line("STATUS: **DONE** all set"),
                         ("DONE", "all set"))
        self.assertEqual(platforms.status_line("STATUS: MERGED by mistake"),
                         ("MERGED", "by mistake"))


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
                     "Model foo is not enabled",
                     "Error: Model 'claude-opus-4.8' is not available."):
            with self.subTest(text=text):
                self.assertTrue(platforms.is_model_unavailable(text))
        for text in ("429 rate limit exceeded", "Add credits to continue",
                     "Daily free limit reached. Try again in 2h",
                     "Model foo loaded but file not found",
                     "Model foo is supported",
                     "Service temporarily not available", "", None):
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

    def test_resume_argv_for_supported_platforms(self):
        cline_conf = {"kind": "cline"}
        kilo_conf = {"kind": "kilo"}
        self.assertEqual(platforms.resume_argv_for(cline_conf, "p", "/wt", "build", 60),
                         platforms.cline_resume_argv(cline_conf, "p", "/wt", "build", 60))
        self.assertEqual(platforms.resume_argv_for(kilo_conf, "p", "/wt", "build", 60, session_id="s1"),
                         platforms.kilo_resume_argv(kilo_conf, "p", "/wt", "build", 60, session_id="s1"))
        with self.assertRaises(ValueError):
            platforms.resume_argv_for({"kind": "claude"}, "p", "/wt", "build", 60)


class ClineModelPinTests(unittest.TestCase):
    def test_default_argv_pins_the_free_model(self):
        argv = platforms.cline_argv(config.DEFAULTS["platforms"]["cline-free"],
                                    "prompt", "/wt", "build")
        self.assertEqual(argv[argv.index("-m") + 1], "z-ai/glm-5.3-flash")

    def test_config_override_wins(self):
        pconf = config._merge(config.DEFAULTS["platforms"]["cline-free"],
                              {"model": "configured/model"})
        argv = platforms.cline_argv(pconf, "prompt", "/wt", "build")
        self.assertEqual(argv[argv.index("-m") + 1], "configured/model")


class KiroArgvTests(unittest.TestCase):
    def test_argv_basic(self):
        with mock.patch.object(platforms, "kiro_exe", return_value="/app/kiro-cli"):
            argv = platforms.kiro_argv(
                {"kind": "kiro", "model": ""}, "do it", "/tmp/wt", "build")
        self.assertEqual(argv[:2], ["/app/kiro-cli", "chat"])
        self.assertIn("do it", argv)
        self.assertIn("--output-format", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertIn("--no-interactive", argv)
        self.assertIn("--trust-tools", argv)
        self.assertEqual(argv[argv.index("--trust-tools") + 1],
                         platforms.KIRO_TRUST_TOOLS)

    def test_argv_no_trust_tools_no_shell(self):
        # The allow-list must not include 'shell' (mahler#77 guardrail).
        self.assertNotIn("shell", platforms.KIRO_TRUST_TOOLS)

    def test_argv_with_model(self):
        with mock.patch.object(platforms, "kiro_exe", return_value="/app/kiro-cli"):
            argv = platforms.kiro_argv(
                {"kind": "kiro", "model": "claude-sonnet-4.5"}, "prompt", "/wt", "build")
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-4.5")

    def test_argv_with_effort(self):
        with mock.patch.object(platforms, "kiro_exe", return_value="/app/kiro-cli"):
            argv = platforms.kiro_argv(
                {"kind": "kiro", "effort": "high"}, "prompt", "/wt", "build")
        idx = argv.index("--effort")
        self.assertEqual(argv[idx + 1], "high")

    def test_argv_no_effort_flag_when_unset(self):
        with mock.patch.object(platforms, "kiro_exe", return_value="/app/kiro-cli"):
            argv = platforms.kiro_argv({"kind": "kiro"}, "prompt", "/wt", "build")
        self.assertNotIn("--effort", argv)

    def test_argv_disables_unwanted_tools(self):
        pconf = {"kind": "kiro", "model": "auto"}
        argv = platforms.kiro_argv(pconf, "prompt", "/wt", "build")
        # Should not allow all tools.
        self.assertNotIn("--trust-all-tools", argv)
        self.assertNotIn("-a", argv)


class ClineProviderTests(unittest.TestCase):
    def test_provider_is_passed_to_cline(self):
        pconf = config.DEFAULTS["platforms"]["jetstream"]
        for build in (platforms.cline_argv, platforms.cline_resume_argv):
            argv = build(pconf, "prompt", "/wt", "build")
            self.assertEqual(argv[argv.index("-P") + 1], "openai-compatible")
            self.assertEqual(argv[argv.index("-m") + 1], "muse-glimmer")

    def test_no_provider_flag_without_provider(self):
        argv = platforms.cline_argv(config.DEFAULTS["platforms"]["cline-free"],
                                    "prompt", "/wt", "build")
        self.assertNotIn("-P", argv)

    def test_jetstream_disabled_and_unrouted_by_default(self):
        cfg = config.resolve_platforms(copy.deepcopy(config.DEFAULTS))
        self.assertFalse(cfg["platforms"]["jetstream"]["enabled"])
        for role in ("sort", "build", "plan"):
            self.assertNotIn("jetstream", router.candidates(cfg, role))
        self.assertNotIn("jetstream", str(config.DEFAULTS["routing"]))

    def test_work_jetstream_never_routes_to_a_personal_project(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["accounts"] = {"work": {"env": {}, "routing": {"build": ["work-jetstream"]}}}
        cfg["platforms"]["work-jetstream"] = {"from": "jetstream", "enabled": True,
                                              "account": "work"}
        cfg["routing"] = {"build": ["work-jetstream", "cline-free"]}
        cfg = config.resolve_platforms(cfg)
        config.validate_accounts(cfg)
        self.assertNotIn("work-jetstream", router.candidates(cfg, "build"))
        self.assertEqual(router.candidates(cfg, "build", account="work"), ["work-jetstream"])
        self.assertEqual(router.candidates(cfg, "build", pin="work-jetstream"), [])

if __name__ == "__main__":
    unittest.main()


class ReadLogContractTests(unittest.TestCase):
    kinds = ('claude', 'cline', 'copilot', 'codex', 'kilo', 'agy', 'kiro', 'vibe')

    def read(self, kind, lines):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'agent.log')
            with open(path, 'w') as stream:
                for line in lines:
                    stream.write((line if isinstance(line, str) else json.dumps(line)) + '\n')
            return platforms.read_log(path, kind, model='configured')

    def empty_result(self):
        return dict(final=None, ok=None, usage=[], quota_hit=False,
                    credit_exhausted=False, overage=False, retry_after=None,
                    last_text='', model='configured', session_id=None,
                    last_error=None, model_unavailable=False,
                    tokens=dict.fromkeys(('in', 'cached', 'out', 'reasoning')),
                    cost_usd=None, credits=None, quota_used={}, requests=None, request_buckets=None,
                    request_coverage=None, rate_limited=None)

    def test_empty_missing_and_non_object_logs_have_identical_schema(self):
        for kind in self.kinds:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                self.assertEqual(platforms.read_log(os.path.join(directory, 'missing'),
                                                   kind, 'configured'), self.empty_result())
                self.assertEqual(self.read(kind, []), self.empty_result())
                self.assertEqual(self.read(kind, [None, [], 42, '"ignored"', {}]),
                                 self.empty_result())

    def test_final_and_status_across_protocols(self):
        final = 'STATUS: DONE complete'
        cases = (
            ('claude', [{'type': 'assistant', 'message': {'content': [
                {'type': 'text', 'text': 'earlier'}]}},
                {'type': 'result', 'subtype': 'success', 'result': final}], True, None),
            ('cline', [{'type': 'run_result', 'finishReason': 'completed', 'text': final}], True, None),
            ('copilot', [{'type': 'assistant.message', 'data': {'content': 'earlier'}},
                {'type': 'assistant.message', 'data': {'content': final}},
                {'type': 'result', 'exitCode': 0}], True, None),
            ('codex', [{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': final}},
                {'type': 'turn.completed'}], True, None),
            ('kilo', [{'type': 'text', 'part': {'text': final}}], None, None),
            ('agy', [{'event': 'step_update', 'step_update': {'text_delta': 'earlier'}},
                 {'event': 'result', 'result': {'status': 'SUCCESS', 'response': final}}], True, None),
            ('vibe', [{'type': 'message', 'role': 'assistant', 'sessionId': 'v1',
                'content': [{'type': 'text', 'text': final}]}], None, 'v1'),
            ('kiro', [{'type': 'metadata', 'data': {'sessionId': 'sess-abc'}},
                {'type': 'sessionUpdate', 'data': {'sessionId': 'sess-abc',
                 'update': {'sessionUpdate': 'agent_message_chunk',
                  'content': {'type': 'text', 'text': 'earlier'}}}},
                {'type': 'runFinished', 'data': {'sessionId': 'sess-abc',
                 'status': 'success', 'finalText': final}}], True, 'sess-abc'),
        )
        for kind, events, ok, session_id in cases:
            with self.subTest(kind=kind):
                expected = self.empty_result()
                expected.update(final=None if kind in ('kilo', 'vibe') else final, ok=ok,
                               last_text=final, session_id=session_id)
                if kind == 'cline':     # no request event seen: unknown, flags observed
                    expected.update(rate_limited=False)
                elif kind == 'kilo':    # real events, no step_start: known zero
                    expected.update(requests=0, request_buckets={},
                                    request_coverage='complete', rate_limited=False)
                result = self.read(kind, events)
                self.assertEqual(result, expected)
                self.assertEqual(platforms.status_line(result['last_text']), ('DONE', 'complete'))

    def test_plaintext_malformed_json_redaction_and_tail(self):
        secret = 'ghp_' + 'a' * 36
        for kind in self.kinds:
            with self.subTest(kind=kind):
                result = self.read(kind, ['  first  ', '{broken json', None, '(' + secret + ')', 'last'])
                separator = '' if kind == 'agy' else '\n'
                self.assertEqual(result['last_text'], separator.join(
                    ['first', '{broken json', '(<redacted>)', 'last']))
                self.assertEqual(self.read(kind, ['x' * 1600])['last_text'], 'x' * 1500)
                error = 'Error: invalid model bogus'
                result = self.read(kind, [error])
                self.assertEqual(result['last_error'], error)
                self.assertTrue(result['model_unavailable'])
                result = self.read(kind, ['Add credits to continue. Try again in 2h'])
                self.assertTrue(result['credit_exhausted'])
                self.assertTrue(result['quota_hit'])
                self.assertEqual(result['retry_after'], 120)

    def test_plaintext_rate_limit_errors(self):
        for kind in self.kinds:
            for message in ('Error: Rate limit exceeded (HTTP 429)',
                            'error: rate_limited (HTTP 429). Try again in 2h'):
                with self.subTest(kind=kind, message=message):
                    result = self.read(kind, [message])
                    self.assertTrue(result['quota_hit'])
                    self.assertFalse(result['credit_exhausted'])
                    self.assertEqual(result['last_error'], message)
                    self.assertEqual(result['retry_after'],
                                     120 if '2h' in message else None)
            for message in ('Error: Invalid API key (from env var MISTRAL_API_KEY)',
                            'Document HTTP 429 rate limit handling'):
                with self.subTest(kind=kind, message=message):
                    self.assertFalse(self.read(kind, [message])['quota_hit'])

    def test_provider_capacity_errors_across_adapters(self):
        for message in ('Selected model is at capacity', 'Model at capacity',
                        'HTTP 429: Too Many Requests', 'HTTP 529',
                        'overloaded_error', 'Provider is overloaded. Try again in 2h'):
            for kind, event in provider_error_events(message):
                with self.subTest(kind=kind, event=event):
                    result = self.read(kind, [event])
                    self.assertTrue(result['quota_hit'])
                    self.assertFalse(result['model_unavailable'])
                    self.assertFalse(result['credit_exhausted'])
                    self.assertEqual(result['retry_after'], 120 if '2h' in message else None)
            for kind in self.kinds:
                with self.subTest(kind=kind, plaintext=message):
                    self.assertTrue(self.read(kind, ['Error: ' + message])['quota_hit'])

    def test_capacity_classification_excludes_permanent_errors_and_prose(self):
        for message in ('Invalid API key', 'model not found: bogus',
                        'AssertionError: expected 5290, got 1'):
            for kind, event in provider_error_events(message):
                with self.subTest(kind=kind, message=message):
                    self.assertFalse(self.read(kind, [event])['quota_hit'])
        for kind in self.kinds:
            with self.subTest(kind=kind):
                self.assertFalse(self.read(kind, [
                    'Document Selected model is at capacity and HTTP 529 handling'
                ])['quota_hit'])
        self.assertFalse(self.read('claude', [{'type': 'result', 'subtype': 'success',
            'result': 'Implemented overloaded_error handling'}])['quota_hit'])

    def test_structured_error_classification_preserves_protocol_differences(self):
        message = '429 rate limit exceeded. Try again in 2h'
        cases = (
            ('claude', {'type': 'rate_limit_event', 'rate_limit_info': {
                'status': 'rejected'}, 'message': message}, None, None),
            ('cline', {'type': 'error', 'message': message}, None, message),
            ('copilot', {'type': 'session.error', 'message': message}, None, None),
            ('codex', {'type': 'turn.failed', 'error': {'message': message}}, False, None),
            ('kilo', {'type': 'error', 'error': {'data': {'message': message}}}, None, message),
            ('agy', {'event': 'result', 'result': {'status': 'FAILED',
                 'response': message}}, False, None),
            ('vibe', {'type': 'error', 'sessionId': 'v1', 'message': message}, None, message),
            ('kiro', {'type': 'runError', 'data': {'sessionId': 's1',
                 'stage': 'prompt', 'message': message}}, False, message),
        )
        for kind, event, ok, last_error in cases:
            with self.subTest(kind=kind):
                result = self.read(kind, [event])
                self.assertTrue(result['quota_hit'])
                self.assertFalse(result['credit_exhausted'])
                self.assertEqual(result['retry_after'], 120)
                self.assertEqual(result['ok'], ok)
                self.assertEqual(result['last_error'], last_error)


class VibeTests(unittest.TestCase):
    def test_real_stream_write_file_effect_then_assistant_message(self):
        # Vibe 2.25.8 / codestral-latest, captured 2026-10-04 (mahler#710).
        records = (
            '{"type":"effect","title":"write_file",'
            '"sessionId":"4e158a39-0e81-8b61-5843-4110359e2b48",'
            '"detail":{"toolName":"write_file","kind":"file_write",'
            '"input":{"filePath":"/tmp/wt/hello.txt","content":"hi"}},'
            '"state":{"status":"completed","decision":"execute",'
            '"approvalSource":"bypass"}}\n'
            '{"type":"message","role":"assistant",'
            '"sessionId":"4e158a39-0e81-8b61-5843-4110359e2b48",'
            '"content":[{"type":"text","text":"DONE"}]}\n'
        )
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "stream.ndjson")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(records)
            summary = platforms.read_log(path, "vibe")
        self.assertEqual(summary["session_id"], "4e158a39-0e81-8b61-5843-4110359e2b48")
        self.assertEqual(summary["last_text"], "DONE")
        self.assertIsNone(summary["last_error"])

    def test_argv(self):
        with mock.patch.object(platforms, "vibe_exe", return_value="/app/vibe"):
            argv = platforms.vibe_argv({"kind": "vibe"}, "do it", "/wt", "build", 60)
            resumed = platforms.vibe_resume_argv({"kind": "vibe"}, "go", "/wt", "build", 60,
                                                 session_id="s9")
            fresh = platforms.vibe_resume_argv({"kind": "vibe"}, "go", "/wt", "build", 60)
        self.assertEqual(argv[:5], ["/app/vibe", "--prompt", "do it", "--workdir", "/wt"])
        for flag in ("--trust", "--auto-approve"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--max-price") + 1], "0")
        self.assertEqual(argv[argv.index("--output") + 1], "streaming")
        self.assertEqual(resumed[-2:], ["--resume", "s9"])
        self.assertNotIn("--resume", fresh)

    def test_env_isolates_home_and_pins_model(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(platforms, "HOME", d):
            os.makedirs(os.path.join(d, ".vibe"))
            with open(os.path.join(d, ".vibe", ".env"), "w") as fh:
                fh.write("MISTRAL_API_KEY=secret-key\n")
            run = os.path.join(d, "run")
            env = platforms.vibe_env({"kind": "vibe", "model": "codestral-latest"}, run, {})
            self.assertEqual(env["VIBE_HOME"], os.path.join(run, "vibe_home"))
            self.assertEqual(env["MISTRAL_API_KEY"], "secret-key")
            with open(os.path.join(env["VIBE_HOME"], "config.toml"), encoding="utf-8") as fh:
                cfg = fh.read()
            self.assertIn('active_model = "codestral-latest"', cfg)
            self.assertNotIn("secret-key", cfg)
            env = platforms.vibe_env({"kind": "vibe"}, run, {"MISTRAL_API_KEY": "x"})
            self.assertNotIn("MISTRAL_API_KEY", env)

    def test_429_is_quota_hit(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "l")
            with open(path, "w") as fh:
                fh.write(json.dumps({"type": "error", "message": "HTTP 429 Rate limit exceeded"}) + "\n")
            self.assertTrue(platforms.read_log(path, "vibe")["quota_hit"])


class KiroUsageProbeTests(unittest.TestCase):
    def test_parse_kiro_usage_real_output(self):
        text = ("Estimated Usage | resets on 2026-11-01 | KIRO FREE\n"
                "Credits (0.23 of 50 covered in plan), 0.5%\n"
                "Manage your plan at https://app.kiro.dev/account/usage\n")
        samples = platforms.parse_kiro_usage(text)
        self.assertEqual(len(samples), 1)
        window, pct, resets = samples[0]
        self.assertEqual(window, "monthly")
        self.assertAlmostEqual(pct, 0.5, places=1)
        self.assertTrue(resets.startswith("2026-11-01"))

    def test_parse_kiro_usage_half_credit(self):
        text = "Credits (1.50 of 50 covered in plan), 3.0%"
        samples = platforms.parse_kiro_usage(text)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0][0], "monthly")
        self.assertAlmostEqual(samples[0][1], 3.0, places=1)

    def test_parse_kiro_usage_no_credit_line(self):
        samples = platforms.parse_kiro_usage("No credit info here")
        self.assertEqual(samples, [])

    def test_parse_kiro_usage_empty(self):
        self.assertEqual(platforms.parse_kiro_usage(""), [])
        self.assertEqual(platforms.parse_kiro_usage(None), [])

    def test_parse_kiro_usage_uses_reset_date(self):
        text = "Estimated Usage | resets on 2026-12-15 | KIRO FREE\nCredits (25 of 50 covered in plan), 50.0%"
        samples = platforms.parse_kiro_usage(text)
        self.assertTrue(samples[0][2].startswith("2026-12-15"))

    def test_probe_kiro_not_installed(self):
        with mock.patch.object(platforms, "kiro_exe", return_value=None):
            self.assertEqual(platforms.probe_kiro(), [])

    def test_probe_kiro_parses_acp_jsonl(self):
        stdout = "\n".join([
            json.dumps({"type": "runStarted", "data": {"payloadSchema": "acp",
                "acpProtocolVersion": 1, "engine": "v2"}}),
            json.dumps({"type": "metadata", "data": {"sessionId": "s1",
                "contextUsagePercentage": 5.9}}),
            json.dumps({"type": "sessionUpdate", "data": {"sessionId": "s1",
                "update": {"sessionUpdate": "agent_message_chunk",
                 "content": {"type": "text",
                  "text": "Estimated Usage | resets on 2026-11-01 | KIRO FREE\n"}}}}),
            json.dumps({"type": "sessionUpdate", "data": {"sessionId": "s1",
                "update": {"sessionUpdate": "agent_message_chunk",
                 "content": {"type": "text",
                  "text": "Credits (0.23 of 50 covered in plan), 0.5%\n"}}}}),
            json.dumps({"type": "runFinished", "data": {"sessionId": "s1",
                "status": "success", "finalText": "Credits (0.23 of 50 covered in plan), 0.5%"}}),
        ])
        result = mock.Mock(returncode=0, stdout=stdout, stderr="")
        with mock.patch.object(platforms, "kiro_exe", return_value="/app/kiro-cli"), \
             mock.patch.object(platforms.subprocess, "run", return_value=result):
            samples = platforms.probe_kiro()
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0][0], "monthly")
        self.assertAlmostEqual(samples[0][1], 0.5, places=1)

    def test_probe_kiro_failed_run(self):
        result = mock.Mock(returncode=1, stdout="", stderr="error: keychain failure")
        with mock.patch.object(platforms, "kiro_exe", return_value="/app/kiro-cli"), \
             mock.patch.object(platforms.subprocess, "run", return_value=result):
            self.assertEqual(platforms.probe_kiro(), [])


class KiroReadLogTests(unittest.TestCase):
    def test_kiro_run_finished_extracts_final_and_session(self):
        text = "STATUS: DONE finished"
        events = [
            {"type": "metadata", "data": {"sessionId": "sess-123"}},
            {"type": "sessionUpdate", "data": {"sessionId": "sess-123",
             "update": {"sessionUpdate": "agent_message_chunk",
              "content": {"type": "text", "text": "Starting work"}}}},
            {"type": "sessionUpdate", "data": {"sessionId": "sess-123",
             "update": {"sessionUpdate": "agent_message_chunk",
              "content": {"type": "text", "text": text}}}},
            {"type": "runFinished", "data": {"sessionId": "sess-123",
             "status": "success", "finalText": text, "stopReason": "end_turn"}},
        ]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "agent.log")
            with open(path, "w") as fh:
                for ev in events:
                    fh.write(json.dumps(ev) + "\n")
            res = platforms.read_log(path, "kiro")
        self.assertTrue(res["ok"])
        self.assertEqual(res["final"], text)
        self.assertEqual(res["session_id"], "sess-123")
        self.assertEqual(platforms.status_line(res["last_text"]), ("DONE", "finished"))

    def test_kiro_run_error_extracts_model_unavailable(self):
        events = [
            {"type": "runError", "data": {"sessionId": "s1", "stage": "prompt",
             "message": "Internal error (code -32603): Encountered an error in the response stream: The model 'nonexistent' is not available. Please use '/model' to select a different model and try again."}},
        ]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "agent.log")
            with open(path, "w") as fh:
                for ev in events:
                    fh.write(json.dumps(ev) + "\n")
            res = platforms.read_log(path, "kiro")
        self.assertFalse(res["ok"])
        self.assertTrue(res["model_unavailable"])

    def test_kiro_plaintext_error_captured(self):
        # Auth failures print "error: ..." to stderr (captured by 2>&1 in the log).
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "agent.log")
            with open(path, "w") as fh:
                fh.write("error: Security error: SecKeychainItemCreateFromContent\n")
            res = platforms.read_log(path, "kiro")
        self.assertEqual(res["last_error"],
                         "error: Security error: SecKeychainItemCreateFromContent")
        self.assertFalse(res["model_unavailable"])
