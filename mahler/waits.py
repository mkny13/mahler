"""Conductor-side waits (DESIGN D52): wait with a cause and a wake check, never ping the owner.

``needs_you`` is for owner-only decisions (D13). When the conductor hits something outside
the item (repo-wide CI red, capacity, GitHub trouble), it holds the item in ``waiting``
with a machine-readable cause, re-checks with backoff and resumes on its own. Only a long
backstop (``wait_escalate_hours``) turns a wait into a ``needs_you`` question.
"""

import json
import re
from datetime import timedelta

from . import ci_blockers, no_change
from .ledger import iso, parse

CAUSES = ("ci_repo_wide", "ci_stalled", "merge_queue", "capacity", "github", "setup",
          "worktree_conflict", "review_inconclusive", "review_nonconvergent", "other")
RESUMES = ("verifying", "ready", "inbox")
BACKOFF_MINUTES = (15, 30, 60, 120, 240, 360)   # capped at the last value
# A cause whose first retry must not come sooner than this (mahler#1261).
MIN_DELAY = {"review_inconclusive": timedelta(hours=6)}


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
        "checks": 0,
        "next_at": iso(led.now() + max(backoff(1), MIN_DELAY.get(cause, timedelta(0))))}))
    led.event("wait", project, n, {"cause": cause, "reason": reason})


def _wake_ci_repo_wide(ctx, project, item, record):
    if ci_blockers.active(ctx.led, project) is None:
        return True, "repo-wide CI failure cleared"
    return False, "repo-wide CI failure still active"


def _wake_time(ctx, project, item, record):
    """Default: the backoff interval elapsed, so just retry."""
    return True, "retrying"


def _wake_ci_stalled(ctx, project, item, record):
    """Retry after backoff with the CI/lookup timers cleared, so they restart."""
    if item["pr"]:
        for prefix in ("ci", "pr-lookup"):
            ctx.led.set_kv(f"{prefix}:{project}#{item['number']}:{item['pr']}", "")
    return True, "retrying with a fresh CI timer"


def _wake_merge_queue(ctx, project, item, record):
    """Retry after backoff with the merge-queue timer restarted."""
    if item["pr"]:
        qkey = f"queue:{project}#{item['number']}:{item['pr']}"
        try:
            queued = json.loads(ctx.led.get_kv(qkey) or "{}")
        except ValueError:
            queued = {}
        if isinstance(queued, dict) and queued:
            queued["since"] = iso(ctx.led.now())
            ctx.led.set_kv(qkey, json.dumps(queued))
    return True, "retrying with a fresh merge-queue timer"


def _wake_capacity(ctx, project, item, record):
    """Met once normal routing finds a platform for the recorded role (#653/#654)."""
    from . import router
    from .ledger import CONDUCTOR
    from .ship import _review_preference, busy_platforms

    led, cfg, n = ctx.led, ctx.cfg, item["number"]
    pol = ctx.policy(project)
    role = (record.get("wake") or {}).get("role") or "fix"
    active = led.active_runs()
    if (len(active) >= cfg["concurrency"]["total"]
            or any(r["project"] == project and r["number"] == n for r in active)):
        return False, "every run slot is busy"
    lease = led.lease(project, n)
    if lease and not (lease["holder"] == CONDUCTOR or lease["holder"].endswith("/conductor")):
        return False, f"held by {lease['holder']}"
    busy = busy_platforms(cfg, active)
    size = next((l.split(":", 1)[1] for l in json.loads(item["labels"] or "[]")
                 if l.startswith("size:")), None)
    if role == "review":
        view = ctx.gh(project).pr_view(item["pr"])
        route, _, waiting, _ = _review_preference(
            ctx, project, item, view.get("headRefOid") or "", busy)
        if waiting and route["pin"] and not router.preferred_for_project(
                cfg, led, pol, "review", route["pin"], busy=busy,
                size=route["size"], exclude=route["exclude"],
                burst_lines=ctx.burst_lines):
            return False, "preferred reviewer still unavailable"
    elif role == "design":
        route = dict(size=size)
    else:
        tier = max(router.cap_escalation(cfg, pol, item["esc_tier"], size, pin=item["pin"]),
                   router.risk_min_tier(item["title"]))
        if size == "l" or (tier >= 2 and size == "s"):
            size = "m"
        route = dict(pin=item["pin"], size=size, min_tier=tier)
    platform, _ = router.pick_for_project(
        cfg, led, pol, role, busy=busy, burst_lines=ctx.burst_lines,
        scorecard_rows=getattr(ctx, "scorecard_rows", None), **route)
    if not platform:
        return False, f"no platform for {role} yet"
    for prefix in ("review-wait", "review-pinged", "reviewfix-status", "design-wait"):
        led.set_kv(f"{prefix}:{project}#{n}", "")
    if item["pr"]:
        for prefix in ("ci", "mergeability"):
            led.set_kv(f"{prefix}:{project}#{n}:{item['pr']}", "")
        _wake_merge_queue(ctx, project, item, record)
    return True, f"{role} capacity returned"


