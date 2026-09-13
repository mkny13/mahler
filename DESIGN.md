# Mahler — Design

Status: design agreed 2026-09-12; the Phase B bootstrap kernel is built and running. Build order lives in [ROADMAP.md](ROADMAP.md).
Gustav Mahler was a conductor. This tool conducts: it decides who plays which part, and when.

---

## The one-page version

You describe what you want: goals, bugs you hit, features you'd like. You do it from any
chat (Claude, Cline, Antigravity, the Claude app on your phone), from Mahler's console, or by
tapping "this doesn't work" inside the app you're testing. Every one of those becomes a
**GitHub issue** in that app's repo. Issues are the single backlog, and nothing important
lives anywhere else.

Mahler runs on the Mac mini. It watches those issues. An agent (Claude, as planner) sorts each
new one: it writes down what "done" means, sizes it, and splits it if it's big. Once an item
is sorted, Mahler hands it to whichever platform has capacity: Antigravity or Cline's free
models first, and Claude only when those are used up and Claude still has headroom left for you.
The agent works on its own branch, in its own copy of the repo. It runs the project's tests
and builds, opens a PR, waits for CI, merges, deploys to your devices, and writes down what you
should check.

If a platform runs low on quota mid-task, the agent saves its work and leaves a handoff note on
the issue, and the next platform picks up exactly where it stopped. If *you* start working on
the same item in a chat, your session wins: the agent steps aside and hands you its work.

When a build is ready, your phone buzzes (ntfy). You open the app, and a small UAT panel lists
what's new to check. You tap pass or fail and add a note or screenshot. A failure goes straight
back into the backlog as a bug. Everything is undoable: every change is one revertible merge,
every deploy can be rolled back, and your data is backed up before anything risky touches it.

The rest of this document is the reasoning behind each of those sentences, written for the
agents that will build it.

---

## Principles

1. **One backlog, many workers.** Every platform reads and writes the same queue. Lost
   threads come from state living inside a platform. So no state lives inside a platform.
2. **Every exit is a handoff.** Finishing, crashing, hitting quota and being pre-empted all go
   through the same path: checkpoint to a pushed branch, write a handoff note, release the
   lease. There is no special "abnormal exit" code to get wrong.
3. **You pre-empt robots.** An interactive session always wins a conflict with an autonomous run.
4. **Safety through isolation, reversibility and backups, not approval gates.** (Standing
   preference: gates are friction. Work pauses only for decisions only you can make.)
5. **Agents must see the results of their own changes.** A change isn't done because an agent
   says so. It's done when tests, CI, preview/smoke checks and, eventually, your UAT agree.
6. **Borrow the commodity, build the glue.** Build only what nobody else provides: quota-aware
   routing across subscription and free-tier CLIs, the lease model, and the UAT loop.
7. **Deterministic control plane.** Mahler's scheduler never calls an LLM. LLMs run only inside
   runs. This keeps the core free, fast and debuggable, the same principle `thread` had.

---

## What exists today, and the gap

| Thing | Where | Role today | Fate under Mahler |
|---|---|---|---|
| `thread` | `~/ai-tools/thread.py` | Read-only anomaly scanner (dead sessions, stray branches, WIP) | **Absorbed as a sensor.** Its scan code is reused unchanged in character; its flags become issues (D2) |
| `dispatch` | `~/ai-tools/dispatch.py`, launchd `com.mike.dispatch` | Launches agents at thread's flags; claim/cooldown store, caps | **Superseded.** Its machinery is generalised into leases/runs; timer retired at cutover |
| ThreadBar | `~/ai-tools/ThreadBar` | Menu-bar view of thread/dispatch | Re-pointed at Mahler's API (it's a thin display), or retired |
| Cline Kanban | `kanban`, `127.0.0.1:3484` | Per-card worktrees for phish-in-app, groundwork | **Retired.** A second board recreates the lost-threads problem |
| TASKS.md | each repo | Session continuity *and* (accidentally) backlog | **Session continuity only**, as its own header says in phish-in-app |
| ROADMAP.md | some repos | Vision + "suggested build order" | Vision stays; build order becomes issue priority |
| UAT.md + `uat-server.py` | phish-in-app | Manual-verification checklist | **The prototype for the UAT loop** (D10); migrated into it |
| In-app Feedback buttons | phish-in-app (Android, macOS) | Prefilled GitHub-issue URL | Kept; later posts to Mahler with the GitHub URL as fallback |
| `ci-wait.sh`, `cut-beta.sh` | phish-in-app/scripts | CI wait + log tail; beta release | Generalised into the verify/release contract (D11) |
| Claude Remote Control | launchd `com.mike.claude-remote-control` | Phone → Claude sessions on the Mac mini | Kept. It's how phone chats reach Mahler's MCP tools |
| Backblaze | whole Mac + `/Volumes/ExtSSD160` | Continuous offsite backup | Kept; Mahler adds app-data backups on top (D12) |

**The gap** (from [BACKLOG.md](BACKLOG.md)): thread's flags are designed to leave healthy,
tracked, in-progress work alone. So dispatch can never pick up ordinary planned work. There's no
fast staleness signal, and no ownership model for "a human might start this any second."
Beyond that gap, the backlog is spread across five places and three platforms, and nothing
knows which platform has quota left.

---

## Architecture

```
 You — Mac or Pixel, over Tailscale
   chats: Claude desktop/CLI · Claude app → Remote Control · Cline · Antigravity
   Mahler console (web) · ntfy pings · GitHub app · in-app UAT panel
        │                         ▲
        ▼                         │
┌──────────────────────────── Mac mini ─────────────────────────────┐
│  mahlerd (one launchd daemon)                                     │
│   ├─ GitHub sync ◀───────────▶ GitHub: Issues · PRs · Actions ·   │
│   │                             Releases  (item content lives here)│
│   ├─ ledger (SQLite): leases · runs · usage · builds · events     │
│   ├─ scheduler + router (quota-aware, deterministic)              │
│   ├─ watchdog: heartbeats · yield · reap · snapshot               │
│   ├─ sensors: thread scan · session presence                      │
│   ├─ API: console (HTML) · MCP · UAT endpoints                    │
│   └─ jobs: backups · notifications · usage probes                 │
│            │ launches, supervises                                 │
│            ▼                                                      │
│   runner ─▶ claude -p │ agy -p │ cline   (one worktree per run)   │
└───────────────────────────────────────────────────────────────────┘
```

Item **content** (what to do, discussion, screenshots, handoff notes) lives in GitHub.
Execution **state** (who holds what right now, runs, quota, builds) lives in Mahler's ledger.
Neither duplicates the other's truth. Labels mirrored to GitHub are a display, never read back
as authority.

