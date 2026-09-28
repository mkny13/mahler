"""Chain idle login windows with one small turn; never spend without a free read.

Learned warm-up times (mahler#536): the relearn pass collects data on when Mike
starts using each login and when he runs out. The learned times are report-only —
they never change any warm-up window. `learn_time` is a pure function covering
the rule; `relearn` runs once daily and persists the result as kv entries.
"""

import glob
import json
import os
import re
import statistics
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone

from . import config, platforms, presence, usage
from .ledger import iso, parse


def environment(cfg, target):
    # Personal probes must not inherit the work login of an invoking agent.
    env = dict(config.run_env(cfg, target['account']) or os.environ)
    if target['account'] == config.DEFAULT_ACCOUNT:
        for key in config.CREDENTIAL_VARS:
            env.pop(key, None)
        env.update({k: os.path.expanduser(str(v)) for k, v in
                    cfg.get('accounts', {}).get('personal', {}).get('env', {}).items()})
    home_var = {'claude': 'CLAUDE_CONFIG_DIR', 'codex': 'CODEX_HOME'}.get(target['kind'])
    if target['account'] != config.DEFAULT_ACCOUNT and home_var and not env.get(home_var):
        raise ValueError('login directory missing')
    # Subscription-only: never let an inherited API key select paid API billing.
    for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'OPENAI_API_KEY', 'CODEX_API_KEY'):
        env.pop(key, None)
    return env


def read_usage(ctx, target):
    pc = ctx.cfg['platforms'][target['platform']]
    env = environment(ctx.cfg, target)
    if target['kind'] == 'claude':
        source = usage.claude_oauth_source(ctx.cfg, pc)
        return platforms.oauth_usage(**source) if source else []
    if target['kind'] == 'codex':
        return platforms.probe_codex(env=env)
    return platforms.probe_agy().get(pc.get('pool'), [])


def record(ctx, target, samples):
    for peer in usage.quota_peers(ctx.cfg, target['platform']):
        if config.account_of(ctx.cfg['platforms'][peer]) != target['account']:
            continue
        for window, pct, reset in samples:
            ctx.led.record_usage(peer, window, pct, reset)
        if isinstance(samples, platforms.CodexUsage):
            ctx.led.set_kv(f'codex:quota:{peer}', json.dumps({
                **samples.metadata, 'sampled_at': iso(ctx.led.now())}))


def nudge(ctx, target):
    env = environment(ctx.cfg, target)
    if target['kind'] == 'claude':
        samples = platforms.probe_claude(env=env)
        if not samples:
            raise RuntimeError('Claude nudge returned no usage')
        record(ctx, target, samples)
        return
    if target['kind'] == 'agy':
        cmd = [platforms.agy_exe(), '-p', 'Reply ok.', '--model', 'gemini-3.8-flash',
               '--effort', 'low', '--output-format', 'json', '--print-timeout', '1m']
        result = subprocess.run(cmd, cwd=config.HOME, env=env, capture_output=True,
                                text=True, timeout=90)
        if result.returncode or json.loads(result.stdout).get('status') != 'SUCCESS':
            raise RuntimeError('Antigravity nudge failed')
    else:
        model = ctx.cfg['platforms'][target['platform']].get('model')
        if not model:
            raise ValueError('warmup Codex platform requires an explicit model')
        cmd = [platforms.codex_exe(), 'exec', '--ephemeral', '--color', 'never', '--json',
               '--skip-git-repo-check', '--sandbox', 'read-only', '-m', model,
               '-c', 'model_reasoning_effort="low"', 'Reply ok.']
        with tempfile.TemporaryDirectory(prefix='mahler-warmup-') as cwd:
            result = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True,
                                    text=True, timeout=90)
        events = []
        for line in result.stdout.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        if result.returncode or not any(e.get('type') == 'turn.completed' for e in events):
            raise RuntimeError('Codex nudge failed')


