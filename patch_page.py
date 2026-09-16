with open("mahler/console/page.py", "r") as f:
    text = f.read()

import re

# Patch _p_now
text = text.replace('f\'<span class="mono t-mut">{e(r["status"])}</span></span></button>\')',
                    'f\'<span class="mono t-{r["status_tone"]}">{e(r["status"])}</span></span></button>\')')

# Patch _d_now - add status as requested: "Its run cards show a live status on the right"
# Desktop right now has platform and timing. We'll add status next to timing or replace platform.
# But wait, it says "live status on the right". Let's insert it before timing, or as a new span.
# In `_d_now`:
old_drun = '''f'<span class="plat mono t-mut">{e(r["platform"])}</span>'
                   f'<span class="timing mono t-{r["tone"]}">{e(r["timing"])}</span>'
                   f'<span class="bar"><span class="f-{r["tone"]}" style="width:{r["progress"]}%">'
'''
new_drun = '''f'<span class="plat mono t-mut">{e(r["platform"])}</span>'
                   f'<span class="mono t-{r["status_tone"]}" style="margin-right:auto">{e(r["status"])}</span>'
                   f'<span class="timing mono t-{r["tone"]}">{e(r["timing"])}</span>'
                   f'<span class="bar"><span class="f-{r["tone"]}" style="width:{r["progress"]}%">'
'''
# Actually wait, `drun` button uses flex layout probably. The easiest is to just add it.
text = text.replace(old_drun, new_drun)

old_overlays = '''                   f'<span class="bar"><span class="f-{r["tone"]}" style="width:{r["progress"]}%">'
                   f'</span></span><span class="timing mono t-{r["tone"]}">{e(r["timing"])}</span>'
                   f'</div></div><div class="runfoot">'
'''

new_overlays = '''                   f'<span class="bar"><span class="f-{r["tone"]}" style="width:{r["progress"]}%">'
                   f'</span></span><span class="timing mono t-{r["tone"]}">{e(r["timing"])}</span>'
                   f'</div>'
                   f'<div style="margin-top:16px"><span class="lbl" style="margin-bottom:8px;display:block">LIVE LOG</span>'
                   f'<div class="log-panel" data-run-log="{r["id"]}">'
                   f'</div></div>'
                   f'</div><div class="runfoot">'
'''
text = text.replace(old_overlays, new_overlays)

with open("mahler/console/page.py", "w") as f:
    f.write(text)
