"""`/mahler platform X` must stick (mahler#20).

sync() re-derives pin=pin_of(labels) on every tick, so the comment command
edits the platform:* labels on GitHub and mirrors the change into the ledger.
Everything here runs against an in-memory Ledger with the gh CLI replaced by
FakeGH: no GitHub, no subprocesses. FakeGH applies label edits to its stored
issues, so a further sync() sees them — that is the regression this file pins
down: a ledger-only pin used to be wiped by the next tick's upsert.
"""

import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mahler import config, scheduler
from mahler.ledger import Ledger, iso

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


def t(minutes):
    return iso(NOW + timedelta(minutes=minutes))


class FakeGH:
    """In-memory stand-in for gh.GH. open_issues() feeds sync(); set_pin_labels
    edits the stored labels, so the next sync() reads the new pin back."""

    def __init__(self, issues=None):
        self.issues = issues or {}     # number -> {"title", "labels", "comments"}
        self.edits = []                # (number, want, current_labels)

    def open_issues(self):
        return [{"number": n, "title": i["title"], "body": "",
                 "createdAt": t(-60), "updatedAt": t(0),
                 "url": f"https://github.com/x/y/issues/{n}",
                 "labels": [{"name": l} for l in i["labels"]],
                 "comments": [{"createdAt": at, "body": body}
                              for at, body in i["comments"]]}
                for n, i in sorted(self.issues.items())]

    def issue_state(self, number):
        return "OPEN"

    def comment(self, number, body):
        pass

    def set_pin_labels(self, number, want, current_labels):
        self.edits.append((number, want, list(current_labels)))
        keep = f"platform:{want}" if want else None
        labels = [l for l in current_labels
                  if not l.startswith("platform:") or l == keep]
        if keep and keep not in labels:
            labels.append(keep)
        self.issues[number]["labels"] = labels


class PinTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["projects"]["x"] = {"path": tmp.name, "repo": "x/y"}
        self.gh = FakeGH({5: {"title": "An issue", "labels": ["mahler:inbox"],
                              "comments": []}})
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.ctx = scheduler.Ctx(self.cfg, self.led)
        self.sync()                     # the item exists, nothing pinned yet

    def sync(self):
        with mock.patch.object(self.ctx, "gh", return_value=self.gh):
            scheduler.sync(self.ctx, "x")

    def command(self, body, minutes=1):
        """A user comment arrives; sync processes it."""
        self.gh.issues[5]["comments"].append((t(minutes), body))
        self.sync()

    def lines(self):
        return "\n".join(self.ctx.lines)

    def test_platform_command_sets_the_label_and_sticks(self):
        self.command("/mahler platform agy-claude")
        self.assertEqual(self.gh.edits, [(5, "agy-claude", ["mahler:inbox"])])
        self.assertEqual(self.led.item("x", 5)["pin"], "agy-claude")
        self.assertIn("platform:agy-claude", self.gh.issues[5]["labels"])
        # The regression: two further ticks must not wipe the pin.
        self.sync()
        self.sync()
        self.assertEqual(self.led.item("x", 5)["pin"], "agy-claude")
        self.assertEqual(len(self.gh.edits), 1)     # the command ran once

    def test_switching_platforms_replaces_the_label(self):
        self.command("/mahler platform agy-gemini")
        self.command("/mahler platform claude", minutes=2)
        self.assertEqual([e[1] for e in self.gh.edits], ["agy-gemini", "claude"])
        self.assertNotIn("platform:agy-gemini", self.gh.issues[5]["labels"])
        self.assertEqual(self.led.item("x", 5)["pin"], "claude")

    def test_platform_none_unpins(self):
        self.command("/mahler platform agy-claude")
        self.command("/mahler platform none", minutes=2)
        self.assertEqual(self.gh.edits[-1][:2], (5, None))
        self.assertNotIn("platform:agy-claude", self.gh.issues[5]["labels"])
        self.assertIsNone(self.led.item("x", 5)["pin"])
        self.sync()                     # the next tick keeps it unpinned
        self.assertIsNone(self.led.item("x", 5)["pin"])

    def test_platform_auto_unpins(self):
        self.command("/mahler platform claude")
        self.command("/mahler platform auto", minutes=2)
        self.assertEqual(self.gh.edits[-1][:2], (5, None))
        self.assertNotIn("platform:claude", self.gh.issues[5]["labels"])
        self.assertIsNone(self.led.item("x", 5)["pin"])
        self.sync()
        self.assertIsNone(self.led.item("x", 5)["pin"])

    def test_unknown_platform_changes_nothing(self):
        before = list(self.gh.issues[5]["labels"])
        self.command("/mahler platform nosuch")
        self.assertIn("unknown platform 'nosuch'", self.lines())
        self.assertEqual(self.gh.edits, [])
        self.assertEqual(self.gh.issues[5]["labels"], before)
        self.assertIsNone(self.led.item("x", 5)["pin"])

    def test_bare_platform_is_logged_without_change(self):
        before = list(self.gh.issues[5]["labels"])
        self.command("/mahler platform")
        self.assertIn("instruction 'platform'", self.lines())
        self.assertEqual(self.gh.edits, [])
        self.assertEqual(self.gh.issues[5]["labels"], before)
        self.assertIsNone(self.led.item("x", 5)["pin"])

    def test_hand_set_label_still_pins_without_a_command(self):
        self.gh.issues[5]["labels"].append("platform:agy-gemini")
        self.sync()
        self.assertEqual(self.led.item("x", 5)["pin"], "agy-gemini")
        self.assertEqual(self.gh.edits, [])


if __name__ == "__main__":
    unittest.main()
