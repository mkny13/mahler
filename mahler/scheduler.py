"""One tick: watchdog → GitHub sync → lease expiry → usage → schedule → labels.

Run every 60s by launchd (through launcher/mahler-launcher). An exclusive
flock makes overlapping ticks exit at once — agents outlive the tick that
launched them, exactly as in dispatch.
"""

import fcntl
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from . import backup, config, digest, janitor, notify, platforms, presence, router, runner
from .gh import (GH, GHError, AGENT_MARK, LABEL_STATES, STATE_LABELS, checks_state,
                 depends_of, has_sections, label_names, needs_human_of, parse_command,
                 part_of, pin_of, pr_body, pr_summary_of, priority_of)
from .ledger import iso, parse

STOP_NOW = ("parked",)                               # no grace period
NO_ATTEMPT = ("quota", "preempted", "closed", "parked", "lost-lease", "silent")
CONDUCTOR = "conductor"                              # the lease holder that ships


class Ctx:
    def __init__(self, cfg, led, dry_run=False, hot_hold=True, verbose=False):
        self.cfg, self.led, self.dry_run, self.hot_hold = cfg, led, dry_run, hot_hold
        self.verbose = verbose
        self.lines = []
        self._gh = {}
        self._labels = {}          # (project, number) -> labels from this tick's sync
        self.burst_lines = None    # D23: set by _compute_burst during this tick

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


MAINTENANCE_TEXT = {
    "security": ("Security & Surface Area Audit", "Security & Surface Area Audit — credential boundaries (`.env` leaks, unpooled URLs), subshell executions (`subprocess.run` argument sanitization), permission boundaries, denial list checks, dependency scans."),
    "health": ("Codebase Health & Refactoring Pass", "Codebase Health & Refactoring Pass — unclosed resource leaks (DB connections, file descriptors), dead code / orphaned helpers, cyclomatic complexity hotspots."),
    "drift": ("Architecture & Specification Drift Audit", "Architecture & Specification Drift Audit — comparing implementation against `DESIGN.md` / `ARCHITECTURE.md` / `ROADMAP.md`, cleaning up zombie abstractions."),
    "tests": ("Test Suite Health & Flakiness Audit", "Test Suite Health & Flakiness Audit — test isolation, false-green tests, execution time creep, `ResourceWarning` checks."),
    "token-economy": ("Token Economy/Quota & Performance Hygiene", "Token Economy/Quota & Performance Hygiene — prompt context bloat in recipes/rules, run duration outliers, excessive polling overhead, DB query efficiency."),
    "guidance": ("Agent Guidance & Rule Calibration", "Agent Guidance & Rule Calibration — reviewing `AGENTS.md` / `CLAUDE.md` / `recipes` against observed failure modes, pruning obsolete instructions."),
}


def queue_maintenance(ctx, projects):
    """File due maintenance passes as issues (deduped)."""
    led, now = ctx.led, ctx.led.now()
    for p in projects:
        pol = config.maintenance_policy(ctx.cfg, p["name"])
        if not pol["enabled"]:
            continue
        passes = pol["passes"]
        if not passes:
            continue
        
        items = led.items(p["name"])
        for pass_name in passes:
            label = f"pass:{pass_name}"
            skip = False
            for it in items:
                labels = json.loads(it["labels"] or "[]")
                if label not in labels:
                    continue
                if it["state"] != "done":
                    skip = True
                    break
                changed_at = parse(it["state_changed_at"])
                if changed_at and now - changed_at < timedelta(days=pol["cooldown_days"]):
                    skip = True
                    break
            
            if skip:
                continue
            
            if not led.maintenance_due(p["name"], pass_name, policy=pol):
                continue
            
            title, body = MAINTENANCE_TEXT[pass_name]
            issue_labels = ["type:chore", "size:l", "p2", label]
            
            ctx.say(f"{p['name']}: queuing {pass_name} pass")
            if not ctx.dry_run:
                try:
                    ctx.gh(p["name"]).ensure_pass_label(pass_name)
                    ctx.gh(p["name"]).create_issue(title, body, issue_labels)
                    led.reset_maintenance(p["name"], pass_name)
                except GHError as e:
                    ctx.say(f"{p['name']}: failed to file {pass_name} pass — {e}")


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
    _compute_burst(ctx, projects)   # D23: before watchdog so running runs
    watchdog(ctx)                   #   see burst lines too
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
        queue_maintenance(ctx, projects)
        schedule(ctx, projects)
        ship(ctx, projects)
    for p in projects:
        mirror_labels(ctx, p["name"])
    if not ctx.dry_run:
        for p in config.enabled_projects(ctx.cfg):     # backups run even while paused
            for spec in p.get("backups") or []:
                backup.run(ctx, p["name"], spec)
    digest.maybe_send(ctx)                  # informational: also runs while paused
    janitor.maybe_run(ctx)                  # daily sweep (mahler#7): also while paused
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
            # The shell is gone, but a child can outlive it: a SIGTERM that lands
            # while the agent is blocked in a syscall is only acted on later, if
            # at all (mahler#12). Nothing of a run may outlive the run.
            runner.kill(run["pid"])
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


