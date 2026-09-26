"""Daily digest ping (mahler#6): one ntfy message a day, first tick after 08:00.

A roll-up of the last 24h — items shipped, items waiting on the owner
(needs_you / failed), handoffs, and current quota per platform — sent from the
tick like the backups are: informational, so it runs even while paused. The
send is gated by a kv key (`last_digest_date`), so restarts never double-send,
and the key is written only after a successful POST, so a failed send retries
on the next tick instead of being lost for the day.
"""

import json
from datetime import timedelta

from . import notify
from .ledger import iso

KV_KEY = "last_digest_date"
DEFAULT_HOUR = 8
TITLE = "Mahler daily digest"


def _local_now():
    """The tick's clock is UTC; the digest fires on *local* 08:00."""
    from datetime import datetime
    return datetime.now().astimezone()


# ---------- gathering ----------

def gather(led, cfg, since, now=None, local_now=None, scorecard_rows=None):
    """Collect the last-24h picture from the ledger. `since` is an aware UTC
    datetime; event `at` stamps are ISO UTC strings."""
    now = now or led.now()

    shipped, handoffs = [], []
    for ev in led.q("SELECT * FROM events WHERE kind='state' AND at>=? ORDER BY id",
                    (iso(since),)):
        detail = ev["detail"] or ""
        if "-> done" in detail:
            item = led.item(ev["project"], ev["number"]) if ev["project"] else None
            shipped.append({"project": ev["project"], "number": ev["number"],
                            "title": item["title"] if item else None})
        if "handoff" in detail or detail.startswith("handed to your session"):
            handoffs.append({"project": ev["project"], "number": ev["number"],
                             "detail": detail})

    waiting = [{"project": it["project"], "number": it["number"], "state": it["state"],
                "title": it["title"]}
               for it in led.items(states=("needs_you", "failed"))]

    usage = []
    for name, pconf in cfg.get("platforms", {}).items():
        if not pconf.get("enabled", True):
            continue
        rows = led.usage(name)
        usage.append({
            "platform": name,
            "5h": rows["5h"]["used_pct"] if "5h" in rows else None,
            "weekly": rows["weekly"]["used_pct"] if "weekly" in rows else None,
        })

    weekly = weekly_models(led, cfg, scorecard_rows) if (local_now or now.astimezone()).weekday() == 0 else None
    return {"models": weekly, "shipped": shipped, "waiting": waiting, "handoffs": handoffs,
            "usage": usage, "since": since, "now": now}


# ---------- the text builder (pure; unit-tested) ----------

def _label(project, number):
    return f"{project}#{number}" if project else f"#{number}"


def _title_of(entry):
    return entry.get("title") or "(untitled)"


def _pct(v):
    return f"{v:.0f}%" if v is not None else "no data"


def format_digest(data):
    """Turn gathered stats into the digest body. Pure: no clock, no I/O."""
    lines = []
    shipped = data["shipped"]
    lines.append(f"Shipped in the last 24h ({len(shipped)}):")
    if shipped:
        for s in shipped:
            lines.append(f"- {_label(s['project'], s['number'])} {_title_of(s)}")
    else:
        lines.append("- none")

    waiting = data["waiting"]
    lines.append(f"Waiting on you ({len(waiting)}):")
    if waiting:
        for w in waiting:
            lines.append(f"- {_label(w['project'], w['number'])} [{w['state']}] "
                         f"{_title_of(w)}")
    else:
        lines.append("- nothing")

    handoffs = data["handoffs"]
    lines.append(f"Handoffs ({len(handoffs)}):")
    if handoffs:
        for h in handoffs:
            lines.append(f"- {_label(h['project'], h['number'])} — {h['detail']}")
    else:
        lines.append("- none")

    lines.append("Quota:")
    for u in data["usage"]:
        lines.append(f"- {u['platform']}: 5h {_pct(u['5h'])}, "
                     f"week {_pct(u['weekly'])}")
    if data.get("models") is not None:
        lines.append("Weekly model scorecard:")
        lines.extend(data["models"]["lines"])
    return "\n".join(lines)


# ---------- gating and the tick hook ----------

def should_send(led, local_now, hour=DEFAULT_HOUR):
    """True on the first tick at/after `hour` local that hasn't sent today."""
    today = local_now.date().isoformat()
    if local_now.hour < hour:
        return False
    return led.get_kv(KV_KEY) != today


def maybe_send(ctx):
    """Tick hook. Never raises — `mahler tick` must not break over a digest."""
    try:
        _maybe_send(ctx)
    except Exception as e:                       # noqa: BLE001 — any digest failure
        ctx.say(f"digest failed (continuing) — {e}")


def _maybe_send(ctx):
    if ctx.dry_run:
        return
    led, cfg = ctx.led, ctx.cfg
    hour = cfg.get("digest", {}).get("hour", DEFAULT_HOUR)
    local = _local_now()
    if not should_send(led, local, hour):
        return
    now = led.now()
    data = gather(led, cfg, since=now - timedelta(hours=24), now=now, local_now=local,
                  scorecard_rows=getattr(ctx, "scorecard_rows", None) if local.weekday() == 0 else None)
    body = format_digest(data)
    if notify.send(cfg, TITLE, body):
        if data.get("models") is not None:
            led.set_kv("weekly_model_scorecard", json.dumps(data["models"]["snapshot"]))
        led.set_kv(KV_KEY, local.date().isoformat())
        ctx.say(f"digest sent for {local.date().isoformat()}")
    else:
        ctx.say("digest: ntfy send failed — will retry next tick")


def weekly_models(led, cfg, rows=None):
    """Compare to the last successfully delivered weekly snapshot, not wall time."""
    from . import scorecard
    rows = scorecard.table(led, cfg) if rows is None else rows
    try:
        previous = json.loads(led.get_kv("weekly_model_scorecard") or "{}")
    except (ValueError, TypeError):
        previous = {}
    if not isinstance(previous, dict):
        previous = {}
    lines, snapshot = [], {}
    for role, size in sorted({(r["role"], r["size"]) for r in rows
                              if r["role"] in {"build", "plan"}},
                             key=lambda pair: (pair[0], pair[1] or "", pair[1] is not None)):
        best = scorecard.ranked(rows, role, size)[0]
        lines.append(f'- Top {role}/{size or "unknown size"}: {scorecard.summary(best)}')
    for row in rows:
        key = json.dumps(scorecard.identity(row))
        old = previous.get(key, {})
        snapshot[key] = {"good": row["status"] == "good", "dominated": row["dominated"]}
        name = f'{row["role"]}/{row["size"] or "unknown size"} · {row["model"] or "unknown model"} · {row["effort"] or "default"} ({row["platform"]})'
        if snapshot[key]["good"] and not old.get("good"):
            lines.append(f"- Newly good: {name}")
        if row["dominated"] and not old.get("dominated"):
            lines.append(f"- Newly dominated: {name}")
    unpriced = sorted({r["model"] or "unknown model" for r in rows if not r["priced"]})
    if unpriced:
        lines.append("- Unpriced models: " + ", ".join(unpriced))
    return {"lines": lines or ["- No attempts in this window."], "snapshot": snapshot}
