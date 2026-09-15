"""Paths and configuration.

Global config lives at ~/.mahler/config.toml and is overlaid on DEFAULTS, so a
config file only needs to name what it changes. Nothing is managed unless a
[projects.<name>] entry says enabled = true — the same load-bearing opt-in as
dispatch (DESIGN D2).
"""

import copy
import os
import tomllib

HOME = os.path.expanduser("~")
STATE = os.environ.get("MAHLER_HOME", os.path.join(HOME, ".mahler"))
CONFIG_PATH = os.path.join(STATE, "config.toml")
DB_PATH = os.path.join(STATE, "mahler.db")
RUNS_DIR = os.path.join(STATE, "runs")
WORKTREES = os.path.join(STATE, "worktrees")
LOCK_PATH = os.path.join(STATE, "tick.lock")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAHLER_BIN = os.path.join(REPO_ROOT, "bin", "mahler")


def ensure_private_dir(path, mode=0o700):
    """Create `path` (and missing parents) user-only (issue #75, following
    backup.py's pattern: dirs 0700, files 0600). os.makedirs' `mode` is masked
    by the process umask — fine for newly created dirs at 0o700, but a
    directory that already exists keeps its old, possibly loose mode, so every
    level from STATE down is chmod'ed explicitly afterwards. Paths outside
    STATE (e.g. a custom worktree_root) get their leaf tightened only —
    ancestors there are not ours to chmod."""
    os.makedirs(path, mode=mode, exist_ok=True)
    state = os.path.abspath(STATE)
    p = os.path.abspath(path)
    while True:
        try:
            os.chmod(p, mode)
        except OSError:
            pass
        if p == state or not p.startswith(state + os.sep):
            break
        p = os.path.dirname(p)

MAINTENANCE_PASSES = ("security", "health", "drift", "tests", "token-economy", "guidance",
                      "backlog", "bugs")
DEFAULT_MAINTENANCE = {
    "enabled": True,
    "cadence_days": 30,
    "merged_threshold": 20,
    "cooldown_days": 14,
    "passes": list(MAINTENANCE_PASSES),
}

# Periodic self-audit of Mahler's own platform tier/capability assumptions
# (mahler#206) — distinct from the per-managed-project D20 passes above: this
# is about mahler/config.py's own tier/max_size/min_size judgment calls and
# each platform's DESIGN.md "verified" annotation, not a managed project's
# codebase. It reuses D20's checkpoint machinery (`Ledger.maintenance_due`,
# keyed on `project`'s own merged-PR throughput) rather than a second
# scheduler, so it shares the same cadence/threshold shape, but it is its own
# pass — not one of the eight in a project's `maintenance.passes` list.
PLATFORM_AUDIT_PASS = "platform-audit"
DEFAULT_PLATFORM_AUDIT = {
    "enabled": True,
    "project": "mahler",        # whose merged-PR throughput anchors the checkpoint
    "cadence_days": 30,
    "merged_threshold": 20,
    "cooldown_days": 14,
    "stale_verified_days": 90,  # DESIGN.md "verified" date older than this is flagged
    # Gate tier comparisons by samples on each side and done-rate gap in points.
    "inversion_min_runs": 10,
    "inversion_margin_pct": 15.0,
}

