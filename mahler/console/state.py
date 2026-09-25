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
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .. import config, presence, router
from ..gh import dependency_ref, dependency_target
from ..ledger import iso, parse, row_get
from . import outbox

EVENTS_SHOWN = 50
DIGEST_SHOWN = 20
SEEN_KEY = "console_seen_event"        # kv: the newest event id marked seen
BRIEF_SEEN_PREFIX = "console_brief_seen:"  # kv: per-project shipped event cursor
DIGEST_HOURS = 24                      # older unseen events stop counting as new
CAPTURE_RECENT_MINUTES = 10            # how long a capture's confirmation note lingers
REASON_ITEMS_CAP = 20                  # max items shown behind an expanded hold reason

# bookkeeping the event stream leaves out: every lease and heartbeat, stats
# rows, and the console's own read marker
QUIET_KINDS = ("lease", "release", "issue_done_stats", "estimate_calibration",
               "console_seen", "console_brief_seen")
# state transitions worth a row; the rest repeat a run_start, pr_opened or
# shipped event logged in the same tick
STREAM_STATES = ("ready", "inbox", "needs_you", "failed", "parked", "parent")
ATTENTION_STATES = ("needs_you", "failed")
# needs-you severity (full ink, and a push); everything else is FYI
ATTENTION_KINDS = ("launch_failed", "backup_failed")

STATE_ORDER = ("needs_you", "failed", "working", "verifying", "ready", "inbox",
               "parked", "parent")
ROLE_WORDS = {"build": "building", "sort": "sorting", "fix": "fixing CI"}
NUMBER_WORDS = ("No", "One", "Two", "Three", "Four", "Five", "Six", "Seven",
                "Eight", "Nine")
DESIGN_D8 = ("https://github.com/mkny13/mahler/blob/main/DESIGN.md"
             "#d8--platforms-quota-sensing-and-routing")

_STATE_DETAIL = re.compile(r"^(\w+) -> (\w+)(?: \((.*)\))?$", re.S)


def build(cfg, led, stats_range="week"):
    """The whole console as plain data (see docs/console/design.md, "Shared
    state model"). Client-only state — tab, theme, expanded groups — is not
    here; the page keeps that in the browser."""
    now = led.now()
    projects = config.enabled_projects(cfg)
    peak = _peak(cfg, led)
    quota = _quota(cfg, led, peak)
    runs = _runs(cfg, led, now)
    needs = _needs(cfg, led, projects, now)
    uat = _uat(cfg, led, projects)
    backlog = _backlog(cfg, led, projects)
    dep_graph = {g["project"]: _graph(g["items"]) for g in backlog if len(g["items"]) > 1}
    events = _events(cfg, led)
    digest = _digest(cfg, led, now)
    paused = led.paused()
    hot = _hot_holds(led, projects, now)
    project_names = [p["name"] for p in projects]
    releases_data = _releases(cfg, led, projects, now)
    briefs = _briefs(cfg, led, projects, now)
    s = {
        "paused": paused,
        "system": ({"label": "PAUSED", "tone": "warn"} if paused else
                   {"label": f"RUNNING · {len(runs)}", "tone": "acc"} if runs else
                   {"label": "IDLE", "tone": "mut"}),
        "peak": peak,
        "runs": runs,
        "needs": needs,
        "needs_count": sum(n["pending"] is None for n in needs),
        "uat": uat,
        # the count you act on: items with no verdict, recorded or queued
        "uat_count": sum(u["pending"] is None for u in uat),
        "backlog": backlog,
        "backlog_total": sum(len(g["items"]) for g in backlog),
        "dep_graph": dep_graph,
        "quota": quota,
        "capability_sections": _capability_sections(quota),
        "capacity": _capacity_line(quota),
        "models": models(led, cfg),
        "stats": stats(led, stats_range, project_names),
        "stats_range": _stats_range_key(stats_range),
        "releases": releases_data,
        "releases_suggested": sum(1 for r in releases_data if r["draft"]["is_suggested"]),
        "briefs": briefs,
        "briefs_unread": sum(b["count"] for b in briefs),
        "events": events,
        "digest": digest,
        "banners": _banners(cfg, led, paused, quota, hot, now),
        "projects": project_names,
        "capture": _capture(cfg, led, project_names, now),
        "settings": config.settings(cfg),
    }
    s["idle"] = None if runs else _idle(cfg, led, s, hot, now)
    s["landing"] = {
        "tab": "triage" if needs or s["uat_count"] else "now",
        "view": "needs" if needs else "test" if s["uat_count"] else "now",
    }
    return s


# ---------- productivity stats ----------

STATS_RANGES = (
    ("today", "Today"),
    ("week", "This week"),
    ("month", "This month"),
    ("last7", "Last 7 days"),
    ("last30", "Last 30 days"),
)


