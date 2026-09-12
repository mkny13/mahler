"""Phone pings via ntfy (DESIGN D10). Titles and links only — never secrets."""

import urllib.request


def send(cfg, title, message="", click=None, priority="default", tags=""):
    topic = cfg.get("ntfy", {}).get("topic")
    if not topic:
        return False
    server = cfg["ntfy"].get("server", "https://ntfy.sh").rstrip("/")
    req = urllib.request.Request(f"{server}/{topic}", data=(message or title).encode(),
                                 method="POST")
    req.add_header("Title", title.encode("ascii", "replace").decode())
    req.add_header("Priority", priority)
    if tags:
        req.add_header("Tags", tags)
    if click:
        req.add_header("Click", click)
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except OSError:
        return False
