"""Runs: one agent process, on one platform, for one item, in its own worktree.

Every run ends in a handoff (DESIGN D9): whatever it left behind is saved to a
pushed ref without touching anyone's working tree, and the next run — on any
platform — starts from there.
"""

import os
import json
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import cline_free, config, github_pat, lessons as lessons_mod, platforms, redact, router
from .ledger import row_get

HOOK_NAMES = ("applypatch-msg", "commit-msg", "post-checkout", "post-commit", "post-merge",
              "post-rewrite", "pre-applypatch", "pre-commit", "pre-merge-commit",
              "prepare-commit-msg", "pre-push", "pre-rebase")


class GitError(RuntimeError):
    pass


class WorktreeConflict(GitError):
    """A PR branch is held by a worktree we must leave alone."""

    def __init__(self, branch, path):
        self.branch, self.path = branch, path
        super().__init__(f"Branch '{branch}' is checked out by worktree at '{path}'. "
                         "Release that branch in the worktree, then retry the item.")


def _fix_worktree_conflict(ctx, repo, branch, root):
    """Recover only inactive managed checkouts; never alter an external tree."""
    listing = git(repo, "worktree", "list", "--porcelain", "-z")
    for record in listing.split("\0\0"):
        fields = dict(field.split(" ", 1) for field in record.split("\0") if " " in field)
        if fields.get("branch") != f"refs/heads/{branch}" or not fields.get("worktree"):
            continue
        path = fields["worktree"]
        candidate, base = Path(path).resolve(), Path(root).resolve()
        if candidate == base or not candidate.is_relative_to(base):
            raise WorktreeConflict(branch, path)
        if any(r["worktree"] and Path(r["worktree"]).resolve() == candidate
               for r in ctx.led.active_runs()):
            return False
        # No --force: dirty, locked, or otherwise unsafe worktrees stay intact.
        git(repo, "worktree", "remove", path)
        git(repo, "worktree", "prune")
        return True
    return False


def git(repo, *args, env=None, check=True):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                       env=env, timeout=300)
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])}: "
                       f"{redact.redact((r.stderr or r.stdout).strip())[:400]}")
    return r.stdout.strip()


_SSH_DENIED = "permission denied (publickey)"
_SSH_PREFIXES = ("git@github.com:", "ssh://git@github.com/")


def fetch_origin(repo, *args, env=None):
    """``git fetch`` from origin. If origin is a GitHub SSH URL and SSH auth is denied,
    retry once over HTTPS with the gh credential, scoped to this command (mahler#1523)."""
    try:
        return git(repo, "fetch", "--quiet", *args, env=env)
    except GitError as ssh_err:
        if _SSH_DENIED not in str(ssh_err).lower():
            raise
        url = git(repo, "remote", "get-url", "origin", check=False)
        if not url.startswith(_SSH_PREFIXES):
            raise
        opts = [o for p in _SSH_PREFIXES
                for o in ("-c", f"url.https://github.com/.insteadOf={p}")]
        opts += ["-c", "credential.https://github.com.helper=",
                 "-c", "credential.https://github.com.helper=!gh auth git-credential"]
        try:
            return git(repo, *opts, "fetch", "--quiet", *args, env=env)
        except GitError as https_err:
            raise GitError(f"origin {url} refused SSH (Permission denied (publickey)) and the "
                           f"HTTPS fallback failed: {https_err}; fix the SSH key or switch "
                           "the remote to https") from https_err


def find_worktree_for_branch(repo, branch):
    """Find a linked checkout, excluding the primary checkout."""
    if not branch:
        return None
    listing = git(repo, "worktree", "list", "--porcelain", "-z")
    for record in listing.split("\0\0")[1:]:
        fields = dict(field.split(" ", 1) for field in record.split("\0") if " " in field)
        if fields.get("branch") == f"refs/heads/{branch}":
            return fields.get("worktree")
    return None


def cleanup_interactive_worktree(repo, wt, branch, expected_head):
    """Remove only the recorded, unchanged checkout; return an operator notice."""
    try:
        if not Path(wt).is_dir():
            return f"skipped worktree cleanup: {wt} no longer exists"
        if not branch or not expected_head:
            return f"skipped worktree cleanup: missing branch or merged PR head for {wt}"
        found = find_worktree_for_branch(repo, branch)
        if not found or Path(found).resolve() != Path(wt).resolve():
            return f"skipped worktree cleanup: {wt} no longer holds {branch}"
        status = git(wt, "status", "--porcelain", "--untracked-files=all", "--ignored", "-z")
        for entry in status.split("\0"):
            if not entry:
                continue
            path = Path(entry[3:])
            # Verification creates disposable bytecode. Keep every other ignored
            # file (including local credentials and non-bytecode cache contents).
            if (entry.startswith("!! ") and path.parent.name == "__pycache__"
                    and path.suffix == ".pyc" and (Path(wt) / path).is_file()
                    and not (Path(wt) / path).is_symlink()):
                continue
            # node_modules/ is gitignored by Groundwork's pnpm install; exempt its contents
            if (entry.startswith("!! ") and "node_modules" in path.parts
                    and (Path(wt) / path).is_file()
                    and not (Path(wt) / path).is_symlink()):
                continue
            return f"skipped worktree cleanup: {wt} has uncommitted or ignored files"
        if git(wt, "rev-parse", "HEAD") != expected_head:
            return f"skipped worktree cleanup: {wt} HEAD differs from merged PR head"
        git(repo, "worktree", "remove", wt)
        # Squash merges do not make the topic commits ancestors of the base.
        # Recheck the ref after removal before deleting it.
        if git(repo, "rev-parse", f"refs/heads/{branch}") != expected_head:
            return f"removed {wt}; skipped branch cleanup: {branch} changed"
        git(repo, "branch", "-D", "--", branch)
        return f"removed interactive worktree {wt} and local branch {branch}"
    except (GitError, OSError, subprocess.SubprocessError) as e:
        return f"skipped worktree cleanup for {wt}: {e}"


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


