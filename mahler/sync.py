"""GitHub is the backlog (DESIGN D2): this pass makes the ledger match it.

Issues, their labels, their comments and the commands in them come in;
Mahler's own state labels go back out. Closing a parent whose sub-issues are
all done is the same kind of work, so it lives here too.
"""

import json
import hashlib
import re
from datetime import timedelta

from . import ci_blockers, config, no_change, releases, waits
from .gh import (GHError, AGENT_MARK, AGENT_NOTE, LABEL_STATES, STATE_LABELS, depends_of,
                 dependency_ref, dependency_target, files_of, has_sections,
                 label_names, parse_command, part_of, pin_of, priority_of, trusted_actor,
                 completion_evidence)
from .ledger import QUIET_DAYS, iso, parse
from .scorecard import DEFECT_WINDOW, linked_bug
from .ship import (_shipped, mirror_shipped, pr_merged, record_uat_if_needed,
                   migrate_capacity_waits)
from .watchdog import request_stop


def missing_scope_counts(led, cfg, project=None):
    """Latest full-sync counts; None means this project has not been measured."""
    counts = {}
    for name in cfg.get("projects", {}):
        pol = config.project_policy(cfg, name)
        if (project and name != project) or pol.get("scope") != "label":
            continue
        if not pol.get("enabled", True):
            continue
        value = led.get_kv(f"missing_scope:{name}")
        counts[name] = int(value) if value is not None else None
    return counts


def _record_backlog(ctx, project, open_count):
    """Net-backlog snapshot (mahler#1104), throttled hourly by the ledger.

    `open_count` None (a 304 tick) reuses the last count: the collection is
    unchanged. Never raises — a snapshot failure must not break sync."""
    if ctx.dry_run:
        return
    try:
        if open_count is None:
            last = ctx.led.last_backlog(project)
            if last is None:
                return
            open_count = last["open_count"]
        ctx.led.record_backlog(project, open_count)
    except Exception as e:  # noqa: BLE001 — a metric must never break sync
        ctx.say(f"{project}: backlog snapshot failed: {e}")


def sync(ctx, project):
    led, gh = ctx.led, ctx.gh(project)
    pol = ctx.policy(project)
    migrate_capacity_waits(ctx, project)
    _bootstrap_release_baseline(ctx, project, gh)
    # Conditional poll (mahler#90): a 304 means the open-issue collection is
    # byte-identical to the last full sync — no new issues, no edits, no
    # comments — so both the fetch and the closed-issue checks below can be
    # skipped. The etag is stored only after a clean sync: if anything fails
    # mid-tick, the next tick probes with the old etag and re-fetches.
    etag_key = f"etag:{project}"
    poll_changed, poll_etag = gh.issues_changed(led.get_kv(etag_key))
    # Reparse once after dependency rules change, even on an unchanged collection.
    # Version 4 adds GitHub's native blocked-by relationships. Version 3 removed
    # self/ancestor deadlocks as well as preserving qualifiers.
    depends_key = f"depends_format:{project}"
    repair_key = f"unmerged_reconciled:{project}"
    # Version 1 reclassifies issue authors against the configured App identity.
    # Force one full poll after upgrade even when GitHub's collection ETag is
    # unchanged, or pre-existing App holds would never reach the release path.
    intake_trust_key = f"intake_trust_format:{project}"
    intake_trust_current = led.get_kv(intake_trust_key) == "1"
    intake_trust_repair = (not intake_trust_current and any(
        led.get_kv(f"intake_hold:{project}#{item['number']}")
        for item in led.items(project)))
    if (not poll_changed and led.get_kv(depends_key) == "4"
            and led.get_kv(repair_key) == "1"
            and not intake_trust_repair):
        if not ctx.dry_run and not intake_trust_current:
            led.set_kv(intake_trust_key, "1")
        _record_backlog(ctx, project, None)
        reconcile_shipped(ctx, project)
        _resume_dependency_waits(ctx, project)
        ctx.say(f"{project}: GitHub unchanged (304) — sync skipped")
        return
    issues = gh.open_issues()

    scope_label = pol.get("scope_label", "mahler")
    repo_owner = pol["repo"].split("/", 1)[0]
    trusted_logins = (repo_owner,)
    configured_logins_resolved = False
    trust_resolution_failed = False
    admitted_issues = []
    untrusted_issues = []
    for iss in issues:
        labels = label_names(iss)
        login = iss.get("authorLogin")
        opted_in = scope_label not in LABEL_STATES and scope_label in labels
        admitted = (opted_in
                    or trusted_actor(login, iss.get("authorAssociation"), trusted_logins))
        if (not admitted and isinstance(login, str) and login.strip()):
            if not configured_logins_resolved:
                configured_logins_resolved = True
                resolved = []
                for identity in (gh.authenticated_login, gh.app_login):
                    try:
                        configured_login = identity()
                    except GHError:
                        trust_resolution_failed = True
                        continue
                    if isinstance(configured_login, str) and configured_login.strip():
                        resolved.append(configured_login)
                trusted_logins += tuple(resolved)
            admitted = trusted_actor(login, iss.get("authorAssociation"),
                                     trusted_logins)
        (admitted_issues if admitted else untrusted_issues).append(iss)

    open_nums = {iss["number"] for iss in issues}
    active_runs = led.active_runs(project)
    for iss in untrusted_issues:
        n = iss["number"]
        labels = label_names(iss)
        item = led.item(project, n)
        if item is None:
            led.upsert_item(project, n, created_at=iss["createdAt"])
        led.upsert_item(
            project, n, title=iss["title"], issue_body=iss.get("body") or "",
            labels=json.dumps(labels), priority=priority_of(labels), pin=pin_of(labels),
            parent=None, depends="[]", files="[]")
        for run in active_runs:
            if run["number"] == n:
                request_stop(ctx, run, "untrusted issue author")
        login = iss.get("authorLogin")
        association = iss.get("authorAssociation") or "unknown association"
        actor = f"@{login}" if isinstance(login, str) and login else "unknown author"
        opt_in = (f"configure a non-Mahler scope label; `{scope_label}` is a state label"
                  if scope_label in LABEL_STATES
                  else f"add the `{scope_label}` label to opt in")
        reason = f"held: untrusted issue author {actor} ({association}); {opt_in}"
        hold_key = f"intake_hold:{project}#{n}"
        if not led.get_kv(hold_key):
            led.set_state(project, n, "parked", reason, sorted_at=None)
            led.set_kv(hold_key, "untrusted")
        ctx.say(f"{project}#{n}: {reason}")

    if pol.get("scope") == "label":
        cutoff = led.now() - timedelta(hours=24)
        count = sum(1 for iss in issues
                    if scope_label not in label_names(iss)
                    and parse(iss["createdAt"]) < cutoff)
        if not ctx.dry_run:
            led.set_kv(f"missing_scope:{project}", str(count))
        in_scope_nums = {
            iss["number"] for iss in admitted_issues
            if scope_label in label_names(iss)
            or any(l in LABEL_STATES for l in label_names(iss))
            or led.item(project, iss["number"]) is not None
        }
        # If child issues reference an in-scope parent via "Part of #N", inherit scope
        changed = True
        while changed:
            changed = False
            for iss in admitted_issues:
                n = iss["number"]
                if n not in in_scope_nums:
                    parent_n = part_of(iss.get("body"))
                    if parent_n and (parent_n in in_scope_nums or led.item(project, parent_n) is not None):
                        in_scope_nums.add(n)
                        changed = True
        hold = ci_blockers.active(led, project, clear=False)
        if hold and hold.get("number"):
            in_scope_nums.add(hold["number"])
    else:
        in_scope_nums = {iss["number"] for iss in admitted_issues}
    _record_backlog(ctx, project,
                    len(issues) if in_scope_nums is None else len(in_scope_nums))

    # Prefer this poll over cached ancestry, regardless of GitHub issue ordering.
    parents = {iss["number"]: part_of(iss.get("body")) for iss in admitted_issues}
    for iss in admitted_issues:
        n = iss["number"]
        labels = label_names(iss)
        if in_scope_nums is not None and n not in in_scope_nums:
            continue                      # not (yet) handed to Mahler
        if pol.get("scope") == "label" and pol["scope_label"] not in labels:
            labels.append(pol["scope_label"])
            if not ctx.dry_run:
                try:
                    gh.add_label(n, pol["scope_label"])
                except GHError:
                    pass
        open_nums.add(n)
        ctx._labels[(project, n)] = labels
        parent = parents[n]
        deps = depends_of(iss.get("body"))
        for blocker in ci_blockers.recorded(led, project, n):
            if blocker not in deps:
                deps.append(blocker)
        for blocker in gh.blocked_by_of(n):
            if blocker not in deps:
                deps.append(blocker)
        deps = _satisfiable_depends(ctx, project, n, parent, deps, parents)
        fields = dict(title=iss["title"], issue_body=iss.get("body") or "",
                      labels=json.dumps(labels), priority=priority_of(labels),
                      depends=json.dumps(deps), pin=pin_of(labels),
                      parent=parent,
                      files=json.dumps(files_of(iss.get("body"))))
        item = led.item(project, n)
        if item is None:
            state = _state_from_labels(labels)
            planned = state is None and planned_child(iss, labels, led, project)
            if state is None:
                state = "ready" if planned else "inbox"
            extra = {"sorted_at": iso(led.now())} if state == "ready" else {}
            led.upsert_item(project, n, created_at=iss["createdAt"], **fields, **extra)
            if planned:
                _born_ready(led, project, n, iss)
            else:
                led.set_state(project, n, state, "new issue")
            ctx.say(f"{project}#{n}: new — {iss['title']}")
            item = led.item(project, n)
        else:
            led.upsert_item(project, n, **fields)
            hold_key = f"intake_hold:{project}#{n}"
            if led.get_kv(hold_key):
                state = _state_from_labels(labels)
                planned = state is None and planned_child(iss, labels, led, project)
                if state is None:
                    state = "ready" if planned else "inbox"
                led.set_state(
                    project, n, state, "trusted issue intake resumed",
                    sorted_at=iso(led.now()) if state == "ready" else None)
                led.set_kv(hold_key, "")
                item = led.item(project, n)
            if (item["state"] == "inbox" and led.lease(project, n) is None
                    and planned_child(iss, labels, led, project)):
                _born_ready(led, project, n, iss)
                item = led.item(project, n)
            try:
                _reconcile_open_completion(ctx, project, item, iss)
            except (GHError, ValueError):
                led.set_kv(etag_key, "")
                raise
            item = led.item(project, n)
            _note_source_reopen(ctx, project, item)
            _adopt_label_edits(ctx, project, item, labels)
        _process_comments(ctx, project, led.item(project, n), iss.get("comments") or [])

    for item in led.items(project):
        if item["number"] in open_nums or item["state"] in ("done", "shipped"):
            continue
        try:
            state = gh.issue_state(item["number"])
        except GHError:
            continue
        if state == "CLOSED":
            running = [r for r in led.active_runs(project) if r["number"] == item["number"]]
            # A run whose own merge closed the issue is usually still writing its
            # summary: give it the normal grace period; finalize marks it done.
            for run in running:
                if not run["yield_at"]:
                    request_stop(ctx, run, "closed")
            if not running:
                # The issue closed without ship.py's own watch catching the
                # merge first — a by-hand merge (CLAUDE.md's protocol) or a
                # same-tick race where sync() (which runs first) sees the
                # closed issue before ship() gets to it. Either way, the
                # merged PR's 'Needs a human to check' list must still reach
                # the UAT queue, or it's lost for good (mahler#285).
                if item["pr"]:
                    try:
                        view = gh.pr_view(item["pr"])
                    except (GHError, ValueError):
                        led.set_kv(etag_key, "")
                        poll_etag = None
                        continue  # Retry rather than lose a possible shipment.
                    else:
                        if pr_merged(view):
                            _shipped(ctx, project, item["number"], item["pr"], item, view, merged=False)
                            led.release(project, item["number"])
                            continue
                led.release(project, item["number"])
                led.set_state(project, item["number"], "done", "closed on GitHub")

    # Ingest this poll's linked bugs and reopen signals before evaluating the
    # quiet window. A failed fetch must never complete from stale evidence.
    reconcile_shipped(ctx, project)
    _resume_dependency_waits(ctx, project)
    if poll_etag:
        led.set_kv(etag_key, poll_etag)
    led.set_kv(depends_key, "4")
    if not ctx.dry_run:
        led.set_kv(repair_key, "1")
        if not trust_resolution_failed:
            led.set_kv(intake_trust_key, "1")


