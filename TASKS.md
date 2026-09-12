# TASKS.md

Last updated by: Claude (bootstrap session handoff), 2026-09-12

## Now

## Status
Mahler's Phase B kernel is live under launchd (`com.mike.mahler`, every 60s, self-updating
from `~/.mahler/app` on green CI). It has shipped its own issues (#1 version command, #3
session hooks, #4 MCP server) and groundwork#80 on free Antigravity quota. Nightly verified
Neon backups for groundwork are running. The groundwork production hold was lifted by the
owner (backup-first rule in config). Cline is paused in routing (mahler#12). The backlog
lives in GitHub Issues, not here.

## Next steps
1. Fix mahler#12 (Cline hangs under launchd), then set `enabled = true` under
   `[platforms.cline-free]` in `~/.mahler/config.toml`. `max_size = "l"` and the
   build order (agy-claude → cline-free → agy-gemini → claude) are already set.
2. Confirm groundwork#81 finishes: `/mahler go` was posted, and the next run should run
   `mahler backup groundwork`, migrate and seed production (`drizzle/0010_*`), then merge
   PR #93. It waits for free quota (Antigravity's 5-hour windows were spent at handoff).
3. Mahler's own queue: #5 status page, #6 daily digest, #7 janitor (retried), #8 setup
   errors, #9 scheduler fairness.
4. ROADMAP Phase 1 remainder: deploy tracking/smoke checks for groundwork; `tailscale serve`
   for the status page once #5 lands (owner turns that on).

## Context
- Controls: `mahler status`, `mahler usage --probe`, `mahler pause` / `resume`,
  `mahler log <run>`, `mahler backup [project]`. Logs are in `~/.mahler/logs/`
  (`tick.log`, `update.log`).
- Config that isn't in git lives in `~/.mahler/config.toml`: projects, ntfy topic, groundwork
  rules, Cline pause, routing overrides.
- Weak-model failure mode seen: Gemini sometimes finishes the code but skips the PR and
  STATUS steps. The snapshot + handoff + retry path recovers it (groundwork#81 run 17 → 19).
- Antigravity's Claude pool (Opus 4.6) exhausted its 5-hour window after roughly 3 runs.
  Sorting was moved to the Gemini pool for that reason.
- groundwork scope is label-based: only issues labelled `mahler` are managed.
- The owner should rotate the groundwork Neon password (it was printed in this session's
  local transcript on 2026-09-12).
