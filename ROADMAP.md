# Mahler — Roadmap

The build plan for [DESIGN.md](DESIGN.md). Each phase ships something usable on its own and
has a concrete "done when." Later phases are planned in full but will be refined with what
the earlier ones teach. The POC is deliberately rough. Polish comes after it proves the hard
parts.

**Bootstrap first (re-sequenced 2026-09-12 at your request).** The fastest route out of
hand-driving Claude Code is a small kernel that can work through its *own* GitHub issues.
From Phase B onward, every later phase is a set of issues in the Mahler repo, and Mahler
builds them. You steer by filing issues (GitHub app, any chat), answering pings and doing UAT.

**First real app:** `groundwork` (Phase 1). It's a Next.js app on Vercel + Neon, with CI,
Playwright and preview deploys already in place, and it has real personal data.

```
0 Spikes (mostly done) → B Bootstrap: Mahler builds Mahler → 1 POC on groundwork
→ ◆ checkpoint → 2 Claude Design (Mahler's screens) → 3 Intake + UAT loop
→ 4 Safety hardening → 5 Cutover & rollout → 6 Deeper feedback loop
→ 7 Cross-product review → 8 Extras
```

---

## Current state (2026-09-16)

Phases 0, B and 1 are done and the project has been running past the original phase
sequence for a while — the day-to-day reality no longer matches "one phase at a time,"
so this section says where things actually stand; the phase list below stays as the
detailed record.

- **Multiple projects are self-managed** by the same 60-second launchd tick, including
  Mahler itself. The full isolated unit suite and randomized-order runner are the release
  contract; avoid embedding a test count here because it changes almost daily.
- **Self-hosting (D17) works:** mahler builds itself through the same lease/ship pipeline
  as any managed project, with CI-gated self-update and rollback.
- **Interaction today is GitHub + chat + ntfy + the phone/desktop console.** Phase 2's
  console is built; the full MCP and in-app UAT designs remain incomplete:
  - a minimal MCP server (`mahler/mcp.py`): `list_items`, `add_item`, `claim`,
    `heartbeat`, `release`, `handoff`, `next_id` — missing `ask_user`, `report_progress`,
    `get_context` from the original Phase 3 list
  - the operator console (`mahler serve`, its own launchd job; DESIGN D27): status, quota,
    idle reasons, backlog and dependency views, capture with attachments, needs-you answers
    with Undo, live logs, stop-and-handoff, UAT pass/fail, and CI-gated merge reverts
  - GitHub comment commands (`/mahler go`, `/mahler park`, `/mahler platform <name>`) and
    a plain reply on a `needs-you` item
  - Claude Code `SessionStart`/`PreToolUse`/`PostToolUse`/`UserPromptSubmit` hooks
    (`mahler hooks`), a daily digest (`digest.maybe_send`, wired into every tick), and
    a deduplicated high-priority ntfy alert when Codex quota is exhausted during probe
    refreshes (cleared upon recovery; prompts to reset in ChatGPT if reset credits exist)
  - the rest of the MCP tool set and app-embedded UAT surfaces remain queued, not dropped
- **Onboarding order diverged from the Phase 5 plan.** Couch Tour went live before the
  projects originally listed ahead of it. The operator's `~/.mahler/config.toml`, not this
  repository, is authoritative for which projects are enabled now; Phase 5 preserves the
  intended/historical order rather than pretending to be live inventory.
