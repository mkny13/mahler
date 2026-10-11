import json
import sys

from . import config
from .gh import project_client

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
                    url = project_client(cfg, pol).create_issue(args["title"], args.get("body", ""))
                    # extract issue number from url, typically https://github.com/org/repo/issues/123
                    num_str = url.split("/")[-1]
                    try:
                        n = int(num_str)
                    except ValueError:
                        content = [{"type": "text",
                                    "text": f"Created issue {url}, but could not read its number "
                                            f"from {num_str!r}; not claiming.",
                                    "isError": True}]
                        send({
                            "jsonrpc": "2.0",
                            "id": req.get("id"),
                            "result": {"content": content}
                        })
                        continue
                    msg = f"Created issue {url}"
                    if args.get("claim"):
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
                    ok = led.release(args["project"], args["number"], holder=holder,
                                     to_state="ready", why=f"released by {holder}")
                    content = [{"type": "text", "text": "Released successfully." if ok else "Failed to release. Not held."}]
                elif name == "handoff":
                    pol = config.project_policy(cfg, args["project"])
                    project_client(cfg, pol).comment(args["number"], f"<!-- mahler:agent handoff -->\n{args['comment']}")
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


def serve_discussion(cfg, led, session_id, turn_id, worker_token):
    """Restricted stdio entry point, called with the trusted worker's binding.

    Deliberately separate from serve(): no fallback to the unrestricted dispatcher.
    The execution adapter owns process setup and native-tool disabling (#924).
    """
    from .discuss_tools import DiscussionTools, tool_definitions

    discussion = DiscussionTools(cfg, led, session_id, turn_id, worker_token)
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(req, dict) or 'id' not in req:
            continue
        response = {'jsonrpc': '2.0', 'id': req['id']}
        try:
            method = req.get('method')
            if method == 'initialize':
                result = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},
                          'serverInfo': {'name': 'mahler-discussion-mcp', 'version': '1.0.0'}}
            elif method == 'tools/list':
                result = {'tools': tool_definitions()}
            elif method == 'tools/call':
                params = req.get('params', {})
                value = discussion.call(params.get('name'), params.get('arguments', {}))
                result = {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}]}
            else:
                response['error'] = {'code': -32601, 'message': 'Method not found'}
                result = None
            if result is not None:
                response['result'] = result
        except Exception as exc:
            from .redact import redact
            response['result'] = {'isError': True, 'content': [
                {'type': 'text', 'text': redact(str(exc))}]}
        sys.stdout.write(json.dumps(response) + '\n')
        sys.stdout.flush()
