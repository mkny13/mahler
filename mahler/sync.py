"""GitHub is the backlog (DESIGN D2): this pass makes the ledger match it.

Issues, their labels, their comments and the commands in them come in;
Mahler's own state labels go back out. Closing a parent whose sub-issues are
all done is the same kind of work, so it lives here too.
"""

import json
from datetime import timedelta

from . import config, releases
from .gh import (GHError, AGENT_MARK, LABEL_STATES, STATE_LABELS, depends_of,
                 dependency_ref, dependency_target, files_of, has_sections,
                 label_names, parse_command, part_of, pin_of, priority_of,
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
    if not poll_changed and led.get_kv(depends_key) == "4":
        reconcile_shipped(ctx, project)
        ctx.say(f"{project}: GitHub unchanged (304) — sync skipped")
        return
    issues = gh.open_issues()

    if pol.get("scope") == "label":
        scope_label = pol["scope_label"]
        cutoff = led.now() - timedelta(hours=24)
        count = sum(1 for iss in issues
                    if scope_label not in label_names(iss)
                    and parse(iss["createdAt"]) < cutoff)
        if not ctx.dry_run:
            led.set_kv(f"missing_scope:{project}", str(count))
        in_scope_nums = {
            iss["number"] for iss in issues
            if scope_label in label_names(iss)
            or any(l in LABEL_STATES for l in label_names(iss))
            or led.item(project, iss["number"]) is not None
        }
        # If child issues reference an in-scope parent via "Part of #N", inherit scope
        changed = True
        while changed:
            changed = False
            for iss in issues:
                n = iss["number"]
                if n not in in_scope_nums:
                    parent_n = part_of(iss.get("body"))
                    if parent_n and (parent_n in in_scope_nums or led.item(project, parent_n) is not None):
                        in_scope_nums.add(n)
                        changed = True
    else:
        in_scope_nums = None

    # Prefer this poll over cached ancestry, regardless of GitHub issue ordering.
    parents = {iss["number"]: part_of(iss.get("body")) for iss in issues}
    open_nums = set()
    for iss in issues:
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
            if (item["state"] == "inbox" and led.lease(project, n) is None
                    and planned_child(iss, labels, led, project)):
                _born_ready(led, project, n, iss)
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
    if poll_etag:
        led.set_kv(etag_key, poll_etag)
    led.set_kv(depends_key, "4")


QUIET_BATCH = 25   # legacy rows migrate in bounded ticks


def _note_source_reopen(ctx, project, item):
    """An open issue whose shipment we closed and labelled was reopened by hand
    (adverse evidence). A transient close failure never set the mirror."""
    led = ctx.led
    n = item["number"]
    shipped = item["state"] == "shipped" and item["mirror"] == "mahler:shipped"
    early = (item["state"] == "done" and led.q1(
        "SELECT 1 FROM completion_evidence WHERE project=? AND number=? AND kind='smoke'",
        (project, n)))
    key = f"source_reopened:{project}:{n}"
    if (shipped or early) and led.get_kv(key) != "1":
        led.set_kv(key, "1")
        led.event("source_reopened", project, n, {})


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
    for item in led.items(project, ["shipped"]):
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
            _apply_instruction(ctx, project, led.item(project, item["number"]), *cmd)
        else:
            current = led.item(project, item["number"])
            if current["state"] == "failed":
                resume_item(led, project, item["number"])
                ctx.say(f"{project}#{item['number']}: answer received, resuming")
            elif current["state"] == "needs_you":
                led.set_state(project, item["number"], "inbox", "you answered — re-sorting",
                              sorted_at=None)
                ctx.say(f"{project}#{item['number']}: answer received, re-sorting")
    if newest and newest != seen:
        led.upsert_item(project, item["number"], last_comment_at=iso(newest))


def resume_item(led, project, number):
    """Apply the same reset as ``/mahler go`` to a failed item."""
    led.set_state(project, number, "ready", "you said go", attempts=0, setup_fails=0,
                  esc_tier=0, esc_fails=0,
                  sorted_at=iso(led.now() - timedelta(days=1)))


def _apply_instruction(ctx, project, item, verb, arg):
    led, n = ctx.led, item["number"]
    if verb == "go":
        resume_item(led, project, n)
    elif verb == "park":
        led.set_state(project, n, "parked", "you parked it")
        for run in led.active_runs(project):
            if run["number"] == n:
                request_stop(ctx, run, "parked")
    elif verb == "inbox":
        led.set_state(project, n, "inbox", "back to inbox", sorted_at=None)
    elif verb == "platform" and arg:
        if arg in ("none", "auto"):
            _set_pin(ctx, project, n, None)
        elif arg in ctx.cfg["platforms"]:
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


def close_finished_parents(ctx, projects):
    """Close parent issues when all their sub-issues are done.
    
    For each item in state 'parent', find children (items with parent == number).
    If there is at least one child and every child is in state 'done':
    post a comment listing the children, close the issue, and set state to 'done'.
    """
    led = ctx.led
    for p in projects:
        try:
            _close_finished_parents_project(ctx, p["name"])
        except Exception as e:                  # noqa: BLE001 — one project can't stop the rest
            ctx.say(f"{p['name']}: close_finished_parents failed — {e}")


def _close_finished_parents_project(ctx, project):
    led, gh = ctx.led, ctx.gh(project)
    for parent_item in led.items(project, ["parent"]):
        parent_num = parent_item["number"]
        # Find children: items of the same project with parent == parent_num
        children = [it for it in led.items(project) if it["parent"] == parent_num]
        if not children:
            continue  # no children, nothing to do
        # Check if all children are done
        if all(child["state"] == "done" for child in children):
            child_nums = [str(c["number"]) for c in children]
            comment = (
                f"<!-- mahler:agent -->\n"
                f"All sub-issues done — closing.\n\n"
                f"Sub-issues: {', '.join(f'#{n}' for n in child_nums)}"
            )
            if ctx.dry_run:
                ctx.say(f"{project}#{parent_num}: would close — all {len(children)} sub-issue(s) done")
            else:
                try:
                    gh.close_issue(parent_num, comment=comment)
                    led.set_state(project, parent_num, "done", "all sub-issues done")
                    ctx.say(f"{project}#{parent_num}: closed — all sub-issues done")
                except GHError as e:
                    ctx.say(f"{project}#{parent_num}: failed to close — {e}")


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
