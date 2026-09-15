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


def _rebuild_on_base(ctx, project, item, pr, base):
    """The base moved under the PR: drop it and build again on current base
    (D19). No attempt is counted — the work was fine, the ground moved."""
    led, n = ctx.led, item["number"]
    led.upsert_item(project, n, pr=None)
    led.set_state(project, n, "ready", f"PR #{pr} conflicts with {base} — rebuilding on it")
    led.release(project, n, holder=CONDUCTOR)
    ctx.ping(f"Rebuilding — {project} #{n}",
             f"PR #{pr} no longer merges into {base}; the next build starts on current {base}",
             project, n, priority="low")


def _watch_pr(ctx, project, item, pr):
    """One step of the open PR's state machine, one step per tick: gone,
    conflicting, CI still running, CI red, or green and merged."""
    led, n = ctx.led, item["number"]
    gh = ctx.gh(project)
    view = gh.pr_view(pr)
    if view["state"] != "OPEN":                 # merged or closed outside Mahler
        _shipped(ctx, project, n, pr, led.item(project, n), view, merged=False)
        return
    if view.get("mergeable") == "CONFLICTING":
        _rebuild_on_base(ctx, project, item, pr, ctx.policy(project).get("base", "main"))
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


def _ship_item(ctx, project, item):
    led, n = ctx.led, item["number"]
    if ctx.dry_run:
        ctx.say(f"{project}#{n}: would ship "
                f"{'PR #' + str(item['pr']) if item['pr'] else '(opening the PR)'}")
        return
    if not _ship_lease(ctx, project, item):
        return
    if item["pr"]:
        _watch_pr(ctx, project, item, item["pr"])
        return
    # no PR yet: open it — its CI is watched from the next tick
    unconfirmed = bool(led.get_kv(f"unconfirmed:{project}#{n}"))
    _open_pr(ctx, project, item, ctx.gh(project), ctx.policy(project),
             unconfirmed=unconfirmed)


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
    escalation as today (retry_or_fail's stuck branch)."""
    led, cfg, n = ctx.led, ctx.cfg, item["number"]
    pol = ctx.policy(project)
    head = view.get("headRefName") or item["branch"]
    attempts = item["attempts"] + 1
    cur_tier = row_get(item, "esc_tier", 0)
    cur_fails = row_get(item, "esc_fails", 0)
    new_fails = cur_fails + 1
    new_tier = cur_tier
    last = led.last_run(project, n)
    last_platform = last["platform"] if last else None
    run_tier = router.tier_of(cfg.get("platforms", {}).get(last_platform, {})) if last_platform else 1
    if new_fails >= 2:
        new_tier = max(cur_tier, run_tier) + 1
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
    busy = busy_platforms(cfg, active)
    size = next((l.split(":", 1)[1] for l in json.loads(row_get(item, "labels", "[]"))
                 if l.startswith("size:")), None)
    # For fix runs, treat size:l as size:m so a CI fix never needs Opus by size alone (DESIGN D21)
    if size == "l":
        size = "m"
    effective_min_tier = max(new_tier, router.risk_min_tier(row_get(item, "title", "")))
    if effective_min_tier >= 2 and size == "s":
        size = "m"
    # D26: route within the project's declared accounts, in order by default
    # or merged round-robin for account_mode = "equal".
    platform, reasons = router.pick_for_project(
        cfg, led, pol, "fix", item["pin"], busy, size=size,
        burst_lines=ctx.burst_lines, min_tier=effective_min_tier)
    if not platform:
        ctx.say(f"{project}#{n}: PR #{pr} — CI red, no platform for a fix run — "
                f"{'; '.join(reasons)}")
        return
    led.upsert_item(project, n, branch=head)
    conductor = led.lease(project, n)
    handoff_from = ((CONDUCTOR, conductor["epoch"])
                    if conductor and (conductor["holder"] == CONDUCTOR
                                      or conductor["holder"].endswith("/conductor")) else None)
    if start(ctx, project, {**item, "branch": head}, "fix", platform,
             handoff_from=handoff_from):
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
    # Platform-audit pass (mahler#206) is not one of the eight D20 passes and
    # isn't per-project opt-in, so it isn't in `passes` above — it anchors on
    # its own configured project's throughput instead.
    audit_pol = config.platform_audit_policy(ctx.cfg)
    if audit_pol["enabled"] and project == audit_pol["project"]:
        led.increment_maintenance_merged(project, config.PLATFORM_AUDIT_PASS)
    led.release(project, n, holder=CONDUCTOR)
