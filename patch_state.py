with open("mahler/console/state.py", "r") as f:
    text = f.read()

import re

old = '''
        status = (f"stopping · {r['stop_reason']}" if r["status"] == "stopping" and r["stop_reason"]
                  else "stopping" if r["status"] == "stopping" or r["id"] in stop_queued
                  else ROLE_WORDS.get(r["role"], r["role"]))
'''

new = '''
        from . import logtail
        ls = logtail.live_status(dict(r))
        status = (f"stopping · {r['stop_reason']}" if r["status"] == "stopping" and r["stop_reason"]
                  else "stopping" if r["status"] == "stopping" or r["id"] in stop_queued
                  else ls["text"])
        status_tone = ls["tone"] if r["status"] not in ("stopping",) and r["id"] not in stop_queued else "mut"
'''

text = text.replace(old, new)
text = text.replace('"status": status, "meta":', '"status": status, "status_tone": status_tone, "meta":')

with open("mahler/console/state.py", "w") as f:
    f.write(text)
