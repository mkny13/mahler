"""`cline-free` runs only on models Cline publishes as free (mahler#1507).

A pinned model went paid without anything noticing, and Cline has no automatic
free routing. So the free list (`free_models_url`) is read, cached in the
ledger, and the model is chosen from it by D33 measurements. An unreachable
list keeps the last known good model. A model that bills ("Insufficient
balance") is rejected and never used again while it stays listed; with no free
model left the platform is held rather than spend credits.
"""

import json
import math
import urllib.request
from datetime import datetime, timedelta

from . import scorecard

TTL_MINUTES = 30
LIST_KEY = "cline:free_models"
FETCHED_KEY = "cline:free_models_at"
GOOD_KEY = "cline:free_last_good_model"
REJECTED_KEY = "cline:free_rejected"
VALIDATED_KEY = "cline:free_validated"


def managed(pconf):
    return bool(pconf.get("kind") == "cline" and pconf.get("free_models_url"))


def fetch_free_models(url, timeout=10, opener=None):
    """-> list of model ids in Cline's `free` list, or None if unreachable."""
    try:
        open_ = opener or urllib.request.urlopen
        with open_(urllib.request.Request(url, headers={"Accept": "application/json"}),
                   timeout=timeout) as res:
            data = json.loads(res.read().decode("utf-8"))
        free = data.get("free")
        if not isinstance(free, list):
            return None
        return [m["id"] if isinstance(m, dict) else m for m in free
                if (isinstance(m, dict) and m.get("id")) or (isinstance(m, str) and m)]
    except Exception:
        return None


def _json(led, key, default):
    try:
        value = json.loads(led.get_kv(key) or "null")
    except (ValueError, TypeError):
        return default
    return value if isinstance(value, type(default)) else default


def _rejected(led):
    return _json(led, REJECTED_KEY, {})


def refresh(cfg, led, pconf, opener=None):
    """Fetch the free list when the cache is stale; on failure keep the cache."""
    at = led.get_kv(FETCHED_KEY)
    try:
        fresh = at and led.now() - datetime.fromisoformat(at) < timedelta(minutes=TTL_MINUTES)
    except ValueError:
        fresh = False
    if fresh:
        return
    models = fetch_free_models(pconf["free_models_url"], opener=opener)
    if models is not None:
        # A model that left the list loses its verdicts: if it comes back it is
        # tested afresh. While it stays listed, a rejection stands.
        for key in (REJECTED_KEY, VALIDATED_KEY):
            kept = {m: v for m, v in _json(led, key, {}).items() if m in models}
            led.set_kv(key, json.dumps(kept))
        led.set_kv(LIST_KEY, json.dumps(models))
        led.set_kv(FETCHED_KEY, led.now().isoformat())


def candidates(led, pconf):
    """Free models not rejected, from the cached list. With no list ever read,
    the last known good model, then the configured default."""
    if led.get_kv(LIST_KEY) is not None:
        listed = _json(led, LIST_KEY, [])            # an empty list is authoritative
    else:
        fallback = led.get_kv(GOOD_KEY) or pconf.get("model")
        listed = [fallback] if fallback else []
    rejected = _rejected(led)
    return [m for m in listed if m not in rejected]


def choose(cfg, led, role="build", size="s", name="cline-free", refresh_list=False, opener=None,
           probe=None):
    """-> the free model to launch with, or None when none is usable.

    A model is only launched once `probe(pconf, model)` has shown it runs
    without billing: True validates it, False rejects it, None (couldn't tell)
    skips it this time. A listed price of 0 proves nothing (mahler#1507)."""
    pconf = cfg["platforms"][name]
    if refresh_list:
        refresh(cfg, led, pconf, opener=opener)
    pool = candidates(led, pconf)
    if not pool:
        return None
    rows = {}
    for r in scorecard.ranked(scorecard.table(led, cfg), role, size):
        if r["platform"] == name:
            rows.setdefault(r["model"], r)              # best-ranked effort per model
    order = {"good": 0, "unproven": 1, "below": 2}

    def key(model):
        r = rows.get(model)
        if r is None:
            return (1, 0, 0)               # unmeasured counts as unproven
        cps = r["cost_per_success"]
        return (order[r["status"]], cps if cps is not None else math.inf, 0)
    validated = _json(led, VALIDATED_KEY, {})
    for model in sorted(pool, key=lambda m: (key(m), pool.index(m))):
        if model not in validated:
            verdict = probe(pconf, model) if probe else None
            if verdict is False:
                reject(led, model)
            if verdict is not True:
                continue
            validated[model] = led.now().isoformat()
            led.set_kv(VALIDATED_KEY, json.dumps(validated))
        led.set_kv(GOOD_KEY, model)
        return model
    return None


def reject(led, model):
    """Record that `model` billed (or left the free list) so it is skipped."""
    rejected = _json(led, REJECTED_KEY, {})
    rejected[model] = led.now().isoformat()
    led.set_kv(REJECTED_KEY, json.dumps(rejected))
    if led.get_kv(GOOD_KEY) == model:
        led.set_kv(GOOD_KEY, None)


def flagged(led, pconf):
    """Models in use or configured that Cline's cached free list no longer has."""
    if led.get_kv(LIST_KEY) is None:
        return []
    listed = _json(led, LIST_KEY, [])
    used = {m for m in (pconf.get("model"), led.get_kv(GOOD_KEY)) if m}
    return sorted(m for m in used if m not in listed)
