"""Repo lesson candidates: scanned before launch, advisory only (DESIGN D44).

Lessons live in the managed repo at `.agents/skills/lesson-*/SKILL.md`. Native
loaders read the worktree directly, so the scanner is the trust boundary: it
reads each lesson from the *base ref*, and every `lesson-*` directory in the
prepared worktree must be byte-identical to its scan-passed base version or it
is suppressed (removed, with skip-worktree so the removal is never staged).
Everything is scoped to one worktree/project (D25): nothing here is global.
"""

import hashlib
import os
import re
import shutil
import subprocess

from . import redact

SKILLS_DIR = ".agents/skills"
PREFIX = "lesson-"
MAX_BYTES = 8 * 1024
MAX_DESC = 1024
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NAME_RE = re.compile(r"^lesson-[a-z0-9-]{1,57}$")

NATIVE, CATALOG, UNSUPPORTED = "native", "catalog", "unsupported"
# D44's compatibility table. A kind not listed gets the catalog fallback.
KIND_MODES = {"codex": NATIVE, "copilot": NATIVE, "agy": NATIVE, "kilo": NATIVE,
              "cline": NATIVE, "vibe": NATIVE, "claude": CATALOG,
              "agent-sdk": CATALOG, "kiro": CATALOG}


def mode_for(kind):
    return KIND_MODES.get(kind, CATALOG)


# (rule id, pattern). Matched against the whole lesson text.
_RULES = (
    ("injection", re.compile(
        r"(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(previous|prior|above|earlier|system|all)\b"
        r"[^.\n]{0,30}\b(instruction|prompt|rule|message)s?"
        r"|(reveal|print|show|repeat)\b[^.\n]{0,30}\b(system|hidden)\s+(prompt|instruction)"
        r"|you\s+are\s+now\b|\bnew\s+instructions\s*:"
        r"|(grant|enable|bypass|disable)\b[^.\n]{0,30}\b(permission|approval|sandbox|safety)",
        re.IGNORECASE)),
    ("hidden-unicode", re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿]")),
    ("exfiltration", re.compile(
        r"https?://|\b(curl|wget|nc|ncat|netcat|scp|rsync|ftp|sftp|telnet|socat|sendmail)\b|webhook"
        r"|\b(printenv|env)\b\s*\||/dev/(tcp|udp)/"
        r"|\b(socket|urllib\d?|requests|httpx|aiohttp|smtplib|ftplib|paramiko|http\.client"
        r"|create_connection|urlopen|XMLHttpRequest|fetch)\b\s*(?:\(|\.\w)"
        r"|\bimport\s+(socket|urllib|requests|httpx|smtplib|ftplib)\b"
        r"|\b(?:\d{1,3}\.){3}\d{1,3}\b"
        r"|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|io|dev|ai|co|xyz|app|cloud|info|biz|ru|cn|tk|ly|me|us|uk|de)\b"
        r"|\b(send|upload|post|transmit|exfiltrate|email|mail)\b[^.\n]{0,60}\b(token|secret|credential|password|key|env(ironment)?|log)s?\b",
        re.IGNORECASE)),
    ("destructive", re.compile(
        r"\brm\s+-[a-z]*[rf][a-z]*\b|git\s+push\b[^\n]*(--force|\s-f\b)|git\s+reset\s+--hard"
        r"|\bsudo\b|\bDROP\s+(TABLE|DATABASE)\b|\bmkfs\b|\bdd\s+if=|>\s*/dev/(sd|disk)"
        r"|\bsecurity\s+(add|delete)-\S+|\.ssh/|keychain", re.IGNORECASE)),
)


def _frontmatter(text):
    """-> (dict of top-level scalar keys, body) or raise ValueError."""
    if not text.startswith("---\n"):
        raise ValueError("missing frontmatter")
    head, sep, body = text[4:].partition("\n---")
    if not sep:
        raise ValueError("unterminated frontmatter")
    fm, meta, in_meta = {}, {}, False
    for line in head.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        if line.startswith((" ", "\t")):
            if in_meta:
                k, colon, v = line.strip().partition(":")
                if colon:
                    meta[k.strip()] = v.strip().strip("\"'")
            continue
        key, colon, val = line.partition(":")
        if not colon:
            raise ValueError("malformed frontmatter line")
        in_meta = key.strip() == "metadata"
        fm[key.strip()] = val.strip().strip("\"'")
    fm["metadata"] = meta
    return fm, body


