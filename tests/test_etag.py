"""Conditional GitHub polling with ETags (mahler#90).

gh.issues_changed() stands in for the full `gh issue list` fetch when
nothing changed: one `gh api -i` GET with If-None-Match, and a 304 — which
costs no rate-limit quota — lets sync() skip the fetch and the per-item
closed checks. sync() stores the last etag per project in the ledger kv,
and only after a clean pass: a failed tick must re-fetch next time, not
skip on a stale etag.
"""

import json
import unittest
from datetime import datetime, timezone
from unittest import mock

from mahler import scheduler, sync
from mahler.gh import GH, GHError
from mahler.ledger import Ledger, iso

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def proj(**over):
    pol = {"name": "mahler", "enabled": True, "repo": "mkny13/mahler", "path": "/tmp"}
    pol.update(over)
    return pol


HEADERS_200 = ('HTTP/2.0 200 OK\r\n'
               'Content-Type: application/json; charset=utf-8\r\n'
               'Etag: W/"abc123"\r\n'
               '\r\n'
               '[]')


class IssuesChangedTests(unittest.TestCase):
    def setUp(self):
        self.gh = GH("o/r")

    def probe(self, out=None, side_effect=None, etag='W/"old"'):
        with mock.patch.object(self.gh, "_gh", return_value=out,
                               side_effect=side_effect) as g:
            result = self.gh.issues_changed(etag)
        return result, g

    def test_200_returns_changed_and_new_etag(self):
        (changed, etag), g = self.probe(out=HEADERS_200)
        self.assertTrue(changed)
        self.assertEqual(etag, 'W/"abc123"')
        # one small conditional GET on the open-issue collection
        self.assertEqual(g.call_args[0][0], "api")
        self.assertEqual(g.call_args[0][2],
                         "repos/o/r/issues?state=open&sort=updated&direction=desc&per_page=1")
        self.assertEqual(g.call_args[0][4], 'If-None-Match: W/"old"')

    def test_304_is_not_modified_and_keeps_the_etag(self):
        err = GHError("gh api -i repos/o/r/issues: HTTP 304")
        (changed, etag), _ = self.probe(side_effect=err)
        self.assertFalse(changed)
        self.assertEqual(etag, 'W/"old"')

    def test_other_gh_errors_propagate(self):
        with self.assertRaises(GHError):
            self.probe(side_effect=GHError("gh: HTTP 500"))

    def test_first_poll_sends_no_condition(self):
        (changed, etag), g = self.probe(out=HEADERS_200, etag=None)
        self.assertTrue(changed)
        self.assertEqual(etag, 'W/"abc123"')
        self.assertNotIn("-H", g.call_args[0])

    def test_etag_parse_ignores_the_body(self):
        body_lie = HEADERS_200 + '{"title": "etag: fake"}'
        (changed, etag), _ = self.probe(out=body_lie)
        self.assertEqual(etag, 'W/"abc123"')

    def test_missing_etag_header_is_reported_as_none(self):
        out = 'HTTP/2.0 200 OK\r\nContent-Type: application/json\r\n\r\n[]'
        (changed, etag), _ = self.probe(out=out)
        self.assertTrue(changed)
        self.assertIsNone(etag)


class SyncETagTests(unittest.TestCase):
    """sync() against a mocked GH: fetch on change, skip on 304, store the
    etag only after a clean pass."""

    def setUp(self):
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)
        self.cfg = {"defaults": {}, "projects": {"mahler": proj()}}
        self.led.set_kv("depends_format:mahler", "2")
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
        self.gh = mock.Mock()
        self.ctx._gh["mkny13/mahler"] = self.gh

    def issue(self, n=11):
        return {"number": n, "title": "T", "labels": [], "body": "",
                "createdAt": iso(NOW), "updatedAt": iso(NOW), "comments": []}

    def test_full_sync_processes_and_stores_the_etag(self):
        self.gh.issues_changed.return_value = (True, 'W/"e1"')
        self.gh.open_issues.return_value = [self.issue()]
        sync.sync(self.ctx, "mahler")
        self.assertEqual(self.led.item("mahler", 11)["title"], "T")
        self.assertEqual(self.led.get_kv("etag:mahler"), 'W/"e1"')

    def test_dependency_format_upgrade_reparses_even_on_304(self):
        self.led.set_kv("depends_format:mahler", None)
        self.led.set_kv("etag:mahler", 'W/"e1"')
        self.led.upsert_item("mahler", 11, depends="[125]")
        issue = self.issue()
        issue["body"] = "Depends on: mkny13/groundwork#125, #7"
        self.gh.issues_changed.return_value = (False, 'W/"e1"')
        self.gh.open_issues.return_value = [issue]
        sync.sync(self.ctx, "mahler")
        self.assertEqual(json.loads(self.led.item("mahler", 11)["depends"]),
                         [{"repo": "mkny13/groundwork", "number": 125}, 7])
        self.assertEqual(self.led.get_kv("depends_format:mahler"), "2")
        sync.sync(self.ctx, "mahler")
        self.gh.open_issues.assert_called_once()

    def test_304_skips_the_fetch(self):
        self.led.set_kv("etag:mahler", 'W/"e1"')
        self.gh.issues_changed.return_value = (False, 'W/"e1"')
        sync.sync(self.ctx, "mahler")
        self.gh.open_issues.assert_not_called()
        self.assertIn("304", "\n".join(self.ctx.lines))
        self.assertEqual(self.led.get_kv("etag:mahler"), 'W/"e1"')

    def test_304_skips_the_closed_issue_checks(self):
        """A full pass probes ledger items not in the open list with
        issue_state(); on a 304 nothing changed, so nothing is probed."""
        self.led.upsert_item("mahler", 12, title="Old", state="ready")
        self.led.set_kv("etag:mahler", 'W/"e1"')
        self.gh.issues_changed.return_value = (False, 'W/"e1"')
        sync.sync(self.ctx, "mahler")
        self.gh.issue_state.assert_not_called()
        self.assertEqual(self.led.item("mahler", 12)["state"], "ready")

    def test_failed_pass_keeps_the_old_etag(self):
        """The etag must be stored only after a clean pass, or the next tick
        would 304-skip data it never processed."""
        self.led.set_kv("etag:mahler", 'W/"e1"')
        self.gh.issues_changed.return_value = (True, 'W/"e2"')
        self.gh.open_issues.side_effect = GHError("gh: HTTP 500")
        with self.assertRaises(GHError):
            sync.sync(self.ctx, "mahler")
        self.assertEqual(self.led.get_kv("etag:mahler"), 'W/"e1"')

    def test_unparseable_etag_is_not_stored(self):
        """etag None just means the next tick probes with the old one."""
        self.led.set_kv("etag:mahler", 'W/"e1"')
        self.gh.issues_changed.return_value = (True, None)
        self.gh.open_issues.return_value = [self.issue()]
        sync.sync(self.ctx, "mahler")
        self.assertEqual(self.led.item("mahler", 11)["title"], "T")
        self.assertEqual(self.led.get_kv("etag:mahler"), 'W/"e1"')