def catch_up(wt, branch, base, number, run_id, env=None):
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
            f"HEAD:refs/heads/{kept}", env=env)
        git(wt, "reset", "--quiet", "--hard", onto)
    # The remote branch must not keep the stale history, or the agent's first push
    # is rejected and a weak model "fixes" that by pulling it back in. But a branch
    # sitting at base's tip reads as merged on an open PR, so drop it instead.
    if int(git(wt, "rev-list", "--count", f"{onto}..HEAD") or 0):
        git(wt, "push", "--quiet", "--no-verify", "--force", "origin",
            f"HEAD:refs/heads/{branch}", env=env)
    else:
        git(wt, "push", "--quiet", "--no-verify", "origin", "--delete", branch, check=False,
            env=env)
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
            body += (f'{shlex.quote(config.MAHLER_BIN)} lease-check || {{ echo "mahler: this run no longer holds '
                     f'the lease on #$MAHLER_ISSUE — push refused" >&2; exit 1; }}\n')
        original_hook = shlex.quote(os.path.join(orig, name))
        body += f'[ -x {original_hook} ] && exec {original_hook} "$@"\nexit 0\n'
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
    # git hosting credentials are the project's own identity (D26's gh_account),
    # not the run's AI-platform account — the same env gh.py's GH client uses,
    # so a work project's private repo isn't fetched/pushed with this machine's
    # default (personal) git credential helper (mahler#296).
    env = config.run_env(ctx.cfg, config.gh_account_of(pol))
    run_dir = os.path.join(config.RUNS_DIR, str(run_id))
    config.ensure_private_dir(run_dir)
    wt = os.path.join(worktree_root(pol), project, f"{item['number']}-run{run_id}")
    config.ensure_private_dir(os.path.dirname(wt))

    fetch_origin(repo, "--prune", "origin", env=env)
    push_branch = None
    branch, start = None, f"origin/{base}"
    claim_sha = row_get(item, "claim_base_sha") if role == "review" else None
    if claim_sha:
        from . import no_change
        # Bind before launch: a restart cannot lose which run owns this review.
        record = no_change.read(ctx.led, project, item["number"])
        lease = ctx.led.lease(project, item["number"])
        if record.get("base_sha") != claim_sha or not lease or lease["run_id"] != run_id:
            raise GitError("claim review lost its base or lease")
        record.update(phase="reviewing", run_id=run_id, epoch=lease["epoch"])
        no_change.save(ctx.led, project, item["number"], record)
        fetch_origin(repo, "origin", claim_sha, env=env)
        start = claim_sha
        git(repo, "worktree", "add", "--quiet", "--detach", wt, start)
    elif role in ("sort", "review"):
        if role == "review":
            start = start_ref(repo, base, item["branch"], item["branch"])
        git(repo, "worktree", "add", "--quiet", "--detach", wt, start)
    else:
        if role == "design" and (not row_get(item, "pr") or not row_get(item, "branch")
                                 or not remote_has(repo, row_get(item, "branch"))):
            raise GitError("design run requires the current PR branch")
        # A fix works on the PR's branch so pushes re-trigger CI (D18);
        # review and design inspect that same head without handoff edits (D11).
        branch = (item["branch"] if role in ("fix", "design") and item["branch"]
                  else f"mahler/{item['number']}-{slug(item['title'])}")
        push_branch = branch
        start = start_ref(repo, base, item["branch"], branch)
        try:
            git(repo, "worktree", "add", "--quiet", "-B", branch, wt, start)
        except GitError:          # branch still checked out by a kept worktree
            if role == "fix":
                # A fix must push the existing PR, never silently fork its head.
                try:
                    freed = _fix_worktree_conflict(ctx, repo, branch, worktree_root(pol))
                except WorktreeConflict:
                    raise
                except GitError:
                    freed = False  # retained dirty/locked checkout stays untouched
                if not freed:
                    branch = f"{branch}-r{run_id}"
            else:
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
        kept = catch_up(wt, branch, base, item["number"], run_id, env=env)
        if kept:
            start = f"origin/{base}"
    # D44: lessons are scanned against the base ref; whatever the worktree
    # holds that isn't byte-identical to a scan-passed base lesson is removed.
    lessons = lessons_mod.load(repo, wt, f"origin/{base}", project)
    for line in lessons["diagnostics"]:
        getattr(ctx, "say", print)(f"{project}#{item['number']}: {line}")
    if lessons.get("fatal"):
        raise GitError("lesson suppression failed; refusing to launch")
    return {"run_dir": run_dir, "worktree": wt, "branch": branch,
            "push_branch": push_branch or branch,
            "base_ref": start, "replayed": replayed, "kept": kept,
            "lessons": lessons,
            "head_sha": git(wt, "rev-parse", "HEAD")}


