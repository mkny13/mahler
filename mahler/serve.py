"""The read-only status page (DESIGN D10, ROADMAP Phase 1 item 5).

A standard-library HTTP server bound to 127.0.0.1 that renders what
`mahler status` shows as a single phone-friendly HTML page: running work,
items by state with links to their issues, quota gauges per platform, and
the last 30 events. It auto-refreshes every 30 seconds and follows the OS
light/dark setting.

Deliberately read-only: only GET is served, and the only GET that answers
is `/`. Exposing it beyond the machine (`tailscale serve`) is a machine
setting the owner turns on; the command is documented in the README, and
the launchd plist template in launcher/ is not installed automatically.
"""

import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, router
from .ledger import STATES, parse

REFRESH_SECONDS = 30
EVENTS_SHOWN = 30

# item states in a fixed display order (done is not shown: closed issues
# live on GitHub)
PAGE_STATES = [s for s in STATES if s != "done"]


def snapshot(cfg, led):
    """Everything the page shows, plain data — the HTML is derived from this."""
    now = led.now()
    runs = led.active_runs()
    items = [i for i in led.items() if i["state"] != "done"]
    by_state = {s: [] for s in PAGE_STATES}
    for i in items:
        by_state.setdefault(i["state"], []).append(i)
    leases = {}
    for i in items:
        lease = led.lease(i["project"], i["number"])
        if lease:
            leases[(i["project"], i["number"])] = lease["holder"]

    quota = []
    for name, pconf in cfg["platforms"].items():
        state, detail = router.usage_state(led, name, pconf)
        # gauge width: worst window's percentage (unmetered platforms show 0)
        windows = [u["used_pct"] for u in led.usage(name).values()
                   if u["window"] in router.WINDOWS]
        pct = max(min(max(windows, default=0), 100), 0)
        quota.append({"name": name, "state": state, "detail": detail,
                      "pct": pct, "metered": pconf.get("metered", True)})

    events = [dict(e) for e in
              led.q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (EVENTS_SHOWN,))][::-1]
    return {
        "now": now,
        "paused": led.paused(),
        "runs": runs,
        "by_state": by_state,
        "leases": leases,
        "quota": quota,
        "events": events,
    }


def _esc(s):
    return html.escape(str(s), quote=True)


def render_page(snap, cfg):
    out = []
    w = out.append
    w(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta http-equiv="refresh" content="{REFRESH_SECONDS}">
<title>Mahler status</title>
<style>
:root {{
  --bg: #f5f5f7; --card: #ffffff; --fg: #1d1d1f; --muted: #6e6e73;
  --line: #e3e3e8; --accent: #0a66c2; --ok: #1a7f37; --soft: #b45309;
  --hard: #b91c1c; --stale: #6e6e73; --bar: #d9d9de;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #111114; --card: #1c1c21; --fg: #f2f2f5; --muted: #a0a0a8;
    --line: #2c2c33; --accent: #6cb2eb; --ok: #4ade80; --soft: #fbbf24;
    --hard: #f87171; --stale: #a0a0a8; --bar: #2c2c33;
  }}
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0 auto; padding: 1rem; max-width: 42rem;
  background: var(--bg); color: var(--fg);
  font: 16px/1.45 -apple-system, system-ui, sans-serif;
}}
h1 {{ font-size: 1.3rem; margin: 0 0 .25rem; }}
h2 {{ font-size: 1.05rem; margin: 1.4rem 0 .4rem; }}
h3 {{ font-size: .95rem; margin: .9rem 0 .35rem; }}
h2 .count, h3 .count {{ color: var(--muted); font-weight: normal; }}
.meta {{ color: var(--muted); font-size: .8rem; }}
.paused {{
  background: var(--soft); color: var(--card); padding: .55rem .8rem;
  border-radius: .5rem; margin: .75rem 0; font-weight: 600;
}}
.card {{
  background: var(--card); border: 1px solid var(--line);
  border-radius: .6rem; padding: .6rem .75rem; margin: .45rem 0;
}}
.mono {{ font-variant-numeric: tabular-nums; }}
.muted {{ color: var(--muted); }}
a {{ color: var(--accent); text-decoration: none; }}
table {{ width: 100%; border-collapse: collapse; }}
td {{ padding: .15rem 0; vertical-align: top; }}
td.t {{ text-align: right; white-space: nowrap; color: var(--muted); }}
.gauge {{ position: relative; background: var(--bar); border-radius: .4rem;
          height: 1.15rem; overflow: hidden; margin-top: .35rem; }}
