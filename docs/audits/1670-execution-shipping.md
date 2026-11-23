# Audit: Run Execution and Conductor Shipping against Architecture (Issue #1670 / #1672)

- **Scope:** DESIGN D9, D11, D17, D18, D19, D28, D30, D33, D40, D47, D50; ARCHITECTURE "Run flow" and "Shipping flow".
- **Audited modules:** `mahler/runner.py`, `prompt.py`, `ship.py`, `gh.py`, `review.py`, `no_change.py`, `ci_blockers.py`, `post_merge.py`, `screenshots.py`, `screenshot_delivery.py`, `desktop.py`, with paired tests in `tests/`.
- **Method:** Hermetic source reading and test execution only. No PR was merged, no desktop was driven, no live GitHub write and no real `~/.mahler` state was read. Python 3.14.10, macOS.
- **Verification:** `python3 -m unittest discover -s tests` ran 3907 tests, OK (2 skipped), on the replayed branch before this commit.

## Decision-to-code matrix

| Decision / section | Result | Source symbols | Test evidence |
|---|---|---|---|
| D9 Handoff protocol | `conformant` | `runner.snapshot`, `runner.catch_up`, `runner.prepare` (starts from the handoff branch or base) | `tests.test_runner.SnapshotTests`, `CatchUpTests` |
| D11 Feedback loop / independent review | `conformant` | `ship._review_required`, `_review_gate`, `_start_review_run`, `_review_route`, `_review_not_converging`, `_review_exhausted`; `review.py` verdict binding to PR and head SHA | `tests.test_review` (`test_head_replacement_invalidates_pending_and_terminal_verdicts`, `test_terminal_verdict_missing_bound_sha_is_not_accepted`), `tests.test_ship.TestReviewConvergence`, `TestGreenReviewRounds` |
| D17 Self-hosting, launch circuit breaker | `conformant` | `runner.launch`, launch-failure path in `finalize`/`tick` (outside this audit's file set, so not re-audited here) | `tests.test_runner.DetachedRunTests`, `RunProcessSafetyTests` |
| D18 Agents build, the conductor ships | `fixed` | `ship._open_pr`, `ship._merge_queued`, `ship._shipped`, `gh.GH.pr_create`, `gh.GH.pr_merge`. Drift found: `GH.pr_merge` passed `--match-head-commit` whatever its argument was, so an empty or option-like head was not rejected. It now raises `GHError` unless the head is a 40-hex SHA. A stale runner comment that described an agent pushing first was corrected. | `tests.test_gh.PRMergeTests` (both tests), `tests.test_ship.TestShip` |
| D19 In flight until merged; base/head revalidation | `conformant` | `ship._ship_lease`, `_rebuild_on_base`, `_update_reviewed_pr`, `_mergeability_observation`; merge uses `--match-head-commit` | `tests.test_ship.ShipTests.test_second_merge_rechecks_base_after_first_tick`, `tests.test_ship.TestReviewConvergence` |
| D28 Wait for GitHub to confirm the merge | `conformant` | `ship._merge_queued`, `ship.pr_merged`, `ship._shipped`, `gh.GH.pr_merge_info` | `tests.test_ship.ShipTests.test_merge_queue_new_head_gets_a_fresh_request_and_timeout`, `test_merge_queue_confirmation_failure_does_not_repeat_request`, `test_confirmed_merge_registers_post_merge_after_shipped` |
| D30 Work-repo decomposition to size:s | `conformant` | `ship._review_required` (size and risk gating), `ci_blockers.record`/`holds` | `tests.test_ci_blockers`, `tests.test_ship.ShipTests` size-gating cases |
| D33 Measured cheapest variant (run evidence) | `conformant` | The run-evidence capture in this path is `finalize`/`runner` log and usage capture; ranking itself belongs to the routing audit (#1673) | `tests.test_runner.SnapshotTests`, see `1670-routing-quota-accounts.md` |
| D40 Desktop gate | `conformant` | `desktop.run_command`, `desktop.check`, `desktop.Deferred`, `desktop.in_window`; only a `GATE-DENIED` line means deferral | `tests.test_desktop.CheckTests`, `CliTests`, `ProcessTests`, `SessionTests` |
| D47 Repository-scoped GitHub tokens | `conformant` | `runner.run_env`, `gh.GH` App identity (`app:<id>:<installation>`), cross-repository guard | `tests.test_runner.RunEnvTests.test_app_backed_run_env_drops_ambient_credentials_and_uses_scoped_token`, `test_scoped_run_token_least_privilege_permissions`, `test_require_scoped_identity_blocks_ambient_credentials_and_personal_app`, `tests.test_gh` (`test_native_children_pagination_and_cross_repository_guard`) |
| D50 Unattended smoke, sleeping display | `conformant` | `desktop.wake_display`, `desktop.probe_password_free`, `desktop.parse_session`; the gate never types a password | `tests.test_desktop.SessionTests.test_lock_needing_a_password_defers`, `test_probe_failures_deny` |
| ARCHITECTURE Run flow | `conformant` | `runner.prepare`, `runner.fence_hooks` (epoch pre-push fence), `runner.run_env` (`MAHLER_*` env, epoch), `runner.launch` (detached), `runner.snapshot`; `prompt.py` recipes; `no_change.begin/finish/advance` with epoch fence | `tests.test_runner.FenceHookTests`, `RunEnvTests.test_run_env_contains_fence_and_epoch`, `DetachedRunTests`, `tests.test_no_change.ClaimTests.test_run_and_epoch_fence_and_replayed_verdict` |
| ARCHITECTURE Shipping flow | `conformant` | `ship._open_pr` (adds `Fixes #N`), CI watch and red-CI fix start, review gate, base and lease recheck then squash merge, `ship._shipped`, `post_merge.register` and `post_merge.advance` | `tests.test_ship`, `tests.test_post_merge.PostMergeTests`, `NamedFailureReleaseTests` |

## Trust-boundary checks

- **Repository-scoped identity:** run env drops ambient credentials and carries only the run's scoped token (D47 tests above).
- **Epoch fencing:** pre-push fence installed by `runner.fence_hooks`; no-change verdicts are bound to run and epoch and a replayed verdict is refused.
- **Current-head evidence:** review verdicts are bound to a PR and head SHA and invalidated when the head moves. Merge now also requires a syntactically valid head SHA (the fix above).
- **Desktop gate:** deferral only on `GATE-DENIED`; deferral is reported as outstanding coverage, never a pass. No GUI-driving command was run by this audit.
- **No-change handling:** `no_change.finish` and `advance` reject unverifiable free-text checks and a false PR criterion (`tests.test_no_change`).

## Superseded abstractions

Searched the bounded source files for manual-verdict, agent-owned-shipping and pre-conductor vocabulary. No live branch implements them. One retained reference:

- `gh.UAT_PASS_COMMENT` and its use in the completion-evidence classifier (`gh.py`, near line 1781). It is not an owner pass step: it recognises a console-generated comment as one accepted kind of attributable evidence that moves `shipped` to `done` (ARCHITECTURE Shipping flow step 5). It is kept, and `tests.test_sync_uat` covers it.

## Remaining coverage

None outstanding for the hermetic scope. Live GitHub merge behaviour and real desktop smoke were deliberately not exercised.
