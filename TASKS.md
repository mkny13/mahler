# TASKS

## Status

2026-09-14 chat session: a zoom-out review of Mahler's own operating state (not a build task —
research, analysis, and issue-filing only; no application code changed). One doc PR shipped
(ROADMAP.md); everything else is GitHub issues queued for Mahler's own builders. The working
tree here is clean — nothing left mid-flight.

## Next steps

1. Land the two prerequisites before raising `max_parallel` above 1 on **any** project (both
   `p1`, both currently unbuilt):
   - **mahler#211** — adopt GitHub's native merge queue. `main` has zero branch protection
     (confirmed: `gh api repos/mkny13/mahler/branches/main/protection` → 404), so `ship.py`
     can merge a PR whose CI ran against a since-moved base — D19's rebuild-on-conflict only
     catches *textual* git conflicts, not that. This is the higher-leverage of the two; land
     it first.
   - **mahler#210** — the sort recipe's `area:` label instruction has no procedure for
     detecting cross-issue file overlap, hence 0% adoption across all 99 open issues in all 3
     projects. A follow-up comment on the issue proposes a more mechanical fix: have the
     scheduler parse each item's already-recorded `## Plan` file list at schedule time instead
     of relying on the sort LLM to notice and hand-label collisions.
2. Once those land: trial `max_parallel = 2` on **groundwork or couch-tour first**, not mahler
   — same isolation mechanism, but a bad concurrent merge there is recoverable without
   threatening the tool that would have to do the recovering. Owner's call, not a build task
   (it's a live-app blast-radius decision, not a pure code change).
3. Only after that trial holds up: raise mahler's own `max_parallel` (D17) for a second
   self-hosting lane — the thing the owner actually asked about.
4. Independent, lower-urgency, already queued (`p1`/`p2`, no ordering dependency on the above):
   - **mahler#209** (`p1`) — D26 multi-account routing should treat `personal`/`work` as
     equally preferred for dual-use projects (currently `work` is pure fallback-overflow,
     tried only once every personal platform is exhausted — doesn't help mahler's throughput
     either way since `max_parallel=1` caps it to one build regardless, but does mean an idle
     fully-quota'd work platform loses to a barely-available personal free-tier one).
   - **mahler#206** (`p2`) — platform tier/capability assumptions in `config.py` never get
     re-verified, unlike time estimates which already self-calibrate (mahler#59).
   - **mahler#207** (`p2`) — time estimates should cross platform × size (s/m/l), not just
     platform × role; `runs` has no `size` column today.

## Context

- **This worktree's branch (`mahler/roadmap-status-update`) is stale/spent** — it was PR #208,
  already squash-merged and remote-deleted. Don't build on it; start fresh from `origin/main`
  (`git fetch && git checkout -b <new-branch> origin/main`) for any further work, same as this
  session had to do after finding the previous `mahler-backlog-cleanup` branch was already
  shipped as PR #205.
- **Live-data findings behind the issues above**, in case the issues' own bodies aren't enough
  context on resume:
  - `mahler status` right now shows real quota pressure: Claude/Claude-Opus personal both at
    98% weekly (D23 burst already active), several free tiers backing off. Not an action item,
    just why the ready-queue is backed up.
  - Couch Tour (phish-in) has 72 open issues, 21 of them (29%) `Part N of #X` fragments —
    maintenance-pass audits recursively splitting faster than they're worked. This is a live
    symptom of the same problem #210/#211 address, not a separate issue to file.
  - Actual project-onboarding state diverged from ROADMAP's original Phase 5 plan: Couch Tour
    is live; **mental-jukebox, puppy-growth-chart and movebreak are not onboarded** (absent
    from `~/.mahler/config.toml` entirely) — worth the owner deciding explicitly whether
    they're still coming, next time this comes up.
  - Phase 2 (phone-first console) and part of Phase 3 (full MCP tool set: `ask_user`,
    `report_progress`, `get_context`) were never built — not superseded, just never
    prioritized. What's actually running today: a read-only status page (`serve.py`), GitHub
    comment commands, ntfy, and a 7-tool MCP server (`list_items`, `add_item`, `claim`,
    `heartbeat`, `release`, `handoff`, `next_id`). ROADMAP.md now reflects this (merged in
    PR #208) so it shouldn't need re-discovering.
- **Research grounding for #210/#211** (2026-09-14, cited in the issues themselves): bors/Homu
  "Not Rocket Science Rule" origin, GitHub/Mergify merge-queue mechanics, and a 33,596-PR study
  of concurrent-AI-agent merge conflicts (arXiv 2604.03551) — cross-agent file conflicts at
  ~42% vs. ~20% for a single agent, fixed by task-scoping to non-overlapping file sets decided
  *before* work starts, not by resolving conflicts after the fact.