def _record_claude_usage(ctx, samples, backoff_until=None, check_human=False):
    """Record usage across all configured platforms that share the Claude account.

    When check_human is True (the periodic probe path, not a run's own log), a
    5h usage increase with no live Claude run is treated as human use of the
    account elsewhere (Claude app on phone, the web UI, another session) and
    sets a kv flag that suppresses the D23 burst for half an hour (D23).
    """
    claude_platforms = [pname for pname, pconf in ctx.cfg["platforms"].items()
                        if pconf.get("kind") == "claude"]
    for pname in claude_platforms:
        prev_5h = ctx.led.usage(pname).get("5h", {}).get("used_pct")
        for w, pct, resets in samples:
            if check_human and w == "5h" and prev_5h is not None and pct > prev_5h:
                active_claude = any(
                    ctx.cfg["platforms"].get(r["platform"], {}).get("kind") == "claude"
                    for r in ctx.led.active_runs())
                if not active_claude:
                    ctx.led.set_kv("human:claude", iso(ctx.led.now()))
            ctx.led.record_usage(pname, w, pct, resets)
        if backoff_until:
            pconf = ctx.cfg["platforms"][pname]
            for w in pconf.get("windows", router.WINDOWS):
                ctx.led.record_usage(pname, w, 100.0, backoff_until)


def _health(ctx, run, pol, now):
    pconf = ctx.cfg["platforms"][run["platform"]]
    if now - parse(run["started_at"]) > timedelta(minutes=pol["run_timeout_minutes"]):
        return "timeout"
    try:
        st = os.stat(run["log_path"])
        idle = now.timestamp() - st.st_mtime
        # The log exists once the agent command starts (after any setup step).
        # Still empty long after that: the agent never got going. Seen when a
        # macOS permission dialog blocks it at startup (mahler#12).
        if st.st_size == 0 and idle > pol["startup_timeout_minutes"] * 60:
            return "silent"
    except OSError:
        idle = (now - parse(run["started_at"])).total_seconds()
    if idle > pol["progress_timeout_minutes"] * 60:
        return "hung"
    if pconf["kind"] == "claude":
        log = platforms.read_log(run["log_path"], "claude")
        _record_claude_usage(ctx, log["usage"])
        if log["overage"]:
            ctx.say(f"#{run['number']}: {run['platform']} started drawing paid extra usage — stopping")
            kv_key = f"overage:{run['id']}"
            if not ctx.led.get_kv(kv_key):
                ctx.led.set_kv(kv_key, "1")
                ctx.ping(f"Mahler: {run['platform']} run drawing paid extra usage",
                         f"Run {run['id']} on {run['platform']} reported isUsingOverage — stopped "
                         "at once so nothing autonomous eats into paid overage (DESIGN D8).",
                         run["project"], run["number"], priority="high", tags="warning")
            return "quota"
    claude_lines = ctx.burst_lines if pconf.get("kind") == "claude" else None
    state, detail = router.usage_state(ctx.led, run["platform"], pconf,
                                        burst_lines=claude_lines)
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
    if kind == "claude":
        until = None
        if log["quota_hit"]:
            pconf = ctx.cfg["platforms"][run["platform"]]
            until = iso(led.now() + timedelta(minutes=pconf.get("backoff_minutes", 60)))
        _record_claude_usage(ctx, log["usage"], backoff_until=until)
    else:
        for w, pct, resets in log["usage"]:
            led.record_usage(run["platform"], w, pct, resets)
        if log["quota_hit"]:
            pconf = ctx.cfg["platforms"][run["platform"]]
            until = iso(led.now() + timedelta(minutes=pconf.get("backoff_minutes", 60)))
            for w in pconf.get("windows", router.WINDOWS):
                led.record_usage(run["platform"], w, 100.0, until)
    verb, rest = platforms.status_line(log["final"] or log["last_text"])
    code = runner.exit_code(run)
    setup_failed = code == 97 and run["role"] == "build"   # setup step failed before the agent ran
    reason = run["stop_reason"] or ("quota" if log["quota_hit"] else None) or \
             ("setup-failed" if setup_failed else None)
    outcome = ("setup failed" if setup_failed
               else verb or (f"exit {code}" if code else "no status line"))
    ctx.say(f"{project}#{n}: run {run['id']} ({run['role']} on {run['platform']}) ended — "
            f"{outcome}{f' [{reason}]' if reason else ''}")
    if ctx.dry_run:
        return
    if reason == "silent":
        _hold_platform(ctx, run)

    gh = ctx.gh(project)
    keep_worktree = False
    if setup_failed:
        _setup_failure(ctx, run, item)
        return
    led.reset_setup_fails(project, n)
    if run["role"] == "sort":
        if verb == "READY":
            led.set_state(project, n, "ready", "sorted", sorted_at=iso(led.now()))
        elif verb == "SPLIT":
            led.set_state(project, n, "parent", "split into sub-issues")
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
            elif verb == "DONE" and reason in (None, "quota"):
                # D18: the run's own job is finished. The conductor (code, not
                # another agent) ships it from here: push, PR, CI, merge. A DONE
                # build is a success, not a failed attempt — no attempt is
                # counted, and the item leaves the ready queue into `verifying`.
                if run["role"] == "fix":
                    # A fix's DONE goes back to verifying on the same PR: the
                    # new SHA re-triggers CI. The summary stays the build's.
                    led.set_state(project, n, "verifying",
                                  "fix pushed — CI re-runs on the new SHA")
                    ctx.ping(f"Fix pushed — {project} #{n}",
                             f"{run['platform']} ended DONE; CI re-runs on the PR",
                             project, n, priority="low")
                elif saved or item["branch"]:
                    led.set_state(project, n, "verifying",
                                  "build done — the conductor ships it",
                                  summary=rest or item["title"])
                    ctx.ping(f"Build finished — {project} #{n}",
                             f"{run['platform']} ended DONE; the conductor opens the PR next",
                             project, n, priority="low")
                else:
                    _retry_or_fail(ctx, project, n, item, reason, outcome)
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
            elif (verb is None or verb == "DONE") and reason in (None, "timeout"):
                # D18 fallback: no STATUS line (or timed out after DONE), but the branch
                # may still be done. Applies both when run exited cleanly without STATUS,
                # or when it timed out with green tests on uncommitted/committed work (mahler#145).
                # Also: Cline resume-once before giving up (mahler#17, only when reason is None).
                if _try_verify_fallback(ctx, run, pol, saved, item):
                    pass   # handled — state set to verifying
                elif reason is None and _try_cline_nudge(ctx, run, kind, log, pol):
                    return   # run is still alive — finalized again when the nudge ends
                else:
                    _retry_or_fail(ctx, project, n, item, reason, outcome)
            else:
                _retry_or_fail(ctx, project, n, item, reason, outcome)
    led.release(project, n, holder=f"run:{run['id']}", epoch=run["epoch"])
    led.update_run(run["id"], status="ended", outcome=outcome, exit_code=code,
                   ended_at=iso(led.now()))
    if not keep_worktree:
        runner.remove_worktree(pol["path"], run["worktree"], run["branch"],
                               runner.worktree_root(pol))


