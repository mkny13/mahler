# Mahler — Architecture

A map of the system as shipped. It describes what the code does today and points to the
records that explain why. It does not repeat them:

- [DESIGN.md](DESIGN.md) is the decision record (D1–D36). It keeps history, so older
  sections may describe a target that later decisions amended.
- [ROADMAP.md](ROADMAP.md) says what is done, in progress, and planned.
- [README.md](README.md) covers setup and operation.
- [CLAUDE.md](CLAUDE.md) and [AGENTS.md](AGENTS.md) are the rules agents follow.

Planned behavior is named as planned. The retired `dispatch`/`thread` tooling (DESIGN D2)
is not part of this architecture.

## Components

Mahler is a macOS, single-operator service written in Python 3.12 with the standard
library only. It shells out to `git`, `gh` and the agent CLIs.

| Component | Where | Role |
|---|---|---|
| Launcher | `launcher/` | Hand-installed. Runs one tick every 60 s under `launchd`, updates `~/.mahler/app` to a CI-green `origin/main`, and rolls back (D17). |
| Scheduler | `mahler/scheduler.py` | The tick's entry: lock, `Ctx`, and the order of passes. |
| Passes | `watchdog.py`, `sync.py`, `finalize.py`, `ship.py`, `tick.py`, `usage.py`, `warmup.py`, `resets.py`, `platform_audit.py` | One module per pass. |
| Ledger | `mahler/ledger.py` | SQLite (WAL): execution state and leases. |
| Router | `mahler/router.py`, `platforms.py`, `config.py` | Quota-aware, deterministic platform and account choice. |
| Runner | `mahler/runner.py`, `prompt.py`, `recipes/` | Worktrees, detached agent processes, snapshots, and the prompt each role is given. |
| GitHub | `mahler/gh.py` | Every GitHub read and write, through `gh`, under the project's own login. |
| Console | `mahler/serve.py`, `mahler/console/` | Optional web console (D27). A separate process. |
| MCP server | `mahler/mcp.py` | Local stdio JSON-RPC. Seven tools: `list_items`, `add_item`, `claim`, `heartbeat`, `release`, `handoff`, `next_id`. |
| CLI | `mahler/cli.py`, `bin/mahler` | Operator and interactive-session commands, plus `mahler hooks` for Claude Code. |
| Maintenance | `backup.py`, `digest.py`, `janitor.py`, `releases.py`, `notify.py`, `presence.py`, `launch_health.py`, `repo_guard.py` | Backups, daily digest, cleanup, release ledger, ntfy pings, session presence, launch circuit breaker, CI repo guard. |

Agents run only inside runs. The tick never calls an LLM (DESIGN Principle 7).

## Persistent state

Item content lives in GitHub: issue text, discussion, handoff notes, labels, PRs. Execution
state lives in the ledger. Neither duplicates the other (DESIGN D4, D5).

- **`~/.mahler/mahler.db`** (SQLite). Tables: `items` (state machine: `inbox`, `ready`,
  `working`, `verifying`, `needs_you`, `parked`, `failed`, `parent`, `done`), `leases`
  (compare-and-set, with epochs), `runs`, `usage`, `events`, `counters`, `console_actions`,
  `uat`, `releases`, `release_items`, and a `kv` table for small flags and caches (pause,
  holds, breakers, review evidence).
- **`~/.mahler/config.toml`**: machine policy (projects, accounts, platforms, routing,
  caps). Per-repo `.mahler/project.toml` declares verify, release and data contracts and
  is checked in so agents can read it.
- **`~/.mahler/runs/<id>/`**: run logs and status files.
- **`~/.mahler/worktrees/<project>/<issue>-run<id>`**: one worktree per run. A project's
  `worktree_root` can move it.
- **`~/.mahler/app`**, plus the launcher's `known_good`, `launch_ok` and `bad_sha` files.
- **Snapshot refs** `mahler/snapshot/*` on the remote, for work saved at the end of a run.

The daemon's database and config are never edited by a task. Tests use `Ledger(':memory:')`
or temp dirs.

## Tick flow

`mahler tick` takes an exclusive lock (an overlapping tick exits at once), then runs these
passes in order, each exception-safe per project:

1. Drain the console outbox (writes the console queued).
2. Compute the Claude burst state (D23).
3. **Watchdog:** heartbeat live runs; enforce idle and wall-clock limits; stop for quota
   or pre-emption; call `finalize` for every run that ended.
