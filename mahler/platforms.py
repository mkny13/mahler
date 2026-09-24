"""Platform adapters: how to launch each CLI headless, and how to read its quota.

Verified against the real CLIs on 2026-09-12 (DESIGN D8):
  * claude -p --output-format stream-json --verbose emits `rate_limit_event`
    lines carrying unifiedWindows.five_hour / seven_day utilization (0..1).
    A lean probe (haiku, no tools, no MCP, no settings) costs ~700 tokens.
    NOT --bare: bare mode skips OAuth and silently does nothing.
  * agy -p ignores its cwd unless given --add-dir; `agy -p /usage
    --output-format json` reports remaining_fraction per pool and window, for
    free. --print-timeout defaults to 5m, so runs must raise it.

Verified against the real CLI on 2026-09-13 (mahler#157):
  * `codex exec --ephemeral --dangerously-bypass-approvals-and-sandbox
    --color never --json -C <worktree> <prompt>` uses the Codex CLI's existing
    ChatGPT login and emits JSONL. Final text is an `item.completed` event whose
    item type is `agent_message`; a successful turn ends with `turn.completed`.
    The exec stream has no account-wide quota field. The zero-token app-server
    account/rateLimits/read probe supplies it separately (mahler#276).

Verified against the real CLIs on 2026-09-13 (mahler#25):
  * `copilot -p <prompt> --allow-all-tools --output-format json -C <dir>`
    (binary `copilot`, package `@github/copilot`) ran a real end-to-end
    non-interactive prompt against this machine's GitHub-Education Copilot
    license. --allow-all-tools is required for non-interactive mode. JSONL
    events: assistant text arrives as `{"type":"assistant.message","data":
    {"content": "..."}}`; the run ends with `{"type":"result","exitCode":0,
    "usage":{...}}`. That per-run event has no account-wide quota, but the
    account-wide cap turned out to be probeable a different way — see below.

  * `kilo run <message> --dir <dir> --auto --format json` (binary `kilo`,
    package `@kilocode/cli`) — flags confirmed from `--help`, but auth
    (`kilo auth login`, a browser flow only the account owner can do) wasn't
    available to verify a real successful run's JSON shape, only a failure:
    `{"type":"error","error":{"data":{"statusCode":401,...}}}`. read_log
    falls back to a generic string-walk over each event for kilo, so a run
    still surfaces its STATUS line even if the exact event schema drifts.

Verified against the real CLI on 2026-09-13, after login (mahler#29):
  * Kilo's default model needs an explicit free route — without one, every
    run fails immediately with `{"type":"error","error":{"data":
    {"statusCode":402,"message":"Add credits to continue, or switch to a
    free model"}}}` (`error_type: "usage_limit_exceeded"`, no "quota" in the
    text). `kilo/kilo-auto/free` works and auto-picks among Kilo's `:free`
    models. A real success event looks like `{"type":"text","part":
    {"type":"text","text":"..."}}` ... `{"type":"step_finish","part":
    {"type":"step-finish","reason":"stop",...}}` — matched by the generic
    string-walk (it finds "text" nested under "part") without changes.

Verified 2026-09-12 (mahler#38): unlike Cline/Kilo, Copilot's cap is real and
checkable, just not from the CLI. GitHub Copilot moved off "premium requests"
to "AI Credits" billing on 2026-06-01 (1 credit = $0.01; Pro/Education include
1500/month). `gh api /users/<login>/settings/billing/ai_credit/usage` reports
this month's consumption (`usageItems[].grossQuantity`, one row per model) —
confirmed live against this account. It needs the `user` OAuth scope on the
`gh` token (`gh auth refresh -h github.com -s user`) and only reports
consumption, not the cap, so the 1500/month figure is config
(`monthly_cap_credits`), not something the response carries. Routed as a
normal metered platform with a single "monthly" window (see router.py's
per-platform `pconf["windows"]`), re-probed at most every `stale_minutes`.
"""

import json
import math
import os
import re
import subprocess
import selectors
import time
from datetime import datetime, timezone

from . import redact

HOME = os.path.expanduser("~")

# Free-tier exhaustion doesn't always say "quota": Kilo's out-of-credits error
# (mahler#29) is "Add credits to continue" / error_type "usage_limit_exceeded".
QUOTA_WORDS = ("rate limit", "429", "quota", "credit", "usage limit",
               "usage_limit_exceeded")

# Transient network drops and session errors that free builders can resume from (mahler#426).
NETWORK_ERRORS = (
    "network connection lost",
    "the socket connection was closed unexpectedly",
    "the operation timed out",
    "session not found",
)


