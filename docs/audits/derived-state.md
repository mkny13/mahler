# Stored and derived item state audit

Audit date: **2026-10-06**. Scope: [#712](https://github.com/mkny13/mahler/issues/712).
Code snapshot: main at **dc30f78a5305960197fce71e2b964c29d6ae408b**
(the worktree HEAD and `origin/main` agreed at audit start). GitHub searches below
were run on that date; their results are a separate, mutable snapshot. No runtime
behavior, schema, leases, live ledger, or labels were changed by this audit.

## Finding

Keep the workflow state machine for now. `items.state` combines instructions,
accepted handoffs, and lifecycle projections; the other columns do not yet record
enough structured history to reconstruct all three without guessing. In particular,
an open PR is not proof that its owner handed it to the conductor. Removing this
column is not a size:m change.

Two small conversions are worthwhile: derive fix-running status from active runs,
and derive effective review-pending status from a stored launch binding and its
run. These remove duplicate lifecycle flags and make stale observations less
misleading. They do **not** replace the epoch fence that fixed #698/PR #699.
A stale writer can corrupt a branch or PR pointer even if the display is derived.

The strongest historical opportunities concern fact identity and precedence:
which ownership epoch, attempt cycle, PR, head, and observation a value belongs
to. Moving an incorrectly scoped predicate to a reader does not make it correct.

## Method and notation

Read the schema and `STATES` in [ledger.py](../../mahler/ledger.py), label mappings
in [gh.py](../../mahler/gh.py), and production callers in sync, finalization,
shipping, scheduling, CLI, MCP, and console. References below use
`module:function` relative to `mahler`; class-qualified methods are explicit.
Searches of calls and SQL were cross-checked against surrounding functions,
including dynamic key prefixes and clears. Tests are evidence, not production
writers. The finite required scope is the state vocabulary, lease/run fields,
mirror, and 21 KV families listed in #712; adjacent facts are listed separately.
Project-wide routing/quota state and browser presentation state are not item state.

- **F — stored fact:** a decision, command, ownership grant, result, or historical
  observation needed after restart. A fact can be updated under its own protocol;
  this does not imply an immutable event store exists today.
- **C — external mirror/cache:** preserve storage for polling, offline reads,
  operator visibility, or detecting edits; it is not independent authority.
- **D — derivable:** exact inputs and a read rule are given. This can describe
  only a subfield of a mixed record. “Derivable in a redesigned schema” is not
  called a ready-to-delete value here.

## Item state inventory

All state writes below ultimately use `ledger:Ledger.set_state` /
`ledger:Ledger.upsert_item` (the latter also initializes `inbox`). Generic
`sync:sync` imports a single recognized state label for a newly seen issue,
so it is an additional writer for every label-backed state. The migration in
`ledger:Ledger.__init__` changes legacy `tracking` to `parent`.
Generic readers for every row are `ledger:Ledger.items`, `cli:cmd_status`,
`sync:mirror_labels`, and `console.state:build`. Specialized readers follow.
This table lists actual writers, including recovery paths, not just the usual
happy path.

| `items.state` value | Production writers in addition to generic import | Readers beyond generic presentation | Classification and derivation/retention reason |
|---|---|---|---|
| `inbox` | `sync:_process_comments`, `sync:_apply_instruction`; `finalize:_setup_failure`, `finalize:retry_or_fail`, `finalize:_ended_out_of_reach` | `tick:_candidates`, `sync:planned_child` | **F**, retain unsorted/re-sort instruction. `sorted_at IS NULL` is insufficient: parked, failed and actively sorting items can also be unsorted. A future projection needs explicit intake/re-sort and hold facts. |
| `ready` | `sync:_born_ready`, `sync:resume_item`; `finalize:_sorted_ready`, `finalize:_ended_preempted`, `finalize:_ended_out_of_reach`, `finalize:_setup_failure`, `finalize:retry_or_fail`; `ship:_rebuild_on_base`; `tick:sweep_orphans`; `ledger:Ledger.release`, `ledger:RoutedLedger.release` (default `to_state`) | `tick:_candidates`, `tick:_check_eligible` | **F**, retain accepted scheduling/retry decision. Sorted, unleased work is not necessarily ready: it may be shipped, explicitly paused, failed or handed off. Deriving eligibility from ready plus dependencies/capacity is already appropriate; deriving this authorization needs additional facts. |
| `working` | `cli:cmd_claim`, `mcp:serve`, `tick:start`, `ship:_ship_lease`, `finalize:_ended_preempted` | `ledger:Ledger.orphan_working_items`, `ledger:Ledger.release`, `tick:sweep_orphans` | **D** for the activity projection: current live non-conductor interactive/primary lease, or current-epoch active build/fix run. Sort/review execution must not automatically change the workflow phase. Keep storage until that distinction and orphan recovery are separated; an activity badge alone cannot replace current scheduler queries. |
| `verifying` | `cli:cmd_ship`, `console.revert:revert` (direct upsert); `finalize:_ended_done`, `finalize:_apply_verify_fallback`, `finalize:_review_passed`, `finalize:_review_failed`, `finalize:_review_inconclusive`; `ship:recover_capacity_waits` | `ship:_ship_project`, `finalize:_close_the_books`, `tick:_init_schedule_state`, `console.state:_verification_wait` | **F**, retain accepted ship/handoff intent. PR presence or last DONE is not sufficient after preemption, closure, rebuild or manual re-ship. Its substatus (CI/review/fix/merge wait) is a derivation over scoped observations. |
| `needs_you` | `finalize:_needs_you`, `finalize:_setup_failure`; `ship:_ci_pending`, `ship:_merge_queued`, `ship:_capacity_wait` | `sync:_process_comments`, `ship:migrate_capacity_waits`, `ship:recover_capacity_waits`, `failures:backfill` | **F**, retain the raised question/hold and its generation. Technical capacity is partially derivable, but owner questions must never disappear merely because routing changed. `capacity-stranded:` distinguishes recoverable technical holds. |
| `parked` | `sync:_apply_instruction`, `finalize:_ended_parked`, `finalize:_repeated_no_work` | `watchdog:watchdog`, `tick:_candidates` | **F**, retain explicit pause or repeated-no-work technical hold. Neither follows from absence of an active run. |
| `failed` | `finalize:retry_or_fail`, `ship:_review_triggered_fix`, `ship:_red_ci` | `cli:cmd_ship`, `sync:resume_item`, `failures:backfill` | **F**, retain exhausted-cycle decision. Attempts alone omit exploration/capacity exemptions, reset boundaries, and post-PR budgets; historical run count is not the budget. |
| `parent` | `finalize:_sorted_split`; migration above | `sync:planned_child`, `sync:_close_finished_parents_project` | **F**, retain accepted SPLIT/planning outcome; one child link or a `type:goal` label does not prove planning completed. Children may be filed while the parent is still planning (#236). |
| `shipped` | `ship:_shipped` (also reached through `sync:reconcile_shipped`) | `ledger:Ledger.accept_evidence`, `sync:reconcile_shipped`, `sync:mirror_labels`, `ship:mirror_shipped` | **D** as presentation: confirmed merge evidence for this delivery, with no accepted completion evidence for it. Retain current transition: it drives release/UAT side effects and needs explicit delivery identity and legacy reconciliation before elimination. Never derive from “PR not OPEN.” |
| `done` | `sync:sync` (external closure), `sync:_close_finished_parents_project`; `finalize:finalize` (closed issue); `ledger:Ledger.accept_evidence` | `tick:_candidates` (dependencies), `sync:_close_finished_parents_project`, `ledger:Ledger.items` | **F**, retain completion/closure decision. Verified shipment, external issue closure and completed parent are different paths. A future projection needs typed closure evidence for all paths, not only merge/UAT evidence. |

### SQLite authority and lifecycle fields

| Surface | Production writers | Readers | Class and reason |
|---|---|---|---|
| `items.state` overall | All state writers above; `ledger:Ledger.set_state`, `ledger:Ledger.upsert_item` | State table above; indexed `ledger:Ledger.items` | **F**, mixed workflow decision record; two conceptual projections do not justify dropping the column. |
| `items.mirror` | `sync:mirror_labels`, `sync:mirror_done`, `ship:mirror_shipped`, `ledger:Ledger.accept_evidence` | `sync:_adopt_label_edits`, `sync:mirror_labels`, `ship:mirror_shipped` | **C**, last acknowledged external mirror. Cannot recompute it from desired current state: the difference detects human label edits and unfinished mirroring. |
| `items.epoch` | `ledger:Ledger.claim`; `ledger:RoutedLedger.claim` remembers canonical epoch with `MAX` | `finalize:_fenced_out`, `ledger:Ledger.claim` | **F**, monotonic ownership fence survives release; indispensable to #699 and D24. |
| `leases.project`, `number`, `holder`, `kind`, `platform`, `epoch`, `run_id` | `ledger:Ledger.claim`; removed by `ledger:Ledger.release`; `ledger:RoutedLedger.claim` / `ledger:remote_lease_operation` relay canonical operations | `ledger:Ledger.lease`, `ledger:Ledger.lease_check`, `ship:_ship_lease`, `finalize:_fenced_out` | **F**, atomic ownership identity, not inferred from a process or a GitHub label. |
| `leases.acquired_at`, `heartbeat_at`, `expires_at` | `ledger:Ledger.claim`, `ledger:Ledger.heartbeat`; release deletes row | `ledger:Ledger.lease`, `ledger:Ledger.expired_leases`, `tick:expire` | **F** timestamps; **D** live/expired = `expires_at > now`, already computed. Preserve TTL and CAS authority. |
| `leases.capacity`, `external` | `ledger:Ledger.claim` (capacity also on renewal) | `ledger:Ledger.claim`, `ledger:Ledger.orphan_lease_rows`, `tick:_init_schedule_state` | **F**, reservation and D24 ownership attributes. Deriving capacity only from local processes misses interactive and remote owners and the atomic admission window. |
| `runs.status`: `running`, `stopping`, `ended` | `ledger:Ledger.create_run` / `ledger:Ledger.update_run` through `tick:start`, `watchdog:stop`, `finalize:_close_the_books`, `finalize:_setup_failure`, `finalize:_try_cline_nudge` | `ledger:Ledger.active_runs`, `watchdog:watchdog`, `ship:_review_gate`, `ship:_clear_charged_if_fix_completed` | **F**, supervisor lifecycle: active means not yet accounted/finalized, not simply PID alive. A dead process can still need finalization. |
| `runs.stop_reason` | `watchdog:stop`, `watchdog:request_stop`, `finalize:_close_the_books` via `ledger:Ledger.update_run` | `finalize:finalize`, `ship:_clear_charged_if_fix_completed`, `ship:_unchanged_done_review` | **F**, reason for stopping changes retry accounting; cannot recover reliably from the final exit code. |
| `runs.outcome` | `tick:start` (not claimed/launch failed), `finalize:_close_the_books`, `finalize:_setup_failure` | `ledger:Ledger.last_run`, `ship:_unchanged_done_review`, `ship:_clear_charged_if_fix_completed`, `scorecard:attempts` | **F**, accepted result, including stale run accounting. Logs can disappear and a DONE result is not proof of a merge. |
| `runs.epoch`, `role`, `started_at`, `ended_at`, `exit_code`, `yield_at` | `ledger:Ledger.create_run`, `ledger:Ledger.claim` (preemption yield), `tick:start`, `watchdog:request_stop`, `finalize:_close_the_books`, `finalize:_setup_failure` | `finalize:finalize`, `ledger:Ledger.active_runs`, `ledger:Ledger.last_run`, `ship:_review_route` | **F**, identity, request and execution evidence; elapsed duration can be computed from timestamps, ownership cannot. |
| `items.state_changed_at`, `sorted_at` | `ledger:Ledger.set_state` / `ledger:Ledger.upsert_item`; `sync:_born_ready`, `sync:resume_item`, `sync:_apply_instruction`, `sync:_process_comments`, `finalize:_sorted_ready` | `ship:_mark_capacity_wait`, `ship:recover_capacity_waits`, `tick:_candidates`, `finalize:retry_or_fail` | **F**, transition generation and sort/re-sort evidence. Do not derive generation from a display string or last arbitrary event. |
| `items.branch`, `pr`, `summary` | `cli:cmd_ship`, `tick:start`, `finalize:finalize`, `finalize:_ended_done`, `finalize:_apply_verify_fallback`, `ship:_open_pr`, `ship:_closed_pr`, `ship:_rebuild_on_base`, `ship:_review_triggered_fix`, `ship:_red_ci`, `console.revert:revert` via upsert/state | `ship:_ship_item`, `ship:_open_pr`, `runner:prepare` | **F**, accepted handoff pointers and summary. Same-title/open PR search is not ownership evidence. |
| `items.question`, `options` | `finalize:_needs_you`, `finalize:_setup_failure`, `ship:_ci_pending`, `ship:_merge_queued`, `ship:_capacity_wait`, `ship:recover_capacity_waits`, `sync:resume_item` | `sync:_process_comments`, `ship:migrate_capacity_waits`, `console.state:build` | **F**, question and answer contract; old text is not an authoritative capacity classification. |
| `items.attempts`, `setup_fails`, `esc_tier`, `esc_fails` | `finalize:retry_or_fail`, `finalize:_setup_failure`, `ledger:Ledger.bump_setup_fails`, `ledger:Ledger.reset_setup_fails`, `ship:_open_pr`, `ship:_review_triggered_fix`, `ship:_red_ci`, `cli:cmd_ship`, `sync:resume_item` | Same accounting functions; `tick:_route`, `failures:report` | **F**, bounded-cycle accounting and routing decisions. Counting runs omits capacity/exploration exemptions and manual reset boundaries. |

### Item-scoped KV inventory (21 required families)

`P#N` denotes project and issue; suffixes matter. `ledger:Ledger.set_kv` is the
common storage primitive. Listed clears are writers too. A “none” reader or
writer is intentional, not a missing attribution.

| Family / stored contents | Production writers (including clear/reset) | Production readers | Class and read rule or reason to retain |
|---|---|---|---|
| `attempt_cycle:P#N` UUID | `cli:cmd_ship` | `failures:report` | **F**, explicit retry generation; preserved history must not collide on reused attempt numbers. |
| `ci:P#N:PR` state, sha, base, since | `ship:_watch_pr`, `ship:_ci_pending`; cleared by `ship:recover_capacity_waits` | `ship:_watch_pr`, `ship:_ci_pending`; `console.state:capacity_wait_text`, `console.state:_verification_wait` | **C** check observation plus **F** first-wait clock. State is derived by `gh:checks_state` from a fetched rollup, but the raw rollup is not persisted here. Keep the cache to avoid network work on console reads; do not turn a cached green into merge authority. `pending` can also mean lookup/update uncertainty, so removing it requires a richer observation schema. |
| `mergeability:P#N:PR` sha/base, observations, refreshes, since, last_refresh, error | `ship:_mergeability_observation`; clear in `ship:recover_capacity_waits` | Same observer, `ship:_watch_pr`, `console.state:_verification_wait` | **C** external observation, **F** refresh/backoff history. Keep restart-safe counters; an UNKNOWN display is derived only when sha/base matches the CI observation. REST refresh is a trigger, not proof of safety. |
| `queue:P#N:PR` sha/since | `ship:_merge_queued`; clock restarted by `ship:recover_capacity_waits` | Same functions; `console.state:_verification_wait` | **F**, merge-request receipt/dedup marker. Open+green does not prove a request was issued; recovery must not clear its SHA and request twice. |
| `review:P#N` pr/sha/run_id, verdict, findings/classification | `ship:_start_review_run`, `ship:_update_reviewed_pr`, `ship:_review_triggered_fix`, `ship:_unchanged_done_review`; `finalize:_update_review_kv` via review handlers; `finalize:_review_inconclusive` clears | `ship:_review_gate`, `ship:_update_reviewed_pr`, `ship:_unchanged_done_review`, `ship:_review_triggered_fix`; `finalize:_update_review_kv`, review handlers; `failures:report`; `console.state:capacity_wait_text`, `console.state:_verification_wait` | **F** terminal pass/fail, findings, launch identity and invalidation; **D** effective pending. For matching PR/head launch binding, pending iff referenced run matches item/role=review and status is running/stopping; otherwise required/unknown unless a valid terminal verdict applies. R2. Never derive a verdict from a process exit or mere DONE. |
| `reviewed-pr:P#N` retained PR number | `ship:_update_reviewed_pr` | Same function; `ship:_review_gate` | **F**, sticky decision to preserve reviewed work across head changes. Current verdict may have been invalidated; removing this loses that history. |
| `review-wait:P#N` state, sha, at, reason, tier, retry_at, recovery_at | `ship:_capacity_wait` via `ship:_start_review_run`; cleared by `ship:_start_review_run`, `ship:recover_capacity_waits` | `ship:_capacity_wait`, `ship:migrate_capacity_waits`, `console.state:capacity_wait_text` | **C** routing observation + **F** wait start. Classification could be recomputed from eligible-route facts, recovery knowledge, at/now and max-hours, but eligible-route facts are not fully retained. Keep until observation schema separates them; request-time routing would be costly and inconsistent with tick snapshot. |
| `review-pinged:P#N` legacy marker | `ship:_start_review_run`, `ship:recover_capacity_waits` only clear it | None at snapshot | **F**, legacy notification receipt, now unused. No new nonempty production writer. Cleanup is not a state derivation and has no demonstrated race benefit. |
| `reviewfix:P#N:PR:SHA`, `:charged`, `:run` | `ship:_review_triggered_fix`, `ship:_clear_charged_if_fix_completed`; reset by `cli:cmd_ship` | Same functions; `ship:_unchanged_done_review` | **F**, failure-cycle start, charged-once receipt and launched fix/verdict binding. Current red/fail status cannot reconstruct which charge already happened. |
| `reviewfix-status:P#N` structured wait or `{state:running}` | `ship:_capacity_wait` via `ship:_fix_wait`; `ship:_review_triggered_fix`, `ship:_red_ci` write running; `ship:recover_capacity_waits` clears | `ship:_capacity_wait`, `ship:migrate_capacity_waits`; `console.state:capacity_wait_text`, `console.state:_verification_wait` | **C/F** wait observation/clock as above; **D** running iff an item-scoped active run has role=fix. Drop the running-only sentinel, preserve wait facts and derive activity with precedence over an old wait. R1. |
| `reviewdup:P#N` sha/reviewer | `ship:_review_triggered_fix` | Same function; `ship:_review_route` | **F**, records that an alternate opinion has already been requested for a repeated finding. Recomputing similarity alone could request a new second opinion every tick. |
| `reviewfindings:P#N` historical failed rounds | `finalize:_review_failed` | Same function; `finalize:_review_passed`; `ship:_repeat_finding`, `ship:_review_not_converging` | **F**, historical findings/identity/source evidence, including legacy unclassified results; current verdict is lossy. |
| `reviewconvergence:P#N` history offset | `finalize:_review_passed` | `ship:_review_not_converging` | **F**, legacy reset boundary on pass. `reviewfindings` contains failures, so its length cannot identify where the last pass happened. Preserve until legacy evidence is migrated. |
| `reviewdrift:P#N:PR` sha/rounds/reason | `ship:_review_not_converging` | No production reader at snapshot | **D**, diagnostic: reproduce the same function over `review:window` (or legacy findings from convergence offset), require ≥3 rounds ending at current head, count successive new file sets with no overlap, reset divergence on empty/overlap; drift iff ≥2 divergent transitions. Defer deletion: not a routing authority or active lost-update surface; stored snapshot preserves the explanation at that time. |
| `reviewresults:P#N` per-run findings, PR/SHA, filing marker/URL/linked | `review:remember`, `review:file_followups` | Same functions | **F**, durable intake/outbox and remote delivery receipts. Reconstructing from current verdict loses findings; external marker reconciliation handles uncertain delivery. |
| `reviewrounds:P#N:PR` ordered distinct heads with usable reviews observed green | `review:record_green` | `review:window`, `review:start_context`, `ship:_review_not_converging` | **F**, historical conjunction of usable verdict and observed green CI. Current CI cannot establish that historical conjunction; round number is already derived by `review:round_number`. |
| `red:P#N:PR:SHA`, `:charged` | `ship:_red_ci`, `ship:_clear_charged_if_fix_completed`; reset by `cli:cmd_ship` | Same functions | **F**, failure-cycle/charge receipts. Retain to prevent per-tick escalation (#232) and repeated charging across capacity interruption (#657). |
| `capacity-stranded:P#N` role/pr/at | `ship:_mark_capacity_wait` via `ship:_capacity_wait`, `ship:migrate_capacity_waits`; cleared by `ship:recover_capacity_waits` | `ship:migrate_capacity_waits`, `ship:recover_capacity_waits` | **F**, provenance and generation of recoverable technical pause. A fresh owner question must not inherit recovery from an old pause; `at == state_changed_at` and PR match are essential. |
| `ship_snapshot:P#N` ref | `ship:_open_pr` | `ship:_closed_pr` | **F**, recoverable saved-work pointer before PR branch changes; current PR head cannot recreate the original snapshot reliably. |
| `unconfirmed:P#N` flag | `finalize:_apply_verify_fallback` | `ship:_ship_item` | **F**, provenance: verify fallback succeeded without agent confirmation. DONE/PR existence does not encode this distinction. |
| `failed_comment:P#N:attempt:run:setup[:cycle=UUID]` payload or sent | `failures:report`, `failures:_retry_pending` | Same functions; `failures:backfill` | **F**, durable diagnostic outbox and delivery receipt. Historical failures still need delivery after state changes; network reads alone cannot replace local pending intent. |

Adjacent facts found while tracing: `reviewdone:` in
`ship:_unchanged_done_review` is a consumed same-head re-review allowance, not a
status; `no_work_done:` records in `finalize:_repeated_no_work` retain run/base/signature
history for a technical hold. Neither should be deleted as derived display state.
`items.labels`, title/body, dependencies, files, parent and priority are imported
GitHub content/cache (`sync:sync`), not an alternative ownership authority.
`completion_evidence` accepted through `ledger:Ledger.accept_evidence`, UAT and
release records preserve observed completion/merge facts and side effects. The
console outbox queues commands; its revert handler delegates to
`console.revert:revert`, which directly upserts the new revert item as verifying.

## GitHub status-label inventory

**C for every row below.** Keep external labels for GitHub/operator visibility and
input compatibility. The desired label is derived from the accepted workflow
state with `gh.STATE_LABELS`; the external write and last-mirror receipt remain
stored. `gh:GH.set_state_label` is the common writer; it removes competing
recognized state labels. `sync:_state_from_labels` / `sync:_adopt_label_edits`
read them, via `sync:sync`. Only ready/parked/inbox edits are adopted as commands
for existing items; arbitrary labels must not become lock/merge authority.

| Label | State | Production writer/caller | Reader / special behavior |
|---|---|---|---|
| `mahler:inbox` | inbox | `sync:mirror_labels` → `gh:GH.set_state_label`; intake via `review:file_followups`, `console.outbox:capture` → `gh:GH.create_issue` | Common readers; intake creates unsorted work. |
| `mahler:ready` | ready | `sync:mirror_labels` → `gh:GH.set_state_label` | Common readers; existing-item edit means go/resume. |
| `mahler:working` | working | `sync:mirror_labels` → `gh:GH.set_state_label` | Common readers; visibility only, never lease authority. |
| `mahler:verifying` | verifying | `sync:mirror_labels` → `gh:GH.set_state_label` | Common readers; cannot independently authorize shipping. |
| `mahler:needs-you` | needs_you | `sync:mirror_labels` → `gh:GH.set_state_label` | Common readers; question remains in ledger/issue history. |
| `mahler:parked` | parked | `sync:mirror_labels` → `gh:GH.set_state_label` | Common readers; existing-item edit is a pause instruction. |
| `mahler:failed` | failed | `sync:mirror_labels` → `gh:GH.set_state_label` | Common readers; retry accounting remains local fact. |
| `mahler:parent` | parent | `sync:mirror_labels` → `gh:GH.set_state_label` | Common readers; replaces legacy tracking label. |
| `mahler:shipped` | shipped | `ship:mirror_shipped` → `gh:GH.set_state_label`, including reconciliation | Common readers; intentionally survives GitHub closure until evidence-backed done. |
| `mahler:tracking` | legacy alias for parent | No new emission; `gh:GH.set_state_label` removes it during mirroring | `gh.LABEL_STATES`, `sync:_state_from_labels`; legacy compatibility only. |

`done` deliberately has **no** `mahler:done` mapping. `sync:mirror_done` calls
`gh:GH.set_state_label(..., "done", ...)` to remove state labels. Other
`mahler` scope/intake labels are backlog selection, not additional state values.

## Recommendations (priority order)

### R1 — derive fix activity; size:s, p2

Builder issue: [#757](https://github.com/mkny13/mahler/issues/757).
Remove the two running-sentinel writes in `ship:_review_triggered_fix` and
`ship:_red_ci`. Successful launch clears the obsolete wait; read-time activity
comes from item-scoped `ledger:Ledger.active_runs` with role=fix. Keep structured
waits, retry clocks and charge receipts. Console readers give an active fix
precedence over old wait observations; ended runs cannot remain running because
of a KV marker. Old `{state:running}` rows are safely ignored.

Race reduction: removes an extra lifecycle write after launch and the need to
clear it on every exit. This is preventive simplification, not a claim that a
listed incident was caused by this sentinel. Low implementation risk: existing
`capacity_wait_text` already suppresses waits while a fix/review is active.
Files: `mahler/ship.py`, `mahler/console/state.py`, corresponding ship/console tests.
Mechanical regression: launch → stopping → ended with an old running/wait marker;
activity must follow the run, failed launch must not show running, and another
item's run must not suppress this item's wait. Retain restart/capacity recovery
and D6 tests. No schema migration or dependency on R2.

### R2 — derive effective review pending; size:m, p2

Builder issue: [#758](https://github.com/mkny13/mahler/issues/758).
Keep a durable launch binding (`pr`, `sha`, `run_id`) and terminal verdict evidence;
stop writing pending as a verdict. Put effective-status computation in
`mahler/review.py`, shared by ship gate and console: match PR/head, accept valid
terminal pass/fail; otherwise pending only when the bound run is the same
item's active review, else required/unknown. Preserve explicit invalidations
and conservative duplicate-start guards. Never use this display rule in place
of live merge checks or epoch fencing.

Race reduction: run completion cannot leave a stale pending assertion as a second
lifecycle authority. Console and conductor stop interpreting that assertion
differently. Medium risk: head binding and required/invalidation semantics are
merge-sensitive. Files: `mahler/review.py`, `mahler/ship.py`, `mahler/finalize.py`,
`mahler/console/state.py`, and their test files (a focused `tests/test_review.py` may be added). Legacy pending records
are read through the same conservative run correlation; missing run identity
must not fabricate activity. Terminal historical evidence stays unchanged.
Mechanical regression: current versus replaced head/PR; active/stopping/ended/
missing/wrong-item/wrong-role run; pass/fail and required invalidation; interactive
preemption during finalization. Assert console/gate agreement, no duplicate
review, and no unsafe merge. Independently mergeable with or without R1.

### Retain or defer

- **D6 leases, epochs, run outcomes and merge requests:** retain as facts. Removing
  `queue:` or charging receipts invites duplicate side effects after restart.
- **Whole `items.state`, working/shipped projections:** defer. Need typed ship,
  close, pause, resume and completion facts plus atomic transition/side-effect
  compatibility. A display-only overlay would not fix stale scheduler writes.
- **CI and mergeability caches:** retain. GitHub must be polled, and console reads
  must not do live network work. Keep observed head/base identity and wait clocks;
  fresh merge-time checks remain mandatory. “Derive from raw rollup” only moves
  storage unless a shared raw observation cache is deliberately introduced.
- **Capacity classifications:** defer broader derivation. Current wait records
  omit some routing inputs; changing configuration, quota and time during a page
  request would disagree with the scheduler. Retain technical-pause provenance.
- **Attempts/escalation:** defer reconstruction. Need durable typed cycle/charge
  facts covering every exemption and reset, rather than counting ended runs.
- **Review histories, sticky reviewed PR, convergence offset, duplicate-review
  receipts:** retain. Current verdict does not encode historical decisions.
- **`reviewdrift:` and unused `review-pinged:`:** no follow-up recommended. Removing
  dead/diagnostic storage offers little race reduction; evidence snapshots can
  be useful. They do not warrant expanding this audit into cleanup work.
- **External labels and `items.mirror`:** retain for visibility and command/echo
  detection; desired label calculation is already derived.

There are exactly **two positive recommendations and two builder issues**. Both
are `type:chore`, `p2`, carry one size label, link back to #712, preserve stored D6
authority, and name files and mechanical regression checks.

## Incident appendix

Window: closed since **2026-08-07**, approximately 60 days before this audit.
These are capped issue searches, not an exhaustive claim about all defects.
GitHub's unquoted `stale state` search is deliberately reproduced as requested;
it returns broader matches than a phrase search. PR #699 is not in the issue
search result set, so it is followed explicitly. Historical descriptions below
refer to code at the incident, not an assertion that the snapshot still has the bug.

### Starting points and linked evidence

- [PR #699](https://github.com/mkny13/mahler/pull/699) fixes
  [#698](https://github.com/mkny13/mahler/issues/698). Its review comments broadened
  fencing from sort to build/setup/review mutations, snapshot-time preemption,
  preservation of stale work and D24 remembered epochs. Follow-ups
  [#700](https://github.com/mkny13/mahler/issues/700) (lost test discovery) and
  [#701](https://github.com/mkny13/mahler/issues/701) (long write transactions)
  were inspected; #700 is a test-placement defect, not a lost-update incident.
- [#433](https://github.com/mkny13/mahler/issues/433) names review budget leakage,
  stray fix branches, repeated fixes, reviewer identity and unreachable escalation.
  Its shipping comment names [PR #443](https://github.com/mkny13/mahler/pull/443)
  as closed; API verification confirms CLOSED with `mergedAt=null`, not a merge receipt. Its owner
  comment separately identifies [#440](https://github.com/mkny13/mahler/issues/440)
  (closed-unmerged PR treated as done), ultimately fixed in
  [PR #457](https://github.com/mkny13/mahler/pull/457).
- [#675](https://github.com/mkny13/mahler/issues/675) is a parent, not one atomic
  race fix. Followed [#676](https://github.com/mkny13/mahler/issues/676) /
  [PR #687](https://github.com/mkny13/mahler/pull/687) for mid-run provider capacity
  classification, and [#677](https://github.com/mkny13/mahler/issues/677) /
  [PR #692](https://github.com/mkny13/mahler/pull/692) for retaining reviewed PRs.
  Also inspected [#657](https://github.com/mkny13/mahler/issues/657) /
  [PR #662](https://github.com/mkny13/mahler/pull/662), which preserve retry budgets
  across capacity-interrupted fixes/failed launches. The triggering incident was
  #618/PR #619; #618 itself is a documentation task, not a separate state bug.
- Search hit [#224](https://github.com/mkny13/mahler/issues/224) is an audit parent.
  Followed its children: #232 (per-tick escalation) and #233 (yield ordering) are
  included below; #230 (path parser), #231 (cross-project collision scope), and
  #234 (backup exception handling) are excluded from the state-race set.

### Included incidents and counterfactuals

“Would prevent” means a correctly scoped fact-based design, not simply renaming
or moving a state calculation. **Neither R1 nor R2 alone is claimed to fix these
already-closed incidents.** The table distinguishes broader possibilities from
what this audit actually recommends implementing.

| Incident | Competing writers / stale value / facts available | Would derivation have prevented it? |
|---|---|---|
| #698 / PR #699 | `cli:cmd_claim` / `cli:cmd_ship` wrote working → verifying and new branch/PR; late `finalize:_sorted_ready` wrote ready. Build paths could also overwrite the branch. Run epoch and newer item/canonical epoch survived release. | **Only partially in a broader design:** an epoch-scoped ship-intent fact taking precedence over a stale sort result would preserve phase. Deriving display from last outcome would not. Branch/PR mutations and work preservation still require fencing. Current fix is the correct immediate protection; R1/R2 do not replace it. |
| #236 | `sync:sync` first imported child as inbox while parent planner ran; `finalize:_sorted_split` later wrote parent, but ETag short-circuit skipped child reconsideration before `tick:schedule`. Parent run, child plan/body and final split outcome were available. | **Yes for the readiness subproblem** if eligibility derives born-ready from planned-child facts and active parent planning before every launch. No need to derive all workflow state; the shipped reconsideration/ETag and planner-run checks solve the targeted race. |
| #671 | `ship:recover_capacity_waits` restored verifying; `ship:_watch_pr` / `_ci_pending` reused an expired CI clock and conflated UNKNOWN mergeability with pending checks. Current head, terminal checks, target and recovery transition existed; remote mergeability needed refresh. | **Partial:** deriving wait kind from correctly scoped observations prevents the false “CI running” classification. It cannot refresh GitHub or reset historical clocks without a new wait generation. R1/R2 do not replace the shipped refresh/reset fix. |
| #474 | Successive `finalize:_review_failed` writes replaced current `review:` findings; `ship:_review_triggered_fix` consumed the current failure without historical convergence context. Prior logs/comments existed but structured round history did not. | **No from then-current local facts:** derivation requires persisting missing history first. Once saved, convergence is derived. Its original owner-escalation policy has since changed (#678/#679); do not revive it through this audit. |
| #433 | `finalize:retry_or_fail` left pre-PR counters for `ship:_review_triggered_fix` / `_red_ci`; fix finalization left the same PR SHA, so the gate retried; latest run could be a review rather than builder. Run roles/outcomes and PR head existed, but charge-cycle and push-target identity needed repair. | **Partial:** reviewer identity can be derived from build/fix-role runs (as now). A typed PR-cycle charge history could avoid budget leakage; raw run counts cannot. Stray branch pushes require correct Git destination, not derived item state; duplicate starts need run binding and admission/fencing. |
| #675 → #676, #677 (with #657/PR #662) | `ship:_rebuild_on_base` discarded PR association/review value after base drift; new build's `finalize:retry_or_fail` consumed budget when provider overload was classified as crash. Capacity-stop/fix charging code could also charge twice after interruption. PR head/review verdict, base ancestry, provider log and run stop reason were inputs. | **No by status derivation alone:** these were update policy/error-classification/accounting defects, not competing state writers. Preserve reviewed-PR intent and charged-once receipts; correct the classifier and update the same PR. Deriving from the misclassified outcome reproduces the defect. |
| #440 | Rebuild/PR closure left an old PR pointer; `ship:_watch_pr` sent any non-OPEN observation to `_shipped`, recording false done and shipment side effects. GitHub explicitly reported CLOSED rather than MERGED. | **Yes for a merge-status projection** requiring confirmed merge evidence; **not automatically for side effects**. A direct corrected MERGED predicate prevents both without rewriting item state. Closure and completion must remain distinct. |
| #239 (related #211) | An independent merge advanced target after an isolated green check; conductor merge path trusted stale checks/mergeability. Current target tip and checked head could be fetched. No local state overwrite was necessary. | **No:** read-time checks still race an external base push. Need target ancestry/fresh-head checks and a pinned merge (or a real merge queue). The residual external race is documented in D19. |
| #701 | `finalize:finalize` held SQLite's writer lock during slow verification/network work; concurrent claims/heartbeats/ship timed out. No stored value was successfully overwritten. | **No:** shorten the transaction and atomically recheck epoch after slow work. A derived label does not remove authoritative lease writes. |
| #232 (via #224) | Repeated `ship:_red_ci` ticks incremented escalation before busy/no-platform return. Same red head and no new fix run were observable; failure-cycle receipt was missing. | **Not from CI color alone:** requires an idempotent cycle/charge fact. Deriving counts from properly scoped charges could work later; retaining `red:` receipts is smaller and restart-safe. |
| #233 (via #224) | Agent returned YIELDED before `watchdog` recorded preempted; `finalize:_dispatch` / `retry_or_fail` treated it as failure. Explicit STATUS verb and yield request were available. | **No:** the outcome classifier must recognize the verb regardless of watcher timing. Derived state using the wrong classifier still charges failure. |
| #654 | Review/fix wait handlers converted transient unavailable capacity to needs_you after verification timeout. Routing/reset/busy facts were available; a real owner question was conflated with a technical wait. | **Partial:** derived technical wait from capacity observations avoids that display conflation, but retry scheduling, unknown-recovery backstop and pause provenance still need stored facts and policy changes. Existing structured wait fix supplies these. |
| #486 | Conductor claim/handoff retained capacity while no run executed; `ledger:Ledger.claim` then refused other work/interactive claims. Lease ownership and local active runs were available. | **Partial:** derived local activity reveals idleness, but replacing stored capacity with local PID/run counts breaks atomic reservations and D24/interactive ownership. The shipped capacity=False conductor lease is the appropriate stored fact. |
| #680 | `cli:cmd_ship` changed phase without resetting exhausted counters/charging receipts; later ship failure accounting reused old cycle. Explicit manual retry and old counters were observable. | **No from existing run history alone:** manual retry must record a new budget generation. Derivation is safe only after that fact exists; keep `attempt_cycle:` and dedup history. |
| #706 | Fix returned DONE without new head; gate reused cached failed review as current and charged another fix without re-review. Fix run identity/outcome and original reviewed SHA were available. | **Partial:** effective status can distinguish a completed fix from an active review, but whether to grant re-review is a policy decision requiring a consumed allowance (`reviewdone:`). R2 must retain it; merely deriving pending does not solve same-head verdict validity. |
| #615 | Merge shipping and sync closure paths left stale verifying labels / collapsed shipped into done. Confirmed merge and completion evidence are distinct facts. | **Yes for lifecycle display given those facts**, but external GitHub labels still require writes and reconciliation. This is a mirror/lifecycle incident, not proof that labels should cease to be stored. |

### Reproducible capped search transcripts

Commands executed on 2026-10-06 (JSON number/title results, preserved below in
returned order):

```sh
gh issue list -R mkny13/mahler --state closed --search 'closed:>=2026-08-07 race' --limit 100 --json number,title
gh issue list -R mkny13/mahler --state closed --search 'closed:>=2026-08-07 overwrite' --limit 100 --json number,title
gh issue list -R mkny13/mahler --state closed --search 'closed:>=2026-08-07 stale state' --limit 100 --json number,title
```

No result set hit 100, so no pagination was needed for this specified search.
“Included” points to the incident table; “trace” identifies a parent used to
reach incidents. Every remaining returned issue has an exclusion reason. Search
matches alone are not evidence of a race.

#### race — 11 results

| Issue | Returned title | Disposition |
|---|---|---|
| [#236](https://github.com/mkny13/mahler/issues/236) | Planned sub-issues filed during their planner's run get re-sorted (D21 born-ready race) | Included in incident table. |
| [#698](https://github.com/mkny13/mahler/issues/698) | A finishing sort run overwrites an interactive claim+ship (verifying -> ready) | Included in incident table. |
| [#360](https://github.com/mkny13/mahler/issues/360) | Per-project since-last-look briefs in the console | Release or briefing feature; no distinct item-state lost update. |
| [#618](https://github.com/mkny13/mahler/issues/618) | Document Xcode relative-DerivedData policy and weekly cache cleanup | Documentation task; triggering runtime incident traced through #675. |
| [#278](https://github.com/mkny13/mahler/issues/278) | A 'Depends on:' naming the item's own parent deadlocks it forever | Dependency cycle validation, not an overwrite. |
| [#350](https://github.com/mkny13/mahler/issues/350) | Audit ai-tools and legacy agent dispatchers for relevance and retirement given Mahler | Documentation, audit, walkthrough or measurement scope; no distinct item-state lost-update incident established by this result. |
| [#301](https://github.com/mkny13/mahler/issues/301) | Occasional agent-driven console walkthrough (manually triggered, not scheduled) | Documentation, audit, walkthrough or measurement scope; no distinct item-state lost-update incident established by this result. |
| [#200](https://github.com/mkny13/mahler/issues/200) | Tier-weighted concurrency budgets: more slots for cheap platforms, fewer for expensive ones | Scheduling isolation/concurrency feature, not a reported lost-update incident. |
| [#197](https://github.com/mkny13/mahler/issues/197) | Area-label dependency-aware scheduling: safely raise concurrency (DESIGN Layer 3) | Scheduling isolation/concurrency feature, not a reported lost-update incident. |
| [#224](https://github.com/mkny13/mahler/issues/224) | Correctness Bug Scan | Trace: audit parent; #232/#233 included; other children excluded above. |
| [#239](https://github.com/mkny13/mahler/issues/239) | Hand-roll a pre-merge freshness check — GitHub's merge queue isn't available on personal-account repos | Included in incident table. |

#### overwrite — 7 results

| Issue | Returned title | Disposition |
|---|---|---|
| [#698](https://github.com/mkny13/mahler/issues/698) | A finishing sort run overwrites an interactive claim+ship (verifying -> ready) | Included in incident table. |
| [#701](https://github.com/mkny13/mahler/issues/701) | Review follow-up: mahler/finalize.py:547 — A routine build that exits without a STATUS line but has commits runs the verify fallback inside a BEGIN IMMEDIATE transaction; verification may take the configured 120 seconds. A concurrent intera | Included in incident table. |
| [#130](https://github.com/mkny13/mahler/issues/130) | Claude peak window: no new Claude runs weekdays 5–11am Pacific, with an override (D22) | Quota, account or warm-up policy/observations, outside item-state races. |
| [#348](https://github.com/mkny13/mahler/issues/348) | Notify on ntfy when Codex quota is exhausted | Quota, account or warm-up policy/observations, outside item-state races. |
| [#509](https://github.com/mkny13/mahler/issues/509) | janitor: rotate serve.err.log | Cleanup or backup infrastructure, not item-state authority. |
| [#357](https://github.com/mkny13/mahler/issues/357) | Manual CLI release preview and GitHub publication | Release or briefing feature; no distinct item-state lost update. |
| [#474](https://github.com/mkny13/mahler/issues/474) | Review loop: escalate to needs-you when successive review findings don't converge | Included in incident table. |

#### stale state — 50 results

| Issue | Returned title | Disposition |
|---|---|---|
| [#698](https://github.com/mkny13/mahler/issues/698) | A finishing sort run overwrites an interactive claim+ship (verifying -> ready) | Included in incident table. |
| [#543](https://github.com/mkny13/mahler/issues/543) | Refactor console idle-state complexity hotspot | Status presentation/layout work, not a separate stored item-state writer race. |
| [#133](https://github.com/mkny13/mahler/issues/133) | Proactive quota refresh: prevent periodic 15-minute stale flicker | Quota, account or warm-up policy/observations, outside item-state races. |
| [#671](https://github.com/mkny13/mahler/issues/671) | Persistent mergeable=UNKNOWN is treated as 'CI running' and re-escalates healed items | Included in incident table. |
| [#654](https://github.com/mkny13/mahler/issues/654) | Keep verification capacity waits retryable | Included in incident table. |
| [#701](https://github.com/mkny13/mahler/issues/701) | Review follow-up: mahler/finalize.py:547 — A routine build that exits without a STATUS line but has commits runs the verify fallback inside a BEGIN IMMEDIATE transaction; verification may take the configured 120 seconds. A concurrent intera | Included in incident table. |
| [#615](https://github.com/mkny13/mahler/issues/615) | Restore shipped as a visible lifecycle state after merge | Included in incident table. |
| [#656](https://github.com/mkny13/mahler/issues/656) | Show scheduled capacity retries in status views | Status presentation/layout work, not a separate stored item-state writer race. |
| [#337](https://github.com/mkny13/mahler/issues/337) | Refresh README, docs, and sanitize machine-specific artifacts | Documentation, audit, walkthrough or measurement scope; no distinct item-state lost-update incident established by this result. |
| [#442](https://github.com/mkny13/mahler/issues/442) | When hot hold is shown due to a recent interactive session, tapping it should | Project presence override feature; not item lifecycle state. |
| [#595](https://github.com/mkny13/mahler/issues/595) | Audit routing, quota, and account policy against the architecture specification | Documentation, audit, walkthrough or measurement scope; no distinct item-state lost-update incident established by this result. |
| [#597](https://github.com/mkny13/mahler/issues/597) | Reconcile architecture documentation after the drift audits | Documentation, audit, walkthrough or measurement scope; no distinct item-state lost-update incident established by this result. |
| [#678](https://github.com/mkny13/mahler/issues/678) | Persist classified review findings and file linked follow-up issues | Review evidence/convergence policy work; historical overwrite counted under #474. |
| [#679](https://github.com/mkny13/mahler/issues/679) | Bound green review rounds and converge through linked follow-ups | Review evidence/convergence policy work; historical overwrite counted under #474. |
| [#137](https://github.com/mkny13/mahler/issues/137) | Burst before a Claude window resets: use the expiring reserve (D23) | Quota, account or warm-up policy/observations, outside item-state races. |
| [#680](https://github.com/mkny13/mahler/issues/680) | Reset exhausted attempt budgets on explicit manual re-ship | Included in incident table. |
| [#239](https://github.com/mkny13/mahler/issues/239) | Hand-roll a pre-merge freshness check — GitHub's merge queue isn't available on personal-account repos | Included in incident table. |
| [#405](https://github.com/mkny13/mahler/issues/405) | Estimate composite available quota by capability route size | Quota, account or warm-up policy/observations, outside item-state races. |
| [#716](https://github.com/mkny13/mahler/issues/716) | Stop repeated no-work DONE builds before a third attempt | No-work result validation/loop guard, not competing state writes. |
| [#359](https://github.com/mkny13/mahler/issues/359) | Console release timeline, Cut release action, and What's New feed | Release or briefing feature; no distinct item-state lost update. |
| [#355](https://github.com/mkny13/mahler/issues/355) | Refresh onboarding, command-reference, and current-state documentation | Documentation, audit, walkthrough or measurement scope; no distinct item-state lost-update incident established by this result. |
| [#269](https://github.com/mkny13/mahler/issues/269) | Console: hold-reason rows expand to the items behind them | Status presentation/layout work, not a separate stored item-state writer race. |
| [#332](https://github.com/mkny13/mahler/issues/332) | Console: say 'waits for off-peak' (with resume time) for peak-held build items | Status presentation/layout work, not a separate stored item-state writer race. |
| [#360](https://github.com/mkny13/mahler/issues/360) | Per-project since-last-look briefs in the console | Release or briefing feature; no distinct item-state lost update. |
| [#436](https://github.com/mkny13/mahler/issues/436) | On the browse page, hide the item text behind an expand icon. And for ones like | Status presentation/layout work, not a separate stored item-state writer race. |
| [#603](https://github.com/mkny13/mahler/issues/603) | this looks terrible. just make a simple, small list of when each model resets. | Status presentation/layout work, not a separate stored item-state writer race. |
| [#464](https://github.com/mkny13/mahler/issues/464) | Hot Hold "End Session" but has no visible effect. Bug. p2. | Stale browser script/action delivery, not stored item-state overwrite. |
| [#586](https://github.com/mkny13/mahler/issues/586) | Security & Surface Area Audit | Documentation, audit, walkthrough or measurement scope; no distinct item-state lost-update incident established by this result. |
| [#533](https://github.com/mkny13/mahler/issues/533) | Back up Mahler's own ledger (mahler.db) nightly with integrity check, pruning and optional off-disk copy | Cleanup or backup infrastructure, not item-state authority. |
| [#440](https://github.com/mkny13/mahler/issues/440) | Conductor marks an item done when its PR was closed without merging | Included in incident table. |
| [#7](https://github.com/mkny13/mahler/issues/7) | Janitor: prune stale worktrees and old snapshot branches | Cleanup or backup infrastructure, not item-state authority. |
| [#611](https://github.com/mkny13/mahler/issues/611) | Gates, not guidance: escaped-bug loop, gate-coverage audit, evidence for shipped→done, project smoke contract | Completion-evidence gate/parent; lifecycle incident counted under #615. |
| [#618](https://github.com/mkny13/mahler/issues/618) | Document Xcode relative-DerivedData policy and weekly cache cleanup | Documentation task; triggering runtime incident traced through #675. |
| [#616](https://github.com/mkny13/mahler/issues/616) | Require evidence to promote shipped work to done | Completion-evidence gate/parent; lifecycle incident counted under #615. |
| [#14](https://github.com/mkny13/mahler/issues/14) | Mahler opens, watches and merges PRs itself; agents stop at 'code done' (DESIGN D18) | Session hooks/conductor feature, not a distinct state-race report. |
| [#69](https://github.com/mkny13/mahler/issues/69) | Dead code audit: remove runner.self_bin, gh.default_branch, ledger.attach_run and prune unused config prose | Cleanup or backup infrastructure, not item-state authority. |
| [#249](https://github.com/mkny13/mahler/issues/249) | Scheduler records why nothing started, so the console can say | Scheduling reason reporting, not item lifecycle mutation. |
| [#369](https://github.com/mkny13/mahler/issues/369) | Platform Tier/Capability Assumptions Audit | Documentation, audit, walkthrough or measurement scope; no distinct item-state lost-update incident established by this result. |
| [#3](https://github.com/mkny13/mahler/issues/3) | Claude Code session hooks: claim nudges, heartbeats, yield delivery | Session hooks/conductor feature, not a distinct state-race report. |
| [#536](https://github.com/mkny13/mahler/issues/536) | Warm-up: learn each login's best warm-up time from when Mike actually starts and runs out | Quota, account or warm-up policy/observations, outside item-state races. |
| [#211](https://github.com/mkny13/mahler/issues/211) | Adopt GitHub's native merge queue instead of hand-rolling the same safety guarantee | Merge-queue feature; freshness incident counted under #239. |
| [#300](https://github.com/mkny13/mahler/issues/300) | console: a refused/failed write action shows nothing; "Passed" copy claims the issue closes when it doesn't | Failed-action feedback and inaccurate success copy, not competing state writers. |
| [#706](https://github.com/mkny13/mahler/issues/706) | Fix run that resolves findings without a new commit burns an attempt and skips re-review | Included in incident table. |
| [#229](https://github.com/mkny13/mahler/issues/229) | platform-audit: only compare tiers whose size gates overlap, on a real sample | Documentation, audit, walkthrough or measurement scope; no distinct item-state lost-update incident established by this result. |
| [#486](https://github.com/mkny13/mahler/issues/486) | An item waiting on the conductor holds the project's only slot for hours | Included in incident table. |
| [#276](https://github.com/mkny13/mahler/issues/276) | Codex has a real account-wide quota API (and reset credits) — probe it for both personal and work accounts | Quota, account or warm-up policy/observations, outside item-state races. |
| [#265](https://github.com/mkny13/mahler/issues/265) | copilot-work's AI-credits probe reads the wrong meter for an org Business seat | Quota, account or warm-up policy/observations, outside item-state races. |
| [#430](https://github.com/mkny13/mahler/issues/430) | Platform Tier/Capability Assumptions Audit | Documentation, audit, walkthrough or measurement scope; no distinct item-state lost-update incident established by this result. |
| [#622](https://github.com/mkny13/mahler/issues/622) | Add opt-in Kiro CLI (kiro-cli) platform adapter | Platform adapter feature. |
| [#623](https://github.com/mkny13/mahler/issues/623) | Add opt-in Mistral Vibe CLI (vibe) platform adapter | Platform adapter feature. |

## Verification of this audit

The report was checked mechanically against `ledger.STATES`, `gh.STATE_LABELS`
plus the tracking alias, and all 21 required KV-family row names. All referenced
production function names were checked against the snapshot's Python AST, then
writer/read semantics checked against call sites (including direct upserts,
legacy clears and dynamic prefixes). Each preserved search-result row has a
disposition. This verifies inventory coverage; it does not claim a documentation
check proves a future concurrency conversion correct.

The two builder issues were fetched with:

```sh
gh issue view 757 -R mkny13/mahler --json number,title,body,labels
gh issue view 758 -R mkny13/mahler --json number,title,body,labels
```

Their backlinks, implementation files, regression checks, type/priority and
single size labels were verified. Repository checks for this documentation
change: `python3 -m mahler.repo_guard`, `python3 -m unittest discover -s tests`,
`python3 -X dev tests/run_strict.py`, and `python3 tests/run_random.py 712`.

Results: repository guard passed; normal, strict and seed-712 randomized suites
each passed **1,896 tests**. No runtime changes were made.
