# Operator and maintenance architecture audit (#1674)

Audited 2026-11-22, starting at `c01dee6`, for parent #1670. This is a
bounded source-and-hermetic-test audit, not a live installation attestation.
The admitted issue body, DESIGN's named decisions, ARCHITECTURE's Operator
surfaces/non-account trust claims, and ROADMAP's distinction between shipped
and planned work are the specification. Later amendments take precedence over
historical proposals. Top-level documentation reconciliation is out of scope.

The earlier #596 run summaries were supplied as background. No live
`mahler history` command was run: it would open/refresh real daemon state,
which this issue explicitly forbids. Repository source, history and tests were
used instead. No live console, notifications, personal data, GitHub mutations,
backup store, daemon checkout or operator configuration was accessed.

## Decision-to-code matrix

Each row has exactly one result. **Documentation drift** means the prose does
not describe the current, explicitly tested implementation; the differences
are enumerated below, not silently counted as conformance. **Fixed** refers
to the bounded changes in this branch. Symbol names are repository-relative;
test method names identify executable evidence, not a claim of live coverage.

| Contract | Result | Concrete implementation and verification evidence |
|---|---|---|
| DESIGN D10 — interaction, automatic completion, optional failures | fixed | `console.state._uat` reads `shipment_history`, describes quiet/automated/historical completion, and does not create a pass queue. `actions.ACTIONS` and `outbox.HANDLERS` expose only `uat_fail`. Replaced the live in-app-UAT/pass-again copy in `outbox.shipment_bug_title`, `shipment_bug_body`, `uat_fail`; replaced generic `_queue_verdict` with failure-only `_queue_failure_report`. `test_console.UatBugShapeTests`, `UatOutboxTests.test_fail_files_a_p1_bug_and_routs_it`, `UatPageTests.test_renders_the_row_with_only_fail`, and `test_digest.FormatTests.test_shipments_never_create_owner_waits_or_nags` cover the boundary. |
| DESIGN D12 — reversible writes, data and backup safety | fixed | `notify.send` previously redacted title/body but forwarded credential-shaped `Click` URLs. It now omits such URLs; `test_notify.TestSend.test_credential_bearing_click_is_not_sent_to_notification_service` is the negative regression and `test_safe_console_click_is_preserved` covers useful links. `backup.backup_postgres`, `backup_ledger`, `keep_set`, `run_ledger` and `janitor` retain the protections described below. `test_backup.test_dump_verified_private_and_password_off_argv` and `test_standalone_credentials_are_not_disclosed_by_failed_dump` (methods in that module), plus the ledger-backup tests, exercise synthetic data only. Planned restore drills are not claimed as implemented. |
| DESIGN D13 — operator decisions vs autonomous work | fixed | `state._needs` uses `ATTENTION_STATES`, `actions.answer` queues replies, and `outbox.answer` distinguishes answers from `/mahler` commands. `test_console.test_answer_on_needs_you_still_re_sorts`, `test_owner_answer_on_failed_item_resumes_without_command`, and `test_console_state.test_waiting_item_renders_and_is_not_a_needs_you` exercise the presentation/action contract. Category admission is at `finalize._needs_you` / `OWNER_CATEGORIES`, outside this issue's editable boundary: product, money, credentials, data-policy, irreversible. Operator surfaces do not invent a second category gate. Added the missing live-project-lease check to `janitor._fast_forward_primary`; `test_janitor.PrimaryCheckoutTests.test_live_project_lease_prevents_primary_update` proves maintenance cannot move a leased checkout, and `test_expired_or_other_project_lease_does_not_prevent_update` prevents overblocking. See the precise maintenance/metric limitations below. |
| DESIGN D20 — recurring maintenance | conformant | `platform_audit.queue` uses `maintenance_due`, the shared `passes_filed` gate and open-pass checks; it files evidence, never rewrites tiers/config. `practices_audit._queue` uses the same checkpoint family and persists partial report identity. `test_platform_audit.test_combined_dry_run_names_only_one_pass`, `test_failed_audit_does_not_claim_tick_or_reset_checkpoint`, `test_audit_never_writes_config`; `test_practices_audit.test_cadence_volume_and_cooldown_gates`, `test_partial_filing_persists_identity_then_resets_after_retry`. Per-project scheduling in `tick.maintenance` is a read-only dependency, not reimplemented here. |
| DESIGN D27 — standard-library console, writes through tick | documentation drift | `serve._Handler.do_POST` validates through `_refusal`, then calls explicit `actions.ACTIONS`; remote/process actions enter `outbox.drain`. `actions.answer` has a 60-second Undo grace. D43 discussions remain the explicit worker exception. `test_serve.test_guard`, `test_only_this_machine_and_the_tailnet_may_write`, `test_console.test_delay_replace_undo_and_validation`, `test_moved_on_and_failures_do_not_stop_next_action`. The current renderer selects one layout/section per response, and requests without Origin are accepted; D27's older wording says both layouts in one document and a same-origin Origin. Details below. |
| DESIGN D29 — hold items and dependency graph | conformant | `state._reason_items`, `_graph`, `_mermaid`; `page._why_items`, `_graph_svg`, `_backlog_groups`; `console.js.loadMermaid` lazily loads pinned Mermaid 12.0.0 with SRI, keeping the ranked SVG fallback. `test_console.test_mixed_holds_preserve_order_counts_and_item_metadata`, `test_graph_produces_ranks_and_edges`, `test_mermaid_browser_lifecycle`, `test_mermaid_titles_cannot_inject_syntax_or_html`, `test_graph_terminates_on_cycle`. This follows the Mermaid amendment, not the original SVG-only proposal. |
| DESIGN D31 — releases and read-only feed | conformant | `cli.cmd_release` previews unless `--publish` plus explicit version; `actions.cut_release` freezes SHA/item numbers and queues; `outbox.cut_release` calls `releases.publish_release`. `get_draft`, `synthesize_notes`, `check_readiness`, `build_feed` are deterministic ledger projections. `test_releases.PublishReleaseTests` covers initial/two-part versions, reconciliation and conflicting SHA/notes/tag; `BuildFeedTests.test_feed_conforms_to_schema_v1_and_excludes_sensitive_data`; `test_serve.test_releases_feed_unknown_or_disabled_project`; `test_console.test_outbox_publish_release_conflict_fails_action_and_keeps_draft`. |
| DESIGN D39 — evidence-only practices audit | documentation drift | `practices_audit.scan` inventories enabled projects; `scan_project` returns six findings; `file_audit` writes only audit/proposal issue metadata. Tests `test_scanner_filer_and_dry_run_never_modify_repository_or_config`, `test_lookup_failure_never_creates_duplicate_and_other_projects_proceed`, `test_scope_labels_anchor_first_links_and_closed_marker_reuse` establish the proposal-only boundary. Newer explicit exemption handling, binary treatment and resolution behavior are not reflected in D39; documented below. |
| DESIGN D43 — operator-started issue-only discussions | conformant | `actions.chat_binding` / `chat` bind session, item and request identity; `discuss.process_turn` / `Supervisor` supervise bounded workers; `discuss_tools.TOOL_FIELDS` / `DiscussionTools.call` allow only bounded reads and issue mutations. `mcp.serve_discussion` has no fallback to `serve`. `test_discuss_tools.test_denies_bypass_cross_project_and_bad_paths`, `test_scope_and_configuration_changes_fail_closed`, `test_create_ambiguous_response_reconciles_without_recreating`; `test_discuss.test_launch_boundary`, `test_real_child_output_death_timeout_and_reaping`; `test_mcp.test_separate_entrypoint_allowlist_and_no_unrestricted_fallback`. |
| DESIGN D44 — lesson admission | conformant | `lessons.read_base`, `scan`, `load`, `_suppress`, `catalog` admit scan-passed base bytes only. `classify`, `gate_proposals`, `check_consolidation` implement UTC clocks, evidence thresholds and snapshots. `test_lessons.test_branch_local_and_modified_lessons_suppressed`, `test_scanner_failure_loads_nothing_and_does_not_raise`, `test_projects_do_not_mix`, `test_boundaries_13_14_29_30`, `test_closed_proposal_needs_three_new_events`, `test_non_verbatim_snapshot_rejected`. No lesson was created for routine audit work. |
| DESIGN D45 — net backlog | conformant | `digest.backlog_rows` / `backlog_line` and `state._net_backlog` read ledger `backlog_net`; no GitHub request or alternate cumulative counter. `test_digest.NetBacklogTests.test_full_partial_and_no_history_lines`, `test_zero_and_hours_of_history`; the full suite covers ledger snapshot retention and synchronization. Missing/short history stays labelled rather than presented as a full window. |
| ARCHITECTURE Operator surfaces | documentation drift | CLI `build_parser`/`cmd_*`, seven-tool `mcp.serve`, HTTP state/actions, `notify.send` and `digest.maybe_send` exist as described. Operator-started discussions and settings writes are additional surfaces omitted from the section. `actions.save_settings` calls `config.save_settings` directly rather than queueing GitHub work; discussion behavior is D43, not an unrestricted dispatcher. `test_mcp.test_tools_list`, `test_console.SettingsConfigTests`, and `test_serve.test_tailnet_json_limits_and_changed_account` cover these distinctions. |
| ARCHITECTURE Data trust claim | conformant | This audit uses in-memory ledgers and temporary synthetic repositories/backups. `janitor.prune_run_dirs` refuses implicit real paths for scratch ledgers; `backup.run_ledger` migrates default storage only for the configured live ledger. `test_janitor.test_default_dirs_are_left_alone_for_a_scratch_ledger`, `test_backup.test_legacy_folder_is_migrated_only_for_the_live_ledger`, `test_history.test_other_project_never_appears` and `test_credential_not_indexed` exercise isolation. This is no claim that heuristic redaction identifies all personal data. |
| ARCHITECTURE Console access trust claim | documentation drift | `serve.serve` defaults to 127.0.0.1; `write_allowed_from` accepts loopback, mapped loopback and the two Tailnet ranges. `_refusal` requires the custom header and JSON for POST, rejects a supplied foreign Origin, and bounds bodies. Ordinary GETs are unauthenticated, while `/api/chat` additionally uses `_refusal`. Tests `test_guard`, `test_same_origin_is_allowed`, `test_only_this_machine_and_the_tailnet_may_write` characterize it. The claim that Origin is mandatory is stronger than the implementation. |
| ARCHITECTURE One dispatcher trust claim | conformant | No retired autonomous `dispatch`/`thread` entry point is registered in `cli.build_parser`, `actions.ACTIONS`, `outbox.HANDLERS`, or the discussion allowlist. `mcp.serve` is the intentionally privileged local operator API, not an autonomous scanner; removing it would remove D10's shipped tools. `test_mcp.test_tools_list` and `test_separate_entrypoint_allowlist_and_no_unrestricted_fallback` distinguish the two MCP entry points. External machine dispatchers were not inspected. |
| ARCHITECTURE daemon-installation trust claim | conformant | No bounded operator/maintenance module installs launcher code or changes the pinned daemon checkout as a task. `serve._check_for_update` only requests restart on HEAD change. Installation/update policy belongs to `launcher/` (read-only dependency); `test_launcher.test_ci_and_tests_still_gate_updates` and `test_two_crashes_record_bad_sha_and_prevent_reupdate` provide hermetic full-suite evidence. No hand-install change or live rollback test is included. |

