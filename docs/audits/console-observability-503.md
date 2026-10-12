# Parent #503 console observability — evidence report (#934)

- Verified source SHA: `f129eeade83bea95533d2f571ca174bda5ffbe60` (base of this branch)
- Date: 2026-10-12
- Python 3.14.3; Node v25.6.0 (`/opt/homebrew/bin/node`). Node is an existing test tool only; production code stays standard-library-only.
- Children (all CLOSED, confirmed via `gh`): #505 (PR #512), #506 (#513), #507 (#514), #508 (#515), #509 (#518).
- Method: repository code, temporary fixtures, loopback servers. No production ledger/logs/phone session used.

## Commands run (observed at the SHA above)

| Command | Exit | Result |
|---|---|---|
| `python3 -m unittest tests.test_serve tests.test_cli_events tests.test_console_state tests.test_console tests.test_janitor` (exact command from #934) | **1 (FAILED)** | 108 tests ran, 6 errors, 0 skips: 4 `TestScreenshotImages` setUp errors (`No module named 'test_console_uat'`) and 2 module import errors (`test_console_state`, `test_console`: `No module named 'console_snapshot'`). Not a pass. |
| `PYTHONPATH=tests python3 -m unittest -v tests.test_serve tests.test_cli_events tests.test_console_state tests.test_console tests.test_janitor` | 0 | Substituted invocation (not the issue's exact command): 343 tests, OK, 0 skips |
| `python3 -m mahler.repo_guard` | 0 | OK |
| `python3 -m unittest discover -s tests` | 0 | 2716 tests, OK, 1 skip |
| `python3 tests/run_random.py 934` | 0 | seed 934, 2716 tests, OK, 1 skip |
| `python3 -X dev tests/run_strict.py` | 0 | 2716 tests, OK, 1 skip |

Notes:
- The focused command as written in the issue (no `PYTHONPATH`) fails with `ModuleNotFoundError` (`test_console_uat`, `console_snapshot`): the tests import sibling modules from `tests/`. Discovery (`-s tests`) sets the path. It passes with `PYTHONPATH=tests`, but that is a different command: the exact focused command **fails (exit 1) and is not represented as passed**. Reproduce: run it from the repo root. Proposed follow-up: make the tests import-safe without `PYTHONPATH` (e.g. a `tests/__init__.py`/sys.path shim) or change the documented command to `PYTHONPATH=tests`. The cause is a test-harness import quirk, not a product defect.
- The single skip in the full gates is `test_screenshot_example.ScreenshotBrowserSmokeTests.test_real_browser_capture_and_protection_rejection` (opt-in real browser, `MAHLER_SCREENSHOT_SMOKE_CWD`). It is unrelated to #503.
- `tests.test_serve.TestClientLog.test_browser_reports_without_waiting_for_telemetry` **executed with Node and passed** (not skipped).
- Results are observed in this run, not historical child-run results.

## Evidence matrix

| # | Parent criterion | Evidence | Verdict |
|---|---|---|---|
| 1 | #505 request log: ISO timestamp, method, path, status, size, duration; POST logs before blocking work | Source inspection: `mahler/serve.py` `_Handler.do_POST` calls `self.log_message("start")` first, before the action lookup/lock; `_Handler.log_message` writes the line. Executable tests: Tests: `tests.test_serve.TestRequestLogging` (start visible during lock wait and sleeping action; GET success/404 sizes; broken logging does not fail the request) — all pass. | Supported |
| 2 | #506 slow lock wait/action → `console_slow_request`, no change to success/failure | Source inspection: `mahler/serve.py` `_Handler.do_POST` (timing the lock wait and action) calls `_Handler._record_slow_request`, which emits `console_slow_request` with `path`, `wait_ms`, `action_ms` when `max(wait, action) > slow_request_ms` (default 3000), inside a try/except that swallows failures, after the action. Executable tests: `tests.test_serve.TestSlowRequests` (lock wait over threshold, action over threshold, event-write failure does not fail request) — pass. | Supported |
| 3 | #507 browser timeout/network/parse/refused → redacted, rate-limited `console_client_error` without the action lock | Source inspection: `mahler/serve.py` `_Handler.do_POST` (the `client_log` branch, gated by `_ClientLogLimiter.allow`, always answering 204 without the action lock) and `mahler/console/actions.py` `client_log` (redaction, writes the event). Executable tests: `tests.test_serve.TestClientLog` — redacted detail, malformed discarded, write failure → 204, auth, rate cap/window reset, per-IP budgets, `test_does_not_wait_for_action_lock_or_load_config`, and the Node browser test covering timeout, network, refused and non-JSON modes. Node test **ran** and passed. Caveat: the browser test uses a mocked `fetch`; the endpoint tests use a real loopback server. No live phone end-to-end trial was performed. | Supported (automated); live-phone check not performed |
| 4 | #508 `mahler events --since 1h --kind console_` and recent-problems view | Tests: `tests.test_cli_events` (since/kind/project/count filters, bounded output), `tests.test_console_state.RecentProblemsTests`, `tests.test_console.RecentProblemsRenderingTests` (desktop render, phone omission) — pass. Source: `cmd_events` in `mahler/cli.py`. | Supported (automated); desktop/phone usability remains an owner check |
| 5 | #509 `serve.err.log` cannot grow without the configured size and retention bounds | Source: `janitor.rotate_serve_log`, called from the once-per-local-day janitor pass (`_maybe_run`), failure caught and reported without stopping the sweep. Copies the live file to a numbered archive when it exceeds `serve.log_max_bytes` (default 5 MiB), truncates the same inode in place, prunes to `serve.log_keep` archives (default 5). Tests: `tests.test_janitor.ServeLogRotationTests` and `BehaviorTests` rotation-failure/missing-log cases — pass. | **Qualified** |
| 6 | Full unit and randomized suites pass, stdlib only | Gates above all exit 0. No non-stdlib dependency introduced; Node is used only by an existing test. | Supported |

### #509 qualification
Rotation is a **daily, best-effort copy-and-truncate**. Between sweeps the live log, and the archives, can exceed the threshold; a copy/truncate failure is logged and the sweep continues. The parent's wording ("cannot grow without bounds") is satisfied in the sense of bounded long-run retention (archive count and per-sweep truncation), but a **continuous hard size cap is not established**. Proposed bounded follow-up, only if the owner wants a hard cap: size check on write (e.g. `RotatingFileHandler`-style) in `serve.py`. No follow-up is needed for the accepted #503 plan, which specified daily rotation.

## Outstanding / not performed
- Parent #503's two human checks remain open and are neither performed nor waived: phone-over-Tailscale usefulness of a recent-problem entry; desktop recent-problems view compactness and one-click access (DESIGN D27).
- No live-phone or real-browser end-to-end run of `console.js`.
- No failed gates; no unsupported criteria.
