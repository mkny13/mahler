"""Raw per-run accounting, shared by finalization and historical backfill."""

import json
import math


def number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def collect(res, ev, kind):
    """Consume only accounting events; absent token usage stays unknown."""
    t = ev.get("type", ev.get("event"))
    usage = None
    additive = False
    fields = {}
    model = None
    if kind == "claude":
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
        usage = (ev.get("part") or {}).get("tokens")
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


def columns(log, run, cfg):
    """Price known usage, preserving a CLI's cost and the run's historical model."""
    run = dict(run)
    pconf = cfg.get("platforms", {}).get(run["platform"], {})
    model = (log.get("model") or run.get("model") or
             pconf.get("sort_model" if run["role"] == "sort" else "build_model"))
    tokens = log.get("tokens") or {}
    out = {"model": model, **{f"tokens_{k}": tokens.get(k)
                              for k in ("in", "cached", "out", "reasoning")},
           "credits": log.get("credits"),
           "quota_used": json.dumps(log["quota_used"]) if log.get("quota_used") else None,
           "cost_usd": None, "cost_source": "unpriced"}
    if number(log.get("cost_usd")):
        out.update(cost_usd=log["cost_usd"], cost_source="cli")
    else:
        price = cfg.get("prices", {}).get(model, {})
        rates = [price.get("in"), price.get("cached_in", price.get("in")), price.get("out")]
        if all(number(r) for r in rates) and tokens.get("out") is not None:
            cost = ((tokens.get("in") or 0) * rates[0] + (tokens.get("cached") or 0) * rates[1]
                    + ((tokens.get("out") or 0) + (tokens.get("reasoning") or 0)) * rates[2]) / 1e6
            out.update(cost_usd=cost, cost_source="priced")
    return out