def spawn(argv, cwd, log_path, status_path, env=None, append=False, prefix="", stdin_path=None):
    """Start `argv` detached, with its output in the run's log and its exit
    code in `status_path`. Every element is shlex-quoted, so untrusted content
    inside the argv (issue titles, agent output) is data, never shell
    (mahler#74). When `stdin_path` is given, stdin is redirected from that
    file (< <quoted path>). -> the pid of the /bin/sh that owns the process group."""
    tracker = None
    gate = None
    if sys.platform == "darwin" and env:
        marker = env.get("MAHLER_RUN_ID")
        runs_dir = env.get("MAHLER_RUNS_DIR")
        if marker and runs_dir and re.fullmatch(r"[0-9]+", marker):
            run_dir = Path(runs_dir) / marker
            # An unrelated subprocess can inherit MAHLER_* from an agent that
            # happens to be testing Mahler. Only the status file Mahler placed
            # in this run directory authorizes tracker/marker creation.
            if (run_dir.is_dir()
                    and Path(status_path).resolve().parent == run_dir.resolve()):
                gate = run_dir / "process-tracker-gate"
                ready = run_dir / "process-tracker-ready"
                for path in (gate, ready):
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                quoted_gate = shlex.quote(str(gate))
                quoted_ready = shlex.quote(str(ready))
                quoted_status = shlex.quote(status_path)
                prefix = (f"i=0; while [ ! -e {quoted_gate} ] && [ $i -lt 500 ]; do "
                          "sleep .01; i=$((i + 1)); done; "
                          f"if [ ! -e {quoted_gate} ]; then echo 98 > {quoted_status}; exit 98; fi; "
                          f"/bin/rm -f {quoted_gate} {quoted_ready}; " + prefix)
    stdin_redir = f" < {shlex.quote(stdin_path)}" if stdin_path else ""
    shell = (f"{shlex.join(argv)}{stdin_redir} {'>>' if append else '>'} {shlex.quote(log_path)} 2>&1; "
             f"echo $? > {shlex.quote(status_path)}")
    marker_stream = None
    pass_fds = ()
    if gate is not None:
        # Darwin intentionally hides another process's environment, including
        # through KERN_PROCARGS2. Keep the run marker open as an additional,
        # kernel-visible ownership token. fd 0 survives Python's default
        # close_fds behaviour; the extra fd survives ordinary shell
        # backgrounding, which redirects fd 0 to /dev/null.
        marker_path = gate.with_name("process-marker")
        marker_flags = os.O_RDONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        marker_fd = os.open(marker_path, marker_flags, 0o600)
        os.fchmod(marker_fd, 0o600)
        marker_stream = os.fdopen(marker_fd, "rb")
        pass_fds = (marker_fd,)
    try:
        proc = subprocess.Popen(["/bin/sh", "-c", prefix + shell], cwd=cwd, env=env,
                                start_new_session=True,
                                stdin=marker_stream or subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                pass_fds=pass_fds)
    finally:
        if marker_stream is not None:
            marker_stream.close()
    if gate is not None:
        ready = gate.with_name("process-tracker-ready")
        tracker_env = {
            "LC_ALL": "C",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
        }
        code = ("from mahler.runner import _track_run_processes; "
                "import sys; _track_run_processes(sys.argv[1], int(sys.argv[2]), "
                "sys.argv[3], sys.argv[4])")
        try:
            tracker = subprocess.Popen(
                [sys.executable, "-c", code, env["MAHLER_RUN_ID"], str(proc.pid),
                 str(_ownership_path(env["MAHLER_RUN_ID"], env["MAHLER_RUNS_DIR"])),
                 str(ready)],
                env=tracker_env, start_new_session=True, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 3
            while not ready.exists() and tracker.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            if not ready.exists():
                raise RuntimeError("run process tracker did not start")
            gate.touch(mode=0o600)
        except Exception:
            signal_group(proc.pid, signal.SIGKILL)
            if tracker is not None and tracker.poll() is None:
                tracker.kill()
            raise
    return proc.pid


def keep_copilot_login(env, base_env, run_dir):
    """Keep Copilot signed in with its own login (mahler#1211).

    Copilot authenticates with COPILOT_GITHUB_TOKEN, GH_TOKEN or GITHUB_TOKEN
    ahead of the login stored under COPILOT_HOME. So a token this run sets
    for `gh` — the project's App token (D47), or another account's token
    (D26) — would become Copilot's login too, and Copilot refuses it with
    "Access denied by policy settings". The token is dropped, not moved to
    hosts.yml: Copilot's fallback is `gh`, which would hand it the App token
    and fail on /user (#1506). Copilot runs authenticate only with the
    stored `copilot login`; the conductor does the pushing. A token the platform's own
    account env sets is left alone."""
    token = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN")
    if not token or all(env.get(var) == base_env.get(var)
                        for var in ("GH_TOKEN", "GITHUB_TOKEN")):
        return
    # Copilot falls back to `gh` when it has no stored login and would read
    # the token from hosts.yml too, so nothing is written there: the run gets
    # an empty gh config dir and Copilot never sees an overlay token (#1506).
    gh_config_dir = os.path.join(run_dir, "gh")
    config.ensure_private_dir(gh_config_dir)
    env["GH_CONFIG_DIR"] = gh_config_dir
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)