def is_network_error(text):
    """Whether error text matches known transient network drops (mahler#426)."""
    if not text:
        return False
    lower = str(text).lower()
    return any(p in lower for p in NETWORK_ERRORS)


# A CLI rejecting the configured model is a permanent problem for that
# variant, not a rate limit — distinct from QUOTA_WORDS so a bad model pin
# never gets treated as a quota hit (issue #420, D33). Phrasing collected
# from real errors, e.g. the Codex 400 that disabled codex-high on the
# personal account.
MODEL_UNAVAILABLE_WORDS = (
    "model not found", "unknown model", "invalid model", "not a valid model",
    "unsupported model", "model is not supported", "does not support model",
    "model not allowed", "model not enabled", "model_not_found",
)


def is_model_unavailable(text):
    """Whether error text says a model isn't supported, found or allowed."""
    if not text:
        return False
    lower = str(text).lower()
    return any(p in lower for p in MODEL_UNAVAILABLE_WORDS)

# Guardrails (DESIGN D12): destructive command stems agents must never run.
# Kept platform-neutral; each argv builder renders its own CLI's deny syntax.
# These are prefix rules — a safety net against accidents, not a fence against
# a determined agent (a reordered flag or `git push origin :branch` evades
# them). Branch protection and the lease pre-push hook remain the real fence.
# Known unmatchable-by-prefix gaps, accepted in mahler#77: mid-string refspec
# deletions (`git push origin :branch`), `git checkout -- <path>`, `gh repo
# edit --visibility`, and `gh api --method DELETE` (a `gh api` prefix rule
# would block all API use).
DENY_STEMS = [
    # remote history and refs
    "git push --force", "git push -f", "git push --force-with-lease",
    "git push --delete", "git push -d", "git push --mirror",
    "git filter-branch", "git filter-repo", "git reset --hard",
    "git worktree remove",
    # GitHub data
    "gh repo delete", "gh repo archive", "gh release delete", "gh issue delete",
    # filesystem (both flag orders)
    "rm -rf /", "rm -rf ~", "rm -fr /", "rm -fr ~",
]

# Claude: --disallowedTools takes Bash prefix rules, `Bash(<stem>:*)`.
CLAUDE_DENY = [f"Bash({stem}:*)" for stem in DENY_STEMS]
# Copilot: shell prefix rules, `shell(<stem>:*)`. Per `copilot help permissions`
# (verified 2026-09-13): "Denial rules always take precedence over allow rules,
# even --allow-all-tools."
COPILOT_DENY = [f"shell({stem}:*)" for stem in DENY_STEMS]


def which(binary, fallbacks=()):
    for d in os.environ.get("PATH", "").split(os.pathsep) + list(fallbacks):
        p = os.path.join(d, binary)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def claude_exe():
    return which("claude", [os.path.join(HOME, ".local/bin")])


def agy_exe():
    return which("agy", [os.path.join(HOME, ".local/bin")])


def cline_exe():
    return which("cline", ["/opt/homebrew/bin"])


def copilot_exe():
    return which("copilot", [os.path.join(HOME, ".local/bin"), "/opt/homebrew/bin"])


def codex_exe():
    return which("codex", [os.path.join(HOME, ".local/bin"), "/opt/homebrew/bin",
                           "/Applications/Codex.app/Contents/Resources"])


def kilo_exe():
    return which("kilo", [os.path.join(HOME, ".local/bin"), "/opt/homebrew/bin"])


def _epoch_iso(secs):
    return datetime.fromtimestamp(int(secs), timezone.utc).isoformat() if secs else None


# ---------- argv builders ----------

EFFORTS = {
    "claude": {"low", "medium", "high", "xhigh", "max"},
    "codex": {"minimal", "low", "medium", "high", "xhigh"},
    "copilot": {"none", "minimal", "low", "medium", "high", "xhigh", "max"},
    "agy": {"low", "medium", "high"},
    "cline": {"none", "low", "medium", "high", "xhigh"},
    "kilo": None,                 # provider-specific free-form variants
}


def effort_value(pconf, role):
    """Return the configured, valid effort/variant for this role, or None."""
    kind = pconf.get("kind")
    value = (pconf.get(f"{role}_effort") if kind == "claude" else None) or pconf.get("effort")
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    if not value or (EFFORTS.get(kind) is not None and value not in EFFORTS[kind]):
        return None
    return value


