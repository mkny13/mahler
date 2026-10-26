"""Conductor-side waits (DESIGN D52): wait with a cause and a wake check, never ping the owner.

``needs_you`` is for owner-only decisions (D13). When the conductor hits something outside
the item (repo-wide CI red, capacity, GitHub trouble), it holds the item in ``waiting``
with a machine-readable cause, re-checks with backoff and resumes on its own. Only a long
backstop (``wait_escalate_hours``) turns a wait into a ``needs_you`` question.
"""

import json
from datetime import timedelta

from . import ci_blockers
from .ledger import iso, parse

CAUSES = ("ci_repo_wide", "ci_stalled", "merge_queue", "capacity", "github", "setup",
          "worktree_conflict", "review_inconclusive", "review_nonconvergent", "other")
RESUMES = ("verifying", "ready")
BACKOFF_MINUTES = (15, 30, 60, 120, 240, 360)   # capped at the last value


def key(project, n):
    return f"wait:{project}#{n}"


def backoff(checks):
    """Delay before the next check, after ``checks`` checks have run (>= 1)."""
    return timedelta(minutes=BACKOFF_MINUTES[min(max(checks, 1), len(BACKOFF_MINUTES)) - 1])


def load(led, project, n):
    try:
        rec = json.loads(led.get_kv(key(project, n)) or "null")
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def clear(led, project, n):
    led.set_kv(key(project, n), "")


def hold(led, project, n, cause, reason, *, resume, wake=None):
    """Put an item into ``waiting``. Never pings."""
    if cause not in CAUSES:
        raise ValueError(f"unknown wait cause: {cause}")
    if resume not in RESUMES:
        raise ValueError(f"invalid wait resume state: {resume}")
    led.set_state(project, n, "waiting", f"waiting ({cause}): {reason}",
                  question=None, options="[]")
    since = iso(led.now())
    led.set_kv(key(project, n), json.dumps({
        "cause": cause, "reason": reason, "resume": resume, "since": since,
        "bound_at": led.item(project, n)["state_changed_at"], "wake": wake,
        "checks": 0, "next_at": iso(led.now() + backoff(1))}))
    led.event("wait", project, n, {"cause": cause, "reason": reason})


def _wake_ci_repo_wide(ctx, project, item, record):
    if ci_blockers.active(ctx.led, project) is None:
        return True, "repo-wide CI failure cleared"
    return False, "repo-wide CI failure still active"


def _wake_time(ctx, project, item, record):
    """Default: the backoff interval elapsed, so just retry."""
    return True, "retrying"


WAKES = {cause: _wake_time for cause in CAUSES}
WAKES["ci_repo_wide"] = _wake_ci_repo_wide


def _fresh_record(led, item):
    return {"cause": "other", "reason": "wait record missing or stale", "resume": "ready",
            "since": item["state_changed_at"], "bound_at": item["state_changed_at"],
            "wake": None, "checks": 0, "next_at": iso(led.now() + backoff(1))}


def _backstop(ctx, project, item, record, hours):
    led, n = ctx.led, item["number"]
    question = (f"Waiting on {record['cause']} for {hours}h: {record['reason']}. "
                f"Retry now or park? [OPTIONS: retry now | park]")
    led.set_state(project, n, "needs_you", f"wait backstop ({record['cause']})",
                  question=question, options=json.dumps(["retry now", "park"]))
    clear(led, project, n)
    ctx.ping(f"{project}#{n} waited too long", question, project=project, number=n)
    ctx.say(f"{project}#{n}: waited {hours}h on {record['cause']} — asking")


def _recheck_item(ctx, project, item):
    led, n = ctx.led, item["number"]
    now = led.now()
    record = load(led, project, n)
    if not record or record.get("bound_at") != item["state_changed_at"]:
        record = _fresh_record(led, item)
        led.set_kv(key(project, n), json.dumps(record))
    cause = record["cause"]
    since = parse(record["since"])
    limit = ctx.policy(project)["wait_escalate_hours"]
    if since and now - since >= timedelta(hours=limit):
        _backstop(ctx, project, item, record, int((now - since).total_seconds() // 3600))
        return
    next_at = parse(record["next_at"])
    if next_at and now < next_at:
        return
    met, detail = WAKES.get(cause, _wake_time)(ctx, project, item, record)
    if met:
        led.set_state(project, n, record["resume"], f"wait over ({cause}): {detail}")
        clear(led, project, n)
        ctx.say(f"{project}#{n}: wait over ({cause}) — {record['resume']}")
        return
    record["checks"] = int(record.get("checks") or 0) + 1
    record["next_at"] = iso(now + backoff(record["checks"] + 1))
    led.set_kv(key(project, n), json.dumps(record))


def recheck(ctx, project):
    """Re-check every waiting item of a project; each item is exception-safe."""
    if ctx.dry_run:
        return
    for item in ctx.led.items(project, ["waiting"]):
        try:
            _recheck_item(ctx, project, item)
        except Exception as exc:  # noqa: BLE001 — one bad record must not break the tick
            ctx.say(f"{project}#{item['number']}: wait recheck skipped — {exc}")
