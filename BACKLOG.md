# Mahler — Backlog / Future Work

Ideas captured before implementation exists. Mahler is the working name for the
dispatch/threads orchestration tool.

## Adversarial cross-product review

Add an option where one product's agent reviews another product's agent's work
adversarially (e.g. agent A critiques/red-teams the output or diff produced by
agent B), rather than only self-review or a single reviewer persona.

Open questions to resolve when this gets picked up:
- How "product" is scoped — different agent configs/personas, different
  underlying tools/repos, or literally different products in a portfolio?
- Trigger: opt-in flag per dispatch/thread, or a default for certain task types?
- Output shape: findings feed back into the same thread, or spawn a new
  review thread/task?
- How this composes with existing single-agent review flows (avoid double
  work / conflicting verdicts).

Status: not started — logged 2026-09-12 for future design work.

## Proactive backlog execution (this is Mahler's actual reason to exist)

`thread`/`dispatch` (in `~/ai-tools`) only ever react to anomalies — a dead
session, a stale unmerged branch, forgotten WIP commits. They have no concept
of "here's planned work, go do it." An open `TASKS.md` Now item with matching
dirty files is the *expected*, healthy shape of in-progress work, so every
existing flag is specifically designed to leave it alone (`died_mid_task`
requires Now to be empty; `stale_now_item` requires 7+ cold days and zero
dirty files). Confirmed directly, 2026-09-12: `phish-in-app` had two open Now
items sitting untouched, and nothing fired — correctly, by the current
design's own logic.

This is the concrete gap Mahler needs to close. Doing it right needs its own
design, not a bolt-on flag:
- **A staleness signal that isn't 7 days.** "Nobody's picked this up in an
  hour" is a completely different judgment from "nobody's picked this up in a
  week," and the current tools only have the second.
- **A risk model for touching something someone might be mid-edit on** —
  unlike an anomaly (which by definition nobody is actively working), a
  queued Now item could get claimed by its author five minutes after an agent
  starts on it. Needs real ownership/locking, not just dispatch's
  claim-by-fingerprint dedup (which is keyed to *flag identity*, not to
  "someone might start this by hand any second").
- **Whether this subsumes or wraps `thread`/`dispatch`.** They're proven,
  working, and in daily use (`~/ai-tools/NOTES.md`, `~/ai-tools/dispatch.toml`
  has 3 projects live as of this writing) — Mahler should decide deliberately
  whether it's a new layer on top of them, a fork, or a rewrite, not
  accidentally duplicate their anomaly-detection work.
- **Naming**: "dispatch" already overpromises what it does (see
  `~/ai-tools/NOTES.md`) by sounding like general task assignment when it's
  purely reactive. If Mahler does what dispatch's name implies, it should be
  named for that.

Status: not started — logged 2026-09-12, the session that discovered the gap.
