# Capacity completion verification for #653 (issue #936)

Verification only. No runtime code, policy or state was changed.

- Tested SHA: `b0b60b6fcf46ce6dc021c203916b4138151d06e4` (main at start of #936 build)
- Python: 3.14.3 (darwin)
- Date: 2026-10-11
- All tests use in-memory ledgers or temp dirs; no live daemon, provider login or `~/.mahler` access.

## Child issues and merged PRs

| Child | Title | PR | Merge SHA | In tested SHA? |
|---|---|---|---|---|
| [#654](https://github.com/mkny13/mahler/issues/654) | Keep verification capacity waits retryable | [#658](https://github.com/mkny13/mahler/pull/658) | `c11480387866e20ca71a93f96256ff6378ff399b` | yes (`git merge-base --is-ancestor` exit 0) |
| [#655](https://github.com/mkny13/mahler/issues/655) | Self-heal items stranded by past capacity escalations | [#661](https://github.com/mkny13/mahler/pull/661) | `85b24d25646cae32cec9c001c3f10eaec1eca281` | yes |
| [#656](https://github.com/mkny13/mahler/issues/656) | Show scheduled capacity retries in status views | [#660](https://github.com/mkny13/mahler/pull/660) | `df5af72fb46b4666c0ce3ff989101d3c14bdeac2` | yes |
| [#657](https://github.com/mkny13/mahler/issues/657) | Audit build and launch capacity escalations | [#662](https://github.com/mkny13/mahler/pull/662) | `be342abb6f7e5b907d2045501889717e75f91494` | yes |

All four issues are CLOSED and all four PRs MERGED. [#659](https://github.com/mkny13/mahler/pull/659) was closed unmerged and is **not** used as evidence for #657.

## Timeout wording: ordinary verification timeout vs. the 24-hour backstop

- `verify_timeout_minutes` (default 60, `mahler/config.py`) bounds *CI pending / merge-queue* waits (`mahler/ship.py` ~L591, ~L663). It does not govern review or fix capacity waits.
- Review/fix capacity waits are retryable and have their own backstop, `capacity_wait_max_hours` (default 24). It applies only when `_capacity_wait` (`ship.py`) is given no known recovery time; the item then moves to `capacity_wait_expired` and `needs_you` ("capacity wait exceeded N hours with no known recovery").
- Waits with a known recovery time never expire by time. Missing-tier (no configured eligible platform) escalates immediately as a configuration problem.
- #654 specifies this backstop explicitly. Tests assert the escalation: `test_review_capacity_wait_uses_its_own_backstop_and_resets_per_head`, `test_fix_capacity_waits_retry_and_only_unknown_backstop_escalates`.
- **Discrepancy:** the parent wording "do not become owner-action `needs_you` items solely because time passed" is broader than shipped policy. Read literally, it is unsupported: an unknown-recovery wait does become `needs_you` after 24h. It is supported if read as "known/ordinary waits never escalate on time, and unknown ones are re-checked until the 24h backstop".

## Six evidence rows (one per #653 checklist entry)

### 1. All four linked child issues complete with targeted regression tests
- Evidence: table above; tests named in rows 2-6, all present in the tested SHA and executed below.
- Verdict: **supported**.

### 2. Temporary capacity waits retry on a schedule and do not become `needs_you` solely because time passed
- Source: `ship._capacity_wait`, `ship._fix_wait`, `ship._observe_wait`, `config` `capacity_wait_max_hours`.
- Tests (`tests/test_ship.py`): `test_review_capacity_wait_uses_its_own_backstop_and_resets_per_head`, `test_fix_capacity_waits_retry_and_only_unknown_backstop_escalates`, `test_fix_start_clears_wait_only_on_success`, `test_known_hold_skips_routing_then_recovers`, `test_capacity_recovery_waits_for_quota_and_required_fix_tier`, `test_missing_required_fix_tier_escalates_distinctly`; `tests/test_escalation.py`: `test_waiting_fix_uses_capacity_backstop_without_recounting`, `test_waiting_review_fix_resumes_when_capacity_returns`.
- Result: waits of 3h stay `verifying`; review/fix waits beyond the 60-minute `verify_timeout_minutes` remain retryable; escalation happens only at the 24h unknown-recovery backstop (reset per head) or on a missing tier.
- Verdict: **supported with caveat** - literal "solely because time passed" is **unsupported** (see timeout section).

### 3. Known retry times visible in console and `mahler status`; unknown times trigger re-checks
- Source: `mahler/console/state.py::capacity_wait_text` (shared copy: "retry around …", "retry due; will re-check", "retry time unknown; will re-check"), used by `mahler/cli.py` status (L272).
- Tests: `tests/test_console.py::test_verifying_capacity_retry_copy`, `test_capacity_review_after_fix_ignores_old_failed_verdict`; `tests/test_cli_status.py::test_status_capacity_retries_and_ordinary_items`; `tests/test_ship.py`: `test_review_wait_is_recorded_and_retry_delay_stays_observable`, `test_unknown_refresh_dispatches_normal_states`, `test_unknown_refresh_backoff_survives_context_restart`.
- Verdict: **supported**. Unknown recovery is re-checked each tick, bounded by the 24h backstop.

### 4. Existing capacity-caused `needs_you` self-heal; genuine human decisions untouched
- Source: `ship.migrate_capacity_waits` (called from `sync.py` L42), `ship.recover_capacity_waits`, `ship._mark_capacity_wait`.
- Tests: `tests/test_sync.py::test_304_migrates_only_known_capacity_questions_once`; `tests/test_ship.py`: `test_legacy_capacity_pauses_recover_only_when_route_returns`, `test_new_capacity_escalation_persists_recoverable_marker`, `test_capacity_recovery_preserves_decisions_and_later_pauses`, `test_capacity_recovery_respects_review_independence`, `test_capacity_recovery_leaves_session_and_dry_run_alone`, `test_capacity_recovery_preserves_preference_and_tier`; `tests/test_console.py::test_legacy_options_are_removed_from_question_and_recovered_as_choices`.
- Verdict: **supported**.

### 5. Remaining scheduler/finalizer capacity-to-failure paths audited and corrected or linked
- Bounded areas and current functions:
  - `mahler/ship.py`: `_capacity_wait`, `_fix_wait`, `recover_capacity_waits`, `migrate_capacity_waits`. Tests in row 2 and 4 and `test_failed_review_releases_capacity_for_another_green_pr`, `test_run_capacity_refuses_review_and_fix_until_holder_finishes`.
  - `mahler/tick.py`: `start` launch-failure branch (~L735-760: releases lease, records `launch_failed`, delegates to `launch_health.failed`; attempts untouched). Test `tests/test_tick_capacity.py::test_three_launch_failures_preserve_budget_and_retry_after_backoff`.
  - `mahler/finalize.py`: `CAPACITY_STOPS = ("quota", "no_credit", "model_unavailable")`, `NO_ATTEMPT`, `_hold_model_unavailable`, `_record_credit_failure`. Tests `tests/test_finalize.py::test_capacity_stops_preserve_fix_shipping_and_failure_budget` (attempts/esc_tier/esc_fails preserved, item returns to `verifying`, no needs-you ping), `test_mid_run_provider_errors_preserve_budgets_only_for_capacity` (invalid API key is *not* treated as capacity).
  - `mahler/sync.py`: migration, `test_304_migrates_only_known_capacity_questions_once`.
  - Unstarted fix launches: `tests/test_escalation.py::test_fix_that_never_started_keeps_cycle_dedup`; `test_capacity_interrupted_fixes_charge_each_head_only_once`.
  - Genuine failure accounting: `test_genuine_fix_failures_on_unchanged_head_keep_escalating`, `test_completed_genuine_fix_clears_cycle_dedup`, `test_failure_retry_and_remote_recovery_after_lost_local_write`.
- No reproducible gap found in the named paths; no follow-up issue needed.
- Verdict: **supported** within the bounded scope (the audit was not widened beyond the four named areas).

### 6. Full unit suite and randomized-order check pass
- See the commands below; all exited 0 at the tested SHA.
- Verdict: **supported** (1 skip, listed below).

## Behavior matrix coverage

| Behavior | Evidence |
|---|---|
| Review/fix waits beyond `verify_timeout_minutes` | row 2 tests (3h waits stay `verifying`) |
| Known recovery vs unknown/stale re-check | row 3 tests; `test_unknown_refresh_*` |
| Missing-tier boundary | `test_missing_required_fix_tier_escalates_distinctly`, `test_max_tier_retries_for_review_and_red_ci` |
| Unknown-recovery backstop | the two 24h tests (row 2) |
| Console and CLI known/unknown copy | row 3 |
| Legacy migration, structured recovery | row 4 |
| Owner decisions preserved | `test_capacity_recovery_preserves_decisions_and_later_pauses`, `test_stale_missing_resume_preserves_owner` |
| quota / no-credit / model-unavailable fix interruption | `test_capacity_stops_preserve_fix_shipping_and_failure_budget` (subtest per `CAPACITY_STOPS`) |
| Unstarted launch retries | `test_three_launch_failures_...`, `test_fix_that_never_started_keeps_cycle_dedup` |
| Genuine repeated failure | `test_genuine_fix_failures_on_unchanged_head_keep_escalating` |

## Commands executed at the tested SHA (newly run, not historical)

Focused (`python3 -m unittest discover -s tests -p 'test_<name>.py'`), all exit OK, no failures:

| Module | Tests |
|---|---|
| routing | 118 |
| config | 44 |
| ship | 257 |
| escalation | 19 |
| sync | 8 |
| console | 218 |
| cli_status | 14 |
| finalize | 84 |
| tick_capacity | 14 |

Repository checks:

| Command | Result |
|---|---|
| `python3 -m mahler.repo_guard` | exit 0 |
| `python3 -m unittest discover -s tests` | Ran 2672 tests, OK (skipped=1) |
| `python3 tests/run_random.py 12345` | seed 12345; Ran 2672 tests, OK (skipped=1); exit 0 |
| `python3 -X dev tests/run_strict.py` | Ran 2672 tests, OK (skipped=1) |

The single skip is in the unrelated full suite and was not investigated further. No failures occurred.

## Recommendation for the #653 checklist

1. Children complete with regression tests: **check**.
2. Waits retry on schedule, no `needs_you` solely from time: **do not check literally**. The 24h unknown-recovery backstop (#654, by design) escalates. Check only if the owner accepts the backstop as the intended policy or rewords the criterion.
3. Retry times visible; unknown re-checks: **check**.
4. Existing capacity `needs_you` self-heal, decisions untouched: **check**.
5. Scheduler/finalizer paths audited: **check** (bounded to the four named areas; no gaps found).
6. Full suite and randomized-order pass: **check**.