def _stats_range_key(range_key):
    if isinstance(range_key, (tuple, list)) and len(range_key) == 2:
        try:
            start, end = date.fromisoformat(range_key[0]), date.fromisoformat(range_key[1])
            if end < start:
                raise ValueError
            return f"custom:{start.isoformat()}:{end.isoformat()}"
        except (TypeError, ValueError):
            return "week"
    return range_key if any(range_key == key for key, _ in STATS_RANGES) else "week"


def _stats_window(now, range_key):
    """Return [start, end) and the equally long window immediately before it."""
    local_now = now.astimezone()
    local_midnight = datetime.combine(local_now.date(), time.min, local_now.tzinfo)
    key = _stats_range_key(range_key)
    if key.startswith("custom:"):
        try:
            _, start_s, end_s = key.split(":", 2)
            start_d, end_d = date.fromisoformat(start_s), date.fromisoformat(end_s)
            if end_d < start_d:
                raise ValueError
            start = datetime.combine(start_d, time.min, local_now.tzinfo)
            end = datetime.combine(end_d + timedelta(days=1), time.min, local_now.tzinfo)
        except (TypeError, ValueError):  # guarded by _stats_range_key; defensive for callers
            key = "week"
    if key == "today":
        start, end = local_midnight, local_now
    elif key == "week":
        start, end = local_midnight - timedelta(days=local_now.weekday()), local_now
    elif key == "month":
        start, end = local_midnight.replace(day=1), local_now
    elif key == "last7":
        start, end = local_now - timedelta(days=7), local_now
    elif key == "last30":
        start, end = local_now - timedelta(days=30), local_now
    duration = end - start
    return start, end, start - duration, start


def stats(led, range_key="week", projects=None):
    """Closed-item counts by project for a range and its preceding peer."""
    start, end, prev_start, prev_end = _stats_window(led.now(), range_key)
    done = led.items(states=("done",))
    names = list(projects) if projects is not None else sorted({row["project"] for row in done})
    out = {name: {"closed": 0, "prev_closed": 0, "delta": 0} for name in names}
    for row in done:
        if row["project"] not in out:
            continue
        changed = parse(row["state_changed_at"])
        if changed is None:
            continue
        changed = changed.astimezone(start.tzinfo)
        if start <= changed < end:
            out[row["project"]]["closed"] += 1
        elif prev_start <= changed < prev_end:
            out[row["project"]]["prev_closed"] += 1
    for counts in out.values():
        counts["delta"] = counts["closed"] - counts["prev_closed"]
    return out


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


def _brief_when(dt, now):
    """A shipment time: time today, date for older catch-up material."""
    local, local_now = dt.astimezone(), now.astimezone()
    if local.date() == local_now.date():
        return local.strftime("%H:%M")
    if local.year == local_now.year:
        return f"{local:%b} {local.day}"
    return f"{local:%b} {local.day} {local.year}"


def _ref(project, number):
    return f"{project}#{number}"


def _issue_url(cfg, project, number):
    repo = config.project_policy(cfg, project).get("repo")
    return f"https://github.com/{repo}/issues/{number}" if repo else None


def _reason_items(cfg, pending, entries):
    """The concrete items behind a hold reason, each as ref + GitHub url + title.

    entries is [(project, number), ...]; a number no longer pending still
    shows (the hold snapshot named it), just without a title."""
    out = []
    for project, number in entries:
        full = next((i for i in pending.get(project, []) if i["number"] == number), None)
        out.append({"ref": _ref(project, number), "url": _issue_url(cfg, project, number),
                    "title": full["title"] if full else None})
    return out


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
    scheduled = now.astimezone(ZoneInfo(pc.get("tz", "America/Los_Angeles")))
    label = router.local_time_label(now)
    active, until = router.peak_state(cfg, led)
    overridden = router.peak_overridden(led)
    # Anchor configured clock times to the schedule's date before converting;
    # the viewer's date or today's fixed UTC offset can differ at DST boundaries.
    endpoints = []
    for key, default in (("start", "05:00"), ("end", "11:00")):
        try:
            hour, minute = map(int, pc.get(key, default).split(":"))
            endpoints.append(scheduled.replace(hour=hour, minute=minute,
                                               second=0, microsecond=0).astimezone())
        except (ValueError, AttributeError):
            endpoints = []
            break
    if endpoints:
        start, end = endpoints
        label = router.local_time_label(end)
        window = f"{start:%H:%M}–{end:%H:%M} {label}"
    else:
        window = "(invalid schedule)"
    until_s = until.astimezone().strftime("%H:%M") if until else None
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
            for n in config.expand_route(cfg, table.get(role) or []):
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


CAPABILITY_SUFFIXES = ("low", "medium", "high", "astra")
CAPABILITY_SIZES = ("large", "medium", "small")
ROUTE_SIZE_NAMES = {"s": "small", "m": "medium", "l": "large"}