def _wake_worktree_conflict(ctx, project, item, record):
    """Met once the branch is no longer checked out elsewhere, or the conflicting
    session's lease is gone (#799)."""
    from . import runner

    wake = record.get("wake") or {}
    holder = wake.get("holder")
    if holder:
        lease = ctx.led.lease(project, item["number"])
        if not lease or lease["holder"] != holder:
            return True, "the conflicting lease is gone"
    branch = wake.get("branch") or item["branch"]
    try:
        found = runner.find_worktree_for_branch(ctx.policy(project)["path"], branch)
    except Exception as exc:  # noqa: BLE001 — an unreadable listing is not a release
        return False, f"worktree listing failed: {exc}"
    if found is None:
        return True, f"branch {branch} is free"
    return False, f"branch {branch} still checked out at {found}"


def _wake_review_inconclusive(ctx, project, item, record):
    """Retry the review with a fresh inconclusive count. The platforms that came back
    without a verdict move to an "avoid" list, so the next reviewer prefers others."""
    from . import no_change

    led, n = ctx.led, item["number"]
    sha = (record.get("wake") or {}).get("sha")
    claim = no_change.read(led, project, n)
    if not sha and claim:
        sha = claim.get("base_sha")
    if sha:
        seen_key = f"reviewinconclusive:{project}#{n}:{sha}"
        avoid_key = f"reviewinconclusive-avoid:{project}#{n}:{sha}"
        try:
            seen = json.loads(led.get_kv(seen_key) or "[]")
            avoid = json.loads(led.get_kv(avoid_key) or "[]")
        except ValueError:
            seen, avoid = [], []
        led.set_kv(avoid_key, json.dumps([*avoid, *seen]))
        led.set_kv(seen_key, "")
    led.set_kv(f"reviewinconclusive_capped:{project}#{n}", "")
    if claim and claim.get("phase") == "exhausted":
        claim.update(phase="waiting", inconclusive=0, run_id=None, epoch=None)
        no_change.save(led, project, n, claim)
    return True, "retrying review with a fresh inconclusive count"


WAKES = {cause: _wake_time for cause in CAUSES}
WAKES["ci_repo_wide"] = _wake_ci_repo_wide
WAKES["ci_stalled"] = _wake_ci_stalled
WAKES["merge_queue"] = _wake_merge_queue
WAKES["capacity"] = _wake_capacity
WAKES["worktree_conflict"] = _wake_worktree_conflict
WAKES["review_inconclusive"] = _wake_review_inconclusive


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


# ---------- one-time migration of items stranded before D52 (mahler#1261) ----------

_STATE_DETAIL = re.compile(r"\S+ -> \S+ \((.*)\)", re.DOTALL)
# Exact conductor-generated escalation texts, most specific first. Owner questions
# never have these shapes.
LEGACY_PATTERNS = (
    (re.compile(r"No fix run could start for PR #\d+ after \d+ minutes: .*", re.DOTALL), "capacity"),
    (re.compile(r"setup failed \d+ times in a row.*", re.DOTALL), "setup"),
    (re.compile(r"PR #\d+ passed checks but hasn't merged in \d+ min.*", re.DOTALL), "merge_queue"),
    (re.compile(r".*Independent no-change reviews exhausted inconclusive attempts.*", re.DOTALL),
     "review_inconclusive"),
    (re.compile(r"\d+ reviews of this PR head ended without a verdict.*", re.DOTALL),
     "review_inconclusive"),
    (re.compile(r"CI still red on PR #\d+ after \d+ attempts"), "ci_repo_wide"),
    (re.compile(r"PR #\d+: .* after \d+ min", re.DOTALL), "ci_stalled"),
)


