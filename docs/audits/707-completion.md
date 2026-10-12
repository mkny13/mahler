# Audit: #707 completion criteria (reviewer context, design role, convergence routing)

- Audited source SHA: `97024f9dae285f16b35f1ebe10a8173e0c7e876a` (main), 2026-10-11, Python 3.14.3.
- Method: read merged source and tests at that SHA; ran targeted tests and the four gates locally with isolated fixtures (in-memory/temp ledgers). No `~/.mahler` or live state touched. One throwaway probe (outside the repo) reproduced the second-design behavior below.
- Verdicts: SUPPORTED (named assertion demonstrates it), COVERAGE GAP (behavior may exist, no assertion), DEFECT (observed behavior contradicts the criterion). Closure, test names and recipe wording alone are not counted as runtime proof.

## Merge identities

| Child | PR | Merge SHA | Merged (UTC) | PR head | CI |
|---|---|---|---|---|---|
| #713 | [#742](https://github.com/mkny13/mahler/pull/742) | `f1db7ab2a1553ce0d8de2544bd6f87357cf18ea0` | 2026-10-06T14:05:10Z | `f52eba9337371fa18beb4181013e11aeec4befe9` | `test` SUCCESS |
| #714 | [#840](https://github.com/mkny13/mahler/pull/840) | `19f6393c6d1d4a7c421d3e0b46f396d037f5c7f9` | 2026-10-08T03:57:18Z | `80aa64ac6784ab99dc0c0473e1dc83a8772c251b` | `test` SUCCESS |
| #715 | [#843](https://github.com/mkny13/mahler/pull/843) | `1016abc3d822efd4e24dc6d1af302478f08110cd` | 2026-10-08T05:20:51Z | `6387c14b9249370e58fefaf858ce4ba2068b28a9` | `test` SUCCESS |

## Criterion matrix (quoting #707's five unchecked criteria)

