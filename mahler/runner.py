"""Runs: one agent process, on one platform, for one item, in its own worktree.

Every run ends in a handoff (DESIGN D9): whatever it left behind is saved to a
pushed ref without touching anyone's working tree, and the next run — on any
platform — starts from there.
"""

import os
import re
import shlex
import shutil
import signal
import subprocess

from . import config, platforms, redact

HOOK_NAMES = ("applypatch-msg", "commit-msg", "post-checkout", "post-commit", "post-merge",
              "post-rewrite", "pre-applypatch", "pre-commit", "pre-merge-commit",
              "prepare-commit-msg", "pre-push", "pre-rebase")


class GitError(RuntimeError):
    pass


def git(repo, *args, env=None, check=True):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                       env=env, timeout=300)
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])}: "
                       f"{redact.redact((r.stderr or r.stdout).strip()[:400])}")
    return r.stdout.strip()


def slug(title, n=40):
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return s[:n].rstrip("-") or "item"


def remote_has(repo, ref):
    return subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "--quiet",
                           f"refs/remotes/origin/{ref}"], capture_output=True).returncode == 0


def start_ref(repo, base, handoff_branch, run_branch):
    """Where a build starts: the freshest saved work for this item, else base."""
    options = [b for b in (run_branch, handoff_branch) if b and remote_has(repo, b)]
    if not options:
        return f"origin/{base}"
    if len(options) == 2 and options[0] != options[1]:
        a, b = (f"origin/{o}" for o in options)
        # prefer whichever contains the other; if they diverged, the named branch wins
        if subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", a, b]).returncode == 0:
            return b
    return f"origin/{options[0]}"


def catch_up(wt, branch, base, number, run_id):
    """Replay the saved work checked out in `wt` onto the current base, and make
    the remote branch match (DESIGN D19). When it no longer applies cleanly,
    keep the old tip on a snapshot ref and start the branch from base.
    -> None if rebased, else the ref the old work was kept on."""
    onto = f"origin/{base}"
    kept = None
    try:
        git(wt, "rebase", "--quiet", "--no-verify", onto)
    except GitError:
        git(wt, "rebase", "--abort", check=False)
        kept = f"mahler/snapshot/{number}-stale-run{run_id}"
        git(wt, "push", "--quiet", "--no-verify", "--force", "origin",
            f"HEAD:refs/heads/{kept}")
        git(wt, "reset", "--quiet", "--hard", onto)
    # The remote branch must not keep the stale history, or the agent's first push
    # is rejected and a weak model "fixes" that by pulling it back in. But a branch
    # sitting at base's tip reads as merged on an open PR, so drop it instead.
    if int(git(wt, "rev-list", "--count", f"{onto}..HEAD") or 0):
        git(wt, "push", "--quiet", "--no-verify", "--force", "origin",
            f"HEAD:refs/heads/{branch}")
    else:
        git(wt, "push", "--quiet", "--no-verify", "origin", "--delete", branch, check=False)
    return kept


def fence_hooks(repo, run_dir):
    """A per-run hooks dir: pre-push checks the lease epoch (DESIGN D6), and every
    hook the repo already has is chained so its own checks still run."""
    hooks = os.path.join(run_dir, "hooks")
    os.makedirs(hooks, exist_ok=True)
    configured = git(repo, "config", "--get", "core.hooksPath", check=False)
    if configured:
        orig = configured if os.path.isabs(configured) else os.path.join(repo, configured)
    else:
        orig = os.path.join(git(repo, "rev-parse", "--path-format=absolute",
                                "--git-common-dir"), "hooks")
    for name in HOOK_NAMES:
        body = "#!/bin/sh\n"
        if name == "pre-push":
            body += (f'"{config.MAHLER_BIN}" lease-check || {{ echo "mahler: this run no longer holds '
                     f'the lease on #$MAHLER_ISSUE — push refused" >&2; exit 1; }}\n')
        body += f'[ -x "{orig}/{name}" ] && exec "{orig}/{name}" "$@"\nexit 0\n'
        path = os.path.join(hooks, name)
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, 0o755)
    return hooks


def check_account(ctx, project, platform):
    """Fail closed: a run must spend an account the project declares (D25).
    D26: 'declares' is membership — any of the project's declared accounts."""
    pol = ctx.policy(project)
    account = config.account_of(ctx.cfg["platforms"][platform])
    accounts = config.accounts_of(pol)
    if account not in accounts:      # the router never does this (D25)
        raise RuntimeError(f"{platform} spends the {account} account; "
                           f"{project} is on {', '.join(accounts)}")


