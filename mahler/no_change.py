"""Independent review of clean zero-ahead DONE claims, without a PR."""

import json

from . import config, practices_audit, redact, router, runner, waits
from .ledger import CONDUCTOR


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
            "evidence": e.rest or "", "build_evidence": e.log.get("final") or e.log.get("last_text") or "",
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
    pending = payload.get("pending_practices_checks", [])
    if (not isinstance(pending, list) or any(
            not isinstance(c, str) or c not in config.PRACTICES_AUDIT_CHECKS for c in pending)):
        raise ValueError("invalid pending_practices_checks")
    question = payload.get("owner_question")
    if question is not None and (not isinstance(question, str) or not question.strip()):
        raise ValueError("invalid owner_question")
    return payload


def _inconclusive(led, project, number, record, run):
    from .finalize import REVIEW_INCONCLUSIVE_CAP

    key = f"reviewinconclusive:{project}#{number}:{record['base_sha']}"
    seen = json.loads(led.get_kv(key) or "[]")
    if run and run["id"] not in [r.get("run_id") for r in seen]:
        seen.append({"run_id": run["id"], "platform": run["platform"]})
        led.set_kv(key, json.dumps(seen))
        record["inconclusive"] += 1
    elif not run:
        record["inconclusive"] += 1
    if record["inconclusive"] >= REVIEW_INCONCLUSIVE_CAP:
        led.set_kv(f"reviewescalate:{project}#{number}:{record['base_sha']}", "1")
    record["phase"] = "waiting"


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
        _inconclusive(e.led, e.project, e.number, record, e.run)
    else:
        record.update(phase="verdict", verdict="pass" if e.verb == "REVIEW-PASS" else "fail",
                      review_evidence=payload["evidence"], human_checks=payload["human_checks"],
                      pending_practices_checks=payload.get("pending_practices_checks", []),
                      owner_question=payload.get("owner_question"))
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
    from .finalize import retry_or_fail, REVIEW_INCONCLUSIVE_CAP

    led, n = ctx.led, item["number"]
    record = read(led, project, n)
    if not record or item["pr"]:
        return False
    if ctx.dry_run:
        return True
    if record["phase"] in ("rejected", "manual", "owner", "done", "exhausted"):
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
        record.update(base_sha=sha, phase="waiting", run_id=None, epoch=None, inconclusive=0)
        record.pop("verdict", None)
        save(led, project, n, record)
    if record["phase"] == "reviewing":
        # A launch interrupted before its receipt or an expired run is inconclusive.
        _inconclusive(led, project, n, record, led.run(record["run_id"]))
        save(led, project, n, record)
    if record["phase"] == "verdict" and record.get("owner_question"):
        from .finalize import OWNER_CATEGORIES, parse_needs_you_category
        category, text = parse_needs_you_category(record["owner_question"])
        if category in OWNER_CATEGORIES:
            record["owner_question"] = text
        else:       # not an owner decision (D52): evidence for the human check, not a park
            record["human_checks"] = record["human_checks"] + [text]
            record["owner_question"] = None
        save(led, project, n, record)
    if record["phase"] == "verdict":
        gh = ctx.gh(project)
        if record["verdict"] == "pass":
            try:
                body = gh.issue_body(n)
                checks = practices_audit.proposal_checks(project, body)
                pending = record.get("pending_practices_checks", [])
                if any(c not in checks for c in pending):
                    raise ValueError("deferred check lacks matching proposal marker")
                results = []
                for check in checks:
                    finding = practices_audit.verify_proposal(ctx, project, body, check, sha)
                    results.append(f"Practices {check}: {finding.state} at {sha}: {finding.reason}")
                    if finding.state == "gap":
                        record["verdict"] = "fail"
                    elif finding.state != "pass":
                        raise ValueError("; ".join(results))
                record["practices_evidence"] = results
            except Exception as exc:  # evidence collection is retryable, never approval
                if not _owned(ctx, project, n, epoch):
                    return True
                record.update(phase="waiting", inconclusive=record["inconclusive"] + 1,
                              practices_error=redact.redact(str(exc)))
                save(led, project, n, record)
                return True
            if not _owned(ctx, project, n, epoch):
                return True
            save(led, project, n, record)
        marker = f"<!-- mahler:no-change claim={record['claim_run']} review={record['run_id']} -->"
        if not any(marker in c["body"] for c in gh.issue_comments(n)):
            if not _owned(ctx, project, n, epoch):
                return True
            gh.comment(n, "<!-- mahler:agent -->\n" + marker
                       + f"\n**Independent no-change review: {record['verdict']}**\n\n"
                       + f"Configured base: `{sha}`.\n\n"
                       + "\n".join(f"- {e}" for e in record["review_evidence"] + record.get("practices_evidence", []))
                       + "\n\nNeeds a human to check:\n"
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
                              "no-change claim rejected: " + " | ".join(record["review_evidence"] + record.get("practices_evidence", [])),
                              run=led.run(record["run_id"]))
            led.release(project, n, holder=CONDUCTOR)
        elif record.get("owner_question"):
            record["phase"] = "owner"
            with led._tx():
                if not _owned(ctx, project, n, epoch):
                    return True
                save(led, project, n, record)
                led.set_state(project, n, "needs_you", record["owner_question"],
                              question=record["owner_question"], options="[]")
            ctx.ping(f"Decision needed — {project} #{n}", record["owner_question"], project, n)
            led.release(project, n, holder=CONDUCTOR)
        else:
            if gh.issue_state(n) != "CLOSED":
                if not _owned(ctx, project, n, epoch):
                    return True
                gh.close_issue(n)
            if gh.issue_state(n) == "CLOSED" and _owned(ctx, project, n, epoch):
                record["phase"] = "done"
                with led._tx():
                    if not _owned(ctx, project, n, epoch):
                        return True
                    save(led, project, n, record)
                    led.set_state(project, n, "done", "independently verified on configured base")
                led.release(project, n, holder=CONDUCTOR)
        return True
    if record["inconclusive"] > REVIEW_INCONCLUSIVE_CAP:
        record["phase"] = "exhausted"
        with led._tx():
            save(led, project, n, record)
            reason = ("Independent no-change reviews exhausted inconclusive attempts; "
                      "retrying later with another reviewer")
            waits.hold(led, project, n, "review_inconclusive", reason, resume="verifying",
                       wake={"sha": record["base_sha"]})
        led.release(project, n, holder=CONDUCTOR)
        return True
    active = led.active_runs()
    if len(active) >= ctx.cfg["concurrency"]["total"]:
        return True
    pin, size, exclude = ship._review_route(ctx, project, item, sha)
    busy = ship.busy_platforms(ctx.cfg, active)
    pol = ctx.policy(project)
    min_tier = 2 if led.get_kv(f"reviewescalate:{project}#{n}:{sha}") else 0
    capacity = router.capacity_recovery(ctx.cfg, led, pol, "review", pin, busy,
                                       size=size, burst_lines=ctx.burst_lines, exclude=exclude, min_tier=min_tier)
    if not capacity[0] or capacity[2]:
        return True
    platform, _ = router.pick_for_project(
        ctx.cfg, led, pol, "review", pin, busy, size=size, exclude=exclude, min_tier=min_tier,
        scorecard_rows=getattr(ctx, "scorecard_rows", None), burst_lines=ctx.burst_lines)
    if platform:
        ship.start(ctx, project, {**item, "claim_base_sha": sha}, "review", platform,
                   handoff_from=(CONDUCTOR, epoch), size=size)
    return True