## Operator path trace and retained names

- CLI: `main` parses a fixed argparse table and opens the routed ledger;
  `cmd_status`/history/event commands read it, interactive commands use the
  existing lease protocol, and release/backup commands have their explicit
  operator semantics. `cmd_history` calls project-scoped `history.refresh` and
  `search`; this is why invoking it against production was inappropriate here.
  The regular stdio MCP `serve` has seven explicit branches, project clients
  for GitHub writes, and no eval/getattr-based arbitrary function dispatcher.
  It is a trusted local operator interface, not a multi-user authorization API.
- HTTP: `do_GET` reads state/fragments/logs/attachments/published release feeds;
  `do_POST` checks the action name and request boundary before `actions.run`.
  State reads remain unauthenticated by design on a trusted network. A
  supplied foreign Origin is refused. Missing Origin remains accepted for
  non-browser callers using the explicit header. This is documentation drift,
  not evidence of an unrestricted cross-site write path; no CORS preflight
  permission is supplied. This audit did not expand or narrow that policy.
- Outbox: capture, answer, stop, revert, failure report and release publish are
  queued; `drain` rechecks pending status within a transaction, dispatches a
  finite handler table, records completion/failure, redacts errors, and
  continues after one failed action. `answer_undo` can cancel only pending
  answers. `outbox.answer` posts an unmarked human comment so the existing
  reply-means-answer path handles it, rather than inventing another state machine.
