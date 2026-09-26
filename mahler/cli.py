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
import subprocess
import sys
from datetime import timedelta

from . import config, notify, platforms, router, scheduler, usage as usage_mod
from .gh import GH, GHError
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


def cmd_status(a, cfg, led):
    now = led.now()
    if not a.json:
        for warning in platforms.effort_warnings(cfg) + config.routing_warnings(cfg):
            print(warning)
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
            "usage": {n: led.usage(n) for n in cfg["platforms"]},
            "estimates": {
                "calibration": led.calibration_stats(),
                "calibration_factor": led.calibration_factor(),
            },
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
        print(f"  {i['state']:<10} {i['project']}#{i['number']:<5} p{i['priority']}  "
              f"{(i['title'] or '')[:60]}{held}{tries}{setup}{est_str}{url}")
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
    gh = GH(pol["repo"], env=config.run_env(cfg, config.gh_account_of(pol)))
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
            r = subprocess.run(["git", "-C", pol["path"], "ls-remote", "--exit-code",
                                "origin", f"refs/heads/{branch}"],
                               capture_output=True, text=True, timeout=90)
            if r.returncode != 0:
                print(f"branch {branch!r} isn't on origin — push it first")
                return 1
    except (GHError, ValueError, subprocess.SubprocessError, OSError) as e:
        print(f"mahler ship: {e}")
        return 1
    fields = {"branch": branch, "pr": pr, "title": title or f"{project} #{n}"}
    if a.summary:
        fields["summary"] = a.summary
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
    gh = GH(pol["repo"], env=config.run_env(cfg, config.gh_account_of(pol)))
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


def cmd_add(a, cfg, led):
    pol = config.project_policy(cfg, a.project)
    if not pol.get("repo"):
        print(f"unknown project {a.project!r}")
        return 1
    print(GH(pol["repo"], env=config.run_env(cfg, config.gh_account_of(pol))).create_issue(a.title, a.body or ""))
    return 0


def cmd_notify(a, cfg, led):
    ok = notify.send(cfg, a.title, a.message or "")
    print("sent" if ok else "not sent (no ntfy topic configured?)")
    return 0 if ok else 1


def cmd_labels(a, cfg, led):
    pol = config.project_policy(cfg, a.project)
    GH(pol["repo"], env=config.run_env(cfg, config.gh_account_of(pol))).ensure_labels()
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
    rows = scorecard.table(led, cfg, project=a.project, since=since)
    rows = [r for r in rows if (not a.role or r["role"] == a.role)
            and (not a.size or r["size"] == a.size)]
    if a.raw:
        print(json.dumps([attempt for row in rows for attempt in row["attempts"]], indent=2))
    else:
        print("Model scorecard · API-equivalent dollars (weighted)")
        for row in rows:
            print(f'{row["role"]} / {row["size"] or "unknown size"}: {scorecard.summary(row)}')
        if not rows:
            print("No attempts in this window.")


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

    s = sub.add_parser("scorecard", help="model cost and first-attempt success by role and size")
    s.add_argument("--project")
    s.add_argument("--role")
    s.add_argument("--size")
    s.add_argument("--since", type=_scorecard_days, metavar="30d")
    s.add_argument("--raw", action="store_true")
    s.set_defaults(fn=cmd_scorecard)

    s = sub.add_parser("serve", help="the operator console (D27)")
    s.add_argument("--host", type=str, default=None,
                   help="interface to bind (default: serve.host in config, else 127.0.0.1)")
    s.add_argument("--port", type=int, default=None,
                   help="port to bind (default: serve.port in config, else 8787)")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("hooks", help="install Claude Code session hooks")
    s.add_argument("project")
    s.set_defaults(fn=cmd_hooks)

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

    s = sub.add_parser("backfill-usage", help="recover accounting from ended run logs; price runs whose model now has a [prices] row")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_backfill_usage)

    s = sub.add_parser("log", help="summarise a run's output")
    s.add_argument("run_id", type=int)
    s.set_defaults(fn=cmd_log)

    s = sub.add_parser("mcp", help="run MCP server over stdio")
    s.set_defaults(fn=lambda a, cfg, led: __import__('mahler.mcp', fromlist=['']).serve(cfg, led) or 0)

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
