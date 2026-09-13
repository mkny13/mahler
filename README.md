# Mahler

Mahler conducts coding agents (Claude Code, Antigravity's `agy`, Cline) through each project's GitHub-issue backlog. It is quota-aware, and hands work off across platforms.

## Status page (read-only)

`mahler serve` starts a standard-library (`http.server`) web server that renders
what `mahler status` shows as a single phone-friendly HTML page: running work
with minutes elapsed and platform, items by state with links to their GitHub
issues, quota gauges per platform, and the last 30 events. It auto-refreshes
every 30 seconds and follows the OS light/dark setting.

```bash
mahler serve                  # http://127.0.0.1:8787
mahler serve --host 0.0.0.0   # listen on all interfaces
mahler serve --port 9000
```

The host and port can also be set in `~/.mahler/config.toml`:

```toml
[serve]
host = "127.0.0.1"
port = 8787
```

The page is strictly read-only: only `GET /` is served; anything else gets a
404 or 405. By default, the server binds to `127.0.0.1`. You can expose it
to your local network using `--host 0.0.0.0`.

### Viewing it from your phone (Tailscale)

Exposing the page over your tailnet is a machine setting you turn on yourself.
On the machine running Mahler, run once:

```bash
tailscale serve --bg http://127.0.0.1:8787
```

Then open `https://<this-machine>.<tailnet-name>.ts.net/` on the phone.
To stop sharing: `tailscale serve --https=443 off`.

### Running it under launchd (optional)

A template plist is included at `launcher/com.mike.mahler.serve.plist`
(KeepAlive, logs to `~/.mahler/logs/serve.*.log`). It is **not installed
automatically**. To install it by hand:

```bash
sed "s|__HOME__|$HOME|g" launcher/com.mike.mahler.serve.plist \
  > ~/Library/LaunchAgents/com.mike.mahler.serve.plist
launchctl load ~/Library/LaunchAgents/com.mike.mahler.serve.plist
```

Note: like everything under `launcher/`, this file only takes effect once
it's copied/installed by hand — Mahler never edits your LaunchAgents.

## MCP Server

Mahler provides a minimal MCP (Model Context Protocol) server over standard I/O to allow interactive chat sessions to read and manage the Mahler queue directly from the chat. The server is implemented using the Python standard library.

To register the server in your favorite agent chat:

**Claude Code:**
```bash
claude mcp add mahler-mcp mahler mcp
```

**Antigravity (`agy`):**
```bash
agy mcp add mahler-mcp mahler mcp
```

**Cline:**
You can add it to Cline's `cline_mcp_settings.json` file like this:
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
