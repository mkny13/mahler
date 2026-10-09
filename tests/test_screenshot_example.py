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
const chromium = {
  launchPersistentContext: async (profile, opts) => {
    fs.writeFileSync('launch.json', JSON.stringify({profile, ...opts}));
    return {
      newPage: async () => ({
        goto: async () => ({ok: () => !process.env.FAKE_HTTP_ERROR}),
        url: () => process.env.FAKE_FINAL_URL || process.env.MAHLER_SCREENSHOT_URL + '/sign-in',
        getByRole: (role, options) => ({waitFor: async () => {
          if (role !== 'heading' || options.name !== 'Sign In to Groundwork' ||
              !options.exact || options.level !== 1 || process.env.FAKE_NO_HEADING) {
            throw new Error('Sign-in heading unavailable');
          }
        }}),
        screenshot: async ({path: p}) => fs.writeFileSync(p, Buffer.from([0x89,0x50,0x4E,0x47]))
      }),
      close: async () => fs.writeFileSync('closed', 'yes')
    };
  }
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
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('MAHLER_SCREENSHOT_', 'FAKE_'))}
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
        launch = json.loads((self.tmp / 'launch.json').read_text())
        self.assertEqual(launch['profile'], str(self.tmp / 'profile'))
        self.assertIs(launch['headless'], True)
        self.assertIn('--disable-extensions', launch['args'])
        self.assertEqual(launch['viewport'], {'width': 390, 'height': 844})
        self.assertTrue((self.tmp / 'closed').exists())
        # Ensure script fails when package.json missing
        other = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(other, ignore_errors=True))
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('MAHLER_SCREENSHOT_', 'FAKE_'))}
        env.update({
            "MAHLER_SCREENSHOT_URL": "https://example.com",
            "MAHLER_SCREENSHOT_DIR": str(other / "out"),
            "MAHLER_SCREENSHOT_SHA": "b" * 40,
            "MAHLER_SCREENSHOT_PROFILE_DIR": str(other / "profile"),
        })
        proc = subprocess.run(["node", str(EXAMPLE)], cwd=str(other), env=env, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("package.json not found", proc.stderr)

    def test_unavailable_pages_close_context_and_remove_output(self):
        for overrides in ({'FAKE_NO_HEADING': '1'}, {'FAKE_HTTP_ERROR': '1'},
                          {'FAKE_FINAL_URL': 'https://example.com/sign-in-error'},
                          {'FAKE_FINAL_URL': 'https://vercel.com/login'}):
            with self.subTest(overrides=overrides):
                result = self._run(overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.tmp / 'out').exists())
                self.assertTrue((self.tmp / 'closed').exists())

    def test_existing_profile_rejected(self):
        profile = self.tmp / 'profile'
        profile.mkdir()
        (profile / 'Cookies').write_text('must not read')
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('fresh and empty', result.stderr)
        self.assertFalse((self.tmp / 'launch.json').exists())

    def test_invalid_url_fails(self):
        result = self._run(env_overrides={"MAHLER_SCREENSHOT_URL": "ftp://bad"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("URL must be http or https", result.stderr)

    def test_missing_env_fails_cleanly(self):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('MAHLER_SCREENSHOT_', 'FAKE_'))}
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
        (mod_dir / "index.js").write_text('module.exports={chromium:{launchPersistentContext:async()=>{throw new Error("boom")}}};')
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        out = self.tmp / "out"
        # Output directory should be removed or empty
        if out.exists():
            self.assertFalse(any(out.iterdir()), "output should be cleaned on error")


@unittest.skipUnless(os.environ.get('MAHLER_SCREENSHOT_SMOKE_CWD'),
                     'opt-in real browser: set MAHLER_SCREENSHOT_SMOKE_CWD')
class ScreenshotBrowserSmokeTests(unittest.TestCase):
    """Real project-local Chromium; disposable HTTP fixture, no app or database."""

    def test_real_browser_capture_and_protection_rejection(self):
        import http.server
        import threading

        class Fixture(http.server.BaseHTTPRequestHandler):
            protected = False

            def do_GET(self):
                body = (b'<!doctype html><html lang="en"><title>Fixture</title>'
                        b'<main><h1>' +
                        (b'Deployment protected' if self.protected else
                         b'Sign In to Groundwork') + b'</h1></main></html>')
                self.send_response(200 if self.path == '/sign-in' else 404)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                env = {k: v for k, v in os.environ.items()
                       if not k.startswith('MAHLER_SCREENSHOT_')}
                env.update({
                    'MAHLER_SCREENSHOT_URL': f'http://127.0.0.1:{server.server_port}',
                    'MAHLER_SCREENSHOT_DIR': str(root / 'out'),
                    'MAHLER_SCREENSHOT_SHA': 'a' * 40,
                    'MAHLER_SCREENSHOT_PROFILE_DIR': str(root / 'profile'),
                })
                cwd = Path(os.environ['MAHLER_SCREENSHOT_SMOKE_CWD']).resolve()
                def capture():
                    return subprocess.run(['node', str(EXAMPLE.resolve())], cwd=cwd,
                                          env=env, capture_output=True, text=True, timeout=60)
                result = capture()
                self.assertEqual(result.returncode, 0, result.stderr)
                manifest = json.loads((root / 'out/manifest.json').read_text())
                self.assertEqual(manifest, {'version': 1, 'sha': 'a' * 40,
                                           'screenshots': [{'route': '/sign-in',
                                                            'file': 'sign-in.png'}]})
                png = (root / 'out/sign-in.png').read_bytes()
                self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
                self.assertEqual(int.from_bytes(png[16:20], 'big'), 390)
                self.assertEqual(int.from_bytes(png[20:24], 'big'), 844)
                self.assertTrue(any((root / 'profile').iterdir()))
                Fixture.protected = True
                env['MAHLER_SCREENSHOT_PROFILE_DIR'] = str(root / 'protected-profile')
                result = capture()
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((root / 'out').exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