def effort_args(pconf, role):
    """CLI arguments for configured effort, preserving an empty default."""
    value = effort_value(pconf, role)
    if value is None:
        return []
    flag = {"claude": "--effort", "codex": "-c", "copilot": "--reasoning-effort",
            "agy": "--effort", "cline": "--thinking", "kilo": "--variant"}[pconf["kind"]]
    if pconf["kind"] == "codex":
        return [flag, f'model_reasoning_effort="{value}"']
    return [flag, value]


def effort_warnings(cfg):
    """One warning per platform with an invalid configured effort."""
    out = []
    for name, pconf in cfg.get("platforms", {}).items():
        for key in ("effort", "sort_effort", "build_effort"):
            if key not in pconf or not pconf[key]:
                continue
            role = key[:-7] if key.endswith("_effort") else "build"
            if effort_value({**pconf, key: pconf[key]}, role) != str(pconf[key]).strip():
                out.append(f"warning: platform {name} has invalid {key} {pconf[key]!r}; using CLI default")
                break
    return out

def claude_argv(pconf, prompt, worktree, role):
    model = pconf.get("sort_model") if role == "sort" else pconf.get("build_model")
    argv = [claude_exe(), "-p", prompt, "--output-format", "stream-json", "--verbose",
            "--permission-mode", "bypassPermissions", "--disallowedTools", *CLAUDE_DENY]
    if model:
        argv += ["--model", model]
    argv += effort_args(pconf, role)
    return argv


def agy_argv(pconf, prompt, worktree, role, timeout_minutes=60):
    # Known guardrail gap (mahler#77, accepted): agy exposes no deny-list flag —
    # `agy --help` offers only the coarse `--sandbox` ("terminal restrictions")
    # next to the `--dangerously-skip-permissions` Mahler needs for unattended
    # runs, and combining them unverified would risk breaking free-tier runs.
    # Nothing to wire; re-check `agy --help` on CLI upgrades.
    argv = [agy_exe(), "-p", prompt, "--add-dir", worktree,
            "--dangerously-skip-permissions", "--output-format", "stream-json",
            "--print-timeout", f"{int(timeout_minutes)}m"]
    if pconf.get("model"):
        argv += ["--model", pconf["model"]]
    argv += effort_args(pconf, role)
    return argv


def cline_argv(pconf, prompt, worktree, role, timeout_minutes=60):
    # Known guardrail gap (mahler#77, accepted): the cline CLI has no deny-list
    # flag — `--auto-approve` is all-or-nothing. `--hooks-dir` could in
    # principle inject a PreToolUse-style command filter, but the hook payload
    # contract is unverified (DESIGN.md's open cline question); revisit there
    # before wiring one.
    argv = [cline_exe(), "--cwd", worktree, "--json", "--auto-approve", "true",
            "-t", str(int(timeout_minutes) * 60)]
    if pconf.get("model"):
        argv += ["-m", pconf["model"]]
    return argv + effort_args(pconf, role) + [prompt]


def copilot_argv(pconf, prompt, worktree, role, timeout_minutes=60):
    argv = [copilot_exe(), "-p", prompt, "-C", worktree,
            "--allow-all-tools", "--allow-all-paths", "--allow-all-urls",
            "--output-format", "json", "--no-color", "--no-auto-update"]
    # Denials take precedence over allow rules (`copilot help
    # permissions`), so these stay enforced in the all-tools mode runs need.
    for pattern in COPILOT_DENY:
        argv += ["--deny-tool", pattern]
    if pconf.get("model"):
        argv += ["--model", pconf["model"]]
    if pconf.get("auto_tier"):
        argv += ["--auto-tier", pconf["auto_tier"]]
    argv += effort_args(pconf, role)
    return argv


def codex_argv(pconf, prompt, worktree, role, timeout_minutes=60):
    # Known guardrail gap (mahler#77, accepted): codex has no deny-list flag.
    # execpolicy `.rules` files are loaded unless `--ignore-rules` and could
    # deny commands, but they live in $CODEX_HOME or the project — outside this
    # repo — and their effect under `--ephemeral` + bypass is unverified.
    # Future work, not a flag to wire today.
    argv = [codex_exe(), "exec", "--ephemeral",
            "--dangerously-bypass-approvals-and-sandbox",
            "--color", "never", "--json", "-C", worktree]
    if pconf.get("model"):
        argv += ["--model", pconf["model"]]
    argv += effort_args(pconf, role)
    return argv + [prompt]


def kilo_argv(pconf, prompt, worktree, role, timeout_minutes=60):
    # Known guardrail gap (mahler#77, accepted): `kilo run` exposes no
    # deny-list flag; `--auto` approves every tool. Accepted for the free-tier
    # runner (build order last, size s only).
    argv = [kilo_exe(), "run"] + effort_args(pconf, role) + [prompt, "--dir", worktree, "--auto", "--format", "json"]
    if pconf.get("model"):
        argv += ["-m", pconf["model"]]
    return argv