DEFAULTS = {
    "defaults": {
        "enabled": False,
        "base": "main",
        "max_parallel": 1,
        "settle_minutes": 10,          # sorted -> eligible to build (DESIGN D7)
        "max_attempts": 3,             # failed runs before needs-you
        "verify_timeout_minutes": 60,  # CI pending longer than this -> needs-you (mahler#18)
        "run_timeout_minutes": 60,
        "progress_timeout_minutes": 20,
        "startup_timeout_minutes": 10,  # agent started but printed nothing (mahler#12)
        "auto_lease_minutes": 10,
        "interactive_lease_minutes": 30,
        "hot_hold": True,
        "hot_hold_minutes": 20,
        "yield_grace_seconds": 120,
        "verify": "",
        # which open issues Mahler manages: "all", or "label" = only those
        # carrying scope_label (for repos with a big pre-Mahler backlog)
        "scope": "all",
        "scope_label": "mahler",
        "worktree_root": "",           # default: ~/.mahler/worktrees
        "link": [],                    # untracked files to symlink from the primary checkout
        "setup": "",                   # shell run in a new worktree before the agent starts
        "rules": "",                   # extra project rules appended to build/sort prompts
        "maintenance": DEFAULT_MAINTENANCE,
    },
    # `total` is the hard overall ceiling (blast radius). `by_tier` (optional,
    # mahler#200) layers a finer cap under it, keyed by the same `tier` field
    # platforms carry for D8 rule-4 escalation (router.tier_of): each entry
    # caps concurrent runs at that tier *or above* (a tier-4 run also counts
    # against a tier-"3 and up" budget, so it can't dodge the cap by being
    # even scarcer). Absent/empty by_tier reproduces today's behavior exactly
    # — every tier is bounded only by `total`. Example, biasing slots toward
    # the cheap/free platforms without touching `total`:
    #   [concurrency]
    #   total = 4
    #   by_tier = { 1 = 3, 2 = 2 }   # tiers 3 and 4 stay unrestricted (still <= total)
    "concurrency": {"total": 2},
    "scheduling": {"priority_projects": ["mahler"]},
    "ntfy": {"server": "https://ntfy.sh", "topic": "", "topic_high": ""},
    # the operator console (`mahler serve`, DESIGN D10, D27)
    "serve": {"host": "127.0.0.1", "port": 8787, "public_url": ""},
    # daily digest ping (mahler#6): one ntfy roll-up a day, sent on the first
    # tick at/after `hour` local time; once-only via the ledger kv table
    "digest": {"hour": 8},
    # daily janitor (mahler#7, DESIGN D12): prune worktrees of ended runs and
    # old mahler/snapshot|abandoned branches; once-only via the ledger kv table
    "janitor": {"retention_days": 14, "worktree_grace_hours": 24},
    # Periodic calibration of run and issue duration estimates (mahler#59)
    "estimates": {
        "calibration_interval": 10,
        "calibration_window": 20,
    },
    "platform_audit": DEFAULT_PLATFORM_AUDIT,
    # Order is preference. DESIGN D8: Claude plans; the free Antigravity pools
    # build first; Claude builds only under its reserve thresholds.
    "routing": {
        # sorting is light: when Claude is over its reserve, spend Gemini, and
        # keep Antigravity's scarcer Claude/Opus pool for building
        "sort": ["claude", "agy-gemini", "agy-claude"],
        # planning is a separate route (DESIGN D21): goals, audits, and size:l
        # items are planned by Opus only; when Opus is over its line, they wait
        "plan": ["claude-opus"],
        # copilot-high sits right after copilot: min_size: "l" gates it to
        # size:l items only (mahler#192), so it never changes routing for
        # normal-size items.
        "build": ["agy-claude", "agy-gemini", "cline-free", "copilot",
                  "copilot-high", "kilo", "claude-opus", "claude"],
    },
    "platforms": {
        "claude": {
            "enabled": True, "kind": "claude",
            "sort_model": "sonnet", "build_model": "",
            "max_size": "m", "tier": 3,
            "soft": {"5h": 60, "weekly": 70},
            "hard": {"5h": 70, "weekly": 80},
            "stale_minutes": 15,
            "quota_group": "claude",
        },
        # Same CLI, same account/quota as "claude" (kind: claude) — forces Opus
        # for hard tasks (size:l by default via min_size: "l", or via explicit
        # `platform:claude-opus` pin) without spending Antigravity's separate,
        # scarcer Claude/Opus pool.
        "claude-opus": {
            "enabled": True, "kind": "claude",
            "sort_model": "opus", "build_model": "opus",
            "min_size": "l", "tier": 4,
            "soft": {"5h": 45, "weekly": 70},
            "hard": {"5h": 70, "weekly": 80},
            "stale_minutes": 15,
            "quota_group": "claude",
        },
        "agy-claude": {
            "enabled": True, "kind": "agy", "pool": "Claude and GPT models",
            "model": "claude-opus-4-6-thinking",
            "max_size": "m", "tier": 2,
            "soft": {"5h": 85, "weekly": 85},
            "hard": {"5h": 90, "weekly": 90},
            "stale_minutes": 5,
        },
        "agy-gemini": {
            "enabled": True, "kind": "agy", "pool": "Gemini Models",
            "model": "gemini-3.1-pro-high",
            "max_size": "m", "tier": 3,
            "soft": {"5h": 85, "weekly": 85},
            "hard": {"5h": 90, "weekly": 90},
            "stale_minutes": 5,
        },
    },
    "projects": {},
    # Logins other than this machine's own (DESIGN D25): each names the env
    # that points the CLIs at its separate login, and its own per-role routing.
    "accounts": {},
    # Burst before a Claude window resets (D23): in the last lead-time before a
    # window rolls over, Claude's soft/hard lines rise to these burst lines so
    # the expiring reserve turns into work instead of going unused. "soft" and
    # "hard" are percentages (0..100). Burst lines stay below 100% so no burst
    # ever spends paid overage. `enabled = false` turns the burst off entirely.
    "burst": {
        "enabled": True,
        "weekly_lead_hours": 5,       # before the weekly reset
        "session_lead_minutes": 60,   # before the 5-hour reset
        "soft": 90,
        "hard": 97,
        "human_quiet_minutes": 20,    # suppress burst after human use (D23)
    },
    # Claude's peak window (D22): on weekdays 5-11am Pacific, Mahler starts no
    # new Claude runs. Running work continues, and the hard lines still apply.
    # Override with `mahler peak off [--for DURATION]`, or by pinning an item
    # to a Claude platform. `enabled = false` turns the window off entirely.
    "claude_peak": {
        "enabled": True,
        "tz": "America/Los_Angeles",
        "weekdays": [0, 1, 2, 3, 4],   # Python weekday(): Mon=0 .. Sun=6
        "start": "05:00",
        "end": "11:00",
    },
}

