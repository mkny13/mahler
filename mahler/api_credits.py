"""Spend the work Team plan's Claude API credits (DESIGN D41, mahler#903).

The Team plan includes a pooled Claude API credit balance each billing cycle
($260 today; it changes with seat count). Mahler may spend `allowance_usd`
($20) of it per cycle at will. In the last `burst_lead_hours` before the cycle
ends, unused credits are about to expire, so the line rises to whatever the
whole organization has left: `pool_usd - org spend this cycle - margin`.

The credits do not cover Claude Code, so the `claude` CLI is never used here
(and config.CREDENTIAL_VARS still strips ANTHROPIC_API_KEY from every other
run). A credits platform is the Agent SDK runner (`kind = "agent-sdk"`,
sdk_runner/) pointed at the gateway through ANTHROPIC_BASE_URL, with a local
per-run token as its ANTHROPIC_API_KEY; the real workspace key stays in the
gateway.

Fail closed (D8):
  * The allowance is measured against Mahler's own spend, the larger of its
    priced runs (`runs.cost_usd`, an unpriced run counts as `run_reserve_usd`)
    and the Admin Cost API's figure for Mahler's workspace.
  * A stale or unknown org reading never enables the burst: the line stays at
    the allowance. A fresh reading that shows the pool used up blocks even the
    allowance.
  * "credit balance is too low" marks the pool exhausted until the cycle ends;
    an authentication failure parks it until a free key check succeeds. A key
    the daemon can't read (login keychain locked, item missing) makes the
    platform unavailable. None of these retry in a loop.

Secrets come from the macOS login keychain at the moment they're needed, are
held only in memory, sent only to api.anthropic.com (or the one child's env),
and are never logged or persisted. The admin key never enters a run's env.
"""

import calendar
import http.client
import http.server
import json
import os
import secrets
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from . import platforms, redact


# Local copies of ledger.iso/parse: config imports this module, and ledger
# imports config, so importing ledger here would be circular.
def iso(dt):
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def parse(s):
    if not s:
        return None
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

WINDOW = "cycle"                 # the usage-table window for console gauges
API = "https://api.anthropic.com"
COST_URL = f"{API}/v1/organizations/cost_report"
MODELS_URL = f"{API}/v1/models"
ANTHROPIC_VERSION = "2023-06-01"

DEFAULT_API_CREDITS = {
    "enabled": False,
    "account": "work",               # D25: the pool belongs to this account only
    "pool_usd": 260.0,               # the org's grant per cycle (changes with seats)
    "allowance_usd": 20.0,           # Mahler may spend this much per cycle at will
    # The cycle ends (and the next starts) on this day of the month at this
    # UTC time; a day past a month's end clamps to its last day. Observed
    # 2026-10-09: grant expires 2026-10-15 (UTC).
    "cycle_anchor_day": 15,
    "cycle_anchor_time": "00:00",
    "burst_lead_hours": 48,          # before the cycle ends: spend the pool's rest
    "safety_margin_usd": 10.0,       # never plan to spend the pool's last dollars
    # Conservative per-run cost bound. Cline has no per-run dollar cap, so a
    # run only starts when this much headroom is left after every live run's
    # own reserve, and an unpriced finished run counts as this much spend.
    "run_reserve_usd": 5.0,
    "cost_cache_minutes": 15,        # re-read the Cost API this often when idle
    "cost_active_minutes": 5,        # ...and this often while a credits run is live
    "cost_stale_minutes": 60,        # older org readings are unknown (allowance only)
    "workspace_id": "",              # Mahler's Console workspace, for its own spend
    "keychain_account": "work",
    "api_key_service": "mahler-anthropic-api",
    "admin_key_service": "mahler-anthropic-admin",
    "api_key_expires": "",           # YYYY-MM-DD; warn 14 days out, daily from 3
    "admin_key_expires": "",
    # Confirmed grants only (D41 step 3): [{"granted_at": "2026-10-09",
    # "expires_at": "2026-10-15T00:00:00Z", "pool_usd": 260.0}, ...]. Spending
    # is authorized only while `now` falls inside one of these windows — never
    # inferred from cycle_anchor_day, which is forecast-only (see below).
    "grants": [],
    # The harness's enforceable hook (D41 step 2): a standard-library gateway
    # between the runner and api.anthropic.com. Only these models and this
    # output-token ceiling are approved; anything else is refused before any
    # request is forwarded.
    "allowed_models": ["claude-sonnet-5-5"],
    "max_output_tokens": 8192,
    "gateway_host": "127.0.0.1",     # loopback only — validate() refuses anything else
    "base_url_env": "ANTHROPIC_BASE_URL",   # the harness's base-URL override
    "model_pricing": {},             # per-model USD/MTok override of MODEL_PRICING
}

KEY_OK, KEY_LOCKED, KEY_MISSING, KEY_ERROR = "ok", "locked", "missing", "error"
KEY_CHECK_MINUTES = 15               # how often to confirm the API key is readable
AUTH_RECHECK_HOURS = 6               # a parked key gets one free check this often
COST_MIN_INTERVAL = timedelta(minutes=1)   # never poll the Cost API faster

AUTH_ERROR_WORDS = (
    "authentication_error", "invalid x-api-key", "invalid api key",
    "api key has expired", "api key expired", "api_key_expired", "key has been revoked",
)

KEY_STATUS_TEXT = {
    KEY_LOCKED: "the mini's login keychain is locked (security: User interaction is not allowed)",
    KEY_MISSING: "the key is not in the login keychain",
    KEY_ERROR: "the keychain could not be read",
}


class CostError(Exception):
    """The Cost API reading failed; the org's spend stays unknown."""


class CostAuthError(CostError):
    """The admin key was rejected (401/403)."""


# ---------- config ----------

def policy(cfg):
    out = dict(DEFAULT_API_CREDITS)
    out.update(cfg.get("api_credits") or {})
    return out


def is_credit_platform(pconf):
    return bool(pconf.get("api_credits"))


def _finite(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and value == value and value not in (float("inf"), float("-inf")))


def _parse_hhmm(value):
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise ValueError
    h, m = int(value[:2]), int(value[3:])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError
    return h, m


def parse_expiry(value):
    """'YYYY-MM-DD' -> date, '' -> None. Raises ValueError."""
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(str(value))


def parse_grant_dt(value):
    """A grant boundary: 'YYYY-MM-DD' floors to that UTC day (the conservative
    cutoff when no exact instant is known); a full ISO datetime is exact."""
    if isinstance(value, str) and len(value) == 10:
        d = date.fromisoformat(value)
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    dt = parse(value)
    if dt is None:
        raise ValueError("empty grant date")
    return dt


def grant_id(grant):
    """A stable identity for a confirmed grant: its own granted_at, exactly
    as configured. Reservations are keyed by this, so a rewritten pool_usd
    or expires_at on the *same* grant still resolves prior attempts; only a
    new granted_at starts a new bucket."""
    return str(grant["granted_at"])


