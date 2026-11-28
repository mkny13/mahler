"""Exercise launcher/install.sh against an isolated local Git origin."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]


class LauncherInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.source = root / "source"
        self.origin = root / "origin.git"
        self.home.mkdir()
        self.source.mkdir()

        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_") and not key.startswith("MAHLER_")
        }
        self.env.update(
            HOME=str(self.home),
            PATH="/usr/bin:/bin",
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
        )
        self._copy_source_files()
        self._git("init", "-q", "-b", "main", cwd=self.source)
        self._git("config", "user.name", "Installer Test", cwd=self.source)
        self._git("config", "user.email", "installer@example.invalid", cwd=self.source)
        self._git("add", ".", cwd=self.source)
        self._git("commit", "-qm", "initial installer fixture", cwd=self.source)
        self._git("clone", "-q", "--bare", str(self.source), str(self.origin), cwd=self.source)
        self._git("remote", "add", "origin", str(self.origin), cwd=self.source)

    def _copy_source_files(self):
        (self.source / "launcher").mkdir()
        (self.source / "bin").mkdir()
        for relative in (
            "launcher/install.sh",
            "launcher/mahler-launcher",
            "launcher/local.mahler.plist",
            "config.example.toml",
        ):
            destination = self.source / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPOSITORY / relative, destination)
        shutil.copy2(REPOSITORY / "bin/mahler", self.source / "bin/mahler")

    def _git(self, *args, cwd):
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def _run_install(self):
        return subprocess.run(
            ["/bin/sh", str(self.source / "launcher/install.sh")],
            cwd=self.source,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def test_initial_install_creates_isolated_artifacts(self):
        result = self._run_install()

        self.assertEqual(result.returncode, 0, result.stderr)
        app = self.home / ".mahler/app"
        self.assertTrue((app / ".git").is_dir())
        self.assertEqual(
            self._git("rev-parse", "HEAD", cwd=app),
            self._git("rev-parse", "origin/main", cwd=app),
        )
        installed_launcher = self.home / ".mahler/bin/mahler-launcher"
        self.assertTrue(installed_launcher.is_file())
        self.assertTrue(os.access(installed_launcher, os.X_OK))

        plist = self.home / "Library/LaunchAgents/local.mahler.plist"
        self.assertTrue(plist.is_file())
        self.assertNotIn("__HOME__", plist.read_text())
        self.assertIn(str(self.home), plist.read_text())
        self.assertEqual(
            (self.home / ".mahler/config.toml").read_text(),
            (self.source / "config.example.toml").read_text(),
        )
        link = self.home / ".local/bin/mahler"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), (app / "bin/mahler").resolve())
        self.assertIn("installed. app at ", result.stdout)
        self.assertIn(
            f"start:  launchctl bootstrap gui/{os.getuid()} {plist}",
            result.stdout,
        )

    def test_rerun_preserves_config_and_updates_checkout(self):
        first = self._run_install()
        self.assertEqual(first.returncode, 0, first.stderr)
        config = self.home / ".mahler/config.toml"
        config.write_text("custom = true\n")

        (self.source / "bin/mahler").write_text("#!/bin/sh\necho updated\n")
        (self.source / "bin/mahler").chmod(0o755)
        self._git("add", "bin/mahler", cwd=self.source)
        self._git("commit", "-qm", "update fixture", cwd=self.source)
        self._git("push", "-q", "origin", "main", cwd=self.source)

        result = self._run_install()

        self.assertEqual(result.returncode, 0, result.stderr)
        app = self.home / ".mahler/app"
        self.assertEqual(
            self._git("rev-parse", "HEAD", cwd=app),
            self._git("rev-parse", "origin/main", cwd=app),
        )
        self.assertEqual(config.read_text(), "custom = true\n")
        self.assertTrue((self.home / ".local/bin/mahler").is_symlink())

    def test_missing_source_artifact_fails(self):
        (self.source / "launcher/mahler-launcher").unlink()

        result = self._run_install()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / ".mahler/bin/mahler-launcher").exists())

    def test_unreachable_origin_fails(self):
        self._git("remote", "set-url", "origin", str(self.origin / "missing"), cwd=self.source)

        result = self._run_install()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / ".mahler/app/.git").exists())


if __name__ == "__main__":
    unittest.main()
