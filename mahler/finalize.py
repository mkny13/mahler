"""Every exit is a handoff (DESIGN D9).

A run that ended — cleanly, out of quota, timed out or killed — passes
through here exactly once: its log is read for usage and a STATUS line, its
work is snapshotted to a pushed ref, the issue gets a handoff comment, and
the item moves to whatever state its outcome implies.
"""

import json
import subprocess
from datetime import timedelta

from . import platforms, router, runner
from .gh import GHError
from .ledger import CONDUCTOR, iso, parse, row_get
from .usage import record_claude_usage

NO_ATTEMPT = ("quota", "preempted", "closed", "parked", "lost-lease", "silent")


class Ending:
    """One run's ending, as the outcome handlers below need it.

    Built once by `finalize` after the log has been read, then passed to the
    one handler whose rule matches. `saved`, `keep_worktree` and `closed` are
    filled in by `_save_work` before the dispatch.
    """

    def __init__(self, ctx, run, item, pol, log, kind, verb, rest, reason, outcome):
        self.ctx, self.led, self.run, self.item, self.pol = ctx, ctx.led, run, item, pol
        self.log, self.kind = log, kind
        self.verb, self.rest, self.reason, self.outcome = verb, rest, reason, outcome
        self.project, self.number = run["project"], run["number"]
        self.saved, self.keep_worktree, self.closed = None, False, False
        self.duration_mins = None
        started_at = row_get(run, "started_at")
        if started_at:
            try:
                self.duration_mins = (ctx.led.now() - parse(started_at)).total_seconds() / 60.0
            except Exception:                   # noqa: BLE001 — an unreadable timestamp
                pass                            #   must not cost us the finalize

    def set_state(self, state, why, **cols):
        self.led.set_state(self.project, self.number, state, why, **cols)

    def ping(self, title, message, **kw):
        self.ctx.ping(title, message, self.project, self.number, **kw)


# ---------- what each ending means ----------

def _retry(e):
    """No usable outcome: another attempt, or `failed` once they run out."""
    retry_or_fail(e.ctx, e.project, e.number, e.item, e.reason, e.outcome,
                  platform=e.run["platform"], duration_mins=e.duration_mins)
    return True


def _needs_you(e):
    """The agent asked a question only the owner can answer (DESIGN D13)."""
    e.set_state("needs_you", e.rest)
    e.ping(f"Mahler needs you — {e.project} #{e.number}", e.rest or e.item["title"],
           priority="high", tags="question")
    return True


def _sorted_ready(e):
    e.set_state("ready", "sorted", sorted_at=iso(e.led.now()))
    return True


def _sorted_split(e):
    e.set_state("parent", "split into sub-issues")
    return True


SORT_OUTCOMES = {"READY": _sorted_ready, "SPLIT": _sorted_split, "NEEDS-YOU": _needs_you}


def _ended_done(e):
    """D18: the run's own job is finished. The conductor (code, not another
    agent) ships it from here: push, PR, CI, merge. A DONE build is a success,
    not a failed attempt — no attempt is counted, and the item leaves the ready
    queue into `verifying`."""
    if e.run["role"] == "fix":
        # A fix's DONE goes back to verifying on the same PR: the new SHA
        # re-triggers CI. The summary stays the build's.
        e.set_state("verifying", "fix pushed — CI re-runs on the new SHA")
        e.ping(f"Fix pushed — {e.project} #{e.number}",
               f"{e.run['platform']} ended DONE; CI re-runs on the PR", priority="low")
        return True
    if not (e.saved or e.item["branch"]):
        return _retry(e)                        # DONE, but nothing to ship
    e.set_state("verifying", "build done — the conductor ships it",
                summary=e.rest or e.item["title"])
    e.ping(f"Build finished — {e.project} #{e.number}",
           f"{e.run['platform']} ended DONE; the conductor opens the PR next",
           priority="low")
    return True


def _ended_parked(e):
    e.set_state("parked", "parked while running")
    return True


def _ended_preempted(e):
    cur = e.led.lease(e.project, e.number)
    if cur and cur["kind"] == "interactive":
        e.set_state("working", "handed to your session")
    else:
        e.set_state("ready", "handoff (preempted)")
    e.ping(f"Handoff to you — {e.project} #{e.number}",
           f"{e.run['platform']} stepped aside; its work is on "
           f"{e.saved['ref'] if e.saved else 'nothing new to save'}", priority="low")
    return True


def _ended_out_of_reach(e):
    """Quota or a lost lease: the item goes back in the queue for whichever
    platform can afford it next (D9)."""
    e.set_state("ready", f"handoff ({e.reason})")
    e.ping(f"Handoff — {e.project} #{e.number}",
           f"{e.run['platform']} stopped ({e.reason}); next platform picks it up",
           priority="low")
    return True


