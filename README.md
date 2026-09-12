# Mahler

Mahler conducts coding agents (Claude Code, Antigravity's `agy`, Cline) through each project's GitHub-issue backlog. It is quota-aware, and hands work off across platforms.

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
