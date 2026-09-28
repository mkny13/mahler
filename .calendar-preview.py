import json
from datetime import datetime, timezone
from pathlib import Path
from mahler.console import state, page
from mahler.ledger import Ledger
s=json.loads(Path('.calendar-live-state.json').read_text())
cfg={'platforms':{}, 'routing':{'build':[]}}
led=Ledger(':memory:')
try:
    for q in s['quota']:
        name=q['name']
        cfg['platforms'][name]={'metered':q['metered'], 'account':'work' if name.startswith('work-') else 'personal', 'windows':[w['window'] for w in q['windows']] or ['weekly']}
        cfg['routing']['build'].append(name)
        for w in q['windows']:
            led.record_usage(name,w['window'],w['pct'],w.get('resets'))
    s['weekly_quota']=state.week_calendar(cfg, led, datetime.now(timezone.utc))
finally:
    led.close()
for layout in ('desktop','phone'):
    doc=page.document(s,layout=layout,view='capacity',tab='browse')
    # Static preview cannot poll the live daemon or send console actions.
    import re
    doc=re.sub(r'<script>.*?</script>', '', doc, flags=re.S)
    doc=doc.replace('<html ', '<html data-capacity-mode="weekly" ')
    Path(f'.calendar-{layout}.html').write_text(doc)
