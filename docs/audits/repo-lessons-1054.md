# Audit: #1054 completion criteria (repo lesson candidates)

- Audited source SHA: `de45d181a4135bf2e772bacc709041bba1d74ca5` (main, 2026-10-14; contains all four child merges plus review follow-ups #1067-#1069), Python 3.14.
- Method: read D44 in `DESIGN.md`, merged `mahler/lessons.py`, `runner.py`, `prompt.py`, `finalize.py`, `ledger.py`, `platforms.py`, `tick.py` and the tests below; ran every cited selector individually, then the four gates. Only in-memory ledgers/temp dirs; no `~/.mahler` touched. A test counts only if it asserts the behaviour.
- Verdicts: SUPPORTED, COVERAGE GAP, DEFECT.

## Merge identities

| Child | PR | Merge SHA | Merged (UTC) | CI |
|---|---|---|---|---|
| #1055 | [#1059](https://github.com/mkny13/mahler/pull/1059) | `ba3c332260a7cca69feb1eec71895eb511ce9ba6` | 2026-10-13T19:07:13Z | SUCCESS |
| #1056 | [#1060](https://github.com/mkny13/mahler/pull/1060) | `aef27e29141a58665fcc8f69d3e66b9f80710a7b` | 2026-10-13T20:22:06Z | SUCCESS |
| #1057 | [#1062](https://github.com/mkny13/mahler/pull/1062) | `4082f36cf4a15c1c3aff869043b363b3c7f178df` | 2026-10-13T21:32:19Z | SUCCESS |
| #1058 | [#1063](https://github.com/mkny13/mahler/pull/1063) | `e6f6d7cd7f2cb826f69fc27d6817f85ca46afc05` | 2026-10-13T22:39:03Z | SUCCESS |

## Criterion matrix

Selectors are fully qualified and run as `python3 -m unittest <selector>`.

| # | Criterion | Verdict | Evidence |
|---|---|---|---|
| 1 | #1055 merged with the allocated DESIGN decision and nine-kind compatibility matrix. | **SUPPORTED** | `DESIGN.md` heading `### D44 — Repo lesson candidates: portable, scanned, advisory until gated (mahler#1054, #1055)`, lines 2649-2776 (matrix under "Platform compatibility"). Nine kinds: `codex`, `copilot`, `agy`, `kilo`, `cline`, `vibe` (native `.agents/skills`; vibe trusted folders only) and `claude`, `agent-sdk`, `kiro` (catalog fallback). The code agrees: `tests.test_lessons.PlatformModeTests.test_nine_kinds` asserts exactly these six native and three fallback kinds. |
| 2 | #1056 blocks unsafe lesson contents before launch; native/fallback outcome per kind. | **SUPPORTED** | Symbols: `lessons.scan`, `lessons.mode_for`, `lessons.catalog`, runner prepare. Scanner rules: `tests.test_lessons.ScanTests.test_injection`, `tests.test_lessons.ScanTests.test_hidden_unicode`, `tests.test_lessons.ScanTests.test_exfiltration`, `tests.test_lessons.ScanTests.test_bare_host_and_python_exfiltration`, `tests.test_lessons.ScanTests.test_destructive`, `tests.test_lessons.ScanTests.test_scanner_exception_fails_closed`. Unsafe removed/suppressed before launch: `tests.test_lessons.LoadTests.test_unsafe_base_lessons_removed_and_redacted`, `tests.test_lessons.LoadTests.test_branch_local_and_modified_lessons_suppressed`, `tests.test_lessons.LoadTests.test_scanner_failure_loads_nothing_and_does_not_raise`, `tests.test_runner.LessonLaunchTests.test_prepare_removes_unsafe_lesson`. Per-kind outcome: `tests.test_lessons.PlatformModeTests.test_nine_kinds`, `tests.test_lessons.PlatformModeTests.test_catalog_only_for_fallback_kinds`, `tests.test_runner.LessonLaunchTests.test_prepare_exposes_safe_lessons_and_prompt_catalog`. Recipes: `tests.test_recipes.LessonRecipeTests.test_build_and_fix_gate_lessons_on_recovery_and_patch_first`. |
| 3 | #1057 records only validated, idempotent, project-scoped usage evidence. | **SUPPORTED** | Validated/bound to launch inventory and idempotent: `tests.test_ledger.LessonUsageTests.test_two_lessons_idempotent_and_later_run_increments`, `tests.test_finalize.RunTests.test_lesson_report_binds_to_launch_inventory_and_is_idempotent`. Bounded report: `tests.test_platforms.CreditExhaustionTests.test_lessons_report_is_bounded_and_leaves_status_alone`. Account/project scope: `tests.test_lessons.LoadTests.test_projects_do_not_mix`, `tests.test_finalize.RunTests.test_launch_records_lessons_under_platform_account`, `tests.test_maintenance_queue.LessonsPassTests.test_inventory_uses_all_authorized_accounts_only`. |
| 4 | #1058: 14/30-day boundaries; never deletes; snapshot before consolidation; deduplicated D20 proposals. | **PARTIAL: two parts COVERAGE GAP** | See parts below. |
| 5 | Four standard gates pass on main with all four children. | **SUPPORTED** | Gates table below. |

### Criterion 4, by part

| Part | Verdict | Evidence |
|---|---|---|
| 14-day stale, 30-day archive boundaries | SUPPORTED | `lessons.classify`: `tests.test_lessons.CurationTests.test_boundaries_13_14_29_30`, `tests.test_lessons.CurationTests.test_never_used_ages_from_created_and_updated`, `tests.test_lessons.CurationTests.test_cite_resets_clock_exposure_does_not`, `tests.test_lessons.CurationTests.test_inventory_skips_unsafe`. 13 active, 14 stale, 29 stale, 30 archive-eligible. |
| Never deletes | **COVERAGE GAP** | The curation pass only files a maintenance issue (`tick.queue_maintenance`); `tests.test_maintenance_queue.LessonsPassTests.test_files_with_scope_label_and_resets_checkpoint` checks only that the issue body contains the text "Never delete". No test asserts behaviour (no code path archives or deletes; the only `rmtree` in `lessons.py` suppresses lessons from a launch worktree). |
| Snapshot before consolidation | **COVERAGE GAP** | The same test only checks the issue body contains `skills-archive/_snapshots`. D44 says a consolidation without its snapshot fails "the scanner's PR check", but no test asserts such a check or that a snapshot is written. |
| Deduplicated D20 proposals | SUPPORTED | `tests.test_lessons.PromotionTests.test_threshold_files_one_proposal_and_rerun_reuses`, `tests.test_lessons.PromotionTests.test_below_threshold_and_single_issue_and_old_and_gated`, `tests.test_lessons.PromotionTests.test_closed_proposal_needs_three_new_events`, `tests.test_lessons.PromotionTests.test_closed_count_only_record_allows_replacement_events`, `tests.test_lessons.PromotionTests.test_closed_proposal_identity_survives_aging_out`. |

Every selector listed in this report was run individually at the audited SHA: all 30 unique fully qualified selectors (31 mentions; `tests.test_lessons.PlatformModeTests.test_nine_kinds` appears twice) printed `OK`.

## Gates (audited SHA)

| Command | Result | Exit |
|---|---|---|
| `python3 -m mahler.repo_guard` | no violations | 0 |
| `python3 -m unittest discover -s tests` | Ran 2843 tests, OK (skipped=1) | 0 |
| `python3 tests/run_random.py` | seed `545716440`; Ran 2843 tests, OK (skipped=1) | 0 |
| `python3 -X dev tests/run_strict.py` | Ran 2843 tests, OK (skipped=1) | 0 |

## Follow-ups

Criterion 4 is not ticked. One follow-up covers both gaps (see the #1054 comment for its number).
