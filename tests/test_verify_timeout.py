"""Tests for per-project verify_timeout configuration and fallback verification.

Ensures that:
1. `config.DEFAULTS["defaults"]` includes `verify_timeout` (default 300 seconds),
   distinct from `verify_timeout_minutes` (CI pending timeout).
2. Per-project overrides for `verify_timeout` are honored by `project_policy`.
3. `finalize._try_verify_fallback` passes the configured `verify_timeout`
   to `runner.verify_in_worktree` rather than a hardcoded 120s.
4. Direct callers of `runner.verify_in_worktree` keep a default fallback timeout of 120s.
"""

import copy
import inspect
import os
import tempfile
import unittest
from unittest import mock

from mahler import config, finalize, runner


class VerifyTimeoutConfigTests(unittest.TestCase):
    def test_default_verify_timeout_in_defaults(self):
        """DEFAULTS contains a per-project verify_timeout in seconds, default 300."""
        self.assertIn("verify_timeout", config.DEFAULTS["defaults"])
        self.assertEqual(config.DEFAULTS["defaults"]["verify_timeout"], 300)

        # distinct from verify_timeout_minutes (which guards CI pending)
        self.assertIn("verify_timeout_minutes", config.DEFAULTS["defaults"])
        self.assertEqual(config.DEFAULTS["defaults"]["verify_timeout_minutes"], 60)

    def test_project_policy_inherits_default_verify_timeout(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["projects"]["sample"] = {"repo": "org/sample"}
        pol = config.project_policy(cfg, "sample")
        self.assertEqual(pol.get("verify_timeout"), 300)

    def test_project_policy_honors_custom_verify_timeout(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["projects"]["custom"] = {"repo": "org/custom", "verify_timeout": 450}
        pol = config.project_policy(cfg, "custom")
        self.assertEqual(pol.get("verify_timeout"), 450)

    def test_runner_verify_in_worktree_default_parameter(self):
        """Direct callers keep timeout=120 as default fallback."""
        sig = inspect.signature(runner.verify_in_worktree)
        self.assertEqual(sig.parameters["timeout"].default, 120)

    def test_runner_verify_in_worktree_forwards_custom_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("subprocess.run") as mock_run:
                mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
                ok = runner.verify_in_worktree(tmp, "echo hi", timeout=300)
                self.assertTrue(ok)
                self.assertEqual(mock_run.call_args.kwargs.get("timeout"), 300)

    def test_try_verify_fallback_applies_configured_timeout(self):
        ctx = mock.Mock()
        ctx.led = mock.Mock()
        run = {
            "id": 1,
            "project": "proj",
            "number": 42,
            "worktree": "/tmp/nonexistent-wt",
            "log_path": "/tmp/run.log",
        }
        item = {"branch": "mahler/42-fix"}
        saved = {"ref": "mahler/snapshot/42-run1", "ahead": 1}

        # Case 1: Custom timeout (e.g. 500s)
        pol_custom = {
            "base": "main",
            "verify": "pytest",
            "verify_timeout": 500,
        }
        with mock.patch.object(runner, "commits_ahead", return_value=1), \
                mock.patch.object(runner, "verify_in_worktree", return_value=True) as mock_verify:
            ok = finalize._try_verify_fallback(ctx, run, pol_custom, saved, item)
            self.assertTrue(ok)
            mock_verify.assert_called_once()
            self.assertEqual(mock_verify.call_args.kwargs.get("timeout"), 500)

        # Case 2: Config policy with default timeout (300s)
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["projects"]["proj"] = {"repo": "org/proj", "verify": "pytest"}
        pol_default = config.project_policy(cfg, "proj")
        with mock.patch.object(runner, "commits_ahead", return_value=1), \
                mock.patch.object(runner, "verify_in_worktree", return_value=True) as mock_verify:
            ok = finalize._try_verify_fallback(ctx, run, pol_default, saved, item)
            self.assertTrue(ok)
            mock_verify.assert_called_once()
            self.assertEqual(mock_verify.call_args.kwargs.get("timeout"), 300)


if __name__ == "__main__":
    unittest.main()
