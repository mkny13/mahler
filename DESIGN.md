# Mahler — Design

Status: design agreed 2026-09-12; the system is built and running. This is the
decision record, so older sections sometimes describe a target that later decisions
amend. Shipped status lives in [ROADMAP.md](ROADMAP.md), and setup/operation lives in
[README.md](README.md).
Gustav Mahler was a conductor. This tool conducts: it decides who plays which part, and when.

---

## The one-page version

You describe what you want: goals, bugs you hit, features you'd like. You do it from any
chat (Claude, Cline, Antigravity, the Claude app on your phone), from Mahler's console, or by
tapping "this doesn't work" inside the app you're testing. Every one of those becomes a
**GitHub issue** in that app's repo. Issues are the single backlog, and nothing important
lives anywhere else.

Mahler runs on an always-on Mac. It watches those issues. A planning agent sorts each
new one: it writes down what "done" means, sizes it, and splits it if it's big. Once an item
is sorted, Mahler hands it to whichever configured platform has capacity. The agent works on
its own branch and worktree, implements the change, verifies it, commits, and pushes. The
deterministic conductor opens the PR, watches CI, confirms the merge, and records what you
should check (D18).

If a platform runs low on quota mid-task, the agent saves its work and leaves a handoff note on
the issue, and the next platform picks up exactly where it stopped. If *you* start working on
the same item in a chat, your session wins: the agent steps aside and hands you its work.

When a change has something only a human can verify, it appears in the console's Ready to test
queue. You can pass it or fail it with a note or screenshot; a failure becomes a p1 bug. The
console can also request a CI-gated revert PR. Platform rollback, in-app UAT panels, and the
full pre-migration backup guarantees below remain planned rather than universally enforced.

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
| `thread` | `~/ai-tools/thread.py` | Legacy anomaly scanner (dead sessions, stray branches, WIP) | **Retired with `ai-tools`.** A future Mahler-native sensor may preserve its useful scan behaviour, but the legacy scanner must not watch Mahler refs or worktrees (D2) |
| `dispatch` | `~/ai-tools/dispatch.py`, launchd `com.mike.dispatch` | Legacy anomaly-driven agent launcher; claim/cooldown store, caps | **Retired 2026-09-16.** The owner unloaded its launchd timer; Mahler is the sole autonomous dispatcher |
| ThreadBar | `~/ai-tools/ThreadBar` | Legacy menu-bar view of thread/dispatch | **Retired 2026-09-16.** It will not be repointed at Mahler |
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
 You — Mac or phone, optionally over Tailscale
   chats/CLIs: Claude · Antigravity · Cline · Copilot · Codex · Kilo
   Mahler console (web) · ntfy pings · GitHub
        │                         ▲
        ▼                         │
