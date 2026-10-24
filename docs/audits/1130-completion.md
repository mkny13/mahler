# Dependency gate completion audit — #1130 / #1233

Audited base: `c6a2b1778053de334a359eaf7a38c58cea67390f`.
Tested code and fixture commit: `483d470bc016cc6446817e1fbd4b746d68ae958f`.
Environment: Python **3.14.3**, macOS, 2026-10-24. The subsequent audit-only
commit records these results; it does not change the tested code.

## Parent criteria

| # | Criterion | Assessment | Evidence and limits |
| --- | --- | --- | --- |
| 1 | Children merged with focused tests | SUPPORTED | Merge ancestry and named child assertions below; all focused tests pass on the tested commit. |
| 2 | Four ecosystems × eight blocking scenarios | COVERAGE GAP | All 32 ship fixture cases pass, including 13-day publication, but Swift/Gradle prove selected-version age, not package first-publication age. See the publication limitation below. |
| 3 | Old clean packages merge; unchanged dependencies make no requests | SUPPORTED | Twelve named clean/unchanged/exact-boundary ship tests, plus four one-second-inside-boundary blockers. Assertions exercise real parsing/vetting and merge requests. |
| 4 | Evidence fields and publisher/source scrutiny | COVERAGE GAP | Deterministic report and rendered-prompt contracts pass. Actual model identification of inconsistent publisher/source metadata is not established by these tests. Gradle supplies a namespace and POM URL, not verified publisher identity or repository metadata. |
| 5 | No installation, credentials, or non-stdlib runtime dependency required | SUPPORTED | Source inspection below, scoped to the dependency checker and its added integration, not Mahler's existing authenticated GitHub operations. No dependencies added. |
| 6 | Five verification commands pass | SUPPORTED | Recorded results below. |

No runtime repair is included in this verification task. Criteria 2 and 4 must
remain unchecked as full parent claims; completing this audit does not mean the
parent is fully supported. There is no owner approval or manual UI check pending.

## Child merge evidence

The admitted issue supplied the GitHub merge metadata and closing-issue mappings.
Local Git confirms each commit is an ancestor of the audited base:
`git merge-base --is-ancestor <commit> c6a2b1778053de334a359eaf7a38c58cea67390f`
returned **0** for all three. `git show --stat` also confirms the named test files
were introduced/extended in the corresponding merge. No raw comments were fetched.

| Child | PR | Merged UTC | Commit | Focused assertions retained and run |
| --- | --- | --- | --- | --- |
| #1137 | [#1230](https://github.com/mkny13/mahler/pull/1230) | 2026-10-24T00:46:06Z | `ae3ad735be79f0fe27cf70ac1569d510855b53a0` | `DependencyVettingTests.test_npm_addition_uses_exact_lock_resolution_and_reports_metadata`, `test_pypi_version_change_is_resolved_from_lock_and_vetted`, `test_missing_lock_resolution_fails_closed_without_registry_calls`; `ShipTests.test_dependency_gate_failures_never_request_merge`; `FreshnessAdapterTests.test_dependency_file_snapshots_only_include_changed_dependency_files` |
| #1138 | [#1231](https://github.com/mkny13/mahler/pull/1231) | 2026-10-24T03:49:09Z | `5d7883dcf6c06c0a3c2840f0d21f1a2f7b0a6518` | `SwiftGradleTests.test_manifest_plus_lock_updates_pass_and_manifest_only_fails`, `test_swift_revision_must_match_release`, `test_wrong_artifact_or_source_metadata_blocks`; `ShipTests.test_swift_gate_runs_before_merge`, `test_gradle_gate_runs_before_merge` |
| #1139 | [#1232](https://github.com/mkny13/mahler/pull/1232) | 2026-10-24T05:38:52Z | `c6a2b1778053de334a359eaf7a38c58cea67390f` | `DependencyRecipeTests.test_build_and_fix_require_dependency_rationale`, `test_review_requires_dependency_diff_and_gate_evidence` |

## Fixture matrix and merge integration

Each test is independently named
`tests.test_ship.ShipTests.test_completion_gate_<ecosystem>_<scenario>`, with
`ecosystem` in `npm`, `pypi`, `swift`, `gradle`. The generator attaches **48**
methods to `ShipTests`, so unittest creates a fresh temporary directory,
in-memory ledger, FakeGH merge list and scoped mocks for every case.

