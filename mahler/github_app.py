"""Conductor-only GitHub App tokens, shared across short-lived tick processes."""

import base64
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request

from .gh import GHError

REFRESH_MARGIN = 300


def _b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class Installation:
    def __init__(self, app_id, installation_id, private_key_path, cache_dir):
        self.app_id = str(app_id)
        self.installation_id = str(installation_id)
        self._key_path = os.path.expanduser(private_key_path)
        self._cache_dir = Path(cache_dir)

    def _jwt(self):
        now = int(time.time())
        header = _b64(b'{"alg":"RS256","typ":"JWT"}')
        claims = _b64(json.dumps({"iat": now - 60, "exp": now + 540,
                                  "iss": self.app_id}).encode())
        message = f"{header}.{claims}"
        try:
            signed = subprocess.run(
                ["openssl", "dgst", "-sha256", "-sign", self._key_path],
                input=message.encode(), capture_output=True, timeout=30)
            if signed.returncode or not signed.stdout:
                raise ValueError("signing failed")
        except (OSError, subprocess.SubprocessError, ValueError):
            raise GHError("GitHub App JWT signing failed; check openssl and private_key_path") from None
        return f"{message}.{_b64(signed.stdout)}"

    @staticmethod
    def _usable(data):
        try:
            token = data["token"]
            expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
            return (isinstance(token, str) and bool(token) and not any(c.isspace() for c in token)
                    and expires.tzinfo is not None
                    and expires.timestamp() > time.time() + REFRESH_MARGIN)
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            return False

    def token(self):
        """Never fall back to a human identity after an App authentication failure."""
        try:
            self._cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self._cache_dir.is_symlink():
                raise OSError("unsafe cache")
            self._cache_dir.chmod(0o700)
            path = self._cache_dir / f"{self.app_id}-{self.installation_id}.json"
            if path.is_symlink():
                raise OSError("unsafe cache")
            try:
                path.chmod(0o600)
                data = json.loads(path.read_text())
            except (FileNotFoundError, ValueError):
                data = None
            if self._usable(data):
                return data["token"]
            jwt = self._jwt()
            request = urllib.request.Request(
                f"https://api.github.com/app/installations/{self.installation_id}/access_tokens",
                data=b"{}", method="POST", headers={
                    "Authorization": f"Bearer {jwt}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "Content-Type": "application/json",
                    "User-Agent": "mahler",
                })
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    data = json.load(response)
                if not self._usable(data):
                    raise ValueError("invalid token response")
            except Exception:
                raise GHError("GitHub App token exchange failed; check installation id, permissions and network") from None
            tmp = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", dir=self._cache_dir,
                                                 delete=False) as fh:
                    tmp = fh.name
                    os.fchmod(fh.fileno(), 0o600)
                    json.dump({"token": data["token"], "expires_at": data["expires_at"]}, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
            finally:
                if tmp and os.path.exists(tmp):
                    os.unlink(tmp)
            return data["token"]
        except OSError:
            raise GHError("GitHub App token cache unavailable; check cache directory permissions") from None