┌──────────────────────────── Mac mini ─────────────────────────────┐
│  launchd tick (60s) + optional console service                    │
│   ├─ GitHub sync ◀───────────▶ GitHub: Issues · PRs · Actions ·   │
│   │                             Releases  (item content lives here)│
│   ├─ ledger (SQLite): leases · runs · usage · builds · events     │
│   ├─ scheduler + router (quota-aware, deterministic)              │
│   ├─ watchdog: heartbeats · yield · reap · snapshot               │
│   ├─ sensor: session presence                                     │
│   ├─ console (HTML/JSON) · local stdio MCP                        │
│   └─ jobs: backups · notifications · usage probes                 │
│            │ launches, supervises                                 │
│            ▼                                                      │
│   runner ─▶ configured agent CLI (one worktree per run)           │
└───────────────────────────────────────────────────────────────────┘
```

Item **content** (what to do, discussion, screenshots, handoff notes) lives in GitHub.
Execution **state** (who holds what right now, runs, quota, builds) lives in Mahler's ledger.
Neither duplicates the other's truth. Mahler-authored state labels are a display; deliberate
human edits to a state label are accepted as instructions, and `platform:*` labels are the
durable store for explicit pins.

---

## Decisions

### D1 — Scope: one system, top to bottom

Mahler covers intake → planning → routing → execution → verification → release → UAT →
rollback → backups, for every project you opt in. Some of this goes beyond "orchestration." It's
included because each missing piece is a place where a thread gets lost, or where an agent
can't see its own results.

### D2 — Retire dispatch; absorb thread's behaviour, not its runtime

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
- **The useful behaviour of `thread` becomes a Mahler-native, read-only sensor.** When that
  sensor is built, every actionable flag becomes an issue labelled `type:anomaly`, with one
  open issue per fingerprint. Anomalies then flow through the same queue, leases and routing
  as planned work: one queue, one lock system. Preserve the proven calibration where it still
  applies (the squash-merge `git cherry` check, default-branch exclusion, and
  `died_mid_task` requiring TASKS.md), but do not import or run the legacy `ai-tools` scanner.
- **Mahler is the sole autonomous dispatch authority for a managed project.** No external
  scheduler, scanner, menu-bar helper, or agent launcher may treat `origin/mahler/*` as an
  anomaly, inspect a configured Mahler worktree root (including `.mahler-worktrees`) as an
  ordinary checkout, or launch remediation against either. A read-only repository health
  tool must ignore those refs and paths and must never trigger an agent. This is a safety
  invariant, not a scanner calibration preference: a second dispatcher has a separate claim
  store and can spend quota or rewrite work without Mahler's lease fence.
- **Legacy cutover is complete.** On 2026-09-16 the owner unloaded
  `com.mike.dispatch`; `dispatch.py`, `thread.py`, ThreadBar, and their `ai-tools` state are
  retired rather than operated beside Mahler. Cline Kanban is retired for the same one-queue
  reason. Any future onboarding confirms that no external dispatcher targets the project;
  it does not add the project to `dispatch.toml` temporarily.

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
  body, which Mahler parses (including blockquotes). Bare `#12` means the same project;
  `owner/repo#12` names that exact repository. `repo#12` is shorthand only when exactly
  one enabled project has that repository basename. Qualified references retain their
  repository in the existing JSON dependency list and hold diagnostics. Builds wait until
  every target is `done` in the local ledger. Unknown, disabled, unmanaged or ambiguous
  repositories remain blocked; the scheduler never falls back to a local issue number or
  queries GitHub to resolve them. Sync drops references to the item itself or its known
  parent/ancestors, logging each correction once: a parent cannot finish until its children
  finish (D21). GitHub's native "blocked by" relationships feed the same dependency list;
  body and native references are deduplicated. Ancestry walks are cycle-safe and capped at
  100 hops; unproven dependencies remain enforced.
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
  started, branch `mahler/12-dark-mode`"). Mahler distinguishes its own last mirror from a
  deliberate human label edit; the latter is an instruction (D4).

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
  sharing an `area:` label aren't run concurrently — `tick.schedule()` gates `build`/`fix`
  starts on area collision against both running and in-flight (`verifying`) items
  (mahler#197). Per-project `max_parallel` defaults to 2.
- **Planned-file collision is mechanical, not label-based** (mahler#210). `area:` adoption was
  0%: the sort recipe's instruction to hand-label overlap had no procedure behind it — nothing
  told the sort agent to look at *other* open issues at all. Rather than ask an LLM to predict a
  collision in advance, `sync()` parses each issue's `## Plan` → `Files:` list into the item's
  `files` column on every tick (`gh.files_of`), and `tick.schedule()` blocks a `build`/`fix`
  start whenever its planned files intersect `busy_files` — seeded from running and `verifying`
  items exactly like `busy_areas`. `area:` labels stay as a manual override for overlap the file
  list can't see (two issues touching the same runtime behavior through different files), not
  the primary mechanism.
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
| **Antigravity: Claude/GPT pool** (free) | `agy -p … --add-dir <worktree> --model claude-opus-4-6-thinking --dangerously-skip-permissions --output-format stream-json` | `agy -p /usage --output-format json`, which costs nothing and reports `remaining_fraction` + `reset_time` per pool and window | **First-choice builder**. Verified 2026-09-12 (Antigravity test results, below). |
| **Antigravity: Gemini pool** (free) | same, `--model gemini-3.1-pro-high` or `gemini-3.8-flash-high` | same probe, separate pool | Second-choice builder. Verified 2026-09-12 (Antigravity test results, below). |
| **Cline** (free models) | `cline --cwd <worktree> --json --auto-approve true -t <secs> <prompt>` | None: its JSON reports `totalCost: 0` and no quota, so it's routed as **unmetered** and backed off after any rate-limit error. The model is GLM-5.3-flash (`z-ai/glm-5.3-flash` in every run log). It has a **daily free cap**: a 429 `INFERENCE_CAP_ERROR` that says when to "try again", and Mahler waits until then (2026-09-13; before that it retried hourly) | Builder of any size, second in build order after Antigravity's Claude pool (you judge its free GLM-5.3-flash on par with Sonnet 4.x; the quota is generous but unstated). Verified 2026-09-12 (S3). Daemon-launched runs need macOS Documents access (see below) |
| **Copilot CLI** (`@github/copilot`, GitHub Education license) | `copilot -p <prompt> -C <worktree> --allow-all-tools --output-format json` | Unlike Cline/Kilo, has a real cap: GitHub bills Copilot in **AI Credits** (mahler#38), Pro/Education include 1500/month. No cheap CLI-level probe, but `gh api /users/<login>/settings/billing/ai_credit/usage` (needs the `user` OAuth scope) reports the month's consumption, so it's routed as a normal **metered** platform with a single `monthly` window instead of 5h/weekly | Builder, size `s` only, ahead of Kilo — it runs real frontier models (verified: `claude-sonnet-5`), despite the smaller monthly allowance. CLI flags verified end-to-end 2026-09-13 (mahler#25); the AI-credits billing probe verified 2026-09-12 (mahler#38). Escalation sibling **`copilot-high`** (mahler#192): same CLI, same account and monthly AI-credits cap (`quota_group: copilot`, one run slot), model `gpt-5.3-codex` (live-verified 2026-09-14 with the `--model` fast-fail check), tier 3 and gated `min_size: "l"`, so it only enters via D8 rule-4 escalation on size:l items; in the default build route right after `copilot` |
| **Codex CLI** (ChatGPT account) | `codex exec --ephemeral --dangerously-bypass-approvals-and-sandbox --color never --json -C <worktree> <prompt>` | Metered through the zero-token app-server `account/rateLimits/read` API (mahler#276), once per account/quota group every 15 minutes. Default soft/hard lines are 70/90%. Exact 300/10080-minute windows map to 5h/weekly; unfamiliar windows remain visible with their actual duration and cannot establish headroom. An explicit account block is hard even with low percentages. Reset credit count and expiries are informational only; Mahler never consumes them, though quota refresh sends a deduplicated high-priority ntfy alert if Codex hits 100% quota or is blocked (prompting to reset in ChatGPT if reset credits exist), cleared on recovery (mahler#349). | Opt-in for plan, sort, or build roles; absent from default routes so Mahler never spends the ChatGPT account implicitly. The unattended flag removes Codex's sandbox, so Mahler relies on the same isolated worktree, lease environment, and fenced git hooks used for every runner. Verified end-to-end 2026-09-13 (mahler#157). Escalation sibling **`codex-high`** (mahler#192): same CLI, same ChatGPT account/quota (`quota_group: codex`, one run slot), model `gpt-5.6-sol` (verified 2026-09-14 from the installed codex-cli 0.154.0's embedded catalog), tier 3 and gated `min_size: "l"`; like `codex` it is deliberately absent from the default routes — a project opting into `codex` adds `codex-high` alongside it in its own `routing.build` override for the size:l escalation tier |
| **Kilo** (`@kilocode/cli`, kilo.ai account, model `kilo/kilo-auto/free`) | `kilo run <prompt> --dir <worktree> --auto --format json -m kilo/kilo-auto/free` | None: usage is per-account credits with no cheap probe, so it's **unmetered** like Cline | Builder, size `s` only, last among the free tiers — `kilo-auto` draws from a grab-bag of smaller/niche `:free` models of unverified quality. Needs `kilo auth login` (a one-time browser flow only the account owner can do), **and** `"small_model": "kilo/kilo-auto/free"` set in `~/.config/kilo/kilo.jsonc` — Kilo's background tasks (session titling, context-window summarization) read `small_model`, not the `-m` flag, so without it they fall through to a paid default model and fail on a $0 balance. The default (non-`:free`) model 402s immediately ("Add credits to continue") — no "quota" in the text, so `QUOTA_WORDS` covers "credit" and `usage_limit_exceeded` too. Verified end-to-end 2026-09-13 (mahler#29) |
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
   over the reserve, sorting falls back to Antigravity rather than waiting. Planning goals,
   audits and `size:l` items is the exception: it waits for Opus (D21).
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
| Cline-free | — | on the first quota or rate-limit error; its free model has a daily cap, and Mahler waits until the reset time the error names |
| Claude, autonomous | 5 h 60% / 7 d 70% | 5 h 70% / 7 d 80% |
| Claude Opus | 5 h 45% / 7 d 70% (D21) | same account and hard lines as Claude |

**Progressive weekly pacing** (mahler#283, 2026-09-15) is opt-in per platform:
`progressive = ["weekly"]` scales both weekly targets by `day_number / 7`, where
`day_number = min(7, max(1, ceil(7 - seconds_until_reset / 86400)))`.
A 40% soft target allows 5.71% on day 1, 11.43% on day 2, and 40% on day 7.
Other windows and platforms without this setting retain their static lines.
Unknown or malformed reset times use day one's allowance; stale samples and expired
resets still block starts. A weekly burst overrides pacing; a session burst lifts
only the 5h lines, preserving the paced weekly reserve. Routing, running-run yields,
and console gauges use the same effective thresholds.

Platforms without a usage percentage are treated as 100% on the first quota error, and stay
unavailable until the reset time, parsed or with a backoff default. All of these numbers live in
`~/.mahler/config.toml`.

**Tier-weighted concurrency budgets** (mahler#200). `concurrency.total` (`tick.schedule()`) is
one flat global ceiling: an expensive Claude Opus run and three cheap Cline/Kilo runs compete
for the exact same pool of slots. `concurrency.by_tier` (optional) layers a second, finer cap
underneath it, reusing the `tier` field every platform already carries for D8 rule-4 escalation
(`router.tier_of`): tier 1 = cline-free, kilo; tier 2 = agy-claude, codex, copilot; tier 3 =
agy-gemini, claude, codex-high, copilot-high; tier 4 = claude-opus. `tier` is the closest
existing signal to "how scarce/strong is this platform" — not a perfect proxy for cost
(`agy-claude`/`agy-gemini` are tier 2/3 but free; `copilot-high` is tier 3 but spends real AI
credits) — refining that further is its own project, not worth blocking this on. An entry
`by_tier = { 1 = 3, 2 = 2 }` caps tier 1 (and above) at 3 concurrent runs, and tier 2 (and above)
at 2; tiers 3 and 4 stay unrestricted, still bounded by `total`. **Budgets are "at or above,"
not "exactly":** a tier-4 run also counts against a tier-"2 and up" budget, so a scarce platform
can't dodge a laxer cap by being even scarcer. `total` stays the hard outer ceiling regardless —
`by_tier` only ever restricts further, never loosens it. Absent/empty `by_tier` reproduces
today's exact behavior (every tier bounded only by `total`), so it's opt-in and backward
compatible. This is a different mechanism from the per-platform `max_runs` (one specific
platform's own slot count, e.g. shared quota) — both apply at once, neither replaces the other.

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
  The designed console and how it's built: D27.
- **MCP server** (`mahler`), registered with Claude Code, Cline and Antigravity. The shipped
  stdio server provides `list_items`, `add_item`, `claim`, `heartbeat`, `release`, `handoff`,
  and `next_id`; `ask_user`, `report_progress`, and `get_context` remain planned. This is what
  makes a chat an input: in any
  chat, on any platform, including the Claude app on your phone through Remote Control, you can
  say "log this as a bug in groundwork" or "start on #12 here." It's also how every platform
  sees the same queue.
- **`/mahler` skill** for Claude Code, plus a short Mahler section in each repo's
  `CLAUDE.md`/`AGENTS.md` (read by Claude, Cline and agy), with the claim/heartbeat/handoff rules.
- **ntfy** pings: *needs you* · *ready to test* · *handoff happened* · *failed and parked* ·
  *daily digest* · *Codex quota exhausted* (high-priority on quota probe, cleared on recovery).
  Messages carry a title and a console link only, never secrets or personal
  data. The POC uses ntfy.sh with an unguessable topic; self-hosting on the Mac mini is an
  option later.
- **GitHub comments are commands**, which is handy from the GitHub app:
  - `/mahler go` · `/mahler park` · `/mahler platform <name|auto>`
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
- **Undo** in the console opens a revert PR and sends it through normal CI-gated conductor
  shipping. A GitHub comment command and platform-specific rollback/redeploy remain planned.
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
   - **per-platform launch-flag deny coverage** (mahler#77): `platforms.DENY_STEMS` (force
     pushes and ref deletes, `--mirror`, history rewrites, `git reset --hard`,
     `gh repo delete`/`archive`, `gh release delete`/`issue delete`, `rm -rf/-fr /` and `~`)
     is rendered per CLI — Claude gets `--disallowedTools` (`CLAUDE_DENY`), Copilot gets
     `--deny-tool` (`COPILOT_DENY`; denials beat `--allow-all-tools` per `copilot help
     permissions`). **Known, accepted gaps — no deny-list flag exists:** agy, cline,
     codex, kilo (comments at each argv builder; codex's execpolicy `.rules` is the
     possible future mechanism). All deny rules are prefix stems: they catch accidents,
     not a determined agent (`git push origin :branch`, reordered flags) — branch
     protection and the lease pre-push hook remain the real fence.
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

- **Hub:** an always-on Mac. The canonical installation is the Mac mini; D24 adds remote lease
  coordination for an explicitly shared project without replicating the scheduler database.
- **Language:** Python 3.12+, like thread and dispatch. Dependencies would be pinned with `uv`;
  so far none are needed (the console is standard library, D27), and the MCP SDK is the
  likely first.
- **Components:**
  - web: server-rendered HTML with minimal JavaScript, phone-friendly — standard
    library, not the FastAPI + HTMX first planned here (D27)
  - database: SQLite, WAL
  - MCP: a minimal standard-library JSON-RPC server over stdio; an SDK/HTTP transport is future work
  - GitHub: `gh` / REST, using your existing `gh` auth
  - notifications: HTTP POST to ntfy
- **Processes:** a launchd job invokes one exception-safe tick every 60 seconds. Sync,
  watchdog, finalization, shipping, maintenance, quota work, and scheduling are ordered passes
  within that tick. The optional console runs as a separate `launchd` service. Backups,
  cleanup, and the digest are cadence-gated from ticks.

  One **runner** subprocess per run: it wraps the CLI, logs `stream-json`, heartbeats, enforces
  the time limit, and delivers yields.
- **Paths:**
  - code: `~/Mahler`
  - state: `~/.mahler/` (db, `runs/<id>/`, usage sidecars, logs)
  - worktrees: `/Volumes/ExtSSD160/.mahler-worktrees/<project>/<issue>-<run>` (same volume as
    the repos; excluded from Backblaze)
  - config: `~/.mahler/config.toml` (platforms, thresholds, caps) and per-repo
    `.mahler/project.toml` (verify, data, release), checked in so agents can read it
- **Recipes** (after Gas City's formulas): the shipped agent roles are `sort`, `build`, and
  `fix`, plus the manually triggered `console_walkthrough`. Review, release, and UAT-author
  roles remain planned.

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
- The bootstrap is **Python standard library only**, like thread and dispatch. The MCP
  server may bring the first `uv`-managed dependency; the console doesn't need one (D27).

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
- **Freshness before a new merge request** (mahler#239): re-read the PR's exact head,
  actual target and checks, then fetch the head and the current target tip in the configured
  checkout with the project's credentials, without switching branches or working files.
  Require `git merge-base --is-ancestor <base-tip> <checked-head>` to succeed. This is a
  conservative proof that the inspected head contains current base, not an inference about
  historical CI from today's `baseRefOid`, synthetic merge ref, or a first-seen KV value.
  Even successful synthetic-merge checks trigger a rebuild if the head lacks current base.
  No configured checks remains acceptable, but still requires ancestry; it isn't green CI.
- Proven stale ancestry uses the same rebuild path as a textual conflict: clear the PR link,
  keep saved work and attempts, return to ready and release the conductor lease. The later
  builder replays the work onto current configured base; the guard itself doesn't rebase or
  repush. Changed head/target/check observations wait for the next tick's state machine.
  Missing metadata, fetch/API errors, shallow history and indeterminate ancestry wait with
  an actionable reason and the existing verification timeout, without consuming attempts.
- Re-check the lease after network work and pin each merge request with
  [`--match-head-commit`](https://cli.github.com/manual/gh_pr_merge) to the inspected SHA.
  **The base check and merge are not atomic.** An independent writer can advance the base
  after the final fetch; this narrows the window and detects visible drift, but cannot offer
  a server-side merge queue's atomic guarantee. Interactive sessions follow the same rule
  in CLAUDE.md. D28's `queue:` request deduplication, timeout and confirmed-merge polling
  remain separate; already requested heads don't enter the freshness rebuild path.
- Snapshot diffstats are measured from the merge base, so a stale branch no longer looks like
  it deletes everything that landed after it.

### D20 — Maintenance passes are triggered by time and shipped volume

Each managed project may enable periodic reviews for security, code health, architecture drift,
test health, token/quota hygiene, agent guidance, issue backlog pruning, correctness bugs, and
documentation accuracy/onboarding (`mahler/tick.py`'s `MAINTENANCE_TEXT`; the first six were the
original set). They default to a 30-day cadence and an early trigger after 20 merged PRs since
that pass was last filed, with a 14-day cooldown after filing. A project can disable maintenance
or select a subset of the nine passes. The documentation pass compares README, commands,
examples, design/roadmap status, and operator/agent guidance with current code and CLI help,
then updates them through the normal issue → agent → conductor pipeline.

The ledger owns one checkpoint per project and pass: `last_filed_at` plus `merged_since`. Every
conductor-confirmed shipped PR increments `merged_since` for the project's enabled passes. A pass
is due immediately before its first checkpoint, then when either the cadence has elapsed or its
merged-PR threshold is reached. Filing the maintenance issue resets the checkpoint. Merged-PR
volume is the throughput signal, not raw agent-run count, so the trigger follows work that
actually reached the project.

What the first night taught (2026-09-13): all six passes were due at once, so every project
filed six `size:l` audits in one tick. On `scope = "label"` projects they were filed without the
scope label, so Mahler never saw them (groundwork #104–#109), yet their checkpoints were reset. A
split pass stays a `parent` forever, so it never becomes due again. So: pass issues carry the
project's scope label; a project has **at most one pass in flight** (a new one is filed only when
no `pass:*` item of that project is open); and a goal closes once all its sub-issues are done
(D21), which is what lets a pass recur.

**Platform tier/capability assumptions get the same treatment, on their own track**
(mahler#206). Time estimates already self-calibrate (`ledger.calibrate_estimates()`, mahler#59),
but `config.py`'s per-platform `tier`/`max_size`/`min_size` are point-in-time judgment calls, and
nothing re-checks them as a provider's model quietly changes under the same CLI/account. This
isn't an entry in the nine passes above — it's not about a managed project's codebase at
all, it's about Mahler's own config — so it lives in `platform_audit.py` and anchors its
checkpoint on one configured project's (default: `mahler`, since Mahler manages itself) merged-PR
throughput instead of every project's. It reuses the exact same checkpoint shape
(`last_filed_at`/`merged_since`, `Ledger.maintenance_due`) and the same at-most-one-pass-in-flight
discipline (a `pass:platform-audit` label sorts into the same `pass:*` check), so it can't crowd
the queue independently of the other nine. Each tick it: greps `DESIGN.md` for a "verified
<date>" mention near each platform's name and flags any older than `stale_verified_days` (default
90) or missing entirely; and cross-checks `runs`/`events` for each platform's done-rate,
needs-you-rate, and how often the *item* it was working escalated a tier away from it
(`ship._red_ci`/`finalize.retry_or_fail` now record the platform in the `escalated` event's
detail for exactly this). It only files the finding as an issue — never changes `config.py`
itself, since re-ranking tiers is a judgment call, not a mechanical recalibration like time
estimates. Done-rate comparisons require overlapping size gates and at least
`inversion_min_runs` observed runs on each side (default 10). This avoids comparing
escalation-only siblings against platforms that never receive the same item sizes.

### D21 — Opus plans; the free tiers build what it planned

Decided 2026-09-13. The research agrees on the split and on its limits:

- **Planner/executor works when the plan is concrete.** Aider's architect/editor pairs set its
  benchmark records at a fraction of the cost, and Claude Code ships the same idea as `opusplan`.
  A weak planner hurts results more than a weak executor (PEAR, 2026).
- **Success falls with task length**, and sooner for weaker models (METR's time horizons). So
  pieces should fit the builder, not be as small as possible.
- **Splitting has a cost.** Pieces lose each other's context, and coordination multiplies
  tokens. In Mahler every piece also pays a sort, a worktree and setup, the agent reading the
  repo, a PR, CI and a merge, one after another (D19).

What the ledger showed: 99 sort runs against 51 builds, because every sub-issue was sorted
again, and some were split again. The first audits, meant for Opus, were planned by Gemini:
Opus was reached only through the size limits, and a config routing list left it out. Opus had
never run.

- **Planning is a route, not a side effect.** Goals (`type:goal`), maintenance passes (`pass:*`)
  and `size:l` items are sorted only by `routing.plan` (default: `claude-opus`). With no headroom
  (the weekly reserve, or the peak window, D22) they wait; they never fall back to a free tier.
  `max_size`/`min_size` are builder limits and don't apply to sorting.
- **The plan is the product.** A planning run writes into each sub-issue a `## Plan` (the files,
  the ordered steps, the test that proves it), a `## Done when`, and a size of `s` or `m`.
- **Planned sub-issues are born ready.** A new issue with `Part of #P`, where P is a `parent`,
  with a `## Plan`, a `## Done when` and a size of `s` or `m`, skips sorting. Sub-issues are never
  split again.
- **The smallest useful piece is one mergeable PR with its own test.** Don't split below it.
- **Opus builds only by escalation** (D8 rule 4, now built): two failed attempts on a tier move
  the item up a tier. Each platform has a `tier`: Cline and Kilo 1, agy-claude and Copilot 2,
  agy-gemini and Claude 3, Claude Opus 4. Fix runs never route to Opus by size alone.
- **Haiku is not a builder.** It draws on the same Claude windows as Opus and Sonnet. The free
  tiers are Mahler's small models.
- **One Claude account, one run slot.** `claude` and `claude-opus` share the same 5-hour and
  weekly windows (there is no separate Opus window on Pro, and `--model opus` runs
  `claude-opus-5` inside the plan, not on overage; checked 2026-09-13). So they count together
  against `max_runs`, and an Opus run starts only below 5h 45%, leaving room to finish under the
  70% hard line. A planning run stopped halfway starts over, so this matters.
- **A goal closes when all its sub-issues are done.**

### D22 — Claude's peak window

Decided 2026-09-13 (your call). On weekdays from 5 to 11am Pacific (8am–2pm Eastern), Mahler
starts no Claude runs (`claude`, `claude-opus`). Running work continues, and the hard lines still
apply. To override: `mahler peak --off [--for 2h]`, or pin an item to a Claude platform. The window
lives in `[claude_peak]` in the config, and can be switched off.

Why: from March 2026 Anthropic cut 5-hour limits in that window, and the Claude Usage menu-bar
app marks it with a flame. Reporting says the cut was lifted for Claude Code on Pro and Max on
2026-05-06, so the window may no longer cost more per token for Mahler's runs. It stays as a
headroom rule, because it's when you're most likely using Claude yourself. Revisit if the
evidence settles.

### D23 — Use it or lose it: burst before a Claude window resets

Decided 2026-09-13 (your call). The reserve (D8) keeps Claude quota back for you. In the last
hours before a window resets, whatever is still in reserve expires unused. On 2026-09-13 the
weekly window stood at 81%, over the 70% soft line, so Claude sat idle until the reset on
2026-09-15 at 5am, while a queue of Opus planning waited.

- **The weekly burst.** In the last `weekly_lead_hours` (5) before the weekly window resets,
  every Claude line (weekly *and* 5-hour) rises to the burst lines: soft 90%, hard 97%. That
  covers midnight to 5am before the 5am reset. The 5-hour lines rise too, or a single session
  would cap the burst well short of the weekly remainder.
- **The session burst.** In the last `session_lead_minutes` (60) before the 5-hour window
  resets, the 5-hour lines rise to the burst lines, and the weekly lines don't. So leftover
  session quota turns into work, and the weekly reserve stays yours.
- **In a burst, Claude goes first.** Claude platforms move ahead of the free tiers in
  `routing.build`, because their quota is the one expiring. Items waiting for Opus planning
  (D21) are served too, since the burst lines apply to `claude-opus` as well.
- **Never while you're using Claude.** No burst starts runs while you're active: an
  interactive Claude Code transcript outside Mahler's worktrees written in the last 20 minutes,
  or 5-hour usage that rose while no Mahler Claude run was live (that catches the Claude app on
  your phone or the web). The peak window (D22) still wins.
- **Never into paid usage.** Burst lines stop below 100%. As a backstop, any run whose
  `rate_limit_event` reports `isUsingOverage: true` is stopped at once and Claude is marked
  exhausted until the reset. This applies at all times, not just in a burst.
- The burst needs a fresh usage sample with a known reset time. Unknown usage still counts as
  over the line (D8).
- `[burst]` in the config holds the lead times and lines, and `enabled = false` turns it off.

### D24 — One canonical lease table for a project shared across machines

Decided 2026-09-13 (mahler#151). D14's single-machine scheduler remains the default, but the
`mahler` project is deliberately worked by two independent Mahler installations: the Mac mini
and a work laptop. Two local SQLite files cannot coordinate D6's compare-and-set leases, and
GitHub labels cannot replace them. The Mini therefore remains the sole lease authority for this
one project; all other execution state and every other project remain local to each machine.

- A project may set `[projects.<name>.remote_ledger]` with an SSH `host`. Only `lease`, `claim`,
  `heartbeat`, `release`, and `lease_check` are relayed. Items, runs, quota, events, backups, and
  every unconfigured project's leases continue to use the caller's local SQLite ledger.
- The transport is one JSON request on stdin and one JSON response on stdout to
  `mahler ledger-remote-op`. The SSH command is static; it may be one executable string or a
  validated argv list when an explicit Python interpreter is needed. Each token excludes shell
  syntax and option-like arguments, and item and holder values never enter the command. It uses
  `BatchMode`, normal SSH host-key checking, a short connect timeout, and
  the laptop's existing key. The endpoint accepts only the five named operations for projects
  enabled in the Mini's own config. SSH authentication and the Mini's OS account remain the
  security boundary; no credential or private config is copied between machines.
- Remote holder ids are prefixed with a stable `client_id`. Local run numbers and the literal
  `conductor` are otherwise only machine-local and could accidentally renew each other's lease.
  A remote run id is never written into the Mini's `runs` table: ids in that table belong to the
  Mini. If the Mini pre-empts a laptop run, the laptop learns by its next failed heartbeat and
  stops safely.
- `max_parallel` is enforced canonically too. A capacity-taking claim counts live capacity
  leases for the project and compares the count in the same `BEGIN IMMEDIATE` transaction that
  grants the item lease. Sorting does not take capacity (D19). A completed build atomically
  hands its item lease from the run to the conductor, and a red-CI repair hands it back, so the
  slot remains occupied through PR, CI, and merge with no cross-machine release/claim race.
- **Failure is closed and project-scoped.** Timeout, SSH failure, a bad host key, non-zero exit,
  malformed JSON, or a rejected protocol response can never grant, renew, release, or validate
  a lease. A candidate is skipped, a running job loses its heartbeat and yields, a fence check
  blocks push/merge, and shipping waits for a later tick. The exception does not escape and
  break other projects' ticks. A stale remote lease expires normally on the Mini.
- This is coordination, not distributed scheduling or database replication. The Mini remains
  a single point of availability for this project's work, which is the safe behavior: if it is
  asleep or unreachable, the laptop leaves `mahler` alone while continuing its local projects.

### D25 — Accounts: work logins on the same machine, never crossed

Decided 2026-09-13 (mahler#163). The Mini is the machine that is always on, so work repos run
there too, on separate work logins for Claude Code, Copilot and Codex. The laptop is only a way
to reach the Mini. D24 stays available, but nothing needs it while one machine runs everything.
The rule that matters is a licensing one. A work project must never spend a personal login, and
a personal project must never spend a work login.

- **An account is a set of logins, selected by environment.** `[accounts.<name>]` sets `env`,
  for example `CLAUDE_CONFIG_DIR`, `COPILOT_HOME`, `CODEX_HOME` and optionally `GH_CONFIG_DIR`.
  Each CLI then reads a separate login from its own directory. This machine's own logins are the
  implicit `personal` account, which needs no entry.
- **Platforms and projects each name their account** (`account = "work"`, default `personal`).
  `from = "<base>"` lets a work platform inherit a base platform's lines and limits, so
  `claude-work` is `claude` on another login. A platform on another account gets its own
  `quota_group` (`claude@work`). Its run slot (D21) and its quota readings are therefore its own.
- **Routing is per account, and it fails closed.** A non-personal account routes only by its
  own `[accounts.<name>.routing]`, and it gets nothing if that is missing. The router offers a
  project only platforms on the project's own account. This holds for pins too: a pin to
  another account's platform is refused, with a reason. `runner.launch` checks the same thing
  again before it creates a worktree.
- **The environment carries the boundary.** A run on another account starts from the daemon's
  environment with every login-carrying variable removed (`CREDENTIAL_VARS`: API keys,
  `GH_TOKEN`, the CLI home variables and so on). The account's `env` is then added. The
  conductor's own `gh` calls for that project (sync, PRs, merges, comments) use the same
  environment. Plain `git` fetches and pushes in a work checkout take their GitHub login from
  that checkout's own credential config, set up once per repo, so no code path has to remember.
  An undefined account raises an error. Mahler never falls back to the personal login.
- **Quota is sensed per login.** Claude usage readings are recorded only onto the platforms in
  the reading's quota group. A work login's zero-token reading comes from the credentials file
  or keychain entry its account names. Otherwise Mahler uses the lean probe (D8), run with the
  work environment. The Antigravity probe, and a Copilot probe with no GitHub login of the
  account's own, would read the personal logins, so they never feed another account's
  platforms. Work Copilot is therefore configured unmetered (backoff on a limit error), unless
  the account has its own `gh` login. An empty personal AI-credits report does not
  measure org-assigned Business/Enterprise usage (mahler#265): it yields an unmetered
  "no quota signal" state, shared only within that login's quota group and retried
  after the normal stale interval. This is a missing-meter heuristic, not seat-type
  identification; a personal plan with no report rows also has no signal until rows
  appear. API failures remain unavailable, not evidence of an unmetered seat.
  `metered = false` remains the manual override and disables probing. No org roster
  or colleagues' usage is queried.
- **Bursts are per quota group** (amended 2026-09-15, mahler#283). D23 applies to work
  Claude logins too, using their own fresh samples and reset times. Personal reset times
  never lift work lines, or vice versa. Each bursting group's platforms move forward in
  their own account's build route. A usage-rise human flag suppresses only its quota group;
  detected human Claude transcript activity suppresses all groups. The peak window (D22)
  still applies to every Claude login.
- Concurrency: `concurrency.total` stays one global cap. Each login's slot is separate, so a
  second account usually wants the total raised by one.

### D26 — Accounts: a project may span more than one account

Decided 2026-09-13. Mahler-the-project is worked by Mahler-the-daemon, and Mike wants it built
with both his personal and his work Claude logins — work projects will depend on it too, so it
isn't purely personal or purely work. That's a deliberate, named exception to D25's "never
crossed" rule, declared per project in `~/.mahler/config.toml`, not a loophole or a fallback.

- A project names `accounts = [...]` instead of the singular `account` when it may spend more
  than one. `account = "x"` stays shorthand for `accounts = ["x"]`; a project sets one or the
  other, never both. Compute routing and quota still work exactly as D25 describes *per
  account*.
- **`account_mode` says how those accounts are tried.** Default (`"order"`, the flag absent):
  a multi-account project tries each of its accounts in turn (personal first, since that's the
  pool D23's burst favors), and spends the first with headroom — a fallback chain, not a merged
  pool. Updated 2026-09-14 (mahler#209): that default under-serves a project that's genuinely
  dual-use rather than personal-with-a-work-fallback — mahler's own personal build list has
  ten platforms, so `work` was essentially never tried even with `codex-work`/`copilot-work`
  idle and fully quota'd. `account_mode = "equal"` opts a project into round-robin merging
  each account's candidate list instead (first candidate from the first account, then the
  second account, then the first account's second candidate, and so on) and picking once
  across the merge — each account's own preference order is preserved, but neither account is
  favored over the other. This is scoped to an explicit opt-in per project: it changes nothing
  for a project that doesn't set it, and nothing for a project with a single `account`.
  Updated 2026-09-18 (mahler#383): `account_mode = "priority"` is the third, explicit mode.
  It treats the project's own `routing` table as one exact platform order across all its
  declared accounts. This covers intentionally asymmetric pools—for example, exhaust one
  ChatGPT login, then try a second login after the commodity builders but before Claude—that
  neither account fallback nor round-robin can express. Every route entry must spend a declared
  account, so the D25 credential boundary remains unchanged.
- Pins keep working the same way, generalized from equality to membership: a pin is valid if the
  pinned platform's account is one of the project's declared accounts, refused otherwise.
- Runner's fail-closed check (D25) generalizes the same way: a run must spend an account the
  project declares — one of its several, not a single fixed one.
- Builder-fairness accounting ("a sort must not eat the last builder", D25) stays per account. A
  multi-account project counts toward every account bucket it can draw from, competing in each
  pool it's eligible for, same as a single-account project competes in its one.
- GitHub identity is a separate axis from compute account and stays singular: `gh_account`
  (default personal) says which login the conductor uses for that project's sync, PRs, merges
  and comments. Mahler's own repo is already reachable from Mike's personal GitHub, so it needs
  no override even though it spends two compute accounts.
- Mahler is the deliberate multi-account project; it may list every compute login it is allowed
  to spend. Nothing else changes: a project that still names a single `account` keeps D25's
  exact behaviour, unchanged.

### D27 — The operator console: server-rendered, standard library, writes through the tick

Decided 2026-09-15, when the Phase 2 design came back from Claude Design. The approved spec
is [docs/console/design.md](docs/console/design.md): desktop `3a`, phone `2a`, copy final.

- **Standard library, not FastAPI and HTMX** (this amends D14). The console is HTML rendered
  by `mahler serve` plus a small vanilla script. No framework, no bundler, no `uv`. The
  design needs none of them, and the daemon must never break itself on a dependency.
- **One state, two layouts.** `mahler/console/state.py` builds one plain-data snapshot,
  including every sentence the page shows, and `page.py` lays it out for desktop and for
  phone in the same document; a media query picks. What only the browser needs to know
  (view, tab, theme, expanded groups, an open overlay) lives on `<html>`, and the 30-second
  refresh swaps the rest. The server is the only renderer: after every write the page
  simply re-renders from it.
- **Writes are `POST /api/<action>`**, and each one writes a ledger event. They're accepted
  only from this machine (which is how `tailscale serve` connects) or a Tailscale address,
  with an `X-Mahler-Console: 1` header, a JSON body, and a same-origin `Origin`. A cross-site
  page can't send that header without a CORS preflight, and the server answers none.
  - Writes that only touch the ledger apply at once, as `mahler pause` and `mahler peak --off`
    already do from outside the tick: pause and resume, the peak override, clearing a
    backoff, marking the digest seen.
  - Writes that reach GitHub or a running agent (answering a needs-you item, UAT pass and
    fail, capture, stop-and-hand-off, a revert) are queued in the ledger and applied by the
    tick. Every GitHub call keeps the project's own login (D25), and only the tick touches
    runs and leases.
- **An answer is a GitHub comment.** The tick posts it on the issue after a 60-second grace,
  which is what Undo cancels, and the existing reply-means-answer path re-sorts the item.
  No second state machine.
- **Ready to test** lists shipped issues whose PR carried a "Needs a human to check" list
  and that have no verdict yet (D10). Pass records the verdict. Fail files a linked
  `type:bug p1` with your note and the SHA, which routes like any other bug.
- **Undo a merge goes through the normal pipeline.** The tick makes the revert commit,
  files the issue and hands it to the conductor, so CI gates a revert like any change. The
  console always asks first.
- **Why nothing is running** is recorded by the scheduler every tick, not guessed by the
  page. Until it is, the page derives what it can see from the ledger: peak hours, quota
  lines, backoffs, a project slot held by an unmerged change, hot holds.
- **The peak override from the console holds until you switch it back**, as the design's
  copy says. `mahler peak --off` keeps its timed default, and `mahler peak --on` or Restore
  clears either.
- The event stream is a view one click away, never ambient. Banners appear only in Triage
  and Needs you, one expanded at a time.

### D28 — Wait for GitHub to confirm a requested merge

Decided 2026-09-15 (mahler#211; D28 reserved in the issue). A successful
`gh pr merge` can request an asynchronous merge. The conductor must observe
GitHub's PR state before announcing that the change shipped.

- Request the merge once per PR head SHA, recording the SHA and request time
  in ledger KV. Re-read the PR immediately so synchronous merges still finish
  in the same tick. Otherwise keep the item verifying and retain its slot.
- Poll on later ticks. An open, green PR that has not merged after
  `verify_timeout_minutes` goes to `needs_you`, releases the conductor lease,
  and sends the same console-linked notification as a pending-CI timeout.
  A new head SHA gets a fresh request and timeout; red CI and textual conflicts
  keep their existing fix/rebuild paths.
- CI also listens for `merge_group`, allowing the workflow to test a queue's
  combined state on repositories where a native queue is enabled.
- **No queue is enabled here.** The earlier run reported GitHub rejecting the
  merge-queue ruleset for this personal-account repository, and Mike ruled out
  moving it to an organization. This change lands the asynchronous plumbing.
  D19 documents mahler#239's conservative pre-request ancestry check and its
  residual base-write race; interactive merges make it relevant even with
  `max_parallel = 1`.

### D29 — Console: hold-reason rows link to their items; a Dependencies graph for the big picture

Decided 2026-09-15, from feedback on the shipped 0-runs and Backlog views (mahler#249, mahler#260):
the console can now say *why* nothing is running, but the aggregated sentences ("27 build item(s)
have no platform with headroom — busy: kilo; past the line: agy-claude, agy-gemini…") don't say
*which* items, and there's no way to see parent/child or depends-on relationships across a project's
backlog at a glance — with couch-tour alone running 25+ open items under several `parent` trackers,
that's the thing actually missing.

- **Hold-reason rows expand in place.** The scheduler already threads `project`/`number` through
  almost every hold (mahler#249's `ctx.hold(...)` calls); the console just isn't showing them.
  Attach the concrete item list to each reason and render it with the same client-only `data-toggle`
  mechanism the digest chip already uses (`console.js`) — no new POST, no new server state, just more
  of what `state._idle` already computes exposed to the page.
- **A Dependencies view, not a literal force-directed graph.** A spatial node-link layout at this
  density invites exactly the clutter the console's "no shadows, no filled cards" ethos exists to
  avoid, and pulling in a layout library would cross the "no dependency outside the standard library"
  line before the MCP phase means to. Instead: one **server-rendered SVG per project**, laid out in
  ranked columns — a plain longest-path rank computed in `state.py` (root items with no parent and no
  unmet dependency at rank 0, everything else one rank past its farthest predecessor), no client
  layout code. Node color reuses the existing three-tone system (`_state_tone`: bad/acc/mut); edges
  are solid for parent→child and dashed for depends-on; every node links straight to its GitHub
  issue, same as a backlog row does today.
- **Desktop only.** The phone console's whole premise is triage from a Pixel in under a minute
  (docs/console/design.md's overview); a ranked graph doesn't fit 390px or that job. The `List` /
  `Graph` toggle lives only in the desktop Backlog view, next to the existing per-project group
  headers; phone Browse keeps its flat list.
- This is new surface beyond the Phase 2 mock, not a deviation from it — `docs/console/design.md`'s
  screens don't cover it. It's designed here, in the same voice and constraints as D27, rather than
  redone in Claude Design, since it's additive to an existing view rather than a new screen.

### D30 — Work-repo compute strategy: decompose to size:s

Decided 2026-09-16 (mahler#331). Work repos use a specific compute strategy (`size_target = "s"`): their daytime builders take only `size:s`, and `size:m` waits for scarce, off-peak capacity. To avoid waiting, the sort agent decomposes `size:m` items into sequential, single-responsibility `size:s` pieces when possible.

- **Where it lives**: `size_target` in `[projects.<name>]` controls this.
- **The default**: If `size_target` is not set, a project that spends only work accounts (all differing from `personal`, see D25) defaults to `"s"`. Other projects default to `""` (no preference).
- **Who decomposes**: The sort agent receives an extra sizing instruction for `"s"` projects to split `size:m` items into `size:s` sub-issues, each one mergeable PR with its test, chained with `Depends on: #N`.
- **Off-peak policy**: `size:m` is kept only when a split would leave a broken or untested intermediate state, or when it touches security/credential boundaries. Those wait for a larger, off-peak builder.

### D31 — Release ledger and rolling synthesized draft

Decided 2026-09-17 (mahler#356; D31 reserved in the issue). Mahler knows when individual issues ship, but needs a durable release model and a deterministic rolling draft for later CLI, console, and managed-app surfaces.

- **Releases vs. Project Briefs**: A release is a durable, tagged checkpoint with a project version, a commit SHA, a frozen set of included shipped items, notes, and a remote release URL. It must not be conflated with the separate "since you last looked" project brief (an ephemeral operator catch-up view of recent activity).
- **Continue each project's build/release sequence** (amended by mahler#377): When Mahler has no local release rows, sync imports the latest existing GitHub release as a published baseline without claiming current draft items. Mahler preserves that project's established strict `X.Y` or `X.Y.Z` format instead of starting a parallel history or adding/removing a component. A two-part sequence advances sequentially (`0.83` → `0.84`) regardless of item labels. A three-part sequence retains SemVer behavior: feature work (`type:feature`) proposes a minor bump (`X.(Y+1).0`), while bug fixes and other non-breaking changes propose a patch bump (`X.Y.(Z+1)`). Major versions are never inferred automatically. Only a project with no Mahler or GitHub release history starts at `0.1.0`.
- **Rolling unreleased draft**: When conductor-ship completes (`ship._shipped`), the shipped issue number, PR, title, agent summary, merge SHA, labels, and shipped timestamp are snapshotted into the project's unreleased draft. Retries are idempotent, and an item belongs to at most one release across its lifetime.
- **Deterministic synthesized notes**: Notes are synthesized purely in Python standard library without calling an LLM during the tick. Features and bug fixes form the main summary; other user-facing changes (e.g. goals, UAT items, untyped) appear in an additional section; and `type:chore` or maintenance-pass work is excluded from the main summary by default, retained in collapsible details.
- **Readiness suggestion**: A draft is marked "release suggested" when it contains at least 5 unreleased items or its oldest unreleased item is at least 7 days old. This signal is advisory only and never publishes automatically.
- **No automatic publishing initially**: Releases publish only when explicitly initiated by the operator. Goal-completion automation and automatic publishing are deferred until real release boundaries and notes have been validated in practice.

#### Managed-app What's New feed contract (schema v1)

Decided 2026-09-17 (mahler#358). Managed apps consume release notes via an HTTP JSON feed exposed by Mahler. The contract is versioned, strictly read-only, project-scoped, and privacy-conscious.

- **Backed only by published releases**: The feed reflects only sealed, published releases from the release ledger (never unreleased drafts, in-progress runs, or unmerged branches).
- **Schema versioning & compatibility**:
  - The contract starts at `schema_version = 1`.
  - Evolution within `schema_version = 1` is strictly additive (new fields may be added; existing fields cannot be removed or have their types altered).
  - Clients must ignore unrecognized fields.
  - Any breaking change (structural restructuring, field removal, or semantic changes) requires incrementing `schema_version` (e.g. `2`).
- **Feed payload format (JSON)**:
  Project-scoped feed (e.g., `GET /api/projects/<project>/releases.json` or `/api/releases/<project>.json`):
  ```json
  {
    "schema_version": 1,
    "project": "couch-tour",
    "generated_at": "2026-09-17T02:00:00Z",
    "releases": [
      {
        "version": "1.2.0",
        "checkpoint_sha": "a1b2c3d4e5f678901234567890abcdef12345678",
        "published_at": "2026-09-17T01:30:00Z",
        "remote_url": "https://github.com/mkny13/couch-tour/releases/tag/v1.2.0",
        "sections": {
          "features": [
            {
              "number": 42,
              "pr": 43,
              "title": "Add dark mode toggle",
              "summary": "Persist theme preference across launches"
            }
          ],
          "fixes": [
            {
              "number": 45,
              "pr": 46,
              "title": "Fix audio stutter on route transition",
              "summary": "Prevent audio buffer underrun when switching screens"
            }
          ],
          "other": []
        },
        "maintenance": [
          {
            "number": 48,
            "pr": 49,
            "title": "Bump dependencies and update test harness",
            "summary": "Update build tooling and library versions"
          }
        ]
      }
    ]
  }
  ```
- **Fields & Stable Identifiers**:
  - Top level: `schema_version` (integer), `project` (string), `generated_at` (ISO 8601 string), `releases` (array of release objects).
  - Release object: `version` (the project's established `X.Y` or SemVer `X.Y.Z` form), `checkpoint_sha` (git commit SHA), `published_at` (ISO 8601 string), `remote_url` (GitHub release link or null/empty), `sections` (object with `features`, `fixes`, and `other` lists), and `maintenance` (list).
  - Item object: `number` (issue integer, stable unique identifier), `pr` (pull request integer or null), `title` (string), `summary` (concise human-facing summary string).
- **Ordering and limits**:
  - Releases are ordered newest first (descending by `published_at` / SemVer).
  - Feeds may accept an optional `limit` query parameter (defaulting to the latest 20 releases) to bound payload size on mobile networks.
- **Empty state and error resilience**:
  - If a project has no published releases, the feed returns HTTP 200 with `"releases": []`.
  - Missing, network-unreachable, or HTTP error responses must be handled gracefully by client apps. An unreachable feed or invalid JSON payload must never crash the app or block app startup.
- **Privacy and operational boundaries**:
  - The feed contains release metadata only.
  - Sensitive and operational data are strictly excluded: no issue comments, agent run logs, platform prompts, credentials/tokens, or D10 UAT checklist items are ever exposed in the feed.
- **Client acknowledgement and read state**:
  - Acknowledgement is **strictly local to each app installation** (stored in local SQLite, `localStorage`, `UserDefaults`, `SharedPreferences`, etc.).
  - Version comparison: the client stores `last_acknowledged_version` (e.g. `"1.1.0"` or `"0.83"`). Any release with greater numeric component precedence is treated as unread/new.
  - Mark-read timing: a release is marked read/acknowledged **only after the user views or dismisses** the What's New surface, never automatically during background fetch or app boot.
  - The client UI may link directly to the durable GitHub Release URL (`remote_url`) for users who want complete commit history.
- **Maintenance visibility in clients**:
  - Features and bug fixes form the primary human-facing surface.
  - Maintenance entries (`maintenance`) are provided in the payload for completeness, but must be collapsed or hidden by default in client UIs to avoid cluttering the user experience with chore/audit noise.
- **Read-only transport**:
  - The feed transport is strictly read-only (`GET`).
  - Managed apps **never** publish releases, modify draft state, or report acknowledgement state back to Mahler. All release publication remains on the Mahler host via operator command (`mahler release` / console).


### D15 — Deliberately not doing

- Not multi-user, and no replicated/distributed scheduler. D24 can relay authoritative lease
  operations for an explicitly shared project.
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