def _hold_platform(ctx, run):
    """A run that never printed anything says more about the machine than the
    task: the next run on that platform would most likely block the same way.
    Stop starting runs there for a while and tell the owner what to look for."""
    pconf = ctx.cfg["platforms"][run["platform"]]
    until = ctx.led.now() + timedelta(minutes=pconf.get("backoff_minutes", 60))
    ctx.led.record_usage(run["platform"], router.HOLD, 100.0, iso(until))
    ctx.ping(f"Mahler: {run['platform']} runs are stuck at startup",
             f"Run {run['id']} printed nothing for {ctx.policy(run['project'])['startup_timeout_minutes']} "
             "min. Usually a macOS permission dialog is waiting on the Mac mini (e.g. Documents "
             "access for python3 after a Homebrew upgrade): click Allow. "
             f"{run['platform']} is on hold until {until.astimezone():%H:%M}.",
             run["project"], run["number"], priority="high", tags="warning")


def _try_verify_fallback(ctx, run, pol, saved, item):
    """D18 verify-green fallback: no STATUS line, but if the branch has commits
    ahead of base (or uncommitted changes in saved snapshot) *and* the project's
    verify command passes in the worktree, treat as DONE. Returns True if the
    fallback applied."""
    led = ctx.led
    project, n = run["project"], run["number"]
    base = pol.get("base", "main")
    verify_cmd = pol.get("verify")
    if not verify_cmd:
        return False
    ahead = runner.commits_ahead(run["worktree"], base)
    if ahead == 0 and saved and saved.get("ahead", 0) > 0:
        ahead = saved["ahead"]
    if ahead == 0:
        ctx.say(f"{project}#{n}: no STATUS line, no commits ahead of {base} — failed attempt")
        return False
    ctx.say(f"{project}#{n}: no STATUS line but {ahead} commit(s) ahead — running verify")
    ok = runner.verify_in_worktree(run["worktree"], verify_cmd,
                                   timeout=pol.get("verify_timeout", 120))
    if not ok:
        ctx.say(f"{project}#{n}: verify failed in worktree — failed attempt")
        return False
    # The branch is done: the conductor ships it, with an honest note.
    ctx.say(f"{project}#{n}: verify green — treating as DONE (agent didn't confirm)")
    ref = (saved["ref"] if saved else None) or item["branch"]
    if ref:
        led.set_state(project, n, "verifying",
                      "verify-green fallback — agent didn't confirm",
                      summary=item["title"])
        led.set_kv(f"unconfirmed:{project}#{n}", "1")
        ctx.ping(f"Build finished (fallback) — {project} #{n}",
                 f"{run['platform']} didn't end with STATUS: DONE, but "
                 f"verify passes; the conductor opens the PR next",
                 project, n, priority="low")
        return True
    return False


