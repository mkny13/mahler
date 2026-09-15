"""Render the console's state as HTML (D27): one document, two layouts.

The desktop (`.dk`) and phone (`.ph`) layouts are both in the page and a
media query picks one, so a rotating tablet or a resized window never needs a
round trip. Which view or tab is showing, the theme, and which groups are
expanded are browser-side state: attributes on <html> that CSS keys off and
console.js keeps across the 30-second refresh, which swaps only #app.
"""

import html
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "console.css"), encoding="utf-8") as _fh:
    CSS = _fh.read()
with open(os.path.join(HERE, "console.js"), encoding="utf-8") as _fh:
    JS = _fh.read()

VIEWS = (("now", "Now"), ("needs", "Needs you"), ("test", "Ready to test"),
         ("capture", "Capture"), ("backlog", "Backlog"), ("history", "Event stream"))


def e(s):
    return html.escape("" if s is None else str(s), quote=True)


def _a(url, text, cls=""):
    """A link when there is somewhere to go, plain text otherwise."""
    c = f' class="{cls}"' if cls else ""
    if url:
        return f'<a href="{e(url)}" target="_blank" rel="noopener"{c}>{e(text)}</a>'
    return f"<span{c}>{e(text)}</span>" if cls else e(text)


def document(s):
    """The full page: head, the #app fragment, and the script."""
    land = s["landing"]
    return f"""<!DOCTYPE html>
<html lang="en" data-theme="auto" data-view="{e(land['view'])}" data-tab="{e(land['tab'])}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>Mahler</title>
<script>{_EARLY}</script>
<style>{CSS}</style>
</head>
<body>
<div id="app">{app(s)}</div>
<script>{JS}</script>
</body>
</html>"""


# runs before first paint: restore theme and the current view/tab so a
# reload doesn't flash the landing view or the wrong theme
_EARLY = """(function(){var d=document.documentElement;try{var t=localStorage.getItem("mahler.theme");
if(t)d.setAttribute("data-theme",t);var v=sessionStorage.getItem("mahler.view");if(v)d.setAttribute("data-view",v);
var b=sessionStorage.getItem("mahler.tab");if(b)d.setAttribute("data-tab",b);}catch(e){}})();"""


def app(s):
    """The #app fragment — what the 30-second refresh replaces."""
    counts = {"runs": len(s["runs"]), "needs": s["needs_count"], "uat": s["uat_count"],
              "digest": s["digest"]["count"], "digest_upto": s["digest"]["upto"]}
    return (f'<script type="application/json" id="counts">{e(json.dumps(counts))}</script>'
            + _desktop(s) + _phone(s) + _run_overlays(s) + _revert_overlays(s)
            + _bug_overlays(s))


# ---------- shared pieces ----------

def _theme_labels():
    return ('<span class="tl" data-tl="auto">Auto</span>'
            '<span class="tl" data-tl="light" hidden>Light</span>'
            '<span class="tl" data-tl="dark" hidden>Dark</span>')


def _pause_button(s, cls="btn pause"):
    if s["paused"]:
        return f'<button class="{cls} on" data-act="resume">Resume</button>'
    return f'<button class="{cls}" data-act="pause">Pause all</button>'


def _state_label(s):
    return f'<span class="state t-{s["system"]["tone"]}">{e(s["system"]["label"])}</span>'


