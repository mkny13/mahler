"""GitHub App identity for the conductor's own GitHub writes (mahler#607).

With a `[github_app]` table in config.toml, labels, comments, PRs and merges
made through the `GH` client show up as `<app>[bot]` instead of the owner's
login. The installation token is minted here from the app id, installation id
and private key, using only the standard library plus `openssl` for the RS256
signature. Anything that goes wrong returns None, and the caller falls back to
whatever `gh auth` holds, so a bad key or a GitHub outage never stops the tick.
"""

import base64
import json
import os
import subprocess
import threading
import time
import urllib.request

API = "https://api.github.com"
REFRESH_BEFORE = 300     # renew a token this many seconds before it expires
RETRY_AFTER = 60         # after a failed mint, don't retry for this long

_cache = {}              # (app_id, installation_id) -> (token, expires_at) or (None, retry_at)
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


def _request_token(jwt, installation_id, urlopen=urllib.request.urlopen):
    req = urllib.request.Request(
        f"{API}/app/installations/{installation_id}/access_tokens", method="POST",
        headers={"Authorization": f"Bearer {jwt}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "mahler"})
    with urlopen(req, timeout=20) as resp:
        body = json.load(resp)
    expires = time.mktime(time.strptime(body["expires_at"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    return body["token"], expires


def installation_token(app, now=time.time, sign=make_jwt, fetch=_request_token):
    """A valid installation token for the configured app, or None."""
    try:
        key = (str(app["app_id"]), str(app["installation_id"]))
        key_path = app["private_key"]
    except (KeyError, TypeError):
        return None
    with _lock:
        token, until = _cache.get(key, (None, 0))
        t = now()
        if token and t < until - REFRESH_BEFORE:
            return token
        if not token and t < until:
            return None
        try:
            token, expires = fetch(sign(key[0], key_path, t), key[1])
        except Exception:
            _cache[key] = (None, t + RETRY_AFTER)
            return None
        _cache[key] = (token, expires)
        return token


def reset():
    with _lock:
        _cache.clear()
