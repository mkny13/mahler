"""Presence-lite (DESIGN D6 layer 2): is a human-driven Claude session active here?

Claude Code writes each session's transcript under
~/.claude/projects/<cwd with every non-alphanumeric char replaced by '-'>/.
Recent edit tool calls or commit/push commands in transcripts under that
project path (including nested worktrees) identify writers; read-only
conversations do not hold builds. Empty, unreadable, or unknown transcripts fall
back to file mtime so a real editing session is never missed. Mahler's own runs
live under ~/.mahler/worktrees, which encodes differently, so they never count as
human activity.
"""

import glob
import json
import os
import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .ledger import iso, parse

HOT_HOLD_END_PREFIX = "hot_hold_end:"

CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")

# Claude writes these bookkeeping records alongside user/assistant messages.
# They carry no tool invocation, so they must not turn an otherwise read-only
# session into transcript-mtime activity. Keep unknown record types on the
# conservative fallback path below.
ROUTINE_TRANSCRIPT_RECORDS = frozenset({
    "agent-name",
    "ai-title",
    "atis-latch",
    "attachment",
    "bridge-session",
    "cost-state",
    "custom-title",
    "file-history-delta",
    "file-history-snapshot",
    "last-prompt",
    "mode",
    "permission-mode",
    "pr-link",
    "progress",
    "queue-operation",
    "relocated",
    "summary",
    "system",
    "worktree-state",
})


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
                      retire=False, details=False):
    """Latest transcript activity that should count toward the hot hold.

    Sessions present when End session was clicked stay exempt while they
    keep writing (watching a run); the exemption retires once all of them
    have been quiet for `window` (retire=True also clears the marker; tick
    only, the console reads without writing). Transcripts of any other
    session always count.
    """
    read = last_claude_edit if details else last_claude_activity
    now = now or led.now()
    marker = _marker(led, project)
    if marker is None:
        return read(project_path)
    ended, exempt = marker
    overall = read(project_path)
    overall_at = overall.at if details and overall else overall
    if exempt is None:                      # legacy marker: old timestamp rule
        if overall is None or overall_at <= ended:
            if retire and (overall is None or now - overall_at >= window):
                led.set_kv(HOT_HOLD_END_PREFIX + project, None)
            return None
        return overall
    files = transcripts(project_path)
    # Each marked session is judged on its own mtime: still writing within
    # the window means it is a watcher (exempt); quiet for a full window
    # means it retires and counts like any other session if it resumes.
    # One session's writes never keep another's exemption alive.
    exempt_active = {f for f in exempt
                     if f in files and now - _utc(files[f]) < window}
    if retire and exempt_active != set(exempt):
        key = HOT_HOLD_END_PREFIX + project
        if exempt_active:
            led.set_kv(key, json.dumps({"at": iso(ended),
                                        "files": sorted(exempt_active)}))
        else:
            led.set_kv(key, None)
    return read(project_path, exclude=exempt_active)


def _utc(mtime):
    return datetime.fromtimestamp(mtime, timezone.utc)


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


@dataclass(frozen=True)
class Activity:
    at: datetime
    directory: str
    fallback: bool = False


