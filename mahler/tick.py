"""The tick's own work: due maintenance, lease expiry, and handing out slots.

Everything here is decided from the ledger alone — which items could run,
which platform can afford them, and what to start. The passes that react to
the outside world live in watchdog.py, sync.py, finalize.py and ship.py.
"""

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import config, launch_health, platforms, presence, prompt, router, runner
from .gh import GHError, dependency_target
from .ledger import iso, parse, row_get
from .usage import compute_burst


MAINTENANCE_TEXT = {
    "security": ("Security & Surface Area Audit", "Security & Surface Area Audit — credential boundaries (`.env` leaks, unpooled URLs), subshell executions (`subprocess.run` argument sanitization), permission boundaries, denial list checks, dependency scans."),
    "health": ("Codebase Health & Refactoring Pass", "Codebase Health & Refactoring Pass — unclosed resource leaks (DB connections, file descriptors), dead code / orphaned helpers, cyclomatic complexity hotspots."),
    "drift": ("Architecture & Specification Drift Audit", "Architecture & Specification Drift Audit — comparing implementation against `DESIGN.md` / `ARCHITECTURE.md` / `ROADMAP.md`, cleaning up zombie abstractions."),
    "tests": ("Test Suite Health & Flakiness Audit", "Test Suite Health & Flakiness Audit — test isolation, false-green tests, execution time creep, `ResourceWarning` checks."),
    "token-economy": ("Token Economy/Quota & Performance Hygiene", "Token Economy/Quota & Performance Hygiene — prompt context bloat in recipes/rules, run duration outliers, excessive polling overhead, DB query efficiency."),
    "guidance": ("Agent Guidance & Rule Calibration", "Agent Guidance & Rule Calibration — reviewing `AGENTS.md` / `CLAUDE.md` / `recipes` against observed failure modes, pruning obsolete instructions."),
    "backlog": ("Issue Backlog Pruning Pass", "Issue Backlog Pruning Pass — parent/goal issues whose sub-issues are all closed but the parent itself wasn't, issues superseded by a later split or refactor (check against current module boundaries, not the description text), duplicate or overlapping issues covering the same ground, and stale mahler:parked items worth reviving or closing."),
    "bugs": ("Correctness Bug Scan", "Correctness Bug Scan — logic errors, off-by-one and boundary conditions, incorrect error handling or silently-swallowed exceptions, race conditions between concurrent runs, and edge cases (None/empty/malformed input) found by close reading or targeted tests. Not style, structure or refactoring — that's the health pass."),
    "docs": ("Documentation Accuracy & Onboarding Review", "Documentation Accuracy & Onboarding Review — verify README setup and safety guidance from a fresh-user perspective; compare commands, examples, architecture, roadmap status, and operator/agent documentation with the current code and CLI help; fix stale claims, broken links, machine-specific examples, and undocumented configuration or behavior. Preserve historical decisions as history, but clearly distinguish shipped behavior from plans."),
}


def queue_maintenance(ctx, projects):
    """File due maintenance passes as issues (deduped)."""
    led, now = ctx.led, ctx.led.now()
    for p in projects:
        pol = config.maintenance_policy(ctx.cfg, p["name"])
        if not pol["enabled"] or p["name"] in ctx.passes_filed:
            continue
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
        # and exact/normalized MAINTENANCE_TEXT titles. Free-form manual audit
        # titles with different wording (e.g. #57 vs #81) are intentionally not
        # heuristically guessed here to avoid false positives against unrelated
        # type:goal / feature items; manual audits should use the canonical pass
        # title or carry a pass:<name> label.
        pass_titles = {text[0].strip().lower() for text in MAINTENANCE_TEXT.values()}
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
            pass_title = MAINTENANCE_TEXT[pass_name][0].strip().lower()
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
            
            title, body = MAINTENANCE_TEXT[pass_name]
            issue_labels = ["type:chore", "size:l", "p2", label]
            if scope_label is not None:
                issue_labels.append(scope_label)
            
            ctx.say(f"{p['name']}: queuing {pass_name} pass")
            if not ctx.dry_run:
                try:
                    ctx.gh(p["name"]).ensure_pass_label(pass_name)
                    ctx.gh(p["name"]).create_issue(title, body, issue_labels)
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


def _candidates(ctx, projects):
    """One global candidate list of (pol, role, item) across all enabled
    projects — sorts and settled builds compete for the same slots (mahler#9)."""
    led = ctx.led
    work = []
    enabled = config.enabled_projects(ctx.cfg)
    done = {(p["name"], i["number"]) for p in enabled
            for i in led.items(p["name"], ["done"])}
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
                continue
            deps = [d for d in json.loads(it["depends"] or "[]")
                    if dependency_target(d, name, enabled) not in done]
            if deps:
                ctx.hold("deps", project=name, number=it["number"], on=deps)
                continue
            work.append((p, "build", it))
    return work


