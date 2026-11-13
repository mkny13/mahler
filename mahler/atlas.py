"""Atlas: a cached inventory of every project, managed or not (mahler#1532).

Three finite sources: enabled configured projects, direct children of
`[atlas] scan_roots`, and Claude Code session files. Folders outside the roots
and managed paths are never touched (stat, list, resolve, git): they are
recorded from session data alone, so a hung iCloud folder cannot stall a scan.
Standard library only. The result is cached in ledger kv `atlas:scan`.
"""
import contextlib
import glob
import json
import os
import re
import signal
import stat
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

from . import config
from .ledger import iso, parse

CACHE_KEY = "atlas:scan"
ATTEMPT_KEY = "atlas:last_attempt"
INTERVAL = 3600                  # seconds between automatic scans
BUDGET = 8.0                     # seconds of work for one whole scan
GIT_TIMEOUT = 5.0
TEMP_ROOTS = ("/private/tmp", "/tmp", "/var/folders", "/private/var/folders")
SESSION_LINES = 50
SESSION_BYTES = 256 * 1024
WORKTREE_MARK = "/.claude/worktrees"
DOC_EXTS = (".md", ".txt")


class ScanError(Exception):
    """The scan failed or ran out of time; the previous cache is untouched."""


class _Clock:
    def __init__(self, budget):
        self.end = time.monotonic() + budget

    def left(self):
        return self.end - time.monotonic()

    def check(self):
        if self.left() <= 0:
            raise ScanError("scan exceeded its time budget")


@contextlib.contextmanager
def _deadline(seconds):
    """Scoped SIGALRM that composes with an enclosing alarm and always restores it."""
    if (not hasattr(signal, "setitimer")
            or threading.current_thread() is not threading.main_thread()):
        yield
        return
    started = time.monotonic()

    def fire(signum, frame):
        raise ScanError("scan exceeded its time budget")
    previous = signal.signal(signal.SIGALRM, fire)
    old = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if old[0]:
            signal.setitimer(signal.ITIMER_REAL,
                             max(.001, old[0] - (time.monotonic() - started)), old[1])


# ---------- paths ----------

def _home():
    return os.path.normpath(os.path.expanduser("~"))


def _lex(p):
    return os.path.normpath(os.path.abspath(os.path.expanduser(p)))


