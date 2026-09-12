"""Version info: commit, known-good status, behind-count.

All heavy logic lives here so it can be unit-tested without shelling out.
The public entry point is `version_info(app_dir, home_dir)` which returns a
plain dict; `format_version(info)` turns that into the human-readable string.
"""

import os
import subprocess


def _git(args, cwd, timeout=10):
    """Run a git command, return (ok, stdout_stripped)."""
    try:
        r = subprocess.run(
            ["git"] + args, cwd=cwd,
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode == 0, r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False, ""


def _short_head(app_dir):
    ok, sha = _git(["rev-parse", "--short", "HEAD"], app_dir)
    return sha if ok else None


def _is_git_repo(app_dir):
    ok, _ = _git(["rev-parse", "--git-dir"], app_dir)
    return ok


def _known_good(home_dir):
    """Read the known-good SHA, return None if the file doesn't exist."""
    path = os.path.join(home_dir, "known_good")
    if not os.path.isfile(path):
        return None
    with open(path) as fh:
        return fh.read().strip() or None


def _behind_count(app_dir):
    """Fetch quietly, then count commits HEAD is behind origin/main.

    Returns an int, or None if anything fails (offline, no remote, etc.).
    """
    _git(["fetch", "--quiet"], app_dir, timeout=15)
    ok, out = _git(
        ["rev-list", "--count", "HEAD..origin/main"], app_dir,
    )
    if ok and out.isdigit():
        return int(out)
    return None


def version_info(app_dir, home_dir):
    """Gather version information.

    Parameters
    ----------
    app_dir : str
        The repo root containing the running ``mahler`` package
        (i.e. the parent of the ``mahler/`` directory).
    home_dir : str
        ``MAHLER_HOME`` (default ``~/.mahler``), where ``known_good``
        lives.

    Returns a dict with keys:
        commit     – short SHA or None
        is_git     – bool
        known_good – the known-good short SHA, or None (file absent)
        kg_match   – True / False / None (no known-good file)
        behind     – int or None
    """
    is_git = _is_git_repo(app_dir)
    commit = _short_head(app_dir) if is_git else None

    kg = _known_good(home_dir)

    # Determine match status.  We compare short SHAs, but the stored value
    # might be a full SHA — handle both by prefix-matching.
    if kg is None:
        kg_match = None
    elif commit is None:
        kg_match = False
    else:
        kg_match = (commit.startswith(kg) or kg.startswith(commit))

    behind = _behind_count(app_dir) if is_git else None

    return {
        "commit": commit,
        "is_git": is_git,
        "known_good": kg,
        "kg_match": kg_match,
        "behind": behind,
    }


def format_version(info):
    """Render *info* (from `version_info`) as a human-readable string."""
    lines = []

    # Line 1: commit
    if info["commit"]:
        lines.append(f"commit:     {info['commit']}")
    else:
        lines.append("commit:     not a git checkout")

    # Line 2: known-good
    if info["kg_match"] is None:
        lines.append("known-good: no known-good recorded yet")
    elif info["kg_match"]:
        lines.append("known-good: matches")
    else:
        kg_display = info["known_good"] or "?"
        lines.append(f"known-good: does not match (known-good is {kg_display})")

    # Line 3: behind origin/main
    if not info["is_git"]:
        lines.append("behind:     not a git checkout")
    elif info["behind"] is None:
        lines.append("behind:     unknown (fetch failed)")
    elif info["behind"] == 0:
        lines.append("behind:     up to date with origin/main")
    else:
        s = "commit" if info["behind"] == 1 else "commits"
        lines.append(f"behind:     {info['behind']} {s} behind origin/main")

    return "\n".join(lines)
