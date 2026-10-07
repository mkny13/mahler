"""The tick's own work: due maintenance, lease expiry, and handing out slots.

Everything here is decided from the ledger alone — which items could run,
which platform can afford them, and what to start. The passes that react to
the outside world live in watchdog.py, sync.py, finalize.py and ship.py.
"""

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import capacity, config, launch_health, platforms, presence, prompt, router, runner
from .gh import GHError, dependency_target
from .ledger import iso, parse, row_get
from .usage import compute_burst


MAINTENANCE_TEXT = {
    "security": ("Security & Surface Area Audit", "Security & Surface Area Audit — credential boundaries (`.env` leaks, unpooled URLs), subshell executions (`subprocess.run` argument sanitization), permission boundaries, denial list checks, dependency scans."),
    "health": ("Codebase Health & Refactoring Pass", "Codebase Health & Refactoring Pass — unclosed resource leaks (DB connections, file descriptors), dead code / orphaned helpers, cyclomatic complexity hotspots."),
    "drift": ("Architecture & Specification Drift Audit", "Architecture & Specification Drift Audit — comparing implementation against `DESIGN.md` / `ARCHITECTURE.md` / `ROADMAP.md`, cleaning up zombie abstractions."),
    "tests": ("Test Suite Health & Flakiness Audit", "Test Suite Health & Flakiness Audit — test isolation, false-green tests, execution time creep, `ResourceWarning` checks; fixtures hand-written to match the decoder rather than recorded from the real upstream service; no scheduled check that upstream API shapes still match (couch-tour#405)."),
    "gate-coverage": ("Shipped-Artifact Gate Coverage Audit", """Inventory every artifact/target the project ships from release workflows, build scripts,
package manifests and release documentation. Inspect the effective project `verify`
command supplied to the run, the repository verify contract, and PR CI workflows
(including called scripts, target selection, triggers, path filters and conditions).
Distinguish PR checks from checks run only at release; testing a shared library does
not prove its app target compiles (couch-tour#402).

Report an evidence-backed artifacts × gates table with columns:
| Artifact/target | verify | PR CI | release-only |
For each cell cite the exact command and file/workflow path that builds or exercises
that artifact, or mark it uncovered, unknown, or not applicable with the reason.
Do not infer coverage from a green badge or a workflow name. Identify release-only
coverage explicitly; compilation first attempted at release is too late to gate PRs.
If the effective verify command or other evidence is unavailable, report that
uncertainty rather than guessing coverage.

For each uncovered artifact/gate gap, search existing issues and file one deduplicated
issue (reuse and link an existing issue for the same gap), linked back to this audit.
Include the artifact, missing gate, evidence, recommended command/workflow change,
and acceptance check. Link the resulting issues in the report; explain any project
non-applicability. This is an audit and recommendation pass: do not automatically
change workflows, build scripts, or verify commands, or run release-only jobs.
Never edit `~/.mahler/config.toml`. Changing the operator's `verify` is an owner
decision; propose the exact change for the owner. Repository fixes go through normal
issues and PRs."""),
    "token-economy": ("Token Economy/Quota & Performance Hygiene", "Token Economy/Quota & Performance Hygiene — prompt context bloat in recipes/rules, run duration outliers, excessive polling overhead, DB query efficiency."),
    "guidance": ("Agent Guidance & Rule Calibration", "Agent Guidance & Rule Calibration — reviewing `AGENTS.md` / `CLAUDE.md` / `recipes` against observed failure modes, pruning obsolete instructions."),
    "backlog": ("Issue Backlog Pruning Pass", "Issue Backlog Pruning Pass — parent/goal issues whose sub-issues are all closed but the parent itself wasn't, issues superseded by a later split or refactor (check against current module boundaries, not the description text), duplicate or overlapping issues covering the same ground, and stale mahler:parked items worth reviving or closing."),
    "bugs": ("Correctness Bug Scan", "Correctness Bug Scan — logic errors, off-by-one and boundary conditions, incorrect error handling or silently-swallowed exceptions, race conditions between concurrent runs, and edge cases (None/empty/malformed input) found by close reading or targeted tests. Not style, structure or refactoring — that's the health pass."),
    "escapes": ("Escaped-Bug Gate Improvement Review", """Inspect only `type:bug` issues closed in the review interval below, using their
closedAt timestamps (paginate results), bodies, comments and linked fixes. Respect
this project's scope label when applicable. Read `Escape cause` and `Check that now
catches it`; missing analysis is unknown, not proof of coverage. Group source bugs
by escape cause / failure class, and assess the mechanical checks now in place.

For each uncovered class, search existing open and closed gate-improvement issues
and links from the source bugs. Reuse and link existing adequate work instead of
re-filing it. File one proposed mechanical-check issue per uncovered class, citing
and linking every source bug in that class and this audit. Specify the missing
gate, proposed test, CI step, lint, contract check or smoke journey, and a concrete
acceptance check. Propose gates, not guidance. If no feasible mechanical check
exists, explain why. Report classes, source links, existing coverage and reused or
new issue links; if no bugs or gaps qualify, say so without filing filler work.
This pass proposes checks; implementing them belongs to separate project issues."""),
    "docs": ("Documentation Accuracy & Onboarding Review", "Documentation Accuracy & Onboarding Review — verify README setup and safety guidance from a fresh-user perspective; compare commands, examples, architecture, roadmap status, and operator/agent documentation with the current code and CLI help; fix stale claims, broken links, machine-specific examples, and undocumented configuration or behavior. Preserve historical decisions as history, but clearly distinguish shipped behavior from plans."),
}


