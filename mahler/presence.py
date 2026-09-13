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
from datetime import datetime, timedelta, timezone

CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")


def encode(path):
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(path))


def last_claude_activity(project_path, root=CLAUDE_PROJECTS):
    """Most recent transcript mtime for sessions in this project, or None."""
    prefix = encode(project_path)
    latest = None
    for d in glob.glob(os.path.join(root, prefix + "*")):
        name = os.path.basename(d)
        # exact dir, or a nested path (worktree) — not a sister like "<name>X"
        if name != prefix and not name.startswith(prefix + "-"):
            continue
        for f in glob.glob(os.path.join(d, "*.jsonl")):
            try:
                m = os.path.getmtime(f)
            except OSError:
                continue
            latest = m if latest is None else max(latest, m)
    return datetime.fromtimestamp(latest, timezone.utc) if latest else None


def human_claude_active(projects, minutes=20, root=CLAUDE_PROJECTS):
    """True if a human is actively using Claude Code on a managed project (D23).

    Walks each project's primary checkout path for a recently modified
    transcript. Mahler's own runs live under ~/.mahler/worktrees, whose
    encoded path differs from the primary checkout, so they never count.
    """
    now = datetime.now(timezone.utc)
    for p in projects:
        path = p.get("path")
        if not path:
            continue
        last = last_claude_activity(path, root=root)
        if last and now - last < timedelta(minutes=minutes):
            return True
    return False