# Codex uses the locally authenticated ChatGPT account through `codex exec`.
# The CLI does not expose an account-wide quota probe, so it is treated like
# the other unmetered adapters: available until a run reports a limit, then
# held for a short backoff. It is deliberately absent from the default routes;
# installations opt in according to which account they want Mahler to spend.
DEFAULTS["platforms"]["codex"] = {
    "enabled": True, "kind": "codex", "model": "", "plan": "free tier",
    "metered": False, "backoff_minutes": 60, "tier": 2,
    "soft": {"5h": 100, "weekly": 100}, "hard": {"5h": 100, "weekly": 100},
    "stale_minutes": 60,
    "quota_group": "codex",
}

# Same CLI, same ChatGPT account/quota as "codex" (kind: codex, shared
# quota_group) — an escalation-only stronger sibling mirroring
# claude/claude-opus (mahler#192): min_size "l" keeps it for hard tasks, and
# tier 3 is one rung above codex's tier 2 so D8 rule-4 escalation lands here.
# Model verified 2026-09-14 from the installed codex-cli 0.154.0's embedded
# catalog: gpt-5.6-sol is the top tier ("Latest frontier agentic coding
# model"); the base codex entry's effective model stays gpt-5.6-terra (set in
# ~/.codex/config.toml). Like codex, deliberately absent from the default
# routes — a project opting into codex adds codex-high alongside it in its own
# routing.build override if it wants the escalation tier.
DEFAULTS["platforms"]["codex-high"] = {
    "enabled": True, "kind": "codex", "model": "gpt-5.6-sol", "plan": "free tier",
    "min_size": "l", "tier": 3,
    "metered": False, "backoff_minutes": 60,
    "soft": {"5h": 100, "weekly": 100}, "hard": {"5h": 100, "weekly": 100},
    "stale_minutes": 60,
    "quota_group": "codex",
}

# Cline's free models report no quota at all (verified 2026-09-12): it is
# "unmetered" — available until a rate-limit/quota error, then backed off.
# Its free models are weaker, so it only takes small items.
DEFAULTS["platforms"]["cline-free"] = {
    "enabled": True, "kind": "cline", "model": "", "plan": "free tier",
    "metered": False, "backoff_minutes": 60, "max_size": "s", "tier": 1,
    "soft": {"5h": 100, "weekly": 100}, "hard": {"5h": 100, "weekly": 100},
    "stale_minutes": 60,
}

# Kilo (kilo.ai account, needs `kilo auth login` once) reports no account-wide
# quota (mahler#25): "unmetered", backed off for an hour after a
# rate-limit/quota error. Its default model needs an explicit `:free` route
# (mahler#29) — without one, every run 402s on "add credits" — and
# kilo-auto/free draws from a grab-bag of smaller/niche models of unverified
# quality, so it's kept last among the free builders and capped to size s
# like the others.
DEFAULTS["platforms"]["kilo"] = {
    "enabled": True, "kind": "kilo", "model": "kilo/kilo-auto/free",
    "metered": False, "backoff_minutes": 60, "max_size": "s", "tier": 1,
    "soft": {"5h": 100, "weekly": 100}, "hard": {"5h": 100, "weekly": 100},
    "stale_minutes": 60,
}

