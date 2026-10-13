#!/usr/bin/env python3
import json
import subprocess
import os

def main():
    if os.environ.get("MAHLER_RUN_ID"):
        return
    my_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")[:8] or "you"
    my_holder = f"interactive:{my_id}"
    try:
        out = subprocess.check_output(["mahler", "status", "--project", "mahler", "--json"], text=True)
        d = json.loads(out)
        for l in d.get("leases", []):
            if (l.get("holder") == my_holder
                    or l.get("holder", "").endswith("/" + my_holder)):
                # Renew only this session's own claim(s) in the background
                subprocess.Popen(
                    ["mahler", "heartbeat", f"{l['project']}#{l['number']}", "--as", my_id],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True
                )
    except Exception:
        pass

if __name__ == "__main__":
    main()