def _ended_unconfirmed(e):
    """D18 fallback: no STATUS line (or a timeout after DONE), but the branch
    may still be done — both when the run exited cleanly without STATUS and
    when it timed out with green tests on committed or uncommitted work
    (mahler#145). A Cline run gets one resume before we give up (mahler#17).
    -> False when that nudge restarted it, so finalize leaves it alone."""
    if _try_verify_fallback(e.ctx, e.run, e.pol, e.saved, e.item):
        return True                             # handled — state set to verifying
    if e.reason is None and _try_cline_nudge(e.ctx, e.run, e.kind, e.log, e.pol):
        return False                            # alive again — finalized when it ends
    return _retry(e)


# Tried in order: the first rule that matches decides the ending. The order is
# the contract — a NEEDS-YOU question outranks the reason the run stopped, and
# the verify-green fallback is the last word before an attempt is counted.
ENDINGS = (
    (lambda e: e.verb == "NEEDS-YOU", _needs_you),
    (lambda e: e.verb == "DONE" and e.reason in (None, "quota"), _ended_done),
    (lambda e: e.reason == "parked", _ended_parked),
    (lambda e: e.reason == "preempted", _ended_preempted),
    (lambda e: e.reason in ("quota", "lost-lease"), _ended_out_of_reach),
    (lambda e: (e.verb is None or e.verb == "DONE") and e.reason in (None, "timeout"),
     _ended_unconfirmed),
)


def _dispatch(e):
    """-> False when the run is alive again and must not be closed out."""
    for matches, handle in ENDINGS:
        if matches(e):
            return handle(e)
    return _retry(e)


# ---------- finalize ----------

def _backoff_until(ctx, run, log):
    """When a quota error named its own reset time, honour it (mahler#124);
    otherwise wait the platform's flat backoff. None when quota wasn't hit."""
    if not log["quota_hit"]:
        return None
    pconf = ctx.cfg["platforms"][run["platform"]]
    minutes = log["retry_after"] if log["retry_after"] is not None \
        else pconf.get("backoff_minutes", 60)
    return iso(ctx.led.now() + timedelta(minutes=minutes))


def _record_run_usage(ctx, run, kind, log):
    """Everything the run's own log says about what it spent."""
    led, until = ctx.led, _backoff_until(ctx, run, log)
    if kind == "claude":
        # one reading covers every platform sharing that Claude login (D21, D25)
        record_claude_usage(ctx, log["usage"], backoff_until=until, platform=run["platform"])
        return
    for w, pct, resets in log["usage"]:
        led.record_usage(run["platform"], w, pct, resets)
    if until:
        for w in ctx.cfg["platforms"][run["platform"]].get("windows", router.WINDOWS):
            led.record_usage(run["platform"], w, 100.0, until)


def _save_work(e):
    """Whatever the run left becomes a pushed ref and a handoff comment (D9) —
    unless its own merge already closed the issue, in which case it is done."""
    ctx, led = e.ctx, e.led
    try:
        e.closed = ctx.gh(e.project).issue_state(e.number) == "CLOSED"
    except GHError:
        pass
    if e.closed:
        e.set_state("done", e.outcome)
        if e.verb == "MERGED":
            e.ping(f"Shipped — {e.project} #{e.number}", e.item["title"], tags="rocket")
        return
    try:
        e.saved = runner.snapshot(e.pol["path"], e.run["worktree"], e.run["id"], e.number,
                                  e.pol.get("base", "main"))
    except runner.GitError as err:
        e.keep_worktree = True
        ctx.say(f"{e.project}#{e.number}: snapshot failed, keeping worktree — {err}")
    if e.saved:
        led.upsert_item(e.project, e.number, branch=e.saved["ref"])
    _handoff_comment(ctx, e.run, e.item, e.reason, e.outcome, e.saved, e.log, e.keep_worktree)


def _close_the_books(e, code):
    """The lease, the run row and the worktree, once the ending is decided."""
    ctx, led, run = e.ctx, e.led, e.run
    # A finished change keeps the canonical project slot while the conductor
    # opens/watches/merges its PR (D19, D24). Transfer the same item lease in
    # one transaction so a second machine cannot claim another issue in the
    # release/claim gap. On transport failure the run lease is left to expire.
    if led.item(e.project, e.number)["state"] == "verifying":
        transferred, info = led.claim(
            e.project, e.number, CONDUCTOR, "auto", e.pol["auto_lease_minutes"],
            handoff_from=(f"run:{run['id']}", run["epoch"]))
        if transferred is None:
            detail = info.get("unavailable") or "canonical lease transfer refused"
            ctx.say(f"{e.project}#{e.number}: {detail}; existing lease left to expire safely")
    else:
        led.release(e.project, e.number, holder=f"run:{run['id']}", epoch=run["epoch"])
    update_cols = {"status": "ended", "outcome": e.outcome, "exit_code": code,
                   "ended_at": iso(led.now())}
    if not run["stop_reason"] and e.reason:     # mahler#124: record why it stopped
        update_cols["stop_reason"] = e.reason
    if e.log.get("model") and e.log["model"] != run["model"]:
        # kilo-auto/free is stateless per invocation — the model actually used
        # is the signal for whether a quota hit reflects one underlying free
        # model being rate-limited rather than the whole pool (mahler#141).
        update_cols["model"] = e.log["model"]
    led.update_run(run["id"], **update_cols)
    _check_estimate_calibration(ctx)
    if not e.keep_worktree:
        runner.remove_worktree(e.pol["path"], run["worktree"], run["branch"],
                               runner.worktree_root(e.pol))