def queue_maintenance(ctx, projects):
    """File due maintenance passes as issues (deduped)."""
    led, now = ctx.led, ctx.led.now()
    for p in projects:
        pol = config.maintenance_policy(ctx.cfg, p["name"])
        if not pol["enabled"] or p["name"] in ctx.passes_filed:
            continue
        definitions = {**MAINTENANCE_TEXT, **{
            key: (entry["title"], entry["text"])
            for key, entry in pol["custom"].items()
        }}
        passes = pol["passes"]
        if not passes:
            continue
        
        pol_full = ctx.policy(p["name"])
        scope_label = pol_full.get("scope_label") if pol_full.get("scope") == "label" else None
        
        items = led.items(p["name"])
        
        # D20: at most one pass in flight per project. If any pass:* item
        # or manually-created audit with a matching pass title is still open,
        # skip all passes for now (issue #204).
        #
        # Note on scope matching (issue #204): we match explicit pass:* labels
        # and exact/normalized configured pass titles. Free-form manual audit
        # titles with different wording (e.g. #57 vs #81) are intentionally not
        # heuristically guessed here to avoid false positives against unrelated
        # type:goal / feature items; manual audits should use the canonical pass
        # title or carry a pass:<name> label.
        pass_titles = {text[0].strip().lower() for text in definitions.values()}
        has_open_pass = False
        for it in items:
            if it["state"] == "done":
                continue
            labels = json.loads(it["labels"] or "[]")
            title = (it["title"] or "").strip().lower()
            if any(l.startswith("pass:") for l in labels) or title in pass_titles:
                has_open_pass = True
                break
        if has_open_pass:
            continue
        
        for pass_name in passes:
            label = f"pass:{pass_name}"
            pass_title = definitions[pass_name][0].strip().lower()
            skip = False
            for it in items:
                labels = json.loads(it["labels"] or "[]")
                title = (it["title"] or "").strip().lower()
                if label not in labels and title != pass_title:
                    continue
                # D20: only one pass in flight per project — an open pass item
                # was already caught above, so here we only handle done items.
                if it["state"] != "done":
                    skip = True
                    break
                # If this pass item has a parent that isn't done, skip
                if it["parent"] is not None:
                    parent = led.item(p["name"], it["parent"])
                    if parent and parent["state"] != "done":
                        skip = True
                        break
                changed_at = parse(it["state_changed_at"])
                if changed_at and now - changed_at < timedelta(days=pol["cooldown_days"]):
                    skip = True
                    break
            
            if skip:
                continue
            
            if not led.maintenance_due(p["name"], pass_name, policy=pol):
                continue
            
            title, body = definitions[pass_name]
            if pass_name == "escapes":
                previous = led.maintenance_checkpoint(p["name"], pass_name)["last_filed_at"]
                start = previous or iso(now - timedelta(days=pol["cadence_days"]))
                body += (f"\n\nReview interval: {start} < closedAt <= {iso(now)} (UTC). "
                         + ("Start is the previous escapes pass filing checkpoint."
                            if previous else
                            "First pass: no prior checkpoint; review one cadence window."))
            issue_labels = ["type:chore", "size:l", "p2", label]
            if scope_label is not None:
                issue_labels.append(scope_label)
            
            ctx.say(f"{p['name']}: queuing {pass_name} pass")
            if not ctx.dry_run:
                try:
                    ctx.gh(p["name"]).ensure_pass_label(pass_name)
                    ctx.gh(p["name"]).create_issue(title, body, issue_labels)
                    if pass_name == "escapes":
                        # Keep successive review intervals contiguous even when
                        # filing requests or earlier projects take time.
                        led.set_maintenance_checkpoint(p["name"], pass_name,
                                                       last_filed_at=now)
                    else:
                        led.reset_maintenance(p["name"], pass_name)
                except GHError as e:
                    ctx.say(f"{p['name']}: failed to file {pass_name} pass — {e}")
                    continue
            # Sync will not see the new issue until the next tick.
            ctx.passes_filed.add(p["name"])
            break


