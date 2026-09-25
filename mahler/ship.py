"""The conductor ships (DESIGN D18).

The mechanical tail of a build run, in code: push the branch, open the PR,
watch CI across ticks, start a fix run when CI goes red, and squash-merge on
green — but never merge once the item's lease has gone to a session (D6).
"""

import json
from datetime import timedelta

from . import config, router, runner
from .finalize import retry_or_fail
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
    for item in ctx.led.items(project, ["verifying"]):
        try:
            _ship_item(ctx, project, item)
        except Exception as e:                  # noqa: BLE001 — one item can't stop the rest
            ctx.say(f"{project}#{item['number']}: shipping failed — {e}")


def _ship_lease(ctx, project, item):
    """Take the conductor's lease on an item that is ready to ship.
    -> True when we hold it; each refusal says why and leaves the PR alone."""
    led, n = ctx.led, item["number"]
    lease, info = led.claim(project, n, CONDUCTOR, "auto",
                            ctx.policy(project)["auto_lease_minutes"])
    if lease is not None:
        return True
    if "unavailable" in info:
        ctx.say(f"{project}#{n}: canonical lease host unavailable — shipping skipped")
    elif "at_capacity" in info:
        ctx.say(f"{project}#{n}: canonical project capacity is already held — shipping skipped")
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


def _closed_pr(ctx, project, item, pr, view):
    """Recover an unmerged closure without recording any shipped evidence."""
    led, n = ctx.led, item["number"]
    head = view.get("headRefName") or item["branch"]
    replacement = ctx.gh(project).pr_for_head(head) if head else None
    reason = f"PR #{pr} was closed without merging"
    if replacement and replacement != pr:
        if not ctx.dry_run:
            if not _ship_lease(ctx, project, item):
                return
            led.upsert_item(project, n, pr=replacement)
        action = f"verifying replacement PR #{replacement}"
    else:
        action = "failed" if item["attempts"] + 1 >= ctx.policy(project)["max_attempts"] else "ready"
        if not ctx.dry_run:
            if not _ship_lease(ctx, project, item):
                return
            led.upsert_item(project, n, pr=None)
            retry_or_fail(ctx, project, n, item, None, reason)
    ctx.say(f"{project}#{n}: {reason} — {'would go to ' if ctx.dry_run else ''}{action}")
    if not ctx.dry_run:
        led.release(project, n, holder=CONDUCTOR)


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
    if view["state"] == "MERGED":
        _shipped(ctx, project, n, pr, led.item(project, n), view, merged=False)
        return
    if view["state"] != "OPEN":
        _closed_pr(ctx, project, item, pr, view)
        return
    if view.get("mergeable") == "CONFLICTING":
        base = view.get("baseRefName") or ctx.policy(project).get("base", "main")
        _rebuild_on_base(ctx, project, item, pr, base)
        return
    state = checks_state(view.get("statusCheckRollup"))
    # Keep the console's explanation in step with the state this watcher saw.
    # The timestamp for a pending head is initialized by _ci_pending; recording
    # the terminal state here prevents an old pending timestamp from being
    # mistaken for the current wait after CI finishes.
    if state == "pending" or view.get("mergeable") == "UNKNOWN":
        _ci_pending(ctx, project, item, pr, view)
        return
    ci_key = f"ci:{project}#{item['number']}:{pr}"
    ci_seen = led.get_kv(ci_key)
    ci_info = json.loads(ci_seen) if ci_seen else {}
    ci_info["state"] = state
    if view.get("headRefOid"):
        ci_info["sha"] = view["headRefOid"]
    led.set_kv(ci_key, json.dumps(ci_info))
    if state == "red":
        # D18 (mahler#18): red CI starts a fix run from the PR branch, with the
        # failing log in its prompt. The conductor's lease goes to the run.
        _red_ci(ctx, project, item, pr, view)
        return
    _review_gate(ctx, project, item, pr, view)