def _headroom(ctx, role, per_platform, busy, burst_lines=None, account=config.DEFAULT_ACCOUNT):
    """Routing platforms for `role` that could take a new run right now:
    enabled, under per-platform max_runs, reachable and under its soft lines.
    Burst lines (D23) raise Claude's soft lines when a window is about to reset.
    The peak window (D22) removes Claude platforms from headroom entirely while
    it's active, so the `sorts_wait` logic stays right: a sort that would pick
    Claude must not be counted as having a free builder available.
    """
    cfg, led = ctx.cfg, ctx.led
    peak_active, _ = router.peak_state(cfg, led)
    free = []
    for name in router.candidates(cfg, role, burst_lines=burst_lines, account=account):
        pc = cfg["platforms"][name]
        if name in busy or per_platform.get(name, 0) >= pc.get("max_runs", 1):
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
    led, cfg = ctx.led, ctx.cfg
    active = led.active_runs()
    total = len(active)
    in_project, per_platform = {}, {}
    for r in active:
        in_project[r["project"]] = in_project.get(r["project"], 0) + 1
        per_platform[r["platform"]] = per_platform.get(r["platform"], 0) + 1
    busy = busy_platforms(cfg, active)
    # concurrency.by_tier (mahler#200): a finer-grained cap layered under
    # `total` — tracked as a running list of tiers so it can be recomputed
    # after each start this pass, same as the quota-group busy set below.
    tiers = [router.tier_of(cfg["platforms"][r["platform"]])
             for r in active if r["platform"] in cfg["platforms"]]
    busy |= tier_budget_busy(cfg, tiers)

    # area: label collision (D6): items sharing an area within a project
    # aren't run concurrently. File paths are project-local too (mahler#231).
    # Seed busy_areas from what's currently running...
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
            hot[p["name"]] = bool(last and led.now() - last < timedelta(minutes=p["hot_hold_minutes"]))

    # Burst before a Claude window resets (D23): in the last lead-time before a
    # window rolls over, Claude's reserve expires unused, so burst lines let
    # Claude build first with higher headroom. Never while you're using Claude.
    burst_lines = compute_burst(ctx, projects)

    work = _candidates(ctx, projects)
    # each account has its own builders, so "a sort must not eat the last
    # builder" is judged per account (D25); a multi-account project competes
    # in every bucket it can draw from (D26)
    buckets = {acct for p in projects for acct in config.accounts_of(p)}
    sorts_wait = {acct: len(_headroom(ctx, "sort", per_platform, busy, burst_lines, acct)) <= 1
                  for acct in buckets}
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

    said = set()             # projects already told they're at capacity
    while work and total < cfg["concurrency"]["total"]:
        started = set()      # projects that took a slot this pass
        for cand in list(work):
            p, role, it = cand
            name, n = p["name"], it["number"]
            if not launch_health.allowed(ctx, name, n):
                work.remove(cand)
                continue
            if name in started:
                continue                    # its next item waits for the next pass
            work.remove(cand)
            if total >= cfg["concurrency"]["total"]:
                work.clear()
                break
            if in_project.get(name, 0) >= p["max_parallel"]:
                if name not in said:
                    said.add(name)
                    ctx.say(f"{name}: at capacity ({total} running)")
                    ctx.hold("capacity", project=name, max_parallel=p["max_parallel"])
                continue
            if role == "build" and in_project.get(name, 0) + in_flight[name] >= p["max_parallel"]:
                if name not in said:
                    said.add(name)
                    ctx.say(f"{name}: builds wait — {in_flight[name]} finished change(s) "
                            "not merged yet")
                    ctx.hold("slot", project=name, verifying=[
                        v["number"] for v in led.items(name, ["verifying"])])
                continue
            # Hot hold (D6 layer 2): no new *code-writing* starts while an
            # untracked Claude session is active in the project — running work
            # continues, and the hold lifts hot_hold_minutes after the last
            # detected edit (transcript mtime if unreadable). Sorts are read-only triage (no worktree, no
            # files) and fixes repair an item already in flight, so neither
            # competes with the human's work; only builds are gated.
            if role == "build" and hot[name]:
                ctx.say(f"{name}#{n}: hot hold — a Claude session is active in this project")
                ctx.hold("hot_hold", project=name, number=n)
                continue
            area = area_of(row_get(it, "labels", "[]")) if role in ("build", "fix") else None
            if area and area in busy_areas[name]:
                ctx.say(f"{name}#{n}: waiting — area:{area} already in progress")
                ctx.hold("area", project=name, number=n, area=area)
                continue
            item_files = files_of(row_get(it, "files", "[]")) if role in ("build", "fix") else []
            file_overlap = busy_files[name].intersection(item_files)
            if file_overlap:
                ctx.say(f"{name}#{n}: waiting — files already in progress: "
                        f"{', '.join(sorted(file_overlap))}")
                ctx.hold("files", project=name, number=n, files=sorted(file_overlap))
                continue
            if led.lease(name, n):
                continue
            remote_error = getattr(led, "remote_error", lambda _project: None)(name)
            if remote_error:
                ctx.say(f"{name}: canonical lease host unavailable — project skipped this tick")
                ctx.hold("lease_host", project=name)
                continue
            size = next((l.split(":", 1)[1] for l in json.loads(row_get(it, "labels", "[]"))
                         if l.startswith("size:")), None)
            if role == "sort" and needs_plan(row_get(it, "labels", "[]")):
                routing_role = "plan"
            else:
                routing_role = role
            pin = it["pin"] if role in ("build", "fix", "sort") else None

            effective_min_tier = max(row_get(it, "esc_tier", 0), router.risk_min_tier(row_get(it, "title", ""))) if role in ("build", "fix") else 0
            effective_size = size
            if role in ("build", "fix") and effective_min_tier >= 2 and effective_size == "s":
                effective_size = "m"

            # D26: route within the project's declared accounts: fallback
            # order, equal round-robin, or an explicit cross-account priority.
            platform, reasons = router.pick_for_project(
                cfg, led, p, routing_role, pin, busy, size=effective_size,
                burst_lines=burst_lines, min_tier=effective_min_tier)
            if not platform:
                ctx.hold("no_platform", project=name, number=n, role=routing_role,
                         size=effective_size or "m", blockers=router.reason_groups(reasons))
                if routing_role == "plan":
                    ctx.say(f"{name}#{n}: waits for planning (routing.plan) — {'; '.join(reasons)}")
                else:
                    ctx.say(f"{name}#{n}: no platform for {role} — {'; '.join(reasons)}")
                continue
            if ctx.dry_run:
                ctx.say(f"{name}#{n}: would {role} on {platform}")
            elif not start(ctx, name, it, role, platform, size=effective_size):
                continue
            total += 1
            in_project[name] = in_project.get(name, 0) + 1
            per_platform[platform] = per_platform.get(platform, 0) + 1
            if area:
                busy_areas[name].add(area)
            busy_files[name].update(item_files)
            group = cfg["platforms"][platform].get("quota_group", platform)
            if per_platform[platform] >= cfg["platforms"][platform].get("max_runs", 1):
                for p in cfg["platforms"]:
                    if cfg["platforms"][p].get("quota_group", p) == group:
                        busy.add(p)
            tiers.append(router.tier_of(cfg["platforms"][platform]))
            busy |= tier_budget_busy(cfg, tiers)
            started.add(name)


