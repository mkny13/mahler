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
import string
import subprocess
import sys

from . import config, platforms

MAHLER_BIN = os.path.join(config.REPO_ROOT, "bin", "mahler")
RECIPES = os.path.join(config.REPO_ROOT, "recipes")
HOOK_NAMES = ("applypatch-msg", "commit-msg", "post-checkout", "post-commit", "post-merge",
              "post-rewrite", "pre-applypatch", "pre-commit", "pre-merge-commit",
              "prepare-commit-msg", "pre-push", "pre-rebase")


class GitError(RuntimeError):
    pass


def git(repo, *args, env=None, check=True):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                       env=env, timeout=300)
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])}: {(r.stderr or r.stdout).strip()[:400]}")
    return r.stdout.strip()


def slug(title, n=40):
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return s[:n].rstrip("-") or "item"


def render(recipe, **vars):
    with open(os.path.join(RECIPES, f"{recipe}.md"), encoding="utf-8") as fh:
        return string.Template(fh.read()).safe_substitute(**vars)


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
            body += (f'"{MAHLER_BIN}" lease-check || {{ echo "mahler: this run no longer holds '
                     f'the lease on #$MAHLER_ISSUE — push refused" >&2; exit 1; }}\n')
        body += f'[ -x "{orig}/{name}" ] && exec "{orig}/{name}" "$@"\nexit 0\n'
        path = os.path.join(hooks, name)
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, 0o755)
    return hooks


def launch(ctx, project, item, role, platform, run_id, epoch):
    """Create the worktree, render the recipe, start the CLI detached."""
    pol = ctx.policy(project)
    repo, base = pol["path"], pol.get("base", "main")
    pconf = ctx.cfg["platforms"][platform]
    run_dir = os.path.join(config.RUNS_DIR, str(run_id))
    os.makedirs(run_dir, exist_ok=True)
    wt = os.path.join(worktree_root(pol), project, f"{item['number']}-run{run_id}")
    os.makedirs(os.path.dirname(wt), exist_ok=True)

    git(repo, "fetch", "--quiet", "--prune", "origin")
    branch, start = None, f"origin/{base}"
    if role == "sort":
        git(repo, "worktree", "add", "--quiet", "--detach", wt, start)
    else:
        branch = f"mahler/{item['number']}-{slug(item['title'])}"
        start = start_ref(repo, base, item["branch"], branch)
        try:
            git(repo, "worktree", "add", "--quiet", "-B", branch, wt, start)
        except GitError:          # branch still checked out by a kept worktree
            branch = f"{branch}-r{run_id}"
            git(repo, "worktree", "add", "--quiet", "-B", branch, wt, start)

    for name in pol.get("link") or []:          # e.g. .env pointing at *local* services only
        src, dst = os.path.join(repo, name), os.path.join(wt, name)
        if os.path.exists(src) and not os.path.lexists(dst):
            os.symlink(src, dst)

    handoff = ""
    if role == "build" and start != f"origin/{base}":
        handoff = (f"- earlier work on this item is already in your branch (started from "
                   f"`{start}`): run `git log --oneline origin/{base}..HEAD`, and read the "
                   f"latest `mahler:agent handoff` comment on the issue before continuing")
    prompt = render(role, number=item["number"], title=item["title"], repo=pol["repo"],
                    worktree=wt, branch=branch or "", base=base, platform=platform,
                    verify=pol.get("verify") or "the project's tests (see CLAUDE.md)",
                    mahler=MAHLER_BIN, handoff=handoff,
                    rules=("\nProject rules (from Mahler's config — these override anything else):\n"
                           + pol["rules"].strip() + "\n") if pol.get("rules") else "")
    argv = platforms.argv_for(pconf, prompt, wt, role, pol["run_timeout_minutes"])
    if not argv[0]:
        raise RuntimeError(f"{platform} CLI not found")

    hooks = fence_hooks(repo, run_dir)
    env = dict(os.environ,
               MAHLER_RUN_ID=str(run_id), MAHLER_PROJECT=project,
               MAHLER_ISSUE=str(item["number"]), MAHLER_EPOCH=str(epoch),
               MAHLER_HOME=config.STATE,
               GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.hooksPath",
               GIT_CONFIG_VALUE_0=hooks)
    log_path = os.path.join(run_dir, "agent.log")
    status_path = os.path.join(run_dir, "exit")
    with open(os.path.join(run_dir, "prompt.md"), "w") as fh:
        fh.write(prompt)
    shell = (f"{shlex.join(argv)} > {shlex.quote(log_path)} 2>&1; "
             f"echo $? > {shlex.quote(status_path)}")
    if pol.get("setup") and role == "build":   # e.g. dependency install; runs detached too
        setup_log = shlex.quote(os.path.join(run_dir, "setup.log"))
        shell = (f"( {pol['setup']} ) > {setup_log} 2>&1 || "
                 f"{{ echo 97 > {shlex.quote(status_path)}; exit 97; }}; " + shell)
    proc = subprocess.Popen(["/bin/sh", "-c", shell], cwd=wt, env=env,
                            start_new_session=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"pid": proc.pid, "worktree": wt, "branch": branch, "base_ref": start,
            "log_path": log_path, "status_path": status_path}


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
        return int(open(run["status_path"]).read().strip())
    except (OSError, ValueError, TypeError):
        return None


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
        stat = git(wt, "diff", "--shortstat", f"origin/{base}", sha, check=False)
        return {"ref": ref, "sha": sha[:9], "ahead": ahead, "stat": stat}
    finally:
        if os.path.exists(idx):
            os.remove(idx)


def worktree_root(pol):
    return os.path.expanduser(pol.get("worktree_root") or config.WORKTREES)


def remove_worktree(repo, wt, branch=None, root=None):
    if wt and os.path.isdir(wt) and os.path.abspath(wt).startswith(root or config.WORKTREES):
        git(repo, "worktree", "remove", "--force", wt, check=False)
        shutil.rmtree(wt, ignore_errors=True)
    git(repo, "worktree", "prune", check=False)
    if branch:
        git(repo, "branch", "-D", branch, check=False)


def self_bin():
    return f"{shlex.quote(sys.executable)} {shlex.quote(MAHLER_BIN)}"
