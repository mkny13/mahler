# TASKS

## Status

2026-09-15 chat session: Mike asked for two console UX improvements — clicking into the
0-runs "no headroom" summary rows to see the actual items, and a visual (dependency-graph-ish)
way to see parent/child and blocker state across the backlog at a glance. Per standing
guidance, this was a plan-and-hand-off session, not a build session: no application code
changed. Everything is shipped or filed. The working tree here is clean — nothing left
mid-flight.

## Next steps

Nothing pending from this session. For context on resume, Mahler's own builders will pick up
the two filed issues on their own (`mahler:ready`, `area:console`, `max_parallel=1` on this
repo so they land one at a time):

- **mahler#269** (p2, size:s) — hold-reason rows in the Now/0-runs block expand client-side to
  the `ref — title` items they aggregate, each linking to its GitHub issue.
- **mahler#270** (p2, size:m) — a `List`/`Graph` toggle in the desktop Backlog view; Graph mode
  renders a per-project ranked SVG (parent→child solid edges, depends-on dashed edges, node
  color = state), no client graph library. Desktop only — phone Browse is unchanged.

## Context

- **This worktree's branch (`claude/dependency-graph-visual-df2372`) is already spent.** It
  carried one docs-only commit — [DESIGN.md](DESIGN.md) D29, recording the design rationale for
  both issues above — which shipped as PR #268 (squash-merged to `main`). Don't build feature
  code on this branch; a fresh worktree off `origin/main` picks up D29 automatically once
  Mahler starts on #269/#270.
- **Why a graph and not a literal force-directed layout** (the reasoning behind D29, in case
  the issue bodies aren't enough on resume): couch-tour alone runs 25+ open items under several
  `parent` trackers, and a spatial node-link diagram at that density fights the console's
  "no shadows, no filled cards" minimalism. A rank-by-longest-path SVG, computed server-side in
  `state.py`, keeps this inside D27's "standard library, no client dependency" rule and stays
  legible — but nobody's screenshotted it against couch-tour's real backlog yet (flagged as
  "Needs a human to check" on mahler#270).
- **Scope note:** this session found no other console issue already covering this (checked open
  issues under `area:console` before filing) — #269/#270 are genuinely new, not duplicates.