- Failure capture keeps `uat_fail`, `console_uat_queued`, `uat_verdict` and
  persisted `verdict` fields for stored actions, event consumers and historical
  evidence. `state._uat`, `_uat_sessions`, page `_uat_*`, `uat_url` settings,
  CSS class names and JavaScript `verdictRequests` now serve only shipment
  display/failure deduplication; no pass/skip/bulk-pass route is registered.
  The CSS comment about a shared “bulk verdict” is stale commentary, not a
  live control. The old generalized Python queue helper and live pass-again
  instructions were removed. Deduplication uses the existing shipment marker,
  so changing a new bug's title does not duplicate a previously created bug.
- `cli.cmd_console_walkthrough` remains a manually invoked software test,
  explicitly retained by D10. Historical `uat` rows, past passes, migration
  evidence, and optional human-check hints remain readable. None is an owner
  shipment-completion requirement. The actual 14-day/adverse-evidence state
  machine is a ledger/conductor dependency outside this audit's modification
  scope; `test_digest.test_automated_and_quiet_completion_roll_up_as_completed`
  exercises its visible result.
- Discussions: HTTP accepts an operator turn; the separate supervisor starts
  a worker with a fenced token and deadline. `launch_args` disables native
  tools, hooks/plugins and alternate MCP configuration. Repository reads use
  tracked regular blobs with bounded output; traversal, symlinks, cross-project
  arguments, build/lease/release tools and unknown argument fields are refused.
  Mutation receipts and markers reconcile ambiguous writes without resending.
  Owner answers and existing issue relationships are preserved by append-only
  refinement through the GitHub helper. No model call is made by HTTP or tick.
