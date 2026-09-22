"""Periodic self-audit of Mahler's own platform tier/capability assumptions
(mahler#206) — distinct from a managed project's D20 maintenance passes: this
audits `config.py`'s own tier/max_size/min_size judgment calls and each
platform's DESIGN.md "verified" annotation, not a managed project's codebase.

It reuses D20's cadence/threshold checkpoint machinery (`Ledger.maintenance_due`,
`last_filed_at` + `merged_since`) instead of inventing a second scheduler, and
files its finding the same way a maintenance pass does: as an issue for a
human (or a future pass) to act on. It never changes `config.py` itself —
tier ordering is a judgment call, not a mechanical recalibration like time
estimates (mahler#59).
"""

import json
import os
import re
from datetime import datetime, timedelta

from . import config, router
from .gh import GHError

TITLE = "Platform Tier/Capability Assumptions Audit"

# A "verified ... 2026-09-13"-shaped mention anywhere within a short window of
# text is read as that window's evidence date. DESIGN.md is prose, not a table
# Mahler owns mechanically, so this is an approximate signal for a human
# reviewer, not a precise citation.
VERIFIED_RE = re.compile(r"verified[^.\n]{0,120}?(\d{4}-\d{2}-\d{2})", re.IGNORECASE)

# Prose names DESIGN.md uses for each platform config key — they don't match
# 1:1, so an alias list is needed to associate a "verified" mention with the
# platform it's about.
PLATFORM_ALIASES = {
    "claude-low": ["claude code", "claude-low", "haiku"],
    "claude": ["claude code"],
    "claude-opus": ["claude code", "claude-opus"],
    "agy-claude": ["antigravity: claude", "agy-claude"],
    "agy-gemini": ["antigravity: gemini", "agy-gemini"],
    "cline-free": ["cline"],
    "kilo": ["kilo"],
    "copilot": ["copilot cli", "copilot"],
    "copilot-high": ["copilot-high"],
    "codex-low": ["codex cli", "codex-low", "gpt-5.6-luna"],
    "codex": ["codex cli", "codex"],
    "codex-high": ["codex-high"],
}


def _design_md_text():
    path = os.path.join(config.REPO_ROOT, "DESIGN.md")
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def verified_dates(text):
    """{platform: latest 'verified YYYY-MM-DD' date found near an alias}."""
    lower = text.lower()
    out = {}
    for platform, aliases in PLATFORM_ALIASES.items():
        best = None
        for alias in aliases:
            start = 0
            while True:
                idx = lower.find(alias, start)
                if idx == -1:
                    break
                window = text[max(0, idx - 150):idx + 700]
                m = VERIFIED_RE.search(window)
                if m and (best is None or m.group(1) > best):
                    best = m.group(1)
                start = idx + len(alias)
        if best:
            out[platform] = best
    return out


def _age_days(date_str, now):
    return (now.date() - datetime.fromisoformat(date_str).date()).days


def _root_platform(cfg, name):
    """Walk a platform's `from` chain (DESIGN D25) to its root, mirroring
    `config.resolve_platforms`'s own cycle/bad-base handling. Returns the
    root platform's name for a platform that is actually derived through a
    valid acyclic chain, or `None` if the platform isn't derived, or the
    chain cycles or names an unknown base. Never mutates `cfg` or raises."""
    plats = cfg.get("platforms", {})
    seen = {name}
    current = name
    while True:
        pconf = plats.get(current)
        if pconf is None:
            return None
        base = pconf.get("from")
        if not base:
            return current if current != name else None
        if base in seen or base not in plats:
            return None
        seen.add(base)
        current = base


