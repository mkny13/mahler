"""Thin wrapper over the `gh` CLI — GitHub is the item store (DESIGN D4).

Mahler clients optionally resolve GitHub App tokens for every command;
other clients preserve their configured `gh auth` identity.
"""

import json
import os
import re
import subprocess
import tempfile
from urllib.parse import quote

from . import config, redact

STATE_LABELS = {
    "inbox": "mahler:inbox", "ready": "mahler:ready", "working": "mahler:working",
    "shipped": "mahler:shipped", "verifying": "mahler:verifying", "needs_you": "mahler:needs-you",
    "parked": "mahler:parked", "failed": "mahler:failed", "parent": "mahler:parent",
}
LABEL_STATES = {v: k for k, v in STATE_LABELS.items()}
LABEL_STATES["mahler:tracking"] = "parent"  # backward compatibility
LABEL_COLORS = {
    "mahler:inbox": "ededed", "mahler:ready": "0e8a16", "mahler:working": "1d76db",
    "mahler:verifying": "00b8d9", "mahler:needs-you": "d93f0b", "mahler:parked": "c5def5",
    "mahler:shipped": "0e8a16", "mahler:failed": "b60205", "mahler:parent": "5319e7",
    "type:bug": "d73a4a", "type:feature": "a2eeef", "type:chore": "fef2c0",
    "type:goal": "7057ff", "type:uat": "fbca04", "type:anomaly": "e99695",
    "size:s": "c2e0c6", "size:m": "bfd4f2", "size:l": "f9d0c4",
    "p1": "b60205", "p2": "fbca04", "p3": "c5def5",
}
PIN_COLOR = "d4c5f9"        # platform:* labels, created on the fly (mahler#20)
AREA_COLOR = "0052cc"       # area:* labels, created on the fly (mahler#197)
AGENT_MARK = "<!-- mahler"          # every Mahler/agent comment starts with this
AGENT_NOTE = "<!-- mahler:agent -->"  # the line Mahler's own comments start with
HELP_FOOTER = "\n\n<sub>[Mahler commands](https://github.com/mkny13/mahler/blob/main/docs/commands.md)</sub>"
_CI_TOOLCHAIN_FILES = {
    ".java-version", ".mise.toml", ".node-version", ".nvmrc", ".python-version",
    ".ruby-version", ".sdkmanrc", ".swift-version", ".tool-versions", ".xcode-version",
    "build.gradle", "build.gradle.kts", "global.json", "gradle.properties",
    "gradle-wrapper.properties", "libs.versions.toml", "mise.toml", "package.json",
    "package.resolved", "project.yml", "project.yaml", "settings.gradle",
    "settings.gradle.kts",
}
# `(?:>\s*)?` tolerates the line living inside a markdown blockquote (a "Part
# of #N" written under a quoted "> **Original request:**" preamble) — without
# it, the reference silently fails to parse and the item never gets linked to
# its parent, so close_finished_parents can never close the parent (found
# 2026-09-14 auditing why mahler#83's tree never auto-closed).
DEPENDS_RE = re.compile(r"^\s*(?:>\s*)?Depends on:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
PART_OF_RE = re.compile(r"^\s*(?:>\s*)?(?:\*{1,2})?Part of:?(?:\*{1,2})?\s*#(\d+)",
                        re.IGNORECASE | re.MULTILINE)
# mahler#210: the `## Plan` section's `Files:` line/list, parsed at every sync
# so the scheduler can block two ready/working items from building at once
# when their planned files intersect — mechanical detection instead of
# relying on the sort agent to notice and hand-label an `area:` collision.
PLAN_SECTION_RE = re.compile(r"^##\s*Plan\s*$(.*?)(?=^##\s|\Z)", re.IGNORECASE | re.MULTILINE | re.DOTALL)
FILES_LABEL_RE = re.compile(r"^[ \t]*(?:[-*][ \t]*)?\*{0,2}Files\b[^:\n]{0,30}:\*{0,2}[ \t]*(.*)$",
                            re.IGNORECASE | re.MULTILINE)
FILE_BULLET_RE = re.compile(r"^\s*[-*]\s*(.+?)\s*$")



def _etag_of(out):
    """The ETag header of a `gh api -i` response, or None.

    `gh api -i` prints the status line, then headers (CRLF-terminated), then
    a blank line, then the body. Only the header block is searched, so a
    body that happens to contain the word "etag" can't fool the parse."""
    head = re.split(r"\r?\n\r?\n", out, maxsplit=1)[0]
    m = re.search(r"^etag:\s*(.+?)\s*$", head, re.IGNORECASE | re.MULTILINE)
    return m.group(1) if m else None


class GHError(RuntimeError):
    pass


def _safe_error(message, env):
    token = (env or {}).get("GH_TOKEN")
    if token:
        message = message.replace(token, redact.MARK)
    return redact.redact(message)


def _gh(*args, input=None, timeout=90, env=None):
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True,
                           input=input, timeout=timeout, env=env)
    except (subprocess.SubprocessError, OSError) as e:
        raise GHError(f"gh {' '.join(args[:3])}: {_safe_error(str(e), env)}") from None
    if r.returncode != 0:
        raise GHError(f"gh {' '.join(args[:3])}: "
                      f"{_safe_error((r.stderr or r.stdout).strip(), env)[:500]}")
    return r.stdout


