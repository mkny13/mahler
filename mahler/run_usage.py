"""Raw per-run accounting, shared by finalization and historical backfill."""

import json
import math
import re
from datetime import datetime, timezone

# Request-boundary evidence (mahler#735). Kilo: one `step_start` event per model
# request. Cline: `iteration_start` (one model call per iteration), documented in
# @cline/shared agents/types.d.ts (cline 3.0.68, @cline/core 0.0.90), emitted
# wrapped as {"type":"agent_event","event":{...}}. Tool calls, content chunks and
# token totals are never used to infer requests.
REQUEST_KINDS = ("kilo", "cline")
UNKNOWN = "unknown"
_RATE_LIMIT_TEXT = re.compile(r"\b429\b|rate[ _-]?limit|too many requests", re.I)


def number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def new_requests():
    """Fresh request-tracking state for one log read."""
    return {"events": 0, "malformed": 0, "reqs": [], "seen": set(), "open": 0,
            "ended": False, "rate_limited": False}


def _day(ts):
    """UTC YYYY-MM-DD for epoch seconds/millis or an ISO string, else None."""
    try:
        if number(ts):
            ts = ts / 1000 if ts > 1e11 else ts
            return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
        if isinstance(ts, str) and ts:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OverflowError, OSError):
        pass
    return None


def _is_429(ev):
    """True for an explicit 429 status or rate-limit wording in an error event."""
    err = ev.get("error")
    data = err.get("data") if isinstance(err, dict) else None
    for node in (ev, err, data):
        if isinstance(node, dict):
            for key in ("statusCode", "status", "code"):
                if node.get(key) in (429, "429"):
                    return True
    text = ev.get("message")
    if isinstance(err, str):
        text = err
    elif isinstance(err, dict):
        text = err.get("message") or (data or {}).get("message") or text
    return isinstance(text, str) and bool(_RATE_LIMIT_TEXT.search(text))


def note_request(res, ev, kind):
    """Track request boundaries, rate-limit errors and log integrity for one event."""
    st = res.get("_requests")
    if st is None or kind not in REQUEST_KINDS:
        return
    if not (ev.get("type") or ev.get("event")):
        return          # not an event: no evidence the log is a real stream
    st["events"] += 1
    inner = ev
    if kind == "cline" and ev.get("type") == "agent_event" and isinstance(ev.get("event"), dict):
        inner = ev["event"]
    t = inner.get("type", inner.get("event"))
    if (t == "error" or inner.get("error")) and _is_429(inner):
        st["rate_limited"] = True
    if kind == "kilo":
        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
        if t == "step_start":
            key = part.get("id") if isinstance(part.get("id"), str) and part["id"] else None
            ts = ev.get("timestamp")
            if ts is None and isinstance(part.get("time"), dict):
                ts = part["time"].get("start")
            _add_request(st, key, _day(ts))
        elif t == "step_finish":
            st["open"] = max(0, st["open"] - 1)
            model = (part.get("model") or {}).get("modelID") if isinstance(part.get("model"), dict) else None
            if isinstance(model, str) and model:
                for req in reversed(st["reqs"]):
                    if req["model"] is None:
                        req["model"] = model
                        break
    elif t == "iteration_start":
        it = inner.get("iteration")
        key = (inner.get("conversationId"), inner.get("agentId"), it) \
            if isinstance(it, int) and not isinstance(it, bool) else None
        ts = next((v for v in (inner.get("timestamp"), inner.get("ts"), ev.get("timestamp"),
                               ev.get("ts")) if v is not None), None)
        _add_request(st, key, _day(ts))
    elif t == "run_result":
        st["ended"] = True


def _add_request(st, key, day):
    if key is not None:
        if key in st["seen"]:
            return
        st["seen"].add(key)
    st["open"] += 1
    st["reqs"].append({"day": day, "model": None})


def finish_requests(res, kind, readable):
    """Fold tracking state into requests / request_buckets / request_coverage / flags."""
    st = res.pop("_requests", None)
    res["requests"] = res["request_buckets"] = res["request_coverage"] = None
    res["rate_limited"] = None
    if st is None or kind not in REQUEST_KINDS or not readable or not st["events"]:
        return
    n = len(st["reqs"])
    res["rate_limited"] = st["rate_limited"]
    if kind == "cline" and not n:
        return          # no iteration_start seen: unsupported/unknown, never a fabricated zero
    buckets = {}
    for req in st["reqs"]:
        day = buckets.setdefault(req["day"] or UNKNOWN, {})
        model = req["model"] or UNKNOWN
        day[model] = day.get(model, 0) + 1
    partial = st["malformed"] or (st["open"] if kind == "kilo" else not st["ended"])
    res["requests"] = n
    res["request_buckets"] = buckets
    res["request_coverage"] = "partial" if partial else "complete"


