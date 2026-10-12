"""Durable, selection-specific model rejections, independent of quota clocks."""

import hashlib
import json

from .platforms import effort_value
from .redact import redact

GUIDANCE = "CLI rejected its model; model configuration needs repair (change model or effort)"


def selection(pconf, run=None):
    run = dict(run or {})
    role = run.get("role") or "build"
    role = "sort" if role in ("design", "plan") else role
    model = run.get("configured_model")
    if model is None:
        model = run.get("model")
    return {"kind": pconf.get("kind"),
            "model": model if model is not None else (pconf.get("sort_model" if role == "sort" else "build_model") or pconf.get("model") or ""),
            "effort": run.get("effort") or effort_value(pconf, role) or "default",
            "role": role}


def fingerprint(selected):
    return hashlib.sha256(json.dumps({k: selected[k] for k in ("kind", "model", "effort")},
                                    sort_keys=True).encode()).hexdigest()


def reject(led, name, pconf, run, log):
    selected = selection(pconf, run)
    key = f"model_rejection:{name}:{fingerprint(selected)}"
    previous = led.get_kv(key)
    record = dict(selected, platform=name,
                  diagnostic=redact(log.get("model_diagnostic") or log.get("last_error") or "")[:4096],
                  available_models=redact(log.get("available_models") or "unavailable")[:4096])
    led.set_kv(key, json.dumps(record))
    # Keep all rejected fingerprints, including a late finalizer's old selection.
    index = json.loads(led.get_kv(f"model_rejections:{name}") or "[]")
    if key not in index:
        index.append(key)
        led.set_kv(f"model_rejections:{name}", json.dumps(index))
    return record, not previous


def blocked(led, name, pconf, role=None, *, migrate=True):
    # Old releases only wrote a timed hold. Recover identity from the latest
    # rejected run; without metadata retire the hold once and allow ordinary
    # routing to classify the next launch. A rejection then persists normally.
    if led.get_kv(f"hold_reason:{name}") == "model_unavailable":
        run = led.q1("SELECT * FROM runs WHERE platform=? AND stop_reason='model_unavailable' "
                     "ORDER BY id DESC LIMIT 1", (name,))
        if run and (run["configured_model"] is not None or run["model"]):
            if migrate:
                reject(led, name, pconf, run, {})
            else:
                selected = selection(pconf, run)
                current = selection(pconf, {"role": role or selected["role"]})
                if fingerprint(selected) == fingerprint(current):
                    return dict(selected, platform=name)
        if migrate:
            # No identity to recover: allow one classification launch, then
            # keep the platform busy until that launch ends (probe_pending).
            last = led.q1("SELECT MAX(id) AS n FROM runs WHERE platform=?", (name,))
            led.set_kv(f"model_probe:{name}", str((last and last["n"]) or 0))
            led.clear_usage(name, ["hold"])
            led.set_kv(f"hold_reason:{name}", None)
    for key in json.loads(led.get_kv(f"model_rejections:{name}") or "[]"):
        record = json.loads(led.get_kv(key))
        current = selection(pconf, {"role": role or record["role"]})
        if fingerprint(current) == fingerprint(record):
            return record
    return None


def probe_pending(led, name):
    """True while a legacy-migration classification launch is unresolved."""
    baseline = led.get_kv(f"model_probe:{name}")
    if baseline is None:
        return False
    if led.q1("SELECT 1 FROM runs WHERE platform=? AND id>? AND status='ended'",
              (name, int(baseline))):
        led.set_kv(f"model_probe:{name}", None)
        return False
    return True
