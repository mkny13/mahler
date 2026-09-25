"""Daily janitor (mahler#7, DESIGN D12): prune stale worktrees and old refs.

Every run normally cleans up after itself — finalize.py removes its
worktree — but a crash, a hung daemon or a deliberately kept worktree leaves
things behind. Once a day the tick sweeps, per project:

  * worktrees named `<item>-run<id>` under *this project's* worktree root
    whose run has ended at least `worktree_grace_hours` ago — the grace
    protects kept worktrees (needs-you handoffs) a human may still be in;
  * `git worktree prune`, so stale admin entries go too;
  * remote `mahler/snapshot/*` and `mahler/abandoned/*` branches older than
    `retention_days` (D12) whose item is `done` in the ledger;
  * up to 100 remote fix branches ending in `-r<run_id>` whose run ended
    and whose commits are contained in main or the item's current PR head.

Only these Mahler worktrees and branch patterns are touched. Fix branches
with unique work survive and are reported. Like the digest (mahler#6) the sweep
is gated by a kv key (`last_janitor_date`) so it runs once a day; `mahler tick --dry-run`
reports what it would do without deleting anything.
"""

import os
import re
from datetime import datetime, timedelta

from . import config, runner
from .ledger import parse

KV_KEY = "last_janitor_date"
WT_RE = re.compile(r"(\d+)-run(\d+)$")
SNAP = "mahler/snapshot/"
ABAN = "mahler/abandoned/"
FIX_RE = re.compile(r"mahler/(\d+)-[^/]+-r(\d+)$")
MAX_FIX_BRANCH_DELETIONS = 100
# Console mahler/revert-* refs are deliberately outside the deletion prefixes.
# They retain the prepared revert for at least 14 days, including after shipping.
RETENTION_DAYS = 14
WORKTREE_GRACE_HOURS = 24


def _local_now():
    """The tick's clock is UTC; the daily gate fires on the *local* day."""
    return datetime.now().astimezone()


def maybe_run(ctx):
    """Tick hook. Never raises — `mahler tick` must not break over a sweep."""
    try:
        _maybe_run(ctx)
    except Exception as e:                       # noqa: BLE001 — any janitor failure
        ctx.say(f"janitor failed (continuing) — {e}")


def _maybe_run(ctx):
    led = ctx.led
    today = _local_now().date().isoformat()
    if not ctx.dry_run and led.get_kv(KV_KEY) == today:
        return                                   # already swept today
    for pol in config.enabled_projects(ctx.cfg):
        if not os.path.isdir(pol["path"]):       # same reachability rule as the tick
            continue
        try:
            sweep(ctx, pol)
        except Exception as e:                   # noqa: BLE001 — keep other projects going
            ctx.say(f"janitor: {pol['name']} sweep failed (continuing) — {e}")
    if not ctx.dry_run:
        led.set_kv(KV_KEY, today)


# ---------- the sweep (unit-tested; dry_run only *reports*) ----------

def sweep(ctx, pol):
    """One project's sweep. Returns True if it did (or would do) anything."""
    repo, acts = pol["path"], []
    # git hosting credentials are the project's own identity (D25/D26), not
    # this machine's default — a work project's private repo otherwise gets
    # fetched/pruned with the wrong account's git credential helper (mahler#296).
    env = config.run_env(ctx.cfg, config.gh_account_of(pol))
    for wt, branch in _stale_worktrees(ctx, pol):
        acts.append(("worktree", wt, branch))
    try:
        runner.git(repo, "fetch", "--quiet", "--prune", "origin", env=env)
    except runner.GitError as e:
        _say(ctx, f"skipping branch sweep, fetch failed — {e}")
    else:
        for name in _stale_branches(ctx, pol, repo, env):
            acts.append(("branch", name))
        for name, tip, target in _stale_fix_branches(ctx, pol, repo, env):
            acts.append(("fix_branch", name, tip, target))
    for act in acts:
        if act[0] == "worktree":
            _say(ctx, f"would remove worktree {act[1]}"
                      + (f" (branch {act[2]})" if act[2] else ""))
        else:
            _say(ctx, f"would delete remote branch {act[1]}")
    if acts and not ctx.dry_run:
        for act in acts:
            if act[0] == "worktree":
                runner.remove_worktree(repo, act[1], act[2], runner.worktree_root(pol))
            elif act[0] == "fix_branch":
                try:
                    _delete_remote_branch(repo, act[1], env, tip=act[2], target=act[3])
                except runner.GitError as e:
                    _say(ctx, f"kept remote branch {act[1]} — deletion failed: {e}")
            else:
                _delete_remote_branch(repo, act[1], env)
    runner.git(repo, "worktree", "prune", check=False)   # stale entries go either way
    return bool(acts)