def prepare(ctx, project, item, role, platform, run_id):
    """Everything a run needs before its CLI starts: a private run dir, the
    worktree on the right ref, the project's linked files, and — for a build —
    the saved work replayed onto current base (D19).

    -> dict(run_dir, worktree, branch, base_ref, replayed, kept). `replayed`
    says whether earlier work was carried over; `kept` is the ref the old tip
    was parked on when it no longer applied. Both feed prompt.build.
    """
    pol = ctx.policy(project)
    repo, base = pol["path"], pol.get("base", "main")
    check_account(ctx, project, platform)
    run_dir = os.path.join(config.RUNS_DIR, str(run_id))
    config.ensure_private_dir(run_dir)
    wt = os.path.join(worktree_root(pol), project, f"{item['number']}-run{run_id}")
    config.ensure_private_dir(os.path.dirname(wt))

    git(repo, "fetch", "--quiet", "--prune", "origin")
    branch, start = None, f"origin/{base}"
    if role == "sort":
        git(repo, "worktree", "add", "--quiet", "--detach", wt, start)
    else:
        # a fix run works on the PR's head branch itself (D18): its pushes
        # re-trigger CI. A build gets the item's canonical branch name.
        branch = (item["branch"] if role == "fix" and item["branch"]
                  else f"mahler/{item['number']}-{slug(item['title'])}")
        start = start_ref(repo, base, item["branch"], branch)
        try:
            git(repo, "worktree", "add", "--quiet", "-B", branch, wt, start)
        except GitError:          # branch still checked out by a kept worktree
            branch = f"{branch}-r{run_id}"
            git(repo, "worktree", "add", "--quiet", "-B", branch, wt, start)
    # git created the worktree under the process umask; keep it user-only —
    # it can hold linked .env files and repo content (issue #75)
    config.ensure_private_dir(wt)

    for name in pol.get("link") or []:          # e.g. .env pointing at *local* services only
        src, dst = os.path.join(repo, name), os.path.join(wt, name)
        if os.path.exists(src) and not os.path.lexists(dst):
            os.symlink(src, dst)

    replayed, kept = False, None
    if role == "build" and start != f"origin/{base}":
        replayed = True
        kept = catch_up(wt, branch, base, item["number"], run_id)
        if kept:
            start = f"origin/{base}"
    return {"run_dir": run_dir, "worktree": wt, "branch": branch,
            "base_ref": start, "replayed": replayed, "kept": kept}


def spawn(argv, cwd, log_path, status_path, env=None, append=False, prefix=""):
    """Start `argv` detached, with its output in the run's log and its exit
    code in `status_path`. Every element is shlex-quoted, so untrusted content
    inside the argv (issue titles, agent output) is data, never shell
    (mahler#74). -> the pid of the /bin/sh that owns the process group."""
    shell = (f"{shlex.join(argv)} {'>>' if append else '>'} {shlex.quote(log_path)} 2>&1; "
             f"echo $? > {shlex.quote(status_path)}")
    proc = subprocess.Popen(["/bin/sh", "-c", prefix + shell], cwd=cwd, env=env,
                            start_new_session=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc.pid


def launch(ctx, project, item, role, platform, run_id, epoch, prompt, prep):
    """Start the platform's CLI, detached, on the worktree `prepare` made.
    `prompt` is already rendered (mahler/prompt.py) — the runner never writes
    the words a run is given."""
    pol = ctx.policy(project)
    pconf = ctx.cfg["platforms"][platform]
    account = config.account_of(pconf)
    wt, run_dir = prep["worktree"], prep["run_dir"]
    argv = platforms.argv_for(pconf, prompt, wt, role, pol["run_timeout_minutes"])
    if not argv[0]:
        raise RuntimeError(f"{platform} CLI not found")

    hooks = fence_hooks(pol["path"], run_dir)
    env = dict(config.run_env(ctx.cfg, account) or os.environ,
               MAHLER_RUN_ID=str(run_id), MAHLER_PROJECT=project,
               MAHLER_ISSUE=str(item["number"]), MAHLER_EPOCH=str(epoch),
               MAHLER_HOME=config.STATE,
               GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.hooksPath",
               GIT_CONFIG_VALUE_0=hooks)
    log_path = os.path.join(run_dir, "agent.log")
    status_path = os.path.join(run_dir, "exit")
    with open(os.path.join(run_dir, "prompt.md"), "w") as fh:
        fh.write(prompt)
    prefix = ""
    if pol.get("setup") and role == "build":   # e.g. dependency install; runs detached too
        setup_log = shlex.quote(os.path.join(run_dir, "setup.log"))
        prefix = (f"( {pol['setup']} ) > {setup_log} 2>&1 || "
                  f"{{ echo 97 > {shlex.quote(status_path)}; exit 97; }}; ")
    pid = spawn(argv, wt, log_path, status_path, env=env, prefix=prefix)
    return {"pid": pid, "worktree": wt, "branch": prep["branch"],
            "base_ref": prep["base_ref"], "log_path": log_path,
            "status_path": status_path}


def alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def signal_group(pid, sig):
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def terminate(pid):
    signal_group(pid, signal.SIGTERM)


def kill(pid):
    signal_group(pid, signal.SIGKILL)


def exit_code(run):
    try:
        with open(run["status_path"]) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError, TypeError):
        return None