def argv_for(pconf, prompt, worktree, role, timeout_minutes):
    if pconf["kind"] == "claude":
        return claude_argv(pconf, prompt, worktree, role)
    if pconf["kind"] == "agy":
        return agy_argv(pconf, prompt, worktree, role, timeout_minutes)
    if pconf["kind"] == "cline":
        return cline_argv(pconf, prompt, worktree, role, timeout_minutes)
    if pconf["kind"] == "copilot":
        return copilot_argv(pconf, prompt, worktree, role, timeout_minutes)
    if pconf["kind"] == "codex":
        return codex_argv(pconf, prompt, worktree, role, timeout_minutes)
    if pconf["kind"] == "kilo":
        return kilo_argv(pconf, prompt, worktree, role, timeout_minutes)
    raise ValueError(f"unknown platform kind {pconf['kind']!r}")


def cline_resume_argv(pconf, prompt, worktree, role, timeout_minutes=60, session_id=None):
    # Verified with Cline 3.0.64 (mahler#426):
    # Exact commands tested:
    #   1. `cline --id <session-id> --cwd <worktree> --json --auto-approve true -t <secs> < <resume_file>`
    #   2. `cline --id <session-id> --json "prompt"`
    # Both fail with:
    #   {"ts":"...","type":"error","message":"JSON output mode requires a prompt argument or piped stdin (interactive mode is unsupported)"}
    # because `--id` sets `interactive = true` in the CLI parser before stdin is checked.
    # Stdin redirection does not bypass this check because `r.interactive` is checked
    # before stdin is read.
    # Therefore, Cline resume always falls back to a fresh cline run in the same worktree
    # with the prompt as positional argument (and stdin redirection via runner.spawn):
    #   `cline --cwd <wt> --json --auto-approve true -t <secs> [-m <model>] <prompt> < <resume_file>`
    argv = [cline_exe(), "--cwd", worktree, "--json", "--auto-approve", "true",
            "-t", str(int(timeout_minutes) * 60)]
    if pconf.get("model"):
        argv += ["-m", pconf["model"]]
    return argv + effort_args(pconf, role) + [prompt]


def kilo_resume_argv(pconf, prompt, worktree, role, timeout_minutes=60, session_id=None):
    # Verified with Kilo 7.6.2 (mahler#426):
    # Exact command tested:
    #   `kilo run <prompt> --session <id> --dir <wt> --auto --format json [-m <model>] < <resume_file>`
    # Kilo CLI accepts `-s/--session <id>` to continue an existing session.
    # When session_id is None (or "session not found" occurred), it falls back to a fresh run
    # without `--session` in the same worktree:
    #   `kilo run <prompt> --dir <wt> --auto --format json [-m <model>] < <resume_file>`
    argv = [kilo_exe(), "run"] + effort_args(pconf, role) + [prompt]
    if session_id:
        argv += ["--session", session_id]
    argv += ["--dir", worktree, "--auto", "--format", "json"]
    if pconf.get("model"):
        argv += ["-m", pconf["model"]]
    return argv


def resume_argv_for(pconf, prompt, worktree, role, timeout_minutes, session_id=None):
    if pconf["kind"] == "cline":
        return cline_resume_argv(pconf, prompt, worktree, role, timeout_minutes, session_id=session_id)
    if pconf["kind"] == "kilo":
        return kilo_resume_argv(pconf, prompt, worktree, role, timeout_minutes, session_id=session_id)
    raise ValueError(f"resume not supported for platform kind {pconf['kind']!r}")


def available(pconf):
    exe = {"claude": claude_exe, "agy": agy_exe, "cline": cline_exe,
           "copilot": copilot_exe, "codex": codex_exe,
           "kilo": kilo_exe}[pconf["kind"]]()
    return exe is not None


# ---------- quota ----------

def claude_samples_from_event(ev):
    """rate_limit_event -> [(window, used_pct, resets_at_iso)]"""
    info = ev.get("rate_limit_info") or {}
    wins = info.get("unifiedWindows") or {}
    out = []
    for src, dst in (("five_hour", "5h"), ("seven_day", "weekly")):
        w = wins.get(src)
        if w and w.get("utilization") is not None:
            out.append((dst, round(100 * float(w["utilization"]), 1), _epoch_iso(w.get("resetsAt"))))
    if info.get("status") == "rejected":           # hit the wall: treat as exhausted
        window = "5h" if info.get("rateLimitType") == "five_hour" else "weekly"
        out.append((window, 100.0, _epoch_iso(info.get("resetsAt"))))
    if info.get("isUsingOverage"):    # paid extra usage: treat as exhausted (mahler#136)
        window = "5h" if info.get("rateLimitType") == "five_hour" else "weekly"
        out.append((window, 100.0, _epoch_iso(info.get("resetsAt"))))
    return out


