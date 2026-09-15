"""Console writes applied by the tick, including while paused (D27)."""

import json

from .. import config, redact


def answer(ctx, row, payload):
    project, number = row['project'], row['number']
    if project not in {p['name'] for p in config.enabled_projects(ctx.cfg)}:
        return 'skipped', 'the project is disabled'
    item = ctx.led.item(project, number)
    if item is None or item['state'] not in ('needs_you', 'failed'):
        return 'skipped', 'the item moved on'
    ctx.gh(project).comment(number, payload['text'], agent=False)
    ctx.led.event('answered', project, number, {'via': 'console'})
    return 'done', 'answer posted'


HANDLERS = {'answer': answer}


def _report(ctx, message):
    try:
        ctx.say(message)
    except Exception:
        pass


def drain(ctx):
    """Isolate every failure, including bookkeeping failures.

    Recheck pending inside a write transaction: a cancelled snapshot must never
    send, and Undo cannot report success during a send. The tick lock serializes
    drainers; the transaction also fences the separate HTTP server connection.
    """
    if ctx.dry_run:
        return
    try:
        rows = ctx.led.due_actions()
    except Exception as exc:
        _report(ctx, f'console outbox unavailable: {redact.redact(str(exc))[:300]}')
        return
    for row in rows:
        try:
            with ctx.led._tx():
                current = ctx.led.q1('SELECT * FROM console_actions WHERE id=?', (row['id'],))
                if current is None or current['status'] != 'pending':
                    continue
                try:
                    handler = HANDLERS.get(row['kind'])
                    if handler is None:
                        raise ValueError(f"unknown action kind: {row['kind']}")
                    status, result = handler(ctx, current, json.loads(current['payload']))
                    ctx.led.finish_action(row['id'], status, result)
                except Exception as exc:
                    result = redact.redact(str(exc))[:300]
                    ctx.led.finish_action(row['id'], 'failed', result)
                    ctx.led.event('console_action_failed', row['project'], row['number'],
                                  {'id': row['id'], 'error': result})
                    _report(ctx, f"console action {row['id']} failed: {result}")
        except Exception as exc:
            _report(ctx, f"console action {row['id']} could not be recorded: {redact.redact(str(exc))[:300]}")