def _try_cline_nudge(ctx, run, kind, log, pol):
    """Resume a Cline session once when it ended with finishReason 'completed'
    but no STATUS line and the verify fallback didn't apply (mahler#17).
    Returns True if the nudge was started (the run stays alive)."""
    if kind != "cline":
        return False
    if run.get("nudged"):
        return False
    code = runner.exit_code(run)
    if code != 0:
        return False
    # The Cline log must show finishReason == "completed"
    if not log.get("ok"):
        return False
    ctx.say(f"{run['project']}#{run['number']}: Cline ended without STATUS — "
            f"nudging once to resume")
    if ctx.dry_run:
        return True
    led = ctx.led
    led.update_run(run["id"], nudged=1, status="running")
    # Find the session ID from cline history
    session_id = _cline_session_id(run["worktree"])
    wt = run["worktree"]
    timeout_secs = int(pol.get("run_timeout_minutes", 60) * 60)
    nudge_prompt = ("You stopped before finishing. Carry on with the next step of "
                    "your instructions, and end with the STATUS line.")
    if session_id:
        argv = [platforms.cline_exe(), "--id", session_id, "--cwd", wt,
                "--json", "--auto-approve", "true", "-t", str(timeout_secs),
                nudge_prompt]
    else:
        # No session ID found — start a fresh prompt in the same worktree
        argv = [platforms.cline_exe(), "--cwd", wt, "--json", "--auto-approve", "true",
                "-t", str(timeout_secs), nudge_prompt]
    log_path = run["log_path"]
    status_path = run["status_path"]
    shell = (f"{shlex.join(argv)} >> {shlex.quote(log_path)} 2>&1; "
             f"echo $? > {shlex.quote(status_path)}")
    proc = subprocess.Popen(["/bin/sh", "-c", shell], cwd=wt,
                            start_new_session=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    led.update_run(run["id"], pid=proc.pid)
    led.heartbeat(run["project"], run["number"], f"run:{run['id']}", run["epoch"],
                  pol["auto_lease_minutes"])
    return True


def _cline_session_id(worktree):
    """Find the Cline session ID whose cwd matches the run's worktree.
    `cline history --json` lists sessions with their cwd."""
    try:
        r = subprocess.run([platforms.cline_exe(), "history", "--json"],
                           capture_output=True, text=True, timeout=15)
        for entry in json.loads(r.stdout):
            if entry.get("cwd") == worktree:
                return entry.get("sessionId") or entry.get("id")
    except (subprocess.SubprocessError, OSError, ValueError, TypeError):
        pass
    return None


SETUP_FAIL_CAP = 2                                   # consecutive setup failures before needs_you


def _setup_failure(ctx, run, item):
    """A build run died in the setup step (exit 97): the environment is broken, not
    the task (issue #8). Post a handoff with the setup.log tail, don't burn an agent
    attempt, and after SETUP_FAIL_CAP in a row hand the item to the owner."""
    led, project, n = ctx.led, run["project"], run["number"]
    pol = ctx.policy(project)
    tail = runner.setup_tail(run)
    fails = led.bump_setup_fails(project, n)
    stuck = fails >= SETUP_FAIL_CAP
    _setup_failed_comment(ctx, run, fails, tail, stuck)
    if stuck:
        led.set_state(project, n, "needs_you",
                      f"setup failed {fails} times in a row — the environment, not the task")
        ctx.ping(f"Mahler needs you — {project} #{n}",
                 f"setup failed {fails} times in a row (setup.log tail is in the handoff comment).",
                 project, n, priority="high", tags="warning")
    else:
        led.set_state(project, n, "ready" if item["sorted_at"] else "inbox",
                      f"setup failed (failure {fails} of {SETUP_FAIL_CAP}) — retrying")
        ctx.ping(f"Setup failed — {project} #{n}",
                 f"run {run['id']}: setup failed ({fails}/{SETUP_FAIL_CAP}); retrying.",
                 project, n, priority="low")
    led.release(project, n, holder=f"run:{run['id']}", epoch=run["epoch"])
    led.update_run(run["id"], status="ended", outcome="setup failed", exit_code=97,
                   ended_at=iso(led.now()))
    runner.remove_worktree(pol["path"], run["worktree"], run["branch"],
                           runner.worktree_root(pol))


def _setup_failed_comment(ctx, run, fails, tail, stuck):
    why = ("this looks like a broken environment, not the task — comment "
           "`/mahler go` to retry once it's fixed" if stuck
           else f"retrying (consecutive setup failures capped at {SETUP_FAIL_CAP})")
    lines = [f"<!-- mahler:handoff run={run['id']} epoch={run['epoch']} "
             f"from={run['platform']} reason=setup-failed -->",
             f"**Setup failed** — run {run['id']} stopped during the project's setup step, "
             f"before the agent started (exit 97). {fails} in a row: {why}", ""]
    if tail:
        lines += ["Last 20 lines of setup.log:", "", "```", tail, "```"]
    else:
        lines.append("(setup.log was empty or missing)")
    try:
        ctx.gh(run["project"]).comment(run["number"], "\n".join(lines))
    except GHError as e:
        ctx.say(f"#{run['number']}: couldn't post setup-failure comment — {e}")


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
    "silent": "never started — printed nothing",
    "closed": "the issue was closed", "parked": "parked", "lost-lease": "lost its lease",
    "setup-failed": "the project's setup step failed (exit 97)",
}


