"""Smoke-test the shipped ``bin/mahler`` executable itself."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = REPO_ROOT / "bin" / "mahler"


class CliEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cwd = root / "unrelated-cwd"
        self.cwd.mkdir()
        self.home = root / "home"
        self.home.mkdir()
        self.mahler_home = root / "mahler-home"
        self.mahler_home.mkdir()
        self.env = os.environ.copy()
        self.env.update({
            "HOME": str(self.home),
            "MAHLER_HOME": str(self.mahler_home),
            "PATH": os.path.dirname(shutil.which("python3") or "python3"),
        })

    def tearDown(self):
        self.tmp.cleanup()

    def run_entrypoint(self, *args, path=ENTRYPOINT):
        return subprocess.run(
            [str(path), *args],
            cwd=self.cwd,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_help_runs_the_real_entrypoint(self):
        result = self.run_entrypoint("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: mahler", result.stdout)
        self.assertIn("version", result.stdout)

    def test_version_runs_with_isolated_state(self):
        result = self.run_entrypoint("version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("commit:", result.stdout)
        self.assertIn("known-good:", result.stdout)
        self.assertIn("behind:", result.stdout)
        self.assertFalse((self.home / ".mahler").exists())

    def test_broken_wrapper_import_fails(self):
        broken = Path(self.tmp.name) / "broken-mahler"
        broken.write_text(ENTRYPOINT.read_text().replace(
            "from mahler.cli import main",
            "from mahler.entrypoint_import_that_does_not_exist import main",
        ))
        broken.chmod(0o755)

        result = self.run_entrypoint(path=broken)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ModuleNotFoundError", result.stderr)

    def test_unexecutable_wrapper_is_rejected(self):
        unexecutable = Path(self.tmp.name) / "unexecutable-mahler"
        shutil.copy2(ENTRYPOINT, unexecutable)
        unexecutable.chmod(0o644)

        with self.assertRaises(PermissionError):
            self.run_entrypoint("--help", path=unexecutable)


if __name__ == "__main__":
    unittest.main()
