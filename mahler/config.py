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
        "run_timeout_minutes": 60,
        "progress_timeout_minutes": 20,
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
    # Order is preference. DESIGN D8: Claude plans; the free Antigravity pools
    # build first; Claude builds only under its reserve thresholds.
    "routing": {
        "sort": ["claude", "agy-claude", "agy-gemini"],
        "build": ["agy-claude", "agy-gemini", "cline-free", "claude"],
    },
    "platforms": {
        "claude": {
            "enabled": True, "kind": "claude",
            "sort_model": "sonnet", "build_model": "",
            "soft": {"5h": 60, "weekly": 70},
            "hard": {"5h": 70, "weekly": 80},
            "stale_minutes": 15,
        },
        "agy-claude": {
            "enabled": True, "kind": "agy", "pool": "Claude and GPT models",
            "model": "claude-opus-4-6-thinking",
            "soft": {"5h": 85, "weekly": 85},
            "hard": {"5h": 90, "weekly": 90},
            "stale_minutes": 5,
        },
        "agy-gemini": {
            "enabled": True, "kind": "agy", "pool": "Gemini Models",
            "model": "gemini-3.1-pro-high",
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
