"""Quota-aware routing (DESIGN D8). Deterministic — no LLM in the control plane.

A platform is usable for a new run only if every quota window has a fresh
sample under its *soft* line. Unknown or stale usage counts as over the line,
so a missing reading can never push Claude into paid extra usage. Running
work is stopped at the *hard* line (see watchdog.py).
"""

from datetime import timedelta
from itertools import zip_longest
from zoneinfo import ZoneInfo

from .config import DEFAULT_ACCOUNT, account_of, account_mode_of, accounts_of
from .ledger import parse

WINDOWS = ("5h", "weekly")   # default window set; a platform can override via pconf["windows"]
HOLD = "hold"      # pseudo-window in the usage table: resets_at = when the hold lifts
PEAK_OVERRIDE = "peak_override_until"   # kv: an ISO time the peak window is overridden until

# short chip labels for countdowns (mahler#52): "5h" reads fine as-is, but
# "weekly" is shortened to "wk" to keep the CLI/web chips compact.
WINDOW_LABELS = {"5h": "5h", "weekly": "wk"}

SIZES = {"s": 1, "m": 2, "l": 3}


def _ts(value):
    """ledger.parse() that treats an unparseable timestamp as missing.

    Usage rows come from probes and run logs; a malformed timestamp must
    degrade to "unknown" (D8: unknown counts as over the soft line), never
    raise out of the router and break the tick.
    """
    try:
        return parse(value)
    except (TypeError, ValueError):
        return None


def peak_state(cfg, led):
    """Claude's peak window (DESIGN D22): is it active right now?

    Active when `claude_peak.enabled`, the local weekday is listed, the local
    time is in [start, end), and there is no live override (kv
    `peak_override_until`, an ISO time in the future). Pinned items bypass the
    window — the caller decides that, not this helper.

    -> (active: bool, until: datetime|None)
    `until` is today's end time, converted to UTC. When the window is not
    active, `until` is None.
    """
    pc = cfg.get("claude_peak") or {}
    if not pc.get("enabled", True):
        return False, None
    now = led.now()
    tz = ZoneInfo(pc.get("tz", "America/Los_Angeles"))
    local = now.astimezone(tz)
    if local.weekday() not in pc.get("weekdays", [0, 1, 2, 3, 4]):
        return False, None
    try:
        sh, sm = (int(x) for x in pc.get("start", "05:00").split(":"))
        eh, em = (int(x) for x in pc.get("end", "11:00").split(":"))
    except (ValueError, AttributeError):
        return False, None
    start = local.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = local.replace(hour=eh, minute=em, second=0, microsecond=0)
    if not (start <= local < end):
        return False, None
    override = _ts(led.get_kv(PEAK_OVERRIDE))
    if override and override > now:
        return False, None
    return True, end.astimezone(now.tzinfo)


