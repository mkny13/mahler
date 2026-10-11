"""Local fake Anthropic API: logs request metadata, returns a canned SSE reply."""
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = sys.argv[2] if len(sys.argv) > 2 else "wire.log"

def sse(ev, data):
    return f"event: {ev}\ndata: {json.dumps(data)}\n\n".encode()

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _rec(self, body):
        h = {k: v for k, v in self.headers.items()}
        if "x-api-key" in {k.lower() for k in h}:
            for k in list(h):
                if k.lower() == "x-api-key": h[k] = h[k][:6] + "..."
        if "authorization" in {k.lower() for k in h}:
            for k in list(h):
                if k.lower() == "authorization": h[k] = h[k][:13] + "..."
        try: b = json.loads(body) if body else {}
        except Exception: b = {}
        rec = {"method": self.command, "path": self.path,
               "header_names": list(h), "anthropic-beta": h.get("anthropic-beta"),
               "anthropic-version": h.get("anthropic-version"),
               "user-agent": h.get("User-Agent"), "x-api-key": h.get("x-api-key"),
               "model": b.get("model"), "max_tokens": b.get("max_tokens"),
               "stream": b.get("stream"), "content_length": self.headers.get("content-length"),
               "n_tools": len(b.get("tools") or []), "system_chars": len(json.dumps(b.get("system"))), "body_keys": list(b)}
        open(LOG, "a").write(json.dumps(rec) + "\n")
    def do_GET(self):
        self._rec(b"")
        out = b'{"data":[],"has_more":false}'
        self.send_response(200); self.send_header("content-type","application/json")
        self.send_header("content-length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        self._rec(body)
        try: model = json.loads(body).get("model", "m")
        except Exception: model = "m"
        if "count_tokens" in self.path:
            out = b'{"input_tokens":10}'
            self.send_response(200); self.send_header("content-type","application/json")
            self.send_header("content-length", str(len(out))); self.end_headers(); self.wfile.write(out); return
        ev = [
          sse("message_start", {"type":"message_start","message":{"id":"msg_x","type":"message","role":"assistant","model":model,"content":[],"stop_reason":None,"stop_sequence":None,"usage":{"input_tokens":10,"output_tokens":1}}}),
          sse("content_block_start", {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}),
          sse("content_block_delta", {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"PONG"}}),
          sse("content_block_stop", {"type":"content_block_stop","index":0}),
          sse("message_delta", {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":None},"usage":{"output_tokens":2}}),
          sse("message_stop", {"type":"message_stop"}),
        ]
        data = b"".join(ev)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream"); self.send_header("content-length", str(len(data)))
        self.end_headers(); self.wfile.write(data)

ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