def finalize(ctx, run):
    """A run that ended passes through here exactly once."""
    led = ctx.led
    project, n = run["project"], run["number"]
    pol = ctx.policy(project)
    item = led.item(project, n)
    kind = ctx.cfg["platforms"][run["platform"]]["kind"]
    log = platforms.read_log(run["log_path"], kind)
    _record_run_usage(ctx, run, kind, log)

    verb, rest = platforms.status_line(log["final"] or log["last_text"])
    code = runner.exit_code(run)
    setup_failed = code == 97 and run["role"] == "build"   # setup died before the agent ran
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
    if setup_failed:
        _setup_failure(ctx, run, item)
        return
    led.reset_setup_fails(project, n)

    ending = Ending(ctx, run, item, pol, log, kind, verb, rest, reason, outcome)
    if run["role"] == "sort":
        SORT_OUTCOMES.get(verb, _retry)(ending)
    else:
        _save_work(ending)
        if not ending.closed and not _dispatch(ending):
            return                  # a nudge resumed it; it finalizes again when it ends
    _close_the_books(ending, code)


def _check_estimate_calibration(ctx):
    """Periodically compare predicted vs actual durations and refine estimates (mahler#59)."""
    est_cfg = ctx.cfg.get("estimates", {})
    interval = est_cfg.get("calibration_interval", 10)
    window = est_cfg.get("calibration_window", 20)

    cur = int(ctx.led.get_kv("runs_since_calibration") or "0") + 1
    if cur >= interval:
        stats = ctx.led.calibrate_estimates(window=window)
        ctx.led.set_kv("runs_since_calibration", "0")
        if stats:
            ctx.say(f"Calibrated time estimates over {stats['samples']} runs: "
                    f"factor={stats['factor']:.2f}, MAE={stats['mae']}m")
    else:
        ctx.led.set_kv("runs_since_calibration", str(cur))


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
    if dict(run).get("nudged"):
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
    # mahler#70: process lifecycle is runner's job, so the resume goes out
    # through the same detached, shlex-quoted spawn a launch uses — appending
    # to the run's own log, so read_log still sees one conversation.
    pid = runner.spawn(argv, wt, run["log_path"], run["status_path"], append=True)
    led.update_run(run["id"], pid=pid)
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


def retry_or_fail(ctx, project, n, item, reason, outcome, platform=None, duration_mins=None):
    led = ctx.led
    if reason in NO_ATTEMPT:
        led.set_state(project, n, "ready" if item["sorted_at"] else "inbox", f"retry ({reason})")
        return
    attempts = item["attempts"] + 1

    cur_tier = row_get(item, "esc_tier", 0)
    cur_fails = row_get(item, "esc_fails", 0)
    new_tier = cur_tier
    new_fails = cur_fails + 1

    pconf = ctx.cfg.get("platforms", {}).get(platform, {}) if platform else {}
    run_tier = router.tier_of(pconf) if platform else 1

    # Overrun heuristic: size:s run took >= 10m on tier 1 and failed
    labels = json.loads(row_get(item, "labels", "[]"))
    size = next((l.split(":", 1)[1] for l in labels if l.startswith("size:")), None)
    overrun = (run_tier == 1 and size == "s" and duration_mins is not None and duration_mins >= 10.0)

    if overrun:
        new_tier = max(cur_tier, run_tier) + 1
        new_fails = 0
        ctx.say(f"{project}#{n}: escalated to tier {new_tier} — size:s overrun on tier {run_tier} ({int(duration_mins)}m >= 10m)")
        led.event("escalated", project, n, f"tier {cur_tier} -> {new_tier} (duration overrun {int(duration_mins)}m)")
    elif new_fails >= 2:
        new_tier = max(cur_tier, run_tier) + 1
        new_fails = 0
        ctx.say(f"{project}#{n}: escalated to tier {new_tier} after 2 failures on tier <= {max(cur_tier, run_tier)}")
        led.event("escalated", project, n, f"tier {cur_tier} -> {new_tier} (after 2 failures)")

    if attempts >= ctx.policy(project)["max_attempts"]:
        led.set_state(project, n, "failed", f"{attempts} failed attempts — last: {outcome}",
                      attempts=attempts, esc_tier=new_tier, esc_fails=new_fails)
        ctx.ping(f"Stuck — {project} #{n}",
                 f"{attempts} attempts failed ({outcome}). Comment `/mahler go` to retry.",
                 project, n, priority="high", tags="warning")
    else:
        back = "ready" if item["sorted_at"] else "inbox"
        led.set_state(project, n, back, f"attempt {attempts} failed: {outcome}",
                      attempts=attempts, esc_tier=new_tier, esc_fails=new_fails)


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
