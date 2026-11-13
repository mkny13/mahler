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

## Live proofs (recorded 2026-10-28)

| Date | Project | Issue/PR | Merge SHA | Evidence reference | Result |
|------|---------|----------|-----------|--------------------|--------|
| 2026-10-28 | mahler (`launch_ok` + version health) | mahler#1281 | `da2ab9edaf998cccc65c43b77ee5448d0a27cf76` | check tag `mahler:da2ab9ed…`, phase `smoke`, "Exact live identity PASS (mahler self-host)" | PASS |
| _pending_ | groundwork (exact-SHA preview + smoke, owner choice mahler#1038) | | | | Not passed: every groundwork Production check recorded 2026-10-28 (e.g. #305-#314, #325) ended `FAIL` with `timeout` in the `live` phase; #328 still pending |

D11 stays "declared, not enforced" and the ROADMAP item stays open until the groundwork
row passes with a real merge SHA. The timeouts show the Production-only exact-SHA gap
(follow-up #1272) is still open; a timeout is a failure here, not a pass.

## Seven managed projects

| Project | Strategy | Disposition |
|---------|----------|-------------|
| `mahler` | `live_strategy = "mahler"` | Enabled; one passing live proof above |
| `groundwork` | GitHub exact-SHA + `smoke` | Feasible; live proof blocked on Production-only exact-SHA check (#1272) |
| `phish-in` (couch-tour) | GitHub exact-SHA `Preview` + smoke report (couch-tour#357) | Feasible; not the proof project; own contract follow-up #1273 |
| `sit-stand-walk` | none | No automatic check: local script/app with no deployed artifact |
| `google-chat-nav` | none | No automatic check: browser extension shipped by hand, no deploy to observe |
| `movebreak` | none | No automatic check: macOS app, GUI-only smoke is screen-locked (D11, D40) |
| `hockey` | none | No automatic check: no deploy target configured in Mahler |
