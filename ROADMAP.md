# Mahler — Roadmap

The build plan for [DESIGN.md](DESIGN.md); [ARCHITECTURE.md](ARCHITECTURE.md) maps what is
built. This file separates done, in progress and planned work: the "Current state" section
is the authority on status, and the phase list below it is the detailed record. Each phase ships something usable on its own and
has a concrete "done when." Later phases are planned in full but will be refined with what
the earlier ones teach. The POC is deliberately rough. Polish comes after it proves the hard
parts.

**Bootstrap first (re-sequenced 2026-09-12 at your request).** The fastest route out of
hand-driving Claude Code is a small kernel that can work through its *own* GitHub issues.
From Phase B onward, every later phase is a set of issues in the Mahler repo, and Mahler
builds them. You steer by filing issues (GitHub app, any chat), answering questions and reporting problems during ordinary use.

**First real app:** `groundwork` (Phase 1). It's a Next.js app on Vercel + Neon, with CI,
Playwright and preview deploys already in place, and it has real personal data.

```
0 Spikes (mostly done) → B Bootstrap: Mahler builds Mahler → 1 POC on groundwork
→ ◆ checkpoint → 2 Claude Design (Mahler's screens) → 3 Intake + fix-on-failure
→ 4 Safety hardening → 5 Cutover & rollout → 6 Deeper feedback loop
→ 7 Cross-product review → 8 Extras
```

---

## Talk it through (#921; design artifacts 2026-10-11)

- #922: D43, the [discussion contract](docs/console/discuss.md), recipe and static
  [phone](docs/console/screens/discuss-phone.svg) /
  [desktop](docs/console/screens/discuss-desktop.svg) mockups are approved, committed
  design artifacts for the shipped operator-started discussion feature.