def _ship_item(ctx, project, item):
    led, n = ctx.led, item["number"]
    if ctx.dry_run:
        if item["pr"]:
            view = ctx.gh(project).pr_view(item["pr"])
            if view["state"] not in ("OPEN", "MERGED"):
                _closed_pr(ctx, project, item, item["pr"], view)
                return
        ctx.say(f"{project}#{n}: would ship "
                f"{'PR #' + str(item['pr']) if item['pr'] else '(opening the PR)'}")
        return
    if not _ship_lease(ctx, project, item):
        return
    if item["pr"]:
        # A rebuild may retain the old PR number while replacing its branch.
        # Resolve the current open PR before watching that stale number. Saved
        # snapshots are pushed to the canonical PR branch by _open_pr.
        branch = item["branch"]
        if branch and branch.startswith("mahler/snapshot/"):
            branch = f"mahler/{n}-{runner.slug(item['title'])}"
        replacement = ctx.gh(project).pr_for_head(branch) if branch else None
        if replacement and replacement != item["pr"]:
            if not _ship_lease(ctx, project, item):
                return
            led.upsert_item(project, n, pr=replacement)
            led.release(project, n, holder=CONDUCTOR)
            ctx.say(f"{project}#{n}: adopted PR #{replacement} — verifying next tick")
            return
        _watch_pr(ctx, project, item, item["pr"])
        return
    # no PR yet: open it — its CI is watched from the next tick
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
            _rebuild_on_base(ctx, project, item, pr, fresh["baseRefName"], stale=True)
            return
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
    if not _review_required(item):
        _merge_queued(ctx, project, item, pr, view)
        return
    sha = view.get("headRefOid") or ""
    key = f"review:{project}#{n}"
    seen = led.get_kv(key)
    info = json.loads(seen) if seen else {}
    if info.get("sha") == sha:
        verdict = info.get("verdict")
        if verdict == "pass":
            _merge_queued(ctx, project, item, pr, view)
            return
        if verdict == "fail":
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


def _start_review_run(ctx, project, item, pr, view, sha):
    """Start DESIGN D11's review run: a different platform from whichever
    one produced this PR's last build/fix run, so the review is a genuine
    second opinion rather than the builder grading its own work."""
    led, cfg, n = ctx.led, ctx.cfg, item["number"]
    pol = ctx.policy(project)
    head = view.get("headRefName") or item["branch"]
    active = led.active_runs()
    if len(active) >= cfg["concurrency"]["total"]:
        ctx.say(f"{project}#{n}: PR #{pr} — CI green, but every run slot is busy; "
                "the review waits for the next tick")
        return
    busy = busy_platforms(cfg, active)
    last = led.last_run(project, n)
    builder_platform = last["platform"] if last else None
    size = next((l.split(":", 1)[1] for l in json.loads(row_get(item, "labels", "[]"))
                 if l.startswith("size:")), None)
    # Claude reviews when the item touches a high-risk surface (D11); the free
    # tiers otherwise lead, same order as build routing. Never pin Claude as
    # its own reviewer — if it also built this, fall back to ordinary routing
    # order (still excluding the builder below) rather than deadlocking on a
    # pin that `exclude` would immediately rule back out.
    pin = ("claude" if router.risk_min_tier(row_get(item, "title", "")) > 0
           and builder_platform != "claude" else None)
    platform, reasons = router.pick_for_project(
        cfg, led, pol, "review", pin, busy, size=size,
        burst_lines=ctx.burst_lines, exclude={builder_platform} if builder_platform else set())
    if not platform:
        ctx.say(f"{project}#{n}: PR #{pr} — CI green, no platform for the review — "
                f"{'; '.join(reasons)}")
        return
    conductor = led.lease(project, n)
    handoff_from = ((CONDUCTOR, conductor["epoch"])
                    if conductor and (conductor["holder"] == CONDUCTOR
                                      or conductor["holder"].endswith("/conductor")) else None)
    if start(ctx, project, {**item, "branch": head}, "review", platform,
             handoff_from=handoff_from, size=size):
        led.set_kv(f"review:{project}#{n}", json.dumps({"sha": sha, "verdict": "pending"}))


def _fix_wait(ctx, project, item, key, reason, *, required_tier=None):
    """A fix waiting for capacity must not lock out other shippable PRs."""
    led, n = ctx.led, item["number"]
    led.release(project, n, holder=CONDUCTOR)
    led.set_kv(f"reviewfix-status:{project}#{n}", json.dumps({
        "reason": reason,
        "tier": required_tier if required_tier is not None else row_get(item, "esc_tier", 0),
        "at": iso(led.now()),
    }))
    minutes = ctx.policy(project).get("verify_timeout_minutes", 120)
    if led.now() - parse(led.get_kv(key)) <= timedelta(minutes=minutes):
        return
    question = f"No fix run could start for PR #{item['pr']} after {minutes} minutes: {reason}"
    led.set_state(project, n, "needs_you", question, question=question, options="[]")
    ctx.ping(f"Fix waiting — {project} #{n}", question,
             project, n, priority="high", tags="warning")


