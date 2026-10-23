#!/usr/bin/env python3
"""Provider-independent executable pinned-source check for Hermes Agent v0.22.2.

Evaluates Hermes Agent contract behavior directly against the pinned upstream source:
commit d923a5eb487a17c0a68801bba9ef0f56c81040a8 (v0.22.2, 2026-10-15).
Standard library only: requires no external credentials or third-party packages.
"""

import argparse
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import types

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from mahler.platforms import DENY_STEMS

PINNED_COMMIT = "d923a5eb487a17c0a68801bba9ef0f56c81040a8"
PINNED_TAG = "v0.22.2"
UPSTREAM_REPO = "https://github.com/NousResearch/hermes-agent"

EXPECTED_OUTPUT = (
    "PASS: HERMES_HOME environment redirection isolates paths.",
    "PASS: CLI parser accepts -q, --format stream-json, --in, and -z.",
    "PASS: Finite turn exit codes verified (0=ok, 1=empty, 2=failed, 130=SIGINT).",
    "PASS: Unconditional approvals.deny rule matching against DENY_STEMS verified.",
    "PASS: auth.adopt_external_logins: false prevents external login adoption.",
    "PASS: Anonymous guest inference restricted to desktop backend (rejected on CLI).",
    "ALL PINNED-SOURCE CONTRACT CHECKS PASSED (exit code 0).",
)


