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

from .. import api_credits, config, digest as daily_digest, model_availability, presence, review, router, screenshots, warmup
from ..gh import dependency_ref, dependency_target
from ..ledger import iso, parse, row_get
from . import outbox

RECENT_PROBLEMS_SHOWN = 5
PROBLEM_KINDS = ("console_action_failed", "console_slow_request", "console_client_error")
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

STATE_ORDER = ("needs_you", "failed", "working", "verifying", "waiting", "ready", "inbox",
               "parked", "parent")
ROLE_WORDS = {"build": "building", "sort": "sorting", "fix": "fixing CI",
              "review": "reviewing", "design": "designing"}
NUMBER_WORDS = ("No", "One", "Two", "Three", "Four", "Five", "Six", "Seven",
                "Eight", "Nine")
DESIGN_D8 = ("https://github.com/mkny13/mahler/blob/main/DESIGN.md"
             "#d8--platforms-quota-sensing-and-routing")

HOW_A_CHANGE_SHIPS_TITLE = "How a change ships"
HOW_A_CHANGE_SHIPS_STEPS = (
    ("Built", "An agent writes the change on its own branch and runs the project's own checks {verify_clause}before saying it's done."),
    ("PR", "Mahler (not the agent) pushes the branch and opens a pull request: the proposed change, waiting to be merged."),
    ("CI checks", "GitHub runs the project's automated tests on the PR. Green means they all passed. Red: Mahler first retries once if it looks like a flaky test, then starts a fix run with the failure log. If the fix limit runs out, the change is parked and shows under Blocked."),
    ("Review", "A different AI platform from the one that built it reads the change. Problems it finds go back for a fix, then through checks again."),
    ("Merged", "On green checks and a clean review, Mahler merges it into main and closes the issue. No one has to press a button."),
    ("Confirmed live", "Where a project has an automatic post-merge check, a pass marks it verified. Otherwise it counts as done after 14 quiet days with no problem reported."),
)

_STATE_DETAIL = re.compile(r"^(\w+) -> (\w+)(?: \((.*)\))?$", re.S)


SCORECARD_TTL = 300                    # seconds the console reuses a scorecard
SCORECARD_SECTIONS = ("models", "capacity", "browse")  # views that show measured rows


def scorecard_rows(cfg, led):
    """scorecard.table rescans all run history (about a second on the live
    ledger), so the console reuses it until a run starts or ends, the config
    changes, or SCORECARD_TTL passes (mahler#532). The cache hangs off the
    ledger, so separate ledgers never share one."""
    from .. import scorecard
    sig = (tuple(led.con.execute(
        "SELECT count(*), max(id), max(ended_at) FROM runs").fetchone()),
        json.dumps(cfg, sort_keys=True, default=str))
    now = led.now()
    cached = getattr(led, "_console_scorecard", None)
    if cached and cached[0] == sig and 0 <= (now - cached[1]).total_seconds() < SCORECARD_TTL:
        return cached[2]
    rows = scorecard.table(led, cfg)
    led._console_scorecard = (sig, now, rows)
    return rows


def build(cfg, led, stats_range="week", section=None, project_scope=None):
    """The whole console as plain data (see docs/console/design.md, "Shared
    state model"). Client-only state — tab, theme, expanded groups — is not
    here; the page keeps that in the browser. `section` is the view or tab
    being rendered; the model scorecard is only built for the sections that
    show it (None builds everything, for /api/state). `project_scope` filters
    project-shaped views after the shared snapshot is built."""
    measured = section is None or section in SCORECARD_SECTIONS
    measurement_rows = scorecard_rows(cfg, led) if measured else []
    now = led.now()
    projects = config.enabled_projects(cfg)
    project_names = [p["name"] for p in projects]
    priority_boost = led.priority_boost()
    if priority_boost:
        priority_boost = {
            **priority_boost,
            "remaining": _dur(parse(priority_boost["expires_at"]) - now),
        }
    if project_scope not in project_names:
        project_scope = None
    peak = _peak(cfg, led)
    runs = _runs(cfg, led, now)
    active_by_platform = {}
    for run in runs:
        active_by_platform.setdefault(run["platform"], []).append(run)
    quota = _quota(cfg, led, peak, active_by_platform)
    needs = _needs(cfg, led, projects, now)
    uat = _uat(cfg, led, projects)
    backlog = _backlog(cfg, led, projects)
    dep_graph = {g["project"]: _graph(g["items"]) for g in backlog if len(g["items"]) > 1}
    events = _events(cfg, led, project_scope)
    digest = _digest(cfg, led, now, project_scope)
    paused = led.paused()
    hot = _hot_holds(led, projects, now)
    releases_data = _releases(cfg, led, projects, now)
    briefs = _briefs(cfg, led, projects, now)
    s = {
        "paused": paused,
        "discussion_worker": led.get_kv("discussion:worker"),
        "system": ({"label": "PAUSED", "tone": "warn"} if paused else
                   {"label": f"RUNNING · {len(runs)}", "tone": "acc"} if runs else
                   {"label": "IDLE", "tone": "mut"}),
        "peak": peak,
        "priority_boost": priority_boost,
        "runs": runs,
        "needs": needs,
        "conductor_waits": [dict(row, ref=f"{row['project']}#{row['number']}",
                                  url=_issue_url(cfg, row["project"], row["number"]))
                            for row in daily_digest.conductor_waits(led)
                            if row["project"] in project_names],
        "needs_count": sum(n["pending"] is None for n in needs),
        "uat": uat,
        "sessions": _uat_sessions(uat),
        "backlog": backlog,
        "backlog_total": sum(len(g["items"]) for g in backlog),
        "dep_graph": dep_graph,
        "quota": quota,
        "capability_sections": _capability_sections(quota),
        "weekly_quota": (week_calendar(cfg, led, now)
                         if section in (None, "capacity", "browse") else None),
        "capacity": _capacity_line(quota),
        "warmup_learned": _warmup_learned(cfg, led),
        "models": models(led, cfg, measurement_rows) if measured else None,
        "measured_routes": measured_routes(cfg, led, projects, measurement_rows) if measured else [],
        "stats": stats(led, stats_range, project_names),
        "stats_range": _stats_range_key(stats_range),
        "net_backlog": _net_backlog(cfg, led, projects, now),
        "releases": releases_data,
        "releases_suggested": sum(1 for r in releases_data if r["draft"]["is_suggested"]),
        "briefs": briefs,
        "briefs_unread": sum(b["count"] for b in briefs),
        "events": events,
        "recent_problems": _recent_problems(cfg, led, now, project_scope),
        "digest": digest,
        "banners": _banners(cfg, led, paused, quota, hot, now),
        "projects": project_names,
        "capture": _capture(cfg, led, project_names, now),
        "settings": config.settings(cfg),
    }
    s["idle"] = None if runs else _idle(cfg, led, s, hot, now)
    all_projects_landing = {
        "tab": "triage" if needs else "now",
        "view": "needs" if needs else "now",
    }
    s["landing"] = dict(all_projects_landing)
    s["all_projects_landing"] = dict(all_projects_landing)
    s["project_scope"] = project_scope
    s["scope_options"] = _scope_options(led, project_names, runs, needs, backlog,
                                         releases_data, paused,
                                         priority_boost["project"] if priority_boost else None)
    if project_scope:
        s["runs"] = [row for row in runs if row["ref"].startswith(project_scope + "#")]
        s["needs"] = [row for row in needs if row["project"] == project_scope]
        s["needs_count"] = sum(row["pending"] is None for row in s["needs"])
        s["projects"] = [project_scope]
        s["conductor_waits"] = [row for row in s["conductor_waits"]
                                if row["project"] == project_scope]
        s["uat"] = [row for row in uat if row["project"] == project_scope]
        s["sessions"] = _uat_sessions(s["uat"])
        s["backlog"] = [group for group in backlog if group["project"] == project_scope]
        s["backlog_total"] = sum(len(group["items"]) for group in s["backlog"])
        s["dep_graph"] = {name: graph for name, graph in dep_graph.items()
                          if name == project_scope}
        s["releases"] = [row for row in releases_data if row["project"] == project_scope]
        s["releases_suggested"] = sum(
            1 for row in s["releases"] if row["draft"]["is_suggested"])
        s["briefs"] = [row for row in briefs if row["project"] == project_scope]
        s["briefs_unread"] = sum(row["count"] for row in s["briefs"])
        s["stats"] = {project_scope: s["stats"][project_scope]}
        s["net_backlog"]["rows"] = [
            row for row in s["net_backlog"]["rows"] if row["project"] == project_scope]
        s["events"] = [row for row in events if row["project"] == project_scope]
        s["recent_problems"] = [
            row for row in s["recent_problems"] if row["project"] == project_scope]
        s["digest"]["rows"] = [
            row for row in s["digest"]["rows"] if row["project"] == project_scope]
        s["digest"]["count"] = len(s["digest"]["rows"])
        s["banners"] = [row for row in s["banners"]
                        if row.get("project") == project_scope]
        s["landing"] = {"tab": "overview", "view": "overview"}
        s["overview"] = _project_overview_state(cfg, led, project_scope, s, now)
        s["project_overview"] = s["overview"]
    return s