def legacy_cause(text):
    """The wait cause for an exact conductor-generated escalation text, else None."""
    for pattern, cause in LEGACY_PATTERNS:
        if pattern.fullmatch(text or ""):
            return cause
    return None


def _last_state_reason(led, project, n):
    row = led.q1("SELECT detail FROM events WHERE project=? AND number=? AND kind='state' "
                 "ORDER BY id DESC LIMIT 1", (project, n))
    m = _STATE_DETAIL.fullmatch((row["detail"] if row else "") or "")
    return m.group(1) if m else None


def _inconclusive_sha(ctx, project, item):
    """The PR head whose reviews ended without a verdict: the capped marker's sha, else
    the PR's current head, so the wake can reset the counter and set the avoid list."""
    n = item["number"]
    try:
        capped = json.loads(ctx.led.get_kv(f"reviewinconclusive_capped:{project}#{n}") or "{}")
    except ValueError:
        capped = {}
    if isinstance(capped, dict) and capped.get("sha"):
        return capped["sha"]
    if item["pr"]:
        try:
            return ctx.gh(project).pr_view(item["pr"]).get("headRefOid") or None
        except Exception:  # noqa: BLE001 — the wake then just retries
            return None
    return None


def migrate_legacy(ctx, project):
    """Once per project: move items stranded in needs_you/failed by a conductor-side
    escalation into ``waiting``. Owner questions never match and stay put."""
    led = ctx.led
    gate = f"waits-migrated:{project}"
    if ctx.dry_run or led.get_kv(gate):
        return 0
    moved = 0
    skipped = False
    for item in led.items(project, ["needs_you", "failed"]):
        n = item["number"]
        try:
            texts = [t for t in (item["question"], _last_state_reason(led, project, n)) if t]
            cause = next((c for c in map(legacy_cause, texts) if c), None)
            if not cause:
                continue
            text = next(t for t in texts if legacy_cause(t) == cause)
            if cause == "ci_repo_wide":
                from .ship import hold_repo_wide, repo_wide_red
                if not item["pr"]:
                    continue
                view = ctx.gh(project).pr_view(item["pr"])
                if not repo_wide_red(ctx, project, view):
                    continue
                key = f"red:{project}#{n}:{item['pr']}:{view.get('headRefOid') or ''}"
                hold_repo_wide(ctx, project, n, item["pr"], key, f"migrated: {text}")
                moved += 1
                ctx.say(f"{project}#{n}: stranded {item['state']} → waiting ({cause})")
                continue
            resume = ("verifying" if item["pr"] else "ready" if item["sorted_at"] else "inbox")
            if cause == "setup":
                resume = "ready" if item["sorted_at"] else "inbox"
            wake = None
            if cause == "capacity":
                wake = {"role": "fix"}
            elif cause == "review_inconclusive":
                claim = no_change.read(led, project, n)
                if claim:               # a no-change item resumes its review workflow
                    resume = "verifying"
                    wake = {"sha": claim.get("base_sha")}
                else:
                    sha = _inconclusive_sha(ctx, project, item)
                    if sha is None:
                        # Do not strand the item in a wait whose wake cannot reset
                        # the old review state; retry migration on the next tick.
                        skipped = True
                        continue
                    wake = {"sha": sha}
            hold(led, project, n, cause, f"migrated: {text}", resume=resume, wake=wake)
            moved += 1
            ctx.say(f"{project}#{n}: stranded {item['state']} → waiting ({cause})")
        except Exception as exc:  # noqa: BLE001 — one item must not stop the migration
            ctx.say(f"{project}#{n}: wait migration skipped — {exc}")
    if not skipped:
        led.set_kv(gate, "1")
    return moved
