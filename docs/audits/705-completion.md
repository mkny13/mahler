# Audit: #705 completion criteria

- Audited: 2026-10-14, Mahler checkout `6378e70811592526679dbf4862d3fbea7c7434d2` (main) plus this document, Python 3.14.3.
- Method: public GitHub metadata read with the existing `gh`, the merged tests and documents read at the audited SHA, and fresh local runs of the four verification commands. No daemon state, `~/.mahler` or live project was touched.
- Merge status is provenance, not proof of behaviour; behaviour is claimed only from named tests or authoritative owner records.
- Test ids below are `file::Class.test`; `FinalizeTests` means `tests/test_finalize.py::RunTests`, `ClaimTests` means `tests/test_no_change.py::ClaimTests`.

## Merge identities

| Child | PR | Base | Merge SHA | Merged (UTC) | PR head | CI |
|---|---|---|---|---|---|---|
| #716 | [#756](https://github.com/mkny13/mahler/pull/756) | `main` | `dc30f78a5305960197fce71e2b964c29d6ae408b` | 2026-10-06T15:05:58Z | `6f853242c2031317b9ab89bdea9468abb555529f` | `test` SUCCESS |
| #717 | [#786](https://github.com/mkny13/mahler/pull/786) | `main` | `a76488b55eccbbf369903c7ebc34fd1d0b7fa311` | 2026-10-07T07:01:21Z | `873b89edd457b0dfe09815b8f0865702105e8f86` | `test` SUCCESS |

Both child issues are CLOSED. #718 was **not** merged: it was closed `NOT_PLANNED` on 2026-10-06T14:21:31Z.

## Criteria (three)