def _banners(s):
    b = s["banners"]
    if not b:
        return ""
    out = ['<div class="banners">']
    for x in b:
        act = ""
        if x.get("act"):
            act = f'<button class="btn" data-act="{e(x["act"])}">{e(x["action"])}</button>'
        elif x.get("href"):
            act = (f'<a class="btn" href="{e(x["href"])}" target="_blank" rel="noopener">'
                   f'{e(x["action"])}</a>')
        out.append(f'<div class="bn bn-{x["tone"]}"><span class="kind mono">{e(x["kind"])}</span>'
                   f'<span class="txt">{e(x["text"])}</span>{act}</div>')
    out.append("</div>")
    if len(b) > 1:
        rest = b[1:]
        n = len(rest)
        notices = f"{n} more notice{'s' if n > 1 else ''}"
        kinds = ", ".join(x["kind"].split(" · ")[0].lower() for x in rest)
        out.append(f'<button class="bn-more" data-toggle="banners">'
                   f'<span class="c">{e(notices)} · {e(kinds)}</span>'
                   f'<span class="o">Collapse {e(notices)}</span>'
                   f'<span class="mono"><span class="c">▾</span><span class="o">▴</span></span></button>')
    return "".join(out)


def _idle(s, phone):
    idle = s["idle"]
    out = [f'<div class="idle"><span class="idle-h">{e(idle["headline"])}</span>']
    for r in idle["reasons"]:
        cd = f'<span class="cd mono">{e(r["countdown"])}</span>' if r.get("countdown") else ""
        btn = ""
        if r.get("act"):
            data = f' data-platforms="{e(",".join(r["platforms"]))}"' if r.get("platforms") else ""
            btn = f'<button class="btn btn-acc" data-act="{e(r["act"])}"{data}>{e(r["action"])}</button>'
        elif r.get("href"):
            btn = (f'<a class="btn btn-acc" href="{e(r["href"])}" target="_blank" rel="noopener">'
                   f'{e(r["action"])}</a>')
        if phone:
            foot = f'<div class="foot">{cd}{btn}</div>' if cd or btn else ""
            out.append(f'<div class="why"><span class="txt">{e(r["text"])}</span>{foot}</div>')
        else:
            out.append(f'<div class="why"><span class="txt">{e(r["text"])}</span>{cd}{btn}</div>')
    out.append("</div>")
    return "".join(out)


def _peak_act(peak):
    return "peak_restore" if peak["overridden"] else "peak_override"


def _capture_opts(cap):
    opts = ['<option value="" disabled>Project</option>']
    opts += [f'<option value="{e(p)}">{e(p)}</option>' for p in cap["projects"]]
    return "".join(opts)


def _capture_notes(s):
    out = []
    for r in s["capture"]["recent"]:
        if r["status"] not in ("pending", "done"):
            continue
        out.append(f'<div class="capnote t-good">Saved to {e(r["repo"] or r["project"])} as a new '
                   f'issue · sorting run queued. It settles 10 minutes before anything picks it up.'
                   f'</div>')
    return "".join(out)


def _composer(s, rows, save_label):
    cap = s["capture"]
    return (f'<textarea class="cap-ta" data-keep="capture" placeholder="Type or dictate." '
            f'maxlength="8000" rows="{rows}"></textarea>'
            f'<div class="cap-row">'
            f'<select class="cap-select" data-capture-select>{_capture_opts(cap)}</select>'
            f'<button class="btn btn-pri cap-save" data-act="capture" data-capture-save disabled>'
            f'{e(save_label)}</button></div>{_capture_notes(s)}')


def _backlog_groups(s, phone):
    out = []
    for g in s["backlog"]:
        n = len(g["items"])
        rows = []
        for i in g["items"]:
            p = (f'<span class="pchip{" p1" if i["p1"] else ""}">{e(i["p"])}</span>' if phone
                 else f'<span class="p mono t-{"bad" if i["p1"] else "mut"}">{e(i["p"])}</span>')
            title = _a(i["url"], i["title"] or i["ref"], "title")
            st = f'<span class="st mono t-{i["tone"]}">{e(i["state"])}</span>'
            rows.append(f'<div class="{"pbl" if phone else "bl"}">{p}{title}{st}</div>')
        out.append(f'<div class="grp" data-group="{e(g["project"])}">'
                   f'<button class="grp-h" data-toggle-group="{e(g["project"])}">'
                   f'<span class="name">{e(g["project"])}</span>'
                   f'<span class="mono">{n} open · <span class="sh-show">show</span>'
                   f'<span class="sh-hide">hide</span></span></button>'
                   f'<div class="grp-items">{"".join(rows)}</div></div>')
    return "".join(out)


