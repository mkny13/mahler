"""Every exit is a handoff (DESIGN D9).

A run that ended — cleanly, out of quota, timed out or killed — passes
through here exactly once: its log is read for usage and a STATUS line, its
work is snapshotted to a pushed ref, the issue gets a handoff comment, and
the item moves to whatever state its outcome implies.
"""

import json
import shlex
import subprocess
from datetime import timedelta

from . import platforms, router, runner
from .gh import GHError
from .ledger import CONDUCTOR, iso, parse, row_get
from .usage import record_claude_usage

NO_ATTEMPT = ("quota", "preempted", "closed", "parked", "lost-lease", "silent")


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
            # mahler#124: honor a reset time the quota error named (e.g. Cline's
            # daily cap) instead of always waiting the flat backoff.
            minutes = log["retry_after"] if log["retry_after"] is not None \
                else pconf.get("backoff_minutes", 60)
            until = iso(led.now() + timedelta(minutes=minutes))
        record_claude_usage(ctx, log["usage"], backoff_until=until, platform=run["platform"])
    else:
        for w, pct, resets in log["usage"]:
            led.record_usage(run["platform"], w, pct, resets)
        if log["quota_hit"]:
            pconf = ctx.cfg["platforms"][run["platform"]]
            # mahler#124: honor a reset time the quota error named (e.g. Cline's
            # daily cap) instead of always waiting the flat backoff.
            minutes = log["retry_after"] if log["retry_after"] is not None \
                else pconf.get("backoff_minutes", 60)
            until = iso(led.now() + timedelta(minutes=minutes))
            for w in pconf.get("windows", router.WINDOWS):
                led.record_usage(run["platform"], w, 100.0, until)
    verb, rest = platforms.status_line(log["final"] or log["last_text"])
    duration_mins = None
    started_at = row_get(run, "started_at")
    if started_at:
        try:
            duration_mins = (led.now() - parse(started_at)).total_seconds() / 60.0
        except Exception:
            pass
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
            retry_or_fail(ctx, project, n, item, reason, outcome,
                           platform=run["platform"], duration_mins=duration_mins)
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
                    retry_or_fail(ctx, project, n, item, reason, outcome,
                                   platform=run["platform"], duration_mins=duration_mins)
            elif reason == "parked":
                led.set_state(project, n, "parked", "parked while running")
            elif reason == "preempted":
                cur = led.lease(project, n)
                if cur and cur["kind"] == "interactive":
                    led.set_state(project, n, "working", "handed to your session")
                else:
                    led.set_state(project, n, "ready", "handoff (preempted)")
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
                    retry_or_fail(ctx, project, n, item, reason, outcome,
                                   platform=run["platform"], duration_mins=duration_mins)
            else:
                retry_or_fail(ctx, project, n, item, reason, outcome,
                               platform=run["platform"], duration_mins=duration_mins)
    # A finished change keeps the canonical project slot while the conductor
    # opens/watches/merges its PR (D19, D24). Transfer the same item lease in
    # one transaction so a second machine cannot claim another issue in the
    # release/claim gap. On transport failure the run lease is left to expire.
    if led.item(project, n)["state"] == "verifying":
        transferred, info = led.claim(
            project, n, CONDUCTOR, "auto", pol["auto_lease_minutes"],
            handoff_from=(f"run:{run['id']}", run["epoch"]))
        if transferred is None:
            detail = info.get("unavailable") or "canonical lease transfer refused"
            ctx.say(f"{project}#{n}: {detail}; existing lease left to expire safely")
    else:
        led.release(project, n, holder=f"run:{run['id']}", epoch=run["epoch"])
    update_cols = {"status": "ended", "outcome": outcome, "exit_code": code,
                   "ended_at": iso(led.now())}
    if not run["stop_reason"] and reason:      # mahler#124: record why it stopped
        update_cols["stop_reason"] = reason
    if log.get("model") and log["model"] != run["model"]:
        # kilo-auto/free is stateless per invocation — the model actually used
        # is the signal for whether a quota hit reflects one underlying free
        # model being rate-limited rather than the whole pool (mahler#141).
        update_cols["model"] = log["model"]
    led.update_run(run["id"], **update_cols)
    _check_estimate_calibration(ctx)
    if not keep_worktree:
        runner.remove_worktree(pol["path"], run["worktree"], run["branch"],
                               runner.worktree_root(pol))


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
