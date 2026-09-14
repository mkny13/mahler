"""Redaction of credential-shaped strings before Mahler surfaces them (issue #76)."""

import os
import tempfile
import unittest
from unittest import mock

from mahler import redact


GHP = "ghp_0123456789abcdefghijklmn"      # classic GitHub token shape
SKANT = "sk-ant-api03-0123456789abcdefgh"  # Anthropic key shape


class TestRedact(unittest.TestCase):
    def test_postgres_url_password_is_masked(self):
        out = redact.redact("connection failed for postgres://bob:s3cret@db.example.com:5432/app")
        self.assertIn("postgres://bob:<redacted>@db.example.com:5432/app", out)
        self.assertNotIn("s3cret", out)

    def test_https_url_password_is_masked(self):
        out = redact.redact("remote: https://octocat:tok12345@github.com/mkny13/mahler.git")
        self.assertIn("https://octocat:<redacted>@github.com/mkny13/mahler.git", out)
        self.assertNotIn("tok12345", out)

    def test_url_without_password_is_untouched(self):
        line = "pushing to https://github.com/mkny13/mahler.git"
        self.assertEqual(redact.redact(line), line)

    def test_named_credential_env_assignment(self):
        out = redact.redact(f"+ export GH_TOKEN={GHP}")
        self.assertIn("GH_TOKEN=<redacted>", out)
        self.assertNotIn(GHP, out)

    def test_generic_suffix_env_assignment(self):
        out = redact.redact("MY_SERVICE_PASSWORD=hunter2")
        self.assertIn("MY_SERVICE_PASSWORD=<redacted>", out)
        self.assertNotIn("hunter2", out)

    def test_assignment_without_credential_name_is_untouched(self):
        line = "GITHUB_HOST=github.com"
        self.assertEqual(redact.redact(line), line)

    def test_bare_github_token_shape(self):
        out = redact.redact(f"got 401 for {GHP}, retrying")
        self.assertNotIn(GHP, out)
        self.assertIn("<redacted>", out)

    def test_bare_anthropic_key_shape(self):
        out = redact.redact(f"using key {SKANT} failed")
        self.assertNotIn(SKANT, out)

    def test_aws_key_shape(self):
        out = redact.redact("upload via AKIAIOSFODNN7EXAMPLE denied")
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", out)

    def test_slack_npm_pypi_and_jwt_shapes(self):
        for token in ("xoxb-123456789012-abc", "npm_0123456789abcdefghijklmn",
                      "pypi-AgEIcHlwaS5vcmcCJDkzWN", 
                      "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"):
            out = redact.redact(f"leaked {token} in a log")
            self.assertNotIn(token, out)

    def test_ordinary_text_is_untouched(self):
        line = "git push origin main: 42% used, run 521 on cline-free ended DONE"
        self.assertEqual(redact.redact(line), line)

    def test_none_and_empty_pass_through(self):
        self.assertIsNone(redact.redact(None))
        self.assertEqual(redact.redact(""), "")


class TestSetupTailRedacted(unittest.TestCase):
    def test_setup_tail_masks_secrets_before_they_are_posted(self):
        from mahler import runner
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "setup.log"), "w") as fh:
                fh.write("npm warn using GH_TOKEN for registry\n"
                         f"+ echo GH_TOKEN={GHP}\n")
            tail = runner.setup_tail({"log_path": os.path.join(d, "agent.log")})
        self.assertIn("GH_TOKEN=<redacted>", tail)
        self.assertNotIn(GHP, tail)


class TestReadLogRedacted(unittest.TestCase):
    def test_last_text_masks_secrets(self):
        from mahler import platforms
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "agent.log")
            with open(path, "w") as fh:
                fh.write(f'debug: handing {GHP} to the API\n')
                fh.write('STATUS: NEEDS-YOU cannot reach the endpoint\n')
            s = platforms.read_log(path, "claude")
        self.assertNotIn(GHP, s["last_text"])
        self.assertIn("<redacted>", s["last_text"])


class TestNotifyRedacted(unittest.TestCase):
    def test_message_body_is_redacted_before_the_post(self):
        from mahler import notify
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = mock.Mock()
            notify.send({"ntfy": {"topic": "t", "server": "https://ntfy.sh"}},
                        "Backup failed", f"pg_dump failed: postgres://bob:s3cret@db/x")
        req = urlopen.call_args[0][0]
        self.assertNotIn(b"s3cret", req.data)
        self.assertIn(b"<redacted>", req.data)


if __name__ == "__main__":
    unittest.main()
