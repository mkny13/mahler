# Mahler — Backlog / Future Work

Ideas captured before implementation exists. Mahler is the working name for the
dispatch/threads orchestration tool.

## Adversarial cross-product review

Add an option where one product's agent reviews another product's agent's work
adversarially (e.g. agent A critiques/red-teams the output or diff produced by
agent B), rather than only self-review or a single reviewer persona.

Open questions, resolved so far (2026-09-21):
- ~~How "product" is scoped~~ — resolved: "product" = platform (ROADMAP Phase 7).
- ~~Trigger~~ — resolved: default for `m`/`l` items and anything touching data
  (ROADMAP Phase 7).
- ~~Output shape~~ — resolved: findings feed back as a fix round (ROADMAP
  Phase 7).
- ~~How this composes with existing single-agent review flows~~ — resolved:
  it's the same D11 "review by a different platform" step, not a second
  parallel flow. "Adversarial" describes the *framing* of that one review
  (instructed to disprove/red-team, per the best-practice notes below) —
  there is still exactly one reviewer per item, so there's no double review
  pass and no verdicts to reconcile between reviewers. This also follows
  from the debate-amplifies-bias note below: two agents arguing would be the
  failure mode, not the design. A finding the builder disagrees with is
  handled the already-resolved way — it feeds back as a fix round to the
  same build agent, per "output shape" above, not a debate with the reviewer.

Best-practice notes from research (2026-09-21), to apply when this is designed:
- **Structural separation, not just prompting.** A same-model reviewer (even in
  a fresh session/persona) tends to rephrase the builder's own assumptions
  rather than surface a genuine second opinion. The independence has to come
  from a different model *family/provider* from the builder, not just a
  different context window — confirms D11's "review by a different platform"
  framing over a same-model self-review persona.
- **Debate-style multi-model setups can amplify bias rather than cancel it.**
  Research on LLM-as-judge recommends a meta-judge / stage-gated pass over
  open debate between models for this reason — relevant to how findings get
  reconciled if builder and reviewer disagree (the "output shape" question
  above).
- **Reference-guided, not open-ended.** Give the reviewer the issue/acceptance
  criteria as the grading reference, rather than asking it to freely judge
  "is this good code" — reduces inconsistency and matches Mahler's own
  verify-contract style (D11) of checking against a declared spec.
- **Adversarial framing beats a vibes verdict.** Explicitly instruct the
  reviewer to try to disprove/red-team the diff and require concrete
  evidence (a failing case, a spec mismatch) for any finding, rather than a
  holistic approve/reject — mirrors the "Refute-or-Promote" stage-gated
  pattern (cross-model critic + adversarial disprove step + mandatory
  empirical validation before a finding counts).
- **LLM judges are weak at "does this code actually work" without running
  it.** The model review is a complement to CI/tests, not a substitute —
  keep D18's "trust but verify" (real CI/build/smoke signals) as the hard
  gate, with cross-model review as an additional filter on top, mainly
  useful for logic/spec-mismatch issues tests didn't cover.
- **Cost tiering.** Matches Phase 7's existing note (free tiers preferred,
  Claude reserved for data/migration items) — frontier/paid models are worth
  reserving for calibration or high-risk items, not every routine review.

Status: not started — logged 2026-09-12. Slotted as ROADMAP Phase 7; its hook point is
the different-platform review step in DESIGN.md D11.

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

Status: designed 2026-09-12 — see [DESIGN.md](DESIGN.md) (D2 supersede/absorb, D6
ownership/locking, D7 staleness clocks; the name "Mahler" is kept — a conductor, which is
what it now does) and
[ROADMAP.md](ROADMAP.md) for the build order.
