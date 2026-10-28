"""PAT provider for work projects (D47, #1265): fake token files only."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mahler import config, github_pat, redact, runner
from mahler.github_pat import PatError

TOKEN = "github_pat_" + "Z" * 40
REPO = "makastel_ncstate/sit-stand-walk"


class PatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(redact._REGISTERED.clear)

    def token_file(self, content=TOKEN + "\n", mode=0o600, name="t"):
        p = os.path.join(self.d, name)
        Path(p).write_text(content)
        os.chmod(p, mode)
        return p

    def cfg(self, path, repository=REPO, project_extra=None, require=True):
        proj = {"repo": REPO, "path": self.d, "github_pat": "work",
                "require_scoped_identity": require, **(project_extra or {})}
        prov = {"token_file": path}
        if repository:
            prov["repository"] = repository
        return {"platforms": {"codex": {"kind": "codex"}}, "accounts": {},
                "github_pats": {"work": prov}, "projects": {"sws": proj}}

    def env(self, cfg, ambient=None):
        pol = {**cfg["projects"]["sws"], "name": "sws", "link": [], "rules": "",
               "run_timeout_minutes": 60}
        ctx = SimpleNamespace(cfg=cfg, policy=lambda p: pol)
        with mock.patch.object(config, "RUNS_DIR", os.path.join(self.d, "runs")), \
                mock.patch.object(runner, "fence_hooks", return_value="/h"), \
                mock.patch.dict(os.environ, ambient or {"PATH": "/bin"}, clear=True):
            return runner.run_env(ctx, "sws", 1, "codex", 5, 1)

    def test_valid_token_is_only_the_run_gh_identity(self):
        cfg = self.cfg(self.token_file())
        config.validate_github_app(cfg)
        env = self.env(cfg, {"PATH": "/bin", "GH_TOKEN": "ambient", "GITHUB_TOKEN": "amb2"})
        self.assertEqual(env["GH_TOKEN"], TOKEN)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertEqual(env["GH_CONFIG_DIR"], os.path.join(self.d, "runs", "5", "gh"))
        self.assertEqual([v for v in env.values() if TOKEN in v], [TOKEN])

    def test_token_is_registered_for_redaction(self):
        self.env(self.cfg(self.token_file()))
        self.assertNotIn("ZZZZ", redact.redact(f"boom {TOKEN} end"))

    def test_bad_token_files_fail_closed_secret_safe(self):
        d = os.path.join(self.d, "dir"); os.mkdir(d)
        cases = {
            "missing": os.path.join(self.d, "nope"),
            "empty": self.token_file("  \n", name="e"),
            "mode": self.token_file(mode=0o644, name="m"),
            "group": self.token_file(mode=0o640, name="g"),
            "dir": d,
        }
        unreadable = self.token_file(name="u")
        with mock.patch("builtins.open", side_effect=PermissionError):
            with self.assertRaises(PatError) as cm:
                github_pat.read_token(unreadable)
        self.assertNotIn(TOKEN, str(cm.exception))
        for label, path in cases.items():
            with self.subTest(label), self.assertRaises(PatError) as cm:
                self.env(self.cfg(path), {"PATH": "/bin", "GH_TOKEN": "ambient"})
            self.assertIn(path, str(cm.exception))
            self.assertNotIn(TOKEN, str(cm.exception))
        # the wrong-mode file's contents never reach the error
        with self.assertRaises(PatError) as cm:
            github_pat.read_token(cases["mode"])
        self.assertIn("0644", str(cm.exception))
        self.assertNotIn("ZZZ", str(cm.exception))

    def test_failure_never_falls_back_to_ambient_or_app(self):
        cfg = self.cfg(os.path.join(self.d, "nope"))
        cfg["github_app"] = {"app_id": 1, "installation_id": 2, "private_key_path": "/k"}
        with mock.patch.object(config, "project_github_app") as app, \
                self.assertRaises(PatError):
            self.env(cfg, {"PATH": "/bin", "GH_TOKEN": "ambient"})
        app.assert_not_called()

    def test_wrong_repo_refused(self):
        cfg = self.cfg(self.token_file(), repository="makastel_ncstate/other")
        with self.assertRaises(PatError):
            config.validate_github_app(cfg)
        with self.assertRaises(PatError):
            self.env(cfg)

    def test_shared_provider_and_dual_binding_refused(self):
        cfg = self.cfg(self.token_file())
        cfg["projects"]["other"] = {**cfg["projects"]["sws"], "repo": "o/other"}
        with self.assertRaises(PatError):
            config.validate_github_app(cfg)
        cfg = self.cfg(self.token_file(), project_extra={"github_provider": "work"})
        with self.assertRaises(PatError):
            config.validate_github_app(cfg)

    def test_config_holds_paths_only(self):
        cfg = self.cfg(self.token_file())
        cfg["github_pats"]["work"]["token"] = TOKEN
        with self.assertRaises(PatError) as cm:
            config.validate_github_app(cfg)
        self.assertNotIn(TOKEN, str(cm.exception))

    def test_shared_token_file_across_providers_refused(self):
        path = self.token_file()
        for bound in (False, True):
            with self.subTest(bound=bound):
                cfg = self.cfg(path)
                cfg["github_pats"]["other"] = {
                    "token_file": path, "repository": "o/other"}
                if bound:
                    cfg["projects"]["other"] = {
                        "repo": "o/other", "github_pat": "other"}
                for validate in (github_pat.validate, config.validate_github_app):
                    with self.subTest(validate=validate.__name__):
                        with self.assertRaises(PatError) as cm:
                            validate(cfg)
                        self.assertIn("token_file", str(cm.exception))
                        self.assertIn("work", str(cm.exception))
                        self.assertIn("other", str(cm.exception))
                        self.assertNotIn(TOKEN, str(cm.exception))

    def test_distinct_token_files_across_repositories_allowed(self):
        cfg = self.cfg(self.token_file())
        cfg["github_pats"]["other"] = {
            "token_file": self.token_file(name="other"), "repository": "o/other"}
        cfg["projects"]["other"] = {"repo": "o/other", "github_pat": "other"}
        self.assertEqual(github_pat.validate(cfg), {"sws", "other"})
        config.validate_github_app(cfg)


if __name__ == "__main__":
    unittest.main()