def _reconcile_open_completion(ctx, project, item, issue):
    """Repair legacy closed-unmerged completions, using positive PR evidence."""
    if ctx.dry_run or item["state"] not in ("done", "shipped"):
        return
    led, gh, n = ctx.led, ctx.gh(project), item["number"]
    pr = item["pr"]
    if not pr:
        key = f"completion_missing_pr:{project}:{n}"
        if led.get_kv(key) != "1":
            ctx.say(f"{project}#{n}: open completed item has no recorded PR; leaving unchanged")
            led.event("completion_missing_pr", project, n, {"state": item["state"]})
            led.set_kv(key, "1")
        return
    view = gh.pr_view(pr)
    if pr_merged(view) or view.get("state") != "CLOSED":
        return
    # A replacement may have shipped the work even though the stored PR did not.
    if any(pr_merged(other) for other in gh.closing_prs(n)):
        return
    reason = f"not shipped: PR #{pr} closed without merging"
    marker = f"<!-- mahler:unmerged-completion:{pr} -->"
    if not any(marker in (c.get("body") or "") for c in issue.get("comments") or []):
        gh.comment(n, f"{AGENT_MARK}\n{marker}\n{reason}. Returning to ready.")
    # Reset the source as well: rebuilding must not reuse the abandoned PR branch.
    led.upsert_item(project, n, pr=None, branch=None, mirror=None)
    resume_item(led, project, n, why=reason)
    led.event("completion_unmerged", project, n, {"pr": pr, "previous_state": item["state"]})
    ctx.say(f"{project}#{n}: {reason} — ready")


def _resume_dependency_waits(ctx, project):
    """A completed Mahler dependency releases a parked technical blocker."""
    if ctx.dry_run:
        return
    enabled = config.enabled_projects(ctx.cfg)
    if not any(p["name"] == "mahler" for p in enabled):
        return
    for item in ctx.led.items(project, ["needs_you"]):
        for dep in json.loads(item["depends"] or "[]"):
            target = dependency_target(dep, project, enabled)
            if not target or target[0] != "mahler":
                continue
            blocker = ctx.led.item(*target)
            if blocker is None or blocker["state"] not in ("shipped", "done"):
                continue
            wait_started = parse(item["state_changed_at"])
            completed_at = _completion_started_at(ctx.led, *target, blocker)
            # The dependency must have completed during this particular wait.
            # A later run can ask an unrelated owner question while retaining
            # the issue body's dependency, which must not answer that question.
            if not wait_started or not completed_at or completed_at <= wait_started:
                continue
            reason = f"dependency {dependency_ref(dep, project)} {blocker['state']}"
            resume_item(ctx.led, project, item["number"], why=reason)
            ctx.say(f"{project}#{item['number']}: resuming — {reason}")
            break


