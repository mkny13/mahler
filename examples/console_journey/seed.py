#!/usr/bin/env python3
"""Fixture console for the #1119 browser journey (mahler#1257).

Writes a config.toml and a ledger into an isolated MAHLER_HOME, seeds one
project with every Overview section, and serves the console on loopback.
Standard library only. It refuses to run unless MAHLER_HOME already points at
the directory it was given, so it can never touch the real ~/.mahler.

    MAHLER_HOME=/tmp/x python3 seed.py --home /tmp/x [--port N]

Prints `CONSOLE_URL=http://127.0.0.1:<port>/` once the database is seeded.
"""

import argparse
import json
import os
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEEDED = "groundwork"
OTHER = "mahler"

CONFIG = """\
[concurrency]
total = 8

[projects.groundwork]
enabled = true
repo = "mkny13/groundwork"
path = "/nonexistent/groundwork"
max_parallel = 2
verify = "pytest · ruff"

[projects.mahler]
enabled = true
repo = "mkny13/mahler"
path = "/nonexistent/mahler"
max_parallel = 1
"""


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def seed(home):
    """Create config.toml and mahler.db under `home`; return (cfg, ledger)."""
    from mahler import config
    from mahler.ledger import Ledger, RoutedLedger

    home = Path(home).resolve()
    if Path(config.STATE).resolve() != home:
        raise SystemExit(f"refusing to seed {home}: MAHLER_HOME is {config.STATE}")
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(CONFIG)
    cfg = config.load(home / "config.toml")
    led = Ledger(str(home / "mahler.db"), thread_safe=True)
    now = datetime.now(timezone.utc)
    p = SEEDED

    # running
    led.upsert_item(p, 1, title="Retry batch", state="working")
    led.create_run(project=p, number=1, role="build", platform="copilot", epoch=1,
                   status="running", est_mins=25, started_at=_iso(now - timedelta(minutes=14)))
    # an open owner question
    led.upsert_item(p, 2, title="Drive search", state="needs_you",
                    question="Drive scope: drive.readonly or drive.file?")
    led.set_state(p, 2, "needs_you", "Drive scope: drive.readonly or drive.file?")
    led.con.execute("UPDATE items SET state_changed_at=? WHERE project=? AND number=2",
                    (_iso(now - timedelta(hours=2, minutes=10)), p))
    led.con.commit()
    # queued, and blocked (dependency, parked)
    led.upsert_item(p, 33, title="Calendar shared", state="ready", labels=json.dumps(["size:s"]))
    led.upsert_item(p, 36, title="Gmail attachments", state="ready", labels=json.dumps(["size:s"]))
    led.upsert_item(p, 34, title="Drive export", state="ready", depends=json.dumps([2]))
    led.upsert_item(p, 26, title="Chat paginate", state="working")
    led.set_state(p, 26, "parked", "CI red twice")
    # shipping rows: built/no PR, CI pending, CI red, reviewing, quiet, verified
    led.upsert_item(p, 10, title="Built no PR", state="verifying", pr=None)
    led.upsert_item(p, 20, title="Token cache", state="verifying", pr=43)
    led.set_kv("ci:groundwork#20:43", json.dumps(
        {"state": "pending", "progress": "3 of 5", "since": _iso(now - timedelta(minutes=4))}))
    led.upsert_item(p, 26, pr=38)
    led.set_kv("ci:groundwork#26:38", json.dumps({"state": "red", "failed_check": "test_chat"}))
    led.upsert_item(p, 32, title="Calendar events", state="verifying", pr=44,
                    labels=json.dumps(["size:m"]))
    led.set_kv("ci:groundwork#32:44", json.dumps({"state": "green"}))
    led.set_kv("review:groundwork#32", json.dumps(
        {"verdict": "pending", "platform": "agy-gemini", "pr": 44}))
    led.add_uat(p, 27, 42, "sha27", "Tasks reopen", "- verify",
                shipped_at=_iso(now - timedelta(days=1)))
    led.upsert_item(p, 30, title="Gmail search", state="shipped")
    led.add_uat(p, 30, 41, "sha30", "Gmail search", "- verify",
                shipped_at=_iso(now - timedelta(hours=3)))
    led.accept_evidence(p, 30, {"source": "https://example.com/check/30", "author": "ci",
                                "created_at": _iso(now - timedelta(hours=3)),
                                "kind": "smoke", "body": "Smoke passed"})
    # release, completed work, activity, backlog trend
    led.create_release(p, "2.4.1", checkpoint_sha="abc123", item_numbers=[27, 30])
    led.snapshot_release_item(p, 20, pr=43, title="Token cache", merge_sha="sha20",
                              labels=["type:feature"])
    led.event("shipped", p, 20, detail={"pr": 43}, at=_iso(now - timedelta(hours=1)))
    led.record_backlog(p, 12, at=now - timedelta(days=7))
    led.record_backlog(p, 9, at=now)
    # the other project: none of this may show under the seeded scope
    led.upsert_item(OTHER, 999, title="Mahler item should not leak", state="ready")
    led.upsert_item(OTHER, 998, title="Mahler question should not leak", state="needs_you",
                    question="Other project question?")
    led.set_state(OTHER, 998, "needs_you", "Other project question?")
    led.event("shipped", OTHER, 999, detail={"pr": 7}, at=_iso(now - timedelta(hours=2)))
    return cfg, RoutedLedger(led, cfg)


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--home", required=True, help="isolated MAHLER_HOME directory")
    ap.add_argument("--port", type=int, default=0)
    a = ap.parse_args(argv)
    sys.path.insert(0, str(ROOT))
    cfg, led = seed(a.home)
    port = a.port or _free_port()
    from mahler import serve
    print(f"CONSOLE_URL=http://127.0.0.1:{port}/", flush=True)
    try:
        return serve.serve(cfg, led, host="127.0.0.1", port=port)
    finally:
        led.close()


if __name__ == "__main__":
    sys.exit(main())
