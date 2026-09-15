# TASKS

## Status

2026-09-15 chat session: built the operator console from the Claude Design handoff (DESIGN
D27, spec in `docs/console/design.md`) — every read-only view on phone and desktop plus
pause, peak override, clear backoff and digest-seen, merged in #246, #259, #261. The rest is
queued as `area:console` issues; Mahler has already shipped #249 (scheduler holds), #256
(serve self-restart) and #257 (needs-you deep links). Nothing is mid-flight in this session.

## Next steps

1. Set `[serve] public_url = "https://mac-mini.bandicoot-vimba.ts.net"` in
   `~/.mahler/config.toml`, so needs-you pings open the console on the item (#257 is merged
   but does nothing until this is set). Owner action, or ask a session to do it.
2. Let Mahler work the queue (one at a time via `area:console`): #247 (p1: the write queue
   and needs-you answers — #250, #251, #252, #254 depend on it), then #248, #250, #251,
   #252, #253, #254, #255.
3. UAT on the phone as they land: answer a needs-you item (#247), capture with the project
   dropdown (#251), fail a UAT item (#250), revert a harmless merge (#254).
4. #266 (p2): #257's deep link re-applies on every 30s refresh and yanks the view back to
   Needs you — queued; set `public_url` (step 1) only after it lands, or expect that.
5. When #211's merge-queue branch lands, its DESIGN entry must be **D28**, not D27 (noted on
   #211 and #239).

## Context

- **Served at** https://mac-mini.bandicoot-vimba.ts.net/ via `tailscale serve` → launchd job
  `com.mike.mahler.serve` (installed 2026-09-15) → `127.0.0.1:8787`. Since #256 it restarts
  itself when `~/.mahler/app` updates; before that, `launchctl kickstart -k
  gui/$(id -u)/com.mike.mahler.serve` was needed.
- **Owner decisions that override the design** (recorded in `docs/console/design.md`,
  "Changed from the design"): no inbox — Capture picks its project from a dropdown beside the
  text box, one composer, remembers the last project; the hot-hold banner says "You were
  working in <project> with Claude Code…", because presence detects a Claude session, not
  uncommitted edits.
- **Writes** are accepted only from loopback or a Tailscale address, with the
  `X-Mahler-Console: 1` header, JSON and same origin — `serve.host = "0.0.0.0"` in config lets
  the LAN view it but not write.
- `codex` is the personal free tier; `codex-work` is a business plan (`plan` key in
  config.toml, shown on the quota gauge).
- Not taken up from the handoff: surfacing routing order and Claude's tighter thresholds in
  the console (the designer's open question 4) — out of scope unless asked.
- Older open items from the 2026-09-14 review still stand: #239 (hand-rolled pre-merge
  freshness check, since GitHub's merge queue isn't available on personal repos) before any
  `max_parallel > 1`, then #209, #206, #207.
