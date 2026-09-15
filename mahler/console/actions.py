"""The console's writes (D27).

Each is a POST to /api/<name> that records a ledger event, like the CLI
command it mirrors. The ones here only touch the ledger, which other
processes already do outside the tick (`mahler pause`, `mahler peak off`), so
they apply at once. Writes that reach GitHub or a running agent go through the
tick instead, so all GitHub traffic keeps the project's own login (D25).
"""

import json

from .. import config, router
from ..gh import AGENT_MARK
from .state import SEEN_KEY


class ActionError(ValueError):
    """A request the console can't act on: bad input or a stale page."""


def pause(cfg, led, body):
    led.set_kv("paused", "1")
    led.event("pause", detail="paused from the console")


def resume(cfg, led, body):
    led.set_kv("paused", "0")
    led.event("resume", detail="resumed from the console")


def peak_override(cfg, led, body):
    """Let Claude build through the peak window until you switch back (D22)."""
    if not (cfg.get("claude_peak") or {}).get("enabled", True):
        raise ActionError("the peak window is switched off in config")
    led.set_kv(router.PEAK_OVERRIDE, router.PEAK_MANUAL)
    led.event("peak_override", detail="peak override on from the console, until switched back")


def peak_restore(cfg, led, body):
    if led.get_kv(router.PEAK_OVERRIDE):
        led.set_kv(router.PEAK_OVERRIDE, None)
        led.event("peak_override", detail="peak override cleared from the console")


def clear_backoff(cfg, led, body):
    """Lift a backoff or startup hold by hand. Only what a backoff wrote is
    dropped: the hold row, and an unmetered platform's quota-error rows — a
    metered platform's real readings stay."""
    names = body.get("platforms")
    if not isinstance(names, list) or not names:
        raise ActionError("platforms: a non-empty list")
    cleared = []
    for name in names:
        pconf = cfg["platforms"].get(name) if isinstance(name, str) else None
        if pconf is None:
            raise ActionError(f"unknown platform {name!r}")
        group = pconf.get("quota_group", name)
        for peer, pc in cfg["platforms"].items():
            if pc.get("quota_group", peer) != group:
                continue
            windows = [router.HOLD]
            if not pc.get("metered", True):
                windows += [w for w, u in led.usage(peer).items() if u["used_pct"] >= 100]
            if led.clear_usage(peer, windows):
                cleared.append(peer)
    led.event("backoff_cleared", detail={"platforms": sorted(set(cleared)),
                                         "asked": names, "by": "console"})


def digest_seen(cfg, led, body):
    """Mark the event stream seen up to an event id — the "N new" chip clears."""
    upto = body.get("upto")
    if not isinstance(upto, int) or isinstance(upto, bool) or upto < 0:
        raise ActionError("upto: a non-negative event id")
    try:
        seen = int(led.get_kv(SEEN_KEY) or 0)
    except (TypeError, ValueError):
        seen = 0
    if upto > seen:
        led.set_kv(SEEN_KEY, str(upto))
        led.event("console_seen", detail={"upto": upto})


def stop_run(cfg, led, body):
    """Stop & hand off (DESIGN D27): queue the run for a watchdog-driven stop
    so the tick's own login does the terminating, not the console process."""
    run_id = body.get("run")
    if type(run_id) is not int or run_id <= 0:
        raise ActionError("run must be a positive run id")
    with led._tx():
        run = led.run(run_id)
        if run is None or run["status"] not in ("running", "stopping"):
            raise ActionError("the run has already ended")
        for row in led.pending_actions("stop_run"):
            if json.loads(row["payload"]).get("run") == run_id:
                return {"id": row["id"]}          # already queued
        id = led.queue_action("stop_run", run["project"], run["number"], {"run": run_id})
        led.event("console_stop_queued", run["project"], run["number"], {"id": id, "run": run_id})
    return {"id": id}


def answer(cfg, led, body):
    project, number, text = (body.get(k) for k in ("project", "number", "text"))
    if not isinstance(project, str) or project not in {p["name"] for p in config.enabled_projects(cfg)}:
        raise ActionError("project must be enabled")
    if type(number) is not int or number <= 0:
        raise ActionError("number must be a positive issue number")
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 4000:
        raise ActionError("text must be 1–4000 characters")
    text = text.strip()
    if text.startswith(AGENT_MARK):
        raise ActionError("an answer cannot start with the agent marker")
    with led._tx():
        item = led.item(project, number)
        if item is None or item["state"] not in ("needs_you", "failed"):
            raise ActionError("the item moved on")
        for row in led.pending_actions("answer"):
            if (row["project"], row["number"]) == (project, number):
                led.cancel_action(row["id"])
        id = led.queue_action("answer", project, number, {"text": text}, delay_seconds=60)
        led.event("console_answer_queued", project, number, {"id": id})
    return {"id": id}


def answer_undo(cfg, led, body):
    id = body.get("id")
    if type(id) is not int or id <= 0:
        raise ActionError("id must be a positive action id")
    with led._tx():
        row = led.q1("SELECT * FROM console_actions WHERE id=? AND kind='answer'", (id,))
        if row is None or not led.cancel_action(id):
            raise ActionError("already sent")
        led.event("console_answer_cancelled", row["project"], row["number"], {"id": id})


ACTIONS = {f.__name__: f for f in (pause, resume, peak_override, peak_restore,
                                   clear_backoff, digest_seen, answer, answer_undo, stop_run)}


def run(cfg, led, name, body):
    """Apply one action. Raises KeyError for an unknown name, ActionError for
    input it can't act on."""
    if not isinstance(body, dict):
        raise ActionError("the body must be a JSON object")
    return ACTIONS[name](cfg, led, body)
