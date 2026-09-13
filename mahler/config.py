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
    },
    "concurrency": {"total": 2},
    "ntfy": {"server": "https://ntfy.sh", "topic": ""},
    # the read-only status page (`mahler serve`, DESIGN D10)
    "serve": {"host": "127.0.0.1", "port": 8787},
    # daily digest ping (mahler#6): one ntfy roll-up a day, sent on the first
    # tick at/after `hour` local time; once-only via the ledger kv table
    "digest": {"hour": 8},
    # daily janitor (mahler#7, DESIGN D12): prune worktrees of ended runs and
    # old mahler/snapshot|abandoned branches; once-only via the ledger kv table
    "janitor": {"retention_days": 14, "worktree_grace_hours": 24},
    # Order is preference. DESIGN D8: Claude plans; the free Antigravity pools
    # build first; Claude builds only under its reserve thresholds.
    "routing": {
        # sorting is light: when Claude is over its reserve, spend Gemini, and
        # keep Antigravity's scarcer Claude/Opus pool for building
        "sort": ["claude", "agy-gemini", "agy-claude", "claude-opus"],
        "build": ["agy-claude", "agy-gemini", "cline-free", "copilot", "kilo",
                  "claude-opus", "claude"],
    },
    "platforms": {
        "claude": {
            "enabled": True, "kind": "claude",
            "sort_model": "sonnet", "build_model": "",
            "max_size": "m",
            "soft": {"5h": 60, "weekly": 70},
            "hard": {"5h": 70, "weekly": 80},
            "stale_minutes": 15,
        },
        # Same CLI, same account/quota as "claude" (kind: claude) — forces Opus
        # for hard tasks (size:l by default via min_size: "l", or via explicit
        # `platform:claude-opus` pin) without spending Antigravity's separate,
        # scarcer Claude/Opus pool.
        "claude-opus": {
            "enabled": True, "kind": "claude",
            "sort_model": "opus", "build_model": "opus",
            "min_size": "l",
            "soft": {"5h": 60, "weekly": 70},
            "hard": {"5h": 70, "weekly": 80},
            "stale_minutes": 15,
        },
        "agy-claude": {
            "enabled": True, "kind": "agy", "pool": "Claude and GPT models",
            "model": "claude-opus-4-6-thinking",
            "max_size": "m",
            "soft": {"5h": 85, "weekly": 85},
            "hard": {"5h": 90, "weekly": 90},
            "stale_minutes": 5,
        },
        "agy-gemini": {
            "enabled": True, "kind": "agy", "pool": "Gemini Models",
            "model": "gemini-3.1-pro-high",
            "max_size": "m",
            "soft": {"5h": 85, "weekly": 85},
            "hard": {"5h": 90, "weekly": 90},
            "stale_minutes": 5,
        },
    },
    "projects": {},
}

# Cline's free models report no quota at all (verified 2026-09-12): it is
# "unmetered" — available until a rate-limit/quota error, then backed off.
# Its free models are weaker, so it only takes small items.
DEFAULTS["platforms"]["cline-free"] = {
    "enabled": True, "kind": "cline", "model": "",
    "metered": False, "backoff_minutes": 60, "max_size": "s",
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
    "metered": False, "backoff_minutes": 60, "max_size": "s",
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
DEFAULTS["platforms"]["copilot"] = {
    "enabled": True, "kind": "copilot", "model": "",
    "metered": True, "windows": ["monthly"], "monthly_cap_credits": 1500,
    "backoff_minutes": 60, "max_size": "s",
    "soft": {"monthly": 80}, "hard": {"monthly": 95},
    "stale_minutes": 360,
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path=None):
    path = path or CONFIG_PATH
    user = {}
    if os.path.exists(path):
        with open(path, "rb") as fh:
            user = tomllib.load(fh)
    return _merge(DEFAULTS, user)


def project_policy(cfg, name):
    """Defaults overlaid with one project's own entry."""
    return {**cfg["defaults"], **cfg["projects"].get(name, {}), "name": name}


def enabled_projects(cfg):
    return [project_policy(cfg, n) for n in cfg["projects"]
            if project_policy(cfg, n).get("enabled")]