def _review_triggered_fix(ctx, project, item, pr, view, findings):
    """A failed review feeds back as a fix round (BACKLOG's resolved "output
    shape"): the same routing and attempts/escalation bookkeeping as a red-CI
    fix (`_red_ci`), except the fix prompt carries the review's findings
    instead of a failing-log tail, and the dedup key is its own so a review
    failure and a CI failure on the same sha are never double-counted as one
    event. Left as its own function rather than sharing `_red_ci`'s body
    (mahler#232's per-cycle dedup fix lives there) so this new path can never
    perturb that already-hardened one."""
    led, cfg, n = ctx.led, ctx.cfg, item["number"]
    pol = ctx.policy(project)
    head = view.get("headRefName") or item["branch"]
    attempts = item["attempts"] + 1
    size = next((l.split(":", 1)[1] for l in json.loads(row_get(item, "labels", "[]"))
                 if l.startswith("size:")), None)
    cur_tier = router.cap_escalation(cfg, pol, row_get(item, "esc_tier", 0),
                                     size, pin=item["pin"])
    if cur_tier != row_get(item, "esc_tier", 0):
        led.upsert_item(project, n, esc_tier=cur_tier)
    key = f"reviewfix:{project}#{n}:{pr}:{view.get('headRefOid') or ''}"
    if not led.get_kv(key):
        led.set_kv(key, iso(led.now()))

        cur_fails = row_get(item, "esc_fails", 0)
        new_fails = cur_fails + 1
        new_tier = cur_tier
        last = led.last_run(project, n, roles=("build", "fix"))
        last_platform = last["platform"] if last else None
        run_tier = router.tier_of(cfg.get("platforms", {}).get(last_platform, {})) if last_platform else 1
        if new_fails >= 2:
            new_tier = router.cap_escalation(cfg, pol, max(cur_tier, run_tier) + 1,
                                              size, pin=item["pin"])
            new_fails = 0
            ctx.say(f"{project}#{n}: escalated to tier {new_tier} after a failed review on tier <= {max(cur_tier, run_tier)}")
            led.event("escalated", project, n, {"tier_from": cur_tier, "tier_to": new_tier,
                      "platform": last_platform, "reason": "failed review"})

        if attempts >= pol["max_attempts"]:
            led.set_state(project, n, "failed",
                          f"review still failing on PR #{pr} after {attempts} attempts",
                          attempts=attempts, esc_tier=new_tier, esc_fails=new_fails)
            ctx.ping(f"Stuck — {project} #{n}",
                     f"the review kept failing ({attempts} attempts). Comment `/mahler go` to retry.",
                     project, n, priority="high", tags="warning")
            led.release(project, n, holder=CONDUCTOR)
            return

        led.upsert_item(project, n, esc_tier=new_tier, esc_fails=new_fails)
        cur_tier = new_tier
        ctx.ping(f"Review failed — {project} #{n}",
                 f"PR #{pr}: the independent review found blocking issues; "
                 "the conductor starts a fix run on it",
                 project, n, priority="high", tags="warning")

    active = led.active_runs()
    if len(active) >= cfg["concurrency"]["total"]:
        ctx.say(f"{project}#{n}: PR #{pr} — review failed, but every run slot is busy; "
                "the fix waits for the next tick")
        _fix_wait(ctx, project, item, key, "every run slot is busy")
        return
    busy = busy_platforms(cfg, active)
    if size == "l":
        size = "m"
    effective_min_tier = max(cur_tier, router.risk_min_tier(row_get(item, "title", "")))
    if effective_min_tier >= 2 and size == "s":
        size = "m"
    platform, reasons = router.pick_for_project(
        cfg, led, pol, "fix", item["pin"], busy, size=size,
        burst_lines=ctx.burst_lines, min_tier=effective_min_tier)
    if not platform:
        ctx.say(f"{project}#{n}: PR #{pr} — review failed, no platform for a fix run — "
                f"{'; '.join(reasons)}")
        _fix_wait(ctx, project, item, key, "; ".join(reasons) or "no eligible route",
                  required_tier=effective_min_tier)
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
    if start(ctx, project, {**item, "branch": head}, "fix", platform,
             handoff_from=handoff_from, size=size, context=context):
        led.set_kv(f"reviewfix-status:{project}#{n}", json.dumps({"state": "running"}))
        led.upsert_item(project, n, attempts=attempts)


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
    attempts = item["attempts"] + 1
    size = next((l.split(":", 1)[1] for l in json.loads(row_get(item, "labels", "[]"))
                 if l.startswith("size:")), None)
    cur_tier = router.cap_escalation(cfg, pol, row_get(item, "esc_tier", 0),
                                     size, pin=item["pin"])
    if cur_tier != row_get(item, "esc_tier", 0):
        led.upsert_item(project, n, esc_tier=cur_tier)
    key = f"red:{project}#{n}:{pr}:{view.get('headRefOid') or ''}"
    if not led.get_kv(key):
        led.set_kv(key, iso(led.now()))

        cur_fails = row_get(item, "esc_fails", 0)
        new_fails = cur_fails + 1
        new_tier = cur_tier
        last = led.last_run(project, n, roles=("build", "fix"))
        last_platform = last["platform"] if last else None
        run_tier = router.tier_of(cfg.get("platforms", {}).get(last_platform, {})) if last_platform else 1
        if new_fails >= 2:
            new_tier = router.cap_escalation(cfg, pol, max(cur_tier, run_tier) + 1,
                                              size, pin=item["pin"])
            new_fails = 0
            ctx.say(f"{project}#{n}: escalated to tier {new_tier} after red CI on tier <= {max(cur_tier, run_tier)}")
            led.event("escalated", project, n, {"tier_from": cur_tier, "tier_to": new_tier,
                      "platform": last_platform, "reason": "red CI"})

        if attempts >= pol["max_attempts"]:
            led.set_state(project, n, "failed",
                          f"CI still red on PR #{pr} after {attempts} attempts",
                          attempts=attempts, esc_tier=new_tier, esc_fails=new_fails)
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
    if size == "l":
        size = "m"
    effective_min_tier = max(cur_tier, router.risk_min_tier(row_get(item, "title", "")))
    if effective_min_tier >= 2 and size == "s":
        size = "m"
    # D26: route within the project's declared accounts: fallback order,
    # equal round-robin, or an explicit cross-account priority.
    platform, reasons = router.pick_for_project(
        cfg, led, pol, "fix", item["pin"], busy, size=size,
        burst_lines=ctx.burst_lines, min_tier=effective_min_tier)
    if not platform:
        ctx.say(f"{project}#{n}: PR #{pr} — CI red, no platform for a fix run — "
                f"{'; '.join(reasons)}")
        _fix_wait(ctx, project, item, key, "; ".join(reasons) or "no eligible route")
        return
    led.upsert_item(project, n, branch=head)
    conductor = led.lease(project, n)
    handoff_from = ((CONDUCTOR, conductor["epoch"])
                    if conductor and (conductor["holder"] == CONDUCTOR
                                      or conductor["holder"].endswith("/conductor")) else None)
    if start(ctx, project, {**item, "branch": head}, "fix", platform,
             handoff_from=handoff_from, size=size):
        led.upsert_item(project, n, attempts=attempts)


