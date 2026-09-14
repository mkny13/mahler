"""ntfy title encoding (mahler#45): em-dash and friends must not become '?'."""

import unittest
from unittest import mock

from mahler import notify


class TestAsciiTitle(unittest.TestCase):
    def test_em_dash_becomes_hyphen(self):
        self.assertEqual(notify._ascii_title("Mahler needs you — mahler #45"),
                          "Mahler needs you - mahler #45")

    def test_en_dash_becomes_hyphen(self):
        self.assertEqual(notify._ascii_title("a – b"), "a - b")

    def test_curly_quotes_and_ellipsis(self):
        self.assertEqual(notify._ascii_title("\u2018hi\u2019 \u201cthere\u201d\u2026"),
                          "'hi' \"there\"...")

    def test_plain_ascii_untouched(self):
        self.assertEqual(notify._ascii_title("Shipped - x #5"), "Shipped - x #5")

    def test_unmapped_non_ascii_still_falls_back_to_question_mark(self):
        self.assertEqual(notify._ascii_title("caf\u00e9"), "caf?")


class TestSend(unittest.TestCase):
    def _cfg(self):
        return {"ntfy": {"topic": "mytopic", "server": "https://ntfy.sh"}}

    def test_title_header_is_ascii_with_hyphen(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = mock.Mock()
            ok = notify.send(self._cfg(), "Mahler needs you — mahler #45", "body")
        self.assertTrue(ok)
        req = urlopen.call_args[0][0]
        self.assertEqual(req.get_header("Title"), "Mahler needs you - mahler #45")

    def test_no_topic_configured_is_a_noop(self):
        self.assertFalse(notify.send({"ntfy": {}}, "title"))

    def test_high_priority_sends_to_both_topics_when_topic_high_configured(self):
        cfg = {"ntfy": {"topic": "mytopic", "topic_high": "mytopic-high", "server": "https://ntfy.sh"}}
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = mock.Mock()
            ok = notify.send(cfg, "High priority", "body", priority="high")
        self.assertTrue(ok)
        self.assertEqual(urlopen.call_count, 2)
        # First call to main topic
        req1 = urlopen.call_args_list[0][0][0]
        self.assertEqual(req1.full_url, "https://ntfy.sh/mytopic")
        self.assertEqual(req1.get_header("Priority"), "high")
        # Second call to high-priority topic
        req2 = urlopen.call_args_list[1][0][0]
        self.assertEqual(req2.full_url, "https://ntfy.sh/mytopic-high")
        self.assertEqual(req2.get_header("Priority"), "high")

    def test_non_high_priority_only_sends_to_main_topic(self):
        cfg = {"ntfy": {"topic": "mytopic", "topic_high": "mytopic-high", "server": "https://ntfy.sh"}}
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = mock.Mock()
            ok = notify.send(cfg, "Normal priority", "body", priority="default")
        self.assertTrue(ok)
        self.assertEqual(urlopen.call_count, 1)
        req = urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "https://ntfy.sh/mytopic")

    def test_high_priority_without_topic_high_only_sends_to_main_topic(self):
        cfg = {"ntfy": {"topic": "mytopic", "server": "https://ntfy.sh"}}
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = mock.Mock()
            ok = notify.send(cfg, "High priority", "body", priority="high")
        self.assertTrue(ok)
        self.assertEqual(urlopen.call_count, 1)
        req = urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "https://ntfy.sh/mytopic")

    def test_only_topic_high_configured_high_priority_sends(self):
        cfg = {"ntfy": {"topic_high": "mytopic-high", "server": "https://ntfy.sh"}}
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = mock.Mock()
            ok = notify.send(cfg, "High priority", "body", priority="high")
        self.assertTrue(ok)
        self.assertEqual(urlopen.call_count, 1)
        req = urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "https://ntfy.sh/mytopic-high")

    def test_only_topic_high_configured_non_high_priority_noop(self):
        cfg = {"ntfy": {"topic_high": "mytopic-high", "server": "https://ntfy.sh"}}
        self.assertFalse(notify.send(cfg, "Normal priority", "body", priority="default"))


if __name__ == "__main__":
    unittest.main()