# Copilot (GitHub Education license) is unlike Cline/Kilo: it has a real,
# checkable cap, and it runs real frontier models (verified: claude-sonnet-5)
# — which is why it goes ahead of Kilo despite the smaller monthly allowance.
# GitHub bills Copilot in "AI Credits" (mahler#38); Pro/Education include
# 1500/month. There's no cheap CLI-level probe, but the billing API (`gh api
# /users/<login>/settings/billing/ai_credit/usage`, needs the `user` OAuth
# scope) reports this month's consumption, so Copilot gets a single "monthly"
# window instead of the usual 5h/weekly pair (see router.py's per-platform
# `windows` override).
# `model: "auto"` (verified 2026-09-14, mahler#192) lets Copilot pick per-turn
# instead of pinning claude-sonnet-5, and gets a 10% multiplier discount on
# every request for it (GitHub's "Copilot auto model selection"). `auto_tier:
# "balance"` is the CLI's own middle profile between "efficiency" and
# "intelligence" — auto is turn-complexity-adaptive either way (confirmed live:
# even "intelligence" picked claude-haiku-4.5 for a trivial one-line reply), so
# this is about nudging routine size:s work toward cheaper models on the easy
# turns, not a capability guarantee — copilot-high below stays pinned instead.
DEFAULTS["platforms"]["copilot"] = {
    "enabled": True, "kind": "copilot", "model": "auto", "auto_tier": "balance",
    "metered": True, "windows": ["monthly"], "monthly_cap_credits": 1500,
    "backoff_minutes": 60, "max_size": "s", "tier": 2,
    "soft": {"monthly": 80}, "hard": {"monthly": 95},
    "stale_minutes": 360,
    "quota_group": "copilot",
}

# Same CLI, same GitHub account and monthly AI-credits cap as "copilot"
# (kind: copilot, shared quota_group) — an escalation-only stronger sibling
# mirroring claude/claude-opus (mahler#192): min_size "l" keeps it for hard
# tasks (D8 rule-4 escalation lands here at tier 3, one rung above copilot's
# tier 2), and unlike copilot it has no max_size cap. Copilot's model catalog
# is fetched live and account-gated (no static bundle), so the model was
# verified live on 2026-09-14 with the fast-fail check — `copilot --model
# <slug> -p "hi"` errors "not available." client-side in ~2s for rejected
# slugs (claude-opus-4.8, claude-sonnet-4 were rejected) before any session
# cost; gpt-5.3-codex passed. Copilot's naming does not mirror Codex's 1:1:
# gpt-5.6-sol does not exist on Copilot.
DEFAULTS["platforms"]["copilot-high"] = {
    "enabled": True, "kind": "copilot", "model": "gpt-5.3-codex",
    "min_size": "l", "tier": 3,
    "metered": True, "windows": ["monthly"], "monthly_cap_credits": 1500,
    "backoff_minutes": 60,
    "soft": {"monthly": 80}, "hard": {"monthly": 95},
    "stale_minutes": 360,
    "quota_group": "copilot",
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def resolve_platforms(cfg):
    """`from = "<base>"` makes a platform inherit a base platform's settings
    (DESIGN D25). A platform on another account gets its own quota group, so
    its run slot and its quota never merge with the base account's."""
    plats = cfg["platforms"]

    def resolve(name, seen):
        own = plats[name]
        base = own.get("from")
        if not base:
            return own
        if base in seen or base not in plats:
            return {**own, "enabled": False, "error": f"bad from = {base!r}"}
        parent = resolve(base, seen | {base})
        merged = _merge(parent, own)
        if account_of(merged) != account_of(parent):
            if "quota_group" not in own and "quota_group" in merged:
                merged["quota_group"] = f"{merged['quota_group']}@{account_of(merged)}"
            # a plan belongs to a login, not to the CLI it inherits from
            if "plan" not in own:
                merged.pop("plan", None)
        return merged

    cfg["platforms"] = {n: resolve(n, {n}) for n in plats}
    return cfg


def load(path=None):
    path = path or CONFIG_PATH
    user = {}
    if os.path.exists(path):
        with open(path, "rb") as fh:
            user = tomllib.load(fh)
    cfg = _merge(DEFAULTS, user)
    validate_accounts(cfg)
    return resolve_platforms(cfg)


DEFAULT_ACCOUNT = "personal"

# Variables that can carry a login. A run on another account never inherits
# them from the daemon's own environment, only from its account's `env`.
CREDENTIAL_VARS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CONFIG_DIR", "COPILOT_HOME", "COPILOT_GITHUB_TOKEN", "GH_TOKEN",
    "GITHUB_TOKEN", "GH_CONFIG_DIR", "GH_HOST", "OPENAI_API_KEY", "CODEX_API_KEY",
    "CODEX_HOME",
)


