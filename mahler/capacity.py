"""Compact queue and platform capacity history (mahler#734, DESIGN D18/D26).

`schedule` and the shipping pass hand the observer the work that is waiting;
`flush` folds that into *state intervals* in the ledger's `capacity_intervals`
table, so an unchanged wait costs no new rows, only a later `last_seen`.

Schema (`capacity_intervals`, one row per interval)
    project, number, role        the waiting item and what it waits to run:
                                 sort | build | fix | review
    routing_role                 plan for a sort that routes through routing.plan
    size, effective_size         the item's size label, and the size routing used
                                 (size:l fixes and tier>=2 size:s escalate to m)
    required_tier                escalation tier a platform must reach (0 = none)
    first_seen, last_seen        UTC ISO; the observed span. Time after last_seen
                                 is unobserved (daemon down, item gone) and is
                                 never waiting time. Retention clamps first_seen.
    open, end_reason             open=1 while still observed. end_reason:
                                 changed | gone | gap | paused | launched
    signature                    hash of the coalescing key (role, sizes, tier,
                                 blockers, per-platform reason codes)
    blockers                     JSON list of item-level blocker codes
    platforms                    JSON {platform: {reasons, account, quota_group,
                                 tier, kind, cost, max_size, min_size}} — config
                                 snapshotted at observation time, so later config
                                 changes cannot rewrite history
    launch_run_id, launched_platform
                                 set only after `start` succeeded

Item blockers: settling, deps, paused, project_slots (project at max_parallel),
in_flight (finished change not merged), hot_hold, area, files, lease_host,
launch_breaker, global_slots (concurrency.total reached), no_platform (routing
found none), start_failed (launch was attempted and failed), shipping_wait
(a persisted fix/review wait in the shipping pass).

Platform reason codes (router.diagnose; several may apply to one platform):
eligible, launched, disabled, unavailable, account, pin, route, excluded,
slots_busy, tier, size, peak, quota, no_credit, hold, unknown. `unknown` is
stale or unreadable usage and a failed start's chosen platform; `hold` is a
non-credit hold. Neither is capacity that quota alone withheld: a reporter
should count only intervals whose every blocker on a free platform is quota or
no_credit. Item blockers other than no_platform/global_slots/shipping_wait are
structural and are not capacity.

No logs, prompts or credentials are stored. Retention is a rolling 90 days.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json

from . import router
from .config import account_of
from .ledger import iso, parse, row_get

RETENTION_DAYS = 90
GAP_SECONDS = 120

ROLES = ("sort", "build", "fix", "review")


def _platform_meta(cfg, name, reasons):
    pc = cfg["platforms"][name]
    return {"reasons": sorted(reasons), "account": account_of(pc),
            "quota_group": pc.get("quota_group", name), "tier": router.tier_of(pc),
            "kind": pc.get("kind"), "cost": router.cost_class(pc),
            "max_size": pc.get("max_size"), "min_size": pc.get("min_size")}


def _signature(obs):
    key = {"role": obs["role"], "routing_role": obs.get("routing_role"),
           "effective_size": obs.get("effective_size"), "size": obs.get("size"),
           "tier": obs.get("required_tier") or 0, "blockers": obs.get("blockers") or [],
           "platforms": {n: p["reasons"] for n, p in (obs.get("platforms") or {}).items()}}
    return hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()


class Observer:
    """One tick's observations. Never raises into the caller."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.obs = {}

    def _enabled(self):
        return not getattr(self.ctx, "dry_run", False)

    def seen(self, project, number, role):
        return (project, number, role) in self.obs

    def observe(self, project, number, role, *, blockers=(), diag=None, routing_role=None,
                size=None, effective_size=None, required_tier=0):
        if not self._enabled():
            return
        try:
            old = self.obs.get((project, number, role), {})
            platforms = ({n: _platform_meta(self.ctx.cfg, n, r) for n, r in diag.items()}
                         if diag is not None else old.get("platforms") or {})
            merged = sorted(set(blockers) | set(old.get("blockers") or []))
            self.obs[(project, number, role)] = {
                "project": project, "number": number, "role": role,
                "routing_role": routing_role or old.get("routing_role"),
                "size": size or old.get("size"),
                "effective_size": effective_size or old.get("effective_size"),
                "required_tier": required_tier or old.get("required_tier") or 0,
                "blockers": merged, "platforms": platforms}
        except Exception as e:                              # noqa: BLE001 — telemetry only
            self.ctx.say(f"capacity: couldn't record observation — {e}")

    def add_blocker(self, project, number, role, blocker, unknown_platform=None):
        entry = self.obs.get((project, number, role))
        if entry is None or not self._enabled():
            return
        if blocker not in entry["blockers"]:
            entry["blockers"] = sorted(entry["blockers"] + [blocker])
        if unknown_platform and unknown_platform in entry["platforms"]:
            entry["platforms"][unknown_platform]["reasons"] = ["unknown"]

    def launched(self, project, number, role, run_id, platform):
        """Only called once `start` succeeded."""
        if not self._enabled():
            return
        try:
            entry = self.obs.setdefault((project, number, role), {
                "project": project, "number": number, "role": role, "blockers": [],
                "platforms": {}})
            entry["blockers"] = []
            entry["launch_run_id"], entry["launched_platform"] = run_id, platform
            if platform in entry["platforms"]:
                entry["platforms"][platform]["reasons"] = ["launched"]
        except Exception as e:                              # noqa: BLE001 — telemetry only
            self.ctx.say(f"capacity: couldn't record launch — {e}")

    def observe_paused(self, projects):
        """A paused tick still sees the queue: everything waits on the pause."""
        if not self._enabled():
            return
        try:
            for p in projects:
                for state, role in (("inbox", "sort"), ("ready", "build")):
                    for it in self.ctx.led.items(p["name"], [state]):
                        self.observe(p["name"], it["number"], role, blockers=["paused"])
                for it in self.ctx.led.items(p["name"], ["verifying"]):
                    n = it["number"]
                    for role, prefix in (("fix", "reviewfix-status"), ("review", "review-wait")):
                        wait_val = self.ctx.led.get_kv(f"{prefix}:{p['name']}#{n}")
                        if not wait_val:
                            continue
                        try:
                            status = json.loads(wait_val)
                        except Exception:
                            continue
                        if status.get("state") == "capacity_wait":
                            req_tier = status.get("tier") or 0
                            size = next((l.split(":", 1)[1]
                                         for l in json.loads(row_get(it, "labels", "[]"))
                                         if l.startswith("size:")), None)
                            effective_size = size
                            if role == "fix" and (size == "l" or (req_tier >= 2 and size == "s")):
                                effective_size = "m"
                            self.observe(p["name"], n, role, blockers=["paused"],
                                         size=size, effective_size=effective_size,
                                         required_tier=req_tier)
        except Exception as e:                              # noqa: BLE001 — telemetry only
            self.ctx.say(f"capacity: couldn't observe paused queue — {e}")

    def flush(self):
        """Fold this tick into the ledger and prune; isolated from the tick."""
        if not self._enabled():
            return
        try:
            for entry in self.obs.values():
                entry["signature"] = _signature(entry)
            led = self.ctx.led
            led.capacity_record(self.obs, gap_seconds=GAP_SECONDS)
            led.capacity_prune(RETENTION_DAYS)
        except Exception as e:                              # noqa: BLE001 — never block a tick
            self.ctx.say(f"couldn't record capacity history — {e}")
        finally:
            self.obs = {}


