"""Quota readings: what each platform has left, and the burst window (D21–D23).

Usage is recorded from two places — a run's own log as it finishes, and the
periodic probe below — so both live here rather than in the tick that calls
them.
"""

import os
import json
from datetime import timedelta

from . import config, notify, platforms, presence, router
from .ledger import iso, parse


def quota_peers(cfg, platform):
    """Platforms that draw on the same login and quota as `platform` (D21, D25)."""
    source = cfg["platforms"].get(platform, {})
    group = source.get("quota_group", platform)
    account = config.account_of(source)
    return [p for p, pc in cfg["platforms"].items()
            if pc.get("quota_group", p) == group and config.account_of(pc) == account]


def _recent_warmup_fire(cfg, led, group, within=timedelta(hours=2)):
    """True when a warm-up nudge fired on `group` recently: its own small turn
    raises usage, which must never be mistaken for human use (mahler#536)."""
    for t in cfg.get("warmup", {}).get("targets", []):
        if group_of_target(cfg, t) != group:
            continue
        last = parse(led.get_kv(f"warmup:{t['name']}:last"))
        if last and led.now() - last < within:
            return True
    return False


def group_of_target(cfg, target):
    pc = cfg["platforms"].get(target["platform"], {})
    return pc.get("quota_group", target["platform"])


def note_human_rise(cfg, led, platform, pct=None):
    """A 5h usage rise (or a ≥90% reading) on `platform`'s login with no live
    Mahler run means Mike is using the account elsewhere. Sets the quota
    group's `human:` flag and emits the `human-use` / `human-out` events the
    warm-up relearn learns from (mahler#536) — per group only, never let a
    personal reading flag a work group (D25)."""
    group = cfg["platforms"].get(platform, {}).get("quota_group", platform)
    peers = quota_peers(cfg, platform)
    if any(r["platform"] in peers for r in led.active_runs()):
        return
    if _recent_warmup_fire(cfg, led, group):
        return
    led.set_kv(f"human:{group}", iso(led.now()))
    led.event("human-use", detail=group)
    if pct is not None and pct >= 90:
        led.event("human-out", detail=group)


def record_claude_usage(ctx, samples, backoff_until=None, check_human=False,
                         platform="claude"):
    """Record usage across the platforms that share `platform`'s Claude login.

    When check_human is True (the periodic probe path, not a run's own log), a
    5h usage increase with no live Claude run is treated as human use of the
    account elsewhere (Claude app on phone, the web UI, another session) and
    sets a quota-group flag that suppresses that login's D23 burst.
    """
    peers = quota_peers(ctx.cfg, platform)
    group = ctx.cfg["platforms"].get(platform, {}).get("quota_group", platform)
    for pname in peers:
        prev_5h = ctx.led.usage(pname).get("5h", {}).get("used_pct")
        for w, pct, resets in samples:
            if check_human and w == "5h" and prev_5h is not None and pct > prev_5h:
                note_human_rise(ctx.cfg, ctx.led, pname, pct)
            ctx.led.record_usage(pname, w, pct, resets)
        if backoff_until:
            pconf = ctx.cfg["platforms"][pname]
            for w in pconf.get("windows", router.WINDOWS):
                ctx.led.record_usage(pname, w, 100.0, backoff_until)


def _usage_needs_refresh(led, name, pconf):
    """True when usage data is stale or approaching stale — needs a refresh.

    A metered platform needs refresh when:
    - usage_state reports 'stale', or
    - any window's sample age exceeds stale_minutes - 3 (proactive), or
    - for claude platforms, oauth_usage hasn't been checked in >5 minutes.
    """
    if not pconf.get("metered", True):
        return False
    if pconf.get("kind") == "copilot":
        checked = parse(led.get_kv(f"copilot:no-quota:{name}"))
        if checked:
            return led.now() - checked >= timedelta(minutes=pconf.get("stale_minutes", 15))
    state, _ = router.usage_state(led, name, pconf)
    if state == "stale":
        return True
    stale_after = timedelta(minutes=pconf.get("stale_minutes", 15))
    proactive_threshold = stale_after - timedelta(minutes=3)
    now = led.now()
    usage = led.usage(name)
    for w in router.target_windows(led, name, pconf):
        u = usage.get(w)
        if u is None:
            continue
        sampled = parse(u.get("sampled_at"))
        if sampled and now - sampled > proactive_threshold:
            return True
    if pconf.get("kind") == "claude":
        oauth_last = parse(led.get_kv(f"probe:oauth:{name}"))
        if not oauth_last or now - oauth_last > timedelta(minutes=5):
            return True
    return False


