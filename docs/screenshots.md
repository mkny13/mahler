# Screenshot capture evidence

This document records the Groundwork screenshot example and end-to-end preview evidence for Mahler issue #727.

## Example implementation

- File: `examples/screenshots/groundwork.cjs`
- Purpose: standalone Node CommonJS capture using Groundwork’s local Playwright toolchain.
- Invocation requirements:
  - cwd = Groundwork exact-head checkout
  - Environment variables:
    - `MAHLER_SCREENSHOT_URL` – preview URL (must be http/https)
    - `MAHLER_SCREENSHOT_DIR` – output directory
    - `MAHLER_SCREENSHOT_SHA` – 40-char head SHA
    - `MAHLER_SCREENSHOT_PROFILE_DIR` – fresh temporary profile
  - Resolves `@playwright/test` via `createRequire` against `cwd/package.json`
  - Launches headless Chromium with `--disable-extensions`, fresh `userDataDir`, viewport 390×844
  - Visits only `/sign-in`, validates origin/path, writes `sign-in.png` and `manifest.json`
  - Manifest contract:
    ```json
    { "version":1, "sha":"<head>", "screenshots":[{"route":"/sign-in","file":"sign-in.png"}] }
    ```
  - Closes browser in `finally`, fails cleanly on navigation/browser errors.

Run command (example):
```bash
cd /path/to/groundwork-checkout
MAHLER_SCREENSHOT_URL=https://<preview>/ \
MAHLER_SCREENSHOT_DIR=/tmp/mahler-screenshot \
MAHLER_SCREENSHOT_SHA=<40-char-sha> \
MAHLER_SCREENSHOT_PROFILE_DIR=/tmp/mahler-profile \
node ../../mahler/examples/screenshots/groundwork.cjs
```

## Tests

- `tests/test_screenshot_example.py` provides deterministic adapter tests with a fake project-local Playwright mock.
- Covers cwd resolution, input/manifest wiring, fresh-profile/headless options, cleanup and error exit.
- A real-browser smoke using a disposable local HTTP sign-in fixture is documented below; it is a harness test, not the required live-preview proof.

## Local fixture smoke

A minimal HTTP server can be used to verify the example without a database:

```bash
python3 -m http.server 8000 &
# serve a page at /sign-in
MAHLER_SCREENSHOT_URL=http://localhost:8000 \
MAHLER_SCREENSHOT_DIR=/tmp/smoke \
MAHLER_SCREENSHOT_SHA=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
MAHLER_SCREENSHOT_PROFILE_DIR=/tmp/profile-smoke \
node examples/screenshots/groundwork.cjs
```

The harness must not import Groundwork `playwright.config.ts` or global-setup.

## Live preview evidence

**Prerequisite:** Groundwork preview deployment access and a successful capture using the example.

**Recorded evidence (to be filled after successful capture):**
- Deployment head SHA: *<sha>*
- Preview URL: *<url>*
- Command executed: *<exact command>*
- Manifest entries: *<json>*
- Console card check: *<screenshot_preview_non_personal mode, local card visible>*
- Privacy mode: `screenshot_preview_non_personal=false` for real Groundwork captures unless an existing project declaration permits publication.

> If live capture is unavailable/protected, record the failure honestly. An unavailable/protected page is not completion evidence. The ROADMAP Phase 6 screenshot item must remain unchecked until real preview evidence exists.

Current status: **pending live preview capture**. No live capture attempted yet; the runner and harness are committed as a checkpoint.

## Privacy

- `screenshot_preview_non_personal` is `false` for Groundwork by default.
- Images remain local-only; no PNG or credentials are committed to git.
- Public delivery is proved with a synthetic non-personal fixture through delivery tests.

## Checklist

- [ ] Example produces `/sign-in` PNG + manifest using project-local Playwright with fresh headless profile
- [ ] Adapter cases and real-browser local fixture smoke pass without Groundwork global setup
- [ ] Evidence document records one successful live Preview capture at identified SHA with console card
- [ ] ROADMAP Phase 6 item checked after real evidence
- [ ] Repository guard, unit, strict and random suites pass
