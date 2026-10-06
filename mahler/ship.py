"""The conductor ships (DESIGN D18).

The mechanical tail of a build run, in code: push the branch, open the PR,
watch CI across ticks, start a fix run when CI goes red, and squash-merge on
green. Only executing runs take capacity; conductor watch leases do not.
Never merge once the item's lease has gone to a session (D6).
"""

import json
import re
from datetime import timedelta

from . import capacity, config, failures, review, router, runner
from .finalize import CAPACITY_STOPS, retry_or_fail
from .gh import GHError, checks_state, needs_human_of, pr_body, pr_summary_of
from .ledger import CONDUCTOR, iso, parse, row_get
from .tick import busy_platforms, start


def ship(ctx, projects):
    """One pass over every project's finished-but-unmerged changes."""
    for p in projects:
        try:
            _ship_project(ctx, p["name"])
        except Exception as e:                  # noqa: BLE001 — a tick must not break
            ctx.say(f"{p['name']}: ship pass failed — {e}")
        try:
            _unowned_prs(ctx, p["name"])
        except Exception as e:                  # noqa: BLE001 — a tick must not break
            ctx.say(f"{p['name']}: unowned-PR check failed — {e}")


UNOWNED_SCAN_MINUTES = 15
_BOT_BRANCHES = ("dependabot/", "renovate/")


def _unowned_prs(ctx, project):
    """Ping once for each open PR no item tracks (mahler#407). Sessions that
    open a PR and neither merge it nor `mahler ship` it leave it open, and
    nothing here would otherwise notice: the ship pass only watches items a
    build (or `mahler ship`) put in `verifying`. Checked every 15 min, and
    only for PRs older than `unowned_pr_hours` (default 2)."""
    if ctx.dry_run:
        return
    led = ctx.led
    key = f"unowned-scan:{project}"
    last = led.get_kv(key)
    if last and led.now() - parse(last) < timedelta(minutes=UNOWNED_SCAN_MINUTES):
        return
    led.set_kv(key, iso(led.now()))
    hours = ctx.policy(project).get("unowned_pr_hours", 2)
    tracked = {it["pr"] for it in led.items(project) if it["pr"]}
    for pr in ctx.gh(project).open_prs():
        n = pr["number"]
        if (n in tracked or pr.get("isDraft")
                or (pr.get("headRefName") or "").startswith(_BOT_BRANCHES)
                or led.now() - parse(pr["createdAt"]) < timedelta(hours=hours)
                or led.get_kv(f"unowned:{project}#{n}")):
            continue
        led.set_kv(f"unowned:{project}#{n}", iso(led.now()))
        led.event("unowned_pr", project, None, {"pr": n, "branch": pr.get("headRefName")})
        ctx.ping(f"PR nobody is shipping — {project} PR #{n}",
                 f"“{pr.get('title', '')}” has been open {hours}h+ and no item tracks it. "
                 f"Merge it, close it, or `mahler ship {project}#<issue> --pr {n}`.",
                 tags="hourglass")
        ctx.say(f"{project}: PR #{n} is open with no item tracking it — pinged")


def _ship_project(ctx, project):
    migrate_capacity_waits(ctx, project)
    recover_capacity_waits(ctx, project)
    ctx.merge_requested = False
    for item in ctx.led.items(project, ["verifying"]):
        try:
            _ship_item(ctx, project, item)
        except Exception as e:                  # noqa: BLE001 — one item can't stop the rest
            ctx.say(f"{project}#{item['number']}: shipping failed — {e}")
        # One conductor serializes merge requests; next tick rechecks base freshness.
        if ctx.merge_requested:
            break


def _ship_lease(ctx, project, item):
    """Take the conductor's lease on an item that is ready to ship.
    -> True when we hold it; each refusal says why and leaves the PR alone."""
    led, n = ctx.led, item["number"]
    lease, info = led.claim(project, n, CONDUCTOR, "auto",
                            ctx.policy(project)["auto_lease_minutes"], capacity=False)
    if lease is not None:
        return True
    if "unavailable" in info:
        ctx.say(f"{project}#{n}: canonical lease host unavailable — shipping skipped")
    else:                                       # a session pre-empted the item (D6)
        pr = f"PR #{item['pr']}" if item["pr"] else "its PR (not yet open)"
        led.set_state(project, n, "working",
                      f"handed to {info['held_by']['holder']} — {pr} stays open, unmerged")
        ctx.ping(f"Handoff to you — {project} #{n}",
                 "you hold this item now; the conductor won't merge " + pr,
                 project, n, priority="low")
    return False


def _rebuild_on_base(ctx, project, item, pr, base, *, stale=False):
    """The base moved under the PR: drop it and build again on current base
    (D19). No attempt is counted — the work was fine, the ground moved."""
    led, n = ctx.led, item["number"]
    reason = f"does not contain current {base}" if stale else f"conflicts with {base}"
    # The PR stays open while the item briefly has no PR link.  Remember that
    # this is a conductor-owned transition so the unowned-PR backstop does not
    # mistake the rebuild window for an abandoned interactive session.
    led.set_kv(f"unowned:{project}#{pr}", iso(led.now()))
    led.upsert_item(project, n, pr=None)
    led.set_state(project, n, "ready", f"PR #{pr} {reason} — rebuilding on it")
    led.release(project, n, holder=CONDUCTOR)
    ctx.ping(f"Rebuilding — {project} #{n}",
             f"PR #{pr} {reason}; the next build starts on current {base}",
             project, n, priority="low")


def _update_reviewed_pr(ctx, project, item, pr, view):
    """Keep reviewed work on its PR; never turn update uncertainty into a build."""
    led, n = ctx.led, item["number"]
    key = f"review:{project}#{n}"
    info = _kv_json(led, key)
    passed = (view.get("headRefOid") and info.get("sha") == view["headRefOid"]
              and info.get("verdict") == "pass")
    retained_key = f"reviewed-pr:{project}#{n}"
    if not passed and led.get_kv(retained_key) != str(pr):
        return False
    led.set_kv(retained_key, str(pr))
    gh = ctx.gh(project)
    def authorize():
        fresh = gh.pr_view(pr)
        return (fresh.get("state") == "OPEN"
                and all(fresh.get(k) == view.get(k) for k in
                        ("headRefOid", "headRefName", "baseRefName"))
                and _ship_lease(ctx, project, item))
    try:
        result = gh.update_reviewed_branch(
            ctx.policy(project)["path"], view.get("headRefName"),
            view.get("baseRefName"), view["headRefOid"], authorize)
        if not _ship_lease(ctx, project, item):
            return True
        if result is None:
            if not authorize():
                raise GHError("update: PR or lease changed before conflict fix")
            _review_triggered_fix(ctx, project, item, pr, view,
                f"Merge origin/{view['baseRefName']} into this PR's existing branch, "
                "resolve conflicts while preserving the reviewed work, verify and push "
                "to the same PR. Do not reset to base or open a replacement PR.",
                base_conflict=True)
            return True
        sha, unchanged = result
        if unchanged and passed:
            led.set_kv(key, json.dumps({**info, "sha": sha, "updated_from": info["sha"]}))
        else:
            led.set_kv(key, json.dumps({"sha": sha, "verdict": "required"}))
        _ci_pending(ctx, project, item, pr, {**view, "headRefOid": sha},
                    reason="PR branch updated; waiting for fresh CI and base verification")
    except (GHError, ValueError) as exc:
        _ci_pending(ctx, project, item, pr, view, reason=f"PR update failed: {exc}")
    return True


