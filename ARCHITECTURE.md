# Mahler — Architecture

A map of the system as shipped. It describes what the code does today and points to the
records that explain why. It does not repeat them:

- [DESIGN.md](DESIGN.md) is the decision record (D1–D52). It keeps history, so older
  sections may describe a target that later decisions amended.
- [ROADMAP.md](ROADMAP.md) says what is done, in progress, and planned.
- [README.md](README.md) covers setup and operation.
- [CLAUDE.md](CLAUDE.md) and [AGENTS.md](AGENTS.md) are the rules agents follow.

Reconciled with repository code, CLI help and hermetic tests on 2026-11-27.
The committed 2026-11-22 drift audits provide earlier evidence:
[lifecycle](docs/audits/1670-lifecycle-state.md) supplies the state/tick corrections;
[execution and shipping](docs/audits/1670-execution-shipping.md) supplies run and merge evidence;
[routing and accounts](docs/audits/1670-routing-quota-accounts.md) supplies credential and quota boundaries;
[operator and maintenance](docs/audits/1670-operator-maintenance.md) supplies console,
completion and practices corrections. These also ground README and ROADMAP's current
claims; DESIGN D10, D27 and D39 retain rationale with present-tense corrections.
No audit attests to live machine configuration.

Planned behavior is named as planned. The retired `dispatch`/`thread` tooling (DESIGN D2)
is not part of this architecture.

## Components

Mahler is a macOS, single-operator service whose Python 3.14+ control plane uses
the standard library only. It shells out to `git`, `gh` and the agent CLIs.

| Component | Where | Role |
|---|---|---|
| Launcher | `launcher/` | Hand-installed. Runs one tick every 60 s under `launchd`, updates `~/.mahler/app` to a CI-green `origin/main`, and rolls back (D17). |
| Scheduler | `mahler/scheduler.py` | The tick's entry: lock, `Ctx`, and the order of passes. |
| Passes | `mahler/watchdog.py`, `mahler/sync.py`, `mahler/finalize.py`, `mahler/ship.py`, `mahler/tick.py`, `mahler/usage.py`, `mahler/warmup.py`, `mahler/resets.py`, `mahler/platform_audit.py` | One module per pass. |
| Ledger | `mahler/ledger.py` | SQLite (WAL): execution state and leases. |
| Router | `mahler/router.py`, `mahler/platforms.py`, `mahler/config.py` | Quota-aware, deterministic platform and account choice. |
| Runner | `mahler/runner.py`, `mahler/prompt.py`, `recipes/` | Worktrees, detached agent processes, snapshots, and the prompt each role is given. |
| GitHub | `mahler/gh.py` | Project-bound GitHub operations through `gh`; account-scoped CLI, App or PAT identity. |
| Console | `mahler/serve.py`, `mahler/console/` | Optional web console (D27). A separate process. |
| MCP server | `mahler/mcp.py` | Local stdio JSON-RPC. Seven tools: `list_items`, `add_item`, `claim`, `heartbeat`, `release`, `handoff`, `next_id`. |
| CLI | `mahler/cli.py`, `bin/mahler` | Operator and interactive-session commands, plus `mahler hooks` for Claude Code. |
| Maintenance | `mahler/backup.py`, `mahler/digest.py`, `mahler/janitor.py`, `mahler/releases.py`, `mahler/notify.py`, `mahler/presence.py`, `mahler/launch_health.py`, `mahler/repo_guard.py` | Backups, daily digest, cleanup, release ledger, ntfy pings, session presence, launch circuit breaker, CI repo guard. |
| Waits and holds | `mahler/waits.py`, `mahler/holds.py`, `mahler/ci_blockers.py` | External waits with wake/backoff records, typed scheduling holds and repository CI blockers. |
| Completion and failures | `mahler/post_merge.py`, `mahler/failures.py`, `mahler/no_change.py`, `mahler/review.py` | Post-merge evidence, linked failure fixes, independently verified no-change claims and head-bound review verdicts. |
| Discussions and API credits | `mahler/discuss.py`, `mahler/discuss_tools.py`, `mahler/discuss_credits.py`, `mahler/api_credits.py`, `sdk_runner/` | Operator-started issue-only workers; confirmed-credit gateway with reservations. The optional SDK runner is separately installed and has its own dependencies. |
| GitHub identity | `mahler/github_app.py`, `mahler/github_pat.py` | Repository-scoped credentials; no ambient fallback for required scoped identities. |
| Screenshots | `mahler/screenshots.py`, `mahler/screenshot_delivery.py`, `mahler/desktop.py` | Capture/delivery evidence and the exclusive desktop gate. |
| Lessons and history | `mahler/lessons.py`, `mahler/history.py` | Scan-passed base lessons and redacted, project-scoped ended-run search. |
| Scorecard and capacity | `mahler/scorecard.py`, `mahler/capacity.py`, `mahler/run_usage.py`, `mahler/model_availability.py`, `mahler/cline_free.py` | Measured routing evidence, bounded historical capacity, usage accounting and model availability. |
| Atlas | `mahler/atlas.py` | Cached inventory from configured projects, scan roots and session metadata; not an autonomous dispatcher. |
| Practices audits | `mahler/practices_audit.py` | Recurring evidence/proposal reports; no repository or policy remediation. |

