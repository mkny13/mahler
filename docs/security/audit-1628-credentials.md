# Credential and private-state audit — #1630 / #1628

Audited on 2026-11-21 at `d5161622d6732eee617f6f3520ad079fcde01aa0`
(planning baseline: `dbde249fd68560bf2377826a581ded962c62c27c`).
Read DESIGN D12, D18, D25, D46 and D47. Runtime code was read-only;
this deliverable adds tests and evidence, not production remediation. No live
credentials, production env files, databases or network services were accessed.
All credential strings below and in the tests are synthetic.

History reuse: `mahler history mahler 'backup credential redaction' --limit 3`
returned runs 521 (#76), 869 (#103), and 10101 (#589). Their conclusions were
background only; current source and executable checks determine this report.

## Six boundary cases

Selectors below run from the repository root with
`PYTHONPATH=tests python3 -m unittest <selector>`.
`audit` in the table abbreviates
`test_security_credentials_audit.CredentialAuditTests`.

| Case / verdict | Source evidence | Executable evidence and limits |
| --- | --- | --- |
| 1. Explicit `DATABASE_URL_UNPOOLED` when `DATABASE_URL` also exists — **supported** | `backup.load_env`, `backup.backup_postgres`: lookup uses the configured `url_var` exactly; default is `DATABASE_URL`. Parsing reads text without shell evaluation. | `audit.test_explicit_unpooled_selection_and_literal_env` selects the direct host from competing URLs, leaves a command-substitution marker uncreated, and refuses a missing explicitly selected key without subprocess execution. `.env` grammar is deliberately small: no interpolation, full dotenv quoting or inline-comment semantics. Input file permissions and symlinks are not validated; trusted operator configuration is assumed. |
| 2. Percent-encoded password to PG environment, no credential argv — **supported / accepted limitation** | `backup.libpq_env` decodes username/password/database and maps selected query parameters; `backup.backup_postgres` passes an argv list and a separate environment to `pg_dump`. | Same new selector verifies encoded punctuation becomes the exact `PGPASSWORD`, SSL mode survives, argv lacks the URL/password, and no shell is enabled. `test_backup.BackupTests.test_dump_verified_private_and_password_off_argv` independently exercises fake executables and dump verification. The password necessarily exists in process memory and the child environment; this is not isolation from the same OS user, privileged process inspection or a malicious dump executable. |
| 3. Backup failure output — **defect**, with supported URL masking | `backup._scrub` removes the exact URL and lowercase postgres URL shapes before truncation; `backup.run` sends exception text to events, CLI and notifications. `redact.redact` masks shapes and registered values, not arbitrary unregistered passwords. | `test_backup.BackupTests.test_failure_is_scrubbed`, `test_redact.TestRedact.test_postgres_url_password_is_masked` cover URL masking. The synthetic reproduction below confirms a decoded standalone password remains visible. No assertion blessing that behavior or expected-failure test was added. |
| 4. Inherited personal credentials in a work run — **supported for declared variables / coverage gap beyond them** | `config.CREDENTIAL_VARS`, `config.run_env`; `runner.prepare` selects the project's hosting account for fetch; `runner.run_env` applies platform-account isolation, replaces scoped GitHub identity, clears askpass/App variables and disables Git credential helpers. | `audit.test_work_env_strips_full_declared_login_set_without_mutation` covers every declared login key, explicit work override, unchanged input and refusal of an undefined other account. Reused `test_accounts.ConfigTests.test_work_env_drops_inherited_logins_and_sets_its_own`, `test_github_pat.PatTests.test_valid_token_is_only_the_run_gh_identity`. The finite denylist is not general secret discovery: arbitrary extra env names, inherited HOME and same-user filesystem access are not a sandbox. CLI/keychain behavior and actual live account isolation were not exercised. |
| 5. Repository/scope separation and fail-closed issuance — **supported / accepted limitation** | `github_app.Installation.token`, `_usable`, `agent_token`: scope allowlist, scope-specific cache names and metadata checks, safe issuance errors; `github_pat.resolve`, `validate`, `read_token`, `agent_token`: local repo binding, one-provider/file rules and private-file validation. `runner.run_env` does not fall back after provider failure. | `audit.test_scope_cache_tampering_cannot_fallback_when_exchange_fails` first verifies a valid request, then injects another repository and broader permissions into its cache and verifies issuance failure refuses both cached identities. Reused `test_github_app.InstallationTests.test_scoped_token_cache_separation_and_rejection_of_broader_tokens`, `.test_invalid_scope_fails_closed_without_leaking_secrets`, `test_github_pat.PatTests.test_wrong_repo_refused`, `.test_failure_never_falls_back_to_ambient_or_app`. GitHub must enforce requested App scope; response scope is not independently attested. PAT permissions cannot be narrowed or verified locally. Transitional identity remains permitted without a required-scoped-identity flag, as D47 documents. |
| 6. Private directories/files under permissive umask — **supported for tested paths / coverage gaps** | `config.ensure_private_dir` tightens STATE descendants; `config._write_private` uses 0600 temporary files and atomic replacement. `Installation.token` tightens cache leaf and file, rejects leaf symlinks and writes atomically. `github_pat.read_token` checks regular-file type, exact 0600 mode and owner. | `audit.test_private_write_replaces_leaf_symlink_without_overwriting_target` verifies normal write and hostile leaf symlink replacement under umask 000, target preservation, 0700 parent, 0600 output and no temporary leftovers. Reused `test_state_permissions.EnsurePrivateDirTests`, `test_state_permissions.StateFilePermissionsTests`, `test_github_app.InstallationTests.test_cache_is_private_atomic_and_installation_scoped`, `.test_symlink_cache_rejected`, `test_github_pat.PatTests.test_bad_token_files_fail_closed_secret_safe`. Tests establish final modes, not race-free protection against a same-user attacker. Ancestor symlinks, stat/open races, ACLs and chmod failures are not proven safe; `ensure_private_dir` suppresses chmod errors. PAT stat/open follows symlinks. |

Direct/unpooled URLs are intentional privileged backup inputs, not a pooling
mistake. **Argv:** no URL/password passed to pg_dump. **Error output:** URL
scrubbing works for tested forms but the defect below remains. **Environment:**
`run_env = dict(os.environ, ..., **libpq_env(url))` inherits unrelated daemon
variables as well as the decoded PG credentials. This is not a minimal child
environment; inherited PG settings not replaced by the URL may also influence
libpq. `pg_restore --list` gets ordinary inherited environment, not the explicit
dump environment. No claim is made that all credential exposure is eliminated.

`runner.prepare` also symlinks configured `link` entries into the worktree;
its comment says local services only, but this function does not inspect their
contents or enforce that restriction. Linked `.env` confidentiality therefore
depends on trusted configuration and worktree permissions. This audit did not
inspect any real linked files or establish that production credentials cannot
be linked. General subprocess sanitization, denylist and dependency auditing
remain outside this child issue's six bounded cases.

## Finding: decoded standalone backup passwords escape output scrubbing

Safe reproduction (mock subprocess only; emits a boolean, never the value):

```sh
python3 - <<'PY'
from mahler import backup, redact
from unittest.mock import patch
import subprocess, tempfile
from pathlib import Path
with tempfile.TemporaryDirectory() as tmp, patch.dict(redact.__dict__, _REGISTERED=set()):
    path = Path(tmp) / '.env'
    path.write_text('DATABASE_URL=postgresql://audit:audit%2Dcanary@db.invalid/db\n')
    spec = {'name': 'audit', 'env_file': str(path)}
    with patch.object(backup, 'tool', return_value='pg_dump'), patch.object(
        backup.subprocess, 'run', return_value=subprocess.CompletedProcess(
            [], 1, '', 'rejected value audit-canary')):
        try:
            backup.backup_postgres('audit', spec, root=tmp)
        except backup.BackupError as error:
            print('decoded password survives backup and generic redaction:',
                  'audit-canary' in redact.redact(str(error)))
PY
```

Observed: `decoded password survives backup and generic redaction: True`.
This is a controlled error-output injection, not evidence that a stock pg_dump
normally prints passwords. If a wrapper/tool/error path prints the decoded
value outside a recognized URL or assignment, it can enter backup events,
logs or notifications. The general redactor does not repair it. The earlier
#76 blanket clean-output conclusion does not establish this boundary.

Bounded remediation proposal: carry URL-derived encoded and decoded credential
values into a backup-local scrubber, scrub before truncation and at the common
exception/reporting boundary, and test dump/restore failures plus raised
subprocess exceptions. Do not fix it by putting secrets in argv or forcing pooled
connections. Production changes and a regression asserting the desired refusal
belong in linked follow-up work; no runtime change is included here.

## Method and validation

Four new scenarios materially extend existing coverage: competing URL variables
and missing-key refusal; the entire declared login set plus non-mutation; cache
metadata substitution followed by unavailable issuance; atomic private-write
behavior against a leaf symlink under umask 000. Every scenario includes a normal
path and adversarial input. All mocks and umask changes are restored; temporary
files are cleaned. No dependencies were added.

This is an audit-only issue with an explicitly read-only runtime write set, so
there is no behavior implementation for a red/green production-change cycle.
Tests were written and run before the report; they pass against the audited
runtime. The defect reproduction is retained separately rather than making a
passing regression contract out of unsafe behavior.

Verification results from CI at reviewed head
`8aae52219967537acb065d8f3ceb8f656130d67d` (review run 15108):

- `python3 -m mahler.repo_guard`: passed (exit 0).
- `python3 -m unittest discover -s tests`: passed, 3,846 tests.
- `python3 tests/run_random.py 417782092`: passed, 3,846 tests;
  randomized seed `417782092`.
- `python3 -X dev tests/run_strict.py`: passed, 3,846 tests.

Passing tests are bounded evidence, not proof of absence of vulnerabilities.
No manual owner acceptance is needed; live provisioning, process inspection and
race/ACL behavior remain untested limitations, not passes.
