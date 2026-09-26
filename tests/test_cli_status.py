"""Tests for mahler status output formatting (including item_url on sqlite3.Row)."""

import io
import os
import tempfile
import unittest
from unittest.mock import patch

from mahler import cli
from mahler.ledger import Ledger


class StatusCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "mahler.db")
        self.led = Ledger(self.db_path)
        self.addCleanup(self.led.close)
        self.cfg = {
            "defaults": {},
            "platforms": {},
            "projects": {
                "proj": {
                    "enabled": True,
                    "repo": "owner/proj",
                },
            },
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_status_item_urls_with_sqlite_row(self):
        # Insert an item without PR
        self.led.upsert_item("proj", 1, title="Test Issue 1", state="ready")
        # Insert an item with PR
        self.led.upsert_item("proj", 2, title="Test Issue 2", state="working", pr=123)

        # Start a run
        run_id = self.led.create_run(
            project="proj", number=2, role="build", platform="platform1",
            epoch=1, worktree="/tmp/wt", branch="b1",
        )
        self.led.claim("proj", 2, f"run:{run_id}", "auto", 10, run_id=run_id, platform="platform1")

        # Record an event
        self.led.event("state", "proj", 2, "ready -> working")

        class Args:
            json = False
            project = None

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            ret = cli.cmd_status(Args(), self.cfg, self.led)

        self.assertEqual(ret, 0)
        output = buf.getvalue()
        self.assertIn(f"proj#2 — platform1 build run {run_id}, 0m", output)
        self.assertIn("https://github.com/owner/proj/issues/1", output)
        self.assertIn("https://github.com/owner/proj/pull/123", output)

    def test_interactive_claim_ignores_watch_lease_but_names_running_holder(self):
        from types import SimpleNamespace
        from mahler import config
        self.cfg["defaults"] = config.DEFAULTS["defaults"].copy()
        self.led.upsert_item("proj", 1, state="verifying", pr=91)
        self.led.upsert_item("proj", 2, state="ready")
        self.led.claim("proj", 1, "conductor", "auto", 10, capacity=False)
        claim = self.led.claim
        def bounded_claim(*args, **kwargs):
            return claim(*args, **kwargs, max_parallel=1)
        args = SimpleNamespace(item=("proj", 2), holder="test", steal=False)
        with patch.object(self.led, "claim", side_effect=bounded_claim), patch("sys.stdout", io.StringIO()):
            self.assertEqual(cli.cmd_claim(args, self.cfg, self.led), 0)
        self.led.release("proj", 2)
        self.led.release("proj", 1)
        run = self.led.create_run(project="proj", number=1, role="review",
                                  platform="claude", epoch=1)
        self.led.claim("proj", 1, f"run:{run}", "auto", 10, run_id=run)
        buf = io.StringIO()
        with patch.object(self.led, "claim", side_effect=bounded_claim), patch("sys.stdout", buf):
            self.assertEqual(cli.cmd_claim(args, self.cfg, self.led), 1)
        self.assertIn(f"proj#1 — claude review run {run}", buf.getvalue())

    def test_status_quota_shows_reset_countdown_chips(self):
        # mahler#52: quota lines append `[5h in ...  · wk in ...]` chips
        # when a window has a fresh, still-future reset time.
        from datetime import timedelta
        from mahler.ledger import iso
        cfg = {
            "defaults": {},
            "platforms": {
                "claude": {"enabled": True, "kind": "claude",
                           "soft": {"5h": 60, "weekly": 70},
                           "hard": {"5h": 70, "weekly": 80},
                           "stale_minutes": 15},
            },
            "projects": {},
        }
        later = iso(self.led.now() + timedelta(hours=1, minutes=26))
        self.led.record_usage("claude", "5h", 42.0, later)

        class Args:
            json = False
            project = None

        buf = io.StringIO()
        with patch("sys.stdout", buf):
               ret = cli.cmd_status(Args(), cfg, self.led)

        self.assertEqual(ret, 0)
        output = buf.getvalue()
        self.assertRegex(output, r"\[5h in 1h 2[0-9]m\]")

    def test_status_shows_burst_note(self):
        """D23: during a burst, `mahler status` prints a burst note."""
        import copy
        from datetime import timedelta
        from mahler import config
        from mahler.ledger import iso
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["projects"] = {}
        # Claude usage within the weekly burst lead (2h), at 85%
        five_reset = iso(self.led.now() + timedelta(minutes=30))
        weekly_reset = iso(self.led.now() + timedelta(hours=2))
        self.led.record_usage("claude", "5h", 85.0, five_reset)
        self.led.record_usage("claude", "weekly", 85.0, weekly_reset)

        class Args:
            json = False
            project = None

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cli.cmd_status(Args(), cfg, self.led)
        output = buf.getvalue()
        self.assertIn("D23", output)
        self.assertIn("burst", output)


if __name__ == "__main__":
    unittest.main()
