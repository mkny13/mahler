#!/usr/bin/env python3
"""PostToolUse hook: run the ledger lease tests after an edit to mahler/ledger.py.

Fast, standard-library only, and exit-safe: always exits 0 so a hook problem
never wedges a session. Test failures go to stdout for the transcript.
"""
import json
import os
import subprocess
import sys

TARGET = os.path.join("mahler", "ledger.py")


def edited_ledger(data):
    path = (data.get("tool_input") or {}).get("file_path") or ""
    if not path:
        return False
    root = data.get("cwd") or os.getcwd()
    full = os.path.realpath(os.path.join(root, path))
    return full == os.path.realpath(os.path.join(root, TARGET)) or \
        full.endswith(os.sep + TARGET)


def main():
    try:
        data = json.load(sys.stdin)
        if data.get("tool_name") not in ("Edit", "Write", "MultiEdit"):
            return
        if not edited_ledger(data):
            return
        root = data.get("cwd") or os.getcwd()
        r = subprocess.run(
            [sys.executable, "-m", "unittest", "tests.test_ledger"],
            cwd=root, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            out = (r.stdout + r.stderr).strip()
            print("ledger tests FAILED after editing mahler/ledger.py "
                  "(lease rules must stay right):\n" + out[-4000:])
    except Exception:
        pass


if __name__ == "__main__":
    main()
    sys.exit(0)
