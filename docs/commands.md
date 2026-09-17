# GitHub Comment Commands

Mahler automates the codebase directly from GitHub issues. This reference documents the `/mahler` commands you can drop as a comment on any open issue.

> **Looking for local CLI commands?** Run `mahler --help` (or `mahler <command> --help`) from your terminal. See [README.md](../README.md#operate-mahler) for common CLI workflows like `mahler status`, `mahler usage`, `mahler pause`, and `mahler serve`.

## Commands

Drop these commands as a comment on an issue to instruct the conductor. They take effect on the next tick.

- `/mahler go`
  Moves a parked or failed issue to `ready`. The conductor will pick it up and assign it to an agent based on quota availability.

- `/mahler park`
  Pauses work on an issue. Moves it to `parked`. Agents working on it will be interrupted. Useful when you want Mahler to ignore an issue for now.

- `/mahler platform <name>`
  Forces Mahler to use a specific agent platform (e.g., `agy-gemini`, `claude`, `codex`, `copilot`) for the next run. This adds a `platform:<name>` label. This overrides the automatic quota-based routing.

- `/mahler platform auto` (or `/mahler platform none`)
  Clears an existing platform pin and restores normal quota-based routing.

Merge undo is available from the operator console: it creates a revert change
and sends it through the normal CI-gated conductor pipeline. There is currently
no `/mahler undo` comment command.

## Replying to `NEEDS-YOU`

When an agent needs human input (e.g. to make a product decision, or provide credentials), it stops and puts the issue into the `needs-you` state.

To reply, simply **add a comment on the issue with your answer**. Mahler will
automatically read the comment and move the item back to `inbox`, so a sorting
run can incorporate the answer before work resumes. The console provides the
same flow with a 60-second Undo window.
