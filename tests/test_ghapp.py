"""GitHub App identity for conductor writes (mahler#607). No network, no real key."""

import base64
import io
import json
import os
import pathlib
import stat
import tempfile
import unittest
from unittest import mock

from mahler import config, ghapp

APP = {"app_id": 5127048, "installation_id": 166283054, "private_key": "/nonexistent.pem"}


def sign(app_id, key_path, now):
    return "jwt"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        ghapp.reset()
        self.addCleanup(ghapp.reset)

    def token(self, app=APP, now=Clock(), sign=sign, fetch=lambda j, i: ("t", 9e9)):
        return ghapp.installation_token(
            app, now, sign, fetch, cache_dir=self.tmp.name)

    def test_mint_and_cache(self):
        clock, calls = Clock(), []

        def fetch(jwt, inst):
            calls.append((jwt, inst))
            return "tok1", clock() + 3600
        self.assertEqual(self.token(now=clock, fetch=fetch), "tok1")
        clock.t += 1000
        self.assertEqual(self.token(now=clock, fetch=fetch), "tok1")
        self.assertEqual(calls, [("jwt", "166283054")])

    def test_mint_sends_signed_jwt_in_token_request(self):
        # Keep the fake visibly synthetic: log scrubbers redact three-segment
        # JWT-shaped fixtures and can make this assertion look like it expects
        # a literal redaction marker.
        signed_jwt = "signed-app-jwt-from-openssl"
        signer = mock.Mock(return_value=signed_jwt)
        requests = []

        def urlopen(req, timeout):
            requests.append(req)
            self.assertEqual(req.get_header("Authorization"), f"Bearer {signed_jwt}")
            self.assertEqual(req.full_url,
                             f"{ghapp.API}/app/installations/166283054/access_tokens")
            self.assertEqual(req.get_method(), "POST")
            self.assertEqual(timeout, 20)
            return io.StringIO(json.dumps({
                "token": "test-installation-token", "expires_at": "2030-01-01T00:00:00Z",
            }))

        def fetch(jwt, installation_id):
            return ghapp._request_token(jwt, installation_id, urlopen=urlopen)

        self.assertEqual(self.token(sign=signer, fetch=fetch), "test-installation-token")
        signer.assert_called_once_with("5127048", APP["private_key"], 1000.0)
        self.assertEqual(len(requests), 1)

    def test_cache_survives_process_memory(self):
        clock, calls = Clock(), []

        def fetch(jwt, inst):
            calls.append(inst)
            return "shared", clock() + 3600
        self.assertEqual(self.token(now=clock, fetch=fetch), "shared")
        ghapp.reset()  # a new launcher process starts with this empty
        self.assertEqual(self.token(now=clock, fetch=fetch), "shared")
        self.assertEqual(calls, ["166283054"])

    def test_cache_files_are_user_only(self):
        self.assertEqual(self.token(), "t")
        files = list(pathlib.Path(self.tmp.name).iterdir())
        self.assertEqual(len(files), 2)
        self.assertEqual(stat.S_IMODE(os.stat(self.tmp.name).st_mode), 0o700)
        self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o600
                            for path in files))

    def test_refreshes_before_expiry(self):
        clock, n = Clock(), [0]

        def fetch(jwt, inst):
            n[0] += 1
            return f"tok{n[0]}", clock() + 3600
        self.token(now=clock, fetch=fetch)
        clock.t += 3600 - ghapp.REFRESH_BEFORE + 1
        self.assertEqual(self.token(now=clock, fetch=fetch), "tok2")

    def test_failure_returns_none_and_backs_off(self):
        clock, n = Clock(), [0]

        def fetch(jwt, inst):
            n[0] += 1
            raise OSError("down")
        self.assertIsNone(self.token(now=clock, fetch=fetch))
        ghapp.reset()
        self.assertIsNone(self.token(now=clock, fetch=fetch))
        self.assertEqual(n[0], 1)
        clock.t += ghapp.RETRY_AFTER + 1
        self.assertIsNone(self.token(now=clock, fetch=fetch))
        self.assertEqual(n[0], 2)

    def test_bad_config_returns_none(self):
        self.assertIsNone(self.token({"app_id": 1}))

    def test_sign_failure_returns_none(self):
        def bad_sign(*a):
            raise RuntimeError("no openssl")
        self.assertIsNone(self.token(sign=bad_sign))

    def test_jwt_shape(self):
        seen = {}

        def run(argv, **kw):
            seen["argv"], seen["input"] = argv, kw["input"]
            return mock.Mock(returncode=0, stdout=b"sig")
        jwt = ghapp.make_jwt(5127048, "/k.pem", 1000, run=run)
        head, payload, sig = jwt.split(".")
        pad = lambda s: s + "=" * (-len(s) % 4)
        self.assertEqual(json.loads(base64.urlsafe_b64decode(pad(payload))),
                         {"iat": 940, "exp": 1540, "iss": "5127048"})
        self.assertEqual(base64.urlsafe_b64decode(pad(sig)), b"sig")
        self.assertEqual(seen["input"], f"{head}.{payload}".encode())
        self.assertEqual(seen["argv"][-1], "/k.pem")