def _completion_started_at(led, project, number, blocker):
    """Start of the blocker's current contiguous shipped/done interval."""
    completed_at = parse(blocker["state_changed_at"])
    current = blocker["state"]
    events = led.q("SELECT at, detail FROM events WHERE kind='state' "
                   "AND project=? AND number=? ORDER BY id DESC", (project, number))
    for event in events:
        before, arrow, rest = (event["detail"] or "").partition(" -> ")
        after = rest.split(" (", 1)[0]
        if not arrow or after != current:
            return None  # Ambiguous history must not answer an owner question.
        completed_at = parse(event["at"]) or completed_at
        if before not in ("shipped", "done"):
            return completed_at
        current = before
    return completed_at


QUIET_BATCH = 25   # legacy rows migrate in bounded ticks


def _note_source_reopen(ctx, project, item):
    """An open shipment is reopen evidence only after confirmed closure."""
    led = ctx.led
    n = item["number"]
    closed = (item["mirror"] == "mahler:shipped"
              or led.get_kv(f"shipped_closed:{project}:{n}") == "1")
    shipped = item["state"] == "shipped"
    early = (item["state"] == "done" and led.q1(
        "SELECT 1 FROM completion_evidence WHERE project=? AND number=? AND kind='smoke'",
        (project, n)))
    key = f"source_reopened:{project}:{n}"
    if closed and (shipped or early) and led.get_kv(key) != "1":
        row = led.uat(project, n)
        if row is None:
            return
        # Fetch before advancing the cursor: failures must retry and must not
        # allow quiet completion from an incomplete poll.
        reopens = ctx.gh(project).issue_reopens(n)
        current = [at for at in reopens if parse(at) >= parse(row["shipped_at"])]
        if not current:
            raise GHError("open shipment has no confirmed reopen event yet")
        for at in current:
            led.event("source_reopened", project, n, {}, at=at)
        led.set_kv(key, "1")


def adverse_evidence(led, row):
    """Why this shipment cannot complete quietly, or None. Looks only at
    evidence created inside [merge, merge + 14 days] (D33's window)."""
    project, n = row["project"], row["number"]
    merged = parse(row["shipped_at"])
    end = merged + DEFECT_WINDOW
    inside = lambda at: bool(at) and merged <= parse(at) <= end   # noqa: E731
    for e in led.q("SELECT at, kind FROM events WHERE project=? AND number=? "
                   "AND kind IN ('revert_requested','source_reopened')", (project, n)):
        if inside(e["at"]):
            return "revert" if e["kind"] == "revert_requested" else "source reopened"
    for a in led.q("SELECT done_at, created_at FROM console_actions "
                   "WHERE kind='revert' AND status='done' AND project=? AND number=?",
                   (project, n)):
        if inside(a["done_at"] or a["created_at"]):
            return "revert"
    if row["verdict"] == "fail" and inside(row["verdict_at"]):
        return "UAT fail"
    item = led.item(project, n)
    refs = {n, row["pr"], item["pr"] if item else None}
    bugs = []
    for b in led.items(project):
        if "type:bug" in json.loads(b["labels"] or "[]"):
            bugs.append(dict(b))
    filed = led.item(project, row["bug"]) if row["bug"] else None
    if linked_bug(bugs, dict(filed) if filed else None, row["bug"], refs, merged):
        return "linked bug"
    return None


def reconcile_shipped(ctx, project):
    """Closed shipments need their own poll, independent of the open-list ETag."""
    if ctx.dry_run:
        return
    led = ctx.led
    quiet_budget = QUIET_BATCH
    # Persist attempts, including failures, so a bad item cannot monopolize the
    # batch. Quiet-window checks have a separate bounded budget and bypass the
    # ordinary six-hour cooldown once when the boundary is crossed.
    candidates, due = [], []
    for item in led.items(project, ["shipped"]):
        row = led.uat(project, item["number"])
        key = f"shipped_poll:{project}:{item['number']}"
        last = parse(led.get_kv(key))
        merged = parse(row["shipped_at"]) if row else None
        boundary = merged + timedelta(days=QUIET_DAYS) if merged else None
        entry = (last or led.now() - timedelta(days=36500), item, key)
        if boundary and led.now() >= boundary and (last is None or last < boundary):
            due.append(entry)
        elif last is None or (merged and last < merged) or led.now() >= last + timedelta(hours=6):
            candidates.append(entry)
    selected = (sorted(due, key=lambda e: (e[0], e[1]["number"]))[:QUIET_BATCH]
                + sorted(candidates, key=lambda e: (e[0], e[1]["number"]))[:5])
    for _, item, poll_key in selected:
        led.set_kv(poll_key, iso(led.now()))
        n = item["number"]
        try:
            row = led.uat(project, n)
            if row is None and item["pr"]:
                record_uat_if_needed(ctx, project, n, item["pr"], item,
                                     ctx.gh(project).pr_view(item["pr"]))
                row = led.uat(project, n)
            if row is None:
                continue
            found_evidence = False
            for comment in ctx.gh(project).issue_comments(n):
                evidence = completion_evidence(comment, row["shipped_at"])
                if evidence:
                    led.accept_evidence(project, n, evidence)
                    found_evidence = True
                    break
            if found_evidence:
                continue
            if (quiet_budget > 0 and parse(row["shipped_at"])
                    and led.now() >= parse(row["shipped_at"]) + timedelta(days=QUIET_DAYS)
                    and adverse_evidence(led, row) is None):
                if led.complete_quiet(project, n, row["shipped_at"]):
                    quiet_budget -= 1
                continue
        except (GHError, ValueError) as exc:
            ctx.say(f"{project}#{n}: evidence lookup failed — {exc}")
    # An early automated completion is undone, once, by adverse evidence that
    # falls inside its observation window; quiet completions never are.
    for item in led.items(project, ["done"]):
        row = led.uat(project, item["number"])
        if row is None or not row["shipped_at"]:
            continue
        if not led.q1("SELECT 1 FROM completion_evidence WHERE project=? AND number=? "
                      "AND kind='smoke'", (project, item["number"])):
            continue
        reason = adverse_evidence(led, row)
        if reason:
            led.reopen_shipment(project, item["number"], reason)
    for key in [r["key"] for r in led.q("SELECT key FROM kv WHERE key LIKE ? AND value='pending'",
                                        (f"reopen_mirror:{project}:%",))]:
        mirror_reopen(ctx, project, int(key.rsplit(":", 1)[1]))
    # Retry the outward mirror after a transient failure, even though the
    # atomic local transition has already completed.
    for item in led.items(project, ["done"]):
        if item["mirror"] == "mahler:shipped":
            mirror_done(ctx, project, item["number"])


def mirror_reopen(ctx, project, number):
    try:
        gh = ctx.gh(project)
        if gh.issue_state(number) == "CLOSED":
            gh.reopen_issue(number)
        current = gh.issue_labels(number)
        gh.set_state_label(number, "ready", current)
        gh.set_priority_label(number, 1, current)
        ctx.led.upsert_item(project, number, mirror=STATE_LABELS["ready"])
        ctx.led.set_kv(f"reopen_mirror:{project}:{number}", "done")
        ctx._labels.pop((project, number), None)
    except GHError as exc:
        ctx.say(f"{project}#{number}: reopen label update failed — {exc}")