def scan(dirname, files, project):
    """Check one lesson given as {relative filename: bytes}.
    -> (info or None, findings). `findings` is a list of (rule, redacted
    evidence); empty means safe. Never raises."""
    try:
        return _scan(dirname, files, project)
    except Exception as e:          # fail closed (D44)
        return None, [("scanner-error", redact.redact(str(e))[:120])]


def _scan(dirname, files, project):
    out = []
    if set(files) != {"SKILL.md"}:
        extra = sorted(set(files) - {"SKILL.md"})
        out.append(("schema", "unexpected files: " + ", ".join(extra)[:100]
                    if extra else "missing SKILL.md"))
        if "SKILL.md" not in files:
            return None, out
    raw = files["SKILL.md"]
    if len(raw) > MAX_BYTES:
        out.append(("schema", f"SKILL.md is {len(raw)} bytes (max {MAX_BYTES})"))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, out + [("schema", "not UTF-8")]
    info = None
    try:
        fm, _ = _frontmatter(text)
        name, desc = fm.get("name", ""), fm.get("description", "")
        if not NAME_RE.match(dirname) or name != dirname:
            out.append(("schema", f"name {name!r} must equal directory {dirname!r} "
                                  f"and match lesson-[a-z0-9-]"))
        if not desc or len(desc) > MAX_DESC:
            out.append(("schema", "description missing or over 1024 chars"))
        meta = fm.get("metadata", {})
        missing = [k for k in ("mahler-id", "created", "updated", "source")
                   if not meta.get(k)]
        if missing:
            out.append(("schema", "metadata missing: " + ", ".join(missing)))
        # Preserve sentence punctuation; normalize case and whitespace only.
        trigger = re.split(r"(?<=[.!?])\s+", " ".join(desc.lower().split()), maxsplit=1)[0]
        expected_id = hashlib.sha256((project + "\n" + trigger).encode("utf-8")).hexdigest()[:16]
        if meta.get("mahler-id") and meta["mahler-id"] != expected_id:
            out.append(("schema", "metadata.mahler-id does not match project and description trigger"))
        for k in ("created", "updated"):
            if meta.get(k) and not DATE_RE.match(meta[k]):
                out.append(("schema", f"metadata.{k} must be an ISO date"))
        if not out:
            info = {"name": name, "description": desc}
    except ValueError as e:
        out.append(("schema", str(e)))
    for rule, pat in _RULES:
        m = pat.search(text)
        if m:
            out.append((rule, redact.redact(repr(m.group(0)[:60]))))
    secret = redact.redact(text)
    if secret != text:
        out.append(("secret", "credential-shaped text"))
    return (None if out else info), out


def _git_bytes(repo, *args):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed")
    return r.stdout


def read_base(repo, ref):
    """Every lesson-* directory at `ref` -> {name: {filename: bytes}}; a
    symlink or nested path becomes an unreadable marker so the scan fails it.
    Reads only the committed tree of this project's repo."""
    listing = _git_bytes(repo, "ls-tree", "-r", "-z", ref, "--", SKILLS_DIR)
    lessons = {}
    for entry in filter(None, listing.split(b"\0")):
        meta, _, path = entry.partition(b"\t")
        mode, _, sha = meta.split()[:3]
        parts = path.decode("utf-8", "replace").split("/")
        if len(parts) < 4 or not parts[2].startswith(PREFIX):
            continue
        name, rel = parts[2], "/".join(parts[3:])
        files = lessons.setdefault(name, {})
        files[rel] = (b"" if mode == b"120000" or mode == b"160000" else
                      _git_bytes(repo, "cat-file", "blob", sha.decode()))
        if mode in (b"120000", b"160000"):
            files[rel + " (link)"] = b""     # extra file name: schema failure
    return lessons


def read_worktree(wt):
    """lesson-* directories present in the worktree -> {name: {rel: bytes}},
    refusing to follow symlinks or leave `wt`."""
    root = os.path.join(wt, SKILLS_DIR)
    out = {}
    if not os.path.isdir(root) or os.path.islink(root):
        return out
    for name in sorted(os.listdir(root)):
        if not name.startswith(PREFIX):
            continue
        d = os.path.join(root, name)
        files = out.setdefault(name, {})
        if os.path.islink(d) or not os.path.isdir(d):
            files["(not a directory)"] = b""
            continue
        for dp, dns, fns in os.walk(d):
            for n in dns + fns:
                p = os.path.join(dp, n)
                rel = os.path.relpath(p, d)
                if os.path.islink(p):
                    files[rel + " (link)"] = b""
            for n in fns:
                p = os.path.join(dp, n)
                if not os.path.islink(p):
                    with open(p, "rb") as fh:
                        files[os.path.relpath(p, d)] = fh.read()
    return out


