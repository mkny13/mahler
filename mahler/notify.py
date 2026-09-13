"""Phone pings via ntfy (DESIGN D10). Titles and links only — never secrets."""

import urllib.request

# ntfy's Title header is an HTTP header, so it has to be ASCII (headers are
# sent as latin-1; ntfy shows anything else as "?" or 400s). RFC 2047
# encoded-words are unreliable in practice, so instead we transliterate the
# handful of punctuation marks Mahler actually emits (mahler#45) to their
# ASCII look-alikes before falling back to "?" for anything else.
_ASCII_PUNCTUATION = str.maketrans({
    "\u2014": "-",   # em dash —
    "\u2013": "-",   # en dash –
    "\u2018": "'",   # left single quote '
    "\u2019": "'",   # right single quote '
    "\u201c": '"',   # left double quote "
    "\u201d": '"',   # right double quote "
    "\u2026": "...", # ellipsis …
})


def _ascii_title(title):
    """Best-effort ASCII rendering of a notification title for the Title header."""
    return title.translate(_ASCII_PUNCTUATION).encode("ascii", "replace").decode()


def send(cfg, title, message="", click=None, priority="default", tags=""):
    topic = cfg.get("ntfy", {}).get("topic")
    if not topic:
        return False
    server = cfg["ntfy"].get("server", "https://ntfy.sh").rstrip("/")
    req = urllib.request.Request(f"{server}/{topic}", data=(message or title).encode(),
                                 method="POST")
    req.add_header("Title", _ascii_title(title))
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
