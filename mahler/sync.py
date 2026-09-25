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
                 label_names, parse_command, part_of, pin_of, priority_of)
from .ledger import iso, parse
from .ship import record_release_item_if_needed, record_uat_if_needed
from .tick import approval_key
from .watchdog import request_stop


def sync(ctx, project):
    led, gh = ctx.led, ctx.gh(project)
    pol = ctx.policy(project)
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
        ctx.say(f"{project}: GitHub unchanged (304) — sync skipped")
        return
    issues = gh.open_issues()

    if pol.get("scope") == "label":
        scope_label = pol["scope_label"]
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
            _adopt_label_edits(ctx, project, item, labels)
        _process_comments(ctx, project, led.item(project, n), iss.get("comments") or [])

    for item in led.items(project):
        if item["number"] in open_nums or item["state"] == "done":
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
                        pass
                    else:
                        record_uat_if_needed(ctx, project, item["number"], item["pr"], item, view)
                        record_release_item_if_needed(ctx, project, item["number"], item["pr"], item, view)
                led.release(project, item["number"])
                led.set_state(project, item["number"], "done", "closed on GitHub")

    if poll_etag:
        led.set_kv(etag_key, poll_etag)
    led.set_kv(depends_key, "4")


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
            author = (c.get("author") or {}).get("login") or ""
            gh_repo = getattr(ctx.gh(project), "repo", "")
            repo_owner = gh_repo.split("/")[0] if isinstance(gh_repo, str) and "/" in gh_repo else ""
            is_owner = c.get("authorAssociation") == "OWNER" or (repo_owner and author.casefold() == repo_owner.casefold())
            _apply_instruction(ctx, project, led.item(project, item["number"]), *cmd, is_owner=is_owner)
        elif led.item(project, item["number"])["state"] == "needs_you":
            led.set_state(project, item["number"], "inbox", "you answered — re-sorting",
                          sorted_at=None)
            ctx.say(f"{project}#{item['number']}: answer received, re-sorting")
    if newest and newest != seen:
        led.upsert_item(project, item["number"], last_comment_at=iso(newest))


def _apply_instruction(ctx, project, item, verb, arg, is_owner=True):
    led, n = ctx.led, item["number"]
    if verb == "go":
        led.set_state(project, n, "ready", "you said go", attempts=0, setup_fails=0,
                      esc_tier=0, esc_fails=0,
                      sorted_at=iso(led.now() - timedelta(days=1)))
    elif verb == "approve":
        if not is_owner:
            ctx.say(f"{project}#{n}: ignored /mahler approve from non-owner")
            return
        # mahler#433: the owner allows approval-gated platforms (Fable, Astra)
        # for this item. An item with an open PR goes back to the conductor,
        # which starts the fix it was waiting for; anything else is rescheduled.
        led.set_kv(approval_key(project, n), iso(led.now()))
        if item["state"] == "needs_you":
            if item["pr"]:
                led.set_state(project, n, "verifying", "you approved — the conductor retries")
            else:
                led.set_state(project, n, "ready", "you approved",
                              sorted_at=item["sorted_at"] or iso(led.now()))
    elif verb == "park":
        led.set_state(project, n, "parked", "you parked it")
        for run in led.active_runs(project):
            if run["number"] == n:
                request_stop(ctx, run, "parked")
    elif verb == "inbox":
        led.set_state(project, n, "inbox", "back to inbox", sorted_at=None)
    elif verb == "platform" and arg:
        # A pin exempts its platform from the approval gate (router), so it is
        # as much the owner's call as /mahler approve (mahler#433).
        if not is_owner:
            ctx.say(f"{project}#{n}: ignored /mahler platform from non-owner")
            return
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
