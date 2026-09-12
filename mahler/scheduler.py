"""One tick: watchdog → GitHub sync → lease expiry → usage → schedule → labels.

Run every 60s by launchd (through launcher/mahler-launcher). An exclusive
flock makes overlapping ticks exit at once — agents outlive the tick that
launched them, exactly as in dispatch.
"""

import fcntl
import json
import os
import sys
from datetime import timedelta

from . import backup, config, notify, platforms, presence, router, runner
from .gh import (GH, GHError, AGENT_MARK, LABEL_STATES, STATE_LABELS, depends_of,
                 label_names, parse_command, pin_of, priority_of)
from .ledger import iso, parse

STOP_NOW = ("parked",)                               # no grace period
NO_ATTEMPT = ("quota", "preempted", "closed", "parked", "lost-lease")


class Ctx:
    def __init__(self, cfg, led, dry_run=False, hot_hold=True, verbose=False):
        self.cfg, self.led, self.dry_run, self.hot_hold = cfg, led, dry_run, hot_hold
        self.verbose = verbose
        self.lines = []
        self._gh = {}
        self._labels = {}          # (project, number) -> labels from this tick's sync

    def policy(self, project):
        return config.project_policy(self.cfg, project)

    def gh(self, project):
        repo = self.policy(project)["repo"]
        if repo not in self._gh:
            self._gh[repo] = GH(repo)
        return self._gh[repo]

    def say(self, msg):
        self.lines.append(msg)

    def url(self, project, number):
        return f"https://github.com/{self.policy(project)['repo']}/issues/{number}"

    def ping(self, title, message="", project=None, number=None, priority="default", tags=""):
        if self.dry_run:
            return
        notify.send(self.cfg, title, message,
                    click=self.url(project, number) if project else None,
                    priority=priority, tags=tags)


# ---------- entry ----------

