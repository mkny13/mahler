import json
import os
import re
import time
from typing import Dict, List, Any

from mahler import config
from mahler import redact

def tail(run: Dict[str, Any], max_bytes: int = 65536, lines: int = 40) -> List[Dict[str, str]]:
    log_path = run.get("log_path")
    if not log_path:
        return []
    
    try:
        real_state = os.path.realpath(config.STATE)
        real_path = os.path.realpath(log_path)
        if not real_path.startswith(real_state) or not os.path.isfile(real_path):
            return []
    except Exception:
        return []

    try:
        with open(real_path, "rb") as f:
            f.seek(0, 2)
            file_size = f.tell()
            read_size = min(max_bytes, file_size)
            f.seek(file_size - read_size, 0)
            data = f.read(read_size)
        
        decoded = data.decode("utf-8", errors="replace").splitlines()
        if file_size > max_bytes and len(decoded) > 0:
            decoded = decoded[1:]
        
        decoded = decoded[-lines:]
    except Exception:
        return []

    cfg = config.load()
    platform = run.get("platform", "")
    pcfg = cfg.get("platforms", {}).get(platform, {})
    kind = pcfg.get("kind", "stream-json")

    results = []
    for line in reversed(decoded):
        redacted_line = line
        parsed = _parse_line(redacted_line, kind)
        if parsed:
            parsed["text"] = redact.redact(parsed["text"])
            results.append(parsed)

    return results

def _extract_time(obj: Dict) -> str:
    ts = None
    if "timestamp" in obj:
        ts = obj["timestamp"]
    elif "ts" in obj:
        ts = obj["ts"]
    elif "time" in obj and isinstance(obj["time"], dict) and "start" in obj["time"]:
        ts = obj["time"]["start"]
    elif "time" in obj and isinstance(obj["time"], dict) and "end" in obj["time"]:
        ts = obj["time"]["end"]
        
    if ts:
        try:
            if isinstance(ts, (int, float)):
                if ts > 2e9:
                    ts /= 1000
                from datetime import datetime
                dt = datetime.fromtimestamp(ts)
                return dt.strftime("%H:%M:%S")
            elif isinstance(ts, str):
                m = re.search(r'T(\d{2}:\d{2}:\d{2})', ts)
                if m:
                    return m.group(1)
        except Exception:
            pass
    return ""

def _truncate_cmd(cmd: str) -> str:
    cmd = str(cmd)
    if len(cmd) > 80:
        return cmd[:80]
    return cmd

