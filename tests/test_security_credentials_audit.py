"""Bounded credential audit: synthetic inputs, no network or production state."""
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mahler import backup, config, github_app
from mahler.gh import GHError


class CredentialAuditTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_explicit_unpooled_selection_and_literal_env(self):
        marker = self.root / "must-not-exist"
        env_file = self.root / ".env"
        direct = "postgresql://audit:p%40ss%3A%24%28literal%29@direct.invalid/db?sslmode=require"
        env_file.write_text(
            'DATABASE_URL="postgresql://pool:other@pooled.invalid/db"\n'
            f'export DATABASE_URL_UNPOOLED="{direct}"\n'
            f'IGNORED=$(touch {marker})\n')
        spec = {"name": "audit", "env_file": str(env_file),
                "url_var": "DATABASE_URL_UNPOOLED"}

        def execute(argv, **kw):
            self.assertNotIn(direct, " ".join(argv))
            self.assertNotIn("p@ss:$(literal)", " ".join(argv))
            self.assertFalse(kw.get("shell", False))
            if argv[0] == "pg_dump":
                self.assertEqual(kw["env"]["PGHOST"], "direct.invalid")
                self.assertEqual(kw["env"]["PGPASSWORD"], "p@ss:$(literal)")
                self.assertEqual(kw["env"]["PGSSLMODE"], "require")
                Path(argv[-1]).write_bytes(b"synthetic dump")
            return subprocess.CompletedProcess(argv, 0, "1; TABLE audit\n", "")

        with patch.dict(os.environ, {}, clear=True), \
                patch.object(backup, "tool", side_effect=lambda name, spec: name), \
                patch.object(backup.subprocess, "run", side_effect=execute) as run:
            result = backup.backup_postgres("audit", spec, root=str(self.root / "backups"))
            self.assertEqual(result["entries"], 1)
            self.assertEqual(run.call_count, 2)
            self.assertFalse(marker.exists())
            # A pooled URL must not silently replace a missing explicit direct URL.
            env_file.write_text('DATABASE_URL="postgresql://pool:other@pooled.invalid/db"\n')
            run.reset_mock()
            with self.assertRaisesRegex(backup.BackupError, "DATABASE_URL_UNPOOLED not set"):
                backup.backup_postgres("audit", spec, root=str(self.root / "backups"))
            run.assert_not_called()

    def test_work_env_strips_full_declared_login_set_without_mutation(self):
        base = {name: "synthetic-personal" for name in config.CREDENTIAL_VARS}
        base["PATH"] = "/synthetic/bin"
        original = dict(base)
        cfg = {"accounts": {"work": {"env": {"CODEX_HOME": "/synthetic/work"}}}}
        env = config.run_env(cfg, "work", base)
        self.assertEqual(env, {"PATH": "/synthetic/bin", "CODEX_HOME": "/synthetic/work"})
        self.assertEqual(base, original)
        env["PATH"] = "/changed"
        self.assertEqual(base, original)
        with self.assertRaisesRegex(ValueError, "not defined"):
            config.run_env(cfg, "other-user", base)

    def test_scope_cache_tampering_cannot_fallback_when_exchange_fails(self):
        app = github_app.Installation(1, 2, "/synthetic/key", self.root / "cache")
        permissions = {"contents": "read"}
        response = {"token": "synthetic-scoped-token", "expires_at": "2099-01-01T00:00:00+00:00"}
        with patch.object(app, "_jwt", return_value="synthetic-jwt"), \
                patch.object(github_app.urllib.request, "urlopen",
                             side_effect=lambda *a, **k: io.BytesIO(json.dumps(response).encode())) as http:
            self.assertEqual(app.agent_token("allowed", permissions), response["token"])
            self.assertEqual(json.loads(http.call_args.args[0].data),
                             {"repositories": ["allowed"], "permissions": permissions})
            cache = next((self.root / "cache").glob("*.json"))
            good = json.loads(cache.read_text())
            for changed in ({"cached_repository": "other-user"},
                            {"cached_permissions": {"contents": "write"}}):
                with self.subTest(changed=changed):
                    cache.write_text(json.dumps({**good, **changed}))
                    http.side_effect = OSError("synthetic transport failure")
                    with self.assertRaisesRegex(GHError, "exchange failed"):
                        app.agent_token("allowed", permissions)
                    self.assertEqual(json.loads(cache.read_text()), {**good, **changed})
            self.assertEqual(http.call_count, 3)

    def test_private_write_replaces_leaf_symlink_without_overwriting_target(self):
        old_umask = os.umask(0)
        self.addCleanup(os.umask, old_umask)
        with patch.object(config, "STATE", str(self.root)):
            path = self.root / "state" / "config.toml"
            config._write_private(str(path), "normal")
            self.assertEqual(path.read_text(), "normal")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            target = self.root / "unrelated"
            target.write_text("leave intact")
            path.unlink()
            path.symlink_to(target)
            path.parent.chmod(0o777)
            config._write_private(str(path), "replacement")
            self.assertFalse(path.is_symlink())
            self.assertEqual(target.read_text(), "leave intact")
            self.assertEqual(path.read_text(), "replacement")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(list(path.parent.iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
