# Audit: #800 completion criteria

- Audited: 2026-10-22, Mahler checkout `9681e3ea3b581686212eecea8b90cd7d78339caa` (branch `mahler/1184-verify-remaining-completion-criteria-for`), Python 3.14.3.
- Method: Public GitHub metadata read using Mahler's configured per-project GitHub identities with a temporary GitHub App token cache directory (`~/.mahler/secrets/github-app.pem` consumed by `mahler.github_app.Installation`); isolated temporary checkouts of current remote heads (`git clone --shared --no-checkout` with detached checkouts at the scanned default-branch SHAs) ensuring zero mutation to shared configured checkouts; read-only execution of `mahler.practices_audit.scan_project`.
- Constraints: No project build, test, or release commands executed; zero credential candidates or private values published (all candidate contexts strictly redacted); no daemon state or live ledger mutated; no project repository or workflow edited; no synthetic blanket exemptions introduced.

---

## Child issue merge identities and regression tests

All four implementation child issues (#803, #804, #805, #806) were merged to `main` with passing CI (`test` check SUCCESS) and are CLOSED on GitHub.

| Child | PR | Base | Merge SHA | Merged (UTC) | PR Head | CI |
|---|---|---|---|---|---|---|
| #803 | [#833](https://github.com/mkny13/mahler/pull/833) | `main` | `70f5cf40832eaf3434bdff29059fee81836b828f` | 2026-10-07T23:33:17Z | `115ab3a4b0d52a93ab0b896a591192f0e0d7c22f` | `test` SUCCESS |
| #804 | [#834](https://github.com/mkny13/mahler/pull/834) | `main` | `c366cef979ac10122119192a0c81e13d9cb91c7a` | 2026-10-07T23:54:25Z | `e522909f2d52b74e5a6869ffe20b2792aabc95f9` | `test` SUCCESS |
| #805 | [#835](https://github.com/mkny13/mahler/pull/835) | `main` | `86f88628b103fa8e1ac5457e48a6d604d8b68732` | 2026-10-08T00:31:54Z | `aa953562bcc9fa5cf74f9f6d8da314cccbb97933` | `test` SUCCESS |
| #806 | [#836](https://github.com/mkny13/mahler/pull/836) | `main` | `34aa2a86f0ebfe08f88cf5135b9b36fe599dd00e` | 2026-10-08T01:03:45Z | `29bb89c9da0ac27728b0945bccd0417904b074f3` | `test` SUCCESS |

### Regression test mapping in `tests/test_practices_audit.py`

- **#803 (Eliminate false `unknown` from concurrency expressions, paths filters, and auxiliary cleanup workflows):**
  - `tests/test_practices_audit.py::TestPracticesAudit.test_pr_paths_filters_pass`: Verifies `paths` and `paths-ignore` PR triggers evaluate to `pass` without triggering structural unknown flags.
  - `tests/test_practices_audit.py::TestPracticesAudit.test_workflow_concurrency_expression_passes`: Verifies top-level `${{ github.workflow }}-${{ github.ref }}` concurrency groups evaluate to `pass`.
  - `tests/test_practices_audit.py::TestPracticesAudit.test_auxiliary_pr_cleanup_workflow_does_not_override_primary_ci`: Verifies auxiliary workflows (such as `pull_request: types: [closed]`) do not override primary CI or cause false unknowns, while isolated non-test workflows evaluate to `gap`.

- **#804 (Recognize script test runners and matching required status checks):**
  - `tests/test_practices_audit.py::TestPracticesAudit.test_npm_test_node_script_runner_is_recognized`: Verifies `package.json` test scripts executing `node scripts/...` invoked via `npm test` are recognized as tests.
  - `tests/test_practices_audit.py::TestPracticesAudit.test_self_test_binary_step_is_recognized`: Verifies executable binary steps running with `--self-test` are recognized by `_is_test` and pass `ci-tests`.
  - `tests/test_practices_audit.py::TestPracticesAudit.test_protection_passes_with_script_test_job_context`: Verifies required branch protection checks matching script-based test jobs evaluate to `pass`.

- **#805 (Eliminate false positives for binary assets, schema definitions, and type annotations while reporting `gap` for real secrets):**
  - `tests/test_practices_audit.py::TestPracticesAudit.test_tracked_binary_assets_and_clean_text_return_pass`: Proves tracked binary assets (`.png`, `.ico`, `.jpg`, `.pdf`) alongside clean code return `pass` without generating uninspected-binary errors. The text-like `.jpg` fixture contains a synthetic credential-shaped assignment, proving its binary extension is skipped rather than accidentally passing through null-byte detection.
  - `tests/test_practices_audit.py::TestPracticesAudit.test_binary_file_alongside_reviewed_text_passes`: Proves binary assets alongside reviewed non-secret candidates in `.mahler/secret-reviews.json` evaluate to `pass`.
  - `tests/test_practices_audit.py::TestPracticesAudit.test_drizzle_schema_definitions_return_pass`: Proves Drizzle ORM column definitions (`password: text("password")`, `token: varchar("token")`) pass cleanly.
  - `tests/test_practices_audit.py::TestPracticesAudit.test_language_type_annotations_return_pass`: Proves type annotations across TypeScript, Kotlin, and Python return `pass`.
  - `tests/test_practices_audit.py::TestPracticesAudit.test_environment_and_config_lookups_return_pass`: Proves environment variable lookups (`os.environ["TOKEN"]`, `process.env.TOKEN`, config parser calls) return `pass`.
  - `tests/test_practices_audit.py::TestPracticesAudit.test_dict_keys_and_mock_fixtures_return_pass`: Proves mock fixtures and dictionary keys return `pass`.
  - `tests/test_practices_audit.py::TestPracticesAudit.test_actual_credential_token_and_private_key_report_gap_with_redacted_evidence`: Proves hardcoded GitHub tokens, passwords, and private keys report `gap` with redacted context.
  - `tests/test_practices_audit.py::TestPracticesAudit.test_unavailable_tracked_inventory_and_corrupt_content_are_explicit_unknowns`: Proves unreadable/corrupt files cleanly report explicit `unknown`.

- **#806 (Clean resolution of verified proposal false positives without escalating to `needs_you`):**
  - `tests/test_practices_audit.py::TestPracticesAudit.test_proposal_body_includes_false_positive_resolution_contract`: Verifies proposal bodies document the resolution paths via `docs/DECISIONS.md`, `DECISIONS.md`, `AGENTS.md`, or `.mahler/project.toml`.
  - `tests/test_practices_audit.py::TestPracticesAudit.test_documented_decisions_in_decisions_md_resolve_cleanly`: Proves documented decisions in `DECISIONS.md` resolve gaps cleanly to `pass`.
  - `tests/test_practices_audit.py::TestPracticesAudit.test_project_toml_exemption_resolves_finding_cleanly`: Proves exemptions configured in `[practices.exemptions]` of `.mahler/project.toml` resolve findings to `pass`.
  - `tests/test_practices_audit.py::TestPracticesAudit.test_agents_md_exemption_resolves_finding_cleanly`: Proves documented exemptions under `## Practices exemptions` in `AGENTS.md` resolve findings to `pass`.
  - `tests/test_practices_audit.py::TestPracticesAudit.test_audit_rerun_cleanly_reconciles_resolved_proposals`: Proves re-running `file_audit` marks resolved proposals as closed with resolution comments without duplication.
  - `tests/test_practices_audit.py::TestPracticesAudit.test_proposal_acceptance_names_conductor_check`: Verifies proposal acceptance references conductor-managed base verification (upgraded via PR #1005).
  - `tests/test_practices_audit.py::DetachedProposalTests.test_scans_requested_commit_with_project_client_and_cleans_up`: Proves conductor acceptance verification scans the exact reviewed base SHA in an isolated directory and cleans up.

---

## Fresh verification results on audited SHA

Executed on `9681e3ea3b581686212eecea8b90cd7d78339caa` (Python 3.14.3):

| Command | Result | Exit Code |
|---|---|---|
| `python3 -m mahler.repo_guard` | Clean (no output) | 0 |
| `python3 -m unittest discover -s tests` | Ran 3049 tests in 202.606s, OK (skipped=1) | 0 |
| `python3 tests/run_random.py` | random order seed: 1310860237, Ran 3049 tests in 202.308s, OK (skipped=1) | 0 |
| `python3 -X dev tests/run_strict.py` | Ran 3049 tests in 214.883s, OK (skipped=1) | 0 |

---

## Enabled project inventory snapshot (2026-10-22)

Snapshot of the finite inventory returned by `config.enabled_projects(config.load())` with configured identities and current scanned default-branch SHAs:

| Project | Repository | Configured Identity | Default Branch | Scanned SHA |
|---|---|---|---|---|
| `mahler` | `mkny13/mahler` | Personal (App ID 5127048, inst 166283054) | `main` | `9681e3ea3b581686212eecea8b90cd7d78339caa` |
| `groundwork` | `mkny13/groundwork` | Personal (App ID 5127048, inst 166283054) | `main` | `28c2a551e42f6729ebbc85e0ae95e156ce30e7b6` |
| `phish-in` | `mkny13/couch-tour` | Personal (App ID 5127048, inst 166283054) | `main` | `702c0f4354f14d2ed0d7906ccc0abd8da0672713` |
| `sit-stand-walk` | `makastel_ncstate/sit-stand-walk` | Work (`gh_account = "work"`, `GH_CONFIG_DIR`) | `main` | `6830b2d9630b2affdd7cfeaf56ec7287e7db2087` |
| `google-chat-nav` | `makastel_ncstate/google-chat-nav` | Work (`gh_account = "work"`, `GH_CONFIG_DIR`) | `main` | `e03146a5d789503c34dd28957f5f172489b060ed` |
| `movebreak` | `mkny13/movebreak` | Personal (App ID 5127048, inst 166283054) | `main` | `1079dba415c49412440047fdedaa580d0d7b568a` |
| `puppy-growth-chart` | `mkny13/puppy-growth-chart` | Personal (App ID 5127048, inst 166283054) | `main` | `1ed489aa01963b2a0430b40ccb773fdd4e1e2c46` |
| `hockey` | `mkny13/hockey-draft-copilot` | Personal (App ID 5127048, inst 166283054) | `main` | `d37ae87604443eb57c4baf64768459bd26f8b2d6` |
| `mental-jukebox` | `mkny13/mental-jukebox` | Personal (App ID 5127048, inst 166283054) | `main` | `8a4c114dc9ebc0668c1b779c96cbc368567ce9d5` |
| `crate-cataloger` | `mkny13/crate-cataloger` | Personal (App ID 5127048, inst 166283054) | `main` | `b6824c077f2c0e38a0f006b03c31bc5658e70997` |
| `spotify-overlay` | `mkny13/spotify-overlay` | Personal (App ID 5127048, inst 166283054) | `main` | `d4eedbb75ab4917741c57e426bd3d87ee337859c` |
| `google-workspace-connector` | `makastel_ncstate/google-workspace-connector` | Work (`gh_account = "work"`, `GH_CONFIG_DIR`) | `main` | `38313b9185983ab868bf7a8fa341189a923fb86e` |

---

## Target proposal/check result matrix (nine pairs)

Scanner evaluated against isolated checkouts at current remote default-branch SHAs:

| Proposal | Check | Scanned SHA | Scanner State | Reason / Evidence (Redacted) | Issue State & Disposition |
|---|---|---|---|---|---|
| `groundwork#190` | `ci-tests` | `28c2a551e4` | **pass** | PR CI invokes test commands; test quality is not assessed. | CLOSED (independent no-change review, run 11331) |
| `groundwork#193` | `verify-command` | `28c2a551e4` | **pass** | Documented effective verify command is invoked by PR CI. | CLOSED (independent no-change review, run 11321) |
| `groundwork#194` | `tracked-secrets` | `28c2a551e4` | **pass** | Documented architectural decision accepts tracked-secrets (Issue #194). | CLOSED (shipped PR #202 squash-merged) |
| `phish-in#555` | `ci-tests` | `702c0f4354` | **pass** | PR CI invokes test commands; test quality is not assessed. | CLOSED (independent no-change review, run 11314) |
| `phish-in#558` | `tracked-secrets` | `702c0f4354` | **pass** | Documented decision D339 / `.mahler/secret-reviews.json` accepts false positives. | CLOSED (shipped PR #572 squash-merged) |
| `hockey#79` | `ci-tests` | `d37ae87604` | **pass** | PR CI invokes test commands; test quality is not assessed. | CLOSED (independent no-change review, run 11305) |
| `hockey#82` | `tracked-secrets` | `d37ae87604` | **pass** | No candidates in inspected tracked text; clean text. | CLOSED (shipped PR #86 squash-merged) |
| `mental-jukebox#13` | `tracked-secrets` | `8a4c114dc9` | **gap** | Suspected credentials require review. Evidence: `tests/test_contracts.py:61: detector=credential-assignment; context=<redacted>`, `tests/test_contracts.py:62: detector=credential-assignment; context=<redacted>`, `.mahler/secret-reviews.json: 6 reviewed non-secret candidates` | CLOSED (shipped PR #19 squash-merged) — **Concrete technical follow-up required (see below)** |
| `movebreak#87` | `branch-protection` | `1079dba415` | **pass** | Squash merging enabled and a PR test check is required. | CLOSED (shipped PR #95 squash-merged) |

---

## State of downstream dependents (five issues)

All five dependent items were previously unblocked, executed, and CLOSED via merged PRs:

| Dependent Issue | Target Check | Issue State | Shipped PR | Current Scanner State on Default Branch |
|---|---|---|---|---|
| `groundwork#195` | `branch-protection` | CLOSED | PR #205 (squash-merged) | `pass` (`Squash merging enabled and a PR test check is required.`) |
| `hockey#83` | `branch-protection` | CLOSED | PR #87 (squash-merged) | `pass` (`Squash merging enabled and a PR test check is required.`) |
| `phish-in#557` | `verify-command` | CLOSED | PR #574 (squash-merged) | `pass` (`Documented effective verify command is invoked by PR CI.`) |
| `phish-in#559` | `branch-protection` | CLOSED | PR #573 (squash-merged) | `unknown` (`Cannot establish squash/test-gate compatibility.`; work completed) |
| `movebreak#85` | `verify-command` | CLOSED | PR #98 (squash-merged) | `unknown` (`Verify command uses dynamic expansion or evaluation.`; work completed) |

---

## Complete enabled inventory practices audit results

Evaluated on 2026-10-22 using `practices_audit.scan_project` on isolated temporary checkouts at remote default-branch SHAs:

| Project | `ci-tests` | `agent-instructions` | `mahler-guidance` | `verify-command` | `tracked-secrets` | `branch-protection` |
|---|---|---|---|---|---|---|
| `mahler` | `unknown` | `pass` | `pass` | `unknown` | `gap` | `pass` |
| `groundwork` | `pass` | `pass` | `pass` | `pass` | `pass` | `pass` |
| `phish-in` | `pass` | `pass` | `pass` | `pass` | `pass` | `unknown` |
| `sit-stand-walk` | `gap` | `pass` | `pass` | `gap` | `pass` | `unknown` |
| `google-chat-nav` | `pass` (exempt) | `pass` | `pass` | `pass` (exempt) | `pass` | `pass` (exempt) |
| `movebreak` | `unknown` | `pass` | `pass` | `unknown` | `pass` | `pass` |
| `puppy-growth-chart` | `pass` | `pass` | `pass` | `pass` | `pass` | `pass` |
| `hockey` | `pass` | `pass` | `pass` | `pass` | `pass` | `pass` |
| `mental-jukebox` | `gap` | `pass` | `pass` | `pass` | `gap` | `unknown` |
| `crate-cataloger` | `pass` | `gap` | `pass` | `pass` | `pass` | `pass` |
| `spotify-overlay` | `gap` | `pass` | `pass` | `pass` | `gap` | `unknown` |
| `google-workspace-connector` | `gap` | `pass` | `gap` | `gap` | `gap` | `unknown` |

---

## Parent #800 completion criteria disposition

| Criterion on #800 | Verdict | Evidence Summary |
|---|---|---|
| 1. #803 is merged and unit tests prove top-level concurrency expressions, PR path filters, and auxiliary cleanup workflows do not cause false `unknown` results. | **SUPPORTED** | Merged via PR #833 (`70f5cf40832eaf3434bdff29059fee81836b828f`). Tested by `test_pr_paths_filters_pass`, `test_workflow_concurrency_expression_passes`, and `test_auxiliary_pr_cleanup_workflow_does_not_override_primary_ci`. |
| 2. #804 is merged and unit tests prove script test runners (`node scripts/...`, `--self-test`) and matching required status checks pass. | **SUPPORTED** | Merged via PR #834 (`c366cef979ac10122119192a0c81e13d9cb91c7a`). Tested by `test_npm_test_node_script_runner_is_recognized`, `test_self_test_binary_step_is_recognized`, and `test_protection_passes_with_script_test_job_context`. |
| 3. #805 is merged and unit tests prove tracked binary assets (PNG/ICO/JPG) and non-secret language constructs (Drizzle schema definitions, type annotations, env lookups) pass cleanly while real credential tokens report `gap`. | **SUPPORTED** | Merged via PR #835 (`86f88628b103fa8e1ac5457e48a6d604d8b68732`). Tested by `test_tracked_binary_assets_and_clean_text_return_pass`, `test_binary_file_alongside_reviewed_text_passes`, `test_drizzle_schema_definitions_return_pass`, `test_language_type_annotations_return_pass`, `test_environment_and_config_lookups_return_pass`, `test_dict_keys_and_mock_fixtures_return_pass`, `test_actual_credential_token_and_private_key_report_gap_with_redacted_evidence`, and `test_unavailable_tracked_inventory_and_corrupt_content_are_explicit_unknowns`. |
| 4. #806 is merged and unit tests prove proposal bodies and acceptance checks allow clean resolution of verified scanner false positives without escalating to `needs_you`. | **SUPPORTED** | Merged via PR #836 (`34aa2a86f0ebfe08f88cf5135b9b36fe599dd00e`) and verified with conductor detached proposal runner (PR #1005). Tested by `test_proposal_body_includes_false_positive_resolution_contract`, `test_documented_decisions_in_decisions_md_resolve_cleanly`, `test_project_toml_exemption_resolves_finding_cleanly`, `test_agents_md_exemption_resolves_finding_cleanly`, `test_audit_rerun_cleanly_reconciles_resolved_proposals`, and `test_proposal_acceptance_names_conductor_check`. |
| 5. `python3 -m mahler.repo_guard`, `python3 -m unittest discover -s tests`, and `python3 -X dev tests/run_strict.py` pass cleanly. | **SUPPORTED** | Clean run at SHA `9681e3ea3b581686212eecea8b90cd7d78339caa`: repo_guard exit 0; unittest 3049 OK (skipped=1); run_random seed 1310860237 3049 OK (skipped=1); run_strict 3049 OK (skipped=1). |
| 6. After merging the sub-issues, re-running the practices audit across the enabled project inventory clears the 9 false-positive items across the 6 projects and unblocks their dependents. | **UNRESOLVED (Partially Supported)** | 8 of 9 items evaluate to `pass`. All 5 dependents are unblocked and CLOSED. However, `mental-jukebox#13/tracked-secrets` currently evaluates to `gap` because test fixture assignments in `tests/test_contracts.py:61-62` trigger `credential-assignment` and are not yet covered in `.mahler/secret-reviews.json`. Remains unchecked until follow-up completes. |

---

## Concrete technical follow-up

- **Target:** `mental-jukebox` (`mkny13/mental-jukebox`)
- **Finding:** `tracked-secrets: gap` on `tests/test_contracts.py:61` and `tests/test_contracts.py:62` (`detector=credential-assignment; context=<redacted>`).
- **Follow-up action:** Review the two synthetic test fixture token values in `tests/test_contracts.py` and record their SHA-256 digests in `.mahler/secret-reviews.json` (or record an exemption in `.mahler/project.toml` per the #806 resolution protocol). Once merged, `practices_audit.scan_project` for `mental-jukebox` will return `pass` for `tracked-secrets`, fully clearing all 9 items and satisfying the final criterion.


## Replayed-branch verification (2026-10-23)

The existing evidence report was reverified at Mahler SHA
`e81f9d7fbcfb68c1593dcc3171fbe3442b320740` with Python 3.14.3.
This verifies the replayed branch; it does not refresh the dated remote inventory
or resolve the outstanding ninth target finding above.

| Command | Result | Exit code |
|---|---|---|
| `python3 -m mahler.repo_guard` | Clean, no output | 0 |
| `python3 -m unittest discover -s tests` | 3084 tests in 175.315s; OK (skipped=1) | 0 |
| `python3 tests/run_random.py` | Seed `1906047960`; 3084 tests in 174.509s; OK (skipped=1) | 0 |
| `python3 -X dev tests/run_strict.py` | 3084 tests in 184.712s; OK (skipped=1) | 0 |

No manual owner check is required. The conductor retains the GitHub evidence
comment/checklist handoff under this run's local-commit-only instructions.
Parent criterion 6 remains unresolved pending the technical follow-up above.