---

## Decisions

### D1 — Scope: one system, top to bottom

Mahler covers intake → planning → routing → execution → verification → release → UAT →
rollback → backups, for every project you opt in. Some of this goes beyond "orchestration." It's
included because each missing piece is a place where a thread gets lost, or where an agent
can't see its own results.

### D2 — Supersede dispatch; absorb thread as a sensor

- **dispatch is replaced, not wrapped.** Its load-bearing ideas carry over directly:
  - per-project opt-in,
  - the claim store as the safety mechanism (now leases, D6),
  - concurrency caps,
  - exponential backoff with an attempt cap,
  - an exclusive tick lock,
  - launchd needing an explicit `PATH`,
  - `bypassPermissions` plus a denylist for Claude.

  Wrapping it would mean two claim stores keyed differently (flag fingerprint vs item), which is
  exactly the duplication BACKLOG warned about.
- **thread's scanner stays read-only and becomes one of Mahler's sensors.** Every 10 minutes
  Mahler runs the scan. It turns each actionable flag into an issue labelled `type:anomaly`, using
  the same flag codes and fingerprint rules dispatch uses for dedupe (so one open issue per
  fingerprint). Anomalies then flow through the same queue, leases and routing as planned
  work: one queue, one lock system. All of thread's calibration (the squash-merge `git cherry`
  check, the default-branch exclusion, `died_mid_task` requiring TASKS.md) is kept verbatim.
- `thread` the CLI keeps working for as long as it's useful. Mahler imports its scan
  functions rather than re-deriving git state.
- **Cutover is per project.** Disable a project in `dispatch.toml` in the same step it's enabled
  in Mahler. Retire the launchd timer when the last project moves.

### D3 — Borrow vs build

| Candidate | Verdict | Why |
|---|---|---|
| **Gas City** (v1.x, open-source SDK built from Gas Town) | **Borrow patterns, don't adopt** | Built for hundreds of concurrent agents on API budgets. It needs tmux, Go, Dolt and Beads, and it has many overlapping concepts. Its cost profile is the opposite of quota-rationed free and Pro tiers, and you couldn't debug it. Patterns taken: persistent work / disposable sessions; "work on your hook means you run it"; handoff notes a successor reads; reusable per-role templates ("formulas" → Mahler **recipes**); a watchdog that un-sticks workers. Re-evaluated at the post-POC checkpoint |
| **Beads** (`bd`) | Don't adopt | Agent-first issue graph, but no phone UI or attachments, and v1 wants Dolt. GitHub Issues won on phone access and screenshots (your choice) |
| **Cline Kanban** | Retire | Good worktree-per-card runner, but no quota awareness, no Antigravity, board-local JSON state, not phone-first. Keeping it means two boards |
| vibe-kanban, Conductor, claude-squad | Don't adopt | Same category as Cline Kanban; same objections |
| **claude-code-action** (GitHub Action) | Later option | Runs Claude on issues in GitHub's cloud. It uses the same Pro quota and can't reach the Mac mini's Xcode, emulator or local files. Borrowed idea: commands in issue comments (`/mahler go`, D10) |
| Claude cloud sessions / routines | Later option (Phase 8) | Extra workers for GitHub-hosted repos, but they share Pro quota; picked up by the presence sensor (D6) meanwhile |
| **GitHub** Issues, PRs, Actions, Releases, mobile app | **Adopt** | Item store, CI, artifact hosting, phone surface |
| **Tailscale** (`tailscale serve`) | **Adopt** | Private HTTPS to the Mac mini from the Pixel and the MacBook; nothing exposed to the internet |
| **ntfy** | **Adopt** | Free push to Android with no account (your choice) |
| **Claude Code** hooks, statusline `rate_limits`, Remote Control, headless `-p` | **Adopt** | Lease participation, usage sensing, phone chats, headless runs |
| **agy** (Antigravity CLI), **cline** CLI | **Adopt** | Headless runners for the two free platforms |
| Playwright, git worktrees, Backblaze, `gh` | **Adopt** | Verification, isolation, offsite backup, GitHub plumbing |

What Mahler builds: the ledger and lease protocol, the quota-aware router, runners, the
handoff protocol, the console, the MCP server, the UAT contract, and backup orchestration.
All of it is glue between adopted parts.

### D4 — GitHub Issues are the backlog

- **One issue = one work item**, in the repo it concerns. Goals are parent issues with
  sub-issues (GitHub sub-issues). Dependencies are written as a `Depends on: #12` line in the
  body, which Mahler parses. That's simple and portable. GitHub's native "blocked by" can be
  adopted later.
- **Labels** (created at onboarding):
  - `type:` `bug` · `feature` · `chore` · `goal` · `uat` · `anomaly`
  - `p1` · `p2` · `p3` (default p2; UAT failures default to p1)
  - `size:` `s` · `m` · `l`
  - `mahler:` `<state>` (a single display label mirroring the ledger)
  - optional `area:<name>` (serialises items that touch the same area, D6)
  - optional `platform:<name>` (you pinned it to a platform)
- **Body template** (the sorting step writes this and keeps your original words quoted at the
  top): *Problem/goal · Done when (acceptance checks) · Needs a human to check (becomes UAT
  items) · Context · Out of scope.*
- **Project goals and vision** stay in-repo (`ROADMAP.md` top section, or `PROJECT.md`) so every
  run reads them. The "suggested build order" is migrated into issue priority.
- **Scope**: a project joins Mahler only when it has a private GitHub repo (this also fixes its
  backup story). Non-git folders (`Fantasy Sports`, `MTG`, …) get `git init` plus a private repo
  when onboarded, not before. `olympic_hockey` is deliberately unpushed, so it stays out of
  scope. The sensor still watches it. `mcp-phish` (read-only upstream) is permanently excluded,
  as in dispatch.
- **Migration**: onboarding converts open TASKS.md Now/Next items, ROADMAP build-order
  entries, open UAT `[!]` items and Cline Kanban cards into issues. Then it writes a
  "Backlog moved to GitHub Issues" note into TASKS.md and CLAUDE.md.

### D5 — Mahler's own state

- SQLite in WAL mode at `~/.mahler/mahler.db`, on the Mac mini's **internal** disk. If the
  external SSD unmounts, Mahler notices, pauses those projects, and can still tell you why.
- Tables: `projects`, `items` (cache of issue fields + ledger state), `leases`, `runs`,
  `handoffs`, `usage_samples`, `builds`, `uat_results`, `events` (append-only audit log,
  every state change with its cause).