def busy_platforms(cfg, active):
    """Count active runs per quota group (quota_group or platform name).

    A platform is busy when its group's count reaches that platform's
    max_runs (default 1), or when it isn't available(). Platforms sharing
    a quota_group share one slot (DESIGN D21).
    """
    per_group = {}
    for r in active:
        pname = r["platform"]
        pconf = cfg["platforms"].get(pname, {})
        group = pconf.get("quota_group", pname)
        per_group[group] = per_group.get(group, 0) + 1
    busy = set()
    for pname, pconf in cfg["platforms"].items():
        group = pconf.get("quota_group", pname)
        if per_group.get(group, 0) >= pconf.get("max_runs", 1):
            busy.add(pname)
        if not platforms.available(pconf):
            busy.add(pname)
    return busy


def tier_budget_busy(cfg, tiers):
    """Platforms whose tier bucket has hit its concurrency budget
    (`concurrency.by_tier`, optional, mahler#200): a budget keyed at tier T
    caps concurrent runs at tier T *or above* — a scarcer run also counts
    against a laxer budget, so it can't dodge a tier-3-and-up cap by running
    at tier 4. `tiers` is the list of tiers of every active-or-just-started
    run this tick. Layered under `concurrency.total`, never a replacement for
    it: an absent/empty `by_tier` returns an empty set every time.
    """
    by_tier = cfg["concurrency"].get("by_tier") or {}
    exhausted = [int(t) for t, cap in by_tier.items()
                 if sum(1 for x in tiers if x >= int(t)) >= cap]
    if not exhausted:
        return set()
    floor = min(exhausted)
    return {name for name, pconf in cfg["platforms"].items()
            if router.tier_of(pconf) >= floor}


def sweep_orphans(ctx):
    """Orphan sweep keyed on items (not just expiring leases):
    1. Items in 'working' with no lease row and no active run -> return to 'ready'.
    2. Mirror case: items in 'ready'/'done' with a live lease row -> drop the lease row.
    """
    led = ctx.led
    for item in led.orphan_working_items():
        project, n = item["project"], item["number"]
        led.set_state(project, n, "ready", "orphan: working with no lease or run returned to ready")
        led.event("orphan_recovered", project, n, {"reason": "working with no lease or run"})
        ctx.say(f"{project}#{n}: orphan working item (no lease, no active run) returned to ready")

    for lease in led.orphan_lease_rows():
        project, n = lease["project"], lease["number"]
        led.release(project, n, holder=lease["holder"], epoch=lease["epoch"], to_state=None)
        led.event("orphan_lease_released", project, n,
                  {"holder": lease["holder"], "item_state": lease["item_state"]})
        ctx.say(f"{project}#{n}: orphan lease by {lease['holder']} on {lease['item_state']} item released")


def expire(ctx):
    led = ctx.led
    for lease in led.expired_leases():
        project, n = lease["project"], lease["number"]
        if lease["kind"] == "auto":
            live = [r for r in led.active_runs(project) if r["id"] == lease["run_id"]]
            if live:
                continue            # the watchdog owns running runs
        pol = ctx.policy(project)
        if lease["kind"] == "interactive":
            last = presence.last_claude_activity(pol["path"]) if pol.get("path") else None
            if last and led.now() - last < timedelta(minutes=pol["interactive_lease_minutes"]):
                led.heartbeat(project, n, lease["holder"], lease["epoch"],
                              pol["interactive_lease_minutes"])
                continue
        led.release(project, n, holder=lease["holder"], epoch=lease["epoch"],
                    to_state="ready", why=f"{lease['kind']} lease expired")
        ctx.say(f"{project}#{n}: {lease['kind']} lease by {lease['holder']} expired")
    sweep_orphans(ctx)


def _size_label(it):
    return next((l.split(":", 1)[1] for l in json.loads(row_get(it, "labels", "[]"))
                 if l.startswith("size:")), None)


def _observe(ctx, p, role, it, *blockers, **kw):
    """Record one waiting candidate for the capacity history (mahler#734)."""
    routing_role = ("plan" if role == "sort" and needs_plan(row_get(it, "labels", "[]"))
                    else role)
    capacity.of(ctx).observe(p["name"], it["number"], role, blockers=blockers,
                             routing_role=routing_role, size=_size_label(it), **kw)


