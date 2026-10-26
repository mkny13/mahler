"""Contract tests for examples/console_journey (mahler#1257): the fixture seed
and the journey driver. Chrome is not launched here; the real-browser journey is
run through `mahler desktop run -- sh examples/console_journey/run.sh`."""

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

JOURNEY = Path(__file__).resolve().parents[1] / "examples" / "console_journey"
REAL_STATE = os.path.expanduser("~/.mahler")


class JourneySeedTests(unittest.TestCase):
    def test_seed_serves_isolated_overview_and_exits_cleanly(self):
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, True)
        env = dict(os.environ, MAHLER_HOME=str(home))
        proc = subprocess.Popen([sys.executable, str(JOURNEY / "seed.py"), "--home", str(home)],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            url = re.match(r"CONSOLE_URL=(\S+)", proc.stdout.readline()).group(1)
            with urllib.request.urlopen(url + "?project=groundwork", timeout=10) as resp:
                page = resp.read().decode()
            for marker in ("How a change ships", "Drive scope: drive.readonly or drive.file?",
                           "Retry batch", "data-project-scope-switcher", "Dependencies"):
                self.assertIn(marker, page)
            self.assertNotIn("Mahler item should not leak", page)
        finally:
            proc.send_signal(signal.SIGINT)
            out, err = proc.communicate(timeout=20)
            proc.stdout.close()
            proc.stderr.close()
        self.assertEqual(proc.returncode, 0, err)
        self.assertTrue((home / "mahler.db").exists())
        config = (home / "config.toml").read_text()
        self.assertEqual(re.findall(r"^\[projects\.(\w+)\]", config, re.M), ["groundwork", "mahler"])
        self.assertIn("total = 8", config)
        # A live daemon writes to the real ~/.mahler, so assert isolation by what the
        # seed reports and creates, not by diffing that directory.
        self.assertNotIn(REAL_STATE, out + err)
        self.assertTrue({"config.toml", "mahler.db"} <= {p.name for p in home.iterdir()})

    def test_seed_refuses_when_mahler_home_differs(self):
        home = Path(tempfile.mkdtemp())
        other = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, True)
        self.addCleanup(shutil.rmtree, other, True)
        proc = subprocess.run([sys.executable, str(JOURNEY / "seed.py"), "--home", str(home)],
                              env=dict(os.environ, MAHLER_HOME=str(other)),
                              capture_output=True, text=True, timeout=30)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("refusing to seed", proc.stderr)
        self.assertEqual(list(home.iterdir()), [])


class JourneyDriverTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_driver_parses_and_uses_only_builtins(self):
        path = JOURNEY / "journey.mjs"
        subprocess.run(["node", "--check", str(path)], check=True, timeout=30)
        imports = re.findall(r"from '([^']+)'", path.read_text())
        self.assertTrue(imports and all(i.startswith("node:") for i in imports), imports)
        self.assertFalse((JOURNEY / "package.json").exists())
        self.assertFalse((JOURNEY / "node_modules").exists())


@unittest.skipUnless(os.environ.get("MAHLER_CONSOLE_JOURNEY_CHROME"),
                     "opt-in real browser: set MAHLER_CONSOLE_JOURNEY_CHROME=1 and run via the desktop gate")
class JourneyBrowserTests(unittest.TestCase):
    def test_journey_passes(self):
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out, True)
        proc = subprocess.run(["sh", str(JOURNEY / "run.sh"), out], capture_output=True, text=True, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
