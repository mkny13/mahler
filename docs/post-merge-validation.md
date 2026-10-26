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
