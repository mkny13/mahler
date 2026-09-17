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
            if not router.is_metered(led, peer, pc):
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


def save_settings(cfg, led, body):
    """Validate and atomically replace the editable config projection."""
    try:
        saved = config.save_settings(body)
    except (OSError, ValueError) as exc:
        raise ActionError(str(exc)) from exc
    led.event("settings_saved", detail={
        "platforms": len(saved["platforms"]),
        "routes": len(saved["routing"]),
    })
    return {"settings": saved}


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


def capture(cfg, led, body):
    """Type-or-dictate capture (mahler#251): files straight into a project's
    backlog — there is no inbox (owner, 2026-09-15)."""
    text, project = body.get("text"), body.get("project")
    if not isinstance(project, str) or project not in {p["name"] for p in config.enabled_projects(cfg)}:
        raise ActionError("project must be enabled")
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 8000:
        raise ActionError("text must be 1-8000 characters")
    payload = {"text": text.strip()}
    if body.get("attachment"):
        payload["attachment"] = body["attachment"]
    id = led.queue_action("capture", project, None, payload)
    led.event("console_capture_queued", project, None, {"id": id})
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


def revert(cfg, led, body):
    event = body.get("event")
    if type(event) is not int or event <= 0:
        raise ActionError("event must be a positive shipped event id")
    with led._tx():
        row = led.q1("SELECT * FROM events WHERE id=? AND kind='shipped'", (event,))
        if row is None:
            raise ActionError("the event is not a merged change")
        if row["project"] not in {p["name"] for p in config.enabled_projects(cfg)}:
            raise ActionError("project must be enabled")
        pr = json.loads(row["detail"]).get("pr")
        if type(pr) is not int or pr <= 0 or not row["number"]:
            raise ActionError("the event has no pull request")
        for action in led.q("SELECT * FROM console_actions WHERE kind='revert'"):
            payload = json.loads(action["payload"])
            if (action["project"] == row["project"] and payload.get("pr") == pr
                    and action["status"] in ("pending", "done")):
                raise ActionError("a revert is already queued or requested")
        if led.get_kv(f"revert:{row['project']}:{pr}"):
            raise ActionError("a revert issue already exists")
        id = led.queue_action("revert", row["project"], row["number"],
                              {"event": event, "pr": pr})
        led.event("console_revert_queued", row["project"], row["number"], {"id": id})
    return {"id": id}


# ---------- Ready to test (the UAT queue) ----------

def _uat_target(cfg, led, body):
    project, number = body.get("project"), body.get("number")
    if not isinstance(project, str) or \
            project not in {p["name"] for p in config.enabled_projects(cfg)}:
        raise ActionError("project must be an enabled project")
    if type(number) is not int or number <= 0:
        raise ActionError("number must be a positive issue number")
    row = led.uat(project, number)
    if row is None:
        raise ActionError("no such UAT item")
    return row


def _verdict_pending(led, project, number):
    """A verdict queued but not yet run (capture can lag): nothing else may
    queue a second one."""
    for kind in ("uat_pass", "uat_fail"):
        for r in led.pending_actions(kind):
            if r["project"] == project and r["number"] == number:
                return True
    return False


def _queue_verdict(cfg, led, kind, body, payload, verdict):
    with led._tx():
        row = _uat_target(cfg, led, body)
        if row["verdict"] or _verdict_pending(led, row["project"], row["number"]):
            raise ActionError("the verdict is already recorded or queued")
        id = led.queue_action(kind, row["project"], row["number"], payload,
                              delay_seconds=0)
        led.event("console_uat_queued", row["project"], row["number"],
                  {"verdict": verdict, "id": id})
    return {"id": id}


def uat_pass(cfg, led, body):
    return _queue_verdict(cfg, led, "uat_pass", body, {}, "pass")


def uat_fail(cfg, led, body):
    note = body.get("note")
    if not isinstance(note, str) or len(note) > 2000:
        raise ActionError("note must be a string of at most 2000 characters")
    payload = {"note": note}
    if body.get("attachment"):
        payload["attachment"] = body["attachment"]
    return _queue_verdict(cfg, led, "uat_fail", body, payload, "fail")


def attach(cfg, led, body):
    import base64
    import os
    import uuid
    import binascii
    
    name = body.get("name")
    ctype = body.get("type")
    data_b64 = body.get("data")
    
    if not isinstance(name, str) or not name:
        raise ActionError("name is required")
    if not isinstance(ctype, str) or not ctype:
        raise ActionError("type is required")
    if not isinstance(data_b64, str):
        raise ActionError("data must be base64 string")
        
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in ("png", "jpg", "jpeg", "gif", "webp", "heic"):
        raise ActionError(f"unsupported extension: {ext}")
        
    expected_ctype = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
        "heic": "image/heic"
    }.get(ext)
    
    if ctype != expected_ctype:
        raise ActionError(f"content type mismatch: {ctype} for ext {ext}")
        
    try:
        data = base64.b64decode(data_b64)
    except binascii.Error:
        raise ActionError("invalid base64 data")
        
    if len(data) > 10 * 1024 * 1024:
        raise ActionError("attachment too large (10MB limit)")
        
    valid = False
    if ext == "png":
        valid = data.startswith(b"\x89PNG\r\n\x1a\n")
    elif ext in ("jpg", "jpeg"):
        valid = data.startswith(b"\xff\xd8\xff")
    elif ext == "gif":
        valid = data.startswith(b"GIF87a") or data.startswith(b"GIF89a")
    elif ext == "webp":
        valid = data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP"
    elif ext == "heic":
        valid = len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1")
        
    if not valid:
        raise ActionError("magic bytes do not match extension")
        
    config.ensure_private_dir(config.ATTACHMENTS_DIR, mode=0o700)
    
    file_id = uuid.uuid4().hex
    filename = f"{file_id}.{ext}"
    file_path = os.path.join(config.ATTACHMENTS_DIR, filename)
    
    fd = os.open(file_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except Exception:
        os.remove(file_path)
        raise ActionError("failed to write attachment")
        
    led.event("attachment_saved", detail={"id": filename, "name": name})
    return {"id": filename, "name": name}


ACTIONS = {f.__name__: f for f in (pause, resume, peak_override, peak_restore,
                                   clear_backoff, digest_seen, answer, answer_undo, stop_run,
                                   capture, revert, uat_pass, uat_fail, attach, save_settings)}


def run(cfg, led, name, body):
    """Apply one action. Raises KeyError for an unknown name, ActionError for
    input it can't act on."""
    if not isinstance(body, dict):
        raise ActionError("the body must be a JSON object")
    return ACTIONS[name](cfg, led, body)