def _get_commit_sha(repo_dir: str) -> str | None:
    """Return HEAD commit SHA of a git repository, or None if invalid."""
    try:
        res = subprocess.run(
            ["git", "-C", repo_dir, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None


def resolve_source(src_path: str | None = None) -> tuple[str, tempfile.TemporaryDirectory | None]:
    """Resolve upstream pinned tree from --src, HERMES_PINNED_SRC, or shallow git fetch."""
    candidate = src_path or os.environ.get("HERMES_PINNED_SRC")
    if candidate:
        if not os.path.isdir(candidate):
            raise RuntimeError(f"Hermes source path is not a directory: {candidate}")
        sha = _get_commit_sha(candidate)
        if sha != PINNED_COMMIT:
            raise RuntimeError(
                f"Hermes source at {candidate} has commit {sha!r}, expected pinned {PINNED_COMMIT}"
            )
        return candidate, None

    # Fetch shallowly into a temporary directory
    tmp = tempfile.TemporaryDirectory(prefix="hermes_pinned_")
    try:
        subprocess.run(
            ["git", "init"],
            cwd=tmp.name,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", UPSTREAM_REPO],
            cwd=tmp.name,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "fetch", "--depth", "1", "origin", f"refs/tags/{PINNED_TAG}"],
            cwd=tmp.name,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-c", "advice.detachedHead=false", "checkout", "FETCH_HEAD"],
            cwd=tmp.name,
            check=True,
            capture_output=True,
        )
        sha = _get_commit_sha(tmp.name)
        if sha != PINNED_COMMIT:
            raise RuntimeError(
                f"Fetched Hermes tag {PINNED_TAG} has commit {sha!r}, expected pinned {PINNED_COMMIT}"
            )
        return tmp.name, tmp
    except Exception:
        tmp.cleanup()
        raise


def check_hermes_home_isolation():
    """Verify HERMES_HOME redirects all config, env, and state paths."""
    with tempfile.TemporaryDirectory() as tmp_home:
        orig_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = tmp_home
        try:
            from hermes_constants import get_hermes_home, get_config_path, get_env_path
            assert str(get_hermes_home()) == tmp_home
            assert str(get_config_path()) == os.path.join(tmp_home, "config.yaml")
            assert str(get_env_path()) == os.path.join(tmp_home, ".env")
        finally:
            if orig_home is not None:
                os.environ["HERMES_HOME"] = orig_home
            else:
                os.environ.pop("HERMES_HOME", None)
    print("PASS: HERMES_HOME environment redirection isolates paths.")


def check_cli_argument_parsing():
    """Verify chat -q and -z oneshot CLI arguments and output formats."""
    from hermes_cli._parser import build_top_level_parser
    parser = build_top_level_parser()[0]
    args_chat = parser.parse_args([
        "chat", "-q", "build issue #1115", "--format", "stream-json", "--in", "/tmp/wt"
    ])
    assert args_chat.query == "build issue #1115"
    assert args_chat.output_format == "stream-json"
    assert args_chat.in_dir == "/tmp/wt"

    args_z = parser.parse_args([
        "-z", "build issue #1115", "--usage-file", "/tmp/usage.json", "--in", "/tmp/wt"
    ])
    assert args_z.oneshot == "build issue #1115"
    assert args_z.usage_file == "/tmp/usage.json"
    assert args_z.in_dir == "/tmp/wt"
    print("PASS: CLI parser accepts -q, --format stream-json, --in, and -z.")


def check_oneshot_exit_codes():
    """Verify process exit code mapping for finite turns."""
    from hermes_cli.oneshot import _oneshot_exit_code
    assert _oneshot_exit_code("ok", {"completed": True}) == 0
    assert _oneshot_exit_code("", {"completed": True}) == 1
    assert _oneshot_exit_code("failed", {"failed": True}) == 2
    assert _oneshot_exit_code(None, {"interrupted": True}) == 130
    print("PASS: Finite turn exit codes verified (0=ok, 1=empty, 2=failed, 130=SIGINT).")


def check_unconditional_deny_floors():
    """Verify approvals.deny matching against Mahler DENY_STEMS."""
    deny_globs = [stem + "*" for stem in DENY_STEMS]
    mock_ctx = types.ModuleType("tools.approval_context")
    mock_ctx._get_approval_config = lambda: {"deny": deny_globs}
    sys.modules["tools.approval_context"] = mock_ctx

    from tools.approval_floors import _match_user_deny_rule
    assert _match_user_deny_rule("git push --force origin main") == "git push --force*"
    assert _match_user_deny_rule("git push -f origin feat") == "git push -f*"
    assert _match_user_deny_rule("git reset --hard HEAD~1") == "git reset --hard*"
    assert _match_user_deny_rule("gh repo delete test/repo") == "gh repo delete*"
    assert _match_user_deny_rule("rm -rf /") == "rm -rf /*"
    assert _match_user_deny_rule("git status") is None
    assert _match_user_deny_rule("python3 -m unittest discover") is None
    print("PASS: Unconditional approvals.deny rule matching against DENY_STEMS verified.")


def check_external_logins_optout():
    """Verify auth.adopt_external_logins: false disables borrowing."""
    mock_cfg = types.ModuleType("hermes_cli.config")
    mock_cfg.load_config_readonly = lambda: {"auth": {"adopt_external_logins": False}}
    sys.modules["hermes_cli.config"] = mock_cfg

    from agent.credential_sources import adopt_external_logins_enabled
    assert adopt_external_logins_enabled() is False
    print("PASS: auth.adopt_external_logins: false prevents external login adoption.")


def check_anonymous_cli_refusal():
    """Verify anonymous free tier inference is restricted to desktop backend and rejected in CLI."""
    mock_auth = types.ModuleType("hermes_cli.auth")
    mock_auth._load_auth_store = lambda: {}
    mock_auth._load_provider_state = lambda store, p: {"auth_method": "anonymous", "welcome_claimed": True}
    mock_auth.resolve_provider = lambda p: "nous"
    mock_auth.is_guest_state = lambda s: bool(s and s.get("auth_method") == "anonymous")
    sys.modules["hermes_cli.auth"] = mock_auth

    from hermes_cli.anon_challenge import client_surface
    from hermes_cli.anon_auth import on_local_desktop_backend, free_inference_active
    assert client_surface() == "cli"
    assert on_local_desktop_backend() is False
    assert free_inference_active() is False
    print("PASS: Anonymous guest inference restricted to desktop backend (rejected on CLI).")


def run_checks(hermes_src_dir: str):
    """Run all contract checks with isolated sys.modules and sys.path."""
    orig_path = list(sys.path)
    orig_modules = dict(sys.modules)

    # Ensure third-party YAML dependencies are stubbed for hermes modules
    mock_yaml = types.ModuleType("hermes_yaml")
    mock_ruamel = types.ModuleType("ruamel")
    mock_ruamel.yaml = types.ModuleType("ruamel.yaml")
    mock_ruamel.yaml.YAML = object
    sys.modules["hermes_yaml"] = mock_yaml
    sys.modules["ruamel"] = mock_ruamel
    sys.modules["ruamel.yaml"] = mock_ruamel.yaml

    sys.path.insert(0, hermes_src_dir)
    try:
        check_hermes_home_isolation()
        check_cli_argument_parsing()
        check_oneshot_exit_codes()
        check_unconditional_deny_floors()
        check_external_logins_optout()
        check_anonymous_cli_refusal()
        print("ALL PINNED-SOURCE CONTRACT CHECKS PASSED (exit code 0).")
    finally:
        sys.path[:] = orig_path
        for mod_name in list(sys.modules.keys()):
            if mod_name not in orig_modules:
                del sys.modules[mod_name]
            else:
                sys.modules[mod_name] = orig_modules[mod_name]


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Hermes Agent contract against pinned source.")
    parser.add_argument("--src", help="Path to local NousResearch/hermes-agent checkout")
    args = parser.parse_args()

    tmp_holder = None
    try:
        src_dir, tmp_holder = resolve_source(args.src)
    except Exception as exc:
        print(f"ERROR: Failed to resolve Hermes pinned source: {exc}", file=sys.stderr)
        return 1

    try:
        print(f"Using Hermes source: {src_dir} (commit {PINNED_COMMIT})", file=sys.stderr)
        run_checks(src_dir)
        return 0
    finally:
        if tmp_holder is not None:
            tmp_holder.cleanup()


if __name__ == "__main__":
    sys.exit(main())
