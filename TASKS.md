# TASKS.md

Last updated by: Claude (mahler#16 ship + #27 stale bases), 2026-09-12

## Now

## Status
Mahler's Phase B kernel is live under launchd (`com.mike.mahler`, every 60s, self-updating
from `~/.mahler/app` on green CI). It builds its own backlog, currently all on Cline's free
tier: #5 status page and #6 daily digest shipped this evening. mahler#12 is resolved: the
Cline "hang" was an unseen macOS Documents-access dialog, and #13 made silent starts
detected, pinged and reaped (DESIGN D8). DESIGN D18 was decided: agents build, and Mahler
itself opens, watches and merges PRs. The backlog lives in GitHub Issues, not here.

## Next steps
1. **Watch the conductor ship on its own.** #16 (the conductor, PR #24) and #27 (DESIGN
   D19, PR #28) are merged and the daemon runs them. #20 and #8 had built on stale bases,
   so they were re-queued with `/mahler go` (2026-09-12). Check that they build one at a
   time (only one mahler item `working`/`verifying` in `mahler status`), and that each PR
   opens and merges without anyone's help. Then #17 (no status line + green verify = DONE)
   and #18 (red CI → fix run). A red PR now pauses that project's builds and pings once.
2. groundwork#81 is ready and pinned by the `platform:agy-claude` label. It waits for
   Antigravity's Claude pool to reset, then should back up, migrate and seed production,
   and merge PR #93. Check that it did. If it failed again, read the handoff comment.
3. mahler#20: `/mahler platform X` pins are lost on the next sync; use a `platform:X`
   label until it's fixed.
4. The rest of Mahler's queue: #7 janitor, #8 setup errors (run 23's work is on its
   snapshot branch), #9 scheduler fairness.
5. ROADMAP Phase 1 remainder: deploy tracking/smoke checks for groundwork. The owner turns
   on `tailscale serve` for the status page (`mahler serve`, #5 shipped).

## Context
- Controls: `mahler status`, `mahler usage --probe`, `mahler pause` / `resume`,
  `mahler log <run>`, `mahler backup [project]`. Logs are in `~/.mahler/logs/`
  (`tick.log`, `update.log`).
- Config that isn't in git lives in `~/.mahler/config.toml`: projects, ntfy topic, groundwork
  rules, routing overrides. `cline-free` is enabled with `max_size = "l"`.
- Weak-model failure mode: the agent finishes the code, then ends its turn with narration
  ("Now I'll run the backup…") and no tool call, so there's no STATUS line. Cline reports
  that as `finishReason: completed`. Seen on groundwork#81 runs 17 and 29–31, and mahler#8
  run 23. D18 (#15–#18) is the fix.
- An interactive Claude session active in ~/Mahler hot-holds Mahler's own builds
  (`hot_hold_minutes` 20). Expected; builds resume once the session goes quiet.
- After any `brew upgrade` of Python, expect a "runs are stuck at startup" ping: click Allow
  on the Documents-access dialog on the Mac mini (DESIGN D8, mahler#12).
- Leftover Cline hub daemon (PID 36000) was started from an old chat session's scratch
  folder. Harmless; Cline restarts a hub if it dies.
- groundwork scope is label-based: only issues labelled `mahler` are managed.
- The owner should rotate the groundwork Neon password (it was printed in a local session
  transcript on 2026-09-12).