def _quota_display_name(members):
    """The account/provider name shared by capability-slot aliases.

    `work-codex-gpt1-low` through `-astra` become `work-codex-gpt1`; the
    capacity view should describe the quota pool, not imply a separate quota
    for every model.  Older compact aliases (claude/claude-opus) converge on
    their common dash-delimited prefix too.
    """
    parts = []
    for member in members:
        bits = member.split("-")
        if bits[-1] in CAPABILITY_SUFFIXES:
            bits.pop()
        parts.append(bits)
    common = []
    for col in zip(*parts):
        if len(set(col)) != 1:
            break
        common.append(col[0])
    return "-".join(common) or members[0]


def _quota_model(pconf, members):
    """A group view names its slot count; a single platform shows its model."""
    if len(members) == 1:
        return _model_line(pconf)
    plan = pconf.get("plan")
    account = config.account_of(pconf)
    plan = plan or (f"{account} account" if account != config.DEFAULT_ACCOUNT else "")
    return f"{len(members)} capability slots" + (f" · {plan}" if plan else "")


def _quota_row(cfg, led, peak, name, members, builders):
    """One quota/capability card, using ``name`` for its effective lines."""
    now = led.now()
    burst = router.all_bursts(cfg, led)
    pconf = cfg["platforms"][name]
    claude = pconf.get("kind") == "claude"
    state, _ = router.usage_state(led, name, pconf,
                                  burst_lines=burst if claude else None)
    usage = led.usage(name)
    hold = usage.pop(router.HOLD, None)
    hold_until = router._ts(hold.get("resets_at")) if hold else None
    metered = router.is_metered(led, name, pconf)
    windows = []
    for w in pconf.get("windows", router.WINDOWS):
        u = usage.get(w)
        if u is None:
            continue
        soft, _ = router.effective_lines(led, name, pconf, w, burst)
        windows.append({"window": w, "pct": max(min(u["used_pct"], 100), 0),
                        "soft": soft, "resets": router._ts(u.get("resets_at"))})
    worst = max(windows, key=lambda x: x["pct"], default=None)
    row = {"name": name, "members": members, "model": _quota_model(pconf, members),
           "state": state, "metered": metered, "claude": claude,
           "builds": any(m in builders for m in members),
           # This is the largest issue size the route is intended to take.
           # An unrestricted or large-only route belongs in the large section;
           # max_size is otherwise the routing contract's exact ceiling.
           "route_size": ROUTE_SIZE_NAMES.get(pconf.get("max_size"), "large"),
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
        if pconf.get("quota_group") == "copilot" or pconf.get("monthly_cap_credits"):
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
    codex = router.codex_detail(led, name, pconf)
    if codex:
        row["detail"] += " · " + codex
    if worst:
        row["soft_pct"] = worst["soft"]
    row["windows"] = [{"window": x["window"], "pct": x["pct"], "soft": x["soft"],
                       "resets": x["resets"],
                       "resets_txt": _when(x["resets"], now) if x["resets"] else None}
                      for x in windows]
    row["peak_held"] = bool(claude and peak and peak["active"])
    row["available"] = row["state"] == "ok" and not row["peak_held"]
    return row


def _quota(cfg, led, peak):
    """One gauge per quota group (platforms sharing a login and quota share a
    gauge, D21): the worst window fills the bar, the soft line is the tick."""
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
        row = _quota_row(cfg, led, peak, name, members, builders)
        row["name"] = _quota_display_name(members)
        row["capabilities"] = [_quota_row(cfg, led, peak, member, [member], builders)
                               for member in members]
        out.append(row)
    return out


def _capability_sections(quota):
    """Individual routing slots arranged by the work size they can take.

    The quota representation intentionally knows nothing about individual
    aliases once it has consolidated a shared pool. The capability view is the
    complementary representation: every routed alias appears once, under the
    largest route size it is eligible to serve.
    """
    buckets = {size: [] for size in CAPABILITY_SIZES}
    for pool in quota:
        for capability in pool["capabilities"]:
            buckets.get(capability["route_size"], buckets["large"]).append(capability)
    sections = []
    for size, capabilities in buckets.items():
        if not capabilities:
            continue
        # Count routing choices, not independent quota pools or concurrent runs.
        # Availability already checks every window and hold; percentages across
        # providers (or aliases sharing a login) cannot be pooled meaningfully.
        available = sum(c["available"] for c in capabilities)
        total = len(capabilities)
        sections.append({"size": size, "label": size.title(),
                         "capabilities": capabilities,
                         "available_count": available, "total_count": total,
                         "composite_estimate":
                             f"≈ {available} of {total} slots available (approx.)"})
    return sections


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
    stop_queued = {json.loads(r["payload"]).get("run") for r in led.pending_actions("stop_run")}
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
        from . import logtail
        ls = logtail.live_status(dict(r))
        status = (f"stopping · {r['stop_reason']}" if r["status"] == "stopping" and r["stop_reason"]
                  else "stopping" if r["status"] == "stopping" or r["id"] in stop_queued
                  else ls["text"])
        status_tone = ls["tone"] if r["status"] not in ("stopping",) and r["id"] not in stop_queued else "mut"
        out.append({
            "id": r["id"], "ref": _ref(project, n), "url": _issue_url(cfg, project, n),
            "title": (item["title"] if item else None) or _ref(project, n),
            "platform": r["platform"], "role": r["role"],
            "mins": mins, "est": est,
            "progress": min(100, round(mins / est * 100)),
            "tone": "warn" if over else "acc",
            "timing": f"{mins}m · {mins - est}m past estimate" if over else f"{mins}m of ~{est}m",
            "status": status, "status_tone": status_tone, "meta": " · ".join(meta),
        })
    return out


# ---------- needs you ----------

def _question(led, project, number, state, item):
    """The question an item is waiting on. For needs_you, item['question']
    when finalize's OPTIONS split stored one (mahler#248); otherwise — and
    always for failed, whose reason isn't a 'question' column — the reason on
    its latest move into that state."""
    if state == "needs_you":
        question = row_get(item, "question")
        if question:
            return question
    for e in led.q("SELECT detail FROM events WHERE project=? AND number=? AND kind='state'"
                   " ORDER BY id DESC LIMIT 20", (project, number)):
        _, to, why = _state_parts(e["detail"])
        if to == state:
            return why
    return None


def _answer_options(item):
    options = row_get(item, "options", [])
    if isinstance(options, str):
        try:
            options = json.loads(options)
        except ValueError:
            return []
    return [{"label": o, "text": o} for o in options if isinstance(o, str)] if isinstance(options, list) else []


_LEGACY_OPTIONS = re.compile(r"\s*\[?OPTIONS:\s*(.*?)\]?\s*$", re.I | re.S)


def _legacy_question_options(question):
    """Recover pre-mahler#248 choices that only survived in the question.

    New handoffs store question/options separately. Older rows may contain a
    terminal ``OPTIONS: a | b`` (optionally bracketed), which the console used
    to display as question text. Keep the same three-choice/40-character caps
    as finalize's current STATUS parser.
    """
    question = question or ""
    match = _LEGACY_OPTIONS.search(question)
    if not match:
        return question, []
    choices = [choice.strip()[:40] for choice in match.group(1).split("|")
               if choice.strip()]
    return question[:match.start()].rstrip(), choices[:3]


def _needs(cfg, led, projects, now):
    names = {p["name"] for p in projects}
    pending = {(r["project"], r["number"]): {"id": r["id"], "text": json.loads(r["payload"])["text"]}
               for r in led.pending_actions("answer")}
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
        question = _question(led, project, n, it["state"], it) or it["title"] or ""
        options = _answer_options(it)
        if it["state"] == "needs_you" and not options:
            question, legacy_options = _legacy_question_options(question)
            options = [{"label": option, "text": option} for option in legacy_options]
        out.append({
            "id": _ref(project, n), "project": project, "number": n,
            "ref": _ref(project, n), "url": _issue_url(cfg, project, n),
            "title": it["title"] or "",
            "body": row_get(it, "issue_body", ""),
            "question": question,
            "pending": pending.get((project, n)),
            "options": ([{"label": "Retry", "text": "/mahler go"},
                         {"label": "Park it", "text": "/mahler park"}]
                        if it["state"] == "failed" else options),
            "p": f"p{it['priority']}", "p1": it["priority"] == 1,
            "state": it["state"], "waited_s": waited.total_seconds(),
            "meta": " · ".join(meta),
        })
    out.sort(key=lambda x: (not x["p1"], -x["waited_s"]))
    return out


def _uat(cfg, led, projects):
    """The Ready-to-test queue (D10): what shipped with a needs-human check,
    until you pass or fail it. A verdict queued but not yet run (the tick
    applies it) still shows, as the copy it will become."""
    pols = {p["name"]: p for p in projects}
    queued = {}
    for kind in ("uat_pass", "uat_fail"):
        for r in led.pending_actions(kind):
            queued[(r["project"], r["number"])] = kind
    out = []
    for row in led.pending_uat():
        project, n = row["project"], row["number"]
        if project not in pols:
            continue
        shipped = parse(row["shipped_at"])
        meta = [f"merged {_hhmm(shipped)}" if shipped else "merged"]
        if row["sha"]:
            meta.append(f"sha {row['sha'][:7]}")
        needs = []
        for line in (row["needs"] or "").splitlines():
            t = re.sub(r"^[-*]\s+(?:\[[ xX]\]\s+)?", "", line.strip())
            if t:
                needs.append(t)
        uat_url = pols[project].get("uat_url")
        if uat_url:
            link = uat_url.replace("{number}", str(n)).replace(
                "{pr}", str(row["pr"] or ""))
            link_label = pols[project].get("uat_url_label") or "Staging"
        elif row["pr"]:
            link, link_label = _pr_url(cfg, project, row["pr"]), f"PR #{row['pr']}"
        else:
            link, link_label = None, None
        out.append({
            "project": project, "number": n, "ref": _ref(project, n),
            "url": _issue_url(cfg, project, n),
            "title": row["title"] or _ref(project, n),
            "meta": " · ".join(meta), "check": "; ".join(needs)[:200],
            "link": link, "link_label": link_label,
            "pending": queued.get((project, n)),
        })
    return out


# ---------- backlog ----------

def _state_tone(state):
    return ("bad" if state in ATTENTION_STATES else
            "acc" if state in ("working", "verifying") else "mut")


def _backlog(cfg, led, projects):
    rank = {s: i for i, s in enumerate(STATE_ORDER)}
    enabled = config.enabled_projects(cfg)
    pending = {}
    for r in led.pending_actions("capture"):
        pending.setdefault(r["project"], []).append(r)
    out = []
    for p in projects:
        name = p["name"]
        items = [i for i in led.items(name) if i["state"] != "done"]
        items.sort(key=lambda i: (rank.get(i["state"], len(rank)), i["priority"], i["number"]))
        ready = sum(1 for i in items if i["state"] in ("ready", "inbox"))
        live = sum(1 for i in items if i["state"] in ("working", "verifying"))
        you = sum(1 for i in items if i["state"] in ATTENTION_STATES)
        open_numbers = {i["number"] for i in items}
        # a queued capture shows as a placeholder inbox row until the tick
        # creates the issue and sync() pulls in the real item (mahler#251)
        rows = [{"ref": None, "url": None, "number": None, "pr": None, "pr_url": None,
                 "title": outbox.capture_title(json.loads(r["payload"])["text"]),
                 "p": "p2", "p1": False, "priority": 2, "state": "inbox", "tone": "mut",
                 "parent": None, "depends": []}
                for r in pending.get(name, [])]
        rows += [{"ref": _ref(name, i["number"]), "url": _issue_url(cfg, name, i["number"]),
                  "number": i["number"],
                  "pr": i["pr"], "pr_url": _pr_url(cfg, name, i["pr"]) if i["pr"] else None,
                  "title": i["title"] or "", "p": f"p{i['priority']}",
                  "p1": i["priority"] == 1, "priority": i["priority"],
                  "state": i["state"].replace("_", "-"), "tone": _state_tone(i["state"]),
                  "parent": i["parent"],
                  "depends": [target[1] for d in json.loads(i["depends"] or "[]")
                              if (target := dependency_target(d, name, enabled))
                              and target[0] == name and target[1] in open_numbers]}
                 for i in items]
        out.append({
            "project": name,
            "counts": f"{len(items)} · {ready} ready · {live} live" + (f" · {you} you" if you else ""),
            "items": rows,
        })
    return out


def _graph(items):
    """Rank one project's backlog rows (D29) into a longest-path DAG layout:
    a node with no unresolved predecessor sits at rank 0, everything else one
    rank past its farthest predecessor. Capture placeholders (no number yet,
    so no issue to link or depend on) are left out."""
    by_number = {i["number"]: i for i in items if i["number"] is not None}
    preds = {n: [] for n in by_number}
    edges = []
    for n, it in by_number.items():
        if it["parent"] in by_number:
            preds[n].append(it["parent"])
            edges.append({"from": it["parent"], "to": n, "kind": "parent"})
        for d in it["depends"]:
            if d in by_number:
                preds[n].append(d)
                edges.append({"from": d, "to": n, "kind": "depends"})
    rank = {n: 0 for n in by_number}
    ranked = {n for n in by_number if not preds[n]}
    for _ in range(len(by_number)):
        progressed = False
        for n in by_number:
            if n in ranked or not all(p in ranked for p in preds[n]):
                continue
            rank[n] = 1 + max(rank[p] for p in preds[n])
            ranked.add(n)
            progressed = True
        if not progressed:
            break   # a cycle: whatever's left stays at rank 0 rather than hang
    nodes = []
    order_in_rank = {}
    for n, it in sorted(by_number.items(), key=lambda kv: (rank[kv[0]], kv[1]["priority"], kv[0])):
        order_in_rank[rank[n]] = order_in_rank.get(rank[n], -1) + 1
        nodes.append({**it, "rank": rank[n], "order": order_in_rank[rank[n]]})
    return {"nodes": nodes, "edges": edges}


# ---------- releases (DESIGN D31, mahler#359) ----------

def _releases(cfg, led, projects, now):
    from .. import releases
    out = []
    for p in projects:
        proj = p["name"]
        draft = releases.get_draft(led, proj, now=now)

        age_str = ""
        if draft.oldest_shipped_at:
            delta = now - draft.oldest_shipped_at
            if delta.days >= 1:
                age_str = f"{delta.days}d"
            else:
                hours = int(delta.total_seconds() // 3600)
                if hours >= 1:
                    age_str = f"{hours}h"
                else:
                    mins = max(1, int(delta.total_seconds() // 60))
                    age_str = f"{mins}m"

        def _item_dict(it):
            return {
                "number": it.number,
                "pr": it.pr,
                "title": it.title,
                "summary": it.summary,
                "ref": f"{proj}#{it.number}",
                "url": _issue_url(cfg, proj, it.number),
                "pr_url": _pr_url(cfg, proj, it.pr) if it.pr else None,
                "labels": it.labels,
                "shipped_at": it.shipped_at,
                "merge_sha": it.merge_sha,
                "formatted_line": it.formatted_line(),
            }

        draft_dict = {
            "count": draft.count,
            "age": age_str,
            "is_suggested": draft.is_suggested,
            "readiness_reasons": draft.readiness_reasons,
            "proposed_version": draft.proposed_version,
            "version_options": releases.semver_options(draft.last_version, draft.proposed_version),
            "checkpoint_sha": draft.checkpoint_sha or "",
            "main_summary": draft.notes.main_summary,
            "other_section": draft.notes.other_section,
            "summary": draft.notes.summary,
            "maintenance_details": draft.notes.maintenance_details,
            "expanded_notes": draft.notes.render(include_maintenance=True, collapsed_maintenance=True),
            "features": [_item_dict(it) for it in draft.notes.features],
            "fixes": [_item_dict(it) for it in draft.notes.fixes],
            "other": [_item_dict(it) for it in draft.notes.other],
            "maintenance": [_item_dict(it) for it in draft.notes.maintenance],
            "items": [_item_dict(it) for it in draft.items],
            "item_numbers": [it.number for it in draft.items],
        }

        pub_rows = [r for r in led.list_releases(proj) if r["state"] == "published"]
        published = []
        for r in pub_rows:
            item_rows = led.release_items_for_release(r["id"])
            rel_items = [releases._to_release_item(it) for it in item_rows]
            rel_notes = releases.synthesize_notes(rel_items)
            published.append({
                "id": r["id"],
                "version": r["version"],
                "checkpoint_sha": r["checkpoint_sha"],
                "published_at": r["published_at"],
                "remote_url": r["remote_url"],
                "notes": r["notes"],
                "item_count": len(item_rows),
                "features": [_item_dict(it) for it in rel_notes.features],
                "fixes": [_item_dict(it) for it in rel_notes.fixes],
                "other": [_item_dict(it) for it in rel_notes.other],
                "maintenance": [_item_dict(it) for it in rel_notes.maintenance],
                "maintenance_details": rel_notes.maintenance_details,
            })

        action_state = {}
        act_row = led.q1(
            "SELECT * FROM console_actions WHERE kind='cut_release' AND project=? ORDER BY id DESC LIMIT 1",
            (proj,)
        )
        if act_row:
            act_payload = _detail_json(act_row["payload"]) or {}
            action_state = {
                "id": act_row["id"],
                "status": act_row["status"],
                "version": act_payload.get("version", ""),
                "checkpoint_sha": act_payload.get("checkpoint_sha", ""),
                "result": act_row["result"] or "",
                "updated_at": act_row["done_at"] or act_row["created_at"],
            }

        out.append({
            "project": proj,
            "draft": draft_dict,
            "published": published,
            "action": action_state,
        })
    return out


# ---------- since-last-look project briefs (mahler#360) ----------

def brief_seen_key(project):
    return f"{BRIEF_SEEN_PREFIX}{project}"


def _briefs(cfg, led, projects, now):
    """Shipped changes after each project's independently acknowledged event.

    Event ids are the read cursor.  The release-item snapshot is the content,
    so publishing a release can add context but never changes what is unread.
    """
    from .. import releases

    out = []
    for p in projects:
        project = p["name"]
        try:
            seen = int(led.get_kv(brief_seen_key(project)) or 0)
        except (TypeError, ValueError):
            seen = 0
        shipped = led.q(
            "SELECT id, at, number, detail FROM events "
            "WHERE kind='shipped' AND project=? AND id>? ORDER BY id",
            (project, seen))
        entries = []
        times = []
        release_versions = []
        for event in shipped:
            item_row = led.release_item(project, event["number"])
            item = releases._to_release_item(item_row or {})
            if not item.number:
                current = led.item(project, event["number"])
                item = releases.ReleaseItem(
                    project=project, number=event["number"],
                    title=row_get(current, "title", ""))
            detail = _detail_json(event["detail"]) or {}
            if not item.pr:
                item.pr = detail.get("pr")
            release_version = ""
            if item.release_id:
                release_row = led.get_release(project, release_id=item.release_id)
                release_version = row_get(release_row, "version", "")
                if release_version and release_version not in release_versions:
                    release_versions.append(release_version)
            at = parse(event["at"])
            if at:
                times.append(at)
            entries.append({
                "event_id": event["id"],
                "number": item.number,
                "pr": item.pr,
                "title": item.title or f"Issue #{item.number}",
                "summary": item.summary,
                "ref": _ref(project, item.number),
                "url": _issue_url(cfg, project, item.number),
                "pr_url": _pr_url(cfg, project, item.pr) if item.pr else None,
                "release_version": release_version,
                "_item": item,
            })

        by_number = {entry["number"]: entry for entry in entries}
        notes = releases.synthesize_notes([entry["_item"] for entry in entries])

        def grouped(items):
            return [{k: v for k, v in by_number[it.number].items() if k != "_item"}
                    for it in items]

        oldest, newest = (min(times), max(times)) if times else (None, None)
        if oldest and newest:
            if oldest == newest:
                range_label = f"shipped {_brief_when(oldest, now)}"
            else:
                range_label = (f"shipped {_brief_when(oldest, now)}–"
                               f"{_brief_when(newest, now)}")
        else:
            range_label = ""
        out.append({
            "project": project,
            "count": len(entries),
            "seen": seen,
            "upto": entries[-1]["event_id"] if entries else seen,
            "oldest_shipped_at": iso(oldest) if oldest else "",
            "newest_shipped_at": iso(newest) if newest else "",
            "range": range_label,
            "features": grouped(notes.features),
            "fixes": grouped(notes.fixes),
            "other": grouped(notes.other),
            "maintenance": grouped(notes.maintenance),
            "maintenance_count": len(notes.maintenance),
            "release_versions": release_versions,
            "up_to_date": not entries,
        })
    return out


# ---------- capture (mahler#251) ----------

def _capture(cfg, led, project_names, now):
    """The capture composer's state: the dropdown's projects, and captures
    from the last few minutes so the console can show its confirmation."""
    names = set(project_names)
    cutoff = iso(now - timedelta(minutes=CAPTURE_RECENT_MINUTES))
    recent = []
    for r in led.q("SELECT * FROM console_actions WHERE kind='capture' AND created_at >= ?"
                   " ORDER BY id DESC", (cutoff,)):
        if r["project"] not in names:
            continue
        recent.append({"id": r["id"], "project": r["project"],
                       "repo": config.project_policy(cfg, r["project"]).get("repo"),
                       "status": r["status"]})
    return {"projects": project_names, "recent": recent}


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
    if kind == "release_published" and d:
        ver = f"v{d.get('version', '')}" if d.get('version') else "release"
        return "release_published", f"{ref} published {ver}".strip(), False, False
    if kind == "console_release_queued" and d:
        ver = f"v{d.get('version', '')}" if d.get('version') else "release"
        return "release_queued", f"{ref} queued {ver}".strip(), False, False
    text = detail if not d else ", ".join(f"{k}={v}" for k, v in d.items())
    return kind, f"{ref} {text}".strip()[:200], kind in ATTENTION_KINDS, False


def _rows(cfg, led, where="", args=(), limit=EVENTS_SHOWN):
    marks = ",".join("?" * len(QUIET_KINDS))
    reverts = {}
    for action in led.q("SELECT * FROM console_actions WHERE kind='revert' ORDER BY id"):
        payload = _detail_json(action["payload"])
        if action["status"] in ("pending", "done"):
            reverts[(action["project"], payload.get("pr"))] = (
                "revert queued" if action["status"] == "pending" else "revert requested")
    out = []
    for e in led.q(f"SELECT * FROM events WHERE kind NOT IN ({marks}) {where}"
                   f" ORDER BY id DESC LIMIT ?", (*QUIET_KINDS, *args, limit * 4)):
        described = _describe(cfg, e)
        if described is None:
            continue
        kind, text, attention, undoable = described
        revert_status = ""
        if undoable:
            pr = _detail_json(e["detail"]).get("pr")
            revert_status = reverts.get((e["project"], pr), "")
            if not revert_status and led.get_kv(f"revert:{e['project']}:{pr}"):
                revert_status = "revert requested"
            undoable = not revert_status
        url = None
        if e["project"]:
            if e["kind"] in ("pr_opened", "shipped"):
                pr = _detail_json(e["detail"]).get("pr")
                if pr:
                    url = _pr_url(cfg, e["project"], pr)
            elif e["number"]:
                url = _issue_url(cfg, e["project"], e["number"])
        at = parse(e["at"])
        out.append({"id": e["id"], "at": at, "when": _hhmm(at) if at else "",
                    "kind": kind, "text": text, "attention": attention,
                    "undoable": undoable, "revert_status": revert_status, 
                    "project": e["project"], "number": e["number"], "url": url})
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


def _hold_reasons(cfg, holds, pending, hot, now, led=None):
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
                out.append({"text": f"{project} is at its limit of {h['max_parallel']} run(s).",
                            "items": _reason_items(cfg, pending, [(project, i["number"])
                                            for i in pending[project] if i["state"] == "ready"])})
            elif kind == "lease_host":
                out.append({"text": f"{project} waits — its canonical lease host is unavailable.",
                            "items": _reason_items(cfg, pending, [(project, i["number"])
                                            for i in pending[project]])})
            elif kind == "hot_hold" and not any(x["project"] == project for x in hot):
                out.append({"text": f"You have been working in {project}, so new builds there wait "
                                    f"until {pol['hot_hold_minutes']} minutes after you stop."})
    for (role, size), items in routes.items():
        groups = {}
        for h in items:
            for category, names in h["blockers"].items():
                groups.setdefault(category, set()).update(names)
        groups = {k: v for k, v in groups.items() if v}
        active, until = router.peak_state(cfg, led) if led else (False, None)
        if set(groups) == {"size"}:
            text = (f"{len(items)} item(s) need a builder that takes size:{size}, "
                    "and none in the route does.")
        elif active and "peak" in groups and set(groups) <= {"size", "over", "busy", "stale", "peak"}:
            platforms = ", ".join(sorted(groups["peak"]))
            text = (f"{len(items)} {role} item(s) (size:{size}) wait for off-peak hours: "
                    f"{platforms} resumes at {until.astimezone():%H:%M} "
                    f"{router.local_time_label(until)} "
                    f"(in {router.fmt_countdown(until - now)})")
        else:
            summary = "; ".join(f"{label}: {', '.join(sorted(groups[k]))}"
                                for k, label in BLOCKER_LABELS.items() if k in groups)
            text = (f"{len(items)} {role} item(s) have no platform with headroom — "
                    f"{summary or 'no platforms in the route'}.")
        out.append({"text": text,
                    "items": _reason_items(cfg, pending,
                                           [(h["project"], h["number"]) for h in items])})
    for minutes, times in settling.items():
        first = max(1, int((min(times) - now).total_seconds() / 60 + .999))
        out.append({"text": f"{len(times)} item(s) were just sorted and settle for "
                            f"{minutes} minutes before a build starts.",
                    "countdown": f"first in {first}m"})
    if len(deps) > 3:
        reason = {"text": f"{len(deps)} items wait for other issues to close.",
                  "items": _reason_items(cfg, pending,
                                         [(h["project"], h["number"])
                                          for h in deps[:REASON_ITEMS_CAP]])}
        if len(deps) > REASON_ITEMS_CAP:
            reason["more"] = len(deps) - REASON_ITEMS_CAP
        out.append(reason)
    else:
        for h in deps:
            refs = _join(dependency_ref(d, h["project"]) for d in h["on"])
            out.append({"text": f"{_ref(h['project'], h['number'])} waits for {refs} to close.",
                        "items": _reason_items(cfg, pending, [(h["project"], h["number"])])})
    return out


def _launch_breaker_reasons(led, projects, now):
    """Breakers persist independently of candidates and the pre-ship snapshot."""
    reasons = []
    scopes = [("launch_broken", "All projects")]
    scopes.extend((f"launch_broken:{p['name']}", p["name"]) for p in projects)
    for key, scope in scopes:
        broken = json.loads(led.get_kv(key) or "null")
        if broken:
            left = max(timedelta(0), parse(broken["retry_at"]) - now)
            reasons.append({
                "text": f"{scope}: launches paused — {broken['signature']}. "
                        "One canary is tried every 30 minutes.",
                "countdown": f"next attempt in {_dur(left)}"})
    return reasons


def _idle(cfg, led, s, hot, now):
    """Why nothing is running: every reason that is actually binding, each a
    sentence with a countdown when one exists and an escape when you have one."""
    projects = list(config.enabled_projects(cfg))
    reasons = _launch_breaker_reasons(led, projects, now)
    pending = _pending(cfg, led, projects)
    n_pending = sum(len(v) for v in pending.values())
    if not n_pending:
        if reasons:
            return {"headline": "Nothing is running.", "reasons": reasons}
        # with nothing queued, quota and peak hours aren't what holds anything
        waiting = any(led.items(p["name"], ["verifying"]) for p in projects)
        return {"headline": "Nothing is running.", "reasons": [{"text": (
            "Nothing is waiting to start — finished changes are waiting on CI." if waiting
            else "The backlog is empty — nothing to work on.")}]}
    schedule_holds = _schedule_holds(led, now)
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
        reasons.extend(_hold_reasons(cfg, schedule_holds, pending, hot, now, led=led))
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


MODELS_LABEL = "Models"


def models(led, cfg):
    from .. import scorecard
    rows = scorecard.table(led, cfg)
    groups = []
    for role, size in sorted({(r["role"], r["size"] or "") for r in rows}):
        entries = scorecard.ranked(rows, role, size or None)
        groups.append({"title": f"{role.title()} · {size or 'unknown size'}",
                       "rows": [{"text": scorecard.summary(r),
                                 "tone": {"good": "acc", "below": "bad", "unproven": "mut"}[r["status"]],
                                 "details_label": "Run outcomes",
                                 "details": [f'Run {a["run"]} · {a["project"]}#{a["number"]} · '
                                             f'{a["result"]}: {a["why"]}' for a in r["attempts"]]}
                                for r in entries]})
    return {"groups": groups, "empty": "No attempts in this window.",
            "note": f'Last {scorecard.policy(cfg)["window_days"]} days · API-equivalent dollars '
                    '(weighted). Pending and excluded runs do not count. '
                    'Unpriced rows have incomplete cost data. Outcomes can change with later evidence.'}
