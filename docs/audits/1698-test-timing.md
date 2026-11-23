# Test suite timing baseline (#1700, part of #1698)

## Reproduce and compare

Run from the repository root with Python 3.14+:

```sh
python3 tests/run_timing.py --json baseline.json
python3 tests/run_timing.py --compare baseline.json --json current.json
```

JSON output is opt-in. Without `--json`, the runner only prints results and the
slowest 20 IDs (all IDs if fewer than 20). `--start-directory` and `--pattern`
allow bounded investigations. Discovery happens once per invocation.

Suite seconds use a monotonic clock and include discovery, class/module fixtures,
and unittest execution/output. Per-test seconds include setup, method, teardown,
and cleanups, excluding class/module fixtures and discovery. Subtests belong to
the parent ID. Repeated IDs are aggregated by summing durations; test count still
counts executions. A class/module setup failure can produce an error without a
started test, so check `successful`/`errors` as well as the count. Test failures,
errors and unexpected successes preserve unittest's unsuccessful exit status.

Comparison lists added/removed IDs separately from common-test deltas. An increase
is advisory only when it exceeds **both 50% and 0.1 seconds**. Equality does not
qualify. A zero baseline has no defined percentage (JSON null); an increase over
0.1 seconds qualifies. No elapsed-time value changes the test exit status.
Compare equivalent discovery settings and runtimes; added tests and shared-fixture
costs mean common-test deltas need not explain the whole suite-time change.

## Evidence scope

These are sequential local measurements on one machine, not a historical CI
trend. Host load, filesystem caches and process startup affect results. No CI
workflow, merge threshold, production implementation or operator verify command
is changed. A slow test is an investigation candidate, not proof of a defect.

The focused tests first failed with `ModuleNotFoundError: run_timing` before the
implementation existed. An initial clock mock also affected unittest's own clock;
isolating the mock to the runner module fixed that fixture. The four deterministic
tests then passed, including isolated CLI passing, failing and erroring suites.

## Three-run local baseline

Revision: `aa18507e9139214376511dd3101ba99d686ebdd5` (no tracked changes in any run).

Runtime: CPython 3.14.10 (main, Oct 25 2026, 17:12:00) [Clang 21.0.0 (clang-2100.3.34.2)];
OS: `macOS-27.2.1-arm64-arm-64bit-Mach-O`; machine: `arm64`.

Each command was `python3 tests/run_timing.py --json timing-evidence/baseline-N.json`
for N = 1, 2, 3. Processes ran sequentially.

| Run | Started (UTC) | Tests | Suite seconds | Process wall seconds | Result |
| --- | --- | ---: | ---: | ---: | --- |
| 1 | 2026-11-23T11:28:10.799260+00:00 | 3917 | 317.916244 | 318.070454 | PASS; 2 skipped; 0 failures/errors |
| 2 | 2026-11-23T11:33:28.877658+00:00 | 3917 | 311.584664 | 311.736930 | PASS; 2 skipped; 0 failures/errors |
| 3 | 2026-11-23T11:38:40.586023+00:00 | 3917 | 312.092108 | 312.225885 | PASS; 2 skipped; 0 failures/errors |

Median suite duration: **312.092108 seconds**.

Top 20 IDs by median per-test duration (seconds):