| Scenario suffix | Input/evidence | Required assertion (all four ecosystems pass) |
| --- | --- | --- |
| `nonexistent` | Registry/source HTTP 404 | Gate reached; no merge; evidence failure reported |
| `recent` | Publication 2026-10-07T12:00:00Z | No merge; reason says less than 14 days |
| `high` | OSV-completion HIGH | No merge; advisory ID and severity reported |
| `critical` | OSV-completion CRITICAL | No merge; advisory ID and severity reported |
| `identity` | Unsupported npm GitHub shorthand, unidentified Python wheel URL, Swift pin without source, malformed Gradle coordinate | No merge; evidence failure; zero HTTP calls |
| `version` | Missing npm/PyPI lock resolution, missing Swift version, Gradle dynamic version | No merge; evidence failure; zero HTTP calls |
| `registry` | Required registry/source request raises URLError | No merge; fixture lookup failure reported |
| `osv` | Required OSV request raises URLError | OSV actually reached; no merge; fixture lookup failure reported |
| `clean` | Publication 2020-01-01, exact version, no advisories | Merge request `[88]`; real vetter and OSV called |
| `unchanged` | Same dependencies with whitespace-only file churn | Merge request `[88]`; zero registry/source/OSV requests; vetter skipped |
| `boundary` | Publication 2026-10-06T12:00:00Z | Exactly 14 days: merge request `[88]` |
| `inside_boundary` | Publication 2026-10-06T12:00:01Z | One second younger than 14 days: no merge |

The UTC check clock is fixed at **2026-10-20T12:00:00+00:00**. HTTP is replaced
with fixture responses at `dependency_vetting.urlopen`; `vet` and
`ship._vet_pr_dependencies` spies wrap the real functions, never canned verdicts.
Every case asserts the ship gate was called once. Every metadata case asserts
real `vet(before, after)` ran. Identity/some version errors intentionally fail in
`changed_dependencies` inside the gate, before `vet` or HTTP can run.

Source path: `GH.dependency_files` reads base/head snapshots;
`ship._vet_pr_dependencies` calls `changed_dependencies`, `vet`, and
`Finding.report`, and returns false on evidence errors or blocked findings.
`vet` uses `_metadata` / `_resolved_metadata`, `_advisories`, and the strict
`published > now - timedelta(days=14)` comparison.

### Publication limitation / required technical follow-up

`_metadata` uses the earliest npm time/PyPI upload date. `_resolved_metadata`
instead uses the selected Swift GitHub release's `published_at` or the selected
Maven version's `timestamp`. `Finding.report` correctly labels the latter
**version published**, not first published. The recent/boundary cases therefore
prove the selected-version policy for those two adapters. They do not establish
that the first publication of the package was checked.

Reproduce the distinction with
`python3 -m unittest tests.test_ship.ShipTests.test_completion_gate_swift_recent tests.test_ship.ShipTests.test_completion_gate_gradle_recent`
and inspect `_resolved_metadata`: Swift only requests the selected release tags;
Maven queries `g`, `a`, and the exact `v`. Neither requests historical package
publication evidence. Both tests pass, demonstrating conservative blocking of a
recent version, not validating the parent's stronger first-publication claim.

Required repair for criterion 2: obtain defensible earliest package-publication
evidence for Swift/Gradle, fail closed when unavailable, and add fixtures that
separate a long-established package with a new version from a newly published
package. This is an evidence/semantics gap, not a demonstrated unsafe merge.

## Report and review contract

`CompletionEvidenceTests.test_report_fields_for_every_ecosystem` asserts package
identity, exact version, publisher/source values, UTC publication date, and
`advisories none` or OSV ID with HIGH/CRITICAL severity for each ecosystem.
Existing version-specific npm metadata and Swift revision/source mismatch tests
remain in the focused suite. Gradle's reported source is a constructed POM URL;
the checker does not fetch the POM or inspect its developers/SCM fields.

`test_missing_optional_publisher_source_are_reported_as_unavailable` proves
npm/PyPI explicitly report unavailable optional metadata. The deterministic gate
can still pass such packages; that is not a legitimacy finding.
`DependencyRecipeTests.test_review_requires_dependency_diff_and_gate_evidence`
asserts the rendered review prompt requires identifying missing or inconsistent
publisher/source details and says metadata presence alone does not prove
legitimacy, while retaining all fail-closed evidence guidance.

Failing-first evidence: before changing `recipes/review.md`, running
`python3 -m unittest tests.test_recipes.DependencyRecipeTests` ran 2 tests and
failed the two new phrase assertions. After the recipe change these assertions
pass in the focused and full suites. Existing gate behavior was covered without
changing runtime policy or weakening assertions.

Required coverage for criterion 4: automated reviewer evaluations with missing,
contradictory and misleading publisher/source metadata, with expected blocking
findings, plus richer Gradle metadata retrieval if claiming actual available
publisher/repository evidence. A rendered instruction contract cannot establish
that a model will follow it. No owner verdict is requested or counted as evidence.