def start(ctx, project, item, role, platform, handoff_from=None, size=None, context=None):
    led, pol, n = ctx.led, ctx.policy(project), item["number"]
    if not launch_health.allowed(ctx, project, n):
        return False
    ests = led.estimates()
    est = led.run_estimate(ests, platform, role, size)
    pconf = ctx.cfg["platforms"][platform]
    effort = platforms.effort_value(pconf, role) or "default"
    model = pconf.get("sort_model" if role == "sort" else "build_model") or pconf.get("model")
    run_id = led.create_run(project=project, number=n, role=role, platform=platform, size=size,
                            model=model, effort=effort, epoch=0, status="running", est_mins=round(est, 2))
    lease, info = led.claim(project, n, f"run:{run_id}", "auto", pol["auto_lease_minutes"],
                            platform=platform, run_id=run_id, capacity=role != "sort",
                            handoff_from=handoff_from)
    if lease is None:
        led.update_run(run_id, status="ended", outcome="not claimed", ended_at=iso(led.now()))
        if "unavailable" in info:
            ctx.say(f"{project}#{n}: canonical lease host unavailable — skipped")
        elif "at_capacity" in info:
            held = ", ".join(f"#{row['number']} by {row['holder']}"
                             for row in info["at_capacity"])
            ctx.say(f"{project}#{n}: canonical project capacity held ({held}) — skipped")
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
                handoff_from=(f"run:{run_id}", lease["epoch"]))
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
    ctx.say(f"{project}#{n}: started {role} on {platform} (run {run_id})")
    if role == "build":
        led.set_state(project, n, "working", f"{platform} run {run_id}")
        try:
            ctx.gh(project).comment(n, f"▶︎ **{platform}** started work (run {run_id}) on "
                                       f"branch `{meta['branch']}`.")
        except GHError:
            pass
    elif role == "fix":
        led.set_state(project, n, "working",
                      f"{platform} fix run {run_id} — CI red on PR #{item['pr']}")
        try:
            ctx.gh(project).comment(n, f"🔁 **{platform}** started a fix run (run {run_id}) on "
                                       f"branch `{meta['branch']}` — CI was red.")
        except GHError:
            pass
    return True
