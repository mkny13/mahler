"""Thin wrapper over the `gh` CLI — GitHub is the item store (DESIGN D4).

Every call goes through `gh`, so auth is whatever `gh auth` already has
(keyring, https + gh as git credential helper — works under launchd).
"""

import json
import re
import subprocess

STATE_LABELS = {
    "inbox": "mahler:inbox", "ready": "mahler:ready", "working": "mahler:working",
    "needs_you": "mahler:needs-you", "parked": "mahler:parked", "failed": "mahler:failed",
    "tracking": "mahler:tracking",
}
LABEL_STATES = {v: k for k, v in STATE_LABELS.items()}
LABEL_COLORS = {
    "mahler:inbox": "ededed", "mahler:ready": "0e8a16", "mahler:working": "1d76db",
    "mahler:needs-you": "d93f0b", "mahler:parked": "c5def5", "mahler:failed": "b60205",
    "mahler:tracking": "5319e7",
    "type:bug": "d73a4a", "type:feature": "a2eeef", "type:chore": "fef2c0",
    "type:goal": "7057ff", "type:uat": "fbca04", "type:anomaly": "e99695",
    "size:s": "c2e0c6", "size:m": "bfd4f2", "size:l": "f9d0c4",
    "p1": "b60205", "p2": "fbca04", "p3": "c5def5",
}
AGENT_MARK = "<!-- mahler"          # every Mahler/agent comment starts with this
DEPENDS_RE = re.compile(r"^\s*Depends on:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


class GHError(RuntimeError):
    pass


def _gh(*args, input=None, timeout=90):
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True,
                           input=input, timeout=timeout)
    except (subprocess.SubprocessError, OSError) as e:
        raise GHError(f"gh {' '.join(args[:3])}: {e}") from e
    if r.returncode != 0:
        raise GHError(f"gh {' '.join(args[:3])}: {(r.stderr or r.stdout).strip()[:500]}")
    return r.stdout


class GH:
    def __init__(self, repo):
        self.repo = repo

    def open_issues(self):
        out = _gh("issue", "list", "-R", self.repo, "--state", "open", "--limit", "300",
                  "--json", "number,title,labels,body,createdAt,updatedAt,comments,url")
        return json.loads(out)

    def issue_state(self, number):
        out = _gh("issue", "view", str(number), "-R", self.repo, "--json", "state")
        return json.loads(out)["state"]          # OPEN | CLOSED

    def comment(self, number, body):
        if not body.startswith(AGENT_MARK):
            body = "<!-- mahler -->\n" + body
        _gh("issue", "comment", str(number), "-R", self.repo, "--body-file", "-", input=body)

    def set_state_label(self, number, state, current_labels):
        want = STATE_LABELS.get(state)
        drop = [l for l in current_labels if l in LABEL_STATES and l != want]
        args = ["issue", "edit", str(number), "-R", self.repo]
        for l in drop:
            args += ["--remove-label", l]
        if want and want not in current_labels:
            args += ["--add-label", want]
        if len(args) > 5:
            _gh(*args)

    def ensure_labels(self):
        for name, color in LABEL_COLORS.items():
            _gh("label", "create", name, "-R", self.repo, "--color", color, "--force")

    def create_issue(self, title, body="", labels=()):
        args = ["issue", "create", "-R", self.repo, "--title", title, "--body-file", "-"]
        for l in labels:
            args += ["--label", l]
        return _gh(*args, input=body).strip()

    def default_branch(self):
        return _gh("repo", "view", self.repo, "--json", "defaultBranchRef",
                   "-q", ".defaultBranchRef.name").strip()


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


COMMAND_RE = re.compile(r"^\s*/mahler\s+(go|park|platform)\b\s*(\S*)", re.IGNORECASE | re.MULTILINE)


def parse_command(body):
    """`/mahler go` · `/mahler park` · `/mahler platform agy-gemini` — or None."""
    m = COMMAND_RE.search(body or "")
    if not m:
        return None
    return m.group(1).lower(), (m.group(2) or None)