## Installation, credentials and runtime dependencies

- `dependency_vetting.py` imports only `json`, `math`, `re`, `tomllib`,
  `dataclasses`, `datetime`, and `urllib`. No package manager, subprocess,
  executable manifest evaluation, installer, credential reader or environment
  reader is invoked by the checker.
- `_request_json` constructs public HTTPS requests with Accept, User-Agent, and
  (for OSV POSTs) Content-Type headers. It adds no Authorization header or token.
  Registry/source endpoints are npm, PyPI, GitHub public repository/release/commit
  metadata, Maven search, and OSV. Lookup failures fail closed.
- `GH.dependency_files` uses Git check-ref-format, rev-parse, diff, ls-tree and
  show to read snapshots; it does not install candidates. Existing GitHub
  fetch/merge authentication elsewhere in Mahler is not a new registry credential
  requirement and is not claimed to be credential-free.
- `ship._vet_pr_dependencies` calls the checker and emits reports; no new runtime
  import outside the standard library was introduced. This task changes tests,
  review instructions and this audit only. All new HTTP tests use fixture
  responses and per-test temporary state; no real registry traffic is needed.

## Verification results

All five verification commands were executed from the worktree root using Python 3.14.3 on macOS:

| # | Command | Tests / Scope | Duration | Result | Exit |
|---|---|---|---|---|---|
| 1 | `python3 -m unittest tests.test_dependency_vetting tests.test_ship tests.test_recipes` | 465 tests | 12.877s | OK | 0 |
| 2 | `python3 -m mahler.repo_guard` | Tracked files / repo guard checks | < 1s | OK (no output) | 0 |
| 3 | `python3 -m unittest discover -s tests` | 3215 tests (skipped=1) | 205.022s | OK | 0 |
| 4 | `python3 tests/run_random.py` | 3215 tests (skipped=1), seed `866945489` | 202.864s | OK | 0 |
| 5 | `python3 -X dev tests/run_strict.py` | 3215 tests (skipped=1) | 220.005s | OK | 0 |

The single skipped test in the full test suites is the standard pre-existing platform skip, unrelated to dependency vetting. All 48 generated completion gate methods in `tests.test_ship.ShipTests`, the adapter fixtures in `tests.test_dependency_vetting`, and the review recipe contracts in `tests.test_recipes` passed without failure or error.

## Reverification at base `6a40d24ec4a77d4423b2bf27ae0494968b35bec8`

The three child PRs remain merged, and PR #1238 is merged. Its rendered-review
prompt assertion is
`tests.test_recipes.DependencyRecipeTests.test_review_requires_dependency_diff_and_gate_evidence`;
the report field
assertions remain in `CompletionEvidenceTests.test_report_fields_for_every_ecosystem`.
This establishes the report and prompt contract, not that a language model
always follows it. Keep the criterion unchecked if requiring observed reviewer
behavior rather than the tested prompt contract.

The first-publication criterion remains unsupported for Swift and Gradle:
`_resolved_metadata` reports the selected GitHub release or Maven artifact
version timestamp, and `test_completion_gate_swift_recent` /
`test_completion_gate_gradle_recent` exercise those selected-version dates.
They do not establish the first publication date of the package. npm and PyPI
use their earliest returned release/upload dates. This remains an actionable
technical gap; add a linked P1 fix issue before treating the criterion as
complete.

On Python 3.14.3, the focused command passed (469 tests), and
`python3 -m mahler.repo_guard` exited 0. The three full-suite commands each
initially failed the same four unrelated purge-history tests because this host
has `safe.bareRepository=explicit`; Git refuses their temporary bare fixture
repositories. The failure is reproducible in `tests.test_purge_calendar_profile_history`.
With a command-scoped override only (no persistent Git configuration change),
the module passed all 5 tests, and the full suite, randomized suite, and strict
suite each passed 3219 tests with one skipped. The random seed was `624717300`.
Commands used for the successful reruns:

- `GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.bareRepository GIT_CONFIG_VALUE_0=all python3 -m unittest discover -s tests`
- `GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.bareRepository GIT_CONFIG_VALUE_0=all python3 tests/run_random.py 624717300`
- `GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.bareRepository GIT_CONFIG_VALUE_0=all python3 -X dev tests/run_strict.py`

GitHub write access was unavailable during this reverification: `gh auth status`
reported the active `x-access-token` credential invalid, and `gh issue create`
returned HTTP 401. Therefore this update records evidence locally only; it has
not been posted to #1130, its supported checklist items have not been edited,
and no linked P1 issue was created. Do not represent the parent-side recording
as complete until an authorized GitHub CLI credential is provisioned.