def mirror_done(ctx, project, number):
    try:
        gh = ctx.gh(project)
        gh.set_state_label(number, "done", gh.issue_labels(number))
        ctx.led.upsert_item(project, number, mirror=None)
        ctx._labels.pop((project, number), None)
    except GHError as exc:
        ctx.say(f"{project}#{number}: completion label update failed — {exc}")


def _bootstrap_release_baseline(ctx, project, gh):
    """Adopt an existing GitHub release before Mahler proposes a first version.

    Imported history is a baseline only: passing an explicit empty item list is
    essential, because the project's current rolling draft must remain unsealed.
    GitHub absence and unsupported legacy tags are harmless and remembered so a
    quiet project does not spend API quota every minute. Transient lookup
    failures are not marked checked and are retried on a later tick.
    """
    led = ctx.led
    checked_key = f"release_baseline_checked:{project}"
    if led.latest_release(project) is not None or led.get_kv(checked_key) == "1":
        return
    try:
        remote = gh.latest_release()
    except Exception as exc:  # noqa: BLE001 — GitHub failure must never stop issue sync
        ctx.say(f"{project}: release baseline lookup skipped — {exc}")
        return
    if not remote:
        led.set_kv(checked_key, "1")
        return
    try:
        version = releases.normalize_semver(remote.get("tagName", ""))
    except ValueError as exc:
        led.set_kv(checked_key, "1")
        ctx.say(f"{project}: release baseline lookup skipped — {exc}")
        return
    try:
        checkpoint_sha = remote.get("checkpointSha")
        if not checkpoint_sha:
            led.set_kv(checked_key, "1")
            return
        if led.latest_release(project) is not None:
            return
        led.create_release(
            project,
            version=version,
            checkpoint_sha=checkpoint_sha,
            notes=remote.get("body") or "",
            state="published",
            published_at=remote.get("publishedAt"),
            remote_url=remote.get("url"),
            item_numbers=[],
        )
        led.set_kv(checked_key, "1")
        ctx.say(f"{project}: adopted existing GitHub release v{version} as version baseline")
    except Exception as exc:  # noqa: BLE001 — release discovery must never stop issue sync
        ctx.say(f"{project}: release baseline lookup skipped — {exc}")


def _satisfiable_depends(ctx, project, number, parent, deps, parents):
    """Remove proven self/ancestor waits; keep unknown and external targets."""
    reasons = {number: "the item itself"}
    ancestor = parent
    for _ in range(100):
        if ancestor is None or ancestor in reasons:
            break
        reasons[ancestor] = ("this item's own parent" if ancestor == parent
                             else "an ancestor of this item")
        if ancestor in parents:
            ancestor = parents[ancestor]
        else:
            item = ctx.led.item(project, ancestor)
            ancestor = item["parent"] if item is not None else None

    enabled = config.enabled_projects(ctx.cfg)
    kept, dropped = [], {}
    for dep in deps:
        target = dependency_target(dep, project, enabled)
        if target and target[0] == project and target[1] in reasons:
            dropped[dependency_ref(dep, project)] = reasons[target[1]]
        else:
            kept.append(dep)

    # Report each correction once, until the body or ancestry changes it again.
    key = f"dropped_depends:{project}:{number}"
    previous = json.loads(ctx.led.get_kv(key) or "{}")
    for ref, reason in dropped.items():
        if previous.get(ref) != reason:
            ctx.say(f"{project}#{number}: dropped depends on {ref} — it is {reason}")
    if dropped != previous:
        ctx.led.set_kv(key, json.dumps(dropped))
    return kept


def _born_ready(led, project, number, iss):
    led.set_state(project, number, "ready",
                  f"born ready (planned under #{part_of(iss.get('body'))})",
                  sorted_at=iso(led.now()))


def planned_child(iss, labels, led, project):
    """Whether a sub-issue was fully planned by its parent."""
    parent_n = part_of(iss.get("body"))
    if parent_n is None:
        return False
    parent = led.item(project, parent_n)
    if parent is None or parent["state"] != "parent":
        return False
    sizes = {label.split(":", 1)[1] for label in labels
             if label.startswith("size:")}
    return (("s" in sizes or "m" in sizes)
            and has_sections(iss.get("body"), "## Plan", "## Done when"))


def _state_from_labels(labels):
    states = [LABEL_STATES[l] for l in labels if l in LABEL_STATES]
    return states[0] if len(states) == 1 else None


def _adopt_label_edits(ctx, project, item, labels):
    """You changed a mahler:* label by hand → that's an instruction.

    `mirror` is the label Mahler last wrote. A label equal to it is Mahler's own
    (possibly not yet updated); a different one was set by you."""
    wanted = _state_from_labels(labels)
    if not wanted or wanted == item["state"] or item["mirror"] is None:
        return
    if STATE_LABELS.get(wanted) == item["mirror"]:
        return
    if wanted in ("ready", "parked", "inbox"):
        _apply_instruction(ctx, project, item, "go" if wanted == "ready" else wanted, None)


def _process_comments(ctx, project, item, comments):
    led = ctx.led
    # Display-only context: escaped by the console, never admitted as instructions.
    relevant = [c for c in comments if (c.get("body") or "").strip()]
    if relevant:
        latest = max(relevant, key=lambda c: c["createdAt"])
        led.set_kv(f"console-comment:{project}#{item['number']}", latest['body'][:8000])
    seen = parse(item["last_comment_at"])
    newest = seen
    for c in sorted(comments, key=lambda c: c["createdAt"]):
        at = parse(c["createdAt"])
        if seen and at <= seen:
            continue
        newest = at if newest is None or at > newest else newest
        body = c.get("body") or ""
        if body.lstrip().startswith(AGENT_MARK):
            continue
        cmd = parse_command(body)
        if cmd:
            owner = ctx.policy(project)["repo"].split("/", 1)[0]
            is_owner = (c.get("authorAssociation") == "OWNER" or
                        (c.get("author") or {}).get("login", "").casefold() == owner.casefold())
            _apply_instruction(ctx, project, led.item(project, item["number"]), *cmd,
                               is_owner=is_owner)
        else:
            current = led.item(project, item["number"])
            if current["state"] == "failed":
                resume_item(led, project, item["number"])
                ctx.say(f"{project}#{item['number']}: answer received, resuming")
            elif (current["state"] == "needs_you" and current["pr"]
                  and body.strip().lower() == "merge with follow-ups"
                  and led.get_kv(f"fresh-rebuild:{project}#{item['number']}")
                  and (c.get("authorAssociation") == "OWNER" or
                       (c.get("author") or {}).get("login", "").casefold()
                       == ctx.policy(project)["repo"].split("/", 1)[0].casefold())):
                # The owner's pick on the non-convergent-review question (mahler#1262).
                led.set_kv(f"owner-followups:{project}#{item['number']}", str(current["pr"]))
                led.set_state(project, item["number"], "verifying",
                              "you chose merge with follow-ups")
                ctx.say(f"{project}#{item['number']}: merging PR #{current['pr']} with follow-ups")
            elif current["state"] == "needs_you":
                led.set_state(project, item["number"], "inbox", "you answered — re-sorting",
                              sorted_at=None)
                ctx.say(f"{project}#{item['number']}: answer received, re-sorting")
    if newest and newest != seen:
        led.upsert_item(project, item["number"], last_comment_at=iso(newest))