class _Null:
    """Stand-in for a ctx built without an observer (partial test contexts)."""

    def __getattr__(self, _name):
        return lambda *a, **k: None

    def seen(self, *_a):
        return False


_NULL = _Null()


def of(ctx):
    return getattr(ctx, "capacity", None) or _NULL


def parse_days(value):
    try:
        val = int(value)
    except (ValueError, TypeError):
        raise ValueError(f"invalid days '{value}': must be an integer between 1 and 90")
    if not (1 <= val <= 90):
        raise ValueError(f"invalid days '{value}': must be an integer between 1 and 90")
    return val


def capacity_report(led, *, days=30, now=None):
    """Historical free-capacity report (mahler#736). Read-only analysis.

    Returns structured data for the requested UTC window and recorded coverage.
    """
    days = parse_days(days)
    if now is None:
        now = led.now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    window_end = now
    window_start = now - timedelta(days=days)

    # Coverage inspection
    cov_row = led.q("SELECT MIN(first_seen) AS earliest, MAX(last_seen) AS latest FROM capacity_intervals")
    earliest_str = cov_row[0]["earliest"] if cov_row and cov_row[0]["earliest"] else None
    latest_str = cov_row[0]["latest"] if cov_row and cov_row[0]["latest"] else None

    if earliest_str is None or latest_str is None:
        coverage = {
            "status": "no_data",
            "days": 0.0,
            "earliest": None,
            "latest": None,
        }
    else:
        db_earliest = parse(earliest_str)
        db_latest = parse(latest_str)
        cov_start = max(db_earliest, window_start)
        cov_end = min(db_latest, window_end)
        if cov_start >= cov_end:
            coverage = {
                "status": "no_data",
                "days": 0.0,
                "earliest": earliest_str,
                "latest": latest_str,
            }
        else:
            cov_days = (cov_end - cov_start).total_seconds() / 86400.0
            cov_status = "partial" if db_earliest > window_start else "complete"
            coverage = {
                "status": cov_status,
                "days": cov_days,
                "earliest": earliest_str,
                "latest": latest_str,
            }

    # Query all intervals
    intervals = [dict(r) for r in led.q("SELECT * FROM capacity_intervals ORDER BY first_seen, id")]

    # Group intervals by (project, number, role) into episodes
    by_item = defaultdict(list)
    for r in intervals:
        by_item[(r["project"], r["number"], r["role"])].append(r)

    all_episodes = []
    for key, item_rows in by_item.items():
        current_ep = []
        for r in item_rows:
            if not current_ep:
                current_ep.append(r)
            else:
                prev = current_ep[-1]
                gap = (parse(r["first_seen"]) - parse(prev["last_seen"])).total_seconds()
                if prev["end_reason"] == "changed" and gap <= GAP_SECONDS:
                    current_ep.append(r)
                else:
                    all_episodes.append(current_ep)
                    current_ep = [r]
        if current_ep:
            all_episodes.append(current_ep)

    # Process episodes and qualifying intervals
    tier_wall_intervals = defaultdict(list)
    tier_qualifying_episode_indices = defaultdict(set)
    observed_tiers = set()

    for ep_idx, ep in enumerate(all_episodes):
        for r in ep:
            r_start = parse(r["first_seen"])
            r_end = parse(r["last_seen"])
            # Bound intervals to the requested UTC window
            if r_end <= window_start or r_start >= window_end:
                continue

            # Qualify: no item-level blockers
            blockers = json.loads(r["blockers"] or "[]")
            if set(blockers) - {"no_platform", "shipping_wait"}:
                continue

            # Qualify: no eligible routed platform
            platforms_dict = json.loads(r["platforms"] or "{}")
            if any("eligible" in pmeta.get("reasons", []) for pmeta in platforms_dict.values()):
                continue

            # Qualify: at least one free platform whose complete blocker set is a nonempty subset of quota/no_credit
            exhausted_tiers = set()
            for pname, pmeta in platforms_dict.items():
                if pmeta.get("cost") == "free":
                    reasons = set(pmeta.get("reasons", []))
                    if reasons and reasons.issubset({"quota", "no_credit"}):
                        exhausted_tiers.add(pmeta.get("tier", 0))

            if not exhausted_tiers:
                continue

            # Interval qualifies!
            clip_s = max(r_start, window_start)
            clip_e = min(r_end, window_end)
            if clip_e > clip_s:
                for t in exhausted_tiers:
                    tier_wall_intervals[t].append((clip_s, clip_e))
                    tier_qualifying_episode_indices[t].add(ep_idx)
                    observed_tiers.add(t)

    # Calculate union wall-clock time and linked distinct launch runs per tier
    tiers_summary = {}
    for t in sorted(observed_tiers):
        # Union intervals
        merged = []
        for s, e in sorted(tier_wall_intervals[t], key=lambda x: (x[0], x[1])):
            if not merged or s > merged[-1][1]:
                merged.append([s, e])
            else:
                merged[-1][1] = max(merged[-1][1], e)
        union_secs = sum((e - s).total_seconds() for s, e in merged)
        hours = union_secs / 3600.0

        # Episodes accounting
        distinct_run_ids = set()
        still_waiting = 0
        for ep_idx in tier_qualifying_episode_indices[t]:
            ep = all_episodes[ep_idx]
            ep_launch_run_id = None
            for r in reversed(ep):
                if r.get("launch_run_id") is not None:
                    ep_launch_run_id = r["launch_run_id"]
                    break
            if ep_launch_run_id is not None:
                distinct_run_ids.add(ep_launch_run_id)
            elif ep[-1]["open"]:
                still_waiting += 1

        tiers_summary[t] = {
            "hours": hours,
            "runs": len(distinct_run_ids),
            "run_ids": sorted(distinct_run_ids),
            "still_waiting": still_waiting,
        }

    # Step 4: Claude/paid diversions
    diversions_by_run = {}
    for r in intervals:
        run_id = r.get("launch_run_id")
        if run_id is None:
            continue
        r_last = parse(r["last_seen"])
        if r_last < window_start or r_last > window_end:
            continue
        launched_plat = r.get("launched_platform")
        plats_dict = json.loads(r["platforms"] or "{}")
        launched_meta = plats_dict.get(launched_plat)
        is_claude_or_paid = (
            launched_meta is not None
            and (launched_meta.get("cost") == "paid" or launched_meta.get("kind") == "claude")
        )
        if not is_claude_or_paid:
            continue

        # Look for free candidate available solely except for quota/no_credit
        blocked_free = []
        for pname, pmeta in plats_dict.items():
            if pmeta.get("cost") == "free":
                reasons = set(pmeta.get("reasons", []))
                if reasons and reasons.issubset({"quota", "no_credit"}):
                    blocked_free.append((pname, pmeta.get("tier", 0)))
        if blocked_free and run_id not in diversions_by_run:
            diversions_by_run[run_id] = {
                "run_id": run_id,
                "project": r["project"],
                "number": r["number"],
                "platform": launched_plat,
                "blocked_candidates": blocked_free,
            }

    # Step 5: Request accounting aggregation
    runs = [dict(r) for r in led.q(
        "SELECT * FROM runs WHERE (started_at <= ? AND (ended_at >= ? OR ended_at IS NULL)) ORDER BY started_at, id",
        (iso(window_end), iso(window_start))
    )]

    daily_requests = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    limits_seen = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: {"rate_limited": False, "limit_hit": False})))
    platform_limits = defaultdict(lambda: {"rate_limited": False, "limit_hit": False})
    unknown_day_counts = defaultdict(int)
    unknown_model_counts = defaultdict(int)
    partial_counts = defaultdict(int)
    unsupported_platforms = set()
    unsupported_stream_runs = defaultdict(int)

    for r in runs:
        platform = r.get("platform") or "unknown"
        coverage_type = r.get("request_coverage")
        requests_count = r.get("requests")
        buckets_json = r.get("request_buckets")
        rate_limited = bool(r.get("rate_limited"))
        limit_hit = bool(r.get("limit_hit"))

        if rate_limited:
            platform_limits[platform]["rate_limited"] = True
        if limit_hit:
            platform_limits[platform]["limit_hit"] = True

        if requests_count is None and not buckets_json:
            if "cline" in platform or "kilo" in platform:
                unsupported_platforms.add(platform)
                unsupported_stream_runs[platform] += 1
            continue

        if buckets_json:
            try:
                buckets = json.loads(buckets_json)
            except Exception:
                buckets = {}
            for day_str, model_dict in buckets.items():
                for model_name, count in model_dict.items():
                    if coverage_type == "partial":
                        partial_counts[platform] += count
                    elif day_str == "unknown":
                        unknown_day_counts[platform] += count
                    elif model_name == "unknown":
                        unknown_model_counts[platform] += count
                    else:
                        try:
                            day_date = datetime.strptime(day_str, "%Y-%m-%d").date()
                        except ValueError:
                            unknown_day_counts[platform] += count
                            continue
                        if window_start.date() <= day_date <= window_end.date():
                            daily_requests[platform][model_name][day_str] += count
                            if rate_limited:
                                limits_seen[platform][model_name][day_str]["rate_limited"] = True
                            if limit_hit:
                                limits_seen[platform][model_name][day_str]["limit_hit"] = True
        elif requests_count is not None:
            if coverage_type == "partial":
                partial_counts[platform] += requests_count
            elif requests_count > 0:
                unknown_day_counts[platform] += requests_count

    peaks = []
    for platform in sorted(daily_requests.keys()):
        for model in sorted(daily_requests[platform].keys()):
            days_dict = daily_requests[platform][model]
            if not days_dict:
                continue
            peak_day = max(days_dict.keys(), key=lambda d: (days_dict[d], d))
            peak_count = days_dict[peak_day]
            r_lim = limits_seen[platform][model][peak_day]["rate_limited"] or platform_limits[platform]["rate_limited"]
            l_hit = limits_seen[platform][model][peak_day]["limit_hit"] or platform_limits[platform]["limit_hit"]
            peaks.append({
                "platform": platform,
                "model": model,
                "peak_requests": peak_count,
                "peak_date": peak_day,
                "rate_limited": r_lim,
                "limit_hit": l_hit,
            })

    completely_unsupported = sorted(unsupported_platforms - set(daily_requests.keys()) - set(unknown_day_counts.keys()) - set(partial_counts.keys()))

    return {
        "days": days,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "coverage": coverage,
        "tiers": tiers_summary,
        "diversions": list(diversions_by_run.values()),
        "requests": {
            "peaks": peaks,
            "unknown_day": dict(unknown_day_counts),
            "unknown_model": dict(unknown_model_counts),
            "partial": dict(partial_counts),
            "unsupported": completely_unsupported,
            "unsupported_streams": dict(unsupported_stream_runs),
        },
    }