def peak_status_line(cfg, led):
    """One-line human-readable peak state for `mahler status` / the web page.

    -> str or None. "peak hours: Claude paused until 11:00 PT (in 2h 10m)" or
    "peak hours: overridden until 09:30" or None when the window is off.
    """
    pc = cfg.get("claude_peak") or {}
    if not pc.get("enabled", True):
        return None
    now = led.now()
    override = _ts(led.get_kv(PEAK_OVERRIDE))
    if override and override > now:
        tz = ZoneInfo(pc.get("tz", "America/Los_Angeles"))
        return (f"peak hours: overridden until {override.astimezone(tz):%H:%M} "
                f"({fmt_countdown(override - now)})")
    active, until = peak_state(cfg, led)
    if not active:
        return None
    tz = ZoneInfo(pc.get("tz", "America/Los_Angeles"))
    return (f"peak hours: Claude paused until {until.astimezone(tz):%H:%M} PT "
            f"(in {fmt_countdown(until - now)})")


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
    sw = _ts(weekly.get("sampled_at"))
    sf = _ts(fiveh.get("sampled_at"))
    if not sw or not sf:
        return None
    stale_after = timedelta(minutes=15)
    if sw < now - stale_after or sf < now - stale_after:
        return None
    weekly_reset = _ts(weekly.get("resets_at"))
    fiveh_reset = _ts(fiveh.get("resets_at"))
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
        resets = _ts(u.get("resets_at"))
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
    hold_until = _ts(hold.get("resets_at")) if hold else None
    if hold_until and hold_until > now:
        # not a quota reading: the platform can't start runs right now (a run
        # sat silent at startup). Soft, so a run already making progress keeps going.
        until = hold_until
        return "soft", (f"on hold until {until.astimezone():%H:%M} "
                        f"(in {fmt_countdown(until - now)}) (a run never started)")
    if not pconf.get("metered", True):
        # no meter: fine unless a quota error put it in the penalty box
        for u in usage.values():
            until = _ts(u.get("resets_at"))
            if u["used_pct"] >= 100 and until and until > now:
                return "hard", f"backing off until {until.astimezone():%H:%M} (in {fmt_countdown(until - now)})"
        return "ok", "unknown limit (platform reports no quota signal)"
    stale_after = timedelta(minutes=pconf.get("stale_minutes", 15))
    worst, detail = "ok", []
    rank = {"ok": 0, "soft": 1, "hard": 2, "stale": 3}
    # burst lines are computed from this machine's own Claude account (D23), so
    # they never lift another account's lines (D25)
    is_claude = pconf.get("kind") == "claude" and account_of(pconf) == DEFAULT_ACCOUNT
    for w in pconf.get("windows", WINDOWS):
        u = usage.get(w)
        if u is None:
            state = "stale"
            detail.append(f"{w}: no sample")
        else:
            resets = _ts(u.get("resets_at"))
            sampled = _ts(u.get("sampled_at"))
            if resets and resets <= now:
                state = "stale"          # window rolled over — re-probe rather than guess
                detail.append(f"{w}: reset since sample")
            elif not sampled or sampled < now - stale_after:
                # an unreadable sample is as good as no sample: unknown counts
                # as over the soft line (D8), it never crashes the tick
                state = "stale"
                detail.append(f"{w}: sample older than {stale_after}"
                              if sampled else f"{w}: unreadable sample")
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


def routing_for(cfg, account):
    """The per-role routing table for `account` (DESIGN D25): this machine's own
    account uses the top-level [routing]; another account uses only its own."""
    if account == DEFAULT_ACCOUNT:
        return cfg["routing"]
    return (cfg.get("accounts", {}).get(account) or {}).get("routing") or {}


def candidates(cfg, role, pin=None, burst_lines=None, account=DEFAULT_ACCOUNT):
    # a fix run routes like a build (DESIGN D18): same platforms, same order
    routing = routing_for(cfg, account)
    if pin:
        order = [pin]
    elif burst_lines and role == "build" and account == DEFAULT_ACCOUNT:
        order = burst_build_order(cfg)
    else:
        order = routing.get(role) or routing.get("build") or []
    # a project only ever spends its own account's logins, pins included (D25)
    return [n for n in order if n in cfg["platforms"] and cfg["platforms"][n].get("enabled")
            and account_of(cfg["platforms"][n]) == account]


def candidates_for_accounts(cfg, role, accounts, pin=None, burst_lines=None):
    """Merged candidates across several accounts (DESIGN D26 "equal" mode):
    a round-robin interleave of each account's own candidate list — first
    candidate from the first account, then the second, then the first
    account's second candidate, and so on — so no one account's list is
    exhausted before another account gets a turn. Each account's internal
    preference order is preserved; tier values across accounts are not
    compared."""
    if pin:
        for account in accounts:
            found = candidates(cfg, role, pin, burst_lines, account)
            if found:
                return found
        return []
    per_account = [candidates(cfg, role, None, burst_lines, account) for account in accounts]
    merged = []
    for group in zip_longest(*per_account):
        merged.extend(name for name in group if name is not None)
    return merged


def tier_of(pconf):
    return pconf.get("tier", 1)


RISK_KEYWORDS = (
    "recipes/", "agents.md", "claude.md", "prompt context",
    "meta-programming", "credentials", "credential boundary",
    "database migration", "schema migration", "concurrency",
    "lease protocol"
)