def validate_grants(grants):
    if not isinstance(grants, list):
        raise ValueError("api_credits.grants must be a list")
    seen = []
    for g in grants:
        if not isinstance(g, dict):
            raise ValueError("api_credits.grants entries must be tables")
        unknown = set(g) - {"granted_at", "expires_at", "pool_usd"}
        if unknown:
            raise ValueError(f"api_credits.grants: unknown fields {', '.join(sorted(unknown))}")
        if "granted_at" not in g or "expires_at" not in g:
            raise ValueError("api_credits.grants entries need granted_at and expires_at")
        try:
            start, end = parse_grant_dt(g["granted_at"]), parse_grant_dt(g["expires_at"])
        except (ValueError, TypeError):
            raise ValueError("api_credits.grants: granted_at/expires_at must be YYYY-MM-DD "
                             "or an ISO UTC datetime") from None
        if end <= start:
            raise ValueError(f"api_credits.grants: expires_at must be after granted_at "
                             f"({g['granted_at']!r})")
        if "pool_usd" in g and (not _finite(g["pool_usd"]) or g["pool_usd"] <= 0):
            raise ValueError("api_credits.grants: pool_usd must be a positive number")
        for s2, e2 in seen:
            if start < e2 and s2 < end:
                raise ValueError("api_credits.grants: entries must not overlap "
                                 f"({g['granted_at']!r})")
        seen.append((start, end))


def validate_model_pricing(pricing):
    if not isinstance(pricing, dict):
        raise ValueError("api_credits.model_pricing must be a table")
    for model, rates in pricing.items():
        if not isinstance(rates, dict):
            raise ValueError(f"api_credits.model_pricing.{model} must be a table")
        unknown = set(rates) - {"input", "output", "cache_write", "cache_read"}
        if unknown:
            raise ValueError(f"api_credits.model_pricing.{model}: unknown fields "
                             f"{', '.join(sorted(unknown))}")
        for field in ("input", "output", "cache_write", "cache_read"):
            if field in rates and (not _finite(rates[field]) or rates[field] < 0):
                raise ValueError(f"api_credits.model_pricing.{model}.{field} must be "
                                 "a non-negative number")


def _bounds(pool, now):
    """(start, end) for whatever's active: a confirmed grant, or — with
    none — the forecast window, for display and cache-keying only."""
    grant = current_grant(pool, now)
    if grant:
        return grant["start"], grant["end"]
    return cycle_bounds(now, pool["cycle_anchor_day"], pool["cycle_anchor_time"])


def current_grant(pol, now):
    """The confirmed grant covering `now`, or None. Never inferred from
    cycle_anchor_day/time — those are forecast-only (D41 step 3): spending
    is refused with no confirmed grant, however plausible a recurring
    monthly anchor looks."""
    for g in pol.get("grants") or []:
        start, end = parse_grant_dt(g["granted_at"]), parse_grant_dt(g["expires_at"])
        if start <= now < end:
            return {"id": grant_id(g), "start": start, "end": end,
                    "pool_usd": float(g.get("pool_usd", pol["pool_usd"]))}
    return None


def validate(cfg):
    """Fail closed on a policy that could spend the wrong account or money."""
    from .config import DEFAULT_ACCOUNT, account_of
    raw = cfg.get("api_credits") or {}
    if not isinstance(raw, dict):
        raise ValueError("api_credits must be a table")
    unknown = set(raw) - set(DEFAULT_API_CREDITS)
    if unknown:
        raise ValueError(f"api_credits: unknown fields {', '.join(sorted(unknown))}")
    pol = policy(cfg)
    if type(pol["enabled"]) is not bool:
        raise ValueError("api_credits.enabled must be a boolean")
    account = pol["account"]
    if not isinstance(account, str) or not account or account == DEFAULT_ACCOUNT:
        raise ValueError("api_credits.account must name a non-personal account "
                         "(the credits belong to the work Team plan, DESIGN D25/D41)")
    for key in ("pool_usd", "allowance_usd", "safety_margin_usd", "run_reserve_usd"):
        if not _finite(pol[key]) or pol[key] < 0:
            raise ValueError(f"api_credits.{key} must be a non-negative number")
    if pol["allowance_usd"] > pol["pool_usd"]:
        raise ValueError("api_credits.allowance_usd must not exceed pool_usd")
    if pol["safety_margin_usd"] >= pol["pool_usd"] and pol["pool_usd"] > 0:
        raise ValueError("api_credits.safety_margin_usd must be below pool_usd")
    day = pol["cycle_anchor_day"]
    if type(day) is not int or not 1 <= day <= 31:
        raise ValueError("api_credits.cycle_anchor_day must be an integer from 1 to 31")
    try:
        _parse_hhmm(pol["cycle_anchor_time"])
    except (ValueError, TypeError):
        raise ValueError("api_credits.cycle_anchor_time must be HH:MM (UTC)") from None
    lead = pol["burst_lead_hours"]
    if not _finite(lead) or not 0 <= lead <= 24 * 14:
        raise ValueError("api_credits.burst_lead_hours must be from 0 to 336")
    for key in ("cost_cache_minutes", "cost_active_minutes", "cost_stale_minutes"):
        if not _finite(pol[key]) or pol[key] < 1:
            raise ValueError(f"api_credits.{key} must be at least 1")
    for key in ("keychain_account", "api_key_service", "admin_key_service"):
        if not isinstance(pol[key], str) or not pol[key].strip():
            raise ValueError(f"api_credits.{key} must be a non-empty string")
    if not isinstance(pol["workspace_id"], str):
        raise ValueError("api_credits.workspace_id must be a string")
    for key in ("api_key_expires", "admin_key_expires"):
        try:
            parse_expiry(pol[key])
        except (ValueError, TypeError):
            raise ValueError(f"api_credits.{key} must be YYYY-MM-DD or empty") from None
    validate_grants(pol["grants"])
    if not isinstance(pol["allowed_models"], list) or not pol["allowed_models"] or not all(
            isinstance(m, str) and m.strip() for m in pol["allowed_models"]):
        raise ValueError("api_credits.allowed_models must be a non-empty list of model names")
    if type(pol["max_output_tokens"]) is not int or pol["max_output_tokens"] <= 0:
        raise ValueError("api_credits.max_output_tokens must be a positive integer")
    if pol["gateway_host"] not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("api_credits.gateway_host must be loopback (127.0.0.1, localhost or ::1) "
                         "— the gateway holds the workspace key and must never bind a routable "
                         "interface")
    if not isinstance(pol["base_url_env"], str) or not pol["base_url_env"].strip():
        raise ValueError("api_credits.base_url_env must be a non-empty string")
    validate_model_pricing(pol["model_pricing"])
    for name, pconf in cfg.get("platforms", {}).items():
        if not is_credit_platform(pconf):
            continue
        if pconf.get("kind") != "agent-sdk":
            raise ValueError(f"platform {name!r}: API credits run only through the Agent SDK "
                             "runner (kind = \"agent-sdk\") — Claude Code is not covered "
                             "(DESIGN D41)")
        if pconf.get("enabled") and account_of(pconf) != account:
            raise ValueError(f"platform {name!r}: an enabled API-credits platform must "
                             f"spend account {account!r}, not {account_of(pconf)!r} (D25)")