def setup_tail(run, lines=20):
    """The last `lines` lines of the run's setup.log ('' when there isn't one) —
    surfaced in handoffs and `mahler status` when setup failed (issue #8)."""
    path = os.path.join(os.path.dirname(run["log_path"]), "setup.log")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            tail = "".join(fh.readlines()[-lines:]).strip()
    except OSError:
        return ""
    # The setup command is project config and can echo its environment (set -x,
    # verbose installers); this tail is posted to GitHub and printed by
    # `mahler log`, so credential-shaped strings are masked first (issue #76).
    return redact.redact(tail)


def snapshot(repo, wt, run_id, number, base):
    """Save everything a run left — commits *and* uncommitted/untracked files —
    to refs/heads/mahler/snapshot/<n>-run<id>, without touching the worktree,
    its index, or the stash. -> dict or None when there was nothing new."""
    if not os.path.isdir(wt):
        return None
    idx = os.path.join(config.RUNS_DIR, str(run_id), "snapshot.index")
    env = dict(os.environ, GIT_INDEX_FILE=idx)
    try:
        head = git(wt, "rev-parse", "HEAD")
        git(wt, "read-tree", "HEAD", env=env)
        git(wt, "add", "-A", env=env)
        tree = git(wt, "write-tree", env=env)
        sha = head
        if tree != git(wt, "rev-parse", "HEAD^{tree}"):
            sha = git(wt, "commit-tree", tree, "-p", head, "-m",
                      f"mahler: snapshot of run {run_id} (uncommitted work)", env=env)
        ahead = int(git(wt, "rev-list", "--count", f"origin/{base}..{sha}") or 0)
        if ahead == 0:
            return None
        ref = f"mahler/snapshot/{number}-run{run_id}"
        git(wt, "push", "--quiet", "--no-verify", "--force", "origin", f"{sha}:refs/heads/{ref}")
        stat = git(wt, "diff", "--shortstat", f"origin/{base}...{sha}", check=False)
        return {"ref": ref, "sha": sha[:9], "ahead": ahead, "stat": stat}
    finally:
        if os.path.exists(idx):
            os.remove(idx)


def worktree_root(pol):
    return os.path.expanduser(pol.get("worktree_root") or config.WORKTREES)


def remove_worktree(repo, wt, branch=None, root=None):
    """`wt` must be *inside* `root` (DESIGN D12: rm -rf never strays outside the
    worktree) — a bare `startswith` would also match a sibling directory that
    merely shares the prefix, e.g. root `.../worktrees` and wt
    `.../worktrees-evil/x`, so the comparison is anchored on a path boundary."""
    base = os.path.abspath(root or config.WORKTREES)
    if wt and os.path.isdir(wt) and os.path.abspath(wt).startswith(base + os.sep):
        git(repo, "worktree", "remove", "--force", wt, check=False)
        shutil.rmtree(wt, ignore_errors=True)
    git(repo, "worktree", "prune", check=False)
    if branch:
        git(repo, "branch", "-D", branch, check=False)


def commits_ahead(wt, base):
    """How many commits the worktree's HEAD is ahead of origin/<base>.
    Returns 0 on any error (missing worktree, detached HEAD, etc.)."""
    if not wt or not os.path.isdir(wt):
        return 0
    try:
        return int(git(wt, "rev-list", "--count", f"origin/{base}..HEAD") or 0)
    except (GitError, ValueError):
        return 0


def verify_in_worktree(wt, verify_cmd, timeout=120):
    """Run the project's verify command in the worktree.
    Returns True if it exits 0 within the timeout, False otherwise."""
    if not verify_cmd or not wt or not os.path.isdir(wt):
        return False
    try:
        r = subprocess.run(verify_cmd, shell=True, cwd=wt,
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError):
        return False
