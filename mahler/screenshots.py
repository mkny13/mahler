"""Bounded, local screenshot artifacts (D11). Advisory capture before review; no publication here.

Callers supply the exact PR head, catch InvalidScreenshot/OSError as advisory
failures, and keep lifecycle state (including any later merge SHA) in ledger KV.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import signal
import subprocess
import tempfile

from . import config

MAX_MANIFEST_BYTES = 64 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_SCREENSHOTS = 10
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class InvalidScreenshot(ValueError):
    """Untrusted output does not meet the screenshot contract."""


def _parts(name):
    if (not isinstance(name, str) or not name or name.startswith("/")
            or "\\" in name or ":" in name
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or any(p in ("", ".", "..") for p in name.split("/"))):
        raise InvalidScreenshot("invalid relative path")
    return name.split("/")


@contextmanager
def _directory(path):
    # Resolve only the caller-owned parent: macOS /var itself is a symlink.
    # Reject a symlink at the supplied output/storage directory.
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield fd
    finally:
        os.close(fd)


def _read(root_fd, name, limit):
    parts = _parts(name)
    fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise InvalidScreenshot("not a bounded regular file")
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise InvalidScreenshot("file exceeds size limit")
            return data
    finally:
        os.close(fd)


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise InvalidScreenshot("duplicate JSON field")
        obj[key] = value
    return obj


def _load(root_fd, sha):
    try:
        manifest = json.loads(_read(root_fd, "manifest.json", MAX_MANIFEST_BYTES),
                              object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise InvalidScreenshot("malformed manifest") from exc
    if (not isinstance(manifest, dict) or type(manifest.get("version")) is not int
            or manifest["version"] != 1 or manifest.get("sha") != sha):
        raise InvalidScreenshot("manifest version or SHA mismatch")
    entries = manifest.get("screenshots")
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_SCREENSHOTS:
        raise InvalidScreenshot("expected 1–10 screenshots")
    files, clean, total = {}, [], 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise InvalidScreenshot("invalid screenshot entry")
        route, name = entry.get("route"), entry.get("file")
        _parts(name)
        if not name.endswith(".png") or name.casefold() in files:
            raise InvalidScreenshot("expected unique PNG filenames")
        if (not isinstance(route, str) or not route.startswith("/")
                or route.startswith("//") or any(c in route for c in "?#\\")
                or any(ord(c) < 32 or ord(c) == 127 for c in route)):
            raise InvalidScreenshot("expected query/fragment-free route label")
        data = _read(root_fd, name, MAX_FILE_BYTES)
        if not data.startswith(PNG_SIGNATURE):
            raise InvalidScreenshot("invalid PNG signature")
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise InvalidScreenshot("total screenshot size exceeded")
        files[name.casefold()] = (name, data)
        clean.append({"route": route, "file": name})
    return {"version": 1, "sha": sha, "screenshots": clean}, list(files.values())


def read_manifest(directory, sha):
    """Validate all referenced files; return sanitized manifest, never extra fields."""
    with _directory(directory) as fd:
        manifest, _ = _load(fd, sha)
    return manifest


def _key(project, pr, sha):
    if (not isinstance(project, str) or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", project)
            or type(pr) is not int or pr < 1
            or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha)):
        raise InvalidScreenshot("invalid project/PR/head key")
    return project, str(pr), sha


def _parent(project, pr, sha, root, create=False):
    key = _key(project, pr, sha)
    base = Path(root) if root is not None else Path(config.STATE) / "screenshots"
    # Storage's parent is trusted operator state; all levels we own reject links.
    for path in (base, base / key[0], base / key[0] / key[1]):
        if create:
            path.mkdir(mode=0o700, exist_ok=True)
        with _directory(path) as fd:
            if create:
                os.fchmod(fd, 0o700)
    return base / key[0] / key[1], key[2]


def store(source, project, pr, sha, *, root=None):
    """Copy validated bytes into an immutable private project/PR/head directory.

    Atomic directory rename publishes PNGs and manifest together. Existing heads
    are never overwritten; FileExistsError means the caller should use lookup.
    `root` overrides MAHLER_HOME/screenshots for isolated tests.
    """
    _key(project, pr, sha)
    with _directory(source) as fd:
        manifest, files = _load(fd, sha)
    parent, head = _parent(project, pr, sha, root, create=True)
    target = parent / head
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    staging = Path(tempfile.mkdtemp(prefix=".capture-", dir=parent))
    try:
        for name, data in files + [("manifest.json", json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))]:
            path = staging / name
            directory = staging
            for part in _parts(name)[:-1]:
                directory /= part
                directory.mkdir(mode=0o700, exist_ok=True)
            with path.open("xb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
        os.rename(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return target


def lookup(project, pr, sha, *, root=None):
    """Return (private directory, validated manifest), or None when absent.

    Invalid existing artifacts raise; they must never become capture evidence.
    Manifest `sha` is always the capture head, never the squash-merge SHA.
    """
    try:
        parent, head = _parent(project, pr, sha, root)
        target = parent / head
        with _directory(target) as fd:
            try:
                manifest, _ = _load(fd, sha)
            except FileNotFoundError as exc:
                raise InvalidScreenshot("stored artifact is incomplete") from exc
    except FileNotFoundError:
        return None
    return target, manifest



def artifact_id(project, pr, sha, index):
    """Opaque identity; never a client-supplied filesystem path."""
    return hashlib.sha256(json.dumps([project, pr, sha, index]).encode()).hexdigest()


def console_artifacts(project, pr, sha):
    """Read validated manifest and bytes through pinned, no-follow directories."""
    parts = _key(project, pr, sha)
    with _directory(Path(config.STATE) / "screenshots") as root:
        fd = os.dup(root)
        try:
            for part in parts:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=fd)
                os.close(fd)
                fd = child
            manifest, files = _load(fd, sha)
        finally:
            os.close(fd)
    data = dict(files)
    return [({"id": artifact_id(project, pr, sha, index),
              "route": entry["route"]}, data[entry["file"]])
            for index, entry in enumerate(manifest["screenshots"])]


def _command(command, cwd, env, timeout):
    """Run only an installed operator tool, never a shell or checkout command."""
    tool = Path(command)
    if not tool.is_absolute():
        return 126
    try:
        tool = tool.resolve(strict=True)
    except FileNotFoundError:
        return 127
    if not tool.is_file() or not os.access(tool, os.X_OK):
        return 126
    for parent in tool.parents:
        marker = parent / ".git"
        if marker.exists() or marker.is_symlink():
            return 126
    with subprocess.Popen([str(tool)], cwd=cwd, env=env,
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, start_new_session=True) as proc:
        try:
            return proc.wait(timeout=timeout)
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()


def _worktree(led, project, number, sha):
    runs = led.con.execute(
        "SELECT worktree FROM runs WHERE project=? AND number=? "
        "AND role IN ('build','fix') ORDER BY id DESC", (project, number))
    for run in runs:
        path = run["worktree"]
        if not path or not Path(path).is_dir():
            continue
        result = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"],
                                capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and result.stdout.strip() == sha:
            return path
    return None


def _capture_path(paths, worktree):
    """Resolve trusted operator directories without borrowing the daemon PATH."""
    checkout = Path(worktree).resolve()
    directories = []
    for entry in paths:
        directory = Path(entry).resolve()
        if directory.is_relative_to(checkout):
            raise InvalidScreenshot("capture dependency path is inside the PR worktree")
        directories.append(str(directory))
    return os.pathsep.join([*directories, "/opt/homebrew/bin", "/usr/local/bin", os.defpath])


def capture(ctx, project, item, pr, view):
    """At most one advisory attempt per head, including across conductor restarts.

    Persist before any external work. An interrupted attempt is terminal unavailable;
    errors (including ledger failures) must never bypass the normal shipping gates.
    Only fixed reason codes leave this boundary, never exception/command/URL text.
    """
    if ctx.dry_run:
        return
    sha = view.get("headRefOid") or ""
    key = f"screenshot:{project}:{pr}:{sha}"
    result = {"sha": sha, "pr": pr, "state": "unavailable", "reason": "interrupted"}
    try:
        policy = ctx.policy(project)
        if not policy.get("screenshot"):
            return
        _key(project, pr, sha)
        previous = ctx.led.get_kv(key)
        if previous:
            return
        # This is already a terminal unavailable result if the process dies here.
        ctx.led.set_kv(key, json.dumps(result))
        environment = policy.get("screenshot_environment", "")
        result["reason"] = "preview_unavailable"
        url = ctx.gh(project).screenshot_preview(sha, environment) if environment else None
        if url:
            result["reason"] = "checkout_unavailable"
            cwd = _worktree(ctx.led, project, item["number"], sha)
            if cwd:
                with tempfile.TemporaryDirectory(prefix="mahler-screenshot-output-") as output, \
                        tempfile.TemporaryDirectory(prefix="mahler-screenshot-profile-") as profile:
                    output, profile = str(Path(output).resolve()), str(Path(profile).resolve())
                    result["reason"] = "path_unavailable"
                    capture_path = _capture_path(policy.get("screenshot_path", []), cwd)
                    env = {"PATH": capture_path, "HOME": profile, "TMPDIR": profile,
                           "XDG_CONFIG_HOME": profile, "XDG_CACHE_HOME": profile,
                           "MAHLER_SCREENSHOT_URL": url,
                           "MAHLER_SCREENSHOT_DIR": output,
                           "MAHLER_SCREENSHOT_PROFILE_DIR": profile,
                           "MAHLER_SCREENSHOT_SHA": sha, "MAHLER_SCREENSHOT_PR": str(pr)}
                    # Optional bypass secret for protected previews; never log value.
                    bypass_file = policy.get("screenshot_bypass_secret_file", "")
                    bypass_secret = ""
                    if bypass_file:
                        try:
                            with open(bypass_file, "r", encoding="utf-8") as f:
                                bypass_secret = f.read().strip()
                        except Exception:
                            bypass_secret = ""
                    if bypass_secret:
                        env["MAHLER_SCREENSHOT_BYPASS_SECRET"] = bypass_secret
                    result.update(state="failed", reason="command_failed")
                    code = _command(policy["screenshot"], profile, env,
                                    policy.get("screenshot_timeout_seconds", 45))
                    if code in (126, 127):
                        result.update(state="unavailable", reason="command_unavailable")
                    elif code == 0:
                        result["reason"] = "invalid_output"
                        read_manifest(output, sha)
                        result["reason"] = "head_lookup_failed"
                        if ctx.gh(project).pr_view(pr).get("headRefOid") != sha:
                            result.update(state="unavailable", reason="head_changed")
                        else:
                            result["reason"] = "storage_failed"
                            store(output, project, pr, sha)
                            result.update(state="success", reason="captured")
    except subprocess.TimeoutExpired:
        result.update(state="failed", reason="timeout")
    except Exception:
        # Advisory boundary: do not leak signed URLs, routes or raw exceptions.
        pass
    try:
        ctx.led.set_kv(key, json.dumps(result))
        ctx.led.event("screenshot", project, item["number"], result)
        ctx.say(f"{project} PR #{pr}: screenshots {result['state']} ({result['reason']})")
    except Exception:
        pass