def attach(cfg):
    """Give each credits platform its pool's settings and peers, so the
    router can judge it from the ledger alone."""
    pol = policy(cfg)
    plats = cfg.get("platforms", {})
    groups = {}
    for name, pconf in plats.items():
        if is_credit_platform(pconf):
            groups.setdefault(pconf.get("quota_group", name), []).append(name)
    for group, names in groups.items():
        for name in names:
            plats[name]["credit_pool"] = {**pol, "group": group, "peers": list(names)}
    return cfg


# ---------- billing cycle ----------

def _anchor(year, month, day, hh, mm):
    last = calendar.monthrange(year, month)[1]
    return datetime(year, month, min(day, last), hh, mm, tzinfo=timezone.utc)


def _shift(year, month, delta):
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def cycle_bounds(now, day, hhmm="00:00"):
    """The billing cycle containing `now`: [start, end), both UTC. The anchor
    day clamps to short months (31 -> Feb 28/29, Apr 30)."""
    hh, mm = _parse_hhmm(hhmm)
    now = now.astimezone(timezone.utc)
    this = _anchor(now.year, now.month, day, hh, mm)
    if now >= this:
        y, m = _shift(now.year, now.month, 1)
        return this, _anchor(y, m, day, hh, mm)
    y, m = _shift(now.year, now.month, -1)
    return _anchor(y, m, day, hh, mm), this


# ---------- keychain ----------

