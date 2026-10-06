"""Compact queue/platform capacity history (mahler#734, part of #733).

Measurement only: nothing here changes which platform a run gets. Each tick,
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

import hashlib
import json

from . import router
from .config import account_of

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
