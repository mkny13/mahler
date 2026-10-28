# Parent #1259 completion audit

Checked 2026-10-28 for #1312. Source checkout:
`2d10d0da3160c02656e1301db167815eaf82f82e`, branch
`mahler/1312-verify-remaining-completion-criteria-for`; target `main`, local
`origin/main` = `398e628504e70aaa481b706ace2f731dbbac8294`.
The replayed snapshot contained only incomplete `.audit-tmp` logs, not this report.
No runtime changes, production-ledger access, GUI checks, or GitHub writes were made.
History reused: `mahler history mahler '1259' --limit 3` (exit 0).
Read DESIGN D13, D18 and D52. Findings below distinguish current behavior from intent.

## Merge evidence

Reconfirmed using `gh pr view N --repo mkny13/mahler --json number,url,state,mergedAt,mergeCommit,baseRefName,closingIssuesReferences`
and `gh issue view N --repo mkny13/mahler --json number,state,closedAt,url`.
All commands exited 0; each issue is CLOSED, each PR is MERGED, its
`closingIssuesReferences` names the corresponding child, and `baseRefName` is `main`.
`git merge-base --is-ancestor <mergeCommit> 2d10d0da3160c02656e1301db167815eaf82f82e`
exited 0 for all five. No closed-unmerged child or successor inference was necessary.

| Child | Merged PR | mergedAt (UTC) | mergeCommit |
| --- | --- | --- | --- |
| #1260 | [#1270](https://github.com/mkny13/mahler/pull/1270) | 2026-10-26T05:24:48Z | c7ccb4787a4ffd7a20ea74faf2215ea78cd8611b |
| #1261 | [#1307](https://github.com/mkny13/mahler/pull/1307) | 2026-10-27T07:44:40Z | 3a59b476250dd403aa4f867ea9d7518994d25047 |
| #1262 | [#1309](https://github.com/mkny13/mahler/pull/1309) | 2026-10-27T10:37:41Z | 8bf52f56200c3b4d8366f6179b0168acf4866b7f |
| #1263 | [#1278](https://github.com/mkny13/mahler/pull/1278) | 2026-10-26T10:13:41Z | a33f4350ce353281ac824fe3678104b1d628f7a8 |
| #1264 | [#1277](https://github.com/mkny13/mahler/pull/1277) | 2026-10-26T09:07:05Z | 2315573d1d38710da8889aa5120d454a4a0ac8e6 |

## Conclusions

| Exact parent criterion | Result | Evidence and limits |
| --- | --- | --- |
| #1260 merged: `waiting` state, `waits.py`, D52 (two kinds of waiting; self-heal mechanism). | met | #1270; `ledger.py:376` includes waiting; `waits.hold`, `WAKES`, `recheck` implement recorded causes, backoff and automatic resumption. `scheduler.tick` invokes ship, whose project pass calls migration/recheck (`ship.py:738,744`). D52 documents the mechanism. Tests below exercise holds and wakes. This is the foundation criterion; legacy migration limitations are assigned to #1261. |
| #1261 merged: conductor escalation sites use `waiting`; external red CI charges no attempts; stranded items migrated. | unmet | #1307 merged and escalation/accounting tests pass, but `waits.migrate_legacy` skips old red-CI failures unless `repo_wide_red` is true now, then marks migration complete. Already-cleared repo-wide failures can remain failed indefinitely. `test_ci_still_red_converts_only_when_repo_wide_now` explicitly demonstrates the skip. Follow-up: safely recognize and revive historically external failures after the blocker clears, with regression coverage; do not revive item-specific failures indiscriminately. |
| #1262 merged: non-converging review never ends in `failed`. | met | #1309; `ship._review_exhausted` merges eligible green changes with follow-ups, otherwise performs one fresh rebuild through `waiting`, then uses `needs_you` after repeated exhaustion. Named tests cover these paths and never-failed behavior. This does not imply all CI or abandoned-PR failures avoid failed. |
| #1263 merged: NEEDS-YOU requires an owner-only category; owner verify/UAT goes to "Needs a human to check" (async). | unmet | #1278; category parsing and first-strike enforcement exist, but `finalize._needs_you` lets the second untagged request in the same cycle park and ping. `test_second_untagged_needs_you_in_the_cycle_reaches_the_owner` proves a scope question bypasses the literal requirement. D52 explicitly documents that exception. Follow-up: enforce owner-only categories across repeated requests and keep non-owner verification asynchronous. No-change claim human checks and post-ship UAT already work. |
| #1264 merged: digest and console separate conductor waits and show the 7-day stuck rate by cause (target < 5%). | unmet | #1277; digest implements and tests the metric, and console separates waits, but console has no stuck-rate data/rendering. `rg -n 'stuck_rate\|Stuck rate' mahler/console` returns no matches. `console.state` consumes `daily_digest.conductor_waits`, not `stuck_rate`; `page._conductor_waits` displays cause/since/retry only. Follow-up: expose seven-day numerator, denominator, rate, target and cause breakdown in console with rendering coverage. |