def _preprocess_shell(cmd):
    """Strip here-document bodies and shell comments from a command string.

    Returns the sanitized command string for tokenization.
    Raises ValueError if a here-document is unclosed.
    """
    lines = cmd.splitlines(keepends=True)
    out_lines = []
    pending_heredocs = []
    i = 0
    stack = []
    escape = False
    in_continuation = False

    while i < len(lines):
        if pending_heredocs and not in_continuation:
            delim, strip_tabs = pending_heredocs[0]
            line = lines[i]
            check_line = line.lstrip("\t") if strip_tabs else line
            if check_line.rstrip("\r\n") == delim:
                pending_heredocs.pop(0)
                out_lines.append(line)
            i += 1
            continue

        line = lines[i]
        j = 0
        cleaned_line_chars = []
        escaped_newline = False

        while j < len(line):
            c = line[j]
            top = stack[-1] if stack else None

            if escape:
                if c == "\n":
                    escaped_newline = True
                escape = False
                cleaned_line_chars.append(c)
                j += 1
                continue

            if top == "'":
                if c == "'":
                    stack.pop()
                cleaned_line_chars.append(c)
                j += 1
                continue

            if c == "\\":
                escape = True
                cleaned_line_chars.append(c)
                j += 1
                continue

            if top == '"':
                if c == '"':
                    stack.pop()
                    cleaned_line_chars.append(c)
                    j += 1
                    continue
                if c == "$" and j + 1 < len(line) and line[j + 1] == "(":
                    stack.append("$(")
                    cleaned_line_chars.append("$(")
                    j += 2
                    continue
                if c == "`":
                    stack.append("`")
                    cleaned_line_chars.append("`")
                    j += 1
                    continue
                cleaned_line_chars.append(c)
                j += 1
                continue

            if top == "`":
                if c == "`":
                    stack.pop()
                    cleaned_line_chars.append(c)
                    j += 1
                    continue

            # top is None, "$(" or "`"
            if c == "'":
                stack.append("'")
                cleaned_line_chars.append(c)
                j += 1
                continue

            if c == '"':
                stack.append('"')
                cleaned_line_chars.append(c)
                j += 1
                continue

            if c == "$" and j + 1 < len(line) and line[j + 1] == "(":
                stack.append("$(")
                cleaned_line_chars.append("$(")
                j += 2
                continue

            if c == ")" and top == "$(":
                stack.pop()
                cleaned_line_chars.append(c)
                j += 1
                continue

            if c == "`":
                stack.append("`")
                cleaned_line_chars.append("`")
                j += 1
                continue

            if c == "#":
                if j == 0 or line[j - 1] in " \t;&|(":
                    if line.endswith("\n"):
                        cleaned_line_chars.append("\n")
                    break

            if (
                line[j : j + 2] == "<<"
                and (j == 0 or line[j - 1] != "<")
                and (j + 2 >= len(line) or line[j + 2] != "<")
            ):
                rest = line[j:]
                m = re.match(
                    r'<<(-?)\s*(?:\'([^\']+)\'|"([^"]+)"|\\?([^\s;&|<>()\'"]+))',
                    rest,
                )
                if m:
                    strip_tabs = bool(m.group(1))
                    delim = m.group(2) or m.group(3) or m.group(4)
                    pending_heredocs.append((delim, strip_tabs))
                    cleaned_line_chars.append(rest[: m.end()])
                    j += m.end()
                    continue

            cleaned_line_chars.append(c)
            j += 1

        in_continuation = escaped_newline
        out_lines.append("".join(cleaned_line_chars))
        i += 1

    if pending_heredocs:
        raise ValueError(f"No closing delimiter for here-document: {pending_heredocs[0][0]}")

    return "".join(out_lines)


def _git_write(command):
    """Recognize commit/push subcommands, never words in their arguments.

    This is a tool-intent heuristic for simple shell commands, not a shell
    interpreter. Invalid quoting raises ValueError for the transcript fallback.
    """
    cleaned = _preprocess_shell(command)
    lexer = shlex.shlex(cleaned, posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace = " \t\r"
    lexer.commenters = ""
    words = list(lexer)
    start = 0
    for end in range(len(words) + 1):
        if end < len(words) and not all(c in ";&|()\n" for c in words[end]):
            continue
        args = words[start:end]
        start = end + 1
        if not args or os.path.basename(args[0]) != "git":
            continue
        i = 1
        while i < len(args) and args[i].startswith("-"):
            option = args[i]
            if option in {"--help", "--version"}:
                break
            # These global options consume the next word; attached values
            # (-Cpath, --git-dir=path) already occupy a single word.
            i += 2 if option in {"-C", "-c", "--git-dir", "--work-tree",
                                  "--namespace", "--config-env", "--super-prefix"} else 1
        if i < len(args) and args[i] in {"commit", "push"}:
            return True
    return False


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
                # Routine session metadata is not a tool invocation. Keep
                # scanning so it neither creates a hold nor hides a later edit.
                if kind in ROUTINE_TRANSCRIPT_RECORDS:
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
                        writes = _git_write(command)
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


def last_claude_edit(project_path, root=None, exclude=()):
    """Latest edit signal and session directory, excluding ended sessions."""
    prefix = encode(project_path)
    latest = None
    for path, mtime in transcripts(project_path, root).items():
        if path in exclude:
            continue
        name = os.path.basename(os.path.dirname(path))
        directory = str(project_path) if name == prefix else name[len(prefix) + 1:]
        activity = _transcript_activity(path, _utc(mtime), directory)
        if activity and (latest is None or activity.at > latest.at):
            latest = activity
    return latest


def last_claude_activity(project_path, root=None, exclude=()):
    """Write timestamp shared by scheduling, lease expiry and burst suppression."""
    activity = last_claude_edit(project_path, root, exclude)
    return activity.at if activity else None


def human_claude_active(projects, minutes=20, root=None):
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
