"""Static guards against tests that accidentally pass without checking anything."""
import ast
from pathlib import Path
import unittest


# Keys are file::test_name; exceptions must explain what successful execution proves.
ALLOWED = {
    'test_accounts.py::test_launch_accepts_a_platform_on_any_declared_account':
        'Account validation succeeds by returning without raising for both declared accounts.',
    'test_backup.py::test_optimize_and_checkpoint_does_not_raise':
        'Smoke test: SQLite optimization and checkpoint must complete without raising.',
    'test_console_state.py::test_refresh_skips_identical_fragments_but_clears_submitted_drafts':
        'Node executes deepStrictEqual assertions; subprocess check=True propagates failures.',
    'test_ship.py::test_no_checks_still_requires_freshness':
        'Delegates to test_stale_base_rebuild_preserves_attempts_and_work and its assertions.',
}
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
HELPER_PREFIXES = ('assert', 'check', '_assert', '_check')


def body_nodes(function):
    """Inspect the executed body, not assertions hidden in nested definitions."""
    pending = list(function.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (*FUNCTIONS, ast.ClassDef, ast.Lambda)):
            continue
        yield node
        pending.extend(ast.iter_child_nodes(node))


def problems(source, filename):
    tree = ast.parse(source, filename=filename)
    helpers = {node.name for node in ast.walk(tree)
               if isinstance(node, FUNCTIONS)
               and node.name.startswith(HELPER_PREFIXES)}
    findings = []
    for function in ast.walk(tree):
        if not isinstance(function, FUNCTIONS) or not function.name.startswith('test_'):
            continue
        nodes = list(body_nodes(function))
        called = {id(node.func) for node in nodes if isinstance(node, ast.Call)}
        has_assertion = any(isinstance(node, ast.Assert) for node in nodes)
        for node in nodes:
            if isinstance(node, ast.Call):
                target = node.func
                if isinstance(target, ast.Name) and target.id in helpers:
                    has_assertion = True
                if not isinstance(target, ast.Attribute):
                    continue
                name = target.attr
                is_self = isinstance(target.value, ast.Name) and target.value.id == 'self'
                if (is_self and (name.startswith('assert') or name == 'fail')
                        or name.startswith('assert_')
                        or is_self and name in helpers):
                    has_assertion = True
                if name.startswith(('called_', 'not_called', 'has_calls', 'any_call')):
                    findings.append((node.lineno, function.name, 'mock assertion missing assert_ prefix'))
                if is_self and name == 'assertTrue' and len(node.args) >= 2:
                    message = node.args[1]
                    if not (isinstance(message, ast.JoinedStr)
                            or isinstance(message, ast.Constant) and isinstance(message.value, str)):
                        findings.append((node.lineno, function.name, 'assertTrue second argument is not a message literal'))
            elif (isinstance(node, ast.Attribute) and node.attr.startswith('assert_')
                  and id(node) not in called):
                findings.append((node.lineno, function.name, 'mock assertion referenced without calling it'))
        if not has_assertion and f'{filename}::{function.name}' not in ALLOWED:
            findings.append((function.lineno, function.name, 'no assertion'))
    return [f'{filename}:{line} {name}: {reason}' for line, name, reason in findings]


class SuiteHygieneTests(unittest.TestCase):
    def test_suite_has_assertions(self):
        findings = []
        for path in sorted(Path(__file__).parent.glob('test_*.py')):
            findings.extend(problems(path.read_text(), path.name))
        self.assertEqual([], findings, '\n'.join(findings))

    def test_exceptions_have_reasons(self):
        for name, reason in ALLOWED.items():
            with self.subTest(name=name):
                self.assertIsInstance(reason, str)
                self.assertTrue(reason.strip())

    def test_detector_rejects_false_greens(self):
        samples = [
            'pass',
            'def hidden():\n        assert False',
            'mock.called_once_with(1)',
            'mock.assert_called_once_with',
            'self.assertTrue(actual, expected)',
        ]
        for body in samples:
            with self.subTest(body=body):
                self.assertTrue(problems('def test_example(self):\n    ' + body, 'sample.py'))

    def test_detector_accepts_assertions(self):
        samples = [
            'assert value', 'self.assertEqual(1, 1)', 'self.fail("bad")',
            'mock.assert_called_once_with(1)',
            'with self.assertRaises(ValueError):\n        action()',
            'with self.assertLogs():\n        action()',
            'with self.assertWarns(UserWarning):\n        action()',
            'self.assertTrue(value, "message")',
            'self.assertTrue(value, f"message {value}")',
            'check_result()', 'self._assert_result()',
        ]
        for body in samples:
            with self.subTest(body=body):
                source = ('def check_result(): pass\ndef _assert_result(): pass\n'
                          'def test_example(self):\n    ' + body)
                self.assertEqual([], problems(source, 'sample.py'))

