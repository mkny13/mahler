#!/bin/sh
# One-shot bootstrap (mahler#918): the Agent SDK runner, on the Team-plan API
# credits, builds its own Mahler integration. Run on the mini, after
# `security unlock-keychain` when over ssh. Hard stop at $6 (SDK estimate);
# the Mahler Console workspace limit is the server-side backstop.
set -eu
BRANCH=mahler/918-agent-sdk-harness
WT="$HOME/Mahler-918"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"

key=$(security find-generic-password -s mahler-anthropic-api -a work -w 2>/dev/null) || {
  echo "Cannot read the API key. Over ssh, run: security unlock-keychain" >&2; exit 1; }

git -C "$HOME/Mahler" fetch -q origin "$BRANCH"
if [ ! -d "$WT" ]; then
  git -C "$HOME/Mahler" worktree add -q "$WT" -B "$BRANCH" "origin/$BRANCH"
fi
uv tool install -q --force --python 3.14 "$WT/sdk_runner"

LOG="$HOME/918-run.jsonl"
echo "Working in $WT; full log: $LOG"
ANTHROPIC_API_KEY="$key" mahler-sdk-run \
  --prompt-file "$WT/sdk_runner/bootstrap/918-task.md" --cwd "$WT" \
  --model claude-sonnet-5-5 --project-settings \
  --max-turns 300 --max-budget-usd 6 --max-output-tokens 16000 \
  --disallowed-tool "Bash(git push:*)" \
  < /dev/null | tee "$LOG" | /opt/homebrew/bin/python3.14 -c '
import json, sys
for line in sys.stdin:
    try: m = json.loads(line)
    except ValueError: continue
    if m.get("type") == "AssistantMessage":
        for b in m.get("content") or []:
            if isinstance(b, dict) and b.get("text"): print(b["text"].strip()[:400], flush=True)
            elif isinstance(b, dict) and b.get("name"): print("  ->", b["name"], str(b.get("input", ""))[:120], flush=True)
    elif m.get("type") == "result":
        print("\nRESULT:", "error" if m.get("is_error") else "ok",
              "| cost $%.2f" % (m.get("total_cost_usd") or 0), "|", (m.get("result") or "")[:1500])
'
