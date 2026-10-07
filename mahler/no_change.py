"""Independent review of clean zero-ahead DONE claims, without a PR."""

import json

from . import config, router, runner
from .ledger import CONDUCTOR, row_get


def key(project, number):
    return f"no_change:{project}#{number}"


def read(led, project, number):
    return json.loads(led.get_kv(key(project, number)) or "{}")


def save(led, project, number, record):
    led.set_kv(key(project, number), json.dumps(record))


def begin(e):
    old = read(e.led, e.project, e.number)
    if old.get("claim_run") != e.run["id"]:
        save(e.led, e.project, e.number, {
            "mode": "claim", "claim_run": e.run["id"], "base_sha": e.no_work_base,
            "evidence": e.rest or "", "build_evidence": e.log.get("last_text") or "",
            "human_checks": [], "phase": "waiting", "inconclusive": 0})
    e.set_state("verifying", "no-change DONE awaiting independent base review",
                summary=e.rest or e.item["title"])


def parse_payload(text):
    payload = json.loads(text or "")
    if not isinstance(payload, dict) or payload.get("mode") != "claim":
        raise ValueError("expected claim-mode JSON")
    for field in ("evidence", "human_checks"):
        values = payload.get(field)
        if (not isinstance(values, list)
                or any(not isinstance(v, str) or not v.strip() for v in values)):
            raise ValueError(f"invalid {field}")
    if not payload["evidence"]:
        raise ValueError("missing evidence")
    return payload


def finish(e):
    """Called inside finalization's epoch-fenced transaction; no external writes."""
    record = read(e.led, e.project, e.number)
    if (record.get("run_id") != e.run["id"]
            or record.get("epoch") != e.run["epoch"]
            or record.get("phase") != "reviewing"):
        return
    try:
        if e.reason or e.verb not in ("REVIEW-PASS", "REVIEW-FAIL"):
            raise ValueError("no completed verdict")
        payload = parse_payload(e.rest)
    except (ValueError, TypeError):
        record["inconclusive"] += 1
        record["phase"] = "waiting"
    else:
        record.update(phase="verdict", verdict="pass" if e.verb == "REVIEW-PASS" else "fail",
                      review_evidence=payload["evidence"], human_checks=payload["human_checks"])
    save(e.led, e.project, e.number, record)
    e.set_state("verifying", "no-change claim review awaiting conductor")


def fetched_base(ctx, project):
    pol = ctx.policy(project)
    base = pol.get("base", "main")
    ref = f"refs/remotes/origin/{base}"
    runner.git(pol["path"], "fetch", "--quiet", "origin", f"+refs/heads/{base}:{ref}",
               env=config.run_env(ctx.cfg, config.gh_account_of(pol)))
    return runner.git(pol["path"], "rev-parse", "--verify", f"{ref}^{{commit}}")


def _owned(ctx, project, number, epoch):
    lease = ctx.led.lease(project, number)
    return bool(lease and lease["holder"] == CONDUCTOR and lease["epoch"] == epoch)


def advance(ctx, project, item):
    """Return true for claim mode so it can never fall through to PR shipping."""
    from . import ship
    from .finalize import retry_or_fail

    led, n = ctx.led, item["number"]
    record = read(led, project, n)
    if not record or item["pr"]:
        return False
    if ctx.dry_run:
        return True
    if record["phase"] in ("rejected", "manual", "done", "exhausted"):
        return True
    # The same rules as PR review: an active reviewer owns its lease until finalize.
    if any(r["project"] == project and r["number"] == n for r in led.active_runs()):
        return True
    if not ship._ship_lease(ctx, project, item):
        return True
    epoch = led.lease(project, n)["epoch"]
    sha = fetched_base(ctx, project)
    if not _owned(ctx, project, n, epoch):
        return True
    if sha != record["base_sha"]:
        record.update(base_sha=sha, phase="waiting", run_id=None, epoch=None)
        record.pop("verdict", None)
        save(led, project, n, record)
    if record["phase"] == "reviewing":
        # A launch interrupted before its receipt or an expired run is inconclusive.
        record["inconclusive"] += 1
        record["phase"] = "waiting"
        save(led, project, n, record)
    if record["phase"] == "verdict":
        gh = ctx.gh(project)
        marker = f"<!-- mahler:no-change claim={record['claim_run']} review={record['run_id']} -->"
        if not any(marker in c["body"] for c in gh.issue_comments(n)):
            if not _owned(ctx, project, n, epoch):
                return True
            gh.comment(n, "<!-- mahler:agent -->\n" + marker
                       + f"\n**Independent no-change review: {record['verdict']}**\n\n"
                       + f"Configured base: `{sha}`.\n\n"
                       + "\n".join(f"- {e}" for e in record["review_evidence"])
                       + "\n\nRemaining manual checks:\n"
                       + ("\n".join(f"- {c}" for c in record["human_checks"]) or "None."))
        # Recheck after slow GitHub calls; never accept an observed base drift.
        if fetched_base(ctx, project) != sha or not _owned(ctx, project, n, epoch):
            return True
        if record["verdict"] == "fail":
            with led._tx():
                if not _owned(ctx, project, n, epoch):
                    return True
                record["phase"] = "rejected"
                save(led, project, n, record)
                retry_or_fail(ctx, project, n, led.item(project, n), None,
                              "no-change claim rejected: " + " | ".join(record["review_evidence"]),
                              run=led.run(record["run_id"]))
            led.release(project, n, holder=CONDUCTOR)
        elif record["human_checks"]:
            record["phase"] = "manual"
            with led._tx():
                save(led, project, n, record)
                led.set_state(project, n, "parked", "verified no-change claim awaiting manual checks")
            led.release(project, n, holder=CONDUCTOR)
        else:
            if gh.issue_state(n) != "CLOSED":
                if not _owned(ctx, project, n, epoch):
                    return True
                gh.close_issue(n)
            if gh.issue_state(n) == "CLOSED" and _owned(ctx, project, n, epoch):
                record["phase"] = "done"
                with led._tx():
                    save(led, project, n, record)
                    led.set_state(project, n, "done", "independently verified on configured base")
                led.release(project, n, holder=CONDUCTOR)
        return True
    if record["inconclusive"] >= ctx.policy(project)["max_attempts"]:
        record["phase"] = "exhausted"
        with led._tx():
            save(led, project, n, record)
            led.set_state(project, n, "failed", "no-change review exhausted inconclusive attempts")
        led.release(project, n, holder=CONDUCTOR)
        ctx.ping(f"Claim review stuck — {project} #{n}",
                 "Independent reviews produced no usable evidence; automatic reviews stopped.",
                 project, n)
        return True
    active = led.active_runs()
    if len(active) >= ctx.cfg["concurrency"]["total"]:
        return True
    pin, size, exclude = ship._review_route(ctx, project, item, sha)
    busy = ship.busy_platforms(ctx.cfg, active)
    pol = ctx.policy(project)
    capacity = router.capacity_recovery(ctx.cfg, led, pol, "review", pin, busy,
                                       size=size, burst_lines=ctx.burst_lines, exclude=exclude)
    if not capacity[0] or capacity[2]:
        return True
    platform, _ = router.pick_for_project(
        ctx.cfg, led, pol, "review", pin, busy, size=size, exclude=exclude,
        scorecard_rows=getattr(ctx, "scorecard_rows", None), burst_lines=ctx.burst_lines)
    if platform:
        ship.start(ctx, project, {**item, "claim_base_sha": sha}, "review", platform,
                   handoff_from=(CONDUCTOR, epoch), size=size)
    return True
