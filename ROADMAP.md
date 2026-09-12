# Mahler — Roadmap

The build plan for [DESIGN.md](DESIGN.md). Each phase ships something usable on its own and
has a concrete "done when." Later phases are planned in full but will be refined with what
the earlier ones teach. The POC is deliberately rough. Polish comes after it proves the hard
parts.

**Pilot project:** `groundwork`. It's a Next.js app on Vercel + Neon, with CI, Playwright and
preview deploys already in place, so it has the fastest feedback loop. It also has real
personal data, so backups get tested properly.

**Dogfooding:** Mahler's own backlog moves into GitHub Issues on a private `Mahler` repo in
Phase 0. From Phase 3 onward, Mahler builds Mahler.

```
0 Prep & spikes → 1 POC core loop → ◆ checkpoint → 2 Claude Design (Mahler's screens)
→ 3 Intake + UAT loop → 4 Safety hardening → 5 Cutover & rollout
→ 6 Deeper feedback loop → 7 Cross-product review → 8 Extras
```

---

## Phase 0 — Prep and spikes (no Mahler code)

Answer the unknowns that could change the design before building on them. Each spike is
time-boxed and ends with a short write-up in `NOTES.md`: what was tried, what's true, and
what it changes.

- [ ] **Mahler repo on GitHub** (private). Move BACKLOG ideas into issues. Add labels (D4).
- [ ] **S1 — Claude usage in headless runs.** Does `claude -p --output-format stream-json`
      expose rate-limit state? Install the statusline sidecar (writes `rate_limits` to
      `~/.mahler/usage/claude.json`). Confirm the values match `/usage`.
- [ ] **S2 — Antigravity CLI (`agy`).** Install. Test:
  - headless auth on the free account
  - running in a chosen directory
  - `--dangerously-skip-permissions`
  - `/usage` output
  - what quota exhaustion looks like (exit code, message)
  - whether the IDE and the CLI share a quota pool

  If it can't be automated, Antigravity stays manual-handoff and the router treats it as
  opportunistic.
- [ ] **S3 — Cline CLI on free models.** Test:
  - model selection flags
  - headless completion and exit signals
  - rate-limit error shape
  - whether `cline hook` can deliver a yield or denylist (this settles dispatch's open
    `backend_cline()` gated question)
  - one real small task, to judge quality
- [ ] **S4 — Reach from the phone.** `tailscale serve` a hello-world page from the Mac mini.
      Open it on the Pixel. POST to it from a page on a `vercel.app` origin (CORS, mixed
      origins).
- [ ] **S5 — GitHub polling.** Conditional requests (ETag) across the pilot repo's issues,
      comments and PR checks. Confirm near-zero rate-limit cost.
- [ ] **ntfy:** install the app on the Pixel and subscribe to a random topic. Send a test
      push.
- [ ] **groundwork prep:**
  - write `.mahler/project.toml` (verify.fast, verify.full, preview, smoke, release, data)
  - move open TASKS.md items and groundwork's Cline Kanban cards into issues
  - note in TASKS.md/CLAUDE.md that the backlog now lives in Issues
- [ ] **groundwork data baseline:**
  - `pg_dump` the Neon database
  - restore it into a scratch Neon branch and verify row counts
  - confirm Neon's restore window on your plan

**Done when:** all five spikes have a written answer. groundwork's backlog is in Issues. A
Neon backup has been restored successfully once. Any spike result that contradicts DESIGN.md
has been folded back into it.

---

## Phase 1 — POC: the core loop on groundwork

Prove the parts nobody else sells: leases, handoff across platforms, quota-aware routing,
and you pre-empting a robot. This phase has no UI beyond a status page and pings. It is
command-line and plain HTML.

1. **Ledger + leases.**
   - SQLite schema (D5)
   - `mahler` CLI: `sync`, `list`, `claim`, `heartbeat`, `release`, `handoff`, `next-id`
   - compare-and-set claims with epochs
   - tests that simulate the clock: expiry, zombie runs fenced by the epoch, interactive
     pre-empting auto, auto-vs-auto contention (it must be impossible)

   This is the one part that gets serious tests at POC stage.
2. **Runner, single platform (Claude, for reliability).**
   - worktree on `mahler/<issue>-<slug>`, launch, heartbeat, stream-json log, 60-minute
     limit
   - on exit: automatic snapshot + handoff note
   - PR with `Fixes #N`, CI wait with the failing log tail, merge, close, register the Vercel
     deploy
   - the epoch `pre-push` hook
3. **Scheduler.**
   - ready items by priority; settle delay; concurrency caps; hot hold
   - `mahlerd` under launchd (`com.mike.mahler`)
   - `mahler pause`