| # | Criterion | Verdict | Evidence |
|---|---|---|---|
| 1 | #716 merges with a regression proving the second identical no-work DONE cannot schedule a third build. | **SUPPORTED** | Merged via #756. `FinalizeTests.test_repeated_no_work_parks_before_third_build_and_replay_is_quiet`: the first clean no-work DONE leaves the item `verifying` with 0 attempts; the ledger is backed up to disk and reopened (daemon restart); a second distinct identical DONE parks the item, `tick._candidates(...)` returns `[]`, and one ping is sent; a replayed ending sends no ping, runs no probe and adds no comment; an explicit resume plus another identical DONE stays parked with one "Repeated no-work DONE" comment in total. Clean versus non-clean versus unknown: `tests/test_runner.py::SnapshotTests.test_no_work_probe_fetches_configured_base` (clean zero-ahead returns the base SHA), `test_no_work_probe_distinguishes_dirty_untracked_and_committed_work` (dirty, untracked and committed work return `None`, tree untouched), `test_no_work_probe_errors_are_not_clean_evidence` (no worktree, missing worktree and missing base raise `GitError`). Finalize side: `FinalizeTests.test_snapshot_failure_does_not_count_as_no_work`, `test_missing_tree_or_git_failure_does_not_count_as_no_work`, `test_changed_no_work_evidence_starts_new_signature`, `test_saved_work_clears_no_work_signature`, `test_no_work_guard_retries_failed_comment_without_duplicate_delivery`, `test_no_work_guard_keeps_comment_receipt_when_ping_raises`, `test_no_work_guard_honors_closed_stale_dry_run_and_fix`. |
| 2 | #717 merges with regressions for independent verification, evidenced closure, failed-claim retries and stale-verdict protection. | **SUPPORTED** | Merged via #786. Independent exact-base review and capacity: `ClaimTests.test_capacity_waits_and_launch_reuses_independent_route` (no launch while reviewer capacity is absent, no inconclusive charge, then launch excludes the builder platform and passes `claim_base_sha`, 0 attempts); `FinalizeTests.test_no_change_done_review_pass_closes_without_pr_or_attempt_charge` (record carries `claim_run` and `base_sha`, the review is a fresh run handed off from the conductor). Evidenced idempotent closure: that same test (one `close_issue(5)`, one `mahler:no-change` comment, no `_open_pr`, state `done`, 0 attempts) and `ClaimTests.test_pass_closes_once_without_shipping_or_attempts` (also zero `release_items` rows, so no fabricated merge or release). One-charge rejection retry: `ClaimTests.test_fail_charges_once_and_returns_to_build`, `test_mechanical_gap_rejects_once`. Bounded and inconclusive outcomes: `test_invalid_verdicts_are_bounded_without_build_attempts`, `test_mechanical_unknown_and_exception_are_inconclusive`, `test_interrupted_launch_is_bounded_and_active_run_is_not_relaunched`, `tests/test_finalize.py::ReviewInconclusiveTests.test_records_dedupes_and_escalates_on_third`. Retryable GitHub operations: `test_restart_after_comment_or_close_failure_is_retryable`, `test_unconfirmed_closure_stays_retryable`. Base drift: `test_base_drift_discards_verdict_and_launches_new_review`, `test_base_drift_during_comment_prevents_closure`, `test_scan_lease_loss_and_base_drift_never_close`. Stale run/epoch/replay: `test_run_and_epoch_fence_and_replayed_verdict`, `test_lease_change_during_github_read_prevents_closure`. Optional observations are non-gating: `test_optional_observation_is_durable_and_non_gating`. Design record: DESIGN.md, "No-change build DONE" (D18, line ~1330). |
| 3 | #718 merges with regressions for manual-only UAT routing and passing-evidence closure without fake merge records. (Original wording; see disposition.) | **SUPERSEDED by owner decision, not merged** | #718 was closed as not planned on 2026-10-06 with an owner-authored comment: "Closed as superseded by #741 and its implementation children #749-#751 ... Building this issue would recreate the Ready-to-test workflow that #741 removes" ([comment](https://github.com/mkny13/mahler/issues/718#issuecomment-6018330582)). #741 is CLOSED (retire UAT: automated evidence or a quiet period; fix on failure) and its eight-criterion audit is `docs/audits/741-completion.md`. Merged successors, each with `test` SUCCESS: #749 / [PR #870](https://github.com/mkny13/mahler/pull/870) (`b01e60964f9b60322500ee192534818113c8e1d3`), #750 / [PR #871](https://github.com/mkny13/mahler/pull/871) (`424e3ea9b10d4aa378b741b37eb06353734f2c95`), #751 / [PR #875](https://github.com/mkny13/mahler/pull/875) (`914eb21a5ab624d4aa4cd074087878d91e7756c6`); completion remediation #1011 / [PR #1037](https://github.com/mkny13/mahler/pull/1037) (`b6ae20e1fccaf91973c1b049552f4d74371deb51`, `test` SUCCESS). Lifecycle: shipments complete on automated evidence or after 14 quiet days, with no owner pass step; a failure becomes linked p1 fix work (DESIGN D10). Optional `human_checks` from a claim review are non-gating evidence (DESIGN.md ~line 1342; `ClaimTests.test_optional_observation_is_durable_and_non_gating`). No-change closure creates no merge or release record (criterion 2 evidence). No manual Ready-to-test gate is recreated. |

## Fresh verification

Run on `6378e70811592526679dbf4862d3fbea7c7434d2` plus this document, Python 3.14.3, from the worktree root.

| Command | Result | Exit |
|---|---|---|
| `python3 -m mahler.repo_guard` | passed (no output) | 0 |
| `python3 -m unittest discover -s tests` | Ran 2885 tests, OK (skipped=1) | 0 |
| `python3 -X dev tests/run_strict.py` | Ran 2885 tests, OK (skipped=1) | 0 |
| `python3 tests/run_random.py 705` | random order seed 705, Ran 2885 tests, OK (skipped=1) | 0 |

The single skipped test is the same in all runs and is not one of the tests cited here.

Audit-document checks: exactly three criterion rows; every cited test identifier was looked up by `def` in `tests/`, and every GitHub URL and PR/issue number was read through `gh`.

## Remaining step

After this merges, one `<!-- mahler:agent -->` evidence comment is posted on #705, checking criteria 1 and 2 and replacing criterion 3 with the checked #718/#741 supersession disposition. That edit happens outside this branch and no other #705 text or labels change.