def _event_text(ev):
    tone = "ink" if ev["attention"] else "mut"
    return tone, e(ev["text"])


# ---------- desktop ----------

def _desktop(s):
    rail_counts = {
        "now": (str(len(s["runs"])) if s["runs"] else "", "acc"),
        "needs": (str(s["needs_count"]) if s["needs"] else "", "bad"),
        "test": (str(s["uat_count"]) if s["uat"] else "", "mut"),
        "capture": ("", "mut"),
        "backlog": (str(s["backlog_total"]), "mut"),
        "history": (f'{s["digest"]["count"]} new' if s["digest"]["count"] else "", "acc"),
    }
    rail = ['<nav class="rail"><div class="brand">Mahler</div>']
    for key, label in VIEWS:
        count, tone = rail_counts[key]
        rail.append(f'<button class="rail-i" data-go="{key}"><span>{e(label)}</span>'
                    f'<span class="mono t-{tone}">{e(count)}</span></button>')
    rail.append(f'<div class="rail-foot"><button class="btn" data-theme-cycle>Theme · '
                f'{_theme_labels()}</button></div></nav>')

    titles = "".join(f'<span class="vt vt-{k}">{e(v)}</span>' for k, v in VIEWS)
    peak = s["peak"]
    peak_btn = ""
    if peak and peak["header"]:
        tone = "acc" if peak["overridden"] else "warn"
        peak_btn = (f'<button class="peak-btn" data-act="{_peak_act(peak)}">'
                    f'<span class="txt">{e(peak["short"])}</span>'
                    f'<span class="mono t-{tone}">{e(peak["action"])}</span></button>')
    head = f'<div class="dhead"><span class="dtitle">{titles}</span>{peak_btn}</div>'

    views = (_d_now(s) + _d_needs(s) + _d_test(s) + _d_capture(s) + _d_backlog(s) + _d_history(s))
    main = f'<main class="dmain">{head}<div class="dbody">{views}</div></main>'
    return f'<div class="dk">{"".join(rail)}{main}{_d_side(s)}</div>'


def _d_now(s):
    out = ['<section class="view view-now">']
    if s["peak"]:
        out.append(f'<div class="peakline">{e(s["peak"]["line"])}</div>')
    out.append(f'<div class="sect"><span class="lbl">Active runs · {len(s["runs"])}</span>')
    for r in s["runs"]:
        out.append(f'<button class="drun" data-open-run="{r["id"]}">'
                   f'<span class="ref mono">{e(r["ref"])}</span>'
                   f'<span class="title">{e(r["title"])}</span>'
                   f'<span class="plat mono t-mut">{e(r["platform"])}</span>'
                   f'<span class="timing mono t-{r["tone"]}">{e(r["timing"])}</span>'
                   f'<span class="bar"><span class="f-{r["tone"]}" style="width:{r["progress"]}%">'
                   f'</span></span></button>')
    if s["idle"]:
        out.append(_idle(s, phone=False))
    out.append("</div>")
    out.append(f'<div class="sect rule"><span class="lbl">Capacity</span>'
               f'<span class="capline">{e(s["capacity"])}</span></div>')
    out.append("</section>")
    return "".join(out)


def _need_meta(n):
    return (f'<span class="meta">{_a(n["url"], n["ref"])} · {e(n["p"])} · '
            f'{e(n["meta"])}</span>')


def _answer_buttons(n):
    return '<div class="answers">' + ''.join(
        f'<button class="btn{ " btn-pri" if i == 0 else ""}" data-act="answer" '
        f'data-project="{e(n["project"])}" data-number="{n["number"]}" '
        f'data-text="{e(o["text"])}">{e(o["label"])}</button>'
        for i, o in enumerate(n["options"])) + '</div>'