def _claude_oauth(keychain_service, credentials_file):
    try:
        if credentials_file:
            with open(credentials_file, encoding="utf-8") as fh:
                return json.load(fh).get("claudeAiOauth") or {}
        if not keychain_service:
            return {}
        raw = subprocess.run(["security", "find-generic-password", "-s",
                              keychain_service, "-w"],
                             capture_output=True, text=True, timeout=10)
        return json.loads(raw.stdout).get("claudeAiOauth") or {}
    except (subprocess.SubprocessError, OSError, ValueError):
        return {}


def oauth_usage(keychain_service="Claude Code-credentials", credentials_file=None):
    """Zero-token Claude reading — the same endpoint Claude Code's /usage and the
    Claude Usage menu-bar app use: GET api.anthropic.com/api/oauth/usage with
    Claude Code's own OAuth access token from the login keychain (or, for a
    login kept in its own config dir, the keychain entry or credentials file
    that account's config names — DESIGN D25).

    The token is read fresh each time, held only in memory, sent only to
    Anthropic, and never logged or written anywhere. Mahler never refreshes it
    (that would race Claude Code's own refresh); an expired token just means
    this source is skipped until Claude Code next refreshes it.
    -> [(window, used_pct, resets_iso)] or [] if unavailable."""
    import urllib.error
    import urllib.request
    oauth = _claude_oauth(keychain_service, credentials_file)
    token, expires = oauth.get("accessToken"), oauth.get("expiresAt")
    if not token or (expires and expires / 1000 < datetime.now(timezone.utc).timestamp()):
        return []
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
        "Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "mahler"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.load(r)
    except (urllib.error.URLError, OSError, ValueError):
        return []
    out = []
    for src, dst in (("five_hour", "5h"), ("seven_day", "weekly")):
        w = body.get(src) or {}
        if w.get("utilization") is not None:
            out.append((dst, float(w["utilization"]), w.get("resets_at")))
    return out


class CodexUsage(list):
    """Routing samples plus a sanitized, read-only account summary."""

    def __init__(self, samples, metadata):
        super().__init__(samples)
        self.metadata = metadata


def _codex_usage(result):
    limits = result["rateLimits"]
    samples, windows = [], []
    for key in ("primary", "secondary"):
        window = limits.get(key)
        if window is None:
            continue
        minutes, pct = window["windowDurationMins"], window["usedPercent"]
        if (type(minutes) is not int or minutes <= 0 or
                type(pct) not in (int, float) or not math.isfinite(pct) or pct < 0):
            raise ValueError("invalid quota window")
        reset = _epoch_iso(window.get("resetsAt"))
        label = {300: "5h", 10080: "weekly"}.get(minutes)
        if label:
            samples.append((label, pct, reset))
        windows.append({"window": label or f"{minutes}m", "used_pct": pct,
                        "resets_at": reset})
    credits = result.get("rateLimitResetCredits") or {}
    count = credits.get("availableCount")
    if count is not None and (type(count) is not int or count < 0):
        raise ValueError("invalid reset credit count")
    expiries = [_epoch_iso(c.get("expiresAt")) for c in credits.get("credits", [])
                if c.get("status") == "available"]
    return CodexUsage(samples, {
        "windows": windows,
        "blocked": result.get("ordinaryUsageAllowed") is False,
        "reset_credits": count,
        "credit_expiries": sorted(e for e in expiries if e),
    })


def probe_codex(env=None, timeout=15):
    """Read account quota over newline JSON-RPC, without starting a model turn.

    stdio is app-server's default transport (older CLIs lack --stdio).
    A single deadline covers initialization and the read; always reap the child.
    No credentials, credit IDs, or server diagnostics are retained.
    """
    exe = codex_exe()
    if not exe:
        return []
    proc = None
    try:
        proc = subprocess.Popen([exe, "app-server"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        deadline, pending = time.monotonic() + timeout, b""
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)

            def send(message):
                proc.stdin.write((json.dumps(message) + "\n").encode())
                proc.stdin.flush()

            def receive(wanted):
                nonlocal pending
                while time.monotonic() < deadline:
                    if b"\n" not in pending:
                        if not selector.select(max(0, deadline - time.monotonic())):
                            raise TimeoutError()
                        chunk = os.read(proc.stdout.fileno(), 65536)
                        if not chunk:
                            raise ValueError("app-server closed")
                        pending += chunk
                        if len(pending) > 1048576:
                            raise ValueError("oversized response")
                        continue
                    line, pending = pending.split(b"\n", 1)
                    message = json.loads(line)
                    if message.get("id") == wanted:
                        return message["result"]
                raise TimeoutError()

            send({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "mahler-probe", "version": "0.1.0"}}})
            receive(1)
            send({"method": "initialized"})
            send({"id": 2, "method": "account/rateLimits/read", "params": {}})
            return _codex_usage(receive(2))
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError,
            AttributeError, OverflowError):
        return []
    finally:
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.kill()
            except ProcessLookupError:
                pass
            proc.wait()
            proc.stdin.close()
            proc.stdout.close()


