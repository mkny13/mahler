# Audit: Lifecycle State, Lease Ownership, Synchronization, and Tick Flow against Architecture (Issue #1670 / #1671)

- **Audited source SHA:** `d67e6e88115079f74f46badfae04f6b2a724f427`, Python 3.14.10, macOS.
- **Audited scope:** DESIGN D4, D5, D6, D7, D9, D17, D18, D19, D20, D24, D45, D46, D51, D52, ARCHITECTURE Persistent state, and ARCHITECTURE Tick flow.
- **Audited modules:**
  - Production code: `mahler/ledger.py`, `mahler/scheduler.py`, `mahler/tick.py`, `mahler/sync.py`, `mahler/watchdog.py`, `mahler/finalize.py`, `mahler/waits.py`, `mahler/holds.py` (with supporting context from `mahler/gh.py`, `mahler/launch_health.py`, `mahler/platform_audit.py`).
  - Paired test suites: `tests/test_ledger.py`, `tests/test_schedule.py`, `tests/test_tick.py`, `tests/test_sync.py`, `tests/test_watchdog.py`, `tests/test_finalize.py`, `tests/test_waits.py`, `tests/test_holds.py` (with additional validation in `tests/test_dependencies.py`, `tests/test_ship.py`, `tests/test_launch_health.py`).
- **Method:** Hermetic source analysis, finite state machine transition tracing across all eleven states (`inbox`, `ready`, `working`, `verifying`, `shipped`, `done`, `needs_you`, `waiting`, `parked`, `failed`, `parent`), verification of atomic compare-and-set (CAS) leases, heartbeat renewals, pre-emption, capacity limits, and epoch fencing tokens, trace of D24 canonical remote lease coordination, audit of trust-gated intake, dependency cycle and ancestry bounds, net-backlog snapshot tracking, and execution of focused unit test suites and repository-wide test gates (`mahler.repo_guard`, `unittest discover`, `tests/run_random.py`, `tests/run_strict.py`) using isolated fixtures (in-memory SQLite, temporary directories). No real `~/.mahler` state was accessed.

---

## Decision-to-Code Matrix