def _candidates(ctx, projects):
    """One global candidate list of (pol, role, item) across all enabled
    projects — sorts and settled builds compete for the same slots (mahler#9)."""
    led = ctx.led
    work = []
    enabled = config.enabled_projects(ctx.cfg)
    # Merged is enough to build on: UAT catches up after the fact (mahler#683).
    done = {(p["name"], i["number"]) for p in enabled
            for i in led.items(p["name"], ["shipped", "done"])}
    planning = {(r["project"], r["number"]) for r in led.active_runs()
                if r["role"] == "sort"}
    for p in projects:
        name = p["name"]
        for it in led.items(name, ["inbox"]):
            if (name, it["parent"]) in planning:
                continue  # The parent planner is still writing this child.
            work.append((p, "sort", it))
        for it in led.items(name, ["ready"]):
            sorted_at = parse(it["sorted_at"])
            if sorted_at and led.now() - sorted_at < timedelta(minutes=p["settle_minutes"]):
                ctx.hold("settling", project=name, number=it["number"],
                         until=iso(sorted_at + timedelta(minutes=p["settle_minutes"])))
                _observe(ctx, p, "build", it, "settling")
                continue
            deps = [d for d in json.loads(it["depends"] or "[]")
                    if dependency_target(d, name, enabled) not in done]
            if deps:
                ctx.hold("deps", project=name, number=it["number"], on=deps)
                _observe(ctx, p, "build", it, "deps")
                continue
            work.append((p, "build", it))
    return work


def _headroom(ctx, role, per_group, busy, burst_lines=None, account=config.DEFAULT_ACCOUNT):
    """Routing platforms for `role` that could take a new run right now:
    enabled, under its quota_group's max_runs (shared across every variant of
    a platform slot, D21/#420), reachable and under its soft lines. Burst
    lines (D23) raise Claude's soft lines when a window is about to reset.
    The peak window (D22) removes Claude platforms from headroom entirely while
    it's active, so the `sorts_wait` logic stays right: a sort that would pick
    Claude must not be counted as having a free builder available.
    """
    cfg, led = ctx.cfg, ctx.led
    peak_active, _ = router.peak_state(cfg, led)
    free = []
    for name in router.candidates(cfg, role, burst_lines=burst_lines, account=account):
        pc = cfg["platforms"][name]
        group = pc.get("quota_group", name)
        if name in busy or per_group.get(group, 0) >= pc.get("max_runs", 1):
            continue
        if peak_active and pc.get("kind") == "claude":
            continue
        claude_lines = burst_lines if pc.get("kind") == "claude" else None
        if router.usage_state(led, name, pc, burst_lines=claude_lines)[0] != "ok":
            continue
        free.append(name)
    return free


def area_of(labels_json):
    """The item's `area:<name>` label, or None (soft mutual exclusion, D6):
    items sharing an area aren't run concurrently, even though neither blocks
    the other's `depends`."""
    try:
        labels = json.loads(labels_json or "[]")
    except json.JSONDecodeError:
        return None
    for label in labels:
        if label.startswith("area:"):
            return label.split(":", 1)[1]
    return None


def files_of(files_json):
    """The item's planned file list, parsed from its `## Plan` section's
    `Files:` line at sync time (`gh.files_of`, mahler#210) — mechanical
    mutual exclusion alongside `area_of`'s label-based one: two items whose
    planned files intersect aren't run concurrently, even when neither
    carries an `area:` label. `area:` stays as a manual override for overlap
    the file list doesn't capture."""
    try:
        return json.loads(files_json or "[]")
    except json.JSONDecodeError:
        return []


def needs_plan(labels_json):
    """True when the labels indicate this item needs Opus planning (DESIGN D21):
    type:goal, size:l, or any pass:* label."""
    try:
        labels = json.loads(labels_json or "[]")
    except json.JSONDecodeError:
        return False
    for label in labels:
        if label == "type:goal" or label == "size:l" or label.startswith("pass:"):
            return True
    return False


