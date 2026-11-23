"""Subprocess regression tests for the repository test-suite gates."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
STRICT_RUNNER = ROOT / "tests" / "run_strict.py"
RANDOM_RUNNER = ROOT / "tests" / "run_random.py"


def run_runner(runner, suite_dir, *args, dev_mode=False, extra_env=None):
    command = [sys.executable]
    if dev_mode:
        command.extend(("-X", "dev"))
    command.extend((str(runner), "--start-directory", str(suite_dir), *args))
    env = {
        "HOME": str(suite_dir),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(ROOT),
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        command,
        cwd=suite_dir,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


class FixtureSuiteTestCase(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.suite_dir = Path(temporary_directory.name)

    def write_test(self, name, source):
        (self.suite_dir / name).write_text(textwrap.dedent(source))


class RunnerEnvironmentTests(FixtureSuiteTestCase):
    def test_fixture_subprocess_does_not_inherit_state_or_credentials(self):
        self.write_test(
            "test_environment.py",
            """
            import os
            from pathlib import Path
            import unittest
            from mahler import config

            class EnvironmentTest(unittest.TestCase):
                def test_isolated_environment(self):
                    home = Path(os.environ["HOME"])
                    self.assertEqual(home.resolve(), Path.cwd().resolve())
                    self.assertEqual(str(home / ".mahler"), config.STATE)
                    self.assertEqual(str(home / ".mahler" / "runs"), config.RUNS_DIR)
                    for name in (
                        "MAHLER_HOME", "MAHLER_RUNS_DIR", "MAHLER_CONFIG",
                        "GH_TOKEN", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY",
                        "UNRELATED_PARENT_SECRET",
                    ):
                        self.assertNotIn(name, os.environ)
            """,
        )
        poisoned = {
            "MAHLER_HOME": "/fake/live-state",
            "MAHLER_RUNS_DIR": "/fake/live-runs",
            "MAHLER_CONFIG": "/fake/config",
            "GH_TOKEN": "fake-token",
            "GITHUB_TOKEN": "fake-token",
            "AWS_SECRET_ACCESS_KEY": "fake-secret",
            "UNRELATED_PARENT_SECRET": "fake-secret",
        }
        with mock.patch.dict(os.environ, poisoned):
            for runner in (STRICT_RUNNER, RANDOM_RUNNER):
                with self.subTest(runner=runner.name):
                    result = run_runner(runner, self.suite_dir)
                    self.assertEqual(0, result.returncode, result.stderr)


class StrictRunnerGateTests(FixtureSuiteTestCase):
    def run_strict(self):
        return run_runner(STRICT_RUNNER, self.suite_dir, dev_mode=True)

    def test_clean_fixture_suite_succeeds(self):
        self.write_test(
            "test_clean.py",
            """
            import unittest

            class CleanTest(unittest.TestCase):
                def test_passes(self):
                    self.assertTrue(True)
            """,
        )
        result = self.run_strict()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("OK", result.stderr)

    def test_assertion_failure_exits_nonzero_and_identifies_cause(self):
        self.write_test(
            "test_failure.py",
            """
            import unittest

            class FailureTest(unittest.TestCase):
                def test_assertion_fails(self):
                    self.assertEqual(1, 2, "fixture assertion failed")
            """,
        )
        result = self.run_strict()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("FAIL: test_assertion_fails", result.stderr)
        self.assertIn("AssertionError: 1 != 2 : fixture assertion failed", result.stderr)

    def test_resource_warning_exits_nonzero_and_identifies_cause(self):
        self.write_test(
            "test_resource_warning.py",
            """
            import unittest
            import warnings

            class ResourceWarningTest(unittest.TestCase):
                def test_warns(self):
                    warnings.warn("fixture resource leaked", ResourceWarning)
            """,
        )
        result = self.run_strict()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("ERROR: test_warns", result.stderr)
        self.assertIn("ResourceWarning: fixture resource leaked", result.stderr)

    def test_unraisable_finalized_during_gc_exits_nonzero_and_identifies_cause(self):
        self.write_test(
            "test_unraisable.py",
            """
            import unittest

            class BrokenFinalizer:
                def __del__(self):
                    raise RuntimeError("fixture finalizer exploded")

            class UnraisableTest(unittest.TestCase):
                def test_creates_cycle(self):
                    cycle = []
                    cycle.extend((cycle, BrokenFinalizer()))
                    self.assertEqual(2, len(cycle))
            """,
        )
        result = self.run_strict()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("RuntimeError: fixture finalizer exploded", result.stderr)
        self.assertIn("FAILED: 1 unraisable exception(s)", result.stderr)


class RandomRunnerGateTests(FixtureSuiteTestCase):
    def test_same_seed_repeats_the_same_order(self):
        order_path = self.suite_dir / "order.log"
        self.write_test(
            "test_order.py",
            """
            import os
            import unittest

            def record(name):
                with open(os.environ["TEST_ORDER_LOG"], "a") as stream:
                    stream.write(name + "\\n")

            class OrderTest(unittest.TestCase):
                def test_alpha(self): record("alpha"); self.assertTrue(True)
                def test_beta(self): record("beta"); self.assertTrue(True)
                def test_gamma(self): record("gamma"); self.assertTrue(True)
                def test_delta(self): record("delta"); self.assertTrue(True)
                def test_epsilon(self): record("epsilon"); self.assertTrue(True)
                def test_zeta(self): record("zeta"); self.assertTrue(True)
            """,
        )
        orders = []
        for _ in range(2):
            order_path.unlink(missing_ok=True)
            result = run_runner(
                RANDOM_RUNNER,
                self.suite_dir,
                "987654321",
                extra_env={"TEST_ORDER_LOG": str(order_path)},
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("random order seed: 987654321", result.stdout)
            orders.append(order_path.read_text().splitlines())
        self.assertEqual(orders[0], orders[1])
        self.assertEqual(6, len(orders[0]))

    def test_failure_propagates_nonzero_exit(self):
        self.write_test(
            "test_random_failure.py",
            """
            import unittest

            class RandomFailureTest(unittest.TestCase):
                def test_fails(self):
                    self.fail("random fixture failure propagated")
            """,
        )
        result = run_runner(RANDOM_RUNNER, self.suite_dir, "12345")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("FAIL: test_fails", result.stderr)
        self.assertIn("AssertionError: random fixture failure propagated", result.stderr)
