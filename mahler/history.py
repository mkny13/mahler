"""Searchable history of ended runs (DESIGN D11), an FTS5 table in the ledger DB.

Standard library only. Every function degrades to a no-op/empty result when
FTS5 is unavailable or anything fails: history is background, never a gate.
Text is redacted before insert, and searches are strictly per project (D25)."""

import logging
import os
import re
import sqlite3
import subprocess

from . import redact as redact_mod

log = logging.getLogger(__name__)

LOG_TAIL_BYTES = 8192
MAX_TERMS = 40
_TABLE = "run_history"
_WORD = re.compile(r"[A-Za-z0-9_]{3,}")


def _ensure(led):
    try:
        led.con.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {_TABLE} USING fts5("
            "project UNINDEXED, run_id UNINDEXED, number UNINDEXED, role, outcome, body)")
        return True
    except sqlite3.Error as e:
        log.warning("history: FTS5 unavailable: %s", e)
        return False


def _log_tail(path):
    if not path:
        return ""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 2 * LOG_TAIL_BYTES))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    text = redact_mod.redact(text)
    return text[-LOG_TAIL_BYTES:]


def _changed_files(run, base=None):
    """Read actual committed, dirty and untracked paths while the tree exists.

    Old runs whose worktree has gone have no Git evidence available; never
    substitute their planned file list for observed changes.
    """
    wt = run["worktree"]
    base = base or run["base_ref"]
    if not wt or not base or not os.path.isdir(wt):
        return ""
    def git(*args):
        result = subprocess.run(["git", "-C", wt, *args], capture_output=True,
                                text=True, timeout=10, check=True)
        return result.stdout
    ancestor = git("merge-base", base, "HEAD").strip()
    paths = git("diff", "--name-only", "-z", ancestor, "--").split("\0")
    paths += git("ls-files", "--others", "--exclude-standard", "-z").split("\0")
    return "\n".join(sorted(set(paths) - {""}))


def _body(led, run, base=None):
    item = led.q1("SELECT * FROM items WHERE project=? AND number=?",
                  (run["project"], run["number"]))
    parts = []
    if item:
        parts.append(item["title"] or "")
        parts.append(item["issue_body"] or "")
        if item["summary"]:
            parts.append("STATUS: " + item["summary"])
    files = _changed_files(run, base)
    if files:
        parts.append("files changed:\n" + files)
    tail = _log_tail(run["log_path"])
    # The final STATUS line is in the log tail; keep it ahead of the rest.
    from . import platforms
    verb, rest = platforms.status_line(tail)
    if verb:
        parts.insert(0, f"STATUS: {verb} {rest}")
    parts.append(tail)
    return redact_mod.redact("\n".join(p for p in parts if p))


def refresh(led, project):
    """Index this project's ended runs not yet indexed. Never raises."""
    try:
        if not _ensure(led):
            return 0
        runs = led.q(
            f"SELECT * FROM runs WHERE project=? AND status='ended' AND id NOT IN "
            f"(SELECT run_id FROM {_TABLE} WHERE project=?) ORDER BY id", (project, project))
    except Exception as e:
        log.warning("history: refresh failed: %s", e)
        return 0
    return sum(index_run(led, run) for run in runs)


def index_run(led, run, base=None):
    """Persist ended-run evidence before finalization removes its worktree."""
    try:
        if run["status"] != "ended" or not _ensure(led):
            return 0
        if led.q1(f"SELECT run_id FROM {_TABLE} WHERE project=? AND run_id=?",
                  (run["project"], run["id"])):
            return 0
        body = _body(led, run, base)
        led.con.execute(
            f"INSERT INTO {_TABLE} (project, run_id, number, role, outcome, body) "
            "VALUES (?,?,?,?,?,?)",
            (run["project"], run["id"], run["number"], run["role"],
             redact_mod.redact(run["outcome"] or ""), body))
        return 1
    except Exception as e:
        log.warning("history: could not index run %s: %s", run["id"], e)
        return 0


def _match_query(text):
    seen, terms = set(), []
    for w in _WORD.findall(text or ""):
        k = w.lower()
        if k not in seen:
            seen.add(k)
            terms.append(f'"{k}"')
        if len(terms) >= MAX_TERMS:
            break
    return " OR ".join(terms)


def search(led, project, query, limit=3, exclude_number=None):
    """Same-project hits, best bm25 first: dicts of run_id, number, role,
    outcome, excerpt. Never raises."""
    try:
        q = _match_query(query)
        if not q or limit < 1 or not _ensure(led):
            return []
        sql = (f"SELECT run_id, number, role, outcome, "
               f"snippet({_TABLE}, 5, '', '', ' … ', 40) AS excerpt FROM {_TABLE} "
               f"WHERE {_TABLE} MATCH ? AND project = ?")
        args = [q, project]
        if exclude_number is not None:
            sql += " AND number != ?"
            args.append(int(exclude_number))
        sql += " ORDER BY bm25(%s) LIMIT ?" % _TABLE
        args.append(int(limit))
        return [dict(r) for r in led.q(sql, args)]
    except Exception as e:
        log.warning("history: search failed: %s", e)
        return []


def render_for_prompt(led, project, item, max_runs=3, max_bytes=3000):
    """A short block of similar past runs for a build/fix prompt, or ""."""
    try:
        title = item["title"] if item["title"] else ""
        try:
            body = item["issue_body"] or ""
        except (KeyError, IndexError):
            body = ""
        refresh(led, project)
        hits = search(led, project, f"{title}\n{body}", limit=max_runs,
                      exclude_number=item["number"])
        lines = []
        for h in hits:
            ex = " ".join((h["excerpt"] or "").split())[:400]
            lines.append(f"- run {h['run_id']} (#{h['number']}, {h['role']}, "
                         f"{h['outcome'] or 'no outcome'}): {ex}")
        text = "\n".join(lines)
        return text.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")
    except Exception as e:
        log.warning("history: render failed: %s", e)
        return ""
