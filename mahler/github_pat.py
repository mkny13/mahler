"""Owner-provided fine-grained PATs as a project's scoped agent identity (D47, #1265).

Mahler cannot mint or narrow a PAT, so the owner scopes it on GitHub (one
repository, the D47 permission set). Config holds only the token file's path.
Every failure raises PatError, whose text names the path and the reason and
never the file's contents.
"""

import os
import stat

from . import redact


class PatError(ValueError):
    """Secret-safe: mentions paths and reasons, never token contents."""


def read_token(path, label="github_pat"):
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        raise PatError(f"{label}: token_file must be a path")
    try:
        st = os.stat(path)
    except OSError as e:
        raise PatError(f"{label}: token file {path} is not readable ({e.strerror or 'error'})") from None
    if not stat.S_ISREG(st.st_mode):
        raise PatError(f"{label}: token file {path} is not a regular file")
    if stat.S_IMODE(st.st_mode) != 0o600:
        raise PatError(f"{label}: token file {path} must have mode 0600, not {stat.S_IMODE(st.st_mode):04o}")
    if st.st_uid != os.getuid():
        raise PatError(f"{label}: token file {path} is not owned by the current user")
    try:
        with open(path, encoding="utf-8") as fh:
            token = fh.read().strip()
    except (OSError, UnicodeDecodeError):
        raise PatError(f"{label}: token file {path} is not readable") from None
    if not token:
        raise PatError(f"{label}: token file {path} is empty")
    if any(c.isspace() for c in token):
        raise PatError(f"{label}: token file {path} must hold a single token")
    return token


def bound_name(pol):
    name = pol.get("github_pat")
    return name if name else None


def resolve(cfg, pol):
    """Return (name, provider table) for a project bound to a PAT, else None."""
    name = bound_name(pol)
    if name is None:
        return None
    proj = pol.get("name", "project")
    if pol.get("github_provider") or isinstance(pol.get("github_app"), str) \
            or "github_app_installation_id" in pol:
        raise PatError(f"project {proj!r}: bind either a GitHub App or github_pat, not both")
    pats = cfg.get("github_pats") or {}
    if not isinstance(name, str) or not isinstance(pats, dict) or not isinstance(pats.get(name), dict):
        raise PatError(f"project {proj!r}: unknown github_pat {name!r}")
    prov = pats[name]
    repo = pol.get("repo", "")
    if prov.get("repository") not in (None, repo):
        raise PatError(f"project {proj!r}: github_pats.{name} is bound to a different repository than {repo!r}")
    return name, prov


def agent_token(cfg, pol):
    """The PAT value for this project's run, registered for redaction; None if unbound."""
    found = resolve(cfg, pol)
    if found is None:
        return None
    name, prov = found
    token = read_token(prov.get("token_file"), f"github_pats.{name}")
    redact.register(token)
    return token


def validate(cfg):
    pats = cfg.get("github_pats", {})
    if not isinstance(pats, dict):
        raise PatError("github_pats must be a table")
    token_files = {}
    for name, prov in pats.items():
        if not isinstance(prov, dict):
            raise PatError(f"github_pats.{name} must be a table")
        extra = set(prov) - {"token_file", "repository"}
        if extra:
            raise PatError(f"github_pats.{name}: unsupported keys {sorted(extra)}; config holds paths only")
        tf = prov.get("token_file")
        if not isinstance(tf, str) or not tf.strip() or "\x00" in tf:
            raise PatError(f"github_pats.{name} requires token_file")
        if tf in token_files:
            raise PatError(
                f"github_pats.{name}: token_file is shared with github_pats.{token_files[tf]}; "
                "use one token file per repository")
        token_files[tf] = name
        repo = prov.get("repository")
        if repo is not None and (not isinstance(repo, str) or repo.count("/") != 1 or not all(repo.split("/"))):
            raise PatError(f"github_pats.{name}: repository must be owner/repo")
    users = {}
    for proj, project in cfg.get("projects", {}).items():
        pol = {**project, "name": proj}
        if bound_name(pol) is None:
            continue
        resolve(cfg, pol)
        users.setdefault(project["github_pat"], []).append(proj)
    for name, projs in users.items():
        if len(projs) > 1:
            raise PatError(f"github_pats.{name} is shared by projects {projs}; use one token per repository")
    return {p for ps in users.values() for p in ps}