def _closed_pr(ctx, project, item, pr, view):
    """Recover an unmerged closure without recording any shipped evidence."""
    led, n = ctx.led, item["number"]
    head = view.get("headRefName") or item["branch"]
    replacement = None
    if head:
        try:
            replacement = ctx.gh(project).pr_for_head(head)
        except (GHError, ValueError) as e:
            ctx.say(f"{project}#{n}: PR #{pr} closed, but replacement lookup failed — {e}")
            if not ctx.dry_run:
                led.release(project, n, holder=CONDUCTOR)
            return
    reason = f"PR #{pr} was closed without merging"
    if replacement and replacement != pr:
        if not ctx.dry_run:
            if not _ship_lease(ctx, project, item):
                return
            led.upsert_item(project, n, pr=replacement, branch=head)
        action = f"verifying replacement PR #{replacement}"
    else:
        action = "failed" if item["attempts"] + 1 >= ctx.policy(project)["max_attempts"] else "ready"
        if not ctx.dry_run:
            if not _ship_lease(ctx, project, item):
                return
            snapshot = led.get_kv(f"ship_snapshot:{project}#{n}")
            led.upsert_item(project, n, pr=None, branch=snapshot or item["branch"])
            retry_or_fail(ctx, project, n, item, None, reason)
    ctx.say(f"{project}#{n}: {reason} — {'would go to ' if ctx.dry_run else ''}{action}")
    if not ctx.dry_run:
        led.release(project, n, holder=CONDUCTOR)


def _mergeability_observation(ctx, project, item, pr, view):
    """Nudge GitHub across ticks; REST is only a trigger, never merge evidence."""
    led = ctx.led
    key = f"mergeability:{project}#{item['number']}:{pr}"
    info = _kv_json(led, key)
    identity = {"sha": view.get("headRefOid"), "base": view.get("baseRefName")}
    if view.get("state") != "OPEN" or view.get("mergeable") != "UNKNOWN":
        led.set_kv(key, "")
        return view
    if any(info.get(k) != v for k, v in identity.items()) or not info:
        info = dict(identity, observations=0, refreshes=0, since=iso(led.now()))
    info["observations"] += 1
    due = (info["observations"] > 2 and
           (not info.get("last_refresh") or
            (info["refreshes"] < 3 and not info.get("error")) or
            led.now() - parse(info["last_refresh"]) >= timedelta(minutes=5)))
    if due:
        info["refreshes"] += 1
        info["last_refresh"] = iso(led.now())
        # Persist before network work, including failed requests, for restart safety.
        led.set_kv(key, json.dumps(info))
        try:
            ctx.gh(project).pr_refresh_mergeability(pr)
            fresh = ctx.gh(project).pr_view(pr)
        except (GHError, ValueError) as exc:
            info["error"] = f"mergeability refresh failed: {exc}"
        else:
            info.pop("error", None)
            if (fresh.get("state") != "OPEN" or fresh.get("mergeable") != "UNKNOWN"):
                led.set_kv(key, "")
                return fresh
            fresh_identity = {"sha": fresh.get("headRefOid"), "base": fresh.get("baseRefName")}
            if fresh_identity != identity:
                info = dict(fresh_identity, observations=1, refreshes=0, since=iso(led.now()))
            view = fresh
    led.set_kv(key, json.dumps(info))
    return view


def _watch_pr(ctx, project, item, pr):
    """One step of the open PR's state machine, one step per tick: gone,
    conflicting, CI still running, CI red, or green and (queued to be)
    merged."""
    led, n = ctx.led, item["number"]
    gh = ctx.gh(project)
    try:
        view = gh.pr_view(pr)
    except (GHError, ValueError) as e:
        _ci_pending(ctx, project, item, pr, {}, reason=f"PR lookup failed: {e}")
        return
    if not _ship_lease(ctx, project, item):
        return
    view = _mergeability_observation(ctx, project, item, pr, view)
    if not _ship_lease(ctx, project, item):
        return
    if view["state"] == "MERGED":
        _shipped(ctx, project, n, pr, led.item(project, n), view, merged=False)
        return
    if view["state"] != "OPEN":
        _closed_pr(ctx, project, item, pr, view)
        return
    if view.get("mergeable") == "CONFLICTING":
        base = view.get("baseRefName") or ctx.policy(project).get("base", "main")
        if not _update_reviewed_pr(ctx, project, item, pr, view):
            _rebuild_on_base(ctx, project, item, pr, base)
        return
    state = checks_state(view.get("statusCheckRollup"))
    # Keep the console's explanation in step with the state this watcher saw.
    # The timestamp for a pending head is initialized by _ci_pending; recording
    # the terminal state here prevents an old pending timestamp from being
    # mistaken for the current wait after CI finishes.
    if state == "pending":
        _ci_pending(ctx, project, item, pr, view)
        return
    ci_key = f"ci:{project}#{item['number']}:{pr}"
    ci_seen = led.get_kv(ci_key)
    ci_info = json.loads(ci_seen) if ci_seen else {}
    ci_info["state"] = state
    ci_info["base"] = view.get("baseRefName")
    if view.get("headRefOid"):
        ci_info["sha"] = view["headRefOid"]
    led.set_kv(ci_key, json.dumps(ci_info))
    if state == "red":
        # D18 (mahler#18): red CI starts a fix run from the PR branch, with the
        # failing log in its prompt. The conductor's lease goes to the run.
        _red_ci(ctx, project, item, pr, view)
        return
    if view.get("mergeable") == "UNKNOWN":
        wait = _kv_json(led, f"mergeability:{project}#{n}:{pr}")
        detail = f"; {wait['error']}" if wait.get("error") else ""
        ctx.say(f"{project}#{n}: PR #{pr} — mergeability unknown{detail}")
        return
    _review_gate(ctx, project, item, pr, view)


def _ship_item(ctx, project, item):
    led, n = ctx.led, item["number"]
    branch = item["branch"]
    has_snapshot = bool(branch and branch.startswith("mahler/snapshot/"))
    if ctx.dry_run:
        if item["pr"] and not has_snapshot:
            view = ctx.gh(project).pr_view(item["pr"])
            if view["state"] not in ("OPEN", "MERGED"):
                _closed_pr(ctx, project, item, item["pr"], view)
                return
        ctx.say(f"{project}#{n}: would ship "
                f"{'PR #' + str(item['pr']) if (item['pr'] and not has_snapshot) else '(opening the PR)'}")
        return
    if not _ship_lease(ctx, project, item):
        return
    if item["pr"] and not has_snapshot:
        _watch_pr(ctx, project, item, item["pr"])
        return
    # no PR yet, or a rebuild finished with a snapshot: open it — its CI is watched from the next tick
    unconfirmed = bool(led.get_kv(f"unconfirmed:{project}#{n}"))
    _open_pr(ctx, project, item, ctx.gh(project), ctx.policy(project),
             unconfirmed=unconfirmed)