def stale_report(cfg, dates, now, stale_days):
    """One row per enabled platform: (name, date_or_None, age_or_None, stale).
    A derived platform (`from = "<base>"`, D25) with no annotation of its own
    inherits its root base's date, since that date is evidence about the CLI
    and model it shares (mahler#227)."""
    rows = []
    for name, pconf in cfg["platforms"].items():
        if not pconf.get("enabled", True):
            continue
        date = dates.get(name)
        if date is None:
            root = _root_platform(cfg, name)
            if root:
                date = dates.get(root)
        age = _age_days(date, now) if date else None
        stale = date is None or age >= stale_days
        rows.append((name, date, age, stale))
    return sorted(rows, key=lambda r: (r[3] is False, r[0]))


def _pct(n, d):
    return round(100.0 * n / d, 1) if d else None


def size_gate(pconf):
    """Item sizes admitted by the router's ordinary builder size bounds."""
    minimum = pconf.get("min_size")
    maximum = pconf.get("max_size")
    return {size for size, rank in router.SIZES.items()
            if (not minimum or rank >= router.SIZES[minimum])
            and (not maximum or rank <= router.SIZES[maximum])}


def _sizes_text(sizes):
    return "+".join(sorted(sizes, key=router.SIZES.get)) or "none"


def outcome_report(cfg, outcomes, escalations):
    """One row per enabled platform, ordered by declared tier (`router.tier_of`):
    (name, tier, runs, done_pct, needs_you_pct, escalated_from, sizes, by_size)."""
    rows = []
    for name, pconf in cfg["platforms"].items():
        if not pconf.get("enabled", True):
            continue
        stats = outcomes.get(name, {"runs": 0, "done": 0, "needs_you": 0, "by_size": {}})
        rows.append((name, router.tier_of(pconf), stats["runs"],
                     _pct(stats["done"], stats["runs"]),
                     _pct(stats["needs_you"], stats["runs"]),
                     escalations.get(name, 0), size_gate(pconf),
                     stats.get("by_size", {})))
    return sorted(rows, key=lambda r: (r[1], r[0]))


def tier_inversions(rows, min_runs=3, margin=15.0):
    """Flag a lower-tier platform whose done-rate beats a higher-tier one by
    at least `margin` points, both with at least `min_runs` observed runs —
    with overlapping size gates. This is a mechanical signal, not a conclusion:
    the finding still needs a human judgment call (mahler#206 proposal item 3)."""
    found = []
    for lo in rows:
        for hi in rows:
            if hi[1] > lo[1]:
                common_sizes = lo[6] & hi[6]
                if not common_sizes:
                    continue
                lo_runs = sum(lo[7].get(sz, {}).get("runs", 0) for sz in common_sizes)
                hi_runs = sum(hi[7].get(sz, {}).get("runs", 0) for sz in common_sizes)
                if lo_runs >= min_runs and hi_runs >= min_runs:
                    lo_done = sum(lo[7].get(sz, {}).get("done", 0) for sz in common_sizes)
                    hi_done = sum(hi[7].get(sz, {}).get("done", 0) for sz in common_sizes)
                    lo_pct = round(100.0 * lo_done / lo_runs, 1) if lo_runs else 0.0
                    hi_pct = round(100.0 * hi_done / hi_runs, 1) if hi_runs else 0.0
                    if lo_pct - hi_pct >= margin:
                        found.append((lo[0], lo[1], lo_pct, hi[0], hi[1], hi_pct))
    return found


