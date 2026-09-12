"""`mahler` — the command line for the daemon, for you, and for agents.

Interactive sessions (any platform) take part in the lease protocol through
`mahler claim / heartbeat / release`; runs fence their pushes and merges with
`mahler lease-check` (DESIGN D6).
"""

import argparse
import json
import os
import sys
from datetime import timedelta

from . import config, notify, router, scheduler
from .gh import GH, GHError
from .ledger import Ledger, parse


def ref(s):
    """'mahler#12' -> ('mahler', 12)"""
    if "#" not in s:
        raise argparse.ArgumentTypeError("use <project>#<issue>, e.g. mahler#12")
    p, n = s.split("#", 1)
    return p, int(n)


def cmd_tick(a, cfg, led):
    return scheduler.main_tick(cfg, led, dry_run=a.dry_run, hot_hold=not a.no_hot_hold)


def cmd_status(a, cfg, led):
    now = led.now()
    if a.json:
        runs = [dict(r) for r in led.active_runs()]
        items = [dict(i) for i in led.items() if i["state"] != "done"]
        leases = [dict(l) for l in led.q("SELECT * FROM leases")]
        if getattr(a, "project", None):
            runs = [r for r in runs if r["project"] == a.project]
            items = [i for i in items if i["project"] == a.project]
            leases = [l for l in leases if l["project"] == a.project]
        print(json.dumps({
            "paused": led.paused(),
            "runs": runs,
            "items": items,
            "leases": leases,
            "usage": {n: led.usage(n) for n in cfg["platforms"]},
        }, indent=2, default=str))
        return 0
    print("PAUSED — nothing new will start (mahler resume)\n" if led.paused() else "", end="")
    runs = led.active_runs()
    if getattr(a, "project", None):
        runs = [r for r in runs if r["project"] == a.project]
    print(f"Running ({len(runs)})")
    for r in runs:
        mins = int((now - parse(r["started_at"])).total_seconds() // 60)
        print(f"  • run {r['id']:<4} {r['project']}#{r['number']:<5} {r['role']:<5} "
              f"{r['platform']:<11} {mins:>3} min  {r['status']}")
    print("\nItems")
    items = [i for i in led.items() if i["state"] != "done"]
    if getattr(a, "project", None):
        items = [i for i in items if i["project"] == a.project]
    for i in items:
        lease = led.lease(i["project"], i["number"])
        held = f"  held by {lease['holder']}" if lease else ""
        tries = f"  tries {i['attempts']}" if i["attempts"] else ""
        print(f"  {i['state']:<10} {i['project']}#{i['number']:<5} p{i['priority']}  "
              f"{(i['title'] or '')[:60]}{held}{tries}")
    print("\nQuota")
    for name, pconf in cfg["platforms"].items():
        state, detail = router.usage_state(led, name, pconf)
        print(f"  {name:<11} {state:<6} {detail}")
    print("\nRecent")
    for e in led.q("SELECT * FROM events ORDER BY id DESC LIMIT 10")[::-1]:
        when = parse(e["at"]).astimezone().strftime("%m-%d %H:%M")
        where = f"{e['project']}#{e['number']}" if e["project"] else ""
        print(f"  {when} {e['kind']:<8} {where:<14} {(e['detail'] or '')[:80]}")
    return 0

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
    
    # 1. SessionStart
    hooks["SessionStart"] = [{"command": f"python3 {os.path.join('.claude', 'hooks', 'session_start.py')}"}]
    with open(os.path.join(hooks_dir, "session_start.py"), "w") as f:
        f.write(f"""#!/usr/bin/env python3
import json
import subprocess
import sys

def main():
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
        print('If you work on a backlog item, claim it with `mahler claim {project}#N`.')
    except Exception:
        pass

if __name__ == "__main__":
    main()
""")

    # 2. PreToolUse
    hooks["PreToolUse"] = [{"command": f"python3 {os.path.join('.claude', 'hooks', 'pre_tool_use.py')}", "tools": ["Edit", "Write", "Bash"]}]
    with open(os.path.join(hooks_dir, "pre_tool_use.py"), "w") as f:
        f.write(f"""#!/usr/bin/env python3
import sys
import os
import json
import subprocess

def main():
    project = "{project}"
    run_id = os.environ.get("MAHLER_RUN_ID")
    
    if run_id:
        yield_file = os.path.expanduser(f"~/.mahler/runs/{{run_id}}/yield")
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
        if command and "gh pr merge" in command:
            res = subprocess.run(["mahler", "lease-check"], capture_output=True, text=True)
            if res.returncode != 0:
                print(res.stdout.strip())
                sys.exit(1)
                
    if not run_id:
        # Nudge once per session if unclaimed
        session_id = os.environ.get("CLAUDE_SESSION_ID", str(os.getpid()))
        state_file = f"/tmp/mahler_nudge_{{session_id}}"
        if not os.path.exists(state_file):
            try:
                out = subprocess.check_output(["mahler", "status", "--project", project, "--json"], text=True)
                d = json.loads(out)
                holds_claim = any(l.get("holder", "").startswith("interactive:") for l in d.get("leases", []))
                if not holds_claim:
                    print(f"If this work relates to a backlog item, run `mahler claim {{project}}#N`.")
                    with open(state_file, "w") as f:
                        f.write("1")
            except Exception:
                pass

if __name__ == "__main__":
    main()
""")

    # 3. PostToolUse / UserPromptSubmit (heartbeat)
    heartbeat_cmd = {"command": f"python3 {os.path.join('.claude', 'hooks', 'heartbeat.py')}"}
    hooks["PostToolUse"] = [heartbeat_cmd]
    hooks["UserPromptSubmit"] = [heartbeat_cmd]
    
    with open(os.path.join(hooks_dir, "heartbeat.py"), "w") as f:
        f.write(f"""#!/usr/bin/env python3
import json
import subprocess
import os

def main():
    if os.environ.get("MAHLER_RUN_ID"):
        return
    try:
        out = subprocess.check_output(["mahler", "status", "--project", "{project}", "--json"], text=True)
        d = json.loads(out)
        for l in d.get("leases", []):
            if l.get("holder", "").startswith("interactive:"):
                # Call mahler heartbeat in background
                subprocess.Popen(
                    ["mahler", "heartbeat", f"{{l['project']}}#{{l['number']}}"],
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


def cmd_usage(a, cfg, led):
    if a.probe:
        from . import platforms
        pools = platforms.probe_agy()
        for name, pconf in cfg["platforms"].items():
            if pconf["kind"] == "agy":
                for w, pct, resets in pools.get(pconf.get("pool"), []):
                    led.record_usage(name, w, pct, resets)
            elif pconf["kind"] == "claude":
                samples = platforms.oauth_usage() or platforms.probe_claude()
                for w, pct, resets in samples:
                    led.record_usage(name, w, pct, resets)
    for name, pconf in cfg["platforms"].items():
        state, detail = router.usage_state(led, name, pconf)
        print(f"{name:<11} {state:<6} {detail}")
    return 0


def cmd_claim(a, cfg, led):
    project, n = a.item
    pol = config.project_policy(cfg, project)
    lease, info = led.claim(project, n, f"interactive:{a.holder}", "interactive",
                            pol["interactive_lease_minutes"], steal=a.steal)
    if lease is None:
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
    return 0


def cmd_heartbeat(a, cfg, led):
    project, n = a.item
    pol = config.project_policy(cfg, project)
    cur = led.lease(project, n)
    ok = bool(cur) and led.heartbeat(project, n, f"interactive:{a.holder}", cur["epoch"],
                                     pol["interactive_lease_minutes"])
    print("renewed" if ok else "you don't hold this item (any more)")
    return 0 if ok else 1


def cmd_release(a, cfg, led):
    project, n = a.item
    ok = led.release(project, n, holder=f"interactive:{a.holder}")
    if ok and led.item(project, n)["state"] == "working":
        led.set_state(project, n, "ready", f"released by {a.holder}")
    print("released" if ok else "you didn't hold it")
    return 0


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


def cmd_add(a, cfg, led):
    pol = config.project_policy(cfg, a.project)
    if not pol.get("repo"):
        print(f"unknown project {a.project!r}")
        return 1
    print(GH(pol["repo"]).create_issue(a.title, a.body or ""))
    return 0


def cmd_notify(a, cfg, led):
    ok = notify.send(cfg, a.title, a.message or "")
    print("sent" if ok else "not sent (no ntfy topic configured?)")
    return 0 if ok else 1


def cmd_labels(a, cfg, led):
    pol = config.project_policy(cfg, a.project)
    GH(pol["repo"]).ensure_labels()
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
    return 0


def cmd_version(a, cfg, led):
    from .version import format_version, version_info
    app_dir = config.REPO_ROOT
    home_dir = config.STATE
    info = version_info(app_dir, home_dir)
    print(format_version(info))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mahler", description="conducts coding agents")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("tick", help="one scheduler pass (launchd runs this every 60s)")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--no-hot-hold", action="store_true",
                   help="ignore active Claude sessions in the project")
    s.set_defaults(fn=cmd_tick)

    s = sub.add_parser("status", help="running work, items, quota, recent events")
    s.add_argument("--json", action="store_true")
    s.add_argument("--project", help="filter by project")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("hooks", help="install Claude Code session hooks")
    s.add_argument("project")
    s.set_defaults(fn=cmd_hooks)

    s = sub.add_parser("usage", help="quota per platform")
    s.add_argument("--probe", action="store_true", help="take fresh readings now")
    s.set_defaults(fn=cmd_usage)

    for name, fn, hlp in (("claim", cmd_claim, "take an item for this session"),
                          ("heartbeat", cmd_heartbeat, "keep your claim alive"),
                          ("release", cmd_release, "give an item back")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("item", type=ref, help="<project>#<issue>")
        s.add_argument("--as", dest="holder", default="you")
        if name == "claim":
            s.add_argument("--steal", action="store_true")
        s.set_defaults(fn=fn)

    s = sub.add_parser("lease-check", help="exit 0 only if this run still holds its lease")
    s.add_argument("item", type=ref, nargs="?")
    s.add_argument("epoch", type=int, nargs="?")
    s.set_defaults(fn=cmd_lease_check)

    s = sub.add_parser("next-id", help="atomic per-project counter (e.g. decision IDs)")
    s.add_argument("project")
    s.add_argument("name")
    s.add_argument("--floor", type=int, default=0)
    s.set_defaults(fn=cmd_next_id)

    sub.add_parser("pause", help="start nothing new").set_defaults(fn=cmd_pause)
    sub.add_parser("resume").set_defaults(fn=cmd_resume)

    s = sub.add_parser("add", help="file an issue")
    s.add_argument("project")
    s.add_argument("title")
    s.add_argument("--body")
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

    s = sub.add_parser("log", help="summarise a run's output")
    s.add_argument("run_id", type=int)
    s.set_defaults(fn=cmd_log)

    s = sub.add_parser("mcp", help="run MCP server over stdio")
    s.set_defaults(fn=lambda a, cfg, led: __import__('mahler.mcp', fromlist=['']).serve(cfg, led) or 0)

    sub.add_parser("version", help="show commit, known-good status, behind-count"
                   ).set_defaults(fn=cmd_version)

    a = ap.parse_args(argv)
    cfg = config.load()
    led = Ledger(config.DB_PATH)
    try:
        return a.fn(a, cfg, led) or 0
    except GHError as e:
        print(f"mahler: {e}", file=sys.stderr)
        return 1