def _init_schedule_state(ctx, projects):
    """Collect all mutable scheduling state before the main loop.

    Returns a dict with: total, in_project, per_group, busy, tiers,
    busy_areas, busy_files, in_flight, hot, burst_lines.
    """
    led, cfg = ctx.led, ctx.cfg
    active = led.active_runs()
    in_project, per_group = {}, {}
    for r in active:
        in_project[r["project"]] = in_project.get(r["project"], 0) + 1
        group = cfg["platforms"].get(r["platform"], {}).get("quota_group", r["platform"])
        per_group[group] = per_group.get(group, 0) + 1
    busy = busy_platforms(cfg, active)
    # concurrency.by_tier (mahler#200): a finer-grained cap layered under
    # `total` — tracked as a running list of tiers so it can be recomputed
    # after each start this pass, same as the quota-group busy set below.
    tiers = [router.tier_of(cfg["platforms"][r["platform"]])
             for r in active if r["platform"] in cfg["platforms"]]
    busy |= tier_budget_busy(cfg, tiers)

    # area: label collision (D6): items sharing an area within a project
    # aren't run concurrently. File paths are project-local too (mahler#231).
    # Planned-file collision (mahler#210): same idea, mechanical — seed
    # busy_files from the same running/verifying items' `## Plan` Files: list.
    busy_areas = defaultdict(set)
    busy_files = defaultdict(set)
    for r in active:
        r_item = led.item(r["project"], r["number"])
        if r_item:
            area = area_of(row_get(r_item, "labels", "[]"))
            if area:
                busy_areas[r["project"]].add(area)
            busy_files[r["project"]].update(files_of(row_get(r_item, "files", "[]")))

    # a finished build keeps its project's build slot until it merges (D19)
    in_flight = {}
    for p in projects:
        verifying = led.items(p["name"], ["verifying"])
        in_flight[p["name"]] = len(verifying)
        # ...and from verifying items too: a merged-but-unverified change
        # still holds its area against a race with a moving base (D19).
        for it in verifying:
            area = area_of(row_get(it, "labels", "[]"))
            if area:
                busy_areas[p["name"]].add(area)
            busy_files[p["name"]].update(files_of(row_get(it, "files", "[]")))

    hot = {}
    for p in projects:
        hot[p["name"]] = False
        if p.get("hot_hold") and ctx.hot_hold:
            last = presence.last_claude_activity(p["path"])
            hot[p["name"]] = bool(
                last and not presence.hot_hold_overridden(led, p["name"], last)
                and led.now() - last < timedelta(minutes=p["hot_hold_minutes"]))

    burst_lines = compute_burst(ctx, projects)
    return {"total": len(active), "in_project": in_project, "per_group": per_group,
            "busy": busy, "tiers": tiers, "busy_areas": busy_areas,
            "busy_files": busy_files, "in_flight": in_flight, "hot": hot,
            "burst_lines": burst_lines}


def _order_candidates(ctx, work, st, projects):
    """Deterministic candidate sort: priority → builds-before-sorts when
    headroom is scarce → priority projects → oldest state_changed_at."""
    cfg = ctx.cfg
    # each account has its own builders, so "a sort must not eat the last
    # builder" is judged per account (D25); a multi-account project competes
    # in every bucket it can draw from (D26)
    buckets = {acct for p in projects for acct in config.accounts_of(p)}

    def _headroom_groups(account):
        # _headroom lists available platform *variants*; several variants of
        # the same platform can share one quota_group slot (D21/#420), so
        # counting variants overstates capacity — dedupe to distinct groups
        # before judging whether a sort would take the last builder.
        free = _headroom(ctx, "sort", st["per_group"], st["busy"],
                         st["burst_lines"], account)
        return {cfg["platforms"][name].get("quota_group", name) for name in free}

    sorts_wait = {acct: len(_headroom_groups(acct)) <= 1 for acct in buckets}
    priority_projects = cfg.get("scheduling", {}).get("priority_projects", ["mahler"])

    def key(c):
        p, role, it = c
        proj_idx = (priority_projects.index(p["name"])
                    if p["name"] in priority_projects
                    else len(priority_projects))
        return (it["priority"],
                1 if (role == "sort" and all(sorts_wait[a] for a in
                                             config.accounts_of(p))) else 0,
                proj_idx,
                parse(it["state_changed_at"]) or datetime.min.replace(tzinfo=timezone.utc),
                p["name"], it["number"])
    work.sort(key=key)