## Source and executed test mapping

All paths below are relative to this checkout; tests use the repository's isolated
fixtures. Passing tests confirm their assertions, including exceptions; they do not
turn the unmet criteria into passes.

- Foundation: `mahler/waits.py` (`hold`, `_recheck_item`, `WAKES`),
  `mahler/ledger.py` (`STATES`), `mahler/scheduler.py` (`tick`), `mahler/ship.py`
  project pass and DESIGN D52. `test_waits`: `test_hold_sets_state_and_record_without_ping`,
  `test_time_wake_resumes_when_next_at_reached`,
  `test_ci_repo_wide_stays_then_resumes_when_blocker_clears`,
  `test_backoff_sequence`, `test_stale_bound_at_gets_fresh_other_record`,
  `test_backstop_escalates_once_with_one_ping`. D52's 72-hour backstop asks the
  owner to retry/park; ordinary holds do not ping. Generic causes retry on time,
  not a dedicated environment/access probe. This is not proof of every production cause.
- Escalations: `ship.py` CI/queue timeout, `hold_repo_wide`, capacity handling;
  `finalize.py` setup and inconclusive review; `tick.py:820` worktree conflict;
  `waits.py` wake functions and `migrate_legacy`. `test_waits`:
  `test_ci_pending_timeout_waits_and_restarts_timer_on_wake`,
  `test_merge_queue_timeout_waits_and_resets_queue_since`,
  `test_missing_tier_waits_and_resumes_when_a_route_appears`,
  `test_worktree_conflict_wakes_when_branch_is_free_or_lease_gone`,
  `test_red_ci_at_max_attempts_repo_wide_waits_without_charge`,
  `test_red_ci_at_max_attempts_with_active_blocker_waits_then_resumes`,
  `test_red_ci_at_max_attempts_on_own_failure_still_fails`,
  `test_each_question_pattern_converts`, `test_failed_state_event_detail_converts`,
  `test_owner_question_is_left_alone`, `test_migration_runs_once`,
  `test_legacy_no_change_item_resumes_review_not_build`, and the migration gap test above.
- Review: `ship._review_exhausted` and `_finish_fresh_rebuild`, DESIGN D18.
  `test_ship`: `test_exhausted_review_with_only_nonprotected_findings_merges_with_followups`,
  `test_exhausted_review_with_protected_finding_rebuilds_fresh_once`,
  `test_second_exhaustion_asks_owner_once_and_never_fails`.
- Owner questions: `finalize.OWNER_CATEGORIES`, `parse_needs_you_category`, `_needs_you`;
  `prompt.py:310` carries the prior question back to the agent; `recipes/build.md`,
  `fix.md`, `sort.md` require categories and asynchronous human checks.
  `test_finalize`: `test_untagged_needs_you_goes_back_to_the_agent_once`,
  `test_unknown_category_counts_as_untagged`, `test_allowed_category_is_stripped_and_parks`,
  and the second-strike test above. `no_change.py:135` converts disallowed questions
  into human checks; `test_no_change.test_non_owner_question_becomes_a_human_check_not_a_park`
  is included in full discovery. `test_ship.test_shipped_needs_human_lands_in_the_uat_queue`
  and `test_digest.test_shipments_never_create_owner_waits_or_nags` cover asynchronous UAT.
- Displays/metric: `digest.conductor_waits`, `stuck_rate`, `format_digest`;
  `console/state.py:132`, `console/page.py:1000`. `test_digest`:
  `test_conductor_waits_separate_and_bounded`, `test_weekly_arithmetic_and_structural_causes`,
  `test_zero_completions`. These test seven-day event transitions divided by completions,
  cause attribution, out-of-window exclusions, target text and n/a with no completions.
  `test_console_state.test_waiting_item_renders_and_is_not_a_needs_you` covers the separate
  console display; it does not assert a console stuck-rate metric.

## Verification

Results will be recorded after the running checks finish.

## Remaining asynchronous feedback

Observe whether production stuck rate trends below 5% and whether "Waiting on you"
contains real decisions. No production trend or owner UAT was measured here; neither
is an acceptance gate. No owner-only decision is needed for this audit. The conductor
owns parent checklist updates and follow-up creation for the three unmet criteria.
