# TASKS.md

Last updated by: Claude (mahler#16 ship + #27 stale bases handoff), 2026-09-12

## Now

## Status
The conductor (#16, DESIGN D18) and D19 (#27) are live in the daemon. Mahler now pushes,
opens, watches and merges its own PRs, holds a project's build slot until the change merges,
and starts every build on current `main`. groundwork#81 shipped (PR #93). mahler#20 and #8
had built on stale bases and were re-queued with `/mahler go`. Nothing is in flight by hand.
The backlog lives in GitHub Issues, not here.

## Next steps
1. **Watch the first builds that ship with nobody's help.** mahler#17, #18, #20 and #8 are
   `ready`. Check that only one mahler item is `working` or `verifying` at a time in
   `mahler status`, and that each PR opens and merges on its own. A resumed build's handoff
   comment should say its work was replayed onto current main, or that it started fresh from
   a `mahler/snapshot/<n>-stale-run<id>` branch. #8 will almost certainly start fresh.
2. Then #17 (no status line + green verify = DONE) and #18 (red CI → fix run). Until #18
   lands, a red PR pauses that project's builds and pings once. Fix it by hand or close it.
3. groundwork#81: PR #93 merged. Confirm the production backup, migrate and seed actually ran.
4. mahler#7 janitor is `failed` after 3 tries. Read its handoff comments before re-queuing.
   It should also prune the new `mahler/snapshot/*-stale-run*` branches.
5. ROADMAP Phase 1 remainder: deploy tracking/smoke checks for groundwork. The owner turns
   on `tailscale serve` for the status page (`mahler serve`).

## Context
- **Sessions share `~/Mahler`.** Work in your own worktree (CLAUDE.md). On 2026-09-12 two
  sessions switched branches under each other, and one's commit was reset off `main`
  (recovered from the reflog). Cross-session messages (ListAgents / SendMessage) work for
  coordinating.
- D19 details worth knowing: the conductor sends a `CONFLICTING` PR back to `ready` (no
  attempt counted). A branch left with nothing beyond base is deleted, not pushed, because a
  PR head at base's tip reads as merged and would close the issue.
- New unmetered builders `copilot` and `kilo` (#26, #31; size s only; Copilot first in
  build routing). Kilo must use `kilo/kilo-auto/free`: its default model is paid and 402s.
- Controls: `mahler status`, `mahler usage --probe`, `mahler pause` / `resume`,
  `mahler log <run>`, `mahler backup [project]`. Logs are in `~/.mahler/logs/`
  (`tick.log`, `update.log`).
- Config that isn't in git lives in `~/.mahler/config.toml`: projects, ntfy topic, groundwork
  rules, routing overrides. `cline-free` is enabled with `max_size = "l"`.
- Weak-model failure mode: the agent finishes the code, then ends on narration with no
  STATUS line. D18 (#15–#18) handles it.
- An interactive Claude session active in ~/Mahler hot-holds Mahler's own builds
  (`hot_hold_minutes` 20). Expected; builds resume once the session goes quiet.
- After any `brew upgrade` of Python, expect a "runs are stuck at startup" ping: click Allow
  on the Documents-access dialog on the Mac mini (DESIGN D8, mahler#12).
- groundwork scope is label-based: only issues labelled `mahler` are managed.
- The owner should rotate the groundwork Neon password (it was printed in a local session
  transcript on 2026-09-12).
