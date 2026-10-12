# Audit: #776 dependency-PR adoption and Dependabot rollout

- Audited: 2026-10-11, Mahler checkout `a9427f69dd15c46ccbbbd7965ec5840fbec3ad1f` (main).
- Method: GitHub reads with the existing authenticated `gh` (public PR/issue/tree metadata only) plus local test runs. No daemon state, `~/.mahler` or target repository was touched.
- **Follow-up 2026-10-12:** couch-tour gap closed by mkny13/couch-tour#627 (PR #629, merge `e31a8ebb`, 2026-10-12T07:13:51Z, 3 checks SUCCESS). Inspected couch-tour default branch `b0cf8f6a`: four weekly `updates` (gradle `/`, github-actions `/`, npm `/sync`, swift `/macos/Packages/CouchTourKit`) with minor/patch groups. Acceptance: Dependabot PRs #630, #631 (npm `/sync`) and #635 (swift CouchTourKit) opened 2026-10-12T07:15-07:16Z, after the merge. Criteria 4 and 6 are now PASS.
- Verdict key: PASS = evidenced; FAIL = evidenced as not met; UNKNOWN = proof missing.

## Parent criteria (six)

| # | Criterion (#776) | Verdict | Evidence | Remaining action |
|---|---|---|---|---|
| 1 | #777 merged and deployed; tests prove adoption, major routing, restart-safe cap, opt-out, capacity/account isolation, one merge per tick | **PASS** (merged, tested, observed live; the deployed commit pin itself was not read) | mahler PR #817 merged 2026-10-07T18:06:31Z (`448fc4c9`, `test` SUCCESS); PR #868 (`f154f695`, 2026-10-08T19:06:34Z) added the parser repair. Tests run 2026-10-11: `test_ship.py` 257 OK, `test_config.py` 42 OK. Mapping below. Deployment is inferred from conductor merges of bot PRs from 2026-10-08T19:15Z (see criterion 2); `~/.mahler/app` was deliberately not read. | None. |
| 2 | Green low-risk puppy-growth-chart bot PR adopted without human merge; definite major creates one tracked item, not auto-merge | **PASS** | PR #19 "Bump actions/deploy-pages from 4 to 5": head `fa6e2cd3`, merge `bff8153a`, merged 2026-10-09T13:52:45Z by `app/mahler-conductor`, `verify` SUCCESS, only `.github/workflows/deploy.yml` changed. Also #14-#17 merged by the conductor. Major: bot PR #23 (vitest 4.1.11 to 5.0.3, head `8933d3c0`) is CLOSED, never merged. Exactly one marker-linked issue, puppy-growth-chart #33 (`<!-- mahler:dependency-pr:23 -->`, created 2026-10-08T19:15:42Z); fixed by PR #44 (`5d538bcb`). Majors #20, #21 and #22 map to issues #36, #35 and #34 respectively (`<!-- mahler:dependency-pr:20 -->`, `:21`, `:22`); major PR #18 (`@vitejs/plugin-react` 5.2.0 to 6.1.1) is tracked by issue #37 (`<!-- mahler:dependency-pr:18 -->`). | None. |
| 3 | `_unowned_prs` still excludes `dependabot/` and `renovate/` branches | **PASS** | `tests.test_ship.ShipTests.test_young_draft_and_bot_prs_are_left_alone` passes in the 257-test run. | None. |
| 4 | Seven separate rollout PRs, each default branch has an accepted weekly config matching its manifests | **PASS** (follow-up 2026-10-12) | Seven PRs merged (matrix below) and Dependabot opened PRs from every config. couch-tour's gap was fixed by couch-tour#627 / PR #629 (`e31a8ebb`); npm and swift PRs #630, #631, #635 confirm acceptance. mahler (actions-only per goal text), crate-cataloger and movebreak (no other manifests, re-checked 2026-10-12) need no change. | None. |
| 5 | No write to google-chat-nav, sit-stand-walk or another non-personal repo | **PASS, bounded** | Bounded to the seven rollout PR diffs, which touch only mkny13 repos. movebreak #100 also changed `ARCHITECTURE.md` and `scripts/check_security_surface.sh`. This is not a global account audit. | None; do not extend the claim. |
| 6 | #777 and #778 closed by merged implementation work with test and rollout evidence | **PASS** (follow-up 2026-10-12) | #777 closed 2026-10-07T18:06:33Z by PR #817. #778 closed 2026-10-08T19:06:36Z by PR #868 (parser repair); rollout delivery came from separate PRs. The ordering deviation (see below) was settled as a technical decision in the #776 comment (issuecomment-6118359287). | None. |

### Test coverage map

| Concern | Test (`tests/test_ship.py::DependencyPRTests` unless noted) |
|---|---|
| Adoption of patch/minor/actions | `test_patch_minor_and_actions`, `test_each_bot_patch_and_minor_merge`, `test_dependabot_body_without_blank_line_before_details` |
| Refusal | `test_refusals`, `test_renovate_ambiguous_group_and_conflicting_type` |
| Exact head/base/metadata change | `test_changed_exact_head_base_checks_and_metadata`, `test_rebase_once_per_head_across_restart_and_base_movement` |
| Major dedup and scope | `test_major_dedup_and_scope` |
| Restart-safe daily cap | `test_daily_cap_restart_next_day_override_and_idempotence` |
| Opt-out, capacity, dry-run, one per tick | `test_opt_out_capacity_precedence_dry_run_and_one_per_tick` |
| Account isolation, exception isolation | `test_dependency_metadata_uses_account_environment`, `test_account_client_and_project_exception_isolation` |
| Policy config | `tests/test_config.py::test_dependency_pr_policy` |
| Bot exclusion from unowned | `ShipTests.test_young_draft_and_bot_prs_are_left_alone` |