def take_lock():
    os.makedirs(config.STATE, exist_ok=True)
    fh = open(config.LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return None
    return fh


def tick(ctx):
    projects = [p for p in config.enabled_projects(ctx.cfg) if _project_ok(ctx, p)]
    watchdog(ctx)
    for p in projects:
        try:
            sync(ctx, p["name"])
        except GHError as e:
            ctx.say(f"{p['name']}: GitHub sync failed — {e}")
    expire(ctx)
    if ctx.led.paused():
        ctx.say("paused — not starting anything (mahler resume)")
    else:
        refresh_usage(ctx, projects)
        for p in projects:
            schedule(ctx, p)
    for p in projects:
        mirror_labels(ctx, p["name"])
    if not ctx.dry_run:
        for p in config.enabled_projects(ctx.cfg):     # backups run even while paused
            for spec in p.get("backups") or []:
                backup.run(ctx, p["name"], spec)
    return ctx.lines


def _project_ok(ctx, p):
    if not p.get("repo") or not p.get("path"):
        ctx.say(f"{p['name']}: needs both repo and path in config")
        return False
    if not os.path.isdir(os.path.join(p["path"], ".git")):
        ctx.say(f"{p['name']}: {p['path']} not reachable (drive unmounted?) — skipping")
        return False
    return True


# ---------- watchdog ----------

def watchdog(ctx):
    led, now = ctx.led, ctx.led.now()
    for run in led.active_runs():
        pol = ctx.policy(run["project"])
        if not runner.alive(run["pid"]):
            finalize(ctx, run)
            continue
        if run["status"] == "stopping":
            runner.kill(run["pid"])                  # asked nicely last tick
            continue
        holder = f"run:{run['id']}"
        still_ours = led.heartbeat(run["project"], run["number"], holder, run["epoch"],
                                   pol["auto_lease_minutes"])
        reason = None
        if run["yield_at"]:
            if ctx.cfg["platforms"][run["platform"]]["kind"] == "claude":
                yield_file = os.path.join(config.RUNS_DIR, str(run["id"]), "yield")
                if not os.path.exists(yield_file):
                    os.makedirs(os.path.dirname(yield_file), exist_ok=True)
                    open(yield_file, "w").close()
            preset = run["stop_reason"]
            grace = timedelta(seconds=0 if preset in STOP_NOW else pol["yield_grace_seconds"])
            if now >= parse(run["yield_at"]) + grace:
                reason = preset or "preempted"
        elif not still_ours:
            reason = "lost-lease"
        if not reason:
            reason = _health(ctx, run, pol, now)
        if reason:
            stop(ctx, run, reason)


def _health(ctx, run, pol, now):
    pconf = ctx.cfg["platforms"][run["platform"]]
    if now - parse(run["started_at"]) > timedelta(minutes=pol["run_timeout_minutes"]):
        return "timeout"
    try:
        idle = now.timestamp() - os.path.getmtime(run["log_path"])
    except OSError:
        idle = (now - parse(run["started_at"])).total_seconds()
    if idle > pol["progress_timeout_minutes"] * 60:
        return "hung"
    if pconf["kind"] == "claude":
        for w, pct, resets in platforms.read_log(run["log_path"], "claude")["usage"]:
            ctx.led.record_usage(run["platform"], w, pct, resets)
    state, detail = router.usage_state(ctx.led, run["platform"], pconf)
    if state == "hard":
        ctx.say(f"#{run['number']}: {run['platform']} over its hard line ({detail})")
        return "quota"
    return None


def stop(ctx, run, reason):
    ctx.say(f"{run['project']}#{run['number']}: stopping run {run['id']} ({reason})")
    if ctx.dry_run:
        return
    ctx.led.update_run(run["id"], status="stopping", stop_reason=reason)
    runner.terminate(run["pid"])


def request_stop(ctx, run, reason):
    """Ask a run to stop at the next watchdog pass (sync uses this)."""
    if not ctx.dry_run:
        ctx.led.update_run(run["id"], yield_at=iso(ctx.led.now()), stop_reason=reason)


# ---------- finalize: every exit is a handoff ----------

def finalize(ctx, run):
    led = ctx.led
    project, n = run["project"], run["number"]
    pol = ctx.policy(project)
    item = led.item(project, n)
    kind = ctx.cfg["platforms"][run["platform"]]["kind"]
    log = platforms.read_log(run["log_path"], kind)
    for w, pct, resets in log["usage"]:
        led.record_usage(run["platform"], w, pct, resets)
    if log["quota_hit"]:
        pconf = ctx.cfg["platforms"][run["platform"]]
        until = iso(led.now() + timedelta(minutes=pconf.get("backoff_minutes", 60)))
        for w in router.WINDOWS:
            led.record_usage(run["platform"], w, 100.0, until)
    verb, rest = platforms.status_line(log["final"] or log["last_text"])
    code = runner.exit_code(run)
    reason = run["stop_reason"] or ("quota" if log["quota_hit"] else None)
    outcome = verb or (f"exit {code}" if code else "no status line")
    ctx.say(f"{project}#{n}: run {run['id']} ({run['role']} on {run['platform']}) ended — "
            f"{outcome}{f' [{reason}]' if reason else ''}")
    if ctx.dry_run:
        return

    gh = ctx.gh(project)
    keep_worktree = False
    if run["role"] == "sort":
        if verb == "READY":
            led.set_state(project, n, "ready", "sorted", sorted_at=iso(led.now()))
        elif verb == "SPLIT":
            led.set_state(project, n, "tracking", "split into sub-issues")
        elif verb == "NEEDS-YOU":
            led.set_state(project, n, "needs_you", rest)
            ctx.ping(f"Mahler needs you — {project} #{n}", rest or item["title"],
                     project, n, priority="high", tags="question")
        else:
            _retry_or_fail(ctx, project, n, item, reason, outcome)
    else:
        closed = False
        try:
            closed = gh.issue_state(n) == "CLOSED"
        except GHError:
            pass
        saved = None
        if closed:
            led.set_state(project, n, "done", outcome)
            if verb == "MERGED":
                ctx.ping(f"Shipped — {project} #{n}", item["title"], project, n, tags="rocket")
        else:
            try:
                saved = runner.snapshot(pol["path"], run["worktree"], run["id"], n,
                                        pol.get("base", "main"))
            except runner.GitError as e:
                keep_worktree = True
                ctx.say(f"{project}#{n}: snapshot failed, keeping worktree — {e}")
            if saved:
                led.upsert_item(project, n, branch=saved["ref"])
            _handoff_comment(ctx, run, item, reason, outcome, saved, log, keep_worktree)
            if verb == "NEEDS-YOU":
                led.set_state(project, n, "needs_you", rest)
                ctx.ping(f"Mahler needs you — {project} #{n}", rest or item["title"],
                         project, n, priority="high", tags="question")
            elif reason == "parked":
                led.set_state(project, n, "parked", "parked while running")
            elif reason == "preempted":
                led.set_state(project, n, "working", "handed to your session")
                ctx.ping(f"Handoff to you — {project} #{n}",
                         f"{run['platform']} stepped aside; its work is on "
                         f"{saved['ref'] if saved else 'nothing new to save'}",
                         project, n, priority="low")
            elif reason in ("quota", "lost-lease"):
                led.set_state(project, n, "ready", f"handoff ({reason})")
                ctx.ping(f"Handoff — {project} #{n}",
                         f"{run['platform']} stopped ({reason}); next platform picks it up",
                         project, n, priority="low")
            else:
                _retry_or_fail(ctx, project, n, item, reason, outcome)
    led.release(project, n, holder=f"run:{run['id']}", epoch=run["epoch"])
    led.update_run(run["id"], status="ended", outcome=outcome, exit_code=code,
                   ended_at=iso(led.now()))
    if not keep_worktree:
        runner.remove_worktree(pol["path"], run["worktree"], run["branch"],
                               runner.worktree_root(pol))


def _retry_or_fail(ctx, project, n, item, reason, outcome):
    led = ctx.led
    if reason in NO_ATTEMPT:
        led.set_state(project, n, "ready" if item["sorted_at"] else "inbox", f"retry ({reason})")
        return
    attempts = item["attempts"] + 1
    if attempts >= ctx.policy(project)["max_attempts"]:
        led.set_state(project, n, "failed", f"{attempts} failed attempts — last: {outcome}",
                      attempts=attempts)
        ctx.ping(f"Stuck — {project} #{n}",
                 f"{attempts} attempts failed ({outcome}). Comment `/mahler go` to retry.",
                 project, n, priority="high", tags="warning")
    else:
        back = "ready" if item["sorted_at"] else "inbox"
        led.set_state(project, n, back, f"attempt {attempts} failed: {outcome}",
                      attempts=attempts)


REASON_TEXT = {
    "quota": "quota line reached", "preempted": "you took over in a session",
    "hung": "no progress for too long", "timeout": "hit the time limit",
    "closed": "the issue was closed", "parked": "parked", "lost-lease": "lost its lease",
}


def _handoff_comment(ctx, run, item, reason, outcome, saved, log, kept):
    mins = int((ctx.led.now() - parse(run["started_at"])).total_seconds() // 60)
    why = REASON_TEXT.get(reason, f"ended: {outcome}")
    lines = [f"<!-- mahler:handoff run={run['id']} epoch={run['epoch']} "
             f"from={run['platform']} reason={reason or 'ended'} -->",
             f"**Handoff** — {run['platform']} stopped after {mins} min ({why}).", ""]
    if saved:
        lines.append(f"Work saved: `{saved['ref']}` @ {saved['sha']} — {saved['ahead']} commit(s) "
                     f"ahead of base{'; ' + saved['stat'] if saved['stat'] else ''}. "
                     "The next run starts from there.")
    elif kept:
        lines.append(f"Couldn't push a snapshot; the worktree was kept at `{run['worktree']}`.")
    else:
        lines.append("Nothing new to save.")
    tail = (log["last_text"] or "").strip()
    if tail:
        quoted = "\n".join("> " + l for l in tail[-700:].splitlines()[-12:])
        lines += ["", "Agent's last words:", quoted]
    try:
        ctx.gh(run["project"]).comment(run["number"], "\n".join(lines))
    except GHError as e:
        ctx.say(f"#{run['number']}: couldn't post handoff comment — {e}")


# ---------- GitHub sync ----------

def sync(ctx, project):
    led, gh = ctx.led, ctx.gh(project)
    pol = ctx.policy(project)
    issues = gh.open_issues()
    open_nums = set()
    for iss in issues:
        n = iss["number"]
        labels = label_names(iss)
        if pol.get("scope") == "label" and led.item(project, n) is None and not (
                pol["scope_label"] in labels or any(l in LABEL_STATES for l in labels)):
            continue                      # not (yet) handed to Mahler
        open_nums.add(n)
        ctx._labels[(project, n)] = labels
        fields = dict(title=iss["title"], labels=json.dumps(labels), priority=priority_of(labels),
                      depends=json.dumps(depends_of(iss.get("body"))), pin=pin_of(labels))
        item = led.item(project, n)
        if item is None:
            state = _state_from_labels(labels) or "inbox"
            extra = {"sorted_at": iso(led.now())} if state == "ready" else {}
            led.upsert_item(project, n, created_at=iss["createdAt"], **fields, **extra)
            led.set_state(project, n, state, "new issue")
            ctx.say(f"{project}#{n}: new — {iss['title']}")
            item = led.item(project, n)
        else:
            led.upsert_item(project, n, **fields)
            _adopt_label_edits(ctx, project, item, labels)
        _process_comments(ctx, project, led.item(project, n), iss.get("comments") or [])

    for item in led.items(project):
        if item["number"] in open_nums or item["state"] == "done":
            continue
        try:
            state = gh.issue_state(item["number"])
        except GHError:
            continue
        if state == "CLOSED":
            running = [r for r in led.active_runs(project) if r["number"] == item["number"]]
            # A run whose own merge closed the issue is usually still writing its
            # summary: give it the normal grace period; finalize marks it done.
            for run in running:
                if not run["yield_at"]:
                    request_stop(ctx, run, "closed")
            if not running:
                led.release(project, item["number"])
                led.set_state(project, item["number"], "done", "closed on GitHub")


def _state_from_labels(labels):
    states = [LABEL_STATES[l] for l in labels if l in LABEL_STATES]
    return states[0] if len(states) == 1 else None


def _adopt_label_edits(ctx, project, item, labels):
    """You changed a mahler:* label by hand → that's an instruction.

    `mirror` is the label Mahler last wrote. A label equal to it is Mahler's own
    (possibly not yet updated); a different one was set by you."""
    wanted = _state_from_labels(labels)
    if not wanted or wanted == item["state"] or item["mirror"] is None:
        return
    if STATE_LABELS.get(wanted) == item["mirror"]:
        return
    if wanted in ("ready", "parked", "inbox"):
        _apply_instruction(ctx, project, item, "go" if wanted == "ready" else wanted, None)


def _process_comments(ctx, project, item, comments):
    led = ctx.led
    seen = parse(item["last_comment_at"])
    newest = seen
    for c in sorted(comments, key=lambda c: c["createdAt"]):
        at = parse(c["createdAt"])
        if seen and at <= seen:
            continue
        newest = at if newest is None or at > newest else newest
        body = c.get("body") or ""
        if body.lstrip().startswith(AGENT_MARK):
            continue
        cmd = parse_command(body)
        if cmd:
            _apply_instruction(ctx, project, led.item(project, item["number"]), *cmd)
        elif led.item(project, item["number"])["state"] == "needs_you":
            led.set_state(project, item["number"], "inbox", "you answered — re-sorting",
                          sorted_at=None)
            ctx.say(f"{project}#{item['number']}: answer received, re-sorting")
    if newest and newest != seen:
        led.upsert_item(project, item["number"], last_comment_at=iso(newest))


def _apply_instruction(ctx, project, item, verb, arg):
    led, n = ctx.led, item["number"]
    if verb == "go":
        led.set_state(project, n, "ready", "you said go", attempts=0,
                      sorted_at=iso(led.now() - timedelta(days=1)))
    elif verb == "park":
        led.set_state(project, n, "parked", "you parked it")
        for run in led.active_runs(project):
            if run["number"] == n:
                request_stop(ctx, run, "parked")
    elif verb == "inbox":
        led.set_state(project, n, "inbox", "back to inbox", sorted_at=None)
    elif verb == "platform" and arg:
        if arg in ctx.cfg["platforms"]:
            led.upsert_item(project, n, pin=arg)
        else:
            ctx.say(f"{project}#{n}: unknown platform {arg!r}")
    ctx.say(f"{project}#{n}: instruction '{verb}{' ' + arg if arg else ''}'")


# ---------- leases that ran out ----------

def expire(ctx):
    led = ctx.led
    for lease in led.expired_leases():
        project, n = lease["project"], lease["number"]
        if lease["kind"] == "auto":
            live = [r for r in led.active_runs(project) if r["id"] == lease["run_id"]]
            if live:
                continue            # the watchdog owns running runs
        pol = ctx.policy(project)
        if lease["kind"] == "interactive":
            last = presence.last_claude_activity(pol["path"]) if pol.get("path") else None
            if last and led.now() - last < timedelta(minutes=pol["interactive_lease_minutes"]):
                led.heartbeat(project, n, lease["holder"], lease["epoch"],
                              pol["interactive_lease_minutes"])
                continue
        led.release(project, n, holder=lease["holder"], epoch=lease["epoch"])
        item = led.item(project, n)
        if item and item["state"] == "working":
            led.set_state(project, n, "ready", f"{lease['kind']} lease expired")
        ctx.say(f"{project}#{n}: {lease['kind']} lease by {lease['holder']} expired")


# ---------- usage ----------

def refresh_usage(ctx, projects):
    led, cfg = ctx.led, ctx.cfg
    wanted = set()
    for p in projects:
        if led.items(p["name"], ["inbox", "ready"]):
            wanted |= set(cfg["routing"]["sort"]) | set(cfg["routing"]["build"])
    wanted |= {r["platform"] for r in led.active_runs()}
    agy = [n for n in wanted if cfg["platforms"].get(n, {}).get("kind") == "agy"
           and router.usage_state(led, n, cfg["platforms"][n])[0] == "stale"]
    if agy:
        pools = platforms.probe_agy()
        for name, pconf in cfg["platforms"].items():
            for w, pct, resets in pools.get(pconf.get("pool"), []):
                led.record_usage(name, w, pct, resets)
    for name in wanted:
        pconf = cfg["platforms"].get(name, {})
        if pconf.get("kind") != "claude" or router.usage_state(led, name, pconf)[0] != "stale":
            continue
        free = platforms.oauth_usage()                     # zero tokens
        for w, pct, resets in free:
            led.record_usage(name, w, pct, resets)
        if free:
            continue
        last = parse(led.get_kv(f"probe:{name}"))
        if last and led.now() - last < timedelta(minutes=pconf.get("stale_minutes", 15)):
            continue
        led.set_kv(f"probe:{name}", iso(led.now()))
        for w, pct, resets in platforms.probe_claude():
            led.record_usage(name, w, pct, resets)


# ---------- schedule ----------

def schedule(ctx, p):
    led, cfg, name = ctx.led, ctx.cfg, p["name"]
    now = led.now()
    active = led.active_runs()
    total = len(active)
    in_project = len([r for r in active if r["project"] == name])
    per_platform = {}
    for r in active:
        per_platform[r["platform"]] = per_platform.get(r["platform"], 0) + 1

    hot = False
    if p.get("hot_hold") and ctx.hot_hold:
        last = presence.last_claude_activity(p["path"])
        hot = bool(last and now - last < timedelta(minutes=p["hot_hold_minutes"]))

    done = {i["number"] for i in led.items(name, ["done"])}
    work = []
    for it in led.items(name, ["inbox"]):
        work.append(("sort", it))
    for it in led.items(name, ["ready"]):
        sorted_at = parse(it["sorted_at"])
        if sorted_at and now - sorted_at < timedelta(minutes=p["settle_minutes"]):
            continue
        deps = [d for d in json.loads(it["depends"] or "[]") if d not in done]
        if deps:
            continue
        work.append(("build", it))

    for role, it in work:
        n = it["number"]
        if total >= cfg["concurrency"]["total"] or in_project >= p["max_parallel"]:
            ctx.say(f"{name}: at capacity ({total} running)")
            return
        if led.lease(name, n):
            continue
        if role == "build" and hot:
            ctx.say(f"{name}#{n}: hot hold — a Claude session is active in this project")
            continue
        busy = {k for k, v in per_platform.items()
                if v >= cfg["platforms"].get(k, {}).get("max_runs", 1)}
        busy |= {k for k, pc in cfg["platforms"].items() if not platforms.available(pc)}
        size = next((l.split(":", 1)[1] for l in json.loads(it["labels"] or "[]")
                     if l.startswith("size:")), None)
        platform, reasons = router.pick(cfg, led, role, it["pin"] if role == "build" else None,
                                        busy, size=size)
        if not platform:
            ctx.say(f"{name}#{n}: no platform for {role} — {'; '.join(reasons)}")
            continue
        if ctx.dry_run:
            ctx.say(f"{name}#{n}: would {role} on {platform}")
        elif not start(ctx, name, it, role, platform):
            continue
        total += 1
        in_project += 1
        per_platform[platform] = per_platform.get(platform, 0) + 1


def start(ctx, project, item, role, platform):
    led, pol, n = ctx.led, ctx.policy(project), item["number"]
    run_id = led.create_run(project=project, number=n, role=role, platform=platform,
                            epoch=0, status="running")
    lease, info = led.claim(project, n, f"run:{run_id}", "auto", pol["auto_lease_minutes"],
                            platform=platform, run_id=run_id)
    if lease is None:
        led.update_run(run_id, status="ended", outcome="not claimed", ended_at=iso(led.now()))
        ctx.say(f"{project}#{n}: held by {info['held_by']['holder']} — skipped")
        return False
    try:
        meta = runner.launch(ctx, project, item, role, platform, run_id, lease["epoch"])
    except Exception as e:                       # noqa: BLE001 — any launch failure
        led.release(project, n, holder=f"run:{run_id}")
        led.update_run(run_id, status="ended", outcome=f"launch failed: {e}"[:300],
                       ended_at=iso(led.now()))
        led.event("launch_failed", project, n, str(e)[:500])
        ctx.say(f"{project}#{n}: launch failed — {e}")
        return False
    led.update_run(run_id, epoch=lease["epoch"], **meta)
    led.event("run_start", project, n, {"run": run_id, "role": role, "platform": platform})
    ctx.say(f"{project}#{n}: started {role} on {platform} (run {run_id})")
    if role == "build":
        led.set_state(project, n, "working", f"{platform} run {run_id}")
        try:
            ctx.gh(project).comment(n, f"▶︎ **{platform}** started work (run {run_id}) on "
                                       f"branch `{meta['branch']}`.")
        except GHError:
            pass
    return True


# ---------- label mirror ----------

def mirror_labels(ctx, project):
    if ctx.dry_run:
        return
    led, gh = ctx.led, ctx.gh(project)
    for item in led.items(project):
        if item["state"] == "done":
            continue
        want = STATE_LABELS.get(item["state"])
        current = ctx._labels.get((project, item["number"]))
        if current is None:
            continue
        if want in current and not [l for l in current if l in LABEL_STATES and l != want]:
            if item["mirror"] != want:
                led.upsert_item(project, item["number"], mirror=want)
            continue
        try:
            gh.set_state_label(item["number"], item["state"], current)
            led.upsert_item(project, item["number"], mirror=want)
        except GHError as e:
            ctx.say(f"{project}#{item['number']}: label update failed — {e}")


def main_tick(cfg, led, dry_run=False, hot_hold=True):
    lock = take_lock()
    if lock is None:
        print("mahler: another tick is running; exiting")
        return 0
    ctx = Ctx(cfg, led, dry_run=dry_run, hot_hold=hot_hold)
    tick(ctx)
    for line in ctx.lines:
        print(line)
    sys.stdout.flush()
    return 0
