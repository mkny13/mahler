"""Exercise the real wheel/install gate, including broken distribution fixtures."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SDKRunnerPackagingTests(unittest.TestCase):
    def run_gate(self, replacement=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / 'checkout'
            (checkout / 'scripts').mkdir(parents=True)
            script = ROOT / 'scripts/verify_sdk_runner.sh'
            self.assertTrue(script.is_file(), 'packaging verification gate is missing')
            self.assertTrue(os.access(script, os.X_OK))
            shutil.copy2(script, checkout / 'scripts' / script.name)
            shutil.copytree(ROOT / 'sdk_runner', checkout / 'sdk_runner',
                            ignore=shutil.ignore_patterns('build', '*.egg-info', '__pycache__'))
            manifest = checkout / 'sdk_runner/pyproject.toml'
            if replacement:
                before, after = replacement
                original = manifest.read_text()
                self.assertIn(before, original)
                manifest.write_text(original.replace(before, after))
            scratch = root / 'scratch'
            scratch.mkdir()
            unrelated = root / 'unrelated'
            unrelated.mkdir()
            result = subprocess.run(
                ['/bin/sh', str(checkout / 'scripts' / script.name)],
                cwd=unrelated, env={'PATH': os.environ['PATH'], 'TMPDIR': str(scratch)},
                capture_output=True, text=True, timeout=180)
            self.assertEqual(list(scratch.iterdir()), [], 'gate leaked temporary files')
            self.assertEqual(sorted(p.name for p in (checkout / 'sdk_runner').iterdir()),
                             ['README.md', 'mahler_sdk_run.py', 'pyproject.toml'])
            return result

    def test_installed_package_metadata_and_cli(self):
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('SDK runner packaging verified', result.stdout)

    def test_invalid_entry_point_fails(self):
        result = self.run_gate(('mahler_sdk_run:main', 'mahler_sdk_run:missing_main'))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('entry point', result.stdout + result.stderr)

    def test_missing_packaged_module_fails(self):
        result = self.run_gate(('py-modules = ["mahler_sdk_run"]',
                                'py-modules = ["missing_runner"]'))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('mahler_sdk_run', result.stdout + result.stderr)
