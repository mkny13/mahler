# Audit: #976 completion criteria (grouped/range dependency PRs and dependency-PR lifecycle)

- Audited source SHA: `9d35a6583588c0f72de888500feaf74082ada5cc` (main, 2026-10-12), Python 3.14.3.
- Method: read merged `mahler/ship.py`, `mahler/gh.py`, `tests/test_ship.py`, `tests/test_gh.py` at that SHA; ran every cited test selector individually, then the four repository gates. Only in-memory ledgers and temp dirs; no `~/.mahler` or live state touched. A test counts only if it asserts the behaviour; closure, PR titles and test names alone were not counted.
- Verdicts: SUPPORTED (named assertion demonstrates it), COVERAGE GAP, DEFECT.

## Merge identities

| Child | PR | Merge SHA | Merged (UTC) | CI |
|---|---|---|---|---|
| #977 | [#1027](https://github.com/mkny13/mahler/pull/1027) | `2f78d07991d2cb26215e7fb44071823c2904b9e1` | 2026-10-12T18:11:39Z | SUCCESS |
| #978 | [#1025](https://github.com/mkny13/mahler/pull/1025) | `e79e977e3478e4ab11863a1f62210a304b8d91f0` | 2026-10-12T14:08:35Z | SUCCESS |
| #979 | [#1024](https://github.com/mkny13/mahler/pull/1024) | `5d7d9dbe3bcd71df9a0787b8f3969aa792e6ca3f` | 2026-10-12T13:03:41Z | SUCCESS |
| #980 | [#1029](https://github.com/mkny13/mahler/pull/1029) | `a54d7c8c65e86b00a8990501d347ff8f89073bd7` | 2026-10-12T19:06:11Z | SUCCESS |

All four merge commits are ancestors of the audited SHA's history (it is `main` after #1044).

Every test selector below is fully qualified and runs as `python3 -m unittest <selector>`.

## Criterion matrix