- Releases: a brief cursor is not a release. Preview SHA and item list are
  checked before queueing; publication reconciles an existing matching remote
  tag/release or fails on a conflict, then seals the local snapshot. Feed v1
  selects published releases and four item fields only, with no run-log,
  prompt, checklist or comment payload. Advisory readiness never publishes.

## Maintenance trace, planned coverage and documentation differences

- **Backups:** `backup_postgres` supplies PG credentials through environment,
  creates private files, checks the dump's table-of-contents and prunes
  14 daily/8 weekly/12 monthly. `run` records failures and hourly backoff.
  `backup_ledger` uses SQLite online backup and checks integrity on the copy;
  `run_ledger` retains 5 daily/4 weekly by default and runs compaction only
  after verified backup. `cmd_restore_ledger` verifies before replacement.
  A `pg_restore --list` check is not a restore drill. ROADMAP Phase 4 explicitly
  leaves other hosted-store kinds, monthly automated restores and staging
  seeding planned; this audit does not infer these exist from D12's target.
- **Janitor:** `maybe_run` isolates project failures; `sweep` fetches before
  considering remote deletion. `_stale_branches` preserves open/unknown items
  and young snapshots; `_stale_fix_branches` requires ended runs and containment,
  with a bounded deletion batch and a remote-tip guard. `prune_run_dirs` keeps
  active/recent work and has the scratch-ledger guard noted above.
  `rotate_serve_log` copies/truncates the existing inode and bounds rotations.
  `_fast_forward_primary` now checks for live project leases before any update;
  that guard was missing despite D13. It also refuses another branch, tracked dirt, merge/rebase/
  cherry-pick state or non-descendant tips. “Clean” here explicitly ignores
  unrelated untracked files (`test_untracked_file_is_kept_while_checkout_fast_forwards`).
  This is the current tested meaning, not a claim that every checkout has no
  untracked files. The new guard only reads leases; acquisition, expiry and
  handoff lifecycle mechanics remain unchanged.
- **Digest/notifications:** `maybe_send` catches errors, checks local hour/day,
  honors dry run and advances the KV key only after successful delivery.
  It never asks for outstanding shipment verdicts. `notify.send` uses bounded
  HTTP timeouts, ASCII titles, optional priority topics and credential redaction;
  this branch closes the unfiltered click-header case. The digest deliberately
  contains roll-up statistics, item titles and handoff descriptions, so D10's
  literal “title and console link only” is incomplete prose (D45 itself requires
  backlog figures). This is not proof that arbitrary personal text is safe:
  redaction is heuristic, and `cmd_notify` is an explicit operator text surface.
  No notification was sent. `digest.stuck_rate` retains historical category-tag
  and sort-run attribution; it is a diagnostic metric, not the D13 admission gate
  or a complete category ledger for newer tag-stripped build questions.
