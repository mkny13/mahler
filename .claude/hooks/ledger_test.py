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


def edited_ledger(data):
    path = (data.get("tool_input") or {}).get("file_path") or ""
    if not path:
        return False
    full = os.path.realpath(os.path.join(data.get("cwd") or ROOT, path))
    return full == os.path.realpath(os.path.join(ROOT, TARGET))


def main():
    try:
        data = json.load(sys.stdin)
        if data.get("tool_name") not in ("Edit", "Write", "MultiEdit"):
            return
        if not edited_ledger(data):
            return
        r = subprocess.run(
            [sys.executable, "-m", "unittest", "tests.test_ledger"],
            cwd=ROOT, capture_output=True, text=True, timeout=120)
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