def risk_min_tier(text):
    """Keywords in title or description that require a stronger platform (tier 2+)
    rather than free tier models like Kilo or Cline."""
    if not text:
        return 0
    lower = text.lower()
    if any(kw in lower for kw in RISK_KEYWORDS):
        return 2
    return 0


def pick(cfg, led, role, pin=None, busy=(), size=None, burst_lines=None,
         account=DEFAULT_ACCOUNT, min_tier=0, accounts=None):
    """First platform in routing order with headroom. -> (name|None, reasons).

    During an active burst (burst_lines from burst_status), build routing puts
    Claude first and Claude platforms use the burst soft/hard lines.

    During Claude's peak window (D22), a candidate whose `kind == "claude"` is
    skipped with the reason `peak hours until HH:MM (in Xh Ym) — mahler peak off
    to override`, unless the item is pinned to it (`pin` is not None). Running
    Claude runs are unaffected — this only gates new starts.

    `accounts`, when given, routes across several accounts at once (DESIGN
    D26 "equal" mode): candidates are the round-robin merge of each account's
    own list, rather than the single `account`'s list.
    """
    reasons = []
    accts = list(accounts) if accounts is not None else [account]
    if pin and pin in cfg["platforms"] and account_of(cfg["platforms"][pin]) not in accts:
        target = accts[0] if len(accts) == 1 else ", ".join(accts)
        reasons.append(f"{pin}: pinned, but it spends the "
                       f"{account_of(cfg['platforms'][pin])} account, not {target}")
    peak_active, peak_until = peak_state(cfg, led)
    cand = (candidates_for_accounts(cfg, role, accts, pin, burst_lines) if accounts is not None
            else candidates(cfg, role, pin, burst_lines, account))
    for name in cand:
        if name in busy:
            reasons.append(f"{name}: busy")
            continue
        pconf = cfg["platforms"][name]
        # Escalation tier (DESIGN D8 rule 4): build and fix skip platforms below min_tier
        if min_tier and not pin and role in ("build", "fix"):
            t = tier_of(pconf)
            if t < min_tier:
                reasons.append(f"{name}: tier {t} below escalation tier {min_tier}")
                continue
        # Size limits (max_size/min_size) are builder limits — they don't apply
        # to sort or plan roles (DESIGN D21).
        if role not in ("sort", "plan"):
            limit = pconf.get("max_size")
            if limit and not pin and SIZES.get(size or "m", 2) > SIZES[limit]:
                reasons.append(f"{name}: only takes size:{limit}")
                continue
            min_limit = pconf.get("min_size")
            if min_limit and not pin and SIZES.get(size or "m", 2) < SIZES[min_limit]:
                reasons.append(f"{name}: requires size:{min_limit}")
                continue
        if peak_active and pconf.get("kind") == "claude" and not pin:
            reasons.append(
                f"{name}: peak hours until {peak_until.astimezone():%H:%M} "
                f"(in {fmt_countdown(peak_until - led.now())}) "
                f"— mahler peak off to override")
            continue
        claude_lines = burst_lines if pconf.get("kind") == "claude" else None
        state, detail = usage_state(led, name, pconf, burst_lines=claude_lines)
        if state == "ok":
            return name, reasons
        reasons.append(f"{name}: {state} ({detail})")
    return None, reasons


def pick_for_project(cfg, led, pol, role, pin=None, busy=(), size=None,
                      burst_lines=None, min_tier=0):
    """Route within a project's declared accounts (DESIGN D26).

    Default ("order"): tries each account in turn, spending the first with
    headroom — reasons pool across the misses. "equal": merges every
    account's candidates round-robin and picks once, so a dual-use project
    doesn't exhaust one account before an idle other one is ever tried.
    """
    accts = accounts_of(pol)
    if account_mode_of(pol) == "equal":
        return pick(cfg, led, role, pin, busy, size=size, burst_lines=burst_lines,
                    min_tier=min_tier, accounts=accts)
    reasons = []
    for account in accts:
        platform, why = pick(cfg, led, role, pin, busy, size=size, burst_lines=burst_lines,
                             account=account, min_tier=min_tier)
        reasons += why
        if platform:
            return platform, reasons
    return None, reasons