def format_capacity_report(report):
    lines = []
    days = report["days"]
    w_start = report["window_start"][:19] + "Z"
    w_end = report["window_end"][:19] + "Z"
    lines.append(f"Mahler Free Capacity Report (last {days} days)")
    lines.append(f"Window: {w_start} to {w_end}")

    cov = report["coverage"]
    if cov["status"] == "no_data":
        lines.append("Coverage: no recorded capacity history in this window.")
    elif cov["status"] == "partial":
        earliest_stamp = cov["earliest"][:19] + "Z" if cov["earliest"] else "unknown"
        lines.append(f"Coverage: {cov['days']:.1f} days (partial; recorded history begins {earliest_stamp})")
    else:
        lines.append(f"Coverage: {days} days (complete)")

    lines.append("")
    lines.append("Summary:")
    if not report["tiers"]:
        lines.append("  No free-platform capacity wait observed.")
    else:
        for t in sorted(report["tiers"].keys()):
            tinfo = report["tiers"][t]
            h = tinfo["hours"]
            r = tinfo["runs"]
            run_str = f"{r} run" if r == 1 else f"{r} runs"
            line = f"  an extra free pool at tier {t} would have been usable for {h:.1f} h / {run_str} in the last {days} days"
            if tinfo["still_waiting"] > 0:
                sw = tinfo["still_waiting"]
                ep_str = "episode" if sw == 1 else "episodes"
                line += f" (plus {sw} still-waiting {ep_str})"
            lines.append(line)

    lines.append("")
    lines.append("Qualification:")
    lines.append("  Observed opportunity only; not guaranteed throughput or a purchase recommendation.")
    lines.append("  X is observed union wall time with replaceable quota/credit blockers, not predicted agent runtime.")
    lines.append("  Y is linked observed launches, not how many hypothetical runs would finish.")
    lines.append("  A replacement pool is assumed to match the exhausted candidate’s account eligibility, role and size support.")

    lines.append("")
    lines.append("Claude/paid diversions:")
    if not report["diversions"]:
        lines.append("  None observed.")
    else:
        for d in report["diversions"]:
            cand_str = ", ".join(f"{name} (tier {tier})" for name, tier in d["blocked_candidates"])
            lines.append(f"  run {d['run_id']}: {d['project']}#{d['number']} launched on {d['platform']}; blocked free candidate: {cand_str}")

    lines.append("")
    lines.append("Request accounting (UTC daily peaks):")
    req = report["requests"]
    has_req_output = False
    if req["peaks"]:
        has_req_output = True
        for p in req["peaks"]:
            lim_str = "429 observed" if p["rate_limited"] else ("limit hit" if p["limit_hit"] else "none observed")
            lines.append(f"  {p['platform']} / {p['model']}: peak {p['peak_requests']} requests on {p['peak_date']} (limit evidence: {lim_str})")

    if req["unknown_day"]:
        has_req_output = True
        for plat, count in sorted(req["unknown_day"].items()):
            lines.append(f"  {plat}: {count} requests with unknown date (undated totals excluded from daily peak)")

    if req["unknown_model"]:
        has_req_output = True
        for plat, count in sorted(req["unknown_model"].items()):
            lines.append(f"  {plat}: {count} requests with unknown model")

    if req["partial"]:
        has_req_output = True
        for plat, count in sorted(req["partial"].items()):
            lines.append(f"  {plat}: {count} requests with partial coverage")

    for plat, stream_count in sorted(req.get("unsupported_streams", {}).items()):
        has_req_output = True
        lines.append(f"  {plat}: {stream_count} run(s) with unsupported request stream (requests: unknown)")

    for plat in req["unsupported"]:
        if plat not in req.get("unsupported_streams", {}):
            has_req_output = True
            lines.append(f"  {plat}: unknown (no request accounting or unsupported stream)")

    if not has_req_output:
        lines.append("  No request accounting recorded in this window.")

    return "\n".join(lines)
