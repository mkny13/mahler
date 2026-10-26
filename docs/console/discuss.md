# Talk it through — discussion contract v1

Design artifact for #922, part of #921. Mike explicitly approved revision
`0f2ebd8b293302cc910eb2fdbe612d9c61c8ecd8` (both mockups) in
[the owner approval on #925](https://github.com/mkny13/mahler/issues/925#issuecomment-6407036362),
admitted to the build before production console changes. Approval includes two
adjustments: chat supplements the Needs-you quick-response buttons; “More detail”
is replaced by issue/PR context and the last relevant comment.

## Mockups and entry points

- [Phone, 390px](screens/discuss-phone.svg): six vertically stacked state snapshots,
  not one long conversation screen. Each frame represents a phone viewport.
- [Desktop, 1440px](screens/discuss-desktop.svg): Triage beside a discussion panel,
  followed by annotated state variants. All content and issue numbers are examples.

Use the existing neutral light console tokens, sans-serif type and blue actions.
On phone, open a full-screen dialog above the console with its own transcript scroll
and a bottom composer that stays visible above the software keyboard. On desktop,
use a right-side dialog with the originating Triage item visible behind it. Closing
returns to the same console view; it does not cancel a submitted turn.

| State | What the operator sees and can do |
| --- | --- |
| Triage context | “Talk it through” on a Needs you item opens its title, project and issue link. Include the current blocker and owner comments as context. Opening makes no model call; Send starts a turn. Existing one-tap answers remain available outside the panel. |
| Capture draft/project selection | Carry the unsent Capture draft into the composer; require a project before Send. Show the funding account beside it. Selection can change before session creation; after first Send it is locked. “New discussion” starts a different project without moving history. |
| Waiting | Show “Thinking…”, elapsed time and “Up to 2 minutes”. Disable Send and Retry while one turn is active. No fabricated token stream; replies arrive together. Keep Close available. |
| Reply with issue links | Show assistant text, then separately “Confirmed issues” with verified links. Distinguish “Created”, “Updated”, and “Answer saved” from a suggestion. A product question with no write has no issue links. Return composer to ready. |
| Quota unavailable | “Discussion quota is unavailable. Your draft is saved.” Show the known reset time only if known. Keep Close and an explicit “Try again”; no alternate account, paid fallback or automatic retry. |
| Failure/retry | “This turn failed. Your message is saved.” Timeout says “No reply within 2 minutes.” Show confirmed partial writes even on failure. Retry reconciles writes first; never promises that nothing happened. |

## Keyboard, focus and refresh

Dialog has an accessible name, visible Close, logical tab order and a visible focus
ring; inert background and focus containment apply on both layouts. Focus the project
selector when Capture has none, otherwise the composer. Escape closes and restores
focus to the invoking button. Enter inserts a newline; Ctrl+Enter or Cmd+Enter sends;
a labelled Send button is always available. Announce waiting and completion politely
without reading the whole transcript again. Announce failure as an alert. Do not move
focus when a reply arrives; links remain reachable by Tab. Use at least 44px tap targets.

The normal console refresh must preserve the open panel, draft, selection, scroll and
focus. Poll durable turn state; reopening or refreshing fetches the same session and
turn IDs, never resubmits a POST. Preserve unsent drafts locally per project/session
until sent or explicitly discarded; never put transcript/draft text in URLs. On reload,
restore the draft and transcript, but do not steal focus. A lost response is reconciled
by client request ID before showing Retry. Scrolling up suppresses auto-scroll and
shows “New reply”; users at the bottom stay at the bottom. Offline status retains the
draft and stops submission until connectivity returns.

## Session and turn lifecycle

The trusted worker binds session ID, project repository, account and engine at creation.
Project and account are immutable on resume; engine session identifiers cannot override
that binding. A config/account mismatch rejects resume and requires a new session.
Only an operator Send or Retry can start a turn. The console HTTP server and tick make
no model calls. A supervised console child worker handles turns independently of the
60-second tick; its failure cannot take down the daemon. No launchd changes are planned.

```json limits-v1
{"turn_timeout_seconds":120,"max_user_characters":8000,"max_session_turns":100,"max_active_turns_per_session":1}
```

Count Unicode code points in user input; reject empty/whitespace-only or over-limit
input before dispatch, without truncation. Count each accepted new turn, including an
explicit retry, toward 100; duplicate submission with the same client request ID returns
the original turn and does not count twice. At 100, retain readable history and offer
New discussion. One active turn includes queued and running; a second submission is
rejected, not silently queued. Atomic claims/completions fence stale workers. The
120-second wall-clock deadline starts at acceptance (including queue time); timeout,
worker death and restart recovery produce terminal error, never a hanging spinner.
Stop the timed-out worker and fence late tool writes/results before allowing retry.

Transitions: accepted → queued → running → success/error; queued can also terminate
as error. Terminal state is immutable. Explicit retries receive a new turn ID, refer
to the failed turn and reuse durable mutation identities for reconciliation. Unknown
GitHub outcomes remain uncertain until looked up; never blindly repeat creation or
an answer. An edit checks the remote version and reports conflict rather than
silently overwriting a newer owner answer. No automatic engine/account fallback.

## Versioned terminal result

The worker emits one JSON object, without Markdown fences, after every turn. Required
fields are exactly the following (transport/session/turn IDs are stored separately):

```json result-success-v1
{"version":1,"assistant_text":"I saved your answer on the issue.","engine_session_id":"opaque-engine-session","confirmed_issue_urls":["https://github.com/example/project/issues/12"],"status":"success","error":null}
```

```json result-error-v1
{"version":1,"assistant_text":"No reply within 2 minutes. Your message is saved.","engine_session_id":null,"confirmed_issue_urls":[],"status":"error","error":{"code":"timeout","message":"Turn exceeded 120 seconds.","retryable":true}}
```

`version` is integer 1. `assistant_text` is a string displayed as escaped text, never
trusted HTML. `engine_session_id` is a nonempty opaque string or null when unavailable;
only the trusted adapter supplies it, never a model's invented identifier. Resume is
engine-specific; an adapter unable to resume must reconstruct bounded context under
the same binding or report an explicit unsupported-resume error.
`confirmed_issue_urls` is a deduplicated array of canonical HTTPS GitHub issue URLs
in the bound repository, verified from successful restricted-tool receipts or
reconciliation. Model prose is not evidence of a write. Partial confirmed writes
remain present on error. `status` is terminal `success` or `error`; `error` is null
on success, otherwise an object with string `code`, string `message` and boolean
`retryable`. Codes include `quota_unavailable`, `timeout`, `worker_lost`,
`engine_error`, `invalid_result`, `conflict`, `session_limit`, and `binding_mismatch`.
The last two are non-retryable in that session. Unsupported versions, missing fields
or malformed engine output become `invalid_result`; the adapter must not treat exit
zero alone as success. The worker constructs timeout/crash errors even without output.

## Capabilities and funding

The restricted tool server, not the prompt alone, enforces a fixed allow-list:
`read_repo_file`, `list_repo_files`, `list_items`, `get_item` (server-filtered trusted comments only),
`add_item`, `edit_item`, `answer_item`. Bind every call to the session project on the
server; reject cross-project arguments. Repository reads are bounded tracked text
only; reject absolute paths, traversal and symlink escapes. Do not attach the existing
unrestricted MCP server or expose shell, generic HTTP, code writes, builds, leases,
merge operations, credentials or scheduler controls. Disable engine-native bypass
tools as well. Prompt injection in repository files/issues grants no capabilities.

Issue mutations use the project's GitHub identity and scope label. Preserve the
original request, owner answers, Part of/Depends on lines and Mahler-owned labels.
Agent comments begin with `<!-- mahler:agent -->`. Persist mutation operation IDs and
confirmed receipts; reconcile ambiguous create/answer responses before retry. The
normal sync → sort → build → ship pipeline handles filed work; chat cannot start it.

Personal projects use the personal Claude subscription only; turns count as operator
usage against the Claude reserve, not autonomous build runs. Work projects use only
the Agent SDK runner through the D41 gateway, with unchanged reservations, allowance,
grant validity and caps. The gateway alone holds its credentials. Personal sessions
never use employer credits; work sessions never borrow a personal login. Unavailable
quota or gateway means an explicit error: no paid fallback and no real-money spend.
Work enablement needs the #903/#918 prerequisites and capped end-to-end trial evidence;
personal delivery can precede it. D41 now has a separate gateway process (#951), so
the parent's original daemon-lifetime limitation is historical; any gateway loss still
must surface a retryable error instead of hanging.

## Delivery and evidence

#922 supplies design and recipe contract tests, not a running worker or console.
#923 owns durable state and restricted tools; #924 owns subscription execution;
#925 owns the approved panel; #926 owns credits. Runtime isolation, duplicate-write,
HTTP protections, 390px/1440px fake-worker journeys and quota-routing tests belong to
those children. This artifact suite checks the JSON examples, finite limits, recipe
boundary and SVG state coverage. It does not prove those future runtime properties.
The revision approval is linked above; real-phone readability and intent checks remain post-merge coverage.

## Personal worker (#924)

The console supervises `python3 -m mahler.discuss`, with its own SQLite connection
and a process lock beside the database. Queued turns are polled once a second;
worker-start failures appear in `discussion_worker` in `/api/state` and the event
stream, with restart delays capped at 30 seconds. Shutdown stops and reaps the
worker and engine; restart fences interrupted turns as errors, never resubmits them.
No launcher installation changes are needed.

Personal execution uses the existing `~/.claude` subscription login. A fresh,
zero-token OAuth quota reading must contain both unexpired 5-hour and weekly
windows below 100%. Unknown, missing or exhausted readings produce a retryable
`quota_unavailable` result. Operator turns can spend the reserve above autonomous
build thresholds; at exhaustion they stop. They record operator activity, which
suppresses autonomous reserve bursts, and never create build runs or attempts.
No build routing, work-credit fallback or token-spending quota probe is used.
Work-account turns use the [credits adapter](../team-api-credits.md#work-discussions-926)
after its runner installation and gateway restart; mixed-account policies fail closed.

Launch flags were checked against the installed `claude --help` on 2026-10-11:
`--restricted`, empty `--setting-sources`, explicit hook/plugin settings, empty
`--tools`, `--disable-slash-commands`, `--no-chrome`, `--strict-mcp-config` and
an explicit seven-tool allow-list. Each session runs in its own empty directory beside the database, preserving
Claude’s project-scoped resume storage.
`--bare` is deliberately excluded because it disables subscription OAuth login.
The adapter accepts only a successful CLI result envelope, bounds output to
256 KiB and assistant text to 64,000 characters, and derives confirmed links
from durable tool receipts. The deadline includes queue and quota-check time.
Real-login interaction remains manual coverage once the panel is available;
automated tests use fake engines and local child processes, never paid calls.
