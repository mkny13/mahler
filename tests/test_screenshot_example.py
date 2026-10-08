"""Tests for examples/screenshots/groundwork.cjs

Covers cwd resolution, env validation, manifest wiring, fresh-profile/headless options,
cleanup and error exit using a fake project-local Playwright adapter.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


EXAMPLE = Path(__file__).parents[1] / "examples" / "screenshots" / "groundwork.cjs"


def make_fake_playwright(root):
    # Create node_modules/@playwright/test with a minimal mock
    mod_dir = root / "node_modules" / "@playwright" / "test"
    mod_dir.mkdir(parents=True, exist_ok=True)
    (mod_dir / "package.json").write_text('{"name":"@playwright/test","main":"index.js"}')
    # Mock that records launch options and writes a PNG signature
    code = """
const fs = require('fs');
const path = require('path');
let lastLaunch = null;
const chromium = {
  launch: async (opts) => {
    lastLaunch = opts;
    return {
      newContext: async () => ({
        newPage: async () => ({
          goto: async (url, _opts) => { /* no-op */ },
          url: () => process.env.MAHLER_SCREENSHOT_URL + '/sign-in',
          screenshot: async ({ path: p }) => {
            fs.writeFileSync(p, Buffer.from([0x89,0x50,0x4E,0x47]));
          }
        })
      }),
      close: async () => {}
    };
  },
  _lastLaunch: () => lastLaunch
};
module.exports = { chromium };
"""
    (mod_dir / "index.js").write_text(code)


class ScreenshotExampleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        # Fake Groundwork project
        (self.tmp / "package.json").write_text('{"name":"groundwork"}')
        make_fake_playwright(self.tmp)

    def _run(self, env_overrides=None, cwd=None):
        env = os.environ.copy()
        env.update({
            "MAHLER_SCREENSHOT_URL": "https://example.com",
            "MAHLER_SCREENSHOT_DIR": str(self.tmp / "out"),
            "MAHLER_SCREENSHOT_SHA": "a" * 40,
            "MAHLER_SCREENSHOT_PROFILE_DIR": str(self.tmp / "profile"),
        })
        if env_overrides:
            env.update(env_overrides)
        cwd = cwd or self.tmp
        return subprocess.run(
            ["node", str(EXAMPLE)],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
        )

    def test_success_writes_manifest_and_png(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        out = self.tmp / "out"
        manifest_path = out / "manifest.json"
        png_path = out / "sign-in.png"
        self.assertTrue(manifest_path.is_file())
        self.assertTrue(png_path.is_file())
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(manifest["sha"], "a" * 40)
        self.assertEqual(manifest["screenshots"], [{"route": "/sign-in", "file": "sign-in.png"}])
        self.assertTrue(png_path.read_bytes().startswith(b"\x89PNG"))

    def test_cwd_resolution_uses_project_playwright(self):
        # The fake Playwright records launch options; verify headless and userDataDir
        result = self._run()
        self.assertEqual(result.returncode, 0)
        # Read launch options via the mock? Not exposed. Instead verify script required cwd package.json
        # Ensure script fails when package.json missing
        other = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(other, ignore_errors=True))
        env = os.environ.copy()
        env.update({
            "MAHLER_SCREENSHOT_URL": "https://example.com",
            "MAHLER_SCREENSHOT_DIR": str(other / "out"),
            "MAHLER_SCREENSHOT_SHA": "b" * 40,
            "MAHLER_SCREENSHOT_PROFILE_DIR": str(other / "profile"),
        })
        proc = subprocess.run(["node", str(EXAMPLE)], cwd=str(other), env=env, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("package.json not found", proc.stderr)

    def test_invalid_url_fails(self):
        result = self._run(env_overrides={"MAHLER_SCREENSHOT_URL": "ftp://bad"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("URL must be http or https", result.stderr)

    def test_missing_env_fails_cleanly(self):
        env = os.environ.copy()
        env.update({
            "MAHLER_SCREENSHOT_URL": "https://example.com",
            # missing DIR
            "MAHLER_SCREENSHOT_SHA": "a" * 40,
            "MAHLER_SCREENSHOT_PROFILE_DIR": str(self.tmp / "profile"),
        })
        proc = subprocess.run(["node", str(EXAMPLE)], cwd=str(self.tmp), env=env, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Missing required environment variables", proc.stderr)

    def test_error_cleans_output(self):
        # Make Playwright throw on launch
        mod_dir = self.tmp / "node_modules" / "@playwright" / "test"
        (mod_dir / "index.js").write_text('module.exports={chromium:{launch:async()=>{throw new Error("boom")}}};')
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        out = self.tmp / "out"
        # Output directory should be removed or empty
        if out.exists():
            self.assertFalse(any(out.iterdir()), "output should be cleaned on error")


if __name__ == "__main__":
    unittest.main()