Component paths are repository-relative. LLM calls run in agent runs or supervised
discussion workers; the tick never calls an LLM (DESIGN Principle 7, D43).

## Persistent state

GitHub is authoritative for issues, comments, handoffs, labels and PRs. The ledger
keeps synchronized item fields and execution state, plus local discussion sessions
and evidence (DESIGN D4, D5, D43).

- **`~/.mahler/mahler.db`** (SQLite). Tables: `items` (state machine: `inbox`, `ready`,
  `working`, `verifying`, `needs_you`, `waiting`, `parked`, `failed`, `parent`, `shipped`, `done`), `leases`
  (compare-and-set, with epochs), `runs`, `usage`, `events`, `counters`, `console_actions`,
  `uat`, `post_merge_checks`, `completion_evidence`, `releases`, `release_items`,
  `backlog_snapshots`, `run_notes`, `lesson_usage`, `credit_reservations`,
  `capacity_intervals`, `capacity_platform_sets`, the `chat_sessions`/`chat_turns`/
  `chat_mutations`/`chat_turn_mutations` discussion tables, and a `kv` table for small flags
  and caches (pause, holds, breakers, review evidence).
- **`~/.mahler/config.toml`**: machine policy (projects, accounts, platforms, routing,
  caps). Per-repo `.mahler/project.toml` declares verify, release and data contracts and
  is checked in so agents can read it.
- **`~/.mahler/runs/<id>/`**: run logs and status files.
- **`~/.mahler/worktrees/<project>/<issue>-run<id>`**: one worktree per run. A project's
  `worktree_root` can move it.
- **`~/.mahler/app`**, plus the launcher's `known_good`, `launch_ok` and `bad_sha` files.
- **Snapshot refs** `mahler/snapshot/*` on the remote, for work saved at the end of a run.

These are default paths; `MAHLER_HOME` relocates the state root. Private GitHub
credential caches and per-run credential/config directories also live under that root
(`github_app.py`, `runner.py`); they are runtime secrets, never repository artifacts.

The daemon's database and config are never edited by a task. Tests use `Ledger(':memory:')`
or temp dirs.

## Tick flow

`mahler tick` takes an exclusive lock (an overlapping tick exits at once), then runs these
passes in order, each exception-safe per project:

1. Reconcile timed project priority expiry (D51), then drain the console outbox.
2. Compute account/quota-group Claude burst state (D23), then run the watchdog.
3. Sync each enabled, reachable project: trust-gated issue intake, comment commands,
   replies, labels, dependencies and parents (D46). One project's failure is isolated.
4. Run the watchdog again, backfill failure links, expire leases, recheck blocked work
   and external waits (D52), then close finished parents.