| # | Criterion | Verdict | Evidence |
|---|---|---|---|
| 1 | #977 is merged and tests the four cited real Dependabot bodies, grouped severity selection, safe pip lower-bound handling, and one-time handling of still-ambiguous metadata. | **SUPPORTED** | Symbols: `ship._dependency_update`, `_dependency_update_full`, `_note_ambiguous_dependency`. **Fixtures:** groundwork#224 `tests.test_ship.DependencyPRTests.test_real_groundwork_224_body` (minor, 17 deps, `dotenv` non-increasing row present); couch-tour#594 `tests.test_ship.DependencyPRTests.test_real_couch_tour_594_body` (minor, includes `com.google.devtools.ksp`, `org.robolectric:robolectric`); hockey-draft-copilot#92 `tests.test_ship.DependencyPRTests.test_real_hockey_92_body` (minor `ws`; changing to 9.0.0 gives major); mental-jukebox#24 `tests.test_ship.DependencyPRTests.test_real_mental_jukebox_24_body` (body alone returns None; with the real title it is minor `flask`). **Grouped severity:** `tests.test_ship.DependencyPRTests.test_grouped_table_and_lines_most_severe_wins`, `tests.test_ship.DependencyPRTests.test_group_with_non_increasing_row_keeps_major`, `tests.test_ship.DependencyPRTests.test_grouped_conflicts_and_incomplete_rows_fail_closed`, `tests.test_ship.DependencyPRTests.test_grouped_major_enters_tracked_item_path`. **Pip lower bounds:** `tests.test_ship.DependencyPRTests.test_pip_range_lower_bound_rule`, `tests.test_ship.DependencyPRTests.test_real_pip_range_title_only_evidence` (upper-bound change, `~=` and no-evidence titles all fail closed). **One-time ambiguous handling:** `tests.test_ship.DependencyPRTests.test_unparseable_pr_pings_once_and_never_merges` (three ticks, one ping, no merge, no issue). Note: the groundwork#224 and couch-tour#594 fixtures are the bodies as stored in the test (table plus the leading `Updates` line; the tail is truncated), not the full GitHub text. Parsing of the table is what the criterion needs, and the tests assert it. |
| 2 | #978 is merged and a red GitHub Actions dependency PR creates one tracked item without being auto-merged; green action adoption remains covered. | **SUPPORTED** | Symbol: `ship._dependency_prs`. `tests.test_ship.DependencyPRTests.test_red_github_actions_pr_tracked_once_never_merged` (two ticks, `create_issue` once, body links `/pull/11` and "Fix the failing checks", PR 11 never in `pr_merge` calls); `tests.test_ship.DependencyPRTests.test_patch_minor_and_actions` (green action PR classified `github-actions`, adopted path still runs; red PR #13 creates exactly one "Fix failing GitHub Actions update" issue); `tests.test_ship.DependencyPRTests.test_rewritten_renovate_action_revision_is_reconsidered` (red action tracked once across a head change). |
| 3 | #979 is merged and a tracked update's confirmed merge closes its original bot PR with a link, without early or repeated closure. | **SUPPORTED** | Symbols: `ship._close_superseded_bot_prs` (keys `dependency-issue:`/`dependency-closed:`), `gh.GH.pr_close`, `GH.dependency_pr_view`, `GH.pr_merge_info`. Early: `tests.test_ship.SupersededBotPRTests.test_no_close_before_shipped`, `tests.test_ship.SupersededBotPRTests.test_no_close_without_merge_evidence`, `tests.test_ship.SupersededBotPRTests.test_untracked_bot_pr_is_not_closed`. Link and once: `tests.test_ship.SupersededBotPRTests.test_closes_once_with_link_and_is_idempotent` (two passes, one `pr_close`, comment contains `#30`). Already closed: `tests.test_ship.SupersededBotPRTests.test_already_closed_bot_pr_is_only_recorded`. Retry after failure without double close: `tests.test_ship.SupersededBotPRTests.test_failure_retries_next_pass`. Comment marker: `tests.test_gh.TestGH.test_pr_close_comments_with_agent_marker`. |
| 4 | #980 is merged and a changed version pair on the same PR is reclassified while unchanged update identities remain deduplicated. | **SUPPORTED** | Symbols: `ship._dependency_identity`, `_dependency_prs`. Reclassified: `tests.test_ship.DependencyPRTests.test_rewritten_major_to_patch_is_reconsidered` (major tracked, then same PR rewritten to patch is adopted), `tests.test_ship.DependencyPRTests.test_rewritten_renovate_action_revision_is_reconsidered`, `tests.test_ship.DependencyPRTests.test_legacy_marker_without_identity_reconsiders_patch`. Deduplicated: `tests.test_ship.DependencyPRTests.test_same_major_pair_not_duplicated_across_sha_change` (new head SHA, same pair: one issue, no merge), `tests.test_ship.DependencyPRTests.test_major_dedup_and_scope`, `tests.test_ship.DependencyPRTests.test_legacy_marker_still_major_stays_suppressed`. |
| 5 | The full repository verification commands in Plan pass. | **SUPPORTED** | Gates table below; repo-guard unit selector `tests.test_repo_guard.TestRepoGuard.test_clean_tracked_list_passes`. |

No COVERAGE GAP or DEFECT found, so no follow-up issues were filed.

## Individually run selectors

Each selector above was run alone as `python3 -m unittest <selector>` at the audited SHA: every selector cited in the matrix printed `OK`.

## Gates (audited SHA)

| Command | Result | Exit |
|---|---|---|
| `python3 -m mahler.repo_guard` | no violations | 0 |
| `python3 -m unittest discover -s tests` | Ran 2770 tests, OK (skipped=1) | 0 |
| `python3 tests/run_random.py` | seed `1365692001`; Ran 2770 tests, OK (skipped=1) | 0 |
| `python3 -X dev tests/run_strict.py` | Ran 2770 tests, OK (skipped=1) | 0 |

Reproduce the random run with `python3 tests/run_random.py 1365692001`.
