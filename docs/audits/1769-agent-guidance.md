# Agent guidance calibration — #1769

Bounded audit of eight physical files, using the assigned source checkout on
2026-11-26. AGENTS.md is a symlink to CLAUDE.md, not a ninth instruction copy.
Counts below are newline counts / UTF-8 bytes (`wc -lc`), before and after this patch.

| File | Before | After | Disposition and rationale |
| --- | ---: | ---: | --- |
| CLAUDE.md | 123 / 7504 | 97 / 5718 | Change: replace duplicated Layout inventory with existing ARCHITECTURE.md link; distinguish assigned source worktree from protected runtime state. |
| recipes/sort.md | 130 / 8862 | unchanged | Keep: admitted context, finite plans, acceptance/access checks, owner categories and relationship preservation are operational contracts. |
| recipes/build.md | 137 / 10400 | unchanged | Keep: local commit/conductor shipping, failing-first verification, test integrity and supervised GUI access. |
| recipes/fix.md | 134 / 10482 | unchanged | Keep: bounded CI retry, diagnosis, no speculative test weakening, local commit handoff and gate marker. |
| recipes/review.md | 236 / 17754 | 236 / 17761 | Change: rule 13 requires gate marker and distinguishes child exit 75; rule 3 names logic, spec compliance and security boundaries instead of a nonexistent high-risk-files list. |
| recipes/design.md | 51 / 4059 | unchanged | Keep; follow-up candidate below: plan-only reviewed-head contract and protected blockers remain necessary. |
| recipes/discuss.md | 40 / 2956 | unchanged | Keep: bound project/account, restricted tools, trusted context, no paid or cross-account fallback, adapter JSON contract. |
| recipes/console_walkthrough.md | 17 / 1085 | unchanged | Keep; follow-up candidate below: manual UAT checklist and pass/fail report, not an autonomous role status. |

## Evidence and limits

- Committed history `6c565a3` (#1747) extracted the manual workflow and introduced
  `mahler/practices_audit.py:MAX_INSTRUCTION_LINES = 150`. `_instructions` resolves
  effective instructions, checks agreement and size, and retains canonical
  participation checks. `tests/test_practices_audit.py` covers 149/150/151 lines
  and pointer targets. Root guidance was already below the approximately 150-line
  warning; no oversized-root incident is claimed. The physical review file includes
  separate claim and PR modes, so its 236 lines are not a root-instruction violation.
- `f48bfa2` (#1737) added `RecipeByteBudgetTests` in `tests/test_recipes.py`:
  fixed-substitution rendered build/fix/review prompts must fit 10,500 UTF-8 bytes.
  This patch adds no enforcement system and increases no limit. Corrected rule 13
  is compressed within that existing budget; status choices remain tested unchanged.
- `mahler history mahler 'GATE-DENIED' --limit 3` succeeded: results were #1173
  build run 12795, review run 12801, and this issue's sort run 15528. The #1173
  snippets support retaining explicit marker evidence and outstanding coverage;
  the sort result is not independent incident evidence. The admitted sorting
  context also cited run 12798, which this bounded search did not return. No
  transcript dumps or secrets are reproduced and no raw issue comments fetched.
- Observed incidents recorded in DESIGN: D18 cites dropped shipping steps on
  groundwork#81 run 17 and mahler#8 run 23; D40 cites couch-tour run 10977 driving
  XCUITest during owner use; D50 cites couch-tour#686 exhausting attempts at the
  lock shield. These justify conductor shipping and supervised desktop access.
  They do not prove that a later review failed because of rule 13's wording.
- Source-level inconsistencies: old review rule 13 said exit 75 means skip,
  contradicting CLAUDE.md and DESIGN D40/D50. `tests/test_finalize.py` explicitly
  rejects a child exit 75 or lock prose as gate-denial evidence; `tests/test_desktop.py`
  checks the denial marker. The blanket old `~/.mahler/` editing ban also included
  assigned source worktrees. The old rule 3 referenced a high-risk-files list absent
  from CLAUDE.md. These are text contradictions, not newly observed runtime failures.
- DESIGN D13/D52 restrict escalation to owner decisions and distinguish external
  waits; D46/D47 establish admitted-context and scoped-account boundaries. Root
  instructions retain those references and the five owner categories verbatim.

## Clutter, style and retained contracts

The removed Layout section was a detailed directory/module inventory. ARCHITECTURE.md
already maps shipped components and README links to it. The short pointer avoids a
second maintained source map. The other seven files have role-specific checklists,
contracts and reading targets rather than directory tree dumps; retain those.

No style-only instruction duplicates an actually configured linter in the audited
scope. Inspected `.github/workflows/ci.yml`, `.mahler/project.toml`, tracked config
filenames and `sdk_runner/pyproject.toml`: verification uses repo_guard, unittest,
randomized and strict runners plus the CI lesson consolidation gate. No configured
Ruff/Black/Flake8/Pylint/ESLint style command was found. Linter names in practices-audit
test fixtures are scanner examples, not this repository's lint configuration.
Test isolation, standard-library constraints, security boundaries, verification and
honest evidence requirements are not cosmetic style and remain intact.

Conductor-owned shipping, admitted context/no ambient credentials, account isolation,
owner-only escalation, test integrity, supervised foreground GUI commands, no unguarded
computer-use and honest outstanding coverage remain. The assigned-worktree exception
explicitly excludes other worktrees and live state/daemon installation. No runtime,
status/JSON schema, prompt-budget, dependency or other-recipe changes are made.

## Additional bounded follow-up candidates (not implemented)

- `recipes/console_walkthrough.md` asks for browser tools without itself stating the
  desktop wrapper requirement. D40 requires a supervised wrapper when tools drive
  the Mac desktop. Inspect its manual invocation context before changing the recipe;
  this is a source-level omission, not evidence an unguarded session occurred.
- `recipes/design.md` requires owner provisioning for missing live/protected access,
  while review rule 2 / D52 allow a documented fallback for permission-gated evidence.
  A later scoped calibration could distinguish genuinely owner-only credentials
  from conductor-owned permissions in design's access paragraph. No observed failure
  is attributed to this wording, and no acceptance requirement is waived here.

## Verification

The new rendered-review test
`DesktopGateRecipeTests.test_review_deferral_requires_gate_marker_not_child_exit`
checks marker-only deferral, child exit 75 as failure and absence of the old skip
instruction, confined to rule 13 rather than unrelated prose. Added before recipe
changes: `python3 -m unittest discover -s tests -p test_recipes.py` ran 75 tests
with exactly this new failure. After correction: the same 75 tests passed.
No pre-existing assertions were removed or relaxed.

Final required check results are recorded below after execution. All evidence is
local/headless; no GUI, production or protected-service verification is required.

Final results (Python 3.14.10; all commands exited 0):

- `python3 -m mahler.repo_guard`: passed.
- `python3 -m unittest discover -s tests`: 4,018 tests, 375.271s, OK (2 existing skips).
- `python3 tests/run_random.py`: 4,018 tests, 371.359s, OK (2 existing skips);
  seed `1063296602`.
- `python3 -X dev tests/run_strict.py`: 4,018 tests, 384.878s,
  OK (2 existing skips).
- Symlink target, 97-line effective instruction count, existing architecture target
  and Markdown link: passed. `git diff --check`: passed.
- Fixed-substitution rendered bytes: build 10,379; fix 10,494; review 10,497
  (unchanged ceiling 10,500). Focused recipe suite: 75 tests, OK.

No tests were disabled for this patch. Existing suite skips remain skips, not passes;
no additional live/GUI coverage is claimed or required. Conductor-owned shipping and
independent review remain outstanding by design.