def _check_eligible(ctx, p, role, it, st):
    """Per-candidate eligibility gates: capacity, in-flight slot, hot hold,
    area/file collision, lease, remote-error.

    Returns (eligible, area, item_files) — eligible is False when the
    candidate is ineligible (a hold or skip was emitted).  area and
    item_files are passed through to _account_start on success.
    """
    led, name, n = ctx.led, p["name"], it["number"]
    if st["in_project"].get(name, 0) >= p["max_parallel"]:
        if name not in st["said"]:
            st["said"].add(name)
            holders = [{"number": r["number"], "label": router.lease_label(led, lease)}
                       for r in led.active_runs() if r["project"] == name
                       if (lease := led.lease(name, r["number"])) and lease["capacity"]]
            detail = ("; ".join(h["label"] for h in holders)
                      or f"{st['in_project'].get(name, 0)} running or planned")
            ctx.say(f"{name}: at capacity ({detail})")
            ctx.hold("capacity", project=name, max_parallel=p["max_parallel"],
                     holders=holders)
        _observe(ctx, p, role, it, "project_slots")
        return False, None, []
    if role == "build" and st["in_project"].get(name, 0) + st["in_flight"][name] >= p["max_parallel"]:
        if name not in st["said"]:
            st["said"].add(name)
            ctx.say(f"{name}: builds wait — {st['in_flight'][name]} finished change(s) "
                    "not merged yet")
            ctx.hold("slot", project=name, verifying=[
                v["number"] for v in led.items(name, ["verifying"])])
        _observe(ctx, p, role, it, "in_flight")
        return False, None, []
    # Hot hold (D6 layer 2): no new *code-writing* starts while an
    # untracked Claude session is active in the project — running work
    # continues, and the hold lifts hot_hold_minutes after the last
    # transcript activity. Sorts are read-only triage (no worktree, no
    # files) and fixes repair an item already in flight, so neither
    # competes with the human's work; only builds are gated.
    if role == "build" and st["hot"][name]:
        ctx.say(f"{name}#{n}: hot hold — a Claude session is active in this project")
        ctx.hold("hot_hold", project=name, number=n)
        _observe(ctx, p, role, it, "hot_hold")
        return False, None, []
    area = area_of(row_get(it, "labels", "[]")) if role in ("build", "fix") else None
    if area and area in st["busy_areas"][name]:
        ctx.say(f"{name}#{n}: waiting — area:{area} already in progress")
        ctx.hold("area", project=name, number=n, area=area)
        _observe(ctx, p, role, it, "area")
        return False, None, []
    item_files = files_of(row_get(it, "files", "[]")) if role in ("build", "fix") else []
    file_overlap = st["busy_files"][name].intersection(item_files)
    if file_overlap:
        ctx.say(f"{name}#{n}: waiting — files already in progress: "
                f"{', '.join(sorted(file_overlap))}")
        ctx.hold("files", project=name, number=n, files=sorted(file_overlap))
        _observe(ctx, p, role, it, "files")
        return False, None, []
    if led.lease(name, n):
        return False, None, []
    remote_error = getattr(led, "remote_error", lambda _project: None)(name)
    if remote_error:
        ctx.say(f"{name}: canonical lease host unavailable — project skipped this tick")
        ctx.hold("lease_host", project=name)
        _observe(ctx, p, role, it, "lease_host")
        return False, None, []
    return True, area, item_files


def _route_inputs(it, role):
    """-> (routing_role, pin, effective_min_tier, effective_size) for one candidate."""
    size = _size_label(it)
    routing_role = "plan" if (role == "sort" and needs_plan(row_get(it, "labels", "[]"))) else role
    pin = it["pin"] if role in ("build", "fix", "sort") else None
    effective_min_tier = (max(row_get(it, "esc_tier", 0),
                              router.risk_min_tier(row_get(it, "title", "")))
                          if role in ("build", "fix") else 0)
    effective_size = size
    if role in ("build", "fix") and effective_min_tier >= 2 and effective_size == "s":
        effective_size = "m"
    return routing_role, pin, effective_min_tier, effective_size


def _diagnose(ctx, p, role, it, st):
    """Read-only per-platform eligibility, or None if the diagnosis itself failed."""
    routing_role, pin, min_tier, size = _route_inputs(it, role)
    try:
        return router.diagnose(ctx.cfg, ctx.led, p, routing_role, pin, st["busy"], size=size,
                               burst_lines=st["burst_lines"], min_tier=min_tier)
    except Exception as e:                       # noqa: BLE001 — telemetry only
        ctx.say(f"{p['name']}#{it['number']}: capacity diagnosis failed — {e}")
        return None


