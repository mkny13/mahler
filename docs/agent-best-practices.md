# Agent best practices (cross-project reference)

Last researched: 2026-11-26. Copied from Macrocosm's `docs/agents/BEST_PRACTICES.md`, where a monthly scheduled task refreshes it; this copy is generalized for any Mahler-managed project and adds a section on what Mahler itself could adopt.

## 1. Distilled rules (project-agnostic)

1. **Verify before "done".** Every change ships with a check the agent ran itself (the project's `verify`). "Looks right" is not done. Gates in CI and hooks beat prose rules.
2. **Separate the doer from the judge.** Never weaken, skip, or delete a test to get green; call out any test change in the PR body. CI and contract/golden tests are the judge, outside the agent's casual edit set.
3. **Test-first for behavior.** Failing test, then code. Property tests for invariants; fixed seeds so failures reproduce.
4. **One issue, one PR, one session.** If the diff can't be described in one sentence, plan first. Split anything much over ~400 changed lines.
5. **Explore, plan, code, verify, commit.** Architecture-affecting choices get a dated decision record before the code.
6. **Enforce boundaries with tools, not prose.** Layering and purity rules become lint/import checks in CI so drift fails the build.
7. **Keep AGENTS.md/CLAUDE.md short and hand-written.** Pointers, commands, gotchas; detail in linked docs. Each line must prevent a real mistake. Must-hold rules become hooks or CI checks.
8. **The repo is the memory.** Decisions, plans, progress, open questions live in the repo and issues, never only in chat.
9. **Look it up, don't recall.** Check current official docs/changelogs before using a library API or choosing a version; pin versions; prefer maintained OSS over custom code.
10. **Fight entropy.** Prefer data-driven code over growing if/else chains, refactor at the third near-duplicate, fail CI on complexity/duplication regressions.
11. **Guard the context window.** Fresh session per task; after two failed fixes, stop and re-plan; push broad research into subagents.
12. **Get an independent review.** Non-trivial PRs get a second-agent review for correctness and requirement gaps, not style.
13. **The owner steers by using the product.** Ship small usable increments, report outcomes in plain language, escalate only product, money, credentials, data policy, or irreversible steps.
14. **Prune the rules.** Re-check instructions after model or tool releases; drop rules that only compensated for old model limits.

## 2. Adoption candidates for Mahler

Mahler already covers much of this: failing-first tests and bug escape analysis (`recipes/build.md`, `fix.md`), independent review with a base-revert run of changed tests (`recipes/review.md`), task sizing in `recipes/sort.md`, the verify contract, lessons (D44), and periodic maintenance passes including agent guidance (D20) and the practices audit (D39). Gaps worth considering, most valuable first. Each would go through the normal issue pipeline; none is adopted by this doc.

1. **Test-tampering guard (rule 2).** Review checks that changed tests exercise behavior, but nothing explicitly flags a PR that deletes tests, adds skips/`xfail`/`.only`, or loosens assertions outside the issue's scope. Options: a line in `build.md`/`fix.md` ("never weaken, skip, or delete a test to pass; explain any test change"), a blocking item in `review.md`, and/or a seventh D39 check (`test-integrity`) proposing a CI diff-guard per project. Sources 4 and 6.
2. **Quality-regression gates (rule 10).** The code-health pass is a periodic review, not a gate. A D39 check could ask whether PR CI fails on complexity or duplication regressions, proposing report-only first, then blocking. Source 8.
3. **Instruction-file budget (rule 7).** Extend `agent-instructions` (D39) or the guidance pass to flag AGENTS.md files well over ~150 lines, directory dumps, and style rules a linter already enforces. Mahler's own CLAUDE.md is 174 lines; the by-hand lease/merge protocol could move to `docs/` with a pointer, since Mahler runs never need it. Sources 1, 2, 5.
4. **Model-release trigger for the guidance pass (rule 14).** D20 runs on 30 days or 20 merged PRs. Anthropic recommends re-reviewing configuration after model releases and dropping rules that compensated for older limits. The platform audit (D20, mahler#206) could file a guidance pass when a platform's default model changes. Source 2.
5. **Look-it-up rule (rule 9).** Rule 2b already requires explaining dependency additions. Extend it: when using a third-party API or choosing a version, check current official docs/changelog and cite the source in the completion summary. Source: Mike's standing project rules.
6. **Within-run re-plan (rule 11).** Issue-level attempts cap at 3 before needs-you. Inside a run, `fix.md` could say: after two failed approaches, stop, record what was tried and why it failed, and end with the evidence rather than thrashing. Source 1.

## 3. Findings by theme

### Verification is the core loop
- Anthropic's Claude Code best-practices page calls giving Claude a runnable check (tests, build, linter, screenshot) the single highest-leverage practice, and lists escalating strengths: prompt, goal condition, Stop hook that blocks until the check passes, verification subagent. [1]
- SkyWalking's maintainers: "Never let AI code without a test harness." Write the test contract first, then let the agent implement; E2E suite as the final gate on every PR. [4]

### Agents game tests they can edit
- Reported reward-hacking patterns: trivial asserts, disabled branches, exiting the runner early. Remedies: tests outside the agent's edit set (PreToolUse hook or read-only dirs), a CI diff-guard that flags test edits in feature PRs, and a holdout set only CI runs. [6]
- SkyWalking saw agents write tests whose expected values matched wrong behavior; a second model caught it. Specs check outputs, not design, so the implementation path still needs review. [4]
- Field notes (2025) go further: humans write tests, AI suggests scenarios. We adopt the softer version (rule 2) because Mike doesn't review code; CI guards plus independent review stand in. [7]

### Plan before coding, size tasks small
- Explore, plan, code; skip the plan only when the diff fits in one sentence. Interview-then-spec, then implement in a fresh session. [1]
- Human (here: Mike plus DECISIONS.md) owns scope, invariants, and boundaries before any agent writes code; interrupt and re-plan on drift. More agents or tokens don't fix a wrong direction. [4]
- One task, one session. [7]

### CLAUDE.md / AGENTS.md
- Short; prune; check into git; emphasize lines that get skipped; hooks for must-hold rules; skills for on-demand knowledge. [1]
- Root file holds pointers and critical gotchas; subdirectory CLAUDE.md files for local conventions; scope test commands per area; deny generated/build files in `.claude/settings.json`. [2]
- Each line costs on every task. Include WHAT/WHY/HOW (stack, purpose, commands); exclude directory dumps and style rules a linter enforces; link detail docs; prefer `file:line` pointers over copied code; write it by hand, not with `/init`. Reports a study finding auto-generated context files slightly lowered success and raised cost. [5]

### Maintainable architecture and drift
- Agents take the locally safest move (copy-paste, another `elif`), producing huge files and high complexity. "The environment was the bug, not the agent": set up the feedback loop before features. Tiered gates: lint with complexity rules, strict types, coverage, duplication/dead-code checks, then architecture/import tests and mutation testing. Fail on regressions. Gates catch slop, not bad design. [8]
- For TypeScript the equivalents are Biome/ESLint complexity rules, `tsc --strict`, jscpd for duplication, knip for dead code, dependency-cruiser for import boundaries, Stryker for mutation testing. (Tool mapping is ours; verify current versions before adopting.)

### Context management
- Context is the main constraint: clear between unrelated tasks, restart after two failed corrections, use subagents for investigation. Named failure modes: kitchen-sink session, over-specified CLAUDE.md, trust-then-verify gap, infinite exploration. [1]
- Agentic search over live code beats stale indexes, given a good CLAUDE.md map. Re-review configuration every 3 to 6 months and after model releases, dropping rules that compensated for old limits. [2]

### Agentic engineering vs. vibe coding
- Willison distinguishes "agentic engineering" (professionals directing agents that write and run code, with tests such as red/green TDD) from unreviewed vibe coding, and keeps his patterns as a living guide. [3] Our setup is a hybrid: Mike directs product, agents own engineering discipline.

## 4. Sources
1. Anthropic, "Best practices for Claude Code" (docs, current as of 2026-11): https://code.claude.com/docs/en/best-practices
2. Anthropic, "How Claude Code works in large codebases" (2026-05-14): https://claude.com/blog/how-claude-code-works-in-large-codebases-best-practices-and-where-to-start
3. Simon Willison, "Agentic Engineering Patterns" (2026-02-23): https://simonwillison.net/2026/Feb/23/agentic-engineering-patterns/
4. Apache SkyWalking, "Agentic Vibe Coding in a Mature OSS Project" (2026-03-08): https://skywalking.apache.org/blog/2026-03-08-agentic-vibe-coding/
5. Philipp Schmid, "Writing a Good AGENTS.md" (2026-02-24): https://www.philschmid.de/writing-good-agents
6. "Your AI agent will pass any test it's allowed to edit" (dev.to, 2026): https://dev.to/penloom_studio_829b7817d3/your-ai-agent-will-pass-any-test-its-allowed-to-edit-51fo
7. "Field notes from shipping real code with Claude" (LessWrong, 2025-06-14): https://www.lesswrong.com/posts/dxiConBZTd33sFaRC/field-notes-from-shipping-real-code-with-claude
8. "Lessons from three months of vibe coding" (dev.to, 2026): https://dev.to/maxkrivich/lessons-from-three-months-of-vibe-coding-and-a-complexity-score-of-53-3bdj

## 5. Refresh log
- 2026-11-26: initial research (sources 1 to 8).