def _answer_input(n, phone=False):
    placeholder = "or say something…" if phone else "or type an answer…"
    return (f'<div class="reply"><input maxlength="4000" aria-label="Answer {e(n["ref"])}" '
            f'data-keep="need:{e(n["id"])}" placeholder="{placeholder}">'
            f'<button class="btn" aria-label="Send answer" data-act="answer" '
            f'data-project="{e(n["project"])}" data-number="{n["number"]}">↑</button></div>')


def _answered(n):
    return (f'<div class="answered"><span>You said: {e(n["pending"]["text"])}</span>'
            f'<button class="link" data-act="answer_undo" '
            f'data-id="{n["pending"]["id"]}">Undo</button></div>')


def _d_needs(s):
    out = ['<section class="view view-needs">', _banners(s)]
    for n in s["needs"]:
        content = (_answered(n) if n["pending"] else
                   f'<div class="body"><span class="q">{e(n["question"])}</span>'
                   f'{_need_meta(n)}{_answer_input(n)}</div>{_answer_buttons(n)}')
        out.append(f'<div class="need" data-need="{e(n["id"])}">{content}</div>')
    if not s["needs"]:
        out.append('<span class="empty">Nothing waiting on you. Runs continue on their own.</span>')
    out.append("</section>")
    return "".join(out)


def _uat_link(u):
    if not u["link"]:
        return ""
    return f'<span class="lnk">{_a(u["link"], u["link_label"] + " ↗")}</span>'


def _uat_left(u):
    return (f'<div class="body"><span class="t">{e(u["title"])}</span>'
            f'<span class="meta">{_a(u["url"], u["ref"])} · {e(u["meta"])}</span>'
            f'<span class="check">{e(u["check"])}</span>{_uat_link(u)}</div>')


def _uat_buttons(u):
    return ('<div class="uatbtns">'
            f'<button class="btn uat-pass" data-act="uat_pass" '
            f'data-project="{e(u["project"])}" data-number="{u["number"]}">Pass</button>'
            f'<button class="btn uat-fail" data-open-bug="{e(u["ref"])}">Fail</button></div>')


def _uat_done(u):
    """A verdict queued but not yet run shows the copy it will become."""
    if u["pending"] == "uat_pass":
        return '<span class="uat-done t-good">Passed — issue closed, UAT recorded.</span>'
    if u["pending"] == "uat_fail":
        return ('<span class="uat-done t-bad">Failed — p1 bug filed and routed. '
                'The revert is one tap away in History.</span>')
    return ""


def _d_test(s):
    out = ['<section class="view view-test">']
    for u in s["uat"]:
        out.append(f'<div class="uat" data-uat="{e(u["ref"])}">'
                   f'{_uat_left(u)}{_uat_done(u) or _uat_buttons(u)}</div>')
    out.append("</section>")
    return "".join(out)


def _d_capture(s):
    return (f'<section class="view view-capture"><div class="cap">'
            f'{_composer(s, 4, "Save to backlog")}</div></section>')


def _d_backlog(s):
    return f'<section class="view view-backlog">{_backlog_groups(s, phone=False)}</section>'


def _d_history(s):
    rows = []
    for ev in s["events"]:
        tone, text = _event_text(ev)
        rows.append(f'<div class="ev"><span class="when mono">{e(ev["when"])}</span>'
                    f'<span class="kind mono">{e(ev["kind"])}</span>'
                    f'<span class="txt t-{tone}">{text}</span>{_undo(ev)}</div>')
    return f'<section class="view view-history" style="gap:2px">{"".join(rows)}</section>'