def run_env(ctx, project, number, platform, run_id, epoch):
    """Shared environment for a run: platform account env, GH identity
    overlay (D25/D26), MAHLER_* vars, and the pre-push fence hooks (DESIGN D6)."""
    pol = ctx.policy(project)
    pconf = ctx.cfg["platforms"][platform]
    account = config.account_of(pconf)
    run_dir = os.path.join(config.RUNS_DIR, str(run_id))
    hooks = fence_hooks(pol["path"], run_dir)

    base_env = config.run_env(ctx.cfg, account) or os.environ
    env = dict(base_env)

    pat_token = github_pat.agent_token(ctx.cfg, pol)
    app = None if pat_token is not None else config.project_github_app(ctx.cfg, pol)
    if app is not None or pat_token is not None:
        for var in config.GH_IDENTITY_VARS:
            env.pop(var, None)
        for var in ("GIT_ASKPASS", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE"):
            env.pop(var, None)
        for k in list(env):
            if k.startswith("GITHUB_APP_") or k.startswith("MAHLER_GITHUB_APP_"):
                env.pop(k, None)
        repo_name = pol.get("repo", "").rsplit("/", 1)[-1]
        token = pat_token if pat_token is not None else app.agent_token(repo_name)
        gh_config_dir = os.path.join(run_dir, "gh")
        config.ensure_private_dir(gh_config_dir)
        env["GH_TOKEN"] = token
        env["GH_CONFIG_DIR"] = gh_config_dir
        git_config = {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": hooks,
            "GIT_CONFIG_KEY_1": "credential.helper",
            "GIT_CONFIG_VALUE_1": "",
            "GIT_CONFIG_KEY_2": "core.askPass",
            "GIT_CONFIG_VALUE_2": "",
        }
    else:
        if (
            pol.get("require_scoped_identity")
            or pol.get("require_scoped_run_identity")
            or (ctx.cfg.get("defaults") or {}).get("require_scoped_identity")
            or (ctx.cfg.get("defaults") or {}).get("require_scoped_run_identity")
        ):
            raise ValueError(f"project {project!r} requires scoped run identity but no approved GitHub provider is available")
        getattr(ctx, "say", print)(f"{project}#{number}: warning: non-App project using transitional GitHub identity (migration tracked in #1145)")
        gh_acct = config.gh_account_of(pol)
        if gh_acct != config.DEFAULT_ACCOUNT and gh_acct not in (ctx.cfg.get("accounts") or {}):
            raise ValueError(f"account {gh_acct!r} is not defined under [accounts]")
        if account != gh_acct:
            overlay = config.gh_identity_env(ctx.cfg, pol)
            if overlay is None:
                for var in config.GH_IDENTITY_VARS:
                    env.pop(var, None)
            else:
                set_vars, drop_vars = overlay
                for var in drop_vars:
                    env.pop(var, None)
                env.update(set_vars)
        git_config = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": hooks,
        }

    if pconf["kind"] == "copilot":
        keep_copilot_login(env, base_env, run_dir)

    env.update(MAHLER_RUN_ID=str(run_id), MAHLER_PROJECT=project,
               MAHLER_ISSUE=str(number), MAHLER_EPOCH=str(epoch),
               MAHLER_HOME=config.STATE, MAHLER_RUNS_DIR=config.RUNS_DIR,
               **git_config)
    if pconf["kind"] == "vibe":
        env.update(platforms.vibe_env(pconf, run_dir, env))
    if pconf.get("api_credits"):
        # D41 step 2: the real workspace key never reaches a run. It lives
        # only in the gateway process; this run's env gets a fresh local
        # token good only for the gateway, plus the base-URL override that
        # points the harness at it instead of api.anthropic.com directly.
        from . import api_credits
        pool = pconf["credit_pool"]
        gw = api_credits.gateway_for_run(ctx, platform, pconf)
        env[pool["base_url_env"]] = f"http://{pool['gateway_host']}:{gw.port}"
        env["ANTHROPIC_API_KEY"] = gw.register(run_id)
    return env


class ResumeUnsupported(ValueError):
    """The requested saved session cannot be used for this launch."""


def resume_session(ctx, project, item, role, platform, resume_from, env):
    """Resolve a ledger run's explicit session in its platform/account namespace.

    No discovery of user sessions or old environment reuse. The caller keeps
    the new run ID, prompt, worktree and fencing. Unusable sources fail closed.
    """
    source = ctx.led.run(resume_from)
    pconf = ctx.cfg["platforms"][platform]
    old_conf = ctx.cfg["platforms"].get(row_get(source, "platform"), {})
    session_id = row_get(source, "session_id")
    if (role != "fix" or not source or source["status"] != "ended"
            or source["project"] != project or source["number"] != item["number"]
            or not platforms.supports_resume(pconf)
            or old_conf.get("kind") != pconf["kind"]
            or config.account_of(old_conf) != config.account_of(pconf)
            or not isinstance(session_id, str) or not session_id.strip()):
        raise ResumeUnsupported("source run is not resumable for this fix")
    session_id = session_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", session_id):
        raise ResumeUnsupported("source session ID is not an explicit identifier")
    kind = pconf["kind"]
    account = config.account_of(pconf)
    namespace_var = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME",
                     "kilo": "XDG_DATA_HOME"}.get(kind)
    if (account != config.DEFAULT_ACCOUNT and namespace_var
            and not (ctx.cfg.get("accounts", {}).get(account, {}).get("env") or {}).get(namespace_var)):
        raise ResumeUnsupported("source account has no isolated session namespace")
    home = Path(env.get("HOME", platforms.HOME))
    try:
        if kind == "claude":
            root = Path(env.get("CLAUDE_CONFIG_DIR", home / ".claude")) / "projects"
            paths = list(root.glob(f"*/{session_id}.jsonl"))
        elif kind == "codex":
            # Fresh Codex runs remain ephemeral; their IDs alone do not prove
            # resumability. Only a persisted rollout is usable.
            root = Path(env.get("CODEX_HOME", home / ".codex")) / "sessions"
            paths = list(root.glob(f"**/*-{session_id}.jsonl"))
        elif kind == "kilo":
            # Kilo 7.6.2 uses the account's XDG data store, shared across
            # worktrees. Never point a work account at another login's store.
            root = Path(env.get("XDG_DATA_HOME", home / ".local/share")) / "kilo"
            paths = [root]
        else:
            # Vibe 2.25.8 local source: paths/_vibe_home.py stores sessions at
            # VIBE_HOME/logs/session. app_server/_runtime.py's _load_session
            # resolves explicit IDs without the cwd filter used by --continue;
            # resume_blueprint builds with the NEW options.cwd. Reuse only
            # session storage, not the old config, credentials or environment.
            # The source's own folder: [storage] runs_dir may have moved since.
            src_dir = (Path(source["log_path"]).parent if row_get(source, "log_path")
                       else Path(config.RUNS_DIR) / str(source["id"]))
            root = src_dir / "vibe_home/logs/session"
            paths = [root]
        if not paths or not all(os.access(p, os.R_OK) for p in paths):
            raise ResumeUnsupported("source session storage is inaccessible")
        if kind == "vibe":
            target = Path(env["VIBE_HOME"]) / "logs/session"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(root.resolve(), target_is_directory=True)
    except OSError as exc:
        raise ResumeUnsupported("source session storage is inaccessible") from exc
    return session_id


