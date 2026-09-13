# TASKS

## Now

- [x] mahler#38 (PR #39): Copilot AI Credits billing probe committed, tested, green CI, and squash-merged to main.
- [x] mahler#35 (PR #36): "unknown limit" quota display wording rebased cleanly on main, tested, green CI, and squash-merged to main.
- [ ] Watch autonomous daemon runs on the backlog (mahler#17, #18, #20, #32, #8).

## Status

Shipped both pending items:
1. **Copilot quota probe (mahler#38, PR #39):** GitHub Copilot CLI now has a real quota probe via `gh api /users/<login>/settings/billing/ai_credit/usage` (summing monthly AI Credits against 1500 monthly allotment), with metered routing using a `monthly` window.
2. **Quota display honest wording (mahler#35, PR #36):** Unmetered platforms (cline-free, kilo) now display as `"unknown limit (platform reports no quota signal)"` rather than asserting `"unmetered"`.

## Next steps

1. **Watch the backlog runs:** mahler#17, #18, #20, #32, #8 are `ready`.
2. mahler#7 janitor is `failed` after 3 tries — inspect handoff comments before re-queuing.
3. groundwork#81: PR #93 merged. Confirm production backup, migrate and seed ran.
4. ROADMAP Phase 1 remainder: deploy tracking/smoke checks for groundwork; tailscale serve for status page.

## Context

- **Why `copilot-ai-credits-probe` has staged-but-uncommitted changes:** this session ran low on context/tokens mid-implementation, right after resolving a 3-way merge conflict against `origin/main` (the branch was rebuilt fresh off `origin/main` after discovering the first attempt was accidentally stacked on the old, unrelated PR #36 branch). The merge conflict is fully resolved and tests pass — what's missing is purely the commit/push/PR mechanics.
- **What changed in this branch, concretely:**
  - `mahler/config.py`: Copilot's platform config flips from `metered: False` to `metered: True`, adds `"windows": ["monthly"]` and `"monthly_cap_credits": 1500`, with `soft`/`hard` keyed on `"monthly"` instead of `"5h"`/`"weekly"`.
  - `mahler/router.py`: `usage_state()` now reads `pconf.get("windows", WINDOWS)` instead of the hardcoded global `WINDOWS`, so a platform can have its own window set. (`serve.py`'s gauge-width calc and `scheduler.py`'s quota-hit backoff were updated the same way.)
  - `mahler/platforms.py`: new `probe_copilot(monthly_cap_credits)` — shells to `gh api /users/<login>/settings/billing/ai_credit/usage`, sums `usageItems[].grossQuantity`, and returns a `("monthly", pct, resets_at)` sample. Needs the `user` OAuth scope on the `gh` CLI token (already granted on this machine via `gh auth refresh -h github.com -s user`, done during this session with the user's explicit go-ahead).
  - `mahler/scheduler.py`: `refresh_usage()` gained a branch to probe Copilot when its `usage_state` is stale, at most every `stale_minutes` (360 = 6h, since it's a billing API, not live-critical).
  - `tests/test_routing.py`, `DESIGN.md` (D8 table): updated to match — Copilot is no longer grouped with Cline/Kilo as "unmetered."
- **Important nuance already baked into `main` that this branch had to merge with**: `main` independently reordered `routing.build` so Copilot now comes *before* Kilo (`"cline-free", "copilot", "kilo", "claude"`) — Copilot runs real frontier models (verified `claude-sonnet-5`) despite the smaller monthly budget, so it's preferred over Kilo's grab-bag `:free` models. The merged code preserves that ordering.
- **Issue numbering**: this session's new issue is `mkny13/mahler#38` (not #37 — #37 was already used by an unrelated, since-merged PR). All in-code comments/docstrings correctly say `mahler#38`.
- **Caveat for future maintenance**: the AI-credits billing endpoint only reports *consumption*, never the cap — 1500/month is hardcoded config based on GitHub's published Pro/Education allotment as of 2026-09-12. If GitHub changes that allotment, `monthly_cap_credits` in `mahler/config.py` needs a manual update.
