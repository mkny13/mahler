"""Occasional agent-driven console walkthrough (manually triggered)."""

import os
import subprocess
import threading
from string import Template
from http.server import ThreadingHTTPServer

from . import config, serve, platforms
from tests.test_serve import make_cfg, make_led

def run(cfg, platform_name):
    """Stand up a fixture-seeded console the agent walks through, then launch
    the named platform from the *real* config to drive it."""
    pconf = (cfg.get("platforms") or {}).get(platform_name)
    if not pconf:
        print(f"unknown platform {platform_name}")
        return 1

    # Stand up a fixture-seeded ledger
    fixture_cfg = make_cfg()
    led = make_led()
    
    # Insert fixture data for the agent to interact with
    led.con.execute("INSERT INTO items (project, number, title, state, labels) VALUES ('mahler', 301, 'Walkthrough item', 'ready', 'mahler:working')")
    led.con.execute("INSERT INTO items (project, number, title, state, question, options) VALUES ('mahler', 302, 'Needs you item', 'needs-you', 'What should we do?', '[\"Option A\", \"Option B\"]')")
    led.con.execute("INSERT INTO uat (project, number, pr, sha, title, needs, shipped_at, verdict, verdict_at, bug, note) VALUES ('mahler', 303, 100, 'abcdef', 'Shipped change', 'Check this.', '2026-09-16T00:00:00', NULL, NULL, NULL, NULL)")
    
    led.upsert_item("mahler", 303, state="shipped")
    led.upsert_item("mahler", 304, state="done")
    led.add_uat("mahler", 304, 101, "fedcba", "Completed change", "")
    led.set_uat_verdict("mahler", 304, "pass")

    load = lambda: fixture_cfg
    handler = type("Handler", (serve._Handler,),
                   {"led": led, "lock": threading.Lock(),
                    "load_cfg": staticmethod(load)})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}"
    
    server_thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    server_thread.start()
    
    try:
        print(f"Test console running at {url}")
        
        with open(os.path.join(config.REPO_ROOT, "recipes", "console_walkthrough.md")) as f:
            prompt = Template(f.read()).safe_substitute(url=url)
            
        argv = platforms.argv_for(pconf, prompt, os.getcwd(), "walkthrough", 60)
        # argv_for picks a machine-readable output format so the tick can parse
        # runs; the walkthrough is interactive, so strip those flags and let
        # the agent's normal output through to the terminal.
        if "--output-format" in argv:
            idx = argv.index("--output-format")
            argv[idx:idx+2] = []
        if "--json" in argv:
            argv.remove("--json")
            
        print(f"Launching agent on {platform_name}...\n")
        env = config.run_env(cfg, config.account_of(pconf))
        subprocess.run(argv, env=env)
        return 0
    finally:
        httpd.shutdown()
        httpd.server_close()
        server_thread.join(timeout=5)