| # | Criterion | Verdict | Evidence |
|---|---|---|---|
| 1 | "#713 merges with tested default/override reviewer context and the single-user data-loss calibration, without weakening normal-flow data-loss or security blockers." | **SUPPORTED** (prompt contract only for the calibration) | Symbols: `config.DEFAULTS["defaults"]["review_context"]`, config validation (`config.py` ~1207), `review.start_context` (`review.py` ~182-203), `recipes/review.md` ~84. Tests: `tests.test_config.SmokePolicyTests.test_review_context_default_override_and_clear` (default, inherited, project override, `""` clear), `tests.test_config.SmokePolicyTests.test_review_context_rejects_non_strings`; `tests.test_ship.ShipTests.test_review_start_delivers_effective_operating_context_in_prompt` (default/override/empty reach the rendered review prompt, including "Security, normal-flow data loss"); `tests.test_recipes.ReviewOperatingContextTests.test_personal_context_calibrates_loss_without_weakening_blockers` (recipe fragments: mixed-version/multi-user loss is follow-up unless silent unrecoverable normal-flow loss; security and normal-flow loss stay blocking). **Limit:** the calibration is wording in the recipe; no test can show a model classifies correctly. |
| 2 | "#714 merges with a tested plan-only `design` role, `routing.plan` selection, read-only recipe, and durable PR/head-scoped structured result." | **SUPPORTED for role, routing, recipe text, result; read-only is a recipe contract only** | Routing: `tests.test_schedule.DesignRoleTests.test_design_routes_through_plan_but_records_design_identity` (picks `claude-opus` from `routing.plan`, run row `role=design`, `routing_role=plan`). Checkout: `tests.test_runner.PrepareAccountEnvTests.test_design_checks_out_pr_head_without_replaying_and_uses_planning_model` (PR head, no replay, clean status; platform argv uses the `sort` role via `runner.py:426`). Recipe: `tests.test_recipes.DesignRecipeTests` (both tests). Result: `tests.test_finalize.RunTests.test_design_result_persists_by_pr_and_head_without_spending_attempts`, `tests.test_finalize.RunTests.test_stale_or_malformed_design_results_retry_in_verification`, `tests.test_finalize.RunTests.test_design_record_write_is_idempotent`; key `design:<project>#<n>:<pr>:<sha>` (`finalize._design_record_key`). **Read-only is not enforced:** `platforms.claude_argv` runs `--permission-mode bypassPermissions` with only the shared `CLAUDE_DENY` list for the design role; `agy`/`cline` expose no deny list (mahler#77 comments). `finalize._prepare_design` checks that the checkout HEAD equals the expected head, which does not detect uncommitted edits or pushes. |
| 3 | "#715 merges with threshold and convergence tests proving exactly one design pass precedes either one planned fix or deduplicated follow-up filing and merge." | **SUPPORTED** for the shipped same-PR/same-head/evidence-window bound; **COVERAGE GAP** for design-specific restart/new-PR cases (criterion 4) | Selectors: `tests.test_ship.TestGreenReviewRounds.test_before_threshold_blocker_starts_fix_without_design`, `tests.test_ship.TestGreenReviewRounds.test_design_starts_only_at_threshold_after_drift_is_established`, `tests.test_ship.TestGreenReviewRounds.test_threshold_with_disjoint_location_drift_starts_one_design_run_and_no_simultaneous_fix`, `tests.test_ship.TestGreenReviewRounds.test_overlapping_findings_reset_drift_and_missing_locations_inconclusive`, `tests.test_ship.TestGreenReviewRounds.test_planned_fix_consumption_starts_one_fix_and_subsequent_head_reviewed`, `tests.test_ship.TestGreenReviewRounds.test_design_result_followups_files_deduped_issues_and_merges`. Together they show: no design before the threshold, one design run at the threshold with no simultaneous fix, one planned fix consumed, and deduplicated follow-up filing then merge. Dedup key: `ship._maybe_start_design` keys on `design:<project>#<n>:<pr>:<sha>`. **Not a defect:** #715 scopes duplicate prevention to the same PR/head/evidence window, and DESIGN.md says the same; later review evidence on a head changed by the planned fix may open a new design window. An earlier draft of this audit called that a defect and proposed a per-PR cap; that was wrong, since it contradicts the shipped contract, and it is withdrawn. A throwaway probe observing a design on each later head is consistent with that bound. No unsupported claim remains for this row beyond the criterion 4 gaps. |
| 4 | "Repeated ticks, restarts, retries, manual re-ship, stale heads, and new-PR reset behavior have finite mechanical coverage in the child test lists." | **COVERAGE GAP (restart, new-PR for design)** | See lifecycle table. |
| 5 | "Current-head independent review, fresh green CI, follow-up intake, and merge/base safety gates remain required." | **SUPPORTED for the generic path; design-follow-up path only partly traced** | See safety table. |

## Lifecycle submatrix (finite cases)

| Case | Evidence | Verdict |
|---|---|---|
| Repeated ticks | `test_one_design_only_across_ticks_and_restarts` (active design run: no second start; consumed record: no design start). Same in-memory `Ledger(":memory:")` throughout. | SUPPORTED (in-memory repeats) |
| Reopened persistent ledger / reconstructed context | `test_one_design_only_across_ticks_and_restarts` never reopens anything: `setUp` uses `Ledger(":memory:")` and the "restart" is the same ledger object, with the run row deleted by SQL. `test_rounds_survive_restart_retries_and_manual_reship` rebuilds `scheduler.Ctx` over the same in-memory ledger. The only real file-reopen in the area is `test_same_head_pending_survives_restart_capacity_and_failed_launch` (SQLite backup then `Ledger(path)`), which covers the same-head fix allowance, not design. | **COVERAGE GAP:** no test reopens a persisted ledger with a design record, `design-wait`, or `reviewdrift` key. Proposed: reuse the backup/reopen idiom from that test around `design:` + `reviewdrift:` keys and assert no second `start(..., "design")`. |
| Failed launch retry | `test_failed_design_start_retries_without_fix_or_competing_review` (start False: no fix, no review, no record; next gate retries once). | SUPPORTED |
| Duplicate verdict | `test_old_finalization_does_not_replace_new_run`, `test_design_record_write_is_idempotent`; `test_design_result_followups_files_deduped_issues_and_merges` plus `test_followups_merge_without_fix_and_retries_preserve_links` (one `create_issue` across retries). | SUPPORTED |
| Alternate same-head reviewer | `test_rounds_survive_restart_retries_and_manual_reship` (two reviews, one round on `abc123`), `test_alternate_reviewer_confirming_the_finding_starts_the_fix`. No test of an alternate reviewer interacting with a design record. | SUPPORTED for rounds; gap for design |
| Manual re-ship | `test_rounds_survive_restart_retries_and_manual_reship` (`cli.cmd_ship`, window stays 1), `test_manual_reship_review_budget_is_fresh_once_and_still_bounded`, `test_same_head_allowance_is_scoped_to_pr_head_and_survives_reship`. None runs design after re-ship. | SUPPORTED for rounds; gap for design |
| Stale head | `test_stale_head_does_not_start_design` (only asserts a review start, not that a recorded design for the old head is ignored; the key includes the sha), `test_stale_head_and_dry_run_never_file`, `test_red_pending_stale_and_missing_verdicts_never_count`; finalization side `finalize._prepare_design` requires live head == expected (`test_stale_or_malformed_design_results_retry_in_verification`). | SUPPORTED |
| New PR | `test_new_pr_starts_fresh_convergence_window` only asserts the PR-89 KV keys are absent and `review.window(..., 89) == []`; it never runs the gate on PR 89. `test_rounds_survive_restart_retries_and_manual_reship` does run `_review_gate` on PR 89 (review starts, rounds empty, round 1). | SUPPORTED for rounds; **gap:** no design-specific gate run after a PR swap |

## Safety submatrix (finite cases)

Trace: design `STATUS: DESIGNED` → `finalize._design_payload`/`_prepare_design`/`_design_result` writes `design:<project>#<n>:<pr>:<sha>` → `ship._review_gate` (status `fail`) → `_handle_design_followups` (protected-blocker checks, `review.file_followups`, evidence comment, `_merge_queued`) or `_review_triggered_fix(design_plan=...)`.

| Case | Evidence | Verdict |
|---|---|---|
| Security | Payload filter rejects `security` anywhere in justification/finding text; `_is_protected_blocker` rejects category/severity `security`; review-classified blocking security refuses followups: `test_design_result_followups_refuses_protected_blocker`, `test_design_result_followups_refuses_blocking_classified_blocker`, `test_design_payload_rejects_data_loss_category_and_spaced_done_when`. | SUPPORTED (classified); see clarification below |
| Credential exposure | No `credential`/`secret`/`token` term in the payload filter or `_is_protected_blocker`. Probe of `finalize._design_payload` with a `behavior` finding whose scenario is "API token written to logs in plaintext" or consequence "credentials exposed" or "secret leaked": all returned a valid payload. A credential finding is refused only if the *review* classified it `security`/`data-loss` and blocking. | **COVERAGE GAP** (no test; guard depends on reviewer classification) |
| Data loss | Category `data-loss` and the phrase "data loss" (also `data-loss`, `data_loss`) rejected. Probe: "dataloss on retry" and "records are permanently deleted" pass the filter. | SUPPORTED for the exact forms; keyword limit |
| Unsatisfied acceptance | `done_when`/`done-when` field or category rejected (`..._refuses_protected_blocker` subtest `unsatisfied-done-when-field`); phrase "done when" rejected in finalize. | SUPPORTED (same keyword limit) |
| Current-head independent review | `review.effective_for_item(..., pr, sha)` binds verdicts to PR+head: `tests/test_review.py::test_head_replacement_invalidates_pending_and_terminal_verdicts`, `test_pr_replacement_invalidates_...`; `test_stale_review_verdict_on_a_new_sha_re_reviews`; `finalize._prepare_design` requires live head == design head. The design follow-up path merges over a current-head `fail` verdict; the design result is the only override. | SUPPORTED |
| Red/pending CI | `_review_gate` returns before any design/merge unless `checks_state` is green/none; `test_red_pending_stale_and_missing_verdicts_never_count`, `test_pending_ci_waits_across_ticks`, `test_red_ci_pings_once_and_starts_a_fix_run`. | SUPPORTED |
| Failed follow-up intake | Generic: `test_create_failure_stays_pending_and_retries_without_fix`, `test_demoted_followup_failures_retry_durably`. `_handle_design_followups` returns True without merging when `file_followups` is False, but no design-specific test makes `create_issue` fail. | **COVERAGE GAP** (design path) |
| Base/head drift | `test_second_merge_rechecks_base_after_first_tick`, `FreshnessAdapterTests::test_real_origin_drift_and_replacement_head`, `test_reviewed_stale_pr_updates_then_waits_for_ci_and_fresh_base`. The design tests use `TestClassifiedReview.gate`, which patches `_merge_queued`, so no test runs design follow-ups through the real merge guards. | SUPPORTED generically; design path not end-to-end |

## #714 conservative clarification evaluation

Owner text ([comment](https://github.com/mkny13/mahler/issues/714#issuecomment-6041698274)): "Build the conservative version: the design run can never accept security, credential or data-loss findings — those always go to a fix." This audit does not relax it.

- Implemented: `finalize._design_payload` (substring filter on `security`, `done when`, `data loss`) and `ship._is_protected_blocker` (structured category/severity/`done_when`/`protected_blocker`), plus the review-classified blocking check. `recipes/design.md` instructs `fix` for these.
- **Not met as written:** credential findings are not named in any filter (probe above). A credential exposure the reviewer classified `behavior`/`hardening`, or one present only in prose with empty `classified`, can be accepted as follow-up. `test_design_result_followups_ignores_legacy_review_findings_marker` explicitly asserts a prose `[blocking/security]` review with empty `classified` still merges via follow-ups. That is the keyword-heuristic limit the #714 builder noted ("a security finding worded without any of these terms would still get through").
- Inconsistency: the recipe and `test_design_result_followups_accepts_incidental_prose_mentions` say incidental prose mentions are accepted, but `finalize._design_payload` rejects any occurrence in justification or finding text, so that ship test writes the KV record directly and never goes through finalization. Conservative direction, but the two layers disagree and the recipe will cause avoidable retries.
- Read-only design behavior is a recipe contract (see criterion 2), not enforcement.

## Verification

Targeted command (all selectors above resolved by `def` lookup and run):

```
python3 -m unittest \
 tests.test_config.SmokePolicyTests.test_review_context_default_override_and_clear \
 tests.test_recipes.ReviewOperatingContextTests tests.test_recipes.DesignRecipeTests \
 tests.test_runner.PrepareAccountEnvTests.test_design_checks_out_pr_head_without_replaying_and_uses_planning_model \
 tests.test_schedule.DesignRoleTests tests.test_ship.TestGreenReviewRounds \
 tests.test_finalize
```

Result: Ran 115 tests, OK, exit 0.

Four repository gates (run on the audited SHA, Python 3.14.3):

| Command | Result | Exit |
|---|---|---|
| `python3 -m mahler.repo_guard` | passed, no output | 0 |
| `python3 -m unittest discover -s tests` | Ran 2672 tests in 225.8s, OK (skipped=1) | 0 |
| `python3 tests/run_random.py 940` | random order seed 940, Ran 2672 tests in 199.6s, OK (skipped=1) | 0 |
| `python3 -X dev tests/run_strict.py` | Ran 2672 tests in 215.2s, OK (skipped=1) | 0 |

The skipped test is the same in all runs and is not one cited here.

## Gaps and defects (proposed regression tests; no production changes made)

1. (Withdrawn.) A second design after a planned fix changes the head is within #715's window bound, not a defect.
2. **Credential findings not protected in the design result (#714 clarification).** Test: `finalize._design_payload` must return None for a followups finding mentioning credentials/token/secret exposure; add the term(s) to the filter and `_is_protected_blocker`. Also decide whether prose-only protected review findings (empty `classified`) may merge via follow-ups; today `test_design_result_followups_ignores_legacy_review_findings_marker` asserts they do.
3. **No persistent-ledger restart test for design (criterion 4).** Test: reopen a file ledger (backup idiom from `test_same_head_pending_survives_restart_capacity_and_failed_launch`) holding `design:`/`reviewdrift:` keys; assert no second design start.
4. **Design new-PR and manual re-ship paths** are covered only for review rounds; add a gate run on PR 89 after a design record on PR 88.
5. **Design follow-up intake failure** and the **real `_merge_queued`** are not exercised in design tests (gate helper mocks the merge). Add one test with `create_issue` raising GHError (no merge, no fix, retry merges) and one with base drift after a design follow-up.
6. **Recipe/finalize disagreement** on incidental prose mentions (finalize rejects, recipe says accepted).
7. **Read-only design is unenforced** (recipe contract; `bypassPermissions` + shared deny list).

## Conclusion

Criteria 1 and 5 are supported by mechanical evidence (with the stated limits). Criterion 2 is partly supported: role, routing, recipe and result persistence pass, but read-only is recipe-only. Criterion 3 is supported within the shipped same-PR/head/evidence-window bound. Criterion 4 has a coverage gap for persistent restart and design-specific new-PR/re-ship. #707 must not be marked complete on this audit.
