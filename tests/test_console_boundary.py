"""D27 write-boundary invariants (mahler#596).

The console splits its writes in two: ledger/local actions apply at once,
and GitHub/agent writes queue in `console_actions` for the tick
(`outbox.HANDLERS`). These tests pin that split, so a future edit can't
quietly move an action across the boundary or let a local action reach
GitHub.
"""

import os
import tempfile
import unittest
from unittest import mock

from mahler.console import actions, outbox
from mahler.ledger import Ledger

from test_serve import make_cfg

# The apply-at-once actions (D27): they touch only local state the tick
# doesn't own — ledger KV, holds, attachments, config.toml.
LEDGER_ONLY = {
    "pause", "resume", "end_session", "peak_override", "peak_restore",
    "clear_backoff", "unhold", "digest_seen", "brief_seen", "save_settings",
    "client_log", "attach", "answer_undo",
}
# The queued actions (D27): applied only by the tick's outbox drain.
QUEUED = {"answer", "stop_run", "capture", "revert", "uat_fail",
          "cut_release"}


class BoundaryClassificationTests(unittest.TestCase):
    def test_registered_actions_are_exactly_ledger_only_plus_queued(self):
        self.assertEqual(set(actions.ACTIONS), LEDGER_ONLY | QUEUED)

    def test_every_queued_kind_is_a_registered_action(self):
        self.assertEqual(set(outbox.HANDLERS), QUEUED)
        self.assertLessEqual(set(outbox.HANDLERS), set(actions.ACTIONS))


class LedgerOnlyActionsStayLocalTests(unittest.TestCase):
    """No apply-at-once action may reach GitHub or queue work for the tick,
    whatever body it is given (ActionError for bad input is fine — the point
    is what it never does on the way)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_action(self, name, body):
        led = Ledger(":memory:")
        self.addCleanup(led.close)
        cfg = make_cfg()
        with mock.patch("mahler.gh.GH",
                        side_effect=AssertionError("ledger-only action reached GitHub")), \
             mock.patch("mahler.config.CONFIG_PATH",
                        os.path.join(self.tmp.name, "config.toml")), \
             mock.patch("mahler.config.ATTACHMENTS_DIR",
                        os.path.join(self.tmp.name, "attachments")):
            try:
                actions.run(cfg, led, name, body)
            except actions.ActionError:
                pass
        return led

    def test_no_github_and_no_queue(self):
        for name in sorted(LEDGER_ONLY):
            with self.subTest(action=name):
                led = self.run_action(name, {})
                self.assertEqual(led.q("SELECT * FROM console_actions"), [],
                                 f"{name} queued work for the tick")
                self.assertEqual(led.q("SELECT * FROM runs"), [],
                                 f"{name} touched runs")
                self.assertEqual(led.q("SELECT * FROM leases"), [],
                                 f"{name} touched leases")
