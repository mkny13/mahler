"""`mahler` — the command line for the daemon, for you, and for agents.

Interactive sessions (any platform) take part in the lease protocol through
`mahler claim / heartbeat / release`, and hand finished work to the conductor
with `mahler ship`; runs fence their pushes and merges with
`mahler lease-check` (DESIGN D6, D18).
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta

from . import config, desktop, holds, mcp, notify, platforms, router, scheduler, usage as usage_mod
from .gh import project_client, GHError
from .ledger import Ledger, RoutedLedger, iso, parse, remote_lease_operation


def ref(s):
    """'mahler#12' -> ('mahler', 12)"""
    if "#" not in s:
        raise argparse.ArgumentTypeError("use <project>#<issue>, e.g. mahler#12")
    p, n = s.split("#", 1)
    return p, int(n)


def default_holder():
    """A per-session default for `--as`, so two concurrent interactive sessions
    don't both show up as the same 'interactive:you' in the ledger (mahler#33).
    Falls back to 'you' outside a Claude Code session (e.g. a bare shell)."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    return sid[:8] if sid else "you"


def other_interactive_holders(led, project, holder):
    """Other sessions' live `interactive:*` leases in this project right now —
    the signal that would have caught mahler#27 before a git reset did."""
    if hasattr(led, "lease_rows"):
        rows = led.lease_rows(project)
        return sorted({r["holder"] for r in rows
                       if "/interactive:" in r["holder"] or r["holder"].startswith("interactive:")
                       if r["holder"] != holder and not r["holder"].endswith(f"/{holder}")})
    rows = led.q("SELECT DISTINCT holder FROM leases WHERE project=? AND holder LIKE 'interactive:%' "
                 "AND holder != ? AND expires_at > ?", (project, holder, iso(led.now())))
    return sorted(r["holder"] for r in rows)


def cmd_tick(a, cfg, led):
    return scheduler.main_tick(cfg, led, dry_run=a.dry_run, hot_hold=not a.no_hot_hold)


def _event_cell(value, width):
    """Keep stored text (including terminal controls) inside one table cell."""
    text = " ".join("".join(c if c.isprintable() else " " for c in str(value or "")).split())
    return text if len(text) <= width else text[:width - 1] + "…"


def cmd_events(a, cfg, led):
    predicates, params = [], []
    if a.project is not None:
        predicates.append("project = ?")
        params.append(a.project)
    kinds = []
    for kind in a.kind or []:
        if kind.endswith("_"):
            # substr gives literal prefix matching: '_' and '%' are not wildcards.
            kinds.append("substr(kind, 1, ?) = ?")
            params.extend((len(kind), kind))
        else:
            kinds.append("kind = ?")
            params.append(kind)
    if kinds:
        predicates.append("(" + " OR ".join(kinds) + ")")
    if a.since is not None:
        try:
            cutoff = iso(led.now() - _parse_duration(a.since))
        except (ValueError, OverflowError) as exc:
            print(f"mahler events: {exc}", file=sys.stderr)
            return 2
        predicates.append("at >= ?")
        params.append(cutoff)
    where = " WHERE " + " AND ".join(predicates) if predicates else ""
    rows = led.q("SELECT * FROM events" + where + " ORDER BY at DESC, id DESC LIMIT ?",
                 (*params, max(1, min(a.limit, 1000))))
    if not rows:
        print("No matching events.")
        return 0
    print(f"{'AT':25}  {'PROJECT/ISSUE':30}  {'KIND':30}  DETAIL")
    for row in rows:
        ref = row["project"] or "—"
        if row["number"] is not None:
            ref += f"#{row['number']}"
        print(f"{_event_cell(row['at'], 25):25}  {_event_cell(ref, 30):30}  "
              f"{_event_cell(row['kind'], 30):30}  {_event_cell(row['detail'], 160)}")
    return 0


_UPDATE_BLOCK_ACTIONS = {
    "app_unavailable": "check the installed app checkout",
    "head_lookup_failed": "check the installed app's Git HEAD",
    "rollback_checkout_failed": "check the app checkout; rollback failed",
    "tick_failed": "inspect logs/tick.log for failing ticks",
    "fetch_failed": "check origin access and the launcher's Git credentials",
    "candidate_lookup_failed": "check the app's locally fetched origin/main ref",
    "rollback_excluded": "origin/main is the rolled-back bad_sha; a newer commit is needed",
    "repository_lookup_failed": "check the launcher's gh authentication and repository access",
    "repository_lookup_empty": "check the app's origin repository",
    "checks_lookup_failed": "check the launcher's gh access to commit checks",
    "checks_missing": "check that CI has run on origin/main",
    "checks_not_green": "inspect failing or pending CI checks on origin/main",
    "checks_result_invalid": "check the launcher's GitHub checks response",
    "candidate_checkout_failed": "check the app checkout for local changes or Git errors",
    "tests_failed_restore_failed": "inspect local unit failures; restoring the old checkout failed",
    "tests_failed": "investigate unit tests failing in the installed app",
}
_UPDATE_DECISION = re.compile(
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) "
    r"head=(unknown|[0-9a-f]{40}|[0-9a-f]{64}) "
    r"candidate=(unknown|[0-9a-f]{40}|[0-9a-f]{64}) "
    r"outcome=(blocked|rejected|updated|unchanged|rolled_back) reason=([a-z_]+)"
)