def _scope_options(led, project_names, runs, needs, backlog, releases, paused,
                   boosted_project=None):
    """Expose each enabled project's one-line status from the existing snapshot."""
    options = []
    for name in project_names:
        running = sum(1 for row in runs if row["project"] == name)
        waiting = sum(1 for row in needs if row["project"] == name and row["pending"] is None)
        group = next((row for row in backlog if row["project"] == name), None)
        queued = sum(1 for row in (group or {}).get("items", [])
                     if row["state"] in ("ready", "inbox"))
        release = next((row for row in releases if row["project"] == name), None)
        version = next((row["version"] for row in (release or {}).get("published", [])), "")
        project_paused = led.get_kv(config.project_pause_key(name)) == "1"
        status = ("paused" if paused or project_paused else "running" if running else
                  "waiting on you" if waiting else f"{queued} queued" if queued else "quiet")
        options.append({
            "name": name,
            "status": status,
            "running": running,
            "queued": queued,
            "waiting_on_you": waiting,
            "paused": paused,
            "project_paused": project_paused,
            "current_version": version,
            "priority_boosted": name == boosted_project,
        })
    return options


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


def _variant_label(pconf):
    """'slot · model · effort' for a synthetic platform variant (issue #420,
    D33), or None for an ordinary platform entry."""
    slot = pconf.get("slot")
    if not slot:
        return None
    model = pconf.get("model") or pconf.get("build_model") or pconf.get("sort_model") or ""
    effort = pconf.get("effort") or "default"
    return " · ".join(p for p in (slot, model, effort) if p)


def _model_line(pconf):
    """'claude-opus-4-6-thinking', 'free tier · size s only': the model, the
    plan it runs on (`plan` in the platform's config), and any size limit.
    A synthetic variant instead shows 'slot · model · effort' (issue #420)."""
    variant = _variant_label(pconf)
    if variant:
        parts = [variant]
        if pconf.get("max_size") == "s":
            parts.append("size s only")
        return " · ".join(parts)
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


def _quota_row(cfg, led, peak, name, members, builders, active_by_platform):
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
    for w in router.target_windows(led, name, pconf):
        u = usage.get(w)
        if u is None:
            continue
        soft, _ = router.effective_lines(led, name, pconf, w, burst)
        windows.append({"window": w, "pct": max(min(u["used_pct"], 100), 0),
                        "soft": soft, "resets": router._ts(u.get("resets_at"))})
    worst = max(windows, key=lambda x: x["pct"], default=None)
    active = [run for member in members for run in active_by_platform.get(member, [])]
    row = {"name": name, "members": members, "model": _quota_model(pconf, members),
           "in_use": bool(active), "active_count": len(active),
           "active_refs": [r.get("ref") or f"run {r['id']}" for r in active],
           "state": state, "metered": metered, "claude": claude,
           "builds": any(m in builders for m in members),
           # This is the largest issue size the route is intended to take.
           # An unrestricted or large-only route belongs in the large section;
           # max_size is otherwise the routing contract's exact ceiling.
           "route_size": ROUTE_SIZE_NAMES.get(pconf.get("max_size"), "large"),
           "until": None, "over": [], "soft_pct": 100}
    if model_availability.blocked(led, name, pconf):
        row.update(label="model config", tone="warn", width=100,
                   detail=model_availability.GUIDANCE)
    elif state == "no_credit" and hold_until and hold_until > now:
        row.update(state="no_credit", until=hold_until, label="credits", tone="bad", width=100,
                   detail=f"out of credits, next try {_hhmm(hold_until)}")
    elif state == "soft" and hold_until and hold_until > now:
        row.update(state="hold", until=hold_until, label="hold", tone="warn",
                   width=100, detail=f"on hold until {_hhmm(hold_until)} — {router.hold_label(led, name)}")
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
    if pconf.get("api_credits") and pconf.get("credit_pool"):
        # D41: spend so far, the line in force and the cycle reset, in dollars
        snap = api_credits.snapshot(led, name, pconf)
        tone = {"ok": "acc", "soft": "warn", "stale": "mut"}.get(snap["state"], "bad")
        row.update(detail=api_credits.describe(snap, now), tone=tone,
                   label="burst" if snap["burst"] else row.get("label", "—"),
                   until=parse(snap["cycle_end"]))
    if worst:
        row["soft_pct"] = worst["soft"]
    row["windows"] = [{"window": x["window"], "pct": x["pct"], "soft": x["soft"],
                       "resets": x["resets"],
                       "resets_txt": _when(x["resets"], now) if x["resets"] else None}
                      for x in windows]
    row["peak_held"] = bool(claude and peak and peak["active"])
    row["available"] = row["state"] == "ok" and not row["peak_held"]
    row["unhold_platform"] = (name if len(members) == 1
                              and row["state"] in ("hold", "no_credit") else None)
    return row


def _quota(cfg, led, peak, active_by_platform=None):
    """One gauge per quota group (platforms sharing a login and quota share a
    gauge, D21): the worst window fills the bar, the soft line is the tick."""
    if active_by_platform is None:
        active_by_platform = {}
        for r in led.active_runs():
            run = dict(r)
            run["ref"] = _ref(r["project"], r["number"])
            active_by_platform.setdefault(r["platform"], []).append(run)
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
        row = _quota_row(cfg, led, peak, name, members, builders, active_by_platform)
        row["name"] = _quota_display_name(members)
        row["capabilities"] = [_quota_row(cfg, led, peak, member, [member], builders, active_by_platform)
                               for member in members]
        out.append(row)
    return out


def week_calendar(cfg, led, now):
    """Current local Sun–Sat week, one route per quota group, read-only.

    Weekly cycles repeat every seven elapsed days from the latest reset anchor.
    Localize each reset individually so a DST boundary does not freeze the
    whole week at today's UTC offset. Roll weekly cycles in 604800-second steps.
    Monthly resets are actual dates, never extrapolated as weekly cycles.
    """
    local = now.astimezone()
    sunday = local.date() - timedelta(days=(local.weekday() + 1) % 7)
    start = datetime.combine(sunday, time())
    end = start + timedelta(days=7)
    groups = {}
    for name in _routed(cfg):
        pc = cfg["platforms"][name]
        groups.setdefault(pc.get("quota_group", name), []).append(name)
    routes, unknown, unmetered, outside = [], [], [], []
    for group, members in groups.items():
        pc = cfg["platforms"][members[0]]
        name = _quota_display_name(members)
        if not any(router.is_metered(led, m, cfg["platforms"][m]) for m in members):
            unmetered.append(name)
            continue
        windows = {w for m in members for w in
                   cfg["platforms"][m].get("windows", router.WINDOWS)}
        window = next((w for w in ("weekly", "monthly") if w in windows), None)
        if window is None:
            unmetered.append(name)
            continue
        # Include non-routed aliases too: a probe may have sampled another slot.
        peers = [m for m, conf in cfg["platforms"].items()
                 if conf.get("quota_group", m) == group]
        readings = [u for m in peers if (u := led.usage(m).get(window))]
        latest = max(readings, key=lambda u: u.get("sampled_at") or "", default={})
        reset = router._ts(latest.get("resets_at"))
        if reset is None:
            unknown.append(name)
            continue
        route = {"name": name, "account": config.account_of(pc), "window": window,
                 "reset": reset.timestamp()}
        routes.append(route)
        if window == "monthly" and not start <= reset.astimezone().replace(tzinfo=None) < end:
            outside.append(f"{name} · monthly · {reset.astimezone():%a %d %b %H:%M}")
    grouped = {}
    for route in routes:
        stamp = route["reset"]
        if route["window"] == "weekly":
            # Select the next occurrence, including when the stored anchor is old.
            stamp += ((now.timestamp() - stamp) // 604800) * 604800
            if stamp < now.timestamp():
                stamp += 604800
        reset_local = datetime.fromtimestamp(stamp).astimezone()
        wall = reset_local.replace(tzinfo=None)
        if route["window"] == "monthly" and not (start <= wall < end and stamp >= now.timestamp()):
            continue
        key = (reset_local.date(), reset_local.strftime("%H:%M"))
        grouped.setdefault(key, []).append(route["name"])
    lines = [{"day": day.strftime("%a"), "time": clock, "names": names}
             for (day, clock), names in sorted(
                 grouped.items(), key=lambda item: ((item[0][0].weekday() + 1) % 7, item[0][1]))]
    return {"title": "Weekly resets", "lines": lines,
            "note": f"Next resets · Sun–Sat · local time ({local:%Z}).",
            "now_label": f"Now · {local:%a %H:%M %Z}",
            "unknown": unknown, "unmetered": unmetered, "outside": outside,
            "unknown_label": "Reset unknown", "unmetered_label": "No weekly cycle",
            "outside_label": "Monthly resets outside this week"}


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


def _warmup_learned(cfg, led):
    """mahler#536: one line per warm-up target with its learned warm-up time
    and the reason, from the daily relearn's persisted kv. The learned time is
    a report only — no schedule reads it."""
    try:
        return warmup.console_lines(cfg, led)
    except Exception:
        return []


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
        label = f"trying {_platform_label(cfg, r['platform'])}" if r["explore"] else r["platform"]
        role_label = ROLE_WORDS.get(r["role"], f"{r['role']} run")
        meta = [role_label, label, f"run {r['id']}"]
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
            "id": r["id"], "project": project, "number": n, "ref": _ref(project, n),
            "url": _issue_url(cfg, project, n),
            "title": (item["title"] if item else None) or _ref(project, n),
            "platform": r["platform"], "role": r["role"], "role_label": role_label,
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
            "state": it["state"], "edge": _edge(it["state"]),
            "waited_s": waited.total_seconds(),
            "meta": " · ".join(meta),
        })
    out.sort(key=lambda x: (not x["p1"], -x["waited_s"]))
    return out