## Rollout matrix (seven rows)

Default branch SHAs were read on 2026-10-11. GitHub acceptance means Dependabot opened PRs attributable to that config (a readable YAML alone would not count).

| Repo | Issue / PR | Merge SHA, time (UTC) | Config on default branch | Verification and GitHub acceptance |
|---|---|---|---|---|
| mahler | #869 / #881 | `8217d5f0`, 2026-10-09T01:00:48Z | github-actions, weekly, minor/patch group; majors ungrouped. Manifests: workflows, `sdk_runner/pyproject.toml` (out of scope; actions-only per goal). Default `a9427f69`. | Pre-handoff (issue #869 comment): repo_guard, 2367 unit tests and strict runner passed. PR `test` SUCCESS. Accepted: #882 and #883 opened 01:01Z, merged by the conductor. |
| groundwork | #208 / #209 | `8bbb7b87`, 2026-10-08T18:40:39Z | npm + actions, weekly, minor/patch groups. `package.json`, workflows. Default `43c863c6`. | Pre-handoff (#208 comment): `pnpm typecheck && pnpm lint && pnpm test` passed (88 files, 956 tests). PR `checks`, `cleanup` SUCCESS. Accepted: #210-#212 opened 18:41Z. |
| couch-tour | #576 / #577 | `9c1a4515`, 2026-10-08T18:40:51Z | gradle + actions, weekly, `non-major` groups. Follow-up: couch-tour#627 / PR #629 (`e31a8ebb`, 2026-10-12T07:13:51Z) added npm `/sync` and swift `/macos/Packages/CouchTourKit`, four ecosystems total. Default `b0cf8f6a` (2026-10-12); originally `2bcde215`. | Pre-handoff (#576 comment): Android `testDebugUnitTest` BUILD SUCCESSFUL; `swift test` 572 tests, 0 failures. PR `mockup-data`, `test`, `workflow-permissions` SUCCESS. Accepted: #582-#588 opened; after #629, npm PRs #630, #631 and swift PR #635. |
| movebreak | #99 / #100 | `599e9fc7`, 2026-10-09T11:49:43Z | actions only, weekly, minor/patch. Only workflows found. Default `70809486`. | Pre-handoff (#99 comment): `./scripts/build_app.sh` passed. CI first hit an unrelated intermittent file-descriptor self-test failure (7 to 8) and was retried; final PR `verify` SUCCESS. Accepted: #101, #102 opened 11:50Z (still open). |
| hockey-draft-copilot | #88 / #89 | `b85c433c`, 2026-10-08T18:49:52Z | npm + actions, weekly, `non-major` groups. Default `9ce3e890`. | Pre-handoff (#88 comment): `npm test` passed (7 suites). PR `test` SUCCESS. Accepted: #90-#94 opened 18:50Z. |
| mental-jukebox | #20 / #21 | `976be3e9`, 2026-10-08T18:57:04Z | pip + actions, weekly, minor/patch. `requirements.txt`. Default `7e0cfd29`. | Pre-handoff (#20 comment): `scripts/verify.sh full` passed. PR `verify` SUCCESS. Accepted: #22-#25 opened 18:58Z. |
| crate-cataloger | #19 / #20 | `e46b3a0a`, 2026-10-08T18:53:33Z | actions only, weekly, minor/patch. Only workflow found. Default `e7007008`. | Pre-handoff (#19 comment): `python3 -m py_compile crate.py` and `python3 -m unittest discover -s tests` (14 tests) passed. PR `test` SUCCESS. Accepted: #21, #22 opened 18:54Z. |

Pre-handoff verification results are taken from the agents' reports in each rollout issue's comments (agent-reported, not re-run here). All six external issues are CLOSED.

## Ordering (#778 instruction: parser fix first, then per-repo rollout; #777 live adoption before enabling more repos)

**Verdict: deviation, historical.**
- Parser repair PR #868 merged 2026-10-08T19:06:34Z. Five of the six external rollouts merged earlier (18:40-18:57Z on 2026-10-08), before the fix. Mahler's rollout (#881) was on 2026-10-09T01:00Z and movebreak's on 2026-10-09T11:49Z.
- First conductor merge of a bot PR in any repo: groundwork #212 at 2026-10-08T19:15:14Z. First on puppy-growth-chart: #19 at 2026-10-09T13:52:45Z. All seven rollouts preceded the puppy-growth-chart adoption.
- Mahler PR #881 also reintroduced a configuration that #868 had removed as premature.
- Nothing was recreated and the work is not undone. The deviation is recorded and not treated as satisfied.

## Summary

PASS: 1, 2, 3, 4, 5 (bounded), 6 (follow-up 2026-10-12; ordering deviation is a settled historical technical decision). No criteria remain open.
