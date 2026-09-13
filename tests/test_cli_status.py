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
        self.assertIn("https://github.com/owner/proj/issues/1", output)
        self.assertIn("https://github.com/owner/proj/pull/123", output)


if __name__ == "__main__":
    unittest.main()