def next_active(target, when):
    """Local wall-clock schedule, including end=24:00 and weekend rollover."""
    local = when.astimezone()
    for offset in range(8):
        day = local.date() + timedelta(days=offset)
        if target['days'] == 'off' or (target['days'] == 'weekdays' and day.weekday() >= 5):
            continue
        hour, minute = map(int, target['start'].split(':'))
        # Convert each date separately so DST changes retain the local start hour.
        start = local.replace(tzinfo=None, year=day.year, month=day.month, day=day.day,
                              hour=hour, minute=minute, second=0, microsecond=0).astimezone()
        h, m = map(int, target['end'].split(':'))
        end = start.replace(tzinfo=None, hour=0, minute=0) + timedelta(hours=h, minutes=m)
        end = end.astimezone()
        if local < end:
            return max(local, start)
    return None


def active(row, now):
    reset = parse(row.get('resets_at'))
    return bool(reset and reset > now and row.get('used_pct', 0) > 0)


def exhausted(rows, now):
    return any(row['used_pct'] >= 100 and
               (not parse(row.get('resets_at')) or parse(row['resets_at']) > now)
               for row in rows.values())


def run_target(ctx, target, manual=False):
    led, now = ctx.led, ctx.led.now()
    name, platform = target['name'], target['platform']
    key = f'warmup:{name}'
    rows = led.usage(platform)
    row = rows.get('5h', {})
    reset = parse(row.get('resets_at'))
    last, failed = parse(led.get_kv(key + ':last')), parse(led.get_kv(key + ':failed'))
    expected = max([now] + ([reset] if active(row, now) else []) +
                   ([last + timedelta(hours=4)] if last else []) +
                   ([failed + timedelta(minutes=30)] if failed else []))
    next_time = next_active(target, expected)
    state = 'active' if active(row, now) else ('idle' if row else 'unknown')
    action = None
    if led.paused():
        action = 'skipped-paused'
    elif not ctx.cfg['warmup']['enabled'] or not ctx.cfg['platforms'][platform].get('enabled', True) or target['days'] == 'off':
        action = 'skipped-disabled'
    elif not manual and next_active(target, now) != now:
        action = 'skipped-hours'
    elif failed and now < failed + timedelta(minutes=30):
        action = 'skipped-backoff'
    elif active(row, now):
        action = 'skipped-active'
    elif any(exhausted(led.usage(peer), now) for peer in
             usage.quota_peers(ctx.cfg, platform)
             if config.account_of(ctx.cfg['platforms'][peer]) == target['account']):
        action = 'skipped-exhausted'
    if action is None:
        samples = read_usage(ctx, target)
        fresh = {w: {'used_pct': pct, 'resets_at': r} for w, pct, r in samples}
        if not ctx.dry_run:
            record(ctx, target, samples)
        if '5h' not in fresh:
            action = 'skipped-unknown'
        elif exhausted(fresh, now) or getattr(samples, 'metadata', {}).get('blocked'):
            action = 'skipped-exhausted'
        elif active(fresh['5h'], now):
            action = 'skipped-active'
            state, reset = 'active', parse(fresh['5h']['resets_at'])
            next_time = next_active(target, reset)
        else:
            state = 'idle'
            reset = parse(fresh['5h'].get('resets_at'))
            fresh_reset = parse(fresh['5h'].get('resets_at'))
            later_reset = (fresh_reset and fresh_reset <= now and last and
                           fresh_reset - timedelta(hours=5) > last)
            if last and now < last + timedelta(hours=4) and not later_reset:
                action = 'skipped-guard'
            elif ctx.dry_run:
                action = 'would-fire'
            else:
                # Persist before spending, so a crash cannot repeat the turn.
                led.set_kv(key + ':last', iso(now))
                try:
                    nudge(ctx, target)
                except Exception:
                    led.set_kv(key + ':last', iso(last) if last else '')
                    raise
                record(ctx, target, read_usage(ctx, target))
                led.set_kv(key + ':failed', '')
                action = 'fired'
                reset = parse(led.usage(platform).get('5h', {}).get('resets_at'))
                next_time = next_active(target, max(now + timedelta(hours=4), reset or now))
    if not ctx.dry_run:
        led.event('warmup', detail={'target': name, 'status': action})
    ctx.say(f"{name}: window={state}, reset={iso(reset) if reset else 'unknown'}; "
            f"{action}; next={iso(next_time) if next_time else 'off'}")