def claude_oauth_source(cfg, pconf):
    """Where a Claude platform's login keeps its OAuth token (D25): this
    machine's own login is Claude Code's default keychain entry; another
    account's is whatever its config names, or the credentials file in its
    CLAUDE_CONFIG_DIR. None when there's nothing to read (use the probe)."""
    account = config.account_of(pconf)
    if account == config.DEFAULT_ACCOUNT:
        return {"keychain_service": "Claude Code-credentials"}
    acct = cfg.get("accounts", {}).get(account) or {}
    creds = acct.get("claude_credentials_file")
    if not creds and (acct.get("env") or {}).get("CLAUDE_CONFIG_DIR"):
        guess = os.path.join(os.path.expanduser(acct["env"]["CLAUDE_CONFIG_DIR"]),
                             ".credentials.json")
        creds = guess if os.path.exists(guess) else None
    if creds:
        return {"keychain_service": None, "credentials_file": os.path.expanduser(creds)}
    if acct.get("claude_keychain_service"):
        return {"keychain_service": acct["claude_keychain_service"]}
    return None


def _has_own_github_login(cfg, account):
    env = (cfg.get("accounts", {}).get(account) or {}).get("env") or {}
    return any(k in env for k in ("GH_CONFIG_DIR", "GH_TOKEN", "GITHUB_TOKEN"))


def codex_env(cfg, account):
    """The environment for reading or spending one Codex login (D25), or None
    when that login has no usable CLI home or auth. Personal means the default
    login, not whichever CODEX_HOME the calling agent inherited; API billing
    keys never reach a subscription read."""
    own_env = (cfg.get("accounts", {}).get(account) or {}).get("env") or {}
    if account != config.DEFAULT_ACCOUNT and not own_env.get("CODEX_HOME"):
        return None
    try:
        env = dict(config.run_env(cfg, account) or os.environ)
    except ValueError:
        return None
    home = os.path.expanduser(str(own_env.get("CODEX_HOME") or "~/.codex"))
    env["CODEX_HOME"] = home
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY"):
        env.pop(key, None)
    if not os.path.isfile(os.path.join(home, "auth.json")):
        return None
    return env


