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
