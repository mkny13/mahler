# Mahler

Mahler conducts coding agents through GitHub-issue backlogs. It sorts new work,
routes it to an available CLI according to capability and quota, gives every run
an isolated git worktree, preserves handoffs, and lets a deterministic conductor
open, verify, and merge the resulting pull request.

Supported runners currently include Claude Code, Antigravity (`agy`), Cline,
GitHub Copilot CLI, Codex CLI, and Kilo. A project chooses which authenticated
accounts and runners it may spend; unavailable CLIs are skipped.

Mahler is currently a macOS, single-operator service. Its control plane uses
Python's standard library, SQLite, `git`, and the GitHub CLI. The stable daemon
runs one scheduler tick every 60 seconds under `launchd`; `mahler serve` is a
separate optional console process.

## What is built today

- GitHub Issues as the backlog, with sorting, planning, dependency and parent
  handling, platform pins, and comment commands.
- Quota-aware routing, multi-account isolation, leases, pre-emption, worktree
  isolation, snapshots, retries, and cross-platform handoffs.
- Conductor-owned PR creation, CI watching, merge confirmation, and revert PRs.
- A phone and desktop console for status, backlog, capture, needs-you answers,
  live logs, stop-and-handoff, quota controls, merge reverts, and UAT pass/fail.
- A small local MCP server for queue and lease operations.
- Scheduled backups, digests, cleanup, calibration, and recurring maintenance
  reviews.

The longer-term design contains additional planned capabilities. See the
current-state section of [ROADMAP.md](ROADMAP.md) before treating a design
statement as shipped behavior.

## Requirements

- macOS with `launchd`
- Python 3.12 or newer
- `git` and an authenticated [GitHub CLI](https://cli.github.com/)
- a GitHub repository for every managed project
- at least one supported agent CLI, installed and authenticated
- optional: ntfy for notifications and Tailscale for private phone access

Mahler runs agent CLIs unattended in isolated worktrees. Read
[DESIGN.md](DESIGN.md), especially D6, D8, D12, D18, and D25, before enabling it
on a repository with sensitive data or multiple accounts.

## Install the daemon

Clone your fork or trusted copy, then run:

```bash
git clone <your-mahler-repository> Mahler
cd Mahler
launcher/install.sh
```

The installer creates `~/.mahler/app`, copies the stable launcher, creates
`~/.mahler/config.toml` from `config.example.toml` if needed, and links
`mahler` into `~/.local/bin`. It does not start the daemon. Ensure
`~/.local/bin` is on your `PATH`, edit the config, then use the printed command:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.mahler.plist
```

The service follows `origin/main` of the repository you cloned. It advances
only after GitHub checks and its local unit suite pass, records a known-good
commit, and rolls back after two failed ticks. Files under `launcher/` are
hand-installed; after changing them, rerun `launcher/install.sh`.

To stop the daemon:

```bash
launchctl bootout gui/$(id -u)/local.mahler
```

## Configure a project

Nothing is managed until a project explicitly has `enabled = true`. Start with
the safe, disabled example in `config.example.toml`:

```toml
[projects.myapp]
enabled = true
repo = "my-org/myapp"
path = "/absolute/path/to/myapp"
max_parallel = 1
verify = "python3 -m unittest discover -s tests"
```

Then create Mahler's labels and inspect a dry scheduler pass:

```bash
mahler labels myapp
mahler tick --dry-run
mahler status
```

For interactive Claude Code participation, install the repository-local lease
hooks:

```bash
mahler hooks myapp
```

Default routes are defined in `mahler/config.py`. Override `[routing]`, or an
account's `routing`, so it names only the logins you intend Mahler to spend.
Codex is deliberately opt-in. Work-account configuration fails closed; see D25
and the commented examples in `config.example.toml`.

## Operate Mahler

Useful commands:

```bash
mahler status
mahler usage --probe
mahler add myapp "Describe the work"
mahler pause
mahler resume
mahler version
```

On a managed GitHub issue, these comments take effect on the next tick:

- `/mahler go` — retry or make the item ready
- `/mahler park` — park it and stop active work
- `/mahler platform <name>` — pin the next run
- `/mahler platform auto` — clear a pin

A normal human comment on a `needs-you` item supplies the answer and sends the
item back through sorting. The complete command reference is in
[docs/commands.md](docs/commands.md).

## Operator console

Run the console in the foreground:

```bash
mahler serve                  # http://127.0.0.1:8787
mahler serve --port 9000
```

Host and port can also be set in `~/.mahler/config.toml`:

```toml
[serve]
host = "127.0.0.1"
port = 8787
```

The console's write endpoints accept only loopback or Tailscale source
addresses, JSON with `X-Mahler-Console: 1`, and a same-origin request. Read
endpoints do not authenticate. They can expose issue titles, events,
attachments, and live agent logs. Keep the default loopback binding unless the
network is trusted; do not expose `mahler serve` directly to the public internet.

For private phone access, proxy the loopback listener through your tailnet:

```bash
tailscale serve http://127.0.0.1:8787
```

Then open the HTTPS URL reported by Tailscale. To stop sharing:

```bash
tailscale serve --https=443 off
```

A template plist is included at `launcher/local.mahler.serve.plist`
(KeepAlive, logs to `~/.mahler/logs/serve.*.log`). It is **not installed
automatically**. To install it by hand:

```bash
sed "s|__HOME__|$HOME|g" launcher/local.mahler.serve.plist \
  > ~/Library/LaunchAgents/local.mahler.serve.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.mahler.serve.plist
```

## MCP server

`mahler mcp` exposes `list_items`, `add_item`, `claim`, `heartbeat`, `release`,
`handoff`, and `next_id` over standard input/output. For example:

```bash
claude mcp add mahler-mcp mahler mcp
agy mcp add mahler-mcp mahler mcp
```

Cline configuration:

```json
{
  "mcpServers": {
    "mahler-mcp": {
      "command": "mahler",
      "args": ["mcp"]
    }
  }
}
```

The MCP server is intentionally minimal and local. Planned tools such as
`ask_user`, `report_progress`, and `get_context` are not implemented yet.

## Develop and verify

```bash
python3 -m unittest discover -s tests
python3 tests/run_random.py
```

Python 3.12+ and the standard library are the supported runtime. Tests must not
touch live `~/.mahler` state. Contributor and agent rules live in
[AGENTS.md](AGENTS.md); design decisions in [DESIGN.md](DESIGN.md); current and
planned work in [ROADMAP.md](ROADMAP.md).