def resume_item(led, project, number, why="you said go"):
    """Apply the same reset as ``/mahler go`` to a waiting item."""
    waits.clear(led, project, number)
    led.set_state(project, number, "ready", why, attempts=0, setup_fails=0,
                  esc_tier=0, esc_fails=0,
                  sorted_at=iso(led.now() - timedelta(days=1)))


def _apply_instruction(ctx, project, item, verb, arg, *, is_owner=False):
    led, n = ctx.led, item["number"]
    if verb == "go":
        resume_item(led, project, n)
    elif verb == "park":
        led.set_state(project, n, "parked", "you parked it")
        waits.clear(led, project, n)
        for run in led.active_runs(project):
            if run["number"] == n:
                request_stop(ctx, run, "parked")
    elif verb == "inbox":
        led.set_state(project, n, "inbox", "back to inbox", sorted_at=None)
    elif verb == "platform" and arg:
        if arg in ("none", "auto"):
            _set_pin(ctx, project, n, None)
        elif arg in ctx.cfg["platforms"]:
            if ctx.cfg["platforms"][arg].get("approval") and not is_owner:
                ctx.say(f"{project}#{n}: only the owner can approve platform {arg!r}")
                return
            _set_pin(ctx, project, n, arg)
        else:
            ctx.say(f"{project}#{n}: unknown platform {arg!r}")
    ctx.say(f"{project}#{n}: instruction '{verb}{' ' + arg if arg else ''}'")


def _set_pin(ctx, project, n, platform):
    """`platform:*` labels on GitHub are the pin's store of record (mahler#20):
    sync() re-derives pin=pin_of(labels) on every tick, so a ledger-only pin
    lasted exactly one tick. The command edits the labels instead, then mirrors
    the change into the ledger so it takes effect in this tick's schedule."""
    current = ctx._labels.get((project, n)) or []
    if not ctx.dry_run:
        try:
            ctx.gh(project).set_pin_labels(n, platform, current)
        except GHError as e:
            ctx.say(f"{project}#{n}: platform label update failed — {e}")
        keep = f"platform:{platform}" if platform else None
        updated = [l for l in current if not l.startswith("platform:") or l == keep]
        if keep and keep not in updated:
            updated.append(keep)
        ctx._labels[(project, n)] = updated
    ctx.led.upsert_item(project, n, pin=platform)


def _parent_sections(body, *, numbered=False):
    """Ignore quoted originals and fenced examples; retain section boundaries."""
    section, fenced = "", False
    for index, line in enumerate(body.splitlines()):
        if line.lstrip().startswith(">"):
            continue
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        heading = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if heading:
            section = heading[1].strip(" *:").lower()
        else:
            yield (section, line.strip(), index) if numbered else (section, line.strip())


def _parent_children(body, repo):
    """Leading refs in Plan/Steps or named Children/Sub-issues list entries.

    Entries are `- #12: title`, `1. #12 title`, or their checkbox variants.
    Qualified refs/URLs must name this repository. Prose refs are not children.
    """
    children = set()
    active = False
    for section, line in _parent_sections(body):
        if section in {"children", "child issues", "sub-issues", "sub issues", "steps", "plan steps"}:
            active = True
        elif section == "plan":
            if line.lower().strip("*:") in {"steps", "children", "child issues", "sub-issues"}:
                active = True
            elif re.match(r"^[A-Za-z][^:]*:$", line):
                active = False
        else:
            active = False
        if not active:
            continue
        entry = re.match(r"^(?:[-*+]\s+(?:\d+[.)]\s+)?|\d+[.)]\s+)(?:\[[ xX]\]\s*)?(.*)$", line)
        if not entry:
            continue
        ref = re.match(r"(?:https://github.com/([^/]+/[^/]+)/issues/|([\w.-]+/[\w.-]+)#|#)(\d+)\b", entry[1])
        if ref:
            if (ref[1] or ref[2] or repo).lower() != repo.lower():
                raise GHError("unsupported cross-repository child reference")
            children.add(int(ref[3]))
    return children


def _parent_checks(body):
    """Return source positions, checkboxes and exact local child criteria."""
    checks = []
    raw = body.splitlines()
    for section, line, index in _parent_sections(body, numbered=True):
        if section not in {"done when", "done-when"} or not line:
            continue
        check = re.fullmatch(r"[-*+] \[([ xX])\]\s+(.+)", line)
        indented = raw[index][:1] in (" ", "\t")
        if checks and indented and not check and re.match(r"[-*+]\s|\d+[.)]\s", line):
            continue  # nested detail bullet under a checkbox item
        if not check:
            # A wrapped item: a plain continuation line joins the previous checkbox.
            if checks and not re.match(r"[-*+]\s|\d+[.)]\s", line):
                idx, prev, _ = checks[-1]
                text = f"{prev[2]} {line}"
                joined = re.fullmatch(r"[-*+] \[([ xX])\]\s+(.+)", f"- [{prev[1]}] {text}")
                ref = re.fullmatch(r"#(\d+) (?:closed|(?:is )?merged)", text)
                checks[-1] = (idx, joined, int(ref[1]) if ref else None)
                continue
            return []
        ref = re.fullmatch(r"#(\d+) (?:closed|(?:is )?merged)", check[2])
        checks.append((index, check, int(ref[1]) if ref else None))
    return checks


def _parent_checklist(body):
    checks = _parent_checks(body)
    if not checks:
        return ["Done-when checklist absent or unparseable"]
    return [f"Unchecked criterion: {check[2]}" for _, check, _ in checks if check[1] == " "]


VERIFY_START = "<!-- mahler:verify-blockers:start -->"
VERIFY_END = "<!-- mahler:verify-blockers:end -->"


def _verify_title(number):
    return f"Verify remaining completion criteria for #{number}"


def _verify_matches(inventory, number):
    """Verification items for one parent in this repository, lowest number first.

    Identity is the parent number: the marker when sort kept it, else the
    generated title plus the pass:verify label (sort strips the marker).
    """
    marker = re.compile(rf"<!-- mahler:parent-verify:{number}:[0-9a-f]+ -->")
    out = []
    for row in inventory:
        labels = {(l.get("name") if isinstance(l, dict) else l) for l in row.get("labels") or []}
        if marker.search(row.get("body") or "") or (
                "pass:verify" in labels and row.get("title") == _verify_title(number)):
            out.append(row)
    return sorted(out, key=lambda r: r["number"])


def _verify_reusable(row):
    """An unresolved owner question remains live even if GitHub is closed."""
    labels = {(label.get("name") if isinstance(label, dict) else label)
              for label in row.get("labels") or []}
    return (str(row.get("state")).upper() == "OPEN"
            or "mahler:needs-you" in labels)


def _verify_managed(number, blockers, fingerprint):
    return (f"{VERIFY_START}\n<!-- mahler:parent-verify:{number}:{fingerprint} -->\n"
            f"Current unresolved criteria for parent #{number}:\n" +
            "\n".join(f"- {b}" for b in blockers) + f"\n{VERIFY_END}")


def _verify_refresh(gh, row, number, blockers, fingerprint):
    """Update only the managed blocker section (and marker); no-op when current."""
    body = row.get("body") or ""
    block = _verify_managed(number, blockers, fingerprint)
    marker_re = re.compile(rf"<!-- mahler:parent-verify:{number}:[0-9a-f]+ -->")
    managed_re = re.compile(re.escape(VERIFY_START) + r".*?" + re.escape(VERIFY_END), re.S)
    if managed_re.search(body):
        new = managed_re.sub(lambda _: block, body, count=1)
    else:
        new = marker_re.sub("", body).rstrip() + "\n\n" + block + "\n"
    if new != body:
        gh.edit_issue_body(row["number"], new)