def _git(path, *args, env=None):
    """Git in a project checkout, for the pushes the conductor owns (D18).
    Goes through the same credential setup as the run's own pushes."""
    try:
        r = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True,
                           timeout=300, env=env)
    except (subprocess.SubprocessError, OSError) as e:
        raise GHError(f"git {' '.join(args[:3])}: {_safe_error(str(e), env)}") from None
    if r.returncode != 0:
        raise GHError(f"git {' '.join(args[:3])}: "
                      f"{_safe_error((r.stderr or r.stdout).strip(), env)[:400]}")
    return r.stdout.strip()


def project_client(cfg, pol):
    """Use one project identity for conductor, interactive CLI and MCP calls."""
    from . import github_app

    settings = config.github_app_settings(cfg, pol)
    app = (github_app.Installation(*settings,
           cache_dir=os.path.join(config.STATE, "github-app-tokens")) if settings else None)
    return GH(pol["repo"], env=config.run_env(cfg, config.gh_account_of(pol)), app=app)


class GH:
    def __init__(self, repo, env=None, app=None):
        self.app = app
        self.repo = repo
        self.env = env      # the project's account env (DESIGN D25); None = inherit
        self._relationships = {}

    def _env(self, git=False):
        if self.app is None:
            return self.env
        env = dict(os.environ if self.env is None else self.env)
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
        env["GH_HOST"] = "github.com"
        env["GH_TOKEN"] = self.app.token()
        if git:
            # Pin origin to HTTPS and clear saved helpers: SSH and keychains
            # otherwise bypass GH_TOKEN. Nothing persists in the checkout.
            entries = [
                ("credential.helper", ""),
                ("credential.https://github.com.helper", ""),
                ("credential.https://github.com.helper", "!gh auth git-credential"),
            ]
            start = int(env.get("GIT_CONFIG_COUNT", "0"))
            for i, (key, value) in enumerate(entries, start):
                env[f"GIT_CONFIG_KEY_{i}"] = key
                env[f"GIT_CONFIG_VALUE_{i}"] = value
            env["GIT_CONFIG_COUNT"] = str(start + len(entries))
            env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    def _gh(self, *args, **kw):
        return _gh(*args, env=self._env(), **kw)

    def _git_args(self, args):
        if self.app and args and args[0] in ("fetch", "push", "ls-remote"):
            return tuple(f"https://github.com/{self.repo}.git" if arg == "origin" else arg
                         for arg in args)
        return args

    def _git(self, path, *args):
        # D38: git fetch/push use the account's own git credentials, not the
        # GitHub App token used for API writes.
        return _git(path, *self._git_args(args), env=self._git_env())

    def _git_env(self):
        if self.app is None:
            return self.env
        # Keep the configured account identity for git fetch/push (including
        # account-scoped GH_TOKEN/GITHUB_TOKEN) while still avoiding App-token
        # minting here: only _env() resolves self.app.token().
        return dict(os.environ if self.env is None else self.env)

    def open_issues(self):
        # gh paginates up to --limit. Grow until the result proves exhaustion,
        # retaining its normalized issue/comment and dependency fields.
        limit = 300
        while True:
            out = self._gh("issue", "list", "-R", self.repo, "--state", "open",
                           "--limit", str(limit), "--json",
                           "number,title,labels,body,createdAt,updatedAt,comments,url,"
                           "blocking,blockedBy")
            issues = json.loads(out)
            if len(issues) < limit:
                break
            limit *= 2
        self._relationships = {
            issue["number"]: {
                "blocking": _relationship_numbers(issue.get("blocking")),
                "blockedBy": _relationship_numbers(issue.get("blockedBy")),
            }
            for issue in issues
        }
        return issues

    def _relationships_of(self, number):
        """Both native GitHub dependency directions for one issue.

        A full sync gets these fields in open_issues(), so this normally costs
        no extra request. Direct callers still get a current issue view.
        """
        if number not in self._relationships:
            out = self._gh("issue", "view", str(number), "-R", self.repo,
                           "--json", "blocking,blockedBy")
            issue = json.loads(out)
            return {
                "blocking": _relationship_numbers(issue.get("blocking")),
                "blockedBy": _relationship_numbers(issue.get("blockedBy")),
            }
        return self._relationships[number]

    def blocking_of(self, number):
        """Issue numbers that ``number`` blocks through GitHub's native relation."""
        return list(self._relationships_of(number)["blocking"])

    def blocked_by_of(self, number):
        """Issue numbers that natively block ``number`` on GitHub."""
        return list(self._relationships_of(number)["blockedBy"])

    def issues_changed(self, etag=None):
        """Conditional probe of the open-issue collection (mahler#90).

        One `gh api -i` GET with If-None-Match instead of the full `gh issue
        list`: a 304 doesn't count against the REST rate limit, so a quiet
        repo costs ~nothing per tick, and sync() skips the fetch (and the
        per-item closed checks) entirely. `sort=updated` makes the ETag a
        change-detector for the whole collection — any issue update re-sorts
        it to the top of page 1, changing the body and with it the ETag.

        Returns (changed, etag): False → keep polling with the same etag;
        True → the new etag to store (None when it couldn't be read, which
        just means the next tick probes with the old one and re-fetches).
        `gh api` treats a 304 as an error (exit 1, "gh: HTTP 304"), so that
        is caught here rather than surfacing as a failed sync."""
        args = ["api", "-i",
                f"repos/{self.repo}/issues?state=open&sort=updated&direction=desc&per_page=1"]
        if etag:
            args += ["-H", f"If-None-Match: {etag}"]
        try:
            out = self._gh(*args, timeout=30)
        except GHError as e:
            if "HTTP 304" in str(e):
                return False, etag
            raise
        return True, _etag_of(out)

    def issue_labels(self, number):
        out = self._gh("issue", "view", str(number), "-R", self.repo, "--json", "labels")
        return label_names(json.loads(out))

    def issue_state(self, number):
        out = self._gh("issue", "view", str(number), "-R", self.repo, "--json", "state")
        return json.loads(out)["state"]          # OPEN | CLOSED

    def issue_comments(self, number):
        """Fetch every comment, retaining GitHub attribution and durable identity."""
        pages = json.loads(self._gh(
            "api", f"repos/{self.repo}/issues/{number}/comments?per_page=100",
            "--paginate", "--slurp"))
        return [{"body": c.get("body", ""), "createdAt": c.get("created_at"),
                 "author": c.get("user"), "authorAssociation": c.get("author_association"),
                 "id": c.get("id"), "url": c.get("html_url")}
                for page in pages for c in page]

    def comment(self, number, body, *, agent=True):
        """Console answers are human replies; all conductor comments stay marked."""
        if agent:
            if not body.startswith(AGENT_MARK):
                body = AGENT_NOTE + "\n" + body
            if not body.endswith(HELP_FOOTER):
                body += HELP_FOOTER
        self._gh("issue", "comment", str(number), "-R", self.repo, "--body-file", "-", input=body)

    def set_state_label(self, number, state, current_labels):
        want = STATE_LABELS.get(state)
        drop = [l for l in current_labels if l in LABEL_STATES and l != want]
        args = ["issue", "edit", str(number), "-R", self.repo]
        for l in drop:
            args += ["--remove-label", l]
        if want and want not in current_labels:
            if state == "shipped":  # Existing projects predate this lifecycle label.
                self._gh("label", "create", want, "-R", self.repo,
                         "--color", LABEL_COLORS[want], "--force")
            args += ["--add-label", want]
        if len(args) > 5:
            self._gh(*args)

    def set_pin_labels(self, number, want, current_labels):
        """`platform:*` labels are the pin's store of record (mahler#20): sync()
        re-derives pin=pin_of(labels) every tick, so a pin change must land here.
        Keeps exactly the label `want` names, or none at all; skips the gh call
        when nothing would change."""
        keep = f"platform:{want}" if want else None
        drop = [l for l in current_labels if l.startswith("platform:") and l != keep]
        args = ["issue", "edit", str(number), "-R", self.repo]
        for l in drop:
            args += ["--remove-label", l]
        if keep and keep not in current_labels:
            # --add-label fails on a label the repo doesn't have yet; --force
            # makes this idempotent (an existing label keeps its color).
            self._gh("label", "create", keep, "-R", self.repo, "--color", PIN_COLOR, "--force")
            args += ["--add-label", keep]
        if len(args) > 5:
            self._gh(*args)

    def add_label(self, number, label):
        self._gh("issue", "edit", str(number), "-R", self.repo, "--add-label", label)

    def ensure_labels(self):
        for name, color in LABEL_COLORS.items():
            self._gh("label", "create", name, "-R", self.repo, "--color", color, "--force")

    def ensure_pass_label(self, pass_name):
        label = f"pass:{pass_name}"
        self._gh("label", "create", label, "-R", self.repo, "--color", PIN_COLOR, "--force")

    def ensure_area_label(self, area_name):
        label = f"area:{area_name}"
        self._gh("label", "create", label, "-R", self.repo, "--color", AREA_COLOR, "--force")

    def issue_by_marker(self, marker):
        """Scan all issue bodies, including closed issues, without search-index lag."""
        pages = json.loads(self._gh("api", "--paginate", "--slurp",
            f"repos/{self.repo}/issues?state=all&per_page=100"))
        for page in pages:
            for issue in page:
                if "pull_request" not in issue and marker in (issue.get("body") or ""):
                    return issue["html_url"]
        return None

    def create_issue(self, title, body="", labels=()):
        args = ["issue", "create", "-R", self.repo, "--title", title, "--body-file", "-"]
        for l in labels:
            args += ["--label", l]
        return self._gh(*args, input=body).strip()

    def edit_issue_body(self, number, body):
        self._gh("issue", "edit", str(number), "-R", self.repo,
                 "--body-file", "-", input=body)

    def default_branch_settings(self):
        """Read merge policy and the default branch; never change settings."""
        return json.loads(self._gh("api", f"repos/{self.repo}"))

    def branch_protection(self, branch):
        """Unavailable/unsupported protection raises GHError, not a clean pass."""
        return json.loads(self._gh(
            "api", f"repos/{self.repo}/branches/{quote(branch, safe='')}/protection"))

    def branch_rules(self, branch):
        """Effective ruleset rules, which can supplement classic protection."""
        return json.loads(self._gh(
            "api", f"repos/{self.repo}/rules/branches/{quote(branch, safe='')}"))

    # ---------- the conductor ships (DESIGN D18) ----------

    def issue_body(self, number):
        return json.loads(self._gh("issue", "view", str(number), "-R", self.repo,
                                   "--json", "body")).get("body") or ""

    def push_branch(self, path, branch, ref):
        """Make origin's `branch` point at the saved work `ref` (D18).

        `ref` is an already-pushed ref (a mahler/snapshot/* branch). No-op when
        `branch` is already there. Returns the sha now at `branch`."""
        if re.fullmatch(r"mahler/revert-\d+", ref):
            # Console reverts are prepared locally by the tick; only the
            # conductor pushes them, after acquiring its lease.
            sha = self._git(path, "rev-parse", "--verify", f"refs/heads/{ref}^{{commit}}")
        else:
            self._git(path, "fetch", "--quiet", "--force", "origin", f"refs/heads/{ref}")
            sha = self._git(path, "rev-parse", "FETCH_HEAD")
        out = subprocess.run(["git", "-C", path, *self._git_args(("ls-remote", "origin",
                              f"refs/heads/{branch}"))], capture_output=True, text=True,
                             timeout=90, env=self._git_env())
        if out.returncode == 0 and out.stdout.strip().startswith(sha):
            return sha
        self._git(path, "push", "--quiet", "--no-verify", "--force", "origin",
                  f"{sha}:refs/heads/{branch}")
        return sha

    def pr_for_head(self, head):
        """An open PR already made from `head`, or None (idempotent PR opening)."""
        out = self._gh("pr", "list", "-R", self.repo, "--head", head, "--state", "open",
                       "--limit", "1", "--json", "number")
        prs = json.loads(out)
        return prs[0]["number"] if prs else None

    def pr_create(self, head, base, title, body):
        out = self._gh("pr", "create", "-R", self.repo, "--head", head, "--base", base,
                       "--title", title, "--body-file", "-", input=body)
        m = re.search(r"/pull/(\d+)", out)
        if not m:
            raise GHError(f"gh pr create: no PR url in {out.strip()[:200]!r}")
        return int(m.group(1))

    def pr_refresh_mergeability(self, number):
        """Request GitHub's on-demand mergeability computation."""
        self._gh("api", "--method", "GET", f"repos/{self.repo}/pulls/{number}")

    def pr_view(self, number):
        return json.loads(self._gh("pr", "view", str(number), "-R", self.repo, "--json",
                                   "state,body,statusCheckRollup,mergeable,headRefName,"
                                   "headRefOid,baseRefName,mergeCommit,title,mergedAt"))

    def exact_preview(self, sha, environment, *, timeout=5):
        """Two bounded lookups; incomplete/ambiguous observations never prove live.

        Do not filter the first lookup by SHA: that hides a newer deployment.
        """
        from urllib.parse import urlencode, urlsplit

        if (not re.fullmatch(r"[0-9a-f]{40}", sha) or not environment
                or environment.strip().casefold() in ("prod", "production")):
            return None
        query = urlencode({"environment": environment, "per_page": 100})
        deployments = json.loads(self._gh(
            "api", "--method", "GET", f"repos/{self.repo}/deployments?{query}", timeout=timeout))
        if not isinstance(deployments, list) or not deployments:
            return None
        if any(not isinstance(d, dict) or type(d.get("id")) is not int
               or d.get("environment") != environment for d in deployments):
            return None
        matches = [d for d in deployments if d.get("sha") == sha]
        if len(matches) != 1:
            return None
        deployment = matches[0]
        if (deployment["id"] != max(d["id"] for d in deployments)
                or deployment.get("production_environment") is not False):
            return None
        repository_url = f"https://api.github.com/repos/{self.repo}"
        if deployment.get("repository_url", repository_url) != repository_url:
            return None
        statuses = json.loads(self._gh(
            "api", "--method", "GET",
            f"repos/{self.repo}/deployments/{deployment['id']}/statuses?per_page=100",
            timeout=timeout))
        if not isinstance(statuses, list) or not statuses:
            return None
        if any(not isinstance(s, dict) or type(s.get("id")) is not int for s in statuses):
            return None
        latest_id = max(s["id"] for s in statuses)
        latest = [s for s in statuses if s["id"] == latest_id]
        if len(latest) != 1:
            return None
        status = latest[0]
        if status.get("state") != "success" or status.get("environment", environment) != environment:
            return None
        url = status.get("environment_url") or ""
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or any(ord(c) <= 32 for c in url) or "\\" in url or "@" in parsed.netloc):
            return None
        return url

    def screenshot_preview(self, sha, environment):
        """Bounded exact-head preview discovery; never infer a production target."""
        from urllib.parse import urlencode, urlsplit

        if not environment or environment.casefold() == "production":
            return None
        query = urlencode({"sha": sha, "environment": environment, "per_page": 20})
        deployments = json.loads(self._gh(
            "api", "--method", "GET", f"repos/{self.repo}/deployments?{query}"))
        for deployment in sorted(deployments[:20], key=lambda d: d["id"], reverse=True):
            if (deployment.get("sha") != sha or deployment.get("environment") != environment
                    or deployment.get("production_environment") is not False):
                continue
            statuses = json.loads(self._gh(
                "api", "--method", "GET",
                f"repos/{self.repo}/deployments/{int(deployment['id'])}/statuses?per_page=1"))
            if not statuses or statuses[0].get("state") != "success":
                continue
            status = statuses[0]
            if status.get("environment", environment) != environment:
                continue
            url = status.get("environment_url") or ""
            parsed = urlsplit(url)
            if (parsed.scheme == "https" and parsed.hostname and not parsed.username
                    and not parsed.password and not any(c.isspace() for c in url)
                    and "\\" not in url and "@" not in parsed.netloc):
                return url
        return None

    def _api_json(self, method, path, payload=None):
        args = ["api", "--method", method, f"repos/{self.repo}/{path}"]
        if payload is not None:
            args += ["--input", "-"]
        out = self._gh(*args, input=json.dumps(payload) if payload is not None else None)
        return json.loads(out) if out.strip() else {}

    def publish_artifacts(self, branch, directory, files, message):
        """Commit `files` ({path: bytes}) under `directory` on the artifact `branch`.

        Git Database API only: blobs, tree, commit, then a non-force ref update.
        A missing branch becomes an orphan root holding only these files. Never
        touches any other ref. Returns the commit SHA. A published directory is
        reused rather than rewritten. Ref races raise GHError (advisory).
        """
        import base64

        if branch in ("main", "master") or not re.fullmatch(r"[A-Za-z0-9._-]+", branch or ""):
            raise GHError("artifacts: unsafe branch")
        ref = f"git/ref/heads/{quote(branch)}"
        try:
            parent = self._api_json("GET", ref)["object"]["sha"]
        except GHError as e:
            if "404" not in str(e) and "Not Found" not in str(e):
                raise
            parent = None
        base_tree = None
        if parent:
            commit = self._api_json("GET", f"git/commits/{parent}")
            base_tree = commit["tree"]["sha"]
            listing = self._api_json("GET", f"git/trees/{base_tree}?recursive=1")
            if any(t.get("path", "").startswith(directory + "/") for t in listing.get("tree", [])):
                return parent
        entries = []
        for name, data in files.items():
            blob = self._api_json("POST", "git/blobs", {
                "content": base64.b64encode(data).decode("ascii"), "encoding": "base64"})
            entries.append({"path": f"{directory}/{name}", "mode": "100644",
                            "type": "blob", "sha": blob["sha"]})
        tree = self._api_json("POST", "git/trees", {
            **({"base_tree": base_tree} if base_tree else {}), "tree": entries})
        commit = self._api_json("POST", "git/commits", {
            "message": message, "tree": tree["sha"], "parents": [parent] if parent else []})
        if parent:
            self._api_json("PATCH", f"git/refs/heads/{quote(branch)}",
                           {"sha": commit["sha"], "force": False})
        else:
            self._api_json("POST", "git/refs",
                           {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
        return commit["sha"]

    def artifact_url(self, commit, path):
        return f"https://github.com/{self.repo}/blob/{commit}/{quote(path)}"

    def find_comment(self, number, marker):
        """Id of the newest agent comment containing `marker`, or None."""
        for c in reversed(self.issue_comments(number)):
            if marker in (c.get("body") or "") and (c.get("body") or "").startswith(AGENT_MARK):
                return c["id"]
        return None

    def comment_create(self, number, body):
        return self._api_json("POST", f"issues/{int(number)}/comments", {"body": body})["id"]

    def comment_edit(self, comment_id, body):
        self._api_json("PATCH", f"issues/comments/{int(comment_id)}", {"body": body})

    def pr_edit_body(self, number, body):
        self._gh("pr", "edit", str(number), "-R", self.repo, "--body-file", "-", input=body)

    def open_prs(self, limit=50):
        """Open PRs, for the unowned-PR check (mahler#407)."""
        return json.loads(self._gh("pr", "list", "-R", self.repo, "--state", "open",
                                   "--limit", str(limit), "--json",
                                   "number,title,headRefName,createdAt,isDraft"))

    def dependency_pr_view(self, number):
        """Bot identity, complete update description, files and exact merge gates."""
        return json.loads(self._gh("pr", "view", str(number), "-R", self.repo, "--json",
            "number,url,title,body,author,isCrossRepository,isDraft,state,headRefName,"
            "headRefOid,baseRefName,mergeable,statusCheckRollup,mergedAt,files"))

    def pr_merge_info(self, number):
        return json.loads(self._gh("pr", "view", str(number), "-R", self.repo, "--json",
                                   "state,mergeCommit,title,baseRefName,mergedAt"))

    def pr_for_commit(self, sha):
        """Find the pull request that merged a commit SHA."""
        try:
            out = self._gh("api", f"repos/{self.repo}/commits/{sha}/pulls")
            prs = json.loads(out or "[]")
            for pr in prs:
                if pr.get("merged_at"):
                    return pr.get("number")
            if prs and prs[0].get("number"):
                return prs[0]["number"]
        except (GHError, json.JSONDecodeError, KeyError, IndexError):
            pass
        return None

    def base_in_head(self, path, base, head):
        """Fetch exact objects without changing working files; prove ancestry.

        False is proven drift. Any failed/indeterminate operation raises GHError.
        Fetch the target last, with an explicit refspec independent of fetch config.
        """
        if not isinstance(head, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", head):
            raise GHError("freshness: missing or invalid PR head SHA")
        if not isinstance(base, str) or not base:
            raise GHError("freshness: missing PR target branch")
        self._git(path, "check-ref-format", f"refs/heads/{base}")
        self._git(path, "fetch", "--quiet", "--no-tags", "origin", head)
        self._git(path, "fetch", "--quiet", "--no-tags", "origin",
                  f"+refs/heads/{base}:refs/remotes/origin/{base}")
        tip = self._git(path, "rev-parse", "--verify", f"refs/remotes/origin/{base}^{{commit}}")
        # Shallow history cannot prove a negative ancestry result.
        if self._git(path, "rev-parse", "--is-shallow-repository") != "false":
            raise GHError("freshness: use a complete checkout to prove base ancestry")
        try:
            result = subprocess.run(
                ["git", "-C", path, "merge-base", "--is-ancestor", tip, head],
                capture_output=True, text=True, timeout=90, env=self._git_env())
        except (subprocess.SubprocessError, OSError) as e:
            raise GHError("freshness: ancestry check failed") from None
        if result.returncode not in (0, 1):
            raise GHError("freshness: ancestry check failed: " +
                          redact.redact(result.stderr.strip())[:400])
        return result.returncode == 0

    def update_reviewed_branch(self, path, branch, base, head, authorize):
        """Merge current target into the reviewed head in an isolated checkout.

        Return (new SHA, identical patch), or None for a proven conflict.
        Errors never change the source checkout or discard the remote branch.
        """
        if not re.fullmatch(r"[0-9a-fA-F]{40}", head or ""):
            raise GHError("update: invalid reviewed head")
        if not branch or not base or branch == base:
            raise GHError("update: missing or unsafe PR branches")
        for ref in (branch, base):
            self._git(path, "check-ref-format", f"refs/heads/{ref}")
        remote = self._git(path, "remote", "get-url", "origin")
        with tempfile.TemporaryDirectory(prefix="mahler-pr-update-") as wt:
            self._git(wt, "init", "--quiet")
            self._git(wt, "remote", "add", "origin", remote)
            for ref in (branch, base):
                self._git(wt, "fetch", "--quiet", "--no-tags", "origin",
                          f"refs/heads/{ref}:refs/remotes/origin/{ref}")
            actual = self._git(wt, "rev-parse", f"refs/remotes/origin/{branch}")
            if actual != head:
                raise GHError("update: PR head changed")
            target = self._git(wt, "rev-parse", f"refs/remotes/origin/{base}")
            old_base = self._git(wt, "merge-base", head, target)
            self._git(wt, "checkout", "--quiet", "--detach", head)
            try:
                self._git(wt, "-c", "user.name=Mahler", "-c", "user.email=mahler@localhost",
                          "-c", "commit.gpgSign=false", "merge", "--no-edit", "--no-verify", target)
            except GHError:
                if self._git(wt, "ls-files", "--unmerged"):
                    return None
                raise
            updated = self._git(wt, "rev-parse", "HEAD")
            def patch(parent, tip):
                return self._git(wt, "diff", "--no-ext-diff", "--no-textconv",
                                 "--binary", "--full-index", parent, tip, "--")
            unchanged = patch(old_base, head) == patch(target, updated)
            if not authorize():
                raise GHError("update: PR or lease changed before push")
            self._git(wt, "push", "--quiet",
                      f"--force-with-lease=refs/heads/{branch}:{head}",
                      "origin", f"{updated}:refs/heads/{branch}")
            return updated, unchanged

    def source_line(self, path, sha, file, line):
        """The text of `file` line `line` at commit `sha`, or None if it can't
        be read (unknown object, missing file, line out of range)."""
        try:
            self._git(path, "fetch", "--quiet", "--no-tags", "origin", sha)
        except GHError:
            pass                      # the object may already be local
        try:
            text = self._git(path, "show", f"{sha}:{file}")
        except GHError:
            return None
        lines = text.splitlines()
        return lines[line - 1] if 0 < line <= len(lines) else None

    def pr_merge(self, number, head):
        self._gh("pr", "merge", str(number), "-R", self.repo, "--squash", "--delete-branch",
                 "--match-head-commit", head)

    def failed_run_log(self, branch, tail=150):
        """The latest failed CI run on a branch: (run id, tail of its failing
        log) — (None, '') when no failed run is there. This is what a fix
        run's prompt diagnoses from (DESIGN D18, mahler#18)."""
        out = self._gh("run", "list", "-R", self.repo, "--branch", branch, "--status", "failure",
                       "--limit", "1", "--json", "databaseId")
        runs = json.loads(out or "[]")
        if not runs:
            return None, ""
        run_id = runs[0]["databaseId"]
        log = self._gh("run", "view", str(run_id), "-R", self.repo, "--log-failed", timeout=300)
        return run_id, "\n".join(log.splitlines()[-tail:])

    def pr_ci_toolchain_files(self, number):
        """Changed PR paths that can alter CI or its toolchain."""
        out = self._gh("pr", "diff", str(number), "-R", self.repo, "--name-only")
        paths = []
        for path in out.splitlines():
            path = path.strip()
            if not path:
                continue
            normalized = path.removeprefix("./")
            name = normalized.rsplit("/", 1)[-1].lower()
            lockfile = ("lockfile" in name or name.endswith((".lock", ".lockb")) or
                        "-lock." in name)
            if (normalized.startswith(".github/workflows/") or
                    name in _CI_TOOLCHAIN_FILES or lockfile):
                paths.append(path)
        return paths

    def close_issue(self, number, comment=None):
        """Close an issue, optionally with a comment."""
        args = ["issue", "close", str(number), "-R", self.repo]
        if comment:
            args += ["--comment", comment]
        self._gh(*args)

    def branch_sha(self, branch):
        """The current remote commit SHA of `branch`."""
        out = self._gh("api", f"repos/{self.repo}/commits/{branch}")
        data = json.loads(out)
        return data["sha"]

    def get_release(self, tag):
        """Information about a GitHub release for `tag`, or None if not found."""
        try:
            out = self._gh("release", "view", tag, "-R", self.repo,
                           "--json", "tagName,targetCommitish,body,url")
            return json.loads(out)
        except GHError as e:
            msg = str(e).lower()
            if "release not found" in msg or "not found" in msg or "404" in msg:
                return None
            raise

    def latest_release(self):
        """Latest published GitHub release with its tag resolved to a commit SHA."""
        try:
            out = self._gh("release", "view", "-R", self.repo,
                           "--json", "tagName,targetCommitish,body,url,publishedAt")
        except GHError as e:
            msg = str(e).lower()
            if "release not found" in msg or "no releases" in msg or "not found" in msg or "404" in msg:
                return None
            raise
        release = json.loads(out)
        tag = release.get("tagName")
        if not tag:
            return None
        release["checkpointSha"] = self.get_tag_sha(tag)
        return release

    def get_tag_sha(self, tag):
        """The commit SHA pointed to by git tag, or None if tag does not exist."""
        try:
            out = self._gh("api", f"repos/{self.repo}/git/ref/tags/{tag}")
            data = json.loads(out)
            obj = data.get("object", {})
            if obj.get("type") == "commit":
                return obj.get("sha")
            elif obj.get("type") == "tag":
                tag_obj = json.loads(self._gh("api", f"repos/{self.repo}/git/tags/{obj.get('sha')}"))
                return tag_obj.get("object", {}).get("sha")
            return obj.get("sha")
        except GHError as e:
            msg = str(e).lower()
            if "not found" in msg or "404" in msg:
                return None
            raise

    def release_create(self, tag, target, title, notes):
        """Create a GitHub release and git tag pointing to `target`.
        Returns the release URL."""
        out = self._gh("release", "create", tag, "-R", self.repo,
                       "--target", target, "--title", title,
                       "--notes-file", "-", input=notes)
        url = out.strip()
        if not url.startswith("http"):
            rel = self.get_release(tag)
            if rel and rel.get("url"):
                url = rel["url"]
            else:
                url = f"https://github.com/{self.repo}/releases/tag/{tag}"
        return url


def label_names(issue):
    return [l["name"] if isinstance(l, dict) else l for l in issue.get("labels", [])]


def _relationship_numbers(relationship):
    """Normalize gh's ``{nodes: [{number: N}]}`` relationship shape."""
    numbers = []
    for node in (relationship or {}).get("nodes", []):
        number = node.get("number") if isinstance(node, dict) else None
        if isinstance(number, int) and not isinstance(number, bool) and number not in numbers:
            numbers.append(number)
    return numbers


def priority_of(labels):
    for p in ("p1", "p2", "p3"):
        if p in labels:
            return int(p[1])
    return 2


def pin_of(labels):
    for l in labels:
        if l.startswith("platform:"):
            return l.split(":", 1)[1]
    return None


def depends_of(body):
    """Keep local issue numbers as ints; qualified refs are JSON-safe objects."""
    deps = []
    for m in DEPENDS_RE.finditer(body or ""):
        for ref in re.finditer(
                r"(?<![\w./#-])(?P<repo>[\w.-]+(?:/[\w.-]+)?)?"
                r"#(?P<number>\d+)(?!\w)", m.group(1)):
            number = int(ref["number"])
            deps.append({"repo": ref["repo"], "number": number}
                        if ref["repo"] else number)
    return deps


def dependency_target(dep, project, projects):
    """Resolve only against enabled config, never guess a qualified ref's owner."""
    if isinstance(dep, int):
        return project, dep
    repo = dep["repo"].casefold()
    matches = [p["name"] for p in projects if p.get("enabled") and
               (p["repo"].casefold() if "/" in repo else
                p["repo"].rsplit("/", 1)[-1].casefold()) == repo]
    return (matches[0], dep["number"]) if len(matches) == 1 else None


def dependency_ref(dep, project):
    """Human-readable hold diagnostics, preserving the authored qualifier."""
    return (f"{project}#{dep}" if isinstance(dep, int) else
            f"{dep['repo']}#{dep['number']}")


def part_of(body):
    """Parent issue number if body has 'Part of #N' (case-insensitive), or None."""
    m = PART_OF_RE.search(body or "")
    return int(m.group(1)) if m else None


def files_of(body):
    """The planned file list from the issue's `## Plan` section (mahler#210):
    a `Files:` line naming paths inline, comma-separated, or a bullet list
    under a bare `Files:` line — whichever the sort agent wrote. [] if the
    section or line is missing, so an old-shaped or hand-written body just
    never blocks anything on file overlap."""
    section = PLAN_SECTION_RE.search(body or "")
    if not section:
        return []
    label = FILES_LABEL_RE.search(section.group(1))
    if not label:
        return []
    inline = label.group(1).strip()
    if inline:
        lines = section.group(1)[label.end():].splitlines()
        if lines:
            lines = lines[1:]
        consumed = 0
        for line in lines:
            line = line.strip()
            if not line:
                break
            if not inline.endswith(","):
                break
            inline += " " + line
            consumed += 1
            if consumed >= 10:
                break
        raw = inline.split(",")
    else:
        raw = []
        for line in section.group(1)[label.end():].splitlines():
            if not line.strip():
                if raw:
                    break
                continue
            bullet = FILE_BULLET_RE.match(line)
            if not bullet:
                break
            raw.append(bullet.group(1))
    files = []
    for tok in raw:
        tok = tok.strip().strip("\"'*")
        if "`" in tok:
            # A path may have a trailing explanation, but prose mentioning a
            # path is not a file declaration (mahler#230).
            quoted = re.match(r"^`([^`]+)`", tok)
            if not quoted:
                continue
            tok = quoted.group(1)
        if (tok and tok.lower() not in ("none", "n/a")
                and not any(c.isspace() for c in tok)
                and "`" not in tok and ("/" in tok or "." in tok)):
            files.append(tok)
    return files


def has_sections(body, *headings):
    """True when the body contains every requested markdown heading."""
    wanted = {re.sub(r"^#{1,6}\s*", "", heading).strip().lower()
              for heading in headings}
    found = set()
    for line in (body or "").splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*$", line, re.IGNORECASE)
        if match:
            found.add(match.group(1).strip().lower())
    return found.issuperset(wanted)



COMMAND_RE = re.compile(r"^\s*/mahler\s+(go|park|platform)\b\s*(\S*)", re.IGNORECASE | re.MULTILINE)


def parse_command(body):
    """`/mahler go` · `/mahler park` · `/mahler platform agy-gemini` — or None."""
    m = COMMAND_RE.search(body or "")
    if not m:
        return None
    return m.group(1).lower(), (m.group(2) or None)


# ---------- PR bodies and CI states (DESIGN D18) ----------

def needs_human_of(body):
    """The issue's 'Needs a human to check' section, verbatim (or empty)."""
    body = (body or "")
    if body.endswith(HELP_FOOTER):
        body = body[:-len(HELP_FOOTER)]
    out, grab = [], False
    for line in body.splitlines():
        h = re.match(r"^(#{1,6})\s+(.*?)\s*$", line)
        if h:
            grab = h.group(2).strip().lower() == "needs a human to check"
            continue
        if grab:
            out.append(line)
    
    text = "\n".join(out).strip()
    cmp_text = re.sub(r"^[-*]\s*", "", text).strip().lower()
    if cmp_text in ("nothing", "nothing.", "nothing to check", "none", "n/a"):
        return ""
    if cmp_text.startswith("nothing known yet"):
        return ""
        
    return text


def pr_body(number, summary, needs="", unconfirmed=False):
    """The PR body the conductor opens with: `Fixes #N`, the agent's one-line
    summary, and the issue's 'Needs a human to check' list."""
    lines = [AGENT_NOTE, f"Fixes #{number}", "", summary or "", ""]
    if unconfirmed:
        lines += ["> **Note:** The agent did not end with a STATUS line confirming "
                  "it had finished.  This PR was opened because the branch has commits "
                  "ahead of base and the project's verify passed.", ""]
    if needs:
        lines += ["## Needs a human to check", needs, ""]
    
    body = "\n".join(lines)
    if not body.endswith(HELP_FOOTER):
        body += HELP_FOOTER
    return body


def pr_summary_of(body):
    """The agent summary out of a PR body the conductor wrote."""
    body = (body or "")
    if body.endswith(HELP_FOOTER):
        body = body[:-len(HELP_FOOTER)]
    m = re.search(r"^Fixes #\d+\s*$", body, re.MULTILINE)
    if not m:
        return ""
    out = []
    for line in body[m.end():].splitlines():
        if line.startswith("#"):
            break
        if line.strip():
            out.append(line.strip())
        elif out:
            break
    return " ".join(out).strip()


_BAD_CHECKS = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "STARTUP_FAILURE", "STALE"}
_WAIT_CHECKS = {"PENDING", "QUEUED", "IN_PROGRESS", "REQUESTED", "WAITING", "EXPECTED"}


def checks_state(rollup):
    """A PR's statusCheckRollup -> green | pending | red | none."""
    states = [(c.get("conclusion") or c.get("state") or c.get("status") or "").upper()
              for c in (rollup or [])]
    if not states:
        return "none"                       # no CI configured: nothing to wait for
    if any(s in _BAD_CHECKS for s in states):
        return "red"
    if any(s in _WAIT_CHECKS or s not in {"SUCCESS", "NEUTRAL", "SKIPPED"}
           for s in states):
        return "pending"
    return "green"


UAT_PASS_COMMENT = "✅ **UAT passed** (from the console)."


def completion_evidence(comment, shipped_at):
    """D10's explicit, attributable evidence grammar; ordinary prose is inert."""
    from .ledger import parse

    author = (comment.get("author") or {}).get("login")
    source = comment.get("url") or comment.get("id")
    try:
        created = parse(comment.get("createdAt"))
        merged = parse(shipped_at)
        if not created or not merged or created <= merged:
            return None
    except (ValueError, TypeError):
        return None
    if not author or not source:
        return None
    body = (comment.get("body") or "").strip()
    if body == UAT_PASS_COMMENT:
        kind = "console"
    elif (comment.get("authorAssociation") == "OWNER"
          and re.fullmatch(r"Verified:\s*\S[^\n]*", body)):
        kind = "owner"
    elif re.fullmatch(
            r"Smoke: PASS[ \t]+(?:https?://\S+|(?:report|tag|artifact)[=:][ \t]*\S+)", body):
        kind = "smoke"
    else:
        return None
    return dict(source=str(source), author=author, created_at=comment["createdAt"],
                kind=kind, body=body)
