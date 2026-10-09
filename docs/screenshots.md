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

## Live preview attempt — blocked, not completion evidence

The earlier attempt could not install dependencies. This fix run installed the
project's actual pnpm lockfile successfully and executed the corrected runner.

- Exact source SHA: `3d5916ab07b15b13e464389ac30d93885e99b40e`.
- GitHub deployment ID: `6945975382`, environment `Preview`, status `success`.
- Deployment status API confirms URL:
  `https://groundwork-j9sfxpl5z-mkny13s-projects.vercel.app`.
- Isolated source: GitHub archive of that exact SHA extracted into ignored
  `scratch-727/groundwork` inside this worktree. No other checkout was modified.
- Groundwork source at this SHA establishes the readiness heading:
  `app/(auth)/sign-in/page.tsx` contains H1 `Sign In to Groundwork`.

Exact command, from `scratch-727/groundwork` (the profile must be new/empty):

```bash
MAHLER_SCREENSHOT_URL=https://groundwork-j9sfxpl5z-mkny13s-projects.vercel.app \
MAHLER_SCREENSHOT_SHA=3d5916ab07b15b13e464389ac30d93885e99b40e \
MAHLER_SCREENSHOT_DIR="$PWD/../live-output" \
MAHLER_SCREENSHOT_PROFILE_DIR="$PWD/../live-profile" \
node ../../examples/screenshots/groundwork.cjs
```

Result on 2026-10-09 UTC: exit **1**, `Screenshot capture failed: Navigation
left /sign-in or redirected to unexpected origin`. An unauthenticated HTTP check
also confirmed a 302 redirect leading to `https://vercel.com/login`.
No PNG or manifest survived, and there are no live manifest entries to report.
No login, protection bypass, stored session, form submission or personal route
was used. Browser context closure runs in `finally`, including failures.

**Outstanding:** a successful exact-SHA accessible Preview capture, then the
isolated capture/delivery/console proof and rendered local console card. Keep
`screenshot_preview_non_personal=false`, use temporary config/ledger/artifacts,
and mock GitHub writes for that proof. No live operator state or review verdict
was changed in this attempt. The private live console-card proof cannot be
claimed from the synthetic fixture.

Deployment access is the remaining blocker. ROADMAP Phase 6 stays unchecked:
a protected deployment is not successful app evidence, even when GitHub marks
its deployment successful. This is a tested checkpoint, not issue completion.

## Privacy

- `screenshot_preview_non_personal` is `false` for Groundwork by default.
- Images remain local-only; no PNG or credentials are committed to git.
- Public delivery is proved with a synthetic non-personal fixture through delivery tests.

## Checklist

- [x] Local fixture: example produces `/sign-in` PNG + manifest using project-local Playwright with fresh headless profile
- [x] Adapter cases and real-browser local fixture smoke pass without Groundwork global setup
- [ ] Evidence document records one successful live Preview capture at identified SHA with console card
- [ ] ROADMAP Phase 6 item checked after real evidence
- [x] Repository guard, unit, strict and seeded-random (711) suites pass

Verification on 2026-10-09 UTC: repository guard, normal unittest discovery,
`python3 -X dev tests/run_strict.py`, and `python3 tests/run_random.py 711`
all exited 0. The opt-in real-browser smoke was run separately as recorded above.
