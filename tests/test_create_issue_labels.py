import unittest
from unittest import mock

from mahler import gh as ghmod
from mahler.gh import GH, GHError


class TestCreateIssueEnsuresLabels(unittest.TestCase):
    def test_missing_labels_created_then_retry(self):
        labels = set()
        calls = []

        def fake(*args, **kw):
            calls.append(args)
            if args[:2] == ("label", "create"):
                labels.add(args[2])
                return ""
            wanted = [args[i + 1] for i, a in enumerate(args) if a == "--label"]
            missing = [w for w in wanted if w not in labels]
            if missing:
                raise GHError(f"gh issue create -R: could not add label: '{missing[0]}' not found")
            return "https://github.com/o/r/issues/1\n"

        client = GH("o/r")
        with mock.patch.object(client, "_gh", side_effect=fake):
            url = client.create_issue("t", "b", ["type:chore", "pass:docs"] if False else ["type:chore"])
        self.assertEqual(url, "https://github.com/o/r/issues/1")
        self.assertIn("type:chore", labels)
        self.assertEqual(sum(1 for c in calls if c[:2] == ("issue", "create")), 2)

    def test_other_errors_not_retried(self):
        client = GH("o/r")
        with mock.patch.object(client, "_gh", side_effect=GHError("boom")) as m:
            with self.assertRaises(GHError):
                client.create_issue("t", "b", ["type:chore"])
        self.assertEqual(m.call_count, 1)


if __name__ == "__main__":
    unittest.main()
