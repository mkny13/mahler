# Test Suite Health & Failure Propagation Audit (#1699, part of #1698)

## Overview & Goal

Audit existing test execution gates (`tests/run_strict.py`, `tests/run_random.py`, `tests/test_suite_hygiene.py`) and establish regression self-tests proving that representative failures and isolation violations are strictly caught and rejected.

This is preventative audit work ensuring:
1. The strict runner (`python3 -X dev tests/run_strict.py`) catches ordinary test failures, `ResourceWarning` emissions, and destructor / unraisable exceptions collected during garbage collection.
2. The randomized runner (`python3 tests/run_random.py`) guarantees deterministic execution ordering for any chosen seed and reliably propagates test failures as non-zero process exits.
3. The suite hygiene guard (`tests/test_suite_hygiene.py`) rejects false-green tests across all five established patterns, with concrete gaps closed and static analysis limits documented.
4. Repository test isolation across environment, module globals, files, and SQLite is assessed against the rule that tests never touch live `~/.mahler` state.

## Four-Row Test Isolation Matrix

The repository contract (`AGENTS.md`, DESIGN D16) mandates that tests run isolated from each other in any random order and never mutate live operator state. The table below details how the four isolation dimensions are implemented in the test suite, with primary source evidence from `tests/test_ledger.py`, `tests/test_platforms.py`, and `tests/test_runner.py`.

| Dimension | Primary Mechanism & Patterns | Source References | Identified Gaps & Failure Modes |
| :--- | :--- | :--- | :--- |
| **Environment** | • Subprocess calls strip ambient Git config overrides via `_no_git_config_overrides` (`GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_*`, `GIT_CONFIG_VALUE_*`).<br>• Explicit `env` dictionaries passed to `subprocess.run`.<br>• Runner fixture subprocesses receive a temporary `HOME`, repository-only `PYTHONPATH`, and explicitly supplied fixture variables rather than the parent environment.<br>• Time-of-day configuration isolation (`claude_peak`). | • `tests/test_runner.py:26-35`: `_no_git_config_overrides` and `sh()` subprocess wrapper.<br>• `tests/test_runner.py:113`: Scoped `GH_CONFIG_DIR` redirection.<br>• `tests/test_suite_runners.py`: `run_runner` and `RunnerEnvironmentTests` verify state paths and credential variables under poisoned parent environment.<br>• `tests/test_escalation.py:86`: `claude_peak` disabled to isolate routing tests from ambient wall-clock peak hours. | • Direct assignment to `os.environ[k] = v` in tests without `mock.patch.dict` or `addCleanup(os.environ.pop, k, None)` leaves variables set if a test fails before cleanup, polluting subsequent randomized tests.<br>• Missing `HOME` isolation in ad-hoc subprocess calls can allow unintentional reads of `~/.mahler`.<br>• A clean environment is not an OS sandbox and does not prevent explicit absolute-path access.<br>• Time-of-day dependency: leaving `claude_peak` enabled in routing tests caused tests expecting Claude platforms to fail during weekday 05:00–11:00 PT peak windows. |
| **Module Globals** | • Scoped monkeypatching via `unittest.mock.patch.object` and `mock.patch` within `with` context managers.<br>• Explicit dependency injection of clock/time providers (`Clock`) rather than monkeypatching `datetime.now()` or `time.time`.<br>• `StubCtx` test doubles provide no-op handlers for scheduler notifications. | • `tests/test_ledger.py:19-28`: `Clock` instance injected into `Ledger(":memory:", clock=self.clock)`.<br>• `tests/test_platforms.py:800`: `mock.patch.object(platforms.subprocess, "run", ...)` inside `with` blocks.<br>• `tests/test_runner.py:54-58`: `mock.patch.object(config, "RUNS_DIR", ...)` in `setUp`/`tearDown`.<br>• `tests/test_escalation.py:64`: `StubCtx.hold` prevents missing-method errors during routing failure branches. | • Using `self.patch.start()` in `setUp()` and `self.patch.stop()` in `tearDown()`: if an exception occurs in `setUp()` after `start()`, `tearDown()` is not invoked, leaking the patched global.<br>• Global caches or module-level singletons in `mahler` modules must be reset between runs. |
| **Files & Filesystem** | • Every filesystem mutation uses `tempfile.TemporaryDirectory()` or `tempfile.NamedTemporaryFile`.<br>• Cleanup registered via context manager or `self.addCleanup(tmp.cleanup)`.<br>• Bare Git remotes and working trees are constructed inside temp directories. | • `tests/test_platforms.py:51, 61, 115, 124, 1000`: `with tempfile.TemporaryDirectory() as tmp:`.<br>• `tests/test_runner.py:40-59`: `SnapshotTests.setUp` builds `remote.git` and clone `repo` under `self.tmp`.<br>• `tests/test_suite_runners.py:39-44`: Per-test `TemporaryDirectory` for runner fixture suites. | • Unclosed file descriptors (e.g. `open()` without `close()` or context manager) leak open handles, which trigger `ResourceWarning` on Python 3.14+ under `run_strict.py`.<br>• Lingering temporary directories if cleanup fails during abrupt test abortion. |
| **SQLite State** | • In-memory databases via `Ledger(":memory:")` for all unit testing.<br>• Explicit connection closing registered with `self.addCleanup(self.led.close)`.<br>• File-backed SQLite tests (backups, optimization) use temporary directories.<br>• No test connects to `~/.mahler/mahler.db`. | • `tests/test_ledger.py:33-34`: `self.led = Ledger(":memory:", clock=self.clock)` and `self.addCleanup(self.led.close)`.<br>• `tests/test_ledger.py:1000`: Standalone in-memory instances.<br>• `.mahler/project.toml:13`: Explicit contract declaring `~/.mahler` prod state forbidden for tests. | • Failing to call `close()` on SQLite connections leaves open C handles until Python garbage collection. Under reference cycles, deallocation is delayed and may fail under strict GC sweeps.<br>• Database schemas must remain self-contained without depending on migrations on disk. |

