"""Prepare a console revert; the conductor owns its push, PR and CI (D27)."""

import json
import os
import re
import tempfile

from .. import config, runner
from ..gh import GHError


def revert(ctx, project, number, pr):
    gh, led = ctx.gh(project), ctx.led
    pol = config.project_policy(ctx.cfg, project)
    info = gh.pr_merge_info(pr)
    sha = (info.get("mergeCommit") or {}).get("oid", "")
    if info.get("state") != "MERGED" or not re.fullmatch(r"[0-9a-f]{40,64}", sha):
        raise ValueError("the pull request has no confirmed merge commit")
    base = info["baseRefName"]
    if base != pol.get("base", "main"):
        raise ValueError("the merged change is not on the project's shipping base")
    key = f"revert:{project}:{pr}"
    if led.get_kv(key):
        raise ValueError("a revert issue already exists")
    title = f"Revert: {info['title']}"
    labels = ["type:bug", "p1"]
    if pol.get("scope") == "label":
        labels.append(pol["scope_label"])
    url = gh.create_issue(title, f"Reverts PR #{pr} ({sha}), which shipped #{number}. "
                          "Asked for from the console.", labels)
    new = int(url.rstrip("/").rsplit("/", 1)[-1])
    # Remember the issue even if git or a later comment fails. Never file it twice.
    led.set_kv(key, str(new))
    led.event("revert_requested", project, number,
              {"pr": pr, "revert_issue": new, "sha": sha})
    led.upsert_item(project, new, title=title, priority=1, labels=json.dumps(labels))
    repo, branch = pol["path"], f"mahler/revert-{pr}"
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
    gh.comment(number, f"↩︎ Revert requested from the console — #{new}.")
    return "done", str(new)
