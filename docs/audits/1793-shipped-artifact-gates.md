# Shipped-artifact gate coverage — #1793

Audited source commit: `6e4cd49cf6bb290ce368e56717d55a747fa9fd03` (current `origin/main`; the report commit sits on it).
Audit date: 2026-11-28. Scope: [#1793](https://github.com/mkny13/mahler/issues/1793).
This is a source-backed audit, not evidence that the live installation was updated.
Only this report changes. No install, release, publish, launchctl or paid SDK job
was run; no operator configuration was read or edited.

## Evidence and gate definitions

**Effective operator verify: unknown for every target.** The admitted run prompt
supplies the four repository checks, but no separately identified effective project
`verify` command. Neither `MAHLER_VERIFY` nor `MAHLER_VERIFY_COMMAND` is supplied.
Those facts do not establish the operator's setting; no live configuration was read.
The `verify` column below explicitly distinguishes that uncertainty from the known
repository contract. No distinct effective command can consequently be executed.

Repository commands, referred to as **V** below:

- [.mahler/project.toml](../../.mahler/project.toml): `fast = "python3 -m unittest discover -s tests"`, `full = "python3 -m unittest discover -s tests -v"`.
- [AGENTS.md](../../AGENTS.md): `python3 -m mahler.repo_guard`, `python3 -m unittest discover -s tests`, `python3 tests/run_random.py`, `python3 -X dev tests/run_strict.py`.
- [tests/run_random.py](../../tests/run_random.py) discovers `tests/`, flattens the suite and shuffles cases; [tests/run_strict.py](../../tests/run_strict.py) discovers the same directory, makes ResourceWarning an error, collects unraisables and fails on either test failure or unraisables. Neither adds a package/build target.

**C** is the single tracked workflow, [.github/workflows/ci.yml](../../.github/workflows/ci.yml):
`pull_request`, `merge_group`, and `push` to `main`. There are no path filters,
PR branch filters, job/step `if` conditions, matrix target selectors, reusable
workflows or release/tag triggers. One `test` job uses `ubuntu-latest`,
`actions/checkout@v7` with `fetch-depth: 0`, and `actions/setup-python@v7` with
Python `3.14`. Exact run commands:

```sh
python -m mahler.repo_guard
python -m mahler.consolidation_gate --base "$BASE_SHA" --head "$HEAD_SHA"
python -m unittest discover -s tests -v
python tests/run_random.py
python -X dev tests/run_strict.py
```

The consolidation step chooses base/head from the PR, merge group or push event;
its shell conditional replaces an all-zero base with
`git hash-object -t tree /dev/null`. [mahler/consolidation_gate.py](../../mahler/consolidation_gate.py)
checks lesson/promotion evidence, not built artifacts.
[mahler/repo_guard.py](../../mahler/repo_guard.py) checks `git ls-files` for forbidden
browser-profile paths; it does not compile executables. Test discovery is the only
artifact exercise in C. A green check does not imply unselected targets passed.

**R**: [.mahler/project.toml](../../.mahler/project.toml) defines release as
`merge to main`; there is no separate release workflow. [launcher/mahler-launcher](../../launcher/mahler-launcher)
fetches `origin/main`, inspects commit check-run conclusions, checks out the candidate,
then runs `python3 -m unittest discover -s "$APP/tests"` with cwd `$APP`.
This is an additional **post-merge deployment check**, not PR coverage. It runs
`"$APP/bin/mahler" tick` before advancing and exercises the new version on a later
tick. [DESIGN.md D17](../../DESIGN.md#d17--self-hosting-mahler-builds-mahler)
describes the known-good/launch-proven rollback contract. Initial
`launcher/install.sh` clones/checks out main without running those update gates.
Neither the presence of an install command nor historical live trials proves the
current artifact passes it.

## Finite inventory and reconciliation

Inventory sources read: `.github/workflows/ci.yml`, `.mahler/project.toml`,
`sdk_runner/pyproject.toml`, all tracked files under `bin/`, `launcher/`, `scripts/`
and `sdk_runner/`, [README.md](../../README.md), [ARCHITECTURE.md](../../ARCHITECTURE.md),
and [DESIGN.md](../../DESIGN.md) D11, D17, D20 and D41. `git ls-files` finds only
the CI workflow above and only the SDK Python package manifest. The following
reconciliation accounts for every file in the four finite entry-point directories:

- **T1** `bin/mahler`: source-tree control plane, including CLI, tick, console/serve,
  MCP and internal gateway subcommands. README and ARCHITECTURE identify them as
  commands of the same checkout, not separately built distributions. Console assets
  under `mahler/console/` are served by that checkout; no separate frontend bundle
  or build manifest is declared. The installed symlink is the same entry point.
- **T2** `launcher/mahler-launcher`: separately copied stable shell executable.
- **T3** `launcher/install.sh`: independently invoked installer, retained as a target
  because it determines the installed tree, executable, configuration and symlink.
- **T4** `launcher/local.mahler.plist`: separately rendered daemon launchd artifact.
- **T5** `launcher/local.mahler.serve.plist`: independently installed console launchd
  artifact; deliberately not installed by `install.sh`.
- **T6** `sdk_runner/pyproject.toml` + `sdk_runner/mahler_sdk_run.py`: one separate
  `mahler-sdk-runner` distribution, whose generated console script is
  `mahler-sdk-run = mahler_sdk_run:main`. Setuptools `py-modules` selects only
  `mahler_sdk_run`; Python >=3.14 and `claude-agent-sdk>=0.2.165` are separate
  requirements. Distribution and console script are reconciled into one row.
- **T7** `bin/purge-calendar-profile-history`: operator incident utility, retained
  despite being one-off because it is an explicitly invocable supported runbook tool.
- **T8** `scripts/post_merge_smoke.py`: standalone HTTPS smoke utility documented in
  DESIGN D11; retained even though it is not a smoke command for Mahler's own
  `mahler:<sha>` artifact identity.
- Excluded `sdk_runner/FINDINGS.md`: historical trial evidence, not an executable
  or build input. Excluded `sdk_runner/bootstrap/918-task.md`, `run-918.sh`, and
  `trial-918.py`: issue-specific historical bootstrap task and paid experiment,
  not selected by the package's `py-modules` or current install instructions.
  Excluded `sdk_runner/wire_capture_server.py`: local fake-server experiment helper
  documented only in FINDINGS; not installed in the distribution or deployed as a
  service. These exclusions do **not** assert those scripts are checked. The
  bootstrap shell is text-inspected by `tests/test_api_credits.py`, which is not
  execution coverage. Historical trial results are not current release gates.
- **T9** `config.example.toml`: the only template `launcher/install.sh` copies into
  operator state (`~/.mahler/config.toml`), and README documents it as the first-install
  config; retained as an installed artifact rather than excluded.
- README/ARCHITECTURE/DESIGN, manifests, recipes and other source
  resources travel with the checkout; they are not independently installed executable
  targets. External agent CLIs, Tailscale, Git and launchd are prerequisites, not
  artifacts Mahler builds. No Xcode app, Android package, container or published
  core Python wheel is declared by this inventory; those target classes are not applicable.

## Artifacts × gates

Each V/C reference below expands to the exact commands and paths above. “Exercised”
means the stated boundary only, not all behavior or the installed production instance.
An **uncovered** sub-boundary has exactly one G remediation link per gate; the same
issue owns both repository-verify and PR-CI coverage for that artifact. “Unknown
operator” applies independently of repository coverage.

| Artifact/target | verify | PR CI | release-only |
| --- | --- | --- | --- |
| T1 — control plane / `bin/mahler` | **Unknown operator**; V exercises Python modules via `tests/test_cli.py` (`build_parser` and handlers), `tests/test_tick.py`, `tests/test_serve.py`, `tests/test_mcp.py`, `tests/test_console.py`. **Uncovered** real executable wrapper/startup from another cwd: [G1 / #1794](https://github.com/mkny13/mahler/issues/1794); the launcher tests replace `bin/mahler` with a stub. | C runs those same tests through `python -m unittest discover -s tests -v` and both runners in `.github/workflows/ci.yml`. **Uncovered** real `bin/mahler` wrapper: [G1 / #1794](https://github.com/mkny13/mahler/issues/1794). Console JS/browser coverage is conditional, as described below. | R runs unit discovery in `$APP`, then `"$APP/bin/mahler" tick` in `launcher/mahler-launcher`; `launcher/install.sh` creates the CLI symlink. First evidenced real installed wrapper execution is deployment/runtime, too late to gate PRs. `mahler/launch_health.py:exact_live` also invokes installed `bin/mahler version`; enabling that live strategy is unknown here. |
| T2 — `launcher/mahler-launcher` | **Unknown operator**; V discovers `tests/test_launcher.py`, whose `run_launcher` executes `['/bin/sh', str(LAUNCHER)]` against temporary Git repositories and stubbed gh, python3 and bin/mahler. Shell parsing and update/rollback behavior are exercised pre-merge. | C discovers the same `tests/test_launcher.py` via unittest and both runners in `.github/workflows/ci.yml`; real shell script, simulated external commands. | `cp "$SRC/launcher/mahler-launcher" "$M/bin/mahler-launcher"` and `chmod +x` in `launcher/install.sh`; `/bin/sh __HOME__/.mahler/bin/mahler-launcher` in `launcher/local.mahler.plist` is the runtime invocation. First actual copy/launchd integration is manual installation, not a separate automated release test. |
| T3 — `launcher/install.sh` | **Unknown operator**; V discovers `tests/test_launcher_install.py`, which runs `/bin/sh launcher/install.sh` against an isolated local Git origin and temporary HOME (initial install, rerun preserving config, missing-source and unreachable-origin failures). Real parse/execution pre-merge; [G2 / #1795](https://github.com/mkny13/mahler/issues/1795) gap closed by commit 6e4cd49 (#1803). | C discovers the same test via `python -m unittest discover -s tests -v` and both runners in `.github/workflows/ci.yml`; no dedicated installer step or selector. | `launcher/install.sh` in README “Install the daemon” against the real remote/`~/.mahler` is the production run; it does not start the daemon. Manual install is not a PR gate, but the script is already exercised pre-merge. |
| T4 — `launcher/local.mahler.plist` | **Unknown operator**; V discovers `tests/test_launchd_templates.py`, which substitutes `__HOME__` (home with spaces), parses with `plistlib.loads` and asserts label, `/bin/sh` + launcher path, 60 s interval, PATH and log paths; malformed XML fails. [G3 / #1796](https://github.com/mkny13/mahler/issues/1796) gap closed by commit ed6410b (#1801). | C discovers the same test via unittest and both runners in `.github/workflows/ci.yml`. | README's `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.mahler.plist` is the first launchd load; `launcher/install.sh` renders it (also covered by T3's test). Loading by launchd itself is not automated and was not run. |
| T5 — `launcher/local.mahler.serve.plist` | **Unknown operator**; V discovers `tests/test_launchd_templates.py` console cases: render, `plistlib.loads`, assert label, `bin/mahler serve --port 8787`, KeepAlive, PATH/log paths. [G4 / #1797](https://github.com/mkny13/mahler/issues/1797) gap closed by commit 6249a92 (#1802). | C discovers the same test via unittest and both runners in `.github/workflows/ci.yml`. | README documents `sed "s\|__HOME__\|$HOME\|g" launcher/local.mahler.serve.plist > ~/Library/LaunchAgents/local.mahler.serve.plist` then `launchctl bootstrap gui/$(id -u) …`; not run by `launcher/install.sh`. launchd load is manual and was not run. |
| T6 — SDK distribution / `mahler-sdk-run` | **Unknown operator**; V imports real `sdk_runner/mahler_sdk_run.py` and executes `_run` with fake SDK in `tests/test_discuss_credits.py:SDKDiscussionTests`. **Uncovered** package build, installed dependencies and generated entry point from `sdk_runner/pyproject.toml`: [G5 / #1798](https://github.com/mkny13/mahler/issues/1798). Adapter argv tests in `tests/test_platforms.py` are not package execution. | C runs the same source test, but `.github/workflows/ci.yml` never builds a wheel or installs the separate SDK requirements. **Uncovered** distribution/console script gate: [G5 / #1798](https://github.com/mkny13/mahler/issues/1798). | `uv tool install ./sdk_runner` in DESIGN D41 is the first evidenced package build/install path. `sdk_runner/FINDINGS.md` records historical `mahler-sdk-run --prompt-file … --cwd … --model …` trials, not a recurring gate. Current install success is **unknown**: no install run here; the manifest's `readme = "README.md"` has no tracked `sdk_runner/README.md`. |
| T7 — `bin/purge-calendar-profile-history` | **Unknown operator**; V executes the actual tool via subprocess in `tests/test_purge_calendar_profile_history.py`, against synthetic repositories, with a git-filter-repo shim. This parses and exercises its real entry point pre-merge; the external filter-repo implementation is outside that proof. | C discovers `tests/test_purge_calendar_profile_history.py` through unittest and both runners in `.github/workflows/ci.yml`; same synthetic executable coverage. | **Not applicable** as a separate release gate: `.mahler/project.toml` ships source on merge; `bin/purge-calendar-profile-history` documents explicit incident-only invocation and remote-write confirmation, not a release job. Its destructive operator workflow was not run. |
| T8 — `scripts/post_merge_smoke.py` | **Unknown operator**; V executes `runpy.run_path(str(SCRIPT), run_name='__main__')` in `tests/test_post_merge_smoke.py`, with mocked HTTPS opener and isolated env. Parses and exercises the script/report contract pre-merge, not live networking. | C discovers `tests/test_post_merge_smoke.py` via unittest and both runners in `.github/workflows/ci.yml`. The script's post-merge name does not make these tests release-only. | DESIGN D11 documents `smoke = "python3 /Users/mike/Mahler/scripts/post_merge_smoke.py"` for HTTPS artifacts. **Unknown** effective configuration; no workflow calls it. **Not applicable** as Mahler's own live artifact smoke: script `validate_url` requires HTTPS, whereas D11's Mahler detector returns `mahler:<sha>`. No live smoke run here. |
| T9 — `config.example.toml` (installed as `~/.mahler/config.toml`) | **Unknown operator**; V discovers `tests/test_config.py` (`config.load` of the example at lines ~50, ~793, ~834) and `tests/test_onboarding_docs.py` (`config.load(str(ROOT / 'config.example.toml'))`, documentation consistency). Parses and validates the template pre-merge; this is the config-loader, not the installer copy. | C discovers the same tests via unittest and both runners in `.github/workflows/ci.yml`. | `[ -f "$M/config.toml" ] || cp "$SRC/config.example.toml" "$M/config.toml"` in `launcher/install.sh` (line 18) is the install step; the copy is exercised pre-merge by `tests/test_launcher_install.py` (T3). The live `~/.mahler/config.toml` is operator state and was not read. |

For T1, `tests/test_console.py` invokes `node -e` and
`node tests/console_graph_test.js` only when Node is present; its headless Chrome
layout tests may skip when Chrome is absent or fails startup twice.
`tests/test_serve.py` also conditionally invokes Node. C installs Python explicitly,
not Node/Chrome; runner-image availability is not a guaranteed gate contract.
These are conditional source UI exercises, not proof of a separately compiled app.
Actual skipped cases must remain visible in verification results. Browser tests
are headless; this audit performs no desktop-driving journey.

## Deduplicated remediation register

Read-only history search: `mahler history mahler 'shipped-artifact' --limit 3`
returned prior #614 and current sorting history. #614 implemented the recurring
pass, not these concrete target gates.

On 2026-11-28, repository issue search used the supplied scoped run token with
`gh issue list -R mkny13/mahler --state all --search '<query>' --limit 100
--json number,title,state` (initial searches also returned bodies/URLs, never
comments). Queries included `launcher plist installer`, `sdk runner package`,
`artifact gate`, `bin/mahler smoke`, `"plist"`, `"sdk" "package"`,
`"gate coverage"`, `"installer" "gate"`, `"plist" "test"`,
`"sdk" "wheel"`, and `"entry point" "smoke"`.
The broad `"launcher" "test"` query reached 100 results, so it was not treated as
exhaustive; the narrower queries above did not reach the limit. Bodies of #614,
#1632 and #1779 were checked: respectively recurring-audit implementation,
dependency-security audit and onboarding documentation, not matching executable
artifact gate fixes. No exact matching remediation was found. No raw comments
were fetched and no ambient human credentials were used.

`gh issue create -R mkny13/mahler --title '<title>' --body-file -` succeeded for
all five issues below. G2–G4 were subsequently implemented on `main` (commits 6e4cd49,
6249a92, ed6410b); the rows above reflect that current coverage and those issues
are retained only as history. Only G1 and G5 remain open gaps. Each body links back to #1793, names both missing repository
verify and PR-CI gates, cites evidence, gives proposed commands and acceptance
checks. There are no unresolved lookup/filing errors. One issue is reused for
both missing gates of the same target, avoiding duplicate implementation tickets.

- **G1 [#1794](https://github.com/mkny13/mahler/issues/1794):** add
  `tests/test_cli_entrypoint.py`; invoke the absolute real `bin/mahler --help`
  and `bin/mahler version` from an unrelated temporary cwd with temporary HOME and
  MAHLER_HOME. Acceptance: `python3 -m unittest discover -s tests -p test_cli_entrypoint.py`;
  assert output/exit and detect broken import/executable permission in fixtures.
- **G2 [#1795](https://github.com/mkny13/mahler/issues/1795):** add
  `tests/test_launcher_install.py`; run `/bin/sh launcher/install.sh` against an
  isolated source fixture/local origin and temporary HOME, not the real source
  remote. Assert clone, copy, executable mode, symlink, rendering and preservation
  of existing config on rerun. Acceptance:
  `python3 -m unittest discover -s tests -p test_launcher_install.py`; omitting
  a copy/symlink operation in a fixture must fail.
- **G3 [#1796](https://github.com/mkny13/mahler/issues/1796):** daemon template
  cases in `tests/test_launchd_templates.py`; render a home containing spaces,
  parse using `plistlib.loads`, assert label, `/bin/sh` plus launcher path,
  60-second interval, PATH/log paths and absence of `__HOME__`. Acceptance:
  `python3 -m unittest discover -s tests -p test_launchd_templates.py`; malformed
  XML and wrong executable must fail without invoking launchctl.
- **G4 [#1797](https://github.com/mkny13/mahler/issues/1797):** console template
  cases in that same test module; assert label, actual CLI path, `serve --port 8787`,
  KeepAlive, PATH/log paths and full substitution. Same executable acceptance
  command; corrupted XML/command must fail. Shares a harness with G3 but covers a
  separately installed artifact and has one distinct issue.
- **G5 [#1798](https://github.com/mkny13/mahler/issues/1798):** introduce a disposable
  SDK packaging verification script, called from PR CI and the repository full
  verify contract. With a fresh temporary `TMP` and cleanup, proposed commands:

  ```sh
  python3 -m venv "$TMP/sdk"
  "$TMP/sdk/bin/python" -m pip wheel --no-deps ./sdk_runner --wheel-dir "$TMP/wheels"
  "$TMP/sdk/bin/python" -m pip install "$TMP"/wheels/mahler_sdk_runner-*.whl
  "$TMP/sdk/bin/mahler-sdk-run" --help
  "$TMP/sdk/bin/python" -c 'from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query'
  ```

  Acceptance: build from a clean checkout, run the installed CLI from an unrelated
  cwd, assert metadata/entry point/exit, and prove broken `py-modules` or script
  mapping fails. This verifies installation/import, not paid model behavior. The
  missing README reference needs resolution or explicit build evidence in that
  issue; this audit does not guess whether setuptools rejects it.

G1–G4 enter the existing unittest discovery gates without changing operator
configuration. G5 requires an explicit new repository/CI package check; installing
only the core suite's standard-library dependencies cannot cover it.

**Operator-only proposal (not applied):** after the repository fixes establish a
`scripts/verify_sdk_runner.sh` implementation for G5, propose the exact effective
command below to the owner. The script name is a recommendation, not an existing
file. The current effective command remains unknown; no configuration issue is
asserted from missing evidence.

```sh
python3 -m mahler.repo_guard && python3 -m unittest discover -s tests && python3 tests/run_random.py && python3 -X dev tests/run_strict.py && /bin/sh scripts/verify_sdk_runner.sh
```

Applying an operator `verify` change requires the owner; repository fixes proceed
through #1794–#1798. This audit neither changes that setting nor requests an owner
verdict to complete the report.

## Validation record

A failing-first report existence assertion failed before writing this document.
The report adds no behavioral implementation or tracked tests. A mechanical check
must verify exactly T1–T9 once each, four cells per row, path-backed classifications,
and a single linked remediation per uncovered cell. The four AGENTS.md commands
are run against the local report commit; their completion evidence is recorded in
the build run's final summary so the checked commit is not changed afterward.
Effective operator verify remains unknown, not a claimed pass.