## Five False-Green Detector Categories

`tests/test_suite_hygiene.py` statically parses the AST of test files (`test_*.py`) to prevent tests that accidentally pass without asserting anything. Five distinct failure categories are tracked:

### 1. No Assertion (`no assertion`)
- **Pattern:** Test methods that execute logic or `pass` but contain no `assert` statements, `self.assert*` calls, `self.fail()`, or approved helper calls.
- **Example:**
  ```python
  def test_example(self):
      pass
  ```
- **Detection:** Inspects `body_nodes(function)` for `ast.Assert`, `self.assert*`, `self.fail()`, or known helper functions. If none are found and the test is not in `ALLOWED`, it is rejected.

### 2. Hidden Assertion in Nested Definitions (`no assertion`)
- **Pattern:** An assertion placed inside a nested function, async coroutine, or local class definition that is never actually invoked.
- **Example:**
  ```python
  def test_example(self):
      def hidden():
          assert False
      async def async_hidden():
          assert False
  ```
- **Detection:** `body_nodes` yields only executable nodes in the outer function scope, explicitly skipping nested `ast.FunctionDef`, `ast.AsyncFunctionDef`, `ast.ClassDef`, and `ast.Lambda`. Consequently, assertions trapped inside uncalled nested blocks do not satisfy `has_assertion`.

### 3. Mock Assertion Missing `assert_` Prefix (`mock assertion missing assert_ prefix`)
- **Pattern:** Using non-existent mock assertion methods (such as `mock.called_once_with(...)`, `mock.not_called(...)`, `mock.has_calls(...)`, or `mock.any_call(...)`). In `unittest.mock.Mock`, accessing or calling these produces another mock or returns None without asserting, creating a false-green test.
- **Example:**
  ```python
  def test_example(self):
      mock.called_once_with(1)
      mock.not_called()
  ```
- **Detection & Gap Closed:** Detects when a call target's attribute name starts with `('called_', 'not_called', 'has_calls', 'any_call')`. Additionally, if such an attribute is referenced without parentheses (e.g. `mock.called_once_with`), the hygiene AST walker now detects it as an uncalled mock assertion missing `assert_` prefix.

### 4. Mock Assertion Referenced Without Calling (`mock assertion referenced without calling it`)
- **Pattern:** Referencing a valid mock assertion method (e.g. `mock.assert_called_once_with` or `mock.assert_not_called`) as an attribute access without invocation parentheses `()`. The method is retrieved but never evaluated, silently passing.
- **Example:**
  ```python
  def test_example(self):
      mock.assert_called_once_with
      mock.assert_not_called
  ```
- **Detection:** Compares all `ast.Attribute` nodes whose attributes start with `assert_` against the set of node function IDs present in `ast.Call`. If an `assert_` attribute is not called, a finding is generated.

### 5. `assertTrue` Second Argument is Not a Message Literal (`assertTrue second argument is not a message literal`)
- **Pattern:** Calling `self.assertTrue(actual, expected)` when `self.assertEqual(actual, expected)` was intended. In `unittest`, the second positional argument to `assertTrue` is the custom failure message (`msg`). If `actual` evaluates to truthy, the test passes regardless of the value of `expected`.
- **Example:**
  ```python
  def test_example(self):
      self.assertTrue(actual, expected)
      self.assertTrue(actual, 1)
  ```
- **Detection:** Inspects `ast.Call` where `func.attr == 'assertTrue'` and `len(args) >= 2`. If `args[1]` is not a string literal (`ast.Constant(str)`) or formatted string literal (`ast.JoinedStr`), it is rejected.

### Static Analysis Limits & Boundary Documentation

