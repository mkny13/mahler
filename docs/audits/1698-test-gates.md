# Test gate isolation and failure-propagation audit

Issue #1699, part of #1698. Audited 2026-11-23 against Python 3.14.10.

This is a preventative audit of `tests/run_strict.py`, `tests/run_random.py`,
and `tests/test_suite_hygiene.py`. It does not claim a reproduced production
failure or that conventions provide an operating-system sandbox.

## Isolation matrix

The repository rule in `CLAUDE.md` and DESIGN D16 is that tests must not depend
on order or touch live `~/.mahler` state. The representative evidence below is
from the requested ledger, platform, and runner test modules; the gaps are
bounded observations, not a suite-wide rewrite.

| Dimension | Current mechanism and evidence | Boundary or unresolved gap |
| --- | --- | --- |
| Environment | Tests commonly scope mutations with `mock.patch.dict`; runner entry-point fixtures go further and launch with an allowlisted environment, temporary `HOME`, and no inherited Mahler or credential variables (`tests/test_suite_runners.py:18-37,50-87`). Git fixtures remove ambient `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_*`, and `GIT_CONFIG_VALUE_*` overrides before subprocesses (`tests/test_runner.py:26-35`). | General test subprocess helpers are not all allowlisted: for example, `tests/test_runner.py:32-35` otherwise inherits the parent environment. A clean environment is not an OS sandbox and cannot prevent code from explicitly opening an absolute live-state path. |
| Module globals | `mock.patch.object` scopes replacements, and newer multi-patch setup registers `addCleanup(patcher.stop)` immediately (`tests/test_runner.py:422-452`). Ledger tests inject a per-test `Clock` instead of changing process time (`tests/test_ledger.py:19-34`). | `SnapshotTests` starts a config patch in `setUp` but stops it only in `tearDown` (`tests/test_runner.py:38-59`); if later setup raises, `tearDown` does not run. Similar setup patterns remain candidates for migration to immediate `addCleanup`. The random runner detects only state that changes another test's result. |
| Files | Platform log/model fixtures use `TemporaryDirectory` and context-managed files (`tests/test_platforms.py:43-66,986-997`). Runner snapshot tests create their bare remote, clone, and run directory beneath one temporary root (`tests/test_runner.py:38-54`). Runner self-test source and order logs are also temporary (`tests/test_suite_runners.py:40-47,165-200`). | Temporary-directory cleanup is process-local, so forced termination may leave files for the OS janitor. `SnapshotTests` registers no immediate cleanup during setup, so a setup exception can retain its temporary tree until object finalization. The audit did not convert every filesystem fixture. |
| SQLite | The central lease fixture uses `Ledger(":memory:")` and immediately registers `close` (`tests/test_ledger.py:30-34`). Migration/reopen tests put file databases under a temporary directory and close each connection (`tests/test_ledger.py:189-211`); the named-file migration example registers unlink and connection cleanup (`tests/test_ledger.py:1671-1693`). | Some short local in-memory ledger tests still rely on finalization rather than explicitly closing (examples at `tests/test_ledger.py:640-676,990-997`). Strict-mode GC and `ResourceWarning` handling detect representative leaks but are a backstop, not deterministic resource management. No test in this audit opens the configured production database. |

## Five false-green detector categories

`tests/test_suite_hygiene.py:33-72` parses direct `tests/test_*.py` files and
rejects the following established patterns. The executable examples are in
`test_detector_rejects_false_greens` at lines 88-105.

1. **No assertion.** A `test_*` function containing only `pass` or ordinary
   calls is rejected unless its exact `file::name` has a non-empty explanation
   in `ALLOWED`.
2. **Assertion hidden in an uncalled nested definition.** Assertions inside a
   nested function, async function, class, or lambda do not prove the outer
   test checked anything; `body_nodes` deliberately skips those definitions.
3. **Mock assertion missing the `assert_` prefix.** Calls or bare references
   such as `mock.called_once_with`, `mock.not_called`, `mock.has_calls`, and
   `mock.any_call` are rejected. This audit closed the diagnostic gap for the
   bare-reference form, which was previously rejected only by the generic
   no-assertion rule.
4. **Mock assertion referenced but not called.** A valid-looking attribute such
   as `mock.assert_called_once_with` without `()` is rejected.
5. **Non-message second positional argument to `assertTrue`.** Patterns such as
   `self.assertTrue(actual, expected)` and `self.assertTrue(actual, 1)` are
   rejected because the second argument is a message, not an expected value;
   string and f-string diagnostics remain allowed.

### Static-analysis limits

- Detection is syntactic and name-based. It does not prove that a recognized
  helper actually asserts, that an assertion is reachable, or that an allowed
  exception's delegated check remains effective.
- Dynamically aliased assertion methods and helpers outside the recognized
  prefixes can be missed or require a documented `ALLOWED` entry.
- Discovery is the non-recursive `tests/test_*.py` glob; tests placed in nested
  directories are outside this hygiene check.
- The positional-message check covers `assertTrue`, not similar misuse of
  `assertFalse`; the current suite sometimes uses non-string diagnostic objects
  there, so expanding it requires a separate compatibility decision.
- AST checks cannot establish environment, file, database, resource, or
  inter-test isolation; the execution gates and coding conventions cover those
  separate concerns.

## Runner entry-point evidence

Both runner entry points retain their repository default (`tests`) and accept
`--start-directory/-s` solely so `tests/test_suite_runners.py` can discover a
temporary fixture suite without recursively launching the real suite.

The strict-runner subprocess tests establish:

- a clean one-test fixture exits 0 and reports `OK`;
- an assertion failure exits nonzero and reports the test plus
  `AssertionError: 1 != 2 : fixture assertion failed`;
- a `ResourceWarning` exits nonzero and reports both `ERROR: test_warns` and
  `ResourceWarning: fixture resource leaked`; and
- a cyclic object whose destructor raises during the runner's forced
  `gc.collect()` exits nonzero, reports `RuntimeError: fixture finalizer
  exploded`, and summarizes one unraisable exception.

The randomized-runner subprocess tests execute six cases twice with seed
`987654321`, assert that both complete orders are identical, and verify the
printed reproduction seed. A separate failing fixture run with seed `12345`
exits nonzero and reports `AssertionError: random fixture failure propagated`.

Each subprocess gets a temporary working directory and `HOME`, a repository
only `PYTHONPATH`, and explicitly supplied fixture variables. A poisoned-parent
test verifies that `MAHLER_HOME`, `MAHLER_RUNS_DIR`, `MAHLER_CONFIG`, sample
GitHub/AWS credentials, and an unrelated sentinel do not cross the boundary.

## Bounded unresolved findings

1. Migrate remaining `setUp` patchers and temporary directories to immediate
   `addCleanup` registration when those tests are next changed.
2. Close remaining local `Ledger` instances explicitly instead of depending on
   collection; strict mode remains the regression backstop meanwhile.
3. Decide separately whether `assertFalse(value, diagnostic_object)` should be
   restricted; changing it here would exceed the five existing categories.
4. The gates verify local fixtures and internal contracts. They do not validate
   external service response shapes; that separate concern is recorded in
   `docs/audits/1698-upstream-contracts.md`.

## Repository verification

Final command results, test counts, durations, and any failures are recorded
here after all four repository gates run against the committed implementation.
