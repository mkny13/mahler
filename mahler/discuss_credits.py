"""D43 work discussions: scoped authorization for the qualified D41 gateway.

No keychain access and no provider credentials. Session/turn IDs live in token
bindings and reservation attempt keys, never in manufactured build runs.
"""
from contextlib import contextmanager
from pathlib import Path
import time

from . import api_credits, config, platforms


def platform_for(cfg, account):
    from .discuss import TurnError
    candidates = [(name, p) for name, p in cfg.get('platforms', {}).items()
                  if p.get('kind') == 'agent-sdk' and p.get('enabled', True)
                  and config.account_of(p) == account and p.get('credit_pool')
                  and p['credit_pool']['account'] == account]
    if account == config.DEFAULT_ACCOUNT or len(candidates) != 1:
        raise TurnError('quota_unavailable',
                        'Configure exactly one enabled work Agent SDK credits platform for discussions.')
    return candidates[0]


def check_capacity(led, name, pconf):
    from .discuss import TurnError
    snap = api_credits.snapshot(led, name, pconf)
    if snap['state'] not in ('ok', 'soft') or not snap['org_fresh']:
        raise TurnError('quota_unavailable',
                        'Work credits unavailable. Check gateway keys, confirmed grant, balance and fresh usage; then retry.')


@contextmanager
def authorization(cfg, led, session, turn, worker_token):
    from .discuss import TurnError
    name, pconf = platform_for(cfg, session['account'])
    check_capacity(led, name, pconf)
    executable = platforms.sdk_run_exe()
    if not executable:
        raise TurnError('quota_unavailable', 'Install the discussion-capable mahler-sdk-run from sdk_runner; then retry.')
    pool = pconf['credit_pool']
    path, _ = api_credits._gateway_files(pool['group'])
    state = api_credits._read_state(path)
    # Only attach to the qualified, independently supervised gateway. The worker
    # never starts a credential consumer with a possibly different config/database.
    def healthy():
        return (state and api_credits._read_state(path) == state
                and api_credits.gateway_healthy(pool['gateway_host'], state['port'], pool['group'], timeout=.5))
    if not healthy():
        raise TurnError('quota_unavailable',
                        'Start or restart the configured mahler gateway for work credits; then retry.')
    tokens = api_credits._TokenStore(led, pool['group'])
    local_token = tokens.register_discussion(session['id'], turn['id'], worker_token, session['account'])
    try:
        env = config.discussion_env()
        # An empty per-session HOME prevents any subscription/profile fallback.
        home = Path(led.path).resolve().parent / 'discussion-sessions' / session['id'] / 'home'
        config.ensure_private_dir(home)
        env.update(HOME=str(home), ANTHROPIC_API_KEY=local_token,
                   ANTHROPIC_BASE_URL=f"http://{pool['gateway_host'] if ':' not in pool['gateway_host'] else '[' + pool['gateway_host'] + ']'}:{state['port']}")
        last_check = 0.0

        def heartbeat(force=False):
            nonlocal last_check
            led.heartbeat_chat_turn(turn['id'], worker_token)
            if force or time.monotonic() - last_check >= 1:
                if not healthy():
                    raise TurnError('engine_error', 'Work gateway stopped or restarted. Retry this turn explicitly.')
                check_capacity(led, name, pconf)
                last_check = time.monotonic()

        yield executable, pconf, env, heartbeat
    finally:
        tokens.revoke_discussion(local_token)


def launch_args(executable, pconf, mcp, directory, engine_id):
    import json
    argv = [executable, '--prompt-file', '-', '--cwd', directory,
            '--model', pconf['model'], '--max-output-tokens', str(pconf['credit_pool']['max_output_tokens']),
            '--discussion-mcp', json.dumps(mcp), '--discussion-system',
            str(Path(config.REPO_ROOT) / 'recipes/discuss.md')]
    if engine_id:
        argv += ['--resume', engine_id]
    return argv
