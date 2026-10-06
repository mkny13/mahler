"""Bounded, local screenshot artifacts (D11). No capture or publication here.

Callers supply the exact PR head, catch InvalidScreenshot/OSError as advisory
failures, and keep lifecycle state (including any later merge SHA) in ledger KV.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import stat
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
