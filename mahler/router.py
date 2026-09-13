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


def burst_status(cfg, led):
    """Detect an active Claude burst window (D23) and return the burst lines.

    The reserve (D8) keeps Claude quota back for you. In the last lead-time
    before a window resets, that reserve expires unused, so the burst raises
    Claude's soft/hard lines to the burst lines (default 90%/97%, still below
    100%) so the expiring quota turns into work instead of going to waste.

    Returns None when burst is off, usage is stale/unknown, or no window is in
    its lead period. Otherwise returns {window: (soft_pct, hard_pct)} for the
    windows currently in a burst:
      - weekly burst (within weekly_lead_hours of the weekly reset): both "5h"
        and "weekly" get burst lines
      - session burst (within session_lead_minutes of the 5h reset, but no
        weekly burst): only "5h" gets burst lines; "weekly" keeps its normal
        reserve lines so the weekly reserve stays yours
    """
    bconf = cfg.get("burst", {})
    if not bconf.get("enabled", False):
        return None
    now = led.now()
    usage = led.usage("claude")
    weekly = usage.get("weekly")
    fiveh = usage.get("5h")
    if not weekly or not fiveh:
        return None
    sw = parse(weekly.get("sampled_at"))
    sf = parse(fiveh.get("sampled_at"))
    if not sw or not sf:
        return None
    stale_after = timedelta(minutes=15)
    if sw < now - stale_after or sf < now - stale_after:
        return None
    weekly_reset = parse(weekly.get("resets_at"))
    fiveh_reset = parse(fiveh.get("resets_at"))
    if not weekly_reset or not fiveh_reset:
        return None
    bsoft = bconf.get("soft", 90)
    bhard = bconf.get("hard", 97)
    lines = {}
    weekly_burst = weekly_reset > now and \
        (weekly_reset - now) < timedelta(hours=bconf.get("weekly_lead_hours", 5))
    session_burst = fiveh_reset > now and \
        (fiveh_reset - now) < timedelta(minutes=bconf.get("session_lead_minutes", 60))
    if weekly_burst:
        lines["5h"] = (bsoft, bhard)
        lines["weekly"] = (bsoft, bhard)
    elif session_burst:
        lines["5h"] = (bsoft, bhard)
    return lines or None


def burst_kind(burst_lines):
    """'weekly' / 'session' / None — for human-readable status display."""
    if not burst_lines:
        return None
    return "weekly" if "weekly" in burst_lines else "session"


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


def usage_state(led, name, pconf, burst_lines=None):
    """-> ('ok'|'soft'|'hard'|'stale', detail). Worst window wins.

    During an active D23 burst, `burst_lines` overrides the soft/hard lines for
    the windows in the burst — but only for Claude platforms (kind == "claude"),
    so free-tier platforms are never affected.
    """
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
    is_claude = pconf.get("kind") == "claude"
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
                soft_w, hard_w = pconf["soft"][w], pconf["hard"][w]
                if is_claude and burst_lines and w in burst_lines:
                    soft_w, hard_w = burst_lines[w]
                state = ("hard" if pct >= hard_w
                         else "soft" if pct >= soft_w else "ok")
                detail.append(f"{w} {pct:.0f}%")
        if rank[state] > rank[worst]:
            worst = state
    return worst, ", ".join(detail)


def burst_build_order(cfg):
    """routing.build with Claude platforms moved ahead of the free tiers (D23).

    During a burst, Claude quota is the one expiring, so Claude platforms go
    first; the free tiers remain as fallback behind them.
    """
    order = cfg["routing"]["build"]
    claude = [n for n in order if cfg["platforms"].get(n, {}).get("kind") == "claude"]
    rest = [n for n in order if cfg["platforms"].get(n, {}).get("kind") != "claude"]
    return claude + rest


def candidates(cfg, role, pin=None, burst_lines=None):
    # a fix run routes like a build (DESIGN D18): same platforms, same order
    if pin:
        order = [pin]
    elif burst_lines and role == "build":
        order = burst_build_order(cfg)
    else:
        order = cfg["routing"].get(role) or cfg["routing"]["build"]
    return [n for n in order if n in cfg["platforms"] and cfg["platforms"][n].get("enabled")]


def pick(cfg, led, role, pin=None, busy=(), size=None, burst_lines=None):
    """First platform in routing order with headroom. -> (name|None, reasons).

    During an active burst (burst_lines from burst_status), build routing puts
    Claude first and Claude platforms use the burst soft/hard lines.
    """
    reasons = []
    for name in candidates(cfg, role, pin, burst_lines):
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
        pconf = cfg["platforms"][name]
        claude_lines = burst_lines if pconf.get("kind") == "claude" else None
        state, detail = usage_state(led, name, pconf, burst_lines=claude_lines)
        if state == "ok":
            return name, reasons
        reasons.append(f"{name}: {state} ({detail})")
    return None, reasons
