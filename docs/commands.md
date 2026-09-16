# Mahler Commands

Mahler automates the codebase directly from GitHub issues. You can control its behavior by leaving a comment on any open issue.

## Commands

Drop these commands as a comment on an issue to instruct the conductor. They take effect on the next tick.

- `/mahler go`
  Moves a parked or failed issue to `ready`. The conductor will pick it up and assign it to an agent based on quota availability.

- `/mahler park`
  Pauses work on an issue. Moves it to `parked`. Agents working on it will be interrupted. Useful when you want Mahler to ignore an issue for now.

- `/mahler platform <name>`
  Forces Mahler to use a specific agent platform (e.g., `agy-gemini`, `claude`, `codex`, `copilot`) for the next run. This adds a `platform:<name>` label. This overrides the automatic quota-based routing.

- `/mahler undo`
  Reverts the most recent agent's work. Moves the issue back to `ready` to try again, usually giving it to another agent if quota allows.

## Replying to `NEEDS-YOU`

When an agent needs human input (e.g. to make a product decision, or provide credentials), it stops and puts the issue into the `needs-you` state.

To reply, simply **add a comment on the issue with your answer**. Mahler will automatically read your comment, resume the item (moving it to `ready`), and provide your answer to the next agent so they can proceed.