def _ci_pending(ctx, project, item, pr, view, *, reason="CI still running"):
    """CI still running. It is watched across ticks (the 30s loop never blocks
    a tick), but a hung CI must not park the item silently: past
    verify_timeout_minutes it goes to needs-you with a ping. A new head SHA
    (e.g. a fix run's push) restarts the wait, because CI starts over."""
    if not _ship_lease(ctx, project, item):
        return
    led, n = ctx.led, item["number"]
    pol = ctx.policy(project)
    key = f"ci:{project}#{n}:{pr}"
    sha = view.get("headRefOid") or ""
    seen = led.get_kv(key)
    info = json.loads(seen) if seen else None
    if not info or not info.get("since") or (sha and info.get("sha") != sha):
        info = {"sha": sha, "since": iso(led.now())}
    info["state"] = "pending"
    led.set_kv(key, json.dumps(info))
    if led.now() - parse(info["since"]) <= timedelta(minutes=pol["verify_timeout_minutes"]):
        ctx.say(f"{project}#{n}: PR #{pr} — {reason}")
        return
    elapsed = int((led.now() - parse(info["since"])).total_seconds() // 60)
    reason = f"PR #{pr}: {reason} after {elapsed} min"
    led.set_state(project, n, "needs_you", reason, question=reason, options="[]")
    ctx.ping(f"Mahler needs you — {project} #{n}",
             f"{reason}; "
             "the PR stays open, unmerged",
             project, n, priority="high", tags="question", console=True)
    led.release(project, n, holder=CONDUCTOR)


def _merge_queued(ctx, project, item, pr, view):
    """Checks are acceptable: request the merge, then watch for it to land.

    mahler#211 — GitHub's native merge queue (once enabled on the base branch)
    re-tests the PR against the *actual* combined state before merging, not
    just the PR in isolation, so `gh pr merge` may only enqueue it rather than
    merge it on the spot. Re-viewing right after the request catches the
    common case (no queue, or the queue was empty and free) in the same tick,
    the same way a direct merge always has. A PR that stays open and green
    past `verify_timeout_minutes` — the queue's re-test failed and it was
    kicked back out, or the queue is stuck — goes to needs-you exactly like
    pending CI does, rather than waiting silently forever."""
    led, n = ctx.led, item["number"]
    gh = ctx.gh(project)
    pol = ctx.policy(project)
    key = f"queue:{project}#{n}:{pr}"
    sha = view.get("headRefOid") or ""
    seen = led.get_kv(key)
    info = json.loads(seen) if seen else None
    if not info or info.get("sha") != sha:
        try:
            fresh = gh.pr_view(pr)
            if (not sha or not view.get("baseRefName") or
                    fresh.get("headRefOid") != sha or
                    fresh.get("baseRefName") != view.get("baseRefName") or
                    fresh.get("state") != "OPEN" or
                    fresh.get("mergeable") != "MERGEABLE" or
                    checks_state(fresh.get("statusCheckRollup")) not in ("green", "none")):
                _ci_pending(ctx, project, item, pr, fresh,
                            reason="PR changed or metadata incomplete; retrying verification")
                return
            contains = gh.base_in_head(pol["path"], fresh["baseRefName"], sha)
            if contains is not True and contains is not False:
                raise GHError("freshness: ancestry is unknown")
        except (GHError, ValueError) as e:
            _ci_pending(ctx, project, item, pr, view, reason=f"freshness check failed: {e}")
            return
        # Fetch/API calls can take long enough for an interactive claim (D6).
        if not _ship_lease(ctx, project, item):
            return
        if not contains:
            if not _update_reviewed_pr(ctx, project, item, pr, fresh):
                _rebuild_on_base(ctx, project, item, pr, fresh["baseRefName"], stale=True)
            return
        ctx.merge_requested = True  # even an uncertain API failure consumes this tick
        gh.pr_merge(pr, sha)
        led.set_kv(key, json.dumps({"sha": sha, "since": iso(led.now())}))
        after = gh.pr_view(pr)
        if after["state"] == "MERGED":
            _shipped(ctx, project, n, pr, led.item(project, n), after)
        elif after["state"] != "OPEN":
            _closed_pr(ctx, project, item, pr, after)
        else:
            ctx.say(f"{project}#{n}: PR #{pr} — checks acceptable, merge requested; waiting for GitHub")
        return
    if led.now() - parse(info["since"]) <= timedelta(minutes=pol["verify_timeout_minutes"]):
        ctx.say(f"{project}#{n}: PR #{pr} — waiting for the merge queue")
        return
    elapsed = int((led.now() - parse(info["since"])).total_seconds() // 60)
    reason = (f"PR #{pr} passed checks but hasn't merged in {elapsed} min — "
              "check the merge queue")
    led.set_state(project, n, "needs_you", reason, question=reason, options="[]")
    ctx.ping(f"Mahler needs you — {project} #{n}",
             f"PR #{pr} passed checks but hasn't merged in {elapsed} min "
             "(the merge queue?); the PR stays open, unmerged",
             project, n, priority="high", tags="question", console=True)
    led.release(project, n, holder=CONDUCTOR)


def _review_required(item):
    """DESIGN D11 / BACKLOG's resolved trigger for the adversarial review:
    the default for size:m and size:l items, and anything the same risk
    keywords used for escalation (router.risk_min_tier) flag as touching
    data, migrations, or another high-risk surface — a chore-sized diff with
    none of those signals ships on CI green alone, same as today."""
    labels = json.loads(row_get(item, "labels", "[]"))
    size = next((l.split(":", 1)[1] for l in labels if l.startswith("size:")), None)
    if size in ("m", "l"):
        return True
    return router.risk_min_tier(row_get(item, "title", "")) > 0


def _review_gate(ctx, project, item, pr, view):
    """Between CI-green and merge, DESIGN D11's independent review: CI-green
    proves the tests that exist pass, not that the diff is sound, so a
    review run on a platform other than the builder's gates the merge
    alongside it. Low-risk items (`_review_required` false) skip straight to
    `_merge_queued`, same as before this existed."""
    led, n = ctx.led, item["number"]
    if (not _review_required(item)
            and led.get_kv(f"reviewed-pr:{project}#{n}") != str(pr)):
        _merge_queued(ctx, project, item, pr, view)
        return
    sha = view.get("headRefOid") or ""
    key = f"review:{project}#{n}"
    seen = led.get_kv(key)
    info = json.loads(seen) if seen else {}
    if checks_state(view.get("statusCheckRollup")) not in {"green", "none"}:
        return
    if sha and info.get("sha") == sha and info.get("pr", pr) == pr:
        verdict = info.get("verdict")
        if verdict in {"pass", "fail"}:
            review.record_green(ctx, project, item, pr, info)
            _review_not_converging(ctx, project, item, pr, view)
        if verdict == "pass":
            if not review.file_followups(ctx, project, item):
                return
            _merge_queued(ctx, project, item, pr, view)
            return
        if verdict == "fail":
            if not _unchanged_done_review(ctx, project, item, pr, sha, info):
                _review_triggered_fix(ctx, project, item, pr, view, info.get("findings", ""))
                return
        # verdict still "pending" for this sha: a review run is (or was) in
        # flight; fall through to the active-run check below rather than
        # trusting a run that may itself have died without finalizing.
    # a new sha (no record, or the record is for an older sha) falls through
    # the same way — _start_review_run below writes the fresh "pending" kv
    # only once a run actually starts.
    if any(r["project"] == project and r["number"] == n and r["role"] == "review"
           for r in led.active_runs()):
        ctx.say(f"{project}#{n}: PR #{pr} — independent review in progress")
        return
    _start_review_run(ctx, project, item, pr, view, sha)


def _kv_json(led, key):
    try:
        value = json.loads(led.get_kv(key) or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _unchanged_done_review(ctx, project, item, pr, sha, info):
    """Consume one durable same-head allowance, before reopening fix accounting.

    Record the exact launched fix and failed verdict, not merely a historical
    DONE. Writing the allowance before invalidation makes interrupted ticks
    replayable; a new verdict can never reuse that allowance.
    """
    led, n = ctx.led, item["number"]
    key = f"reviewfix:{project}#{n}:{pr}:{sha}"
    allowance = f"reviewdone:{project}#{n}:{pr}:{sha}"
    used = _kv_json(led, allowance)
    if used:
        if used.get("verdict") != info:
            return False
    else:
        launched = _kv_json(led, f"{key}:run")
        if not led.get_kv(key) or launched.get("verdict") != info:
            return False
        run = led.last_run(project, n, roles=("build", "fix"))
        if (not run or run["id"] != launched.get("run_id")
                or run["role"] != "fix" or run["status"] != "ended"
                or run["outcome"] != "DONE" or run["stop_reason"]):
            return False
        previous = led.last_run(project, n, roles=("review",))
        led.set_kv(allowance, json.dumps({
            "fix_run": run["id"], "verdict": info,
            "reviewer": previous["platform"] if previous else None}))
    led.set_kv(f"review:{project}#{n}", None)
    ctx.say(f"{project}#{n}: PR #{pr} — fix DONE without a new head; re-reviewing once")
    return True


def _clear_charged_if_fix_completed(led, project, number, key):
    """Reopen a failure cycle after a genuine fix run completed.

    The cycle key and its ``:charged`` marker keep capacity-interrupted fixes
    from spending the attempt budget again. An ended fix that actually ran
    and was not a capacity interruption means the unchanged failing head is
    a new failure cycle and must count normally.
    """
    cycle_ts = led.get_kv(key)
    if not cycle_ts:
        return
    run = led.q1(
        "SELECT 1 FROM runs WHERE project=? AND number=? AND role='fix' "
        "AND status='ended' AND started_at > ? "
        "AND outcome IS NOT NULL AND outcome != 'not claimed' "
        "AND outcome NOT LIKE 'launch failed:%' "
        "AND coalesce(stop_reason, '') NOT IN (?, ?, ?) LIMIT 1",
        (project, number, cycle_ts, *CAPACITY_STOPS))
    if run:
        led.set_kv(key, None)
        led.set_kv(f"{key}:charged", None)


def _mark_capacity_wait(led, project, item, role):
    # Tie the marker to this particular pause, so a later owner decision cannot
    # inherit it. Question wording is deliberately not part of recovery.
    led.set_kv(f"capacity-stranded:{project}#{item['number']}", json.dumps({
        "role": role, "pr": item["pr"], "at": item["state_changed_at"],
    }))


def migrate_capacity_waits(ctx, project):
    """Recognize the old conductor messages once per project, never heuristically."""
    led = ctx.led
    key = f"capacity-stranded-migrated:{project}"
    if ctx.dry_run or led.get_kv(key):
        return
    for item in led.items(project, ["needs_you"]):
        if not item["pr"]:
            continue
        marker = f"capacity-stranded:{project}#{item['number']}"
        if led.get_kv(marker):
            continue
        pr = item["pr"]
        question = item["question"] or ""
        role = None
        if re.fullmatch(rf"No fix run could start for PR #{pr} after \d+ minutes: .+",
                        question):
            role = "fix"
        elif re.fullmatch(rf"(?:Review waiting[ —:]+)?PR #{pr} is CI-green but no review "
                          rf"could start for over \d+ minutes: .+", question):
            role = "review"
        # #654 already persisted structured escalations before this migration.
        for candidate, prefix in (("fix", "reviewfix-status"), ("review", "review-wait")):
            status = _kv_json(led, f"{prefix}:{project}#{item['number']}")
            if status.get("state") in ("missing_tier", "capacity_wait_expired"):
                # These records predate an explicit pause marker: only adopt
                # the exact question the conductor generated for that role.
                if (question.startswith(f"No configured eligible platform for {candidate} ")
                        or question.startswith(f"{candidate.capitalize()} capacity wait exceeded ")):
                    role = candidate
        if role:
            _mark_capacity_wait(led, project, item, role)
    led.set_kv(key, "1")


def recover_capacity_waits(ctx, project):
    """Requeue marked pauses only after normal routing finds a suitable platform."""
    if ctx.dry_run:
        return
    led, cfg = ctx.led, ctx.cfg
    pol = ctx.policy(project)
    for item in led.items(project, ["needs_you"]):
        n = item["number"]
        key = f"capacity-stranded:{project}#{n}"
        marker = _kv_json(led, key)
        if marker.get("role") not in ("fix", "review"):
            continue
        if marker.get("at") != item["state_changed_at"] or marker.get("pr") != item["pr"]:
            led.set_kv(key, "")
            continue
        try:
            active = led.active_runs()
            if (len(active) >= cfg["concurrency"]["total"]
                    or any(r["project"] == project and r["number"] == n for r in active)):
                continue
            lease = led.lease(project, n)
            if lease and not (lease["holder"] == CONDUCTOR
                              or lease["holder"].endswith("/conductor")):
                continue
            busy = busy_platforms(cfg, active)
            role = marker["role"]
            if role == "review":
                view = ctx.gh(project).pr_view(item["pr"])
                pin, size, exclude = _review_route(ctx, project, item, view.get("headRefOid") or "")
                route = dict(pin=pin, size=size, exclude=exclude)
            else:
                size = next((l.split(":", 1)[1] for l in json.loads(item["labels"])
                             if l.startswith("size:")), None)
                tier = max(router.cap_escalation(cfg, pol, item["esc_tier"], size,
                                                 pin=item["pin"]),
                           router.risk_min_tier(item["title"]))
                if size == "l" or (tier >= 2 and size == "s"):
                    size = "m"
                route = dict(pin=item["pin"], size=size, min_tier=tier)
            platform, _ = router.pick_for_project(
                cfg, led, pol, role, busy=busy, burst_lines=ctx.burst_lines,
                scorecard_rows=getattr(ctx, "scorecard_rows", None), **route)
            if not platform:
                continue
            led.set_state(project, n, "verifying", f"{role} capacity returned",
                          question=None, options="[]")
            led.set_kv(key, "")
            for prefix in ("review-wait", "review-pinged", "reviewfix-status"):
                led.set_kv(f"{prefix}:{project}#{n}", "")
            for prefix in ("ci", "mergeability"):
                led.set_kv(f"{prefix}:{project}#{n}:{item['pr']}", "")
            queue_key = f"queue:{project}#{n}:{item['pr']}"
            queued = _kv_json(led, queue_key)
            if queued:
                queued["since"] = iso(led.now())
                led.set_kv(queue_key, json.dumps(queued))
            ctx.say(f"{project}#{n}: {role} capacity returned — verifying again")
        except Exception as exc:  # one unavailable route must not stop other items
            ctx.say(f"{project}#{n}: capacity recovery skipped — {exc}")


def _review_route(ctx, project, item, sha):
    """Share reviewer independence and risk routing with capacity recovery."""
    led, cfg, n = ctx.led, ctx.cfg, item["number"]
    # A failed/inconclusive review remains the latest run, but it did not
    # produce the PR head. Always fence against the latest builder/fixer so a
    # retry can never let the builder grade its own work (DESIGN D11).
    last = led.last_run(project, n, roles=("build", "fix"))
    builder_platform = last["platform"] if last else None
    builder_slot = router.platform_slot(cfg, builder_platform) if builder_platform else None
    size = next((l.split(":", 1)[1] for l in json.loads(row_get(item, "labels", "[]"))
                 if l.startswith("size:")), None)
    # Claude reviews when the item touches a high-risk surface (D11); the free
    # tiers otherwise lead, same order as build routing. Never pin Claude as
    # its own reviewer — if it also built this, fall back to ordinary routing
    # order (still excluding the builder below) rather than deadlocking on a
    # pin that `exclude` would immediately rule back out.
    exclude = {builder_platform} if builder_platform else set()
    dup = _kv_json(led, f"reviewdup:{project}#{n}")
    if (dup.get("sha") == sha and dup.get("reviewer")
            and not led.get_kv(f"reviewdone:{project}#{n}:{item['pr']}:{sha}")):
        exclude.add(dup["reviewer"])     # a repeat finding needs a different reviewer
    pin = ("claude" if router.risk_min_tier(row_get(item, "title", "")) > 0
           and builder_slot != "claude" else None)
    if pin and any(router.platform_slot(cfg, p) == pin for p in exclude):
        pin = None
    return pin, size, exclude


def _start_review_run(ctx, project, item, pr, view, sha):
    """Start DESIGN D11's review run: a different platform from whichever
    one produced this PR's last build/fix run, so the review is a genuine
    second opinion rather than the builder grading its own work."""
    led, cfg, n = ctx.led, ctx.cfg, item["number"]
    pol = ctx.policy(project)
    head = view.get("headRefName") or item["branch"]
    active = led.active_runs()
    busy = busy_platforms(cfg, active)
    pin, size, exclude = _review_route(ctx, project, item, sha)
    wait_key, ping_key = f"review-wait:{project}#{n}", f"review-pinged:{project}#{n}"
    capacity = router.capacity_recovery(
        cfg, led, pol, "review", pin, busy, size=size,
        burst_lines=ctx.burst_lines, exclude=exclude)
    full = len(active) >= cfg["concurrency"]["total"]
    if full or not capacity[0] or capacity[2]:
        _capacity_wait(ctx, project, item, wait_key, sha,
                       "every run slot is busy" if full else "no eligible platform for review",
                       "review", (capacity[0], None, None) if full else capacity)
        return
    # Prefer another reviewer for a metadata-only fix, but never turn that
    # preference into a capacity wait or weaken builder/slot independence.
    previous = _kv_json(led, f"reviewdone:{project}#{n}:{pr}:{sha}").get("reviewer")
    platform, reasons = router.pick_for_project(
        cfg, led, pol, "review", pin, busy, size=size,
        scorecard_rows=getattr(ctx, "scorecard_rows", None), burst_lines=ctx.burst_lines,
        exclude=exclude | ({previous} if previous else set()))
    if not platform and previous:
        platform, reasons = router.pick_for_project(
            cfg, led, pol, "review", pin, busy, size=size,
            scorecard_rows=getattr(ctx, "scorecard_rows", None),
            burst_lines=ctx.burst_lines, exclude=exclude)
    if not platform:
        detail = "; ".join(reasons) or "no eligible platform for review"
        _capacity_wait(ctx, project, item, wait_key, sha, detail, "review", capacity)
        return
    conductor = led.lease(project, n)
    handoff_from = ((CONDUCTOR, conductor["epoch"])
                    if conductor and (conductor["holder"] == CONDUCTOR
                                      or conductor["holder"].endswith("/conductor")) else None)
    if start(ctx, project, {**item, "branch": head}, "review", platform,
             handoff_from=handoff_from, size=size,
             context=review.start_context(ctx, project, item, pr, sha)):
        led.set_kv(f"review:{project}#{n}", json.dumps({"sha": sha, "pr": pr, "verdict": "pending",
            "run_id": row_get(led.last_run(project, n, roles=("review",)) or {}, "id")}))
        led.set_kv(wait_key, "")
        led.set_kv(ping_key, "")


def _capacity_wait(ctx, project, item, status_key, cycle, reason, role,
                   capacity, required_tier=None):
    """Persist a retryable wait; only configuration or unknown backstop escalates."""
    led, n = ctx.led, item["number"]
    _observe_wait(ctx, project, item, role, required_tier)
    if role == "fix":
        led.release(project, n, holder=CONDUCTOR)
    previous = _kv_json(led, status_key)
    at = previous.get("at") if previous.get("sha") == cycle else None
    at = at if router._ts(at) else iso(led.now())
    eligible, recovery, retry = capacity
    state = "capacity_wait" if eligible else "missing_tier"
    hours = ctx.policy(project).get("capacity_wait_max_hours", 24)
    if eligible and recovery is None and led.now() - parse(at) >= timedelta(hours=hours):
        state = "capacity_wait_expired"
    led.set_kv(status_key, json.dumps({
        "state": state, "sha": cycle, "at": at, "reason": reason,
        "tier": required_tier, "retry_at": iso(retry) if retry else None,
        "recovery_at": iso(recovery) if recovery else None,
    }))
    ctx.say(f"{project}#{n}: PR #{item['pr']} — {role} waiting: {reason}")
    if state == "capacity_wait":
        return
    question = (f"No configured eligible platform for {role}"
                f" at required tier {required_tier or 0} on PR #{item['pr']}: {reason}"
                if state == "missing_tier" else
                f"{role.capitalize()} capacity wait exceeded {hours} hours with no known recovery "
                f"for PR #{item['pr']}: {reason}")
    led.set_state(project, n, "needs_you", question, question=question, options="[]")
    _mark_capacity_wait(led, project, led.item(project, n), role)
    ctx.ping(f"{role.capitalize()} {state.replace('_', ' ')} — {project} #{n}", question,
             project, n, priority="high", tags="warning")


def _observe_wait(ctx, project, item, role, required_tier):
    """Record a persisted fix/review wait for the capacity history (mahler#734):
    read-only, never forces a retry and never blocks the wait itself."""
    try:
        cfg, led = ctx.cfg, ctx.led
        pol = ctx.policy(project)
        busy = busy_platforms(cfg, led.active_runs())
        size = next((l.split(":", 1)[1] for l in json.loads(row_get(item, "labels", "[]"))
                     if l.startswith("size:")), None)
        effective_size = size
        pin, exclude, tier = item["pin"], (), required_tier or 0
        if role == "review":
            pin, effective_size, exclude = _review_route(ctx, project, item, "")
            tier = 0
        elif size == "l" or (tier >= 2 and size == "s"):
            effective_size = "m"
        diag = router.diagnose(cfg, led, pol, role, pin, busy, size=effective_size,
                               burst_lines=ctx.burst_lines, min_tier=tier, exclude=exclude)
        capacity.of(ctx).observe(project, item["number"], role, blockers=["shipping_wait"],
                                 diag=diag, routing_role=role, size=size,
                                 effective_size=effective_size, required_tier=tier)
    except Exception as e:                      # noqa: BLE001 — telemetry only
        ctx.say(f"{project}#{item['number']}: capacity diagnosis failed — {e}")


def _fix_wait(ctx, project, item, key, reason, *, required_tier=None, capacity=None):
    if capacity is None:
        capacity = (["busy"], None, None)
    _capacity_wait(ctx, project, item, f"reviewfix-status:{project}#{item['number']}",
                   key, reason, "fix", capacity, required_tier)


def _finding_files(findings):
    """Return each finding's leading repo-relative location without line numbers.

    The review recipe separates findings with `` | `` and requires each one to
    name its file.  Only the leading ``file:`` location is structural; dotted
    expressions later in the prose (for example ``json.loads``) are not files.
    Unknown or malformed locations are inconclusive rather than evidence of
    divergence.
    Directory paths and every dotted filename component remain significant:
    ``src/a/index.test.ts`` and ``src/b/index.test.tsx`` are distinct files.
    Extensions are optional, and leading-dot basenames are valid:
    ``Dockerfile``, ``build/Makefile``, and ``.gitignore`` all count.
    """
    locations = set()
    for finding in re.split(r"\s+\|\s+", findings or ""):
        match = re.match(
            r"\s*(?:[-*]\s+)?`?(?P<path>(?:[\w@+.-]+/)*"
            r"\.?[\w@+-]+(?:\.[\w@+-]+)*)`?:",
            finding)
        if match:
            locations.add(match.group("path"))
    return locations


def _finding_locations(findings):
    """Every finding's leading ``file:line`` as a set of (file, line), or None
    when any finding lacks one — such a review can't be judged a repeat."""
    locations = set()
    for finding in re.split(r"\s+\|\s+", findings or ""):
        if not finding.strip():
            continue
        match = re.match(r"\s*(?:[-*]\s+)?`?(?P<path>[\w@+./-]+?):(?P<line>\d+)`?\b",
                         finding)
        if not match:
            return None
        locations.add((match.group("path"), int(match.group("line"))))
    return locations or None


def _repeat_finding(ctx, project, n, view):
    """Whether this failed review repeats the previous round's finding: every
    finding names a file:line the previous round also named, on a different
    PR head, and the cited source line reads the same at both heads. A
    reviewer misreading masked tool output (`******`, mahler#628) re-fails the
    same unchanged line every round; a real regression or a changed line is
    never a repeat. Anything unreadable counts as not a repeat."""
    led, sha = ctx.led, view.get("headRefOid")
    history = [r for r in json.loads(led.get_kv(f"reviewfindings:{project}#{n}") or "[]")
               if r.get("pr", led.item(project, n)["pr"]) == led.item(project, n)["pr"]]
    if len(history) < 2 or history[-1]["sha"] != sha:
        return None
    prev, cur = history[-2], history[-1]
    if not prev.get("sha") or prev["sha"] == sha:
        return None
    if cur.get("classified") is not None or prev.get("classified") is not None:
        if (cur.get("classified") is None or prev.get("classified") is None
                or not review.same_findings(prev["classified"], cur["classified"])):
            return None
    now, before = _finding_locations(cur["findings"]), _finding_locations(prev["findings"])
    if not now or not before or not now <= before:
        return None
    gh, path = ctx.gh(project), ctx.policy(project)["path"]
    try:
        for file, line in now:
            old = gh.source_line(path, prev["sha"], file, line)
            if old is None or old != gh.source_line(path, sha, file, line):
                return None
    except GHError:
        return None
    return cur


def _review_not_converging(ctx, project, item, pr, view):
    """Record two divergent transitions as evidence, without stopping shipping."""
    led, n = ctx.led, item["number"]
    window = review.window(led, project, n, pr)
    rounds = [{"sha": r["sha"], "findings": review.render(r["reviews"][-1]["classified"])
               if r["reviews"][-1].get("classified") is not None
               else r["reviews"][-1].get("findings") or ""} for r in window]
    # Legacy failed-review evidence remains useful during rollout.
    if len(rounds) < 3:
        history = json.loads(led.get_kv(f"reviewfindings:{project}#{n}") or "[]")
        rounds = [r for r in history[int(led.get_kv(f"reviewconvergence:{project}#{n}") or 0):]
                  if r.get("pr", pr) == pr]
    if len(rounds) < 3 or rounds[-1]["sha"] != view.get("headRefOid"):
        return False

    # A location-less review is inconclusive. Overlap with any earlier round
    # means the latest problem is not new, so it resets the divergence streak.
    seen_files = set()
    divergent = 0
    for round_ in rounds:
        files = _finding_files(round_["findings"])
        if not files or files & seen_files:
            divergent = 0
        elif seen_files:
            divergent += 1
        seen_files.update(files)
    if divergent < 2:
        return False

    # File drift is diagnostic evidence, never a reason to demote a blocker
    # or ask the owner to arbitrate review scope.
    if not ctx.dry_run:
        led.set_kv(f"reviewdrift:{project}#{n}:{pr}", json.dumps({
            "sha": view.get("headRefOid"), "rounds": rounds,
            "reason": "Different files signal scope drift; classified evidence decides blocking."}))
    return True


def _explored_head(led, project, n):
    """Whether the PR head now failing came from an exploration run (mahler#423).

    Judged by the run that pushed it — the latest build/fix that ended DONE —
    not the latest row: a fix that failed to launch or died on quota leaves a
    newer row with explore=0, and must not turn an explored failure into a
    spent attempt on the next tick."""
    run = led.q1("SELECT explore FROM runs WHERE project=? AND number=? "
                 "AND role IN ('build','fix') AND outcome='DONE' "
                 "ORDER BY id DESC LIMIT 1", (project, n))
    return bool(run and run["explore"])


def _review_triggered_fix(ctx, project, item, pr, view, findings, *, base_conflict=False):
    """A failed review feeds back as a fix round (BACKLOG's resolved "output
    shape"): the same routing and attempts/escalation bookkeeping as a red-CI
    fix (`_red_ci`), except the fix prompt carries the review's findings
    instead of a failing-log tail, and the dedup key is its own so a review
    failure and a CI failure on the same sha are never double-counted as one
    event. Left as its own function rather than sharing `_red_ci`'s body
    (mahler#232's per-cycle dedup fix lives there) so this new path can never
    perturb that already-hardened one."""
    led, cfg, n = ctx.led, ctx.cfg, item["number"]
    sha = view.get("headRefOid") or ""
    dup_key = f"reviewdup:{project}#{n}"
    if (not base_conflict and _kv_json(led, dup_key).get("sha") != sha
            and not led.get_kv(f"reviewdone:{project}#{n}:{pr}:{sha}")):
        repeated = _repeat_finding(ctx, project, n, view)
        if repeated:
            # Same finding on the same unchanged line: a second opinion, not
            # another fix run, ping or tier escalation. If the alternate
            # reviewer fails it too, the record above lets the normal fix flow run.
            led.set_kv(dup_key, json.dumps({
                "sha": sha, "reviewer": repeated.get("platform")}))
            led.set_kv(f"review:{project}#{n}", None)
            ctx.say(f"{project}#{n}: PR #{pr} — review repeats its previous finding on an "
                    "unchanged line; asking a reviewer on another platform")
            led.event("review_repeat", project, n,
                      {"sha": sha, "reviewer": repeated.get("platform")})
            return
    pol = ctx.policy(project)
    head = view.get("headRefName") or item["branch"]
    last = led.last_run(project, n, roles=("build", "fix"))
    explore_failure = base_conflict or _explored_head(led, project, n)
    attempts = item["attempts"] + (0 if explore_failure else 1)
    size = next((l.split(":", 1)[1] for l in json.loads(row_get(item, "labels", "[]"))
                 if l.startswith("size:")), None)
    cur_tier = router.cap_escalation(cfg, pol, row_get(item, "esc_tier", 0),
                                     size, pin=item["pin"])
    if cur_tier != row_get(item, "esc_tier", 0):
        led.upsert_item(project, n, esc_tier=cur_tier)
    key = f"reviewfix:{project}#{n}:{pr}:{view.get('headRefOid') or ''}"
    _clear_charged_if_fix_completed(led, project, n, key)
    if led.get_kv(f"{key}:charged"):
        attempts = item["attempts"]
    if not led.get_kv(key):
        led.set_kv(key, iso(led.now()))

        cur_fails = row_get(item, "esc_fails", 0)
        new_fails = cur_fails + (0 if explore_failure else 1)
        new_tier = cur_tier
        last_platform = last["platform"] if last else None
        run_tier = router.tier_of(cfg.get("platforms", {}).get(last_platform, {})) if last_platform else 1
        if not explore_failure and new_fails >= 2:
            new_tier = router.cap_escalation(cfg, pol, max(cur_tier, run_tier) + 1,
                                              size, pin=item["pin"])
            new_fails = 0
            ctx.say(f"{project}#{n}: escalated to tier {new_tier} after a failed review on tier <= {max(cur_tier, run_tier)}")
            led.event("escalated", project, n, {"tier_from": cur_tier, "tier_to": new_tier,
                      "platform": last_platform, "reason": "failed review"})

        if not explore_failure and attempts >= pol["max_attempts"]:
            led.set_state(project, n, "failed",
                          f"review still failing on PR #{pr} after {attempts} attempts",
                          attempts=attempts, esc_tier=new_tier, esc_fails=new_fails)
            failures.report(ctx, project, n, "review_rejected",
                            run=led.last_run(project, n, roles=("review",)),
                            output=findings, branch=head)
            ctx.ping(f"Stuck — {project} #{n}",
                     f"the review kept failing ({attempts} attempts). Comment `/mahler go` to retry.",
                     project, n, priority="high", tags="warning")
            led.release(project, n, holder=CONDUCTOR)
            return

        led.upsert_item(project, n, esc_tier=new_tier, esc_fails=new_fails)
        cur_tier = new_tier
        ctx.ping(f"{'Base conflict' if base_conflict else 'Review failed'} — {project} #{n}",
                 (f"PR #{pr}: conflicts with its target; " if base_conflict else
                  f"PR #{pr}: the independent review found blocking issues; ") +
                 "the conductor starts a fix run on it",
                 project, n, priority="high", tags="warning")

    active = led.active_runs()
    if len(active) >= cfg["concurrency"]["total"]:
        ctx.say(f"{project}#{n}: PR #{pr} — review failed, but every run slot is busy; "
                "the fix waits for the next tick")
        _fix_wait(ctx, project, item, key, "every run slot is busy")
        return
    busy = busy_platforms(cfg, active)
    real_size = size
    if size == "l":
        size = "m"
    effective_min_tier = max(cur_tier, router.risk_min_tier(row_get(item, "title", "")))
    if effective_min_tier >= 2 and size == "s":
        size = "m"
    capacity = router.capacity_recovery(
        cfg, led, pol, "fix", item["pin"], busy, size=size,
        min_tier=effective_min_tier, burst_lines=ctx.burst_lines)
    platform = router.explore_for_project(
        cfg, led, pol, item, "fix", busy, size=real_size,
        scorecard_rows=getattr(ctx, "scorecard_rows", None), burst_lines=ctx.burst_lines, min_tier=effective_min_tier)
    explore = platform is not None
    reasons = []
    if explore:
        size = real_size
        ctx.say(f"{project}#{n}: trying {platform} (fix exploration)")
    else:
        if not capacity[0] or capacity[2]:
            _fix_wait(ctx, project, item, key, "no eligible route with capacity",
                      required_tier=effective_min_tier, capacity=capacity)
            return
        platform, reasons = router.pick_for_project(
            cfg, led, pol, "fix", item["pin"], busy, size=size,
            scorecard_rows=getattr(ctx, "scorecard_rows", None), burst_lines=ctx.burst_lines, min_tier=effective_min_tier)
    if not platform:
        ctx.say(f"{project}#{n}: PR #{pr} — review failed, no platform for a fix run — "
                f"{'; '.join(reasons)}")
        _fix_wait(ctx, project, item, key, "; ".join(reasons) or "no eligible route",
                  required_tier=effective_min_tier, capacity=capacity)
        return
    led.upsert_item(project, n, branch=head)
    conductor = led.lease(project, n)
    handoff_from = ((CONDUCTOR, conductor["epoch"])
                    if conductor and (conductor["holder"] == CONDUCTOR
                                      or conductor["holder"].endswith("/conductor")) else None)
    context = ("- an independent review of this PR found blocking issues (posted as a PR "
               f"comment already); address every one of them, verify, push, and end with "
               f"STATUS: DONE:\n\n{findings}" if findings else
               "- an independent review of this PR found blocking issues (see the PR "
               "comments); address them, verify, push, and end with STATUS: DONE")
    if base_conflict:
        context = findings
    if start(ctx, project, {**item, "branch": head}, "fix", platform,
             handoff_from=handoff_from, size=size, context=context, fix_reason="review",
             **({"explore": True} if explore else {})):
        led.set_kv(f"reviewfix-status:{project}#{n}", json.dumps({"state": "running"}))
        led.upsert_item(project, n, attempts=attempts)
        led.set_kv(f"{key}:charged", "1")
        if not base_conflict:
            run = led.last_run(project, n, roles=("fix",))
            led.set_kv(f"{key}:run", json.dumps({
                "run_id": run["id"] if run else None,
                "verdict": _kv_json(led, f"review:{project}#{n}")}))


def _red_ci(ctx, project, item, pr, view):
    """Red CI on a verifying item: a fix run (D18's second run role) starts on
    the PR's head branch, its prompt carrying the failing-log tail (runner
    fetches it). Routing is the build routing (D8), and `max_attempts` caps
    build and fix runs together — each red cycle counts as an attempt — then
    escalation as today (retry_or_fail's stuck branch).

    This runs every tick while the PR stays red, so the counting above must
    fire once per CI cycle, not once per tick (mahler#232): the same head sha
    is re-observed on every tick that a fix run couldn't start (no free slot,
    no platform), and previously re-incremented esc_fails/esc_tier and
    re-evaluated the max_attempts cutoff each time. It's now gated behind the
    same per-sha key already used to dedupe the ping. Ticks after the first
    still retry starting a fix run — a platform may free up — without
    counting a cycle already counted.
    """
    led, cfg, n = ctx.led, ctx.cfg, item["number"]
    pol = ctx.policy(project)
    head = view.get("headRefName") or item["branch"]
    last = led.last_run(project, n, roles=("build", "fix"))
    explore_failure = _explored_head(led, project, n)
    attempts = item["attempts"] + (0 if explore_failure else 1)
    size = next((l.split(":", 1)[1] for l in json.loads(row_get(item, "labels", "[]"))
                 if l.startswith("size:")), None)
    cur_tier = router.cap_escalation(cfg, pol, row_get(item, "esc_tier", 0),
                                     size, pin=item["pin"])
    if cur_tier != row_get(item, "esc_tier", 0):
        led.upsert_item(project, n, esc_tier=cur_tier)
    key = f"red:{project}#{n}:{pr}:{view.get('headRefOid') or ''}"
    _clear_charged_if_fix_completed(led, project, n, key)
    if led.get_kv(f"{key}:charged"):
        attempts = item["attempts"]
    if not led.get_kv(key):
        led.set_kv(key, iso(led.now()))

        cur_fails = row_get(item, "esc_fails", 0)
        new_fails = cur_fails + (0 if explore_failure else 1)
        new_tier = cur_tier
        last_platform = last["platform"] if last else None
        run_tier = router.tier_of(cfg.get("platforms", {}).get(last_platform, {})) if last_platform else 1
        if not explore_failure and new_fails >= 2:
            new_tier = router.cap_escalation(cfg, pol, max(cur_tier, run_tier) + 1,
                                              size, pin=item["pin"])
            new_fails = 0
            ctx.say(f"{project}#{n}: escalated to tier {new_tier} after red CI on tier <= {max(cur_tier, run_tier)}")
            led.event("escalated", project, n, {"tier_from": cur_tier, "tier_to": new_tier,
                      "platform": last_platform, "reason": "red CI"})

        if not explore_failure and attempts >= pol["max_attempts"]:
            led.set_state(project, n, "failed",
                          f"CI still red on PR #{pr} after {attempts} attempts",
                          attempts=attempts, esc_tier=new_tier, esc_fails=new_fails)
            failures.report(ctx, project, n, "ci_failed", run=last, branch=head)
            ctx.ping(f"Stuck — {project} #{n}",
                     f"CI stayed red ({attempts} attempts). Comment `/mahler go` to retry.",
                     project, n, priority="high", tags="warning")
            led.release(project, n, holder=CONDUCTOR)
            return

        led.upsert_item(project, n, esc_tier=new_tier, esc_fails=new_fails)
        cur_tier = new_tier
        ctx.ping(f"CI red — {project} #{n}",
                 f"PR #{pr} failed CI; the conductor starts a fix run on it",
                 project, n, priority="high", tags="warning")

    active = led.active_runs()
    if len(active) >= cfg["concurrency"]["total"]:
        ctx.say(f"{project}#{n}: PR #{pr} — CI red, but every run slot is busy; "
                "the fix waits for the next tick")
        _fix_wait(ctx, project, item, key, "every run slot is busy")
        return
    busy = busy_platforms(cfg, active)
    # For fix runs, treat size:l as size:m so a CI fix never needs Opus by size alone (DESIGN D21)
    real_size = size
    if size == "l":
        size = "m"
    effective_min_tier = max(cur_tier, router.risk_min_tier(row_get(item, "title", "")))
    if effective_min_tier >= 2 and size == "s":
        size = "m"
    # D26: route within the project's declared accounts: fallback order,
    # equal round-robin, or an explicit cross-account priority.
    capacity = router.capacity_recovery(
        cfg, led, pol, "fix", item["pin"], busy, size=size,
        min_tier=effective_min_tier, burst_lines=ctx.burst_lines)
    platform = router.explore_for_project(
        cfg, led, pol, item, "fix", busy, size=real_size,
        scorecard_rows=getattr(ctx, "scorecard_rows", None), burst_lines=ctx.burst_lines, min_tier=effective_min_tier)
    explore = platform is not None
    reasons = []
    if explore:
        size = real_size
        ctx.say(f"{project}#{n}: trying {platform} (fix exploration)")
    else:
        if not capacity[0] or capacity[2]:
            _fix_wait(ctx, project, item, key, "no eligible route with capacity",
                      required_tier=effective_min_tier, capacity=capacity)
            return
        platform, reasons = router.pick_for_project(
            cfg, led, pol, "fix", item["pin"], busy, size=size,
            scorecard_rows=getattr(ctx, "scorecard_rows", None), burst_lines=ctx.burst_lines, min_tier=effective_min_tier)
    if not platform:
        ctx.say(f"{project}#{n}: PR #{pr} — CI red, no platform for a fix run — "
                f"{'; '.join(reasons)}")
        _fix_wait(ctx, project, item, key, "; ".join(reasons) or "no eligible route",
                  required_tier=effective_min_tier, capacity=capacity)
        return
    led.upsert_item(project, n, branch=head)
    conductor = led.lease(project, n)
    handoff_from = ((CONDUCTOR, conductor["epoch"])
                    if conductor and (conductor["holder"] == CONDUCTOR
                                      or conductor["holder"].endswith("/conductor")) else None)
    if start(ctx, project, {**item, "branch": head}, "fix", platform,
             handoff_from=handoff_from, size=size, fix_reason="ci",
             **({"explore": True} if explore else {})):
        led.set_kv(f"reviewfix-status:{project}#{n}", json.dumps({"state": "running"}))
        led.upsert_item(project, n, attempts=attempts)
        led.set_kv(f"{key}:charged", "1")


def _open_pr(ctx, project, item, gh, pol, unconfirmed=False):
    led, n = ctx.led, item["number"]
    ref = item["branch"]
    if not ref:
        retry_or_fail(ctx, project, n, led.item(project, n), None, "nothing to ship")
        led.release(project, n, holder=CONDUCTOR)
        return
    base = pol.get("base", "main")
    branch = None
    if item["pr"]:
        try:
            view = gh.pr_view(item["pr"])
            if view["state"] == "MERGED":
                _shipped(ctx, project, n, item["pr"], item, view, merged=False)
                return
            if view.get("headRefName"):
                branch = view["headRefName"]
        except (GHError, ValueError) as e:
            ctx.say(f"{project}#{n}: existing PR #{item['pr']} lookup failed — {e}")
            led.release(project, n, holder=CONDUCTOR)
            return

    if not branch:
        branch = f"mahler/{n}-{runner.slug(item['title'])}"

    try:
        sha = gh.push_branch(pol["path"], branch, ref)   # the branch, pushed if needed
        pr = gh.pr_for_head(branch)
        created = False
        if pr is None:
            needs = needs_human_of(gh.issue_body(n))
            pr = gh.pr_create(branch, base, item["title"],
                              pr_body(n, item["summary"], needs, unconfirmed=unconfirmed))
            created = True
    except (GHError, ValueError) as e:
        ctx.say(f"{project}#{n}: opening PR failed — {e}")
        led.release(project, n, holder=CONDUCTOR)
        return
    # Keep the durable rebuild fallback separately from the branch watched for
    # CI: a closed PR's head may be deleted before runner.prepare fetches/prunes.
    # Save it first so a crash cannot leave only the disposable PR head recorded.
    if ref.startswith("mahler/snapshot/"):
        led.set_kv(f"ship_snapshot:{project}#{n}", ref)
    led.upsert_item(project, n, pr=pr, branch=branch)
    led.event("pr_opened", project, n, {"pr": pr, "branch": branch, "sha": sha})
    action = f"opened PR #{pr}" if created else f"adopted PR #{pr}"
    ctx.say(f"{project}#{n}: {action} from `{branch}` (base {base}) — verifying")


def pr_merged(view):
    """True only with proof the PR merged: a closed-unmerged PR also leaves
    'not OPEN' behind, and must not become UAT, release-note or scorecard
    evidence of a shipped change."""
    return view.get("state") == "MERGED" or bool(view.get("mergedAt"))


def record_uat_if_needed(ctx, project, n, pr, item, view):
    """Register every confirmed merge, preserving custom checks when supplied.

    Used by the conductor and by sync's recovery of externally merged PRs.
    A temporary write failure is retried by the closed-shipment evidence poll.
    """
    needs = needs_human_of(view.get("body"))
    if not pr_merged(view):
        return needs
    needs = needs or "- Verify the shipped change and record passing evidence."
    sha = (view.get("mergeCommit") or {}).get("oid") or ""
    try:
        ctx.led.add_uat(project, n, pr, sha, row_get(item, "title", ""), needs,
                        shipped_at=view.get("mergedAt"))
    except Exception as e:                  # noqa: BLE001 — a ship must not break
        ctx.say(f"{project}#{n}: couldn't record the UAT item — {e}")
    return needs


def record_release_item_if_needed(ctx, project, n, pr, item, view):
    """Snapshot shipped issue into the project's unreleased draft (DESIGN D31).
    Bookkeeping — a failure here never stops the ship."""
    if not pr_merged(view):
        return
    sha = (view.get("mergeCommit") or {}).get("oid") or view.get("headRefOid") or ""
    summary = row_get(item, "summary", "") or pr_summary_of(view.get("body")) or ""
    labels = row_get(item, "labels", "[]")
    title = row_get(item, "title", "")
    try:
        ctx.led.snapshot_release_item(
            project, n, pr=pr, title=title, summary=summary,
            merge_sha=sha, labels=labels, shipped_at=view.get("mergedAt") or iso(ctx.led.now()))
    except Exception as e:                  # noqa: BLE001 — a ship must not break
        ctx.say(f"{project}#{n}: couldn't snapshot release item — {e}")


def _shipped(ctx, project, n, pr, item, view, merged=True):
    """Close the loop: comment the summary plus the issue's 'Needs a human to
    check' list, ping, and mark the item shipped."""
    if not pr_merged(view):
        raise ValueError(f"PR #{pr} has not been confirmed merged")
    led = ctx.led
    how = "squash-merged" if merged else "merged"
    lines = [f"**Shipped** — PR #{pr} {how}.", "",
             item["summary"] or pr_summary_of(view.get("body")) or ""]
    needs = record_uat_if_needed(ctx, project, n, pr, item, view)
    record_release_item_if_needed(ctx, project, n, pr, item, view)
    if needs:
        lines += ["", "## Needs a human to check", needs]
    try:
        ctx.gh(project).comment(n, "\n".join(lines))
    except GHError as e:
        ctx.say(f"{project}#{n}: couldn't post the shipped comment — {e}")
    ctx.ping(f"Shipped — {project} #{n}", item["title"], project, n, tags="rocket")
    led.set_state(project, n, "shipped", f"shipped via PR #{pr}")
    maintenance = config.maintenance_policy(ctx.cfg, project)
    passes = maintenance["passes"] if maintenance["enabled"] else ()
    led.event("shipped", project, n, {"pr": pr, "merged": pr_merged(view)}, passes=passes)
    # Platform-audit pass (mahler#206) is not one of the eight D20 passes and
    # isn't per-project opt-in, so it isn't in `passes` above — it anchors on
    # its own configured project's throughput instead.
    audit_pol = config.platform_audit_policy(ctx.cfg)
    if audit_pol["enabled"] and project == audit_pol["project"]:
        led.increment_maintenance_merged(project, config.PLATFORM_AUDIT_PASS)
    led.release(project, n, holder=CONDUCTOR)
    mirror_shipped(ctx, project, led.item(project, n))


def mirror_shipped(ctx, project, item):
    """Close and label a shipment, retryable even after it leaves the open poll."""
    n = item["number"]
    try:
        gh = ctx.gh(project)
        if gh.issue_state(n) != "CLOSED":
            gh.close_issue(n)
        current = gh.issue_labels(n)
        gh.set_state_label(n, "shipped", current)
        ctx.led.upsert_item(project, n, mirror="mahler:shipped")
        ctx._labels.pop((project, n), None)
    except GHError as e:
        ctx.say(f"{project}#{n}: shipped label update failed — {e}")
