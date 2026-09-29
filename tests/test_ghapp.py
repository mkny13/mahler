"""GitHub App identity for conductor writes (mahler#607). No network, no real key."""

import base64
import json
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
        ghapp.reset()
        self.addCleanup(ghapp.reset)

    def test_mint_and_cache(self):
        clock, calls = Clock(), []

        def fetch(jwt, inst):
            calls.append((jwt, inst))
            return "tok1", clock() + 3600
        self.assertEqual(ghapp.installation_token(APP, clock, sign, fetch), "tok1")
        clock.t += 1000
        self.assertEqual(ghapp.installation_token(APP, clock, sign, fetch), "tok1")
        self.assertEqual(calls, [("jwt", "166283054")])

    def test_refreshes_before_expiry(self):
        clock, n = Clock(), [0]

        def fetch(jwt, inst):
            n[0] += 1
            return f"tok{n[0]}", clock() + 3600
        ghapp.installation_token(APP, clock, sign, fetch)
        clock.t += 3600 - ghapp.REFRESH_BEFORE + 1
        self.assertEqual(ghapp.installation_token(APP, clock, sign, fetch), "tok2")

    def test_failure_returns_none_and_backs_off(self):
        clock, n = Clock(), [0]

        def fetch(jwt, inst):
            n[0] += 1
            raise OSError("down")
        self.assertIsNone(ghapp.installation_token(APP, clock, sign, fetch))
        self.assertIsNone(ghapp.installation_token(APP, clock, sign, fetch))
        self.assertEqual(n[0], 1)
        clock.t += ghapp.RETRY_AFTER + 1
        self.assertIsNone(ghapp.installation_token(APP, clock, sign, fetch))
        self.assertEqual(n[0], 2)

    def test_bad_config_returns_none(self):
        self.assertIsNone(ghapp.installation_token({"app_id": 1}))

    def test_sign_failure_returns_none(self):
        def bad_sign(*a):
            raise RuntimeError("no openssl")
        self.assertIsNone(ghapp.installation_token(APP, Clock(), bad_sign, lambda j, i: ("t", 9e9)))

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
            env = config.gh_env(self.cfg(github_app=APP), {"name": "p"})
        self.assertEqual(env["GH_TOKEN"], "bot")
        m.assert_called_once_with(APP)

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
