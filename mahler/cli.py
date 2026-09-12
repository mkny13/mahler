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
        print(json.dumps({
            "paused": led.paused(),
            "runs": [dict(r) for r in led.active_runs()],
            "items": [dict(i) for i in led.items() if i["state"] != "done"],
            "usage": {n: led.usage(n) for n in cfg["platforms"]},
        }, indent=2, default=str))
        return 0
    print("PAUSED — nothing new will start (mahler resume)\n" if led.paused() else "", end="")
    runs = led.active_runs()
    print(f"Running ({len(runs)})")
    for r in runs:
        mins = int((now - parse(r["started_at"])).total_seconds() // 60)
        print(f"  • run {r['id']:<4} {r['project']}#{r['number']:<5} {r['role']:<5} "
              f"{r['platform']:<11} {mins:>3} min  {r['status']}")
    print("\nItems")
    for i in led.items():
        if i["state"] == "done":
            continue
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
    s.set_defaults(fn=cmd_status)

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

    s = sub.add_parser("log", help="summarise a run's output")
    s.add_argument("run_id", type=int)
    s.set_defaults(fn=cmd_log)

    a = ap.parse_args(argv)
    cfg = config.load()
    led = Ledger(config.DB_PATH)
    try:
        return a.fn(a, cfg, led) or 0
    except GHError as e:
        print(f"mahler: {e}", file=sys.stderr)
        return 1