def recover_legacy(ctx, project):
    """Re-review only passed legacy manual claims; never interpret old prose."""
    if ctx.dry_run:
        return
    led = ctx.led
    for item in led.items(project, ["parked"]):
        n = item["number"]
        record = read(led, project, n)
        if item["pr"] or record.get("phase") != "manual" or record.get("verdict") != "pass":
            continue
        if any(r["project"] == project and r["number"] == n for r in led.active_runs()):
            continue
        lease = led.lease(project, n)
        if lease and lease["holder"] != CONDUCTOR:
            continue
        lease, _ = led.claim(project, n, CONDUCTOR, "auto",
                             ctx.policy(project)["auto_lease_minutes"], capacity=False)
        if not lease:
            continue
        sha = fetched_base(ctx, project)
        with led._tx():
            if not _owned(ctx, project, n, lease["epoch"]):
                continue
            record["legacy_evidence"] = {k: record.get(k) for k in
                                         ("review_evidence", "human_checks", "base_sha")}
            record.update(phase="waiting", base_sha=sha, run_id=None, epoch=None,
                          inconclusive=0, human_checks=[], pending_practices_checks=[],
                          owner_question=None)
            record.pop("verdict", None)
            save(led, project, n, record)
            led.set_state(project, n, "verifying", "legacy no-change claim requires fresh review")
