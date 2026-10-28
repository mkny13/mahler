# #921 completion evidence ("Talk it through")

Verification report for mahler#1290. Inspected checkout: `eb8497ad330a2aed186e33e1e5d80abaa9dd27a2`
(main), 2026-10-27 UTC. Sources: GitHub metadata, committed artifacts, git history, local isolated
tests (fake workers/providers only; no live API calls, credential reads or issue edits).

## Criterion 1 — #922 is closed with its committed artifacts and verification evidence.
status: met
- Issue #922: CLOSED 2026-10-11. PR [#959](https://github.com/mkny13/mahler/pull/959) merged 2026-10-11, merge SHA `36248e6f71ae5dde1d8bf7cbb918021e77a1baff`.
- Artifacts present: `DESIGN.md` (D43), `docs/console/discuss.md`, `docs/console/screens/discuss-{phone,desktop}.svg`, `recipes/discuss.md`, `tests/test_discuss_contract.py`.
- Verification: contract test runs inside the full suite (see criterion 8).

## Criterion 2 — #923 is closed with its committed artifacts and verification evidence.
status: met
- #923 CLOSED 2026-10-11; PR [#960](https://github.com/mkny13/mahler/pull/960) merged, SHA `7a5535eb3700be2b2fef149cd65042e08ea6df5c`.
- Artifacts: `mahler/discuss_tools.py`, `mahler/ledger.py`, `mahler/gh.py`, `mahler/mcp.py`, `tests/test_discuss_tools.py`, `tests/test_ledger.py`, `tests/test_mcp.py`.

## Criterion 3 — #924 is closed with its committed artifacts and verification evidence.
status: met
- #924 CLOSED 2026-10-11; PR [#961](https://github.com/mkny13/mahler/pull/961) merged, SHA `bf7f4ce5b6254985feb4d89e37fb5685fc951aed`.
- Artifacts: `mahler/discuss.py`, `mahler/config.py`, `mahler/serve.py`, `mahler/usage.py`, `tests/test_discuss.py`, `tests/test_serve.py`.

## Criterion 4 — #925 is closed with its committed artifacts and verification evidence.
status: met
- #925 CLOSED 2026-10-26; PR [#1289](https://github.com/mkny13/mahler/pull/1289) merged 2026-10-26, SHA `24af2579e13c374a383658b99488cebf586da070`.
- Artifacts: `mahler/console/{actions,page,state}.py`, `console.css`, `console.js`, `mahler/discuss.py`, `mahler/serve.py`, `tests/console_discuss_test.js`, `tests/test_console.py`, `tests/test_console_boundary.py`.
- Not covered here: the post-merge phone usability check (parent "Needs a human to check").

