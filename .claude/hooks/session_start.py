#!/usr/bin/env python3
import json
import os
import subprocess
import sys

def main():
    my_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")[:8]
    try:
        out = subprocess.check_output(["mahler", "status", "--project", "mahler", "--json"], text=True)
        d = json.loads(out)
        held = []
        for r in d.get('runs', []):
            held.append(f'#{r["number"]} ({r["role"]} on {r["platform"]})')
        if held:
            print('Items currently held by autonomous runs in this project:')
            for h in held:
                print(f'  - {h}')
            print('')
        others = sorted({l['holder'] for l in d.get('leases', [])
                          if ('/interactive:' in l.get('holder', '')
                              or l.get('holder', '').startswith('interactive:'))
                          and not l.get('holder', '').endswith(f'interactive:{my_id}')})
        if others:
            print(f'Another interactive session ({", ".join(others)}) is active in this '
                  'project right now. Work in your own worktree, not by switching branches '
                  'in the shared checkout (see CLAUDE.md).')
        print('If you work on a backlog item, claim it with `mahler claim mahler#N`.')
        print('When it is pushed, finish with `mahler ship mahler#N [--pr X]` (the conductor '
              'merges on green) or merge it yourself. Nothing picks up a PR you leave open.')
    except Exception:
        pass

if __name__ == "__main__":
    main()
