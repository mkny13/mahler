import json
import os
import sys
from datetime import datetime

from . import config
from .gh import GH
from .ledger import Ledger, parse

def serve(cfg, led):
    def send(msg):
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
            
        if req.get("method") == "initialize":
            send({
                "jsonrpc": "2.0",
                "id": req.get("id"),
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mahler-mcp", "version": "1.0.0"}
                }
            })
        elif req.get("method") == "tools/list":
            send({
                "jsonrpc": "2.0",
                "id": req.get("id"),
                "result": {
                    "tools": [
                        {
                            "name": "list_items",
                            "description": "List all active items in a project",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "project": {"type": "string", "description": "Project name (e.g. mahler)"}
                                },
                                "required": ["project"]
                            }
                        },
                        {
                            "name": "add_item",
                            "description": "Add a new item to the queue",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "project": {"type": "string"},
                                    "title": {"type": "string"},
                                    "body": {"type": "string"},
                                    "claim": {"type": "boolean", "description": "Whether to also claim this item immediately"}
                                },
                                "required": ["project", "title"]
                            }
                        },
                        {
                            "name": "claim",
                            "description": "Claim an item for work",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "project": {"type": "string"},
                                    "number": {"type": "integer"},
                                    "holder": {"type": "string", "description": "Name of the agent claiming it (e.g. claude, agy)"},
                                    "steal": {"type": "boolean"}
                                },
                                "required": ["project", "number", "holder"]
                            }
                        },
                        {
                            "name": "heartbeat",
                            "description": "Renew a claim on an item",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "project": {"type": "string"},
                                    "number": {"type": "integer"},
                                    "holder": {"type": "string"}
                                },
                                "required": ["project", "number", "holder"]
                            }
                        },
                        {
                            "name": "release",
                            "description": "Release a claimed item",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "project": {"type": "string"},
                                    "number": {"type": "integer"},
                                    "holder": {"type": "string"}
                                },
                                "required": ["project", "number", "holder"]
                            }
                        },
                        {
                            "name": "handoff",
                            "description": "Hand off an item by posting a handoff comment",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "project": {"type": "string"},
                                    "number": {"type": "integer"},
                                    "comment": {"type": "string", "description": "The handoff notes"}
                                },
                                "required": ["project", "number", "comment"]
                            }
                        },
                        {
                            "name": "next_id",
                            "description": "Get the next counter ID for a project",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "project": {"type": "string"},
                                    "name": {"type": "string", "description": "Counter name"},
                                    "floor": {"type": "integer"}
                                },
                                "required": ["project", "name"]
                            }
                        }
                    ]
                }
            })
        elif req.get("method") == "tools/call":
            params = req.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})
            
            try:
                if name == "list_items":
                    items = [dict(i) for i in led.items(project=args["project"]) if i["state"] != "done"]
                    res = json.dumps(items, default=str)
                    content = [{"type": "text", "text": res}]
                elif name == "add_item":
                    pol = config.project_policy(cfg, args["project"])
                    url = GH(pol["repo"]).create_issue(args["title"], args.get("body", ""))
                    # extract issue number from url, typically https://github.com/org/repo/issues/123
                    num_str = url.split("/")[-1]
                    try:
                        n = int(num_str)
                    except ValueError:
                        n = 0
                    msg = f"Created issue {url}"
                    if args.get("claim") and n > 0:
                        holder = "interactive:mcp"
                        lease, info = led.claim(args["project"], n, holder, "interactive", pol["interactive_lease_minutes"])
                        if lease:
                            led.set_state(args["project"], n, "working", f"claimed by {holder}")
                            msg += f"\nClaimed as {holder}"
                        else:
                            msg += f"\nFailed to claim: {info}"
                    content = [{"type": "text", "text": msg}]
                elif name == "claim":
                    pol = config.project_policy(cfg, args["project"])
                    holder = f"interactive:{args['holder']}"
                    lease, info = led.claim(args["project"], args["number"], holder, "interactive",
                                            pol["interactive_lease_minutes"], steal=args.get("steal", False))
                    if lease is None:
                        h = info.get("held_by", {})
                        content = [{"type": "text", "text": f"Failed to claim. Held by {h.get('holder')} since {h.get('heartbeat_at')}"}]
                    else:
                        led.set_state(args["project"], args["number"], "working", f"claimed by {holder}")
                        content = [{"type": "text", "text": f"Claimed successfully. Epoch {lease['epoch']}."}]
                elif name == "heartbeat":
                    pol = config.project_policy(cfg, args["project"])
                    holder = f"interactive:{args['holder']}"
                    cur = led.lease(args["project"], args["number"])
                    if cur and led.heartbeat(args["project"], args["number"], holder, cur["epoch"], pol["interactive_lease_minutes"]):
                        content = [{"type": "text", "text": "Heartbeat successful."}]
                    else:
                        content = [{"type": "text", "text": "Failed to heartbeat. Lease lost or not held.", "isError": True}]
                elif name == "release":
                    holder = f"interactive:{args['holder']}"
                    ok = led.release(args["project"], args["number"], holder=holder)
                    item = led.item(args["project"], args["number"])
                    if ok and item and item["state"] == "working":
                        led.set_state(args["project"], args["number"], "ready", f"released by {holder}")
                    content = [{"type": "text", "text": "Released successfully." if ok else "Failed to release. Not held."}]
                elif name == "handoff":
                    pol = config.project_policy(cfg, args["project"])
                    GH(pol["repo"]).comment(args["number"], f"<!-- mahler:agent handoff -->\n{args['comment']}")
                    content = [{"type": "text", "text": "Handoff comment posted."}]
                elif name == "next_id":
                    val = led.next_id(args["project"], args["name"], floor=args.get("floor", 0))
                    content = [{"type": "text", "text": str(val)}]
                else:
                    content = [{"type": "text", "text": f"Unknown tool: {name}", "isError": True}]
                    
                send({
                    "jsonrpc": "2.0",
                    "id": req.get("id"),
                    "result": {"content": content}
                })
            except Exception as e:
                send({
                    "jsonrpc": "2.0",
                    "id": req.get("id"),
                    "error": {"code": -32000, "message": str(e)}
                })
        elif req.get("method") == "notifications/initialized":
            pass