## Criterion 5 — #926 is closed with its committed artifacts and verification evidence.
status: met
- #926 CLOSED 2026-10-11; PR [#982](https://github.com/mkny13/mahler/pull/982) merged, SHA `cb45bdaa628fd4de089497eb75e26a63b5dc8945`.
- Artifacts: `mahler/discuss_credits.py`, `mahler/api_credits.py`, `sdk_runner/mahler_sdk_run.py`, `docs/team-api-credits.md`, `tests/test_discuss_credits.py`, `tests/test_api_credits.py`.
- No closed-unmerged child; all five children have merged PRs. (Unrelated: PR #913 closed unmerged, superseded by #928 below.)

## Criterion 6 — The two console journeys (Triage refinement and Capture filing) pass at 390px and 1440px using fake workers; owner mockup approval is recorded before production panel changes.
status: met
- Command: `mahler desktop run -- python3 -m unittest discover -s tests -p test_console.py -k test_fake_worker_journeys_at_phone_and_desktop_widths -v` → `ok`, Ran 1 test, not skipped (gate allowed; headless Chrome + Node WebSocket available; temp profiles per width).
- Named results (`tests/test_console.py` `DiscussionBrowserTests`, subTests width=390 and 1440, driving `tests/console_discuss_test.js` which loops `['triage','capture']`): Triage@390 PASS, Capture@390 PASS, Triage@1440 PASS, Capture@1440 PASS. Caveat: the subTests are per-width; the two journeys run together inside each width's single script, so per-journey results are not separately reported by the harness.
- Approval: `docs/console/discuss.md` records approval of revision `0f2ebd8b293302cc910eb2fdbe612d9c61c8ecd8` (commit 2026-10-11 01:53 -0400, ancestor of main, merged in #959; mockup SVGs unchanged since) via https://github.com/mkny13/mahler/issues/925#issuecomment-6407036362 (not re-fetched per instructions). Production panel code first appears in #1289 (commits 2026-10-26 17:03Z onward, merged 2026-10-26). The chronology evidence is the explicit committed statement in `docs/console/discuss.md` lines 3–6 that approval was admitted to the build before production console changes. The revision date alone would not prove approval chronology. The comment's own timestamp was not fetched, per the prohibition on fetching raw comments.
- Gap: `ROADMAP.md` line 33 still says "Mike's approval ... remains pending" (stale; see criterion 8).

## Criterion 7 — Routing tests prove personal chats use only subscription and work chats use only the gateway; work enablement has linked #918 capped trial evidence.
status: met
- `tests/test_discuss_credits.py::test_project_policy_matrix` — personal account→engine `claude`; work→`agent-sdk`; mismatched/ambiguous accounts (`['personal','work']`, `[]`, disabled, personal project bound as work) → `binding_mismatch`. The same file patches `platforms.oauth_usage` to raise "no personal fallback" and `api_key_for_run` to raise "worker cannot read key".
- `tests/test_discuss.py::test_changed_binding` (account/repo change rejected), `::test_work_unavailable` (work with no credits → `quota_unavailable`, probe never called, no fallback), `::test_two_turn_resume_operator_usage` (personal turns count as operator usage, no runs/items created).
- `tests/test_accounts.py::test_personal_project_never_gets_a_work_platform`, `::test_work_project_routes_only_to_work_platforms`; `tests/test_api_credits.py` covers gateway reservations/caps.
- #918: CLOSED 2026-10-11, PR [#928](https://github.com/mkny13/mahler/pull/928) merged SHA `6ee0e180e4a621019f4f0efc33d85f5a553b6e74`; capped trials in `docs/team-api-credits.md`: trial 2 passed $0.0035 under a $0.10 ceiling; trial 3 failed $0.136 (gateway died with the tick, #951); trial 3 re-run (run 11801, #930) passed with a $3 ceiling, 13 reservations settled. Owner evidence: https://github.com/mkny13/mahler/issues/903#issuecomment-6108885290.
- Note: a live work-project conversation in the panel remains a post-merge human check (#983), not required by this criterion.

## Criterion 8 — The allocated D15/D27 amendment and ROADMAP reflect delivered behavior; the four verification commands pass.
status: met
- Supported: D43 allocated; D15 (`DESIGN.md` lines 2188–2189) and D27 (`DESIGN.md` lines 1974–1976) carry the discussion-worker exceptions matching delivered behavior.
- In `DESIGN.md`, D43 (lines 2732–2763) reflects shipped and approved status (lines 2735–2736: "The design and its issue-only discussion worker are shipped and approved; #925 delivered the production console panel"; lines 2760–2763: "The phone and desktop mockups were approved as part of #925, and the production panel code is shipped."). The stale phrases ("design contract, not shipped execution", "are proposals", "must have Mike's explicit approval") are absent.
- In `ROADMAP.md` "Talk it through" (lines 27–41), lines 29–32 state mockups are approved/committed design artifacts for the shipped feature, and lines 39–40 state "#925 delivered the approved console panel... the operator-started discussion feature is shipped." The stale phrases ("remains pending", "owns the approved console panel", "does not ship a chat panel") are absent.
- Documentation drift resolved in `6c7eece` (#1461) and regression-tested by `tests/test_discuss_documentation.py`.
- Verification commands against `350ef60c` (see "Command results (tested SHA 350ef60c5683d130503fd7d381fe819056550666)" below).

## Command results (tested SHA eb8497ad330a2aed186e33e1e5d80abaa9dd27a2)
- `python3 -m mahler.repo_guard` → exit 0.
- `python3 -m unittest discover -s tests` → Ran 3333, OK, skipped=1 (the unit run's own exit status was not captured; "OK" output is the evidence).
- `python3 tests/run_random.py` → seed 814535737, Ran 3333, OK (skipped=1), exit 0.
- `python3 -X dev tests/run_strict.py` → Ran 3333, OK (skipped=1), exit 0.
- The one skip is not the discussion browser journey (that test ran and passed when run alone through the desktop gate).

## Command results (tested SHA 350ef60c5683d130503fd7d381fe819056550666)
- `python3 -m mahler.repo_guard` → exit 0.
- `python3 -m unittest discover -s tests` → exit 0, Ran 3447 tests in 222.266s, OK (skipped=2).
- `python3 tests/run_random.py` → exit 0, seed 1576217253, Ran 3447 tests in 213.637s, OK (skipped=2).
- `python3 -X dev tests/run_strict.py` → exit 0, Ran 3447 tests in 227.046s, OK (skipped=2).
- The two skips are the discussion browser journeys (which require the desktop gate, passing in earlier gated run).

## Replay verification (2026-10-27, run 13616)

The inherited report above retains its original inspected SHA and historical results.
Following the replay instruction to run the fast verification first and finish on
success, `python3 -m unittest discover -s tests` was rerun at
`e158da5fe1f49aaea514ac4e312e282d87544801`: exit 0, 3393 tests in 189.345s,
OK (skipped=2). These two skips are not claimed as fresh browser journey coverage;
the four journey results above remain the prior gated run's evidence. The other
three verification commands were not rerun during this replay. A structural check
confirmed exactly eight numbered criterion records, seven met and one unmet.
The approval chronology citation was clarified to use the explicit committed
statement, rather than infer approval timing from the mockup revision date.

## Re-verification (2026-10-28, run 13872, #1462)

Inspected checkout: `350ef60c5683d130503fd7d381fe819056550666` (main), 2026-10-28 UTC.
Summary count: all eight criteria are met (8 met, 0 unmet).
Documentation drift in criterion 8 was resolved in `6c7eece` (#1461) and verified by `tests/test_discuss_documentation.py`.
All four verification commands pass on the tested SHA.

No owner-only decision is outstanding. Real-phone Triage/Capture usability and a
work conversation after deployment/qualification remain post-merge checks.