EVIDENCE_START = "PARENT-EVIDENCE:"
EVIDENCE_END = "END-PARENT-EVIDENCE"
# The parent-verify run's evidence block (mahler#1267). The run makes no
# GitHub writes (D18/D47); the conductor parses this block from the run's
# final message, posts it on the parent, ticks the checkboxes of met
# criteria and files one follow-up issue per unmet criterion.
EVIDENCE_CONTRACT = (
    "End your final message, before the STATUS line, with this evidence block, one line "
    "per remaining criterion (copy the criterion text exactly as the parent checklist "
    "words it):\n"
    f"{EVIDENCE_START}\n"
    "- met | <criterion> | <evidence: links, commands, results>\n"
    "- unmet | <criterion> | <what is missing>\n"
    f"{EVIDENCE_END}\n"
    "A DONE without a well-formed block is retried. Do not comment on or edit any issue — "
    "the conductor posts the block on the parent, ticks the checkboxes of met criteria and "
    "files one follow-up issue per unmet criterion. Identify any owner-only decision.")
_EVIDENCE_LINE = re.compile(r"[-*+]\s*(met|unmet)\s*\|\s*([^|]*?)\s*\|\s*(.*?)\s*", re.I)


def parse_parent_evidence(text):
    """Entries of the last evidence block in `text`: [{met, criterion, evidence}].

    Raises ValueError when the block is missing or malformed, so the caller
    takes the retry path rather than closing on nothing.
    """
    lines = (text or "").splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip().strip("`*") == EVIDENCE_START]
    if not starts:
        raise ValueError("evidence block missing")
    entries = []
    for line in lines[starts[-1] + 1:]:
        line = line.strip()
        if line.strip("`*") == EVIDENCE_END:
            break
        if not line or line.startswith("```"):
            continue
        m = _EVIDENCE_LINE.fullmatch(line)
        if not m or not m[2] or not m[3]:
            raise ValueError(f"malformed evidence line: {line[:120]}")
        entries.append({"met": m[1].lower() == "met", "criterion": m[2], "evidence": m[3]})
    else:
        raise ValueError(f"evidence block has no {EVIDENCE_END} line")
    if not entries:
        raise ValueError("evidence block is empty")
    return entries


def parent_verify_parent(item):
    """The parent a verification item verifies, or None (see _verify_matches)."""
    item = dict(item)
    m = re.search(r"<!-- mahler:parent-verify(?:-followup)?:(\d+):", item.get("issue_body") or "")
    if m:
        return int(m[1])
    labels = json.loads(item.get("labels") or "[]")
    m = re.fullmatch(r"Verify remaining completion criteria for #(\d+)", item.get("title") or "")
    return int(m[1]) if m and "pass:verify" in labels else None


def evidence_key(project, verify_number, run_id):
    return f"parent_evidence:{project}#{verify_number}:{run_id}"


def _criterion_norm(text):
    text = re.sub(r"^(?:unchecked criterion:\s*)", "", text.strip(), flags=re.I)
    return " ".join(text.strip("`*_ ").split()).casefold()


def post_parent_evidence(ctx, projects):
    """Apply recorded parent-verify evidence; each record is retried until done."""
    if ctx.dry_run:
        return
    for p in projects:
        project = p["name"]
        try:
            rows = ctx.led.q("SELECT key, value FROM kv WHERE key LIKE ?",
                             (f"parent_evidence:{project}#%",))
        except Exception as e:  # never break the tick
            ctx.say(f"{project}: parent evidence lookup failed — {e}")
            continue
        for row in rows:
            try:
                record = json.loads(row["value"] or "{}")
                if record and not record.get("done"):
                    # A clean-worktree claim is unverified until its independent
                    # no-change review passes; evidence must not close the parent first.
                    review = no_change.read(ctx.led, project, record["verify"])
                    # Only the run whose claim the review accepted may apply: evidence
                    # from an earlier, rejected run stays unapplied.
                    if review and (review.get("phase") != "done"
                                   or review.get("claim_run") != record["run"]):
                        continue
                    _apply_parent_evidence(ctx, project, record)
                    record["done"] = True
                    ctx.led.set_kv(row["key"], json.dumps(record))
            except Exception as e:  # one record cannot stop the tick; retried next tick
                ctx.say(f"{project}: parent evidence {row['key']} unresolved — {e}")


def _apply_parent_evidence(ctx, project, record):
    """Tick met criteria, file follow-ups for unmet ones, post one comment.

    Every step is idempotent per run: ticking is a no-op once checked, each
    follow-up and the comment carry per-run markers checked before writing.
    """
    led, gh = ctx.led, ctx.gh(project)
    parent, run_id, verify = record["parent"], record["run"], record["verify"]
    entries = record["entries"]
    body = gh.issue_body(parent)
    lines = body.splitlines(keepends=True)
    by_text = {_criterion_norm(check[2]): (index, check) for index, check, _ in _parent_checks(body)}
    ticked, unmatched = [], []
    for entry in entries:
        if not entry["met"]:
            continue
        found = by_text.get(_criterion_norm(entry["criterion"]))
        if not found:
            unmatched.append(entry["criterion"])
        elif found[1][1] == " ":
            lines[found[0]] = lines[found[0]].replace("[ ]", "[x]", 1)
            ticked.append(entry["criterion"])
    if ticked:
        gh.edit_issue_body(parent, "".join(lines))
    policy = ctx.policy(project)
    labels = ["type:chore", "p2"]
    if policy.get("scope") == "label":
        labels.append(policy["scope_label"])
    inventory = None
    followups = []
    for i, entry in enumerate(entries):
        if entry["met"]:
            continue
        marker = f"<!-- mahler:parent-evidence-followup:{parent}:{run_id}:{i} -->"
        key = f"parent_evidence_followup:{project}:{parent}:{run_id}:{i}"
        url = led.get_kv(key)
        if not url:
            if inventory is None:
                inventory = gh.parent_issue_inventory()
            row = next((r for r in inventory if marker in (r.get("body") or "")), None)
            url = row and (row.get("html_url") or f"#{row['number']}")
        if not url:
            url = gh.create_issue(
                f"Unmet criterion for #{parent}: {entry['criterion'][:80]}",
                f"{marker}\nPart of #{parent}\n\n## Problem / goal\nVerification #{verify} "
                f"(run {run_id}) found this parent criterion unmet:\n\n> {entry['criterion']}\n\n"
                f"What is missing: {entry['evidence']}\n\n## Done when\n"
                f"- [ ] {entry['criterion']}\n", labels)
        led.set_kv(key, url)
        followups.append((entry["criterion"], url))
    marker = f"<!-- mahler:parent-evidence:{parent}:{run_id} -->"
    if not any(marker in c.get("body", "") for c in gh.issue_comments(parent)):
        text = [f"**Parent verification evidence** from #{verify} (run {run_id}):", ""]
        text += [f"- {'met' if e['met'] else 'unmet'}: {e['criterion']} — {e['evidence']}"
                 for e in entries]
        if ticked:
            text += ["", "Ticked: " + "; ".join(ticked)]
        if unmatched:
            text += ["", "Met but not on the checklist (nothing ticked): " + "; ".join(unmatched)]
        if followups:
            text += ["", "Follow-ups:"] + [f"- {c}: {u}" for c, u in followups]
        gh.comment(parent, f"{AGENT_NOTE}\n{marker}\n" + "\n".join(text))