def _d_side(s):
    out = ['<aside class="side">',
           f'<div class="side-top">{_state_label(s)}{_pause_button(s)}</div>']
    open_needs = [n for n in s["needs"] if not n["pending"]]
    if open_needs:
        first = max(open_needs, key=lambda n: n["waited_s"])
        out.append(f'<div class="needcard"><span class="lbl">Needs you · {s["needs_count"]}</span>'
                   f'<span class="q">{e(first["question"])}</span>'
                   f'<span class="meta">{_a(first["url"], first["ref"])} · {e(first["meta"])}</span>'
                   f'{_answer_buttons(first)}'
                   f'<button class="link" data-go="needs">All {s["needs_count"]} →</button></div>')
    out.append('<div class="sblock"><span class="lbl">Backlog</span>')
    for g in s["backlog"]:
        out.append(f'<button class="sproj" data-go="backlog" data-open-group="{e(g["project"])}">'
                   f'<span>{e(g["project"])}</span><span class="mono">{e(g["counts"])}</span></button>')
    out.append('</div><div class="sblock" style="gap:7px"><span class="lbl">Capacity</span>')
    for q in s["quota"]:
        out.append(f'<div class="sq"><div class="sq-row"><span class="name mono">{e(q["name"])}</span>'
                   f'<span class="bar"><span class="f-{q["tone"]}" style="width:{q["width"]:.0f}%">'
                   f'</span></span><span class="val mono t-{q["tone"]}">{e(q["label"])}</span></div>'
                   f'<span class="model mono">{e(q["model"])}</span></div>')
    out.append('<button class="link" data-go="history">Event stream →</button></div></aside>')
    return "".join(out)


# ---------- phone ----------

def _phone(s):
    runs, needs = len(s["runs"]), s["needs_count"]
    head = (f'<header class="phead"><div class="phead-row"><div><span class="brand">Mahler</span>'
            f'{_state_label(s)}</div><div class="phead-btns">'
            f'<button class="btn theme" data-theme-cycle>{_theme_labels()}</button>'
            f'{_pause_button(s)}</div></div><div class="tabs">'
            f'<button class="tab" data-tab-go="now"><span>Now</span>'
            f'<span class="mono t-acc">{runs or ""}</span></button>'
            f'<button class="tab" data-tab-go="triage"><span>Triage</span>'
            f'<span class="mono t-bad">{needs or ""}</span></button>'
            f'<button class="tab" data-tab-go="browse"><span>Browse</span></button></div></header>')
    return f'<div class="ph">{head}<div class="pbody">{_p_triage(s)}{_p_now(s)}{_p_browse(s)}</div></div>'


def _p_now(s):
    out = ['<section class="tabv tabv-now"><div class="pad">']
    peak = s["peak"]
    if peak:
        tone = "acc" if peak["overridden"] else "warn"
        out.append(f'<button class="peakrow" data-act="{_peak_act(peak)}">'
                   f'<span class="txt">{e(peak["line"])}</span>'
                   f'<span class="mono t-{tone}">{e(peak["action"])}</span></button>')
    chip = ""
    d = s["digest"]
    if d["count"]:
        chip = (f'<button class="chip" data-toggle="digest"><span class="dot"></span>'
                f'<span class="mono"><span class="c">{d["count"]} new</span>'
                f'<span class="o">new · hide</span></span></button>')
    out.append(f'<div class="psect"><div class="runhead"><span style="display:flex;align-items:center;'
               f'gap:9px"><span class="lbl">Active runs · {len(s["runs"])}</span>{chip}</span></div>')
    if d["count"]:
        rows = "".join(f'<div class="dg"><span class="when mono">{e(r["when"])}</span>'
                       f'<span class="txt t-{"ink" if r["attention"] else "mut"}">'
                       f'{e(r["kind"])} — {e(r["text"])}</span></div>' for r in d["rows"])
        out.append(f'<div class="digest">{rows}<button class="link" data-act="digest_seen" '
                   f'data-upto="{d["upto"]}">Mark all seen</button></div>')
    for r in s["runs"]:
        out.append(f'<button class="prun" data-open-run="{r["id"]}">'
                   f'<span class="row"><span class="ref mono">{e(r["ref"])}</span>'
                   f'<span class="plat mono">{e(r["platform"])}</span></span>'
                   f'<span class="title">{e(r["title"])}</span>'
                   f'<span class="bar" style="width:100%"><span class="f-{r["tone"]}" '
                   f'style="width:{r["progress"]}%"></span></span>'
                   f'<span class="row"><span class="mono t-{r["tone"]}">{e(r["timing"])}</span>'
                   f'<span class="mono t-mut">{e(r["status"])}</span></span></button>')
    if s["idle"]:
        out.append(_idle(s, phone=True))
    out.append('</div>')
    out.append(f'<div class="psect" style="gap:9px"><span class="lbl">Capacity</span>'
               f'<span class="capline">{e(s["capacity"])}</span>'
               f'<button class="link" data-tab-go="browse">All quota gauges →</button></div>')
    out.append("</div></section>")
    return "".join(out)


