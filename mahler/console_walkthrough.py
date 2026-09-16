"""Occasional agent-driven console walkthrough (manually triggered)."""

import os
import subprocess
import threading
from string import Template
from http.server import ThreadingHTTPServer

from . import config, serve, platforms
from tests.test_serve import make_cfg, make_led

def run(cfg, platform_name):
    # Stand up a fixture-seeded ledger
    cfg = make_cfg()
    led = make_led()
    
    # Insert fixture data for the agent to interact with
    led.con.execute("INSERT INTO items (project, number, title, state, labels) VALUES ('mahler', 301, 'Walkthrough item', 'ready', 'mahler:working')")
    led.con.execute("INSERT INTO items (project, number, title, state, question, options) VALUES ('mahler', 302, 'Needs you item', 'needs-you', 'What should we do?', '[\"Option A\", \"Option B\"]')")
    led.con.execute("INSERT INTO uat (project, number, pr, sha, title, needs, shipped_at, verdict, verdict_at, bug, note) VALUES ('mahler', 303, 100, 'abcdef', 'UAT item', 'Check this.', '2026-09-16T00:00:00', NULL, NULL, NULL, NULL)")
    
    load = lambda: cfg
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
            
        pconf = cfg["platforms"].get(platform_name)
        if not pconf:
            print(f"unknown platform {platform_name}")
            return 1
            
        argv = platforms.argv_for(pconf, prompt, os.getcwd(), "walkthrough", 60)
        # agy_argv uses stream-json if kind is agy. For manual walkthrough, we might want to just run the agent interactively or without --output-format stream-json so we can read it?
        # Actually argv_for forces json formats. That's fine, we can just run it and let the user see the raw output, or we can strip the JSON flags.
        # But wait, argv_for might not have a way to disable json. Let's just run it as-is or override the format flag.
        if "--output-format" in argv:
            idx = argv.index("--output-format")
            argv[idx:idx+2] = [] # remove the flag and its value to let it be standard output
        if "--json" in argv:
            argv.remove("--json")
            
        print(f"Launching agent on {platform_name}...\n")
        subprocess.run(argv)
        return 0
    finally:
        httpd.shutdown()
        httpd.server_close()
        server_thread.join(timeout=5)