def _parent_verify(ctx, project, number, blockers, fingerprint, inventory, comments=()):
    """Give unresolved criteria and child evidence one verifier per parent.

    Reuse any open or needs-you item for the parent (updating its blocker
    section). A needs-you label keeps the item reusable even if GitHub closed
    it, because the owner question is still unresolved. If only other closed
    items remain and the same blockers stand, file one linked follow-up; if
    that also closes unresolved, raise one diagnostic instead of a chain.
    """
    led, gh = ctx.led, ctx.gh(project)
    matches = _verify_matches(inventory, number)
    reusable_rows = [r for r in matches if _verify_reusable(r)]
    item_key = f"parent_verify_item:{project}:{number}"
    if reusable_rows:
        row = reusable_rows[0]
        url = row.get("html_url") or row.get("url") or ""
        _verify_refresh(gh, row, number, blockers, fingerprint)
        led.set_kv(item_key, url)
        if len(reusable_rows) > 1:
            dup_key = f"parent_verify_dups:{project}:{number}"
            ids = ",".join(str(r["number"]) for r in reusable_rows)
            if led.get_kv(dup_key) != ids:
                ctx.say(f"{project}#{number}: duplicate verify items {ids}; using #{row['number']}")
                led.set_kv(dup_key, ids)
        return
    follow_key = f"parent_verify_followup:{project}:{number}:{fingerprint}"
    followup_marker = f"<!-- mahler:parent-verify-followup:{number}:{fingerprint} -->"
    policy = ctx.policy(project)
    labels = ["type:chore", "size:m", "p2", "pass:verify"]
    if policy.get("scope") == "label":
        labels.append(policy["scope_label"])
    def create(body):
        gh.ensure_pass_label("verify")
        url = gh.create_issue(_verify_title(number), body, labels)
        led.set_kv(item_key, url)
        return url
    evidence_contract = EVIDENCE_CONTRACT
    tail = ("\n\n## Done when\n- [ ] Each remaining parent criterion has a line in the run's PARENT-EVIDENCE block "
            "or an explicit actionable owner question.\n\n## Needs a human to check\nNothing")
    if not matches:
        create(f"{_verify_managed(number, blockers, fingerprint)}\n\n## Problem / goal\n"
               f"Parent #{number} has completed children but still needs evidence for:\n" +
               "\n".join(f"- {b}" for b in blockers) +
               "\n\n## Plan\nCollect evidence for each criterion. "
               "Investigate closed unmerged children for supersession and record the successor and "
               "its merge evidence. " + evidence_contract
               + tail)
        return
    # Only closed verification items remain.
    prior = matches[-1]
    follow = next((r for r in matches if followup_marker in (r.get("body") or "")), None)
    if follow is None and led.get_kv(follow_key):
        follow = next((r for r in matches
                       if (r.get("html_url") or "") == led.get_kv(follow_key)), None)
        if follow is None:
            raise GHError("recorded verify follow-up not found in inventory")
    if follow is None:
        url = create(f"{_verify_managed(number, blockers, fingerprint)}\n{followup_marker}\n\n"
                     f"## Problem / goal\nVerification item #{prior['number']} for parent "
                     f"#{number} closed, but the parent still lists unresolved criteria:\n" +
                     "\n".join(f"- {b}" for b in blockers) +
                     f"\n\n## Plan\nRead the previous result on #{prior['number']} and parent "
                     f"#{number}. " + evidence_contract
                     + tail)
        led.set_kv(follow_key, url)
        return
    led.set_kv(follow_key, follow.get("html_url") or "1")
    diag_key = f"parent_verify_diag:{project}:{number}:{fingerprint}"
    diag_marker = f"<!-- mahler:parent-verify-diagnostic:{fingerprint} -->"
    diagnosed = led.get_kv(diag_key)
    new_diagnostic = not diagnosed
    if new_diagnostic:
        # A fix may merge after the final attempt closed but before the
        # diagnostic is recorded: look back to that closure, not to now().
        diagnosed = follow.get("closed_at") or follow.get("closedAt")
    if diagnosed:
        legacy_diagnostic = not new_diagnostic and diagnosed == "1"
        # Old diagnostics stored only "1"; recover their actual timestamp from
        # GitHub.  They have no merge watermark, so the migration must inspect
        # all linked merges once: a fix can land after the last verification
        # attempt but just before the conductor posts its diagnostic comment.
        if legacy_diagnostic:
            diagnosed = next((c.get("created_at") or c.get("createdAt") for c in comments
                              if diag_marker in c.get("body", "") and
                              (c.get("created_at") or c.get("createdAt"))), iso(led.now()))
            led.set_kv(diag_key, diagnosed)
        targets = {number} | {r["number"] for r in matches}
        texts = [c.get("body", "") for c in comments]
        for row in matches:
            texts.append(row.get("body") or "")
            texts.extend(c.get("body", "") for c in gh.issue_comments(row["number"]))
        # Follow-up fixes are often named in the verification result, rather
        # than recorded as children. Only resolve local issue references.
        for text in texts:
            targets.update(int(m[1]) for m in re.finditer(r"(?<![\w/])#(\d+)\b", text))
        # The inventory contains issues only: a prose PR reference must not
        # become an issue lookup (GitHub uses the same number space).
        targets.intersection_update({number} | {r["number"] for r in inventory})
        merges = {}
        for target in sorted(targets):
            prs = list(gh.closing_prs(target))
            for event in gh._parent_pages(f"repos/{policy['repo']}/issues/{target}/timeline?"):
                source = (event.get("source") or {}).get("issue") or {}
                if (event.get("event") == "cross-referenced" and source.get("pull_request")
                        and source.get("repository_url", "").lower() ==
                        f"https://api.github.com/repos/{policy['repo']}".lower()):
                    prs.append(dict(gh.pr_merge_info(source["number"]), number=source["number"]))
            for pr in prs:
                if (pr_merged(pr) and pr.get("mergedAt") and
                        (legacy_diagnostic or parse(pr["mergedAt"]) > parse(diagnosed))):
                    merges[pr["number"]] = pr["mergedAt"]
        if merges:
            evidence = ", ".join(f"PR #{n}" for n in sorted(merges))
            retry_marker = f"<!-- mahler:parent-verify-merge:{number}:{fingerprint}:" + ",".join(
                str(n) for n in sorted(merges)) + " -->"
            if not any(retry_marker in (r.get("body") or "") for r in matches):
                create(f"{_verify_managed(number, blockers, fingerprint)}\n{retry_marker}\n\n"
                       f"## Plan\nNew merge evidence after the diagnostic: {evidence}. "
                       f"Re-verify parent #{number}, read prior verification results, and "
                       "collect evidence. " + evidence_contract + tail)
            led.set_kv(diag_key, max(merges.values(), key=parse))
            return
        # A merge watermark may have been stored before any diagnostic was
        # posted; if the retry closed unresolved, still emit the diagnostic.
        if not new_diagnostic and any(diag_marker in c.get("body", "") for c in comments):
            return
        if not new_diagnostic and diagnosed == "1":
            return
    first = next((r for r in matches if r is not follow), prior)
    text = (f"Verification attempts #{first['number']} and #{follow['number']} both closed while "
            f"parent #{number} still lists unresolved criteria:\n" +
            "\n".join(f"- {b}" for b in blockers) +
            "\nFurther verification waits for new linked merge evidence; these criteria need attention.")
    if not any(diag_marker in c.get("body", "") for c in comments):
        gh.comment(number, f"{AGENT_MARK}\n{diag_marker}\n{text}")
    ctx.ping(f"{project}#{number}: verification closed without resolving criteria",
             text, project, number)
    led.set_kv(diag_key, diagnosed or iso(led.now()))


