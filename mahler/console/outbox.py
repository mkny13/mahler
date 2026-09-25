"""Console writes applied by the tick, including while paused (D27)."""

import json

from .. import config, redact, watchdog
from ..gh import parse_command


def capture_title(text, limit=80):
    """The new issue's title: the capture's first line, cut at a word
    boundary to at most `limit` characters."""
    line = text.split("\n", 1)[0].strip()
    if len(line) <= limit:
        return line
    cut = line[:limit]
    sp = cut.rfind(" ")
    return cut[:sp] if sp > 0 else cut


def format_attachment(cfg, payload):
    filename = payload.get("attachment")
    if not filename:
        return ""
    public_url = cfg.get("serve", {}).get("public_url", "")
    local_path = f"~/.mahler/attachments/{filename}"
    if public_url:
        return f"\n\nAttachment: [{filename}]({public_url.rstrip('/')}/attachments/{filename}) (`{local_path}`)"
    return f"\n\nAttachment: `{local_path}`"


def capture(ctx, row, payload):
    project = row["project"]
    if project not in {p["name"] for p in config.enabled_projects(ctx.cfg)}:
        return "skipped", "the project is disabled"
    pol = config.project_policy(ctx.cfg, project)
    text = payload["text"]
    labels = ["type:feature", "p2", "mahler:inbox"]
    if pol.get("scope") == "label":
        labels.append(pol["scope_label"])
    
    body = f"{text}\n\n— captured from the Mahler console"
    att = format_attachment(ctx.cfg, payload)
    if att:
        body += att

    url = ctx.gh(project).create_issue(capture_title(text), body, labels)
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
    resumes = item['state'] == 'failed' and parse_command(payload['text']) is None
    if resumes:
        from ..sync import resume_item
        resume_item(ctx.led, project, number)
        result = 'answer posted — resuming'
    else:
        result = 'answer posted'
    ctx.led.event('answered', project, number, {'via': 'console',
                                                'resuming': resumes})
    return 'done', result


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


def uat_pending_row(ctx, project, number):
    """The UAT row the verdict applies to, or the reason to skip."""
    if project not in {p["name"] for p in config.enabled_projects(ctx.cfg)}:
        return None, "the project is disabled"
    r = ctx.led.uat(project, number)
    if r is None or r["verdict"]:
        return None, "the verdict is already recorded"
    return r, None


def uat_pass(ctx, row, payload):
    project, number = row["project"], row["number"]
    r, skip = uat_pending_row(ctx, project, number)
    if r is None:
        return "skipped", skip
    ctx.gh(project).comment(number, "✅ **UAT passed** (from the console).", agent=False)
    ctx.led.set_uat_verdict(project, number, "pass")
    ctx.led.event("uat_verdict", project, number, {"verdict": "pass", "via": "console"})
    return "done", "UAT passed"


def uat_fail(ctx, row, payload):
    project, number = row["project"], row["number"]
    r, skip = uat_pending_row(ctx, project, number)
    if r is None:
        return "skipped", skip
    pol = config.project_policy(ctx.cfg, project)
    labels = ["type:bug", "p1"]
    if pol.get("scope") == "label":
        labels.append(pol["scope_label"])
    note = payload.get("note") or ""
    lines = []
    if note:
        lines += ["> " + line for line in note.splitlines()] + [""]
    lines += [f"Found checking #{number} — PR #{r['pr'] or '?'}, "
              f"build {r['sha'] or 'unknown'}."]
    
    att = format_attachment(ctx.cfg, payload)
    if att:
        lines += [att]
        
    lines += ["", "## Needs a human to check", r["needs"] or ""]
    url = ctx.gh(project).create_issue(f"UAT failed: {r['title'] or f'{project}#{number}'}",
                                       "\n".join(lines), labels)
    try:
        bug = int(url.rstrip("/").rsplit("/", 1)[-1])
    except ValueError:
        raise ValueError(f"gh issue create: no issue number in {url[:200]!r}")
    ctx.gh(project).comment(number, f"❌ **UAT failed** — filed #{bug}.", agent=False)
    if not ctx.led.set_uat_verdict(project, number, "fail", bug=bug,
                                   note=note or None):
        return "skipped", "the verdict is already recorded"
    ctx.led.event("uat_verdict", project, number,
                  {"verdict": "fail", "bug": bug, "via": "console"})
    return "done", f"filed #{bug}"


def cut_release(ctx, row, payload):
    project = row["project"]
    if project not in {p["name"] for p in config.enabled_projects(ctx.cfg)}:
        return "skipped", "the project is disabled"
    version = payload["version"]
    checkpoint_sha = payload["checkpoint_sha"]
    notes = payload.get("notes")
    item_numbers = payload.get("item_numbers")

    from .. import releases
    res = releases.publish_release(
        led=ctx.led,
        gh=ctx.gh(project),
        project=project,
        version=version,
        checkpoint_sha=checkpoint_sha,
        notes=notes,
        item_numbers=item_numbers,
    )
    url = res.get("url", "")
    status = res.get("status", "published")
    ctx.led.event("release_published", project=project, detail={
        "version": version, "url": url, "status": status, "via": "console"
    })
    return "done", f"{status} {url}".strip()


HANDLERS = {'answer': answer, 'stop_run': stop_run, 'capture': capture, 'revert': revert,
            'uat_pass': uat_pass, 'uat_fail': uat_fail, 'cut_release': cut_release}


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
