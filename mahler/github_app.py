"""Conductor and repository-scoped agent GitHub App tokens (DESIGN D47)."""

import base64
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.request

from .gh import GHError

REFRESH_MARGIN = 300

AGENT_PERMISSIONS = {
    "actions": "read",
    "checks": "read",
    "contents": "read",
    "issues": "write",
    "metadata": "read",
    "pull_requests": "read",
}

ALLOWED_PERMISSIONS = {
    "actions": {"read"},
    "checks": {"read"},
    "contents": {"read"},
    "issues": {"read", "write"},
    "metadata": {"read"},
    "pull_requests": {"read"},
}


def _b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class Installation:
    def __init__(self, app_id, installation_id, private_key_path, cache_dir):
        self.app_id = str(app_id)
        self.installation_id = str(installation_id)
        self._key_path = os.path.expanduser(private_key_path)
        self._cache_dir = Path(cache_dir)
        self._app_login = None

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
    def _app_slug(data, app_id):
        if (not isinstance(data, dict) or data.get("app_id") != app_id
                or not isinstance(data.get("slug"), str)
                or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?",
                                    data["slug"])):
            return None
        return data["slug"]

    def app_login(self):
        """Resolve the exact login GitHub attributes to this configured App."""
        if self._app_login is not None:
            return self._app_login
        try:
            self._cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self._cache_dir.is_symlink():
                raise OSError("unsafe cache")
            self._cache_dir.chmod(0o700)
            path = self._cache_dir / f"{self.app_id}-app.json"
            if path.is_symlink():
                raise OSError("unsafe cache")
            try:
                path.chmod(0o600)
                cached = json.loads(path.read_text())
            except (FileNotFoundError, ValueError):
                cached = None
            slug = self._app_slug(cached, self.app_id)
            if slug is None:
                jwt = self._jwt()
                request = urllib.request.Request(
                    "https://api.github.com/app", method="GET", headers={
                        "Authorization": f"Bearer {jwt}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                        "User-Agent": "mahler",
                    })
                try:
                    with urllib.request.urlopen(request, timeout=30) as response:
                        metadata = json.load(response)
                    metadata = {"app_id": self.app_id,
                                "slug": metadata.get("slug") if isinstance(metadata, dict) else None}
                    slug = self._app_slug(metadata, self.app_id)
                    if slug is None:
                        raise ValueError("invalid app metadata")
                except Exception:
                    raise GHError(
                        "GitHub App identity lookup failed; check app id, private key and network") from None
                tmp = None
                try:
                    with tempfile.NamedTemporaryFile(mode="w", dir=self._cache_dir,
                                                     delete=False) as fh:
                        tmp = fh.name
                        os.fchmod(fh.fileno(), 0o600)
                        json.dump(metadata, fh)
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.replace(tmp, path)
                finally:
                    if tmp and os.path.exists(tmp):
                        os.unlink(tmp)
            self._app_login = f"app/{slug}"
            return self._app_login
        except OSError:
            raise GHError(
                "GitHub App identity cache unavailable; check cache directory permissions") from None

    @staticmethod
    def _usable(data, repository=None, permissions=None):
        try:
            token = data["token"]
            expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
            if not (isinstance(token, str) and bool(token) and not any(c.isspace() for c in token)
                    and expires.tzinfo is not None
                    and expires.timestamp() > time.time() + REFRESH_MARGIN):
                return False
            if "cached_repository" in data or "cached_permissions" in data:
                if repository is None:
                    return data.get("cached_repository") is None and data.get("cached_permissions") is None
                return data.get("cached_repository") == repository and data.get("cached_permissions") == permissions
            if repository is not None:
                return False
            return True
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            return False

    def token(self, repository=None, permissions=None):
        """Never fall back to a human identity after an App authentication failure."""
        if repository is None and permissions is not None:
            raise GHError("GitHub App scoped token request failed: invalid scope input")
        if repository is not None:
            if (not isinstance(repository, str)
                    or isinstance(repository, bool)
                    or not repository.strip()
                    or repository.strip() != repository
                    or not re.match(r"^[a-zA-Z0-9_.-]+$", repository)
                    or repository in (".", "..")):
                raise GHError("GitHub App scoped token request failed: invalid repository scope")
            if permissions is None:
                permissions = dict(AGENT_PERMISSIONS)
            else:
                if not isinstance(permissions, dict) or not permissions:
                    raise GHError("GitHub App scoped token request failed: invalid permissions scope")
                for perm, level in permissions.items():
                    if perm not in ALLOWED_PERMISSIONS or level not in ALLOWED_PERMISSIONS[perm]:
                        raise GHError("GitHub App scoped token request failed: invalid permissions scope")
                permissions = dict(permissions)

        try:
            self._cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self._cache_dir.is_symlink():
                raise OSError("unsafe cache")
            self._cache_dir.chmod(0o700)
            if repository is None:
                path = self._cache_dir / f"{self.app_id}-{self.installation_id}.json"
            else:
                scope_canonical = json.dumps({
                    "repositories": [repository],
                    "permissions": permissions,
                }, sort_keys=True)
                scope_hash = hashlib.sha256(scope_canonical.encode("utf-8")).hexdigest()[:16]
                path = self._cache_dir / f"{self.app_id}-{self.installation_id}-{repository}-{scope_hash}.json"
            if path.is_symlink():
                raise OSError("unsafe cache")
            try:
                path.chmod(0o600)
                data = json.loads(path.read_text())
            except (FileNotFoundError, ValueError):
                data = None
            if self._usable(data, repository=repository, permissions=permissions):
                return data["token"]
            jwt = self._jwt()
            if repository is None:
                payload = b"{}"
            else:
                payload = json.dumps({
                    "repositories": [repository],
                    "permissions": permissions,
                }, sort_keys=True).encode("utf-8")
            request = urllib.request.Request(
                f"https://api.github.com/app/installations/{self.installation_id}/access_tokens",
                data=payload, method="POST", headers={
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
                    record = {
                        "token": data["token"],
                        "expires_at": data["expires_at"],
                        "cached_repository": repository,
                        "cached_permissions": permissions,
                    }
                    json.dump(record, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
            finally:
                if tmp and os.path.exists(tmp):
                    os.unlink(tmp)
            return data["token"]
        except OSError:
            raise GHError("GitHub App token cache unavailable; check cache directory permissions") from None

    def agent_token(self, repository, permissions=None):
        """Mint a repository-scoped agent token under D47."""
        return self.token(repository=repository, permissions=permissions)

    @classmethod
    def for_project(cls, cfg, pol, cache_dir=None):
        """Resolve the GitHub App installation for a project configuration."""
        from . import config

        return config.project_github_app(cfg, pol, cache_dir=cache_dir)


def for_project(cfg, pol, cache_dir=None):
    """Resolve the GitHub App installation for a project configuration."""
    from . import config

    return config.project_github_app(cfg, pol, cache_dir=cache_dir)
