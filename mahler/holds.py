"""Inspect and clear explicit platform holds.

Holds are distinct from real quota readings: ``finalize`` writes a ``hold``
pseudo-window, and an unmetered credit/quota failure may also write synthetic
100% 5h/weekly rows until the same retry time.  Clearing a hold must not erase
another platform's state or a metered platform's genuine usage.
"""

import json

from . import router


def _credit_state(led, platform):
    try:
        state = json.loads(led.get_kv(f"credit_state:{platform}") or "{}")
    except (TypeError, ValueError):
        state = {}
    return state if isinstance(state, dict) else {}


def inspect(cfg, led, platform):
    """Describe exactly what ``clear`` would remove for one platform."""
    if platform not in cfg.get("platforms", {}):
        raise ValueError(f"unknown platform {platform!r}")
    pconf = cfg["platforms"][platform]
    usage = led.usage(platform)
    hold = usage.get(router.HOLD)
    windows = [router.HOLD] if hold else []

    # The unmetered adapters have no genuine percentage reading: their 100%
    # rows are penalty-box rows written after an error.  For a metered adapter,
    # remove a 100% row only when it shares the explicit hold's retry time;
    # otherwise it is a real reading and must survive the manual unhold.
    for window in router.WINDOWS:
        row = usage.get(window)
        if not hold or not row or row["used_pct"] < 100:
            continue
        same_hold = bool(hold and row.get("resets_at") == hold.get("resets_at"))
        if not router.is_metered(led, platform, pconf) or same_hold:
            windows.append(window)

    return {
        "platform": platform,
        "windows": windows,
        "hold_reason": led.get_kv(f"hold_reason:{platform}"),
        "credit_state": _credit_state(led, platform),
        "until": hold.get("resets_at") if hold else None,
    }


def clear(cfg, led, platform, *, by):
    """Clear one platform's explicit hold and record who requested it."""
    found = inspect(cfg, led, platform)
    with led._tx():
        led.clear_usage(platform, found["windows"])
        led.set_kv(f"hold_reason:{platform}", None)
        led.set_kv(f"credit_state:{platform}", "{}")
        led.event("hold_cleared", detail={
            "platform": platform,
            "windows": found["windows"],
            "reason": found["hold_reason"],
            "by": by,
        })
    return found
