"""Persistent launch circuit breakers; canaries use the normal routing/lease gates."""

import json
import re
from datetime import timedelta

from . import config, version
from .ledger import iso, parse
from .redact import redact

CANARY_INTERVAL = timedelta(minutes=30)


def signature(error):
    line = redact(str(error)).splitlines()[0] if str(error) else ""
    line = re.sub(r"(?:[A-Za-z]:\\|[~/]|\./)[^\s'\"<>]+", "<path>", line)
    line = re.sub(r"\b[0-9a-fA-F]{7,40}\b", "<sha>", line)
    line = re.sub(r"\d+", "#", line)
    return f"{type(error).__name__}: {line}"[:500]


def _read(led, key, default):
    return json.loads(led.get_kv(key) or json.dumps(default))


def _key(project=None):
    return "launch_broken" + (f":{project}" if project else "")


def allowed(ctx, project, number, consume=False):
    """Reserve at most one canary per scope, only immediately before an attempt."""
    led = ctx.led
    states = [(key, _read(led, key, None)) for key in (_key(), _key(project))]
    for key, state in states:
        if state and led.now() < parse(state["retry_at"]):
            ctx.hold("launch_broken", project=project, number=number,
                     signature=state["signature"], until=state["retry_at"],
                     scope="global" if key == _key() else "project")
            return False
    if consume and not ctx.dry_run:
        for key, state in states:
            if state:
                state["retry_at"] = iso(led.now() + CANARY_INTERVAL)
                led.set_kv(key, json.dumps(state))
    return True


def failed(ctx, project, number, run_id, error):
    led = ctx.led
    sig = signature(error)
    failures = _read(led, "launch_failures", [])
    failures.append(dict(signature=sig, project=project, number=number,
                         at=iso(led.now()), run=run_id))
    failures = failures[-50:]
    led.set_kv("launch_failures", json.dumps(failures))
    successes = _read(led, "launch_successes", {})
    global_matches = [f for f in failures if f["signature"] == sig
                      and f["run"] > successes.get("global", 0)]
    local_matches = [f for f in failures if f["signature"] == sig
                     and f["project"] == project
                     and f["run"] > successes.get(f"project:{project}", 0)]
    if len({(f["project"], f["number"]) for f in global_matches}) >= 2:
        scope, matches = None, global_matches
    elif len(local_matches) >= 3:
        scope, matches = project, local_matches
    else:
        return
    key = _key(scope)
    if _read(led, key, None):
        return  # failed canary: retain the incident and its notification dedupe
    state = dict(signature=sig, since=matches[0]["at"],
                 sha=version._short_head(config.REPO_ROOT),
                 retry_at=iso(led.now() + CANARY_INTERVAL))
    led.set_kv(key, json.dumps(state))
    led.event("launch_broken", scope, detail=state)
    ctx.ping(f"Mahler can't launch any runs: {sig}" if scope is None else
             f"Mahler can't launch runs in {project}: {sig}",
             "Launches paused; one canary will be tried every 30 minutes.",
             priority="high" if scope is None else "default")
    ctx.hold("launch_broken", project=project, number=number, signature=sig,
             until=state["retry_at"], scope="global" if scope is None else "project")


def succeeded(ctx, project, run_id):
    led = ctx.led
    successes = _read(led, "launch_successes", {})
    successes.update({"global": run_id, f"project:{project}": run_id})
    led.set_kv("launch_successes", json.dumps(successes))
    for scope in (None, project):
        key = _key(scope)
        state = _read(led, key, None)
        if state:
            led.set_kv(key, "null")
            led.event("launch_recovered", scope, detail=state)
            ctx.ping("Mahler launches recovered" if scope is None else
                     f"Mahler launches recovered in {project}", state["signature"])
