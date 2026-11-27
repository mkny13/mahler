# GitHub Comment Commands

Mahler automates the codebase directly from GitHub issues. This reference documents the `/mahler` commands you can drop as a comment on any open issue.

> **Looking for local CLI commands?** Run `mahler --help` (or `mahler <command> --help`) from your terminal. See [README.md](../README.md#operate-mahler) for common CLI workflows like `mahler status`, `mahler usage`, `mahler pause`, and `mahler serve`.

## Commands

Drop these commands as a comment on an issue or PR to instruct the conductor. They take effect on the next tick.

- `/mahler go`
  Resumes work on a parked, waiting, or failed item. If the item has an open PR, it moves the state to `verifying` to resume verification, CI tracking, and review; otherwise it moves the issue to `ready` for quota-based agent scheduling. Clears active waits, clears launch failure records, resets the review cycle, and resets attempt and escalation counters (`attempts=0`, `setup_fails=0`, `esc_tier=0`, `esc_fails=0`).

- `/mahler park`
  Pauses work on an issue. Moves the state to `parked`, clears active waits, and interrupts any active agent runs on that issue. Can also be commented on an open PR to stop active runs and park the item.

- `/mahler stop`
  Accepted alias for `/mahler park` with identical behavior: transitions the issue or PR to `parked`, clears active waits, and interrupts active runs.

- `/mahler platform <name>`
  Forces Mahler to use a specific agent platform (e.g., `agy-gemini`, `claude`, `codex`, `copilot`) for subsequent runs by applying the `platform:<name>` label on GitHub. Overrides automatic quota-based routing. If the target platform is configured with `approval = true`, only the repository owner can approve and pin that platform.

- `/mahler platform auto`
  Clears an existing platform pin (removes any `platform:*` label on GitHub) and restores normal automatic quota-based routing.

- `/mahler platform none`
  Clears an existing platform pin (removes any `platform:*` label on GitHub) and restores normal automatic quota-based routing (equivalent to `/mahler platform auto`).

Merge undo is available from the operator console: it creates a revert change
and sends it through the normal CI-gated conductor pipeline. There is currently
no `/mahler undo` comment command.

## Replying to `NEEDS-YOU` and Plain Answers

When an agent needs human input (e.g. to make a product decision, or provide credentials), it stops and puts the issue into the `needs_you` state.

To reply, simply **add a comment on the issue with your answer**. Mahler automatically distinguishes plain answers from `/mahler` commands:

- **On a `needs_you` issue**: Mahler reads the comment and moves the item back to `inbox` with reason "you answered — re-sorting", so a sorting run can incorporate the answer before work resumes.
- **On a `needs_you` PR**: When a non-convergent review question asks the owner for adjudication, commenting `merge with follow-ups` transitions the item to `verifying` to merge the PR and file follow-ups.
- **On a `failed` issue**: A plain answer comment automatically resumes the item (identical to `/mahler go`), moving it to `ready` (or `verifying` if a PR exists) and resetting failure counters.
- **Console integration**: The operator console provides the same reply and resume actions with a 60-second Undo window.

## Review Adjudication and Precision (D33)

Operator CLI commands for managing durable review adjudications and measuring reviewer precision:

- `mahler review-adjudicate <project>#N --review-run ID --sha SHA --classification false|justified|unresolved --evidence URL --reason TEXT [--fix-run ID ...]`
  - **Authority**: The explicit operator command is the sole adjudication authority. Neither a builder's claim, a later pass, nor a PR merge automatically adjudicates a verdict.
  - **Events**: Appends a versioned `review_adjudication` event (`version: 1`, `review_run`, `sha`, `classification`, `evidence`, `reason`, `fix_runs`). If `--fix-run` is supplied, also appends durable `review_fix_trigger` event(s).
  - **Idempotency**: Identical submissions are idempotent no-ops; later corrections append a new event without modifying earlier events (latest event ID wins).
  - **Validation & Historical Links**: Validates that `--review-run` is an ended `REVIEW-FAIL` run on that item with a matching `review_verdict` event and matching SHA. For `--fix-run`, validates the same item, `role: "fix"`, valid chronology (fix started after review ended), and rejects conflicting links. Any validation error fails atomically without partial writes.
  - **D33 Attribution Rule**: In `scorecard`, a failed review is disregarded only when its latest valid adjudication is `false`. A later fix is disregarded only when an exact durable `review_fix_trigger` links solely to that false verdict. Unlinked legacy fixes, CI fixes, and base-conflict fixes remain adverse. Historical run rows and operational counters are never mutated.

- `mahler scorecard --review-precision [--project P] [--since 30d] [--json]`
  - Reports reviewer precision grouped by recorded platform, model, and effort over failed review runs in the requested window.
  - Each failed verdict is counted once despite duplicate finalization events.
  - Precision is `justified / (justified + false)`; false-fail share is `false / (justified + false)`. Both excluded counts (`unresolved` and `unadjudicated`) are displayed. When the denominator is zero, precision and false-fail share are reported as `unknown` (`null` in JSON).

## Hands-off completion scorecard

`mahler scorecard [--project P] [--since 30d] [--json]` also reports issue-level
hands-off completion: autonomous / (autonomous + human-assisted), as a percentage,
with all three classification counts and the denominator. An empty denominator is
`unknown` (`null` in JSON), not zero percent.

Each issue is counted once, using its latest recorded transition to `done`, within
the selected window (inclusive start). The default window is `measure.window_days`.
Build `DONE` outcomes and merges alone are not completed issues. Repeated runs or
completions do not inflate the count. Lifecycle evidence before the window still
counts; evidence after the selected completion does not. Project filtering applies
to both reports; role and size filters apply only to the existing model rows.

Any recorded interactive/primary lease, transition into or out of `needs_you`, or
posted human answer makes the issue human-assisted. Automated agent handoffs do
not. Without positive human evidence, an autonomous classification requires a
continuous state history from inbox/creation through completion and recorded lease
epochs from 1 without gaps. Legacy done snapshots, missing transitions or lease
history are unknown and excluded from the percentage denominator. Queued or undone
console answers do not count as posted answers. This measures Mahler's recorded
history only: unrecorded work outside Mahler is not measured.

Normal JSON output is an object with `completion` and `attempts`. `completion`
contains `autonomous`, `human_assisted`, `unknown`, `denominator`, `completed`,
`percentage` (0–100 or null), and `limitation`. `attempts` retains the previous
attempt records and meanings; `--raw` retains the legacy attempts-only array.
`--review-precision` keeps its existing independent output format.