def close_finished_parents(ctx, projects):
    """Reconcile tracking parents, independently of child verification clocks."""
    post_parent_evidence(ctx, projects)   # ticks first, so this pass sees them
    for p in projects:
        try:
            _close_finished_parents_project(ctx, p["name"])
        except Exception as e:  # one project cannot stop the tick
            ctx.say(f"{p['name']}: close_finished_parents failed — {e}")


def _close_finished_parents_project(ctx, project):
    led, gh = ctx.led, ctx.gh(project)
    items = led.items(project)
    parents = [it for it in items if it["state"] == "parent" or (
        it["state"] not in {"done", "shipped"} and
        set(json.loads(it["labels"] or "[]")) & {"type:goal", "mahler:parent"})
        or led.get_kv(f"parent_pending:{project}:{it['number']}")]
    if not parents:
        return
    inventory = gh.parent_issue_inventory()
    repo = ctx.policy(project)["repo"]
    # One finite pass: nested parents completed later become eligible next tick.
    # No recursion means cycles remain waiting without manufacturing evidence.
    for parent in parents:
        n = parent["number"]
        try:
            body = gh.issue_body(n)
            state = gh.issue_state(n)
            key = f"parent_complete:{project}:{n}"
            pending = f"parent_pending:{project}:{n}"
            if state not in {"OPEN", "CLOSED"}:
                raise GHError("unknown parent issue state")
            children = _parent_children(body, repo) | set(gh.sub_issues(n))
            children.update(it["number"] for it in items if it["parent"] == n)
            children.update(it["number"] for it in inventory
                            if part_of("\n".join(line for section, line in
                                       _parent_sections(it.get("body") or "")
                                       if section not in {"original request", "context", "dependencies"})) == n)
            children.difference_update(r["number"] for r in _verify_matches(inventory, n))
            children.discard(n)
            reasons = {it["number"]: str(it.get("state_reason") or it.get("stateReason") or "")
                       .strip().lower().replace(" ", "_") for it in inventory}
            # A goal may be built directly after planning. Its type alone does
            # not make it a tracker; explicit markers still permit recovery.
            if not children and not (
                parent["state"] == "parent"
                or "mahler:parent" in json.loads(parent["labels"] or "[]")
                or led.get_kv(pending)
            ):
                continue
            checks = _parent_checks(body)
            blockers = []
            evidence, waiting, completed = [], False, set()
            verifiable = bool(children)
            if not children:
                blockers.append("No child relationships found")
            for child in sorted(children):
                child_state = gh.issue_state(child)
                if child_state == "OPEN":
                    verifiable = False
                    waiting = True
                    continue
                if child_state != "CLOSED":
                    verifiable = False
                    blockers.append(f"#{child}: unknown issue state")
                    continue
                if led.get_kv(f"parent_complete:{project}:{child}"):
                    completed.add(child)
                    evidence.append(f"#{child}: recorded parent completion")
                    continue
                item = led.item(project, child)
                prs = []
                if item and item["pr"]:
                    view = gh.pr_merge_info(item["pr"])
                    if pr_merged(view):
                        prs = [dict(view, number=item["pr"])]
                if not prs:
                    prs = [pr for pr in gh.closing_prs(child) if pr_merged(pr)]
                if prs:
                    completed.add(child)
                    evidence.append(f"#{child}: merged " + ", ".join(
                        f"PR #{pr['number']}" for pr in prs))
                elif reasons.get(child) in {"not_planned", "duplicate"}:
                    # Disposition removes only the demand for a child merge; it is
                    # never added to `completed`, so it cannot tick merge criteria.
                    evidence.append(f"#{child}: closed {reasons[child].replace('_', ' ')} without a PR")
                else:
                    blockers.append(f"#{child}: closed without confirmed merged PR evidence")
            lines = body.splitlines(keepends=True)
            ticked = set()
            for index, check, child in checks:
                if check[1] == " " and child in completed:
                    lines[index] = lines[index].replace("[ ]", "[x]", 1)
                    ticked.add(child)
            if ticked:
                if ctx.dry_run:
                    body = "".join(lines)
                else:
                    gh.edit_issue_body(n, "".join(lines))
                    for child in ticked:
                        led.set_kv(f"parent_tick:{project}:{n}#{child}", "1")
                    body = gh.issue_body(n)
            checklist_blockers = _parent_checklist(body)
            blockers = checklist_blockers + blockers
            if waiting:
                continue
            comments = gh.issue_comments(n)
            if blockers:
                explanation = "Parent completion blocked:\n" + "\n".join(f"- {b}" for b in blockers)
                fingerprint = hashlib.sha256(json.dumps(
                    blockers, sort_keys=True).encode()).hexdigest()
                marker = f"<!-- mahler:parent-blocked:{fingerprint} -->"
                notify_key = f"parent_blocked:{project}:{n}:{fingerprint}"
                ctx.say(f"{project}#{n}: {explanation}")
                if not ctx.dry_run:
                    if verifiable:
                        _parent_verify(ctx, project, n, blockers, fingerprint, inventory, comments)
                    if not any(marker in c.get("body", "") for c in comments):
                        gh.comment(n, f"{AGENT_MARK}\n{marker}\n{explanation}")
                    if not led.get_kv(notify_key):
                        ctx.ping(f"{project}#{n}: parent completion blocked", explanation, project, n)
                        led.set_kv(notify_key, "1")
                continue
            fingerprint = hashlib.sha256(json.dumps([body, evidence]).encode()).hexdigest()
            marker = f"<!-- mahler:parent-complete:{fingerprint} -->"
            ctx.say(f"{project}#{n}: would close — verified children" if ctx.dry_run
                    else f"{project}#{n}: closing — verified children")
            if ctx.dry_run:
                continue
            led.set_kv(pending, fingerprint)
            if not any(marker in c.get("body", "") for c in comments):
                gh.comment(n, f"{AGENT_MARK}\n{marker}\n"
                           "Parent checklist checked; all children completed:\n" +
                           "\n".join(f"- {line}" for line in evidence))
            if state != "CLOSED":
                gh.close_issue(n)
            led.set_kv(key, fingerprint)
            led.set_state(project, n, "done", "verified parent completion")
            led.set_kv(pending, "")
        except Exception as e:  # reads and partially successful writes retry next tick
            ctx.say(f"{project}#{n}: parent completion unresolved — {e}")


def mirror_labels(ctx, project):
    if ctx.dry_run:
        return
    led, gh = ctx.led, ctx.gh(project)
    for item in led.items(project):
        if item["state"] == "done":
            continue
        if item["state"] == "shipped":
            if item["mirror"] != "mahler:shipped":
                mirror_shipped(ctx, project, item)
            continue
        want = STATE_LABELS.get(item["state"])
        current = ctx._labels.get((project, item["number"]))
        if current is None:
            continue
        if want in current and not [l for l in current if l in LABEL_STATES and l != want]:
            if item["mirror"] != want:
                led.upsert_item(project, item["number"], mirror=want)
            continue
        try:
            gh.set_state_label(item["number"], item["state"], current)
            led.upsert_item(project, item["number"], mirror=want)
        except GHError as e:
            ctx.say(f"{project}#{item['number']}: label update failed — {e}")
