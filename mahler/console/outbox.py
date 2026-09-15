"""Console writes applied by the tick, including while paused (D27)."""

import json

from .. import config, redact, watchdog


def capture_title(text, limit=80):
    """The new issue's title: the capture's first line, cut at a word
    boundary to at most `limit` characters."""
    line = text.split("\n", 1)[0].strip()
    if len(line) <= limit:
        return line
    cut = line[:limit]
    sp = cut.rfind(" ")
    return cut[:sp] if sp > 0 else cut


def capture(ctx, row, payload):
    project = row["project"]
    if project not in {p["name"] for p in config.enabled_projects(ctx.cfg)}:
        return "skipped", "the project is disabled"
    pol = config.project_policy(ctx.cfg, project)
    text = payload["text"]
    labels = ["type:feature", "p2", "mahler:inbox"]
    if pol.get("scope") == "label":
        labels.append(pol["scope_label"])
    url = ctx.gh(project).create_issue(capture_title(text),
                                       f"{text}\n\n— captured from the Mahler console", labels)
    try:
        number = int(url.rstrip("/").rsplit("/", 1)[-1])
    except ValueError:
        raise ValueError(f"gh issue create: no issue number in {url[:200]!r}")
    ctx.led.event("captured", project, number, {"via": "console"})
    return "done", str(number)


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


def stop_run(ctx, row, payload):
    run = ctx.led.run(payload['run'])
    if run is None or run['status'] not in ('running', 'stopping'):
        return 'skipped', 'the run already ended'
    watchdog.request_stop(ctx, run, 'handoff')
    return 'done', 'stop requested'


def revert(ctx, row, payload):
    from .revert import revert as prepare
    if row["project"] not in {p["name"] for p in config.enabled_projects(ctx.cfg)}:
        return "skipped", "the project is disabled"
    return prepare(ctx, row["project"], row["number"], payload["pr"])


HANDLERS = {'answer': answer, 'stop_run': stop_run, 'capture': capture, 'revert': revert}


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