4. **Sorting recipe.** Claude turns a raw issue into the template, sizes it, and either marks
   it `ready` or asks one `needs-you` question.
5. **Second and third platforms.**
   - add Antigravity and/or Cline, whichever S2/S3 cleared
   - add the usage probes and routing policy (free tiers first; Claude under 60% / 70%)
   - **forced handoff test:** lower a threshold mid-run and watch the item finish on another
     platform from the same branch
6. **Interactive participation.**
   - a minimal MCP server (`add_item`, `claim`, `heartbeat`, `handoff`, `list_items`)
   - Claude `SessionStart` + `PreToolUse` hooks
   - the Mahler section in groundwork's CLAUDE.md
   - **pre-emption test:** open a chat (from the phone, via Remote Control) on an item an
     agent holds; confirm the agent yields and the chat continues its branch
7. **Pings + status page.** ntfy for needs-you / shipped / handoff / failed. A read-only
   status page over `tailscale serve`: running work, quota gauges, recent events.

**Done when**, on groundwork over one real week:

- at least 5 items go issue → sorted → built → CI green → merged → deployed with no manual
  steps
- at least 1 item completes after a cross-platform handoff
- at least 1 pre-emption by a phone chat, with no lost work
- zero double assignments
- zero autonomous extra-usage spend
- Pause all works

dispatch keeps running for all other projects throughout. groundwork is disabled in
`dispatch.toml` for the duration.

### ◆ Checkpoint after the POC

A short review session before investing in UI:

- What broke? What were the real quota numbers?
- Were the free models good enough, and how often did work escalate to Claude?
- Were the lease TTLs right?
- Does anything push toward adopting Gas City or keeping Cline Kanban after all?
  (Re-evaluate honestly with real data.)

DESIGN.md gets updated with the answers.

---

## Phase 2 — Claude Design: Mahler's own screens

Your request: a design phase after the POC for Mahler's own interfaces. The POC's status
page is functional and ugly on purpose. This phase decides what the real thing looks like
before anyone builds it.

- [ ] **Console, phone-first:**
  - **Capture:** text, voice via the keyboard, photo or screenshot, project picker
  - **Needs you:** one-tap answers
  - **Ready to test**
  - **Now:** running work, quota gauges, handoffs
  - **Backlog:** per project, reorder, park
  - **History:** with Undo
  - **Pause all**
  - the same console on the Mac's larger screen
- [ ] **In-app UAT panel:** the web overlay (what's new in this build; pass/fail/note;
      screenshot; report a problem here). Then the Android and macOS equivalents, based on the
      existing Feedback buttons.
- [ ] **Notification copy:** what each ntfy ping says, and where tapping lands.
- [ ] Built as a Claude Design canvas (`/design`), revised with you until you're happy.
      Output: the approved canvas + a short component spec the build phases follow.

**Done when:** you've approved the console and the web UAT panel designs. Android/macOS panel
designs can trail into Phase 5.

---

## Phase 3 — Intake and the UAT loop (first "real" version)

Close the loop so you can run a project from your phone without opening a laptop. Mahler
starts building itself: its own issues are in scope from here.

- [ ] Console built to the Phase 2 designs, served over Tailscale. Capture creates issues
      (with photo attachments), and "Needs you" answers post as issue comments.
- [ ] Full MCP tool set (`ask_user`, `report_progress`, `next_id`, `get_context`), registered
      in Claude Code, Cline and Antigravity. `/mahler` skill. The `handoff`/`pickup` skills
      become Mahler-aware.
- [ ] GitHub comment commands: `/mahler go | park | platform <x> | undo`.
- [ ] Build registration on each deploy. The `uat-author` recipe turns each shipped issue's
      "needs a human to check" section into UAT items.
- [ ] `mahler-uat.js` web panel in groundwork previews. A fail reopens the issue or opens a
      linked p1 bug with your note and screenshot. Offline queue + GitHub-URL fallback.
- [ ] Daily digest ping: what shipped, what's waiting on you, quota left.

**Done when:** you log a groundwork bug from your phone, it gets fixed and deployed, the ping
arrives, you check it in the in-app panel and mark it passed, all without touching a laptop.
Also, a failed UAT check has turned into a fix at least once, with no action from you beyond
tapping "fails".

---

## Phase 4 — Safety hardening (before any second project)

Make "fast and unreviewed" safe to spread across the portfolio.

- [ ] Data inventory format and nightly backup jobs (`pg_dump`, `wrangler d1 export`,
      `sqlite3 .backup`) into `/Volumes/ExtSSD160/mahler-backups/`, with retention. Monthly
      automated restore drills, whose results appear in the digest.
