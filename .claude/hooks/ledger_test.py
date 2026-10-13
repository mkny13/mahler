#!/usr/bin/env python3
"""PostToolUse hook: run the ledger lease tests after an edit to mahler/ledger.py.

Fast, standard-library only, and exit-safe: always exits 0 so a hook problem
never wedges a session. Failures are reported as a JSON block decision on stdout.
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
TARGET = os.path.join("mahler", "ledger.py")


def edited_root(data):
    """Return the checkout root whose mahler/ledger.py was edited, else None.

    CLAUDE_PROJECT_DIR (and so this file's location) can point at the original
    checkout after the session enters a task worktree, so find the root from the
    edited file itself, not from where the hook lives.
    """
    path = (data.get("tool_input") or {}).get("file_path") or ""
    if not path:
        return None
    full = os.path.realpath(os.path.join(data.get("cwd") or ROOT, path))
    if os.path.basename(full) != "ledger.py":
        return None
    root = os.path.dirname(os.path.dirname(full))
    if os.path.dirname(full) != os.path.join(root, "mahler"):
        return None
    if not os.path.isdir(os.path.join(root, "tests")):
        return None
    return root


def main():
    try:
        data = json.load(sys.stdin)
        if data.get("tool_name") not in ("Edit", "Write", "MultiEdit"):
            return
        root = edited_root(data)
        if not root:
            return
        r = subprocess.run(
            [sys.executable, "-m", "unittest", "tests.test_ledger"],
            cwd=root, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            out = (r.stdout + r.stderr).strip()
            # Exit-0 JSON is what Claude Code feeds back to the model;
            # plain stdout only reaches the debug log.
            print(json.dumps({
                "decision": "block",
                "reason": "ledger tests FAILED after editing mahler/ledger.py "
                          "(lease rules must stay right):\n" + out[-4000:]}))
    except Exception:
        pass


if __name__ == "__main__":
    main()
    sys.exit(0)