def read_key(service, account, run=None):
    """-> (secret|None, status). Never prompts: stdin is closed and the call is
    bounded, so a locked keychain reports instead of waiting for a person."""
    run = run or subprocess.run           # resolved per call so tests can patch it
    try:
        r = run(["security", "find-generic-password", "-s", service, "-a", account, "-w"],
                capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (subprocess.SubprocessError, OSError):
        return None, KEY_ERROR
    err = (r.stderr or "").lower()
    if r.returncode == 0 and (r.stdout or "").strip():
        return r.stdout.strip(), KEY_OK
    if "user interaction is not allowed" in err or "-25308" in err:
        return None, KEY_LOCKED
    if r.returncode == 44 or "could not be found" in err:
        return None, KEY_MISSING
    return None, KEY_ERROR


def _api_key(pool, run=None):
    return read_key(pool["api_key_service"], pool["keychain_account"], run=run)


def _admin_key(pool, run=None):
    return read_key(pool["admin_key_service"], pool["keychain_account"], run=run)


def api_key_for_run(pconf, run=None):
    """The workspace API key for one run's environment, or raise. The admin
    key is never returned here."""
    pool = pconf.get("credit_pool")
    if not pool:
        raise RuntimeError("API credits are not configured for this platform")
    secret, status = _api_key(pool, run=run)
    if status != KEY_OK:
        raise RuntimeError(f"Anthropic API key unavailable: {KEY_STATUS_TEXT[status]}")
    return secret


# ---------- Cost API ----------

def _amount(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise CostError("malformed amount") from None
    if not amount.is_finite():
        raise CostError("non-finite amount")
    return amount


def parse_cost_page(body, workspace_id=""):
    """One cost_report page -> (org_cents, workspace_cents). Amounts are
    decimal strings in cents. A malformed page raises, never reads as zero."""
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise CostError("malformed cost report")
    org = Decimal(0)
    ws = Decimal(0)
    for bucket in body["data"]:
        if not isinstance(bucket, dict) or not isinstance(bucket.get("results"), list):
            raise CostError("malformed cost bucket")
        for result in bucket["results"]:
            if not isinstance(result, dict) or "amount" not in result:
                raise CostError("malformed cost result")
            currency = result.get("currency", "USD")
            if currency not in (None, "USD"):
                raise CostError(f"unexpected currency {currency!r}")
            amount = _amount(result["amount"])
            org += amount
            if workspace_id and result.get("workspace_id") == workspace_id:
                ws += amount
    return org, ws


def fetch_cost(admin_key, since, workspace_id="", opener=None, timeout=20, max_pages=20):
    """Org-wide (and Mahler workspace) spend since `since`, in USD.

    `since` is floored to its UTC day, because the report's buckets are daily:
    the reading can only overcount, never undercount, what this cycle spent.
    -> {"org_usd": float, "workspace_usd": float|None}. Raises CostError."""
    opener = opener or urllib.request.urlopen
    start = since.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    params = [("starting_at", start.strftime("%Y-%m-%dT%H:%M:%SZ")),
              ("bucket_width", "1d"), ("limit", "31")]
    if workspace_id:
        params.append(("group_by[]", "workspace_id"))
    org, ws, page = Decimal(0), Decimal(0), None
    for _ in range(max_pages):
        query = params + ([("page", page)] if page else [])
        req = urllib.request.Request(f"{COST_URL}?{urllib.parse.urlencode(query)}", headers={
            "x-api-key": admin_key, "anthropic-version": ANTHROPIC_VERSION,
            "User-Agent": "mahler"})
        try:
            with opener(req, timeout=timeout) as r:
                body = json.load(r)
        except urllib.error.HTTPError as e:
            code = e.code
            e.close()
            if code in (401, 403):
                raise CostAuthError(f"HTTP {code}") from None
            raise CostError(f"HTTP {code}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise CostError(type(e).__name__) from None
        o, w = parse_cost_page(body, workspace_id)
        org += o
        ws += w
        if not body.get("has_more"):
            return {"org_usd": float(org / 100),
                    "workspace_usd": float(ws / 100) if workspace_id else None}
        page = body.get("next_page")
        if not isinstance(page, str) or not page:
            raise CostError("has_more without next_page")
    raise CostError("too many pages")


def check_api_key(api_key, opener=None, timeout=15):
    """A free authentication check (lists models; no tokens). -> 'ok', 'auth'
    or 'error'."""
    opener = opener or urllib.request.urlopen
    req = urllib.request.Request(MODELS_URL, headers={
        "x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION, "User-Agent": "mahler"})
    try:
        with opener(req, timeout=timeout):
            return "ok"
    except urllib.error.HTTPError as e:
        code = e.code
        e.close()
        return "auth" if code in (401, 403) else "error"
    except (urllib.error.URLError, OSError, ValueError):
        return "error"


# ---------- ledger state ----------

def _kv_json(led, key):
    try:
        value = json.loads(led.get_kv(key) or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _key(kind, group):
    return f"api_credits:{kind}:{group}"


def own_spend(led, peers, since, reserve):
    """Mahler's settled spend on `peers` this cycle. An ended run with no
    price counts as `reserve`: its cost is unknown, not zero."""
    if not peers:
        return 0.0, 0
    rows = led.q("SELECT cost_usd, started_at, outcome FROM runs WHERE status='ended' "
                 f"AND platform IN ({','.join('?' * len(peers))})", tuple(peers))
    total, unpriced = 0.0, 0
    for row in rows:
        try:
            started = parse(row["started_at"])
        except (TypeError, ValueError):
            started = None
        if not started or started < since:
            continue
        outcome = row["outcome"] or ""
        if outcome.startswith(("launch failed", "not claimed")):
            continue                     # never reached the agent
        cost = row["cost_usd"]
        if cost is None or not _finite(cost) or cost < 0:
            unpriced += 1
            total += reserve
        else:
            total += cost
    return total, unpriced


def snapshot(led, name, pconf):
    """Everything known about the pool right now, computed from the ledger.

    -> dict with the grant, spends, the line in force and why. Money in USD.
    Authorization always keys off a confirmed grant (D41 step 3); with none,
    cycle_anchor_day/time only forecast where the *next* one is expected, for
    display — they never open the line."""
    pool = pconf.get("credit_pool") or {}
    now = led.now()
    if not pool:
        return {"configured": False, "state": "hard", "blocking": "not configured"}
    group, peers = pool["group"], pool["peers"]
    reserve = float(pool["run_reserve_usd"])
    grant = current_grant(pool, now)
    forecast_start, forecast_end = cycle_bounds(now, pool["cycle_anchor_day"], pool["cycle_anchor_time"])
    if grant:
        start, end, pool_usd, gid = grant["start"], grant["end"], grant["pool_usd"], grant["id"]
    else:
        start, end, pool_usd, gid = forecast_start, forecast_end, float(pool["pool_usd"]), None
    settled, unpriced = own_spend(led, peers, start, reserve)
    exposure = led.credit_exposure(group, gid) if gid else 0.0
    active = sum(1 for r in led.active_runs() if r["platform"] in peers)
    cost = _kv_json(led, _key("cost", group))
    fetched = parse(cost.get("fetched_at")) if cost.get("fetched_at") else None
    fresh = bool(fetched and cost.get("cycle_start") == iso(start)
                 and now - fetched < timedelta(minutes=pool["cost_stale_minutes"]))
    org = cost.get("org_usd") if fresh else None
    ws = cost.get("workspace_usd") if fresh else None
    # `exposure` (the reservation ledger) is authoritative for every
    # gateway-mediated spend; `settled`/`ws` are independent cross-checks
    # that can only push the figure up, never down (D8). Anything they see
    # beyond what the reservation ledger already covers ("external_only")
    # shrinks the room left for a *new* reservation by the same amount, so
    # the atomic per-request check (ledger.credit_exposure + the new
    # estimate <= reservation_ceiling_usd) agrees with this display figure.
    mahler = max(settled, exposure, ws or 0.0)
    external_only = max(0.0, max(settled, ws or 0.0) - exposure)
    allowance = float(pool["allowance_usd"])
    margin = float(pool["safety_margin_usd"])
    in_window = bool(grant) and now >= end - timedelta(hours=pool["burst_lead_hours"])
    burst = in_window and org is not None
    allowance_left = allowance - mahler
    pool_left = pool_usd - org - margin if org is not None else None
    if burst:
        remaining = pool_left
    elif pool_left is not None:
        remaining = min(allowance_left, pool_left)
    else:
        remaining = allowance_left
    line = (pool_usd - margin) if burst else allowance
    unresolved_other = led.unresolved_credit_reservations(group, exclude_grant_id=gid)
    snap = {
        "configured": True, "enabled": bool(pool["enabled"]), "account": pool["account"],
        "group": group, "grant_confirmed": bool(grant), "grant_id": gid,
        "cycle_start": iso(start), "cycle_end": iso(end),
        "pool_usd": pool_usd, "allowance_usd": allowance, "safety_margin_usd": margin,
        "mahler_spend_usd": round(mahler, 4), "ledger_spend_usd": round(settled, 4),
        "reserved_exposure_usd": round(exposure, 4),
        "unresolved_other_grants": len(unresolved_other),
        "unpriced_runs": unpriced, "workspace_spend_usd": ws, "org_spend_usd": org,
        "org_sampled_at": cost.get("fetched_at"), "org_fresh": fresh,
        "org_error": cost.get("error") if not fresh else None,
        "burst_window": in_window, "burst": burst,
        "line_usd": line, "reservation_ceiling_usd": round(line - external_only, 4),
        "remaining_usd": round(remaining, 4), "active_runs": active,
        "run_reserve_usd": reserve,
        "keys": key_expiries(pool, now.date()),
    }
    exhausted = _kv_json(led, _key("exhausted", group))
    key_state = _kv_json(led, _key("key", group))
    parked = _kv_json(led, _key("auth", group))
    if not pool["enabled"]:
        state, why = "hard", "disabled ([api_credits] enabled = false)"
    elif key_state.get("status") and key_state["status"] != KEY_OK:
        state, why = "hard", f"API key unreadable: {KEY_STATUS_TEXT.get(key_state['status'], key_state['status'])}"
    elif not key_state.get("status"):
        state, why = "stale", "API key not checked yet"
    elif parked.get("since"):
        state, why = "hard", f"API key rejected (expired or revoked) since {parked['since']} — parked"
    elif not grant:
        state, why = "hard", "no confirmed grant covers now — spending refused (DESIGN D41)"
    elif exhausted.get("grant_id") == gid:
        state, why = "no_credit", "credit balance too low — exhausted until the grant resets"
    elif remaining <= 0:
        state, why = "hard", "line reached"
    elif remaining - active * reserve < reserve:
        state, why = "soft", f"under ${reserve:.2f} headroom for another run"
    else:
        state, why = "ok", None
    snap.update(state=state, blocking=why)
    return snap


def _countdown(delta):
    minutes = max(int(delta.total_seconds() // 60), 0)
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    return f"{days}d {hours}h" if days else f"{hours}h {mins}m" if hours else f"{mins}m"


def describe(snap, now):
    """One line for status, the router's reasons and the console."""
    if not snap.get("configured"):
        return "API credits not configured"
    end = parse(snap["cycle_end"])
    if snap["burst"]:
        org = snap["org_spend_usd"]
        parts = [f"burst: org ${org:.2f} of ${snap['pool_usd']:.0f} pool "
                 f"(margin ${snap['safety_margin_usd']:.0f}), ${max(snap['remaining_usd'], 0):.2f} left"]
    else:
        parts = [f"${snap['mahler_spend_usd']:.2f} of ${snap['allowance_usd']:.2f} allowance"]
        if snap["org_spend_usd"] is not None:
            parts.append(f"org ${snap['org_spend_usd']:.2f} of ${snap['pool_usd']:.0f}")
        if snap["burst_window"]:
            parts.append("org reading stale or unknown — no burst")
    if snap["grant_confirmed"]:
        parts.append(f"grant ends {end:%m-%d %H:%M} UTC (in {_countdown(end - now)})")
    else:
        parts.append(f"no confirmed grant — next forecast around {end:%m-%d} UTC, unconfirmed")
    if snap.get("unresolved_other_grants"):
        parts.append(f"{snap['unresolved_other_grants']} reservation(s) from a past grant "
                     "never resolved")
    for key in snap["keys"]:
        if key["days_left"] is not None and key["days_left"] <= 30:
            parts.append(f"{key['name']} key expires {key['expires']} "
                         f"({'expired' if key['days_left'] < 0 else f'in {key['days_left']}d'})")
    if snap.get("blocking") and snap["state"] != "ok":
        parts.insert(0, snap["blocking"])
    return " · ".join(parts)


def usage_state(led, name, pconf):
    """router.usage_state for a credits platform -> (state, detail)."""
    snap = snapshot(led, name, pconf)
    if not snap.get("configured"):
        return "hard", "API credits not configured"
    return snap["state"], describe(snap, led.now())


def bursting(led, name, pconf):
    if not is_credit_platform(pconf) or not pconf.get("credit_pool"):
        return False
    snap = snapshot(led, name, pconf)
    return snap["burst"] and snap["state"] == "ok"


def promote(cfg, led, order):
    """A bursting pool's platforms go first in a build route, like D23."""
    first = [n for n in order if bursting(led, n, cfg["platforms"].get(n, {}))]
    return first + [n for n in order if n not in first] if first else order


# ---------- key expiry ----------

def key_expiries(pool, today):
    out = []
    for label, key in (("API", "api_key_expires"), ("admin", "admin_key_expires")):
        try:
            expires = parse_expiry(pool.get(key))
        except (ValueError, TypeError):
            expires = None
        out.append({"name": label, "expires": expires.isoformat() if expires else None,
                    "days_left": (expires - today).days if expires else None})
    return out


def expiry_notice_due(days_left, last):
    """Notify 14 days before expiry, then daily from 3 days before (and daily
    once expired). `last` is the previous notice: {"stage", "date"}.
    -> stage name to send now, or None."""
    if days_left is None or days_left > 14:
        return None
    if days_left > 3:
        return None if last.get("stage") == "14d" else "14d"
    return "daily"


# ---------- the tick pass ----------

def _groups(cfg):
    seen = {}
    for name, pconf in cfg.get("platforms", {}).items():
        if is_credit_platform(pconf) and pconf.get("enabled") and pconf.get("credit_pool"):
            seen.setdefault(pconf["credit_pool"]["group"], name)
    return seen


def _alert_once(ctx, key, value, title, message, priority="high"):
    """Ping when a condition starts or changes, never again while it holds."""
    if ctx.led.get_kv(key) == value:
        return
    ctx.led.set_kv(key, value)
    ctx.ping(title, message, priority=priority, tags="warning")


def refresh(ctx, run=None, opener=None):
    """Per pool, once a tick: key readability, a parked key's free recheck,
    the cached org spend reading, expiry warnings and the console gauge.
    Exception-safe per pool."""
    for group, name in _groups(ctx.cfg).items():
        try:
            _refresh_group(ctx, group, name, run, opener)
        except Exception as e:   # noqa: BLE001 — one pool must not break the tick
            ctx.say(f"api credits ({group}): refresh failed — {redact.redact(repr(e))}")


def _refresh_group(ctx, group, name, run, opener):
    led, cfg = ctx.led, ctx.cfg
    pconf = cfg["platforms"][name]
    pool = pconf["credit_pool"]
    if not pool["enabled"] or ctx.dry_run:
        return
    now = led.now()
    start, end = _bounds(pool, now)

    # 1. Can the daemon read the workspace key? (one `security` call per 15m)
    key_state = _kv_json(led, _key("key", group))
    checked = parse(key_state.get("at")) if key_state.get("at") else None
    api_key = None
    if not checked or now - checked >= timedelta(minutes=KEY_CHECK_MINUTES):
        api_key, status = _api_key(pool, run=run)
        led.set_kv(_key("key", group), json.dumps({"status": status, "at": iso(now)}))
        if status == KEY_OK:
            led.set_kv(_key("key-alert", group), "")
        else:
            _alert_once(ctx, _key("key-alert", group), status,
                        f"Mahler: API credits paused — {pool['api_key_service']} unreadable",
                        f"{name} can't start: {KEY_STATUS_TEXT[status]}. "
                        + ("Log in on the mini's console (or unlock the login keychain) so the "
                           "daemon's session can read it." if status == KEY_LOCKED else
                           f"Check `security find-generic-password -s {pool['api_key_service']} "
                           f"-a {pool['keychain_account']}` on the mini."))

    # 2. A parked key gets one free authentication check every few hours.
    parked = _kv_json(led, _key("auth", group))
    if parked.get("since"):
        last = parse(parked.get("checked_at") or parked["since"])
        if now - last >= timedelta(hours=AUTH_RECHECK_HOURS):
            if api_key is None:
                api_key, status = _api_key(pool, run=run)
            verdict = check_api_key(api_key, opener=opener) if api_key else "error"
            if verdict == "ok":
                led.set_kv(_key("auth", group), "")
                led.event("api_credits_key_ok", detail=f"{group}: API key accepted again")
                ctx.ping(f"Mahler: {name} API key works again",
                         "The parked Team-plan API credits pool is back in rotation.",
                         priority="high", tags="white_check_mark")
            else:
                led.set_kv(_key("auth", group), json.dumps({**parked, "checked_at": iso(now)}))
    api_key = None

    # 3. Org spend from the Admin Cost API, cached.
    cost = _kv_json(led, _key("cost", group))
    fetched = parse(cost.get("fetched_at")) if cost.get("fetched_at") else None
    attempted = parse(cost.get("attempted_at")) if cost.get("attempted_at") else None
    active = any(r["platform"] in pool["peers"] for r in led.active_runs())
    cache = timedelta(minutes=pool["cost_active_minutes" if active else "cost_cache_minutes"])
    due = (not fetched or cost.get("cycle_start") != iso(start) or now - fetched >= cache)
    if due and (not attempted or now - attempted >= COST_MIN_INTERVAL):
        cost["attempted_at"] = iso(now)
        admin, status = _admin_key(pool, run=run)
        if status != KEY_OK:
            cost["error"] = f"admin key: {status}"
            _alert_once(ctx, _key("admin-alert", group), status,
                        f"Mahler: API credits burst off — {pool['admin_key_service']} unreadable",
                        f"The org's spend can't be read ({KEY_STATUS_TEXT[status]}), so {name} "
                        "stays on its allowance and never bursts.", priority="default")
        else:
            try:
                reading = fetch_cost(admin, start, pool["workspace_id"], opener=opener)
            except CostAuthError:
                cost["error"] = "admin key rejected"
                _alert_once(ctx, _key("admin-alert", group), "auth",
                            f"Mahler: {pool['admin_key_service']} rejected",
                            "The Admin Cost API refused the admin key (expired or revoked). "
                            f"{name} stays on its allowance and never bursts.")
            except CostError as e:
                cost["error"] = str(e)[:80]
            else:
                cost = {"fetched_at": iso(now), "attempted_at": iso(now),
                        "cycle_start": iso(start), **reading}
                led.set_kv(_key("admin-alert", group), "")
            finally:
                admin = None
        led.set_kv(_key("cost", group), json.dumps(cost))

    # 4. Key expiry warnings.
    for key in key_expiries(pool, now.date()):
        flag = _key(f"expiry-{key['name'].lower()}", group)
        last = _kv_json(led, flag)
        if last.get("expires") != key["expires"]:
            last = {}
        stage = expiry_notice_due(key["days_left"], last)
        if not stage or (stage == "daily" and last.get("date") == now.date().isoformat()):
            continue
        led.set_kv(flag, json.dumps({"stage": stage, "date": now.date().isoformat(),
                                     "expires": key["expires"]}))
        days = key["days_left"]
        when = "has expired" if days < 0 else "expires today" if days == 0 else f"expires in {days} days"
        ctx.ping(f"Mahler: Anthropic {key['name']} key {when}",
                 f"The {key['name']} key for the Team-plan API credits ({group}) {when} "
                 f"({key['expires']}). Make a new one in the Console, store it in the mini's "
                 f"login keychain and update api_credits.{key['name'].lower()}_key_expires.",
                 priority="high" if days <= 3 else "default", tags="warning,key")

    # 5. Console gauge: spend against the line in force, resetting at cycle end.
    snap = snapshot(led, name, pconf)
    line = snap["line_usd"]
    used = snap["org_spend_usd"] if snap["burst"] else snap["mahler_spend_usd"]
    pct = 100.0 if line <= 0 else max(0.0, min(100.0, 100.0 * used / line))
    for peer in pool["peers"]:
        led.record_usage(peer, WINDOW, round(pct, 1), snap["cycle_end"])


# ---------- run endings ----------

def _log_tail(path, limit=65536):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def is_auth_error(text):
    lower = (text or "").lower()
    return any(word in lower for word in AUTH_ERROR_WORDS)


def mark_exhausted(led, ping, group, grant, run_id=None, platform=None):
    """'credit balance is too low' — park the pool until its grant ends.
    Shared by the post-run log scan (after_run) and the gateway's real-time
    detection, so either path pings exactly once per grant. `grant` is
    whatever current_grant(pool, now) already returned (or None) — this
    function never recomputes it, so it works the same from a Ctx (which
    has a policy to resolve) or from the gateway (which doesn't)."""
    now = led.now()
    gid = grant["id"] if grant else None
    if _kv_json(led, _key("exhausted", group)).get("grant_id") != gid:
        led.set_kv(_key("exhausted", group), json.dumps({"grant_id": gid, "since": iso(now)}))
        led.event("api_credits_exhausted", detail=f"{group}: credit balance too low")
        when = f"its grant ends ({iso(grant['end'])})" if grant and grant.get("end") \
            else "a new grant is confirmed"
        source = f"Run {run_id} on {platform} hit" if run_id else "A request hit"
        ping(f"Mahler: Team API credits exhausted ({group})",
             f"{source} 'credit balance too low'. No more credits spending until {when}.",
             priority="high", tags="warning")


def mark_auth_parked(led, ping, group, run_id=None, platform=None):
    """An authentication failure (expired or revoked key) parks the pool
    until a free recheck succeeds — shared by the log scan and the gateway."""
    now = led.now()
    if not _kv_json(led, _key("auth", group)).get("since"):
        led.set_kv(_key("auth", group), json.dumps({"since": iso(now)}))
        led.event("api_credits_auth", detail=f"{group}: API key rejected")
        source = f"Run {run_id} on {platform} failed" if run_id else "A request failed"
        ping(f"Mahler: Anthropic API key rejected ({group})",
             f"{source} authentication — the key is expired or revoked. The pool is parked "
             f"(no retries); Mahler checks the key for free every {AUTH_RECHECK_HOURS}h and "
             "resumes when it works.",
             priority="high", tags="warning,key")


def after_run(ctx, run, pconf, log):
    """What a credits run's ending says about the pool. Sets log['auth_failed'].

    This is the post-hoc fallback: the gateway (below) already reacts to
    both conditions in real time, in-request, before this ever runs. Both
    paths share mark_exhausted/mark_auth_parked, so whichever sees it first
    pings once and the other is a no-op."""
    pool = pconf.get("credit_pool")
    if not pool:
        return
    group = pool["group"]
    if log.get("credit_exhausted"):
        mark_exhausted(ctx.led, ctx.ping, group, current_grant(pool, ctx.led.now()),
                       run_id=run["id"], platform=run["platform"])
        return
    text = " ".join(str(x) for x in (log.get("last_error"), log.get("last_text"),
                                      log.get("final")) if x)
    if not is_auth_error(text):
        text = _log_tail(run["log_path"]) if run["log_path"] else ""
    if is_auth_error(text):
        log["auth_failed"] = True
        mark_auth_parked(ctx.led, ctx.ping, group, run_id=run["id"], platform=run["platform"])


# ---------- pricing: the pre-request budget contract (D41 step 2) ----------
#
# A conservative, static USD/MTok table — not a source of billing truth, only
# of the reservation math below. Override per model with
# [api_credits.model_pricing.<model>]. Rates current as of 2026-10-10.

# claude-sonnet-5-5's rates are confirmed from Cline 3.0.70's own model
# registry (mahler#903 trial 1, 2026-10-10: `pricing":{"input":2,"output":10,
# "cacheRead":0.1,"cacheWrite":2.5}`). claude-opus-5 and claude-haiku-4-5 are
# estimates (not independently verified) scaled at the same ratios as the
# previous sonnet guess — update them from a verified source before relying
# on either for a real reservation.
MODEL_PRICING = {
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_write": 2.5, "cache_read": 0.10},
    "claude-opus-5": {"input": 10.0, "output": 50.0, "cache_write": 12.5, "cache_read": 0.50},
    "claude-haiku-4-5": {"input": 0.53, "output": 2.67, "cache_write": 0.67, "cache_read": 0.027},
}
DEFAULT_PRICING = MODEL_PRICING["claude-sonnet-5-5"]

# Worst-case input: the whole serialized request body (system prompt, tool
# definitions, tool calls and tool results, not just message text) at one
# token per byte. BPE tokens almost always span several bytes, so this
# overcounts, which only costs transient headroom until the request settles.
# The Agent SDK's tool schemas alone are ~76 KB per request (mahler#918), so
# counting message text only would under-reserve by an order of magnitude.
BYTES_PER_TOKEN = 1
AUX_TOKEN_OVERHEAD = 256      # framing slack neither side reports upfront


def model_pricing(pool):
    """The effective price table: built-ins plus config overrides."""
    out = {m: dict(rates) for m, rates in MODEL_PRICING.items()}
    for model, rates in (pool.get("model_pricing") or {}).items():
        out[model] = {**out.get(model, DEFAULT_PRICING), **rates}
    return out


def _price_for(pricing, model):
    return pricing.get(model, DEFAULT_PRICING)


def estimate_request_cost(body, pricing, max_output_tokens):
    """The worst-case reservation for one /v1/messages call: every byte of
    the request body counts as a fresh (uncached) input token, and every output token
    up to the request's own max_tokens (capped) gets produced. Covers one
    HTTP attempt; a retry or an auxiliary call reserves its own estimate
    separately — distinct attempt_keys, not a shared budget (D41 step 2/4).
    Raises ValueError on a malformed or out-of-bounds request."""
    if not isinstance(body, dict):
        raise ValueError("malformed request body")
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError("request is missing a model")
    max_tokens = body.get("max_tokens")
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
        raise ValueError("max_tokens must be a positive integer")
    if max_tokens > max_output_tokens:
        raise ValueError(f"max_tokens {max_tokens} exceeds the approved ceiling "
                         f"({max_output_tokens})")
    price = _price_for(pricing, model)
    input_bytes = len(json.dumps(body, separators=(",", ":")).encode())
    input_tokens = input_bytes // BYTES_PER_TOKEN + AUX_TOKEN_OVERHEAD
    cost = (input_tokens * price["input"] + max_tokens * price["output"]) / 1_000_000
    return round(cost, 6)


def price_usage(usage, pricing, model):
    """The actual cost of a completed request from its reported usage."""
    if not isinstance(usage, dict):
        raise ValueError("malformed usage")
    price = _price_for(pricing, model)
    cost = (int(usage.get("input_tokens") or 0) * price["input"]
            + int(usage.get("output_tokens") or 0) * price["output"]
            + int(usage.get("cache_creation_input_tokens") or 0) * price["cache_write"]
            + int(usage.get("cache_read_input_tokens") or 0) * price["cache_read"])
    return round(cost / 1_000_000, 6)


def parse_stream_usage(raw):
    """Accumulate token usage from a buffered Anthropic Messages SSE stream.
    `message_start` carries the initial (input/cache) usage; each
    `message_delta` carries the running output_tokens total — taking the max
    per field across every event tracks the final total without depending on
    event ordering. -> a usage dict, or None if no usage event ever arrived
    (an ambiguous outcome: the caller must not guess the cost was zero)."""
    usage = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        try:
            payload = json.loads(line[len("data:"):].strip())
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        candidates = [payload.get("usage"), (payload.get("message") or {}).get("usage")
                      if isinstance(payload.get("message"), dict) else None]
        for u in candidates:
            if not isinstance(u, dict):
                continue
            for k, v in u.items():
                if isinstance(v, int) and not isinstance(v, bool):
                    usage[k] = max(v, usage.get(k, 0))
    return usage or None


# ---------- the gateway (D41 step 2) ----------
#
# Cline has no per-run dollar cap and reports usage only at the end of a run,
# so a post-request cost report or a watchdog kill cannot bound spend before
# it happens. This standard-library reverse proxy is the enforceable hook
# instead: it is the only thing that ever holds the real workspace key, every
# run gets its own random local token good only for this gateway, and every
# POST /v1/messages reserves its worst-case cost before forwarding and
# settles or releases it afterward. Any other path is refused.
#
# It fully buffers each upstream response (even a streamed one) before
# replying: at one local hop the latency cost is negligible, and it lets a
# single synchronous handler compute real settlement before the client sees
# the response, with no partial-forward edge cases. A client that wants true
# token-by-token delivery would see it arrive all at once instead — accepted
# for a headless, non-interactive CLI harness.

ANTHROPIC_HOST = "api.anthropic.com"
MESSAGES_PATH = "/v1/messages"
# The Agent SDK's requests carry `?beta=true`; nothing else is accepted.
MESSAGES_PATHS = (MESSAGES_PATH, MESSAGES_PATH + "?beta=true")


def _gateway_error(kind, message):
    return json.dumps({"type": "error", "error": {"type": kind, "message": message}}).encode()


def _body_text(raw):
    try:
        return raw.decode("utf-8", "replace")
    except Exception:
        return ""


class _SerializedLedger:
    """One ledger connection shared by the gateway's handler threads. The
    Ledger contract (thread_safe=True) leaves serialising to the caller:
    two handlers entering `BEGIN IMMEDIATE` on one connection at once fail
    with "cannot start a transaction within a transaction" (mahler#918).
    Every method call takes one re-entrant lock; none of them do network
    I/O, so holding it is brief. Plain attributes pass straight through."""

    def __init__(self, led):
        self._led = led
        self._lock = threading.RLock()

    def __getattr__(self, name):
        attr = getattr(self._led, name)
        if not callable(attr):
            return attr

        def locked(*a, **k):
            with self._lock:
                return attr(*a, **k)
        return locked


class Gateway:
    """One pool group's local proxy. Long-lived: started once, serves every
    run of every platform in the group until stopped."""

    def __init__(self, led, group, grant_fn, pricing, allowed_models, max_output_tokens,
                 real_key, connect=None, ping=None):
        self.led = led if isinstance(led, _SerializedLedger) else _SerializedLedger(led)
        self.group = group
        self.grant_fn = grant_fn
        self.pricing = pricing
        self.allowed_models = set(allowed_models)
        self.max_output_tokens = max_output_tokens
        self.real_key = real_key
        self.connect = connect or (lambda: http.client.HTTPSConnection(ANTHROPIC_HOST, timeout=600))
        self.ping = ping or (lambda *a, **k: None)
        self._tokens = {}
        self._lock = threading.Lock()
        self._server = None
        self._thread = None
        self.port = None

    # -- run token lifecycle --

    def register(self, run_id):
        """A fresh local token for one run. Never the real key — the runner
        puts only this in the run's environment (D41 step 2)."""
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._tokens[token] = run_id
        return token

    def revoke(self, run_id):
        with self._lock:
            for token, rid in list(self._tokens.items()):
                if rid == run_id:
                    del self._tokens[token]

    def _run_for_token(self, token):
        with self._lock:
            return self._tokens.get(token)

    # -- serving --

    def start(self, host="127.0.0.1"):
        if self._server is not None:
            return self.port
        gateway = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass   # never the default stderr access log: it would echo headers/paths

            def _respond(self, status, headers, body):
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                status, headers, resp = gateway.handle(
                    "POST", self.path, dict(self.headers), body)
                self._respond(status, headers, resp)

            def do_GET(self):
                status, headers, resp = gateway.handle("GET", self.path, dict(self.headers), b"")
                self._respond(status, headers, resp)

        # Threaded: HTTP/1.1 keeps connections alive, and the Agent SDK opens
        # a second connection while its first sits idle. A single-threaded
        # server blocks on the idle one and never accepts the next — every
        # run hangs (mahler#918). Reservations are already transactional
        # (BEGIN IMMEDIATE), so concurrent handlers are safe.
        self._server = http.server.ThreadingHTTPServer((host, 0), _Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name=f"credit-gateway-{self.group}", daemon=True)
        self._thread.start()
        return self.port

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            self._thread = None
            self.port = None

    # -- request handling, independent of http.server for direct testing --

    def handle(self, method, path, headers, body):
        """-> (status, response_headers, response_body_bytes)."""
        if method != "POST" or path not in MESSAGES_PATHS:
            return 404, {}, _gateway_error(
                "not_found_error", "Mahler gateway: only POST /v1/messages is proxied")
        lower = {str(k).lower(): v for k, v in headers.items()}
        token = lower.get("x-api-key") or ""
        run_id = self._run_for_token(token)
        if run_id is None:
            return 401, {}, _gateway_error(
                "authentication_error", "Mahler gateway: unrecognized local token")
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return 400, {}, _gateway_error("invalid_request_error",
                                           "Mahler gateway: malformed JSON body")
        if not isinstance(data, dict) or data.get("model") not in self.allowed_models:
            return 400, {}, _gateway_error(
                "invalid_request_error",
                f"Mahler gateway: model {data.get('model') if isinstance(data, dict) else None!r} "
                "is not an approved credits model")
        try:
            amount = estimate_request_cost(data, self.pricing, self.max_output_tokens)
        except ValueError as e:
            return 400, {}, _gateway_error("invalid_request_error", f"Mahler gateway: {e}")
        grant = self.grant_fn()
        if grant is None:
            return 402, {}, _gateway_error(
                "permission_error",
                "Your credit balance is too low to access the Anthropic API. "
                "(Mahler: no confirmed grant covers now.)")
        attempt_key = secrets.token_hex(16)
        rid, info = self.led.reserve_credit(self.group, grant["id"], attempt_key, amount,
                                            grant["ceiling_usd"], run_id=run_id)
        if rid is None:
            return 402, {}, _gateway_error(
                "permission_error",
                "Your credit balance is too low to access the Anthropic API. "
                f"(Mahler: ${max(info.get('remaining_usd', 0.0), 0.0):.2f} of headroom left, "
                f"${amount:.2f} requested.)")
        model = data["model"]
        try:
            status, resp_headers, resp_body = self._forward(data, path, lower)
        except (OSError, http.client.HTTPException) as e:
            # Nothing reached (or finished talking to) Anthropic: nothing was
            # spent, so free the reservation. Cline's own retry makes a new
            # HTTP call to the gateway with its own attempt_key.
            self.led.release_credit(self.group, grant["id"], attempt_key, note=repr(e)[:200])
            return 502, {}, _gateway_error("api_error",
                                           f"Mahler gateway: upstream request failed: {e}")
        usage = None
        if status < 300:
            usage = (parse_stream_usage(resp_body) if data.get("stream")
                     else self._json_usage(resp_body))
        if usage is not None:
            self.led.settle_credit(self.group, grant["id"], attempt_key,
                                   price_usage(usage, self.pricing, model))
        elif status < 300:
            # A 2xx with no parseable usage: ambiguous. Keep the exposure
            # (D41 step 4) rather than assume nothing was spent.
            self.led.event("credit_ambiguous", detail={"group": self.group,
                           "grant_id": grant["id"], "attempt_key": attempt_key, "status": status})
        elif platforms.is_credit_exhausted(_body_text(resp_body)):
            self.led.release_credit(self.group, grant["id"], attempt_key,
                                    note="credit balance too low")
            mark_exhausted(self.led, self.ping, self.group, grant, run_id=run_id)
        elif is_auth_error(_body_text(resp_body)):
            self.led.release_credit(self.group, grant["id"], attempt_key, note="auth rejected")
            mark_auth_parked(self.led, self.ping, self.group, run_id=run_id)
        else:
            # A definite pre-usage error (4xx/5xx, no usage reported): safe
            # to release — the request never produced billable output.
            self.led.release_credit(self.group, grant["id"], attempt_key, note=f"HTTP {status}")
        return status, resp_headers, resp_body

    @staticmethod
    def _json_usage(raw):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        usage = parsed.get("usage") if isinstance(parsed, dict) else None
        return usage if isinstance(usage, dict) else None

    def _forward(self, data, path=MESSAGES_PATH, client_headers=None):
        body = json.dumps(data).encode()
        client_headers = client_headers or {}
        # The client's anthropic-beta / anthropic-version / User-Agent go
        # upstream unchanged: Anthropic tells Agent SDK traffic (covered by
        # the credits) from Claude Code (not covered) by them. The client's
        # own x-api-key / Authorization are never forwarded.
        out = {"x-api-key": self.real_key,
               "anthropic-version": client_headers.get("anthropic-version") or ANTHROPIC_VERSION,
               "content-type": "application/json",
               "User-Agent": client_headers.get("user-agent") or "mahler-gateway"}
        if client_headers.get("anthropic-beta"):
            out["anthropic-beta"] = client_headers["anthropic-beta"]
        conn = self.connect()
        try:
            conn.request("POST", path, body=body, headers=out)
            resp = conn.getresponse()
            raw = resp.read()
            headers = {k: v for k, v in resp.getheaders()
                      if k.lower() not in ("content-length", "transfer-encoding", "connection",
                                           "server", "date")}
            return resp.status, headers, raw
        finally:
            conn.close()



_GATEWAYS = {}
_GATEWAYS_LOCK = threading.Lock()


def _open_gateway_ledger(path, clock):
    from .ledger import Ledger        # deferred: ledger imports config, config imports us
    return Ledger(path, clock=clock, thread_safe=True)


def ensure_gateway(ctx, name, pconf, open_ledger=None):
    """The group's gateway, starting it on first use. Idempotent: a platform
    sharing a quota_group with another credits platform reuses the same
    running gateway and its registered tokens stay namespaced by run_id."""
    pool = pconf.get("credit_pool")
    if not pool:
        raise RuntimeError("API credits are not configured for this platform")
    group = pool["group"]
    with _GATEWAYS_LOCK:
        gw = _GATEWAYS.get(group)
        if gw is not None:
            return gw
        real_key = api_key_for_run(pconf)
        led = _SerializedLedger((open_ledger or _open_gateway_ledger)(ctx.led.path, ctx.led.now))

        def grant_fn(led=led, name=name, pconf=pconf):
            snap = snapshot(led, name, pconf)
            if not snap.get("grant_confirmed") or snap["state"] in ("hard", "no_credit"):
                return None
            return {"id": snap["grant_id"], "ceiling_usd": snap["reservation_ceiling_usd"],
                   "end": parse(snap["cycle_end"])}

        gw = Gateway(led, group, grant_fn, model_pricing(pool), pool["allowed_models"],
                    pool["max_output_tokens"], real_key, ping=ctx.ping)
        gw.start(pool["gateway_host"])
        _GATEWAYS[group] = gw
        return gw


def revoke_run_token(pconf, run_id):
    """Forget one run's local token once it has ended (finalize.py)."""
    pool = pconf.get("credit_pool")
    gw = _GATEWAYS.get(pool["group"]) if pool else None
    if gw is not None:
        gw.revoke(run_id)


def shutdown_gateways():
    """Stop every running gateway: daemon shutdown, or test cleanup."""
    with _GATEWAYS_LOCK:
        for gw in _GATEWAYS.values():
            gw.stop()
            if gw.led is not None and gw.led.path != ":memory:":
                gw.led.close()
        _GATEWAYS.clear()
