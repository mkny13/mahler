"""GitHub App identity for the conductor's own GitHub writes (mahler#607).

With a `[github_app]` table in config.toml, labels, comments, PRs and merges
made through the `GH` client show up as `<app>[bot]` instead of the owner's
login. The installation token is minted here from the app id, installation id
and private key, using only the standard library plus `openssl` for the RS256
signature. Anything that goes wrong returns None, and the caller falls back to
whatever `gh auth` holds, so a bad key or a GitHub outage never stops the tick.
"""

import base64
import calendar
import contextlib
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import threading
import time
import urllib.request

API = "https://api.github.com"
REFRESH_BEFORE = 300     # renew a token this many seconds before it expires
RETRY_AFTER = 60         # after a failed mint, don't retry for this long

_cache = {}              # (cache dir, app id, installation id) -> (token, until)
_lock = threading.Lock()


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def make_jwt(app_id, key_path, now, run=subprocess.run):
    signing_input = b".".join([
        _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()),
        _b64(json.dumps({"iat": int(now) - 60, "exp": int(now) + 540,
                         "iss": str(app_id)}).encode()),
    ])
    r = run(["openssl", "dgst", "-sha256", "-sign", os.path.expanduser(key_path)],
            input=signing_input, capture_output=True, timeout=15)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("openssl could not sign the app JWT")
    return (signing_input + b"." + _b64(r.stdout)).decode()


def _request_token(signed_jwt, installation_id, urlopen=urllib.request.urlopen):
    req = urllib.request.Request(
        f"{API}/app/installations/{installation_id}/access_tokens", method="POST",
        headers={"Authorization": f"Bearer {signed_jwt}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "mahler"})
    with urlopen(req, timeout=20) as resp:
        body = json.load(resp)
    expires = calendar.timegm(time.strptime(body["expires_at"], "%Y-%m-%dT%H:%M:%SZ"))
    return body["token"], expires


def select_installation(app, repo, override=None):
    """Return app config for ``repo``'s installation, or None to use gh auth.

    An explicit project override wins. Otherwise, when ``installations`` is
    present, it is an owner-to-installation-id map and an unmapped owner falls
    back safely. The singular installation_id remains the convenient global
    form for an App installation shared by every managed repository.
    """
    if not isinstance(app, dict):
        return None
    installation_id = override
    if installation_id is None:
        installations = app.get("installations")
        if installations is not None:
            if not isinstance(installations, dict):
                return None
            owner, separator, _ = str(repo).partition("/")
            if not separator:
                return None
            installation_id = installations.get(owner)
        else:
            installation_id = app.get("installation_id")
    if not installation_id:
        return None
    selected = dict(app)
    selected.pop("installations", None)
    selected["installation_id"] = installation_id
    return selected


def _cache_paths(cache_dir, key):
    digest = hashlib.sha256(":".join(key).encode()).hexdigest()
    return (os.path.join(cache_dir, f"{digest}.json"),
            os.path.join(cache_dir, f"{digest}.lock"))


def _prepare_cache_dir(cache_dir):
    os.makedirs(cache_dir, mode=0o700, exist_ok=True)
    os.chmod(cache_dir, 0o700)


@contextlib.contextmanager
def _file_lock(path):
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_cache(path):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            return None
        with os.fdopen(fd, encoding="utf-8") as fh:
            fd = None
            value = json.load(fh)
    except (OSError, ValueError, TypeError):
        return None
    finally:
        if fd is not None:
            os.close(fd)
    if not isinstance(value, dict):
        return None
    token, until = value.get("token"), value.get("until")
    if token is not None and not isinstance(token, str):
        return None
    if not isinstance(until, (int, float)):
        return None
    return token, float(until)


def _write_cache(path, value):
    parent = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".token.", suffix=".tmp", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = None
            json.dump({"token": value[0], "until": value[1]}, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        dfd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if fd is not None:
            os.close(fd)
        if os.path.exists(tmp):
            os.unlink(tmp)


def _usable(value, now):
    if not value:
        return False
    token, until = value
    return now < until - REFRESH_BEFORE if token else now < until


def installation_token(app, now=time.time, sign=make_jwt, fetch=_request_token,
                       *, cache_dir=None):
    """A valid installation token for the configured app, or None."""
    try:
        key = (str(app["app_id"]), str(app["installation_id"]))
        key_path = app["private_key"]
    except (KeyError, TypeError):
        return None
    cache_dir = cache_dir or os.path.join(
        os.environ.get("MAHLER_HOME", os.path.expanduser("~/.mahler")),
        "github-app-tokens")
    memory_key = (os.path.abspath(cache_dir), *key)
    with _lock:
        t = now()
        try:
            cached = _cache.get(memory_key)
            if _usable(cached, t):
                return cached[0]
            _prepare_cache_dir(cache_dir)
            cache_path, lock_path = _cache_paths(cache_dir, key)
            with _file_lock(lock_path):
                cached = _read_cache(cache_path)
                if _usable(cached, t):
                    _cache[memory_key] = cached
                    return cached[0]
                try:
                    token, expires = fetch(sign(key[0], key_path, t), key[1])
                    cached = (token, float(expires))
                except Exception:
                    cached = (None, t + RETRY_AFTER)
                _write_cache(cache_path, cached)
                _cache[memory_key] = cached
                return cached[0]
        except Exception:
            return None


def reset():
    with _lock:
        _cache.clear()