def _p_triage(s):
    out = ['<section class="tabv tabv-triage">', _banners(s), '<div class="pad">']
    out.append(f'<div class="psect" style="gap:12px"><span class="lbl t-bad">Needs you · '
               f'{s["needs_count"]}</span>')
    for n in s["needs"]:
        if n["pending"]:
            out.append(f'<div class="pneed" data-need="{e(n["id"])}">{_answered(n)}</div>')
            continue
        out.append(f'<div class="pneed{" p1" if n["p1"] else ""}" data-need="{e(n["id"])}">'
                   f'<div class="row"><span class="meta">{_a(n["url"], n["ref"])}</span>'
                   f'<span class="pchip{" p1" if n["p1"] else ""}">{e(n["p"])}</span>'
                   f'<span class="meta" style="font-size:10.5px">{e(n["meta"])}</span></div>'
                   f'<div class="q">{e(n["question"])}</div>'
                   f'{_answer_buttons(n)}{_answer_input(n, phone=True)}</div>')
    if not s["needs"]:
        out.append('<div class="empty" style="font-size:13.5px;padding:4px 0">Nothing waiting on '
                   'you. Runs continue on their own.</div>')
    out.append("</div>")
    if s["uat"]:
        out.append(f'<div class="psect" style="gap:12px"><span class="lbl">Ready to test · '
                   f'{s["uat_count"]}</span>')
        for u in s["uat"]:
            out.append(f'<div class="puat" data-uat="{e(u["ref"])}">'
                       f'<div class="row"><span class="meta">{_a(u["url"], u["ref"])}</span>'
                       f'<span class="meta" style="font-size:10.5px">{e(u["meta"])}</span></div>'
                       f'<div class="t">{e(u["title"])}</div>'
                       f'<div class="check">{e(u["check"])}</div>'
                       f'{_uat_link(u)}{_uat_done(u) or _uat_buttons(u)}</div>')
        out.append("</div>")
    out.append(f'<div class="psect" style="gap:10px"><span class="lbl">Capture</span>'
               f'<div class="cap">{_composer(s, 3, "Save")}</div></div>')
    out.append("</div></section>")
    return "".join(out)


def _p_browse(s):
    out = ['<section class="tabv tabv-browse"><div class="pad">',
           '<div class="psect"><span class="lbl">Quota · worst window</span>']
    for q in s["quota"]:
        tick = ""
        if q["metered"] and q["state"] not in ("backoff", "hold"):
            tick = f'<span class="tick" style="left:{min(q["soft_pct"], 100):.0f}%"></span>'
        out.append(f'<div class="pq"><div class="row"><span><span class="name mono">{e(q["name"])}</span>'
                   f'<span class="model mono">{e(q["model"])}</span></span>'
                   f'<span class="val mono t-{q["tone"]}">{e(q["label"])}</span></div>'
                   f'<span class="bar"><span class="f-{q["tone"]}" style="width:{q["width"]:.0f}%">'
                   f'</span>{tick}</span><span class="detail">{e(q["detail"])}</span></div>')
    out.append('<div class="foot-note">Tick marks the soft line — Mahler stops starting runs there. '
               'Hard line yields work in flight.</div></div>')
    out.append(f'<div class="psect" style="gap:10px"><span class="lbl">Backlog</span>'
               f'{_backlog_groups(s, phone=True)}</div>')
    rows = []
    for ev in s["events"]:
        tone, text = _event_text(ev)
        rows.append(f'<div class="pev"><span class="when mono">{e(ev["when"])}</span>'
                    f'<span class="txt t-{tone}"><span class="kind mono t-ink">{e(ev["kind"])}</span> '
                    f'{text}</span>{_undo(ev)}</div>')
    out.append(f'<div class="psect" style="gap:10px"><span class="lbl">History</span>{"".join(rows)}</div>')
    out.append("</div></section>")
    return "".join(out)