class InstallationSelectionTests(unittest.TestCase):
    def test_owner_mapping_selects_each_installation(self):
        app = {"app_id": 1, "private_key": "/k",
               "installations": {"one": 11, "two": 22}}
        self.assertEqual(ghapp.select_installation(app, "one/repo")["installation_id"], 11)
        self.assertEqual(ghapp.select_installation(app, "two/repo")["installation_id"], 22)

    def test_unmapped_owner_falls_back(self):
        app = {"app_id": 1, "private_key": "/k", "installation_id": 99,
               "installations": {"one": 11}}
        self.assertIsNone(ghapp.select_installation(app, "other/repo"))

    def test_project_override_wins(self):
        app = {"app_id": 1, "private_key": "/k", "installations": {"one": 11}}
        self.assertEqual(
            ghapp.select_installation(app, "one/repo", 44)["installation_id"], 44)

    def test_singular_global_installation_remains_supported(self):
        self.assertEqual(ghapp.select_installation(APP, "one/repo"), APP)


class GhEnvTests(unittest.TestCase):
    def setUp(self):
        ghapp.reset()
        self.addCleanup(ghapp.reset)

    def cfg(self, **extra):
        return {"accounts": {"work": {"env": {"GH_CONFIG_DIR": "~/gh-work"}}}, **extra}

    def test_no_app_is_run_env(self):
        self.assertIsNone(config.gh_env(self.cfg(), {"name": "p"}))

    def test_personal_project_gets_bot_token(self):
        with mock.patch.object(ghapp, "installation_token", return_value="bot") as m:
            env = config.gh_env(
                self.cfg(github_app=APP), {"name": "p", "repo": "one/repo"})
        self.assertEqual(env["GH_TOKEN"], "bot")
        m.assert_called_once_with(APP, cache_dir=config.GITHUB_APP_CACHE_DIR)

    def test_owner_mapping_selects_token_for_project(self):
        app = {"app_id": 1, "private_key": "/k", "installations": {"one": 11}}
        with mock.patch.object(ghapp, "installation_token", return_value="bot") as m:
            env = config.gh_env(
                self.cfg(github_app=app), {"name": "p", "repo": "one/repo"})
        self.assertEqual(env["GH_TOKEN"], "bot")
        selected = {"app_id": 1, "private_key": "/k", "installation_id": 11}
        m.assert_called_once_with(selected, cache_dir=config.GITHUB_APP_CACHE_DIR)

    def test_unmapped_owner_keeps_existing_identity(self):
        app = {"app_id": 1, "private_key": "/k", "installations": {"one": 11}}
        with mock.patch.object(ghapp, "installation_token", return_value="bot") as m:
            env = config.gh_env(
                self.cfg(github_app=app), {"name": "p", "repo": "other/repo"})
        self.assertIsNone(env)
        m.assert_not_called()

    def test_work_project_keeps_its_own_identity(self):
        with mock.patch.object(ghapp, "installation_token", return_value="bot") as m:
            env = config.gh_env(self.cfg(github_app=APP), {"name": "p", "gh_account": "work"})
        self.assertNotIn("GH_TOKEN", env)
        m.assert_not_called()

    def test_failed_mint_falls_back(self):
        with mock.patch.object(ghapp, "installation_token", return_value=None):
            self.assertIsNone(config.gh_env(self.cfg(github_app=APP), {"name": "p"}))

    def test_pinned_personal_token_wins(self):
        cfg = self.cfg(github_app=APP)
        cfg["accounts"]["personal"] = {"env": {"GH_TOKEN": "pinned"}}
        with mock.patch.object(ghapp, "installation_token", return_value="bot") as m:
            env = config.gh_env(cfg, {"name": "p"})
        self.assertEqual(env["GH_TOKEN"], "pinned")
        m.assert_not_called()


if __name__ == "__main__":
    unittest.main()
