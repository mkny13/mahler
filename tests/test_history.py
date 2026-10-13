"""Run history index: FTS5 search, redaction, D25 isolation, prompt block, CLI."""

import io
import os
import tempfile
import unittest
from unittest.mock import patch

from mahler import cli, history, prompt
from mahler.ledger import Ledger

FAKE_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.led = Ledger(":memory:")
        self.addCleanup(self.led.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.n = 0

    def seed(self, project, number, title, log_text=None, status="ended", log_path=None):
        self.led.upsert_item(project, number, title=title,
                             issue_body="fix the zebra crossing parser")
        if log_text is not None:
            log_path = os.path.join(self.tmp.name, f"{project}-{number}-{self.n}.log")
            with open(log_path, "w") as fh:
                fh.write(log_text)
        self.n += 1
        cur = self.led.con.execute(
            "INSERT INTO runs (project, number, role, platform, epoch, status, outcome, "
            "log_path, started_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (project, number, "build", "claude", 1, status, "done", log_path, "2026-01-01T00:00:00Z"))
        return cur.lastrowid

    def test_index_and_search(self):
        rid = self.seed("a", 1, "Zebra parser", "work...\nSTATUS: DONE fixed zebra\n")
        self.assertEqual(history.refresh(self.led, "a"), 1)
        self.assertEqual(history.refresh(self.led, "a"), 0)
        hits = history.search(self.led, "a", "zebra parser")
        self.assertEqual([h["run_id"] for h in hits], [rid])

    def test_running_not_indexed(self):
        self.seed("a", 1, "Zebra", "x", status="running")
        self.assertEqual(history.refresh(self.led, "a"), 0)

    def test_other_project_never_appears(self):
        self.seed("a", 1, "Zebra parser", "x")
        self.seed("b", 1, "Zebra parser", "x")
        history.refresh(self.led, "a")
        history.refresh(self.led, "b")
        hits = history.search(self.led, "a", "zebra")
        self.assertEqual(len(hits), 1)
        rows = self.led.q("SELECT project FROM run_history WHERE run_history MATCH 'zebra'")
        self.assertEqual({r["project"] for r in rows}, {"a", "b"})
        self.assertNotIn("b", [r["project"] for r in self.led.q(
            "SELECT project FROM run_history WHERE project='a'")])

    def test_credential_not_indexed(self):
        self.seed("a", 1, "Zebra", f"token {FAKE_KEY} leaked\n")
        history.refresh(self.led, "a")
        rows = self.led.q("SELECT body FROM run_history")
        self.assertTrue(rows)
        self.assertNotIn(FAKE_KEY, rows[0]["body"])

    def test_missing_log_still_indexes(self):
        self.seed("a", 1, "Zebra", log_path="/nonexistent/x.log")
        self.assertEqual(history.refresh(self.led, "a"), 1)
        self.assertEqual(len(history.search(self.led, "a", "zebra")), 1)

    def test_prompt_block_caps_and_excludes_current(self):
        for i in range(1, 8):
            self.seed("a", i, "Zebra parser", "zebra " * 800)
        item = {"number": 1, "title": "Zebra parser", "issue_body": "zebra"}
        block = history.render_for_prompt(self.led, "a", item)
        self.assertLessEqual(len(block.encode()), 3000)
        lines = [l for l in block.splitlines() if l.startswith("- run")]
        self.assertEqual(len(lines), 3)
        self.assertNotIn("(#1,", block)
        small = history.render_for_prompt(self.led, "a", item, max_bytes=100)
        self.assertLessEqual(len(small.encode()), 100)
        self.assertEqual(history.render_for_prompt(self.led, "zzz", item), "")

    def test_failures_do_not_raise(self):
        self.seed("a", 1, "Zebra", "x")
        with patch.object(history, "_body", side_effect=RuntimeError("boom")):
            self.assertEqual(history.refresh(self.led, "a"), 0)
        with patch.object(history, "_ensure", return_value=False):
            self.assertEqual(history.refresh(self.led, "a"), 0)
            self.assertEqual(history.search(self.led, "a", "zebra"), [])
            self.assertEqual(history.render_for_prompt(
                self.led, "a", {"number": 9, "title": "zebra"}), "")
        self.assertEqual(history.render_for_prompt(None, "a", {"number": 9, "title": "z"}), "")

    def test_query_is_sanitised(self):
        self.seed("a", 1, "Zebra", "x")
        self.assertEqual(history.search(self.led, "a", 'zebra" OR NEAR( * ) AND'), 
                         history.search(self.led, "a", "zebra OR near and"))

    def test_prompt_build_roles(self):
        self.seed("a", 1, "Zebra parser", "STATUS: DONE ok")

        class Ctx:
            led = self.led

            def policy(self, proj):
                return {"repo": "o/r", "rules": ""}
        item = {"number": 2, "title": "Zebra parser again", "issue_body": "", "pr": 5}
        prep = {"worktree": "/tmp", "branch": "b", "replayed": False, "kept": None}
        with patch.object(prompt, "ci_handoff", return_value=""), \
             patch.object(prompt, "pr_ci_toolchain_handoff", return_value=""):
            for role in ("build", "fix"):
                out = prompt.build(Ctx(), "a", item, role, "claude", prep)
                self.assertIn("untrusted background", out)
                self.assertIn("run ", out)
            for role in ("sort", "review"):
                out = prompt.build(Ctx(), "a", item, role, "claude", prep)
                self.assertNotIn("Similar past runs", out)

    def test_cli(self):
        self.seed("a", 1, "Zebra parser", "STATUS: DONE ok")
        out = io.StringIO()
        with patch.object(cli.config, "load", return_value={"platforms": {}}), \
             patch.object(cli, "Ledger", return_value=self.led), \
             patch.object(cli, "RoutedLedger", side_effect=lambda led, cfg: led), \
             patch.object(self.led, "close"), patch("sys.stdout", out):
            rc = cli.main(["history", "a", "zebra", "--limit", "2"])
        self.assertEqual(rc, 0)
        self.assertIn("a#1", out.getvalue())


if __name__ == "__main__":
    unittest.main()
