"""Subprocess tests verifying test suite runner gates (run_strict.py, run_random.py)."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
STRICT_RUNNER = ROOT / "tests" / "run_strict.py"
RANDOM_RUNNER = ROOT / "tests" / "run_random.py"


def run_runner(runner_path, args, suite_dir, dev_mode=False, extra_env=None):
    cmd = [sys.executable]
    if dev_mode:
        cmd.extend(["-X", "dev"])
    cmd.append(str(runner_path))
    cmd.extend(args)

    env = {"HOME": str(suite_dir), "PYTHONPATH": str(ROOT)}
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        cmd,
        cwd=suite_dir,
        capture_output=True,
        text=True,
        env=env,
    )


class RunnerEnvironmentTests(unittest.TestCase):
    def test_fixture_subprocess_does_not_inherit_state_or_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            suite_dir = Path(tmp)
            (suite_dir / "test_environment.py").write_text(
                "import os\n"
                "from pathlib import Path\n"
                "import unittest\n"
                "from mahler import config\n\n"
                "class EnvironmentTest(unittest.TestCase):\n"
                "    def test_isolated_environment(self):\n"
                "        home = Path(os.environ['HOME'])\n"
                "        self.assertEqual(home.resolve(), Path.cwd().resolve())\n"
                "        self.assertEqual(str(home / '.mahler'), config.STATE)\n"
                "        self.assertEqual(str(home / '.mahler' / 'runs'), config.RUNS_DIR)\n"
                "        self.assertEqual(config.REPO_ROOT, os.environ['PYTHONPATH'])\n"
                "        for name in ('MAHLER_HOME', 'MAHLER_RUNS_DIR', 'MAHLER_CONFIG',\n"
                "                     'GH_TOKEN', 'GITHUB_TOKEN', 'AWS_SECRET_ACCESS_KEY',\n"
                "                     'UNRELATED_PARENT_SECRET'):\n"
                "            self.assertNotIn(name, os.environ)\n"
            )
            parent_env = {
                "MAHLER_HOME": "/fake/live-state",
                "MAHLER_RUNS_DIR": "/fake/live-runs",
                "MAHLER_CONFIG": "/fake/config",
                "GH_TOKEN": "fake-token",
                "GITHUB_TOKEN": "fake-token",
                "AWS_SECRET_ACCESS_KEY": "fake-secret",
                "UNRELATED_PARENT_SECRET": "fake-secret",
                "PYTHONPATH": "/fake/pythonpath",
            }
            with mock.patch.dict(os.environ, parent_env):
                for runner in (STRICT_RUNNER, RANDOM_RUNNER):
                    with self.subTest(runner=runner.name):
                        proc = run_runner(runner, ["--start-directory", str(suite_dir)], suite_dir)
                        self.assertEqual(0, proc.returncode, proc.stderr)


class StrictRunnerGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.suite_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_fixture_suite_succeeds(self):
        test_file = self.suite_dir / "test_clean.py"
        test_file.write_text(
            "import unittest\n\n"
            "class CleanTest(unittest.TestCase):\n"
            "    def test_pass(self):\n"
            "        self.assertTrue(True)\n"
        )
        proc = run_runner(STRICT_RUNNER, ["--start-directory", str(self.suite_dir)], self.suite_dir, dev_mode=True)
        self.assertEqual(0, proc.returncode, f"Expected 0 exit, got {proc.returncode}.\nstderr:\n{proc.stderr}")
        self.assertIn("OK", proc.stderr)
        self.assertNotIn("FAILED", proc.stderr)
        self.assertNotIn("unraisable", proc.stderr)

    def test_assertion_failure_exits_nonzero_and_identifies_cause(self):
        test_file = self.suite_dir / "test_fail.py"
        test_file.write_text(
            "import unittest\n\n"
            "class FailingTest(unittest.TestCase):\n"
            "    def test_assertion_fails(self):\n"
            "        self.assertEqual(1, 2, 'custom assertion failure message')\n"
        )
        proc = run_runner(STRICT_RUNNER, ["--start-directory", str(self.suite_dir)], self.suite_dir, dev_mode=True)
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("AssertionError: 1 != 2 : custom assertion failure message", proc.stderr)
        self.assertIn("FAIL: test_assertion_fails", proc.stderr)

    def test_resource_warning_exits_nonzero_and_identifies_cause(self):
        test_file = self.suite_dir / "test_warning.py"
        test_file.write_text(
            "import unittest\n"
            "import warnings\n\n"
            "class ResourceWarningTest(unittest.TestCase):\n"
            "    def test_emits_resource_warning(self):\n"
            "        warnings.warn('unclosed socket handle simulated', ResourceWarning)\n"
        )
        proc = run_runner(STRICT_RUNNER, ["--start-directory", str(self.suite_dir)], self.suite_dir, dev_mode=True)
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("ResourceWarning: unclosed socket handle simulated", proc.stderr)
        self.assertIn("ERROR: test_emits_resource_warning", proc.stderr)

    def test_unraisable_exception_during_collection_exits_nonzero_and_identifies_cause(self):
        test_file = self.suite_dir / "test_unraisable.py"
        test_file.write_text(
            "import unittest\n\n"
            "class LeakyResource:\n"
            "    def __del__(self):\n"
            "        raise RuntimeError('destructor finalized during collection boom')\n\n"
            "class UnraisableTest(unittest.TestCase):\n"
            "    def test_create_cycle(self):\n"
            "        cycle = []\n"
            "        cycle.append(cycle)\n"
            "        cycle.append(LeakyResource())\n"
        )
        proc = run_runner(STRICT_RUNNER, ["--start-directory", str(self.suite_dir)], self.suite_dir, dev_mode=True)
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("destructor finalized during collection boom", proc.stderr)
        self.assertIn("FAILED: 1 unraisable exception(s)", proc.stderr)

    def test_positional_start_directory(self):
        test_file = self.suite_dir / "test_pos.py"
        test_file.write_text(
            "import unittest\n\n"
            "class PosTest(unittest.TestCase):\n"
            "    def test_pos(self):\n"
            "        self.assertTrue(True)\n"
        )
        proc = run_runner(STRICT_RUNNER, [str(self.suite_dir)], self.suite_dir, dev_mode=True)
        self.assertEqual(0, proc.returncode)
        self.assertIn("OK", proc.stderr)


class RandomRunnerGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.suite_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_repeatable_ordering_with_same_seed(self):
        log_file = self.suite_dir / "execution_order.log"
        test_file = self.suite_dir / "test_order.py"
        test_file.write_text(
            "import os\n"
            "import unittest\n\n"
            "def log(name):\n"
            "    path = os.environ.get('TEST_ORDER_LOG')\n"
            "    if path:\n"
            "        with open(path, 'a') as f:\n"
            "            f.write(name + '\\n')\n\n"
            "class OrderTest(unittest.TestCase):\n"
            "    def test_case_alpha(self): log('alpha'); self.assertTrue(True)\n"
            "    def test_case_beta(self): log('beta'); self.assertTrue(True)\n"
            "    def test_case_gamma(self): log('gamma'); self.assertTrue(True)\n"
            "    def test_case_delta(self): log('delta'); self.assertTrue(True)\n"
            "    def test_case_epsilon(self): log('epsilon'); self.assertTrue(True)\n"
            "    def test_case_zeta(self): log('zeta'); self.assertTrue(True)\n"
        )
        seed = "987654321"
        extra_env = {"TEST_ORDER_LOG": str(log_file)}

        # Run 1
        proc1 = run_runner(
            RANDOM_RUNNER,
            [seed, "--start-directory", str(self.suite_dir)],
            self.suite_dir,
            extra_env=extra_env,
        )
        self.assertEqual(0, proc1.returncode, f"Run 1 failed:\n{proc1.stderr}")
        order1 = log_file.read_text().strip().splitlines()

        # Clear log
        log_file.unlink()

        # Run 2 with same seed
        proc2 = run_runner(
            RANDOM_RUNNER,
            [seed, "--start-directory", str(self.suite_dir)],
            self.suite_dir,
            extra_env=extra_env,
        )
        self.assertEqual(0, proc2.returncode, f"Run 2 failed:\n{proc2.stderr}")
        order2 = log_file.read_text().strip().splitlines()

        self.assertEqual(order1, order2)
        self.assertEqual(6, len(order1))
        self.assertIn(f"random order seed: {seed}", proc1.stdout)
        self.assertIn(f"random order seed: {seed}", proc2.stdout)

        # Run 3 with different seed
        log_file.unlink()
        seed3 = "1"
        proc3 = run_runner(
            RANDOM_RUNNER,
            [seed3, str(self.suite_dir)],
            self.suite_dir,
            extra_env=extra_env,
        )
        self.assertEqual(0, proc3.returncode)
        order3 = log_file.read_text().strip().splitlines()
        self.assertEqual(6, len(order3))
        self.assertNotEqual(order1, order3)
        self.assertIn(f"random order seed: {seed3}", proc3.stdout)

    def test_failure_propagates_nonzero_exit(self):
        test_file = self.suite_dir / "test_random_fail.py"
        test_file.write_text(
            "import unittest\n\n"
            "class RandomFailingTest(unittest.TestCase):\n"
            "    def test_passes(self):\n"
            "        self.assertTrue(True)\n"
            "    def test_propagates_failure(self):\n"
            "        self.fail('random runner must propagate failure')\n"
        )
        seed = "12345"
        proc = run_runner(
            RANDOM_RUNNER,
            [seed, "--start-directory", str(self.suite_dir)],
            self.suite_dir,
        )
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("AssertionError: random runner must propagate failure", proc.stderr)
        self.assertIn("FAIL: test_propagates_failure", proc.stderr)
