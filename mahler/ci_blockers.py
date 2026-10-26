"""Durable, project-scoped dependencies reported by build and fix runs."""
import json
import re

from .ledger import iso

_REFERENCE = re.compile(
    r"\b(?:waiting for|blocked by|blocked on|depends on)\s+"
    r"(?:(?P<project>[\w./-]+)#|#)(?P<number>\d+)\b", re.IGNORECASE)
REPO_WIDE = re.compile(r"\b(?:all prs|every pr|repo-wide|broken on main)\b", re.IGNORECASE)


def references(led, project, number, reason, repo):
    """Only explicit dependencies on known local items; never guess PR numbers."""
    result = []
    for match in _REFERENCE.finditer(reason or ""):
        qualifier = match['project']
        n = int(match['number'])
        if qualifier and qualifier.casefold() not in {project.casefold(), repo.casefold()}:
            continue
        if n != number and n not in result and led.item(project, n) is not None:
            result.append(n)
    return result


def recorded(led, project, number):
    return json.loads(led.get_kv(f"blocked-deps:{project}#{number}") or "[]")


def pending(led, project, number):
    return [n for n in recorded(led, project, number)
            if not resolved(led, project, n)]


def resolved(led, project, number):
    item = led.item(project, number)
    return item is not None and item['state'] in ('shipped', 'done')


def active(led, project, *, clear=True):
    key = f"ci_blocker:{project}"
    record = json.loads(led.get_kv(key) or "null")
    if record and record.get('number') and resolved(led, project, record['number']):
        if clear:
            clear_hold(led, project, record)
        return None
    return record


def clear_hold(led, project, record):
    """End a hold; PR heads seen red under it must not re-trigger detection."""
    led.set_kv(f"ci_blocker:{project}", None)
    led.set_kv(f"ci_blocker_stale:{project}", json.dumps((record or {}).get('heads') or {}))


def record(ending, numbers):
    """Called inside finalization's fenced transaction, including its refund."""
    e = ending
    deps = json.loads(e.led.item(e.project, e.number)['depends'] or '[]')
    reported = recorded(e.led, e.project, e.number)
    for n in numbers:
        if n not in deps:
            deps.append(n)
        if n not in reported:
            reported.append(n)
    e.led.set_kv(f"blocked-deps:{e.project}#{e.number}", json.dumps(reported))
    if REPO_WIDE.search(e.rest):
        unresolved = [n for n in numbers if not resolved(e.led, e.project, n)]
        if unresolved:
            e.led.set_kv(f"ci_blocker:{e.project}", json.dumps({
                'number': unresolved[0], 'reason': e.rest, 'since': iso(e.led.now())}))
    return deps


def holds(blocker, number):
    """True when a project hold applies to item `number` (the blocker item is exempt)."""
    return bool(blocker) and (blocker.get('number') is None or blocker['number'] != number)


def label(blocker):
    return [blocker.get('number') or blocker.get('bot_pr') or blocker.get('check')]


def repo_wide_checks(red_by_pr, base_red):
    """Checks failing on >=2 open PRs, or on the base branch: facts, not prose."""
    counts = {}
    for names in red_by_pr.values():
        for name in set(names):
            counts[name] = counts.get(name, 0) + 1
    found = [n for n, c in sorted(counts.items()) if c >= 2]
    return found + [n for n in base_red if n not in found]
