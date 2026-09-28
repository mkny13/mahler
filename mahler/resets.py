"""Spend banked Codex reset credits before they expire (mahler#557, D35).

A banked reset is free, refreshes both of the login's Codex windows, moves the
weekly reset date, and expires unused — today nothing spends them, so they are
lost. This pass runs in the tick right after the usage refresh and decides, per
Codex login, whether a banked reset should be spent now:

- Rule (a): the login's weekly window is at or above its hard line, the next
  natural weekly reset is more than 24 hours away, and there is ready work
  routed to this login. The login is full with work waiting — a reset turns
  the wall into a fresh week immediately.
- Rule (b): a banked reset expires within 48 hours, the week is at least 60%
  used, and the natural weekly reset is not due before the credit expires (a
  reset survives the natural weekly reset, so when the week rolls over first
  the credit can still be spent against the fresh week later). Without this
  the credit would expire on a nearly fresh week.

Never spent: purchased resets. A credit is only treated as banked — and only
ever targeted — when the rate-limits read showed it available *with* an expiry;
if the fresh read cannot produce such a row, nothing is spent. A dry-run tick
(`mahler tick --dry-run`) describes the proposed spend but never writes attempt
state or calls the consume RPC, so a diagnostic tick can never spend a real
reset. Every spend pings ntfy with the login and the new
weekly reset time, and every credit about to expire unspent is pinged once so
Mike can still spend it by hand.
"""

import json
import uuid
from datetime import timedelta

from . import config, platforms, router, usage
from .ledger import iso, parse

EXPIRY_WINDOW = timedelta(hours=48)     # rule (b): act on credits expiring this soon
MIN_WEEKLY_PCT = 60.0                   # rule (b): never spend into a nearly fresh week
RESET_MIN_DISTANCE = timedelta(hours=24)  # rule (a): natural weekly reset this far out
RETRY_AFTER = timedelta(minutes=30)     # between spend attempts on the same credit


def _codex_logins(cfg):
    """One representative platform per Codex login: (account, quota_group).

    Low/medium/high variants share a login's quota; the credit lives on the
    login, so the decision is made once per group and fanned out to the peers.
    """
    seen, order = {}, []
    for name, pc in cfg["platforms"].items():
        if pc.get("kind") != "codex" or not pc.get("enabled", True):
            continue
        key = (config.account_of(pc), pc.get("quota_group", name))
        if key not in seen:
            seen[key] = name
            order.append(name)
    return [(name, cfg["platforms"][name]) for name in order]


def _ready_work_routed(ctx, projects, account, group_names):
    """True when some project on `account` has ready work whose build route
    reaches one of this login's platforms. Headroom is deliberately ignored:
    rule (a) fires exactly when the login is over its line, which no
    headroom-aware pick would ever report."""
    for p in projects:
        pol = ctx.policy(p["name"])
        if account not in config.accounts_of(pol):
            continue
        if not ctx.led.items(p["name"], ["ready"]):
            continue
        cands = (router.candidates_for_priority(
            ctx.cfg, "build", config.accounts_of(pol), pol.get("routing") or {})
            if config.account_mode_of(pol) == "priority" else
            router.candidates_for_accounts(ctx.cfg, "build", config.accounts_of(pol)))
        if set(cands) & set(group_names):
            return True
    return False


def _ping_expiring(ctx, account, expiry, weekly_pct):
    """A banked reset will expire and the rule did not spend it — say so once."""
    led = ctx.led
    flag = f"notified:reset-expiring:{account}:{iso(expiry)}"
    if led.get_kv(flag):
        return
    if ctx.dry_run:
        ctx.say(f"codex ({account}): banked reset expiring {iso(expiry)} — would ping")
        return
    led.set_kv(flag, iso(led.now()))
    led.event("reset_expiring", detail={"account": account, "expires": iso(expiry),
                                        "weekly_pct": weekly_pct})
    ctx.ping(f"Codex ({account}): banked reset expiring unused",
             f"A banked reset on the {account} Codex login expires "
             f"{router.fmt_countdown(expiry - led.now())} from now and the spend "
             f"rule did not fire (weekly at {weekly_pct:.0f}%). Spend it in "
             "ChatGPT if you want it.", priority="default", tags="warning,hourglass")
    ctx.say(f"codex ({account}): banked reset expiring {iso(expiry)} — pinged")


def _record_usage(ctx, name, account, samples):
    """Fan a fresh reading out across the login's quota group, as the probe does."""
    led, cfg = ctx.led, ctx.cfg
    for peer in usage.quota_peers(cfg, name):
        if config.account_of(cfg["platforms"][peer]) != account:
            continue
        led.set_kv(f"probe:{peer}", iso(led.now()))
        if isinstance(samples, platforms.CodexUsage):
            led.set_kv(f"codex:quota:{peer}", json.dumps(
                {**samples.metadata, "sampled_at": iso(led.now())}))
        for w, pct, resets in samples:
            led.record_usage(peer, w, pct, resets)