def build_body(cfg, led, pol):
    now = led.now()
    dates = verified_dates(_design_md_text())
    stale_days = pol["stale_verified_days"]
    stale_rows = stale_report(cfg, dates, now, stale_days)
    since = now - timedelta(days=180)
    outcomes = led.platform_outcomes(since=since)
    escalations = led.platform_escalations(since=since)
    out_rows = outcome_report(cfg, outcomes, escalations)
    min_runs = pol["inversion_min_runs"]
    inversions = tier_inversions(out_rows, min_runs=min_runs,
                                margin=pol["inversion_margin_pct"])
    by_name = {row[0]: row for row in out_rows}

    lines = [
        "Periodic self-audit of `config.py`'s platform tier/capability assumptions "
        "(mahler#206) — mechanical data only, no tier/size change applied. Tier "
        "ordering stays a judgment call for whoever picks this up.",
        "",
        f"## DESIGN.md verified-date staleness (flag threshold: {stale_days} days)",
        "",
        "| Platform | Last verified mention | Age (days) | Flag |",
        "|---|---|---|---|",
    ]
    for name, date, age, stale in stale_rows:
        flag = "**STALE**" if stale and date else ("**NO ANNOTATION FOUND**" if stale else "ok")
        root = _root_platform(cfg, name) if dates.get(name) is None else None
        platform_cell = f"`{name}` (via `{root}`)" if root else name
        lines.append(f"| {platform_cell} | {date or '—'} | "
                     f"{age if age is not None else '—'} | {flag} |")

    lines += [
        "",
        "## Observed ledger outcomes (build/fix runs, last 180 days)",
        "",
        "| Platform | Tier | Runs | Done % | Needs-you % | Escalated away from (count) | Sizes |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, tier, runs, done_pct, needs_you_pct, esc, sizes, by_size in out_rows:
        lines.append(f"| {name} | {tier} | {runs} | "
                     f"{done_pct if done_pct is not None else '—'} | "
                     f"{needs_you_pct if needs_you_pct is not None else '—'} | {esc} | {_sizes_text(sizes)} |")

    lines += ["", "## Possible tier inconsistencies"]
    if inversions:
        lines.append("")
        for lo_name, lo_tier, lo_pct, hi_name, hi_tier, hi_pct in inversions:
            lo, hi = by_name[lo_name], by_name[hi_name]
            lines.append(f"- `{lo_name}` (tier {lo_tier}, sizes {_sizes_text(lo[6])}, "
                         f"{lo_pct}% of {lo[2]} runs) outperforms "
                         f"`{hi_name}` (tier {hi_tier}, sizes {_sizes_text(hi[6])}, "
                         f"{hi_pct}% of {hi[2]} runs) by "
                         f"{round(lo_pct - hi_pct, 1)} points — worth a look.")
    else:
        lines.append(f"None found (comparisons require overlapping size gates and at least "
                     f"{min_runs} runs on both sides, with a done-rate gap of at least "
                     f"{pol['inversion_margin_pct']} points).")

    return "\n".join(lines)


def _has_open_pass(led, project):
    """D20 discipline (mahler#204): at most one pass in flight per project,
    across every pass kind — including this one."""
    for it in led.items(project):
        if it["state"] == "done":
            continue
        labels = json.loads(it["labels"] or "[]")
        if any(l.startswith("pass:") for l in labels):
            return True
        if (it["title"] or "").strip().lower() == TITLE.strip().lower():
            return True
    return False


def queue(ctx, projects):
    """File the due platform-tier/capability self-audit as an issue, deduped
    and cadenced the same way a D20 maintenance pass is."""
    led = ctx.led
    pol = config.platform_audit_policy(ctx.cfg)
    if not pol["enabled"]:
        return
    project = pol["project"]
    if project not in {p["name"] for p in projects}:
        return
    if project in ctx.passes_filed or _has_open_pass(led, project):
        return
    if not led.maintenance_due(project, config.PLATFORM_AUDIT_PASS, policy=pol):
        return

    label = f"pass:{config.PLATFORM_AUDIT_PASS}"
    body = build_body(ctx.cfg, led, pol)
    ctx.say(f"{project}: queuing {config.PLATFORM_AUDIT_PASS} pass")
    if ctx.dry_run:
        ctx.passes_filed.add(project)
        return
    try:
        ctx.gh(project).ensure_pass_label(config.PLATFORM_AUDIT_PASS)
        ctx.gh(project).create_issue(TITLE, body, ["type:chore", "size:s", "p2", label])
        ctx.passes_filed.add(project)
        led.reset_maintenance(project, config.PLATFORM_AUDIT_PASS)
    except GHError as e:
        ctx.say(f"{project}: failed to file {config.PLATFORM_AUDIT_PASS} pass — {e}")