def launch(ctx, project, item, role, platform, run_id, epoch, prompt, prep,
           resume_from=None):
    """Start the platform's CLI, detached, on the worktree `prepare` made.
    `prompt` is already rendered (mahler/prompt.py) — the runner never writes
    the words a run is given."""
    pol = ctx.policy(project)
    pconf = ctx.cfg["platforms"][platform]
    wt, run_dir = prep["worktree"], prep["run_dir"]
    platform_role = "sort" if role == "design" else role
    if cline_free.managed(pconf) and hasattr(ctx, "led"):
        row = ctx.led.q1("SELECT model FROM runs WHERE id=?", (run_id,))
        if row and row["model"]:
            pconf = dict(pconf, model=row["model"])      # the free model tick chose
    argv = platforms.argv_for(pconf, prompt, wt, platform_role, pol["run_timeout_minutes"],
                          **({"run_dir": run_dir} if pconf["kind"] == "agent-sdk" else {}))
    env = run_env(ctx, project, item["number"], platform, run_id, epoch)
    if resume_from is not None:
        check_account(ctx, project, platform)
        session_id = resume_session(ctx, project, item, role, platform, resume_from, env)
        argv = platforms.resume_argv_for(pconf, prompt, wt, role,
                                         pol["run_timeout_minutes"], session_id=session_id)
    if not argv[0]:
        raise RuntimeError(f"{platform} CLI not found")
    # Keep the run record aligned with the exact validated setting passed to
    # the adapter, including the explicit default for unconfigured runs.
    if hasattr(ctx, "led"):
        lines = router.platform_burst(platform, pconf, getattr(ctx, "burst_lines", None))
        ctx.led.update_run(run_id, effort=platforms.effort_value(pconf, platform_role) or "default",
                           burst_lines=json.dumps(lines) if lines else None)

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
    if not pid:
        return
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def terminate(pid):
    signal_group(pid, signal.SIGTERM)


def kill(pid):
    signal_group(pid, signal.SIGKILL)


OWNERSHIP_FILE = "process-ownership.json"


def _ownership_path(run_id, runs_dir=None):
    marker = str(run_id)
    if not re.fullmatch(r"[0-9]+", marker):
        raise ValueError("run id must be numeric")
    return Path(runs_dir or config.RUNS_DIR) / marker / OWNERSHIP_FILE


def _read_ownership(run_id=None):
    """Read tracker records as data. Invalid or partial files own nothing."""
    if run_id is None:
        paths = Path(config.RUNS_DIR).glob(f"[0-9]*/{OWNERSHIP_FILE}")
    else:
        try:
            paths = [_ownership_path(run_id)]
        except ValueError:
            return {}
    found = {}
    for path in paths:
        try:
            marker = path.parent.name
            data = json.loads(path.read_text())
            if (not isinstance(data, dict)
                    or not re.fullmatch(r"[0-9]+", marker)
                    or data.get("run_id") != marker
                    or not isinstance(data.get("processes"), list)):
                continue
            records = []
            for item in data["processes"]:
                if (not isinstance(item, dict)
                        or not isinstance(item.get("pid"), int)
                        or not isinstance(item.get("uid"), int)
                        or not isinstance(item.get("identity"), str)
                        or not item["identity"]):
                    continue
                records.append({"pid": item["pid"], "uid": item["uid"],
                                "identity": item["identity"]})
            found[marker] = records
        except (OSError, ValueError, TypeError):
            continue
    return found


