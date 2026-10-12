# Audit: #673 completion criteria (review convergence)

- Tested revision: `9458c84931c347ec0b8017662458b775e8996d19` (main, "Verify remaining completion criteria for #707 (#1003)"), run 2026-10-12 UTC, Python 3.14.3.
- Method: read merged source and tests at that revision, ran the named tests and the four repository gates locally. All fixtures are in-memory or temp-dir ledgers; no `~/.mahler` or live state was queried or altered. Historical shipment facts (from GitHub during sorting) are kept separate from checks executed for this report.
- Verdicts: SUPPORTED (a named assertion that passed in this run demonstrates it) or UNSUPPORTED (with gap and reproduction). Issue closure alone was not counted as evidence.

## Merge ancestry (checked with `git merge-base --is-ancestor <sha> HEAD`, all exit 0)

| Child | PR | Merge SHA | Ancestor of tested revision |
|---|---|---|---|
| #680 | #689 | `4665019f40f91374dc2a54247723a43ac6a5d1fc` | yes |
| #678 | #690 | `ae76345863208b5df9c24ace12152a62bed9587e` | yes |
| #679 | #693 | `5f74900fee71b2b74316d38f92b7af773cbea1df` | yes |
| #895 | #899 | `be2bcdef08ced2ad861a39973e3c69d527eebb26` | yes |

## Gate results (executed for this report at the tested revision)

| Command | Exit | Result |
|---|---|---|
| `python3 -m mahler.repo_guard` | 0 | passed |
| `python3 -m unittest discover -s tests` | 0 | Ran 2672 tests, OK (skipped=1) |
| `python3 tests/run_random.py 938` | 0 | seed 938, Ran 2672 tests, OK (skipped=1) |
| `python3 -X dev tests/run_strict.py` | 0 | Ran 2672 tests, OK (skipped=1) |
| Targeted: `python3 -m unittest -v <named tests below>` | 0 | Ran 30 tests, OK |

The one skip is a Node-dependent console browser regression (`skipTest`/`skipUnless` on `node` in `tests/test_console_state.py` and `tests/test_console_selection.py`); it is unrelated to review convergence. Unittest discovery/imports were used, so classes below `test_ship.py`'s `__main__` block are included.

## Owner's 2026-10-09 report and #895

The owner reported that #837 (PR #857), #855 (PR #887) and #727 (PR #885) each failed after 3 `review_rejected` attempts, with last findings being edge cases beyond Done-when. #895 / PR #899 resolved this (DESIGN D11 records the conductor disposition). The earlier #678/#679 fixtures used pre-classified follow-ups; the failure mode was a *blocking* finding arriving in the same file as a previously fixed one.

`TestGreenReviewRounds.test_same_file_incoming_blocker_converges` addresses that directly (executed, passed). `convergence_case` first records blocking `spec` findings on earlier heads, then submits an incoming finding with `severity: blocking` in the same file (`app.py:12` and `app.py:192`), `relation: new-edge-case`, a `fixed` entry, passing acceptance tests and an `outside_tests` note. For thresholds 2 and 3 it asserts: merge is called once; no fix run, review start or design run; one follow-up issue is created; the "Conductor convergence" comment is posted; the stored row keeps `severity=blocking` but `effective_severity=follow-up` and `linked`; the result survives a rebuilt `scheduler.Ctx` without a second issue; and the round window is exactly `threshold`.

Protected-case controls (`test_same_file_protected_controls_and_mixed_review`, passed) each still hand the review to the fix path with no merge and no issue: `unresolved`, `regression`, `acceptance-failure`, `security`, `data-loss`, `behavior`, `different-criterion`, `missing-evidence`, `failed-test`, `unknown-head`, `different-file`, `same-scenario`, and `mixed` (a security blocker alongside the edge case; the edge case text is not forwarded to the fixer). `test_demoted_path_requires_current_green_head_and_threshold` shows red/pending CI, a stale head, a below-threshold count, or a different PR each prevent demotion.

Conclusion: the owner's same-file report is covered by a regression that starts from an incoming blocking finding, not only from pre-classified follow-ups. **Limit:** convergence depends on the reviewer emitting the structured `convergence` evidence; tests prove the conductor's handling, not that a live model will always emit it (`tests.test_recipes.ReviewEvidenceTests.test_convergence_evidence_contract` checks only recipe wording, including that missing evidence does not authorize demotion). No live re-run of #837/#855/#727 was performed (out of scope).

## Criterion matrix (the four unchecked criteria on #673)

Selectors below are `tests.test_ship.<Class>.<test>` unless a module is given.

