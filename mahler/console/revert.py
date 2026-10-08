"""Prepare a console revert; the conductor owns its push, PR and CI (D27)."""

import json
import os
import re
import tempfile

from .. import config, runner
from ..gh import GHError


def revert(ctx, project, number, pr=None, *, source="console", recover=False):
    gh, led = ctx.gh(project), ctx.led
    pol = config.project_policy(ctx.cfg, project)
    if pr is None:
        from ..ledger import row_get
        uat = led.uat(project, number)
        item = led.item(project, number)
        pr = (row_get(uat, "pr") if uat else None) or (row_get(item, "pr") if item else None)
        if not pr and hasattr(gh, "pr_for_commit"):
            sha_cand = (row_get(uat, "sha") if uat else None) or (row_get(item, "sha") if item else None)
            if sha_cand:
                pr = gh.pr_for_commit(sha_cand)
    if not pr:
        raise ValueError("the pull request has no confirmed merge commit")
    info = gh.pr_merge_info(pr)
    sha = (info.get("mergeCommit") or {}).get("oid", "")
    if info.get("state") != "MERGED" or not re.fullmatch(r"[0-9a-f]{40,64}", sha):
        raise ValueError("the pull request has no confirmed merge commit")
    base = info["baseRefName"]
    if base != pol.get("base", "main"):
        raise ValueError("the merged change is not on the project's shipping base")
    key = f"revert:{project}:{pr}"
    marker = f"<!-- mahler:revert pr={pr} sha={sha} -->"
    new = None
    if led.get_kv(key):
        if not recover:
            raise ValueError("a revert issue already exists")
        new = int(led.get_kv(key))
    elif recover:
        try:
            existing = gh.issue_by_marker(marker)
            if not existing:
                existing = gh.issue_by_marker(f"Reverts PR #{pr} ({sha})")
        except Exception:
            existing = None
        if existing:
            new = int(str(existing).rstrip("/").rsplit("/", 1)[-1])
            led.set_kv(key, str(new))

    title = f"Revert: {info['title']}"
    labels = ["type:bug", "p1"]
    if pol.get("scope") == "label":
        labels.append(pol["scope_label"])

    if new is None:
        if source == "console":
            body = (f"Reverts PR #{pr} ({sha}), which shipped #{number}. "
                    "Asked for from the console.")
        else:
            body = (f"<!-- mahler:agent -->\n{marker}\n"
                    f"Reverts PR #{pr} ({sha}), which shipped #{number}. "
                    "Asked for automatically after post-merge failure.")
        url = gh.create_issue(title, body, labels)
        new = int(str(url).rstrip("/").rsplit("/", 1)[-1])
        # Remember the issue even if git or a later comment fails. Never file it twice.
        led.set_kv(key, str(new))

    detail = {"pr": pr, "revert_issue": new, "sha": sha}
    if source != "console":
        detail["via"] = source
    led.event("revert_requested", project, number, detail)
    led.upsert_item(project, new, title=title, priority=1, labels=json.dumps(labels))

    from ..ledger import row_get
    repo, branch = pol["path"], f"mahler/revert-{pr}"
    item = led.item(project, new)
    if item and row_get(item, "state") == "verifying" and row_get(item, "branch") == branch:
        return "done", str(new)

    root = os.path.join(runner.worktree_root(pol), project)
    os.makedirs(root, exist_ok=True)
    conflict = False
    with tempfile.TemporaryDirectory(prefix="revert-", dir=root) as parent:
        wt = os.path.join(parent, "tree")
        try:
            gh._git(repo, "fetch", "--quiet", "origin", f"refs/heads/{base}")
            gh._git(repo, "worktree", "add", "-b", branch, wt, "FETCH_HEAD")
            gh._git(wt, "merge-base", "--is-ancestor", sha, "HEAD")
            try:
                gh._git(wt, "revert", "--no-edit", sha)
            except GHError:
                # Only a real content conflict goes to an agent. Other git
                # errors remain failed outbox results, with the issue preserved.
                conflict = bool(gh._git(wt, "diff", "--name-only", "--diff-filter=U"))
                if not conflict:
                    raise
                gh._git(wt, "revert", "--abort")
        finally:
            if os.path.exists(wt):
                gh._git(repo, "worktree", "remove", "--force", wt)
    if conflict:
        gh.comment(new, f"Reverting PR #{pr} ({sha}) conflicts with current {base}. "
                       "Sort and build this issue to resolve the revert on the current base.")
    else:
        led.upsert_item(project, new, state="verifying", branch=branch,
                        summary=f"Revert PR #{pr}")
    comment_text = (f"↩︎ Revert requested from the console — #{new}."
                    if source == "console"
                    else f"↩︎ Revert requested automatically — #{new}.")
    already_commented = False
    if recover:
        try:
            comments = gh.issue_comments(number)
            already_commented = any(f"#{new}" in ((c.get("body") or "") if isinstance(c, dict) else str(c))
                                    for c in comments)
        except Exception:
            already_commented = False
    if not already_commented:
        gh.comment(number, comment_text)
    return "done", str(new)