def probe_claude(env=None):
    exe = claude_exe()
    if not exe:
        return []
    try:
        r = subprocess.run(
            [exe, "-p", "ok", "--model", "claude-haiku-4-5-20251001", "--tools", "",
             "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
             "--system-prompt", "Reply ok.", "--output-format", "stream-json", "--verbose"],
            capture_output=True, text=True, timeout=90, cwd=HOME, env=env)
    except (subprocess.SubprocessError, OSError):
        return []
    samples = []
    for line in r.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "rate_limit_event":
            samples = claude_samples_from_event(ev)
    return samples


def parse_agy_usage(data):
    """`agy -p /usage --output-format json` -> {pool_name: [(window, used_pct, resets)]}"""
    out = {}
    groups = (((data or {}).get("command") or {}).get("data") or {}).get("groups") or []
    for g in groups:
        rows = []
        for b in g.get("buckets", []):
            window = {"5h": "5h", "weekly": "weekly"}.get(b.get("window"))
            if window and b.get("remaining_fraction") is not None:
                rows.append((window, round(100 * (1 - float(b["remaining_fraction"])), 1),
                             b.get("reset_time")))
        out[g.get("name")] = rows
    return out


def probe_agy():
    exe = agy_exe()
    if not exe:
        return {}
    try:
        r = subprocess.run([exe, "-p", "/usage", "--output-format", "json", "--print-timeout", "1m"],
                           capture_output=True, text=True, timeout=90, cwd=HOME)
        return parse_agy_usage(json.loads(r.stdout))
    except (subprocess.SubprocessError, OSError, ValueError):
        return {}


def _next_month_start(now):
    year, month = (now.year, now.month + 1) if now.month < 12 else (now.year + 1, 1)
    return datetime(year, month, 1, tzinfo=timezone.utc)


def _gh_login(env=None):
    try:
        r = subprocess.run(["gh", "api", "user", "--jq", ".login"],
                           capture_output=True, text=True, timeout=15, env=env)
        return (r.stdout.strip() or None) if r.returncode == 0 else None
    except (subprocess.SubprocessError, OSError):
        return None


class CopilotNoQuota(list):
    """An empty successful billing report, distinct from a failed probe.

    Org-assigned seats can return this from the personal credits endpoint.
    It is evidence of no usable meter, not proof of a particular seat type.
    """


def probe_copilot(monthly_cap_credits, env=None):
    """GitHub's AI-credits billing report (mahler#38): consumption only, no
    cap in the response, so `monthly_cap_credits` (plan-fixed, config) is what
    turns it into a percentage. Needs the `user` OAuth scope on the `gh` token.
    -> [("monthly", used_pct, resets_at_iso)] or [] if unavailable."""
    login = _gh_login(env)
    if not login:
        return []
    try:
        r = subprocess.run(["gh", "api", f"/users/{login}/settings/billing/ai_credit/usage"],
                           capture_output=True, text=True, timeout=20, env=env)
        data = json.loads(r.stdout)
    except (subprocess.SubprocessError, OSError, ValueError):
        return []
    if r.returncode != 0 or not isinstance(data, dict):
        return []
    items = data.get("usageItems")
    if not isinstance(items, list):
        return []
    if not items:
        return CopilotNoQuota()
    # A missing quantity is not a measured zero. Reject malformed reports.
    if any(not isinstance(item, dict)
           or isinstance(item.get("grossQuantity"), bool)
           or not isinstance(item.get("grossQuantity"), (int, float))
           or not math.isfinite(item["grossQuantity"])
           or item["grossQuantity"] < 0 for item in items):
        return []
    used = sum(item["grossQuantity"] for item in items)
    if not monthly_cap_credits or monthly_cap_credits < 0:
        return []
    pct = round(100 * used / monthly_cap_credits, 1)
    resets = _next_month_start(datetime.now(timezone.utc)).isoformat()
    return [("monthly", pct, resets)]