def _stale_worktrees(ctx, pol):
    """Worktrees of ended runs, past the grace period, under this project's
    worktree root only. -> [(path, branch or None)]."""
    led, now = ctx.led, ctx.led.now()
    grace = timedelta(hours=_cfg(pol, "worktree_grace_hours", WORKTREE_GRACE_HOURS))
    root = os.path.abspath(runner.worktree_root(pol))
    pdir = os.path.join(root, pol["name"])
    if not os.path.isdir(pdir):
        return []
    busy = {os.path.normpath(r["worktree"]) for r in led.active_runs(pol["name"])
            if r["worktree"]}
    out = []
    for entry in sorted(os.listdir(pdir)):
        m = WT_RE.search(entry)
        if not m:
            continue
        wt = os.path.normpath(os.path.join(pdir, entry))
        if os.path.commonpath((root, os.path.abspath(wt))) != root:
            continue                             # never cross the configured root
        run = led.run(int(m.group(2)))
        if (run is None or run["status"] != "ended" or run["project"] != pol["name"]
                or wt in busy):
            continue                             # not ours, not over, or still live
        launch_failed_without_path = (
            not run["worktree"] and (run["outcome"] or "").startswith("launch failed")
        )
        if not launch_failed_without_path and (
                not run["worktree"] or os.path.normpath(run["worktree"]) != wt):
            continue                             # not ours, or not a launch leak
        ended = parse(run["ended_at"])
        if not ended or now - ended < grace:
            continue
        if launch_failed_without_path:
            branch = run["branch"]
            branch = branch if branch and branch.endswith(f"-r{run['id']}") else None
        else:
            branch = run["branch"]
        out.append((wt, branch))
    return out


def _stale_branches(ctx, pol, repo, env=None):
    """Remote snapshot/abandoned branches past the D12 retention whose item is
    closed, after a successful fetch. -> [branch names]."""
    led = ctx.led
    cutoff = led.now() - timedelta(days=_cfg(pol, "retention_days", RETENTION_DAYS))
    raw = runner.git(repo, "for-each-ref", f"refs/remotes/origin/{SNAP}",
                     f"refs/remotes/origin/{ABAN}",
                     "--format=%(refname) %(committerdate:iso-strict)", check=False)
    out = []
    for line in raw.splitlines():
        ref, _, when = line.strip().partition(" ")
        branch = ref[len("refs/remotes/origin/"):]
        if not (branch.startswith(SNAP) or branch.startswith(ABAN)):
            continue
        try:
            tip = parse(when)
        except ValueError:
            continue
        if tip is None or tip > cutoff:
            continue
        seg = branch[len(SNAP if branch.startswith(SNAP) else ABAN):].split("/")[0]
        m = re.match(r"(\d+)", seg)
        if not m:
            continue
        item = led.item(pol["name"], int(m.group(1)))
        if item is None or item["state"] != "done":
            continue                             # closed only on the ledger's word
        out.append(branch)
    return out


def _stale_fix_branches(ctx, pol, repo, env=None):
    """After a successful fetch, find ended-run tips preserved by another ref.

    Unknown runs and mismatched projects/items fail closed. Cache each item's
    PR lookup so an outage's hundreds of leftover branches need only one fetch.
    """
    raw = runner.git(repo, "for-each-ref", "refs/remotes/origin/mahler/",
                     "--format=%(refname) %(objectname)")
    targets, out = {}, []
    for line in raw.splitlines():
        ref, tip = line.split()
        branch = ref.removeprefix("refs/remotes/origin/")
        match = FIX_RE.fullmatch(branch)
        if not match:
            continue
        number, run_id = map(int, match.groups())
        run = ctx.led.run(run_id)
        if (run is None or run["status"] != "ended"
                or run["project"] != pol["name"] or run["number"] != number):
            continue
        item = ctx.led.item(pol["name"], number)
        if item and item["branch"] == branch:
            continue                    # never delete the item's current branch
        if number not in targets:
            heads = ["refs/remotes/origin/main"]
            if item and item["pr"]:
                try:
                    runner.git(repo, "fetch", "--quiet", "origin",
                               f"refs/pull/{int(item['pr'])}/head", env=env)
                    heads.append(runner.git(repo, "rev-parse", "FETCH_HEAD^{commit}"))
                except runner.GitError as e:
                    _say(ctx, f"could not fetch PR head for #{number} — {e}")
            elif item and item["branch"]:
                heads.append(f"refs/remotes/origin/{item['branch']}")
            targets[number] = heads
        target = next((head for head in targets[number]
                       if _contained(repo, tip, head)), None)
        if target is None:
            _say(ctx, f"kept remote branch {branch} — unique commits or ancestry unproven")
            continue
        out.append((branch, tip, target))
        if len(out) >= MAX_FIX_BRANCH_DELETIONS:
            _say(ctx, f"fix branch deletion cap reached ({MAX_FIX_BRANCH_DELETIONS}); "
                      "remaining branches deferred to a later sweep")
            break
    return out


def _contained(repo, tip, target):
    try:
        runner.git(repo, "merge-base", "--is-ancestor", tip, target)
        return True
    except runner.GitError:
        return False


def _delete_remote_branch(repo, branch, env=None, *, tip=None, target=None):
    """Only known patterns; fix refs require ancestry proof and a tip lease."""
    if FIX_RE.fullmatch(branch):
        if not tip or not target or not _contained(repo, tip, target):
            raise RuntimeError(f"janitor refused unproven fix branch {branch!r}")
        # Reject deletion if anyone pushed new work since enumeration.
        runner.git(repo, "push", "--quiet", f"--force-with-lease=refs/heads/{branch}:{tip}",
                   "origin", "--delete", f"refs/heads/{branch}", env=env)
        return
    if not (branch.startswith(SNAP) or branch.startswith(ABAN)):
        raise RuntimeError(f"janitor refused to delete {branch!r}")
    runner.git(repo, "push", "--quiet", "origin", "--delete", f"refs/heads/{branch}", env=env)


def _cfg(pol, key, default):
    return (pol.get("janitor") or {}).get(key, default)


def _say(ctx, what):
    if ctx.dry_run:
        ctx.say(f"janitor (dry run): {what}")
    elif what.startswith("would "):
        ctx.say(f"janitor: {what[len('would '):]}")
    else:
        ctx.say(f"janitor: {what}")
