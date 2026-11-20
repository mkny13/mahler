# Post-merge validation

Mahler can validate a shipped merge against the exact commit that was merged.
This page records the bounded contract for `phish-in/couch-tour`; it is an
operator configuration template, not an activation of the check.

## phish-in/couch-tour contract

This project uses the GitHub exact-SHA preview strategy:

- Mahler asks GitHub for the successful deployment of the merge commit's full
  SHA in the exact environment named by `environment`.
- The environment is `Preview`, the non-production GitHub environment. Never
  point this contract at `Production` or another production environment.
- After the preview is available, Mahler runs the project's `[projects.phish-in]
  smoke` command. The command must run the Couch Tour smoke machinery from
  [couch-tour#357](https://github.com/mkny13/couch-tour/issues/357) and produce
  the referenced report artifact, such as
  `smoke-reports/<tag>.md`. The report contains exactly one matching `Tag:`
  line and exactly one `Smoke: PASS` or `Smoke: FAIL` line; a bare or missing
  report is not evidence.
- The complete post-merge check is bounded by `timeout_seconds`; the allowed
  range is 1 through 86400 seconds. The template uses 1800 seconds (30
  minutes), including deployment lookup and smoke execution.

This follows the post-merge foundation from [#748](https://github.com/mkny13/mahler/issues/748).
It is not the live-proof project selected in [#1038](https://github.com/mkny13/mahler/issues/1038),
and it does not activate a live proof. The contract remains disabled until an
operator deliberately changes `enabled` after supplying the real repository
and GitHub account values.

## Operator template

Copy this TOML into the operator configuration and replace the two uppercase
placeholders. The project-level `smoke` command below is the command whose
report artifact is consumed by the post-merge check; adjust its path or
arguments to the checked-out Couch Tour command if needed.

```toml
# [projects.phish-in]
# repo = "OWNER/REPOSITORY"
# gh_account = "ACCOUNT_NAME"
# smoke = "./scripts/smoke/run-smoke.sh --report smoke-reports/${MAHLER_TAG}.md"
#
# [projects.phish-in.post_merge]
# enabled = false
# deploy_strategy = "watch"
# deploy_command = ""
# live_strategy = "github"
# live_command = ""
# environment = "Preview"
# timeout_seconds = 1800
# auto_revert = false
```

`enabled = false` is intentional. The exact-SHA lookup and smoke report are
only eligible to run after the operator enables the contract; no live-proof or
production activation is part of this document.

## Console status (mahler#748)

The "What changed" console shows each shipment's durable `post_merge_checks` row as
**Deploying** (phase, elapsed of timeout), **Live** (exact version live, smoke passed;
artifact link only when it is an http(s) URL) or **Deploy failed** (reason such as
"timed out" plus the single linked remediation bug). A deploy that never shows up before
the deadline is a failure, never a pass. There is no pass/UAT control; the optional Fail
button stays. Summaries, evidence paths and command output never reach the browser.
Tests: `tests/test_console_uat.py` (`PostMergeStatusTests`).

## Parent #740 evidence (#1605)

The #748 merge `c70a0d88fe4dbbc5e93f573d995244357009bc40` is recorded as
successful CI and after the #741 audit commit
`b6ae20e1fccaf91973c1b049552f4d74371deb51` (the merge-base check passed).
The phone and desktop Deploying, Live and Deploy failed states are covered by
`tests/test_console_uat.py::PostMergeStatusTests`.

The recorded live journeys satisfy the parent requirement: Mahler: PASS at
`da2ab9edaf998cccc65c43b77ee5448d0a27cf76`, and Groundwork: PASS (target:
Production) at `83a70594bc246e76b698958d54fd1be050eda900`. Groundwork is the
exactly one of Groundwork/Couch Tour selected for this criterion; Couch Tour
Preview remains disabled and is not counted.

## Live proofs (recorded 2026-10-28)

| Date | Project | Issue/PR | Merge SHA | Evidence reference | Result |
|------|---------|----------|-----------|--------------------|--------|
| 2026-10-28 | mahler (`launch_ok` + version health) | mahler#1281 | `da2ab9edaf998cccc65c43b77ee5448d0a27cf76` | check tag `mahler:da2ab9ed…`, phase `smoke`, "Exact live identity PASS (mahler self-host)" | PASS |
| 2026-11-17 | groundwork (exact-SHA Production check + smoke, owner choice mahler#1038) | groundwork#438 | `83a70594bc246e76b698958d54fd1be050eda900` | check tag `83a70594…`, phase `smoke`, "Exact live identity and smoke PASS", local report under `post-merge/…/smoke/report.md` | PASS |

Both live proofs pass with real merge SHAs (groundwork also passed #437, #432, #428, #426 once #1272 landed), so D11 and the ROADMAP item are marked enforced.

## Seven managed projects

| Project | Strategy | Disposition |
|---------|----------|-------------|
| `mahler` | `live_strategy = "mahler"` | Enabled; one passing live proof above |
| `groundwork` | GitHub exact-SHA + `smoke` | Enabled; passing live proof above |
| `phish-in` (couch-tour) | GitHub exact-SHA `Preview` + smoke report (couch-tour#357) | Feasible; not the proof project; own contract follow-up #1273 |
| `sit-stand-walk` | none | No automatic check: local script/app with no deployed artifact |
| `google-chat-nav` | none | No automatic check: browser extension shipped by hand, no deploy to observe |
| `movebreak` | none | No automatic check: macOS app, GUI-only smoke is screen-locked (D11, D40) |
| `hockey` | none | No automatic check: no deploy target configured in Mahler |