def _route(ctx, p, role, it, st):
    """Routing decision: returns (platform, explore, effective_size) or
    (None, False, None) when no platform is available."""
    cfg, led, name, n = ctx.cfg, ctx.led, p["name"], it["number"]
    routing_role, pin, effective_min_tier, effective_size = _route_inputs(it, role)

    # D26: route within the project's declared accounts: fallback
    # order, equal round-robin, or an explicit cross-account priority.
    platform = router.explore_for_project(
        cfg, led, p, it, routing_role, st["busy"], size=effective_size,
        scorecard_rows=getattr(ctx, "scorecard_rows", None),
        burst_lines=st["burst_lines"], min_tier=effective_min_tier)
    explore = platform is not None
    reasons = []
    if explore:
        ctx.say(f"{name}#{n}: trying {platform} ({routing_role} exploration)")
    else:
        platform, reasons = router.pick_for_project(
            cfg, led, p, routing_role, pin, st["busy"], size=effective_size,
            scorecard_rows=getattr(ctx, "scorecard_rows", None),
            burst_lines=st["burst_lines"], min_tier=effective_min_tier)
    _observe(ctx, p, role, it, *(() if platform else ("no_platform",)),
             diag=_diagnose(ctx, p, role, it, st), effective_size=effective_size,
             required_tier=effective_min_tier)
    if not platform:
        ctx.hold("no_platform", project=name, number=n, role=routing_role,
                 size=effective_size or "m", blockers=router.reason_groups(reasons))
        if routing_role == "plan":
            ctx.say(f"{name}#{n}: waits for planning (routing.plan) — {'; '.join(reasons)}")
        else:
            ctx.say(f"{name}#{n}: no platform for {role} — {'; '.join(reasons)}")
        return None, False, None
    return platform, explore, effective_size


def _account_start(cfg, platform, name, area, item_files, st):
    """Update quota-group, tier, project, area, and file occupancy after a
    successful (or dry-run) start — one place for all post-start bookkeeping."""
    st["total"] += 1
    st["in_project"][name] = st["in_project"].get(name, 0) + 1
    if area:
        st["busy_areas"][name].add(area)
    st["busy_files"][name].update(item_files)
    group = cfg["platforms"][platform].get("quota_group", platform)
    st["per_group"][group] = st["per_group"].get(group, 0) + 1
    if st["per_group"][group] >= cfg["platforms"][platform].get("max_runs", 1):
        for pname in cfg["platforms"]:
            if cfg["platforms"][pname].get("quota_group", pname) == group:
                st["busy"].add(pname)
    st["tiers"].append(router.tier_of(cfg["platforms"][platform]))
    st["busy"] |= tier_budget_busy(cfg, st["tiers"])


def schedule(ctx, projects):
    """Hand out run slots across every project at once (mahler#9).

    Order: priority first; then ready builds before inbox sorts when the
    platform a sort would choose is the last one with headroom (sorts must
    not eat a scarce builder); then priority projects (mahler work prioritized
    over other projects, all else being equal); then oldest state_changed_at.
    The ordered list is then dealt round-robin by project — each pass starts
    at most one run per project — so no project waits more than one slot
    behind another. Per-project max_parallel, per-platform max_runs, leases,
    hot hold and the settle/dependency gates all still apply.
    """
    cfg = ctx.cfg
    st = _init_schedule_state(ctx, projects)
    work = _candidates(ctx, projects)
    _order_candidates(ctx, work, st, projects)

    st["said"] = set()       # projects already told they're at capacity
    candidates = list(work)
    while work and st["total"] < cfg["concurrency"]["total"]:
        started = set()      # projects that took a slot this pass
        for cand in list(work):
            p, role, it = cand
            name, n = p["name"], it["number"]
            if not launch_health.allowed(ctx, name, n):
                _observe(ctx, p, role, it, "launch_breaker")
                work.remove(cand)
                continue
            if name in started:
                continue                    # its next item waits for the next pass
            work.remove(cand)
            if st["total"] >= cfg["concurrency"]["total"]:
                work.clear()
                break
            eligible, area, item_files = _check_eligible(ctx, p, role, it, st)
            if not eligible:
                continue
            platform, explore, effective_size = _route(ctx, p, role, it, st)
            if not platform:
                continue
            if ctx.dry_run:
                ctx.say(f"{name}#{n}: would {role} on {platform}")
            elif not start(ctx, name, it, role, platform, size=effective_size,
                           **({"explore": True} if explore else {})):
                capacity.of(ctx).add_blocker(name, n, role, "start_failed",
                                             unknown_platform=platform)
                continue
            _account_start(cfg, platform, name, area, item_files, st)
            started.add(name)
    if st["total"] >= cfg["concurrency"]["total"]:
        # The global ceiling stopped the loop: still record who was waiting on it.
        for p, role, it in candidates:
            name, n = p["name"], it["number"]
            if capacity.of(ctx).seen(name, n, role) or ctx.led.lease(name, n):
                continue
            _routing_role, _pin, required_tier, effective_size = _route_inputs(it, role)
            _observe(ctx, p, role, it, "global_slots", diag=_diagnose(ctx, p, role, it, st),
                     effective_size=effective_size, required_tier=required_tier)