| # | Criterion | Verdict | Evidence |
|---|---|---|---|
| 1 | "#678 merges: classified follow-ups become linked issues without failing review." | **SUPPORTED** | PR #690. Source: `review.file_followups` (`mahler/review.py`; deterministic `<!-- mahler:review-follow-up:<sha256> -->` marker, remote `issue_by_marker` lookup recovers lost local writes), `review.effective_dispositions`, `ship._review_gate`. Tests (passed): `TestClassifiedReview.test_followups_merge_without_fix_and_retries_preserve_links` (merge without fix; one `create_issue` across retries); `TestClassifiedReview.test_mixed_only_hands_blockers_to_fix_and_retains_followups`; `TestClassifiedReview.test_create_failure_stays_pending_and_retries_without_fix`; `TestClassifiedReview.test_stale_head_and_dry_run_never_file`; `TestGreenReviewRounds.test_demoted_followup_failures_retry_durably` (create/comment failures retry; no duplicate after manual reship); `TestGreenReviewRounds.test_inclusive_threshold_and_different_file_followups_merge` (`threshold+1` issues, "Classified as nonblocking" comment). |
| 2 | "#679 merges: default N=2 convergence proceeds without owner intervention when only follow-ups remain, while substantive blockers retain their gate." | **SUPPORTED** | PR #693 (+ #899). Config: `config.DEFAULTS["defaults"]["review_green_rounds"] = 2`, validated as a positive int (`config.py` ~1214); `tests.test_config.SmokePolicyTests.test_green_review_round_policy` (default 2, defaults/project override, rejects 0, -1, True, 1.5, "2"). Behavior: `TestGreenReviewRounds.test_inclusive_threshold_and_different_file_followups_merge` (N=2 and N=3, inclusive: merge at round N and after, no fix, "Convergence threshold reached" in the review context from round N); `TestGreenReviewRounds.test_same_file_incoming_blocker_converges` (above); `TestGreenReviewRounds.test_new_and_unresolved_blockers_remain_blocking_after_threshold` (security, data-loss, behavior blockers still go to fix after the threshold; `review.same_findings` does not equate findings by location alone); `TestGreenReviewRounds.test_same_file_protected_controls_and_mixed_review`; `TestGreenReviewRounds.test_rounds_survive_restart_retries_and_manual_reship` (alternate reviewer on the same head is one round; PR swap resets the window). |
| 3 | "#680 merges: manual re-ship starts an appropriate fresh attempt cycle and failure diagnostics are accurate." | **SUPPORTED** | PR #689. Source: `cli.cmd_ship` (`mahler/cli.py` ~730-742: for `failed`/`needs_you` items zeroes `attempts`, `setup_fails`, `esc_tier`, `esc_fails`, clears `reviewfix:`/`red:` keys, sets `attempt_cycle:<project>#<n>`, emits an `attempt_cycle` event), `mahler/failures.py` (reads the cycle for diagnostics). Tests: `tests.test_cli_ship.ShipCommandTests.test_terminal_reship_resets_only_retry_budget` (counters reset, new cycle id, event recorded, review/failure history preserved, run rows untouched); `tests.test_cli_ship.ShipCommandTests.test_repeated_active_ship_does_not_replenish_budget`; `ShipTests.test_manual_reship_review_budget_is_fresh_once_and_still_bounded` (fresh once, then attempts 1/2/3 then `failed`; the comment contains "attempt 3 of 3" and the current `cycle=`); `ShipTests.test_same_head_allowance_is_scoped_to_pr_head_and_survives_reship`; `tests.test_setup_failures.SetupFailureTests.test_first_setup_failure_retries_without_burning_an_attempt` and `...test_second_consecutive_setup_failure_needs_you_with_the_log_tail` (setup-failure diagnostics and budget). |
| 4 | "Review, CI, head freshness, and merge gates retain regression coverage." | **SUPPORTED** | See the table below; every gate has at least one passing named test. |

## Safety-gate coverage (existing mechanical tests, passed in this run)

| Gate | Tests |
|---|---|
| Independent review required before merge | `TestGreenReviewRounds.test_red_pending_stale_and_missing_verdicts_never_count`; `ShipTests.test_claude_never_pinned_as_its_own_reviewer` (reviewer selection without deadlock); `tests.test_review.TestEffectiveReviewWithLedger.test_console_and_ship_gate_agree_on_pending_review` and sibling gate tests |
| Red / pending CI | `ShipTests.test_pending_ci_waits_across_ticks`, `ShipTests.test_red_ci_pings_once_and_starts_a_fix_run`, `ShipTests.test_red_ci_gives_up_after_max_attempts`; `TestGreenReviewRounds.test_demoted_path_requires_current_green_head_and_threshold` (FAILURE and PENDING block demotion) |
| Stale review / head | `ShipTests.test_stale_review_verdict_on_a_new_sha_re_reviews`; `tests.test_review.TestEffectiveReviewPure.test_head_replacement_invalidates_pending_and_terminal_verdicts`; `TestClassifiedReview.test_stale_head_and_dry_run_never_file`; `TestGreenReviewRounds.test_new_pr_starts_fresh_convergence_window` |
| Merge / base freshness | `ShipTests.test_reviewed_stale_pr_updates_then_waits_for_ci_and_fresh_base`, `ShipTests.test_second_merge_rechecks_base_after_first_tick`, `ShipTests.test_no_checks_still_requires_freshness`; the merge call passes `--match-head-commit` (`mahler/gh.py` ~832) |
| Follow-up retry / dedup | `TestClassifiedReview.test_followups_merge_without_fix_and_retries_preserve_links`, `TestClassifiedReview.test_create_failure_stays_pending_and_retries_without_fix`, `TestGreenReviewRounds.test_demoted_followup_failures_retry_durably` |
| Bounded manual re-ship | `ShipTests.test_manual_reship_review_budget_is_fresh_once_and_still_bounded`, `tests.test_cli_ship.ShipCommandTests.test_terminal_reship_resets_only_retry_budget`, `...test_repeated_active_ship_does_not_replenish_budget` |

**Coverage limits (reported, not waived):**
- Reproduce any row with `python3 -m unittest -v <selector>` at the tested revision.
- No test drives a live model, so reviewer emission of `convergence` evidence is a recipe contract only.
- Tests use `FakeGH` and in-memory ledgers; real GitHub merge semantics for `--match-head-commit` are not exercised locally, only the argument construction.

## Conclusion

All four criteria are SUPPORTED at `9458c84`, with the limits above. No unsupported criterion, runtime change or new issue is needed. Parent checkbox bookkeeping is left to the conductor.
