"""Process health and lease heartbeats for the runs that are already going.

Every tick looks at each active run: is its process still there, does it
still hold the lease, is it making progress, is it still inside its quota
(DESIGN D8). A run that fails any of those is asked to stop; when its
process is gone, finalize.py turns what it left into a handoff.
"""

import os
from datetime import timedelta

from . import config, platforms, router, runner
from .finalize import finalize
from .ledger import iso, parse
from .usage import record_claude_usage

STOP_NOW = ("parked",)                               # no grace period


def watchdog(ctx):
    led, now = ctx.led, ctx.led.now()
    for run in led.active_runs():
        try:
            _watch_one(ctx, run, now)
        except Exception as e:      # one bad row must not disarm the watchdog
            try:
                ref = f"{run['project']}#{run['number']} run {run['id']}"
            except Exception:
                ref = "<unreadable run row>"
            ctx.say(f"watchdog: skipped {ref} — {e!r}")


def _watch_one(ctx, run, now):
    led = ctx.led
    pol = ctx.policy(run["project"])
    if not runner.alive(run["pid"]):
        # The shell is gone, but a child can outlive it: a SIGTERM that lands
        # while the agent is blocked in a syscall is only acted on later, if
        # at all (mahler#12). Nothing of a run may outlive the run.
        runner.kill(run["pid"])
        finalize(ctx, run)
        return
    if run["status"] == "stopping":
        runner.kill(run["pid"])                  # asked nicely last tick
        return
    holder = f"run:{run['id']}"
    still_ours = led.heartbeat(run["project"], run["number"], holder, run["epoch"],
                               pol["auto_lease_minutes"])
    reason = None
    if run["yield_at"]:
        if ctx.cfg["platforms"][run["platform"]]["kind"] == "claude":
            yield_file = os.path.join(config.RUNS_DIR, str(run["id"]), "yield")
            if not os.path.exists(yield_file):
                config.ensure_private_dir(os.path.dirname(yield_file))
                with open(yield_file, "w") as fh:
                    fh.write("")
                os.chmod(yield_file, 0o600)
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


def _log_idle(run, now):
    """(idle seconds, log size) for a run's own output.

    The agent log exists once the agent command starts (after any setup step).
    While a build's setup step runs it doesn't exist yet; setup.log is then the
    progress signal, so a slow dependency install isn't mistaken for a hung
    run. When not even setup.log exists, nothing has started: return
    (None, None) and let the caller apply the startup rule to the run's age.
    """
    try:
        st = os.stat(run["log_path"])
        return now.timestamp() - st.st_mtime, st.st_size
    except OSError:
        setup = os.path.join(os.path.dirname(run["log_path"]), "setup.log")
        try:
            return now.timestamp() - os.stat(setup).st_mtime, None
        except OSError:
            return None, None


def _health(ctx, run, pol, now):
    pconf = ctx.cfg["platforms"].get(run["platform"])
    if now - parse(run["started_at"]) > timedelta(minutes=pol["run_timeout_minutes"]):
        return "timeout"
    idle, size = _log_idle(run, now)
    if idle is None:
        # Nothing was ever written — the run never got going. Seen when a
        # macOS permission dialog blocks the agent at startup (mahler#12).
        idle = (now - parse(run["started_at"])).total_seconds()
        if idle > pol["startup_timeout_minutes"] * 60:
            return "silent"
    else:
        if size == 0 and idle > pol["startup_timeout_minutes"] * 60:
            # The log exists but is empty long after the agent command started.
            return "silent"
        if idle > pol["progress_timeout_minutes"] * 60:
            return "hung"
    if not pconf:
        return None                 # no config left to read quota from
    if pconf["kind"] == "claude":
        log = platforms.read_log(run["log_path"], "claude")
        record_claude_usage(ctx, log["usage"], platform=run["platform"])
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
