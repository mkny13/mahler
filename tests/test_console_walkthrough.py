"""Smoke test for the manual console walkthrough (mahler#596).

The walkthrough is manually triggered and never shipped, so it gets a
contract test rather than full coverage: an unknown platform fails fast
before any server or agent starts, and a known one launches with the JSON
output flags stripped and the fixture data in place (which also pins the
raw INSERTs to the ledger schema).
"""

import unittest
from unittest import mock

from mahler import console_walkthrough

from test_serve import make_cfg


class WalkthroughTests(unittest.TestCase):
    def test_unknown_platform_fails_fast(self):
        with mock.patch("mahler.console_walkthrough.subprocess.run",
                        side_effect=AssertionError("no agent should launch")):
            self.assertEqual(console_walkthrough.run({"platforms": {}}, "nope"), 1)

    def test_known_platform_launches_without_json_flags(self):
        cfg = make_cfg()
        cfg.setdefault("platforms", {})["claude"] = {"kind": "claude"}
        argv = ["fake-agent", "--output-format", "stream-json", "--verbose", "--json"]
        with mock.patch("mahler.console_walkthrough.platforms.argv_for",
                        return_value=argv) as argv_for, \
             mock.patch("mahler.console_walkthrough.subprocess.run") as run:
            rc = console_walkthrough.run(cfg, "claude")
        self.assertEqual(rc, 0)
        self.assertEqual(argv_for.call_args.args[0], {"kind": "claude"})
        launched = run.call_args.args[0]
        self.assertEqual(launched, ["fake-agent", "--verbose"])
        # The prompt must carry the fixture console's URL.
        self.assertIn("--verbose", launched)
        prompt = argv_for.call_args.args[1]
        self.assertIn("http://127.0.0.1:", prompt)
        self.assertIn("What changed", prompt)
        self.assertIn("report a failure", prompt)

    def test_launch_uses_platform_account_environment(self):
        cfg = make_cfg()
        cfg.setdefault("accounts", {})["work"] = {
            "env": {"CODEX_HOME": "/isolated/work-codex"},
        }
        cfg.setdefault("platforms", {})["work-codex"] = {
            "kind": "codex", "account": "work",
        }
        with mock.patch.dict(console_walkthrough.config.os.environ, {
                "CODEX_HOME": "/personal/codex",
                "CLAUDE_CONFIG_DIR": "/personal/claude",
        }), mock.patch("mahler.console_walkthrough.platforms.argv_for",
                       return_value=["fake-agent"]), \
             mock.patch("mahler.console_walkthrough.subprocess.run") as run:
            rc = console_walkthrough.run(cfg, "work-codex")

        self.assertEqual(rc, 0)
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["CODEX_HOME"], "/isolated/work-codex")
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)
