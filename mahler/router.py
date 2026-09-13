"""Quota-aware routing (DESIGN D8). Deterministic — no LLM in the control plane.

A platform is usable for a new run only if every quota window has a fresh
sample under its *soft* line. Unknown or stale usage counts as over the line,
so a missing reading can never push Claude into paid extra usage. Running
work is stopped at the *hard* line (see scheduler.watchdog).
"""

from datetime import timedelta

from .ledger import parse

WINDOWS = ("5h", "weekly")   # default window set; a platform can override via pconf["windows"]
HOLD = "hold"      # pseudo-window in the usage table: resets_at = when the hold lifts


SIZES = {"s": 1, "m": 2, "l": 3}


def usage_state(led, name, pconf):
    """-> ('ok'|'soft'|'hard'|'stale', detail). Worst window wins."""
    now = led.now()
    usage = led.usage(name)
    hold = usage.pop(HOLD, None)
    if hold and parse(hold["resets_at"]) and parse(hold["resets_at"]) > now:
        # not a quota reading: the platform can't start runs right now (a run
        # sat silent at startup). Soft, so a run already making progress keeps going.
        return "soft", f"on hold until {parse(hold['resets_at']).astimezone():%H:%M} (a run never started)"
    if not pconf.get("metered", True):
        # no meter: fine unless a quota error put it in the penalty box
        for u in usage.values():
            until = parse(u["resets_at"])
            if u["used_pct"] >= 100 and until and until > now:
                return "hard", f"backing off until {until.astimezone():%H:%M}"
        return "ok", "unknown limit (platform reports no quota signal)"
    stale_after = timedelta(minutes=pconf.get("stale_minutes", 15))
    worst, detail = "ok", []
    rank = {"ok": 0, "soft": 1, "hard": 2, "stale": 3}
    for w in pconf.get("windows", WINDOWS):
        u = usage.get(w)
        if u is None:
            state = "stale"
            detail.append(f"{w}: no sample")
        else:
            resets = parse(u["resets_at"])
            if resets and resets <= now:
                state = "stale"          # window rolled over — re-probe rather than guess
                detail.append(f"{w}: reset since sample")
            elif parse(u["sampled_at"]) < now - stale_after:
                state = "stale"
                detail.append(f"{w}: sample older than {stale_after}")
            else:
                pct = u["used_pct"]
                state = ("hard" if pct >= pconf["hard"][w]
                         else "soft" if pct >= pconf["soft"][w] else "ok")
                detail.append(f"{w} {pct:.0f}%")
        if rank[state] > rank[worst]:
            worst = state
    return worst, ", ".join(detail)


def candidates(cfg, role, pin=None):
    # a fix run routes like a build (DESIGN D18): same platforms, same order
    order = [pin] if pin else cfg["routing"].get(role) or cfg["routing"]["build"]
    return [n for n in order if n in cfg["platforms"] and cfg["platforms"][n].get("enabled")]


def pick(cfg, led, role, pin=None, busy=(), size=None):
    """First platform in routing order with headroom. -> (name|None, reasons)."""
    reasons = []
    for name in candidates(cfg, role, pin):
        if name in busy:
            reasons.append(f"{name}: busy")
            continue
        limit = cfg["platforms"][name].get("max_size")
        if limit and not pin and SIZES.get(size or "m", 2) > SIZES[limit]:
            reasons.append(f"{name}: only takes size:{limit}")
            continue
        min_limit = cfg["platforms"][name].get("min_size")
        if min_limit and not pin and SIZES.get(size or "m", 2) < SIZES[min_limit]:
            reasons.append(f"{name}: requires size:{min_limit}+")
            continue
        state, detail = usage_state(led, name, cfg["platforms"][name])
        if state == "ok":
            return name, reasons
        reasons.append(f"{name}: {state} ({detail})")
    return None, reasons