# ---------- overlays ----------

def _run_overlays(s):
    out = []
    for r in s["runs"]:
        out.append(f'<div class="ov runov" data-run-detail="{r["id"]}"><div class="box">'
                   f'<div class="top"><button class="back" data-close-run>← Now</button>'
                   f'<span class="ref mono">{_a(r["url"], r["ref"])}</span></div>'
                   f'<div class="inner"><div style="display:flex;flex-direction:column;gap:7px">'
                   f'<h2>{e(r["title"])}</h2><span class="meta">{e(r["meta"])}</span></div>'
                   f'<div style="display:flex;flex-direction:column;gap:6px">'
                   f'<span class="bar"><span class="f-{r["tone"]}" style="width:{r["progress"]}%">'
                   f'</span></span><span class="timing mono t-{r["tone"]}">{e(r["timing"])}</span>'
                   f'</div></div><div class="runfoot">'
                   f'<button class="btn" data-close-run>Leave running</button>'
                   f'<button class="btn btn-bad" data-act="stop_run" data-run="{r["id"]}">'
                   f'Stop &amp; hand off</button></div></div></div>')
    return "".join(out)


def _undo(ev):
    if ev["undoable"]:
        return f'<button class="undo" data-open-revert="{ev["id"]}">Undo</button>'
    return f'<span class="revert-status">{e(ev.get("revert_status", ""))}</span>'


def _revert_overlays(s):
    out = []
    for ev in s["events"]:
        if not ev["undoable"]:
            continue
        out.append(f'<div class="ov revertov" data-revert-detail="{ev["id"]}">'
                   f'<div class="box" role="dialog" aria-modal="true" '
                   f'aria-labelledby="revert-title-{ev["id"]}">'
                   f'<h2 id="revert-title-{ev["id"]}">Revert {e(ev["text"])}?</h2>'
                   '<p>This opens a revert PR, waits for CI, merges it and redeploys. '
                   'The branch is kept for 14 days.</p><div class="revertfoot">'
                   '<button class="btn" data-close-revert>Keep it</button>'
                   f'<button class="btn revert-confirm" data-act="revert" '
                   f'data-event="{ev["id"]}">Open revert PR</button></div></div></div>')
    return "".join(out)


def _bug_overlays(s):
    """The bug sheet (mahler#250): Fail opens it, 'File p1 bug' confirms."""
    out = []
    for u in s["uat"]:
        out.append(f'<div class="ov bugov" data-bug-detail="{e(u["ref"])}">'
                   f'<div class="box" role="dialog" aria-modal="true">'
                   f'<h2>What went wrong?</h2>'
                   f'<span class="meta">{_a(u["url"], u["ref"])} · {e(u["meta"])}</span>'
                   f'<textarea rows="3" maxlength="2000" data-keep="bug:{e(u["ref"])}" '
                   f'placeholder="One line is enough — it opens a p1 bug with the '
                   f'build SHA and your note."></textarea>'
                   f'<div class="bugfoot"><button class="btn" data-close-bug>Cancel</button>'
                   f'<button class="btn bug-confirm" data-act="uat_fail" '
                   f'data-project="{e(u["project"])}" data-number="{u["number"]}">'
                   f'File p1 bug</button></div></div></div>')
    return "".join(out)
