"""`mahler serve`: the operator console over HTTP (DESIGN D10, D27).

A standard-library server for the console in mahler/console/: the page at
`/`, the part the 30-second refresh swaps at `/fragment`, the same state as
JSON at `/api/state`, and the console's writes as `POST /api/<action>`.

Writes are guarded three ways, because the page is reachable from a phone
(`tailscale serve`) and any web page could try to post to it:
  - the caller must be on this machine or the tailnet (loopback, which is
    also how `tailscale serve` connects, or a Tailscale address);
  - the request must carry `X-Mahler-Console: 1` and a JSON body — a
    cross-site form or fetch can't send that header without a CORS preflight,
    and this server answers no preflight;
  - an `Origin`, when sent, must match the `Host` it was sent to.

Exposing it beyond the machine is a machine setting the owner turns on; the
launchd plist template in launcher/ says how.
"""

import ipaddress
import json
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import config
from .console import actions, page, state
from . import version as _version

MAX_BODY = 64 * 1024
TAILNET = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))


def _default_get_head():
    return _version._short_head(config.REPO_ROOT)


def _check_for_update(httpd, get_head, old_head, interval):
    """Daemon thread: poll HEAD; restart server when it changes."""
    while True:
        time.sleep(interval)
        new_head = get_head()
        if new_head is None:
            sys.stderr.write("serve: cannot read HEAD, will retry\n")
            continue
        if new_head != old_head[0]:
            sys.stderr.write(
                f"serve: code updated {old_head[0]} -> {new_head}, restarting\n"
            )
            old_head[0] = new_head
            httpd.shutdown()
            break


class _Handler(BaseHTTPRequestHandler):
    led = None
    lock = None
    load_cfg = None

    def _send(self, status, body, ctype):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj, default=_json_default), "application/json")

    def _state(self):
        cfg = self.load_cfg()
        return cfg, state.build(cfg, self.led)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith("/api/run/") and path.endswith("/log"):
            run_id_str = path[len("/api/run/"):-len("/log")]
            try:
                run_id = int(run_id_str)
            except ValueError:
                self.send_error(404)
                return
            
            with self.lock:
                row = self.led.con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            
            if not row or row["status"] not in ("running", "stopping"):
                self.send_error(404)
                return
                
            from .console import logtail
            run_dict = dict(row)
            lines = logtail.tail(run_dict)
            status_obj = logtail.live_status(run_dict)
            
            self._json(200, {"lines": lines, "status": status_obj})
            return

        if path not in ("/", "/fragment", "/api/state"):
            self.send_error(404)
            return
        try:
            with self.lock:
                _, s = self._state()
                if path == "/":
                    out, ctype = page.document(s), "text/html; charset=utf-8"
                elif path == "/fragment":
                    out, ctype = page.app(s), "text/html; charset=utf-8"
                else:
                    out, ctype = json.dumps(s, default=_json_default), "application/json"
        except Exception:
            self.send_error(500, "render failed")
            raise
        self._send(200, out, ctype)

    def _refusal(self):
        """Why this write is refused, or None to let it through."""
        if not write_allowed_from(self.client_address[0]):
            return 403, "writes are only accepted from this machine or the tailnet"
        if self.headers.get("X-Mahler-Console") != "1":
            return 403, "missing X-Mahler-Console header"
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return 415, "the body must be JSON"
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != (self.headers.get("Host") or ""):
            return 403, "cross-origin write refused"
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return 400, "bad Content-Length"
        if length > MAX_BODY:
            return 413, "body too large"
        return None

    def do_POST(self):
        path = urlsplit(self.path).path
        name = path[len("/api/"):] if path.startswith("/api/") else None
        if name not in actions.ACTIONS:
            self.send_error(404 if path.startswith("/api/") else 405)
            return
        refused = self._refusal()
        if refused:
            self._json(refused[0], {"ok": False, "error": refused[1]})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json(400, {"ok": False, "error": "the body is not valid JSON"})
            return
        try:
            with self.lock:
                result = actions.run(self.load_cfg(), self.led, name, body)
        except actions.ActionError as e:
            self._json(400, {"ok": False, "error": str(e)})
            return
        except Exception:
            self._json(500, {"ok": False, "error": "the action failed"})
            raise
        self._json(200, {"ok": True, **(result or {})})

    def _reject(self):
        self.send_error(405, "only GET, and POST to /api/<action>")

    do_PUT = do_DELETE = do_PATCH = do_HEAD = _reject

    def log_message(self, fmt, *args):
        # one line per hit; launchd captures stderr for us
        sys.stderr.write("serve: %s %s\n" % (self.address_string(), fmt % args))


def _json_default(o):
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


def write_allowed_from(addr):
    """True for loopback and Tailnet addresses — who may use the writes."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip.is_loopback or any(ip in net for net in TAILNET)


def serve(cfg, led, host="127.0.0.1", port=8787,
          get_head=_default_get_head, check_interval=60):
    """Start the console server. Blocks until interrupted.

    `cfg` is only used to resolve the initial bind address by the caller;
    the server itself re-reads `~/.mahler/config.toml` on every request
    (`config.load()`), so projects added to config after the server started
    show up without a restart (mahler#50).

    If the checkout's git HEAD changes while serving (code updated by the
    launcher), the server shuts itself down so launchd's KeepAlive restarts
    it on the new code.  *get_head* and *check_interval* are parameters so
    tests can drive them.
    """
    handler = type("Handler", (_Handler,),
                   {"led": led, "lock": threading.Lock(),
                    "load_cfg": staticmethod(config.load)})
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"mahler console: http://{host}:{port}/ (Ctrl-C to stop)")
    old_head = [get_head()]
    if old_head[0] is not None:
        t = threading.Thread(
            target=_check_for_update,
            args=(httpd, get_head, old_head, check_interval),
            daemon=True,
        )
        t.start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
