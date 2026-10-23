from contextlib import closing
"""Scorecard CLI contract, using only an isolated ledger."""
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
import unittest

from mahler.cli import build_parser
from mahler.ledger import Ledger


class ScorecardTests(unittest.TestCase):
    def test_text_json_and_legacy_raw(self):
        led = Ledger(':memory:', clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.addCleanup(led.close)
        led.upsert_item('p', 1)
        led.set_state('p', 1, 'working')
        led.set_state('p', 1, 'done')
        led.upsert_item('p', 2, state='done')
        def invoke(*flags):
            args = build_parser({}).parse_args(['scorecard', '--project', 'p', '--since', '30d', *flags])
            out = io.StringIO()
            with redirect_stdout(out):
                args.fn(args, {}, led)
            return out.getvalue()
        payload = json.loads(invoke('--json'))
        self.assertEqual(payload['completion']['autonomous'], 1)
        self.assertEqual(payload['completion']['unknown'], 1)
        self.assertEqual(payload['completion']['percentage'], 100)
        self.assertEqual(payload['attempts'], json.loads(invoke('--raw')))
        text = invoke()
        self.assertIn('1/1 (100.0%)', text)
        self.assertIn('1 autonomous · 0 human-assisted · 1 unknown', text)
        self.assertIn(payload['completion']['limitation'], text)
        self.assertIn('No attempts in this window.', text)


class UnstickTests(unittest.TestCase):
    def test_recognized_waits_only_and_blocker_is_respected(self):
        with closing(Ledger(":memory:")) as led:
            led.upsert_item("p", 1, state="needs_you", pr=10,
                question="PR #10: waiting for failed-job rerun attempt after 60 min")
            led.upsert_item("p", 2, state="needs_you", question="Which account?")
            led.upsert_item("p", 3, state="parked", depends="[4]")
            led.upsert_item("p", 4, state="ready")
            led.set_kv("blocked-deps:p#3", "[4]")
            led.set_kv("ci_blocker:p", '{"number": 4}')
            args = build_parser({}).parse_args(["unstick", "p"])
            cfg = {"projects": {"p": {"repo": "p/r", "path": "/unused"}}}
            with redirect_stdout(io.StringIO()):
                args.fn(args, cfg, led)
            self.assertEqual(led.item("p", 1)["state"], "verifying")
            self.assertEqual(led.item("p", 2)["state"], "needs_you")
            self.assertEqual(led.item("p", 3)["state"], "parked")
            led.set_state("p", 4, "done")
            with redirect_stdout(io.StringIO()):
                args.fn(args, cfg, led)
            self.assertEqual(led.item("p", 3)["state"], "ready")