def digest(files):
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(rel.encode() + b"\0" + files[rel] + b"\0")
    return h.hexdigest()


def _suppress(repo_wt, name):
    d = os.path.join(repo_wt, SKILLS_DIR, name)
    tracked = subprocess.run(["git", "-C", repo_wt, "ls-files", "-z", "--", d],
                             capture_output=True, timeout=60)
    paths = [p for p in tracked.stdout.split(b"\0") if p]
    if paths:
        r = subprocess.run(["git", "-C", repo_wt, "update-index", "--skip-worktree",
                            "-z", "--stdin"], input=b"\0".join(paths) + b"\0",
                           capture_output=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError("skip-worktree failed")
    if os.path.islink(d) or os.path.isfile(d):
        os.remove(d)
    else:
        shutil.rmtree(d)
    if os.path.lexists(d):
        raise RuntimeError("suppression left the directory")


def _suppress_roots(wt):
    """A symlinked `.agents` or `.agents/skills` would let native discovery
    escape the scan: untrack-hide and remove the link itself."""
    for rel in (".agents", SKILLS_DIR):
        p = os.path.join(wt, rel)
        if not os.path.islink(p):
            continue
        r = subprocess.run(["git", "-C", wt, "ls-files", "-z", "--", rel],
                           capture_output=True, timeout=60)
        paths = [x for x in r.stdout.split(b"\0") if x]
        if paths:
            u = subprocess.run(["git", "-C", wt, "update-index", "--skip-worktree",
                                "-z", "--stdin"], input=b"\0".join(paths) + b"\0",
                               capture_output=True, timeout=60)
            if u.returncode != 0:
                raise RuntimeError("skip-worktree failed")
        os.remove(p)
        if os.path.lexists(p):
            raise RuntimeError("could not remove symlinked skills root")
        return True
    return False


def load(repo, wt, ref, project):
    """Prepare lessons for a launch in worktree `wt`. Never raises.

    -> {"safe": [{name, description, path, hash}], "rejected":
    [{name, rule, evidence}], "diagnostics": [str]}. Rejected content is
    never returned, only redacted evidence."""
    res = {"safe": [], "rejected": [], "diagnostics": []}
    try:
        base = {n: (scan(n, f, project), digest(f)) for n, f in read_base(repo, ref).items()}
        if _suppress_roots(wt):
            res["rejected"].append({"name": SKILLS_DIR, "rule": "symlink",
                                    "evidence": "skills root is a symlink; removed"})
        present = read_worktree(wt)
    except Exception as e:
        res["diagnostics"].append(
            "lesson scanner could not run; no lessons loaded: "
            + redact.redact(str(e))[:120])
        _remove_all(wt, res)
        return res
    for name, files in present.items():
        entry = base.get(name)
        verdict = None
        if entry is None:
            verdict = ("branch-local", "not in base; takes effect after merge")
        else:
            (info, findings), base_hash = entry
            if findings:
                verdict = findings[0]
                res["rejected"] += [{"name": name, "rule": r, "evidence": e}
                                    for r, e in findings[1:]]
            elif digest(files) != base_hash:
                verdict = ("branch-local", "differs from scan-passed base")
        if verdict:
            res["rejected"].append({"name": name, "rule": verdict[0],
                                    "evidence": verdict[1]})
            try:
                _suppress(wt, name)
            except Exception as e:
                res["diagnostics"].append(
                    "suppressing a lesson failed; removing every lesson: "
                    + redact.redact(str(e))[:120])
                res["safe"] = []
                _remove_all(wt, res)
                return res
        else:
            info = entry[0][0]
            res["safe"].append({"name": name, "description": info["description"],
                                "path": f"{SKILLS_DIR}/{name}/SKILL.md",
                                "hash": entry[1]})
    for r in res["rejected"]:
        res["diagnostics"].append(
            f"lesson {r['name']} unavailable [{r['rule']}]: {r['evidence']}")
    return res


def _remove_all(wt, res):
    res["safe"] = []
    try:
        for name in read_worktree(wt):
            _suppress(wt, name)
    except Exception as e:
        res["diagnostics"].append("could not remove lessons: " + redact.redact(str(e))[:120])
        res["fatal"] = True


def catalog(kind, lessons):
    """The prompt block for a platform kind: only catalog-mode kinds get one
    (native kinds discover the same lessons themselves; unsupported get none)."""
    safe = (lessons or {}).get("safe") or []
    if mode_for(kind) != CATALOG or not safe:
        return ""
    lines = ["", "Repo lessons (advisory candidates, scanned by Mahler; read the file "
              "when its description matches your situation):"]
    for l in safe:
        desc = " ".join(l["description"].split())[:300]
        lines.append(f"- `{l['name']}` — {desc} (`{l['path']}`)")
    return "\n".join(lines) + "\n"


# ---- curation (D44 clocks; D20 `lessons` pass) ------------------------------
ARCHIVE_DIR = ".agents/skills-archive"
STALE_DAYS, ARCHIVE_DAYS = 14, 30
PROMOTE_EVENTS, PROMOTE_ISSUES, PROMOTE_WINDOW_DAYS = 3, 2, 90
ACTIVE, STALE, ARCHIVE_ELIGIBLE = "active", "stale", "archive-eligible"


def _day(s):
    from datetime import date
    return date.fromisoformat(str(s)[:10])


def last_used(created, updated, cited=None):
    """D44: max(created, updated, latest cited run). A lesson never cited is
    aged from created/updated; exposure alone never refreshes it."""
    return max(_day(d) for d in (created, updated, cited) if d)


def classify(created, updated, cited, today):
    """-> (state, unused_days) in UTC calendar days; stale at >=14, archive-
    eligible at >=30."""
    days = (_day(today) - last_used(created, updated, cited)).days
    state = (ARCHIVE_ELIGIBLE if days >= ARCHIVE_DAYS else
             STALE if days >= STALE_DAYS else ACTIVE)
    return state, max(days, 0)


def durable_key(project, mahler_id):
    return f"lesson-gate:{project}:{mahler_id}"


def _new_events(evs, prior):
    """Events not counted when the proposal closed. Identity-based when the
    prior records `closed_events` (refs); aged-out events can't mask new ones.
    Without refs, count current events as new because a historical count cannot
    identify which current events were already counted."""
    if prior.get("closed_events") is not None:
        return len(set(evs) - set(prior["closed_events"]))
    return len(evs)


def gate_proposals(project, events, today, existing=None, gated=()):
    """Lessons meeting D44's promotion threshold.

    `events`: [{"id": mahler-id, "event": distinct event ref, "issue": n,
    "date": ISO}]. Counts distinct events in the trailing 90 days; needs >=3
    events across >=2 distinct issues. `existing`: {key: {"open": bool,
    "closed_event_count": events counted when it closed,
    optional "closed_events": their refs, preferred}}; an open proposal is
    reused, a closed one re-files only after 3 new events. Without refs, current
    events are treated as new because the historical count cannot identify
    them. `gated`: mahler-ids
    that already name an adequate shipped gate -> never proposed.
    -> [{"key", "id", "events", "issues", "reuse": bool}] sorted by key."""
    today, existing, per = _day(today), existing or {}, {}
    for e in events or ():
        if (today - _day(e["date"])).days > PROMOTE_WINDOW_DAYS or _day(e["date"]) > today:
            continue
        per.setdefault(e["id"], {})[e["event"]] = e["issue"]
    out = []
    for mid in sorted(per):
        evs = per[mid]
        key = durable_key(project, mid)
        if mid in gated or len(evs) < PROMOTE_EVENTS or len(set(evs.values())) < PROMOTE_ISSUES:
            continue
        prior = existing.get(key)
        if prior and prior.get("open"):
            out.append({"key": key, "id": mid, "events": sorted(evs),
                        "issues": sorted(set(evs.values())), "reuse": True})
        elif prior and _new_events(evs, prior) < PROMOTE_EVENTS:
            continue
        else:
            out.append({"key": key, "id": mid, "events": sorted(evs),
                        "issues": sorted(set(evs.values())), "reuse": False})
    return out


def inventory(project, lessons, stats, today, limit=50):
    """Bounded per-project inventory from validated lessons plus ledger usage.
    `lessons`: {dirname: files} (read_base output); `stats`: ledger
    lesson_usage_stats(project, account). Unsafe lessons are skipped."""
    rows = []
    for name in sorted(lessons):
        info, findings = scan(name, lessons[name], project)
        if findings or not info:
            continue
        meta = _frontmatter(lessons[name]["SKILL.md"].decode("utf-8"))[0]["metadata"]
        cited = (stats.get(name) or {}).get("last_used")
        state, days = classify(meta["created"], meta["updated"], cited, today)
        rows.append({"name": name, "mahler_id": meta["mahler-id"], "state": state,
                     "unused_days": days, "cited_runs": (stats.get(name) or {}).get("count", 0),
                     "gate_key": durable_key(project, meta["mahler-id"]),
                     "path": f"{SKILLS_DIR}/{name}"})
    return rows[:limit]