# ---------- run logs ----------

def _collect_text(obj, keys=("text", "content", "message", "delta", "deltaContent")):
    """Recursively pull string values out from under any of `keys`, in
    document order — a schema-agnostic fallback for platforms (kilo) whose
    exact JSON event shape isn't verified yet."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, str):
                out.append(v)
            else:
                out.extend(_collect_text(v, keys))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_collect_text(v, keys))
    return out


# mahler#124: Cline's daily cap names its own reset time, e.g. "Try again in
# 9h 41m" / "Try again in 2h" / "try again in 45m" — matched case-insensitively.
_RETRY_AFTER_RE = re.compile(
    r"try\s+again\s+in\s+(?:(\d+)\s*h(?:\s*(\d+)\s*m)?|(\d+)\s*m)", re.IGNORECASE)


def retry_after_minutes(text):
    """Minutes until the reset time named by the last "try again in ..." in
    text, or None if text names none."""
    matches = list(_RETRY_AFTER_RE.finditer(text or ""))
    if not matches:
        return None
    m = matches[-1]
    hours = int(m.group(1)) if m.group(1) else 0
    minutes = m.group(2) or m.group(3)
    return hours * 60 + (int(minutes) if minutes else 0)


def _note_quota_hit(res, ev):
    """Record a quota hit and, if the event names a reset time, keep the
    largest retry_after minutes seen across the log (mahler#124)."""
    res["quota_hit"] = True
    ra = retry_after_minutes(json.dumps(ev))
    if ra is not None and (res["retry_after"] is None or ra > res["retry_after"]):
        res["retry_after"] = ra


def _extract_error_message(ev):
    """Extract a human-readable error message from an agent JSON event."""
    err = ev.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or (err.get("data") or {}).get("message")
        if msg:
            return msg
    elif err is not None:
        return str(err)
    if ev.get("message"):
        return ev["message"]
    if ev.get("text"):
        return ev["text"]
    return json.dumps(ev)


def read_log(path, kind, model=None):
    """Summarise a run's stream-json log.

    Returns {'final': str|None, 'ok': bool|None, 'usage': [(window, pct, resets)],
             'quota_hit': bool, 'overage': bool, 'retry_after': int|None,
             'last_text': str, 'model': str|None, 'session_id': str|None,
             'last_error': str|None, 'model_unavailable': bool,
             'tokens': {in, cached, out, reasoning},
             'cost_usd': float|None, 'credits': float|None, 'quota_used': dict}
    Missing token usage is represented by None counts, never invented zeros.
    """
    res = {"final": None, "ok": None, "usage": [], "quota_hit": False, "overage": False,
           "retry_after": None, "last_text": "", "model": model, "session_id": None,
           "last_error": None, "model_unavailable": False,
           "tokens": dict.fromkeys(("in", "cached", "out", "reasoning")),
           "cost_usd": None, "credits": None, "quota_used": {}}
    from .run_usage import collect
    first_quota = {}
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return res
    texts = []
    with fh:
        for line in fh:
            try:
                ev = json.loads(line)
            except ValueError:
                line_str = line.strip()
                if line_str:
                    texts.append(line_str)
                    if is_network_error(line_str) or line_str.lower().startswith("error:"):
                        res["last_error"] = line_str
                    if line_str.lower().startswith("error:") and is_model_unavailable(line_str):
                        res["model_unavailable"] = True
                continue
            if not isinstance(ev, dict):
                continue
            collect(res, ev, kind)
            if kind == "claude":
                t = ev.get("type")
                if t == "rate_limit_event":
                    res["usage"] = claude_samples_from_event(ev)
                    for window, pct, resets in res["usage"]:
                        first_quota.setdefault(window, pct)
                        res["quota_used"][window] = round(pct - first_quota[window], 1)
                    info = ev.get("rate_limit_info") or {}
                    if info.get("status") == "rejected":
                        _note_quota_hit(res, ev)
                    if info.get("isUsingOverage"):    # paid extra usage: stop at once (mahler#136)
                        res["overage"] = True
                        _note_quota_hit(res, ev)
                elif t == "assistant":
                    for block in (ev.get("message") or {}).get("content") or []:
                        if block.get("type") == "text" and block.get("text"):
                            texts.append(block["text"])
                elif t == "result":
                    res["final"] = ev.get("result")
                    res["ok"] = ev.get("subtype") == "success" and not ev.get("is_error")
                    if ev.get("is_error") and is_model_unavailable(json.dumps(ev)):
                        res["model_unavailable"] = True
            elif kind == "cline":
                if ev.get("type") == "run_result":
                    res["final"] = ev.get("text")
                    res["ok"] = ev.get("finishReason") == "completed"
                    if not res["ok"]:
                        blob = json.dumps(ev).lower()
                        if any(w in blob for w in QUOTA_WORDS):
                            _note_quota_hit(res, ev)
                        if any(w in blob for w in MODEL_UNAVAILABLE_WORDS):
                            res["model_unavailable"] = True
                        if ev.get("text"):
                            res["last_error"] = ev.get("text")
                elif ev.get("type") == "error" or ev.get("error"):
                    blob = json.dumps(ev).lower()
                    if any(w in blob for w in QUOTA_WORDS):
                        _note_quota_hit(res, ev)
                    if any(w in blob for w in MODEL_UNAVAILABLE_WORDS):
                        res["model_unavailable"] = True
                    res["last_error"] = _extract_error_message(ev)
            elif kind == "copilot":
                t = ev.get("type")
                if t == "assistant.message":
                    c = (ev.get("data") or {}).get("content")
                    if c:
                        res["final"] = c
                        texts.append(c)
                elif t == "result":
                    res["ok"] = ev.get("exitCode") == 0
                elif t == "error" or "error" in (t or ""):
                    blob = json.dumps(ev).lower()
                    if any(w in blob for w in QUOTA_WORDS):
                        _note_quota_hit(res, ev)
                    if any(w in blob for w in MODEL_UNAVAILABLE_WORDS):
                        res["model_unavailable"] = True
            elif kind == "codex":
                t = ev.get("type")
                if t == "item.completed":
                    item = ev.get("item") or {}
                    if item.get("type") == "agent_message" and item.get("text"):
                        res["final"] = item["text"]
                        texts.append(item["text"])
                elif t == "turn.completed":
                    res["ok"] = True
                elif t in {"turn.failed", "error"} or "error" in (t or ""):
                    res["ok"] = False
                    blob = json.dumps(ev).lower()
                    if any(w in blob for w in QUOTA_WORDS):
                        _note_quota_hit(res, ev)
                    if any(w in blob for w in MODEL_UNAVAILABLE_WORDS):
                        res["model_unavailable"] = True
            elif kind == "kilo":
                if ev.get("sessionID"):
                    res["session_id"] = ev.get("sessionID")
                if ev.get("type") == "error":
                    blob = json.dumps(ev).lower()
                    if any(w in blob for w in QUOTA_WORDS):
                        _note_quota_hit(res, ev)
                    if any(w in blob for w in MODEL_UNAVAILABLE_WORDS):
                        res["model_unavailable"] = True
                    res["last_error"] = _extract_error_message(ev)
                elif ev.get("type") == "step_finish":
                    # kilo-auto/free is stateless per invocation: each `kilo run`
                    # is a fresh routing decision across the free pool, so the
                    # model actually used is the signal for whether a quota hit
                    # reflects one underlying free model being rate-limited
                    # rather than the whole account/pool (mahler#141).
                    model = ((ev.get("part") or {}).get("model") or {}).get("modelID")
                    if model:
                        res["model"] = model
                else:
                    texts.extend(_collect_text(ev))
            else:  # agy
                if ev.get("event") == "result":
                    r = ev.get("result") or {}
                    res["final"] = r.get("response")
                    res["ok"] = r.get("status") == "SUCCESS"
                    if not res["ok"]:
                        blob = json.dumps(r).lower()
                        if "quota" in blob:
                            _note_quota_hit(res, r)
                        if is_model_unavailable(blob):
                            res["model_unavailable"] = True
                elif ev.get("event") == "step_update":
                    su = ev.get("step_update") or {}
                    if su.get("text_delta"):
                        texts.append(su["text_delta"])
    joined = "".join(texts) if kind == "agy" else "\n".join(texts)
    # The last_text feeds handoff comments (GitHub) and the final text can land
    # in a NEEDS-YOU ping (ntfy), so credential-shaped strings that leaked into
    # the agent's own output are masked before anything sees them (issue #76).
    res["final"] = redact.redact(res["final"])
    res["last_text"] = redact.redact((res["final"] or joined)[-1500:])
    return res


def status_line(text):
    """Last `STATUS: ...` line an agent printed -> (verb, rest) or (None, None)."""
    for line in reversed((text or "").splitlines()):
        line = line.strip().strip("`*")
        if line.upper().startswith("STATUS:"):
            body = line.split(":", 1)[1].strip()
            verb, _, rest = body.partition(" ")
            return verb.upper(), rest.strip()
    return None, None