def _write_ownership(path, run_id, records):
    """Atomically persist PID identities without commands or environments."""
    path = Path(path)
    marker = str(run_id)
    if path.parent.name != marker or path.name != OWNERSHIP_FILE:
        raise ValueError("ownership path does not match run id")
    payload = {
        "run_id": marker,
        "processes": sorted(records.values(), key=lambda item: item["pid"]),
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, separators=(",", ":"))
            fh.write("\n")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _descendants(rows, leader_pid):
    children = {}
    for row in rows.values():
        children.setdefault(row["ppid"], []).append(row["pid"])
    found = set()
    pending = [leader_pid]
    while pending:
        parent = pending.pop()
        for child in children.get(parent, ()):
            if child not in found:
                found.add(child)
                pending.append(child)
    return found


def _track_run_processes(run_id, leader_pid, ownership_path, ready_path):
    """Darwin launch sidecar: remember descendants while ancestry still exists."""
    marker = str(run_id)
    path = Path(ownership_path)
    ready = Path(ready_path)
    records = {}
    leader_identity = None
    started = time.monotonic()
    while True:
        try:
            rows = _ps_process_listing()
        except (OSError, subprocess.SubprocessError, RuntimeError):
            if time.monotonic() - started > 5:
                return
            time.sleep(.1)
            continue
        leader = rows.get(leader_pid)
        if leader_identity is None:
            if leader is None:
                if time.monotonic() - started > 5:
                    return
                time.sleep(.01)
                continue
            leader_identity = leader["identity"]
            try:
                ready.touch(mode=0o600)
            except OSError:
                return
        elif leader is None or leader["identity"] != leader_identity:
            return
        changed = False
        for pid in _descendants(rows, leader_pid):
            row = rows.get(pid)
            if row is None or row["uid"] != os.getuid():
                continue
            record = {"pid": pid, "uid": row["uid"], "identity": row["identity"]}
            if records.get(pid) != record:
                records[pid] = record
                changed = True
        if changed:
            try:
                _write_ownership(path, marker, records)
            except (OSError, ValueError):
                return
        time.sleep(.1)


def _merge_recorded_ownership(rows, run_id=None):
    """Attach durable ownership only after current UID/start identity validation."""
    claims = {}
    for marker, records in _read_ownership(run_id).items():
        for record in records:
            row = rows.get(record["pid"])
            if (row is None or row["uid"] != record["uid"]
                    or row["identity"] != record["identity"]):
                continue
            claims.setdefault(record["pid"], set()).add(marker)
    for pid, markers in claims.items():
        if len(markers) != 1:
            continue
        marker = next(iter(markers))
        observed = rows[pid].get("run_id")
        if observed is None:
            rows[pid]["run_id"] = marker


def _merge_open_marker_ownership(rows, run_id=None):
    """Attach Darwin ownership proved by a process's open per-run marker.

    ``ps`` and KERN_PROCARGS2 omit native-process environments on current
    macOS. An inherited open file is still kernel state, remains observable
    after setsid/reparenting, and cannot accidentally claim an unrelated
    process merely because it uses the same worktree.
    """
    if run_id is None:
        paths = Path(config.RUNS_DIR).glob("[0-9]*/process-marker")
    else:
        marker = str(run_id)
        if not re.fullmatch(r"[0-9]+", marker):
            return
        paths = [Path(config.RUNS_DIR) / marker / "process-marker"]
    claims = {}
    for path in paths:
        try:
            if not path.is_file() or path.is_symlink():
                continue
            marker = path.parent.name
            result = subprocess.run(
                ["/usr/sbin/lsof", "-Fn", "--", str(path)],
                capture_output=True, text=True, timeout=10,
                env=dict(os.environ, LC_ALL="C"))
            if result.returncode not in (0, 1):
                continue
            for line in result.stdout.splitlines():
                if not line.startswith("p"):
                    continue
                try:
                    pid = int(line[1:])
                except ValueError:
                    continue
                if pid in rows and rows[pid]["uid"] == os.getuid():
                    claims.setdefault(pid, set()).add(marker)
        except (OSError, subprocess.SubprocessError):
            continue
    for pid, markers in claims.items():
        if len(markers) == 1 and rows[pid].get("run_id") is None:
            rows[pid]["run_id"] = next(iter(markers))


def _compact_ownership(run_id, rows=None):
    """Drop exited/reused PIDs; remove the file once no identities remain."""
    try:
        path = _ownership_path(run_id)
        records = _read_ownership(run_id).get(str(run_id), [])
        rows = _process_snapshot() if rows is None else rows
        live = {record["pid"]: record for record in records
                if record["pid"] in rows
                and rows[record["pid"]]["uid"] == record["uid"]
                and rows[record["pid"]]["identity"] == record["identity"]}
        if live:
            _write_ownership(path, run_id, live)
        else:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    except (OSError, ValueError, subprocess.SubprocessError, RuntimeError):
        pass


def _ps_process_listing(environment=False):
    """Darwin/BSD process metadata; callers must never log the command field."""
    args = ["ps", "-axo", "pid=,ppid=,uid=,lstart=,command=", "-ww"]
    if environment:
        args.append("-E")
    result = subprocess.run(args, capture_output=True, text=True, timeout=10,
                            env=dict(os.environ, LC_ALL="C"))
    if result.returncode:
        raise RuntimeError("process inspection failed")
    parsed = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 8)
        if len(parts) != 9:
            continue
        try:
            pid, parent, uid = map(int, parts[:3])
        except ValueError:
            continue
        parsed[pid] = dict(pid=pid, ppid=parent, uid=uid,
                           identity=" ".join(parts[3:8]), command=parts[8])
    return parsed