def _handoff_comment(ctx, run, item, reason, outcome, saved, log, kept):
    mins = int((ctx.led.now() - parse(run["started_at"])).total_seconds() // 60)
    why = REASON_TEXT.get(reason, f"ended: {outcome}")
    lines = [f"<!-- mahler:agent handoff run={run['id']} epoch={run['epoch']} "
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


# ---------- the conductor ships (DESIGN D18) ----------

def ship(ctx, projects):
    """The mechanical tail of a build run, in code (D18): push the branch, open
    the PR, watch CI across ticks and squash-merge on green — but never merge
    once the item's lease has gone to an interactive session (D6)."""
    for p in projects:
        try:
            _ship_project(ctx, p["name"])
        except Exception as e:                  # noqa: BLE001 — a tick must not break
            ctx.say(f"{p['name']}: ship pass failed — {e}")


def _ship_project(ctx, project):
    for item in ctx.led.items(project, ["verifying"]):
        try:
            _ship_item(ctx, project, item)
        except Exception as e:                  # noqa: BLE001 — one item can't stop the rest
            ctx.say(f"{project}#{item['number']}: shipping failed — {e}")


def _ship_item(ctx, project, item):
    led, n = ctx.led, item["number"]
    if ctx.dry_run:
        ctx.say(f"{project}#{n}: would ship "
                f"{'PR #' + str(item['pr']) if item['pr'] else '(opening the PR)'}")
        return
    gh, pol = ctx.gh(project), ctx.policy(project)
    lease, info = led.claim(project, n, CONDUCTOR, "auto", pol["auto_lease_minutes"])
    if lease is None:                           # a session pre-empted the item (D6)
        pr = f"PR #{item['pr']}" if item["pr"] else "its PR (not yet open)"
        led.set_state(project, n, "working",
                      f"handed to {info['held_by']['holder']} — {pr} stays open, unmerged")
        ctx.ping(f"Handoff to you — {project} #{n}",
                 "you hold this item now; the conductor won't merge " + pr,
                 project, n, priority="low")
        return
    if not item["pr"]:
        unconfirmed = bool(led.get_kv(f"unconfirmed:{project}#{n}"))
        _open_pr(ctx, project, item, gh, pol, unconfirmed=unconfirmed)
        return                                  # CI is watched from the next tick
    pr = item["pr"]
    view = gh.pr_view(pr)
    if view["state"] != "OPEN":                 # merged or closed outside Mahler
        _shipped(ctx, project, n, pr, led.item(project, n), view, merged=False)
        return
    if view.get("mergeable") == "CONFLICTING":
        # base moved under it: rebuild on current base (D19); no attempt counted
        base = pol.get("base", "main")
        led.upsert_item(project, n, pr=None)
        led.set_state(project, n, "ready", f"PR #{pr} conflicts with {base} — rebuilding on it")
        led.release(project, n, holder=CONDUCTOR)
        ctx.ping(f"Rebuilding — {project} #{n}",
                 f"PR #{pr} no longer merges into {base}; the next build starts on current {base}",
                 project, n, priority="low")
        return
    state = checks_state(view.get("statusCheckRollup"))
    if state == "pending" or view.get("mergeable") == "UNKNOWN":
        _ci_pending(ctx, project, item, pr, view)
        return
    if state == "red":
        # D18 (mahler#18): red CI starts a fix run from the PR branch, with the
        # failing log in its prompt. The conductor's lease goes to the run.
        _red_ci(ctx, project, item, pr, view)
        return
    gh.pr_merge(pr)
    _shipped(ctx, project, n, pr, led.item(project, n), view)


def _ci_pending(ctx, project, item, pr, view):
    """CI still running. It is watched across ticks (the 30s loop never blocks
    a tick), but a hung CI must not park the item silently: past
    verify_timeout_minutes it goes to needs-you with a ping. A new head SHA
    (e.g. a fix run's push) restarts the wait, because CI starts over."""
    led, n = ctx.led, item["number"]
    pol = ctx.policy(project)
    key = f"ci:{project}#{n}:{pr}"
    sha = view.get("headRefOid") or ""
    seen = led.get_kv(key)
    info = json.loads(seen) if seen else None
    if not info or info.get("sha") != sha:
        info = {"sha": sha, "since": iso(led.now())}
        led.set_kv(key, json.dumps(info))
    if led.now() - parse(info["since"]) <= timedelta(minutes=pol["verify_timeout_minutes"]):
        ctx.say(f"{project}#{n}: PR #{pr} — CI still running")
        return
    elapsed = int((led.now() - parse(info["since"])).total_seconds() // 60)
    led.set_state(project, n, "needs_you",
                  f"CI on PR #{pr} still pending after {elapsed} min")
    ctx.ping(f"Mahler needs you — {project} #{n}",
             f"CI on PR #{pr} hasn't finished in {elapsed} min; "
             "the PR stays open, unmerged",
             project, n, priority="high", tags="question")
    led.release(project, n, holder=CONDUCTOR)


def _red_ci(ctx, project, item, pr, view):
    """Red CI on a verifying item: a fix run (D18's second run role) starts on
    the PR's head branch, its prompt carrying the failing-log tail (runner
    fetches it). Routing is the build routing (D8), and `max_attempts` caps
    build and fix runs together — each red cycle counts as an attempt — then
    escalation as today (_retry_or_fail's stuck branch)."""
    led, cfg, n = ctx.led, ctx.cfg, item["number"]
    pol = ctx.policy(project)
    head = view.get("headRefName") or item["branch"]
    attempts = item["attempts"] + 1
    if attempts >= pol["max_attempts"]:
        led.set_state(project, n, "failed",
                      f"CI still red on PR #{pr} after {attempts} attempts", attempts=attempts)
        ctx.ping(f"Stuck — {project} #{n}",
                 f"CI stayed red ({attempts} attempts). Comment `/mahler go` to retry.",
                 project, n, priority="high", tags="warning")
        led.release(project, n, holder=CONDUCTOR)
        return

    key = f"red:{project}#{n}:{pr}:{view.get('headRefOid') or ''}"
    if not led.get_kv(key):
        led.set_kv(key, iso(led.now()))
        ctx.ping(f"CI red — {project} #{n}",
                 f"PR #{pr} failed CI; the conductor starts a fix run on it",
                 project, n, priority="high", tags="warning")
    active = led.active_runs()
    if len(active) >= cfg["concurrency"]["total"]:
        ctx.say(f"{project}#{n}: PR #{pr} — CI red, but every run slot is busy; "
                "the fix waits for the next tick")
        return
    per_platform = {}
    for r in active:
        per_platform[r["platform"]] = per_platform.get(r["platform"], 0) + 1
    busy = {k for k, v in per_platform.items()
            if v >= cfg["platforms"].get(k, {}).get("max_runs", 1)}
    busy |= {k for k, pc in cfg["platforms"].items() if not platforms.available(pc)}
    size = next((l.split(":", 1)[1] for l in json.loads(item["labels"] or "[]")
                 if l.startswith("size:")), None)
    # For fix runs, treat size:l as size:m so a CI fix never needs Opus by size alone (DESIGN D21)
    if size == "l":
        size = "m"
    platform, reasons = router.pick(cfg, led, "fix", item["pin"], busy,
                                    size=size, burst_lines=ctx.burst_lines)
    if not platform:
        ctx.say(f"{project}#{n}: PR #{pr} — CI red, no platform for a fix run — "
                f"{'; '.join(reasons)}")
        return
    led.upsert_item(project, n, branch=head)
    led.release(project, n, holder=CONDUCTOR)   # the lease passes to the fix run
    if start(ctx, project, {**item, "branch": head}, "fix", platform):
        led.upsert_item(project, n, attempts=attempts)


def _open_pr(ctx, project, item, gh, pol, unconfirmed=False):
    led, n = ctx.led, item["number"]
    ref = item["branch"]
    if not ref:
        _retry_or_fail(ctx, project, n, led.item(project, n), None, "nothing to ship")
        led.release(project, n, holder=CONDUCTOR)
        return
    base = pol.get("base", "main")
    branch = f"mahler/{n}-{runner.slug(item['title'])}"
    sha = gh.push_branch(pol["path"], branch, ref)   # the branch, pushed if needed
    pr = gh.pr_for_head(branch)
    if pr is None:
        needs = needs_human_of(gh.issue_body(n))
        pr = gh.pr_create(branch, base, item["title"],
                          pr_body(n, item["summary"], needs, unconfirmed=unconfirmed))
    led.upsert_item(project, n, pr=pr)
    led.event("pr_opened", project, n, {"pr": pr, "branch": branch, "sha": sha})
    ctx.say(f"{project}#{n}: opened PR #{pr} from `{branch}` (base {base}) — verifying")


def _shipped(ctx, project, n, pr, item, view, merged=True):
    """Close the loop: comment the summary plus the issue's 'Needs a human to
    check' list, ping, and mark the item done."""
    led = ctx.led
    how = "squash-merged" if merged else view["state"].lower()
    lines = [f"**Shipped** — PR #{pr} {how}.", "",
             item["summary"] or pr_summary_of(view.get("body")) or ""]
    needs = needs_human_of(view.get("body"))
    if needs:
        lines += ["", "## Needs a human to check", needs]
    try:
        ctx.gh(project).comment(n, "\n".join(lines))
    except GHError as e:
        ctx.say(f"{project}#{n}: couldn't post the shipped comment — {e}")
    ctx.ping(f"Shipped — {project} #{n}", item["title"], project, n, tags="rocket")
    led.set_state(project, n, "done", f"shipped via PR #{pr}")
    maintenance = config.maintenance_policy(ctx.cfg, project)
    passes = maintenance["passes"] if maintenance["enabled"] else ()
    led.event("shipped", project, n, {"pr": pr}, passes=passes)
    led.release(project, n, holder=CONDUCTOR)


# ---------- GitHub sync ----------

def sync(ctx, project):
    led, gh = ctx.led, ctx.gh(project)
    pol = ctx.policy(project)
    issues = gh.open_issues()

    if pol.get("scope") == "label":
        scope_label = pol["scope_label"]
        in_scope_nums = {
            iss["number"] for iss in issues
            if scope_label in label_names(iss)
            or any(l in LABEL_STATES for l in label_names(iss))
            or led.item(project, iss["number"]) is not None
        }
        # If child issues reference an in-scope parent via "Part of #N", inherit scope
        changed = True
        while changed:
            changed = False
            for iss in issues:
                n = iss["number"]
                if n not in in_scope_nums:
                    parent_n = part_of(iss.get("body"))
                    if parent_n and (parent_n in in_scope_nums or led.item(project, parent_n) is not None):
                        in_scope_nums.add(n)
                        changed = True
    else:
        in_scope_nums = None

    open_nums = set()
    for iss in issues:
        n = iss["number"]
        labels = label_names(iss)
        if in_scope_nums is not None and n not in in_scope_nums:
            continue                      # not (yet) handed to Mahler
        if pol.get("scope") == "label" and pol["scope_label"] not in labels:
            labels.append(pol["scope_label"])
            if not ctx.dry_run:
                try:
                    gh.add_label(n, pol["scope_label"])
                except GHError:
                    pass
        open_nums.add(n)
        ctx._labels[(project, n)] = labels
        fields = dict(title=iss["title"], labels=json.dumps(labels), priority=priority_of(labels),
                      depends=json.dumps(depends_of(iss.get("body"))), pin=pin_of(labels),
                      parent=part_of(iss.get("body")))
        item = led.item(project, n)
        if item is None:
            state = _state_from_labels(labels)
            planned = state is None and planned_child(iss, labels, led, project)
            if state is None:
                state = "ready" if planned else "inbox"
            extra = {"sorted_at": iso(led.now())} if state == "ready" else {}
            led.upsert_item(project, n, created_at=iss["createdAt"], **fields, **extra)
            why = "born ready (planned under #{})".format(part_of(iss.get("body"))) \
                if planned else "new issue"
            led.set_state(project, n, state, why)
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


def planned_child(iss, labels, led, project):
    """Whether a newly-synced sub-issue was fully planned by its parent."""
    parent_n = part_of(iss.get("body"))
    if parent_n is None:
        return False
    parent = led.item(project, parent_n)
    if parent is None or parent["state"] != "parent":
        return False
    sizes = {label.split(":", 1)[1] for label in labels
             if label.startswith("size:")}
    return (("s" in sizes or "m" in sizes)
            and has_sections(iss.get("body"), "## Plan", "## Done when"))


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
        led.set_state(project, n, "ready", "you said go", attempts=0, setup_fails=0,
                      sorted_at=iso(led.now() - timedelta(days=1)))
    elif verb == "park":
        led.set_state(project, n, "parked", "you parked it")
        for run in led.active_runs(project):
            if run["number"] == n:
                request_stop(ctx, run, "parked")
    elif verb == "inbox":
        led.set_state(project, n, "inbox", "back to inbox", sorted_at=None)
    elif verb == "platform" and arg:
        if arg in ("none", "auto"):
            _set_pin(ctx, project, n, None)
        elif arg in ctx.cfg["platforms"]:
            _set_pin(ctx, project, n, arg)
        else:
            ctx.say(f"{project}#{n}: unknown platform {arg!r}")
    ctx.say(f"{project}#{n}: instruction '{verb}{' ' + arg if arg else ''}'")


def _set_pin(ctx, project, n, platform):
    """`platform:*` labels on GitHub are the pin's store of record (mahler#20):
    sync() re-derives pin=pin_of(labels) on every tick, so a ledger-only pin
    lasted exactly one tick. The command edits the labels instead, then mirrors
    the change into the ledger so it takes effect in this tick's schedule."""
    current = ctx._labels.get((project, n)) or []
    if not ctx.dry_run:
        try:
            ctx.gh(project).set_pin_labels(n, platform, current)
        except GHError as e:
            ctx.say(f"{project}#{n}: platform label update failed — {e}")
        keep = f"platform:{platform}" if platform else None
        updated = [l for l in current if not l.startswith("platform:") or l == keep]
        if keep and keep not in updated:
            updated.append(keep)
        ctx._labels[(project, n)] = updated
    ctx.led.upsert_item(project, n, pin=platform)


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
            wanted |= set(cfg["routing"]["sort"]) | set(cfg["routing"]["build"]) | set(cfg["routing"].get("plan", []))
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
        if router.usage_state(led, name, pconf)[0] != "stale":
            continue
        if pconf.get("kind") == "copilot":
            last = parse(led.get_kv(f"probe:{name}"))
            if last and led.now() - last < timedelta(minutes=pconf.get("stale_minutes", 15)):
                continue
            led.set_kv(f"probe:{name}", iso(led.now()))
            for w, pct, resets in platforms.probe_copilot(pconf.get("monthly_cap_credits", 1500)):
                led.record_usage(name, w, pct, resets)
            continue
        if pconf.get("kind") != "claude":
            continue
        free = platforms.oauth_usage()                 # zero tokens
        if free:
            _record_claude_usage(ctx, free, check_human=True)
            continue
        last = parse(led.get_kv(f"probe:{name}"))
        if last and led.now() - last < timedelta(minutes=pconf.get("stale_minutes", 15)):
            continue
        for cname in [cn for cn, cp in cfg["platforms"].items() if cp.get("kind") == "claude"]:
            led.set_kv(f"probe:{cname}", iso(led.now()))
        probed = platforms.probe_claude()
        if probed:
            _record_claude_usage(ctx, probed, check_human=True)


# ---------- schedule ----------

def _candidates(ctx, projects):
    """One global candidate list of (pol, role, item) across all enabled
    projects — sorts and settled builds compete for the same slots (mahler#9)."""
    led = ctx.led
    work = []
    for p in projects:
        name = p["name"]
        done = {i["number"] for i in led.items(name, ["done"])}
        for it in led.items(name, ["inbox"]):
            work.append((p, "sort", it))
        for it in led.items(name, ["ready"]):
            sorted_at = parse(it["sorted_at"])
            if sorted_at and led.now() - sorted_at < timedelta(minutes=p["settle_minutes"]):
                continue
            deps = [d for d in json.loads(it["depends"] or "[]") if d not in done]
            if deps:
                continue
            work.append((p, "build", it))
    return work


def _headroom(ctx, role, per_platform, busy, burst_lines=None):
    """Routing platforms for `role` that could take a new run right now:
    enabled, under per-platform max_runs, reachable and under its soft lines.
    Burst lines (D23) raise Claude's soft lines when a window is about to reset.
    """
    cfg, led = ctx.cfg, ctx.led
    free = []
    for name in router.candidates(cfg, role, burst_lines=burst_lines):
        pc = cfg["platforms"][name]
        if name in busy or per_platform.get(name, 0) >= pc.get("max_runs", 1):
            continue
        claude_lines = burst_lines if pc.get("kind") == "claude" else None
        if router.usage_state(led, name, pc, burst_lines=claude_lines)[0] != "ok":
            continue
        free.append(name)
    return free


def needs_plan(labels_json):
    """True when the labels indicate this item needs Opus planning (DESIGN D21):
    type:goal, size:l, or any pass:* label."""
    try:
        labels = json.loads(labels_json or "[]")
    except json.JSONDecodeError:
        return False
    for label in labels:
        if label == "type:goal" or label == "size:l" or label.startswith("pass:"):
            return True
    return False


def _compute_burst(ctx, projects):
    """D23 burst lines for Claude routing (cached per tick).

    In the last lead-time before a window resets, Claude's reserve expires
    unused, so burst lines (default 90%/97%) let Claude build first with higher
    headroom. Suppressed while you're actively using Claude: the 5h-usage-rise
    flag set by _record_claude_usage during the probe, or recent Claude Code
    transcript activity in a managed project. Returns the cached value on
    repeated calls within one tick. Logs the burst state once per tick.
    """
    if ctx.burst_lines is not None:
        return ctx.burst_lines
    lines = router.burst_status(ctx.cfg, ctx.led)
    suppressed = None
    if lines and ctx.hot_hold:
        flag = ctx.led.get_kv("human:claude")
        if flag and parse(flag) and ctx.led.now() - parse(flag) < timedelta(minutes=ctx.cfg["burst"].get("human_quiet_minutes", 20)):
            suppressed = "5h usage rose with no live Claude run"
            lines = None
        elif presence.human_claude_active(projects):
            suppressed = "Claude in use (recent transcript activity)"
            lines = None
    if suppressed:
        ctx.say(f"D23: burst window open — deferring ({suppressed})")
    elif lines:
        kind = router.burst_kind(lines)
        scope = "5h and weekly" if kind == "weekly" else "5h only"
        ctx.say(f"D23: {kind} burst active — Claude builds first, lines 90/97 (scope: {scope})")
    ctx.burst_lines = lines
    return lines


def schedule(ctx, projects):
    """Hand out run slots across every project at once (mahler#9).

    Order: priority first; then ready builds before inbox sorts when the
    platform a sort would choose is the last one with headroom (sorts must
    not eat a scarce builder); then priority projects (mahler work prioritized
    over other projects, all else being equal); then oldest state_changed_at.
    The ordered list is then dealt round-robin by project — each pass starts
    at most one run per project — so no project waits more than one slot
    behind another. Per-project max_parallel, per-platform max_runs, leases,
    hot hold and the settle/dependency gates all still apply.
    """
    led, cfg = ctx.led, ctx.cfg
    active = led.active_runs()
    total = len(active)
    in_project, per_platform = {}, {}
    for r in active:
        in_project[r["project"]] = in_project.get(r["project"], 0) + 1
        per_platform[r["platform"]] = per_platform.get(r["platform"], 0) + 1
    busy = {k for k, v in per_platform.items()
            if v >= cfg["platforms"].get(k, {}).get("max_runs", 1)}
    busy |= {k for k, pc in cfg["platforms"].items() if not platforms.available(pc)}
    # a finished build keeps its project's build slot until it merges (D19)
    in_flight = {p["name"]: len(led.items(p["name"], ["verifying"])) for p in projects}

    hot = {}
    for p in projects:
        hot[p["name"]] = False
        if p.get("hot_hold") and ctx.hot_hold:
            last = presence.last_claude_activity(p["path"])
            hot[p["name"]] = bool(last and led.now() - last < timedelta(minutes=p["hot_hold_minutes"]))

    # Burst before a Claude window resets (D23): in the last lead-time before a
    # window rolls over, Claude's reserve expires unused, so burst lines let
    # Claude build first with higher headroom. Never while you're using Claude.
    burst_lines = _compute_burst(ctx, projects)

    work = _candidates(ctx, projects)
    sorts_wait = len(_headroom(ctx, "sort", per_platform, busy, burst_lines)) <= 1
    priority_projects = cfg.get("scheduling", {}).get("priority_projects", ["mahler"])

    def key(c):
        p, role, it = c
        proj_idx = (priority_projects.index(p["name"])
                    if p["name"] in priority_projects
                    else len(priority_projects))
        return (it["priority"],
                1 if (sorts_wait and role == "sort") else 0,
                proj_idx,
                parse(it["state_changed_at"]) or datetime.min.replace(tzinfo=timezone.utc),
                p["name"], it["number"])
    work.sort(key=key)

    said = set()             # projects already told they're at capacity
    while work and total < cfg["concurrency"]["total"]:
        started = set()      # projects that took a slot this pass
        for cand in list(work):
            p, role, it = cand
            name, n = p["name"], it["number"]
            if name in started:
                continue                    # its next item waits for the next pass
            work.remove(cand)
            if total >= cfg["concurrency"]["total"]:
                work.clear()
                break
            if in_project.get(name, 0) >= p["max_parallel"]:
                if name not in said:
                    said.add(name)
                    ctx.say(f"{name}: at capacity ({total} running)")
                continue
            if role == "build" and in_project.get(name, 0) + in_flight[name] >= p["max_parallel"]:
                if name not in said:
                    said.add(name)
                    ctx.say(f"{name}: builds wait — {in_flight[name]} finished change(s) "
                            "not merged yet")
                continue
            if role == "build" and hot[name]:
                ctx.say(f"{name}#{n}: hot hold — a Claude session is active in this project")
                continue
            if led.lease(name, n):
                continue
            size = next((l.split(":", 1)[1] for l in json.loads(it["labels"] or "[]")
                         if l.startswith("size:")), None)
            if role == "sort" and needs_plan(it["labels"]):
                routing_role = "plan"
            else:
                routing_role = role
            platform, reasons = router.pick(cfg, led, routing_role,
                                            it["pin"] if role == "build" else None,
                                            busy, size=size, burst_lines=burst_lines)
            if not platform:
                if routing_role == "plan":
                    ctx.say(f"{name}#{n}: waits for planning (routing.plan) — {'; '.join(reasons)}")
                else:
                    ctx.say(f"{name}#{n}: no platform for {role} — {'; '.join(reasons)}")
                continue
            if ctx.dry_run:
                ctx.say(f"{name}#{n}: would {role} on {platform}")
            elif not start(ctx, name, it, role, platform):
                continue
            total += 1
            in_project[name] = in_project.get(name, 0) + 1
            per_platform[platform] = per_platform.get(platform, 0) + 1
            if per_platform[platform] >= cfg["platforms"][platform].get("max_runs", 1):
                busy.add(platform)
            started.add(name)


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
    elif role == "fix":
        led.set_state(project, n, "working",
                      f"{platform} fix run {run_id} — CI red on PR #{item['pr']}")
        try:
            ctx.gh(project).comment(n, f"🔁 **{platform}** started a fix run (run {run_id}) on "
                                       f"branch `{meta['branch']}` — CI was red.")
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