def refresh_codex(cfg, led, name, force=False):
    """One bounded probe per login, including failures; never fall back accounts."""
    pc = cfg["platforms"][name]
    if not pc.get("metered", True):
        return
    last = parse(led.get_kv(f"probe:{name}"))
    if not force and last and led.now() - last < timedelta(minutes=pc.get("stale_minutes", 15)):
        return
    account = config.account_of(pc)
    first_key = f"codex:unsupported-shape:first:{account}"
    shape_key = f"codex:unsupported-shape:shape:{account}"
    last_key = f"codex:unsupported-shape:last:{account}"
    notified_shape_key = f"notified:codex-unsupported:{account}"
    env = codex_env(cfg, account)
    if env is None:
        return
    samples = platforms.probe_codex(env=env)
    prev_5h = led.usage(name).get("5h", {}).get("used_pct")
    for peer in quota_peers(cfg, name):
        peer_pc = cfg["platforms"][peer]
        if config.account_of(peer_pc) != account or not peer_pc.get("metered", True):
            continue
        led.set_kv(f"probe:{peer}", iso(led.now()))
        if isinstance(samples, platforms.CodexUsage):
            led.set_kv(f"codex:quota:{peer}", json.dumps({
                **samples.metadata, "sampled_at": iso(led.now())}))
        for w, pct, resets in samples:
            led.record_usage(peer, w, pct, resets)
    if not isinstance(samples, platforms.CodexUsage):
        led.set_kv(first_key, "")
        led.set_kv(shape_key, "")
        led.set_kv(last_key, "")
        led.set_kv(notified_shape_key, "")
        return
    raw_windows = samples.metadata.get("windows", [])
    if router.is_supported_codex_shape(raw_windows):
        led.set_kv(first_key, "")
        led.set_kv(shape_key, "")
        led.set_kv(last_key, "")
        led.set_kv(notified_shape_key, "")
    else:
        shape_label = ", ".join(w["window"] for w in raw_windows) if raw_windows else "none"
        prev_shape = led.get_kv(shape_key)
        last_probe_time = parse(led.get_kv(last_key))
        max_gap = timedelta(minutes=pc.get("stale_minutes", 15) * 5)
        if prev_shape != shape_label or (last_probe_time and led.now() - last_probe_time > max_gap):
            led.set_kv(first_key, iso(led.now()))
            led.set_kv(shape_key, shape_label)
            led.set_kv(notified_shape_key, "")
        first_time = parse(led.get_kv(first_key))
        if first_time is None:
            first_time = led.now()
            led.set_kv(first_key, iso(first_time))
            led.set_kv(shape_key, shape_label)
        led.set_kv(last_key, iso(led.now()))
        if led.now() - first_time >= timedelta(hours=4):
            if not led.get_kv(notified_shape_key):
                notify.send(cfg, f"Codex ({account}) unsupported quota shape: {shape_label}",
                            f"Codex platform {name} ({account}) reported unsupported quota shape '{shape_label}' continuously for 4 hours.",
                            priority="high", tags="warning")
                led.set_kv(notified_shape_key, iso(led.now()))
    if isinstance(samples, platforms.CodexUsage):
        new_5h = next((pct for w, pct, _ in samples if w == "5h"), None)
        if prev_5h is not None and new_5h is not None and new_5h > prev_5h:
            note_human_rise(cfg, led, name, new_5h)
        is_exhausted = bool(samples.metadata.get("blocked") or any(pct >= 100.0 for _, pct, _ in samples))
        notified_key = f"notified:codex-exhausted:{account}"
        if is_exhausted:
            if not led.get_kv(notified_key):
                credits = samples.metadata.get("reset_credits")
                if credits:
                    msg = (f"Codex ({account}) reached 100% quota ({credits} reset credit"
                           f"{'s' if credits != 1 else ''} available). Hit reset in ChatGPT to refresh.")
                elif credits == 0:
                    msg = f"Codex ({account}) reached 100% quota (no reset credits remaining)."
                else:
                    msg = f"Codex ({account}) reached 100% quota."
                notify.send(cfg, f"Codex ({account}) quota exhausted", msg,
                            priority="high", tags="warning,hourglass")
                led.set_kv(notified_key, iso(led.now()))
        else:
            if led.get_kv(notified_key):
                led.set_kv(notified_key, "")