5. Unless paused: refresh usage, spend banked resets, learn warm-up times (report-only),
   run warm-ups, queue maintenance, platform and practices audits, then schedule.
   Eligibility includes account, capability, quota freshness, holds and capacity.
6. Record scheduling holds. Unless paused, ship and advance post-merge validation.
7. Flush capacity observations and mirror state labels.
8. Run configured data and ledger backups, digest, Atlas scan and janitor even while
   paused, then run the watchdog again.

Passes have time budgets (normally 300 seconds); watchdog checks also run between
long passes and after failures. Shared-project lease operations use the canonical
remote ledger and fail closed on transport failure (D24).

Exit code 3 requests rollback when a global launch breaker is open and `launch_ok`
names a different, launch-proven commit (D17).

## Run flow

1. `tick.start` takes a lease on the item and picks a platform and account (D8, D25, D26).
2. `runner.prepare` creates the worktree on `mahler/<issue>-<slug>`, starting from the
   handoff branch or base. It installs a pre-push epoch fence through `GIT_CONFIG_*` that
   chains to the repo's own hooks. `runner.launch` spawns the agent CLI detached, with a
   `MAHLER_*` environment and the run's own account credentials.
3. The recipe determines what the agent may change and what its `STATUS:` line means:
   - **Sort** is repository-read-only. It edits the issue and labels through GitHub, then
     reports `READY`, `SPLIT`, or `NEEDS-YOU`; it does not edit files, verify, commit, or push.
   - **Review** is repository-read-only. It inspects the PR and reports `REVIEW-PASS`,
     `REVIEW-FAIL`, or `NEEDS-YOU`; it does not edit the review checkout, commit, or push.
     It may run verification and sensitivity checks in a separate isolated baseline.
   - **Build and fix** edit the worktree, run the repository's verify contract, commit locally,
     and report `DONE`, `NEEDS-YOU`, `BLOCKED`, or `YIELDED`. They do not
     open or merge PRs.
4. The watchdog supervises every process across ticks, and `finalize` reads its log and
   status. Sort outcomes move the item to `ready`, `parent`, or `needs_you` (or retry).
   Review outcomes are posted to the PR and recorded for the shipping gate; the item stays
   `verifying`, ready either to merge or to start a fix, and an inconclusive review is retried.
5. Only build and fix use the code-work handoff path (D9): `finalize` snapshots work to a
   pushed ref, posts the issue handoff, and dispatches the result to retry, `needs_you`,
   `waiting`, `parked`, `failed`, or shipping. Only this path has D18's fallback that accepts a missing
   `STATUS:` line when the branch has changes and Mahler's verify command passes.
6. Build, fix, and review take run capacity; sort does not (D19). Work is bounded by project
   and platform caps, attempts, and escalation tiers. Launch failures feed a persistent
   circuit breaker (D17). Runs may also report `waiting` for an external wait (D52).
   `NEEDS-YOU` is reserved for the five owner-only categories; invalid questions are
   retried under D52. A clean no-change claim gets independent verification.

## Shipping flow

The conductor owns the mechanical tail (D18, D19, D28). One lease holder, `conductor`,
carries an item while it verifies, and watching takes no run capacity. Unmerged
`verifying` items still count against project build admission (D19).

1. Push the branch and open a PR with `Fixes #N`, the agent's summary and the issue's
   "Needs a human to check" list.
2. Watch CI across ticks. Red CI starts a `fix` run from the failing log tail.
3. For `size:m`, `size:l` and risk-keyword items, run an independent **review** on a
   different platform between green CI and merge (D11). Review has a blocking bar and a
   convergence check; operational review waits use `waiting` (D52). Verdicts bind to
   the current PR head and become invalid when it changes.
4. Recheck the base and lease, then squash-merge, wait for GitHub to confirm the merge, and
   comment. A moved base sends the item back for a rebuild.
5. Record the merged item as `shipped`, its release snapshot (D31), and post-merge
   checks. Automated completion evidence or 14 quiet days moves it to `done` (D10).
   Adverse evidence inside that window reopens early automated completion; an optional
   failure report creates linked p1 fix work. Historical UAT rows remain readable;
   there is no owner pass step or in-app verdict queue.

