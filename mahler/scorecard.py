"""On-demand attempt outcomes for D33; late evidence can change old scores."""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import re


def _time(value):
    if not value:
        return None
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _json(value, default):
    try:
        result = json.loads(value or "null")
    except (ValueError, TypeError):
        return default
    return result if isinstance(result, type(default)) else default


def _exclusion(run):
    outcome = run['outcome'] or ''
    if outcome.startswith('launch failed'):
        return 'launch failed'
    if outcome == 'setup failed' or run['stop_reason'] == 'setup-failed':
        return 'setup failed'
    if outcome == 'not claimed':
        return 'not claimed'
    if run['stop_reason'] in {'quota', 'preempted', 'closed', 'parked'}:
        return run['stop_reason']
    if outcome.split(' ', 1)[0] in {'BLOCKED', 'NEEDS-YOU'}:
        return outcome.split(' ', 1)[0]
    return None


def attempts(led, since, until=None):
    """Return ended runs in [since, until), selected by end time.

    Bounds accept ISO timestamps or datetimes (None means unbounded). Evidence
    always uses the full current ledger, including outside the reporting window.
    Legacy ended rows without an end time use their start time. Excluded builds
    do not consume the first model attempt; until one exists planning is pending.
    DONE is provisionally successful immediately, without a 14-day waiting period.
    """
    since, until = _time(since), _time(until)
    runs = [dict(r) for r in led.q('SELECT * FROM runs ORDER BY started_at, id')]
    items = {(r['project'], r['number']): dict(r) for r in led.q('SELECT * FROM items')}
    by_item, events, children = defaultdict(list), defaultdict(list), defaultdict(list)
    bugs = defaultdict(list)
    for r in runs:
        by_item[r['project'], r['number']].append(r)
    for key, item in items.items():
        if item['parent'] is not None:
            children[item['project'], item['parent']].append(key)
        if 'type:bug' in _json(item['labels'], []):
            bugs[item['project']].append(item)
    for row in led.q('SELECT * FROM events ORDER BY at, id'):
        event = dict(row)
        event['detail'] = _json(event['detail'], {})
        events[event['project'], event['number']].append(event)
    # Older console reverts predate revert_requested; completed outbox rows
    # retain their item and completion time. Merely queued/cancelled actions do
    # not establish a defect.
    for row in led.q("SELECT * FROM console_actions WHERE kind='revert' AND status='done'"):
        events[row['project'], row['number']].append({
            'kind': 'revert_requested', 'at': row['done_at'] or row['created_at'],
            'detail': {}})
    uat = {(r['project'], r['number']): dict(r) for r in led.q('SELECT * FROM uat')}
    releases = {(r['project'], r['number']): dict(r)
                for r in led.q('SELECT * FROM release_items')}

    def later(run, other):
        return (_time(other['started_at']), other['id']) > (
            _time(run['ended_at'] or run['started_at']), run['id'])

    def defect(run, review=False):
        key = run['project'], run['number']
        end = _time(run['ended_at'] or run['started_at'])
        history = [e for e in events[key] if _time(e['at']) >= end]
        if not review:
            # Needing a fix is adverse evidence even if that fix's own attempt
            # is excluded (for example, it later stops for quota).
            if any(r['role'] == 'fix' and later(run, r)
                   for r in by_item[key]):
                return 'later fix run'
            if (any(e['kind'] == 'review_verdict' and e['detail'].get('verdict') == 'fail'
                    for e in history)
                    or any(r['role'] == 'review' and r['outcome'] == 'REVIEW-FAIL'
                           and later(run, r) for r in by_item[key])):
                return 'failed review'
        if any(e['kind'] == 'revert_requested' for e in history):
            return 'revert'
        check = uat.get(key, {})
        if (not review and check.get('verdict') == 'fail'
                and _time(check.get('verdict_at')) and _time(check['verdict_at']) >= end):
            return 'UAT fail'
        # Shipping events cover items without UAT; release snapshots cover sync's
        # externally merged path. Do not infer a merge from mutable item state.
        merges = [_time(e['at']) for e in history
                  if e['kind'] == 'shipped' and e['detail'].get('merged') is not False]
        for source in (check, releases.get(key, {})):
            stamp = _time(source.get('shipped_at'))
            if stamp and stamp >= end:
                merges.append(stamp)
        if not merges:
            return None
        merged = min(merges)
        refs = {run['number'], items.get(key, {}).get('pr'), check.get('pr'),
                releases.get(key, {}).get('pr')}
        refs.update(e['detail'].get('pr') for e in history if e['kind'] == 'shipped')
        pattern = re.compile(r'(?<![\w/#])#(?:' + '|'.join(str(n) for n in refs if n is not None)
                             + r')(?!\w)')
        candidates = list(bugs[run['project']])
        linked = items.get((run['project'], check.get('bug')))
        if linked and linked not in candidates:
            candidates.append(linked)
        for bug in candidates:
            created = _time(bug['created_at'])
            if (created and merged <= created <= merged + timedelta(days=14)
                    and (bug['number'] == check.get('bug')
                         or pattern.search(bug['issue_body'] or ''))):
                return 'bug within 14 days'
        return None

    def first_build(key):
        for r in by_item[key]:
            if r['role'] == 'build' and not _exclusion(r):
                return classify(r)
        return 'pending', 'awaiting first build'

    def split_again(key, run=None):
        return bool(children[key]) or any(
            r['outcome'] == 'SPLIT' and (run is None or later(run, r))
            for r in by_item[key])

    def classify(run):
        excluded = _exclusion(run)
        if excluded:
            return 'excluded', excluded
        if run['status'] != 'ended':
            return 'pending', 'build running'
        if run['exit_code'] not in (None, 0):
            return 'failure', 'nonzero exit'
        outcome = run['outcome'] or ''
        key = run['project'], run['number']
        if run['role'] in {'build', 'fix'} and outcome == 'DONE':
            why = defect(run)
            return ('failure', why) if why else ('success', 'DONE with no adverse evidence')
        if run['role'] == 'review' and outcome in {'REVIEW-PASS', 'REVIEW-FAIL'}:
            why = defect(run, review=True)
            return ('failure', why) if why else ('success', 'review with no missed defect')
        if run['role'] in {'sort', 'plan'}:
            if outcome == 'READY':
                if split_again(key, run):
                    return 'failure', 'item split again'
                if items.get(key, {}).get('esc_tier', 0) > 0:
                    return 'failure', 'item escalated'
                return first_build(key)
            if outcome == 'SPLIT':
                subs = children[key]
                if any(split_again(sub) for sub in subs):
                    return 'failure', 'child split again'
                results = [first_build(sub)[0] for sub in subs]
                if not results or 'pending' in results:
                    return 'pending', 'awaiting child builds'
                ok = results.count('success') * 4 >= len(results) * 3
                if ok:
                    return 'success', 'child first-build success at least 75%'
                return 'failure', 'child first-build success below 75%'
        return 'failure', outcome or 'no status line'

    result = []
    for run in runs:
        end = _time(run['ended_at'] or run['started_at'])
        if run['status'] != 'ended' or (since and end < since) or (until and end >= until):
            continue
        outcome, why = classify(run)
        result.append(dict(run=run['id'], **{k: run[k] for k in (
            'project', 'number', 'role', 'size', 'platform', 'model', 'effort')},
            result=outcome, why=why))
    return result