- **Audits:** platform dates are evidence hints, not a mechanical tier change;
  optional CLI catalog probes are disabled by default and failed probes stay
  unknown. Practices scans read tracked evidence without executing repository
  scripts, redact candidates, and keep unknowns for unresolved execution and
  missing metadata. Reports/proposals use durable markers, including closed
  proposals, and partial filing resumes its saved report identity.
  D39 predates `_load_exemptions`/`scan_project` accepting documented decisions,
  `_secrets` excluding recognized binary assets, and `file_audit` closing an
  existing proposal when the check becomes pass. These are intentional tested
  refinements (`test_documented_decisions_in_decisions_md_resolve_cleanly`,
  `test_tracked_binary_assets_and_clean_text_return_pass`,
  `test_audit_rerun_cleanly_reconciles_resolved_proposals`), not silent repository
  remediation. The D39 prose needs reconciliation with those tests; no scanner
  semantics were reversed in this audit.
- **History:** `index_run` stores redacted ended-run evidence before cleanup;
  `search` is parameterized and project-scoped, caps query terms, and degrades
  safely when FTS fails. `prune_no_work` blanks only old no-work bodies while
  retaining index identity. Tests cover credential removal, project exclusion,
  malformed search, absent logs and evidence surviving worktree removal.
- **Lessons:** only scan-passed base content can reach native discovery or a
  fallback catalog. New/changed branch lessons are suppressed with skip-worktree;
  suppression failure removes all candidates or marks the launch fatal. The
  scanner's schema, injection, Unicode, exfiltration, destructive-command and
  credential checks fail closed. Exposure does not reset curation clocks;
  consolidation needs verbatim snapshots; archive eligibility is a proposal,
  not deletion. These guarantees are exercised with temporary repositories.
- **Console documentation:** `page.app` now renders the selected section/layout
  and requested overlays, with one shared `state.build` model. D27's old
  both-layouts-in-every-document description no longer matches. Standard-library
  server imports and vanilla client assets remain; Mermaid is the explicit D29
  browser-only exception. ARCHITECTURE's older shipment-flow/manual-evidence
  wording and “in-app UAT panels planned” also conflict with amended D10 and
  ROADMAP's superseded-panel entry. None was used to restore a manual gate.

## Verification

Failing-first evidence:

1. `PYTHONPATH=tests python3 -m unittest test_console.UatBugShapeTests test_console.UatOutboxTests.test_fail_files_a_p1_bug_and_routs_it`
   failed three assertions against the old title/body; after the correction,
   all 19 shape/outbox/action tests passed.
2. `PYTHONPATH=tests python3 -m unittest test_notify.TestSend.test_credential_bearing_click_is_not_sent_to_notification_service test_notify.TestSend.test_safe_console_click_is_preserved`
   failed on the exposed synthetic credential URL; after the correction,
   all 33 notification plus shipment shape/outbox/action tests passed.
3. `PYTHONPATH=tests python3 -m unittest test_janitor.PrimaryCheckoutTests.test_live_project_lease_prevents_primary_update test_janitor.PrimaryCheckoutTests.test_expired_or_other_project_lease_does_not_prevent_update`
   reproduced the forbidden primary update with a live lease. After the guard
   and its import correction, all 9 primary-checkout tests passed.

Focused command (all requested modules):

```sh
PYTHONPATH=tests python3 -m unittest test_cli test_mcp test_serve test_console_state test_console test_discuss test_discuss_tools test_digest test_notify test_backup test_janitor test_practices_audit test_platform_audit test_history test_lessons test_releases
```

Initial result: 828 tests passed; the two additional janitor regressions are
included in the final rerun recorded below. The HTTP tests bind ephemeral loopback ports with
in-memory state; this does not expose the production console. Existing Node
browser lifecycle fixtures are exercised by the console suite, not a GUI run.

Final verification on the replayed branch (2026-11-22, Python 3.14.10):

- Focused command above: 830 tests passed.
- `python3 -m mahler.repo_guard`: passed (exit 0).
- `python3 -m unittest discover -s tests`: 3,903 tests, passed (2 skipped).
- `python3 tests/run_random.py`: 3,903 tests, passed (2 skipped; seed 276721196).
- `python3 -X dev tests/run_strict.py`: 3,903 tests, passed (2 skipped).

The replayed implementation required no further source changes.

Remaining coverage: no live Tailnet proxy, notification delivery, remote release,
production backup/restore, installed worker CLI, external dispatcher or daemon
installation was exercised. These are deliberately outside this hermetic audit;
there is no owner pass step and no required human check for this issue.