Interactive sessions hand over with `mahler ship <project>#N` (D18). An open PR that no
item tracks draws a "PR nobody is shipping" ping.

## Operator surfaces

- **GitHub** issues and comment commands (`/mahler go`, `park`, `platform`), documented
  in [docs/commands.md](docs/commands.md).
- **Console** (`mahler serve`, D27; design in [docs/console/design.md](docs/console/design.md)).
  Reads come from `console/state.py`. Writes are `POST /api/<action>`. Those that touch
  only local state apply at once. Those that reach GitHub or a running agent are queued
  through the ledger and applied by the next tick, so GitHub traffic keeps each project's
  own login.
- **CLI** (`mahler --help`) and Claude Code hooks (`mahler hooks`) for interactive
  sessions, which follow the lease protocol (`claim`, `heartbeat`, `ship`, `release`).
- **Discussions** are operator-started, project-bound issue workers (D43), separate
  from the tick and HTTP process. Their restricted MCP cannot build, lease or merge.
- **Settings** writes apply locally through the config API; answers have a 60-second
  Undo grace before the outbox posts them. Atlas, history and capacity expose stored evidence.
- **MCP** over stdio is a privileged local operator API with seven tools, separate from
  the restricted discussion entry point.
- **ntfy** pings and a daily digest.

### CLI command inventory

Top-level names accepted by `mahler --help` and its parser (2026-11-27):

- `desktop`, `tick`, `status`, `events`, `scorecard`, `review-adjudicate`.
- `capacity`, `gateway`, `serve`, `hooks`, `warmup`, `usage`.
- `claim`, `heartbeat`, `ship`, `release`, `lease-check`, `ledger-remote-op`.
- `next-id`, `prioritize`, `atlas`, `pause`, `resume`, `unstick`.
- `unhold`, `add`, `notify`, `labels`, `backup`, `restore-ledger`.
- `backfill-usage`, `history`, `log`, `mcp`, `peak`, `version`.
- `console-walkthrough`.

The gateway and ledger-remote-op entries are internal transport commands, not
operator workflows. Subcommands and flags are described by each command’s help.

## Trust boundaries

- **The daemon runs from a pinned clone** (`~/.mahler/app`), never from a working checkout.
  Changes reach it only through a merge to `main` with green CI and passing unit tests
  (D17). `launcher/` changes take effect only when someone re-runs `launcher/install.sh`.
- **Agents are unattended and isolated:** one worktree per run, secrets redacted from logs
  (`redact.py`), the epoch fence on push, and a lease that lets an interactive session
  pre-empt a run (D6).
- **Accounts:** each project names the logins it may spend. Work accounts stay on their
  own credentials and never fall back to personal ones. A missing account fails closed
  (D25, D26). Work GitHub identities require scoped configuration, and work Claude
  probes require their own config directory. App/PAT-backed runs drop ambient human
  credentials; required scoped identity never falls back (D47).
- **Console access:** loopback by default. Writes accept only loopback or Tailscale source
  addresses and a JSON body with `X-Mahler-Console: 1`; a supplied foreign Origin is
  rejected. Missing Origin is accepted for non-browser callers. Ordinary reads do not
  authenticate (`/api/chat` is guarded), so exposing the console beyond a trusted
  network is the operator's decision.
- **One dispatcher:** Mahler is the only autonomous dispatcher of managed projects. No
  external scanner may act on `origin/mahler/*` or Mahler worktree roots (D2).
- **Data:** production data is off-limits to agents, and tests never touch `~/.mahler`
  (D12, D16).

## Not built

The remaining MCP tools (`ask_user`, `report_progress`, `get_context`),
platform rollback, a Mahler-native anomaly sensor, and unattended restore drills are
planned, not shipped. [ROADMAP.md](ROADMAP.md) tracks them.

Manual-verdict in-app panels are superseded by D10, not pending implementation.
