# mahler#903 findings: Claude Agent SDK runner

- `claude-agent-sdk` 0.2.165 (Python; bundles Claude Code CLI 2.1.294, user-agent
  `claude-cli/2.1.294 (external, sdk-py, agent-sdk/0.2.165)`). Python 3.14, managed by uv.
- Runner: `mahler_sdk_run.py`, console script `mahler-sdk-run` (setuptools single module).
  Mahler stays stdlib-only; this directory has its own venv.

## Coverage check (real API, step 3)

**Succeeded.** One call, no retry, no "credit balance is too low" error.
- Command (key from env, `ANTHROPIC_BASE_URL` unset):
  `mahler-sdk-run --prompt-file pong.txt --cwd <empty tmp dir> --model claude-haiku-5-5 --max-turns 1 --max-output-tokens 64 <--disallowed-tool X for all 23 built-in tools>`
- Reply `PONG`, `is_error: false`, `terminal_reason: completed`, `apiKeySource: ANTHROPIC_API_KEY`.
- Usage: input 2, cache_creation 2305, output 5. SDK-reported `total_cost_usd` = 0.00029 (client-side list-price estimate).
- Caveat: the call was accepted by the API with this key and the balance was not exhausted. The client
  cannot see which credit pool was debited; confirm the balance drop in the Anthropic console.
- Cost note: with default tools the request body is ~76 KB (~20k tokens; ~108 KB if host MCP/plugins
  load), which could exceed $0.01 even on Haiku. Disallowing all built-in tools cut it to ~5.7 KB.
  The Check call used this to stay under budget; real Mahler runs will want their tools.

## Wire capture (local fake server, `ANTHROPIC_API_KEY=dummy`, step 4)

Every run made exactly **2 requests**, both `POST /v1/messages?beta=true` (no GETs, no other paths,
only that query string), `stream: true`, `max_tokens: 64` (= `CLAUDE_CODE_MAX_OUTPUT_TOKENS`
honored on both), `model: claude-haiku-5-5`:
1. A small helper call (~4 KB, 0 tools, ~3.3k-char system prompt; Claude Code's side request such as
   title generation). Beta header: `interleaved-thinking-2025-05-14,thinking-token-count-2026-05-13,
   context-management-2025-06-27,prompt-caching-scope-2026-01-05,mid-conversation-system-2026-04-07,
   per-turn-control-2026-07-01,mid-conversation-tool-changes-2026-07-01,inline-tools-2026-09-15,
   advisor-tool-2026-03-01,effort-2025-11-24,structured-outputs-2025-12-15`. Body has `output_config`.
2. The main agent turn (tools, `thinking`, `context_management`, `output_config`). Same betas plus
   `claude-code-20250219` and `thinking-display-updates-2026-08-18` (minus `structured-outputs`).

Headers (both): Accept, Content-Type, User-Agent, X-Claude-Code-Session-Id, X-Stainless-{Arch,Lang,OS,
Package-Version,Retry-Count,Runtime,Runtime-Version,Timeout}, anthropic-beta,
anthropic-dangerous-direct-browser-access, anthropic-version (`2023-06-01`), x-api-key (auth via
`x-api-key`, no `Authorization`), x-app, Connection, Host, Accept-Encoding, Content-Length.

Gotchas:
- By default the SDK loads host user settings, plugins, skills and MCP servers (from `~/.claude`);
  the runner sets `setting_sources=[]`, `strict_mcp_config=True`, `mcp_servers={}` for hermetic runs.
- Stderr prints a warning that claude.ai connectors are disabled because ANTHROPIC_API_KEY is set; harmless.
- `max_tokens` goes on the wire as exactly the env value.

## Working command line

    cd ~/mahler-sdk-runner
    echo "Reply with exactly PONG" > /tmp/pong.txt
    ANTHROPIC_API_KEY=... [ANTHROPIC_BASE_URL=...] \
      .venv/bin/mahler-sdk-run --prompt-file /tmp/pong.txt --cwd /tmp/empty \
        --model claude-haiku-5-5 --max-turns 1 --max-output-tokens 64 \
        --disallowed-tool Bash --disallowed-tool Read ...   # repeatable

Stdout is JSON lines (one per SDK message, key `type` = class name), final line
`{"type":"result","usage":...,"total_cost_usd":...,"is_error":...,"subtype":...,"result":...}`,
plus `errors` and `credit_error` (verbatim) if any error mentions "credit balance is too low".
Exit 0 on success, 1 on error, 2 on bad args. The API key is redacted from all output.

`wire_capture_server.py <port> <logfile>` is the fake Anthropic server used for step 4.