.gauge .fill {{ height: 100%; }}
.q-state-ok {{ color: var(--ok); }}
.q-state-soft {{ color: var(--soft); }}
.q-state-hard {{ color: var(--hard); }}
.q-state-stale {{ color: var(--stale); }}
.state-working {{ color: var(--accent); font-weight: 600; }}
.state-needs_you {{ color: var(--hard); font-weight: 600; }}
.state-ready {{ color: var(--ok); }}
.fill-ok {{ background: var(--ok); }}
.fill-soft {{ background: var(--soft); }}
.fill-hard {{ background: var(--hard); }}
.fill-stale {{ background: var(--stale); }}
</style>
</head>
<body>
<h1>Mahler</h1>
<p class="meta">read-only &middot; refreshes every {REFRESH_SECONDS}s</p>""")
    if snap["paused"]:
        w('<div class="paused">PAUSED &mdash; nothing new will start</div>')
    return _render_rest(out, snap, cfg)


def _render_rest(out, snap, cfg):
    w = out.append

    w(f"<h2>Running <span class=\"count\">({len(snap['runs'])})</span></h2>")
    if snap["runs"]:
        for r in snap["runs"]:
            started = parse(r["started_at"])
            mins = max(int((snap["now"] - started).total_seconds() // 60), 0) if started else 0
            stopped = f" &middot; {_esc(r['stop_reason'])}" if r["stop_reason"] else ""
            w(f"<div class=\"card run\">"
              f"<div><b>{_esc(r['project'])}#{r['number']}</b> "
              f"<span class=\"muted\">{_esc(r['role'])}</span></div>"
              f"<div><span class=\"mono\">{mins} min</span> on "
              f"<b>{_esc(r['platform'])}</b> &middot; {_esc(r['status'])}{stopped}</div></div>")
    else:
        w('<div class="muted">nothing running</div>')

    w("<h2>Items</h2>")
    any_items = False
    for state in PAGE_STATES:
        group = snap["by_state"].get(state, [])
        if not group:
            continue
        any_items = True
        w(f"<h3 class=\"state-{_esc(state)}\">"
          f"{_esc(state)} <span class=\"count\">({len(group)})</span></h3>")
        for i in group:
            url = None
            repo = config.project_policy(cfg, i["project"]).get("repo")
            if repo:
                url = f"https://github.com/{repo}/issues/{i['number']}"
            ref = f"{_esc(i['project'])}#{i['number']}"
            title = _esc((i["title"] or "")[:70])
            label = f'<a href="{_esc(url)}">{ref}</a> {title}' if url else f"{ref} {title}"
            holder = snap["leases"].get((i["project"], i["number"]))
            held = f' &middot; held by <b>{_esc(holder)}</b>' if holder else ""
            tries = f' &middot; tries {i["attempts"]}' if i["attempts"] else ""
            w(f"<div class=\"card item\"><div>{label}</div>"
              f"<div class=\"meta\">p{i['priority']}{held}{tries}</div></div>")
    if not any_items:
        w('<div class="muted">nothing open</div>')

    w("<h2>Quota</h2>")
    for q in snap["quota"]:
        cls = _esc(q["state"])
        label = "unmetered" if not q["metered"] else f"{q['pct']:.0f}%"
        w(f"<div class=\"card\">"
          f"<div><b>{_esc(q['name'])}</b> <span class=\"q-state-{cls}\">{_esc(q['state'])}</span>"
          f" <span class=\"muted\">{_esc(q['detail'])}</span></div>"
          f"<div class=\"gauge\"><div class=\"fill fill-{cls}\" "
          f"style=\"width:{q['pct']:.0f}%\"></div>"
          f"<div class=\"meta\" style=\"position:absolute;inset:0;display:flex;"
          f"align-items:center;padding:0 .5rem;line-height:1.15rem\">"
          f"<span class=\"mono\">{_esc(label)}</span></div></div></div>")

    w(f"<h2>Events <span class=\"count\">(last {EVENTS_SHOWN})</span></h2>")
    if snap["events"]:
        w('<div class="card"><table>')
        for e in snap["events"]:
            when = parse(e["at"]).astimezone().strftime("%m-%d %H:%M") if e["at"] else ""
            where = f"{e['project']}#{e['number']}" if e["project"] else ""
            w(f"<tr><td class=\"t mono\">{_esc(when)}</td>"
              f"<td><b>{_esc(e['kind'])}</b> <span class=\"muted\">{_esc(where)}</span> "
              f"{_esc((e['detail'] or '')[:100])}</td></tr>")
        w("</table></div>")
    else:
        w('<div class="muted">no events yet</div>')

    w("</body></html>")
    return "\n".join(out)


def _page_bytes(cfg, led, lock):
    with lock:
        return render_page(snapshot(cfg, led), cfg).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    cfg = None
    led = None
    lock = None

    def do_GET(self):
        if self.path != "/":
            self.send_error(404)
            return
        try:
            body = _page_bytes(self.cfg, self.led, self.lock)
        except Exception:
            self.send_error(500, "render failed")
            raise
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _reject(self):
        self.send_error(405, "read-only: only GET / is served")

    do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _reject

    def log_message(self, fmt, *args):
        # one line per hit; launchd captures stderr for us
        import sys
        sys.stderr.write("serve: %s %s\n" % (self.address_string(), fmt % args))


def serve(cfg, led, port=8787):
    """Start the read-only status server. Blocks until interrupted."""
    import threading
    handler = type("Handler", (_Handler,),
                   {"cfg": cfg, "led": led, "lock": threading.Lock()})
    # localhost only, always: putting the page on the tailnet is the owner's
    # `tailscale serve` call (README), never ours.
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"mahler status page: http://127.0.0.1:{port}/ (read-only, Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0

