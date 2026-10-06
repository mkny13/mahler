# Review precision audit

## Copilot: gpt-6-luna failed verdicts

Issue: [mahler#737](https://github.com/mkny13/mahler/issues/737), part of #732.

The 30 sampled verdicts contain **24 justified failures, 4 false failures and 2
unresolved cases**. Among the 28 resolved verdicts, the false-fail share is **14.3%**.
The 34 distinct blockers comprise 28 real, 4 false/contrived and 2 unresolved findings.

### Frozen sample

Cutoff: 2026-10-01 00:00 America/New_York (2026-10-01T04:00:00+00:00).
Query time (inclusive upper bound): `2026-10-06T19:45:39.308271+00:00`.
Read-only live ledger query; eligible population: **62 verdicts**.
Stable ordering: ascending unique `runs.id`; sample: Python `random.Random(732).sample(population, 30)`, without replacement.
All 62 eligible rows recorded platform `copilot`. Eligibility uses the recorded model, not a current routing alias.
The earliest fail `review_verdict` event is the verdict time; repeated finalization events count once.

Population run IDs, in stable order:

```json
[10321, 10329, 10339, 10340, 10363, 10382, 10384, 10405, 10408, 10413, 10415, 10423, 10429, 10431, 10448, 10453, 10458, 10463, 10468, 10471, 10474, 10479, 10480, 10494, 10496, 10498, 10500, 10502, 10504, 10508, 10521, 10542, 10549, 10568, 10580, 10586, 10606, 10610, 10662, 10672, 10673, 10675, 10681, 10683, 10685, 10691, 10730, 10747, 10759, 10773, 10775, 10785, 10792, 10821, 10823, 10834, 10841, 10865, 10869, 10872, 10875, 10906]
```

Selected run IDs, in draw order:

```json
[10681, 10405, 10823, 10496, 10675, 10673, 10841, 10453, 10363, 10730, 10384, 10568, 10498, 10413, 10906, 10662, 10382, 10423, 10865, 10480, 10672, 10685, 10494, 10504, 10415, 10321, 10521, 10329, 10610, 10747]
```

| Draw | Run | Project issue | PR | Reviewed SHA | Verdict time (UTC) |
| --- | --- | --- | --- | --- | --- |
| 1 | 10681 | [phish-in#507](https://github.com/mkny13/couch-tour/issues/507) | [514](https://github.com/mkny13/couch-tour/pull/514) | [d447520487e6](https://github.com/mkny13/couch-tour/commit/d447520487e6939125977b7fcd5f084171b765a9) | 2026-10-02T20:27:00.116311+00:00 |
| 2 | 10405 | [mahler#613](https://github.com/mkny13/mahler/issues/613) | [637](https://github.com/mkny13/mahler/pull/637) | [27ee26c4733a](https://github.com/mkny13/mahler/commit/27ee26c4733ab73b336bf712e318456a0d25ae5b) | 2026-10-01T22:10:21.203626+00:00 |
| 3 | 10823 | [phish-in#533](https://github.com/mkny13/couch-tour/issues/533) | [534](https://github.com/mkny13/couch-tour/pull/534) | [68b93077e8ff](https://github.com/mkny13/couch-tour/commit/68b93077e8ffc7ce524e574cdae5e830de2fff89) | 2026-10-06T04:22:04.623536+00:00 |
| 4 | 10496 | [phish-in#369](https://github.com/mkny13/couch-tour/issues/369) | [425](https://github.com/mkny13/couch-tour/pull/425) | [975d629712b1](https://github.com/mkny13/couch-tour/commit/975d629712b1820ce1644a8fc294663c3a6c6b4a) | 2026-10-02T03:27:40.860777+00:00 |
| 5 | 10675 | [phish-in#427](https://github.com/mkny13/couch-tour/issues/427) | [512](https://github.com/mkny13/couch-tour/pull/512) | [b2b5f3d19446](https://github.com/mkny13/couch-tour/commit/b2b5f3d19446d2110e5b8e069bb3b395a8732556) | 2026-10-02T19:54:41.739831+00:00 |
| 6 | 10673 | [phish-in#434](https://github.com/mkny13/couch-tour/issues/434) | [458](https://github.com/mkny13/couch-tour/pull/458) | [bd5001710794](https://github.com/mkny13/couch-tour/commit/bd500171079404b20908780a0bc9e616df30ed7d) | 2026-10-02T19:44:44.096775+00:00 |
| 7 | 10841 | [puppy-growth-chart#9](https://github.com/mkny13/puppy-growth-chart/issues/9) | [13](https://github.com/mkny13/puppy-growth-chart/pull/13) | [b2226c908e75](https://github.com/mkny13/puppy-growth-chart/commit/b2226c908e7539a2eadfdc15af8e2dd46403683d) | 2026-10-06T05:28:02.046128+00:00 |
| 8 | 10453 | [phish-in#435](https://github.com/mkny13/couch-tour/issues/435) | [457](https://github.com/mkny13/couch-tour/pull/457) | [db2e7fc3e80b](https://github.com/mkny13/couch-tour/commit/db2e7fc3e80b38bba720be3805d9e0c886702bf7) | 2026-10-01T23:47:25.554080+00:00 |
| 9 | 10363 | [phish-in#431](https://github.com/mkny13/couch-tour/issues/431) | [440](https://github.com/mkny13/couch-tour/pull/440) | [9d870a5066ae](https://github.com/mkny13/couch-tour/commit/9d870a5066ae2ea1cdc1c6028183ba7435d2e510) | 2026-10-01T17:10:04.674005+00:00 |
| 10 | 10730 | [phish-in#351](https://github.com/mkny13/couch-tour/issues/351) | [523](https://github.com/mkny13/couch-tour/pull/523) | [33ea7879b01d](https://github.com/mkny13/couch-tour/commit/33ea7879b01db49f6f3510d122421fb25505a3d9) | 2026-10-03T15:10:12.807038+00:00 |
| 11 | 10384 | [mahler#607](https://github.com/mkny13/mahler/issues/607) | [610](https://github.com/mkny13/mahler/pull/610) | [541fe7169c7e](https://github.com/mkny13/mahler/commit/541fe7169c7ee7b11bd4f945df4a1a5bcb3a077c) | 2026-10-01T18:40:09.849694+00:00 |
| 12 | 10568 | [phish-in#492](https://github.com/mkny13/couch-tour/issues/492) | [503](https://github.com/mkny13/couch-tour/pull/503) | [0f29774c18a5](https://github.com/mkny13/couch-tour/commit/0f29774c18a51e560e215b3d3196375253a0e6fb) | 2026-10-02T11:55:55.891966+00:00 |
| 13 | 10498 | [phish-in#369](https://github.com/mkny13/couch-tour/issues/369) | [425](https://github.com/mkny13/couch-tour/pull/425) | [e90032fb1fad](https://github.com/mkny13/couch-tour/commit/e90032fb1fada83c29889b05bd6c19bb6e58da7e) | 2026-10-02T03:53:58.608297+00:00 |
| 14 | 10413 | [hockey#70](https://github.com/mkny13/hockey-draft-copilot/issues/70) | [74](https://github.com/mkny13/hockey-draft-copilot/pull/74) | [78961aed9a1d](https://github.com/mkny13/hockey-draft-copilot/commit/78961aed9a1deb99c765d4a046423d4f548190df) | 2026-10-01T22:22:51.010317+00:00 |
| 15 | 10906 | [mahler#734](https://github.com/mkny13/mahler/issues/734) | [764](https://github.com/mkny13/mahler/pull/764) | [12dc0196723c](https://github.com/mkny13/mahler/commit/12dc0196723c7fd6b1fbc19c85fc1278c6c2ca0c) | 2026-10-06T18:56:09.947513+00:00 |
| 16 | 10662 | [phish-in#369](https://github.com/mkny13/couch-tour/issues/369) | [425](https://github.com/mkny13/couch-tour/pull/425) | [7e6877784f8f](https://github.com/mkny13/couch-tour/commit/7e6877784f8f2a5807df644a13b8dba2f0405eaf) | 2026-10-02T19:23:18.121107+00:00 |
| 17 | 10382 | [phish-in#386](https://github.com/mkny13/couch-tour/issues/386) | [450](https://github.com/mkny13/couch-tour/pull/450) | [0612abfada00](https://github.com/mkny13/couch-tour/commit/0612abfada001703375ba4c12dc1001c1bea84b8) | 2026-10-01T18:28:23.521286+00:00 |
| 18 | 10423 | [mahler#597](https://github.com/mkny13/mahler/issues/597) | [602](https://github.com/mkny13/mahler/pull/602) | [ee32873c1ace](https://github.com/mkny13/mahler/commit/ee32873c1ace3c6787f3dbb3f10ac74c742c2f8d) | 2026-10-01T22:43:59.325405+00:00 |
| 19 | 10865 | [mahler#714](https://github.com/mkny13/mahler/issues/714) | [731](https://github.com/mkny13/mahler/pull/731) | [7770149f4f8c](https://github.com/mkny13/mahler/commit/7770149f4f8c9de783c088169068c18ac04554d2) | 2026-10-06T12:43:42.100696+00:00 |
| 20 | 10480 | [mahler#643](https://github.com/mkny13/mahler/issues/643) | [648](https://github.com/mkny13/mahler/pull/648) | [0d2b0bb7c8f9](https://github.com/mkny13/mahler/commit/0d2b0bb7c8f922d37b6b6d12fe790c669fdf2875) | 2026-10-02T00:49:41.580852+00:00 |
| 21 | 10672 | [phish-in#369](https://github.com/mkny13/couch-tour/issues/369) | [425](https://github.com/mkny13/couch-tour/pull/425) | [1b44b06fa79d](https://github.com/mkny13/couch-tour/commit/1b44b06fa79de95f72f977f2b841245ae697ba7e) | 2026-10-02T19:42:15.653258+00:00 |
| 22 | 10685 | [phish-in#507](https://github.com/mkny13/couch-tour/issues/507) | [514](https://github.com/mkny13/couch-tour/pull/514) | [148ac5cdaa3e](https://github.com/mkny13/couch-tour/commit/148ac5cdaa3eea7ab4911e4e1ebbf03e3d348427) | 2026-10-02T20:42:05.775551+00:00 |
| 23 | 10494 | [phish-in#369](https://github.com/mkny13/couch-tour/issues/369) | [425](https://github.com/mkny13/couch-tour/pull/425) | [cdd788c39207](https://github.com/mkny13/couch-tour/commit/cdd788c3920780fbbefb5a42e496cb1fbfc7623f) | 2026-10-02T03:19:36.285395+00:00 |
| 24 | 10504 | [phish-in#428](https://github.com/mkny13/couch-tour/issues/428) | [460](https://github.com/mkny13/couch-tour/pull/460) | [9782a271feae](https://github.com/mkny13/couch-tour/commit/9782a271feaea1d9797943afa4dae7b64c2354f8) | 2026-10-02T04:30:48.304174+00:00 |
| 25 | 10415 | [mahler#623](https://github.com/mkny13/mahler/issues/623) | [638](https://github.com/mkny13/mahler/pull/638) | [f84db9867394](https://github.com/mkny13/mahler/commit/f84db98673942e3591062e74804eb520c4c5f6c7) | 2026-10-01T22:28:01.957006+00:00 |
| 26 | 10321 | [mahler#607](https://github.com/mkny13/mahler/issues/607) | [610](https://github.com/mkny13/mahler/pull/610) | [1d34dd3fa7d9](https://github.com/mkny13/mahler/commit/1d34dd3fa7d972b60546884792cc3dbd36eb468d) | 2026-10-01T15:48:12.187141+00:00 |
| 27 | 10521 | [phish-in#444](https://github.com/mkny13/couch-tour/issues/444) | [469](https://github.com/mkny13/couch-tour/pull/469) | [e18fcc241061](https://github.com/mkny13/couch-tour/commit/e18fcc241061f933228f58f1d390ec1bdf6ed58f) | 2026-10-02T05:41:15.661120+00:00 |
| 28 | 10329 | [mahler#607](https://github.com/mkny13/mahler/issues/607) | [610](https://github.com/mkny13/mahler/pull/610) | [e42035e3cb38](https://github.com/mkny13/mahler/commit/e42035e3cb38127cb5f1d8022eb2c6a50b55fb90) | 2026-10-01T15:57:49.873670+00:00 |
| 29 | 10610 | [phish-in#428](https://github.com/mkny13/couch-tour/issues/428) | [460](https://github.com/mkny13/couch-tour/pull/460) | [145a4d8053e9](https://github.com/mkny13/couch-tour/commit/145a4d8053e9ddc0e23a92a16617d724d23b2791) | 2026-10-02T18:03:19.487795+00:00 |
| 30 | 10747 | [phish-in#522](https://github.com/mkny13/couch-tour/issues/522) | [529](https://github.com/mkny13/couch-tour/pull/529) | [a6608fe315d0](https://github.com/mkny13/couch-tour/commit/a6608fe315d0a2f290914156c4347f65ae59a8ed) | 2026-10-03T16:08:30.942770+00:00 |

### Rubric and evidence handling

Apply [D11](../../DESIGN.md#d11--the-agents-feedback-loop) and the
[review blocking bar](../../recipes/review.md): a blocker must have a realistic
trigger and an actual consequence (including an explicit acceptance failure), or
be a demonstrated security/data-loss defect. Report completeness is consequential
when the report itself is the requested deliverable. A code change made in response
to a review is **not**, by itself, proof the finding was valid. Existing documented
limitations, description-only wording, and masked tool output need separate scrutiny.
This is an audit against the stated D11 bar, not a historical policy-compliance score.

The sample table links the issue contract, historical PR and immutable reviewed SHA.
The table below paraphrases **every distinct blocking finding**, with one row per
finding. Four sampled verdicts have structured `classified` records (10823, 10841,
10865, 10906); the other 26 have legacy text. For structured records only
`severity=blocking` counts: 10906's paused-history follow-up is excluded. Legacy
records are all-blocker failure lists. Repeated STATUS text duplicates the findings
in 10363 and 10496; those copies are counted once, not as extra findings. The
compound journey-coverage finding in 10672 remains one finding, with its seeded
subclaim discussed explicitly. No verdicts were replaced to obtain clearer evidence.

Historical PRs were resolved from review history, PR comments and `pr_opened`
events, not just today's `items.pr`: 10363 reviewed #440 (not replacement #528),
and 10480 reviewed #648 (not replacement #652). Rebased PR histories can have
new commit IDs; reviewed SHAs in the sample remain authoritative. Later fix links
below sometimes name their rebased equivalents. Issue/PR comments and bodies were
read on 2026-10-06; mutable bodies are not assumed to be archived originals.

This is source/diff and recorded-disposition adjudication. No Mac UI automation,
production mutation, or rerun of other projects' app suites was performed. Where
historical tests are mentioned, those are the linked fix's tests or handoff results,
not tests executed by this audit. Unavailable proof is explicitly retained as
`unresolved evidence` rather than inferred from merge status.

### Blocking findings

| Finding | Classification | Reviewed finding | Scenario/consequence or reason it misses D11 | Subsequent fix or disposition |
| --- | --- | --- | --- | --- |
| 10681.1 | real | DB_QUERY_EFFICIENCY.md omits SourceLoudnessDao.upsert, saveSourceLoudness and other normal writes. | The audit is the deliverable: its explicit every-query inventory cannot be used to compare later audits while growing-table writes are absent. This is report completeness, not a claim that those writes are slow. | [ed77c141](https://github.com/mkny13/couch-tour/commit/ed77c141b28258e089e56cd642ce3d91aa9f680c): adds write plan/verdict rows, including both named methods. |
| 10405.1 | real | tick.py advances the escapes checkpoint after filing, beyond the emitted upper bound. | A bug closed during a normal GitHub request falls into neither interval and is permanently omitted from the required recurring escape analysis. | [0f1123d4](https://github.com/mkny13/mahler/commit/0f1123d4ae5e8b17337595e2024f3db9f46755ba): saves the tick-start upper bound and adds a clock-advance regression test. |
| 10823.1 | real | run-android.sh searches Home only downward for Browse artists. | Retained Home scroll below the entry is ordinary use; reselection does not reset it. Favorite actions report unavailable/SKIP and fail the required default sync journey. | [cefaf1f5](https://github.com/mkny13/couch-tour/commit/cefaf1f531c0759e2523842bb4f3ab99cad4471d): searches both directions, starting upward on Home; extends stubbed-runner coverage. |
| 10496.1 | real | run-mac.sh calls a failing mac::relaunch unguarded under set -e. | A cold-start failure is exactly what the smoke runner must report; instead it exits before the required one result line and failure screenshot. | [ed8769c4](https://github.com/mkny13/couch-tour/commit/ed8769c4cf7d3ca172fb3cb8b4ec31a62008ccc2): guards all three callers and emits FAIL plus screenshot. |
| 10496.2 | real | The relaunch persistence check accepts the empty home.in_progress shelf. | A seeded in-progress item can disappear while the empty container still exists; the runner then reports successful persistence without evidence of the seeded item. This concerns a false PASS, not a demand to alter app data. | [ed8769c4](https://github.com/mkny13/couch-tour/commit/ed8769c4cf7d3ca172fb3cb8b4ec31a62008ccc2): adds a card assertion; [4cb35711](https://github.com/mkny13/couch-tour/commit/4cb35711bec7a453bd9ac9df2e5d8d8bdf095d8d) later removes out-of-scope app tags and explicitly SKIPs the unobservable journey instead of claiming PASS. |
| 10496.3 | real | jam_chart.note alone passes without checking its required source link. | An actual note card with no link passes the journey although JOURNEYS.md requires both note and source link. | [ed8769c4](https://github.com/mkny13/couch-tour/commit/ed8769c4cf7d3ca172fb3cb8b4ec31a62008ccc2): adds link assertion; [4cb35711](https://github.com/mkny13/couch-tour/commit/4cb35711bec7a453bd9ac9df2e5d8d8bdf095d8d) later removes app-source changes and reports explicit SKIP for the tag gap. |
| 10675.1 | real | SearchView.swift playlist rows have navigation but no Play action. | Every search result lacks the play button expressly required to queue and start playback. | [7d47b8e9](https://github.com/mkny13/couch-tour/commit/7d47b8e9f0a42027c49f72cdc0f58c43a5e01882): adds a button that fetches the playlist and starts its queue. |
| 10673.1 | real | Sync.swift shares and resets pulled/pushed counts and failure state across calls. | Foreground/timer/manual/debounced syncs overlap; one call overwrites another’s diagnostics, including a successful call being labeled unauthorized. Accurate sync events are the feature being delivered, even though user data is unaffected. | [a6608fe3](https://github.com/mkny13/couch-tour/commit/a6608fe315d0a2f290914156c4347f65ae59a8ed): subsequent issue #522 supplies per-call state and deterministic overlapping-call tests; its handoff reports both tests failing on the shared-state version. |
| 10841.1 | false/contrived | PR #13 describes zero vulnerabilities but does not paste the literal npm audit summary. | D11 expressly makes PR-description-only requests follow-ups. The verdict identifies no failing audit or missing repository/CI gate; reformatting the same result in a PR cannot justify a failed code verdict. | PR body now includes “found 0 vulnerabilities”; PR contains only the same b2226c908e75 implementation commit and later merged. No production fix. |
| 10453.1 | real | DiagnosticsView constructs DiagnosticsLog synchronously. | Opening the sheet performs directory creation/pruning/reads on the UI caller, directly violating the explicit all-disk-I/O-off-main criterion. | [8598824e](https://github.com/mkny13/couch-tour/commit/8598824e79a7e36816085f14c34e5830928581a0): constructs the log in Task.detached and awaits it for reload/copy/clear. |
| 10363.1 | unresolved evidence | MainActivity.kt action pills allegedly clip Share on a 360dp phone. | The reviewed source has a non-scrollable padded Row and the added Like pill, so the scenario is plausible. The claimed remaining width depends on measured text/count/button constraints. No 360dp rendering, layout assertion or targeted fix was found; do not count the reviewer’s estimate as proof. | Historical PR #440 closed unmerged. Replacement PR #528 rebased the feature; its handoff describes a manual header reapplication, not a clipping reproduction or fix. Missing: measured layout at the reviewed SHA, or a targeted before/after test. |
| 10730.1 | real | schema.sql replays non-idempotent CREATEs on an existing D1 database. | The schema-change deploy path replays the file; an existing first table aborts it before the new favorites table is created, preventing the requested deployed sync feature. | [bf485cb6](https://github.com/mkny13/couch-tour/commit/bf485cb6507103abed939e0d8b8b5481c321fd9a): changes table/index CREATEs to IF NOT EXISTS; the reviewed sync-deploy.yml identifies the schema replay path. |
| 10384.1 | real | gh.py removes account-scoped GH_TOKEN/GITHUB_TOKEN from Git operations. | A configured project relying on its account environment fetches/pushes with the wrong/default identity or fails access. The App token was never added to this Git environment, so removing account credentials breaks supported behavior. | [d9e02ab2](https://github.com/mkny13/mahler/commit/d9e02ab23056f38a0b68c1bed2c8d7f2652a1836): retains account credentials in _git_env and updates regression expectations. |
| 10568.1 | real | ExpandedNowPlayingView extraction drops chromeBar padding. | Every expanded player loses its pre-existing horizontal/top/bottom spacing. This is not a stylistic preference: #492 explicitly requires structural-only extraction preserving layout and styling. | [07a04f30](https://github.com/mkny13/couch-tour/commit/07a04f3026ed60b72957b72a9f50fbc84ec2e07a): restores exactly the original three padding modifiers. |
| 10498.1 | real | PR #425 changes app source, JOURNEYS.md and Package.resolved outside its single allowed script. | The delivered diff directly violates the explicit no-other-files acceptance criterion and crosses concurrently owned app/spec boundaries. The prior review did not authorize widening scope. | [4cb35711](https://github.com/mkny13/couch-tour/commit/4cb35711bec7a453bd9ac9df2e5d8d8bdf095d8d): removes app identifiers/spec edits and changes unsupported checks to SKIP; PR history also contains the incidental lockfile snapshot. |
| 10413.1 | real | sanitizeLoadedState defaults a malformed counter to 1 while retaining higher-numbered picks. | Recovery of wrong-typed saved fields is the issue’s explicit normal input domain. The next unnumbered pick duplicates an existing pick number, corrupting the retained draft history. | [f24b42f9](https://github.com/mkny13/hockey-draft-copilot/commit/f24b42f98c6eaaacb8464dcacdc26f3d71bc3b3e): derives the counter floor from retained picks and adds a malformed-counter/valid-history test. |
| 10906.1 | real | capacity_record closes a launch before checking the stale observation gap. | After downtime exceeding 120 seconds, a normal launch charges unknown downtime as waiting, contradicting the telemetry acceptance criterion. | [f9763673](https://github.com/mkny13/mahler/commit/f976367322cc17d0868a8d5dff6a5ab9c4b72895): detects stale launch intervals, closes at the prior observation with reason gap, and adds a fake-clock regression test. |
| 10662.1 | real | mac::relaunch foregrounds Beta with open -b even without --allow-focus. | Switching applications during the quit/relaunch wait is ordinary use. Relaunch steals the owner’s focus despite the explicit default refusal requirement. | [36ca5e02](https://github.com/mkny13/couch-tour/commit/36ca5e02a5eb3f9a4498d12c1bc35e5f6739545b): restores open -g -b unless focus is explicitly allowed. |
| 10382.1 | real | NowPlaying.kt reports MP3 for a YouTube item lacking FLAC_URL. | Ordinary YouTube playback now shows a false format in the restored format indicator. The newly displayed badge’s information is wrong, not merely styled differently. | [e1651d76](https://github.com/mkny13/couch-tour/commit/e1651d76efcf6a14f51f43dc97d55320725e9217): derives no format for YouTube, suppresses the empty badge and tests the distinction. |
| 10423.1 | real | ARCHITECTURE.md omits shipped and completion_evidence. | After a normal merge the persistent-state map cannot explain the actual shipped-to-done transition. Accurately documenting that lifecycle is an explicit acceptance requirement of this documentation issue. | [43a317bf](https://github.com/mkny13/mahler/commit/43a317bf73b296386538ec35924bb26be9295e48): adds the state/table and the completion-evidence transition. |
| 10865.1 | real | Design-result validation scans followups but not rationale for a quoted unmet criterion. | An agent can put the exact unsatisfied criterion in its ordinary explanation and still persist followups, allowing the conductor to consume a disposition that the explicit conservative contract forbids. | [018ac7cf](https://github.com/mkny13/mahler/commit/018ac7cfe771b91d57f8697051592bc9d52cb172): scans rationale too and adds quoted-criterion regression coverage. Later security-rationale fixes are separate from this sampled blocker. |
| 10480.1 | real | Only randomized CI execution is added; test_suite_hygiene.py is absent. | A newly introduced assertion-free test still passes CI. The requested mechanical detection gate and its explicit file acceptance criterion are missing. | Original PR #648 closed; replacement #652 commit [d6220587](https://github.com/mkny13/mahler/commit/d6220587f9fe088de90c3975d337e91aa9f5241b) adds the stdlib hygiene test and test dispositions. The sampled [0d2b0bb7](https://github.com/mkny13/mahler/commit/0d2b0bb7c8f922d37b6b6d12fe790c669fdf2875) changes only ci.yml. |
| 10672.1 | unresolved evidence | Six eligible Mac journeys always SKIP, leaving five assertions rather than the plan’s minimum ten; seeded Home also always SKIPs. | The numerical shortfall is real, but blocking adjudication is ambiguous: the same contract forbids app-source changes, permits every journey to be implemented OR explicitly SKIP with reason, and expressly permits expensive seeded detection to SKIP. Prior review demanded SKIP where app identifiers cannot prove a pass. | PR #425 merged with the tag-gap SKIPs; no ten-journey fix or authoritative reconciliation was found. Missing: which contract takes precedence and evidence that ten meaningful journeys were possible within the script-only boundary. The seeded-SKIP subclaim alone misses the explicit carve-out. |
| 10685.1 | real | DB_QUERY_EFFICIENCY.md omits the readiness SELECT 1 in sync/src/index.ts. | The issue expressly requests every D1 statement and every query in the report. A normal readiness query is missing from that exhaustive deliverable. This is a literal report-completeness blocker, not evidence of a performance defect; SELECT 1 needs no index. | No targeted addition in subsequent PR #514 commits (only README count update after the sampled head); the issue records exhausted review attempts and the PR subsequently merged. A merge does not prove the omission false. |
| 10494.1 | real | Failure screenshots use scripts/smoke/artifacts and a -failure suffix. | A normal failed journey writes outside the exact smoke-reports/<tag>/mac-<journey>.png acceptance path, so the report cannot find its required evidence. | [303aff86](https://github.com/mkny13/couch-tour/commit/303aff86bf2c5a8e46ea81fbab4822b57758fb23): changes the path and filename to the specified location. |
| 10494.2 | real | launch-cold-start checks an already-running Home without relaunch. | Preflight requires Beta to be running, so the purported cold-start check can PASS without executing startup; startup regressions escape the named journey. | [303aff86](https://github.com/mkny13/couch-tour/commit/303aff86bf2c5a8e46ea81fbab4822b57758fb23): calls relaunch before the launch assertion. |
| 10494.3 | real | mac::relaunch quits background Beta without checking focus permission. | A seeded relaunch journey interrupts the owner’s background session while another app is frontmost, violating the input/focus guard. | [303aff86](https://github.com/mkny13/couch-tour/commit/303aff86bf2c5a8e46ea81fbab4822b57758fb23): calls mac::focus before quitting. |
| 10504.1 | false/contrived | Casting a playlist excerpt plays the whole file rather than its 30–90s bounds. | At the reviewed SHA, DECISIONS.md D66 explicitly accepts whole-track Cast playback as a known limitation; #428 asks for macOS browsing/playback parity and does not overturn it. The finding expands supported Cast behavior rather than exposing a regression or explicit Cast-clipping acceptance failure. | [a2ee888f](https://github.com/mkny13/couch-tour/commit/a2ee888f87a26cc8650c1add85f01201122e2511) implements sender clipping and adds D325 expressly superseding D66 on macOS. That is a real feature change, but its existence does not retroactively make this a D11 blocker. |
| 10415.1 | real | Plaintext Error: Rate limit exceeded (HTTP 429) does not set quota_hit. | An ordinary free-tier quota error skips the configured Vibe backoff and can retry a rate-limited platform. Plaintext error classification is explicitly part of the adapter plan. | [3efde12f](https://github.com/mkny13/mahler/commit/3efde12f566207f1b7e866c9d9b1e5520c64ac9b): sends plaintext errors through _note_log_error and adds a 429 regression test. |
| 10321.1 | false/contrived | ghapp.py allegedly sends literal ****** instead of the signed JWT. | Raw source at the reviewed SHA contains the interpolated signed JWT Authorization value, not six asterisks. The failure premise is output masking; no realistic bad request exists in this code path as alleged. | [e42035e3](https://github.com/mkny13/mahler/commit/e42035e3cb38127cb5f1d8022eb2c6a50b55fb90): adds an outgoing-request assertion only; production forwarding unchanged. The linked fix handoff explicitly reports no reproduced production defect. |
| 10521.1 | real | The mockup checker scans a multiline expression-bodied Kotlin @Preview body. | An ordinary preview containing sample text is incorrectly rejected by CI, violating the explicit exemption for preview code. No exotic syntax is needed. | [057b5c87](https://github.com/mkny13/couch-tour/commit/057b5c873b8babf752edd9fdc768008fd2b32f3e): skips complete expression bodies and tests multiline plus following-declaration boundaries. |
| 10329.1 | false/contrived | ghapp.py and its new test allegedly expect a literal masked Authorization header. | Raw source still interpolates the JWT and the added test asserts that exact value. This repeats the same display-masking mistake; the sampled record’s trailing “send” is truncated, but its sole allegation is preserved in the review comment. | [5ec86f080](https://github.com/mkny13/mahler/commit/5ec86f08085f7b7f43811f66d4cf130b44137ab5), fix run 10331: renames the JWT parameter and changes the test fixture presentation to avoid redaction, without changing the verified production authorization behavior. |
| 10610.1 | real | Player.swift seeks a new AVPlayerItem before readiness. | Selecting the ordinary fixture excerpt can start at 0 instead of 30 seconds because the early seek is ignored; the existing resume path already documents the same readiness requirement. | [0172d867](https://github.com/mkny13/couch-tour/commit/0172d8675bc2a5ba2403f2cefc44ddbfe825d651): moves excerpt/resume seek to observed ready state, delaying playback until seek completes. |
| 10747.1 | real | Sync.swift increments/decrements syncInFlight without synchronization. | Independent sync tasks can lose a read-modify-write increment; finishing one then clears isSyncing while another runs, violating the explicit last-call-completes criterion. | [a382d114](https://github.com/mkny13/couch-tour/commit/a382d114d2a2bbe025c51b81c8c73ee4e30a949f): locks counter and isSyncing updates. The handoff says no simultaneous-entry race test was added; the source change supports the mechanism, not a claim that that particular race was experimentally reproduced. |

### Additional evidence for disputed dispositions

- **10321 / 10329:** reviewed [10321 source](https://github.com/mkny13/mahler/blob/1d34dd3fa7d972b60546884792cc3dbd36eb468d/mahler/ghapp.py#L49) and [10329 source](https://github.com/mkny13/mahler/blob/e42035e3cb38127cb5f1d8022eb2c6a50b55fb90/mahler/ghapp.py#L49); [first fix handoff](https://github.com/mkny13/mahler/issues/607#issuecomment-5935160381), [second fix handoff](https://github.com/mkny13/mahler/issues/607#issuecomment-5935338153). Raw reads confirm the interpolated header on both SHAs, independently of the review display.
- **10504:** [D66 at the reviewed head](https://github.com/mkny13/couch-tour/blob/9782a271feaea1d9797943afa4dae7b64c2354f8/DECISIONS.md#L411) explicitly describes whole-track Cast as known/unfixed; [D325 addition](https://github.com/mkny13/couch-tour/commit/a2ee888f87a26cc8650c1add85f01201122e2511) supersedes that limitation rather than showing it was absent. Local excerpt readiness (10610) has no corresponding accepted exception.
- **10841:** [review comment](https://github.com/mkny13/puppy-growth-chart/pull/13#issuecomment-6009997464) names only PR text. The current [PR body and commit list](https://github.com/mkny13/puppy-growth-chart/pull/13) show the added summary and unchanged implementation head.
- **10363:** [historical review](https://github.com/mkny13/couch-tour/pull/440#issuecomment-5936532343), [reviewed row](https://github.com/mkny13/couch-tour/blob/9d870a5066ae2ea1cdc1c6028183ba7435d2e510/app/src/main/java/dev/mike/couchtour/MainActivity.kt#L4011), [replacement handoff](https://github.com/mkny13/couch-tour/issues/431#issuecomment-5970778634). Neither old PR nor replacement supplies the missing measured clipping evidence.
- **10672:** [review comment](https://github.com/mkny13/couch-tour/pull/425#issuecomment-5960095969), [contract including both minimum ten and script-only boundary](https://github.com/mkny13/couch-tour/issues/369), [earlier instruction to SKIP unobservable conditions](https://github.com/mkny13/couch-tour/pull/425#issuecomment-5922965513), [merged disposition](https://github.com/mkny13/couch-tour/issues/369#issuecomment-5968924075). A merge alone does not resolve those conflicting instructions.
- **10685:** [reviewed report](https://github.com/mkny13/couch-tour/blob/148ac5cdaa3eea7ab4911e4e1ebbf03e3d348427/docs/DB_QUERY_EFFICIENCY.md) versus [readiness statement](https://github.com/mkny13/couch-tour/blob/148ac5cdaa3eea7ab4911e4e1ebbf03e3d348427/sync/src/index.ts#L588); [exhausted-attempt disposition and later shipment](https://github.com/mkny13/couch-tour/issues/507). This audit counts the explicit inventory omission despite its negligible performance significance.
- **10673:** [subsequent issue #522](https://github.com/mkny13/couch-tour/issues/522) records the overlapping-call defect, fix and pre-fix failing tests. Moving a finding to another issue does not itself make the underlying defect false.

### Arithmetic and limits

| Unit | Real | False/contrived | Unresolved evidence | Total |
| --- | --- | --- | --- | --- |
| Distinct blocking findings | 28 | 4 | 2 | 34 |
| Failed verdicts | 24 | 4 | 2 | 30 |

A fully adjudicated verdict is a true fail if **any** blocker is real; it is a
false fail only if **all** blockers are false/contrived. A verdict containing
unresolved evidence is kept separately and excluded from both numerator and
denominator, even if another blocker were real. There are no mixed real/false
verdicts in this sample. Runs 10494 and 10496 each contain three distinct real
blockers; all other runs contain one distinct blocker.

- Blocker precision among resolved findings: **28 / (28 + 4) = 87.5%**.
- False blockers among resolved findings: **4 / 32 = 12.5%**.
- Failed-verdict precision among resolved verdicts: **24 / (24 + 4) = 85.7%**.
- False-fail rate among resolved failed verdicts: **4 / 28 = 14.3%**.
- False-fail runs: **10321, 10329, 10504, 10841**.
- Unresolved runs: **10363, 10672** (2/30); missing evidence is named in their rows.

This estimates precision conditional on an eligible failed verdict, not overall
review accuracy, recall, or false-positive rate among all reviews. Repeated rounds
on the same issue are separate verdicts by design, not independent project samples.
The parent’s earlier 58-failure count is context, not this population: this audit
freezes a later query at 62 and records its exact eligibility/time bounds. It does
not infer that every accepted fix was necessary, that every merged finding was
false, or that these rates apply to another model. No remedy is proposed here.

### Reproduction and verification

Run the following from the repository root with Python 3.12+ and read access to
`~/.mahler/mahler.db`. It opens SQLite in `mode=ro` and never instantiates the
application Ledger or changes runtime state. Keep the frozen upper bound; using
“now” would select a different population. The report itself contains the expected
population and draw-order identifiers in its first two JSON blocks.

```python
import collections
import contextlib
import json
import pathlib
import random
import re
import sqlite3

report = pathlib.Path("docs/audits/review-precision.md").read_text()
expected_population, expected_sample = [
    json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", report, re.S)
]
cutoff = "2026-10-01T04:00:00+00:00"
query_time = "2026-10-06T19:45:39.308271+00:00"
query = """
SELECT r.id, r.project, r.number, r.platform, r.ended_at,
       MIN(e.at) AS verdict_at
FROM runs r JOIN events e
  ON e.kind = 'review_verdict'
 AND json_extract(e.detail, '$.review_run') = r.id
 AND json_extract(e.detail, '$.verdict') = 'fail'
WHERE r.role = 'review' AND r.model = 'gpt-6-luna'
  AND r.outcome = 'REVIEW-FAIL'
GROUP BY r.id
HAVING julianday(MIN(e.at)) >= julianday(?)
   AND julianday(MIN(e.at)) <= julianday(?)
ORDER BY r.id
"""
uri = (pathlib.Path.home() / ".mahler/mahler.db").as_uri() + "?mode=ro"
with contextlib.closing(sqlite3.connect(uri, uri=True)) as con:
    population = con.execute(query, (cutoff, query_time)).fetchall()
assert [r[0] for r in population] == expected_population
assert len(population) == 62
assert {r[3] for r in population} == {"copilot"}
selected = random.Random(732).sample(population, 30)
assert [r[0] for r in selected] == expected_sample
assert len(set(expected_sample)) == 30

# Independently read the classification table, not a separate tally file.
findings = re.findall(
    r"^\| (\d+)\.(\d+) \| (real|false/contrived|unresolved evidence) \|",
    report, re.M,
)
assert len({(run, finding) for run, finding, _ in findings}) == len(findings)
blockers = collections.Counter(label for _, _, label in findings)
by_run = collections.defaultdict(list)
for run, _, label in findings:
    by_run[int(run)].append(label)
assert set(by_run) == set(expected_sample)
verdicts = collections.Counter()
for labels in by_run.values():
    result = ("unresolved evidence" if "unresolved evidence" in labels else
              "real" if "real" in labels else "false/contrived")
    verdicts[result] += 1
assert blockers == {"real": 28, "false/contrived": 4, "unresolved evidence": 2}
assert verdicts == {"real": 24, "false/contrived": 4, "unresolved evidence": 2}
assert len(findings) == 34 and len(by_run) == 30
print("sample:", [r[0] for r in selected])
print("blockers:", dict(blockers), "verdicts:", dict(verdicts))
```

To retrieve an individual verdict's original findings without copying unrelated
ledger data, query `kv.value` for `reviewfindings:<project>#<issue>`, decode its JSON
array and select only the entry whose `run_id` matches the sample. That entry
provides `sha`, `findings`, and, when present, `classified` and `pr`. Match missing
PR identifiers against that issue's `pr_opened` events and dated GitHub review
comments. Inspect the file at the sampled SHA, not just the PR’s latest diff;
then compare the linked fix commit or recorded disposition. The first verdict
event can differ slightly from the history entry's `at`/run end time; the table
consistently uses the event timestamp selected above.

Verification performed on 2026-10-06 with Python 3.14.3:

- Executed the Python block above against the read-only live ledger: same 62-row
  population and same 30 sampled IDs; independently parsed 34 classification rows
  and obtained the exact blocker/verdict totals shown above.
- Separately checked sample-table draw order, all 30 reviewed SHAs, and relative links.
- `python3 -m mahler.repo_guard` — passed.
- `python3 -m unittest discover -s tests` — 1,920 tests, passed.
- `python3 -X dev tests/run_strict.py` — 1,920 tests, passed.
- `python3 tests/run_random.py 732` — 1,920 tests, passed.
- `git diff --check` — passed.

Only this report is changed; no routing, prompt, scorecard, ledger or runtime code
is changed. The two unresolved classifications are audit results with named
missing evidence, not omitted sample entries.
