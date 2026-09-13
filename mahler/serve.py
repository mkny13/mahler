"""The read-only status page (DESIGN D10, ROADMAP Phase 1 item 5).

A standard-library HTTP server that renders what
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
    ests = led.estimates()

    runs = [dict(r) for r in led.active_runs()]
    for r in runs:
        r["est"] = int(led.run_estimate(ests, r["platform"], r["role"]))
        # fetch parent info for this run's issue
        item = led.item(r["project"], r["number"])
        if item:
            r["parent"] = item["parent"] if "parent" in item.keys() else None

    items = [dict(i) for i in led.items() if i["state"] != "done"]
    by_state = {s: [] for s in PAGE_STATES}
    leases = {}
    for i in items:
        i["est"] = int(led.issue_estimate(ests, i["project"]))
        by_state.setdefault(i["state"], []).append(i)

        lease = led.lease(i["project"], i["number"])
        if lease:
            leases[(i["project"], i["number"])] = lease["holder"]

    quota = []
    burst_lines = router.burst_status(cfg, led)
    for name, pconf in cfg["platforms"].items():
        claude_lines = burst_lines if pconf.get("kind") == "claude" else None
        state, detail = router.usage_state(led, name, pconf, burst_lines=claude_lines)
        # gauge width: worst window's percentage (unmetered platforms show 0)
        windows = [u["used_pct"] for u in led.usage(name).values()
                   if u["window"] in pconf.get("windows", router.WINDOWS)]
        pct = max(min(max(windows, default=0), 100), 0)
        chips = router.window_countdowns(led, name, pconf)
        quota.append({"name": name, "state": state, "detail": detail,
                      "pct": pct, "metered": pconf.get("metered", True),
                      "chips": chips})

    events = [dict(e) for e in
              led.q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (EVENTS_SHOWN,))][::-1]
    return {
        "now": now,
        "paused": led.paused(),
        "peak": router.peak_status_line(cfg, led),
        "runs": runs,
        "by_state": by_state,
        "leases": leases,
        "quota": quota,
        "burst": router.burst_kind(burst_lines) if burst_lines else None,
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
.q-head {{ display: flex; justify-content: space-between; align-items: flex-start; gap: .4rem; }}
.chips {{ display: flex; gap: .3rem; flex-wrap: wrap; justify-content: flex-end; }}
.chip {{
  font-size: .72rem; padding: .1rem .4rem; border-radius: .4rem;
  background: var(--bar); color: var(--muted); white-space: nowrap;
}}
.chip-warn {{ background: var(--hard); color: #fff; }}
.topbar {{
  position: sticky; top: 0; z-index: 10;
  background: var(--bg);
  margin: -1rem -1rem .6rem;
  padding: .6rem 1rem .5rem;
  border-bottom: 1px solid var(--line);
}}
.topbar-row {{
  display: flex; justify-content: space-between;
  align-items: baseline; gap: .5rem; flex-wrap: wrap;
}}
.jump {{ display: flex; gap: .35rem; flex-wrap: wrap; margin-top: .3rem; }}
.jump a {{
  font-size: .85rem; padding: .5rem .65rem; border-radius: .45rem;
  background: var(--card); border: 1px solid var(--line);
}}
details.section {{ scroll-margin-top: 4rem; }}
details.section > summary {{
  cursor: pointer; margin: 1.1rem 0 .4rem;
  padding: .35rem 0; border-radius: .4rem;
}}
details.section > summary h2 {{ margin: 0; }}
</style>
</head>
<body>
<header class="topbar">
  <div class="topbar-row"><h1>Mahler</h1>
  <p class="meta">read-only &middot; refreshes every {REFRESH_SECONDS}s</p></div>
  <nav class="jump" aria-label="Sections">
    <a href="#running">Running</a>
    <a href="#quota">Quota</a>
    <a href="#items">Items</a>
    <a href="#events">Events</a>
  </nav>
</header>""")
    if snap["paused"]:
        w('<div class="paused">PAUSED &mdash; nothing new will start</div>')
    return _render_rest(out, snap, cfg)


