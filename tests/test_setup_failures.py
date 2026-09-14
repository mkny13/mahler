"""Setup failures (exit 97) must surface in handoffs and cap out (issue #8)."""

import os
import tempfile
import unittest

from mahler import finalize, scheduler, sync
from mahler.gh import GHError
from mahler.ledger import Ledger


class FakeGH:
    def __init__(self, fail_comment=False):
        self.comments = []
        self.fail_comment = fail_comment

    def issue_state(self, n):
        return "OPEN"

    def comment(self, n, body):
        if self.fail_comment:
            raise GHError("rate limited")
        self.comments.append(body)


class StubCtx:
    def __init__(self, cfg, led, gh):
        self.cfg, self.led, self._gh = cfg, led, gh
        self.lines, self.pings = [], []
        self.dry_run = False

    def policy(self, project):
        return {"path": "/nonexistent", "base": "main", "run_timeout_minutes": 30,
                "progress_timeout_minutes": 15, "auto_lease_minutes": 30,
                "yield_grace_seconds": 30, "max_attempts": 3}

    def gh(self, project):
        return self._gh

    def say(self, msg):
        self.lines.append(msg)

    def ping(self, title, message="", project=None, number=None,
             priority="default", tags=""):
        self.pings.append((title, message, priority))


class SetupFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.log_path = os.path.join(d, "agent.log")
        with open(self.log_path, "w") as fh:
            fh.write("the agent never got to speak\n")
        self.status_path = os.path.join(d, "exit")
        with open(self.status_path, "w") as fh:
            fh.write("97")
        self.setup_path = os.path.join(d, "setup.log")
        with open(self.setup_path, "w") as fh:
            fh.write("npm ERR! missing package.json\n")
        self.led = Ledger(":memory:")
        self.addCleanup(self.led.close)
        self.led.upsert_item("p", 8, title="surface setup failures")
        self.run_id = self.led.create_run(
            project="p", number=8, role="build", platform="test", epoch=1,
            log_path=self.log_path, status_path=self.status_path,
            worktree=os.path.join(d, "wt"))
        self.gh = FakeGH()
        self.ctx = StubCtx({"platforms": {"test": {"kind": "claude"}}}, self.led, self.gh)

    def tearDown(self):
        self.tmp.cleanup()

    def end(self, code=None):
        if code is not None:
            with open(self.status_path, "w") as fh:
                fh.write(str(code))
        finalize.finalize(self.ctx, self.led.run(self.run_id))

    def test_first_setup_failure_retries_without_burning_an_attempt(self):
        self.end()
        item = self.led.item("p", 8)
        self.assertEqual(item["setup_fails"], 1)
        self.assertEqual(item["attempts"], 0)
        self.assertEqual(item["state"], "inbox")
        self.assertEqual(self.led.run(self.run_id)["outcome"], "setup failed")
        body = self.gh.comments[-1]
        self.assertTrue(body.startswith("<!-- mahler:handoff"))
        self.assertIn("reason=setup-failed", body)
        self.assertIn("**Setup failed**", body)
        self.assertIn("npm ERR! missing package.json", body)
        self.assertIn("Last 20 lines of setup.log", body)

    def test_second_consecutive_setup_failure_needs_you_with_the_log_tail(self):
        self.end()
        self.end()
        item = self.led.item("p", 8)
        self.assertEqual(item["state"], "needs_you")
        self.assertEqual(item["setup_fails"], 2)
        self.assertEqual(item["attempts"], 0)
        self.assertIn("/mahler go", self.gh.comments[-1])
        self.assertIn("npm ERR! missing package.json", self.gh.comments[-1])
        self.assertEqual([p[2] for p in self.ctx.pings], ["low", "high"])

    def test_missing_setup_log_is_said_so(self):
        os.remove(self.setup_path)
        self.end()
        self.assertIn("(setup.log was empty or missing)", self.gh.comments[-1])

    def test_non_setup_exit_resets_the_consecutive_count(self):
        self.end()
        self.end(1)                      # setup succeeded this time; the agent failed
        item = self.led.item("p", 8)
        self.assertEqual(item["setup_fails"], 0)
        self.assertEqual(item["attempts"], 1)
        self.assertEqual(item["state"], "inbox")

    def test_go_clears_the_count(self):
        self.end()
        sync._apply_instruction(self.ctx, "p", self.led.item("p", 8), "go", None)
        self.assertEqual(self.led.item("p", 8)["setup_fails"], 0)

    def test_comment_failure_does_not_break_the_setup_failure_handling(self):
        self.gh.fail_comment = True
        self.end()   # must not raise
        item = self.led.item("p", 8)
        self.assertEqual(item["setup_fails"], 1)
        self.assertEqual(item["state"], "inbox")
        self.assertEqual(self.gh.comments, [])
        self.assertIn("couldn't post setup-failure comment", "\n".join(self.ctx.lines))


if __name__ == "__main__":
    unittest.main()
