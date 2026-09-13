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
- [ ] **S5 — GitHub polling cost with ETags.** Filed as an issue. The bootstrap polls
      plainly at a low rate meanwhile.
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
  self-update and rollback, launchd `com.mike.mahler`.
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

**Status (2026-09-12):** kernel built and running under launchd (`com.mike.mahler`).
First pipeline run: issue mahler#1 sorted by Claude, built by Antigravity's Claude pool.
groundwork was brought in early at your request, scoped to issues labelled `mahler`
(#80, #81), with production data off-limits until Phase 4. Phase 1 items are filed as
mahler#3–#8.

**Done when:** you file an issue on `mkny13/mahler` from your phone, and Mahler sorts it,
builds it on a free platform, gets CI green, merges it, updates itself to the new known-good,
and pings you. No Claude Code session involved.

---

## Phase 1 — POC on groundwork (built by Mahler)

Prove the hard parts on a real app. Each bullet is an issue in the Mahler repo.

1. **Interactive participation:**
   - Claude `SessionStart` + `PreToolUse` hooks (claim nudges, heartbeat, yield delivery,
     fence on `gh pr merge`)
   - Mahler section in groundwork's CLAUDE.md/AGENTS.md
   - a minimal **MCP server** (`add_item`, `claim`, `heartbeat`, `handoff`, `list_items`), so
     Cline and Antigravity chats participate too
2. **S3 Cline spike → Cline backend** (free models, `s` items).
3. **groundwork onboarding:**
   - `.mahler/project.toml` (verify, preview, smoke, release, data, environments)
   - backlog migrated from TASKS.md and Cline Kanban into issues
   - a Neon backup restored once
   - groundwork disabled in `dispatch.toml`
4. **Deploy tracking:** register each Vercel deploy as a build; run the smoke check; `shipped`
   only when independent signals agree (D11).
5. **Status page** over `tailscale serve` (S4 first): running work, quota gauges, recent
   events.

**Done when**, on groundwork over one real week:

- at least 5 items shipped hands-off
- at least 1 item completed after a cross-platform handoff
- at least 1 pre-emption by a phone chat (Remote Control) with no lost work
- zero double assignments
- zero autonomous extra-usage spend
- Pause works

### ◆ Checkpoint after the POC

A short review before investing in UI:

- What broke? What were the real quota numbers?
- Were the free models good enough, and how often did work escalate to Claude?
- Were the lease TTLs right?
- Does anything push toward adopting Gas City or keeping Cline Kanban after all?

DESIGN.md gets updated with the answers.

---

## Phase 2 — Claude Design: Mahler's own screens

Your request: a design phase after the POC for Mahler's own interfaces.

- [ ] **Console, phone-first:**
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

- [ ] The console, built to the Phase 2 designs, served over Tailscale. It needs `uv`-managed
      dependencies; this is the kernel's first step beyond the standard library.
- [ ] Full MCP tool set (`ask_user`, `report_progress`, `next_id`, `get_context`), registered
      in Claude Code, Cline and Antigravity. `/mahler` skill. The `handoff`/`pickup` skills
      become Mahler-aware.
- [ ] `/mahler undo`.
- [ ] Build registration on each deploy. A `uat-author` recipe turns each shipped issue's
      "needs a human to check" section into UAT items.
- [ ] `mahler-uat.js` web panel in groundwork's staging/preview builds. A fail reopens the
      issue or opens a linked p1 bug with your note and screenshot. Offline queue +
      GitHub-URL fallback.
- [ ] Daily digest ping.

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
- [ ] **Undo:** revert + platform rollback, end to end.
- [ ] Exclude `.mahler-worktrees` from Backblaze. Confirm Backblaze still covers the SSD
      (it's now the only non-GitHub file backup; Time Machine is dropped).
- [ ] Secrets audit.

**Done when:** a deliberately bad migration on groundwork is caught or rolled back with its
data intact. Undo has reverted a real change. A restore drill has passed for every groundwork
store.

---

## Phase 5 — Cutover and rollout

- [ ] **thread as a sensor:** anomaly flags become `type:anomaly` issues.
- [ ] Per-project onboarding checklist, run by an agent:
  - private GitHub repo
  - labels
  - `project.toml` (verify, data, release, environments, canary)
  - backlog migration
  - CLAUDE.md/AGENTS.md section
  - data inventory + first restore drill
  - disable in `dispatch.toml`
- [ ] Onboard in this order:
  1. **mental-jukebox**
  2. **puppy-growth-chart**
  3. **movebreak**
  4. **phish-in-app / Couch Tour**:
     - single release channel, with manual promotion dropped
     - **a staging sync backend + side-by-side test app** so UAT listening stays out of your
       real history (D16)
     - `gated` lifted
     - local Xcode/Gradle verify with canary checks
  5. Non-git projects, as you choose to activate them
- [ ] Android + macOS UAT panels. Migrate phish-in-app's `UAT.md` history.
- [ ] Retire the dispatch launchd timer and Cline Kanban. Point ThreadBar at Mahler, or
      retire it. Update `~/ai-tools` NOTES/TASKS.

**Done when:** every active project runs through Mahler, and dispatch and Kanban are off.

---

## Phase 6 — A deeper feedback loop

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