def _process_snapshot():
    """Private process metadata; never log ps output (it can contain secrets)."""
    rows = {}
    if sys.platform.startswith("linux"):
        for path in Path("/proc").glob("[0-9]*"):
            try:
                stat = (path / "stat").read_text().rsplit(")", 1)[1].split()
                if stat[0] == "Z":
                    continue
                env = (path / "environ").read_bytes().split(b"\0")
                markers = [v[len(b"MAHLER_RUN_ID="):].decode("ascii") for v in env
                           if v.startswith(b"MAHLER_RUN_ID=")]
                rows[int(path.name)] = dict(
                    pid=int(path.name), ppid=int(stat[1]), uid=path.stat().st_uid,
                    identity=stat[19],
                    command=(path / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                        "utf-8", errors="replace"),
                    run_id=markers[0] if len(markers) == 1 else None)
            except (OSError, ValueError, UnicodeError, IndexError):
                continue
        return rows

    rows = _ps_process_listing()
    expanded = _ps_process_listing(True)
    for pid, row in rows.items():
        other = expanded.get(pid)
        row["run_id"] = None
        # Strip the independently read command before looking for environment
        # entries: a marker mentioned in argv is not a run's environment.
        prefix = row["command"] + " "
        if (other and other["identity"] == row["identity"]
                and other["command"].startswith(prefix)):
            env = other["command"][len(prefix):]
            markers = re.findall(r"(?:^|\s)MAHLER_RUN_ID=([0-9]+)(?=\s|$)", env)
            if len(markers) == 1:
                row["run_id"] = markers[0]
    return rows


def find_run_processes(run_id=None, ppid=None):
    """Find same-user marked processes, excluding ancestors and host services."""
    rows = _process_snapshot()
    if not sys.platform.startswith("linux"):
        _merge_recorded_ownership(rows, run_id)
        _merge_open_marker_ownership(rows, run_id)
    protected = {1, os.getpid(), os.getppid()}
    parent = os.getppid()
    while parent in rows:
        parent = rows[parent]["ppid"]
        if parent in protected:
            break
        protected.add(parent)
    found = []
    for pid, row in rows.items():
        marker = row.get("run_id")
        if (pid in protected or row["uid"] != os.getuid()
                or not marker or not re.fullmatch(r"[0-9]+", marker)
                or (run_id is not None and marker != str(run_id))
                or (ppid is not None and row["ppid"] != ppid)):
            continue
        # Protect host entry points even if launched with an inherited marker.
        command = row["command"]
        if re.search(r"(?:^|[/\s])(?:mahlerd|mahler-launcher)(?:\s|$)", command):
            continue
        if re.search(r"(?:^|[/\s])mahler(?:\.py|\.__main__)?\s+(?:serve|daemon|tick)(?:\s|$)",
                     command):
            continue
        found.append(row)
    return found


def reap_run(run_id, grace_seconds=2.0, *, ppid=None):
    """TERM marked run processes, then KILL survivors after a bounded grace.

    Re-read ownership and start identity before every signal. No unmarked
    process is signalled here; watchdog retains its existing group cleanup.
    ppid restricts the janitor backstop to orphans.
    """
    try:
        targets = {p["pid"]: p for p in find_run_processes(run_id, ppid)}
    except (OSError, ValueError, subprocess.SubprocessError, RuntimeError):
        return
    if not targets:
        _compact_ownership(run_id)
        return
    deadline = None
    termed = set()
    while True:
        try:
            current = find_run_processes(run_id, ppid)
        except (OSError, ValueError, subprocess.SubprocessError, RuntimeError):
            return
        expired = deadline is not None and time.monotonic() >= deadline
        for process in current:
            key = (process["pid"], process["identity"])
            original = targets.setdefault(process["pid"], process)
            if original["identity"] != process["identity"]:
                continue
            sig = signal.SIGKILL if expired and key in termed else signal.SIGTERM
            if sig == signal.SIGTERM and key in termed:
                continue
            try:
                os.kill(process["pid"], sig)
            except (ProcessLookupError, PermissionError):
                pass
            termed.add(key)
        if expired or not current:
            _compact_ownership(run_id)
            return
        if deadline is None:
            deadline = time.monotonic() + max(0, grace_seconds)
        time.sleep(min(.05, max(0, deadline - time.monotonic())))


def exit_code(run):
    try:
        with open(run["status_path"]) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError, TypeError):
        return None


def setup_tail(run, lines=20):
    """The last `lines` lines of the run's setup.log ('' when there isn't one) —
    surfaced in handoffs and `mahler status` when setup failed (issue #8)."""
    if not run["log_path"]:
        return ""
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


def snapshot(repo, wt, run_id, number, base, env=None):
    """Save everything a run left — commits *and* uncommitted/untracked files —
    to refs/heads/mahler/snapshot/<n>-run<id>, without touching the worktree,
    its index, or the stash. `env` is the project's git-hosting account (D25/D26);
    None inherits this machine's own. -> dict or None when there was nothing new."""
    if not os.path.isdir(wt):
        return None
    idx = os.path.join(config.RUNS_DIR, str(run_id), "snapshot.index")
    env = dict(env or os.environ, GIT_INDEX_FILE=idx)
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
        git(wt, "push", "--quiet", "--no-verify", "--force", "origin", f"{sha}:refs/heads/{ref}",
            env=env)
        stat = git(wt, "diff", "--shortstat", f"origin/{base}...{sha}", check=False)
        return {"ref": ref, "sha": sha[:9], "ahead": ahead, "stat": stat}
    finally:
        if os.path.exists(idx):
            os.remove(idx)