def account_of(conf):
    """The account a platform or project policy belongs to."""
    return conf.get("account") or DEFAULT_ACCOUNT


def accounts_of(conf):
    """The accounts a policy may spend, in declared order (D26). Platforms
    stay single-account (D25); a project may name `accounts = [...]` instead
    of the singular `account`, which still reads as one account either way."""
    accts = conf.get("accounts")
    if accts is None:
        return [account_of(conf)]
    return list(accts)


def gh_account_of(conf):
    """The GitHub identity a project's sync, PRs, merges and comments use
    (D26): its own gh_account when set, else its first declared account."""
    return conf.get("gh_account") or accounts_of(conf)[0]


ACCOUNT_MODES = ("order", "equal")


def account_mode_of(conf):
    """How a multi-account project's declared accounts are routed (D26).

    "order" (default) tries them in declared order, spending the first with
    headroom — a fallback chain. "equal" merges every account's candidate
    list round-robin instead, so a genuinely dual-use project (e.g. mahler
    itself) doesn't exhaust one account's whole list before an idle other
    account is ever tried; it expresses whose quota gets spent, not which
    account gets tried first.
    """
    mode = conf.get("account_mode", "order")
    if mode not in ACCOUNT_MODES:
        raise ValueError(f"account_mode must be one of {ACCOUNT_MODES}, "
                         f"got {mode!r} (DESIGN D26)")
    return mode


def validate_accounts(cfg):
    """A project sets `account` or `accounts`, never both (D26)."""
    for name, proj in cfg.get("projects", {}).items():
        if "account" in proj and "accounts" in proj:
            raise ValueError(f"project {name!r} sets both 'account' and "
                             "'accounts' — one or the other (DESIGN D26)")
        if "accounts" in proj:
            accts = proj["accounts"]
            if not (isinstance(accts, list) and accts
                    and all(isinstance(a, str) and a for a in accts)):
                raise ValueError(f"project {name!r}: 'accounts' must be a "
                                 "non-empty list of account names (DESIGN D26)")
        if "account_mode" in proj and proj["account_mode"] not in ACCOUNT_MODES:
            raise ValueError(f"project {name!r}: account_mode must be one of "
                             f"{ACCOUNT_MODES} (DESIGN D26)")


def run_env(cfg, account, base=None):
    """The environment for anything spending `account`'s logins, or None for
    this machine's own account with no overrides (inherit as-is). Fails closed:
    an account the config doesn't define raises rather than falling back."""
    acct = cfg.get("accounts", {}).get(account)
    if acct is None:
        if account == DEFAULT_ACCOUNT:
            return None
        raise ValueError(f"account {account!r} is not defined under [accounts]")
    env = dict(os.environ if base is None else base)
    if account != DEFAULT_ACCOUNT:
        for var in CREDENTIAL_VARS:
            env.pop(var, None)
    for k, v in (acct.get("env") or {}).items():
        env[k] = os.path.expanduser(str(v))
    return env


def project_policy(cfg, name):
    """Defaults overlaid with one project's own entry."""
    return {**_merge(cfg["defaults"], cfg["projects"].get(name, {})), "name": name}


def maintenance_policy(cfg, name):
    return project_policy(cfg, name).get("maintenance", DEFAULT_MAINTENANCE)


def platform_audit_policy(cfg):
    """Global, not per-project (mahler#206): merged with the default shape so
    a minimal test/partial cfg dict (no [platform_audit] section) still has
    every key `Ledger.maintenance_due` and `platform_audit.queue` expect."""
    return _merge(DEFAULT_PLATFORM_AUDIT, cfg.get("platform_audit") or {})


def enabled_projects(cfg):
    return [project_policy(cfg, n) for n in cfg["projects"]
            if project_policy(cfg, n).get("enabled")]