def warmup_pass(ctx, target=None, manual=False):
    targets = ctx.cfg.get('warmup', {}).get('targets', [])
    if target is not None and not any(t['name'] == target for t in targets):
        raise ValueError(f'unknown warmup target: {target}')
    failures = 0
    for item in targets:
        if target is not None and item['name'] != target:
            continue
        try:
            run_target(ctx, item, manual=manual)
        except Exception as exc:
            failures += 1
            if not ctx.dry_run:
                ctx.led.set_kv(f"warmup:{item['name']}:failed", iso(ctx.led.now()))
                ctx.led.event('warmup', detail={'target': item['name'], 'status': 'failed',
                                              'error': type(exc).__name__})
            ctx.say(f"{item['name']}: failed ({type(exc).__name__}); retry in 30 minutes")
    return failures


# ---------- learned warm-up times (mahler#536: collect data, never reschedule) ----------
#
# The owner asked to *learn* each login's best warm-up time from when he actually
# starts using it and when he runs out — but to change nothing: the learned times
# are a report (dry-run output + a Capacity console line), never a new schedule.
#
# Per day of use we derive a first-use time F (local) and an exhaustion E (minutes
# from F until the login's 5h window ran out). `learn_time` then applies the
# decided rule to a slice of days. Read-only signals only: transcript timestamps
# and ledger events, never transcript content, never Mahler's own worktrees.

LEARN_WINDOW_DAYS = 30       # the owner reviews the report after a month
LEARN_MIN_DAYS = 5           # fewer days of use than this → keep the configured time
LEARN_ROUND_MINUTES = 15     # learned times are rounded to a quarter hour
RELEARN_AFTER_HOUR = 3       # once daily, on the first tick at/after 03:00 local

