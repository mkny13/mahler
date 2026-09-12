# TASKS.md

Last updated by: Claude (bootstrap session), 2026-09-12

## Now

## Status
Phase B bootstrap kernel is built, pushed (`mkny13/mahler`, private) and running under
launchd (`com.mike.mahler`, every 60s, from the pinned clone `~/.mahler/app`). Managed
projects: `mahler` (all issues) and `groundwork` (only issues labelled `mahler`: #80, #81;
production data off-limits by config rule until Phase 4). groundwork is disabled in
`~/ai-tools/dispatch.toml`. The backlog lives in GitHub Issues now, not in this file.

## Next steps
- Watch the first end-to-end runs: mahler#1 (build on agy-claude → PR #2), groundwork#80/#81.
- Mahler works mahler#3–#8 itself (hooks, MCP server, status page, digest, janitor, setup errors).
- Owner: subscribe to the ntfy topic in `~/.mahler/config.toml`; rotate the groundwork Neon
  password (it was printed in a local session transcript on 2026-09-12).

## Context
- `mahler status` / `mahler usage --probe` / `mahler pause` are the controls; `mahler log <run>`
  summarises a run. Tick output: `~/.mahler/logs/tick.log`; self-updates: `logs/update.log`.
- Don't edit `~/.mahler/app` by hand — merge to main; the launcher updates on green CI.
- Claude usage is read for free from the OAuth usage endpoint (Claude Code's keychain token);
  agy from `agy -p /usage`; Cline is unmetered (backs off on rate-limit errors).
