# Post-merge validation (D11, mahler#748)

The "What changed" console shows each shipment's durable `post_merge_checks` row as
**Deploying** (phase, elapsed of timeout), **Live** (exact version live and smoke passed;
artifact link when it is an http(s) URL) or **Deploy failed** (failure reason such as
"timed out", plus the single linked remediation bug). A deploy that never shows up before
the deadline is a failure, never a pass. No pass/UAT control exists; the optional Fail
button stays. Summaries, evidence paths and command output never reach the browser.
Tests: `tests/test_console_uat.py` (`PostMergeStatusTests`).

## Live proofs — NOT YET RECORDED (tracked in #1038)

Both proofs need a change merged *after* this console change is running on the daemon, in
a project whose operator config enables `[projects.<name>.post_merge]`. As of 2026-10-12
no project has that enabled in `~/.mahler/config.toml` (operator-only, off limits to
build agents), so neither journey could run. Fixture results do not count.

| Date | Project | Issue/PR | Merge SHA | Evidence reference | Result |
|------|---------|----------|-----------|--------------------|--------|
| _pending_ | mahler (`launch_ok` + version health) | | | | |
| _pending_ | exactly one of groundwork / phish-in (couch-tour) (exact-SHA preview + smoke) | | | | |

Owner decision (mahler#1038, 2026-10-21): the preview proof uses **groundwork**. Blockers
before the operator enables config: (1) groundwork merges get only a Vercel *Production*
deployment, which the `github` strategy refuses, so an exact-SHA read-only check or a match
to the PR head's Preview deployment is needed in code; (2) mahler and groundwork have no
`smoke` command, so the smoke phase fails with `smoke_disabled`; (3) the `mahler` strategy
checks the installed app, which self-updates later, so the timeout must cover that delay.
Until those land and a real merge passes, no row below may be filled in.

D11 stays "declared, not enforced" and the ROADMAP item stays open until both rows pass.

## Seven managed projects

| Project | Strategy | Disposition |
|---------|----------|-------------|
| `mahler` | `live_strategy = "mahler"` (exact `launch_ok` + version health) | Feasible; needs operator config enabling it and a self-update-aware timeout (follow-ups #1038, #1272) |
| `groundwork` | `deploy_strategy = "watch"` + GitHub exact-SHA preview + `smoke` | Feasible (Vercel); needs Production-only exact-SHA check, smoke command and operator config (follow-ups #1272, #1038) |
| `phish-in` (couch-tour) | GitHub exact-SHA preview + `smoke` report (couch-tour#357) | Feasible; not the live-proof project (groundwork chosen, #1038); needs its own contract and operator config (follow-up #1273) |
| `sit-stand-walk` | none | No automatic check: local script/app with no deployed artifact |
| `google-chat-nav` | none | No automatic check: browser extension shipped by hand, no deploy to observe |
| `movebreak` | none | No automatic check: macOS app, GUI-only smoke is screen-locked (D11, D40) |
| `hockey` | none | No automatic check: no deploy target configured in Mahler |