def refresh_usage(ctx, projects):
    led, cfg = ctx.led, ctx.cfg
    wanted = set()
    for p in projects:
        queued = led.items(p["name"], ["inbox", "ready", "verifying"])
        if queued:
            # a pinned platform may sit in no route (metered opt-in, e.g. kiro);
            # without a probe its usage stays stale and the router skips it (#708)
            accounts = config.accounts_of(p)
            wanted |= {pin for it in queued if (pin := it["pin"]) in cfg["platforms"]
                       and config.account_of(cfg["platforms"][pin]) in accounts}
            for account in accounts:
                routing = router.routing_for(cfg, account)
                wanted |= {n for role in ("sort", "build", "plan", "review") for n in routing.get(role, [])}
    wanted |= {r["platform"] for r in led.active_runs()}
    wanted = {n for n in wanted if n in cfg["platforms"]}
    # probe_agy and a copilot probe without an account's own GitHub login read
    # this machine's own logins, so they only ever feed its own platforms (D25)
    own = {n for n, pc in cfg["platforms"].items()
           if config.account_of(pc) == config.DEFAULT_ACCOUNT}
    agy = [n for n in wanted & own if cfg["platforms"][n].get("kind") == "agy"
           and router.usage_state(led, n, cfg["platforms"][n])[0] == "stale"]
    if agy:
        pools = platforms.probe_agy()
        for name in own:
            pc = cfg["platforms"][name]
            prev_5h = led.usage(name).get("5h", {}).get("used_pct")
            for w, pct, resets in pools.get(pc.get("pool"), []):
                if w == "5h" and prev_5h is not None and pct > prev_5h:
                    note_human_rise(cfg, led, name, pct)
                led.record_usage(name, w, pct, resets)
    for name in wanted:
        pconf = cfg["platforms"][name]
        account = config.account_of(pconf)
        if not _usage_needs_refresh(led, name, pconf):
            continue
        if pconf.get("kind") == "codex":
            refresh_codex(cfg, led, name)
            continue
        if pconf.get("kind") == "copilot":
            if name not in own and not _has_own_github_login(cfg, account):
                continue
            last = parse(led.get_kv(f"probe:{name}"))
            if last and led.now() - last < timedelta(minutes=pconf.get("stale_minutes", 15)):
                continue
            # Fan the one AI-credits reading out to every platform sharing this
            # account's Copilot quota_group (e.g. copilot-high) — same account,
            # same credits, one `gh api` call — mirroring record_claude_usage's
            # peer fan-out below.
            samples = platforms.probe_copilot(pconf.get("monthly_cap_credits", 1500),
                                              env=config.run_env(cfg, account))
            for peer in quota_peers(cfg, name):
                if not cfg["platforms"][peer].get("metered", True):
                    continue
                no_quota = isinstance(samples, platforms.CopilotNoQuota)
                led.set_kv(f"copilot:no-quota:{peer}", iso(led.now()) if no_quota else "")
                if no_quota and led.get_kv(f"probe:{peer}"):
                    led.set_kv(f"probe:{peer}", "")
                for w, pct, resets in samples:
                    led.record_usage(peer, w, pct, resets)
                if samples:
                    led.set_kv(f"probe:{peer}", iso(led.now()))
            continue
        if pconf.get("kind") == "kiro":
            # kiro-cli reads its own login from the macOS keychain, so only
            # this machine's own account can be probed (D25).
            if name not in own:
                continue
            last = parse(led.get_kv(f"probe:{name}"))
            if last and led.now() - last < timedelta(minutes=pconf.get("stale_minutes", 15)):
                continue
            samples = platforms.probe_kiro(env=config.run_env(cfg, account))
            for peer in quota_peers(cfg, name):
                if not cfg["platforms"][peer].get("metered", True):
                    continue
                for w, pct, resets in samples:
                    led.record_usage(peer, w, pct, resets)
                if samples:
                    led.set_kv(f"probe:{peer}", iso(led.now()))
            continue
        if pconf.get("kind") != "claude":
            continue
        # D22: during the peak window, keep the zero-token OAuth reading and skip
        # the lean Claude probe (`probe_claude`, which costs tokens) — the window
        # is a headroom rule, not a quota rule, so a stale reading is still "over
        # the line" and Claude simply won't start.
        peak_active, _ = router.peak_state(cfg, led)
        source = claude_oauth_source(cfg, pconf)
        free = platforms.oauth_usage(**source) if source else []   # zero tokens
        if free:
            record_claude_usage(ctx, free, check_human=True, platform=name)
            led.set_kv(f"probe:oauth:{name}", iso(led.now()))
            continue
        if peak_active:
            continue
        last = parse(led.get_kv(f"probe:{name}"))
        if last and led.now() - last < timedelta(minutes=pconf.get("stale_minutes", 15)):
            continue
        probed = platforms.probe_claude(env=config.run_env(cfg, account))
        if probed:
            record_claude_usage(ctx, probed, check_human=True, platform=name)
            for cname in quota_peers(cfg, name):
                led.set_kv(f"probe:{cname}", iso(led.now()))


def compute_burst(ctx, projects):
    """D23 burst lines for Claude routing (cached per tick).

    In the last lead-time before a window resets, Claude's reserve expires
    unused, so burst lines (default 90%/97%) let Claude build first with higher
    headroom. Suppressed while you're actively using Claude: the 5h-usage-rise
    flag set by record_claude_usage during the probe, or recent Claude Code
    transcript activity in a managed project. Returns the cached value on
    repeated calls within one tick. Logs the burst state once per tick.
    """
    if ctx.burst_lines is not None:
        return ctx.burst_lines
    lines = router.all_bursts(ctx.cfg, ctx.led)
    suppressed = []
    if lines and ctx.hot_hold:
        quiet = timedelta(minutes=ctx.cfg["burst"].get("human_quiet_minutes", 20))
        for group in list(lines):
            flag = router._ts(ctx.led.get_kv(f"human:{group}"))
            if flag and ctx.led.now() - flag < quiet:
                suppressed.append(f"{group}: 5h usage rose with no live Claude run")
                del lines[group]
        for group in presence.human_claude_groups(ctx.cfg, projects):
            if group in lines:
                suppressed.append(f"{group}: Claude in use (recent transcript activity)")
                del lines[group]
        lines = lines or None
    if suppressed:
        ctx.say(f"D23: burst window open — deferring ({'; '.join(suppressed)})")
    if lines:
        kind = router.burst_kind(lines)
        scope = "5h and weekly" if kind == "weekly" else "5h only"
        ctx.say(f"D23: {kind} burst active — Claude builds first, lines 90/97 (scope: {scope})")
    ctx.burst_lines = lines
    return lines
