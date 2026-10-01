"""Durable, per-attempt diagnostics for terminal failures (mahler#612)."""

import json
import os
import re

from .gh import GHError
from .ledger import row_get
from .redact import redact


def log_tail(path):
    if not path:
        return ""
    try:
        with open(path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - 16384))
            text = stream.read().decode("utf-8", errors="replace")
        if size > 16384:
            text = text.partition("\n")[2]
        return "\n".join(text.splitlines()[-30:])
    except OSError:
        return ""


def failure_class(reason):
    text = (reason or "").lower()
    for words, kind in (("verify", "verify_failed"), ("ci", "ci_failed"),
                        ("review", "review_rejected"), ("closed without merging", "pr_closed"),
                        ("setup", "setup_failed"), ("hung", "hung"),
                        ("timeout", "timeout"), ("silent", "silent"),
                        ("exit", "crashed"), ("no status", "silent")):
        if re.search(r"\b" + re.escape(words) + r"(?:\b|_)", text):
            return kind
    return "unknown"


def report(ctx, project, number, reason=None, *, run=None, output=None, branch=None):
    """Persist the diagnostic before posting; failed deliveries retry on ticks."""
    if ctx.dry_run:
        return
    led = ctx.led
    item = led.item(project, number)
    run = dict(run or led.last_run(project, number) or {})
    attempt = item["attempts"]
    latest = led.last_run(project, number)
    identity = latest["id"] if latest else run.get("id", 0)
    key = (f"failed_comment:{project}#{number}:{attempt}:"
           f"{identity}:{row_get(item, 'setup_fails', 0)}")
    if led.get_kv(key) == "sent":
        return
    pending = led.get_kv(key)
    if pending:
        body = json.loads(pending)["body"]
    else:
        if not reason:
            event = led.q1("SELECT detail FROM events WHERE project=? AND number=? "
                           "AND kind='state' ORDER BY id DESC LIMIT 1", (project, number))
            reason = event["detail"] if event else run.get("stop_reason") or run.get("outcome")
        kind = failure_class(reason)
        if kind == "unknown":
            reason = run.get("stop_reason") or run.get("outcome") or reason
            kind = failure_class(reason)
        platform = run.get("platform") or "not recorded"
        conf = ctx.cfg.get("platforms", {}).get(platform, {})
        model = (run.get("model") or conf.get(f"{run.get('role', 'build')}_model")
                 or conf.get("model") or "not recorded")
        branch = branch or item["branch"] or run.get("branch")
        if output is None and kind == "ci_failed" and branch:
            try:
                _, output = ctx.gh(project).failed_run_log(branch, tail=30)
            except GHError as err:
                output = f"CI output unavailable: {err}"
        if output is None and kind == "review_rejected":
            review = json.loads(led.get_kv(f"review:{project}#{number}") or "{}")
            output = review.get("findings")
        if output is None and kind == "setup_failed" and run.get("log_path"):
            output = log_tail(os.path.join(os.path.dirname(run["log_path"]), "setup.log"))
        if output is None and kind == "verify_failed" and run.get("log_path"):
            output = log_tail(os.path.join(os.path.dirname(run["log_path"]), "verify.log"))
        if output is None:
            output = log_tail(run.get("log_path"))
        tail = "\n".join((output or "reason not recorded — output unavailable").splitlines()[-30:])[-16384:]
        body = (f"<!-- mahler:agent -->\n"
                f"<!-- mahler:failed attempt={attempt} run={run.get('id', 0)} class={kind} -->\n"
                f"**Run failed** — {platform} / {model}; attempt {attempt} of "
                f"{ctx.policy(project)['max_attempts']}.\n\n"
                f"Failure class: `{kind}`. {reason or 'reason not recorded'}\n\n"
                + (f"Consecutive setup failures: {item['setup_fails']}.\n\n"
                   if kind == "setup_failed" else "")
                +
                f"Resume from: {branch or 'none recorded (no saved branch or snapshot)'}.\n\n"
                "Comment `/mahler go` to retry once the cause is fixed.\n\n"
                "Last output (up to 30 lines):\n\n" +
                "\n".join("> " + line for line in tail.splitlines()))
        body = redact(body)
        led.set_kv(key, json.dumps({"body": body}))
    try:
        ctx.gh(project).comment(number, body)
    except GHError as err:
        ctx.say(f"{project}#{number}: couldn't post failure comment — {err}")
        return
    led.set_kv(key, "sent")


def backfill(ctx, projects):
    """Once per failed attempt, including historical items with no surviving run."""
    if ctx.dry_run:
        return
    for project in projects:
        name = project["name"]
        for item in ctx.led.items(name, states=("failed", "needs_you")):
            try:
                prefix = f"failed_comment:{name}#{item['number']}:"
                pending = ctx.led.q1(
                    "SELECT 1 FROM kv WHERE substr(key, 1, ?)=? "
                    "AND value IS NOT NULL AND value != 'sent' LIMIT 1",
                    (len(prefix), prefix))
                if (pending or item["state"] == "failed" or
                        item["attempts"] >= ctx.policy(name)["max_attempts"] or
                        row_get(item, "setup_fails", 0) >= 2):
                    report(ctx, name, item["number"])
            except Exception as err:
                ctx.say(f"{name}#{item['number']}: failure backfill failed — {err}")
