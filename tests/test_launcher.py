"""Exercise the installed POSIX launcher with isolated Git and command stubs."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

LAUNCHER = Path(__file__).resolve().parents[1] / 'launcher' / 'mahler-launcher'


class LauncherTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.app = self.home / 'app'
        self.app.mkdir()
        self.stubs = self.home / 'stubs'
        self.stubs.mkdir()
        self.env = {**os.environ, 'MAHLER_HOME': str(self.home),
                    'PATH': str(self.stubs) + os.pathsep + os.environ['PATH'],
                    'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull}
        for key in list(self.env):
            if key.startswith('GIT_') and key not in ('GIT_CONFIG_NOSYSTEM', 'GIT_CONFIG_GLOBAL'):
                del self.env[key]
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Launcher Test')
        self.git('config', 'user.email', 'launcher@example.invalid')
        (self.app / 'bin').mkdir()
        self.script(self.app / 'bin' / 'mahler', '''case "$1" in
  tick) echo "tick rc=$TICK_RC"; exit "$TICK_RC" ;;
  notify) echo "$*" >> "$MAHLER_HOME/notifications" ;;
esac
''')
        self.good = self.commit('good')
        self.bad = self.commit('bad')
        # A local origin gives fetch a real main ref without any network access.
        self.remote = self.home / 'origin.git'
        self.git('clone', '-q', '--bare', str(self.app), str(self.remote))
        self.git('remote', 'add', 'origin', str(self.remote))
        self.script(self.stubs / 'gh', '''echo "$*" >> "$MAHLER_HOME/gh_calls"
case "$1" in
  repo) echo test/launcher ;;
  api) echo "${CI_RESULT:-green}" ;;
esac
''')
        self.script(self.stubs / 'python3', 'exit "${TEST_RC:-0}"\n')
        (self.home / 'known_good').write_text(self.good + '\n')
        (self.home / 'launch_ok').write_text(self.good + '\n')

    def script(self, path, body):
        path.write_text('#!/bin/sh\n' + body)
        path.chmod(0o755)

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.app), *args], env=self.env,
                              capture_output=True, text=True, check=True).stdout.strip()

    def commit(self, label):
        (self.app / 'version').write_text(label)
        self.git('add', '.')
        self.git('commit', '-qm', label)
        return self.git('rev-parse', 'HEAD')

    def run_launcher(self, rc=0, **env):
        result = subprocess.run(['/bin/sh', str(LAUNCHER)], cwd=self.home,
                                env={**self.env, 'TICK_RC': str(rc), **env},
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def read(self, name):
        return (self.home / name).read_text().strip()

    def test_launch_failure_rolls_back_immediately_and_waits_for_new_main(self):
        # Clean ticks may already have blessed the broken version.
        (self.home / 'known_good').write_text(self.bad + '\n')
        self.run_launcher(3)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.good)
        self.assertEqual(self.read('known_good'), self.bad)
        self.assertEqual(self.read('bad_sha'), self.bad)
        self.assertEqual(self.read('tick_failures'), '0')
        self.assertIn(f'rolled back {self.bad} -> {self.good}', self.read('logs/update.log'))
        self.assertIn(f'launches were failing on {self.bad}', self.read('notifications'))
        for _ in range(2):
            self.run_launcher()
            self.assertEqual(self.git('rev-parse', 'HEAD'), self.good)
        self.assertFalse((self.home / 'gh_calls').exists())
        self.assertEqual(len(self.read('notifications').splitlines()), 1)
        self.git('checkout', '-q', 'main')
        fixed = self.commit('fixed')
        self.git('push', '-q', 'origin', 'main')
        self.git('checkout', '-q', '--detach', self.good)
        self.run_launcher()
        self.assertEqual(self.git('rev-parse', 'HEAD'), fixed)
        self.assertIn(f'updated {self.good} -> {fixed}', self.read('logs/update.log'))

    def test_two_crashes_record_bad_sha_and_prevent_reupdate(self):
        self.run_launcher(1)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.bad)
        self.assertFalse((self.home / 'bad_sha').exists())
        self.run_launcher(1)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.good)
        self.assertEqual(self.read('bad_sha'), self.bad)
        self.run_launcher()
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.good)
        self.assertEqual(len(self.read('notifications').splitlines()), 1)

    def test_ci_and_tests_still_gate_updates(self):
        self.git('checkout', '-q', '--detach', self.good)
        self.run_launcher(CI_RESULT='wait')
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.good)
        self.run_launcher(TEST_RC='1')
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.good)
        self.run_launcher()
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.bad)

    def test_missing_or_invalid_rollback_target_does_not_claim_success(self):
        for target in ('', 'not-a-commit'):
            with self.subTest(target=target):
                (self.home / 'launch_ok').write_text(target)
                self.run_launcher(3)
                self.assertEqual(self.git('rev-parse', 'HEAD'), self.bad)
                self.assertFalse((self.home / 'bad_sha').exists())
                self.assertFalse((self.home / 'notifications').exists())
