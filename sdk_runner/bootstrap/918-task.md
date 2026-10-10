# Task: wire the Agent SDK runner into Mahler as the API-credits harness (mahler#918)

You are a Claude Agent SDK run spending the Team plan's API credits, working in a git
worktree of `mkny13/mahler` on branch `mahler/918-agent-sdk-harness`. Read `CLAUDE.md`,
then DESIGN.md's D41 section, `docs/team-api-credits.md`, `sdk_runner/FINDINGS.md`,
`sdk_runner/mahler_sdk_run.py`, and `mahler/api_credits.py` (the `Gateway` class and
`ensure_gateway`) before changing anything. Issue: https://github.com/mkny13/mahler/issues/918

Be economical: read the parts of big files you need (grep first), not whole files.

## What to build

1. **Gateway passes the Agent SDK's requests through** (`mahler/api_credits.py`):
   - Accept `POST /v1/messages` with or without the exact query string `?beta=true`
     and forward the same path+query upstream. Anything else stays 404.
   - Forward the client's `anthropic-beta`, `anthropic-version` and `User-Agent`
     headers unchanged (header lookups must be case-insensitive). Use
     `ANTHROPIC_VERSION` only when the client sent none. Never forward the client's
     `x-api-key` or `Authorization`; upstream always gets the real key.
     Don't mask the client: Anthropic tells Agent SDK traffic (covered by the credits)
     from Claude Code (not covered) by these headers.
   - Everything else (reserve-before-forward, settle/release, ambiguity handling)
     stays as is. Add tests in `tests/test_api_credits.py` for: the `?beta=true` path,
     an unknown query string (404), header pass-through, and that the client's key is
     never forwarded.

2. **A `kind = "agent-sdk"` platform adapter** (`mahler/platforms.py`), modeled on the
   existing kinds:
   - exe lookup for `mahler-sdk-run` (`~/.local/bin`, `/opt/homebrew/bin`), like `kilo_exe`.
   - argv: write the prompt to a file in the run's dir (follow how other adapters get
     a prompt file/arg) and call `mahler-sdk-run --prompt-file F --cwd <worktree>
     --model <pconf model> --project-settings --max-output-tokens <credit_pool
     max_output_tokens if set>`, plus one `--disallowed-tool` per entry of
     `CLAUDE_DENY` (it already uses Claude Code's `Bash(<stem>:*)` syntax).
   - log reader for its stdout JSON lines: assistant text comes from messages with
     `"type": "AssistantMessage"` (their `content` list's text blocks); the final
     `{"type":"result", ...}` line carries `usage`, `total_cost_usd`, `is_error` and,
     on exhaustion, `credit_error` (must trip `is_credit_exhausted`). Use the existing
     reader registry pattern. STATUS lines must still be found.
   - No resume support (leave it out of the resumable set).
   - `DEFAULTS["platforms"]["claude-api"]` switches from `kind = "cline"` /
     `provider = "anthropic"` to `kind = "agent-sdk"` (drop `provider`). Fix config
     validation and `config.example.toml` to match. Keep `enabled = False`.
   - Tests in the matching `tests/` files (argv, log reading incl. credit error,
     config).

3. **Environment** (`mahler/runner.py` `run_env`): the existing credits branch already
   sets `ANTHROPIC_BASE_URL` to the gateway and `ANTHROPIC_API_KEY` to the run's local
   token; that is exactly what the Agent SDK reads. Confirm it applies to the new kind
   and fix the stale comments that say the wiring must become `cline auth --data-dir`.

4. **Docs**: update DESIGN.md D41 (harness = Agent SDK runner in `sdk_runner/`,
   hand-installed with `uv tool install ./sdk_runner`; why: the 2026-10-10 findings in
   `sdk_runner/FINDINGS.md`; Kilo verified as a working fallback; Cline ruled out),
   `docs/team-api-credits.md` (status + qualifying steps), ROADMAP.md, and CLAUDE.md's
   Layout (one line for `sdk_runner/`). Short and factual; don't invent results.

## Verify, then stop

Run with `/opt/homebrew/bin/python3.14`:

    python3.14 -m mahler.repo_guard
    python3.14 -m unittest discover -s tests
    python3.14 tests/run_random.py
    python3.14 -X dev tests/run_strict.py

All four must pass. Commit in small logical commits ending with the line
`Co-Authored-By: Claude Agent SDK <noreply@anthropic.com>`. Do NOT push, do not open a PR,
do not touch `~/.mahler`, `~/Mahler`, launchd, keychain items, or `[api_credits] enabled`.
Do not make any network calls to Anthropic yourself. End with a short summary of what
changed and the test results.
