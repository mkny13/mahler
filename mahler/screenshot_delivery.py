"""Privacy-gated delivery of captured screenshots to the PR and the reviewer (D11).

Nothing here may block review or merge: every failure is advisory and only fixed,
generic text leaves this module. Publication happens only when the project's
`screenshot_preview_non_personal` is exactly true (D4: privacy applies before any
upload, whatever the repository's visibility). Evidence lives on a dedicated
orphan branch, never on the PR branch, the base, releases or tags.
"""
import json
import re

from . import screenshots
from .gh import AGENT_NOTE, HELP_FOOTER, GHError

BRANCH = "mahler-screenshots"
MARKER = "<!-- mahler:screenshots -->"
MAX_ATTEMPTS = 3
OBSERVATION = ("Screenshots are observations of the preview, not proof of functional "
               "correctness.")


def _json(ctx, key):
    try:
        return json.loads(ctx.led.get_kv(key) or "{}")
    except ValueError:
        return {}


def _captured(ctx, project, pr, sha):
    """The capture result for exactly this head, or None."""
    result = _json(ctx, f"screenshot:{project}:{pr}:{sha}")
    return result if result.get("sha") == sha and result.get("state") else None


def opted_in(policy):
    return policy.get("screenshot_preview_non_personal") is True


def _body(sha, capture, opted, record):
    short = sha[:12]
    head = f"{AGENT_NOTE}\n{MARKER}\n**Screenshot evidence** for head `{short}`"
    if capture["state"] != "success":
        text = f"{head}: capture {capture['state']}; no screenshots to show."
    elif not opted:
        text = f"{head}: captured and kept local-only; not published."
    elif record.get("links"):
        lines = [f"{head}: captured.", ""]
        lines += [f"- `{x['route']}` — [screenshot]({x['url']})" for x in record["links"]]
        lines += ["", OBSERVATION]
        text = "\n".join(lines)
    else:
        text = f"{head}: captured; publication unavailable, kept local-only."
    return text + HELP_FOOTER


def _publish(ctx, project, pr, sha, gh, record):
    found = screenshots.lookup(project, pr, sha)
    if not found:
        return
    directory, manifest = found
    files = {"manifest.json": (directory / "manifest.json").read_bytes()}
    for entry in manifest["screenshots"]:
        files[entry["file"]] = (directory / entry["file"]).read_bytes()
    if gh.pr_view(pr).get("headRefOid") != sha:
        return
    prefix = f"pr-{pr}/{sha}"
    commit = gh.publish_artifacts(BRANCH, prefix, files,
                                  f"Screenshots for PR #{pr} head {sha[:12]}")
    record["commit"] = commit
    record["links"] = [{"route": e["route"], "url": gh.artifact_url(commit, f"{prefix}/{e['file']}")}
                       for e in manifest["screenshots"]]


def deliver(ctx, project, item, pr, view):
    """Publish (if allowed) and announce evidence for the current head, once."""
    if ctx.dry_run:
        return
    try:
        policy = ctx.policy(project)
        sha = view.get("headRefOid") or ""
        if not policy.get("screenshot") or not sha:
            return
        capture = _captured(ctx, project, pr, sha)
        if not capture:
            return
        key = f"screenshot-delivery:{project}:{pr}:{sha}"
        record = _json(ctx, key) or {"sha": sha, "pr": pr}
        if record.get("done") or record.get("attempts", 0) >= MAX_ATTEMPTS:
            return
        record["attempts"] = record.get("attempts", 0) + 1
        ctx.led.set_kv(key, json.dumps(record))
        gh = ctx.gh(project)
        opted = opted_in(policy)
        if opted and capture["state"] == "success" and not record.get("commit"):
            try:
                _publish(ctx, project, pr, sha, gh, record)
            except Exception:
                record["publish"] = "failed"
            ctx.led.set_kv(key, json.dumps(record))
        # Never label evidence current unless the PR still points at this head.
        if gh.pr_view(pr).get("headRefOid") != sha:
            return
        body = _body(sha, capture, opted, record)
        ckey = f"screenshot-comment:{project}:{pr}"
        comment = _json(ctx, ckey).get("id")
        if comment:
            try:
                gh.comment_edit(comment, body)
            except GHError as error:
                if not re.search(r"\bHTTP 404\b", str(error)):
                    raise
                # Invalidate before lookup so a failed recovery can retry cleanly.
                ctx.led.set_kv(ckey, "")
                comment = None
        if not comment:
            comment = gh.find_comment(pr, MARKER)
            if comment:
                gh.comment_edit(comment, body)
            else:
                comment = gh.comment_create(pr, body)
        ctx.led.set_kv(ckey, json.dumps({"id": comment}))
        record.update(done=True, comment=comment)
        ctx.led.set_kv(key, json.dumps(record))
    except Exception:
        # Advisory boundary: no exception, URL or route text is reported.
        try:
            ctx.say(f"{project} PR #{pr}: screenshot delivery unavailable")
        except Exception:
            pass


def review_context(ctx, project, pr, sha):
    """Reviewer-prompt text for this exact head; empty when nothing was captured."""
    try:
        policy = ctx.policy(project)
        capture = _captured(ctx, project, pr, sha) if policy.get("screenshot") and sha else None
        if not capture or capture["state"] != "success":
            return ""
        if not opted_in(policy):
            return ("Screenshot evidence for this head exists but is restricted and "
                    "local-only; it is not available to reviewers.\n")
        found = screenshots.lookup(project, pr, sha)
        if not found:
            return ""
        directory, manifest = found
        record = _json(ctx, f"screenshot-delivery:{project}:{pr}:{sha}")
        urls = {x["route"]: x["url"] for x in record.get("links", []) if record.get("sha") == sha}
        lines = [f"Screenshot evidence for head {sha[:12]} (current head only). "
                 f"{OBSERVATION} Judge the diff on its own merits.\n"]
        for entry in manifest["screenshots"]:
            line = f"- {entry['route']}: local image {directory / entry['file']}"
            if entry["route"] in urls:
                line += f" ; durable link {urls[entry['route']]}"
            lines.append(line + "\n")
        return "".join(lines)
    except Exception:
        return ""


def record_merge(ctx, project, number, pr, view):
    """Tie the final head's evidence to confirmed merge metadata for UAT lookup.

    The squash-merge SHA is stored beside, never in place of, the PR-head SHA.
    """
    try:
        sha = view.get("headRefOid") or ""
        if not sha or not _captured(ctx, project, pr, sha):
            return
        merge = (view.get("mergeCommit") or {}).get("oid")
        delivery = _json(ctx, f"screenshot-delivery:{project}:{pr}:{sha}")
        ctx.led.set_kv(f"screenshot-final:{project}#{number}", json.dumps({
            "pr": pr, "head": sha, "merge_sha": merge, "commit": delivery.get("commit"),
            "comment": delivery.get("comment")}))
    except Exception:
        pass
