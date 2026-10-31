# Completion evidence for mahler#800

Verified on **2026-10-31**, scanner and test source commit [`4d6453f1a75a05b07fa640b033289fbf235768a7`](https://github.com/mkny13/mahler/commit/4d6453f1a75a05b07fa640b033289fbf235768a7) (containing PR #1492 at [`ee030b07ea8fa69483c614837096b9cc91f59845`](https://github.com/mkny13/mahler/commit/ee030b07ea8fa69483c614837096b9cc91f59845)), Python 3.14.3. Live collection: 2026-10-31T17:18:31.319533+00:00 through 2026-10-31T17:19:17.001308+00:00.

**Result: eight of nine target checks pass; one remains gap (`movebreak#87/branch-protection`). Keep the final #800 criterion unchecked.** All nine original proposals and five named dependents are currently closed. Closure does not establish a passing scan. This report completes #1494’s evidence collection, not #800’s remaining remediation.

This refresh supersedes the report’s 2026-10-26 inventory. With PR #1492 merged, `mental-jukebox#13/tracked-secrets` has cleared to `pass`, while `movebreak#87/branch-protection` remains `gap`. Historical results remain in Git history. No inference about a repository regression is made solely from that difference.

## Parent criteria

| #800 criterion | Evidence / disposition |
|---|---|
| #803 workflow false positives | Supported by merged #833 and named regression tests below. |
| #804 script runners and required checks | Supported by merged #834 and named regression tests below. |
| #805 assets, source constructs and real credentials | Supported by merged #835 and named regression tests below. |
| #806 verified false-positive resolution | Supported by merged #836 and named regression tests below. |
| Repository verification | **Supported:** `python3 -m mahler.repo_guard` exits 0 (clean); focused PR #1492 regressions run 3 tests in 0.285s, OK; isolated test suite runs 3505 tests in 193.905s, OK (skipped=2). |
| Inventory rerun clears all nine and unblocks dependents | **Unchecked:** eight of nine target checks pass; mental-jukebox/tracked-secrets cleared to pass following PR #1492; movebreak/branch-protection remains gap. Dependents are closed; scanner evidence is still incomplete for the parent. |

## Merged fixes and regression coverage

Fresh read-only `GET /repos/mkny13/mahler/pulls/{number}` confirmed `merged=true` for all child PRs and follow-up PR #1492. `git merge-base --is-ancestor <merge-sha> 4d6453f1a75a05b07fa640b033289fbf235768a7` exited 0 for each merge. All named tests are methods of `TestPracticesAudit` in [tests/test_practices_audit.py](../../tests/test_practices_audit.py). The test commands below exercise these tests. No behavior change or new test was needed for this evidence-only task.

| Issue | Merged PR | Merge SHA | Merged at (UTC) |
|---|---|---|---|
| #803 | [#833](https://github.com/mkny13/mahler/pull/833) | `70f5cf40832eaf3434bdff29059fee81836b828f` | 2026-10-07T23:33:17Z |
| #804 | [#834](https://github.com/mkny13/mahler/pull/834) | `c366cef979ac10122119192a0c81e13d9cb91c7a` | 2026-10-07T23:54:25Z |
| #805 | [#835](https://github.com/mkny13/mahler/pull/835) | `86f88628b103fa8e1ac5457e48a6d604d8b68732` | 2026-10-08T00:31:54Z |
| #806 | [#836](https://github.com/mkny13/mahler/pull/836) | `34aa2a86f0ebfe08f88cf5135b9b36fe599dd00e` | 2026-10-08T01:03:45Z |
| #1487 | [#1492](https://github.com/mkny13/mahler/pull/1492) | `ee030b07ea8fa69483c614837096b9cc91f59845` | 2026-10-31T16:24:51Z |

PR #1492 merged with successful `ci / test` check (completed at 2026-10-31T16:15:42Z, conclusion `SUCCESS`).

**#803:**

- `test_workflow_concurrency_expression_passes`
- `test_pr_paths_filters_pass`
- `test_auxiliary_pr_cleanup_workflow_does_not_override_primary_ci`
- `test_step_condition_and_expression_test_execution_are_unknown`

**#804:**

- `test_npm_test_node_script_runner_is_recognized`
- `test_self_test_binary_step_is_recognized`
- `test_protection_passes_with_script_test_job_context`

**#805:**

- `test_tracked_binary_assets_and_clean_text_return_pass`
- `test_drizzle_schema_definitions_return_pass`
- `test_language_type_annotations_return_pass`
- `test_environment_and_config_lookups_return_pass`
- `test_dict_keys_and_mock_fixtures_return_pass`
- `test_actual_credential_token_and_private_key_report_gap_with_redacted_evidence`

**#806:**

- `test_proposal_body_includes_false_positive_resolution_contract`
- `test_documented_decisions_in_decisions_md_resolve_cleanly`
- `test_project_toml_exemption_resolves_finding_cleanly`
- `test_agents_md_exemption_resolves_finding_cleanly`
- `test_audit_rerun_cleanly_reconciles_resolved_proposals`
- `test_proposal_acceptance_names_conductor_check`

**#1492:**

- `test_valid_token_fixture_labels_return_pass`
- `test_valid_prefix_does_not_hide_credential_assignments`
- `test_movebreak_classic_payload_requires_strict_admin_enforcement`

The #803 fixture also uses a primary workflow with a push/main filter. Negative coverage retains unknown for conditional execution and gap for cleanup-only CI. #804 rejects a build-only Node script. #805 asserts real tokens/private keys remain gap without leaking their values. #806 verifies closing the resolved proposal, marker reuse and no duplicate closing comment; current acceptance defers the final scanner check to the conductor. #1492 classifies synthetic `valid-token` and `valid-refresh` fixture labels as placeholders while verifying longer credential assignments still report gap, and verifies that the movebreak classic branch protection payload without strict required checks and admin enforcement reports gap.

## Repository verification

The completed verification executes at commit `4d6453f1a75a05b07fa640b033289fbf235768a7`. Each command runs in this worktree with a fresh temporary `MAHLER_HOME`; tests use their isolated fixtures. In the test subprocess environment only, inherited `MAHLER_*` run identity variables are removed and the inherited `core.hooksPath` override is set to `/dev/null`, preventing the live lease hook from running against fixture repositories. No live ledger is opened. Only Mahler’s own verification commands run; no scanned project test, build or release command runs.

| Command | Exit | Result |
|---|---|---|
| `python3 -m mahler.repo_guard` | 0 | PASS (clean, no output) |
| `python3 -m unittest tests.test_practices_audit.TestPracticesAudit.test_valid_token_fixture_labels_return_pass tests.test_practices_audit.TestPracticesAudit.test_valid_prefix_does_not_hide_credential_assignments tests.test_practices_audit.TestPracticesAudit.test_movebreak_classic_payload_requires_strict_admin_enforcement` | 0 | Ran 3 tests in 0.285s; OK |
| `python3 -m unittest discover -s tests` | 0 | Ran 3505 tests in 193.905s; OK (skipped=2) |

## Inventory and identity boundary

The finite inventory is exactly `config.enabled_projects(config.load())` at collection time: **13 enabled projects**. Global configuration is read only; effective policies are copied and only `path` is replaced for scanning. Every scan uses an isolated snapshot of the remote default-branch SHA below, even where the configured checkout already matches. Shared checkouts are read only for `git rev-parse HEAD`; none is updated.

Each client is created by `gh.project_client(cfg, pol)`. Personal projects use the configured App through `github_app.Installation`, consuming the authorized external key path `~/.mahler/secrets/github-app.pem`; only a temporary token cache is used by redirecting process-local `config.STATE`. Work projects retain account `work` and `GH_CONFIG_DIR=/Users/mike/.config/gh-work`, without a personal App or alternate login. No token or key value is recorded.

Personal snapshots use an isolated shallow fetch of the exact SHA. Work-account snapshots download the exact-SHA archive with `gh api repos/{repo}/tarball/{sha}` through that same client environment, safely extract it and build a temporary Git index (`git add --all --force`) so `git ls-files` enumerates the archive. Temporary snapshots/cache are removed after collection.

| Project / repository | Identity | Default branch | Configured checkout SHA | Scanned remote SHA | Checkout comparison |
|---|---|---|---|---|---|
| mahler / mkny13/mahler | configured App | main | `c1d8b548d7f7325bc02be608fb62945d322cff5a` | `4d6453f1a75a05b07fa640b033289fbf235768a7` | stale/different; scanned remote snapshot |
| groundwork / mkny13/groundwork | configured App | main | `599b55df200275a51ea73a9b87caba29951fc792` | `148b4a00d4993161fbf67da56db2a0447649e419` | stale/different; scanned remote snapshot |
| phish-in / mkny13/couch-tour | configured App | main | `5b5a1e9f811d7234bc76391e01237fe9c9ef982b` | `dd220aac327ea0eae6f4569d1c9d30cf5f6d6a82` | stale/different; scanned remote snapshot |
| sit-stand-walk / makastel_ncstate/sit-stand-walk | account work | main | `5affa4cedf6ee42a51868c57c874b2523e2fa8a4` | `c0a34bd1808527ddd4fd7c35371821e8cbd45c42` | stale/different; scanned remote snapshot |
| google-chat-nav / makastel_ncstate/google-chat-nav | account work | main | `2dff9cea7efe681d184f01952c64ab335fdbd056` | `e03146a5d789503c34dd28957f5f172489b060ed` | stale/different; scanned remote snapshot |
| movebreak / mkny13/movebreak | configured App | main | `cef91550f37791c82ef2caa46a87955c535ff457` | `50779f5ee19e31b6b2c9322a2fe6f9864a678aea` | stale/different; scanned remote snapshot |
| puppy-growth-chart / mkny13/puppy-growth-chart | configured App | main | `553c7d8d431cbab55cfcdc69ce1d5ce85ded8ee3` | `553c7d8d431cbab55cfcdc69ce1d5ce85ded8ee3` | same |
| hockey / mkny13/hockey-draft-copilot | configured App | main | `0418fdfec5aa3cfbd777acf1e40f324ec4d94bd4` | `33eb3f392a8aa403330fd3a3cc848e6099aef3c4` | stale/different; scanned remote snapshot |
| mental-jukebox / mkny13/mental-jukebox | configured App | main | `157af5bccd008d80b46347d94732e84dd140accc` | `8a4c114dc9ebc0668c1b779c96cbc368567ce9d5` | stale/different; scanned remote snapshot |
| ask-gpt-mcp / makastel_ncstate/ask-gpt-mcp | account work | main | `139f266725b57015395a07e0a8c9a618e37da3cb` | `139f266725b57015395a07e0a8c9a618e37da3cb` | same |
| crate-cataloger / mkny13/crate-cataloger | configured App | main | `b0411ef88c3adba7adab42be3399a405c65c3881` | `b6824c077f2c0e38a0f006b03c31bc5658e70997` | stale/different; scanned remote snapshot |
| spotify-overlay / mkny13/spotify-overlay | configured App | main | `45e7b2f999bccb46e54c76a43ae12727d5003a13` | `45e7b2f999bccb46e54c76a43ae12727d5003a13` | same |
| google-workspace-connector / makastel_ncstate/google-workspace-connector | account work | main | `bb3b413934ecfff9442c6a426a46b452032f3042` | `bb3b413934ecfff9442c6a426a46b452032f3042` | same |

## Nine target pairs

Historical closure evidence: [sorting report](https://github.com/mkny13/mahler/issues/949#issuecomment-6105366649). Current states below came from fresh issue API reads, independently of `practices_audit.scan(snapshot_cfg, client)` results. `phish-in` maps to `mkny13/couch-tour`; `hockey` maps to `mkny13/hockey-draft-copilot`.

| Proposal | Check | Current issue state (closed at UTC) | Scanner | Redacted evidence |
|---|---|---|---|---|
| [groundwork#190](https://github.com/mkny13/groundwork/issues/190) | ci-tests | closed (2026-10-08T13:27:11Z) | **pass** | PR CI invokes test commands; test quality is not assessed. [source](https://github.com/mkny13/groundwork/blob/148b4a00d4993161fbf67da56db2a0447649e419/.github/workflows/ci.yml#L56) |
| [groundwork#193](https://github.com/mkny13/groundwork/issues/193) | verify-command | closed (2026-10-08T12:47:08Z) | **pass** | Documented effective verify command is invoked by PR CI. [source](https://github.com/mkny13/groundwork/blob/148b4a00d4993161fbf67da56db2a0447649e419/AGENTS.md#L108) |
| [groundwork#194](https://github.com/mkny13/groundwork/issues/194) | tracked-secrets | closed (2026-10-08T12:31:40Z) | **pass** | Documented architectural decision or exemption accepts tracked-secrets: 2026-10-08 — Practices audit tracked-secrets verification and false positive review (Issue #194) — **Context:** [source](https://github.com/mkny13/groundwork/blob/148b4a00d4993161fbf67da56db2a0447649e419/docs/DECISIONS.md#L19) |
| [phish-in#555](https://github.com/mkny13/couch-tour/issues/555) | ci-tests | closed (2026-10-08T12:25:59Z) | **pass** | PR CI invokes test commands; test quality is not assessed. [source](https://github.com/mkny13/couch-tour/blob/dd220aac327ea0eae6f4569d1c9d30cf5f6d6a82/.github/workflows/checks.yml#L18) |
| [phish-in#558](https://github.com/mkny13/couch-tour/issues/558) | tracked-secrets | closed (2026-10-08T14:21:30Z) | **pass** | Documented architectural decision or exemption accepts tracked-secrets: D339: Practices audit tracked-secrets reviewed false positives (.mahler/secret-reviews.json) (#558) — The Mahler practices audit (mahler#780) flagged heuristic candidates for `tracked-secrets`. A manual audit confirmed that zero real secrets, credentials, or private personal data exist in git-tracked files. All flagged lines fall into clean non-secret categories: [source](https://github.com/mkny13/couch-tour/blob/dd220aac327ea0eae6f4569d1c9d30cf5f6d6a82/DECISIONS.md#L592) |
| [hockey#79](https://github.com/mkny13/hockey-draft-copilot/issues/79) | ci-tests | closed (2026-10-08T11:57:31Z) | **pass** | PR CI invokes test commands; test quality is not assessed. [source](https://github.com/mkny13/hockey-draft-copilot/blob/33eb3f392a8aa403330fd3a3cc848e6099aef3c4/.github/workflows/ci.yml#L19) |
| [hockey#82](https://github.com/mkny13/hockey-draft-copilot/issues/82) | tracked-secrets | closed (2026-10-08T00:46:19Z) | **pass** | No candidates in inspected tracked text; heuristics do not prove absence of credentials or private data. Git history is not inspected. [source](https://github.com/mkny13/hockey-draft-copilot/tree/33eb3f392a8aa403330fd3a3cc848e6099aef3c4) |
| [mental-jukebox#13](https://github.com/mkny13/mental-jukebox/issues/13) | tracked-secrets | closed (2026-10-08T12:15:06Z) | **pass** | No unresolved candidates in inspected tracked text (6 reviewed); heuristics do not prove absence of credentials or private data. Git history is not inspected. [source](https://github.com/mkny13/mental-jukebox/tree/8a4c114dc9ebc0668c1b779c96cbc368567ce9d5) |
| [movebreak#87](https://github.com/mkny13/movebreak/issues/87) | branch-protection | closed (2026-10-07T16:28:03Z) | **gap** | Default-branch protection can be bypassed: strict required checks, administrator enforcement, force pushes disabled, deletion disabled. [source](https://api.github.com/repos/mkny13/movebreak/branches/main/protection) |

Groundwork and phish-in tracked-secrets pass through **pre-existing documented decisions**, as the scanner explicitly reports; neither result means its raw candidate detector found zero candidates. This run added no exemption or review entry. Hockey’s tracked-secrets result reports no candidates in inspected tracked text. Mental-jukebox’s tracked-secrets result now passes cleanly following PR #1492, which classifies the `valid-token` and `valid-refresh` test fixture labels as placeholders, leaving 0 unreviewed candidates alongside the 6 existing entries in `.mahler/secret-reviews.json`. These are heuristic scans, not proof of secret absence or history scans.

### Fresh movebreak endpoint results

- `GET repos/mkny13/movebreak/branches/main/protection` → `HTTP/2.0 200 OK`.
- `GET repos/mkny13/movebreak/rules/branches/main` → `HTTP/2.0 200 OK`.

The owner’s [access confirmation](https://github.com/mkny13/mahler/issues/949#issuecomment-6406873270) is independently reproduced. The current gap is protection semantics, not missing API access.

### Concrete follow-up required

- **mental-jukebox#13 / tracked-secrets:** Resolved by merged PR #1492. Scanner now classifies synthetic valid-token and valid-refresh fixture labels as placeholders while preserving gap detection for longer credential assignments. The check now passes cleanly without modifying repository files or exemptions.
- **movebreak#87 / branch-protection:** Inspect the classic protection and effective rules at the endpoints above. The scanner reports missing effective safeguards for strict required checks, administrator enforcement, force-push prevention and deletion prevention, despite the required `verify` context. Linked technical follow-up is already tracked under `mkny13/movebreak#123` to establish each effective safeguard on the repository. No repository settings were changed here.
- The conductor should track the remaining gap as linked p1 follow-up work under D10; no owner verdict is required for this report. Until `movebreak#87` passes, leave #800’s final criterion unchecked.

## Five named dependents

| Dependent | Current state | Closed at (UTC) |
|---|---|---|
| [groundwork#195](https://github.com/mkny13/groundwork/issues/195) | closed | 2026-10-08T14:54:04Z |
| [hockey#83](https://github.com/mkny13/hockey-draft-copilot/issues/83) | closed | 2026-10-08T12:31:59Z |
| [phish-in#557](https://github.com/mkny13/couch-tour/issues/557) | closed | 2026-10-08T14:02:57Z |
| [phish-in#559](https://github.com/mkny13/couch-tour/issues/559) | closed | 2026-10-08T13:07:49Z |
| [movebreak#85](https://github.com/mkny13/movebreak/issues/85) | closed | 2026-10-07T21:04:26Z |

These current closures show that the five named dependents are no longer open blockers; they do not override the remaining audit gap on movebreak branch protection. No closure, label, requeue or issue-body mutation was performed.

## Complete inventory scan states

P = pass; G = gap; U = unknown. Non-target findings are recorded only, not remediated.

| Project | ci-tests | agent-instructions | mahler-guidance | verify-command | tracked-secrets | branch-protection |
|---|---|---|---|---|---|---|
| mahler | U | P | P | U | G | P |
| groundwork | P | P | P | P | P | G |
| phish-in | P | P | P | P | P | U |
| sit-stand-walk | G | P | P | G | P | U |
| google-chat-nav | P | P | P | P | P | P |
| movebreak | U | P | P | U | P | G |
| puppy-growth-chart | P | P | P | P | P | G |
| hockey | P | P | P | P | P | P |
| mental-jukebox | G | P | P | P | P | U |
| ask-gpt-mcp | G | P | G | G | G | U |
| crate-cataloger | P | G | P | P | P | G |
| spotify-overlay | G | P | P | P | G | U |
| google-workspace-connector | G | P | G | G | G | U |

Non-target gap/unknown reasons (redacted scanner summaries):

- **mahler/ci-tests (unknown):** No proven unconditional PR test execution; workflow semantics need review.
- **mahler/verify-command (unknown):** Unresolved execution precedes verify command.
- **mahler/tracked-secrets (gap):** Suspected credentials require review.
- **groundwork/branch-protection (gap):** Default-branch protection can be bypassed: strict required checks, administrator enforcement, force pushes disabled, deletion disabled.
- **phish-in/branch-protection (unknown):** Cannot establish squash/test-gate compatibility.
- **sit-stand-walk/ci-tests (gap):** No test invocation found in PR CI (a build alone is insufficient).
- **sit-stand-walk/verify-command (gap):** Verify command is absent from literal PR CI commands.
- **sit-stand-walk/branch-protection (unknown):** Default-branch protection metadata is unavailable or unsupported.
- **movebreak/ci-tests (unknown):** No proven unconditional PR test execution; workflow semantics need review.
- **movebreak/verify-command (unknown):** Verify command uses dynamic expansion or evaluation.
- **puppy-growth-chart/branch-protection (gap):** Default-branch protection can be bypassed: strict required checks, administrator enforcement, force pushes disabled, deletion disabled.
- **mental-jukebox/ci-tests (gap):** No test invocation found in PR CI (a build alone is insufficient).
- **mental-jukebox/branch-protection (unknown):** Cannot establish squash/test-gate compatibility.
- **ask-gpt-mcp/ci-tests (gap):** No test invocation found in PR CI (a build alone is insufficient).
- **ask-gpt-mcp/mahler-guidance (gap):** Missing/stale markers: conductor opens/reviews/merges
- **ask-gpt-mcp/verify-command (gap):** Verify command is absent from literal PR CI commands.
- **ask-gpt-mcp/tracked-secrets (gap):** Suspected credentials require review.
- **ask-gpt-mcp/branch-protection (unknown):** Default-branch protection metadata is unavailable or unsupported.
- **crate-cataloger/agent-instructions (gap):** AGENTS.md and CLAUDE.md diverge.
- **crate-cataloger/branch-protection (gap):** Default-branch protection can be bypassed: strict required checks, administrator enforcement, force pushes disabled, deletion disabled.
- **spotify-overlay/ci-tests (gap):** No test invocation found in PR CI (a build alone is insufficient).
- **spotify-overlay/tracked-secrets (gap):** Suspected credentials require review.
- **spotify-overlay/branch-protection (unknown):** Default-branch protection metadata is unavailable or unsupported.
- **google-workspace-connector/ci-tests (gap):** No test invocation found in PR CI (a build alone is insufficient).
- **google-workspace-connector/mahler-guidance (gap):** Missing/stale markers: mahler claim google-workspace-connector#, mahler heartbeat google-workspace-connector#, mahler ship google-workspace-connector#, mahler release google-workspace-connector#, mahler/<N>-short-slug, Fixes #N, STATUS, verify, commit, push, conductor opens/reviews/merges
- **google-workspace-connector/verify-command (gap):** Verify command is absent from documentation and PR CI.
- **google-workspace-connector/tracked-secrets (gap):** Suspected credentials require review.
- **google-workspace-connector/branch-protection (unknown):** Default-branch protection metadata is unavailable or unsupported.

## Reproduction contract

1. Use the named Mahler source SHA and load the current configured inventory once; record the UTC time and effective project identities. Inventory/policies can change, so a future scan is a new observation.
2. Set process-local `config.STATE` to a temporary directory before constructing `gh.project_client` instances. Do not modify live configuration or read/write the ledger.
3. Resolve each repository’s default branch and commit through its own client. Record the configured checkout HEAD, then materialize the exact remote commit in an isolated snapshot using that client’s credentials. Preserve all effective project policy fields except the snapshot path.
4. Call `practices_audit.scan(snapshot_cfg, lambda name: clients[name])`. Save only check states, safe reasons and redacted citations. Never execute scanned repository commands.
5. Read issue state/closed timestamps and merged PR metadata separately; read both movebreak protection endpoints and record HTTP status separately. Re-run `python3 -m mahler.repo_guard` and the focused PR #1492 regression tests with fresh temporary `MAHLER_HOME` directories and the test-only run-identity/hook isolation described above.

## Needs a human to check

Nothing. Remaining work is the concrete technical follow-up for movebreak branch-protection (`mkny13/movebreak#123`), owned and tracked by the conductor.