| Decision / Claim | Result | Key Source Symbols | Concrete Test Evidence | Summary & Verification Proof |
|---|---|---|---|---|
| **DESIGN D4** (GitHub Issues are the backlog) | `conformant` | `gh.depends_of`, `gh.STATE_LABELS`, `gh.LABEL_STATES`, `gh.LABEL_COLORS`, `sync._sync_project`, `sync._sync_issue`, `sync._parse_dependencies`, `sync._reconcile_dependencies`, `sync._reconcile_parents`, `tick._candidates` (`done` set) | `tests.test_schedule.DependencyTests`, `tests.test_dependencies.DependenciesParsingTests`, `tests.test_sync.DiscussionContextTests`, `tests.test_sync.MissingScopeTests` | One issue equals one work item in its repository. Dependencies parsed from `Depends on: #12` (bare `#N`, `owner/repo#N`, unambiguous `repo#N`) and native "blocked by" relations, deduplicated. Dependency ancestry walks are cycle-safe and capped at 100 hops; self-references and ancestor references are dropped. Builds wait until targets are merged (`shipped` or `done` in local ledger). Labels mirror types, priorities (default p2; shipment failures p1), sizes, and `mahler:<state>` display state. |
| **DESIGN D5** (Mahler's own state) | `conformant` | `ledger.SCHEMA`, `ledger.Ledger.__init__`, `ledger.iso`, `ledger.parse`, `sync.sync`, `sync._sync_project` | `tests.test_ledger.ConnectionTests`, `tests.test_ledger.SchemaDriftTests`, `tests.test_ledger.LedgerInitializationResourcesTests` | SQLite in WAL mode at `~/.mahler/mahler.db` on internal disk (`PRAGMA journal_mode=WAL`, `PRAGMA synchronous=NORMAL`). File mode 0600 on DB and WAL/SHM sidecars. Tables store execution state (`projects`, `items`, `leases`, `runs`, `usage`, `events`, `counters`, `console_actions`, `uat`, `completion_evidence`, `releases`, `release_items`, `backlog_snapshots`, `kv`). Polling sync every 60s with HTTP ETags (`sync_etag:<project>`); unchanged responses (304) avoid rate limits. DB loss loses run history and live leases only; backlog and code remain in GitHub. |
| **DESIGN D6** (Ownership and locking) | `conformant` | `ledger.Ledger.claim`, `ledger.Ledger.heartbeat`, `ledger.Ledger.release`, `ledger.Ledger.lease_check`, `ledger.Ledger.lease`, `watchdog._watch_one` | `tests.test_ledger.LeaseTests`, `tests.test_watchdog.YieldGraceTests`, `tests.test_watchdog.LeaseLossTests` | Mahler's ledger is the sole lock authority. Claims execute an atomic compare-and-set in `BEGIN IMMEDIATE` transaction. Epochs act as monotonic fencing tokens (`UPDATE items SET epoch = epoch + 1`), written into `leases.epoch`. Autonomous leases yield when interactive claims pre-empt them (`UPDATE runs SET yield_at=?`), honoring grace periods. Heartbeats extend lease expiration; non-matching holder or epoch returns `False`, causing the run to stop immediately. Capacity limits (`max_parallel`) are enforced atomically during lease grant. |
| **DESIGN D7** (Staleness: activity clocks, not a calendar) | `conformant` | `config.DEFAULTS["projects"]["settle_minutes"]`, `config.DEFAULTS["projects"]["auto_lease_minutes"]`, `config.DEFAULTS["projects"]["interactive_lease_minutes"]`, `config.DEFAULTS["projects"]["progress_timeout_minutes"]`, `config.DEFAULTS["projects"]["run_timeout_minutes"]`, `tick.expire`, `watchdog._health` | `tests.test_watchdog.HealthTests`, `tests.test_watchdog.WallClockTests`, `tests.test_schedule.InteractiveLeaseRenewalTests`, `tests.test_schedule.HotHoldTests` | Four finite clocks govern work: (1) Settle delay (default 10 min after sorting before autonomous pickup); (2) Autonomous lease TTL (default 10 min without heartbeat, 20 min without progress, and configurable run timeout); (3) Interactive lease TTL (default 30 min idle; renewed if local presence detected, otherwise released to `ready`); (4) Primary-checkout orphan (24h without activity). Watchdog monitors health, progress logs, and timeouts. |
| **DESIGN D9** (Handoff protocol) | `conformant` | `finalize.finalize`, `finalize._post_handoff_comment`, `finalize.snapshot_branch`, `finalize._handoff_note_body`, `watchdog._watch_one` | `tests.test_finalize.RunTests`, `tests.test_watchdog.YieldGraceTests`, `tests.test_watchdog.TimeoutHandoffTests` | Runs checkpoint continuously. On yield or termination, runs stop cleanly. Claude runs receive yield requests via filesystem hook (`~/.mahler/runs/<id>/yield`); other adapters receive termination signals after grace period. If an agent dies or hangs, Mahler snapshots the worktree, pushes snapshot refs (`mahler/snapshot/*`), and publishes a structured durable handoff comment (`<!-- mahler:handoff run=... epoch=... from=... reason=... -->`). Successor runs inherit prompt context and resume from the pushed branch. |
| **DESIGN D17** (Self-hosting: Mahler builds Mahler) | `conformant` | `launch_health.record_launch_failure`, `launch_health.record_launch_success`, `launch_health.global_breaker_tripped`, `launch_health.tick_exit_code`, `scheduler.main_tick`, `config.LAUNCH_OK_PATH`, `config.BAD_SHA_PATH` | `tests.test_launch_health`, `tests.test_schedule.ProjectPauseTests` | Daemon executes from `~/.mahler/app`, pinned to a known-good commit. Changes land with `max_parallel = 1`, standard-library only. Launch failures trigger persistent circuit breakers in SQLite KV: 2 matching normalized exceptions across distinct items pause all launches globally; 3 matching failures in one project pause that project. Successful launches write `$MAHLER_HOME/launch_ok`; global breaker triggers exit code 3 only when `launch_ok` exists and differs from HEAD, driving launcher rollback to `launch_ok` and recording the bad commit in `bad_sha`. |
| **DESIGN D18** (Agents build; conductor ships) | `conformant` | `finalize.finalize`, `finalize._handle_run_exit`, `finalize.led.claim`, `ship.ship`, `ship._ship_project` | `tests.test_finalize.FinalizeConductorPushTests`, `tests.test_finalize.RunTests`, `tests.test_ship` | Agents implement, test, commit locally, and conclude with STATUS lines. Build runs hand off item leases atomically to `CONDUCTOR` with `capacity=False` (`handoff_from=(f"run:{run['id']}", run["epoch"])`), releasing run capacity while preserving item fencing tokens. Conductor pushes the saved branch, opens/updates the PR, watches CI, coordinates independent review, and squash-merges on green. |
| **DESIGN D19** (A change is in flight until it merges) | `conformant` | `ledger.Ledger.claim` (`capacity=False`), `tick._candidates` (`verifying` check), `ship._check_merge_ancestry`, `ship._merge_pr` (`--match-head-commit`) | `tests.test_ship`, `tests.test_schedule`, `tests.test_finalize` | Build/fix/review runs occupy concurrency slots; conductor PR watches release execution capacity. Items in `verifying` count against `max_parallel` for new builds. Every build starts on current base (rebase at launch). Conflicting or stale-ancestry PRs are returned to `ready` for replay. Merge requests verify current base ancestry (`git merge-base --is-ancestor`) and pin head commits via `gh pr merge --squash --match-head-commit <sha>`. |
| **DESIGN D20** (Maintenance passes) | `conformant` | `tick.MAINTENANCE_TEXT`, `tick.queue_maintenance`, `tick.maintenance_due`, `platform_audit.queue`, `practices_audit.queue` | `tests.test_tick`, `tests.test_platform_audit`, `tests.test_ledger.MaintenanceConfigTests`, `tests.test_ledger.MaintenanceCheckpointTests` | Configurable periodic reviews (security, health, drift, tests, gate, hygiene, guidance, pruning, bugs, escape, onboarding). Cadence defaults to 30 days or early trigger after 20 merged PRs, with 14-day cooldown. At most one maintenance pass in flight per project. Platform-audit pass periodically inspects `DESIGN.md` verification dates (>90d stale) and analyzes ledger done-rates and escalation patterns, filing findings as issues without autonomous policy alterations. |
| **DESIGN D24** (One canonical lease table for shared projects) | `conformant` | `ledger.RoutedLedger`, `ledger.remote_lease_operation`, `ledger.Ledger.claim` (`external=True`), `ledger.Ledger.orphan_lease_rows` (`WHERE l.external = 0`) | `tests.test_ledger.RemoteLedgerTests`, `tests.test_schedule.RemoteLeaseFailureTests` | Shared projects configure `remote_ledger` pointing to canonical host (Mac mini). Only five operations are relayed over static SSH JSON protocol: `lease`, `claim`, `heartbeat`, `release`, and `lease_check`. Remote holders are prefixed with `client_id/` and stored with `external=1`. Concurrency (`max_parallel`) is checked atomically on the canonical ledger in the grant transaction. Failures fail closed, isolating remote transport disruptions from local ticks. Canonical orphan sweeps preserve external leases. |
| **DESIGN D45** (Net backlog: open-count deltas from hourly snapshots) | `conformant` | `sync._record_backlog_snapshot`, `sync._prune_backlog_snapshots`, `ledger.Ledger.record_backlog_snapshot`, `ledger.Ledger.backlog_delta`, `ledger.SCHEMA` (`backlog_snapshots`) | `tests.test_ledger.BacklogSnapshotTests`, `tests.test_sync.BacklogSnapshotSyncTests` | Backlog snapshots are recorded directly during sync passes using open-issue count in hand, without extra GitHub API queries. Stored in `backlog_snapshots` table (project, timestamp, open_count). Hourly cadence with 35-day retention (rows older than 35 days pruned on write). Net backlog deltas (opened minus closed) over 24h, 7d, and 30d are computed directly from snapshots, flagging incomplete history windows. Snapshot failures are logged and do not abort sync. |
| **DESIGN D46** (Trust-gated public issue intake) | `conformant` | `sync._admit_issue`, `sync.is_trusted_actor`, `sync._sync_project`, `gh.TRUSTED_ASSOCIATIONS` | `tests.test_sync.TrustGatedIntakeTests` | Issues from GitHub authors with `authorAssociation` in `OWNER`, `MEMBER`, `COLLABORATOR`, matching repository owner, or configured project GitHub identity/App slug are admitted to `inbox`. Untrusted issues require project `scope_label`. Untrusted issues lacking the label are stored in the ledger as non-schedulable `parked` items (`parked_reason: "untrusted intake"`). A subsequent trusted comment or scope-label addition resumes normal intake to `inbox`. |
| **DESIGN D51** (Timed project priority) | `conformant` | `tick.reconcile_priority_boost`, `tick.set_priority_boost`, `tick.clear_priority_boost`, `tick.get_priority_boost`, `tick._candidates`, `scheduler.tick`, `cli.cmd_prioritize` | `tests.test_schedule.SchedulerPriorityBoostExpiryTests` | Enables temporary priority boost for one project for `1h`, `1d`, `1w`, or `off`. Stored as a durable JSON KV record (`priority_boost`). Re-prioritizing the same project extends from now; prioritizing another atomically replaces the previous boost and records the replaced project. Boosted project candidates sort first in `tick._candidates`. Expiry is reconciled at the start of every scheduler tick before other passes, deleting the record and logging `priority_boost_expired`. Concurrency and running work remain untouched. |
| **DESIGN D52** (Waiting on the world is not waiting on you) | `conformant` | `waits.CAUSES`, `waits.RESUMES`, `waits.BACKOFF_MINUTES`, `waits.hold`, `waits.recheck`, `waits.load`, `waits.clear`, `waits.WAKES`, `finalize.recheck_blocked` | `tests.test_waits.WaitTests`, `tests.test_waits.SiteTests`, `tests.test_waits.MigrationTests` | Distinguishes owner-only decisions (`needs_you`) from external operational blockers (`waiting`). Managed via `waits.hold`, writing structured KV records (`wait:<project>#<n>`) with machine-readable causes (`ci_repo_wide`, `ci_stalled`, `ci_cancelled`, `merge_queue`, `capacity`, `github`, `setup`, `worktree_conflict`, `review_inconclusive`, `review_nonconvergent`, `launch`, `dependency_vetting`, `other`). Each tick re-evaluates wake conditions (`waits.WAKES`); unsatisfied waits back off (15m, 30m, 1h, 2h, 4h, 6h). After `wait_escalate_hours` (72h), backstop escalates item to `needs_you` with owner notification. |
| **ARCHITECTURE Persistent state** | `documentation drift` | `ledger.SCHEMA`, `ledger.STATES`, `ARCHITECTURE.md:41-45` | `tests.test_ledger.StateTests`, `tests.test_ledger.BacklogSnapshotTests` | Implementation fully supports SQLite WAL, all required tables, permissions, and eleven finite item states. Documentation drift identified in `ARCHITECTURE.md:41-45`: the specification states list omits `waiting` (`inbox, ready, working, verifying, needs_you, parked, failed, parent, shipped, done`), and the tables list omits `backlog_snapshots`. |
| **ARCHITECTURE Tick flow** | `documentation drift` | `scheduler.tick`, `scheduler.run_pass`, `scheduler.take_lock`, `scheduler.main_tick`, `ARCHITECTURE.md:57-75` | `tests.test_tick.PassBudgetTests`, `tests.test_schedule` | Exclusive non-blocking flock (`LOCK_PATH`) guarantees single tick execution; exit code 3 signals global launch breaker trip. Pass execution in `scheduler.tick` enforces per-project exception isolation and SIGALRM pass budgets (`budget=300`). Documentation drift identified in `ARCHITECTURE.md:57-75`: the 10-step overview predates explicit enumeration of priority boost reconciliation (D51), recheck_blocked/waits pass (D52), post-merge advance, and practices audit. |

---

## Detailed Lifecycle Traces

### 1. Finite Lifecycle State Transitions
The Mahler state machine defines eleven mutually exclusive item states in `ledger.STATES`:
`("inbox", "ready", "working", "verifying", "needs_you", "waiting", "parked", "failed", "parent", "shipped", "done")`.
Every state transition is mediated by `ledger.Ledger.set_state`, which validates membership in `STATES`, updates `state_changed_at`, appends to the append-only `events` audit table, and calculates issue completion statistics on transition to `done`.

- **`inbox` (Intake):**
  - Items enter `inbox` on ingestion via `sync._sync_issue` or `mahler add`.
  - Untrusted intake (D46) transitions directly to `parked` with `parked_reason="untrusted intake"`.
  - Goals (`type:goal` or sub-issues) transition to `parent`.
  - Normal items transition from `inbox` to `ready` upon completion of a sort or plan run (`finalize._handle_sort_exit`), stamping `sorted_at`.
- **`ready` (Schedulable):**
  - Schedulable candidates wait in `ready` until selected by `tick.schedule`.
  - Settle delay (D7) enforces `sorted_at + settle_minutes` before candidate eligibility.
  - Schedulers check dependency satisfaction: all targets must be `shipped` or `done` in the local ledger (D4).
  - On run launch, `tick.schedule` claims the item lease and sets state to `working`.
  - An item can be moved to `parked` (manual park or active CI blocker), or `needs_you` (if max attempts or verify timeouts are reached).
- **`working` (Agent Execution):**
  - An agent run or interactive session holds an active lease with `capacity=1`.
  - The watchdog heartbeats the lease each tick (`watchdog._watch_one`).
  - If the agent finishes with `STATUS: DONE` or passing verification, the run exits and transitions the item to `verifying` (`finalize._handle_run_exit`).
  - If pre-empted, crashed, or yielded due to quota, the run transitions the item back to `ready` (or `working` if claimed by an interactive session) with a handoff snapshot and comment (D9).
  - If a fatal failure occurs or attempt budgets expire, `finalize.retry_or_fail` transitions to `failed` or `needs_you`.
  - External operational blockers discovered during execution transition the item to `waiting` via `waits.hold` (D52).
- **`verifying` (Conductor Shipping & Verification):**
  - The item lease is transferred atomically to `CONDUCTOR` with `capacity=False` (`finalize.finalize`), freeing concurrency capacity while maintaining the fencing lock (D18, D19, D24).
  - The conductor watches CI, coordinates review, and verifies head freshness and base ancestry (`ship.ship`).
  - Merge conflicts or stale ancestry return the item to `ready` for base replaying without consuming attempt budgets (D19).
  - Upon successful squash-merge, the item transitions to `shipped` (`ship._shipped`, mahler#634).
  - Conductor operational blockers (repo-wide CI red, capacity, merge queue, GitHub API) transition the item to `waiting` via `waits.hold` (D52).
- **`shipped` (Merged & Awaiting Quiet Window or Evidence):**
  - On GitHub, the issue is closed and retains the `mahler:shipped` display label (D4, mahler#634).
  - Leases and capacity are fully released.
  - Release snapshots and UAT entries (`uat` table) are recorded at merge time.
  - In `mahler/sync.py:reconcile_shipped`, closed shipments are polled periodically. After 14 quiet days without reopen/bug or upon verified automated evidence (`completion_evidence`), `ledger.Ledger.set_state` transitions the item to `done`. Transition from `shipped` to `done` strictly requires a recorded `completion_evidence` row (`ledger.py:1640`).
- **`done` (Final Resolution):**
  - Issue is closed and verified. GitHub label mirrors `mahler:done`.
  - Satisfies parent completion (`sync.close_finished_parents`) and downstream dependencies (D4).
- **`needs_you` (Owner Decision Required):**
  - Reserved strictly for D13 owner-only categories: `product`, `money`, `credentials`, `data-policy`, `irreversible` (D52).
  - Owner responses via issue comments, `/mahler go`, or console actions transition the item back to `ready` or `inbox`.
- **`waiting` (External Operational Blocker):**
  - Managed by `mahler/waits.py` (D52). Set when conductor hits environmental blockers (CI red, capacity, GitHub outage, etc.).
  - Rechecked each tick by `waits.recheck` / `finalize.recheck_blocked`.
  - If the wake condition is met, the item resumes into its declared resume state (`verifying`, `ready`, or `inbox`).
  - If unsatisfied, next check backs off (15m, 30m, 1h, 2h, 4h, 6h). After `wait_escalate_hours` (72h), backstop escalates to `needs_you`.
- **`parked` (Deliberate Suspension):**
  - Used for untrusted public intake (D46), explicit manual pauses (`mahler park`), or blocked preconditions.
  - Non-schedulable; resumed to `ready` or `inbox` via trusted collaborator actions or `/mahler go`.
- **`failed` (Terminal Failure):**
  - Reached when attempt limits are exhausted and the issue cannot be automatically retried.
- **`parent` (Tracking Goal):**
  - Applied to `type:goal` parent issues that split into sub-issues.
  - Remains in `parent` until all child issues reach `shipped` or `done` and PR/owner verification evidence is posted (`sync.close_finished_parents`).

---

### 2. Lease Ownership, Fencing, and Capacity Management
- **Atomic Compare-and-Set:** `ledger.Ledger.claim` executes within an immediate transaction (`self._tx()`). A claim succeeds if the lease is unheld or expired (`expires_at <= now`), if the holder matches (renewal), or if an interactive claim pre-empts an autonomous run (`cur["kind"] == "auto" and human`).
- **Epoch Fencing:** Every new lease grant increments `items.epoch` and assigns it to `leases.epoch`. `ledger.Ledger.lease_check` verifies that an execution epoch remains current before any snapshot or merge. If a lease is stolen or displaced, the displaced runner's next heartbeat or fencing check fails, preventing split-brain writes.
- **Pre-emption & Yielding:** An interactive session (`interactive` or `primary`) pre-empts an `auto` lease immediately. `Ledger.claim` updates `runs.yield_at` for the active run. The watchdog delivers a yield signal (via `yield` file hook for Claude, or SIGTERM after grace period for other runners), ensuring the displaced run snapshots its work and exits cleanly (D6, D9).
- **Capacity Accounting:** Concurrency is tracked via `leases.capacity = 1`. During a capacity-taking claim (`capacity=True`), active leases with `capacity=1` are counted against `max_parallel`. Sort runs and conductor PR watches claim with `capacity=False` (D19, D24), ensuring watching does not block new builds.
- **Atomic Conductor Handoff:** When a build finishes, `finalize.finalize` transfers the lease directly to `CONDUCTOR` with `capacity=False` using `handoff_from=(f"run:{run['id']}", run["epoch"])`. This atomically hands off ownership and fencing while releasing execution concurrency without an unleased gap.
- **Expiry & Orphan Sweeps:**
  - `tick.expire` scans expired leases. Unheartbeated auto leases release to `ready`. Interactive leases check presence and renew if the user was active within `interactive_lease_minutes` (D7).
  - `tick.sweep_orphans` recovers working items that have neither a lease row nor an active process (`orphan_working_items`).
  - `ledger.Ledger.orphan_lease_rows` identifies lease rows on idle states (`i.state NOT IN ('working', 'verifying') AND l.external = 0`). It exempts active/stopping runs and D24 external leases, releasing orphaned leases safely.

---

### 3. Synchronization, Trust Intake, and Tick Flow Orchestration
- **Trust-Gated Intake (D46):** `sync._admit_issue` verifies GitHub author associations. Authors with `OWNER`, `MEMBER`, or `COLLABORATOR`, matching the repository owner, or matching the configured GitHub App slug are admitted directly. Untrusted authors without the project's `scope_label` are held in `parked`.
- **Dependency & Parent Resolution (D4, D21):** `sync._parse_dependencies` parses explicit dependencies and GitHub native sub-issue relations. Sync breaks self-dependencies and ancestral cycles. `sync.close_finished_parents` verifies that all sub-issues of a parent are in `shipped` or `done`, validates PR merge evidence, posts an evidence summary comment, and closes the parent issue.
- **Net-Backlog Snapshots (D45):** Hourly snapshots are recorded in `backlog_snapshots` during sync from the in-memory issue list. Rows older than 35 days are pruned. History window checks in `backlog_delta` prevent partial window misrepresentation.
- **Tick Flow & Pass Boundaries:**
  - `mahler tick` enforces single-instance execution via non-blocking exclusive flock on `config.LOCK_PATH`.
  - Every pass is executed via `scheduler.run_pass`, bounded by a SIGALRM timer (`budget=300`), which catches stalls and reports stack locations without leaking credentials.
  - Per-project exception safety: a failure during project sync (`sync:project`), backup, or child reconciliation is trapped and logged (`ctx.say`), allowing the tick to proceed with other projects.
  - Timed project priority (D51) reconciles expiration at the beginning of each tick before scheduling.

---

## Retained Compatibility Paths and Justifications

1. **`tracking` state migration (`mahler/ledger.py:345`):**
   - *Status:* Retained and conformant.
   - *Justification:* During startup migration, `UPDATE items SET state = 'parent' WHERE state = 'tracking'` ensures legacy databases created before `parent` was introduced do not strand rows outside `STATES`.
   - *Exercised tests:* `tests.test_parent_state.ParentStateTests`.
2. **`mahler:tracking` label backward compatibility (`mahler/gh.py:21`):**
   - *Status:* Retained and conformant.
   - *Justification:* `LABEL_STATES["mahler:tracking"] = "parent"` ensures existing GitHub issues tagged with the legacy label are mapped correctly to `parent` during sync.
   - *Exercised tests:* `tests.test_sync`.
3. **`shipped` state retention (`mahler/ledger.py:377`):**
   - *Status:* Retained and conformant (restored in mahler#634).
   - *Justification:* Retained to represent confirmed squash-merged pull requests awaiting quiet-window completion or evidence-based promotion to `done`. `shipped` rows are preserved and mirrored to GitHub (`mahler:shipped`).
   - *Exercised tests:* `tests.test_sync_uat`, `tests.test_ship`.
4. **`external` column default in `leases` (`mahler/ledger.py:321`):**
   - *Status:* Retained and conformant.
   - *Justification:* Preserves leases created before the explicit ownership marker by checking `instr(holder, '/') > 0`, ensuring D24 remote leases are never treated as local orphans.
   - *Exercised tests:* `tests.test_ledger.RemoteLedgerTests`.

---

## Audit Findings and Drift Reconciliation

1. **ARCHITECTURE.md Persistent state omissions (`documentation drift`):**
   - `ARCHITECTURE.md:41-43` enumerates the items table state machine as:
     `items (state machine: inbox, ready, working, verifying, needs_you, parked, failed, parent, shipped, done)`
     Omits the explicit `waiting` state introduced by DESIGN D52 (mahler#1259).
   - `ARCHITECTURE.md:44` lists database tables but omits `backlog_snapshots` introduced by DESIGN D45 (mahler#1104).
   - *Resolution:* Verified that production implementation in `mahler/ledger.py` correctly defines `STATES` including `waiting` and `SCHEMA` including `backlog_snapshots`. Documented as documentation drift for top-level documentation reconciliation.
2. **ARCHITECTURE.md Tick flow pass list omissions (`documentation drift`):**
   - `ARCHITECTURE.md:57-75` specifies the high-level 10-step tick pass sequence. It does not explicitly mention:
     - Priority boost reconciliation (`reconcile_priority_boost`, D51), which executes prior to outbox draining.
     - External wait recheck pass (`recheck_blocked` / `waits.recheck`, D52), which executes after lease expiry.
     - Post-merge advance (`post_merge.advance`), which runs alongside `ship`.
     - Practices audit pass (`practices_audit.queue`, mahler#1562).
   - *Resolution:* Verified that `mahler/scheduler.py` implements the complete, robust, exception-safe pass sequence with SIGALRM budgeting. Documented as documentation drift for top-level documentation reconciliation.
3. **Implementation Conformance:**
   - All 14 decisions (D4, D5, D6, D7, D9, D17, D18, D19, D20, D24, D45, D46, D51, D52) and both ARCHITECTURE sections were verified against production code and tests.
   - No zombie lifecycle abstractions or dead operational branches exist in the audited files.
   - All four repository verification commands pass.
