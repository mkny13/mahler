"""Platform adapters: how to launch each CLI headless, and how to read its quota.

Verified against the real CLIs on 2026-09-12 (DESIGN D8):
  * claude -p --output-format stream-json --verbose emits `rate_limit_event`
    lines carrying unifiedWindows.five_hour / seven_day utilization (0..1).
    A lean probe (haiku, no tools, no MCP, no settings) costs ~700 tokens.
    NOT --bare: bare mode skips OAuth and silently does nothing.
  * agy -p ignores its cwd unless given --add-dir; `agy -p /usage
    --output-format json` reports remaining_fraction per pool and window, for
    free. --print-timeout defaults to 5m, so runs must raise it.

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
import os
import subprocess
from datetime import datetime, timezone

HOME = os.path.expanduser("~")

# Free-tier exhaustion doesn't always say "quota": Kilo's out-of-credits error
# (mahler#29) is "Add credits to continue" / error_type "usage_limit_exceeded".
QUOTA_WORDS = ("rate limit", "429", "quota", "credit", "usage_limit_exceeded")

# Guardrails for Claude runs — a safety net, not the plan (DESIGN D12).
CLAUDE_DENY = [
    "Bash(git push --force:*)", "Bash(git push -f:*)", "Bash(git push --force-with-lease:*)",
    "Bash(gh repo delete:*)", "Bash(gh release delete:*)", "Bash(git worktree remove:*)",
    "Bash(rm -rf /:*)", "Bash(rm -rf ~:*)",
]


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


def kilo_exe():
    return which("kilo", [os.path.join(HOME, ".local/bin"), "/opt/homebrew/bin"])


def _epoch_iso(secs):
    return datetime.fromtimestamp(int(secs), timezone.utc).isoformat() if secs else None


# ---------- argv builders ----------

def claude_argv(pconf, prompt, worktree, role):
    model = pconf.get("sort_model") if role == "sort" else pconf.get("build_model")
    argv = [claude_exe(), "-p", prompt, "--output-format", "stream-json", "--verbose",
            "--permission-mode", "bypassPermissions", "--disallowedTools", *CLAUDE_DENY]
    if model:
        argv += ["--model", model]
    return argv


def agy_argv(pconf, prompt, worktree, role, timeout_minutes=60):
    argv = [agy_exe(), "-p", prompt, "--add-dir", worktree,
            "--dangerously-skip-permissions", "--output-format", "stream-json",
            "--print-timeout", f"{int(timeout_minutes)}m"]
    if pconf.get("model"):
        argv += ["--model", pconf["model"]]
    return argv


def cline_argv(pconf, prompt, worktree, role, timeout_minutes=60):
    argv = [cline_exe(), "--cwd", worktree, "--json", "--auto-approve", "true",
            "-t", str(int(timeout_minutes) * 60)]
    if pconf.get("model"):
        argv += ["-m", pconf["model"]]
    return argv + [prompt]


def copilot_argv(pconf, prompt, worktree, role, timeout_minutes=60):
    argv = [copilot_exe(), "-p", prompt, "-C", worktree, "--allow-all-tools",
            "--output-format", "json", "--no-color", "--no-auto-update"]
    if pconf.get("model"):
        argv += ["--model", pconf["model"]]
    return argv


def kilo_argv(pconf, prompt, worktree, role, timeout_minutes=60):
    argv = [kilo_exe(), "run", prompt, "--dir", worktree, "--auto", "--format", "json"]
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
    if pconf["kind"] == "kilo":
        return kilo_argv(pconf, prompt, worktree, role, timeout_minutes)
    raise ValueError(f"unknown platform kind {pconf['kind']!r}")


def available(pconf):
    exe = {"claude": claude_exe, "agy": agy_exe, "cline": cline_exe,
           "copilot": copilot_exe, "kilo": kilo_exe}[pconf["kind"]]()
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
    return out


def oauth_usage():
    """Zero-token Claude reading — the same endpoint Claude Code's /usage and the
    Claude Usage menu-bar app use: GET api.anthropic.com/api/oauth/usage with
    Claude Code's own OAuth access token from the login keychain.

    The token is read fresh each time, held only in memory, sent only to
    Anthropic, and never logged or written anywhere. Mahler never refreshes it
    (that would race Claude Code's own refresh); an expired token just means
    this source is skipped until Claude Code next refreshes it.
    -> [(window, used_pct, resets_iso)] or [] if unavailable."""
    import urllib.error
    import urllib.request
    try:
        raw = subprocess.run(["security", "find-generic-password", "-s",
                              "Claude Code-credentials", "-w"],
                             capture_output=True, text=True, timeout=10)
        oauth = json.loads(raw.stdout).get("claudeAiOauth") or {}
    except (subprocess.SubprocessError, OSError, ValueError):
        return []
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


def probe_claude():
    exe = claude_exe()
    if not exe:
        return []
    try:
        r = subprocess.run(
            [exe, "-p", "ok", "--model", "claude-haiku-4-5-20251001", "--tools", "",
             "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
             "--system-prompt", "Reply ok.", "--output-format", "stream-json", "--verbose"],
            capture_output=True, text=True, timeout=90, cwd=HOME)
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


def _gh_login():
    try:
        r = subprocess.run(["gh", "api", "user", "--jq", ".login"],
                           capture_output=True, text=True, timeout=15)
        return r.stdout.strip() or None
    except (subprocess.SubprocessError, OSError):
        return None


def probe_copilot(monthly_cap_credits):
    """GitHub's AI-credits billing report (mahler#38): consumption only, no
    cap in the response, so `monthly_cap_credits` (plan-fixed, config) is what
    turns it into a percentage. Needs the `user` OAuth scope on the `gh` token.
    -> [("monthly", used_pct, resets_at_iso)] or [] if unavailable."""
    login = _gh_login()
    if not login:
        return []
    try:
        r = subprocess.run(["gh", "api", f"/users/{login}/settings/billing/ai_credit/usage"],
                           capture_output=True, text=True, timeout=20)
        data = json.loads(r.stdout)
    except (subprocess.SubprocessError, OSError, ValueError):
        return []
    used = sum(item.get("grossQuantity", 0) for item in data.get("usageItems", []))
    if not monthly_cap_credits:
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


def read_log(path, kind):
    """Summarise a run's stream-json log.

    Returns {'final': str|None, 'ok': bool|None, 'usage': [(window, pct, resets)],
             'quota_hit': bool, 'last_text': str}
    """
    res = {"final": None, "ok": None, "usage": [], "quota_hit": False, "last_text": ""}
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
                if line.strip():
                    texts.append(line.strip())
                continue
            if kind == "claude":
                t = ev.get("type")
                if t == "rate_limit_event":
                    res["usage"] = claude_samples_from_event(ev)
                    if (ev.get("rate_limit_info") or {}).get("status") == "rejected":
                        res["quota_hit"] = True
                elif t == "assistant":
                    for block in (ev.get("message") or {}).get("content") or []:
                        if block.get("type") == "text" and block.get("text"):
                            texts.append(block["text"])
                elif t == "result":
                    res["final"] = ev.get("result")
                    res["ok"] = ev.get("subtype") == "success" and not ev.get("is_error")
            elif kind == "cline":
                if ev.get("type") == "run_result":
                    res["final"] = ev.get("text")
                    res["ok"] = ev.get("finishReason") == "completed"
                    if not res["ok"] and any(w in json.dumps(ev).lower() for w in QUOTA_WORDS):
                        res["quota_hit"] = True
                elif ev.get("type") == "error" or ev.get("error"):
                    blob = json.dumps(ev).lower()
                    if any(w in blob for w in QUOTA_WORDS):
                        res["quota_hit"] = True
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
                    if any(w in json.dumps(ev).lower() for w in QUOTA_WORDS):
                        res["quota_hit"] = True
            elif kind == "kilo":
                if ev.get("type") == "error":
                    if any(w in json.dumps(ev).lower() for w in QUOTA_WORDS):
                        res["quota_hit"] = True
                else:
                    texts.extend(_collect_text(ev))
            else:  # agy
                if ev.get("event") == "result":
                    r = ev.get("result") or {}
                    res["final"] = r.get("response")
                    res["ok"] = r.get("status") == "SUCCESS"
                    if not res["ok"] and "quota" in json.dumps(r).lower():
                        res["quota_hit"] = True
                elif ev.get("event") == "step_update":
                    su = ev.get("step_update") or {}
                    if su.get("text_delta"):
                        texts.append(su["text_delta"])
    joined = "".join(texts) if kind == "agy" else "\n".join(texts)
    res["last_text"] = (res["final"] or joined)[-1500:]
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