- **GitHub sync is polling, not webhooks**, so nothing is exposed publicly. Mahler polls every
  60s with conditional requests (ETags); unchanged responses don't count against the rate
  limit. Your comments, label changes and closes are picked up within a minute.
- Losing `mahler.db` loses run history and live leases only. Items, handoffs and code are
  all in GitHub. The DB is still backed up nightly (D12).

### D6 — Ownership and locking (the core decision)

This is the problem thread and dispatch never had: anomalies are unclaimed by definition,
but queued work can be claimed by anyone at any moment.

**The simplifying fact:** you don't edit code by hand. "A human starts on an item" always means
*an interactive agent session* starts on it. Sessions can be instrumented, told the rules and
given tools. So the model has three layers: cooperative leases for sessions that participate,
presence detection for those that don't, and isolation so that when both fail, the cost is
wasted tokens rather than damaged work.

#### Layer 1 — Leases (cooperative, authoritative)

- **Mahler's ledger is the only lock authority.** Every claim goes through `mahlerd`, whether it
  comes from MCP, the CLI or HTTP. A claim is an atomic compare-and-set in SQLite:
  `UPDATE leases … WHERE item=? AND (holder IS NULL OR expires_at < now)`.
  This is the property nothing else here can give: GitHub assignees and labels have no
  compare-and-set and are eventually consistent; a lock file committed to git needs a push
  round-trip and races across clones; `flock` dies with its process, while leases must outlive
  processes to support handoffs.
- **Lease record:** `item`, `holder_id`, `holder_kind` (`auto` | `interactive` | `primary`),
  `platform`, `worktree`, `branch`, `epoch`, `acquired_at`, `heartbeat_at`, `expires_at`, `state`
  (`held` | `yielding`).
- **Epochs are fencing tokens.** Every grant increments the item's epoch. Before any
  outward-facing step, Mahler-managed worktrees check that the epoch is still current: via a git
  `pre-push` hook, and via a Claude `PreToolUse` hook on `gh pr create|merge`. So a "zombie" run
  that was presumed dead but is still going can't push over its successor.
- **Heartbeats:**
  - **Autonomous runs:** the runner wrapper heartbeats every 60s while the child process lives.
    A separate progress watchdog treats 20 minutes without a tool call or commit as hung.
    Lease TTL is 10 minutes.
  - **Interactive Claude sessions:** heartbeat from hooks (`UserPromptSubmit`, `PostToolUse`).
    TTL is 30 minutes of inactivity; you're allowed to think.
  - **Cline and Antigravity interactive sessions:** heartbeat through the MCP `heartbeat` tool,
    which their rules files tell them to call. Where no heartbeat arrives, the presence
    sensor (layer 2) stands in.
