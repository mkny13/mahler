# TASKS

## Now

- [ ] unpause: groundwork and phish-in are paused (`max_parallel = 0`, lines marked PAUSED in ~/.mahler/config.toml) until Mahler's efficiency batch lands | next: when #124–#131, #136, #137 are all closed, set both back to `max_parallel = 1`

## Status

The 2026-09-13 Opus session reviewed the Opus routing and the maintenance passes, and decided
D21 (Opus plans, the free tiers build), D22 (Claude peak window) and D23 (burst before a reset),
merged as docs in #122 and #135. The implementation is queued as planned, born-ready issues for
Mahler's builders. Nothing was coded by hand.

## Next steps

1. Let Mahler build the p1 batch, in queue order: #124 (quota-error reset times), #125 (Opus
   planning route), #126 (Plan sections, children born ready), #136 (overage backstop), #137
   (burst). **#137 should merge before 2026-09-15 04:00 UTC** to catch this week's burst
   (weekly resets 09:00 UTC).
2. Then p2: #56, #127–#131, #133, #134. The audits are p3.
3. When the batch is closed, un-pause groundwork and phish-in (see Now). ntfy reminders are
   scheduled for 2026-09-13 21:00 and 2026-09-15 09:00 Eastern.
4. groundwork #108 and #109 (token-economy and guidance passes) wait in the inbox for Opus
   planning, after the weekly reset.

## Context

- **Live config changes** (backup in the session scratchpad, not the repo):
  - `claude-opus` is first in `routing.sort`, as a stopgap until #125 lands.
  - `claude-opus` starts only below 5h 45%.
  - `cline-free` is capped at size m, only so that size:l reaches Opus.
  - groundwork and phish-in are paused.
- **Cline is GLM-5.3-flash** (confirmed in every run log). Its failures were mostly the old
  PR-step tail, the macOS dialog, and its **daily free cap**. It's 3 for 3 on builds since D18.
  Don't cap it for quality.
- **Opus on Pro:** `--model opus` runs `claude-opus-5` inside the plan (`isUsingOverage: false`),
  billed in the same two windows as Sonnet. Pro has no separate Opus window.
- **Peak hours:** reporting says Anthropic lifted the Claude Code peak cut for Pro/Max on
  2026-05-06. D22 keeps the window as a headroom rule at the owner's request.
- **Backlog cleanup done:**
  - Closed duplicate audits: mahler #96, #100–#106 and groundwork #104–#107.
  - Fixed a dependency deadlock (#68 depended on its own parent #58).
  - #70 (the big scheduler refactor) now waits for the whole batch.
  - Removed stale `platform:claude-opus` pins.
  - Created the missing `mahler:*` labels on groundwork and couch-tour.
- **Blocked by the permission classifier:** a self-removing launchd job that would un-pause the
  projects automatically. The ntfy reminders stand in for it.
