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

# short chip labels for countdowns (mahler#52): "5h" reads fine as-is, but
# "weekly" is shortened to "wk" to keep the CLI/web chips compact.
WINDOW_LABELS = {"5h": "5h", "weekly": "wk"}

SIZES = {"s": 1, "m": 2, "l": 3}


def fmt_countdown(delta):
    """timedelta -> '1h 26m' / '2d 5h' / '35m', biggest two non-zero units."""
    total_minutes = max(int(delta.total_seconds() // 60), 0)
    days, rem = divmod(total_minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def window_countdowns(led, name, pconf):
    """-> [(label, 'in <countdown>'), ...] for each metered window with a
    known, still-future reset time, in config order (mahler#52)."""
    now = led.now()
    usage = led.usage(name)
    out = []
    for w in pconf.get("windows", WINDOWS):
        u = usage.get(w)
        if u is None:
            continue
        resets = parse(u["resets_at"])
        if not resets or resets <= now:
            continue
        out.append((WINDOW_LABELS.get(w, w), f"in {fmt_countdown(resets - now)}"))
    return out


def usage_state(led, name, pconf):
    """-> ('ok'|'soft'|'hard'|'stale', detail). Worst window wins."""
    now = led.now()
    usage = led.usage(name)
    hold = usage.pop(HOLD, None)
    if hold and parse(hold["resets_at"]) and parse(hold["resets_at"]) > now:
        # not a quota reading: the platform can't start runs right now (a run
        # sat silent at startup). Soft, so a run already making progress keeps going.
        until = parse(hold["resets_at"])
        return "soft", (f"on hold until {until.astimezone():%H:%M} "
                         f"(in {fmt_countdown(until - now)}) (a run never started)")
    if not pconf.get("metered", True):
        # no meter: fine unless a quota error put it in the penalty box
        for u in usage.values():
            until = parse(u["resets_at"])
            if u["used_pct"] >= 100 and until and until > now:
                return "hard", f"backing off until {until.astimezone():%H:%M} (in {fmt_countdown(until - now)})"
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
        # Size limits (max_size/min_size) are builder limits — they don't apply
        # to sort or plan roles (DESIGN D21).
        if role not in ("sort", "plan"):
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