def _open_pr(ctx, project, item, gh, pol, unconfirmed=False):
    led, n = ctx.led, item["number"]
    ref = item["branch"]
    if not ref:
        retry_or_fail(ctx, project, n, led.item(project, n), None, "nothing to ship")
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


def pr_merged(view):
    """True only with proof the PR merged: a closed-unmerged PR also leaves
    'not OPEN' behind, and must not become UAT, release-note or scorecard
    evidence of a shipped change."""
    return view.get("state") == "MERGED" or bool(view.get("mergedAt"))


def record_uat_if_needed(ctx, project, n, pr, item, view):
    """UAT queue (D10): a merged PR whose body carries a 'Needs a human to
    check' list lands in Ready to test until you pass or fail it. Used both
    by the conductor's own merge (_shipped, below) and by sync.py's fallback
    for issues closed outside the conductor — by hand, per CLAUDE.md's merge
    protocol, or a merge sync notices before ship.py's own watch does
    (mahler#285). Bookkeeping — a failure here never stops the ship."""
    needs = needs_human_of(view.get("body"))
    if not needs or not pr_merged(view):
        return needs
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
    check' list, ping, and mark the item done."""
    if view.get("state") != "MERGED":
        raise ValueError(f"PR #{pr} has not been confirmed merged")
    led = ctx.led
    how = "squash-merged" if merged else view["state"].lower()
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
    led.set_state(project, n, "done", f"shipped via PR #{pr}")
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