- #923 durable sessions/restricted tools and #924 personal subscription worker are built.
- #926 adds the work Agent SDK adapter, turn-scoped gateway authorization and shared D41
  request accounting. Personal and work paths are isolated; ambiguous policies fail closed.
  [Setup and qualification evidence](docs/team-api-credits.md#work-discussions-926) include
  the required runner reinstall and gateway restart. Fake SDK/gateway tests cover routing,
  resumes, retries, caps, secrets and restarts. Responses remain buffered.
- #925 delivered the approved console panel. One live work conversation and its confirmed
  issue links remain post-merge evidence; the operator-started discussion feature is shipped.

## Current state (repository evidence, 2026-11-27)

This reconciliation uses the current modules, CLI parser, MCP tools/list response and
hermetic tests. Earlier committed audits provide supporting evidence, not live machine
or project inventory: [lifecycle](docs/audits/1670-lifecycle-state.md),
[execution/shipping](docs/audits/1670-execution-shipping.md),
[routing/accounts](docs/audits/1670-routing-quota-accounts.md), and
[operator/maintenance](docs/audits/1670-operator-maintenance.md).
The phase records below preserve dated rollout observations and original plans;
operator configuration remains the authority for enablement, credentials and exposure.

- **Lifecycle and self-hosting:** eleven item states, epoch-fenced leases, canonical
  remote lease operations, trust-gated intake, timed priority and external waits are
  implemented. The launcher gates updates on CI and local tests, with launch breakers,
  canaries and rollback to launch-proven commits. Launcher installation is separate.
- **Execution and shipping:** agents verify and commit locally; the conductor snapshots,
  pushes, opens PRs, watches CI, coordinates independent head-bound review and confirms
  merges. Post-merge checks and 14 quiet days support automatic completion. Optional
  failure reports create linked p1 fixes; manual-verdict panels are superseded (D10).
- **Routing and accounts:** deterministic account-aware routing, fail-closed metered
  quota freshness, Claude burst, reset credits, warm-ups, model availability, run usage,
  scorecard and historical capacity reporting are built. Opt-in runners and the D41
  confirmed-credit gateway/Agent SDK integration exist; tests do not prove live enablement.
  Work GitHub and Claude identities require scoped configuration with no personal fallback.
- **Operator surfaces:** phone/desktop console, settings, capture, answers with Undo,
  What changed/failure reports, logs, release feed, CLI, seven-tool local MCP, and restricted
  operator-started discussions are shipped. Atlas inventory and project-scoped lessons/history
  are built. The remaining MCP tools (`ask_user`, `report_progress`, `get_context`) are planned.
- **Maintenance:** scheduled project reviews, platform and practices audits, ledger and
  PostgreSQL backups, cleanup, digests, net backlog and notification redaction are implemented.
  Practices audits file evidence/proposals and reconcile resolved proposals; they never
  remediate repositories or change policy. Automated restore drills, other hosted-store
  backups and staging seeding remain planned. Backup validation is not a restore drill.
- **Verification:** CI runs the repository guard, unit discovery, randomized-order runner
  and strict resource checks. The audits used hermetic fixtures; they do not attest to live
  provider behavior, console exposure, GitHub merges, desktop smoke or production restores.


---

## Historical phase records

The phases below retain the original plan and dated rollout observations, beginning
2026-09-12. Checked boxes record those observations, not a fresh live check.
Installed providers, backup disks, account access, project enablement and console
exposure remain unknown in this repository-only review. Current implementation
status is summarized above; remaining work is consolidated below.

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
- automated post-merge evidence and optional failure capture (done, mahler#748)
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
(DESIGN D27). Manual-verdict panels are superseded by D10; notification copy remains.

Your request: a design phase after the POC for Mahler's own interfaces.

- [x] **Console, phone-first** (approved 2026-09-15, docs/console/design.md):
  - **Capture:** text, voice via the keyboard, photo or screenshot, project picker
  - **Needs you:** one-tap answers
  - **What changed**
  - **Now:** running work, quota gauges, handoffs
  - **Backlog:** per project, reorder, park
  - **History:** with Undo
  - **Pause all**
  - the same console on the Mac's larger screen
- [x] **In-app UAT panel — superseded (2026-10-06):** the proposed web, Android
      and macOS manual-verdict surfaces are retired in favor of automated post-merge
      evidence and ordinary failure capture (D10). The original Feedback buttons
      remain the historical starting point.
- [ ] **Notification copy:** what each ping says, and where tapping lands.
- [ ] Built as a Claude Design canvas (`/design`), revised with you until you're happy.
      Output: the approved canvas + a short component spec, filed as issues for Phase 3.

**Done when:** the console design supports What changed and optional failure capture;
no owner shipment verdict is required.

---

## Phase 3 — Intake and fix-on-failure

- [x] The standard-library console, built to the Phase 2 designs (DESIGN D27): phone and
      desktop views; scheduler-recorded idle reasons; answers; What changed; capture and attachments;
      stop-and-handoff; live logs; merge revert; pause, quota and digest controls. Tailscale
      exposure remains an explicit per-machine operator setting.
- [~] Full MCP tool set — `next_id` shipped with the Phase 1 minimal server; `ask_user`,
      `report_progress`, `get_context` still open. `/mahler` skill and Mahler-aware
      `handoff`/`pickup` skills not confirmed.
- [ ] `/mahler undo`.
- [x] **Manual-UAT loop — superseded (2026-10-06):** the former checklist queue
      and proposed `uat-author` role no longer gate completion. Shipments complete
      on automated evidence or a 14-day quiet period; early automated completion
      reopens on adverse evidence within that window (D10).
- [x] **`mahler-uat.js` web panel — superseded:** the planned groundwork staging
      pass/fail panel and its offline verdict queue are retired. Ordinary optional
      failure capture files linked p1 fix work; historical evidence is retained.
- [x] Daily digest ping — `digest.maybe_send()` runs every tick (mahler/scheduler.py).

**Done when:** you log a groundwork bug from your phone, it gets fixed and deployed,
and automated evidence or 14 quiet days completes it. An optional failure report
creates linked fix work without an owner pass step.

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
store. These are automated safety checks, not owner shipment verdicts; D10 governs
automated-or-14-day-quiet completion and optional failure reports.

---

## Phase 5 — Cutover and rollout

**Historical rollout snapshot (2026-09-23), not current inventory.** The recorded mini enabled `mahler`,
`groundwork`, `movebreak`, `phish-in`, `hockey`, and `sit-stand-walk`. Current enablement is
machine state in `~/.mahler/config.toml`; this dated list only makes the rollout history
legible. Unchecked project entries below are remaining candidates, not a claim about live
config.

- [ ] **Mahler-native anomaly sensor:** useful legacy `thread` flags become `type:anomaly`
      issues. The retired `~/ai-tools/thread.py` is not run or imported; the native sensor
      must ignore `origin/mahler/*` and Mahler worktree roots (DESIGN D2).
- [ ] Per-project onboarding checklist, run by an agent:
  - GitHub repo, with its public/private visibility chosen deliberately
  - labels
  - `project.toml` (verify, data, release, environments, canary)
  - backlog migration
  - CLAUDE.md/AGENTS.md section
  - data inventory + first restore drill
  - confirm no external agent dispatcher targets the project
- [ ] Decide whether to onboard the remaining original candidates:
  - [ ] **mental-jukebox**
  - [ ] **puppy-growth-chart**
  - [ ] Other non-git projects, as you choose to activate them
- **Enabled rollout record (not a remaining order):**
  - [x] **movebreak** — already enabled and proven before the formal onboarding audit:
    - live policy uses `./scripts/build_app.sh`, isolated Mahler worktrees,
      `max_parallel = 1`, and the declared personal/work compute accounts
    - the complete Mahler label set exists and agent-built PRs have repeatedly passed CI
      and merged; the original ROADMAP work and issue backlog are complete
    - repository-side policy reconciliation is tracked in movebreak#70
  - [x] **phish-in-app / Couch Tour** — onboarded and running, out of the planned order:
    - single release channel / staging sync backend status not confirmed from this review
    - `gated` status not confirmed
    - local Xcode/Gradle verify with canary checks: `canary` referenced in DESIGN D-table
      and ROADMAP's own onboarding checklist above, not independently verified here
  - [x] **Hockey Draft Copilot** — added outside the original sequence as project `hockey`:
    - live repo is `mkny13/hockey-draft-copilot`; the separate `mkny13/olympic_hockey`
      checkout is not managed by Mahler
    - live verification is `npm test`, with `max_parallel = 1`
    - both agent-instruction files exist; the full `project.toml`, data, release, and restore
      checklist has not been reconciled in this review
  - [x] **Sit Stand Walk** — added outside the original sequence as `sit-stand-walk`:
    - live repo is `makastel_ncstate/sit-stand-walk`, using `./test.sh` for verification
    - both agent-instruction files exist; the full `project.toml`, data, release, and restore
      checklist has not been reconciled in this review
- [x] Android + macOS manual-UAT panels — superseded by D10 (2026-10-06).
      Retain phish-in-app's `UAT.md` history; use ordinary failure capture.
- [x] **Retire the legacy agent dispatchers.** The owner unloaded
      `com.mike.dispatch` on 2026-09-16; `dispatch.py`, the legacy `thread.py` runtime,
      ThreadBar, and Cline Kanban are retired. ThreadBar will not be repointed at Mahler.
      Mahler is the only autonomous dispatcher for managed projects (DESIGN D2).
  - **Manual machine cleanup still required:** verify whether the historically running ThreadBar still needs removal;
    disable every entry in `~/ai-tools/dispatch.toml` (especially `mental-jukebox`); then
    archive or remove the unloaded dispatch plist and update/archive `~/ai-tools` NOTES/TASKS.
    These files and processes live outside this repository and are not changed by a Mahler PR.

**Done when:** every active project runs through Mahler; dispatch and Kanban are off; and no
external scanner or dispatcher acts on Mahler refs or worktrees.

---

## Phase 6 — A deeper feedback loop

- [x] Console walkthrough for Mahler (manually triggered agent UI sweep)
- [x] Playwright screenshot checks against preview URLs, attached to PRs and handoffs.
- [ ] Android emulator + `adb` screencaps; Maestro flows.
- [ ] Runtime error capture → auto-filed issues.
- [ ] **Spike:** can offscreen SwiftUI snapshot tests render while the Mac mini is locked?
      Headless checks remain useful; GUI-driving regression checks use the D40 desktop
      gate. Unavailable checks are reported as coverage gaps, not owner verdict tasks.
- [ ] Self-hosted GitHub Actions runner vs local-only verify for Apple/Android. (couch-tour is
      a public repo, so its GitHub-hosted macOS minutes are free.)

---

## Phase 7 — Adversarial cross-product review

**Status: shipped in substance (mahler#395, DESIGN D11).** The second BACKLOG idea:
"product" = platform. Between green CI and merge, `size:m`, `size:l` and risk-keyword items
get one review run on a different platform (`recipes/review.md`). Findings feed back as a
fix round, with a blocking bar (mahler#499) and a convergence check (mahler#474). What is
still open is tuning: keep it only where it pays for itself.

---

## Phase 8 — Extras

- Claude cloud sessions / claude-code-action as extra workers.
- More backends: OpenCode. (Copilot CLI and Kilo landed in mahler#25.)
  - Kiro CLI (mahler#622) and Mistral Vibe (mahler#623): opt-in free-tier builders, size `s`,
    disabled by default and absent from default routes until each is verified end to end.
    Both are installed and signed in on the mini (2026-10-01). Kiro's `KIRO_API_KEY` is paid-only,
    so the free tier runs on a stored `kiro-cli login`.
  - Meta Muse Code was evaluated on 2026-10-01 and not pursued: it has a headless `muse exec` and
    API-key auth, but no bundled free quota was found, and D8 never spends real money.
- Team-plan Claude API credits (mahler#903, DESIGN D41): `api_credits.Gateway`, confirmed
  grants, transactional reservations, crash/retry accounting and confirmed-grant rollover are
  built and tested, account question answered ("Yes, credits only"). The live trial (gated on
  Mike's go-ahead, 2026-10-10) ruled out Cline (no base URL for its `anthropic` provider).
  The harness is now the Agent SDK runner (`sdk_runner/`, `kind = "agent-sdk"`, mahler#918),
  wired into the gateway and platform adapter, installed on the mini, and through the gateway
  in live trial 2 (2026-10-10, $0.0035). Remaining before `enabled = true`: the end-to-end
  trial through a real Mahler run on a work project.
- Goals → automatic breakdown into sub-issues.
- Quota analytics.
- Self-hosted ntfy.

---

## Still open (repository reconciliation, 2026-11-27)

This finite list consolidates the remaining phase targets; it is not a claim that
all are scheduled or required for the shipped service.

- **MCP and interactive integration:** `ask_user`, `report_progress`, `get_context`,
  and the proposed Mahler-aware handoff/pickup skills; the shipped local server has
  seven tools (`mahler/mcp.py`).
- **Recovery:** comment-command undo and platform rollback/redeploy; configured
  PostgreSQL and ledger backups exist, while other store adapters, automated restore
  drills, staging seeding, and mandatory risky-deploy backup receipts remain targets.
- **Intake and feedback:** native anomaly sensing, runtime-error intake, goal breakdown,
  and Android/Maestro capture flows. Existing screenshots and console walkthroughs
  do not establish these integrations.
- **Operator and rollout qualification:** notification-copy review, phone reachability,
  pre-emption journeys, project onboarding/data inventories, external-dispatcher cleanup,
  offsite coverage and secrets audits remain deployment evidence or phase targets.
  Repository tests cannot attest to their live completion.
- **Optional workers and reporting:** cloud workers, OpenCode, self-hosted ntfy and
  further quota analytics remain proposals. Run usage, scorecards and capacity already
  ship; opt-in SDK/Kiro/Vibe code does not establish live login or enablement.
- **Platform experiments:** locked-screen SwiftUI snapshots and Apple/Android runner
  placement remain experiments, subject to the desktop gate for GUI-driving work.


- **Antigravity IDE quota:** whether it shares the CLI's pools. It only matters if you also
  use the IDE interactively.
- **Couch Tour staging data:** what exactly counts as "listening history" (sync backend
  only, or also phish.in account likes), settled at its onboarding.
