"""Repository guard: ignored browser-profile state must never be tracked.

Commit d943be9 accidentally added 495 files of a Chromium profile — cookies,
login state, history, device-bound sessions — to this public repository. The
directory is ignored now (`/.calendar-*` in .gitignore), but files that were
added before the rule stay tracked. This guard (mahler#588) checks Git's
tracked-path list (`git ls-files`), not the working tree, and never opens any
candidate file: a tracked path is only ever a name to test.

It fails when a tracked path is:

  * under a root-level `.calendar-*` path — the ignore rule's own pattern, so
    anything it covers must never be tracked again, file or directory; or
  * named after a high-risk browser-state database: `Cookies`, `Login Data`,
    `History`, `Device Bound Sessions`, `Local State` — including their SQLite
    companions (`-journal`, `-wal`, `-shm`), anywhere in the tree.

CI runs it before the unit suite, so a future `git add .` of ignored browser
state fails deterministically instead of shipping. On failure it prints only
tracked path names and a remediation hint — never file contents.
"""

import subprocess
import sys

IGNORED_PREFIX = ".calendar-"
PROHIBITED_BASENAMES = (
    "Cookies",
    "Login Data",
    "History",
    "Device Bound Sessions",
    "Local State",
)
# SQLite companions of the databases above; Chrome leaves these beside the
# .db file (first_party_sets.db-journal was among the committed files).
COMPANION_SUFFIXES = ("-journal", "-wal", "-shm")


def _prohibited(path):
    """Return a reason string if this tracked path must not be tracked."""
    if path.split("/", 1)[0].startswith(IGNORED_PREFIX):
        return f"ignored browser-profile path ({IGNORED_PREFIX}*)"
    base = path.rsplit("/", 1)[-1]
    for name in PROHIBITED_BASENAMES:
        if base == name or any(base == name + s for s in COMPANION_SUFFIXES):
            return f"browser-state file {name!r}"
    return None


def violations(paths):
    """The subset of tracked `paths` that must never be tracked."""
    return [
        (path, reason)
        for path in paths
        if (reason := _prohibited(path)) is not None
    ]


def tracked_paths(root):
    """Everything Git tracks under `root`, as repo-relative names."""
    res = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True
    )
    if res.returncode != 0:
        raise RuntimeError(
            "git ls-files failed: "
            + res.stderr.decode(errors="replace").strip()
        )
    return [
        p for p in res.stdout.decode(errors="surrogateescape").split("\0") if p
    ]


def main(root=".", paths=None):
    """Check the tracked-path list; return a process exit code."""
    try:
        listed = tracked_paths(root) if paths is None else paths
    except RuntimeError as e:
        print(f"repo-guard: {e}", file=sys.stderr)
        return 2
    bad = violations(listed)
    for path, reason in bad:
        print(f"tracked browser-profile path: {path} ({reason})")
    if bad:
        print(
            f"\n{len(bad)} tracked path(s) look like committed browser "
            "profile state. Untrack them (git rm -r --cached) and keep the "
            "/.calendar-* ignore rule; never open or quote their contents."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