def _spend(ctx, name, pconf, account, rule):
    """Target the earliest-expiring provably-banked credit and consume it."""
    led, cfg = ctx.led, ctx.cfg
    env = usage.codex_env(cfg, account)
    if env is None:
        ctx.say(f"codex ({account}): no usable login — not spending")
        return
    fresh = platforms.probe_codex(env=env, keep_ids=True)
    if not isinstance(fresh, platforms.CodexUsage):
        ctx.say(f"codex ({account}): couldn't read reset credits — not spending")
        return
    rows = [c for c in fresh.metadata.get("credit_rows", [])
            if c.get("status") == "available" and c.get("expires_at")]
    if not rows:
        # availableCount > 0 with no expiring row: the banked kind cannot be
        # proven, so the credit is left alone (never spend a purchased reset).
        ctx.say(f"codex ({account}): no credit with a known expiry — not spending")
        return
    rows.sort(key=lambda c: c["expires_at"])
    credit = rows[0]
    attempt_key = f"reset:attempt:{account}"
    try:
        stored = json.loads(led.get_kv(attempt_key) or "null")
    except ValueError:
        stored = None
    if (isinstance(stored, dict) and stored.get("credit_id") == credit["id"]
            and (t := parse(stored.get("at"))) and led.now() - t < RETRY_AFTER):
        return                      # same attempt already in flight or just failed
    if ctx.dry_run:
        # A dry-run tick is a diagnostic: it must never spend a real reset.
        # Stop here, before attempt state is written or the consume RPC is
        # called, and only describe the spend that would have happened.
        ctx.say(f"codex ({account}): dry-run — would spend banked reset credit "
                f"{credit['id']} (rule {rule}, expires {credit['expires_at']})")
        return
    if not isinstance(stored, dict) or stored.get("credit_id") != credit["id"]:
        stored = {"credit_id": credit["id"], "key": str(uuid.uuid4()),
                  "at": iso(led.now())}
        led.set_kv(attempt_key, json.dumps(stored))
    outcome = platforms.consume_codex_credit(env=env, credit_id=credit["id"],
                                             idempotency_key=stored["key"])
    if outcome in (None, "noCredit", "nothingToReset"):
        ctx.say(f"codex ({account}): reset credit not spent ({outcome or 'call failed'})")
        return
    samples = platforms.probe_codex(env=env)   # free read: the new window state
    weekly_reset = next((r for w, _, r in samples if w == "weekly"), None)
    if isinstance(samples, platforms.CodexUsage):
        _record_usage(ctx, name, account, samples)
    flag = f"notified:reset-spent:{account}:{credit['id']}"
    if not led.get_kv(flag) and not ctx.dry_run:
        led.set_kv(flag, iso(led.now()))
        led.event("reset_spent", detail={
            "account": account, "rule": rule, "credit": credit["id"],
            "expires": credit["expires_at"], "weekly_reset": weekly_reset})
        resets_txt = iso(parse(weekly_reset)) if weekly_reset else "unknown"
        ctx.ping(f"Codex ({account}): banked reset spent",
                 f"Spent a banked reset on the {account} Codex login (rule {rule}). "
                 f"Both windows are fresh; the weekly window now resets {resets_txt}.",
                 priority="default", tags="hourglass")
    ctx.say(f"codex ({account}): banked reset spent (rule {rule}, outcome {outcome}); "
            f"weekly resets {weekly_reset or 'unknown'}")


def spend_banked(ctx, projects):
    """The tick pass: decide per Codex login whether a banked reset is due.

    Exception-safe per login: one broken login never blocks the others, and a
    broken pass never blocks the tick."""
    for name, pconf in _codex_logins(ctx.cfg):
        try:
            _handle_login(ctx, projects, name, pconf)
        except Exception as e:       # noqa: BLE001 — one login must not break the tick
            ctx.say(f"codex reset pass failed for {name} — {e}")


def _handle_login(ctx, projects, name, pconf):
    led, cfg, now = ctx.led, ctx.cfg, ctx.led.now()
    account = config.account_of(pconf)
    group = {peer for peer in usage.quota_peers(cfg, name)
             if config.account_of(cfg["platforms"][peer]) == account}
    quota = router.codex_quota(led, name, pconf)
    if not quota:
        return
    sampled = router._ts(quota.get("sampled_at"))
    if not sampled or now - sampled >= timedelta(minutes=pconf.get("stale_minutes", 15)):
        return                       # stale reading: decide on nothing (fail closed)
    weekly = led.usage(name).get("weekly") or {}
    weekly_pct, weekly_reset = weekly.get("used_pct"), router._ts(weekly.get("resets_at"))
    if weekly_pct is None:
        return
    expiries = sorted(e for e in (router._ts(x) for x in quota.get("credit_expiries") or [])
                      if e and e > now)
    if not expiries:
        return
    expiry = expiries[0]
    _, hard = router.effective_lines(led, name, pconf, "weekly")
    rule_a = (weekly_pct >= hard and weekly_reset
              and weekly_reset > now + RESET_MIN_DISTANCE
              and _ready_work_routed(ctx, projects, account, group))
    rule_b = (expiry <= now + EXPIRY_WINDOW and weekly_pct >= MIN_WEEKLY_PCT
              and weekly_reset and weekly_reset >= expiry)
    if rule_a or rule_b:
        _spend(ctx, name, pconf, account, "a" if rule_a else "b")
    elif expiry <= now + EXPIRY_WINDOW:
        _ping_expiring(ctx, account, expiry, weekly_pct)