def _shipment_capture(led, row, policy):
    """Only final evidence confirmed for this shipment, never a latest-head guess."""
    if not policy.get("screenshot"):
        return None
    try:
        final = json.loads(led.get_kv(
            f"screenshot-final:{row['project']}#{row['number']}") or "{}")
        if (not isinstance(final, dict) or final.get("pr") != row["pr"]
                or not final.get("merge_sha") or final["merge_sha"] != row["sha"]):
            return None
        head = final.get("head")
        capture = json.loads(led.get_kv(
            f"screenshot:{row['project']}:{row['pr']}:{head}") or "{}")
        if (not isinstance(capture, dict) or capture.get("pr") != row["pr"]
                or capture.get("sha") != head or not capture.get("state")):
            return None
        return head, capture["state"]
    except (ValueError, TypeError):
        return None


def _shipment_gallery(led, row, policy):
    capture = _shipment_capture(led, row, policy)
    if capture is None:
        return {}
    head, status = capture
    gallery = {"screenshot_head": head, "screenshots": [],
               "screenshot_status": "Screenshots unavailable."}
    if status == "success":
        try:
            gallery["screenshots"] = [
                dict(entry, url=f"/screenshots/{entry['id']}")
                for entry, _ in screenshots.console_artifacts(
                    row["project"], row["pr"], head)]
            gallery["screenshot_status"] = ""
        except (OSError, ValueError):
            pass
    return gallery


def screenshot_image(cfg, led, identifier):
    """Resolve an opaque ID only through enabled projects' shipment metadata."""
    if not re.fullmatch(r"[0-9a-f]{64}", identifier):
        return None
    for policy in config.enabled_projects(cfg):
        for row in led.shipment_history(policy["name"]):
            capture = _shipment_capture(led, row, policy)
            if capture is None or capture[1] != "success":
                continue
            # Match the bounded manifest slot before reading any PNG bytes.
            if identifier not in {
                    screenshots.artifact_id(row["project"], row["pr"], capture[0], i)
                    for i in range(screenshots.MAX_SCREENSHOTS)}:
                continue
            try:
                for entry, data in screenshots.console_artifacts(
                        row["project"], row["pr"], capture[0]):
                    if entry["id"] == identifier:
                        return data
            except (OSError, ValueError):
                continue
    return None