def collect(res, ev, kind):
    """Consume only accounting events; absent token usage stays unknown."""
    t = ev.get("type", ev.get("event"))
    usage = None
    additive = False
    fields = {}
    model = None
    if kind in ("claude", "agent-sdk"):
        if t == "system":
            model = ev.get("model")
        if t == "result":
            usage = ev.get("usage")
            if number(ev.get("total_cost_usd")):
                res["cost_usd"] = ev["total_cost_usd"]
            fields = {"in": ("input_tokens", "cache_creation_input_tokens"),
                      "cached": ("cache_read_input_tokens",), "out": ("output_tokens",)}
    elif kind == "codex" and t == "turn.completed":
        usage = ev.get("usage")
        additive = True
        fields = {"in": ("input_tokens",), "cached": ("cached_input_tokens",),
                  "out": ("output_tokens",), "reasoning": ("reasoning_output_tokens",)}
    elif kind == "agy":
        if t == "init":
            model = ev.get("model") or (ev.get("init") or {}).get("model")
        if t == "result":
            usage = (ev.get("result") or {}).get("usage")
            fields = {"in": ("input_tokens",), "cached": ("cache_read_tokens",),
                      "out": ("output_tokens",), "reasoning": ("thinking_tokens",)}
    elif kind == "cline":
        model = ev.get("model")
        if t == "run_result":
            usage = ev.get("aggregateUsage")
            fields = {"in": ("inputTokens", "cacheWriteTokens"),
                      "cached": ("cacheReadTokens",), "out": ("outputTokens",)}
    elif kind == "copilot":
        data = ev.get("data") or {}
        if t == "assistant.message":
            model = data.get("model")
        if t == "session.usage_checkpoint":
            if number(data.get("totalNanoAiu")):
                res["credits"] = data["totalNanoAiu"] / 1e9
            usage = data.get("usage")
            fields = {"in": ("input_tokens",), "cached": ("cached_input_tokens",),
                      "out": ("output_tokens",), "reasoning": ("reasoning_output_tokens",)}
    elif kind == "kilo" and t == "step_finish":
        part = ev.get("part") or {}
        model = (part.get("model") or {}).get("modelID")
        usage = part.get("tokens")
        additive = True
        fields = {"in": ("input",), "out": ("output",), "reasoning": ("reasoning",)}
    if isinstance(model, str) and model:
        res["model"] = model
    if not isinstance(usage, dict):
        return
    counts = {key: sum(usage.get(f, 0) for f in names if number(usage.get(f)))
              for key, names in fields.items()}
    if not any(number(usage.get(f)) for names in fields.values() for f in names):
        return
    if kind == "claude":
        counts["reasoning"] = (usage.get("output_tokens_details") or {}).get("thinking_tokens", 0)
    if kind == "kilo":
        counts["cached"] = (usage.get("cache") or {}).get("read", 0)
    if kind == "codex":
        counts["in"] = max(0, counts["in"] - counts["cached"])
    for key in ("in", "cached", "out", "reasoning"):
        value = counts.get(key, 0)
        value = int(value) if number(value) else 0
        res["tokens"][key] = (res["tokens"][key] or 0) + value if additive else value


def _flag(value):
    return None if value is None else int(bool(value))


def columns(log, run, cfg):
    """Price known usage, preserving a CLI's cost and the run's historical model."""
    run = dict(run)
    pconf = cfg.get("platforms", {}).get(run.get("platform")) or {}
    model = (log.get("model") or run.get("model") or
             pconf.get("sort_model" if run.get("role") == "sort" else "build_model") or
             pconf.get("model"))
    tokens = log.get("tokens") or {}
    out = {"model": model, **{f"tokens_{k}": tokens.get(k)
                              for k in ("in", "cached", "out", "reasoning")},
           "credits": log.get("credits"),
           "quota_used": json.dumps(log["quota_used"]) if log.get("quota_used") else None,
           "requests": log.get("requests"),
           "request_buckets": (json.dumps(log["request_buckets"], sort_keys=True)
                               if log.get("request_buckets") is not None else None),
           "request_coverage": log.get("request_coverage"),
           "rate_limited": _flag(log.get("rate_limited")),
           "limit_hit": _flag(None if log.get("rate_limited") is None else
                              log.get("quota_hit") or log.get("credit_exhausted")
                              or log.get("rate_limited")),
           "cost_usd": None, "cost_source": "unpriced"}
    if number(log.get("cost_usd")):
        out.update(cost_usd=log["cost_usd"], cost_source="cli")
    else:
        cost = price(tokens, model, cfg)
        if cost is not None:
            out.update(cost_usd=cost, cost_source="priced")
    return out


def price(tokens, model, cfg):
    """API-equivalent dollars for `tokens` at `[prices."<model>"]`, or None if unpriced."""
    rates = cfg.get("prices", {}).get(model) or {}
    rates = [rates.get("in"), rates.get("cached_in", rates.get("in")), rates.get("out")]
    if not all(number(r) for r in rates) or tokens.get("out") is None:
        return None
    return ((tokens.get("in") or 0) * rates[0] + (tokens.get("cached") or 0) * rates[1]
            + ((tokens.get("out") or 0) + (tokens.get("reasoning") or 0)) * rates[2]) / 1e6