def _parse_line(line: str, kind: str) -> Dict[str, str]:
    mut = lambda t, txt: {"t": t, "text": txt, "tone": "mut"}
    
    try:
        obj = json.loads(line)
    except Exception:
        return mut("", line)

    t = _extract_time(obj)

    if kind in ("claude", "agy"):
        if obj.get("type") == "assistant" and "message" in obj and "content" in obj["message"]:
            for item in obj["message"]["content"]:
                if item.get("type") == "tool_use":
                    name = item.get("name", "Tool")
                    cmd = str(item.get("input", {}))
                    if name == "Bash" and "command" in item.get("input", {}):
                        cmd = item["input"]["command"]
                    return {"t": t, "text": f"tool {name} {_truncate_cmd(cmd)}", "tone": "mut"}
                elif item.get("type") == "text":
                    first_line = str(item.get("text", "")).splitlines()[0] if item.get("text") else ""
                    return {"t": t, "text": first_line, "tone": "ink"}
        
        if obj.get("event") == "step_update" and "step_update" in obj:
            step = obj["step_update"]
            if step.get("step_type") == "tool" and step.get("state") == "ACTIVE":
                name = step.get("tool_name", "Tool")
                cmd = str(step.get("tool_info", {}).get("parameters", {}))
                params = step.get("tool_info", {}).get("parameters", {})
                if "CommandLine" in params:
                    cmd = params["CommandLine"]
                elif "command" in params:
                    cmd = params["command"]
                return {"t": t, "text": f"tool {name} {_truncate_cmd(cmd)}", "tone": "mut"}
                
        if obj.get("type") == "result" or obj.get("event") == "result":
            is_err = obj.get("is_error", False)
            res_obj = obj.get("result", {})
            if isinstance(res_obj, dict) and res_obj.get("status") not in ("SUCCESS", None):
                is_err = True
            
            res = obj.get("result", "")
            if isinstance(res, dict) and "response" in res:
                res = res["response"]
            
            snippet = str(res).splitlines()[0] if res else ""
            if len(snippet) > 80: snippet = snippet[:80]
            
            tone = "warn" if is_err else "good"
            return {"t": t, "text": f"result: {snippet}", "tone": tone}
            
        if obj.get("type") == "assistant.message_delta" and "data" in obj and "deltaContent" in obj["data"]:
            first_line = str(obj["data"]["deltaContent"]).splitlines()[0] if obj["data"]["deltaContent"] else ""
            return {"t": t, "text": first_line, "tone": "ink"}
            
        return mut(t, line)

    else:
        evt = obj.get("event", {})
        if not evt and isinstance(obj, dict): evt = obj
        
        if (isinstance(evt, dict) and evt.get("type") in ("tool_use", "content_start")) or obj.get("type") == "tool_use":
            part = obj.get("part", {})
            contentType = evt.get("contentType", part.get("type"))
            if contentType == "tool" or "toolName" in evt or "tool" in evt or "tool" in part:
                name = evt.get("toolName", evt.get("tool", part.get("tool", "Tool")))
                input_data = evt.get("input", part.get("state", {}).get("input", {}))
                
                cmd = str(input_data)
                if isinstance(input_data, dict):
                    if "commands" in input_data and input_data["commands"]:
                        cmd = input_data["commands"][0]
                    elif "command" in input_data:
                        cmd = input_data["command"]
                    elif "filePath" in input_data:
                        cmd = input_data["filePath"]
                
                return {"t": t, "text": f"tool {name} {_truncate_cmd(cmd)}", "tone": "mut"}
            elif contentType in ("text", "reasoning"):
                text_val = evt.get("text", evt.get("reasoning", ""))
                first_line = str(text_val).splitlines()[0] if text_val else ""
                return {"t": t, "text": first_line, "tone": "ink"}
                
        if isinstance(evt, dict) and evt.get("type") == "content_end" and evt.get("contentType") == "tool":
            outputs = evt.get("output", [])
            is_err = False
            snippet = ""
            if outputs and isinstance(outputs, list):
                is_err = not outputs[-1].get("success", True)
                snippet = str(outputs[-1].get("result", outputs[-1].get("output", "")))
            
            snippet = snippet.splitlines()[0] if snippet else "done"
            if len(snippet) > 80: snippet = snippet[:80]
            
            tone = "warn" if is_err else "good"
            return {"t": t, "text": f"result: {snippet}", "tone": tone}
            
        return mut(t, line)

def live_status(run: Dict[str, Any]) -> Dict[str, str]:
    log_path = run.get("log_path")
    if not log_path:
        return {"text": "starting", "tone": "mut"}
        
    try:
        real_state = os.path.realpath(config.STATE)
        real_path = os.path.realpath(log_path)
        if not real_path.startswith(real_state) or not os.path.isfile(real_path):
            return {"text": "starting", "tone": "mut"}
            
        st = os.stat(real_path)
        mtime = st.st_mtime
        
        now = time.time()
        diff = now - mtime
        if diff >= 300:
            minutes = int(diff / 60)
            return {"text": f"no output {minutes}m", "tone": "warn"}
    except Exception:
        pass
        
    lines = tail(run, max_bytes=65536, lines=200)
    for line in lines:
        if line["text"].startswith("tool "):
            parts = line["text"].split(" ", 2)
            if len(parts) >= 3:
                name = parts[1]
                cmd = parts[2]
                if cmd.startswith("git push"):
                    return {"text": "pushing commits", "tone": "mut"}
                return {"text": f"tool {name}", "tone": "mut"}
            elif len(parts) == 2:
                return {"text": f"tool {parts[1]}", "tone": "mut"}
            
    return {"text": "running", "tone": "mut"}
