"""Presence-lite (DESIGN D6 layer 2): is a human-driven Claude session active here?

Claude Code writes each session's transcript under
~/.claude/projects/<cwd with every non-alphanumeric char replaced by '-'>/.
Edit timestamps in transcripts matching the project path (including nested
worktrees) identify writers; read-only conversations do not hold builds.
Mahler's own runs live under ~/.mahler/worktrees, which encodes differently,
so they never count as human activity.
"""

import glob
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")


def encode(path):
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(path))


@dataclass(frozen=True)
class Activity:
    at: datetime
    directory: str
    fallback: bool = False


def _transcript_activity(path, mtime, directory):
    """Stream the whole transcript so a long read-only tail cannot hide an edit.

    Unknown records and incomplete writes retain the old mtime safety net.
    Tool invocations are intent signals; waiting for results could miss a writer
    while the tool is still running.
    """
    fallback = Activity(mtime, directory, True)
    latest = None
    seen = False
    try:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict):
                    return fallback
                cwd = row.get("cwd")
                if isinstance(cwd, str) and cwd:
                    directory = cwd
                    fallback = Activity(mtime, directory, True)
                kind = row.get("type")
                if kind in {"system", "progress", "summary", "file-history-snapshot",
                            "queue-operation", "last-prompt"}:
                    continue
                if kind not in {"assistant", "user"}:
                    return fallback
                content = row.get("message", {}).get("content")
                if isinstance(content, str):
                    seen = True
                    continue
                if not isinstance(content, list):
                    return fallback
                seen = True
                for block in content:
                    if not isinstance(block, dict) or "type" not in block:
                        return fallback
                    if block["type"] != "tool_use":
                        if block["type"] not in {"text", "thinking", "redacted_thinking",
                                                "tool_result", "image", "document"}:
                            return fallback
                        continue
                    name = block.get("name")
                    if not isinstance(name, str):
                        return fallback
                    writes = name in {"Edit", "Write", "NotebookEdit", "MultiEdit"}
                    if name == "Bash":
                        command = block.get("input", {}).get("command")
                        if not isinstance(command, str):
                            return fallback
                        writes = bool(re.search(r"\bgit\b[^\n;&|]*\b(?:commit|push)\b", command))
                    if not writes:
                        continue
                    at = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                    if at.tzinfo is None:
                        return fallback
                    activity = Activity(at.astimezone(timezone.utc), directory)
                    if latest is None or activity.at > latest.at:
                        latest = activity
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return fallback
    return latest if seen else fallback


def last_claude_edit(project_path, root=None):
    """Latest edit signal and its session directory, or None for read-only chats."""
    root = CLAUDE_PROJECTS if root is None else root
    prefix = encode(project_path)
    latest = None
    for d in glob.glob(os.path.join(root, prefix + "*")):
        name = os.path.basename(d)
        # exact dir, or a nested path (worktree) — not a sister like "<name>X"
        if name != prefix and not name.startswith(prefix + "-"):
            continue
        for f in glob.glob(os.path.join(d, "*.jsonl")):
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(f), timezone.utc)
            except OSError:
                continue
            # Encoding is lossy; prefer the recorded cwd whenever available.
            directory = str(project_path) if name == prefix else name[len(prefix) + 1:]
            activity = _transcript_activity(f, mtime, directory)
            if activity and (latest is None or activity.at > latest.at):
                latest = activity
    return latest


def last_claude_activity(project_path, root=None):
    """Timestamp shared by scheduling, lease expiry and burst suppression."""
    activity = last_claude_edit(project_path, root=root)
    return activity.at if activity else None


def human_claude_active(projects, minutes=20, root=None):
    """True if a human is actively using Claude Code on a managed project (D23).

    Walks each project's primary checkout path for a recently modified
    edit signal. Mahler's own runs live under ~/.mahler/worktrees, whose
    encoded path differs from the primary checkout, so they never count.
    A project's own hot_hold_minutes (D6) wins over the default window,
    so burst suppression and the schedule's hot hold agree on how long
    activity keeps counting.
    """
    now = datetime.now(timezone.utc)
    for p in projects:
        path = p.get("path")
        if not path:
            continue
        window = timedelta(minutes=p.get("hot_hold_minutes", minutes))
        last = last_claude_activity(path, root=root)
        if last and now - last < window:
            return True
    return False
