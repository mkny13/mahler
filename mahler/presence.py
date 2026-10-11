"""Presence-lite (DESIGN D6 layer 2): is a human-driven Claude session active here?

Claude Code writes each session's transcript under
~/.claude/projects/<cwd with every non-alphanumeric char replaced by '-'>/.
A recently modified transcript whose directory matches the project path
(including worktrees nested inside it) means an interactive session is
working there. Mahler's own runs live under ~/.mahler/worktrees, which
encodes differently, so they never count as human activity.
"""

import glob
import json
import os
import re
from datetime import datetime, timedelta, timezone

from .ledger import iso, parse

HOT_HOLD_END_PREFIX = "hot_hold_end:"

CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")


def _marker(led, project):
    """(ended_at, exempt transcript paths or None for a legacy marker), or None."""
    raw = led.get_kv(HOT_HOLD_END_PREFIX + project)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        data = raw
    try:
        if isinstance(data, dict):
            return parse(data["at"]), frozenset(data.get("files") or ())
        return parse(raw), None
    except (TypeError, ValueError, KeyError):
        return None


def end_marker(led, project, project_path):
    """Value stored by End session: the time plus the transcripts of the
    sessions that existed then. Only those sessions are exempt; a session
    that starts later is a different session and still holds."""
    return json.dumps({"at": iso(led.now()),
                       "files": sorted(transcripts(project_path))})


def hot_hold_activity(led, project, project_path, window, now=None,
                      retire=False):
    """Latest transcript activity that should count toward the hot hold.

    Sessions present when End session was clicked stay exempt while they
    keep writing (watching a run); the exemption retires once all of them
    have been quiet for `window` (retire=True also clears the marker; tick
    only, the console reads without writing). Transcripts of any other
    session always count.
    """
    now = now or led.now()
    marker = _marker(led, project)
    if marker is None:
        return last_claude_activity(project_path)
    _ended, exempt = marker
    overall = last_claude_activity(project_path)
    if exempt is None:                      # legacy marker: all sessions exempt
        exempt_last, counting = overall, None
    else:
        files = transcripts(project_path)
        mt = [files[f] for f in exempt if f in files]
        exempt_last = (datetime.fromtimestamp(max(mt), timezone.utc)
                       if mt else None)
        counting = last_claude_activity(project_path, exclude=exempt)
    if exempt_last is None or now - exempt_last >= window:
        if retire:
            led.set_kv(HOT_HOLD_END_PREFIX + project, None)
        return overall
    return counting


def encode(path):
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(path))


def transcripts(project_path, root=None):
    """{transcript path: mtime} for sessions in this project."""
    root = root or CLAUDE_PROJECTS
    prefix = encode(project_path)
    out = {}
    for d in glob.glob(os.path.join(root, prefix + "*")):
        name = os.path.basename(d)
        # exact dir, or a nested path (worktree) — not a sister like "<name>X"
        if name != prefix and not name.startswith(prefix + "-"):
            continue
        for f in glob.glob(os.path.join(d, "*.jsonl")):
            try:
                out[f] = os.path.getmtime(f)
            except OSError:
                continue
    return out


def last_claude_activity(project_path, root=None, exclude=()):
    """Most recent transcript mtime for sessions in this project, or None."""
    times = [m for f, m in transcripts(project_path, root).items()
             if f not in exclude]
    return datetime.fromtimestamp(max(times), timezone.utc) if times else None


def human_claude_active(projects, minutes=20, root=CLAUDE_PROJECTS):
    """True if a human is actively using Claude Code on a managed project (D23).

    Walks each project's primary checkout path for a recently modified
    transcript. Mahler's own runs live under ~/.mahler/worktrees, whose
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


def human_claude_groups(cfg, projects, minutes=20):
    """Quota groups with recent human transcripts, attributed to their login."""
    projects = list(projects)
    roots = {"claude": CLAUDE_PROJECTS}
    for account, settings in cfg.get("accounts", {}).items():
        home = (settings.get("env") or {}).get("CLAUDE_CONFIG_DIR")
        if home:
            group = "claude" if account == "personal" else f"claude@{account}"
            roots[group] = os.path.join(os.path.expanduser(home), "projects")
    return {group for group, root in roots.items()
            if human_claude_active(projects, minutes, root)}
