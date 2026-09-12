"""Quota-aware routing (DESIGN D8). Deterministic — no LLM in the control plane.

A platform is usable for a new run only if every quota window has a fresh
sample under its *soft* line. Unknown or stale usage counts as over the line,
so a missing reading can never push Claude into paid extra usage. Running
work is stopped at the *hard* line (see scheduler.watchdog).
"""

from datetime import timedelta

from .ledger import parse

WINDOWS = ("5h", "weekly")


def usage_state(led, name, pconf):
    """-> ('ok'|'soft'|'hard'|'stale', detail). Worst window wins."""
    now = led.now()
    usage = led.usage(name)
    stale_after = timedelta(minutes=pconf.get("stale_minutes", 15))
    worst, detail = "ok", []
    rank = {"ok": 0, "soft": 1, "hard": 2, "stale": 3}
    for w in WINDOWS:
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
    order = [pin] if pin else cfg["routing"][role]
    return [n for n in order if n in cfg["platforms"] and cfg["platforms"][n].get("enabled")]


def pick(cfg, led, role, pin=None, busy=()):
    """First platform in routing order with headroom. -> (name|None, reasons)."""
    reasons = []
    for name in candidates(cfg, role, pin):
        if name in busy:
            reasons.append(f"{name}: busy")
            continue
        state, detail = usage_state(led, name, cfg["platforms"][name])
        if state == "ok":
            return name, reasons
        reasons.append(f"{name}: {state} ({detail})")
    return None, reasons
