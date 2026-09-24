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
AREA_COLOR = "0052cc"       # area:* labels, created on the fly (mahler#197)
AGENT_MARK = "<!-- mahler"          # every Mahler/agent comment starts with this
AGENT_NOTE = "<!-- mahler:agent -->"  # the line Mahler's own comments start with
HELP_FOOTER = "\n\n<sub>[Mahler commands](https://github.com/mkny13/mahler/blob/main/docs/commands.md)</sub>"
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
        self._relationships = {}

    def _gh(self, *args, **kw):
        return _gh(*args, env=self.env, **kw)

    def _git(self, path, *args):
        return _git(path, *args, env=self.env)

    def open_issues(self):
        out = self._gh("issue", "list", "-R", self.repo, "--state", "open", "--limit", "300",
                       "--json", "number,title,labels,body,createdAt,updatedAt,comments,url,"
                       "blocking,blockedBy")
        issues = json.loads(out)
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

    def issue_state(self, number):
        out = self._gh("issue", "view", str(number), "-R", self.repo, "--json", "state")
        return json.loads(out)["state"]          # OPEN | CLOSED

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
        if re.fullmatch(r"mahler/revert-\d+", ref):
            # Console reverts are prepared locally by the tick; only the
            # conductor pushes them, after acquiring its lease.
            sha = self._git(path, "rev-parse", "--verify", f"refs/heads/{ref}^{{commit}}")
        else:
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
                                   "headRefOid,baseRefName,mergeCommit,title"))

    def pr_edit_body(self, number, body):
        self._gh("pr", "edit", str(number), "-R", self.repo, "--body-file", "-", input=body)

    def open_prs(self, limit=50):
        """Open PRs, for the unowned-PR check (mahler#407)."""
        return json.loads(self._gh("pr", "list", "-R", self.repo, "--state", "open",
                                   "--limit", str(limit), "--json",
                                   "number,title,headRefName,createdAt,isDraft"))

    def pr_merge_info(self, number):
        return json.loads(self._gh("pr", "view", str(number), "-R", self.repo, "--json",
                                   "state,mergeCommit,title,baseRefName"))

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
                capture_output=True, text=True, timeout=90, env=self.env)
        except (subprocess.SubprocessError, OSError) as e:
            raise GHError(f"freshness: ancestry check failed: {e}") from e
        if result.returncode not in (0, 1):
            raise GHError("freshness: ancestry check failed: " +
                          redact.redact(result.stderr.strip()[:400]))
        return result.returncode == 0

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



COMMAND_RE = re.compile(r"^\s*/mahler\s+(go|park|platform|approve)\b\s*(\S*)",
                        re.IGNORECASE | re.MULTILINE)


def parse_command(body):
    """`/mahler go` · `/mahler park` · `/mahler platform agy-gemini` ·
    `/mahler approve` — or None."""
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