def start(ctx, project, item, role, platform, handoff_from=None, size=None, context=None,
          fix_reason="ci", explore=False):
    led, pol, n = ctx.led, ctx.policy(project), item["number"]
    if not launch_health.allowed(ctx, project, n):
        return False
    ests = led.estimates()
    est = led.run_estimate(ests, platform, role, size)
    pconf = ctx.cfg["platforms"][platform]
    effort = platforms.effort_value(pconf, role) or "default"
    model = pconf.get("sort_model" if role == "sort" else "build_model") or pconf.get("model")
    routing_role = "plan" if role == "sort" and needs_plan(row_get(item, "labels")) else role
    run_id = led.create_run(project=project, number=n, role=role, platform=platform,
                            size=size or "m", model=model, configured_model=model or "", effort=effort,
                            explore=int(explore), routing_role=routing_role,
                            epoch=0, status="running", est_mins=round(est, 2))
    lease, info = led.claim(project, n, f"run:{run_id}", "auto", pol["auto_lease_minutes"],
                            platform=platform, run_id=run_id, capacity=role != "sort",
                            handoff_from=handoff_from)
    if lease is None:
        led.update_run(run_id, status="ended", outcome="not claimed", ended_at=iso(led.now()))
        if "unavailable" in info:
            ctx.say(f"{project}#{n}: canonical lease host unavailable — skipped")
        elif "at_capacity" in info:
            held = ", ".join(router.lease_label(led, row)
                             for row in info["at_capacity"])
            ctx.say(f"{project}#{n}: canonical project capacity held ({held}) — skipped")
            ctx.hold("capacity", project=project, max_parallel=pol["max_parallel"],
                     holders=[{"number": row["number"], "label": router.lease_label(led, row)}
                              for row in info["at_capacity"]])
        else:
            ctx.say(f"{project}#{n}: held by {info['held_by']['holder']} — skipped")
        return False
    launch_health.allowed(ctx, project, n, consume=True)
    prep = None
    try:
        prep = runner.prepare(ctx, project, item, role, platform, run_id)
        text = prompt.build(ctx, project, item, role, platform, prep, context=context)
        meta = runner.launch(ctx, project, item, role, platform, run_id, lease["epoch"],
                             text, prep)
    except Exception as e:                       # noqa: BLE001 — any launch failure
        if prep is not None:
            branch = prep.get("branch")
            own_branch = (branch if branch and branch.endswith(f"-r{run_id}") else None)
            try:
                runner.remove_worktree(pol["path"], prep.get("worktree"), own_branch,
                                       runner.worktree_root(pol))
            except Exception as cleanup_error:    # noqa: BLE001 — preserve launch error
                ctx.say(f"{project}#{n}: launch cleanup failed — {cleanup_error}")
        if handoff_from:
            restored, _ = led.claim(
                project, n, handoff_from[0], "auto", pol["auto_lease_minutes"],
                capacity=False, handoff_from=(f"run:{run_id}", lease["epoch"]))
            if restored is None:
                # Do not release a canonical slot we could not safely restore;
                # its short lease will expire and shipping will retry.
                ctx.say(f"{project}#{n}: couldn't restore the prior lease after launch failure")
        else:
            led.release(project, n, holder=f"run:{run_id}")
        led.update_run(run_id, status="ended", outcome=f"launch failed: {e}"[:300],
                       ended_at=iso(led.now()))
        led.event("launch_failed", project, n, str(e)[:500])
        ctx.say(f"{project}#{n}: launch failed — {e}")
        launch_health.failed(ctx, project, n, run_id, e)
        return False
    led.update_run(run_id, epoch=lease["epoch"], **meta)
    led.event("run_start", project, n, {"run": run_id, "role": role, "platform": platform})
    launch_health.succeeded(ctx, project, run_id)
    capacity.of(ctx).launched(project, n, role, run_id, platform)
    ctx.say(f"{project}#{n}: started {role} on {platform} (run {run_id})")
    if role == "build":
        led.set_state(project, n, "working", f"{platform} run {run_id}")
        try:
            ctx.gh(project).comment(n, f"▶︎ **{platform}** started work (run {run_id}) on "
                                       f"branch `{meta['branch']}`.")
        except GHError:
            pass
    elif role == "fix":
        reason = "CI was red"
        state_reason = f"{reason} on PR #{item['pr']}"
        if fix_reason == "review":
            pr_url = f"https://github.com/{pol['repo']}/pull/{item['pr']}"
            reason = ("review found blocking issues "
                      f"(see the review comment on [PR #{item['pr']}]({pr_url}))")
            state_reason = reason
        led.set_state(project, n, "working",
                      f"{platform} fix run {run_id} — {state_reason}")
        try:
            ctx.gh(project).comment(n, f"🔁 **{platform}** started a fix run (run {run_id}) on "
                                       f"branch `{meta['branch']}` — {reason}.")
        except GHError:
            pass
    return True