- [ ] **Backup receipt before risky deploys:** a PR touching migrations, schema or
      data-writing scripts can't deploy until a fresh backup has been proven to restore.
- [ ] Migrations tested against a copy of real data (Neon branch; restored D1 copy).
- [ ] Guardrail hooks: Claude `PreToolUse` denylist, agy permission rules, Cline equivalent
      per S3.
- [ ] **Undo:** a revert PR plus the platform rollback recorded in `project.toml`, from the
      console or `/mahler undo`.
- [ ] Time Machine: include `/Volumes/ExtSSD160/scripts`, exclude `node_modules` and build
      output. Exclude `.mahler-worktrees` from Backblaze. `mahler.db` nightly `.backup`.
- [ ] Secrets audit: no secrets in prompts, issues, pings or logs. Keychain/.env only.

**Done when:** a deliberately bad migration on groundwork is caught before it can deploy, or
rolled back with its data intact. One-tap Undo has reverted a real change end to end. A
restore drill has passed for every groundwork data store.

---

## Phase 5 — Cutover and rollout

Retire the old tools and bring the portfolio in, one project at a time.

- [ ] **thread as a sensor:** anomaly flags become `type:anomaly` issues, deduped by
      fingerprint.
- [ ] Per-project onboarding checklist, run by an agent:
  - private GitHub repo
  - labels
  - `.mahler/project.toml` (verify/data/release/canary)
  - backlog migration (TASKS.md Now/Next, ROADMAP build order, UAT `[!]`, Kanban cards)
  - Mahler section in CLAUDE.md/AGENTS.md
  - data inventory + first restore drill
  - disable in `dispatch.toml`
- [ ] Onboard in this order:
  1. **mental-jukebox** (local Python; "deploy" = fast-forward the primary checkout)
  2. **puppy-growth-chart** (Vite + Cloudflare Worker)
  3. **movebreak** (macOS menu-bar app; GitHub Releases auto-update)
  4. **phish-in-app / Couch Tour** (Android + macOS + D1 sync backend; lift the `gated`
     tier; decide its beta → production handling; local Xcode/Gradle verify with canary
     checks)
  5. Non-git projects, as you choose to activate them
- [ ] Android + macOS UAT panels (from the Phase 2 designs; evolve the existing Feedback
      buttons). Migrate phish-in-app's `UAT.md` history into Mahler.
- [ ] Retire the `com.mike.dispatch` launchd timer and Cline Kanban. Point ThreadBar at
      Mahler's API, or retire it.
- [ ] Update `~/ai-tools/NOTES.md` and TASKS.md to say dispatch is superseded, and why.

**Done when:** every active project runs through Mahler, dispatch and Kanban are off, and
there's one backlog view across all projects.

---

## Phase 6 — A deeper feedback loop

- [ ] Playwright screenshot checks against preview URLs, attached to PRs and handoffs.
- [ ] Android emulator + `adb` screencaps; Maestro flows for key screens.
- [ ] Runtime error capture (Vercel/Worker logs or Sentry free tier) → auto-filed issues.
- [ ] **Your decision:** allow an unlocked, logged-in GUI session on the Mac mini so agents can
      click through macOS apps (XCUITest). This is a home-security trade-off.
- [ ] Decide on a self-hosted GitHub Actions runner on the Mac mini, versus local-only
      verify, for Apple/Android jobs.

**Done when:** agents catch at least one visual or runtime regression before you do.

---

## Phase 7 — Adversarial cross-product review

The second BACKLOG idea. Phase 1–3 review is "a different platform glances at the diff."
This phase makes review adversarial: the reviewer is prompted to break the change, not
approve it.

- [ ] Resolve the BACKLOG's open questions:
  - "product" = platform
  - trigger: default for `m`/`l` items and anything touching data; opt-in otherwise
  - findings feed back to the same item as a fix round
  - verdict precedence against verify results
- [ ] Measure: defects caught vs quota spent. Keep it only where it pays for itself.

---

## Phase 8 — Extras (pull in when wanted)

- Claude cloud sessions / claude-code-action as extra workers for GitHub-hosted repos.
- More backends: OpenCode free models, GitHub Copilot CLI.
- Goals → automatic breakdown into sub-issues, with a weekly "is this still the plan?" check.
- Quota analytics: which platform delivers the most shipped items per unit of quota.
- Self-hosted ntfy; home-screen console with Android Chrome notifications as a second channel.

---

## Still open (not blocking the POC)

- **macOS UI automation / unlocked session:** Phase 6, your call.
- **phish-in-app beta vs production** once its gate is lifted: at its Phase 5 onboarding.
- **Antigravity fallback mode:** depends on S2.