TS_RE = re.compile(r'"timestamp"\s*:\s*"'
                   r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"')


def group_of(cfg, target):
    """The quota group string a target's human-use events are filed under."""
    pc = cfg['platforms'].get(target['platform'], {})
    return pc.get('quota_group', target['platform'])


def learn_time(days, weekend):
    """Pure rule for one slice of days. `days` is a list of (F, E) minute
    offsets: F minutes after local midnight when use started, E minutes from
    F until exhaustion (None when no exhaustion was seen that day).

    - W = F + E − 5h, clamped to [F − 4h30, F − 30min] per day (the window
      must be alive when Mike starts, and reset ≥30 min in), combined by
      median across exhaustion days;
    - no exhaustion days → W = F − 3h (median F);
    - fewer than LEARN_MIN_DAYS days with any use → None (keep the
      configured time);
    - a weekend slice with no use at all → 'off';
    - rounded to 15 minutes.

    Returns minutes after local midnight, the string 'off', or None.
    """
    used = [(f, e) for f, e in days if f is not None]
    if not used:
        return 'off' if weekend else None
    if len(used) < LEARN_MIN_DAYS:
        return None
    exhausted_days = [(f, e) for f, e in used if e is not None]
    if exhausted_days:
        candidates = [max(f - 270, min(f - 30, f + e - 300)) for f, e in exhausted_days]
        w = statistics.median(candidates)
    else:
        w = statistics.median(f for f, _ in used) - 180
    return int(w / LEARN_ROUND_MINUTES + 0.5) * LEARN_ROUND_MINUTES % 1440


def hhmm(minutes):
    return 'off' if minutes == 'off' else f'{minutes // 60:02d}:{minutes % 60:02d}'


def _excluded_cwds():
    """cwd prefixes that mean Mahler's own activity, never Mike's use."""
    return (os.path.abspath(config.WORKTREES), os.path.abspath(tempfile.gettempdir()))


def transcript_first_use(root, since, now, excluded=None):
    """Per local date, the earliest transcript timestamp in `root` (a Claude
    `projects/` or Codex `sessions/` tree), read-only: timestamps and cwd
    mentions only, never message content. Files untouched since `since` are
    skipped; lines whose cwd sits under an excluded prefix (Mahler worktrees,
    nudge temp dirs) never count as human use."""
    excluded = _excluded_cwds() if excluded is None else excluded
    out = {}
    for path in glob.glob(os.path.join(os.path.expanduser(str(root)), '**', '*.jsonl'),
                          recursive=True):
        try:
            if datetime.fromtimestamp(os.path.getmtime(path), timezone.utc) < since:
                continue
            with open(path, encoding='utf-8', errors='replace') as fh:
                for line in fh:
                    m = TS_RE.search(line)
                    if not m or any(x in line for x in excluded):
                        continue
                    ts = datetime.fromisoformat(m.group(1).replace('Z', '+00:00'))
                    if not (since <= ts <= now):
                        continue
                    local = ts.astimezone()
                    day = local.date()
                    if day not in out or local < out[day]:
                        out[day] = local
        except OSError:
            continue
    return out


def transcript_roots(cfg, target):
    """Read-only transcript roots for a target's login (D25: per account).
    Claude: the account's Claude Code `projects` dir(s); Codex: its CLI home's
    `sessions`; Antigravity keeps no local transcripts (empty)."""
    account = target['account']
    if target['kind'] == 'claude':
        if account == config.DEFAULT_ACCOUNT:
            return [presence.CLAUDE_PROJECTS]
        home = (cfg.get('accounts', {}).get(account, {}).get('env') or {}).get('CLAUDE_CONFIG_DIR')
        return [os.path.join(os.path.expanduser(home), 'projects')] if home else []
    if target['kind'] == 'codex':
        home = (cfg.get('accounts', {}).get(account, {}).get('env') or {}).get('CODEX_HOME')
        return [os.path.join(os.path.expanduser(str(home or '~/.codex')), 'sessions')]
    return []


def collect_days(cfg, led, target, since, now):
    """Per local date, (F, E) minute offsets for one target, merging the
    read-only signals: transcript first use, `human-use` rises, and `human-out`
    exhaustion events on the target's quota group."""
    first, out = {}, {}
    for root in transcript_roots(cfg, target):
        for day, ts in transcript_first_use(root, since, now).items():
            if day not in first or ts < first[day]:
                first[day] = ts
    group = group_of(cfg, target)
    for row in led.q("SELECT at, kind FROM events WHERE project IS NULL AND detail=? "
                     "AND kind IN ('human-use', 'human-out') ORDER BY at", (group,)):
        ts = parse(row['at']).astimezone()
        day = ts.date()
        if row['kind'] == 'human-use' and (day not in first or ts < first[day]):
            first[day] = ts
        elif row['kind'] == 'human-out' and day in first and ts > first[day]:
            if day not in out or ts < out[day]:
                out[day] = ts
    return {day: (int((ts - ts.replace(hour=0, minute=0, second=0, microsecond=0))
                      .total_seconds() // 60),
                  int((out[day] - ts).total_seconds() // 60) if day in out else None)
            for day, ts in first.items()}


def summarize(learned, days):
    """The kv payload behind one learned slice: the learned time plus the F/E
    counts the report's reason quotes."""
    used = sorted(f for f, _ in days if f is not None)
    exhausted = sorted(e for _, e in days if e is not None)
    return {'learned': learned,
            'F': int(statistics.median(used)) if used else None,
            'E': int(statistics.median(exhausted)) if exhausted else None,
            'n': len(used)}


def learn_slice(cfg, led, target, now, weekend):
    """One target's learned payload for the weekday or weekend slice."""
    per_day = collect_days(cfg, led, target, now - timedelta(days=LEARN_WINDOW_DAYS), now)
    days = [v for d, v in per_day.items() if (d.weekday() < 5) != weekend]
    return summarize(learn_time(days, weekend), days)


def relearn(ctx):
    """Daily relearn pass (first tick at/after 03:00 local, once per day):
    persists `warmup:learned:<target>:<weekday|weekend>` payloads. Report-only
    — nothing that schedules a nudge ever reads these entries."""
    if not relearn_due(ctx.led):
        return
    now = ctx.led.now()
    for target in ctx.cfg.get('warmup', {}).get('targets', []):
        if target.get('days') == 'off':
            continue
        for slice_name, weekend in (('weekday', False), ('weekend', True)):
            payload = learn_slice(ctx.cfg, ctx.led, target, now, weekend)
            payload['at'] = iso(now)
            if not ctx.dry_run:
                ctx.led.set_kv(f"warmup:learned:{target['name']}:{slice_name}",
                               json.dumps(payload))
    if not ctx.dry_run:
        ctx.led.set_kv('warmup:learned:at', now.astimezone().strftime('%Y-%m-%d'))


def relearn_due(led):
    local = led.now().astimezone()
    if local.hour < RELEARN_AFTER_HOUR:
        return False
    return led.get_kv('warmup:learned:at') != local.strftime('%Y-%m-%d')


def reason(learned, payload, target):
    """The plain-English reason a report line quotes."""
    start = target.get('start', '05:00')
    if learned == 'off':
        return 'no use seen — warm-up would be off'
    if learned is None:
        return (f"not enough data yet ({payload.get('n', 0)} of {LEARN_MIN_DAYS} "
                f"days with use) — keeping {start}")
    f, e, n = payload.get('F'), payload.get('E'), payload.get('n')
    text = f"usually starts ~{hhmm(f)}"
    if e is not None:
        text += f", runs out ~{e // 60}h{e % 60:02d}m in"
    return f"{text} ({n} days) — learned {hhmm(learned)}; schedule keeps {start}"


def learned_lines(cfg, led):
    """Per-target report lines, computed fresh (a CLI run): the learned time
    and its reason, or the clean fallback when there's too little data."""
    lines = []
    now = led.now()
    for target in cfg.get('warmup', {}).get('targets', []):
        if target.get('days') == 'off':
            continue
        per_day = collect_days(cfg, led, target, now - timedelta(days=LEARN_WINDOW_DAYS), now)
        for slice_name, weekend in (('weekdays', False), ('weekends', True)):
            days = [v for d, v in per_day.items() if (d.weekday() < 5) != weekend]
            learned = learn_time(days, weekend)
            lines.append(f"{target['name']} {slice_name}: "
                         f"{reason(learned, summarize(learned, days), target)}")
    return lines


def console_lines(cfg, led):
    """Per-target Capacity lines, from the persisted kv (the console rebuilds
    state per request, so it never rescans transcripts): each target's learned
    warm-up and reason, falling back cleanly where there's too little data."""
    lines = []
    for target in cfg.get('warmup', {}).get('targets', []):
        if target.get('days') == 'off':
            continue
        for slice_name, label in (('weekday', 'weekdays'), ('weekend', 'weekends')):
            raw = led.get_kv(f"warmup:learned:{target['name']}:{slice_name}")
            try:
                payload = json.loads(raw) if raw else None
            except ValueError:
                payload = None
            if payload and payload.get('learned') is not None:
                lines.append(f"{target['name']} {label}: learned warm-up "
                             f"{hhmm(payload['learned'])} — "
                             f"{reason(payload['learned'], payload, target)}")
            else:
                lines.append(f"{target['name']} {label}: no learned warm-up yet — "
                             f"{reason(None, payload or {}, target)}")
    return lines
