# Remaining practices gaps: mahler#1487

Collected 2026-10-31 against Mahler source `d50fc07`.

**Neither target has a fresh passing scan. Keep #800’s final criterion unchecked.**
This is an evidence and cross-repository handoff, not a scanner fix or completed
remediation. No repository settings, secret reviews, or scanner behavior changed.

## Current evidence and access boundary

| Read | Result |
|---|---|
| mental-jukebox repository metadata, run identity | HTTP 404 |
| mental-jukebox repository metadata, unauthenticated public API | HTTP 404 |
| movebreak main commit | `50779f5ee19e31b6b2c9322a2fe6f9864a678aea` (same as prior report) |
| movebreak effective rules, `rules/branches/main` | HTTP 200; `[]` |
| movebreak classic protection, `branches/main/protection` | HTTP 403, resource not accessible by integration |
| movebreak repository merge metadata | `default_branch=main`; `allow_squash_merge` absent |

The earlier [#800 report](800-completion.md) had both protection reads return
200 under the conductor’s configured identity. This build identity cannot reproduce
that observation. No alternate credentials or other checkout were accessed.
A 404 does not establish that mental-jukebox is missing; its contents remain
unavailable to this run. No credential candidate values were read or disclosed.

A direct current `_protection(GH("mkny13/movebreak"), ["verify"], False)`
probe returns **unknown**, reason: “Default-branch protection metadata is unavailable
or unsupported.” The injected historical job name makes this an API-boundary probe,
not a fresh whole-repository/workflow scan or passing acceptance evidence.

A subsequent targeted snapshot replay downloaded movebreak’s public exact-SHA
archive into a temporary directory inside this worktree and ran `_ci(_Tree(path))`
and `_protection` with its actual discovered jobs. Discovery returned
`jobs=["verify"]`, `uncertain=True`, CI state **unknown**; branch-protection again
returned **unknown** with the same metadata-unavailable reason. No repository
commands were executed. This replay did not scan tracked secrets, and archive
coverage was not compared to the Git tree; it is not the final acceptance scan.

## What the scanner establishes

`_load_secret_reviews` and `_secrets` require exact path, one-based line,
detector and SHA-256 of the **entire file bytes**. An existing review does not
cover a new line or candidate; a byte change invalidates reviews for that file.
The historical six matches and two unmatched candidates do not establish a parser
bug or establish that the two candidates are fixtures. Classification requires the
actual source through mental-jukebox’s authorized project context.

`_hardening` requires a coherent non-bypassable provider for each safeguard.
Classic strict checks, force-push prevention and deletion prevention are each
coupled to administrator enforcement. Therefore one disabled administrator flag
can explain all four historical missing labels; the report alone cannot prove
four independently incorrect settings. Effective rules are currently empty, so
there is no observed ruleset alternative. Classic settings must be read before
choosing a settings change or scanner fix. Denied reads must never become pass.

## Follow-up tracking

Decided: preserve the scanner’s security requirements and route repository work
through repository issues, rather than invent a parser fix without source evidence.

- Filed [movebreak#123](https://github.com/mkny13/movebreak/issues/123), with
  concrete classic/ruleset comparison, conditional settings instructions and
  acceptance requiring actual workflow discovery plus a passing audit. No settings
  were changed. The repository has no `mahler` label; an attempted combined label
  update was refused. A separate p1 assignment was denied by integration permissions.
  The conductor must assign p1 to #123; issue creation succeeded but labeling did not.
- mental-jukebox issue creation was refused: “Could not resolve to a Repository
  with the name 'mkny13/mental-jukebox'.” The conductor must file the exact body
  below in that project with p1 priority and link its resulting issue to #1487/#800.
  **No mental-jukebox follow-up issue number exists from this run.**
- After the repository follow-ups complete, the conductor must scan both current
  default-branch snapshots with each project’s authorized identity, record snapshot
  and scanner SHAs, UTC time and redacted evidence, and require both targets to
  return pass before ticking #800’s last criterion. No owner verdict is needed.

## Ready-to-file mental-jukebox issue

Title: **Classify and clear the two tracked-secrets candidates blocking mahler#800**

<!-- mahler:agent -->
## Problem / goal
Follow-up to mkny13/mahler#1487 and mkny13/mahler#800; prior proposal #13.
The 2026-10-26 audit found two unreviewed credential-assignment candidates in tests/test_contracts.py:61-62 at 8a4c114dc9ebc0668c1b779c96cbc368567ce9d5. Six existing reviews did not cover them. The Mahler build identity cannot read this repository (HTTP 404 on 2026-10-31), so classification must run in this project's authorized context. Do not assume these are false positives.

## Work
Inspect both candidates without printing values. Compare each review's path, one-based line, detector and SHA-256 of the ENTIRE file bytes (not the candidate line). If non-secret fixtures, add exact version-1 reviews with a specific rationale; refresh other reviews for that file if its bytes change. Never exempt the whole file or repository. If real credentials, remove and rotate through the authorized repository process. If the scanner misclassifies a source construct, send a sanitized minimal reproduction to mkny13/mahler#1487 for a failing-first scanner regression.

## Acceptance criteria
- [ ] Both candidates classified, with no credential values in logs/issues.
- [ ] Exact reviews or appropriate remediation committed in this repository.
- [ ] Current Mahler tracked-secrets scan returns pass; record repository and scanner SHAs and UTC time.
- [ ] Negative check confirms a new/unreviewed candidate still produces gap.
- [ ] Link passing evidence to mkny13/mahler#1487 and mkny13/mahler#800.

## Needs a human to check
Nothing. Conductor tracks automated evidence; no owner verdict.

## Verification

Tests used a fresh temporary `MAHLER_HOME` for each command, with inherited
`MAHLER_*` run identity removed and a subprocess-only `core.hooksPath=/dev/null`
override, as documented in the previous report. No live ledger was used.

| Command | Result |
|---|---|
| `python3 -m mahler.repo_guard` | PASS, exit 0 |
| `python3 -m unittest discover -s tests -p test_practices_audit.py` | PASS, 95 tests, 8.957s |
| `python3 -m unittest discover -s tests` | PASS, 3478 tests, 182.428s, 2 skips |
| `python3 tests/run_random.py` | PASS, seed 1660414144, 3478 tests, 186.458s, 2 skips |
| `python3 -X dev tests/run_strict.py` | PASS, 3478 tests, 197.796s, 2 skips |

Named exercised checks in `tests/test_practices_audit.py` include
`test_exact_review_match_suppresses_candidate`,
`test_new_candidate_in_reviewed_file_remains_gap`,
`test_candidate_changed_in_same_file_remains_gap`,
`test_same_candidate_in_different_file_remains_gap`,
`test_actual_credential_token_and_private_key_report_gap_with_redacted_evidence`,
`test_each_unsafe_protection_setting_is_a_gap`,
`test_missing_protection_fields_are_unknown_not_pass`,
`test_ruleset_alone_matches_classic_protection`, and
`test_bypassable_ruleset_cannot_supply_force_push_or_deletion_safeguards`.

No behavior was implemented, so no failing-first behavior test was added.
Existing tests exercise the exact matching and security boundaries; passing
fixtures do not certify the inaccessible repositories. No dependencies added.

## Needs a human to check

Nothing. Outstanding work is conductor-owned filing, repository remediation and
fresh automated evidence; neither original Done-when pass is claimed.
