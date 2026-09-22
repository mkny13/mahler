"""`mahler ship`: an interactive session hands its PR or pushed branch to the
conductor (mahler#407), so a session's PR no longer sits open until someone
merges it by hand."""

import contextlib
import copy
import io
import unittest
from types import SimpleNamespace
from unittest import mock

from mahler import cli, config
from mahler.ledger import Ledger


def view(state="OPEN", body="Fixes #7\n\nthe summary", head="review-gate"):
    return {"state": state, "body": body, "headRefName": head, "title": "Review gate"}


class ShipCommandTests(unittest.TestCase):
    def setUp(self):
        self.led = Ledger(":memory:")
        self.addCleanup(self.led.close)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["projects"]["x"] = {"path": "/nonexistent", "repo": "x/y"}
        self.led.upsert_item("x", 7, title="Wire the review gate")
        self.led.claim("x", 7, "interactive:me", "interactive", 30)
        self.led.set_state("x", 7, "working", "claimed by me")
        p = mock.patch("mahler.cli.GH")
        self.gh = p.start().return_value
        self.addCleanup(p.stop)
        self.gh.pr_view.return_value = view()

    def run_ship(self, **kw):
        a = SimpleNamespace(item=("x", 7), pr=None, branch=None, summary=None, holder="me")
        for k, v in kw.items():
            setattr(a, k, v)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.cmd_ship(a, self.cfg, self.led)
        return rc, out.getvalue()

    def test_pr_goes_to_verifying_and_the_lease_is_released(self):
        rc, out = self.run_ship(pr=395, summary="gate wired")
        self.assertEqual(rc, 0, out)
        item = self.led.item("x", 7)
        self.assertEqual((item["state"], item["pr"], item["branch"], item["summary"]),
                         ("verifying", 395, "review-gate", "gate wired"))
        self.assertIsNone(self.led.lease("x", 7))     # the conductor can take it
        self.gh.pr_edit_body.assert_not_called()      # already says Fixes #7

    def test_pr_without_fixes_line_gets_one(self):
        self.gh.pr_view.return_value = view(body="Related to groundwork#147")
        rc, _ = self.run_ship(pr=395)
        self.assertEqual(rc, 0)
        self.gh.pr_edit_body.assert_called_once_with(
            395, "Fixes #7\n\nRelated to groundwork#147")

    def test_finds_the_pr_for_the_current_branch(self):
        self.gh.pr_for_head.return_value = 395
        with mock.patch.object(cli, "_current_branch", return_value="review-gate"):
            rc, _ = self.run_ship()
        self.assertEqual(rc, 0)
        self.gh.pr_for_head.assert_called_once_with("review-gate")
        self.assertEqual(self.led.item("x", 7)["pr"], 395)

    def test_pushed_branch_without_pr_is_left_for_the_conductor_to_open(self):
        self.gh.pr_for_head.return_value = None
        with mock.patch.object(cli.subprocess, "run",
                               return_value=SimpleNamespace(returncode=0, stdout="", stderr="")):
            rc, out = self.run_ship(branch="my-work")
        self.assertEqual(rc, 0, out)
        item = self.led.item("x", 7)
        self.assertEqual((item["state"], item["pr"], item["branch"]),
                         ("verifying", None, "my-work"))

    def test_unpushed_branch_is_refused(self):
        self.gh.pr_for_head.return_value = None
        with mock.patch.object(cli.subprocess, "run",
                               return_value=SimpleNamespace(returncode=2, stdout="", stderr="")):
            rc, out = self.run_ship(branch="my-work")
        self.assertEqual(rc, 1)
        self.assertIn("push it first", out)
        self.assertEqual(self.led.item("x", 7)["state"], "working")

    def test_closed_pr_is_refused(self):
        self.gh.pr_view.return_value = view(state="MERGED")
        rc, out = self.run_ship(pr=395)
        self.assertEqual(rc, 1)
        self.assertIn("merged", out)
        self.assertEqual(self.led.item("x", 7)["state"], "working")

    def test_someone_elses_lease_is_refused(self):
        rc, out = self.run_ship(pr=395, holder="other")
        self.assertEqual(rc, 1)
        self.assertIn("held by interactive:me", out)
        self.assertEqual(self.led.item("x", 7)["state"], "working")

    def test_unclaimed_item_can_be_shipped(self):
        self.led.release("x", 7, holder="interactive:me", to_state=None)
        rc, _ = self.run_ship(pr=395)
        self.assertEqual(rc, 0)
        self.assertEqual(self.led.item("x", 7)["state"], "verifying")

    def test_parser_wires_the_command(self):
        with mock.patch.object(cli, "cmd_ship", return_value=0) as fn, \
                mock.patch.object(cli.config, "load", return_value=self.cfg), \
                mock.patch.object(cli, "RoutedLedger"), \
                mock.patch.object(cli, "Ledger"):
            cli.main(["ship", "x#7", "--pr", "395"])
        a = fn.call_args[0][0]
        self.assertEqual((a.item, a.pr), (("x", 7), 395))


if __name__ == "__main__":
    unittest.main()
