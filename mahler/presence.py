"""Presence-lite (DESIGN D6 layer 2): is a human-driven Claude session active here?

Claude Code writes each session's transcript under
~/.claude/projects/<cwd with every non-alphanumeric char replaced by '-'>/.
A recently modified transcript whose directory matches the project path
(including worktrees nested inside it) means an interactive session is
working there. Mahler's own runs live under ~/.mahler/worktrees, which
encodes differently, so they never count as human activity.
"""

import glob
import os
import re
from datetime import datetime, timezone

CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")


def encode(path):
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(path))


def last_claude_activity(project_path, root=CLAUDE_PROJECTS):
    """Most recent transcript mtime for sessions in this project, or None."""
    prefix = encode(project_path)
    latest = None
    for d in glob.glob(os.path.join(root, prefix + "*")):
        name = os.path.basename(d)
        # exact dir, or a nested path (worktree) — not a sibling like "<name>X"
        if name != prefix and not name.startswith(prefix + "-"):
            continue
        for f in glob.glob(os.path.join(d, "*.jsonl")):
            try:
                m = os.path.getmtime(f)
            except OSError:
                continue
            latest = m if latest is None else max(latest, m)
    return datetime.fromtimestamp(latest, timezone.utc) if latest else None