Static AST analysis provides lightweight, fast protection without executing test code, but has fundamental limitations:
1. **Dynamic Aliasing:** Assigning an assertion method to a local variable (e.g. `check = self.assertEqual; check(1, 1)`) or alias cannot be fully resolved statically.
2. **Dynamic Helper Prefixes:** Assertion helper functions not prefixed with `assert`, `check`, `_assert`, or `_check` are not recognized unless explicitly added to `ALLOWED`.
3. **Keyword Arguments:** `self.assertTrue(actual, msg=expected)` where `msg` is passed by keyword is not matched by positional `len(args) >= 2` inspection.
4. **`self.assertFalse(actual, expected)`:** Similar to `assertTrue`, passing an expected value to `assertFalse` as a second positional argument is treated as `msg`. However, existing codebase tests occasionally pass diagnostic objects (e.g. `self.assertFalse(result, error_dict)`) as failure diagnostics. Hardening `assertFalse` would require either typing `msg` or whitelisting diagnostics.

## Runner Gate Self-Test Evidence

`tests/test_suite_runners.py` verifies the test runners as isolated subprocess gates against real temporary fixture suites.

### Strict Runner (`tests/run_strict.py`)
Invocation: `python3 -X dev tests/run_strict.py [--start-directory DIR]`

1. **Clean Fixture Suite:**
   - Command: `python3 -X dev tests/run_strict.py --start-directory <tmp_clean>`
   - Exit Code: `0`
   - Stderr: `Ran 1 test in ... OK`
   - Verification: Confirms normal passing tests exit cleanly without false alarms.

2. **Ordinary Assertion Failure:**
   - Command: `python3 -X dev tests/run_strict.py --start-directory <tmp_fail>`
   - Exit Code: `1`
   - Stderr: `FAIL: test_assertion_fails`, `AssertionError: 1 != 2 : custom assertion failure message`
   - Verification: Confirms assertion failures propagate non-zero exit and identify the failing test and failure cause.

3. **ResourceWarning Emission:**
   - Command: `python3 -X dev tests/run_strict.py --start-directory <tmp_warn>`
   - Exit Code: `1`
   - Stderr: `ERROR: test_emits_resource_warning`, `ResourceWarning: unclosed socket handle simulated`
   - Verification: `warnings.simplefilter("error", ResourceWarning)` turns resource warnings into fatal test errors.

4. **Destructor / Unraisable Exception Finalized During Collection:**
   - Command: `python3 -X dev tests/run_strict.py --start-directory <tmp_unraisable>`
   - Exit Code: `1`
   - Stderr:
     ```text
     Exception ignored while calling deallocator <function LeakyResource.__del__ at ...>: <LeakyResource object at ...>
     Traceback (most recent call last):
       File ".../test_unraisable.py", line 6, in __del__
         raise RuntimeError('destructor finalized during collection boom')
     RuntimeError: destructor finalized during collection boom
     FAILED: 1 unraisable exception(s)
     ```
   - Verification: `gc.collect()` following `del suite` catches cyclical unraisable exceptions via `sys.unraisablehook`, prints the traceback, and exits with code 1.

### Randomized Runner (`tests/run_random.py`)
Invocation: `python3 tests/run_random.py [seed] [--start-directory DIR]`

1. **Repeatable Ordering with Identical Seed:**
   - Run 1: `python3 tests/run_random.py 987654321 --start-directory <tmp_order>`
     - Order recorded: `['gamma', 'alpha', 'beta', 'delta', 'zeta', 'epsilon']`
     - Exit Code: `0`
   - Run 2: `python3 tests/run_random.py 987654321 --start-directory <tmp_order>`
     - Order recorded: `['gamma', 'alpha', 'beta', 'delta', 'zeta', 'epsilon']`
     - Exit Code: `0`
   - Comparison: Order 1 exactly matches Order 2 (`order1 == order2`).
   - Run 3: `python3 tests/run_random.py 1 --start-directory <tmp_order>`
     - Order recorded: `['beta', 'alpha', 'delta', 'gamma', 'epsilon', 'zeta']`
     - Comparison: Order 3 differs from Order 1 (`order1 != order3`).
   - Verification: Demonstrates exact reproducibility when re-running with a reported seed.

2. **Failure Propagation:**
   - Command: `python3 tests/run_random.py 12345 --start-directory <tmp_fail>`
   - Exit Code: `1`
   - Stderr: `FAIL: test_propagates_failure`, `AssertionError: random runner must propagate failure`
   - Verification: Confirms test failures in randomized suites immediately propagate non-zero exit codes.

3. **Argument Flexibility & Discovery Isolation:**
   - Both runners now support optional `--start-directory` / `-s` or positional discovery directories while preserving default discovery (`tests`) when omitted.

## Bounded Unresolved Findings & Recommendations

1. **`assertFalse` Diagnostic Arguments:** `test_suite_hygiene.py` checks `assertTrue` second arguments to avoid `assertTrue(a, b)`, but `assertFalse` second arguments can also be misused. A future enhancement could check that `assertFalse(a, b)` second arguments are string literals, f-strings, or explicit `msg=` keywords.
2. **`setUp` / `tearDown` Cleanup Migration:** Tests using `mock.patch.object(...).start()` in `setUp()` and `.stop()` in `tearDown()` should migrate to `self.addCleanup(patcher.stop)` to guarantee teardown even if `setUp()` raises midway.
3. **SQLite Explicit Connection Management:** Ensure helper routines creating local `Ledger` instances register `addCleanup(led.close)` to avoid depending on garbage collector finalization.
