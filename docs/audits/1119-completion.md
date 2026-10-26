# Completion audit: project console and timed Prioritize (#1119)

Audit for #1249, 2026-10-25 local (2026-10-26 UTC), macOS, Python 3.14.3,
Node v25.6.0. Audited base: `e159e61fa36f3b996f854136621253399d408bec`;
`git rev-parse HEAD origin/main` returned that SHA twice before any edits.
The working tree was clean. Product code remains identical to that base.
This is an evidence-only change, so no behavior change or failing-first product
regression test is introduced. Existing executable checks and a rendered-fixture
probe below establish both supported behavior and a remaining gap.

## Assessment

SUPPORTED means the named source and automated contracts substantiate the criterion;
it does not imply a real-browser visual walkthrough. Criterion 7 tracks that separately.

| Parent criterion (in checklist order) | Assessment | Evidence / remaining work |
| --- | --- | --- |
| 1. #1121 merged; URL scope for Needs you, Backlog, Releases, Stats and Event stream on desktop/phone; global views remain global | SUPPORTED | Merge ancestry; exact two-project filtering, HTTP query/fragment persistence, both layout navigation contracts |
| 2. #1122 merged; six headline measures, inline questions, active/queued/blocked, six shipping stages/explainer, dependencies, completion/activity, briefs/releases/trend for one project | SUPPORTED (re-verified by #1257 after #1254/#1256) | Probe and tests on `c7ccb4787a4ffd7a20ea74faf2215ea78cd8611b`, plus browser journey step `b` at desktop and phone; see "Criterion 2 re-verification" |
| 3. #1123 merged; one durable 1h/1d/1w boost, replacement/stop/expiry events, ordering ahead of issue/static priority, CLI/console, unchanged concurrency/running work | SUPPORTED | Ledger, scheduler, CLI, console action and rendering contracts below |
| 4. #1124 merged; targeted atomic 1–64 max_parallel; project pause/resume preserves runs and visibility | SUPPORTED | Config boundaries/byte preservation, fresh config reload, atomic replacement, scheduler and both layout contracts |
| 5. Committed approved mockup, integrated console design, allocated Prioritize decision | SUPPORTED | Mockup introduced by #1161; console design sections and DESIGN D51 |
| 6. Four required checks pass on merged base | SUPPORTED | All four commands exited 0; 3,259 tests per suite, one opt-in browser smoke skipped |
| 7. Gated integrated desktop/phone walkthrough | OUTSTANDING (#1257: journey ran; one product defect) | Journey ran through the gate; every step passed except `a1`, which failed on product defect [#1274](https://github.com/mkny13/mahler/issues/1274). See "Criterion 7 re-run" |

No owner-only decision is needed to finish this audit. Existing visual/taste notes
on #1119 are not an owner-pass gate: shipments complete through automated evidence
or 14 quiet days; a reported failure becomes linked p1 work (D10).

## Merge and supersession evidence

For each row, executed `git merge-base --is-ancestor <merge> <base>` and
`git show --stat --oneline <merge>`. All ancestry exits were **0**.
Merge times below are from the admitted issue context; #1248 was also confirmed
through `gh pr view 1248 --repo mkny13/mahler --json number,state,mergedAt,mergeCommit,title`.

| Child | PR | Merged UTC | Merge commit | Ancestry |
| --- | --- | --- | --- | --- |
| #1121 | #1142 | 2026-10-20T16:58:57Z | `4776193899ca3480d4fed8fe4807e5b714438e22` | 0 |
| #1122 | #1161 | 2026-10-21T04:33:17Z | `b2303e1d76ee085058df15ca2ddb7cc6cf4febf2` | 0 |
| #1124 | #1162 | 2026-10-21T05:25:15Z | `01aa041cc935fde8a502b9f6a2125defc2fa23bb` | 0 |
| #1123 | #1248 | 2026-10-25T22:14:17Z | `d5e42559ca9c26de9d2bfdcadef8d3cebc9fb884` | 0 |

PR #1164 is **CLOSED**, `mergedAt: null`, as confirmed by
`gh pr view 1164 --repo mkny13/mahler --json number,state,mergedAt,title`.
It is superseded by **MERGED #1248**, the same titled Timed project Prioritize
implementation, per admitted context and #1248's matching merge metadata.
#1164 is not counted as a merge. No raw issue comments were fetched.

The merge stats identify the original focused evidence: #1142 changed
`test_console`, `test_console_state`, `test_console_selection`, `test_serve` and
`console_selection_test.js`; #1161 changed console/state tests; #1162 changed
config, console state/boundary and schedule tests; #1248 changed ledger, schedule,
CLI and console/state/boundary tests. All referenced Python modules are in `tests/`.

## Criterion 1: scope

`console.state.build(..., project_scope=...)` validates enabled project names,
filters Needs you/backlog/releases/stats/events exactly, and computes global
quota/settings independently. `_events` filters before limits. `serve` forwards
the URL project to state; `page._scope_switcher` and console JS preserve it across
navigation. Project navigation hides Capacity, Models and Settings; requesting
a global-only section in project scope falls back to Overview.

Executed named contracts:
- `ProjectScopeStateTests.test_project_views_filter_exactly_and_menu_uses_snapshot_status`
  covers all five named views and both projects' counts, version/status and landing.
- `test_unknown_and_disabled_scope_falls_back_to_all_projects` and
  `test_project_event_limits_apply_after_filtering` cover fallback/filter order.
- `ProjectScopePageTests.test_switcher_and_project_navigation_render_on_both_layouts`
  asserts desktop/phone switchers, scoped navigation and global-only fallback.
- `SelectionTests.test_project_navigation_renders_only_scoped_sections_and_global_scope_falls_back`
  and `SelectionHTTPTests.test_project_fragment_and_full_page_scope_survive_selection`
  cover reloadable full-page/fragment navigation. `BrowserSelectionTests` executes
  the existing Node DOM harness, not a real browser walkthrough.

## Criterion 2: Overview, with phone gap

`state._project_overview_state` constructs open (7-day delta), active, queued
(next item), waiting-on-you (age), blocked-other and done-7d (last shipment age).
`_blocked_overview_rows`, `_queued_overview_rows`, `_completed_7d_rows`,
`_shipping_rows` and `_how_a_change_ships` supply the corresponding evidence.
`page._desktop_overview` renders inline answer controls, active work, queued/blocked,
six stage dots, explainer, scoped graph, completed rows and activity.
The project sidebar uses `_project_side_brief`, release data and
`_project_side_trend`; state filters their project before rendering.

Executed `ProjectOverviewStateTests` includes:
- `test_overview_six_headline_measures_and_two_project_isolation` (all six values,
  next queued item, waiting age, last shipment age and no other-project rows).
- `test_shipping_rows_mechanically_distinguish_all_seven_finite_fixtures` (built/no
  PR, pending CI, named red check, reviewing platform, failed review/fixing,
  merged quiet 1/14d, verified; six stages for each fixture).
- `test_how_a_change_ships_explainer_copy` (all six stage names and 14 quiet days).
- `test_blocked_rows_named_reasons` (question, dependency and parked CI reasons).

`ProjectOverviewPageTests.test_desktop_overview_renders_approved_hierarchy_and_reused_actions`
asserts headline, inline answer action, active/queue/block, shipping, dependencies,
completed/activity, brief acknowledgement, release action and 30-day trend.
`test_phone_overview_condensed_tab` passes but asserts only a subset.

**UNSUPPORTED subclauses on phone:** six-stage shipping evidence/explainer,
dependencies, completion/activity and backlog trend. `_phone_overview` renders
shipping subtitle rows but does not use stage arrays or explainer; its final body
join excludes the graph, completed rows, activity and trend. `docs/console/design.md`
"Phone Condensation" documents this smaller subset; that is not evidence that the
parent's stronger same-content requirement has been met. Desktop remains supported.

Executed reproduction (exit 0; observation, not a regression test):

```sh
PYTHONPATH=tests python3 - <<'PY'
from test_console import ProjectOverviewPageTests
from mahler.console import page, state
case = ProjectOverviewPageTests()
case.setUp()
try:
    s = state.build(case.cfg, case.led, project_scope='groundwork')
    for label in ['How a change ships', 'Dependencies', 'Completed · last 7 days',
                  'Activity', 'Open issues · 30 days']:
        print(repr(label),
              'desktop=', label in page.app(s, 'desktop', view='overview'),
              'phone=', label in page.app(s, 'phone', tab='overview'))
finally:
    case.doCleanups()
PY
```

All five lines printed `desktop= True phone= False`.
Filed **#1254**, `type:bug`, `p1`, `Part of #1119`, with escape analysis and the
required failing-first phone rendering regression check. No product repair is
included in this audit.

## Criterion 3: timed priority

`Ledger.start_priority_boost`, `priority_boost`, `stop_priority_boost` and
`expire_priority_boost` use one durable KV record and ledger transactions/events.
`LeaseTests.test_priority_boost_durations_replacement_stop_and_expiry` checks
1/24/168-hour durations, extension, replacement, matching stop and event payloads;
`test_priority_boost_corruption_and_expiry_are_safe_and_idempotent` checks one
expiry event; `test_priority_boost_survives_ledger_reopen_as_one_record` proves
persistence and uniqueness. Invalid durations and malformed records are covered.

`tick._order_candidates` puts boosted-project key before `it['priority']`, scarcity
and static `priority_projects`. `FairnessTests.test_boost_precedes_priority_labels_and_static_priority_projects`,
`test_expired_boost_does_not_affect_candidate_order` and
`test_boost_does_not_bypass_project_capacity` exercise those boundaries.
`SchedulerPriorityBoostExpiryTests.test_tick_expires_boost_with_no_candidates_and_ignores_corruption`
executes scheduler reconciliation without candidates.

`PrioritizeTests.test_start_and_stop_use_shared_ledger_state` and its rejection
case exercise the CLI parser/handler. `LedgerOnlyActionsStayLocalTests.test_priority_boost_actions_require_confirmation_for_replacement`
executes console start/extend/replace/stop, refusing unconfirmed and stale replacements.
`PriorityBoostConsoleTests.test_overview_menu_active_controls_and_switcher_marker`
covers both layouts' duration controls, banner and marker. Boost operations mutate
KV/events only; ordering only sorts candidates, with existing capacity/eligibility
checks retained. No concurrency configuration or active-run cancellation is written.
The capacity test keeps an existing boosted run occupying its slot while another
project receives the free slot. This is source/contract evidence, not browser clicks.

## Criterion 4: project controls

`actions.set_max_parallel` validates the enabled project and strict integer 1–64.
`config.set_project_max_parallel` reloads the current file, changes only the
selected project value and uses `_write_private` (`os.replace`) for atomic file
replacement. This is atomic replacement, not a claim of transaction isolation
against an unrelated writer racing the file read/replace interval.

Executed `SetProjectMaxParallelTests`: `test_changes_only_that_project_value`,
`test_rejections_leave_file_byte_identical`, `test_boundaries_accepted`.
Executed `ProjectHeaderControlsTests`: `test_state_and_both_layouts`,
`test_min_boundary_and_paused_project_stays_visible`,
`test_stepper_action_preserves_other_config_changed_after_render`, and
`test_pause_resume_idempotent_and_validated`. They cover effective global capacity,
1/64 button boundaries, fresh file reload, idempotence and isolated events.

`actions._set_project_pause` writes a project KV flag and event in one ledger
transaction; `tick` checks that flag before producing new candidates.
`ProjectPauseTests.test_paused_project_starts_nothing_and_other_project_takes_the_slot`
asserts no new project launches and its existing run remains active/countable;
`test_resumed_project_is_eligible_again` checks resume. The project stays enabled
and reachable, as the both-layout state/rendering contracts assert.

## Criterion 5: design artifacts

`docs/console/mockups/project-view.html` is tracked and was introduced in
`b2303e1` (#1161); `git log -1 --format='%h %s' -- <path>` confirms it.
`docs/console/design.md` contains "Project scope and Overview (mahler#1119,
mahler#1122)", "Overview header controls (mahler#1124)", Shipping Stage Evidence
Rules, Phone Condensation and timed Prioritize copy. `DESIGN.md` contains allocated
**D51 — Timed project priority (mahler#1123)**, introduced by #1248. Presence and
provenance are verified; subjective pixel/taste approval is not claimed.

## Verification

All commands run from this worktree on the audited product base. Tests use their
existing isolated fixtures; no real project concurrency, pause or boost was changed.

Focused command (exit 0, **88 tests, 0.825s, OK**):

```sh
PYTHONPATH=tests python3 -m unittest -v \
  test_console_state.ProjectScopeStateTests test_console.ProjectScopePageTests \
  test_console_selection.SelectionTests test_console_selection.SelectionHTTPTests \
  test_console_state.ProjectOverviewStateTests test_console.ProjectOverviewPageTests \
  test_console_state.PriorityBoostConsoleTests test_console_state.ProjectHeaderControlsTests \
  test_config.SetProjectMaxParallelTests test_schedule.FairnessTests \
  test_schedule.SchedulerPriorityBoostExpiryTests test_schedule.ProjectPauseTests \
  test_cli.PrioritizeTests test_ledger.LeaseTests
```

Additional command (exit 0, **12 tests, 0.632s, OK**):

```sh
PYTHONPATH=tests python3 -m unittest -v test_console_boundary \
  test_console_selection.BrowserSelectionTests test_serve.TestPages
```

| Command | Exit | Summary | Test duration |
| --- | --- | --- | --- |
| `python3 -m mahler.repo_guard` | 0 | No output; guard passed | 0.042s wall |
| `python3 -m unittest discover -s tests` | 0 | Ran 3259 tests; OK (skipped=1) | 176.152s |
| `python3 tests/run_random.py` | 0 | Ran 3259 tests; OK (skipped=1); seed **2083573844** | 178.895s |
| `python3 -X dev tests/run_strict.py` | 0 | Ran 3259 tests; OK (skipped=1); no unraisable failures | 193.148s |

The skip is `ScreenshotBrowserSmokeTests.test_real_browser_capture_and_protection_rejection`,
which requires opt-in `MAHLER_SCREENSHOT_SMOKE_CWD`. Node-backed contract tests ran.
The skipped screenshot smoke is not coverage for criterion 7. Full command wall
elapsed times were 177.057s, 179.425s and 193.454s respectively. Test fixture
warning/error output did not result in suite failures.

## Criterion 7: gated walkthrough remains outstanding

Exact gated browser-driver preflight executed:

```sh
mahler desktop run -- node -e 'require("playwright"); console.log("Browser driver available; integrated journey still required")'
```

The gate **allowed execution**. Node exited **1**, `MODULE_NOT_FOUND: Cannot find
module 'playwright'`; wrapper printed **`mahler desktop: command ran and exited 1`**.
This is neither `GATE-DENIED` nor a successful walkthrough. No viewport was opened
and no desktop/phone browser journey ran. Playwright is not a declared repository
dependency; no dependency or browser installation was added for this evidence-only
audit. The existing `console-walkthrough` command delegates to another agent and
its recipe lacks this integrated journey; invoking it would not establish these
criteria without further tooling and assertions.

Explicit outstanding coverage for the conductor: run a fixture-seeded browser
journey under `mahler desktop run -- <full browser command>` at desktop and phone
widths, switching projects/reloading scoped URLs; checking Overview and shipping
stages; starting/replacing/stopping Prioritize; changing parallel jobs; and pausing/
resuming only the selected project. Verify ordering/expiry with the mechanical
checks above. Include light/dark readability if visual evidence is collected.
Keep criterion 7 unticked until such evidence exists. No CI UI-job coverage is
claimed. #1254 tracks the discovered product gap; lack of an available browser
driver is the explicit reason for outstanding walkthrough coverage here.

## Parent checklist handoff

Parent #1119 checklist updated to tick **1, 3, 4, 5 and 6** only; **2 and 7** remain unchecked. The run's final instruction
reserves issue comments, branch push and PR shipping for the conductor. No agent
comment or PR is posted by this build run; the conductor can link this committed
audit when reporting the checklist evidence. No owner pass is required.

## Criterion 2 re-verification (#1257)

On `main` at `c7ccb4787a4ffd7a20ea74faf2215ea78cd8611b` (`HEAD` and `origin/main` agreed),
2026-10-26. The probe above, re-run unchanged, exited 0 and printed
`desktop= True phone= True` for all five labels (`How a change ships`,
`Dependencies`, `Completed · last 7 days`, `Activity`, `Open issues · 30 days`).
`PYTHONPATH=tests python3 -m unittest -v test_console.ProjectOverviewPageTests
test_console_state.ProjectOverviewStateTests` ran 7 tests, OK (exit 0).

The browser journey (below) step `b` also passed at both widths against the seeded
fixture: six headline measures, inline answer button, active/queued/blocked rows,
explainer, dependencies, completed, activity, what's new, release v2.4.1 and the
30-day trend, with the other project's rows absent. On desktop it additionally
asserted six stage dots on each of the six shipping rows plus the red check name,
reviewing platform, `quiet 1/14d` and `verified`.

Caveat, not a failure of the criterion: the phone Overview shows shipping rows as
subtitle evidence (`PR #44 · agy-gemini reviewing`) plus the six-stage explainer, not a
row of six dots. The parent wording is "six-stage shipping evidence/explainer", which this
satisfies. Verdict: **SUPPORTED** on both layouts.

## Criterion 7 re-run (#1257)

Tooling (no dependency added, no `package.json`): `examples/console_journey/seed.py`
(stdlib; writes a two-project config with `concurrency.total = 8` and a seeded ledger into a
temp `MAHLER_HOME`, and refuses to run otherwise) and `journey.mjs` (Node built-ins only:
global `WebSocket`, `node:sqlite`; drives Google Chrome 156 headless over CDP). `run.sh`
starts the seed, runs the journey and tears down. Contract test:
`tests/test_console_journey.py`.

Command, always through the gate:

```sh
mahler desktop run -- sh examples/console_journey/run.sh
```

The gate allowed it; the journey exited 1 and the wrapper printed
`mahler desktop: command ran and exited 1`. Per-step output (desktop 1280x900, phone
390x844 mobile emulation):

| Step | Desktop | Phone |
| --- | --- | --- |
| a1 all-projects page offers the scope switcher | **FAIL** | **FAIL** |
| a2 switcher moves scope, URL `?project=` follows, reload keeps it, Needs you/Backlog/Releases/Stats/Event stream show only the project | pass | pass |
| b Overview content | pass | pass |
| c Prioritize 1 hour, banner, ▲ in switcher; replacement on the other project asked for confirmation; Stop; ledger has `priority_boost_started`/`replaced`/`stopped` | pass | pass |
| d Parallel jobs + then −; only that project's `max_parallel` changes in the fixture config | pass | pass |
| e Pause then resume; project stays listed (switcher shows paused), running item stays active; events recorded | pass | pass |
| f Overview in dark mode | pass | pass |

PNGs for each step and layout are in `docs/audits/1119-journey/`
(`02-FAILED-a1-*` shows the all-projects page with no switcher).

**Product defect found:** the Mahler-wide view renders a plain "Mahler" brand, not the
switcher, so a project can only be entered by typing `?project=`. `design.md` and #1119 §1
say the rail/phone brand is the dropdown. Filed as p1 `type:bug`
[#1274](https://github.com/mkny13/mahler/issues/1274), `Part of #1119`. Step a2 therefore
enters by URL. Criterion 7 stays **unchecked** until #1274 merges and the journey passes
`a1`; re-run the same gated command then. Ordering and expiry are not repeated by the
journey; criterion 3's scheduler and ledger tests cover them. Visual note, not an
assertion: on the phone Overview the lower sections sit flush against the left edge and ref
numbers run into titles; taste notes are not a gate (D10).

## Parent checklist handoff (#1257)

Criterion 2 ticked on #1119 from the evidence above. Criterion 7 left unchecked.
