"""Thin wrapper over the `gh` CLI — GitHub is the item store (DESIGN D4).

Every call goes through `gh`, so auth is whatever `gh auth` already has
(keyring, https + gh as git credential helper — works under launchd).
"""

import json
import re
import subprocess

from . import redact

STATE_LABELS = {
    "inbox": "mahler:inbox", "ready": "mahler:ready", "working": "mahler:working",
    "verifying": "mahler:verifying", "needs_you": "mahler:needs-you",
    "parked": "mahler:parked", "failed": "mahler:failed", "parent": "mahler:parent",
}
LABEL_STATES = {v: k for k, v in STATE_LABELS.items()}
LABEL_STATES["mahler:tracking"] = "parent"  # backward compatibility
LABEL_COLORS = {
    "mahler:inbox": "ededed", "mahler:ready": "0e8a16", "mahler:working": "1d76db",
    "mahler:verifying": "00b8d9", "mahler:needs-you": "d93f0b", "mahler:parked": "c5def5",
    "mahler:failed": "b60205", "mahler:parent": "5319e7",
    "type:bug": "d73a4a", "type:feature": "a2eeef", "type:chore": "fef2c0",
    "type:goal": "7057ff", "type:uat": "fbca04", "type:anomaly": "e99695",
    "size:s": "c2e0c6", "size:m": "bfd4f2", "size:l": "f9d0c4",
    "p1": "b60205", "p2": "fbca04", "p3": "c5def5",
}
PIN_COLOR = "d4c5f9"        # platform:* labels, created on the fly (mahler#20)
AGENT_MARK = "<!-- mahler"          # every Mahler/agent comment starts with this
AGENT_NOTE = "<!-- mahler:agent -->"  # the line Mahler's own comments start with
# `(?:>\s*)?` tolerates the line living inside a markdown blockquote (a "Part
# of #N" written under a quoted "> **Original request:**" preamble) — without
# it, the reference silently fails to parse and the item never gets linked to
# its parent, so close_finished_parents can never close the parent (found
# 2026-09-14 auditing why mahler#83's tree never auto-closed).
DEPENDS_RE = re.compile(r"^\s*(?:>\s*)?Depends on:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
PART_OF_RE = re.compile(r"^\s*(?:>\s*)?(?:\*{1,2})?Part of:?(?:\*{1,2})?\s*#(\d+)",
                        re.IGNORECASE | re.MULTILINE)



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


def _gh(*args, input=None, timeout=90, env=None):
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True,
                           input=input, timeout=timeout, env=env)
    except (subprocess.SubprocessError, OSError) as e:
        raise GHError(f"gh {' '.join(args[:3])}: {e}") from e
    if r.returncode != 0:
        raise GHError(f"gh {' '.join(args[:3])}: "
                      f"{redact.redact((r.stderr or r.stdout).strip()[:500])}")
    return r.stdout


def _git(path, *args, env=None):
    """Git in a project checkout, for the pushes the conductor owns (D18).
    Goes through the same credential setup as the run's own pushes."""
    try:
        r = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True,
                           timeout=300, env=env)
    except (subprocess.SubprocessError, OSError) as e:
        raise GHError(f"git {' '.join(args[:3])}: {e}") from e
    if r.returncode != 0:
        raise GHError(f"git {' '.join(args[:3])}: "
                      f"{redact.redact((r.stderr or r.stdout).strip()[:400])}")
    return r.stdout.strip()


class GH:
    def __init__(self, repo, env=None):
        self.repo = repo
        self.env = env      # the project's account env (DESIGN D25); None = inherit

    def _gh(self, *args, **kw):
        return _gh(*args, env=self.env, **kw)

    def _git(self, path, *args):
        return _git(path, *args, env=self.env)

    def open_issues(self):
        out = self._gh("issue", "list", "-R", self.repo, "--state", "open", "--limit", "300",
                       "--json", "number,title,labels,body,createdAt,updatedAt,comments,url")
        return json.loads(out)

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

    def issue_state(self, number):
        out = self._gh("issue", "view", str(number), "-R", self.repo, "--json", "state")
        return json.loads(out)["state"]          # OPEN | CLOSED

    def comment(self, number, body):
        if not body.startswith(AGENT_MARK):
            body = AGENT_NOTE + "\n" + body
        self._gh("issue", "comment", str(number), "-R", self.repo, "--body-file", "-", input=body)

    def set_state_label(self, number, state, current_labels):
        want = STATE_LABELS.get(state)
        drop = [l for l in current_labels if l in LABEL_STATES and l != want]
        args = ["issue", "edit", str(number), "-R", self.repo]
        for l in drop:
            args += ["--remove-label", l]
        if want and want not in current_labels:
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

    def create_issue(self, title, body="", labels=()):
        args = ["issue", "create", "-R", self.repo, "--title", title, "--body-file", "-"]
        for l in labels:
            args += ["--label", l]
        return self._gh(*args, input=body).strip()

    # ---------- the conductor ships (DESIGN D18) ----------

    def issue_body(self, number):
        return json.loads(self._gh("issue", "view", str(number), "-R", self.repo,
                                   "--json", "body")).get("body") or ""

    def push_branch(self, path, branch, ref):
        """Make origin's `branch` point at the saved work `ref` (D18).

        `ref` is an already-pushed ref (a mahler/snapshot/* branch). No-op when
        `branch` is already there. Returns the sha now at `branch`."""
        self._git(path, "fetch", "--quiet", "--force", "origin", f"refs/heads/{ref}")
        sha = self._git(path, "rev-parse", "FETCH_HEAD")
        out = subprocess.run(["git", "-C", path, "ls-remote", "origin",
                              f"refs/heads/{branch}"], capture_output=True, text=True,
                             timeout=90, env=self.env)
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

    def pr_view(self, number):
        return json.loads(self._gh("pr", "view", str(number), "-R", self.repo, "--json",
                                   "state,body,statusCheckRollup,mergeable,headRefName,"
                              "headRefOid,baseRefName"))

    def pr_merge(self, number):
        self._gh("pr", "merge", str(number), "-R", self.repo, "--squash", "--delete-branch")

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

    def close_issue(self, number, comment=None):
        """Close an issue, optionally with a comment."""
        args = ["issue", "close", str(number), "-R", self.repo]
        if comment:
            args += ["--comment", comment]
        self._gh(*args)


def label_names(issue):
    return [l["name"] if isinstance(l, dict) else l for l in issue.get("labels", [])]


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
    deps = []
    for m in DEPENDS_RE.finditer(body or ""):
        deps += [int(n) for n in re.findall(r"#(\d+)", m.group(1))]
    return deps


def part_of(body):
    """Parent issue number if body has 'Part of #N' (case-insensitive), or None."""
    m = PART_OF_RE.search(body or "")
    return int(m.group(1)) if m else None


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
    out, grab = [], False
    for line in (body or "").splitlines():
        h = re.match(r"^(#{1,6})\s+(.*?)\s*$", line)
        if h:
            grab = h.group(2).strip().lower() == "needs a human to check"
            continue
        if grab:
            out.append(line)
    return "\n".join(out).strip()


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
    return "\n".join(lines)


def pr_summary_of(body):
    """The agent summary out of a PR body the conductor wrote."""
    m = re.search(r"^Fixes #\d+\s*$", body or "", re.MULTILINE)
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
    states = [(c.get("state") or c.get("status") or "").upper() for c in (rollup or [])]
    if not states:
        return "none"                       # no CI configured: nothing to wait for
    if any(s in _BAD_CHECKS for s in states):
        return "red"
    if any(s in _WAIT_CHECKS for s in states):
        return "pending"
    return "green"
