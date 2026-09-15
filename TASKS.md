# TASKS

## Status

2026-09-15 chat session: operator-side work on the `work` GitHub account's Copilot quota
signal, not a build task — no application code changed, working tree is clean. Set up a
separate `gh` identity for the work account, discovered it can't actually answer the
question it was meant to answer, and filed the finding as a Mahler issue instead of
hand-patching around it.

## Next steps

1. **mahler#265** (just filed) — decide the real fix: either teach `probe_copilot` to detect
   an org-assigned Business/Enterprise seat and fall back cleanly to unmetered, or build a
   manual-usage-import path (the owner has used a hand-exported-usage-file workaround for
   this same GHE limitation on another project). This is a design call for Mahler's builders,
   not something resolved in this session.
2. Carried over from the 2026-09-14 session, still open, no ordering dependency on #265:
   - **mahler#211** (`p1`) — adopt GitHub's native merge queue before raising `max_parallel`
     above 1 on any project; `main` has zero branch protection.
   - **mahler#210** (`p1`) — sort recipe's `area:` label has 0% adoption; needs the scheduler
     to parse each item's `## Plan` file list instead of relying on the sort LLM.
   - **mahler#209** (`p1`), **mahler#206**/**mahler#207** (`p2`) — see issue bodies.

## Context

- **Work `gh` identity is now set up and confirmed working**: `GH_CONFIG_DIR=~/.config/gh-work`
  on `accounts.work.env` in `~/.mahler/config.toml`, logged in as `makastel_ncstate`
  (distinct from personal `mkny13`). This part doesn't need redoing regardless of how #265
  is resolved — it's a genuine prerequisite fix (previously `copilot-work`'s billing probe,
  if ever attempted, would have silently read the *personal* GitHub login).
- **Why `copilot-work` is back to `metered = false`** (config.toml, reverted this session
  after briefly trying to remove it): the work account's Copilot access is an org-assigned
  **Business seat** (`ncstate-libraries`, confirmed via `gh api
  /orgs/ncstate-libraries/copilot/billing/seats`), not a personal Pro/Education plan. The
  `/users/{login}/settings/billing/ai_credit/usage` endpoint mahler's probe calls only
  tracks personal pay-as-you-go AI credits — it returns empty forever for a Business seat
  regardless of real usage (confirmed: the seat showed real `last_activity_at` overnight
  activity from `copilot-cli` while the billing endpoint reported zero). Full writeup in
  mahler#265.
- **mahler#264** was an empty test issue accidentally created while debugging the `mahler
  add` CLI invocation for #265 — already closed, no action needed.
- This worktree's branch (`claude/pickup-e0102c`) has no commits of its own on top of
  `origin/main` — all of this session's work happened outside the git repo (gh auth, a
  config.toml edit on the host, and a GitHub issue). Nothing to build on here; start fresh
  from `origin/main` for any next task.