| Test ID | Run 1 | Run 2 | Run 3 | Median |
| --- | ---: | ---: | ---: | ---: |
| `test_tick_capacity.CapacityObservationTests.test_full_tick_flushes_even_when_the_ship_pass_raises` | 49.997311 | 49.643176 | 49.603524 | 49.643176 |
| `test_ledger.CreditReservationTests.test_credit_exposure_sums_without_float_drift` | 48.199302 | 49.919855 | 47.756950 | 48.199302 |
| `test_escalation.TierEscalationTests.test_two_failures_on_tier_1_escalates_to_tier_2` | 12.591879 | 12.476019 | 12.287872 | 12.476019 |
| `test_escalation.TierEscalationTests.test_two_failures_on_tier_2_escalates_to_tier_3` | 12.430882 | 12.334256 | 12.550713 | 12.430882 |
| `test_escalation.TierEscalationTests.test_overrun_heuristic_escalates_size_s_after_10_minutes_on_tier_1` | 6.349374 | 6.116407 | 6.297180 | 6.297180 |
| `test_escalation.TierEscalationTests.test_no_attempt_does_not_increment_esc_fails` | 6.340742 | 6.213636 | 6.218904 | 6.218904 |
| `test_finalize.RunTests.test_snapshot_failure_does_not_count_as_no_work` | 6.112557 | 6.217651 | 6.253317 | 6.217651 |
| `test_finalize.RunTests.test_snapshot_failure_keeps_the_worktree_and_still_ends_the_run` | 6.263302 | 6.181666 | 6.141907 | 6.181666 |
| `test_gateway_process.GatewayOutlivesLauncherTest.test_an_idle_gateway_with_no_active_run_exits_and_removes_its_state` | 3.960020 | 4.190174 | 4.119033 | 4.119033 |
| `test_desktop.ProcessTests.test_second_wrapper_defers_while_first_runs` | 3.251223 | 3.291486 | 3.378520 | 3.291486 |
| `test_discuss.DiscussionTests.test_supervisor_gives_up_after_max_retries` | 3.325397 | 3.051437 | 3.179331 | 3.179331 |
| `test_tick.PassBudgetTests.test_tick_finalizes_before_sync_and_again_after_budget_or_timeout` | 2.763759 | 2.635808 | 2.735103 | 2.735103 |
| `test_schedule.ScheduleHoldTests.test_post_merge_runs_after_shipping_and_respects_pause` | 2.015973 | 2.279901 | 2.448245 | 2.279901 |
| `test_discuss.HelperLifecycleTests.test_seed_signals_remove_worker_and_fixture` | 2.433774 | 2.148253 | 2.155884 | 2.155884 |
| `test_purge_calendar_profile_history.PurgeHistoryTest.test_force_push_requires_confirmation_and_honors_ref_leases` | 2.156713 | 2.081275 | 2.128425 | 2.128425 |
| `test_console.DiscussionBrowserTests.test_fake_worker_journeys_at_phone_and_desktop_widths` | 3.203769 | 1.955311 | 2.087182 | 2.087182 |
| `test_runner.DetachedRunTests.test_detached_native_child_after_leader_exit_is_reaped_on_darwin` | 2.008397 | 1.885875 | 2.037664 | 2.008397 |
| `test_practices_audit.SchedulerOrderingTests.test_scan_exception_is_reported_without_aborting_tick` | 1.925713 | 1.886430 | 2.005646 | 1.925713 |
| `test_ship.ShipTests.test_review_start_delivers_effective_operating_context_in_prompt` | 1.909661 | 1.923476 | 1.747635 | 1.909661 |
| `test_purge_calendar_profile_history.PurgeHistoryTest.test_rewrite_accepts_hash_version_and_preserves_unrelated_history` | 1.807361 | 1.788469 | 1.778220 | 1.788469 |

## Bounded optimization candidates

1. `tests/test_ledger.py:2082`, `test_credit_exposure_sums_without_float_drift`:
   10,000 sequential `reserve_credit` calls dominate this assertion. Investigate
   a smaller case that still detects floating-point drift, or separate stress
   coverage from arithmetic correctness; retain reservation/ceiling semantics.
2. `tests/test_tick_capacity.py:356`,
   `test_full_tick_flushes_even_when_the_ship_pass_raises`: invokes an entire
   scheduler tick while mocking only ship and observing flush. Investigate
   unrelated pass dependencies and isolate them while retaining the real
   exception-to-flush control flow. The timings do not prove which pass is slow.
3. `tests/test_escalation.py:384` and `:403`, the two-failure escalation tests:
   each calls `_end_run` twice, which runs `finalize.finalize` against temporary
   worktrees. Investigate incidental finalization dependencies while keeping
   escalation state transitions under test. Do not infer a network/retry cause
   without profiling.

These are the complete candidate list for this bounded audit. Their repeated
costs appear in the table above; no candidate was optimized in this issue.

## Required repository verification

All commands ran sequentially on the baseline code; the later documentation-only
commit does not change the tests. Wall time includes interpreter startup and discovery.

| Command | Tests | unittest seconds | Process wall seconds | Outcome |
| --- | ---: | ---: | ---: | --- |
| `python3 -m mahler.repo_guard` | n/a | n/a | 0.039622 | PASS, exit 0 |
| `python3 -m unittest discover -s tests` | 3917 | 311.303 | 311.582060 | PASS, exit 0, 2 skipped |
| `python3 tests/run_random.py` | 3917 | 321.297 | 322.002830 | PASS, exit 0, 2 skipped |

Randomized seed: **2112414675**; reproduce with
`python3 tests/run_random.py 2112414675`.
