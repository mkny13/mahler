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
  - Launches headless Chromium with `--disable-extensions`, fresh profile via `launchPersistentContext(profileDir, options)`, viewport 390×844
  - Visits only `/sign-in`, validates exact origin/path, successful HTTP status and visible exact H1 `Sign In to Groundwork`, writes `sign-in.png` and `manifest.json`
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

The committed opt-in `ScreenshotBrowserSmokeTests` starts and stops its own
loopback HTTP server and uses a temporary output/profile directory. It checks the
real PNG signature and 390×844 dimensions, the complete matching-head manifest,
and that Chromium populated the supplied profile. A second capture serves a
protection heading at the same `/sign-in` URL and must fail without artifacts.
No Groundwork config, global setup, application server or database is invoked.

From the Mahler worktree, with an isolated Groundwork source checkout:

```bash
(cd scratch-727/groundwork && pnpm install --frozen-lockfile --ignore-scripts)
(cd scratch-727/groundwork && pnpm exec playwright install chromium)
MAHLER_SCREENSHOT_SMOKE_CWD="$PWD/scratch-727/groundwork" \
  python3 -m unittest discover -s tests -p test_screenshot_example.py -v
```

Result on 2026-10-09 UTC: **8 tests passed**, including the real-browser smoke
(18.487 seconds), using Groundwork's locked `@playwright/test` 1.62.1 and Chromium
151.0.7922.34. Without the opt-in environment variable, Python-only CI runs the
seven adapter tests and explicitly skips the real-browser smoke.

## Live preview evidence — successful capture

The earlier attempt failed due to Vercel deployment protection. The owner-provided
bypass secret is now plumbed via `screenshot_bypass_secret_file` and passed to the
capture command through `MAHLER_SCREENSHOT_BYPASS_SECRET`. The example sends
`x-vercel-protection-bypass` and `x-vercel-set-bypass-cookie` headers when the
variable is set, and the secret is never logged, committed or written to manifest.

- Exact source SHA: `3d5916ab07b15b13e464389ac30d93885e99b40e`.
- GitHub deployment ID: `6945975382`, environment `Preview`, status `success`.
- Deployment URL: `https://groundwork-j9sfxpl5z-mkny13s-projects.vercel.app`.
- Isolated source: worktree at `/tmp/groundwork-3d5916` checked out to the exact SHA.
  Project-local Playwright 1.62.1 installed via pnpm, Chromium installed.
- Groundwork source at this SHA establishes the readiness heading:
  `app/(auth)/sign-in/page.tsx` contains H1 `Sign In to Groundwork`.

Exact command, from the isolated checkout (profile must be new/empty):

```bash
MAHLER_SCREENSHOT_URL=https://groundwork-j9sfxpl5z-mkny13s-projects.vercel.app \
MAHLER_SCREENSHOT_SHA=3d5916ab07b15b13e464389ac30d93885e99b40e \
MAHLER_SCREENSHOT_DIR=/tmp/live-output \
MAHLER_SCREENSHOT_PROFILE_DIR=/tmp/live-profile \
MAHLER_SCREENSHOT_BYPASS_SECRET=$(cat ~/.mahler/secrets/groundwork-vercel-bypass) \
node examples/screenshots/groundwork.cjs
```

Result on 2026-10-09 UTC: exit **0**. Capture succeeded.

- Output: `/tmp/live-output/sign-in.png` (24,159 bytes, PNG signature verified, dimensions 390×844).
- Manifest: `/tmp/live-output/manifest.json`
  ```json
  {
    "version": 1,
    "sha": "3d5916ab07b15b13e464389ac30d93885e99b40e",
    "screenshots": [
      { "route": "/sign-in", "file": "sign-in.png" }
    ]
  }
  ```
- Browser context closed in finally, no login performed, no personal routes visited.
- Secret never appears in manifest, logs, argv or output files; `mahler/redact.py`
  masks `MAHLER_SCREENSHOT_BYPASS_SECRET` assignments.

Isolated capture/delivery/console proof was exercised with temporary
config/ledger/artifacts and mocked GitHub writes; `screenshot_preview_non_personal`
remains false, so no public delivery occurred. The rendered local console card
exists in the temporary artifact root and confirms advisory capture state
`success` for the head.

ROADMAP Phase 6 is now checkable after this real preview evidence and the four
prerequisite features.

## Privacy

- `screenshot_preview_non_personal` is `false` for Groundwork by default.
- Images remain local-only; no PNG or credentials are committed to git.
- Public delivery is proved with a synthetic non-personal fixture through delivery tests.

## Checklist

- [x] Local fixture: example produces `/sign-in` PNG + manifest using project-local Playwright with fresh headless profile
- [x] Adapter cases and real-browser local fixture smoke pass without Groundwork global setup
- [x] Evidence document records one successful live Preview capture at identified SHA with console card
- [x] ROADMAP Phase 6 item checked after real evidence
- [x] Repository guard, unit, strict and seeded-random (711) suites pass

Verification on 2026-10-09 UTC: repository guard, normal unittest discovery,
`python3 -X dev tests/run_strict.py`, and `python3 tests/run_random.py 711`
all exited 0. The opt-in real-browser smoke was run separately as recorded above.
