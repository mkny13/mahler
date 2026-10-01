"""GitHub App auth: no real credentials, state or network."""

import base64
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import traceback
import unittest
from unittest import mock

from mahler import config, github_app
from mahler.gh import GH, GHError

NOW = 1800000000
TOKEN = "ghs_" + "a" * 36


def response(token=TOKEN, expiry=NOW + 3600):
    return io.BytesIO(json.dumps({"token": token, "expires_at":
        datetime.fromtimestamp(expiry, timezone.utc).isoformat()}).encode())


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "cache"
        self.app = github_app.Installation(123, 456, "/secret/key.pem", self.cache)
        self.clock = mock.patch("mahler.github_app.time.time", return_value=NOW).start()
        self.addCleanup(mock.patch.stopall)
        self.sign = mock.patch("mahler.github_app.subprocess.run", return_value=
                               subprocess.CompletedProcess([], 0, b"signature", b"")).start()
        self.http = mock.patch("mahler.github_app.urllib.request.urlopen",
                               side_effect=lambda *a, **kw: response()).start()

    def test_jwt_and_exchange(self):
        self.assertEqual(self.app.token(), TOKEN)
        argv = self.sign.call_args.args[0]
        self.assertEqual(argv, ["openssl", "dgst", "-sha256", "-sign", "/secret/key.pem"])
        self.assertNotIn("shell", self.sign.call_args.kwargs)
        message = self.sign.call_args.kwargs["input"].decode()
        header, claims = [json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
                          for p in message.split(".")]
        self.assertEqual(header, {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(claims, {"iat": NOW - 60, "exp": NOW + 540, "iss": "123"})
        req = self.http.call_args.args[0]
        self.assertEqual(req.full_url, "https://api.github.com/app/installations/456/access_tokens")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"),
                         "Bearer " + message + "." + github_app._b64(b"signature"))
        self.assertEqual(self.http.call_args.kwargs["timeout"], 30)

    def test_disk_reuse_and_early_refresh(self):
        self.app.token()
        other = github_app.Installation(123, 456, "/secret/key.pem", self.cache)
        self.clock.return_value = NOW + 3299
        self.assertEqual(other.token(), TOKEN)
        self.assertEqual(self.http.call_count, 1)
        self.clock.return_value = NOW + 3300
        self.http.side_effect = lambda *a, **kw: response("next-token", NOW + 6900)
        self.assertEqual(other.token(), "next-token")
        self.assertEqual(self.http.call_count, 2)

    def test_cache_is_private_atomic_and_installation_scoped(self):
        self.cache.mkdir(mode=0o755)
        with mock.patch("mahler.github_app.os.replace", wraps=os.replace) as replace:
            self.app.token()
        path = self.cache / "123-456.json"
        self.assertEqual(stat.S_IMODE(self.cache.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(replace.call_count, 1)
        self.assertEqual(list(self.cache.iterdir()), [path])
        github_app.Installation(123, 789, "/secret/key.pem", self.cache).token()
        github_app.Installation(321, 456, "/secret/key.pem", self.cache).token()
        self.assertEqual(self.http.call_count, 3)
        path.chmod(0o644)
        self.app.token()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_bad_cache_is_replaced(self):
        self.app.token()
        path = self.cache / "123-456.json"
        for data in ('not json', 'null', '[]', '{"token":"secret","expires_at":"bad"}'):
            path.write_text(data)
            self.assertEqual(self.app.token(), TOKEN)
        self.assertEqual(self.http.call_count, 5)

    def test_signing_errors_never_expose_path_or_output(self):
        for result in (OSError("/secret/key.pem " + TOKEN),
                       subprocess.TimeoutExpired(["openssl", "/secret/key.pem"], 30),
                       subprocess.CompletedProcess([], 1, TOKEN.encode(), b"/secret/key.pem")):
            self.sign.side_effect = result if isinstance(result, Exception) else None
            self.sign.return_value = result
            with self.assertRaises(GHError) as caught:
                self.app.token()
            rendered = ''.join(traceback.format_exception(caught.exception))
            self.assertNotIn("/secret/key.pem", rendered)
            self.assertNotIn(TOKEN, rendered)
        self.http.assert_not_called()

    def test_http_and_cache_errors_are_safe_and_fail_closed(self):
        self.http.side_effect = OSError(TOKEN + "/secret/key.pem")
        with self.assertRaisesRegex(GHError, "token exchange failed") as caught:
            self.app.token()
        self.assertNotIn(TOKEN, ''.join(traceback.format_exception(caught.exception)))
        self.assertEqual(list(self.cache.iterdir()), [])
        self.http.side_effect = lambda *a, **kw: response(expiry=NOW + 299)
        with self.assertRaisesRegex(GHError, "token exchange failed"):
            self.app.token()
        self.http.side_effect = lambda *a, **kw: response()
        with mock.patch("mahler.github_app.os.replace", side_effect=OSError("/secret/key.pem")):
            with self.assertRaisesRegex(GHError, "cache unavailable"):
                self.app.token()
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_symlink_cache_rejected(self):
        self.cache.symlink_to(self.tmp.name, target_is_directory=True)
        with self.assertRaisesRegex(GHError, "cache unavailable"):
            self.app.token()
        self.http.assert_not_called()


class ConfigTests(unittest.TestCase):
    def test_global_and_project_selection(self):
        cfg = {"github_app": {"app_id": 123, "installation_id": 456,
                              "private_key_path": "/secret/key.pem"}}
        self.assertEqual(config.github_app_settings(cfg, {}), ("123", "456", "/secret/key.pem"))
        self.assertEqual(config.github_app_settings(cfg, {"github_app_installation_id": 789}),
                         ("123", "789", "/secret/key.pem"))
        del cfg["github_app"]["installation_id"]
        cfg["projects"] = {"p": {"github_app_installation_id": 789}}
        config.validate_github_app(cfg)
        self.assertIsNone(config.github_app_settings({}, {}))

    def test_incomplete_or_invalid_configuration_fails_without_values(self):
        for app in ({"app_id": 123}, {"private_key_path": "/secret/key.pem"},
                    {"app_id": True}, {"app_id": "../secret"}, "secret", None):
            with self.subTest(app=app), self.assertRaises(ValueError) as caught:
                config.validate_github_app({"github_app": app})
            self.assertNotIn("/secret/key.pem", str(caught.exception))
            self.assertNotIn("../secret", str(caught.exception))
        with self.assertRaises(ValueError):
            config.github_app_settings({}, {"github_app_installation_id": 123})

    def test_load_validates_app_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('[github_app]\napp_id = 123\n')
            with self.assertRaisesRegex(ValueError, "installation_id"):
                config.load(path)


class ClientTests(unittest.TestCase):
    def test_every_call_resolves_fresh_token_without_mutating_base(self):
        base = {"GH_TOKEN": "human", "GITHUB_TOKEN": "other", "GH_HOST": "elsewhere", "PATH": "/bin"}
        app = mock.Mock()
        app.token.side_effect = ["one", "two", "three"]
        client = GH("org/repo", env=base, app=app)
        with mock.patch("mahler.gh.subprocess.run", return_value=
                        subprocess.CompletedProcess([], 0, "", "")) as run:
            client.comment(1, "test")
            client._git("/repo", "push", "origin", "branch")
            client._git("/repo", "fetch", "origin", "main")
        self.assertEqual(run.call_args_list[0].kwargs["env"]["GH_TOKEN"], "one")
        self.assertNotIn("GITHUB_TOKEN", run.call_args_list[0].kwargs["env"])
        for call in run.call_args_list[1:]:
            self.assertEqual(call.kwargs["env"]["GH_TOKEN"], "human")
            self.assertEqual(call.kwargs["env"]["GITHUB_TOKEN"], "other")
        self.assertIn("https://github.com/org/repo.git", run.call_args_list[1].args[0])
        self.assertEqual(base["GH_TOKEN"], "human")
        self.assertEqual(base["GITHUB_TOKEN"], "other")
        self.assertNotIn("GIT_CONFIG_COUNT", base)
        self.assertEqual(app.token.call_count, 1)

    def test_git_credential_resolution_uses_app_instead_of_saved_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "gh"
            executable.write_text('#!/bin/sh\nprintf "username=x-access-token\\npassword=%s\\n" "$GH_TOKEN"\n')
            executable.chmod(0o700)
            env = {"PATH": tmp + os.pathsep + os.defpath, "HOME": tmp,
                   "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
            subprocess.run(["git", "init", "-q", tmp], env=env, check=True,
                           capture_output=True)
            subprocess.run(["git", "-C", tmp, "config", "credential.helper",
                            "!echo username=human; echo password=human"],
                           env=env, check=True, capture_output=True)
            app = mock.Mock()
            app.token.return_value = TOKEN
            client = GH("org/repo", env=env, app=app)
            result = subprocess.run(["git", "-C", tmp, "credential", "fill"],
                env=client._env(git=True), input="protocol=https\nhost=github.com\n\n",
                text=True, capture_output=True, check=True)
            self.assertIn("password=" + TOKEN, result.stdout)
            self.assertIn("username=x-access-token", result.stdout)
            self.assertNotIn("human", result.stdout)

    def test_push_direct_subprocess_uses_git_credentials_without_app_token(self):
        app = mock.Mock()
        app.token.return_value = TOKEN
        client = GH("org/repo", env={}, app=app)
        with mock.patch("mahler.gh.subprocess.run", side_effect=[
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "abc", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]) as run:
            self.assertEqual(client.push_branch("/repo", "branch", "snapshot"), "abc")
        self.assertEqual(app.token.call_count, 0)
        for call in run.call_args_list:
            self.assertNotIn("GH_TOKEN", call.kwargs["env"])
        self.assertIn("https://github.com/org/repo.git", run.call_args_list[2].args[0])

    def test_inherit_fallback_and_app_failure(self):
        with mock.patch.dict(os.environ, {"GH_TOKEN": "human"}):
            client = GH("org/repo")
            self.assertIsNone(client._env())
            app = mock.Mock()
            app.token.return_value = TOKEN
            client = GH("org/repo", app=app)
            self.assertEqual(client._env()["GH_TOKEN"], TOKEN)
            self.assertEqual(os.environ["GH_TOKEN"], "human")
            app.token.side_effect = GHError("failed")
            with mock.patch("mahler.gh.subprocess.run") as run:
                with self.assertRaises(GHError):
                    client.comment(1, "test")
                run.assert_not_called()

    def test_command_error_redacts_actual_token(self):
        app = mock.Mock()
        app.token.return_value = "arbitrary-token-value"
        with mock.patch("mahler.gh.subprocess.run", return_value=
                        subprocess.CompletedProcess([], 1, "", "arbitrary-token-value")):
            with self.assertRaises(GHError) as caught:
                GH("o/r", app=app).comment(1, "test")
            self.assertNotIn("arbitrary-token-value", str(caught.exception))
            with self.assertRaises(GHError):
                GH("o/r", app=app)._git("/repo", "push", "origin", "main")