- **GitHub mirror:** on grant, the `mahler:working` label plus a short comment ("Antigravity
  started, branch `mahler/12-dark-mode`"). This is for you to see; it's never read back.

**Interactive `holder_id`s are per-session, not a shared `"you"` (mahler#33, 2026-09-13).**
Two interactive Claude Code sessions sharing `~/Mahler` at once both claimed under the literal
string `interactive:you` — indistinguishable in the ledger, so nothing showed that a second
session was even active until one nearly discarded the other's uncommitted work with a `git
reset` (mahler#27). `mahler claim/heartbeat/release`'s `--as` now defaults to the first 8
characters of `$CLAUDE_CODE_SESSION_ID` (falling back to `"you"` outside a Claude Code session)
instead of a hardcoded default, so concurrent sessions show up as distinct holders — no ledger
migration needed, `holder` is a free-text label and old leases keep whatever they were claimed
under. `mahler claim` and the `SessionStart` hook now also print a note when another session
already holds a live `interactive:*` lease in the same project, pointing at CLAUDE.md's worktree
rule (added alongside D19, mahler#27) rather than the shared primary checkout.

#### Layer 2 — Presence (non-cooperative, advisory)

Some sessions won't claim: a forgotten instruction, a platform without hooks, a Claude cloud
session. The sensor infers their activity from the traces sessions leave:

- Claude Code transcripts (`~/.claude/projects/<path>/*.jsonl`: mtime and recorded cwd)
- Cline task history mtimes and cwd
- Antigravity's conversation store (location confirmed in a Phase 0 spike)
- dirty-file mtimes in the primary checkout and in any worktree Mahler didn't create
- branches, commits or PRs that mention `#N` or were pushed from outside Mahler

Two rules use this:

- **Attributable activity** (a non-Mahler branch or PR references `#12`) becomes an implicit
  `interactive` lease on #12. If an autonomous run holds #12, the pre-emption rule applies.
- **Unattributable activity** (files changing in a project, but no idea which item) puts the
  project on a **hot hold**: no *new* autonomous starts there until 20 minutes after the
  activity stops. Running work continues. Instrumented Claude sessions that simply haven't
  claimed anything don't trigger a hot hold. They're handled by nudges instead:
  - `SessionStart` injects "these items are currently held by autonomous runs: … If you work on
    a backlog item, claim it (`mahler_claim`)."
  - The first `Edit`/`Write` in an unclaimed session injects a one-line reminder to claim if the
    work relates to an item.

#### Layer 3 — Isolation and optimistic merge (collisions are cheap)

- Autonomous runs **never** touch the primary checkout. Each run gets its own worktree on
  branch `mahler/<issue>-<slug>`. Interactive sessions are encouraged into worktrees too; Remote
  Control already uses `--spawn=worktree`.
- So the worst collision is **two branches addressing one issue**. Nothing is corrupted.
  The first PR to merge wins and closes the issue. The other holder's next heartbeat returns
  `item_closed`: it stops, and its branch is kept as `mahler/abandoned/<issue>-<run>` for
  14 days. The cost is bounded by how often runs check the lease: a few minutes of tokens.
- **Parallel items in one project** start on current base and hold their slot until they merge
  (D19), so they rarely collide; when one does, the conductor sends it back for a rebuild. Items
  sharing an `area:` label aren't run concurrently. Per-project `max_parallel` defaults to 2.
- **Shared counters** (like phish-in-app's `Dnnn` decision IDs, which collided in D208) are
  handed out by Mahler: `mahler next-id <project> D` is atomic. That removes a whole category of
  merge-time collision git can't detect.

#### Pre-emption: you win

When an interactive session claims an item an autonomous run holds:

1. Mahler grants the interactive claim **immediately**, and marks the auto lease `yielding`.
2. The auto run is told to yield at its next hook or heartbeat check. It gets 2 minutes to
   commit, push its branch and post a handoff note. After that the runner stops it and Mahler
   takes a snapshot itself (below).
3. Your session is told: "Antigravity was on this for 14 minutes. Its work is on
   `mahler/12-dark-mode` (3 commits); handoff note is on the issue. Continue from that branch."
   By default it does.
4. Two interactive claims on one item: the second is told who holds it and when they were last
   active, and may take it over explicitly (it's all you). The first learns this at its next
   hook.
5. Two autonomous runs never contend. The scheduler's compare-and-set makes double assignment
   impossible.

#### Work in the primary checkout (what thread/dispatch saw)

TASKS.md `## Now` items that reference `#N` are treated as a `primary` lease on #N, held by
the primary checkout. It stays alive while that checkout shows activity. After **24 hours** of
nothing, the lease expires. Mahler then snapshots the dirty tree **without touching it**: it
builds a commit from a temporary index (`GIT_INDEX_FILE=… git add -A`, `write-tree`,
`commit-tree`) and pushes it to `mahler/snapshot/<issue>-<date>`. The item goes back to `ready`,
with the snapshot branch as its starting point. Your working tree, index and stash are never
modified. This is how the phish-in-app case from BACKLOG gets picked up: two open Now items with
matching dirty files, untouched.

#### Item state machine

```
inbox ─sort─▶ ready ─claim─▶ working ─PR─▶ verifying ─merge─▶ shipped ─UAT─▶ done
  │             ▲  ▲            │                                  │
  │             │  └─handoff────┤ (quota · pre-empted · hung ·     │
  │             │               │  crashed · partial)              │
  ▼             │               ▼                                  ▼
needs-you ──────┘            failed (attempts ≥3 → needs-you)   reopened → ready (p1)
parked (you said "not now")
```

On GitHub, `shipped` and `done` are closed issues. Everything else is open, with a
`mahler:<state>` label.

### D7 — Staleness: activity clocks, not a calendar

The 7-day signal is replaced by four clocks, all configurable per project:

| Clock | Default | Answers |
|---|---|---|
| **Settle delay** | 10 min after sorting | "Did you just log this because you're about to do it yourself?" Items created from a chat can be claimed on the spot (`mahler_add_item(…, claim=true)`); otherwise they wait this long before autonomous pickup |
| **Autonomous lease TTL** | 10 min without heartbeat; 20 min without progress | "Did the agent die or hang?" Leads to a handoff |
| **Interactive lease TTL** | 30 min without activity | "Did you walk away?" The item becomes `ready` from the session's branch |
| **Primary-checkout orphan** | 24 h without activity | "Was this Now item abandoned?" Leads to a non-destructive snapshot, then `ready` |

With explicit leases, "nobody's picked this up in an hour" stops being a judgement. An unleased
`ready` item is simply available.

### D8 — Platforms, quota sensing and routing

| Platform | Headless runner | Usage signal (verified 2026-09-12) | Role |
|---|---|---|---|
| **Claude Code** (Pro) | `claude -p --output-format stream-json --verbose --permission-mode bypassPermissions` (+ denylist) | Every headless run emits a `rate_limit_event` with `unifiedWindows.five_hour` / `seven_day.utilization`. When no run is live, a lean probe gives the same reading for ~700 tokens (`--model haiku --tools "" --strict-mcp-config --setting-sources ""`). Not `--bare`: it skips OAuth | **Planner** (sorting, specs, splitting, hard-bug diagnosis) always. **Builder** only after the free tiers are spent, and under the reserve |
| **Antigravity: Claude/GPT pool** (free) | `agy -p … --add-dir <worktree> --model claude-opus-4-6-thinking --dangerously-skip-permissions --output-format stream-json` | `agy -p /usage --output-format json`, which costs nothing and reports `remaining_fraction` + `reset_time` per pool and window | **First-choice builder** |
| **Antigravity: Gemini pool** (free) | same, `--model gemini-3.1-pro-high` or `gemini-3.8-flash-high` | same probe, separate pool | Second-choice builder |
| **Cline** (free models) | `cline --cwd <worktree> --json --auto-approve true -t <secs> <prompt>` | None: its JSON reports `totalCost: 0` and no quota, so it's routed as **unmetered** and backed off for an hour after any rate-limit error | Builder of any size, second in build order after Antigravity's Claude pool (you judge its free GLM-5.3-flash on par with Sonnet 4.x; the quota is generous but unstated). Verified 2026-09-12 (S3). Daemon-launched runs need macOS Documents access (see below) |
| **Copilot CLI** (`@github/copilot`, GitHub Education license) | `copilot -p <prompt> -C <worktree> --allow-all-tools --output-format json` | Unlike Cline/Kilo, has a real cap: GitHub bills Copilot in **AI Credits** (mahler#38), Pro/Education include 1500/month. No cheap CLI-level probe, but `gh api /users/<login>/settings/billing/ai_credit/usage` (needs the `user` OAuth scope) reports the month's consumption, so it's routed as a normal **metered** platform with a single `monthly` window instead of 5h/weekly | Builder, size `s` only, ahead of Kilo — it runs real frontier models (verified: `claude-sonnet-5`), despite the smaller monthly allowance. CLI flags verified end-to-end 2026-09-13 (mahler#25); the AI-credits billing probe verified 2026-09-12 (mahler#38) |
| **Kilo** (`@kilocode/cli`, kilo.ai account, model `kilo/kilo-auto/free`) | `kilo run <prompt> --dir <worktree> --auto --format json -m kilo/kilo-auto/free` | None: usage is per-account credits with no cheap probe, so it's **unmetered** like Cline | Builder, size `s` only, last among the free tiers — `kilo-auto` draws from a grab-bag of smaller/niche `:free` models of unverified quality. Needs `kilo auth login` (a one-time browser flow only the account owner can do). The default (non-`:free`) model 402s immediately ("Add credits to continue") — no "quota" in the text, so `QUOTA_WORDS` covers "credit" and `usage_limit_exceeded` too. Verified end-to-end 2026-09-13 (mahler#29) |
| OpenCode | — | — | Later backend (Phase 8) |

**Antigravity test results (2026-09-12).**
- `agy` 1.1.23 was already installed at `~/.local/bin/agy` and signed in. dispatch only
  searched the app bundle, which is why NOTES.md says Antigravity has no CLI.
- Headless runs edit, run commands and commit unattended, and exit 0 with `status: SUCCESS`.
- **Trap:** without `--add-dir <path>`, agy ignores the directory it's started in and works in
  `~/.gemini/antigravity-cli/scratch`. Every Mahler invocation passes `--add-dir` and names
  the worktree in the prompt.
- The free account has **two independent quota pools**: Gemini models, and Claude
  Opus/Sonnet 4.6 + GPT-OSS. Each has a 5-hour and a weekly window. So Antigravity counts as
  two platforms for routing.
- Not yet observed: what quota exhaustion looks like mid-run. Mahler probes before starting,
  and treats any result `status` other than `SUCCESS` as a failed run.
- Manual clipboard handoff to the Antigravity IDE is no longer needed.

**Cline and macOS privacy (mahler#12, 2026-09-12).** Cline reads `~/Documents/Cline/` (global
rules, hooks, workflows) every time it starts, and nothing relocates it except `HOME`. When
the launchd daemon starts it, macOS holds the daemon's Homebrew `python3.14` responsible for
that access. The first time, macOS showed an Allow/Don't Allow dialog on the Mac mini, and every
Cline process blocked on it without printing anything. Runs 20–23 sat for up to 73 minutes
until someone clicked Allow over Screen Sharing. From a terminal the test passed, because the
terminal app already had access. Consequences:
- The grant belongs to that exact Python build. **A Homebrew Python upgrade brings the dialog
  back**, and it can hit any platform that touches a protected folder.
- A run whose log is still empty `startup_timeout_minutes` (10) after the agent started is
  stopped as `silent`. It doesn't count as an attempt. Its platform goes on hold for
  `backoff_minutes`, meaning no new runs start there. You get a high-priority ping saying to
  look for a permission dialog.
- A SIGTERM that lands while a process is blocked like this can be acted on late or never.
  So the watchdog SIGKILLs a run's whole process group before finalizing it, even after the
  shell has exited. Before this fix, runs 20–22 woke up after their worktrees were deleted,
  and crashed.

**Routing policy** (your answer: *Claude plans; use up the two free tiers first; after that
Claude may build, but keep headroom for me*):

1. **Sorting and planning runs go to Claude.** They're short and high-leverage. If Claude is
   over the reserve, sorting falls back to Antigravity rather than waiting.
2. **Build runs, in order:**
   - Antigravity's Claude/GPT pool, then its Gemini pool, then Cline-free, then Copilot CLI,
     then Kilo. Each is used while it has headroom and fits the item's size (`s` → any;
     `m` → Antigravity or Claude; `l` → split first). Copilot and Kilo are size `s` only,
     same as Cline — Copilot goes first for its model quality, Kilo last since its free
     route is a grab-bag of smaller models.
   - Then Claude, only while the **5-hour window is under 60% and the weekly under 70%**.
   - A `platform:` label overrides the order.
3. **Nothing autonomous ever runs into paid extra usage.** At or above 100%, Claude is marked
   exhausted until `resets_at`.
4. **Escalation:** two failed verify rounds on a weaker platform → the item is retried a tier
   up, starting from the same branch and handoff note.

**Thresholds.** Each platform has a *soft* line (stop starting runs) and a *hard* line (running
work yields):

| Platform | Soft (stop starting) | Hard (running work yields) |
|---|---|---|
| Antigravity | 85% | 90% (your figure) |
| Cline-free | — | on the first quota or rate-limit error |
| Claude, autonomous | 5 h 60% / 7 d 70% | 5 h 70% / 7 d 80% |

Platforms without a usage percentage are treated as 100% on the first quota error, and stay
unavailable until the reset time, parsed or with a backoff default. All of these numbers live in
`~/.mahler/config.toml`.

**Sensing Claude:** every Mahler Claude run's log is scanned for `rate_limit_event`. When no
run has reported in 15 minutes and a Claude run is about to start, Mahler takes a lean probe
first. A statusline sidecar (writing `rate_limits` from your own interactive sessions to
`~/.mahler/usage/claude.json`) is an optional extra source. Samples older than 15 minutes
count as "unknown", and the router treats unknown as "over soft". The probe showed
`overageStatus: rejected`, so extra usage is currently refused at the account level. Mahler
doesn't rely on that and stops on its own thresholds regardless.

### D9 — Handoff protocol

- **Checkpoint continuously:** every run's recipe says to commit after each meaningful step and
  push the `mahler/*` branch at least every 10 minutes. A hard kill loses minutes, not hours.
- **Handoff note** is an issue comment. It's durable, readable on the phone, and readable by any
  platform:
  ```
  <!-- mahler:handoff run=123 epoch=4 from=antigravity reason=quota -->
  **Handoff** — Antigravity → next platform (quota 91% weekly)
  Branch: `mahler/12-dark-mode` @ a1b2c3d (pushed)
  Done: toggle + theme tokens; unit tests pass
  Next: persist preference to settings table; wire Settings screen
  Watch out: `ThemeProvider` re-renders on every route change — see commit a1b2c3d msg
  Verify: fast ✅  full ⏳ not run
  ```
- **Yield delivery:**
  - **Claude:** a `PreToolUse` hook checks `~/.mahler/runs/<id>/yield`. If present, it blocks
    the tool call with the instruction to checkpoint, call `mahler_handoff`, and stop.
  - **agy and cline:** no hook support is confirmed yet, so the runner sends a termination
    signal after the grace period.
- **Mahler writes the note itself when an agent couldn't.** After a crash, hang or kill, it
  snapshots the worktree (the same non-destructive commit as D6), pushes it, and writes an
  automatic handoff note. The note contains the branch and diffstat, the last verify result,
  and the tail of the run transcript.
- **Successor prompt** = the item + project goals + the latest handoff note +
  `git log main..branch` + the verify contract. The successor starts from the branch, replayed
  onto current `main` first (D19).
- The existing `handoff`/`pickup` skills become Mahler-aware. They write and read this comment,
  and they record `#N` in TASKS.md Now items.

### D10 — How you talk to Mahler

- **Console** (web, on the Mac mini, reached at `https://mac-mini.<tailnet>.ts.net` via
  `tailscale serve`). Designed phone-first. Screens:
  - **Capture** (text, voice through the keyboard, photo or screenshot)
  - **Needs you** (one-tap answers)
  - **Ready to test**
  - **Now** (running work and quota gauges)
  - **Backlog** (per project, reorderable)
  - **History** (with **Undo**)
  - **Pause all**

  The POC version is plain server-rendered pages. The real screens come from the Claude Design
  phase after the POC (ROADMAP Phase 2).
- **MCP server** (`mahler`), registered with Claude Code, Cline and Antigravity. Tools:
  `list_items`, `add_item`, `claim`, `heartbeat`, `release`, `handoff`, `ask_user`,
  `report_progress`, `next_id`, `get_context`. This is what makes a chat an input: in any
  chat, on any platform, including the Claude app on your phone through Remote Control, you can
  say "log this as a bug in groundwork" or "start on #12 here." It's also how every platform
  sees the same queue.
- **`/mahler` skill** for Claude Code, plus a short Mahler section in each repo's
  `CLAUDE.md`/`AGENTS.md` (read by Claude, Cline and agy), with the claim/heartbeat/handoff rules.
- **ntfy** pings: *needs you* · *ready to test* · *handoff happened* · *failed and parked* ·
  *daily digest*. Messages carry a title and a console link only, never secrets or personal
  data. The POC uses ntfy.sh with an unguessable topic; self-hosting on the Mac mini is an
  option later.
- **GitHub comments are commands**, which is handy from the GitHub app:
  - `/mahler go` · `/mahler park` · `/mahler platform antigravity` · `/mahler undo`
  - Any reply on a `needs-you` item is taken as the answer.

#### The in-app UAT panel

- Every deploy or release **registers a build** with Mahler: project, version, SHA, channel,
  URL or APK link, and the UAT items it contains.
- UAT items come from the "Needs a human to check" section of each shipped issue. They are
  written by the agent that did the work, so you're only asked to check what automation can't.
- **Web apps:** a drop-in `mahler-uat.js` panel, loaded only in preview builds or when a UAT
  cookie is set, so your normal use stays clean. It shows "what's new in this build", with
  pass / fail / note per item, plus "report a problem here". A report captures the URL,
  a screenshot, console errors and the build SHA.
- **Android and macOS:** the existing Feedback buttons evolve into the same panel for
  debug/beta builds (shake or bubble on Android, a menu item on macOS).
- **Scripts:** the checklist lives in the console only.
- **Transport:** posts to the Mahler API over Tailscale (the phone is on the tailnet). When
  it's unreachable, the panel queues and retries, or falls back to the prefilled GitHub-issue
  URL the Feedback buttons use today.
- **A fail is new work:** it reopens the issue, or opens a linked `type:bug p1` with your note
  and screenshot, ready to route. Tapping "fails" is the whole bug report.
- phish-in-app's `UAT.md` and `uat-server.py` are the working prototype of this flow.
  Their item ids and status marks carry over.

### D11 — The agents' feedback loop

- **Verify contract.** Each repo carries `.mahler/project.toml`, which declares:
  - `verify.fast`: lint + unit tests, under ~2 minutes, run in the worktree before every push
  - `verify.full`: build + e2e
  - `preview`: how to get a preview URL or artifact
  - `smoke`: a post-deploy check
  - `release`: how a build reaches your devices
  - `data`: stores and backup/restore commands (D12)
  - `canary`: an optional "break a file, confirm the build fails" check, for stacks where
    worktree builds can silently compile the wrong checkout (phish-in-app D206/D207)
- **Trust but verify** (made concrete by D18). An agent's "done" is a claim. Mahler moves an item to `shipped` only
  after independent signals agree: CI green on the PR, `verify.full` green, the deploy
  succeeded, and smoke green. On failure, Mahler feeds the failing log tail (generalised from
  `ci-wait.sh`) back to the live run. If the run has ended, it starts a fix run from the
  handoff. Attempts are capped.
- **Local first, CI second.** The Mac mini runs Xcode, Gradle and emulator checks locally.
  That's fast and free, and GitHub's macOS minutes count 10× against private-repo allowances.
  GitHub Actions stays authoritative for web and Linux jobs. A self-hosted runner is a Phase 6
  decision.
- **Eyes for agents.** Playwright screenshots of preview URLs for web. An Android emulator
  plus `adb` screencaps (Maestro flows later).
- **The Mac mini's screen is locked, and that's a given** (your call, 2026-09-12). Agents
  never get click-through automation of macOS apps. macOS UI is verified by headless tests
  (package tests, plus offscreen SwiftUI snapshot rendering if a spike shows it works while
  the screen is locked) and by your UAT. That's what phish-in-app already learned the hard way
  (D208).
- **Review by a different platform.** Before merge, a short review run on a *different*
  platform than the builder. Free tiers are preferred; Claude is used when an item touches
  data or migrations, or the free tiers are exhausted. This is where the BACKLOG's
  **adversarial cross-product review** plugs in (ROADMAP Phase 7). It's agent review, not a
  human gate.
- **Runtime signals later:** post-deploy error capture (Vercel logs, a tiny error endpoint,
  or Sentry's free tier) becomes issues automatically.

### D12 — Safety: checkpoints, rollback, data, backups

**Code is always reversible.**

- Every change is one squash-merge linked to its issue.
- **Undo** (console, or `/mahler undo`) opens a revert PR, lets CI run, merges and redeploys.
  It's autonomous like everything else.
- Each platform's rollback is recorded in `project.toml`:
  - Vercel: promote the previous deployment
  - Cloudflare: `wrangler rollback`
  - Android: the previous APK stays installable from GitHub Releases
  - macOS: the previous release via the Sparkle appcast or GitHub Release
- Unmerged `mahler/*` branches are never deleted for 14 days.

**Data is the part that isn't reversible, so it gets the hard rules:**

1. **Inventory.** At onboarding, each project's `[data]` section lists every persistent store
   with backup and restore commands. Examples: groundwork's Neon Postgres, phish-in-app's
   Cloudflare D1 sync DB, mental-jukebox's local SQLite and tokens, on-device Room databases.
2. **Nightly backups** run as Mahler jobs, not agents:
   - `pg_dump`, `wrangler d1 export`, `sqlite3 .backup`
   - written to `/Volumes/ExtSSD160/mahler-backups/`, which Backblaze already covers offsite
   - retention: 14 daily, 8 weekly, 12 monthly
   - platform-native point-in-time restore (Neon, D1 Time Travel) on top, with each retention
     window confirmed at onboarding
   - a **restore drill** runs monthly per store; an untested backup doesn't count
3. **Backup receipt before risky deploys.** If a PR touches migrations, schema or
   data-writing scripts, Mahler takes a fresh backup and proves it restores before it lets the
   deploy proceed. Mahler enforces this, not the agent's good intentions.
4. **Migrations** are forward-only and tested against a copy of real data. groundwork already
   has Neon preview branches; D1 gets a restored copy. On-device DB migrations need migration
   tests (phish-in-app's Room section already demands this).
5. **Guardrail hooks** are a safety net, not the plan:
   - the Claude `PreToolUse` denylist, and agy permission rules
   - blocked: destructive SQL against production connections, `wrangler d1 execute --remote`
     with DROP/DELETE/TRUNCATE, `rm -rf` outside the worktree, force-push to the default
     branch, `gh repo delete`, `gh release delete`, and touching `~/.ssh` or the keychain
6. **Secrets** come from the Keychain or untracked `.env` files. They are never put in prompts,
   issues, ntfy or logs.

**Blast-radius limits:**

- global concurrency 3, per project 2, per platform set by quota
- a 60-minute wall clock per run
- 3 attempts per item, then `needs-you`
- **Pause all** (console, `mahler pause`)
- per-project opt-in (as in dispatch)

**Machine backups** (updated 2026-09-12, after you re-included `scripts/` in Time Machine):

- **GitHub** holds everything committed and pushed. Runs push their branches continuously.
- **Time Machine** now includes `/Volumes/ExtSSD160/scripts`: fast local restore of files.
- **Backblaze** (continuous, offsite) covers the whole SSD, including gitignored files,
  local databases and non-git folders.
- **Hosted databases are covered by none of those.** That's the job of `mahler/backup.py`,
  built 2026-09-12:
  - a nightly `pg_dump --format=custom` per declared store, verified with
    `pg_restore --list`
  - kept 14 daily / 8 weekly / 12 monthly, in `/Volumes/ExtSSD160/mahler-backups/`
    (files 0600), which Time Machine and Backblaze then copy
  - credentials read from the project's env file at run time and passed as `PG*`
    environment variables, never on a command line or in a log
  - a failure pings you, then retries hourly
  - it's a Mahler job, not an agent, so agents never need production credentials to
    make backups happen
- `mahler.db` is copied nightly with `.backup` into the backups folder.

### D13 — Autonomy

- **Everything sorted is fair game** (your answer). The sorting run makes an item `ready`
  unless it hits a question only you can answer. Then the item goes to `needs-you` with that
  specific question, and you get a ping.
- **Default tier for every project: `autonomous`.** Merge on green verify + review, deploy or
  release to your devices, UAT afterwards. You confirmed no app has users besides you, so
  phish-in-app's `gated` setting (premised on "ships to real users") is lifted when it
  onboards. Its manual beta → production *promotion* goes too (D16), but its test
  *environment* stays.
- A `gated` tier remains available per project, since you said it "depends on the project."
- **What always pauses:**
  - product decisions only you can make
  - anything needing your credentials, payment or a new account
  - a destructive data operation beyond the guardrails
  - three failed attempts
- **Local apps** (e.g. mental-jukebox runs from its checkout): "deploy" means Mahler
  fast-forwards the primary checkout, but only when it's clean and has no lease. Otherwise it
  pings you.

### D14 — Stack and runtime

- **Hub:** the Mac mini. It's already always-on and hosts Remote Control and dispatch. The
  MacBook and the Pixel are clients only. No multi-machine scheduling.
- **Language:** Python 3.12+, like thread and dispatch. Dependencies are pinned with `uv`, since
  Mahler needs a web server and the MCP SDK, unlike the stdlib-only thread.
- **Components:**
  - web: FastAPI + server-rendered HTML (HTMX); minimal JavaScript, phone-friendly
  - database: SQLite, WAL
  - MCP: the official Python SDK (streamable HTTP on the tailnet, stdio shim for local clients)
  - GitHub: `gh` / REST, using your existing `gh` auth
  - notifications: HTTP POST to ntfy
- **Processes:** one launchd daemon, `com.mike.mahler`, with `KeepAlive` and an explicit `PATH`.
  Loops:
  - GitHub sync 60s · scheduler 30s · watchdog 15s
  - usage probes 5 min · thread sensor 10 min
  - backups nightly · digest daily

  One **runner** subprocess per run: it wraps the CLI, logs `stream-json`, heartbeats, enforces
  the time limit, and delivers yields.
- **Paths:**
  - code: `~/Mahler`
  - state: `~/.mahler/` (db, `runs/<id>/`, usage sidecars, logs)
  - worktrees: `/Volumes/ExtSSD160/.mahler-worktrees/<project>/<issue>-<run>` (same volume as
    the repos; excluded from Backblaze)
  - config: `~/.mahler/config.toml` (platforms, thresholds, caps) and per-repo
    `.mahler/project.toml` (verify, data, release), checked in so agents can read it
- **Recipes** (after Gas City's formulas): one versioned prompt template per role in
  `recipes/`: `sort`, `build`, `fix-ci`, `review`, `release`, `uat-author`. Each has the same
  checkpoint/handoff boilerplate.

### D16 — Environments: testing never touches your real data

Release gating and data isolation are separate questions. Mahler drops the first (no manual
promotion) but keeps the second wherever testing would pollute real use.

- Each project's `project.toml` declares `[environments]`: `prod` (your daily use) and, where
  needed, `staging`. Staging means separate backend resources, like a Worker environment, its
  own D1/Neon database or branch, and its own API keys. It also means a **side-by-side
  install** where the platform allows it: a different Android `applicationId` suffix and a
  different macOS bundle id, so the test app and the real app coexist on your devices.
- **Agents' automated tests and your UAT builds always run against staging.** Merges ship to
  prod automatically, with no promotion step.
- **Staging is seeded from the latest prod backup** (D12). So you test against realistic data
  without writing into the real thing, and every seed is also a free restore drill.
- **Couch Tour** (your call, 2026-09-12): a single release channel, with no beta → production
  promotion. But a staging sync backend and a separately-installed test app, so UAT listening
  never lands in your real listening history.
- Projects where testing can't pollute anything (a read-only script, a static page) just
  declare `prod`.

### D17 — Self-hosting: Mahler builds Mahler

Mahler's own repo is its first managed project (ROADMAP Phase B). A conductor that edits
itself needs a stable place to stand:

- The daemon runs from its own clone at `~/.mahler/app`, **never** from `~/Mahler` (where
  sessions and agents work). The clone is pinned to a **known-good** commit.
- A tiny, rarely-changing launcher script (`~/.mahler/bin/mahler-launcher`, installed by
  copying it, not run from the updating tree) does four things:
  1. runs the tick
  2. advances `~/.mahler/app` to the new `origin/main` only when CI on that commit is green,
     and the unit tests pass in the clone
  3. records the new known-good after a clean tick
  4. rolls back to the previous known-good, and pings you, after two consecutive crashed
     ticks
- Mahler's own repo runs with `max_parallel = 1`, so changes to the conductor land one at a
  time.
- The bootstrap is **Python standard library only**, like thread and dispatch. The web
  console and the MCP server (which need `uv`-managed dependencies) are later issues Mahler
  builds for itself.

### D18 — Agents build; the conductor ships

Decided 2026-09-12, after weak free models kept finishing the code and then dropping the tail
of the build recipe. Gemini skipped the PR and STATUS steps on groundwork#81 (run 17). Cline
on mahler#8 (run 23) pushed working commits, then ended on "Now opening the PR:".

- **A run does one job that needs judgment,** then ends:
  - **build:** implement, run `verify`, commit and push, then end with `STATUS: DONE <one-line
    summary>`. `NEEDS-YOU` and `BLOCKED` stay.
  - **fix:** CI failed on the PR; the run gets the failing log tail and starts from the branch.
  - **review:** D11's review by a different platform, when it lands.
- **Mahler does the mechanical steps in code** (Principle 7). It pushes the branch, then opens
  the PR with `Fixes #N`, the agent's summary and the issue's "Needs a human to check" list.
  It watches CI across ticks, checks the lease, squash-merges, deletes the branch and comments
  on the issue. These steps need no model, cost no tokens and can't be forgotten.
- **Not more agents.** A separate "PR agent" would be the same failure mode aimed at work
  that needs no judgment.
- **A missing STATUS line isn't the end of the story.** If the branch has commits ahead of
  base and the project's `verify` passes when Mahler runs it, the build counts as done. The PR
  goes up with a note that the agent didn't confirm it had finished; CI and review decide
  from there. Otherwise it's a failed attempt, as before.
- Red CI starts a `fix` run. `max_attempts` caps build and fix runs together, and escalation
  (D8) applies as before.
- Shorter recipes also mean fewer tokens on every run, and a smaller surface for the model to
  lose track of.

### D19 — A change is in flight until it merges

Decided 2026-09-12 (mahler#27). With `max_parallel = 1`, mahler#16, #20 and #8 still built
back to back with none of them merged, so each started on a `main` that was missing the
others. #8's run also resumed a branch saved 2½ hours earlier, 9 commits behind. Two of the
three had to be re-run. D6 had counted on "the later run rebases" at merge time, but D18 took
merging away from agents, and nobody took the rebase over.

Throughput counts merged changes, not finished runs. So:

- **The slot is held until merge.** A `verifying` item counts against its project's
  `max_parallel` for builds. Sorts don't write code, so they don't wait. A red PR keeps holding
  the slot until it's fixed (D18 fix runs) or closed, and pings once, because that project's
  builds stop behind it.
- **Every build starts on current base.** Resumed work is rebased onto `origin/<base>` at
  launch, and the run's branch is force-pushed to match. If the rebase doesn't apply cleanly,
  the old tip is kept on `mahler/snapshot/<n>-stale-run<id>`, the branch starts fresh from base,
  and the prompt points the agent at the old work. A branch left with nothing beyond base is
  deleted rather than pushed, because a PR head at base's tip reads as merged and would close
  the issue.
- **A PR that stops merging goes back.** If GitHub reports the PR `CONFLICTING`, the conductor
  returns the item to `ready`, with no attempt counted. If the rebuild's rebase applies, it
  updates the same PR. If not, dropping the branch closes that PR, and the conductor opens a
  new one.
- Snapshot diffstats are measured from the merge base, so a stale branch no longer looks like
  it deletes everything that landed after it.

### D20 — Maintenance passes are triggered by time and shipped volume

Each managed project may enable periodic reviews for security, code health, architecture drift,
test health, token/quota hygiene, and agent guidance. They default to a 30-day cadence and an
early trigger after 20 merged PRs since that pass was last filed, with a 14-day cooldown after
filing. A project can disable maintenance or select a subset of the six passes.

The ledger owns one checkpoint per project and pass: `last_filed_at` plus `merged_since`. Every
conductor-confirmed shipped PR increments `merged_since` for the project's enabled passes. A pass
is due immediately before its first checkpoint, then when either the cadence has elapsed or its
merged-PR threshold is reached. Filing the maintenance issue resets the checkpoint. Merged-PR
volume is the throughput signal, not raw agent-run count, so the trigger follows work that
actually reached the project.

### D15 — Deliberately not doing

- Not multi-user, and no scheduling across multiple machines.
- No code-review UI. You don't review code, and the verify contract and cross-platform review
  stand in.
- Not replacing GitHub's issue UI. The console is a simpler window onto it.
- No LLM calls in the control plane.
- No cloud execution and no public endpoints in the POC.

---

## Risks and unknowns (answered by Phase 0 spikes unless noted)

1. ~~agy on the free tier~~ **Answered 2026-09-12:** it works headless, with `--add-dir`
   required and quota readable for free (D8). Still open: exhaustion behaviour mid-run, and
   whether the Antigravity IDE shares the same pools. That matters only if you also use the
   IDE interactively.
2. ~~Claude usage during headless runs~~ **Answered 2026-09-12:** `rate_limit_event` in
   `stream-json`, plus a ~700-token lean probe (D8).
3. **Cline free-model quality and CLI control:** model selection, a completion signal, and
   whether hooks can deliver yields or a denylist. dispatch's open `backend_cline()`
   gated/auto question resolves here.
4. **Worktree build hazards per stack:** `pnpm install` time per worktree (use pnpm's shared
   store), and Xcode's symlink and package-path traps (canary check).
5. **Tailscale reachability from app pages:** can a page served from a `vercel.app` origin POST
   to the tailnet API? That needs CORS, and the phone must be on the tailnet. The fallback is
   the GitHub-issue URL.
6. **Free-tier churn:** Antigravity's free tier has been cut repeatedly. The router must degrade
   gracefully to "fewer runs", never to "paid runs".
7. **The Mac mini is a single point of failure.** If it's down, nothing runs, but nothing is
   lost either. Items live in GitHub, code is pushed, and data is backed up.
8. **macOS UI can't be automated.** The Mac mini stays locked, so macOS UI regressions are
   caught by headless tests and your UAT, not by agents clicking through (D11).
9. **A self-modifying conductor:** a bad merge to Mahler could stop the daemon that would fix
   it. The known-good launcher and rollback (D17) are the mitigation. The launcher itself is
   deliberately tiny, and it's updated only by hand.

## Glossary

- **Item:** a GitHub issue Mahler manages.
- **Lease:** Mahler's record of who holds an item right now.
- **Epoch:** a lease generation number used as a fencing token.
- **Run:** one agent process on one platform for one item.
- **Runner:** Mahler's supervisor process around a run.
- **Handoff:** checkpoint + note + release. It's how every run ends.
- **Recipe:** a per-role prompt template.
- **Verify contract:** a repo's declared checks and release steps.
- **Build:** a registered deploy or release carrying UAT items.
- **Hot hold:** a pause on new autonomous starts in a project while untracked activity is seen.
- **Reserve:** the share of Claude quota kept for your own chats.
