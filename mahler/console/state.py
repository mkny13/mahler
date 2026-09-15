"""The console's state: one plain-data snapshot that both layouts render (D27).

Everything the operator console shows is derived here from the ledger and
config, including the plain-English sentences (idle reasons, banners, the
capacity line). The page module only lays it out, so the phone and desktop
layouts can never disagree, and tests can check the copy without parsing HTML.
Copy comes from docs/console/design.md, which is final; sentences the design
did not cover are listed at the end of that file.
"""

import json
import re
from datetime import timedelta
from zoneinfo import ZoneInfo

from .. import config, presence, router
from ..ledger import parse, row_get

EVENTS_SHOWN = 50
DIGEST_SHOWN = 20
SEEN_KEY = "console_seen_event"        # kv: the newest event id marked seen
DIGEST_HOURS = 24                      # older unseen events stop counting as new

# bookkeeping the event stream leaves out: every lease and heartbeat, stats
# rows, and the console's own read marker
QUIET_KINDS = ("lease", "release", "issue_done_stats", "estimate_calibration",
               "console_seen")
# state transitions worth a row; the rest repeat a run_start, pr_opened or
# shipped event logged in the same tick
STREAM_STATES = ("ready", "inbox", "needs_you", "failed", "parked", "parent")
ATTENTION_STATES = ("needs_you", "failed")
# needs-you severity (full ink, and a push); everything else is FYI
ATTENTION_KINDS = ("launch_failed", "backup_failed")

STATE_ORDER = ("needs_you", "failed", "working", "verifying", "ready", "inbox",
               "parked", "parent")
ROLE_WORDS = {"build": "building", "sort": "sorting", "fix": "fixing CI"}
TZ_LABELS = {"America/Los_Angeles": "PT", "America/New_York": "ET",
             "America/Chicago": "CT", "America/Denver": "MT"}
NUMBER_WORDS = ("No", "One", "Two", "Three", "Four", "Five", "Six", "Seven",
                "Eight", "Nine")
DESIGN_D8 = ("https://github.com/mkny13/mahler/blob/main/DESIGN.md"
             "#d8--platforms-quota-sensing-and-routing")

_STATE_DETAIL = re.compile(r"^(\w+) -> (\w+)(?: \((.*)\))?$", re.S)


def build(cfg, led):
    """The whole console as plain data (see docs/console/design.md, "Shared
    state model"). Client-only state — tab, theme, expanded groups — is not
    here; the page keeps that in the browser."""
    now = led.now()
    projects = config.enabled_projects(cfg)
    peak = _peak(cfg, led)
    quota = _quota(cfg, led, peak)
    runs = _runs(cfg, led, now)
    needs = _needs(cfg, led, projects, now)
    backlog = _backlog(cfg, led, projects)
    events = _events(cfg, led)
    digest = _digest(cfg, led, now)
    paused = led.paused()
    hot = _hot_holds(led, projects, now)
    s = {
        "paused": paused,
        "system": ({"label": "PAUSED", "tone": "warn"} if paused else
                   {"label": f"RUNNING · {len(runs)}", "tone": "acc"} if runs else
                   {"label": "IDLE", "tone": "mut"}),
        "peak": peak,
        "runs": runs,
        "needs": needs,
        "uat": [],              # the Ready-to-test source is its own change (D27)
        "backlog": backlog,
        "backlog_total": sum(len(g["items"]) for g in backlog),
        "quota": quota,
        "capacity": _capacity_line(quota),
        "events": events,
        "digest": digest,
        "banners": _banners(cfg, led, paused, quota, hot, now),
        "projects": [p["name"] for p in projects],
    }
    s["idle"] = None if runs else _idle(cfg, led, s, hot, now)
    s["landing"] = {
        "tab": "triage" if needs or s["uat"] else "now",
        "view": "needs" if needs else "test" if s["uat"] else "now",
    }
    return s


# ---------- formatting ----------

