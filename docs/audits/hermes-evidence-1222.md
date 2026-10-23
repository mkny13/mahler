# Hermes pinned-source execution evidence — #1222

Follow-up to issue #1141 and PR #1168 (reviewed head
`28ad597861307c5b38c7f3f6b54bfb772ee62c4a`). This is a fresh execution on
2026-10-23, not a reconstruction of the original run.

Mahler source commit: `3c3ea5cc84ff14895531fef9e75498a20e9fca63`.
Check: `tests/verify_hermes_contract.py`, executed from
`/Users/mike/.mahler/worktrees/mahler/1222-run13213` using Python 3.14.3.

Command: `python3 tests/verify_hermes_contract.py`.
Exit status: `0`.

Exact stdout (seven lines, including final newline):

```text
PASS: HERMES_HOME environment redirection isolates paths.
PASS: CLI parser accepts -q, --format stream-json, --in, and -z.
PASS: Finite turn exit codes verified (0=ok, 1=empty, 2=failed, 130=SIGINT).
PASS: Unconditional approvals.deny rule matching against DENY_STEMS verified.
PASS: auth.adopt_external_logins: false prevents external login adoption.
PASS: Anonymous guest inference restricted to desktop backend (rejected on CLI).
ALL PINNED-SOURCE CONTRACT CHECKS PASSED (exit code 0).
```

Exact stderr (source resolution, including upstream path and SHA):

```text
Using Hermes source: /Users/mike/.mahler/worktrees/mahler/1222-run13213/scratch-1222/hermes_pinned_so8xb7ca (commit d923a5eb487a17c0a68801bba9ef0f56c81040a8)
```

The script shallow-fetched public NousResearch/hermes-agent tag `v0.22.2`,
verified HEAD equals `d923a5eb487a17c0a68801bba9ef0f56c81040a8`, and removed its temporary
source checkout on completion. The source path above identifies this execution;
rerunning the command creates a new temporary checkout.

Execution conditions: the child process received only PATH, an empty isolated HOME
(`/Users/mike/.mahler/worktrees/mahler/1222-run13213/scratch-1222/home`), TMPDIR inside this worktree,
GIT_CONFIG_NOSYSTEM=1, GIT_CONFIG_GLOBAL=/dev/null, GIT_TERMINAL_PROMPT=0,
and PYTHONDONTWRITEBYTECODE=1. No account or credential was supplied or used by
this check, no Nous Portal account was created, no paid tier or credits were
enabled, and no network inference was performed. Network access was used only
to fetch public source. Existing Claude/Codex credentials were not read or
adopted. No global Hermes installation or real `~/.hermes` state was modified.
The check uses standard-library stubs for configuration/authentication/YAML;
these are provider-independent contract checks, not a live authenticated run.

The capture asserted exit status 0, byte-for-byte stdout equality with
`EXPECTED_OUTPUT` (including its trailing newline), and the pinned SHA in stderr.
No product behavior changed, so no new failing-first behavior test applies.
No manual checks remain for this evidence-only follow-up; live authenticated
inference remains outside the investigation's scope. The conductor publishes
this committed evidence in the follow-up PR audit trail; this build does not
edit the original PR or post issue comments.

Repository verification on this evidence-only change (all exit status 0):

- `python3 -m mahler.repo_guard`: passed.
- `python3 -m unittest discover -s tests`: 3,111 tests, OK (skipped=1).
- `python3 tests/run_random.py`: seed `405811458`, 3,111 tests, OK (skipped=1).
- `python3 -X dev tests/run_strict.py`: 3,111 tests, OK (skipped=1).