def _uat(cfg, led, projects):
    """Newest shipments, with optional hints and failure-report status."""
    pols = {p["name"]: p for p in projects}
    queued = {(r["project"], r["number"]): "uat_fail"
              for r in led.pending_actions("uat_fail")}
    out = []
    for row in (row for project in pols for row in led.shipment_history(project)):
        project, n = row["project"], row["number"]
        shipped = parse(row["shipped_at"])
        minutes = max(0, int((led.now() - shipped).total_seconds() // 60)) if shipped else 0
        age = (f"{minutes // 1440}d" if minutes >= 1440 else
               f"{minutes // 60}h" if minutes >= 60 else f"{minutes}m")
        meta = [f"merged {age} ago" if shipped else "merged"]
        if row["pr"]:
            meta.append(f"PR #{row['pr']}")
        if row["verdict"] == "fail":
            meta.append("Defect reported")
        if row["bug"] and row["verdict"] != "fail":
            meta.append("Defect reported")
        kind = row["completion_kind"]
        if kind:
            meta.append("Quiet period completed" if kind == "quiet" else "Automated completion" if kind == "smoke" else "Completion evidence recorded")
        elif row["verdict"] == "pass":
            meta.append("Historical pass recorded")
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
        shared = bool(uat_url and "{number}" not in uat_url and "{pr}" not in uat_url)
        reported = bool(row["bug"] or row["verdict"] == "fail")
        pending = queued.get((project, n))
        waiting = not (reported or pending or kind or row["verdict"] == "pass")
        out.append({
            **_shipment_gallery(led, row, pols[project]),
            "shared_link": link if shared else None,
            "pr_link": _pr_url(cfg, project, row["pr"]) if row["pr"] else None,
            "pr_label": f"PR #{row['pr']}" if row["pr"] else None,
            "project": project, "number": n, "ref": _ref(project, n),
            "url": _issue_url(cfg, project, n),
            "title": row["title"] or _ref(project, n),
            "meta": " · ".join(meta), "check": "; ".join(needs),
            "link": link, "link_label": link_label,
            "pending": pending, "reported": reported, "edge": "warn" if waiting else "",
            "sha": row["sha"], "shipped_at": row["shipped_at"], "completion_kind": kind,
        })
    return out


def _uat_sessions(uat):
    """One newest-first shipment history per enabled project."""
    groups = {}
    for u in uat:
        groups.setdefault(u["project"], []).append(u)
    return [{"title": project, "project": project, "changes": rows,
             "link": rows[0].get("shared_link"), "link_label": rows[0]["link_label"]}
            for project, rows in sorted(groups.items())]


# ---------- backlog ----------

def _state_tone(state):
    return ("bad" if state in ATTENTION_STATES else
            "acc" if state in ("working", "verifying", "waiting") else "mut")


def _edge(state):
    """The urgency edge on a card or row: red when it failed, amber when it waits."""
    return "bad" if state == "failed" else "warn" if state == "needs_you" else ""


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
        live = sum(1 for i in items if i["state"] in ("working", "verifying", "waiting"))
        you = sum(1 for i in items if i["state"] in ATTENTION_STATES)
        open_numbers = {i["number"] for i in items}
        # a queued capture shows as a placeholder inbox row until the tick
        # creates the issue and sync() pulls in the real item (mahler#251)
        rows = [{"ref": None, "url": None, "number": None, "pr": None, "pr_url": None,
                 "title": outbox.capture_title(json.loads(r["payload"])["text"]),
                 "p": "p2", "p1": False, "priority": 2, "state": "inbox", "tone": "mut",
                 "edge": "", "parent": None, "depends": []}
                for r in pending.get(name, [])]
        rows += [{"ref": _ref(name, i["number"]), "url": _issue_url(cfg, name, i["number"]),
                  "number": i["number"],
                  "pr": i["pr"], "pr_url": _pr_url(cfg, name, i["pr"]) if i["pr"] else None,
                  "title": i["title"] or "", "p": f"p{i['priority']}",
                  "p1": i["priority"] == 1, "priority": i["priority"],
                  "state": i["state"].replace("_", "-"), "tone": _state_tone(i["state"]),
                  "edge": _edge(i["state"]),
                  "reason": (_question(led, name, i["number"], "parked", i)
                             if i["state"] == "parked" else None),
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
    return {"nodes": nodes, "edges": edges, "mermaid": _mermaid(nodes, edges)}


def _mermaid(nodes, edges):
    """Pure browser diagram source; never interpolate issue text as syntax."""
    by_number = {n["number"]: n for n in nodes}
    children = {n: [] for n in by_number}
    parents = {}
    related = set()
    for edge in edges:
        a, b = edge["from"], edge["to"]
        if a not in by_number or b not in by_number:
            continue
        related.update((a, b))
        if edge["kind"] == "parent" and b not in parents:
            # A malformed parent cycle must not recurse forever.
            ancestor = a
            while ancestor in parents and ancestor != b:
                ancestor = parents[ancestor]
            if ancestor != b:
                parents[b] = a
                children[a].append(b)

    def title(node):
        # Remove syntax/HTML delimiters, including entity and Markdown escapes.
        text = " ".join(node.get("title", "").split())[:40]
        return "".join(c for c in text if c not in '\"<>#;&`\\')

    lines = ["flowchart LR"]
    def emit(number, indent="  "):
        node = by_number[number]
        if children[number]:
            lines.append(f'{indent}subgraph p{number}["#{number} {title(node)}"]')
            indent += "  "
        # Trackers retain a linked, coloured node as well as their enclosing box.
        lines.append(f'{indent}n{number}["#{number}<br/>{title(node)}"]')
        for child in children[number]:
            emit(child, indent)
        if children[number]:
            lines.append(indent[:-2] + "end")

    for number in by_number:
        if number in related and number not in parents:
            emit(number)
    free = [n for n in by_number if n not in related]
    if free:
        lines.extend(['  subgraph free["No dependencies"]', '    direction TB'])
        for number in free:
            emit(number, "    ")
        lines.append("  end")
    for edge in edges:
        if (edge["kind"] == "depends" and edge["from"] in by_number
                and edge["to"] in by_number):
            lines.append(f'  n{edge["from"]} --> n{edge["to"]}')
    for tone, color in (("bad", "#a8180f"), ("acc", "#1256c4"), ("mut", "#4a5158")):
        lines.append(f"  classDef {tone} fill:transparent,stroke:{color}")
    for number, node in by_number.items():
        tone = node.get("tone", "mut")
        lines.append(f'  class n{number} {tone if tone in ("bad", "acc", "mut") else "mut"}')
        url = node.get("url") or ""
        # Links are generated by the server; keep malformed inputs out of loose mode.
        if url.startswith("https://") and not any(c in url for c in '\"<>\n\r'):
            lines.append(f'  click n{number} "{url}" _blank')
    return "\n".join(lines)


# ---------- releases (DESIGN D31, mahler#359) ----------

RELEASES_HISTORY_CAP = 10
DRAFT_ITEMS_CAP = 20


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

        # Ledger draft items are oldest first; keep the full notes for publication.
        visible_items = list(reversed(draft.items))[:DRAFT_ITEMS_CAP]
        visible_notes = releases.synthesize_notes(visible_items)
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
            "features": [_item_dict(it) for it in visible_notes.features],
            "fixes": [_item_dict(it) for it in visible_notes.fixes],
            "other": [_item_dict(it) for it in visible_notes.other],
            "maintenance": [_item_dict(it) for it in visible_notes.maintenance],
            "items": [_item_dict(it) for it in visible_items],
            "items_total": len(draft.items),
            "item_numbers": [it.number for it in draft.items],
        }

        pub_rows = [r for r in led.list_releases(proj) if r["state"] == "published"]
        published = []
        for r in pub_rows[:RELEASES_HISTORY_CAP]:
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
            "published_total": len(pub_rows),
            "published_omitted": len(pub_rows) - len(published),
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


def _recent_problems(cfg, led, now, project=None):
    marks = ",".join("?" for _ in PROBLEM_KINDS)
    where = f"AND kind IN ({marks}) AND at >= ? AND at <= ?"
    args = (*PROBLEM_KINDS, iso(now - timedelta(hours=24)), iso(now))
    if project:
        where += " AND project=?"
        args += (project,)
    rows = _rows(cfg, led, where, args,
                 limit=RECENT_PROBLEMS_SHOWN)
    for row in rows:
        row["text"] = " ".join(row["text"].split())
    return rows


def _events(cfg, led, project=None):
    return _rows(cfg, led, "AND project=?" if project else "",
                 (project,) if project else ())


def _digest(cfg, led, now, project=None):
    """Events since you last marked the stream seen — the "3 new" chip."""
    try:
        seen = int(led.get_kv(SEEN_KEY) or 0)
    except (TypeError, ValueError):
        seen = 0
    where = "AND id > ?"
    args = (seen,)
    if project:
        where += " AND project=?"
        args += (project,)
    rows = [r for r in _rows(cfg, led, where, args, limit=DIGEST_SHOWN)
            if r["at"] and now - r["at"] < timedelta(hours=DIGEST_HOURS)]
    return {"count": len(rows), "rows": rows,
            "upto": max((r["id"] for r in rows), default=seen)}


# ---------- net backlog (mahler#1104) ----------

NET_BACKLOG_TITLE = "Net backlog · opened − closed"
NET_BACKLOG_EXPLAIN = ("+ means more issues opened than closed — the backlog grew. "
                       "− means more closed than opened — it shrank. Measured from "
                       "Mahler's hourly snapshots of each project's open-issue count.")
NET_BACKLOG_EMPTY = "History starts today; figures appear after the next hourly snapshot."
NET_BACKLOG_TREND_CAPTION = "Open issues"
NET_BACKLOG_TREND_LABEL = "Open issues over"


def _signed(n):
    return f"+{n}" if n > 0 else f"\u2212{-n}" if n < 0 else "0"


def _span(covered):
    if covered >= timedelta(days=1):
        return f"{covered.days}d"
    return f"{int(covered.total_seconds() // 3600)}h"


def _trend_span(series):
    covered = parse(series[-1]["at"]) - parse(series[0]["at"])
    days = covered.total_seconds() / timedelta(days=1).total_seconds()
    value = f"{days:g}" if days.is_integer() else f"{days:.2f}".rstrip("0").rstrip(".")
    return f"{value} day" if days == 1 else f"{value} days"


def _net_backlog(cfg, led, projects, now):
    """Per project: the 24h/7d/30d net backlog, read from ledger snapshots only."""
    rows = []
    for p in projects:
        net = led.backlog_net(p["name"], now=now)
        series = led.backlog_series(p["name"], since=iso(now - timedelta(days=30)))
        if len(series) < 2:
            series = []
            caption = trend_label = None
        else:
            span = _trend_span(series)
            first, last = series[0]["count"], series[-1]["count"]
            low = min(point["count"] for point in series)
            high = max(point["count"] for point in series)
            caption = (f"{NET_BACKLOG_TREND_CAPTION} · last {span}: "
                       f"{first} → {last}")
            trend_label = (f"{NET_BACKLOG_TREND_LABEL} the last {span}: "
                           f"{first} to {last}, low {low}, high {high}")
        label = p.get("scope_label") if p.get("scope") == "label" else None
        windows = []
        for key in ("24h", "7d", "30d"):
            w = (net or {}).get(key)
            if w is None:
                continue
            tone = "bad" if w["net"] > 0 else "good" if w["net"] < 0 else "mut"
            windows.append({"key": key, "net": w["net"], "text": _signed(w["net"]),
                            "full": w["full"], "tone": tone,
                            "note": None if w["full"] else
                            f"only {_span(w['covered'])} of history"})
        rows.append({"project": p["name"], "windows": windows,
                     "empty": None if windows else NET_BACKLOG_EMPTY,
                     "series": series, "trend_caption": caption,
                     "trend_label": trend_label,
                     "scope": (f"Counts only issues labelled {label}, the ones Mahler manages."
                               if label else None)})
    return {"title": NET_BACKLOG_TITLE, "explain": NET_BACKLOG_EXPLAIN, "rows": rows}


# ---------- banners and idle reasons ----------

def _hot_holds(led, projects, now):
    """Projects under a hot hold (D6 layer 2): a Claude session active there
    in the last hot_hold_minutes, so no new build starts."""
    out = []
    for p in projects:
        if not p.get("hot_hold") or not p.get("path"):
            continue
        activity = presence.hot_hold_activity(
            led, p["name"], p["path"],
            timedelta(minutes=p["hot_hold_minutes"]), now=now, details=True)
        last = activity.at if activity else None
        if last and now - last < timedelta(minutes=p["hot_hold_minutes"]):
            out.append({"project": p["name"], "ago": _mins(now - last),
                        "directory": activity.directory, "fallback": activity.fallback,
                        "hold_minutes": p["hot_hold_minutes"],
                        "until": last + timedelta(minutes=p["hot_hold_minutes"])})
    return out


def _hot_hold_text(hold):
    directory = str(hold.get("directory") or hold["project"]).rstrip("/").rsplit("/", 1)[-1]
    signal = "last activity; transcript unreadable" if hold.get("fallback") else "last edit"
    return (f"Claude session in {directory} ({signal} {hold['ago']}m ago). "
            f"New builds in {hold['project']} wait until {hold['hold_minutes']} minutes "
            "after that activity.")


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
        out.append({"kind": f"HOT HOLD · {h['project'].upper()}", "tone": "warn",
                    "text": _hot_hold_text(h) + " Work in flight continues.",
                    "act": "end_session", "action": "End session", "project": h["project"]})
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


def _platform_label(cfg, name):
    """A variant's 'slot · model · effort' in idle-reason text (issue #420),
    else the platform name as-is."""
    return _variant_label(cfg["platforms"].get(name, {})) or name


def _current_schedule_holds(holds, pending):
    """Keep snapshot order and duplicates, but drop projects/items no longer queued."""
    numbers = {project: {i["number"] for i in items} for project, items in pending.items()}
    for h in holds:
        queued = numbers.get(h.get("project"))
        if queued and (h.get("number") is None or h["number"] in queued):
            yield h


def _project_hold_reason(cfg, h, pending, hot, led):
    """Project-wide holds use current leases and session state."""
    kind, project = h.get("kind"), h.get("project")
    pol = config.project_policy(cfg, project)
    if kind == "capacity":
        labels = []
        for holder in h.get("holders", []):
            lease = led.lease(project, holder["number"]) if led else None
            if lease and lease["capacity"]:
                labels.append(router.lease_label(led, lease))
        if h.get("holders") and not labels:
            return None  # the recorded holder no longer consumes a run slot
        detail = " Held by " + "; ".join(labels) + "." if labels else ""
        return {"text": f"{project} is at its limit of {h['max_parallel']} run(s)." + detail,
                "items": _reason_items(cfg, pending, [(project, i["number"])
                                        for i in pending[project] if i["state"] == "ready"])}
    elif kind == "lease_host":
        return {"text": f"{project} waits — its canonical lease host is unavailable.",
                "items": _reason_items(cfg, pending, [(project, i["number"])
                                        for i in pending[project]])}
    elif kind == "hot_hold" and not any(x["project"] == project for x in hot):
        return {"text": f"You have been working in {project}, so new builds there wait "
                        f"until {pol['hot_hold_minutes']} minutes after you stop."}


def _route_hold_reason(cfg, role, size, items, pending, now, led):
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
        platforms = ", ".join(sorted(_platform_label(cfg, n) for n in groups["peak"]))
        text = (f"{len(items)} {role} item(s) (size:{size}) wait for off-peak hours: "
                f"{platforms} resumes at {until.astimezone():%H:%M} "
                f"{router.local_time_label(until)} "
                f"(in {router.fmt_countdown(until - now)})")
    else:
        summary = "; ".join(f"{label}: {', '.join(sorted(_platform_label(cfg, n) for n in groups[k]))}"
                            for k, label in BLOCKER_LABELS.items() if k in groups)
        text = (f"{len(items)} {role} item(s) have no platform with headroom — "
                f"{summary or 'no platforms in the route'}.")
    return {"text": text,
            "items": _reason_items(cfg, pending,
                                   [(h["project"], h["number"]) for h in items])}


def _settling_hold_reasons(settling, now):
    out = []
    for minutes, times in settling.items():
        first = max(1, int((min(times) - now).total_seconds() / 60 + .999))
        out.append({"text": f"{len(times)} item(s) were just sorted and settle for "
                            f"{minutes} minutes before a build starts.",
                    "countdown": f"first in {first}m"})
    return out


def _dependency_hold_reasons(cfg, deps, pending):
    out = []
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


def _hold_reasons(cfg, holds, pending, hot, now, led=None):
    """Immediate holds first, then routes, settling and dependencies in encounter order."""
    out, routes, settling, deps = [], {}, {}, []
    seen = set()
    for h in _current_schedule_holds(holds, pending):
        kind, project = h.get("kind"), h.get("project")
        if kind == "no_platform":
            routes.setdefault((h["role"], h["size"]), []).append(h)
        elif kind == "settling":
            until = router._ts(h.get("until"))
            if until and until > now:
                minutes = config.project_policy(cfg, project)["settle_minutes"]
                settling.setdefault(minutes, []).append(until)
        elif kind == "deps":
            deps.append(h)
        elif kind in ("area", "files"):
            overlap = f"area:{h['area']}" if kind == "area" else ", ".join(h["files"])
            ref = _ref(project, h.get("number"))
            out.append({"text": f"{ref} waits — {overlap} already in progress."})
        elif (kind, project) not in seen:
            seen.add((kind, project))
            reason = _project_hold_reason(cfg, h, pending, hot, led)
            if reason:
                out.append(reason)
    out.extend(_route_hold_reason(cfg, role, size, items, pending, now, led)
               for (role, size), items in routes.items())
    out.extend(_settling_hold_reasons(settling, now))
    out.extend(_dependency_hold_reasons(cfg, deps, pending))
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


def capacity_wait_text(led, project, item, now):
    """Shared console/CLI copy for the current review or fix capacity wait."""
    if row_get(item, "state") != "verifying" or not row_get(item, "pr"):
        return ""

    def read(key):
        try:
            value = json.loads(led.get_kv(key) or "null")
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError):
            return {}

    number = item["number"]
    ci = read(f"ci:{project}#{number}:{item['pr']}")
    rev_status = review.effective_for_item(led, project, number, pr=item["pr"], sha=ci.get("sha"))
    if ci.get("state") == "red" or (ci.get("state") == "green"
                                         and rev_status == "fail"):
        role, key = "fix", "reviewfix-status"
    elif ci.get("state") == "green" and rev_status in (None, "pending", "unknown", "required"):
        role, key = "review", "review-wait"
    else:
        return ""
    wait = read(f"{key}:{project}#{number}")
    if wait.get("state") != "capacity_wait":
        return ""
    if any(r["project"] == project and r["number"] == number
           and r["role"] in ("fix", "review") for r in led.active_runs()):
        return ""
    try:
        retry = parse(wait.get("retry_at"))
    except (AttributeError, TypeError, ValueError):
        retry = None
    if retry and retry > now:
        timing = f"retry around {retry.astimezone():%b %d %H:%M %Z}"
    elif retry:
        timing = "retry due; will re-check"
    else:
        timing = "retry time unknown; will re-check"
    return f"{role} waiting for capacity; {timing}"


def _verification_wait(led, project, item, now):
    """Describe the conductor's current wait for a verifying item.

    The ship pass records the head/check state and its first-seen time in KV;
    review and merge-queue state use the same ledger signals that _watch_pr
    consumes.  Keep the legacy state_changed_at fallback for items created by
    older daemons whose first CI observation predates this marker.
    """
    number, pr = item["number"], row_get(item, "pr")
    if not pr:
        return ".", 0

    def read(key):
        try:
            return json.loads(led.get_kv(key) or "null") or {}
        except (TypeError, ValueError):
            return {}

    ci = read(f"ci:{project}#{number}:{pr}")
    ci_state = ci.get("state")
    since = parse(ci.get("since")) or parse(row_get(item, "state_changed_at"))
    elapsed = _mins(now - since) if since else 0
    capacity = capacity_wait_text(led, project, item, now)
    if capacity:
        return f" — {capacity}.", None
    if ci_state in (None, "pending"):
        return (f" — CI has been pending {elapsed} minute{'s' if elapsed != 1 else ''}.",
                elapsed)
    if ci_state == "red":
        return " — CI failed; waiting for a fix run.", elapsed

    mergeability = read(f"mergeability:{project}#{number}:{pr}")
    if (mergeability and mergeability.get("sha") == ci.get("sha")
            and mergeability.get("base") == ci.get("base")):
        detail = f"; {mergeability['error']}" if mergeability.get("error") else ""
        return f" — mergeability unknown; waiting for GitHub{detail}.", None

    rev_status = review.effective_for_item(led, project, number, pr=pr, sha=ci.get("sha"))
    # Run records, not legacy running-only KV values, own the live fix signal.
    fix_running = any(r["number"] == number and r["role"] == "fix"
                      for r in led.active_runs(project))
    needs_review = rev_status in ("pending", "fail")
    if not needs_review:
        labels = json.loads(row_get(item, "labels", "[]"))
        size = next((x.split(":", 1)[1] for x in labels if x.startswith("size:")), None)
        needs_review = (size in ("m", "l") or router.risk_min_tier(row_get(item, "title", "")) > 0
                        or led.get_kv(f"reviewed-pr:{project}#{number}") == str(pr))
    if needs_review and rev_status in (None, "pending", "unknown", "required"):
        return " — waiting for the independent review.", elapsed
    if rev_status == "fail":
        if fix_running:
            return " — review failed; waiting for a fix run.", elapsed
        fix_wait = read(f"reviewfix-status:{project}#{number}")
        if fix_wait.get("reason") and "no eligible route" in fix_wait["reason"]:
            tier = fix_wait.get("tier") or 1
            return (f" — review failed; waiting for a fix run (no platform available "
                    f"at tier {tier}).", elapsed)
        return " — review failed; waiting for a fix run.", elapsed
    if led.get_kv(f"queue:{project}#{number}:{pr}"):
        return " — green, waiting for its turn to merge.", elapsed
    return " — green, waiting for its turn to merge.", elapsed


def _quota_idle_reasons(quota, now):
    reasons = []
    # metered platforms over a soft line, grouped by window and line
    over = {}
    for q in quota:
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
    stale = [q["name"] for q in quota if q["state"] == "stale"]
    if stale:
        reasons.append({"text": f"{_join(stale)} {'has' if len(stale) == 1 else 'have'} no "
                                f"fresh quota reading, and Mahler counts unknown as over the "
                                f"line."})
    backoff = [q for q in quota if q["state"] == "backoff"]
    if backoff:
        reasons.append({
            "text": f"{_join(q['name'] for q in backoff)} "
                    f"{'is' if len(backoff) == 1 else 'are'} backing off after quota errors.",
            "countdown": " · ".join(f"{q['name']} {_dur(q['until'] - now)}"
                                    for q in backoff if q["until"]) or None,
            "action": "Clear backoff", "act": "clear_backoff",
            "platforms": [q["name"] for q in backoff]})
    no_credit = [q for q in quota if q["state"] == "no_credit"]
    if no_credit:
        reasons.append({
            "text": f"{_join(q['name'] for q in no_credit)} "
                    f"{'is' if len(no_credit) == 1 else 'are'} out of credits.",
            "countdown": " · ".join(f"{q['name']} next try {_dur(q['until'] - now)}"
                                    for q in no_credit if q["until"]) or None,
            "action": "Unhold", "act": "unhold",
            "platforms": [q["unhold_platform"] for q in no_credit
                          if q.get("unhold_platform")]})
    holds = [q for q in quota if q["state"] == "hold"]
    if holds:
        reasons.append({
            "text": f"{_join(q['name'] for q in holds)} "
                    f"{'is' if len(holds) == 1 else 'are'} on hold after a run never started.",
            "countdown": " · ".join(f"{q['name']} {_dur(q['until'] - now)}" for q in holds),
            "action": "Unhold", "act": "unhold",
            "platforms": [q["name"] for q in holds]})
    return reasons


def _project_idle_reasons(cfg, led, projects, pending, schedule_holds, now):
    reasons = []
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
            text = f"New builds wait for {_ref(name, v['number'])} to merge"
            wait_text, pending_m = _verification_wait(led, name, v, now)
            text += wait_text if v["pr"] else "."
            timeout = (p.get("verify_timeout_minutes", 60) - pending_m
                       if pending_m is not None else 0)
            reason = {"text": text,
                      "countdown": f"verify timeout in {_dur(timedelta(minutes=timeout))}"
                      if v["pr"] and timeout > 0 else None}
            if v["pr"]:
                reason.update(action=f"Open PR #{v['pr']}", href=_pr_url(cfg, name, v["pr"]))
            reasons.append(reason)
    return reasons


def _hot_idle_reasons(hot, pending, now):
    reasons = []
    for h in hot:
        if any(i["state"] == "ready" for i in pending.get(h["project"], [])):
            reasons.append({
                "text": _hot_hold_text(h),
                "countdown": f"{_dur(h['until'] - now)} left",
                "act": "end_session", "action": "End session", "project": h["project"]})
    return reasons


_ITEM_REASON = re.compile(r"^([\w.-]+#\d+) waits\b")
_COUNT_WORDS = ("", "One", "Two", "Three", "Four")


def _binding_reasons(reasons):
    """Collapse reasons that are facets of one binding (several per-item
    reasons for the same item, e.g. area and files) into one entry each."""
    seen, out = set(), []
    for r in reasons:
        m = _ITEM_REASON.match(r.get("text", ""))
        if m:
            if m.group(1) in seen:
                continue
            seen.add(m.group(1))
        out.append(r)
    return out


def _idle_headline(reasons):
    n = len(_binding_reasons(reasons))
    if not n:
        return "Nothing is running."
    word = _COUNT_WORDS[n] if n < len(_COUNT_WORDS) else "A few" if n < 7 else "Several"
    return (f"Nothing is running. {word} thing{'s are' if n != 1 else ' is'} "
            "holding it:")


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
    reasons.extend(_quota_idle_reasons(s["quota"], now))
    reasons.extend(_project_idle_reasons(cfg, led, projects, pending, schedule_holds, now))
    reasons.extend(_hot_idle_reasons(hot, pending, now))
    if schedule_holds is not None:
        reasons.extend(_hold_reasons(cfg, schedule_holds, pending, hot, now, led=led))
    if not reasons and schedule_holds is None:
        reasons.append({"text": f"{n_pending} item(s) queued, but no platform has "
                                f"headroom for them right now."})
    return {"headline": _idle_headline(reasons), "reasons": reasons}


MODELS_LABEL = "Models"

# Shown in both headers after a failed 30s refresh (mahler#1108); console.js
# fills {time} with when the page last updated and hides it on the next success.
STALE_NOTE = "Couldn't refresh · showing {time}"


def measured_routes(cfg, led, projects, rows):
    """Project/role/size routes cannot be represented by a single quota-pool order."""
    result = []
    from ..tick import busy_platforms
    bursts = router.all_bursts(cfg, led)
    busy = busy_platforms(cfg, led.active_runs())
    for pol in projects:
        if router.routing_mode(cfg, pol) != "measured":
            continue
        accts = config.accounts_of(pol)
        for role in ("plan", "sort", "build", "fix", "review"):
            for size in ("s", "m", "l"):
                if config.account_mode_of(pol) == "priority":
                    order = router.candidates_for_priority(cfg, role, accts, pol.get("routing") or {})
                elif config.account_mode_of(pol) == "equal":
                    order = router.candidates_for_accounts(cfg, role, accts)
                else:
                    order = [n for a in accts for n in router.candidates(cfg, role, account=a)]
                order = router.measured_order(cfg, order, rows, role, size, bursts)
                if not order:
                    continue
                pick, _ = router.pick_for_project(
                    cfg, led, pol, role, size=size, busy=busy, burst_lines=bursts, scorecard_rows=rows)
                reason = router.pick_reason(cfg, rows, role, size, pick) if pick else "no headroom"
                result.append(f'{pol["name"]} · {role} · {size}: '
                              + " → ".join(order) + f". Unpinned preference: {reason}")
    return result


def models(led, cfg, rows=None):
    from .. import scorecard
    rows = scorecard.table(led, cfg) if rows is None else rows
    groups = []
    for role, size in sorted({(r["role"], r["size"]) for r in rows},
                             key=lambda pair: (pair[0], pair[1] or "", pair[1] is not None)):
        entries = scorecard.ranked(rows, role, size)
        groups.append({"title": f"{role.title()} · {size or 'unknown size'}",
                       "rows": [{"text": scorecard.summary(r),
                                 "tone": {"good": "acc", "below": "bad", "unproven": "mut"}[r["status"]],
                                 "details_label": "Run outcomes",
                                 "details": [f'Run {a["run"]} · {a["project"]}#{a["number"]} · '
                                             f'{a["result"]}: {a["why"]}' for a in reversed(r["attempts"][-20:])]
                                            + ([f'…and {len(r["attempts"]) - 20} earlier runs']
                                               if len(r["attempts"]) > 20 else [])}
                                for r in entries]})
    return {"groups": groups, "empty": "No attempts in this window.",
            "note": f'Last {scorecard.policy(cfg)["window_days"]} days · API-equivalent dollars '
                    '(weighted). Pending and excluded runs do not count. '
                    'Unpriced rows have incomplete cost data. Outcomes can change with later evidence.'}


# ---------- project overview ----------

def _how_a_change_ships(policy):
    verify = policy.get("verify")
    verify_clause = f"(for this project: {verify}) " if verify else ""
    steps = []
    for title, template in HOW_A_CHANGE_SHIPS_STEPS:
        text = template.replace("{verify_clause}", verify_clause).strip()
        steps.append({"title": title, "text": text})
    return {"title": HOW_A_CHANGE_SHIPS_TITLE, "steps": steps}


def _shipping_rows(cfg, led, project, s, now):
    rows = []
    seen_numbers = set()

    # 1. In-flight built-but-unfinished changes
    candidates = led.items(project, states=["verifying", "parked", "failed"])
    for it in candidates:
        n = it["number"]
        pr = row_get(it, "pr")
        st = it["state"]
        has_prior_build = bool(led.last_run(project, n, roles=("build",)))
        if st != "verifying" and not pr and not has_prior_build:
            continue
        seen_numbers.add(n)
        title = row_get(it, "title") or _ref(project, n)

        built_stage = {"key": "built", "label": "Built", "status": "ok", "text": ""}

        if not pr:
            pr_stage = {"key": "pr", "label": "PR", "status": "now", "text": ""}
            ci_stage = {"key": "ci", "label": "CI checks", "status": "", "text": ""}
            rev_stage = {"key": "review", "label": "Review", "status": "", "text": ""}
            merged_stage = {"key": "merged", "label": "Merged", "status": "", "text": ""}
            confirmed_stage = {"key": "confirmed", "label": "Confirmed live", "status": "", "text": ""}
            change_subtitle = "built · waiting for PR"
        else:
            pr_stage = {"key": "pr", "label": "PR", "status": "ok", "text": ""}
            ci_data = {}
            ci_raw = led.get_kv(f"ci:{project}#{n}:{pr}")
            if ci_raw:
                try:
                    ci_data = json.loads(ci_raw) or {}
                except (TypeError, ValueError):
                    ci_data = {}
            ci_state = ci_data.get("state")
            failed_check = (ci_data.get("failed_check") or ci_data.get("check_name") or
                            ci_data.get("check"))
            progress = ci_data.get("progress")
            if not progress and "completed" in ci_data and "total" in ci_data:
                progress = f"{ci_data['completed']} of {ci_data['total']}"

            rev_data = {}
            rev_raw = led.get_kv(f"review:{project}#{n}")
            if rev_raw:
                try:
                    rev_data = json.loads(rev_raw) or {}
                except (TypeError, ValueError):
                    rev_data = {}
            rev_status = review.effective_for_item(led, project, n, pr=pr, sha=ci_data.get("sha"))
            if not rev_status or rev_status == "unknown":
                rev_status = rev_data.get("verdict")
            rev_plat = rev_data.get("platform")
            active_rev = next((r for r in led.active_runs(project)
                               if r["number"] == n and r["role"] == "review"), None)
            if active_rev:
                rev_plat = active_rev["platform"]
            active_fix = next((r for r in led.active_runs(project)
                               if r["number"] == n and r["role"] == "fix"), None)

            merged_stage = {"key": "merged", "label": "Merged", "status": "", "text": ""}
            confirmed_stage = {"key": "confirmed", "label": "Confirmed live", "status": "", "text": ""}

            if ci_state == "red":
                ci_stage = {
                    "key": "ci", "label": "CI checks", "status": "bad",
                    "text": f"✕ {failed_check}" if failed_check else "failed",
                }
                rev_stage = {"key": "review", "label": "Review", "status": "", "text": ""}
                why = row_get(it, "reason") or _question(led, project, n, it["state"], it)
                if it["state"] == "parked":
                    change_subtitle = f"PR #{pr} · {why if why else 'checks failed · parked'}"
                else:
                    change_subtitle = f"PR #{pr} · CI failed"
            elif ci_state in (None, "pending"):
                since = parse(ci_data.get("since")) if ci_data.get("since") else None
                elapsed = _mins(now - since) if since else None
                ci_stage = {
                    "key": "ci", "label": "CI checks", "status": "now",
                    "text": progress or "checks running",
                }
                rev_stage = {"key": "review", "label": "Review", "status": "", "text": ""}
                sub = f"checks running {elapsed}m" if elapsed is not None else "checks running"
                change_subtitle = f"PR #{pr} · {sub}"
            elif ci_state in ("green", "none"):
                opt = ci_data.get("non_required_failures") or ci_data.get("optional_failures")
                if opt:
                    names = ", ".join(opt) if isinstance(opt, list) else str(opt)
                    ci_stage = {
                        "key": "ci", "label": "CI checks", "status": "ok",
                        "text": f"optional check failed: {names}",
                    }
                else:
                    ci_stage = {"key": "ci", "label": "CI checks", "status": "ok", "text": ""}
                if rev_status == "fail" or active_fix:
                    rev_stage = {"key": "review", "label": "Review", "status": "bad", "text": ""}
                    change_subtitle = (f"PR #{pr} · review failed; fixing"
                                       if active_fix else f"PR #{pr} · review failed")
                elif rev_status in ("pending", "required", "unknown") or active_rev or rev_plat:
                    rev_stage = {"key": "review", "label": "Review", "status": "now", "text": ""}
                    plat_label = f"{rev_plat} reviewing" if rev_plat else "in review"
                    change_subtitle = f"PR #{pr} · {plat_label}"
                else:
                    rev_stage = {"key": "review", "label": "Review", "status": "ok", "text": ""}
                    change_subtitle = f"PR #{pr} · ready to merge"
            else:
                ci_stage = {"key": "ci", "label": "CI checks", "status": "", "text": ""}
                rev_stage = {"key": "review", "label": "Review", "status": "", "text": ""}
                change_subtitle = f"PR #{pr}"

        rows.append({
            "ref": _ref(project, n),
            "number": n,
            "title": title,
            "pr": pr,
            "pr_url": _pr_url(cfg, project, pr) if pr else None,
            "change_subtitle": change_subtitle,
            "stages": [built_stage, pr_stage, ci_stage, rev_stage, merged_stage, confirmed_stage],
        })

    # 2. Recently merged changes
    for sh in led.shipment_history(project, limit=10):
        n = sh["number"]
        if n in seen_numbers:
            continue
        seen_numbers.add(n)
        pr = row_get(sh, "pr")
        title = row_get(sh, "title") or _ref(project, n)
        shipped_at = row_get(sh, "shipped_at")
        shipped_dt = parse(shipped_at) if shipped_at else None
        mins = max(0, int((now - shipped_dt).total_seconds() // 60)) if shipped_dt else 0
        age_str = ("yesterday" if 1440 <= mins < 2880 else
                   f"{mins // 1440}d ago" if mins >= 1440 else
                   f"{mins // 60}h ago" if mins >= 60 else f"{mins}m ago")
        if mins == 0:
            age_str = "just now"
        verified = bool(row_get(sh, "completion_kind") or row_get(sh, "verdict") == "pass")
        quiet_days = max(0, (now - shipped_dt).days) if shipped_dt else 0

        built_stage = {"key": "built", "label": "Built", "status": "ok", "text": ""}
        pr_stage = {"key": "pr", "label": "PR", "status": "ok", "text": ""}
        ci_stage = {"key": "ci", "label": "CI checks", "status": "ok", "text": ""}
        rev_stage = {"key": "review", "label": "Review", "status": "ok", "text": ""}
        merged_stage = {"key": "merged", "label": "Merged", "status": "ok", "text": ""}
        if verified:
            confirmed_stage = {"key": "confirmed", "label": "Confirmed live", "status": "ok",
                               "text": "verified"}
            change_subtitle = f"merged {age_str} · verified"
        else:
            confirmed_stage = {"key": "confirmed", "label": "Confirmed live", "status": "wait",
                               "text": f"quiet {min(14, quiet_days)}/14d"}
            change_subtitle = f"merged {age_str} · no problem reported"

        rows.append({
            "ref": _ref(project, n),
            "number": n,
            "title": title,
            "pr": pr,
            "pr_url": _pr_url(cfg, project, pr) if pr else None,
            "change_subtitle": change_subtitle,
            "stages": [built_stage, pr_stage, ci_stage, rev_stage, merged_stage, confirmed_stage],
        })

    return rows


def _blocked_overview_rows(cfg, led, project, items, s, now):
    rows = []
    seen = set()

    # 1. Owner questions (needs_you and failed)
    for it in s.get("needs", []):
        if it["project"] == project and it["pending"] is None:
            n = it["number"]
            seen.add(n)
            rows.append({
                "ref": _ref(project, n),
                "number": n,
                "url": _issue_url(cfg, project, n),
                "title": it["title"],
                "reason": "needs you",
                "tone": "bad",
            })

    # 2. Dependency blocked
    open_numbers = {it["number"] for it in items}
    for it in items:
        n = it["number"]
        if n in seen:
            continue
        deps = []
        raw_deps = row_get(it, "depends")
        if raw_deps:
            try:
                parsed_deps = json.loads(raw_deps)
                deps = [d for d in parsed_deps if isinstance(d, int) and d in open_numbers]
            except Exception:
                deps = []
        parent = row_get(it, "parent")
        if it["state"] == "parent" or deps:
            seen.add(n)
            dep_ref = deps[0] if deps else parent
            reason = f"waits on #{dep_ref}" if dep_ref else "waits on dependency"
            rows.append({
                "ref": _ref(project, n),
                "number": n,
                "url": _issue_url(cfg, project, n),
                "title": row_get(it, "title") or _ref(project, n),
                "reason": reason,
                "tone": "warn",
            })

    # 3. Parked
    for it in items:
        n = it["number"]
        if n in seen:
            continue
        if it["state"] == "parked":
            seen.add(n)
            why = _question(led, project, n, "parked", it)
            reason = (f"parked · {why}"
                      if (why and not why.lower().startswith("parked"))
                      else (why or "parked"))
            rows.append({
                "ref": _ref(project, n),
                "number": n,
                "url": _issue_url(cfg, project, n),
                "title": row_get(it, "title") or _ref(project, n),
                "reason": reason,
                "tone": "bad",
            })

    # 4. Holds from scheduler
    schedule_holds = _schedule_holds(led, now) or []
    for h in schedule_holds:
        if h.get("project") != project:
            continue
        n = h.get("number")
        if n and n in open_numbers and n not in seen:
            seen.add(n)
            it = next((i for i in items if i["number"] == n), None)
            overlap = f"area:{h['area']}" if h.get("area") else (", ".join(h.get("files", [])) or "overlap")
            rows.append({
                "ref": _ref(project, n),
                "number": n,
                "url": _issue_url(cfg, project, n),
                "title": (row_get(it, "title") if it else None) or _ref(project, n),
                "reason": f"waits — {overlap} already in progress",
                "tone": "warn",
            })

    return rows


def _queued_overview_rows(cfg, led, project, items, blocked_numbers):
    rows = []
    queued_candidates = [
        it for it in items
        if it["state"] in ("ready", "inbox") and it["number"] not in blocked_numbers
    ]
    queued_candidates.sort(key=lambda i: (i["priority"], i["number"]))
    for idx, it in enumerate(queued_candidates):
        n = it["number"]
        labels = json.loads(row_get(it, "labels", "[]"))
        size = next((l.split(":", 1)[1] for l in labels if l.startswith("size:")), None)
        parts = []
        if idx == 0:
            parts.append("next")
        if size:
            parts.append(size)
        if it["state"] == "inbox":
            parts.append("needs sort")
        meta = " · ".join(parts) if parts else ""
        rows.append({
            "ref": _ref(project, n),
            "number": n,
            "url": _issue_url(cfg, project, n),
            "title": it["title"] or _ref(project, n),
            "meta": meta,
            "tone": "meta",
        })
    return rows


def _completed_7d_rows(cfg, led, project, now):
    out = []
    for sh in led.shipment_history(project, limit=50):
        shipped_at = row_get(sh, "shipped_at")
        shipped_dt = parse(shipped_at) if shipped_at else None
        if not shipped_dt or shipped_dt < now - timedelta(days=7):
            continue
        n = sh["number"]
        mins = max(0, int((now - shipped_dt).total_seconds() // 60))
        age_str = ("yesterday" if 1440 <= mins < 2880 else
                   f"{mins // 1440}d ago" if mins >= 1440 else
                   f"{mins // 60}h ago" if mins >= 60 else f"{mins}m ago")
        if mins == 0:
            age_str = "just now"
        last_run = led.last_run(project, n, roles=("build",))
        plat = last_run["platform"] if last_run else ""
        verified = bool(row_get(sh, "completion_kind") or row_get(sh, "verdict") == "pass")
        quiet_days = max(0, (now - shipped_dt).days)
        status_label = "verified" if verified else f"quiet {min(14, quiet_days)}/14d"
        status_tone = "good" if verified else "mut"
        meta_parts = []
        if not age_str.startswith("yesterday"):
            meta_parts.append(f"merged {age_str}")
        else:
            meta_parts.append(age_str)
        if plat:
            meta_parts.append(plat)
        pr = row_get(sh, "pr")
        out.append({
            "ref": _ref(project, n),
            "number": n,
            "url": _issue_url(cfg, project, n),
            "pr": pr,
            "pr_url": _pr_url(cfg, project, pr) if pr else None,
            "title": row_get(sh, "title") or _ref(project, n),
            "meta": " · ".join(meta_parts),
            "status": status_label,
            "status_tone": status_tone,
            "verified": verified,
        })
    return out


def _builders_30d(led, project, now):
    since_30d = iso(now - timedelta(days=30))
    rows = led.q("SELECT platform, count(*) AS count FROM runs "
                 "WHERE project=? AND role='build' AND status='ended' AND outcome='DONE' "
                 "AND ended_at >= ? GROUP BY platform ORDER BY count DESC",
                 (project, since_30d))
    total = sum(r["count"] for r in rows)
    out = []
    for r in rows:
        pct = round(r["count"] / total * 100) if total else 0
        out.append({"platform": r["platform"], "count": r["count"], "pct": pct})
    return out


def _project_settings_summary(cfg, project):
    pol = config.project_policy(cfg, project)
    account = config.account_of(pol)
    verify = pol.get("verify") or "none configured"
    return {
        "account": account,
        "verify": verify,
    }


def _project_overview_state(cfg, led, project, s, now):
    pol = config.project_policy(cfg, project)
    repo = pol.get("repo", "")
    repo_url = f"https://github.com/{repo}" if repo else ""
    summary = next((row for row in s["scope_options"] if row["name"] == project), {})
    current_ver = summary.get("current_version", "")

    all_items = led.items(project)
    open_items = [it for it in all_items if it["state"] in STATE_ORDER]
    open_count = len(open_items)

    net = led.backlog_net(project, now=now)
    net_7d = (net or {}).get("7d")
    delta_7d = net_7d["net"] if net_7d else 0
    delta_text = f"{_signed(delta_7d)} in 7d" if net_7d else ""
    delta_tone = "good" if delta_7d < 0 else "bad" if delta_7d > 0 else "mut"

    blocked_rows = _blocked_overview_rows(cfg, led, project, open_items, s, now)
    blocked_numbers = {r["number"] for r in blocked_rows}
    blocked_otherwise = sum(1 for r in blocked_rows if r["reason"] != "needs you")

    queued_rows = _queued_overview_rows(cfg, led, project, open_items, blocked_numbers)
    next_item = f"#{queued_rows[0]['number']}" if queued_rows else None

    waiting_items = [n for n in s["needs"] if n["project"] == project and n["pending"] is None]
    waiting_on_you = len(waiting_items)
    waiting_age = _dur(timedelta(seconds=max(n["waited_s"] for n in waiting_items))) if waiting_items else ""

    completed_7d_rows = _completed_7d_rows(cfg, led, project, now)
    done_7d = len(completed_7d_rows)
    last_shipment = led.shipment_history(project, limit=1)
    if last_shipment and row_get(last_shipment[0], "shipped_at"):
        last_dt = parse(row_get(last_shipment[0], "shipped_at"))
        lmins = max(0, int((now - last_dt).total_seconds() // 60))
        last_shipped_age = (f"{lmins // 1440}d ago" if lmins >= 1440 else
                            f"{lmins // 60}h ago" if lmins >= 60 else f"{lmins}m ago")
        if lmins == 0:
            last_shipped_age = "just now"
    else:
        last_shipped_age = ""

    shipping_rows = _shipping_rows(cfg, led, project, s, now)
    how_ships = _how_a_change_ships(pol)
    activity_rows = [
        {"when": ev["when"], "text": ev["text"], "url": ev.get("url")}
        for ev in s.get("events", [])[:4]
    ]
    builders_30d = _builders_30d(led, project, now)
    settings_summary = _project_settings_summary(cfg, project)

    runs = s.get("runs", [])
    active_count = len(runs)
    project_paused = led.get_kv(config.project_pause_key(project)) == "1"
    paused = s.get("paused", False) or project_paused
    status_label = "PAUSED" if paused else f"RUNNING · {active_count}" if active_count else "IDLE"
    status_tone = "warn" if paused else "acc" if active_count else "mut"

    return {
        "project": project,
        "repo": repo,
        "repo_url": repo_url,
        "current_version": current_ver,
        "status_label": status_label,
        "status_tone": status_tone,
        "parallel": {
            "running": active_count,
            "max": pol.get("max_parallel", 1),
            "min_value": 1,
            "max_value": 64,
            "mahler_active": len(led.active_runs()),
            "mahler_total": cfg["concurrency"]["total"],
        },
        "project_paused": project_paused,
        "kpis": {
            "open": open_count,
            "open_delta_7d": delta_7d,
            "open_delta_text": delta_text,
            "open_delta_tone": delta_tone,
            "active": active_count,
            "queued": len(queued_rows),
            "next_item": next_item,
            "waiting_on_you": waiting_on_you,
            "waiting_age": waiting_age,
            "blocked_otherwise": blocked_otherwise,
            "done_7d": done_7d,
            "last_shipped_age": last_shipped_age,
        },
        "needs_you": [n for n in s.get("needs", []) if n["project"] == project],
        "active_rows": runs,
        "queued_rows": queued_rows,
        "blocked_rows": blocked_rows,
        "shipping_rows": shipping_rows,
        "how_a_change_ships": how_ships,
        "completed_7d_rows": completed_7d_rows,
        "activity_rows": activity_rows,
        "builders_30d": builders_30d,
        "settings": settings_summary,
    }