4. **Sync** each enabled project from GitHub: open issues into items, closed issues
   release work, comment commands, plain replies on `needs-you` items, label edits,
   dependencies, parents.
5. Expire stale leases, and close parents whose children are done.
6. If paused, stop here for scheduling. Otherwise: refresh usage, spend banked Codex
   resets, run the warm-up passes (D34–D36), queue maintenance and platform-audit issues
   (D20), then **schedule**: order candidates, check eligibility, route, and start runs.
7. Record why anything was held (the console's idle reasons).
8. **Ship** (skipped while paused).
9. Mirror state labels back to GitHub.
10. Backups, daily digest and janitor run even while paused.

Exit code 3 tells the launcher a global launch breaker is open (D17).

## Run and build flow

1. `tick.start` takes a lease on the item and picks a platform and account (D8, D25, D26).
2. `runner.prepare` creates the worktree on `mahler/<issue>-<slug>`, starting from the
   handoff branch or base. It installs a pre-push epoch fence through `GIT_CONFIG_*` that
   chains to the repo's own hooks. `runner.launch` spawns the agent CLI detached, with a
   `MAHLER_*` environment and the run's own account credentials.
3. The agent works from a recipe (`recipes/sort.md`, `build.md`, `fix.md`, `review.md`),
   verifies, commits, pushes, and ends with a `STATUS:` line. It does not open PRs.
4. The watchdog supervises the process across ticks. **Every exit is a handoff** (D9):
   `finalize` reads the log and STATUS line, snapshots unsaved work to a pushed ref,
   comments on the issue, and moves the item: retry, `needs_you`, `parked`, `failed`,
   or on to shipping. A missing STATUS line on a branch that passes `verify` still counts
   as done (D18).
5. Work is capped by `max_parallel`, attempts (`max_attempts`) and escalation tiers.
   Launch failures feed a persistent circuit breaker (D17).

## Shipping flow

The conductor owns the mechanical tail (D18, D19, D28). One lease holder, `conductor`,
carries an item while it verifies, and watching takes no run capacity.

1. Push the branch and open a PR with `Fixes #N`, the agent's summary and the issue's
   "Needs a human to check" list.
2. Watch CI across ticks. Red CI starts a `fix` run from the failing log tail.
3. For `size:m`, `size:l` and risk-keyword items, run an independent **review** on a
   different platform between green CI and merge (D11). Review has a blocking bar and a
   convergence check that escalates to `needs_you`.
4. Recheck the base and lease, then squash-merge, wait for GitHub to confirm the merge, and
   comment. A moved base sends the item back for a rebuild.
5. Record the shipped change: the console UAT queue when a human check is listed, and the
   release ledger (D31).

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
- **MCP** over stdio for chats that cannot run the CLI.
- **ntfy** pings and a daily digest.

## Trust boundaries

- **The daemon runs from a pinned clone** (`~/.mahler/app`), never from a working checkout.
  Changes reach it only through a merge to `main` with green CI and passing unit tests
  (D17). `launcher/` changes take effect only when someone re-runs `launcher/install.sh`.
- **Agents are unattended and isolated:** one worktree per run, secrets redacted from logs
  (`redact.py`), the epoch fence on push, and a lease that lets an interactive session
  pre-empt a run (D6).
- **Accounts:** each project names the logins it may spend. Work accounts stay on their
  own credentials and never fall back to personal ones. A missing account fails closed
  (D25, D26).
- **Console access:** loopback by default. Writes accept only loopback or Tailscale source
  addresses, a JSON body with `X-Mahler-Console: 1`, and a matching origin. Reads do not
  authenticate, so exposing the console beyond a trusted network is the operator's
  decision.
- **One dispatcher:** Mahler is the only autonomous dispatcher of managed projects. No
  external scanner may act on `origin/mahler/*` or Mahler worktree roots (D2).
- **Data:** production data is off-limits to agents, and tests never touch `~/.mahler`
  (D12, D16).

## Not built

The remaining MCP tools (`ask_user`, `report_progress`, `get_context`), in-app UAT panels,
platform rollback, a Mahler-native anomaly sensor, and unattended restore drills are
planned, not shipped. [ROADMAP.md](ROADMAP.md) tracks them.