def _daemon_update_stall(now):
    """Use only consecutive launcher decisions and locally fetched Git refs."""
    since = latest = head = reason = None
    try:
        with open(os.path.join(config.STATE, "logs", "update.log"), encoding="ascii") as log:
            for line in log:
                match = _UPDATE_DECISION.fullmatch(line.rstrip("\n"))
                if not match:
                    since = latest = head = reason = None
                    continue
                stamp, recorded_head, _, outcome, recorded_reason = match.groups()
                try:
                    at = parse(stamp)
                except ValueError:
                    since = latest = head = reason = None
                    continue
                if (outcome not in ("blocked", "rejected")
                        or recorded_reason not in _UPDATE_BLOCK_ACTIONS
                        or recorded_head == "unknown" or at > now
                        or (latest is not None and at < latest)):
                    since = latest = head = reason = None
                    continue
                if since is None or head != recorded_head:
                    since = at
                latest, head, reason = at, recorded_head, recorded_reason
    except (OSError, UnicodeError):
        return None
    if since is None or now - since < timedelta(hours=24):
        return None

    app = os.path.join(config.STATE, "app")
    try:
        installed = subprocess.run(
            ["git", "-C", app, "rev-parse", "--verify", "HEAD"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        if installed != head:
            return None
        behind = int(subprocess.run(
            ["git", "-C", app, "rev-list", "--count", "HEAD..refs/remotes/origin/main"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if behind <= 0:
        return None
    return {
        "commits_behind": behind,
        "blocked_since": iso(since),
        "blocked_seconds": int((now - since).total_seconds()),
        "reason": reason,
        "action": _UPDATE_BLOCK_ACTIONS[reason],
    }


def cmd_status(a, cfg, led):
    from .console.state import capacity_wait_text
    from .sync import missing_scope_counts

    missing_scope = missing_scope_counts(led, cfg, getattr(a, "project", None))
    now = led.now()
    update_stall = _daemon_update_stall(now)
    if not a.json:
        for warning in platforms.effort_warnings(cfg) + config.routing_warnings(cfg):
            print(warning)
        if update_stall:
            minutes = update_stall["blocked_seconds"] // 60
            print(f"WARNING: daemon update stalled — {update_stall['commits_behind']} commits "
                  f"behind fetched origin/main, blocked for {minutes // 60}h {minutes % 60}m; "
                  f"{update_stall['reason']}: {update_stall['action']}.")
    if a.json:
        ests = led.estimates()
        runs = [dict(r) for r in led.active_runs()]
        for r in runs:
            r["est_mins"] = int(led.run_estimate(ests, r["platform"], r["role"], r.get("size")))
        items = [dict(i) for i in led.items() if i["state"] != "done"]
        for i in items:
            i["est_mins"] = int(led.issue_estimate(ests, i["project"]))
        project_filter = getattr(a, "project", None)
        leases = (led.lease_rows(project_filter) if hasattr(led, "lease_rows")
                  else [dict(l) for l in led.q("SELECT * FROM leases")])
        if project_filter:
            runs = [r for r in runs if r["project"] == a.project]
            items = [i for i in items if i["project"] == a.project]
            leases = [l for l in leases if l["project"] == a.project]
        print(json.dumps({
            "paused": led.paused(),
            "runs": runs,
            "items": items,
            "leases": leases,
            "missing_scope_counts": missing_scope,
            "usage": {n: led.usage(n) for n in cfg["platforms"]},
            "estimates": {
                "calibration": led.calibration_stats(),
                "calibration_factor": led.calibration_factor(),
            },
            **({"daemon_update_stall": update_stall} if update_stall else {}),
        }, indent=2, default=str))
        return 0
    def item_url(project, number, it=None):
        repo = config.project_policy(cfg, project).get("repo")
        if not repo:
            return ""
        if it is None:
            it = led.item(project, number)
        pr = dict(it).get("pr") if it else None
        if pr:
            return f" https://github.com/{repo}/pull/{pr}"
        return f" https://github.com/{repo}/issues/{number}"

    print("PAUSED — nothing new will start (mahler resume)\n" if led.paused() else "", end="")
    peak = router.peak_status_line(cfg, led)
    if peak:
        print(peak)
    runs = led.active_runs()
    if getattr(a, "project", None):
        runs = [r for r in runs if r["project"] == a.project]
    print(f"Running ({len(runs)})")
    ests = led.estimates()
    for r in runs:
        mins = int((now - parse(r["started_at"])).total_seconds() // 60)
        est = int(led.run_estimate(ests, r["platform"], r["role"], r["size"]))
        time_str = f"{mins}m / ~{est}m" if mins <= est else f"{mins}m (+{mins-est}m past est)"
        url = item_url(r["project"], r["number"])
        model = f" {r['model']}" if r["model"] else ""
        print(f"  • run {r['id']:<4} {r['project']}#{r['number']:<5} {r['role']:<5} "
              f"{r['platform']:<11} {time_str:>16}  {r['status']}{model}{url}")
    print("\nItems")
    items = [i for i in led.items() if i["state"] != "done"]
    if getattr(a, "project", None):
        items = [i for i in items if i["project"] == a.project]
    for i in items:
        lease = led.lease(i["project"], i["number"])
        held = f"  held by {router.lease_label(led, lease)}" if lease else ""
        tries = f"  tries {i['attempts']}" if i["attempts"] else ""
        setup = f"  setup failed ×{i['setup_fails']}" if i["setup_fails"] else ""
        est = int(led.issue_estimate(ests, i["project"]))
        est_str = f"  ~{est}m"
        url = item_url(i["project"], i["number"], i)
        capacity = capacity_wait_text(led, i["project"], i, now)
        wait = f"  — {capacity}" if capacity else ""
        print(f"  {i['state']:<10} {i['project']}#{i['number']:<5} p{i['priority']}  "
              f"{(i['title'] or '')[:60]}{held}{tries}{setup}{est_str}{url}{wait}")
    if missing_scope:
        print("\nOpen issues older than 24h missing scope label (latest full sync)")
        for project, count in missing_scope.items():
            label = config.project_policy(cfg, project)["scope_label"]
            print(f"  {project}: {count if count is not None else 'not yet synced'} ({label})")
    print("\nQuota")
    burst_lines = router.all_bursts(cfg, led)
    burst_kind = router.burst_kind(burst_lines) if burst_lines else None
    if burst_kind:
        print(f"  D23 {burst_kind} burst active — Claude builds first, lines raised to 90/97")
    peak = router.peak_status_line(cfg, led)
    if peak:
        print(f"  {peak}")
    width = max([11] + [len(n) for n in cfg["platforms"]])
    for name, pconf in cfg["platforms"].items():
        claude_lines = burst_lines if pconf.get("kind") == "claude" else None
        state, detail = router.usage_state(led, name, pconf, burst_lines=claude_lines)
        chips = router.window_countdowns(led, name, pconf)
        tags = f"  [{' · '.join(f'{label} {cd}' for label, cd in chips)}]" if chips else ""
        print(f"  {name:<{width}} {state:<6} {detail}{tags}")
    print("\nRecent")
    for e in led.q("SELECT * FROM events ORDER BY id DESC LIMIT 10")[::-1]:
        when = parse(e["at"]).astimezone().strftime("%m-%d %H:%M")
        where = f"{e['project']}#{e['number']}" if e["project"] else ""
        url = item_url(e["project"], e["number"]) if e["project"] and e["number"] else ""
        print(f"  {when} {e['kind']:<8} {where:<14} {(e['detail'] or '')[:80]}{url}")
    return 0

def cmd_serve(a, cfg, led):
    from . import serve
    host = a.host or cfg.get("serve", {}).get("host", "127.0.0.1")
    port = a.port or cfg.get("serve", {}).get("port", 8787)
    # request threads must be able to use this connection, so re-open the
    # same database thread-safe; serve serialises access with a lock
    served = RoutedLedger(Ledger(led.path, thread_safe=True), cfg)
    try:
        return serve.serve(cfg, served, host, port)
    finally:
        served.close()


def cmd_hooks(a, cfg, led):
    project = a.project
    pol = config.project_policy(cfg, project)
    if not pol.get("path"):
        print(f"Project {project} has no configured path")
        return 1
    
    repo_path = os.path.expanduser(pol["path"])
    claude_dir = os.path.join(repo_path, ".claude")
    hooks_dir = os.path.join(claude_dir, "hooks")
    os.makedirs(hooks_dir, exist_ok=True)
    
    settings_path = os.path.join(claude_dir, "settings.json")
    settings = {}
    if os.path.exists(settings_path):
        with open(settings_path) as f:
            settings = json.load(f)
    
    if "hooks" not in settings:
        settings["hooks"] = {}
    
    hooks = settings["hooks"]
    
    # Claude Code hook configuration schema (code.claude.com/docs/en/hooks):
    # each event maps to a list of {"matcher": ..., "hooks": [{"type": "command",
    # "command": ...}]} entries. A bare {"command": ...} entry never fires.
    # 1. SessionStart (D6 layer 2: held-items nudge, claim prompt, other-session note)
    hooks["SessionStart"] = [{
        "hooks": [{"type": "command", "command": f"python3 {os.path.join('.claude', 'hooks', 'session_start.py')}"}]
    }]
    with open(os.path.join(hooks_dir, "session_start.py"), "w") as f:
        f.write(f"""#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import uuid

def main():
    my_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")[:8]
    try:
        out = subprocess.check_output(["mahler", "status", "--project", "{project}", "--json"], text=True)
        d = json.loads(out)
        held = []
        for r in d.get('runs', []):
            held.append(f'#{{r["number"]}} ({{r["role"]}} on {{r["platform"]}})')
        if held:
            print('Items currently held by autonomous runs in this project:')
            for h in held:
                print(f'  - {{h}}')
            print('')
        others = sorted({{l['holder'] for l in d.get('leases', [])
                          if ('/interactive:' in l.get('holder', '')
                              or l.get('holder', '').startswith('interactive:'))
                          and not l.get('holder', '').endswith(f'interactive:{{my_id}}')}})
        if others:
            print(f'Another interactive session ({{", ".join(others)}}) is active in this '
                  'project right now. Work in your own worktree, not by switching branches '
                  'in the shared checkout (see CLAUDE.md).')
        print('If you work on a backlog item, claim it with `mahler claim {project}#N`.')
        print('When it is pushed, finish with `mahler ship {project}#N [--pr X]` (the conductor '
              'merges on green) or merge it yourself. Nothing picks up a PR you leave open.')
    except Exception:
        pass

if __name__ == "__main__":
    main()
""")

    # 2. PreToolUse: lease fencing on pushes/PRs from a run (D6 — the epoch is
    # checked for `gh pr create|merge`), plus the one-time unclaimed nudge.
    hooks["PreToolUse"] = [{
        "matcher": "Edit|Write|Bash",
        "hooks": [{"type": "command", "command": f"python3 {os.path.join('.claude', 'hooks', 'pre_tool_use.py')}"}]
    }]
    with open(os.path.join(hooks_dir, "pre_tool_use.py"), "w") as f:
        f.write(f"""#!/usr/bin/env python3
import sys
import uuid
import os
import json
import subprocess

def main():
    project = "{project}"
    run_id = os.environ.get("MAHLER_RUN_ID")
    
    if run_id:
        # MAHLER_RUNS_DIR lets tests redirect the runs dir; production
        # leaves it unset and gets the real state dir (mahler#93).
        runs_dir = os.environ.get("MAHLER_RUNS_DIR") or os.path.expanduser("~/.mahler/runs")
        yield_file = os.path.join(runs_dir, str(run_id), "yield")
        if os.path.exists(yield_file):
            print("yield delivered: commit your work, push the branch, and end with STATUS: YIELDED")
            sys.exit(1)
            
    tool_args = {{}}
    if not sys.stdin.isatty():
        try:
            tool_args = json.load(sys.stdin)
        except Exception:
            pass
            
    if run_id:
        command = tool_args.get("command", "")
        # D6: fence outward-facing steps — `gh pr create|merge` — on the epoch.
        if command and ("gh pr merge" in command or "gh pr create" in command):
            res = subprocess.run(["mahler", "lease-check"], capture_output=True, text=True)
            if res.returncode != 0:
                print(res.stdout.strip())
                sys.exit(1)
                
    if not run_id:
        # Nudge once per session if unclaimed
        session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", str(os.getpid()))
        state_file = f"/tmp/mahler_nudge_{{session_id}}"
        if not os.path.exists(state_file):
            try:
                out = subprocess.check_output(["mahler", "status", "--project", project, "--json"], text=True)
                d = json.loads(out)
                my_holder = f"interactive:{{session_id[:8]}}"
                holds_claim = any(l.get("holder") == my_holder
                                  or l.get("holder", "").endswith("/" + my_holder)
                                  for l in d.get("leases", []))
                if not holds_claim:
                    print(f"If this work relates to a backlog item, run `mahler claim {{project}}#N`.")
                    with open(state_file, "w") as f:
                        f.write("1")
            except Exception:
                pass

if __name__ == "__main__":
    main()
""")

    # 3. PostToolUse / UserPromptSubmit (interactive heartbeats, D6 layer 1)
    heartbeat_entry = {"hooks": [{"type": "command",
                                  "command": f"python3 {os.path.join('.claude', 'hooks', 'heartbeat.py')}"}]}
    hooks["PostToolUse"] = [heartbeat_entry]
    hooks["UserPromptSubmit"] = [heartbeat_entry]
    
    with open(os.path.join(hooks_dir, "heartbeat.py"), "w") as f:
        f.write(f"""#!/usr/bin/env python3
import json
import subprocess
import os

def main():
    if os.environ.get("MAHLER_RUN_ID"):
        return
    my_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")[:8] or "you"
    my_holder = f"interactive:{{my_id}}"
    try:
        out = subprocess.check_output(["mahler", "status", "--project", "{project}", "--json"], text=True)
        d = json.loads(out)
        for l in d.get("leases", []):
            if (l.get("holder") == my_holder
                    or l.get("holder", "").endswith("/" + my_holder)):
                # Renew only this session's own claim(s) in the background
                subprocess.Popen(
                    ["mahler", "heartbeat", f"{{l['project']}}#{{l['number']}}", "--as", my_id],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True
                )
    except Exception:
        pass

if __name__ == "__main__":
    main()
""")

    # Make them executable
    for script in ["session_start.py", "pre_tool_use.py", "heartbeat.py"]:
        os.chmod(os.path.join(hooks_dir, script), 0o755)

    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)
    
    print(f"Hooks installed in {claude_dir}")
    return 0


def cmd_warmup(a, cfg, led):
    from .scheduler import Ctx, take_lock
    from .warmup import learned_lines, warmup_pass
    lock = None if a.dry_run else take_lock()
    if not a.dry_run and lock is None:
        print("mahler: another tick is running; try again")
        return 1
    try:
        ctx = Ctx(cfg, led, dry_run=a.dry_run)
        try:
            for line in learned_lines(cfg, led):
                print(f"  {line}")
            failures = warmup_pass(ctx, target=a.target, manual=True)
        except ValueError as exc:
            print(f"mahler: {exc}", file=sys.stderr)
            return 1
        for line in ctx.lines:
            print(line)
        return int(bool(failures))
    finally:
        if lock is not None:
            lock.close()


def cmd_usage(a, cfg, led):
    if a.probe:
        from . import platforms
        pools = platforms.probe_agy()
        done = set()
        for name, pconf in cfg["platforms"].items():
            account = config.account_of(pconf)
            if pconf["kind"] == "agy" and account == config.DEFAULT_ACCOUNT:
                for w, pct, resets in pools.get(pconf.get("pool"), []):
                    led.record_usage(name, w, pct, resets)
            elif pconf["kind"] == "codex" and name not in done:
                done |= set(usage_mod.quota_peers(cfg, name))
                usage_mod.refresh_codex(cfg, led, name, force=True)
            elif pconf["kind"] == "claude" and name not in done:
                # one reading per Claude login, recorded only on its own platforms (D25)
                peers = usage_mod.quota_peers(cfg, name)
                done |= set(peers)
                try:
                    env = config.run_env(cfg, account)
                except ValueError as e:
                    print(f"  {name}: {e}")
                    continue
                source = usage_mod.claude_oauth_source(cfg, pconf)
                samples = ((platforms.oauth_usage(**source) if source else [])
                           or platforms.probe_claude(env=env))
                for peer in peers:
                    for w, pct, resets in samples:
                        led.record_usage(peer, w, pct, resets)
    burst_lines = router.all_bursts(cfg, led)
    burst_kind = router.burst_kind(burst_lines) if burst_lines else None
    if burst_kind:
        print(f"  D23 {burst_kind} burst active — Claude builds first, lines raised to 90/97")
    peak = router.peak_status_line(cfg, led)
    if peak:
        print(f"  {peak}")
    width = max([11] + [len(n) for n in cfg["platforms"]])
    for name, pconf in cfg["platforms"].items():
        claude_lines = burst_lines if pconf.get("kind") == "claude" else None
        state, detail = router.usage_state(led, name, pconf, burst_lines=claude_lines)
        chips = router.window_countdowns(led, name, pconf)
        tags = f"  [{' · '.join(f'{label} {cd}' for label, cd in chips)}]" if chips else ""
        print(f"  {name:<{width}} {state:<6} {detail}{tags}")
    return 0


def cmd_claim(a, cfg, led):
    project, n = a.item
    pol = config.project_policy(cfg, project)
    lease, info = led.claim(project, n, f"interactive:{a.holder}", "interactive",
                            pol["interactive_lease_minutes"], steal=a.steal)
    if lease is None:
        if "unavailable" in info:
            print(f"{project}#{n}: canonical lease host unavailable; claim denied safely")
            return 1
        if "at_capacity" in info:
            held = ", ".join(router.lease_label(led, row)
                             for row in info["at_capacity"])
            print(f"{project} is at its canonical capacity ({held}); claim denied")
            return 1
        h = info["held_by"]
        print(f"#{n} is held by {h['holder']} ({h['kind']}, last active {h['heartbeat_at']}). "
              "Use --steal to take it anyway.")
        return 1
    led.set_state(project, n, "working", f"claimed by {a.holder}")
    print(f"You hold {project}#{n} (epoch {lease['epoch']}) for "
          f"{pol['interactive_lease_minutes']} min of inactivity.")
    if "preempted" in info:
        p = info["preempted"]
        print(f"An autonomous run ({p['platform']}, {p['holder']}) was working on this. It has "
              "been told to step aside; within ~2 minutes its work is pushed to a "
              f"mahler/snapshot/{n}-* branch and summarised in a handoff comment on the issue. "
              "Continue from that branch.")
    others = other_interactive_holders(led, project, f"interactive:{a.holder}")
    if others:
        print(f"Note: {', '.join(others)} also active in {project} right now — work in your "
              "own worktree, not by switching branches in the shared checkout (CLAUDE.md).")
    return 0


def cmd_heartbeat(a, cfg, led):
    project, n = a.item
    pol = config.project_policy(cfg, project)
    cur = led.lease(project, n)
    ok = bool(cur) and led.heartbeat(project, n, f"interactive:{a.holder}", cur["epoch"],
                                     pol["interactive_lease_minutes"])
    print("renewed" if ok else "you don't hold this item (any more)")
    return 0 if ok else 1


def _current_branch():
    try:
        r = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True, timeout=30)
    except (subprocess.SubprocessError, OSError):
        return None
    b = r.stdout.strip()
    return b if r.returncode == 0 and b and b != "HEAD" else None


def _links_issue(body, n):
    """Does the PR body close issue #n on merge?"""
    return re.search(rf"(?im)\b(fix(es|ed)?|close[sd]?|resolve[sd]?)\s+#{n}\b",
                     body or "") is not None


def cmd_ship(a, cfg, led):
    """Hand a session's pushed work to the conductor (mahler#407, D18 for
    sessions). The item goes to `verifying` with its PR (or branch) recorded
    and the session's lease released. From the next tick the ship pass
    watches CI and merges, exactly as it does after a build run. Before this,
    a session's PR sat open until someone merged it by hand."""
    project, n = a.item
    pol = config.project_policy(cfg, project)
    if not pol.get("repo"):
        print(f"unknown project {project!r}")
        return 1
    holder = f"interactive:{a.holder}"
    cur = led.lease(project, n)
    if cur and cur["holder"] != holder and not cur["holder"].endswith(f"/{holder}"):
        print(f"{project}#{n} is held by {cur['holder']}, not you — "
              f"`mahler claim {project}#{n}` first")
        return 1
    item = led.item(project, n)
    if item and item["state"] == "done":
        print(f"{project}#{n} is already done")
        return 1
    gh = project_client(cfg, pol)
    pr, branch, title = a.pr, a.branch, (item["title"] if item else None)
    try:
        if pr is None:
            branch = branch or _current_branch()
            if not branch:
                print("no --pr or --branch, and not on a branch here")
                return 1
            pr = gh.pr_for_head(branch)
        if pr is not None:
            view = gh.pr_view(pr)
            if view["state"] != "OPEN":
                print(f"PR #{pr} is {view['state'].lower()} — nothing to ship")
                return 1
            if not _links_issue(view.get("body"), n):
                # The conductor's own PRs say `Fixes #N`; without it the merge
                # would leave the issue open behind a done item.
                gh.pr_edit_body(pr, f"Fixes #{n}\n\n{view.get('body') or ''}")
            branch = view.get("headRefName") or branch
            title = title or view.get("title")
        else:
            try:
                gh._git(pol["path"], "ls-remote", "--exit-code",
                        "origin", f"refs/heads/{branch}")
            except GHError:
                print(f"branch {branch!r} isn't on origin — push it first")
                return 1
    except (GHError, ValueError, subprocess.SubprocessError, OSError) as e:
        print(f"mahler ship: {e}")
        return 1
    fields = {"branch": branch, "pr": pr, "title": title or f"{project} #{n}"}
    if a.summary:
        fields["summary"] = a.summary
    if item and item["state"] in ("failed", "needs_you"):
        # An explicit, validated retry gets the same counters as /mahler go.
        # Keep review rounds, follow-ups and diagnostic delivery history intact.
        fields.update(attempts=0, setup_fails=0, esc_tier=0, esc_fails=0)
        for prefix in (f"reviewfix:{project}#{n}:", f"red:{project}#{n}:"):
            for saved in led.q("SELECT key FROM kv WHERE substr(key, 1, ?)=?",
                               (len(prefix), prefix)):
                led.set_kv(saved["key"], None)
        cycle = uuid.uuid4().hex
        led.set_kv(f"attempt_cycle:{project}#{n}", cycle)
        led.event("attempt_cycle", project, n,
                  {"cycle": cycle, "reason": "manual ship", "previous_state": item["state"],
                   "previous_attempts": item["attempts"], "holder": a.holder})
    led.upsert_item(project, n, **fields)
    led.set_state(project, n, "verifying", f"handed to the conductor by {a.holder}")
    if cur:
        led.release(project, n, holder=holder, to_state=None)
    what = f"PR #{pr}" if pr else f"branch {branch} (the conductor opens the PR)"
    print(f"{project}#{n}: {what} handed to the conductor — it watches CI and merges on green.")
    return 0


def cmd_release(a, cfg, led):
    target = getattr(a, "target", None)
    if target is None:
        target = getattr(a, "project", None)
    if target is None:
        target = getattr(a, "item", None)

    if target is None:
        print("mahler release: error: target project or <project>#<issue> required")
        return 1

    # Lease release if target is a tuple or contains "#"
    if isinstance(target, tuple) or (isinstance(target, str) and "#" in target):
        if isinstance(target, tuple):
            project, n = target
        else:
            project, n = ref(target)
        holder = getattr(a, "holder", None) or default_holder()
        ok = led.release(project, n, holder=f"interactive:{holder}",
                         to_state="ready", why=f"released by {holder}")
        print("released" if ok else "you didn't hold it")
        return 0

    # Software release preview or publish
    from . import releases
    project = str(target)
    pol = config.project_policy(cfg, project)
    if not pol.get("repo"):
        print(f"unknown project {project!r}")
        return 1

    base_branch = pol.get("base", "main")
    gh = project_client(cfg, pol)
    draft = releases.get_draft(led, project)

    version_arg = getattr(a, "version", None)
    publish_arg = getattr(a, "publish", False)

    if publish_arg and not version_arg:
        print("mahler release: error: publishing requires both an explicit valid version (--version X.Y or X.Y.Z) and --publish")
        return 1

    selected_version = None
    if version_arg:
        try:
            selected_version = releases.normalize_semver(version_arg)
            parsed = releases.version_key(selected_version)
            latest_rel = led.latest_release(project)
            local_rel = led.get_release(project, version=selected_version)
            if latest_rel:
                last_parsed = releases.parse_semver(latest_rel["version"])
                if last_parsed:
                    last_key = releases.version_key(latest_rel["version"])
                    if parsed < last_key:
                        print(f"mahler release: error: version {selected_version} must be greater than latest recorded version {latest_rel['version']}")
                        return 1
                    if parsed == last_key and not local_rel and not publish_arg:
                        print(f"mahler release: error: version {selected_version} must be greater than latest recorded version {latest_rel['version']}")
                        return 1
        except ValueError as e:
            print(f"mahler release: error: {e}")
            return 1

    try:
        checkpoint_sha = gh.branch_sha(base_branch)
    except Exception as e:
        if publish_arg:
            print(f"mahler release: error: cannot resolve base branch {base_branch!r} SHA: {e}")
            return 1
        checkpoint_sha = draft.checkpoint_sha or "unknown"

    preview = releases.format_preview(draft, version=selected_version, checkpoint_sha=checkpoint_sha)
    print(preview)

    if not publish_arg:
        return 0

    try:
        res = releases.publish_release(led, gh, project, version=selected_version,
                                       checkpoint_sha=checkpoint_sha)
        status = res["status"]
        url = res["url"]
        if status == "reconciled":
            print(f"\nRelease v{selected_version} already published (reconciled): {url}")
        else:
            print(f"\nPublished v{selected_version}: {url}")
        return 0
    except (releases.ReleaseConflictError, ValueError) as e:
        print(f"mahler release: error: {e}")
        return 1
    except GHError as e:
        print(f"mahler release: error: {e}")
        return 1


def cmd_lease_check(a, cfg, led):
    if a.item:
        (project, n), epoch = a.item, a.epoch
    else:
        project, n = os.environ.get("MAHLER_PROJECT"), os.environ.get("MAHLER_ISSUE")
        epoch = os.environ.get("MAHLER_EPOCH")
        if not (project and n and epoch):
            print("lease-check: not inside a Mahler run (no MAHLER_* env) — allowed")
            return 0
    ok = led.lease_check(project, int(n), int(epoch))
    print("ok — you still hold the lease" if ok else
          f"STALE — {project}#{n} epoch {epoch} is no longer the lease holder; stop now")
    return 0 if ok else 1


def cmd_ledger_remote_op(a, cfg, led):
    """SSH-only JSON endpoint for D24's canonical project lease operations."""
    try:
        raw = sys.stdin.read(65537)
        if len(raw) > 65536:
            raise ValueError("remote ledger request is too large")
        request = json.loads(raw)
        result = remote_lease_operation(request, cfg, led.local)
        print(json.dumps({"version": 1, "ok": True, "result": result}, default=dict))
        return 0
    except Exception as exc:  # machine-readable failure; never expose a traceback over SSH
        print(json.dumps({"version": 1, "ok": False, "error": str(exc)}))
        return 1


def cmd_next_id(a, cfg, led):
    print(led.next_id(a.project, a.name, floor=a.floor))
    return 0


def cmd_pause(a, cfg, led):
    led.set_kv("paused", "1")
    led.event("pause", detail="paused by command")
    print("paused — running agents finish; nothing new starts. `mahler resume` to continue.")
    return 0


def cmd_resume(a, cfg, led):
    led.set_kv("paused", "0")
    led.event("resume", detail="resumed by command")
    print("resumed")
    return 0


def cmd_unhold(a, cfg, led):
    """Preview or clear one platform's explicit scheduler hold."""
    try:
        found = holds.inspect(cfg, led, a.platform)
    except ValueError as exc:
        print(f"mahler: {exc}", file=sys.stderr)
        return 2
    windows = ", ".join(found["windows"]) or "no usage rows"
    reason = found["hold_reason"] or "unspecified"
    until = f" until {found['until']}" if found["until"] else ""
    if a.dry_run:
        print(f"{a.platform}: would clear {windows}; reason {reason}{until}; "
              "reset hold_reason and credit_state")
        return 0
    holds.clear(cfg, led, a.platform, by="cli")
    print(f"{a.platform}: cleared {windows}; reset hold_reason and credit_state")
    return 0


def cmd_add(a, cfg, led):
    requested_labels = a.label or []
    for label in requested_labels:
        if label.startswith("mahler:"):
            print(f"mahler: label {label!r} is reserved for Mahler-managed state",
                  file=sys.stderr)
            return 1
    pol = config.project_policy(cfg, a.project)
    if not pol.get("repo"):
        print(f"unknown project {a.project!r}")
        return 1
    labels = [pol["scope_label"]] if pol.get("scope") == "label" else []
    labels.extend(requested_labels)
    print(project_client(cfg, pol).create_issue(
        a.title, a.body or "", labels))
    return 0


def cmd_notify(a, cfg, led):
    ok = notify.send(cfg, a.title, a.message or "")
    print("sent" if ok else "not sent (no ntfy topic configured?)")
    return 0 if ok else 1


def cmd_labels(a, cfg, led):
    pol = config.project_policy(cfg, a.project)
    project_client(cfg, pol).ensure_labels()
    print(f"labels ensured on {pol['repo']}")
    return 0


def cmd_backup(a, cfg, led):
    from . import backup
    ctx = scheduler.Ctx(cfg, led)
    specs = [(p["name"], s) for p in config.enabled_projects(cfg)
             for s in p.get("backups") or [] if not a.project or p["name"] == a.project]
    if not specs:
        print("no backups configured (add [[projects.<name>.backups]] to ~/.mahler/config.toml)")
        return 1
    ok = True
    for project, spec in specs:
        ok &= backup.run(ctx, project, spec, force=True) is not None
    for line in ctx.lines:
        print(line)
    return 0 if ok else 1


def cmd_restore_ledger(a, cfg, led):
    """Verify a ledger backup, then swap it in. Never deletes the current
    DB — it's moved aside. Refuses if the daemon still holds the tick lock;
    stop it first (mahler#533)."""
    from . import backup
    src = a.file
    if not os.path.isfile(src):
        print(f"mahler: {src} not found", file=sys.stderr)
        return 1
    result = backup.integrity_check(src)
    if result != "ok":
        print(f"mahler: {src} fails integrity check: {result}", file=sys.stderr)
        return 1
    led.close()
    uid = os.getuid()
    lock = scheduler.take_lock()
    if lock is None:
        print("mahler: the daemon still holds the tick lock — stop it first:\n"
              f"  launchctl bootout gui/{uid}/local.mahler\n"
              f"  launchctl bootout gui/{uid}/local.mahler.serve\n"
              "then run this again.", file=sys.stderr)
        return 1
    try:
        dest = config.DB_PATH
        if os.path.exists(dest):
            aside = f"{dest}.before-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            os.rename(dest, aside)
            print(f"moved current ledger aside to {aside}")
        for suffix in ("-wal", "-shm"):
            try:
                os.remove(dest + suffix)
            except OSError:
                pass
        shutil.copy2(src, dest)
        os.chmod(dest, 0o600)
    finally:
        lock.close()
    print(f"restored {src} -> {dest}")
    print("restart the daemon:\n"
          f"  launchctl bootstrap gui/{uid} ~/Library/LaunchAgents/local.mahler.plist\n"
          f"  launchctl bootstrap gui/{uid} ~/Library/LaunchAgents/local.mahler.serve.plist")
    return 0


def cmd_log(a, cfg, led):
    run = led.run(a.run_id)
    if not run:
        print("no such run")
        return 1
    from . import platforms
    kind = cfg["platforms"][run["platform"]]["kind"]
    s = platforms.read_log(run["log_path"], kind)
    print(f"run {run['id']} — {run['project']}#{run['number']} {run['role']} on "
          f"{run['platform']} — {run['status']} {run['outcome'] or ''}")
    print(f"log: {run['log_path']}\n")
    print(s["last_text"])
    if run["exit_code"] == 97:
        from . import runner
        tail = runner.setup_tail({"log_path": run["log_path"]})
        print(f"\nsetup.log (last 20 lines):\n{tail}" if tail
              else "\n(no setup.log left for this run)")
    return 0


def _parse_duration(s):
    """'2h' / '90m' / '1h30m' -> timedelta. Raises ValueError on garbage."""
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?", s or "")
    if not m or not (m.group(1) or m.group(2)):
        raise ValueError(f"bad duration {s!r}: use e.g. 2h or 90m")
    return timedelta(hours=int(m.group(1) or 0), minutes=int(m.group(2) or 0))


def _peak_window_end(cfg, led):
    """Today's peak-window end in UTC, or the next one if today's has passed."""
    from zoneinfo import ZoneInfo
    pc = cfg.get("claude_peak") or {}
    tz = ZoneInfo(pc.get("tz", "America/Los_Angeles"))
    now = led.now()
    local = now.astimezone(tz)
    try:
        eh, em = (int(x) for x in pc.get("end", "11:00").split(":"))
    except (ValueError, AttributeError):
        return None
    end = local.replace(hour=eh, minute=em, second=0, microsecond=0)
    if end <= local:
        end = end + timedelta(days=1)
    return end.astimezone(now.tzinfo)


def cmd_version(a, cfg, led):
    from .version import format_version, version_info
    app_dir = config.REPO_ROOT
    home_dir = config.STATE
    info = version_info(app_dir, home_dir)
    print(format_version(info))
    return 0


def cmd_peak(a, cfg, led):
    """Override Claude's peak window (D22).

    `peak --off` allows Claude runs until the current (or next) window ends,
    or for `--for DURATION`. `peak --on` clears the override and restores
    normal scheduling.
    """
    pc = cfg.get("claude_peak") or {}
    if not pc.get("enabled", True):
        print("claude_peak is disabled in config — nothing to override")
        return 1
    if a.off:
        if a.for_duration:
            until = led.now() + _parse_duration(a.for_duration)
        else:
            until = _peak_window_end(cfg, led)
            if until is None:
                print("peak window has no end configured")
                return 1
        led.set_kv(router.PEAK_OVERRIDE, iso(until))
        led.event("peak_override", detail=f"peak override until {until.isoformat()}")
        print(f"peak override on — Claude runs allowed until "
              f"{until.astimezone():%H:%M} {router.local_time_label(until)} "
              f"(in {router.fmt_countdown(until - led.now())})")
        return 0
    if a.on:
        if led.get_kv(router.PEAK_OVERRIDE):
            led.set_kv(router.PEAK_OVERRIDE, None)
            led.event("peak_override", detail="peak override cleared")
        print("peak override cleared")
        return 0
    line = router.peak_status_line(cfg, led)
    print(line if line else "peak window: inactive")
    return 0


def cmd_console_walkthrough(a, cfg, led):
    from . import console_walkthrough
    return console_walkthrough.run(cfg, a.platform)

def cmd_backfill_usage(a, cfg, led):
    """Recover historical accounting without touching active runs or leases."""
    from .run_usage import columns, price
    updated = skipped = 0
    rows = led.con.execute(
        "SELECT * FROM runs WHERE status='ended' AND tokens_out IS NULL "
        "AND cost_source IS NULL ORDER BY id").fetchall()
    for row in rows:
        run = dict(row)
        if (run["outcome"] or "").startswith("launch failed"):
            skipped += 1
            continue
        pc = cfg.get("platforms", {}).get(run["platform"])
        try:
            if not pc or not run["log_path"]:
                raise OSError("no platform or log")
            with open(run["log_path"], encoding="utf-8"):
                pass
        except OSError:
            skipped += 1
            continue
        model = run.get("model") or pc.get("sort_model" if run["role"] == "sort" else "build_model") or pc.get("model")
        log = platforms.read_log(run["log_path"], pc["kind"], model=model)
        if not a.dry_run:
            led.update_run(run["id"], **columns(log, run, cfg))
        updated += 1
    # Runs ended before their model had a [prices] row kept their tokens but were
    # stamped 'unpriced'; price them from the stored columns now. CLI costs stay.
    repriced = 0
    for row in led.con.execute(
            "SELECT * FROM runs WHERE status='ended' AND cost_source='unpriced' "
            "AND tokens_out IS NOT NULL ORDER BY id").fetchall():
        tokens = {k: row[f"tokens_{k}"] for k in ("in", "cached", "out", "reasoning")}
        cost = price(tokens, row["model"], cfg)
        if cost is None:
            continue
        if not a.dry_run:
            led.update_run(row["id"], cost_usd=cost, cost_source="priced")
        repriced += 1
    print(f"{'Would update' if a.dry_run else 'Updated'} {updated} run(s); skipped {skipped}. "
          f"{'Would re-price' if a.dry_run else 'Re-priced'} {repriced} unpriced run(s).")


def _scorecard_days(value):
    if not re.fullmatch(r"[1-9][0-9]*d", value):
        raise argparse.ArgumentTypeError("use a positive day window, e.g. 30d")
    return int(value[:-1])


def cmd_scorecard(a, cfg, led):
    from . import scorecard
    since = led.now() - timedelta(days=a.since) if a.since else None
    if getattr(a, "review_precision", False):
        rows = scorecard.review_precision(led, cfg=cfg, project=a.project, since=since)
        if a.raw or getattr(a, "json", False):
            print(json.dumps(rows, indent=2))
        else:
            print(scorecard.format_review_precision(rows))
        return 0
    rows = scorecard.table(led, cfg, project=a.project, since=since)
    rows = [r for r in rows if (not a.role or r["role"] == a.role)
            and (not a.size or r["size"] == a.size)]
    if a.raw or getattr(a, "json", False):
        print(json.dumps([attempt for row in rows for attempt in row["attempts"]], indent=2))
    else:
        print("Model scorecard · API-equivalent dollars (weighted)")
        for row in rows:
            print(f'{row["role"]} / {row["size"] or "unknown size"}: {scorecard.summary(row)}')
        if not rows:
            print("No attempts in this window.")


def cmd_review_adjudicate(a, cfg, led):
    from . import review
    project, number = a.item
    try:
        created = review.adjudicate(
            led, project, number,
            review_run=a.review_run,
            sha=a.sha,
            classification=a.classification,
            evidence=a.evidence,
            reason=a.reason,
            fix_runs=a.fix_runs,
        )
    except ValueError as exc:
        print(f"mahler review-adjudicate: {exc}", file=sys.stderr)
        return 2
    if created:
        print(f"Recorded review adjudication for {project}#{number} (run {a.review_run}, {a.classification}).")
    else:
        print(f"Adjudication already recorded for {project}#{number} (run {a.review_run}, {a.classification}).")
    return 0


def _capacity_days(value):
    try:
        val = int(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError(f"invalid days '{value}': must be an integer between 1 and 90")
    if not (1 <= val <= 90):
        raise argparse.ArgumentTypeError(f"invalid days '{value}': must be an integer between 1 and 90")
    return val


def cmd_capacity(a, cfg, led):
    from . import capacity
    report = capacity.capacity_report(led, days=a.days)
    print(capacity.format_capacity_report(report))
    return 0


def build_parser(cfg=None):
    ap = argparse.ArgumentParser(prog="mahler", description="conducts coding agents")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("desktop", help="gate GUI-driving work on the mini being idle: "
                       "`desktop [--project P] [--scheduled]` or `desktop run -- CMD` (D40)")
    s.add_argument("args", nargs=argparse.REMAINDER)

    s = sub.add_parser("tick", help="one scheduler pass (launchd runs this every 60s)")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--no-hot-hold", action="store_true",
                   help="ignore active Claude sessions in the project")
    s.set_defaults(fn=cmd_tick)

    s = sub.add_parser("status", help="running work, items, quota, recent events")
    s.add_argument("--json", action="store_true")
    s.add_argument("--project", help="filter by project")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("events", help="inspect ledger event history, newest first")
    s.add_argument("--project", help="filter by project")
    s.add_argument("--kind", action="append", help="exact kind, or prefix ending in _; repeatable")
    s.add_argument("--since", help="age window, e.g. 1h, 90m or 1h30m")
    s.add_argument("-n", "--limit", type=int, default=50, help="row count (clamped to 1–1000; default 50)")
    s.set_defaults(fn=cmd_events)

    s = sub.add_parser("scorecard", help="model cost and first-attempt success by role and size")
    s.add_argument("--project")
    s.add_argument("--role")
    s.add_argument("--size")
    s.add_argument("--since", type=_scorecard_days, metavar="30d")
    s.add_argument("--raw", action="store_true")
    s.add_argument("--json", action="store_true", help="output JSON")
    s.add_argument("--review-precision", action="store_true", help="reviewer precision over failed verdicts")
    s.set_defaults(fn=cmd_scorecard)

    s = sub.add_parser("review-adjudicate", help="record an operator review adjudication")
    s.add_argument("item", type=ref, help="project#number, e.g. mahler#12")
    s.add_argument("--review-run", type=int, required=True, help="review run ID")
    s.add_argument("--sha", required=True, help="reviewed commit SHA")
    s.add_argument("--classification", required=True, choices=["false", "justified", "unresolved"],
                   help="verdict classification")
    s.add_argument("--evidence", required=True, help="evidence URL")
    s.add_argument("--reason", required=True, help="reason text")
    s.add_argument("--fix-run", action="append", type=int, dest="fix_runs", default=[],
                   help="attested fix run ID (repeatable)")
    s.set_defaults(fn=cmd_review_adjudicate)

    s = sub.add_parser("capacity", help="historical free-capacity report (mahler#736)")
    s.add_argument("--days", type=_capacity_days, default=30,
                   help="reporting window in days (1–90, default 30)")
    s.set_defaults(fn=cmd_capacity)

    s = sub.add_parser("serve", help="the operator console (D27)")
    s.add_argument("--host", type=str, default=None,
                   help="interface to bind (default: serve.host in config, else 127.0.0.1)")
    s.add_argument("--port", type=int, default=None,
                   help="port to bind (default: serve.port in config, else 8787)")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("hooks", help="install Claude Code session hooks")
    s.add_argument("project")
    s.set_defaults(fn=cmd_hooks)

    s = sub.add_parser("warmup", help="inspect or nudge idle login windows now")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("target", nargs="?")
    s.set_defaults(fn=cmd_warmup)

    s = sub.add_parser("usage", help="quota per platform")
    s.add_argument("--probe", action="store_true", help="take fresh readings now")
    s.set_defaults(fn=cmd_usage)

    for name, fn, hlp in (("claim", cmd_claim, "take an item for this session"),
                          ("heartbeat", cmd_heartbeat, "keep your claim alive")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("item", type=ref, help="<project>#<issue>")
        s.add_argument("--as", dest="holder", default=default_holder())
        if name == "claim":
            s.add_argument("--steal", action="store_true")
        s.set_defaults(fn=fn)

    s = sub.add_parser("ship", help="hand your pushed branch or open PR to the conductor")
    s.add_argument("item", type=ref, help="<project>#<issue>")
    s.add_argument("--pr", type=int, help="the open PR (default: the one for --branch)")
    s.add_argument("--branch", help="the pushed branch (default: the current one)")
    s.add_argument("--summary", help="one line for the shipped comment")
    s.add_argument("--as", dest="holder", default=default_holder())
    s.set_defaults(fn=cmd_ship)

    s = sub.add_parser("release", help="preview or publish a project release, or give an item back")
    s.add_argument("target", help="<project> or <project>#<issue>")
    s.add_argument("--version", help="version to publish (X.Y or X.Y.Z)")
    s.add_argument("--publish", action="store_true", help="publish the release to GitHub")
    s.add_argument("--as", dest="holder", default=default_holder(), help="holder name when giving an item back")
    s.set_defaults(fn=cmd_release)

    s = sub.add_parser("lease-check", help="exit 0 only if this run still holds its lease")
    s.add_argument("item", type=ref, nargs="?")
    s.add_argument("epoch", type=int, nargs="?")
    s.set_defaults(fn=cmd_lease_check)

    s = sub.add_parser("ledger-remote-op", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_ledger_remote_op)

    s = sub.add_parser("next-id", help="atomic per-project counter (e.g. decision IDs)")
    s.add_argument("project")
    s.add_argument("name")
    s.add_argument("--floor", type=int, default=0)
    s.set_defaults(fn=cmd_next_id)

    sub.add_parser("pause", help="start nothing new").set_defaults(fn=cmd_pause)
    sub.add_parser("resume").set_defaults(fn=cmd_resume)

    s = sub.add_parser("unhold", help="clear an explicit platform hold")
    s.add_argument("platform")
    s.add_argument("--dry-run", action="store_true",
                   help="show the hold rows and keys without changing them")
    s.set_defaults(fn=cmd_unhold)

    s = sub.add_parser("add", help="file an issue")
    s.add_argument("project")
    s.add_argument("title")
    s.add_argument("--body")
    s.add_argument("--label", action="append", help="issue label (repeatable; mahler:* is reserved)")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("notify", help="send a ntfy ping")
    s.add_argument("title")
    s.add_argument("message", nargs="?")
    s.set_defaults(fn=cmd_notify)

    s = sub.add_parser("labels", help="create Mahler's labels on a project's repo")
    s.add_argument("project")
    s.set_defaults(fn=cmd_labels)

    s = sub.add_parser("backup", help="back up project databases now")
    s.add_argument("project", nargs="?")
    s.set_defaults(fn=cmd_backup)

    s = sub.add_parser("restore-ledger", help="restore Mahler's own ledger from a verified backup")
    s.add_argument("file", help="path to a mahler-<stamp>.db backup")
    s.set_defaults(fn=cmd_restore_ledger)

    s = sub.add_parser("backfill-usage", help="recover accounting from ended run logs; price runs whose model now has a [prices] row")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_backfill_usage)

    s = sub.add_parser("log", help="summarise a run's output")
    s.add_argument("run_id", type=int)
    s.set_defaults(fn=cmd_log)

    s = sub.add_parser("mcp", help="run MCP server over stdio")
    s.set_defaults(fn=lambda a, cfg, led: mcp.serve(cfg, led) or 0)

    s = sub.add_parser("peak", help="override Claude's peak window (D22)")
    grp = s.add_mutually_exclusive_group(required=True)
    grp.add_argument("--off", action="store_true", help="allow Claude through the peak hold")
    grp.add_argument("--on", action="store_true", help="restore normal scheduling")
    s.add_argument("--for", dest="for_duration", default=None,
                   help="override duration, e.g. 2h or 90m (default: until the window ends)")
    s.set_defaults(fn=cmd_peak)

    sub.add_parser("version", help="show commit, known-good status, behind-count"
                   ).set_defaults(fn=cmd_version)

    s = sub.add_parser("console-walkthrough", help="agent-driven manual UAT of the console")
    s.add_argument("platform", nargs="?", default="agy-gemini", help="platform to run on (e.g. agy-gemini)")
    s.set_defaults(fn=cmd_console_walkthrough)

    return ap


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["desktop"]:  # needs no ledger; a bad config must exit 2 (D40)
        try:
            return desktop.main(argv[1:])
        except ValueError as e:
            print(f"mahler desktop: {e}", file=sys.stderr)
            return desktop.EX_INVALID

    ap = build_parser()
    a = ap.parse_args(argv)
    cfg = config.load()
    led = RoutedLedger(Ledger(config.DB_PATH), cfg)
    try:
        return a.fn(a, cfg, led) or 0
    except GHError as e:
        print(f"mahler: {e}", file=sys.stderr)
        return 1
    finally:
        led.close()