def _render_rest(out, snap, cfg):
    w = out.append

    w('<details class="section" id="running" open>')
    w(f"<summary><h2>Running <span class=\"count\">({len(snap['runs'])})</span></h2></summary>")
    if snap["runs"]:
        for r in snap["runs"]:
            started = parse(r["started_at"])
            mins = max(int((snap["now"] - started).total_seconds() // 60), 0) if started else 0
            est = r["est"]
            time_str = f"{mins} min / ~{est} min" if mins <= est else f"{mins} min <span class=\"hard\">(+{mins-est}m past est)</span>"
            stopped = f" &middot; {_esc(r['stop_reason'])}" if r["stop_reason"] else ""
            repo = config.project_policy(cfg, r["project"]).get("repo")
            ref = f"{_esc(r['project'])}#{r['number']}"
            if repo:
                run_url = f"https://github.com/{repo}/issues/{r['number']}"
                run_label = f'<a href="{_esc(run_url)}">{ref}</a>'
            else:
                run_label = ref
            parent_str = ""
            if r.get("parent"):
                parent_ref = f"{_esc(r['project'])}#{r['parent']}"
                if repo:
                    parent_url = f"https://github.com/{repo}/issues/{r['parent']}"
                    parent_str = f' &middot; part of <a href="{_esc(parent_url)}">{parent_ref}</a>'
                else:
                    parent_str = f' &middot; part of {parent_ref}'
            w(f"<div class=\"card run\">"
              f"<div><b>{run_label}</b> "
              f"<span class=\"muted\">{_esc(r['role'])}</span></div>"
              f"<div><span class=\"mono\">{time_str}</span> on "
              f"<b>{_esc(r['platform'])}</b> &middot; {_esc(r['status'])}{stopped}{parent_str}</div></div>")
    else:
        w('<div class="muted">nothing running</div>')
    w("</details>")

    w('<details class="section" id="quota" open>')
    w("<summary><h2>Quota</h2></summary>")
    if snap.get("peak"):
        w(f'<div class="card" style="margin-bottom:.5rem"><b style="color:var(--soft)">'
          f'Peak window</b> <span class="muted">{_esc(snap["peak"])}</span></div>')
    if snap.get("burst"):
        w(f'<div class="card" style="margin-bottom:.5rem"><b style="color:var(--accent)">'
          f'D23 {snap["burst"]} burst active</b> <span class="muted">— Claude '
          f'builds first, lines raised to 90%/97%</span></div>')
    for q in snap["quota"]:
        cls = _esc(q["state"])
        label = "unknown limit" if not q["metered"] else f"{q['pct']:.0f}%"
        warn = q["state"] in ("soft", "hard")
        chips = "".join(
            f'<span class="chip{" chip-warn" if warn else ""}">{_esc(w_label)} {_esc(cd)}</span>'
            for w_label, cd in q["chips"])
        chips_html = f'<div class="chips">{chips}</div>' if chips else ""
        w(f"<div class=\"card\">"
          f"<div class=\"q-head\"><div><b>{_esc(q['name'])}</b> <span class=\"q-state-{cls}\">{_esc(q['state'])}</span>"
          f" <span class=\"muted\">{_esc(q['detail'])}</span></div>{chips_html}</div>"
          f"<div class=\"gauge\"><div class=\"fill fill-{cls}\" "
          f"style=\"width:{q['pct']:.0f}%\"></div>"
          f"<div class=\"meta\" style=\"position:absolute;inset:0;display:flex;"
          f"align-items:center;padding:0 .5rem;line-height:1.15rem\">"
          f"<span class=\"mono\">{_esc(label)}</span></div></div></div>")

    w("</details>")
    w('<details class="section" id="items" open>')
    w("<summary><h2>Items</h2></summary>")
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
            est = i["est"]
            est_str = f' &middot; ~{est}m'
            parent_str = ""
            if i.get("parent"):
                parent_ref = f"{_esc(i['project'])}#{i['parent']}"
                if repo:
                    parent_url = f"https://github.com/{repo}/issues/{i['parent']}"
                    parent_str = f' &middot; part of <a href="{_esc(parent_url)}">{parent_ref}</a>'
                else:
                    parent_str = f' &middot; part of {parent_ref}'
            w(f"<div class=\"card item\"><div>{label}</div>"
              f"<div class=\"meta\">p{i['priority']}{held}{tries}{est_str}{parent_str}</div></div>")
    if not any_items:
        w('<div class="muted">nothing open</div>')
    w("</details>")

    w('<details class="section" id="events" open>')
    w(f"<summary><h2>Events <span class=\"count\">(last {EVENTS_SHOWN})</span></h2></summary>")
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
    w("</details>")

    # minimal script (issue #134): keep each section's open/closed state in
    # localStorage keyed by section id, so the 30s meta-refresh does not snap
    # collapsed sections back open. A header jump link also opens its target.
    w("""<script>
(function () {
  var KEY = "mahler.section.";
  var secs = document.querySelectorAll("details.section");
  for (var i = 0; i < secs.length; i++) {
    var s = secs[i];
    try {
      var v = localStorage.getItem(KEY + s.id);
      if (v === "closed") { s.open = false; }
      else if (v === "open") { s.open = true; }
    } catch (e) {}
    s.addEventListener("toggle", function () {
      try { localStorage.setItem(KEY + this.id, this.open ? "open" : "closed"); }
      catch (e) {}
    });
  }
  var links = document.querySelectorAll(".jump a");
  for (var j = 0; j < links.length; j++) {
    links[j].addEventListener("click", function () {
      var t = document.getElementById(this.getAttribute("href").slice(1));
      if (t) { t.open = true; }
    });
  }
})();
</script>""")
    w("</body></html>")
    return "\n".join(out)


def _page_bytes(led, lock, load_cfg):
    with lock:
        cfg = load_cfg()
        return render_page(snapshot(cfg, led), cfg).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    led = None
    lock = None
    load_cfg = None

    def do_GET(self):
        if self.path != "/":
            self.send_error(404)
            return
        try:
            body = _page_bytes(self.led, self.lock, self.load_cfg)
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


def serve(cfg, led, host="127.0.0.1", port=8787):
    """Start the read-only status server. Blocks until interrupted.

    `cfg` is only used to resolve the initial bind address by the caller;
    the server itself re-reads `~/.mahler/config.toml` on every request
    (`config.load()`), so projects added to config after the server started
    still get their GitHub links without a restart (mahler#50).
    """
    import threading
    handler = type("Handler", (_Handler,),
                   {"led": led, "lock": threading.Lock(),
                    "load_cfg": staticmethod(config.load)})
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"mahler status page: http://{host}:{port}/ (read-only, Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0