def _under(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def _within(path, roots):
    return any(_under(path, r) for r in roots)


def _strip_worktree(cwd):
    i = cwd.find(WORKTREE_MARK + "/")
    if i >= 0:
        return cwd[:i]
    return cwd[:-len(WORKTREE_MARK)] if cwd.endswith(WORKTREE_MARK) else cwd


def _builtin_excluded(path):
    home = _home()
    if path == home or path == "/":
        return True
    parts = path.split("/")
    if ".mahler-worktrees" in parts or "scratch-workspaces" in parts:
        return True
    roots = [*TEMP_ROOTS, os.path.join(home, ".mahler"), os.path.join(home, ".cline", "worktrees"),
             config.STATE]
    return _within(path, roots)


def _user_ignored(path, ignore):
    for entry in ignore:
        if "/" in entry:
            if _under(path, _lex(entry)):
                return True
        elif os.path.basename(path) == entry:
            return True
    return False


# ---------- sessions ----------

def _history_dir():
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def _read_cwd(path):
    """First valid absolute cwd in a bounded read of the first lines, or None."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(SESSION_BYTES)
    except OSError:
        return None
    for raw in data.split(b"\n")[:SESSION_LINES]:
        try:
            cwd = json.loads(raw).get("cwd")
        except (ValueError, AttributeError):
            continue
        if isinstance(cwd, str) and cwd.startswith("/"):
            return _strip_worktree(os.path.normpath(cwd))
    return None


def _sessions(history, clock):
    """[(lexical cwd, mtime)] one per session file."""
    out = []
    for path in sorted(glob.glob(os.path.join(glob.escape(history), "*", "*.jsonl"))):
        clock.check()
        try:
            st = os.stat(path)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        cwd = _read_cwd(path)
        if cwd:
            out.append((cwd, st.st_mtime))
    return out


# ---------- folder inspection (admitted folders only) ----------

def _list_children(root):
    with os.scandir(root) as it:
        return sorted(it, key=lambda e: e.name)


def _git(path, clock, *args):
    timeout = min(GIT_TIMEOUT, clock.left())
    if timeout <= 0:
        raise ScanError("scan exceeded its time budget")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    try:
        return subprocess.run(["git", "-C", path, *args], capture_output=True, text=True,
                              timeout=timeout, env=env)
    except (subprocess.TimeoutExpired, OSError):
        return None


def _git_info(path, clock):
    """(commits or None, last commit datetime or None)."""
    if not os.path.exists(os.path.join(path, ".git")):
        return None, None
    r = _git(path, clock, "rev-list", "--count", "HEAD")
    if r is None:
        return None, None
    if r.returncode != 0:
        g = _git(path, clock, "rev-parse", "--git-dir")
        return (0, None) if g is not None and g.returncode == 0 else (None, None)
    try:
        commits = int(r.stdout.strip())
    except ValueError:
        return None, None
    last = None
    d = _git(path, clock, "log", "-1", "--format=%cI")
    if d is not None and d.returncode == 0:
        with contextlib.suppress(ValueError):
            last = parse(d.stdout.strip())
    return commits, last


def _first_sentence(text):
    lines, in_fence = [], False
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not s:
            if lines:
                break
            continue
        if s.startswith(("#", "![", "[![", "<", "|", "---", "===")):
            if lines:
                break
            continue
        lines.append(s)
    para = re.sub(r"\s+", " ", " ".join(lines))
    m = re.match(r"(.+?[.!?])(\s|$)", para)
    return (m.group(1) if m else para)[:200]


def _desc(path):
    for name in ("README.md", "CLAUDE.md"):
        fp = os.path.join(path, name)
        try:
            if not stat.S_ISREG(os.stat(fp).st_mode):
                continue
            with open(fp, "rb") as fh:
                text = fh.read(8192).decode("utf-8", "replace")
        except OSError:
            continue
        sentence = _first_sentence(text)
        if sentence:
            return sentence
    return ""


def _inspect(path, clock):
    """Facts about a readable admitted folder, or None when unreadable."""
    clock.check()
    try:
        mtime = os.stat(path).st_mtime
        entries = _list_children(path)
    except OSError:
        return None
    nondoc = 0
    for e in entries:
        with contextlib.suppress(OSError):
            if e.is_file(follow_symlinks=False) and not e.name.lower().endswith(DOC_EXTS):
                nondoc += 1
    commits, last = _git_info(path, clock)
    return {"mtime": mtime, "nondoc": nondoc, "commits": commits, "commit_at": last,
            "desc": _desc(path)}


# ---------- cache ----------

def _cached(led):
    """(scanned_at, projects) from the cache, or None when absent or malformed."""
    try:
        raw = json.loads(led.get_kv(CACHE_KEY) or "")
        projects = raw["projects"]
        if isinstance(projects, list) and all(isinstance(p, dict) for p in projects):
            return raw.get("scanned_at"), projects
    except (ValueError, TypeError, KeyError, AttributeError):
        pass
    return None


def load(led):
    """The cached project list. Never scans or touches the filesystem."""
    hit = _cached(led)
    return hit[1] if hit else []


# ---------- scan ----------

def _new(key, name, path, **kw):
    return {"key": key, "name": name, "path": path, "managed": False, "project": None,
            "work": False, "sessions": 0, "mtimes": [], "facts": None, "aliases": {key, path},
            "managed_info": None, **kw}


def _managed_info(led, name, now):
    items = led.items(name)
    cutoff = iso(now - timedelta(days=8))
    lo, hi = now - timedelta(days=7), now
    runs7 = 0
    for r in led.q("SELECT started_at FROM runs WHERE project=? AND started_at>=?", (name, cutoff)):
        t = parse(r["started_at"])
        if t and lo <= t <= hi:
            runs7 += 1
    changed = [parse(i["state_changed_at"]) for i in items if i["state_changed_at"]]
    return {"open": sum(i["state"] != "done" for i in items),
            "done": sum(i["state"] == "done" for i in items),
            "total": len(items), "runs7": runs7,
            "changed": max((c for c in changed if c), default=None)}


def _build(cfg, led, now, history, clock):
    policy = config.atlas_policy(cfg)
    ignore = policy["ignore"]
    recs, alias = {}, {}

    def register(rec):
        recs[rec["key"]] = rec
        for a in rec["aliases"]:
            alias[a] = rec["key"]

    # 1. managed projects (their paths are admitted)
    managed_roots = []
    worktree_roots = [_lex(config.WORKTREES)]
    for p in config.enabled_projects(cfg):
        clock.check()
        name = p["name"]
        path = _lex(p["path"]) if p.get("path") else ""
        usable = bool(path) and os.path.isdir(path)
        real = os.path.realpath(path) if usable else path
        rec = _new(real if usable else f"project:{name}", name, path, managed=True, project=name,
                   work=config.gh_account_of(p) == "work")
        rec["aliases"] = {a for a in (rec["key"], path, real) if a}
        rec["managed_info"] = _managed_info(led, name, now)
        if usable:
            rec["facts"] = _inspect(real, clock)
        register(rec)
        if path:
            managed_roots += [path, real]
        worktree_roots.append(_lex(p.get("worktree_root") or config.WORKTREES))
    barred = managed_roots + worktree_roots

    # 2. folder roots: direct children only
    roots = [_lex(r) for r in policy["scan_roots"]]
    roots = [r for r in roots if os.path.isdir(r)]
    admitted = list(dict.fromkeys(roots + [os.path.realpath(r) for r in roots]))
    for root in roots:
        try:
            children = _list_children(root)
        except OSError:
            continue
        for e in children:
            clock.check()
            if e.name.startswith("."):
                continue
            lexical = os.path.join(root, e.name)
            readable = True
            try:
                if e.is_symlink():
                    target = _lex(os.path.join(os.path.dirname(lexical), os.readlink(lexical)))
                    if _within(target, admitted):
                        real = os.path.realpath(lexical)
                        readable = _within(real, admitted)
                        if readable and not os.path.isdir(real):
                            continue
                    else:
                        readable = False
                    if not readable:
                        real = lexical
                elif e.is_dir(follow_symlinks=False):
                    real = os.path.realpath(lexical)
                else:
                    continue
            except OSError:
                continue
            if _within(lexical, barred) or _within(real, barred):
                continue
            if _user_ignored(lexical, ignore) or _user_ignored(real, ignore):
                continue
            if real in recs:
                recs[real]["aliases"].add(lexical)
                alias[lexical] = real
                continue
            rec = _new(real, os.path.basename(real), real)
            rec["aliases"] = {real, lexical}
            if readable:
                rec["facts"] = _inspect(real, clock)
            register(rec)

    # 3. session history: lexical decisions only outside the admitted set
    for cwd, mtime in _sessions(history, clock):
        key = alias.get(cwd)
        if key is None:
            if _within(cwd, barred):
                continue
            under = next((r for r in admitted if _under(cwd, r)), None)
            if under:
                if cwd == under:
                    continue
                child = os.path.join(under, cwd[len(under):].lstrip("/").split("/")[0])
                key = alias.get(child)
                if key is None:
                    if (os.path.basename(child).startswith(".")
                            or _user_ignored(child, ignore)):
                        continue
                    rec = _new(child, os.path.basename(child), child)
                    register(rec)
                    key = child
            else:
                if _builtin_excluded(cwd) or _user_ignored(cwd, ignore):
                    continue
                rec = _new(cwd, os.path.basename(cwd), cwd)
                register(rec)
                key = cwd
        recs[key]["sessions"] += 1
        recs[key]["mtimes"].append(mtime)
    return list(recs.values())


def _override(policy_phase, rec):
    paths = {a for a in rec["aliases"]}
    for k, v in policy_phase.items():
        if (k.startswith("~") or "/" in k) and _lex(k) in paths:
            return v
    for k, v in policy_phase.items():
        if not (k.startswith("~") or "/" in k) and k == rec["name"]:
            return v
    return None


def _finish(recs, cfg, prev, now):
    phases = config.atlas_policy(cfg)["phase"]
    out = []
    for r in recs:
        f, mi = r["facts"], r["managed_info"]
        if r["managed"]:
            phase = ("onboarded" if mi["total"] == 0
                     else "building" if mi["open"] or mi["runs7"] else "steady")
        else:
            phase = ("idea" if f is None or (f["commits"] is None and f["nondoc"] < 3)
                     else "prototype")
        phase = _override(phases, r) or phase
        stamps = [datetime.fromtimestamp(m, timezone.utc) for m in r["mtimes"]]
        if f:
            if f["commit_at"]:
                stamps.append(f["commit_at"])
            if f["commits"] is None:
                stamps.append(datetime.fromtimestamp(f["mtime"], timezone.utc))
        if mi and mi["changed"]:
            stamps.append(mi["changed"])
        seen = [prev[a] for a in r["aliases"] if a in prev and prev[a]]
        out.append({
            "key": r["key"], "name": r["name"], "managed": r["managed"], "project": r["project"],
            "work": r["work"], "path": r["path"], "phase": phase,
            "first_seen": min(seen) if seen else iso(now),
            "last_touched": iso(max(stamps)) if stamps else None,
            "sessions": r["sessions"], "commits": f["commits"] if f else None,
            "desc": f["desc"] if f else "",
            "open": mi["open"] if mi else None, "done": mi["done"] if mi else None,
            "runs7": mi["runs7"] if mi else None})
    out.sort(key=lambda p: (p["name"].casefold(), p["key"]))
    return out


def scan(cfg, led, now, *, history=None, budget=None):
    """Scan now, cache `{scanned_at, projects}`, return the project list.

    Raises ScanError on timeout (previous cache kept); nothing partial is written.
    """
    budget = BUDGET if budget is None else budget
    clock = _Clock(budget)
    hit = _cached(led)
    prev = {}
    for p in hit[1] if hit else []:
        if p.get("key") and p.get("first_seen"):
            prev[p["key"]] = p["first_seen"]
            if p.get("path"):
                prev.setdefault(p["path"], p["first_seen"])
    with _deadline(budget):
        recs = _build(cfg, led, now, history or _history_dir(), clock)
        projects = _finish(recs, cfg, prev, now)
    led.set_kv(CACHE_KEY, json.dumps({"scanned_at": iso(now), "projects": projects}))
    return projects


def maybe_scan(ctx):
    """Tick hook: at most one attempt per INTERVAL, success or failure."""
    if ctx.dry_run:
        return
    now = ctx.led.now()
    last = parse(ctx.led.get_kv(ATTEMPT_KEY))
    if last and 0 <= (now - last).total_seconds() < INTERVAL:
        return
    ctx.led.set_kv(ATTEMPT_KEY, iso(now))
    try:
        scan(ctx.cfg, ctx.led, now)
    except Exception as e:
        ctx.say(f"atlas: scan failed — {e}")


def format_table(projects):
    rows = [("NAME", "FLAGS", "PHASE", "SESS", "COMMITS", "LAST TOUCHED", "PATH")]
    for p in projects:
        rows.append((p["name"], ("M" if p["managed"] else "-") + ("W" if p["work"] else "-"),
                     p["phase"], str(p["sessions"]),
                     "-" if p["commits"] is None else str(p["commits"]),
                     (p["last_touched"] or "-")[:10], p["path"] or "-"))
    w = [max(len(r[i]) for r in rows) for i in range(6)]
    return "\n".join("  ".join(c.ljust(w[i]) if i < 6 else c for i, c in enumerate(r)).rstrip()
                     for r in rows)
