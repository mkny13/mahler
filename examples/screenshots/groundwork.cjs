#!/usr/bin/env node
// Mahler Groundwork screenshot example
// Invoked with Groundwork checkout as cwd. Resolves @playwright/test from that project.
// Environment:
//   MAHLER_SCREENSHOT_URL   – preview URL to capture
//   MAHLER_SCREENSHOT_DIR   – output directory for PNG and manifest
//   MAHLER_SCREENSHOT_SHA   – head SHA for manifest
//   MAHLER_SCREENSHOT_PR    – PR number (unused here, for traceability)
//   MAHLER_SCREENSHOT_PROFILE_DIR – temporary profile directory
//
// Contract: write <dir>/sign-in.png and <dir>/manifest.json
// Manifest: { version:1, sha:"...", screenshots:[{route:"/sign-in", file:"sign-in.png"}] }

const { createRequire } = require('module');
const path = require('path');
const fs = require('fs');
const { URL } = require('url');

function fail(msg) {
  console.error(msg);
  process.exit(1);
}

function requireFromCwd() {
  const cwd = process.cwd();
  const pkgPath = path.join(cwd, 'package.json');
  if (!fs.existsSync(pkgPath)) {
    fail('package.json not found in cwd; run from Groundwork checkout');
  }
  return createRequire(pkgPath);
}

const urlStr = process.env.MAHLER_SCREENSHOT_URL;
const outDir = process.env.MAHLER_SCREENSHOT_DIR;
const sha = process.env.MAHLER_SCREENSHOT_SHA;
const profileDir = process.env.MAHLER_SCREENSHOT_PROFILE_DIR;

if (!urlStr || !outDir || !sha || !profileDir) {
  fail('Missing required environment variables');
}

let urlObj;
try {
  urlObj = new URL(urlStr);
  if (!/^https?:$/.test(urlObj.protocol)) fail('URL must be http or https');
} catch {
  fail('Invalid MAHLER_SCREENSHOT_URL');
}

if (!/^[0-9a-f]{40}$/.test(sha)) {
  fail('Invalid SHA');
}

// Ensure output directory exists
try {
  fs.mkdirSync(outDir, { recursive: true });
} catch {
  fail('Cannot create output directory');
}

const requireCwd = requireFromCwd();
let chromium;
try {
  // Resolve Playwright from the Groundwork project
  const playwright = requireCwd('@playwright/test');
  // @playwright/test re-exports chromium via playwright.chromium
  chromium = playwright.chromium || requireCwd('@playwright/chromium');
} catch (e) {
  fail('Could not resolve @playwright/test from cwd: ' + e.message);
}

(async () => {
  let browser;
  try {
    browser = await chromium.launch({
      headless: true,
      args: ['--disable-extensions', '--no-sandbox', '--disable-dev-shm-usage'],
      userDataDir: profileDir,
    });
    const context = await browser.newContext({
      viewport: { width: 390, height: 844 },
      userAgent: 'Mahler Screenshot Bot',
    });
    const page = await context.newPage();

    // Validate route: only allow /sign-in
    const targetUrl = new URL('/sign-in', urlObj);
    // Preserve origin, avoid redirects to other paths
    await page.goto(targetUrl.toString(), { waitUntil: 'domcontentloaded', timeout: 30000 });

    // Treat unexpected redirects / protection pages as unavailable
    const finalUrl = new URL(page.url());
    if (finalUrl.origin !== urlObj.origin || !finalUrl.pathname.startsWith('/sign-in')) {
      fail('Navigation left /sign-in or redirected to unexpected origin');
    }

    const pngPath = path.join(outDir, 'sign-in.png');
    await page.screenshot({ path: pngPath, fullPage: false });

    const manifest = {
      version: 1,
      sha,
      screenshots: [{ route: '/sign-in', file: 'sign-in.png' }],
    };
    const manifestPath = path.join(outDir, 'manifest.json');
    fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2), 'utf8');

  } catch (err) {
    // Clean up partial output on error
    try { fs.rmSync(outDir, { recursive: true, force: true }); } catch {}
    console.error('Screenshot capture failed:', err.message);
    process.exit(1);
  } finally {
    if (browser) {
      try { await browser.close(); } catch {}
    }
  }
})();
