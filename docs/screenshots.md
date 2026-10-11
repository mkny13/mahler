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

Command shape, from the isolated checkout (profile must be new/empty). The bypass
secret is **not** shown here: in production the conductor (not an agent) reads the
owner-managed file named by project policy `screenshot_bypass_secret_file`
(`~/.mahler/secrets/groundwork-vercel-bypass`, mode 0600) and passes its value only to
the capture subprocess as `MAHLER_SCREENSHOT_BYPASS_SECRET`. Agents must never read
that file or paste its value into a shell command, log or issue.

```bash
MAHLER_SCREENSHOT_URL=https://groundwork-j9sfxpl5z-mkny13s-projects.vercel.app \
MAHLER_SCREENSHOT_SHA=3d5916ab07b15b13e464389ac30d93885e99b40e \
MAHLER_SCREENSHOT_DIR=/tmp/live-output \
MAHLER_SCREENSHOT_PROFILE_DIR=/tmp/live-profile \
node examples/screenshots/groundwork.cjs   # MAHLER_SCREENSHOT_BYPASS_SECRET supplied by the conductor
```

`examples/screenshots/groundwork.cjs` is a standalone reference/smoke. It is not an
instruction to load PR-controlled code or dependencies in the production trusted
capture runtime (DESIGN D11); production capture runs only the operator-configured
installed tool.

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

## Historical nature and limits of the live proof

The live capture above was run once on 2026-10-09 UTC by the #727 builder and is
recorded here, not re-run for #942 (no fresh protected deployment, Vercel login or
secret read was performed). The temporary output (`/tmp/live-output`) has since
expired, so the PNG cannot be re-inspected. Provenance is this document plus merged
PR #885, which checked the ROADMAP Phase 6 item after this successful capture; the
earlier attempt blocked by Vercel protection did not satisfy the criterion. The
SHA/deployment/manifest figures above are as recorded by that builder; they are not
independently re-verifiable from GitHub beyond the deployment record.

## #711 completion-criteria evidence matrix (verified for #942)

Tested revision: `f117c13bf4ca60f4881b1fdd77ad308d4dc65b79` (main). Fresh results at
that revision, all exit 0: `python3 -m mahler.repo_guard`;
`python3 -m unittest discover -s tests` (2672 tests, OK, 1 skipped);
`python3 tests/run_random.py 711` (seed 711, OK); `python3 -X dev tests/run_strict.py`
(2672 tests, OK, 1 skipped). The one skip is the opt-in real-browser
`ScreenshotBrowserSmokeTests` (needs `MAHLER_SCREENSHOT_SMOKE_CWD`); it was not re-run
and is not counted as a new pass; its only pass is the 2026-10-09 record above.

Child merges (all PRs MERGED; single check `test` SUCCESS on each PR head):

| Issue | PR | Merge commit | PR head |
|---|---|---|---|
| #723 | [#760](https://github.com/mkny13/mahler/pull/760) | cdbe3aa | d74c12b |
| #724 | [#788](https://github.com/mkny13/mahler/pull/788) | 63564c8 | dd61db9 |
| #725 | [#792](https://github.com/mkny13/mahler/pull/792) | dab69f8 | 63afddb |
| #726 | [#874](https://github.com/mkny13/mahler/pull/874) | 3acbad6 | 0bfe497 |
| #727 | [#885](https://github.com/mkny13/mahler/pull/885) | 5beb597 | 76d9142 |

| # | #711 criterion | Evidence | Verdict |
|---|---|---|---|
| 1 | Five children merged, named tests passing | Table above; `test` check SUCCESS on each head; full suite fresh-green at f117c13 | Supported |
| 2 | Disabled does no work; fresh headless profile; validated current-head manifest, ≤10 PNGs | `CaptureTests.test_disabled`, `test_success_once_and_new_head`, `test_worktree_requires_exact_head_and_build_role`, `test_project_script_and_symlink_are_never_executed`; `ScreenshotTests.test_total_bound_and_exact_limits`, `test_invalid_manifest_values`, `test_malformed_and_oversized_manifest`; `test_config.py::test_screenshot_defaults_inheritance_and_disable`; `ScreenshotExampleTests.test_existing_profile_rejected`; real-browser fresh-profile check only in the historical smoke | Supported (browser-profile claim for real Chromium is historical) |
| 3 | Opted-in non-personal: PR links + review context; absent/false: zero uploads, console only | `DeliveryTests.test_privacy_not_exactly_true_leaks_nothing`, `test_opted_in_publishes_immutable_links_and_reviewer_context`, `test_disabled_or_dry_run_does_nothing`; `TestScreenshotImages.test_png_private_headers_and_missing_file` | Supported (Groundwork itself stays `screenshot_preview_non_personal=false`; public delivery proven with synthetic fixture only) |
| 4 | One/ten-image phone/desktop fixtures; wrong-head/privacy/path rejection | `ShipmentScreenshotTests.test_one_and_ten_private_images_on_both_layouts`, `test_superseded_head_never_appears`, `test_failed_missing_and_malformed_artifacts_are_unavailable`; `DeliveryTests.test_wrong_head_never_publishes_or_comments`; `ScreenshotTests.test_invalid_paths_and_routes`, `test_storage_keys_and_symlinks_cannot_escape`; `TestScreenshotImages.test_unknown_and_traversal_identifiers`, `test_symlink_file_and_directory_escape_and_invalid_png` | Supported |
| 5 | One real Groundwork Preview capture with deployment/head identity; roadmap checked after | "Live preview evidence" above (SHA 3d5916ab…, deployment 6945975382, Preview, `/sign-in`); ROADMAP.md Phase 6 item is `[x]` | Supported as historical record; not re-run, artifacts expired (see limits above) |
| 6 | Capture/upload/comment failures advisory | `test_ship.py::test_capture_failure_still_reaches_review`, `test_delivery_runs_between_capture_and_review_and_never_blocks`; `DeliveryTests.test_upload_failure_is_advisory_generic_and_keeps_local_images`, `test_comment_failure_is_advisory_and_retries_without_reupload`; `CaptureTests.test_unavailable_and_failures_are_terminal` | Supported |

Not verified here: the optional phone check of thumbnail readability and opening the
full image (Mike's preference check, carried from #711/#726).

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