- **Maintenance is steady-state work.** Manual-audit deduplication (mahler#204), periodic
  platform capability review (mahler#206), and platform × role × size estimates
  (mahler#207) have shipped. Periodic documentation accuracy/onboarding review was added in
  mahler#336 so these docs are checked by the same cadence/throughput scheduler.

---

## Phase 0 — Spikes

- [x] **S1 — Claude usage in headless runs.** Every `claude -p --output-format stream-json
      --verbose` run emits `rate_limit_event` with the 5-hour and weekly utilization. A lean
      probe costs ~700 tokens. (DESIGN D8)
- [x] **S2 — Antigravity CLI.** `agy` 1.1.23 is installed and signed in; headless edit, run
      and commit work. `--add-dir` is required. `/usage` gives JSON quota for free across
      **two pools** (Gemini; Claude/GPT). (DESIGN D8)
- [x] **S3 — Cline CLI on free models.** `cline --cwd --json` works unattended at $0 with no
      quota reporting → routed as unmetered, `size:s` only. (DESIGN D8)
- [ ] **S4 — Reach from the phone** (`tailscale serve`, CORS from app origins). Filed as an
      issue.
- [x] **S5 — GitHub polling cost with ETags.** Shipped (mahler#90): sync() probes the
      open-issue collection with one conditional `gh api -i` GET per tick; a 304 skips the
      fetch and the closed-issue checks and costs no quota.
- [ ] **You:** install **ntfy** on the Pixel and subscribe to the topic printed at the end of
      Phase B.

---

## Phase B — Bootstrap: Mahler builds Mahler

A standard-library-only Python kernel, run by launchd every 60 seconds, that manages one
project: Mahler itself.

**In the kernel:**
- **Ledger** (SQLite): items, **leases with compare-and-set claims and epochs**, runs, events,
  usage samples, counters, pause flag. The lease semantics get real unit tests.
- **GitHub sync** through `gh`:
  - open issues → items
  - closed issues → release and stop
  - comment commands: `/mahler go`, `/mahler park`, `/mahler platform <name>`
  - a plain reply on a `needs-you` item is taken as the answer
- **Router** (D8 policy):
  - sorting → Claude Code (falls back to Antigravity's Claude pool)
  - building → Antigravity Claude pool → Antigravity Gemini pool → Claude Code (under the
    60% / 70% reserve)
  - never extra usage; unknown usage is treated as over the line
- **Runners** for `claude` and `agy`:
  - one worktree per run on `mahler/<issue>-<slug>`
  - detached, with a status file
  - `MAHLER_*` environment
  - pre-push epoch fence, injected via `GIT_CONFIG_*`, which chains to the repo's own hooks
- **Watchdog:**
  - heartbeat while the process lives
  - 20-minute no-progress and 60-minute wall-clock limits
  - a quota hard line or pre-emption → stop, snapshot, handoff
- **Handoff:**
  - automatic note on any exit that didn't merge
  - non-destructive snapshot of the worktree to `mahler/snapshot/*`
  - attempts cap → `needs-you`
- **Presence-lite:** recent Claude Code transcript activity in the project → a hot hold on
  new starts, and renewal of interactive leases.
- **CLI for interactive sessions:** `mahler claim | heartbeat | release | lease-check |
  next-id`, plus `status`, `usage`, `add`, `pause`, `resume`, `tick`.
- **Recipes:** `sort` and `build`.
- **ntfy** pings: needs-you, shipped, handoff, failed.
- **Self-hosting** (D17): `~/.mahler/app` clone at known-good, the tiny launcher with CI-gated
  self-update and rollback, launchd `local.mahler`.
- **Mahler repo:** private GitHub remote, CI (unit tests), labels, `CLAUDE.md`/`AGENTS.md`
  with the agent rules, `.mahler/project.toml`.

**Deliberately not in the kernel** (filed as issues for Mahler to build):
- the MCP server
- the console
- Claude session hooks
- the Cline backend
- the thread sensor
- the UAT loop
- backups
- the statusline sidecar
- groundwork onboarding

**Status (2026-09-12):** kernel built and running under launchd (`local.mahler`).
First pipeline run: issue mahler#1 sorted by Claude, built by Antigravity's Claude pool.
groundwork was brought in early at your request, scoped to issues labelled `mahler`
(#80, #81), with production data off-limits until Phase 4. Phase 1 items are filed as
mahler#3–#8.

**Done when:** you file an issue on `mkny13/mahler` from your phone, and Mahler sorts it,
builds it on a free platform, gets CI green, merges it, updates itself to the new known-good,
and pings you. No Claude Code session involved.

---

## Phase 1 — POC on groundwork (built by Mahler)

**Status: done.** groundwork has been running under Mahler for weeks with real throughput
(~45 PRs merged in the trailing 14 days as of 2026-09-14); the checkpoint below happened
in substance, even though it was never written up as a standalone doc — its answers are
what DESIGN.md's D17–D26 decisions are.

Prove the hard parts on a real app. Each bullet is an issue in the Mahler repo.

1. **Interactive participation:** [x] done
   - [x] Claude `SessionStart` + `PreToolUse` hooks (claim nudges, heartbeat, yield delivery,
     fence on `gh pr merge`) — plus `PostToolUse`/`UserPromptSubmit` for heartbeat, all
     installed via `mahler hooks` (mahler/cli.py)
   - [x] Mahler section in groundwork's CLAUDE.md/AGENTS.md
   - [x] a minimal **MCP server** (mahler/mcp.py): `list_items`, `add_item`, `claim`,
     `heartbeat`, `release`, `handoff`, `next_id` — Cline and Antigravity chats can
     participate, though `ask_user`/`report_progress`/`get_context` (Phase 3) aren't there yet
2. [x] **S3 Cline spike → Cline backend** (free models, `s` items) — `cline-free` platform.
3. [x] **groundwork onboarding:**
   - [x] project policy in `~/.mahler/config.toml` (verify, `scope = "label"`, `worktree_root`,
     `link`, production-migration rules)
   - [x] backlog migrated into issues
   - [x] a Neon backup restored once (see Phase 4 — nightly job is live)
   - [x] groundwork disabled in the old `dispatch.toml`
4. **Deploy tracking:** not directly verified from this review — worth a status check next
   time groundwork ships a deploy-sensitive change.
5. [x] **Operator console** (`mahler/serve.py`, own launchd job): the original status page
   grew into the D27 phone/desktop console described in Phase 3. `tailscale serve` exposure
   remains a machine setting rather than repository state.

**Done when**, on groundwork over one real week:

- [x] at least 5 items shipped hands-off
- [x] at least 1 item completed after a cross-platform handoff (D9 handoffs are in daily use)
- [ ] at least 1 pre-emption by a phone chat (Remote Control) with no lost work — not
  confirmed either way
- [x] zero double assignments (lease compare-and-set with epochs, unit-tested)
- [x] zero autonomous extra-usage spend (D8: unknown usage counts as over the line)
- [x] Pause works (`mahler pause`/`resume`)

### ◆ Checkpoint after the POC

**Status: happened, informally.** No standalone writeup exists, but every question below
has a real answer baked into DESIGN.md's decision log by now (D8 evolved into D17
self-hosting, D20 maintenance passes, D21 size-based routing, D22 peak windows, D23 quota
burst, D25/D26 multi-account routing — all things the POC would have surfaced). Worth a
real half-hour review if you want the answers written down explicitly rather than inferred
from the decisions that came out of them:

- What broke? What were the real quota numbers?
- Were the free models good enough, and how often did work escalate to Claude?
- Were the lease TTLs right?
- Does anything push toward adopting Gas City or keeping Cline Kanban after all?

DESIGN.md gets updated with the answers.

---

## Phase 2 — Claude Design: Mahler's own screens

**Status: the console is designed (2026-09-15).** The approved canvas and spec are in
[docs/console/design.md](docs/console/design.md), and building it is Phase 3's first item
(DESIGN D27). The in-app UAT panel and the notification copy are not designed yet.

Your request: a design phase after the POC for Mahler's own interfaces.

- [x] **Console, phone-first** (approved 2026-09-15, docs/console/design.md):
  - **Capture:** text, voice via the keyboard, photo or screenshot, project picker
  - **Needs you:** one-tap answers
  - **Ready to test**
  - **Now:** running work, quota gauges, handoffs
  - **Backlog:** per project, reorder, park
  - **History:** with Undo
  - **Pause all**
  - the same console on the Mac's larger screen
- [ ] **In-app UAT panel:** the web overlay first. Then the Android and macOS equivalents,
      based on the existing Feedback buttons, plus the **test-app vs real-app** distinction
      from D16.
- [ ] **Notification copy:** what each ping says, and where tapping lands.
- [ ] Built as a Claude Design canvas (`/design`), revised with you until you're happy.
      Output: the approved canvas + a short component spec, filed as issues for Phase 3.

**Done when:** you've approved the console and the web UAT panel designs.

---

## Phase 3 — Intake and the UAT loop

- [x] The standard-library console, built to the Phase 2 designs (DESIGN D27): phone and
      desktop views; scheduler-recorded idle reasons; answers; UAT; capture and attachments;
      stop-and-handoff; live logs; merge revert; pause, quota and digest controls. Tailscale
      exposure remains an explicit per-machine operator setting.
- [~] Full MCP tool set — `next_id` shipped with the Phase 1 minimal server; `ask_user`,
      `report_progress`, `get_context` still open. `/mahler` skill and Mahler-aware
      `handoff`/`pickup` skills not confirmed.
- [ ] `/mahler undo`.
- [~] Shipped issues whose PR has a "Needs a human to check" section register in the console
      UAT queue. Full deploy/channel registration and a separate `uat-author` recipe remain.
- [ ] `mahler-uat.js` web panel in groundwork's staging/preview builds. A fail reopens the
      issue or opens a linked p1 bug with your note and screenshot. Offline queue +
      GitHub-URL fallback.
- [x] Daily digest ping — `digest.maybe_send()` runs every tick (mahler/scheduler.py).

**Done when:** you log a groundwork bug from your phone, it gets fixed and deployed, you check
it in the in-app panel and mark it passed, all without touching a laptop. A failed UAT check
has turned into a fix with no action from you beyond tapping "fails".

---

## Phase 4 — Safety hardening (before any second app)

- [x] Nightly verified backup job (`mahler/backup.py`, pulled forward 2026-09-12):
      groundwork's Neon production database (Postgres 18.6, dumped with Homebrew `libpq`
      18.6) → `/Volumes/ExtSSD160/mahler-backups/`, 14/8/12 retention. First dump taken
      2026-09-12 17:02.
- [ ] Other store kinds (`wrangler d1 export`, `sqlite3 .backup`) as projects onboard.
      Monthly automated restore drills.
- [ ] **Staging seeded from backups** (D16). Every seed doubles as a restore drill.
- [ ] **Backup receipt before risky deploys.**
- [ ] Migrations tested against a copy of real data.
- [ ] Guardrail hooks for Claude, agy and Cline.
- [~] **Undo:** CI-gated revert PRs ship from the console; platform rollback/redeploy remains.
- [ ] Exclude `.mahler-worktrees` from Backblaze. Confirm Backblaze still covers the SSD
      (it's now the only non-GitHub file backup; Time Machine is dropped).
- [ ] Secrets audit.

**Done when:** a deliberately bad migration on groundwork is caught or rolled back with its
data intact. Undo has reverted a real change. A restore drill has passed for every groundwork
store.

---

## Phase 5 — Cutover and rollout

**Status: reordered by reality.** Couch Tour (phish-in-app) went live ahead of the
original sequence. Current enablement is machine state in `~/.mahler/config.toml`; the
unchecked entries below are remaining roadmap candidates, not a claim about live config.

- [ ] **Mahler-native anomaly sensor:** useful legacy `thread` flags become `type:anomaly`
      issues. The retired `~/ai-tools/thread.py` is not run or imported; the native sensor
      must ignore `origin/mahler/*` and Mahler worktree roots (DESIGN D2).
- [ ] Per-project onboarding checklist, run by an agent:
  - private GitHub repo
  - labels
  - `project.toml` (verify, data, release, environments, canary)
  - backlog migration
  - CLAUDE.md/AGENTS.md section
  - data inventory + first restore drill
  - confirm no external agent dispatcher targets the project
- [ ] Onboard in this order:
  1. **mental-jukebox** — onboarding audit complete; repository-side policy is tracked
     in [mental-jukebox#3](https://github.com/mkny13/mental-jukebox/issues/3):
     - the private repo has the complete Mahler label taxonomy, but its default branch
       must be normalized to `main` before Mahler creates worktrees
     - production state is local `cache.sqlite`, `config.ini`, and
       `spotify_token.json`; agent verification must be credential-free and must not
       touch those files, the installed app, or the live Spotify playlist
     - all PLAN milestones are complete and TASKS contains only a legacy-dispatch log,
       so there is no open TASKS/PLAN/UAT backlog to migrate
     - live enablement waits for the repository contract and verifier; the retired
       ThreadBar process is still running and ROADMAP records mental-jukebox as an
       explicit entry in the legacy dispatcher configuration
  2. **puppy-growth-chart** — original candidate
  3. **movebreak** — original candidate
  4. [x] **phish-in-app / Couch Tour** — onboarded and running, out of the planned order:
     - single release channel / staging sync backend status not confirmed from this review
     - `gated` status not confirmed
     - local Xcode/Gradle verify with canary checks: `canary` referenced in DESIGN D-table
       and ROADMAP's own onboarding checklist above, not independently verified here
  5. Non-git projects, as you choose to activate them
- [ ] Android + macOS UAT panels. Migrate phish-in-app's `UAT.md` history.
- [x] **Retire the legacy agent dispatchers.** The owner unloaded
      `com.mike.dispatch` on 2026-09-16; `dispatch.py`, the legacy `thread.py` runtime,
      ThreadBar, and Cline Kanban are retired. ThreadBar will not be repointed at Mahler.
      Mahler is the only autonomous dispatcher for managed projects (DESIGN D2).
  - **Manual machine cleanup still required:** stop/remove the currently running ThreadBar;
    disable every entry in `~/ai-tools/dispatch.toml` (especially `mental-jukebox`); then
    archive or remove the unloaded dispatch plist and update/archive `~/ai-tools` NOTES/TASKS.
    These files and processes live outside this repository and are not changed by a Mahler PR.

**Done when:** every active project runs through Mahler; dispatch and Kanban are off; and no
external scanner or dispatcher acts on Mahler refs or worktrees.

---

## Phase 6 — A deeper feedback loop

- [x] Console walkthrough for Mahler (manually triggered agent UI sweep)
- [ ] Playwright screenshot checks against preview URLs, attached to PRs and handoffs.
- [ ] Android emulator + `adb` screencaps; Maestro flows.
- [ ] Runtime error capture → auto-filed issues.
- [ ] **Spike:** can offscreen SwiftUI snapshot tests render while the Mac mini is locked?
      If yes, macOS UI gets agent-visible regression checks. If not, macOS UI stays UAT-only
      (the screen stays locked; decided).
- [ ] Self-hosted GitHub Actions runner vs local-only verify for Apple/Android. (couch-tour is
      a public repo, so its GitHub-hosted macOS minutes are free.)

---

## Phase 7 — Adversarial cross-product review

The second BACKLOG idea: "product" = platform. The trigger is the default for `m`/`l` items
and anything touching data. Findings feed back as a fix round. Keep it only where it pays for
itself.

---

## Phase 8 — Extras

- Claude cloud sessions / claude-code-action as extra workers.
- More backends: OpenCode. (Copilot CLI and Kilo landed in mahler#25.)
- Goals → automatic breakdown into sub-issues.
- Quota analytics.
- Self-hosted ntfy.

---

## Still open (not blocking)

- **Antigravity IDE quota:** whether it shares the CLI's pools. It only matters if you also
  use the IDE interactively.
- **Couch Tour staging data:** what exactly counts as "listening history" (sync backend
  only, or also phish.in account likes), settled at its onboarding.
