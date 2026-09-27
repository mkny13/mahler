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
import sys
import threading
import time
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from . import config, redact
from .console import actions, page, state
from . import version as _version

CLIENT_LOG_WINDOW_SECONDS = 60
CLIENT_LOG_MAX_EVENTS = 60
MAX_BODY = 64 * 1024
TAILNET = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))



class _ClientLogLimiter:
    """Per-server, per-source fixed windows; never takes the action lock."""

    def __init__(self):
        self.lock = threading.Lock()
        self.windows = {}

    def allow(self, address):
        now = time.monotonic()
        with self.lock:
            self.windows = {ip: entry for ip, entry in self.windows.items()
                            if now - entry[0] < CLIENT_LOG_WINDOW_SECONDS}
            start, count = self.windows.get(address, (now, 0))
            if count >= CLIENT_LOG_MAX_EVENTS:
                return False
            self.windows[address] = (start, count + 1)
            return True


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

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.client_log_limiter = _ClientLogLimiter()

    def handle_one_request(self):
        # Reset for each request, including when a connection is reused.
        self._request_started = time.monotonic()
        self._response_status = None
        self._response_size = 0
        try:
            super().handle_one_request()
        finally:
            if self._response_status is not None:
                self.log_message("end")

    def log_request(self, code="-", size="-"):
        # send_response calls this before headers/body; defer until completion.
        self._response_status = code

    def send_header(self, keyword, value):
        super().send_header(keyword, value)
        if keyword.lower() == "content-length" and self.command != "HEAD":
            self._response_size = int(value)

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

    def _state(self, stats_range="week", section=None):
        cfg = self.load_cfg()
        return cfg, state.build(cfg, self.led, stats_range, section=section)

    def do_GET(self):
        request = urlsplit(self.path)
        path = request.path
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

        if path.startswith("/attachments/"):
            import re, os
            filename = path[len("/attachments/"):]
            if not re.match(r"^[0-9a-f]{32}\.(png|jpe?g|gif|webp|heic)$", filename):
                self.send_error(404)
                return
            
            file_path = os.path.join(config.ATTACHMENTS_DIR, filename)
            if not os.path.isfile(file_path):
                self.send_error(404)
                return
                
            ext = filename.rsplit(".", 1)[-1]
            ctype = {
                "png": "image/png",
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "gif": "image/gif",
                "webp": "image/webp",
                "heic": "image/heic"
            }.get(ext, "application/octet-stream")
            
            try:
                with open(file_path, "rb") as f:
                    data = f.read()
            except OSError:
                self.send_error(404)
                return
                
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=86400")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
            return

        if path.startswith("/api/releases/") or (path.startswith("/api/projects/") and path.endswith("/releases.json")):
            if path.startswith("/api/releases/"):
                proj = path[len("/api/releases/"):]
                if proj.endswith(".json"):
                    proj = proj[:-len(".json")]
            else:
                proj = path[len("/api/projects/"):-len("/releases.json")]
            proj = unquote(proj).strip("/")
            if not proj:
                self.send_error(404)
                return
            with self.lock:
                cfg = self.load_cfg()
                enabled = {p["name"] for p in config.enabled_projects(cfg)}
                if proj not in enabled:
                    self.send_error(404)
                    return
                query = parse_qs(request.query)
                limit_str = (query.get("limit") or ["20"])[0]
                try:
                    limit = int(limit_str)
                    if limit <= 0:
                        raise ValueError
                except ValueError:
                    self.send_error(400, "invalid limit: must be a positive integer")
                    return
                from . import releases
                feed = releases.build_feed(self.led, proj, limit=limit)
                self._json(200, feed)
                return

        if path == "/api/settings":
            try:
                with self.lock:
                    self._json(200, config.settings(self.load_cfg()))
            except Exception:
                self.send_error(500, "settings failed")
                raise
            return

        if path not in ("/", "/fragment", "/api/state"):
            self.send_error(404)
            return
        try:
            with self.lock:
                query = parse_qs(request.query)
                range_key = (query.get("range") or ["week"])[0]
                if range_key == "custom":
                    range_key = ((query.get("start") or [""])[0],
                                 (query.get("end") or [""])[0])
                cookies = SimpleCookie()
                try:
                    cookies.load(self.headers.get("Cookie", ""))
                except Exception:
                    pass
                def preference(key, default=None):
                    return (query.get(key) or [
                        cookies["mahler_" + key].value if "mahler_" + key in cookies else default
                    ])[0]
                selected = dict(layout=preference("layout", "desktop"),
                                view=preference("view"), tab=preference("tab"))
                # Only the rendered section's data is built; /api/state gets it all.
                section = None if path == "/api/state" else (
                    selected["tab"] if selected["layout"] == "phone" else selected["view"]) or ""
                _, s = self._state(range_key, section)
                if path == "/":
                    out, ctype = page.document(s, **selected), "text/html; charset=utf-8"
                elif path == "/fragment":
                    out, ctype = page.app(s, **selected, overlays={
                        key: (query.get(key) or [""])[0]
                        for key in ("run", "revert", "bug", "capture", "release")
                    }), "text/html; charset=utf-8"
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
        
        path = urlsplit(self.path).path
        max_body = 14 * 1024 * 1024 if path == "/api/attach" else MAX_BODY
        if length < 0:
            return 400, "bad Content-Length"
        if length > max_body:
            return 413, "body too large"
        return None

    def do_POST(self):
        self.log_message("start")
        path = urlsplit(self.path).path
        name = path[len("/api/"):] if path.startswith("/api/") else None
        if name == "settings":
            name = "save_settings"
        if name not in actions.ACTIONS:
            self.send_error(404 if path.startswith("/api/") else 405)
            return
        refused = self._refusal()
        if refused:
            self._json(refused[0], {"ok": False, "error": refused[1]})
            return
        if name == "client_log":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                if self.client_log_limiter.allow(self.client_address[0]):
                    actions.ACTIONS[name]({}, self.led, json.loads(raw))
            except Exception:
                # Invalid reports and telemetry failures must not affect the UI.
                pass
            self._send(204, b"", "application/json")
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json(400, {"ok": False, "error": "the body is not valid JSON"})
            return
        wait_started = time.monotonic()
        wait_ms = action_ms = None
        try:
            try:
                with self.lock:
                    wait_ms = (time.monotonic() - wait_started) * 1000
                    action_started = time.monotonic()
                    try:
                        result = actions.run(self.load_cfg(), self.led, name, body)
                    finally:
                        action_ms = (time.monotonic() - action_started) * 1000
            finally:
                # The event is deliberately outside the action lock: a slow
                # or broken telemetry write must not delay other console work.
                self._record_slow_request(path, wait_ms, action_ms)
        except actions.ActionError as e:
            self._json(400, {"ok": False, "error": str(e)})
            return
        except Exception:
            self._json(500, {"ok": False, "error": "the action failed"})
            raise
        self._json(200, {"ok": True, **(result or {})})

    def _record_slow_request(self, path, wait_ms, action_ms):
        """Best-effort telemetry for a POST delayed before or inside its action."""
        try:
            if wait_ms is None or action_ms is None:
                return
            threshold = self.load_cfg()["serve"].get("slow_request_ms", 3000)
            if max(wait_ms, action_ms) <= threshold:
                return
            self.led.event("console_slow_request", detail={
                "path": redact.redact(path),
                "wait_ms": wait_ms,
                "action_ms": action_ms,
            })
        except Exception:
            # Observability must never change the request's result.
            pass

    def _reject(self):
        self.send_error(405, "only GET, and POST to /api/<action>")

    do_PUT = do_DELETE = do_PATCH = do_HEAD = _reject

    def log_message(self, fmt, *args):
        # launchd captures stderr. Never let a broken log sink fail a request.
        line = "serve: request logging failed"
        try:
            elapsed = (time.monotonic() - self._request_started) * 1000
            timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            # Quote client-controlled fields so each event remains one line.
            method = json.dumps(getattr(self, "command", None))
            path = json.dumps(getattr(self, "path", ""))
            message = json.dumps(fmt % args)
            status = self._response_status if self._response_status is not None else "-"
            line = (f"serve: {timestamp} method={method} path={path} "
                    f"status={status} size={self._response_size} "
                    f"duration_ms={elapsed:.3f} event={message}")
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
        except Exception:
            try:
                print(line, file=sys.stderr, flush=True)
            except Exception:
                pass


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