def _mins(delta):
    return max(int(delta.total_seconds() // 60), 0)


def _dur(delta):
    return router.fmt_countdown(delta)


def _hhmm(dt):
    return dt.astimezone().strftime("%H:%M")


def _when(dt, now):
    """A reset time as the design writes it: 13:40 today, Mon 00:00 this
    week, Oct 1 further out."""
    local = dt.astimezone()
    ahead = dt - now
    if ahead < timedelta(hours=20):
        return local.strftime("%H:%M")
    if ahead < timedelta(days=6):
        return local.strftime("%a %H:%M")
    return f"{local:%b} {local.day}"


def _ref(project, number):
    return f"{project}#{number}"


def _issue_url(cfg, project, number):
    repo = config.project_policy(cfg, project).get("repo")
    return f"https://github.com/{repo}/issues/{number}" if repo else None


def _pr_url(cfg, project, pr):
    repo = config.project_policy(cfg, project).get("repo")
    return f"https://github.com/{repo}/pull/{pr}" if repo and pr else None


def _join(names):
    names = list(names)
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _state_parts(detail):
    """'working -> needs_you (the question)' -> ('working', 'needs_you', 'the question')."""
    m = _STATE_DETAIL.match(detail or "")
    return m.groups() if m else (None, None, None)


# ---------- peak hours (D22) ----------

def _peak(cfg, led):
    pc = cfg.get("claude_peak") or {}
    if not pc.get("enabled", True):
        return None
    now = led.now()
    tzname = pc.get("tz", "America/Los_Angeles")
    tz = ZoneInfo(tzname)
    label = TZ_LABELS.get(tzname) or now.astimezone(tz).tzname()
    active, until = router.peak_state(cfg, led)
    overridden = router.peak_overridden(led)
    window = f"{pc.get('start', '05:00')}–{pc.get('end', '11:00')} {label}"
    until_s = until.astimezone(tz).strftime("%H:%M") if until else None
    return {
        "active": active,
        "overridden": overridden,
        "until": until_s,
        "tz": label,
        "left": _dur(until - now) + " left" if until else None,
        "line": ("Peak hours overridden — Claude may build until you switch back"
                 if overridden else
                 f"Claude peak hours {window} — planning only, free tiers build"),
        "short": ("Peak hours overridden" if overridden else
                  f"Peak hours until {until_s} · Claude plans only" if active else None),
        "action": "Restore" if overridden else "Override",
        # the desktop header button only means something inside the window or
        # while an override is live
        "header": active or overridden,
    }


# ---------- quota (D8) ----------

def _routed(cfg, roles=("sort", "plan", "build")):
    """Enabled platforms any route can pick for `roles`, in first-mention order."""
    tables = [cfg.get("routing") or {}]
    tables += [(a or {}).get("routing") or {} for a in cfg.get("accounts", {}).values()]
    tables += [(p or {}).get("routing") or {} for p in cfg.get("projects", {}).values()]
    names = []
    for table in tables:
        for role in roles:
            for n in table.get(role) or []:
                pc = cfg["platforms"].get(n)
                if pc and pc.get("enabled", True) and n not in names:
                    names.append(n)
    return names


def _model_line(pconf):
    """'claude-opus-4-6-thinking', 'free tier · size s only': the model, the
    plan it runs on (`plan` in the platform's config), and any size limit."""
    model = pconf.get("model") or pconf.get("build_model") or pconf.get("sort_model") or ""
    kind = pconf.get("kind", "")
    if kind and model.startswith(kind + "/"):
        model = model[len(kind) + 1:]
    account = config.account_of(pconf)
    plan = pconf.get("plan") or (f"{account} account" if account != config.DEFAULT_ACCOUNT else "")
    parts = [p for p in (model, plan) if p] or ["default model"]
    if pconf.get("max_size") == "s":
        parts.append("size s only")
    return " · ".join(parts)


def _quota(cfg, led, peak):
    """One gauge per quota group (platforms sharing a login and quota share a
    gauge, D21): the worst window fills the bar, the soft line is the tick."""
    now = led.now()
    burst = router.burst_status(cfg, led)
    builders = set(_routed(cfg, ("build",)))
    groups, rows = {}, []
    for name in _routed(cfg):
        pconf = cfg["platforms"][name]
        group = pconf.get("quota_group", name)
        if group in groups:
            groups[group]["members"].append(name)
            continue
        groups[group] = {"members": [name]}
        rows.append((group, name))
    out = []
    for group, first in rows:
        members = groups[group]["members"]
        name = group if group in members else first
        pconf = cfg["platforms"][name]
        claude = pconf.get("kind") == "claude"
        state, _ = router.usage_state(led, name, pconf,
                                      burst_lines=burst if claude else None)
        usage = led.usage(name)
        hold = usage.pop(router.HOLD, None)
        hold_until = router._ts(hold.get("resets_at")) if hold else None
        metered = pconf.get("metered", True)
        windows = []
        for w in pconf.get("windows", router.WINDOWS):
            u = usage.get(w)
            if u is None:
                continue
            soft = pconf.get("soft", {}).get(w, 100)
            if claude and burst and w in burst:
                soft = burst[w][0]
            windows.append({"window": w, "pct": max(min(u["used_pct"], 100), 0),
                            "soft": soft, "resets": router._ts(u.get("resets_at"))})
        worst = max(windows, key=lambda x: x["pct"], default=None)
        row = {"name": name, "members": members, "model": _model_line(pconf),
               "state": state, "metered": metered, "claude": claude,
               "builds": any(m in builders for m in members),
               "until": None, "over": [], "soft_pct": 100}
        if state == "soft" and hold_until and hold_until > now:
            row.update(state="hold", until=hold_until, label="hold", tone="warn",
                       width=100, detail=f"on hold until {_hhmm(hold_until)} — a run never started")
        elif state == "hard" and not metered:
            until = max((x["resets"] for x in windows if x["resets"]), default=None)
            row.update(state="backoff", until=until, label="off", tone="bad", width=100,
                       detail=(f"quota error — backing off until {_hhmm(until)}"
                               if until else "quota error — backing off"))
        elif state == "stale":
            row.update(label="stale", tone="mut", width=worst["pct"] if worst else 0,
                       detail="no fresh reading — counted as over the line")
        elif not metered:
            row.update(label="unmetered", tone="acc", width=0,
                       detail="unmetered — no quota signal until an error")
        else:
            parts = [f"{x['window']} {x['pct']:.0f}%" for x in windows]
            if group == "copilot" or pconf.get("monthly_cap_credits"):
                cap = pconf.get("monthly_cap_credits")
                parts = [f"{x['window']} {x['pct']:.0f}%" + (f" of {cap} AI credits" if cap else "")
                         for x in windows]
            over = [x for x in windows if x["pct"] >= x["soft"]]
            if over:
                parts.append(f"soft line {over[0]['soft']:.0f}%")
            elif worst and worst["resets"] and worst["resets"] > now:
                parts.append(f"resets {_when(worst['resets'], now)}")
            row.update(label=f"{worst['pct']:.0f}%" if worst else "—",
                       tone="bad" if state == "hard" else "warn" if state == "soft" else "acc",
                       width=100 if state == "hard" else (worst["pct"] if worst else 0),
                       detail=" · ".join(parts), over=over)
        if worst:
            row["soft_pct"] = worst["soft"]
        # held by the peak window even when its quota is fine (D22)
        row["peak_held"] = bool(claude and peak and peak["active"])
        row["available"] = row["state"] == "ok" and not row["peak_held"]
        out.append(row)
    return out


def _capacity_line(quota):
    avail = [q["name"] for q in quota if q["available"]]
    held = []
    for q in quota:
        if q["available"]:
            continue
        if q["peak_held"] and q["state"] == "ok":
            held.append(f"{q['name']} is held by peak hours")
        elif q["state"] == "backoff":
            held.append(f"{q['name']} is backing off"
                        + (f" until {_hhmm(q['until'])}" if q["until"] else ""))
        elif q["state"] == "hold":
            held.append(f"{q['name']} is on hold until {_hhmm(q['until'])}")
        elif q["state"] == "stale":
            held.append(f"{q['name']} has no fresh quota reading")
        else:
            held.append(f"{q['name']} is past its soft line")
    head = f"{len(avail)} of {len(quota)} platforms available"
    line = head + (" — " + ", ".join(avail) + "." if avail else ".")
    if held:
        line += " " + "; ".join(held) + "."
    return line


# ---------- runs ----------

def _runs(cfg, led, now):
    ests = led.estimates()
    out = []
    for r in sorted(led.active_runs(), key=lambda r: r["started_at"] or ""):
        project, n = r["project"], r["number"]
        item = led.item(project, n)
        est = row_get(r, "est_mins") or led.run_estimate(ests, r["platform"], r["role"],
                                                         row_get(r, "size"))
        est = max(int(round(est)), 1)
        started = parse(r["started_at"])
        mins = _mins(now - started) if started else 0
        over = mins > est
        lease = led.lease(project, n)
        held = bool(lease and lease["holder"] == f"run:{r['id']}")
        wt = (r["worktree"] or "").rstrip("/").rsplit("/", 1)[-1]
        meta = [r["platform"], f"run {r['id']}"]
        if wt:
            meta.append(f"worktree {wt}")
        meta += [f"epoch {r['epoch']}", "lease held" if held else "no lease"]
        status = (f"stopping · {r['stop_reason']}" if r["status"] == "stopping" and r["stop_reason"]
                  else "stopping" if r["status"] == "stopping"
                  else ROLE_WORDS.get(r["role"], r["role"]))
        out.append({
            "id": r["id"], "ref": _ref(project, n), "url": _issue_url(cfg, project, n),
            "title": (item["title"] if item else None) or _ref(project, n),
            "platform": r["platform"], "role": r["role"],
            "mins": mins, "est": est,
            "progress": min(100, round(mins / est * 100)),
            "tone": "warn" if over else "acc",
            "timing": f"{mins}m · {mins - est}m past estimate" if over else f"{mins}m of ~{est}m",
            "status": status, "meta": " · ".join(meta),
        })
    return out


# ---------- needs you ----------

def _question(led, project, number, state):
    """The question an item is waiting on: the reason on its latest move into
    needs_you/failed (finalize writes the agent's NEEDS-YOU line there)."""
    for e in led.q("SELECT detail FROM events WHERE project=? AND number=? AND kind='state'"
                   " ORDER BY id DESC LIMIT 20", (project, number)):
        _, to, why = _state_parts(e["detail"])
        if to == state:
            return why
    return None


def _needs(cfg, led, projects, now):
    names = {p["name"] for p in projects}
    out = []
    for it in led.items(states=ATTENTION_STATES):
        if it["project"] not in names:
            continue
        project, n = it["project"], it["number"]
        since = parse(it["state_changed_at"])
        waited = now - since if since else timedelta(0)
        meta = [f"waiting {_dur(waited)}"]
        if it["attempts"] and it["attempts"] >= 2:
            meta.append(f"{it['attempts']} attempts")
        out.append({
            "id": _ref(project, n), "project": project, "number": n,
            "ref": _ref(project, n), "url": _issue_url(cfg, project, n),
            "title": it["title"] or "",
            "question": _question(led, project, n, it["state"]) or it["title"] or "",
            "options": [],       # structured answer options are their own change (D27)
            "p": f"p{it['priority']}", "p1": it["priority"] == 1,
            "state": it["state"], "waited_s": waited.total_seconds(),
            "meta": " · ".join(meta),
        })
    out.sort(key=lambda x: (not x["p1"], -x["waited_s"]))
    return out


# ---------- backlog ----------

def _state_tone(state):
    return ("bad" if state in ATTENTION_STATES else
            "acc" if state in ("working", "verifying") else "mut")


def _backlog(cfg, led, projects):
    rank = {s: i for i, s in enumerate(STATE_ORDER)}
    out = []
    for p in projects:
        name = p["name"]
        items = [i for i in led.items(name) if i["state"] != "done"]
        items.sort(key=lambda i: (rank.get(i["state"], len(rank)), i["priority"], i["number"]))
        ready = sum(1 for i in items if i["state"] in ("ready", "inbox"))
        live = sum(1 for i in items if i["state"] in ("working", "verifying"))
        you = sum(1 for i in items if i["state"] in ATTENTION_STATES)
        out.append({
            "project": name,
            "counts": f"{len(items)} · {ready} ready · {live} live" + (f" · {you} you" if you else ""),
            "items": [{"ref": _ref(name, i["number"]), "url": _issue_url(cfg, name, i["number"]),
                       "title": i["title"] or "", "p": f"p{i['priority']}",
                       "p1": i["priority"] == 1, "state": i["state"].replace("_", "-"),
                       "tone": _state_tone(i["state"])} for i in items],
        })
    return out


# ---------- events ----------

def _detail_json(detail):
    try:
        d = json.loads(detail or "")
    except (TypeError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _describe(cfg, e):
    """-> (kind, text, attention, undoable) for one event row, or None to skip."""
    kind, detail = e["kind"], e["detail"] or ""
    ref = _ref(e["project"], e["number"]) if e["project"] and e["number"] else (e["project"] or "")
    d = _detail_json(detail)
    if kind == "state":
        frm, to, why = _state_parts(detail)
        if to not in STREAM_STATES:
            return None
        if to == "ready" and why and why.startswith("handoff"):
            return "handoff", f"{ref} {why}".strip(), False, False
        text = f"{ref} {why}" if why else f"{ref} {frm} → {to}"
        return to, text.strip(), to in ATTENTION_STATES, False
    if kind == "run_start" and d:
        return ("run_started",
                f"{ref} {d.get('role', 'run')} on {d.get('platform', '?')}, run {d.get('run', '?')}",
                False, False)
    if kind == "shipped" and d:
        return "pr_merged", f"{ref} squash-merged #{d.get('pr', '?')}", False, True
    if kind == "pr_opened" and d:
        return "pr_opened", f"{ref} opened PR #{d.get('pr', '?')}", False, False
    if kind == "escalated" and d:
        return ("escalated",
                f"{ref} retried a tier up ({d.get('tier_from', '?')} → {d.get('tier_to', '?')})",
                False, False)
    if kind == "backoff_cleared" and d:
        return kind, f"cleared by hand on {', '.join(d.get('platforms') or d.get('asked') or [])}", False, False
    text = detail if not d else ", ".join(f"{k}={v}" for k, v in d.items())
    return kind, f"{ref} {text}".strip()[:200], kind in ATTENTION_KINDS, False


def _rows(cfg, led, where="", args=(), limit=EVENTS_SHOWN):
    marks = ",".join("?" * len(QUIET_KINDS))
    out = []
    for e in led.q(f"SELECT * FROM events WHERE kind NOT IN ({marks}) {where}"
                   f" ORDER BY id DESC LIMIT ?", (*QUIET_KINDS, *args, limit * 4)):
        described = _describe(cfg, e)
        if described is None:
            continue
        kind, text, attention, undoable = described
        at = parse(e["at"])
        out.append({"id": e["id"], "at": at, "when": _hhmm(at) if at else "",
                    "kind": kind, "text": text, "attention": attention,
                    "undoable": undoable, "project": e["project"], "number": e["number"]})
        if len(out) >= limit:
            break
    return out


def _events(cfg, led):
    return _rows(cfg, led)


def _digest(cfg, led, now):
    """Events since you last marked the stream seen — the "3 new" chip."""
    try:
        seen = int(led.get_kv(SEEN_KEY) or 0)
    except (TypeError, ValueError):
        seen = 0
    rows = [r for r in _rows(cfg, led, "AND id > ?", (seen,), limit=DIGEST_SHOWN)
            if r["at"] and now - r["at"] < timedelta(hours=DIGEST_HOURS)]
    return {"count": len(rows), "rows": rows,
            "upto": max((r["id"] for r in rows), default=seen)}


# ---------- banners and idle reasons ----------

def _hot_holds(led, projects, now):
    """Projects under a hot hold (D6 layer 2): a Claude session active there
    in the last hot_hold_minutes, so no new build starts."""
    out = []
    for p in projects:
        if not p.get("hot_hold") or not p.get("path"):
            continue
        last = presence.last_claude_activity(p["path"])
        if last and now - last < timedelta(minutes=p["hot_hold_minutes"]):
            out.append({"project": p["name"], "ago": _mins(now - last),
                        "hold_minutes": p["hot_hold_minutes"],
                        "until": last + timedelta(minutes=p["hot_hold_minutes"])})
    return out


def _silent_runs(led, quota):
    """Runs stopped for printing nothing whose platform is still on hold."""
    out = []
    for name in (q["name"] for q in quota if q["state"] == "hold"):
        r = led.q1("SELECT * FROM runs WHERE platform=? AND stop_reason='silent'"
                   " ORDER BY id DESC LIMIT 1", (name,))
        if r:
            out.append((name, r))
    return out


def _banners(cfg, led, paused, quota, hot, now):
    """Persistent notices for blocked states, most severe first. Only
    Triage / Needs you shows them, and only the first is expanded."""
    out = []
    if paused:
        out.append({"kind": "PAUSED BY YOU", "tone": "warn",
                    "text": "Nothing new will start. Runs in flight finish at their next "
                            "checkpoint. Quota probes and GitHub sync keep going.",
                    "action": "Resume all", "act": "resume"})
    builders = [q for q in quota if q["builds"]]
    if builders and all(q["state"] != "ok" for q in builders):
        out.append({"kind": "ALL PLATFORMS OVER SOFT LINE", "tone": "warn",
                    "text": "Every builder is at or past its soft line, so the scheduler is "
                            "holding. It starts again on its own as windows roll over."})
    for h in hot:
        ago = (f"{h['ago']} minute{'s' if h['ago'] != 1 else ''} ago" if h["ago"]
               else "under a minute ago")
        out.append({"kind": f"HOT HOLD · {h['project'].upper()}", "tone": "warn",
                    # what presence sees is a Claude Code session, and a hot
                    # hold only holds builds (D6 layer 2)
                    "text": f"You were working in {h['project']} with Claude Code {ago}. No "
                            f"new builds start there until {h['hold_minutes']} minutes after "
                            f"you stop. Work in flight continues."})
    for name, r in _silent_runs(led, quota):
        mins = config.project_policy(cfg, r["project"]).get("startup_timeout_minutes", 10)
        out.append({"kind": f"RUN SAT SILENT · {name.upper()}", "tone": "bad",
                    "text": f"Run {r['id']} printed nothing for {mins} minutes and was "
                            f"stopped. Check the Mac mini for a macOS permission dialog — it "
                            f"blocks the agent with no output.",
                    "action": "How to fix", "href": DESIGN_D8})
    return out


def _pending(cfg, led, projects):
    """Items a run could start on: inbox (to sort) and ready (to build)."""
    return {p["name"]: [i for i in led.items(p["name"], ["inbox", "ready"])] for p in projects}


BLOCKER_LABELS = {
    "busy": "busy", "over": "past the line", "peak": "peak hours",
    "size": "too small", "tier": "below required tier",
    "stale": "no fresh reading", "account": "wrong account", "other": "unavailable",
}


def _schedule_holds(led, now):
    """None means absent/stale; an empty list is a fresh scheduler snapshot."""
    try:
        snapshot = json.loads(led.get_kv("schedule_holds") or "null")
        at = parse(snapshot["at"])
        holds = snapshot["holds"]
        if not (timedelta(0) <= now - at <= timedelta(minutes=3)):
            return None
        if not isinstance(holds, list) or not all(isinstance(h, dict) for h in holds):
            return None
        return holds
    except (ValueError, TypeError, KeyError):
        return None


def _hold_reasons(cfg, holds, pending, hot, now):
    out, routes, settling, deps = [], {}, {}, []
    seen = set()
    for h in holds:
        kind, project = h.get("kind"), h.get("project")
        if project not in pending or not pending[project]:
            continue
        number = h.get("number")
        if number is not None and number not in {i["number"] for i in pending[project]}:
            continue
        pol = config.project_policy(cfg, project)
        ref = _ref(project, number)
        if kind == "no_platform":
            routes.setdefault((h["role"], h["size"]), []).append(h)
        elif kind == "settling":
            until = router._ts(h.get("until"))
            if until and until > now:
                settling.setdefault(pol["settle_minutes"], []).append(until)
        elif kind == "deps":
            deps.append(h)
        elif kind in ("area", "files"):
            overlap = f"area:{h['area']}" if kind == "area" else ", ".join(h["files"])
            out.append({"text": f"{ref} waits — {overlap} already in progress."})
        elif (kind, project) not in seen:
            seen.add((kind, project))
            if kind == "capacity":
                out.append({"text": f"{project} is at its limit of {h['max_parallel']} run(s)."})
            elif kind == "lease_host":
                out.append({"text": f"{project} waits — its canonical lease host is unavailable."})
            elif kind == "hot_hold" and not any(x["project"] == project for x in hot):
                out.append({"text": f"You have been working in {project}, so new builds there wait "
                                    f"until {pol['hot_hold_minutes']} minutes after you stop."})
    for (role, size), items in routes.items():
        groups = {}
        for h in items:
            for category, names in h["blockers"].items():
                groups.setdefault(category, set()).update(names)
        groups = {k: v for k, v in groups.items() if v}
        if set(groups) == {"size"}:
            text = (f"{len(items)} item(s) need a builder that takes size:{size}, "
                    "and none in the route does.")
        else:
            summary = "; ".join(f"{label}: {', '.join(sorted(groups[k]))}"
                                for k, label in BLOCKER_LABELS.items() if k in groups)
            text = (f"{len(items)} {role} item(s) have no platform with headroom — "
                    f"{summary or 'no platforms in the route'}.")
        out.append({"text": text})
    for minutes, times in settling.items():
        first = max(1, int((min(times) - now).total_seconds() / 60 + .999))
        out.append({"text": f"{len(times)} item(s) were just sorted and settle for "
                            f"{minutes} minutes before a build starts.",
                    "countdown": f"first in {first}m"})
    if len(deps) > 3:
        out.append({"text": f"{len(deps)} items wait for other issues to close."})
    else:
        for h in deps:
            refs = _join(_ref(h["project"], n) for n in h["on"])
            out.append({"text": f"{_ref(h['project'], h['number'])} waits for {refs} to close."})
    return out


def _idle(cfg, led, s, hot, now):
    """Why nothing is running: every reason that is actually binding, each a
    sentence with a countdown when one exists and an escape when you have one."""
    projects = config.enabled_projects(cfg)
    pending = _pending(cfg, led, projects)
    n_pending = sum(len(v) for v in pending.values())
    if not n_pending:
        # with nothing queued, quota and peak hours aren't what holds anything
        waiting = any(led.items(p["name"], ["verifying"]) for p in projects)
        return {"headline": "Nothing is running.", "reasons": [{"text": (
            "Nothing is waiting to start — finished changes are waiting on CI." if waiting
            else "The backlog is empty — nothing to work on.")}]}
    schedule_holds = _schedule_holds(led, now)
    reasons = []
    if s["paused"]:
        reasons.append({"text": "You paused everything, so nothing new starts.",
                        "action": "Resume all", "act": "resume"})
    peak = s["peak"]
    if peak and peak["active"]:
        reasons.append({
            "text": f"Claude is in your peak hours — it plans but doesn't build until "
                    f"{peak['until']} {peak['tz']}. Free tiers are unaffected.",
            "countdown": peak["left"], "action": "Let Claude build", "act": "peak_override"})
    # metered platforms over a soft line, grouped by window and line
    over = {}
    for q in s["quota"]:
        if q["state"] in ("soft", "hard") and q["metered"]:
            for x in q["over"] or []:
                over.setdefault((x["window"], x["soft"]), []).append((q["name"], x["resets"]))
                break
    for (window, line), plats in over.items():
        names = [n for n, _ in plats]
        verb = ("is" if len(names) == 1 else "are both" if len(names) == 2 else "are all")
        resets = min((r for _, r in plats if r and r > now), default=None)
        reasons.append({
            "text": f"{_join(names)} {verb} past {line:.0f}% on the {window} window, so the "
                    f"scheduler won't start there.",
            "countdown": f"{window} resets {_when(resets, now)}" if resets else None})
    stale = [q["name"] for q in s["quota"] if q["state"] == "stale"]
    if stale:
        reasons.append({"text": f"{_join(stale)} {'has' if len(stale) == 1 else 'have'} no "
                                f"fresh quota reading, and Mahler counts unknown as over the "
                                f"line."})
    backoff = [q for q in s["quota"] if q["state"] == "backoff"]
    if backoff:
        reasons.append({
            "text": f"{_join(q['name'] for q in backoff)} "
                    f"{'is' if len(backoff) == 1 else 'are'} backing off after quota errors.",
            "countdown": " · ".join(f"{q['name']} {_dur(q['until'] - now)}"
                                    for q in backoff if q["until"]) or None,
            "action": "Clear backoff", "act": "clear_backoff",
            "platforms": [q["name"] for q in backoff]})
    holds = [q for q in s["quota"] if q["state"] == "hold"]
    if holds:
        reasons.append({
            "text": f"{_join(q['name'] for q in holds)} "
                    f"{'is' if len(holds) == 1 else 'are'} on hold after a run never started.",
            "countdown": " · ".join(f"{q['name']} {_dur(q['until'] - now)}" for q in holds),
            "action": "Clear backoff", "act": "clear_backoff",
            "platforms": [q["name"] for q in holds]})
    for p in projects:
        name = p["name"]
        builds = [i for i in pending[name] if i["state"] == "ready"]
        if (pending[name] and p.get("max_parallel", 1) == 0
                and not any(h.get("kind") == "capacity" and h.get("project") == name
                            for h in schedule_holds or [])):
            reasons.append({"text": f"{name} has max_parallel set to 0, so its "
                                    f"{len(pending[name])} waiting item(s) stay put."})
            continue
        verifying = led.items(name, ["verifying"])
        if builds and verifying and len(verifying) >= p.get("max_parallel", 1):
            v = verifying[0]
            slot = ("the project's only parallel slot" if p.get("max_parallel", 1) == 1
                    else "one of the project's parallel slots")
            since = parse(v["state_changed_at"])
            pending_m = _mins(now - since) if since else 0
            text = f"{_ref(name, v['number'])} holds {slot} until its PR merges"
            text += (f" — CI has been pending {pending_m} minute{'s' if pending_m != 1 else ''}."
                     if v["pr"] else ".")
            timeout = p.get("verify_timeout_minutes", 60) - pending_m
            reason = {"text": text,
                      "countdown": f"verify timeout in {_dur(timedelta(minutes=timeout))}"
                      if v["pr"] and timeout > 0 else None}
            if v["pr"]:
                reason.update(action=f"Open PR #{v['pr']}", href=_pr_url(cfg, name, v["pr"]))
            reasons.append(reason)
    for h in hot:
        if any(i["state"] == "ready" for i in pending.get(h["project"], [])):
            reasons.append({
                "text": f"You have been working in {h['project']}, so new builds there wait "
                        f"until {h['hold_minutes']} minutes after you stop.",
                "countdown": f"{_dur(h['until'] - now)} left"})
    if schedule_holds is not None:
        reasons.extend(_hold_reasons(cfg, schedule_holds, pending, hot, now))
    if not reasons and schedule_holds is None:
        reasons.append({"text": f"{n_pending} item(s) queued, but no platform has "
                                f"headroom for them right now."})
    n = len(reasons)
    if not n:
        return {"headline": "Nothing is running.", "reasons": []}
    word = NUMBER_WORDS[n] if n < len(NUMBER_WORDS) else str(n)
    return {"headline": f"Nothing is running. {word} thing{'s are' if n != 1 else ' is'} "
                        f"holding it:",
            "reasons": reasons}
