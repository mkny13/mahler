"""Scorecard CLI contract, using only an isolated ledger."""
from contextlib import redirect_stdout
from datetime import datetime, timezone
import copy
import io
import json
import unittest

from mahler import config
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


class PrioritizeCliTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)
        self.led = Ledger(":memory:", clock=lambda: self.now)
        self.addCleanup(self.led.close)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["projects"] = {
            "groundwork": {"repo": "owner/groundwork", "enabled": True},
            "other": {"repo": "owner/other", "enabled": True},
            "disabled_proj": {"repo": "owner/disabled", "enabled": False},
        }

    def invoke(self, *args):
        parser = build_parser(self.cfg)
        out = io.StringIO()
        try:
            parsed = parser.parse_args(["prioritize", *args])
        except SystemExit as exc:
            return exc.code, "", ""
        with redirect_stdout(out):
            rc = parsed.fn(parsed, self.cfg, self.led)
        return rc, out.getvalue(), ""

    def test_prioritize_start_reports_absolute_expiry(self):
        rc, out, _ = self.invoke("groundwork", "1h")
        self.assertEqual(rc, 0)
        boost = self.led.priority_boost()
        self.assertIsNotNone(boost)
        self.assertEqual(boost["project"], "groundwork")
        self.assertEqual(boost["expires_at"], "2026-10-21T13:00:00+00:00")
        self.assertIn("groundwork", out)
        self.assertIn(boost["expires_at"], out)

    def test_prioritize_durations(self):
        for dur, expected_expiry in [("1d", "2026-10-22T12:00:00+00:00"),
                                     ("1w", "2026-10-28T12:00:00+00:00")]:
            with self.subTest(duration=dur):
                rc, out, _ = self.invoke("groundwork", dur)
                self.assertEqual(rc, 0)
                boost = self.led.priority_boost()
                self.assertEqual(boost["expires_at"], expected_expiry)
                self.assertIn(expected_expiry, out)

    def test_prioritize_off_stops_matching_boost(self):
        self.led.start_priority_boost("groundwork", "1h")
        rc, out, _ = self.invoke("groundwork", "off")
        self.assertEqual(rc, 0)
        self.assertIsNone(self.led.priority_boost())
        self.assertIn("groundwork", out)
        self.assertTrue("stopped" in out.lower() or "off" in out.lower())

    def test_prioritize_off_when_no_matching_boost(self):
        # Case 1: no boost active
        rc, out, _ = self.invoke("groundwork", "off")
        self.assertNotEqual(rc, 0)
        self.assertIn("no matching boost", out.lower())

        # Case 2: boost active for different project
        self.led.start_priority_boost("other", "1h")
        rc, out, _ = self.invoke("groundwork", "off")
        self.assertNotEqual(rc, 0)
        self.assertIn("no matching boost", out.lower())
        # Other project's boost is still intact
        self.assertEqual(self.led.priority_boost()["project"], "other")

    def test_prioritize_rejects_unknown_or_disabled_project(self):
        rc, out, _ = self.invoke("unknown_proj", "1h")
        self.assertNotEqual(rc, 0)
        self.assertTrue("unknown" in out.lower() or "disabled" in out.lower())

        rc, out, _ = self.invoke("disabled_proj", "1h")
        self.assertNotEqual(rc, 0)
        self.assertTrue("unknown" in out.lower() or "disabled" in out.lower())

    def test_prioritize_rejects_invalid_duration(self):
        rc, out, _ = self.invoke("groundwork", "2h")
        self.assertNotEqual(rc, 0)