def no_work_base(wt, base, env=None):
    """Return the freshly fetched base SHA only for a clean, zero-ahead tree.

    None means work exists; missing trees and failed probes raise GitError,
    never masquerading as proof that a build did no work.
    """
    if not wt or not os.path.isdir(wt):
        raise GitError("no-work probe: missing worktree")
    try:
        ref = f"refs/remotes/origin/{base}"
        fetch_origin(wt, "origin", f"+refs/heads/{base}:{ref}", env=env)
        sha = git(wt, "rev-parse", "--verify", f"{ref}^{{commit}}")
        if git(wt, "status", "--porcelain", "--untracked-files=all"):
            return None
        ahead = int(git(wt, "rev-list", "--count", f"{sha}..HEAD"))
        return sha if ahead == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError) as err:
        raise GitError(f"no-work probe failed: {err}") from err


def worktree_root(pol):
    return os.path.expanduser(pol.get("worktree_root") or config.WORKTREES)


def remove_worktree(repo, wt, branch=None, root=None):
    """`wt` must be *inside* `root` (DESIGN D12: rm -rf never strays outside the
    worktree). Resolve both paths first so neither a sibling sharing the same
    string prefix nor a symlinked ancestor can escape the cleanup boundary."""
    base = os.path.realpath(root or config.WORKTREES)
    candidate = os.path.realpath(wt) if wt else None
    inside = bool(candidate and candidate != base
                  and os.path.commonpath((base, candidate)) == base)
    if wt and os.path.isdir(wt) and inside:
        git(repo, "worktree", "remove", "--force", wt, check=False)
        shutil.rmtree(wt, ignore_errors=True)
    git(repo, "worktree", "prune", check=False)
    if branch:
        git(repo, "branch", "-D", branch, check=False)


def head_sha(wt):
    """The worktree's HEAD sha, or None on any error."""
    try:
        return git(wt, "rev-parse", "HEAD")
    except (GitError, OSError):
        return None


def worktree_dirty(wt):
    """Whether the worktree has uncommitted changes (True if unreadable)."""
    try:
        return bool(git(wt, "status", "--porcelain"))
    except (GitError, OSError):
        return True


def commits_ahead(wt, base):
    """How many commits the worktree's HEAD is ahead of origin/<base>.
    Returns 0 on any error (missing worktree, detached HEAD, etc.)."""
    if not wt or not os.path.isdir(wt):
        return 0
    try:
        return int(git(wt, "rev-list", "--count", f"origin/{base}..HEAD") or 0)
    except (GitError, ValueError):
        return 0


def verify_in_worktree(wt, verify_cmd, timeout=120, output_path=None):
    """Run the project's verify command in the worktree.
    Returns True if it exits 0 within the timeout, False otherwise."""
    if not verify_cmd or not wt or not os.path.isdir(wt):
        return False
    try:
        r = subprocess.run(verify_cmd, shell=True, cwd=wt,
                           capture_output=True, text=True, timeout=timeout)
        output = (r.stdout or "") + (r.stderr or "")
        ok = r.returncode == 0
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError) as err:
        output, ok = str(err), False
    if output_path:
        try:
            with open(output_path, "w") as stream:
                stream.write(output[-16384:])
        except OSError:
            pass
    return ok


# Retain Popen handles until polled so long-lived daemon children are reaped.
_verify_children = {}


def start_verify_in_worktree(wt, verify_cmd, timeout=300, output_path=None):
    """Start an independently bounded verifier; return durable polling metadata.

    The supervisor shares a fresh process group with the command. It publishes
    its result atomically before killing the whole group, including descendants.
    Its timeout still applies if the conductor exits between ticks.
    """
    status_path = output_path + ".exit"
    Path(status_path).unlink(missing_ok=True)
    script = """
import os, signal, subprocess, sys
from pathlib import Path
command, timeout, result = sys.argv[1:]
try:
    code = subprocess.call(command, shell=True, timeout=float(timeout))
except subprocess.TimeoutExpired:
    code = 124
except Exception:
    code = 125
try:
    path = Path(result)
    path.with_suffix('.tmp').write_text(str(code))
    os.replace(path.with_suffix('.tmp'), path)
finally:
    os.killpg(os.getpgrp(), signal.SIGKILL)
"""
    proc = None
    try:
        with open(output_path, "w") as output:
            proc = subprocess.Popen([sys.executable, "-c", script, verify_cmd,
                                     str(timeout), status_path], cwd=wt,
                                    start_new_session=True, stdin=subprocess.DEVNULL,
                                    stdout=output, stderr=subprocess.STDOUT)
        _verify_children[proc.pid] = proc
        return {"pid": proc.pid, "status_path": status_path}
    except BaseException:
        if proc is not None:
            kill(proc.pid)
            proc.wait()
        raise


def stop_verify_in_worktree(record):
    """Kill the verifier's group and reap our child, without a grace-period wait."""
    pid = record.get("pid")
    kill(pid)
    proc = _verify_children.pop(pid, None)
    if proc is not None:
        proc.wait(timeout=5)


def poll_verify_in_worktree(record):
    """None while running; otherwise success/failure, including lost supervisors."""
    pid = record.get("pid")
    proc = _verify_children.get(pid)
    if proc is not None:
        proc.poll()
    try:
        code = int(Path(record["status_path"]).read_text().strip())
    except (OSError, ValueError, KeyError):
        return None if pid and alive(pid) else False
    stop_verify_in_worktree(record)
    return code == 0
