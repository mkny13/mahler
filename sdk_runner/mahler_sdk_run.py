"""Standalone Claude Agent SDK runner that Mahler can invoke as a subprocess.

Reads ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL from the environment only (never
written or printed). Emits one JSON object per SDK message on stdout, then a
final {"type": "result", ...} line. Exit 0 on success, non-zero on error.
"""

import argparse
import asyncio
import dataclasses
import json
import os
import sys
from pathlib import Path

CREDIT_MSG = "credit balance is too low"


def _redact(text: str) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    return text.replace(key, "[REDACTED]") if key else text


def _emit(obj: dict) -> None:
    print(_redact(json.dumps(obj, default=str)), flush=True)


def _to_dict(msg) -> dict:
    body = dataclasses.asdict(msg) if dataclasses.is_dataclass(msg) else {"repr": repr(msg)}
    return {"type": type(msg).__name__, **body}


def _parse_args(argv):
    p = argparse.ArgumentParser(prog="mahler-sdk-run", description=__doc__)
    p.add_argument("--prompt-file", required=True, type=Path)
    p.add_argument("--cwd", required=True, type=Path)
    p.add_argument("--model", required=True)
    p.add_argument("--max-turns", type=int, default=None)
    p.add_argument("--max-output-tokens", type=int, default=None)
    p.add_argument("--disallowed-tool", action="append", default=[], dest="disallowed")
    p.add_argument("--max-budget-usd", type=float, default=None,
                   help="hard stop once the SDK's own cost estimate reaches this")
    p.add_argument("--project-settings", action="store_true",
                   help="load the --cwd project's CLAUDE.md and .claude/settings (never the host user's)")
    return p.parse_args(argv)


async def _run(args) -> int:
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    prompt = args.prompt_file.read_text()
    # Claude Code sends side calls (titles, summaries) to its small/fast
    # model, Haiku by default; pin them to the run's model so every request
    # stays on the one model the credits gateway allows.
    env = {"ANTHROPIC_SMALL_FAST_MODEL": args.model,
           "ANTHROPIC_DEFAULT_HAIKU_MODEL": args.model}
    if args.max_output_tokens is not None:
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(args.max_output_tokens)
    opts = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(args.cwd),
        model=args.model,
        max_turns=args.max_turns,
        max_budget_usd=args.max_budget_usd,
        disallowed_tools=list(args.disallowed),
        env=env,
        # Hermetic: never the host user's settings, plugins or MCP servers;
        # the worktree's own project settings only when asked.
        setting_sources=["project"] if args.project_settings else [],
        strict_mcp_config=True,
        mcp_servers={},
    )

    final = None
    errors: list[str] = []
    try:
        async for msg in query(prompt=prompt, options=opts):
            _emit(_to_dict(msg))
            if isinstance(msg, ResultMessage):
                final = msg
    except Exception as exc:  # noqa: BLE001 - surface every failure in the result line
        errors.append(f"{type(exc).__name__}: {exc}")

    if final is not None:
        if final.is_error:
            errors.extend(final.errors or [])
            if final.result:
                errors.append(final.result)
        result = {
            "type": "result",
            "usage": final.usage,
            "total_cost_usd": final.total_cost_usd,
            "is_error": bool(final.is_error) or bool(errors),
            "subtype": final.subtype,
            "result": final.result,
        }
    else:
        result = {"type": "result", "usage": None, "total_cost_usd": None, "is_error": True}
        if not errors:
            errors.append("no result message received from SDK")

    if errors:
        result["errors"] = errors
        credit = [e for e in errors if CREDIT_MSG in e.lower()]
        if credit:
            result["credit_error"] = credit[0]
    _emit(result)
    return 1 if result["is_error"] else 0


def main(argv=None) -> None:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
