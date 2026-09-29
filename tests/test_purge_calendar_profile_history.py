"""Synthetic, isolated coverage for the Chromium-history purge (mahler#589)."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "bin" / "purge-calendar-profile-history"
PROHIBITED = ".calendar-chrome3/"
SECRET = "synthetic-cookie-value-never-print"


class PurgeHistoryTest(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self.env = os.environ.copy()
        self.env.update(
            {
                "XDG_CONFIG_HOME": str(self.root / "xdg"),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "PATH": str(self.fake_bin) + os.pathsep + os.environ["PATH"],
            }
        )
        self._install_filter_repo()

    def tearDown(self):
        self._temp.cleanup()

    def _run(self, args, cwd=None, check=True, env=None):
        result = subprocess.run(
            [str(a) for a in args],
            cwd=cwd,
            env=env or self.env,
            text=True,
            capture_output=True,
        )
        if check and result.returncode:
            self.fail(
                f"command failed ({result.returncode}): {' '.join(map(str, args))}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def _git(self, cwd, *args, check=True):
        return self._run(["git", *args], cwd=cwd, check=check)

    def _install_filter_repo(self):
        """Install a test-only shim with upstream's hexadecimal --version form."""
        shim = self.fake_bin / "git-filter-repo"
        shim.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import os
                import subprocess
                import sys

                if sys.argv[1:] == ["--version"]:
                    print("0123456789ab")
                    raise SystemExit(0)
                if sys.argv[1:] != [
                    "--path", ".calendar-chrome3/", "--invert-paths", "--force"
                ]:
                    print("unexpected arguments", file=sys.stderr)
                    raise SystemExit(2)
                if os.environ.get("FILTER_REPO_TEST_NOOP") == "1":
                    raise SystemExit(0)

                env = os.environ.copy()
                env["FILTER_BRANCH_SQUELCH_WARNING"] = "1"
                command = [
                    "git", "filter-branch", "--force", "--index-filter",
                    "git rm -r --cached --ignore-unmatch -- .calendar-chrome3/",
                    "--prune-empty", "--tag-name-filter", "cat", "--", "--all",
                ]
                result = subprocess.run(command, env=env)
                if result.returncode:
                    raise SystemExit(result.returncode)
                refs = subprocess.run(
                    ["git", "for-each-ref", "--format=%(refname)", "refs/original"],
                    text=True, capture_output=True, check=True,
                ).stdout.splitlines()
                for ref in refs:
                    subprocess.run(["git", "update-ref", "-d", ref], check=True)
                subprocess.run(
                    ["git", "reflog", "expire", "--expire=now", "--all"], check=True
                )
                subprocess.run(["git", "gc", "--prune=now"], check=True)
                """
            ),
            encoding="utf-8",
        )
        shim.chmod(0o755)

    def _write(self, root, relative, contents):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    def _make_remote(self, name="remote.git"):
        work = self.root / (name + "-work")
        work.mkdir()
        self._git(work, "init", "-b", "main")
        self._git(work, "config", "user.name", "Synthetic Test")
        self._git(work, "config", "user.email", "synthetic@example.invalid")

        self._write(work, "README.md", "unrelated root history\n")
        self._git(work, "add", "README.md")
        self._git(work, "commit", "-m", "clean base")
        clean_sha = self._git(work, "rev-parse", "HEAD").stdout.strip()
        self._git(work, "tag", "clean")

        self._write(work, ".calendar-chrome3/Default/Cookies", SECRET + "\n")
        self._write(work, ".calendar-chrome30/keep.txt", "similar path stays\n")
        self._git(work, "add", ".")
        self._git(work, "commit", "-m", "synthetic exposed profile")
        self._git(work, "tag", "exposed")

        self._git(work, "switch", "-c", "feature")
        self._write(work, "feature.txt", "feature survives\n")
        self._git(work, "add", "feature.txt")
        self._git(work, "commit", "-m", "feature history")

        self._git(work, "switch", "main")
        self._write(work, "main.txt", "main survives\n")
        self._git(work, "add", "main.txt")
        self._git(work, "commit", "-m", "main history")

        remote = self.root / name
        self._git(self.root, "clone", "--bare", str(work), str(remote))
        return remote, clean_sha

    def _tool(self, *args, env=None):
        return self._run([TOOL, *args], check=False, env=env)

    def _refs(self, repo):
        output = self._git(
            repo,
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            "refs/heads",
            "refs/tags",
        ).stdout
        return dict(line.split() for line in output.splitlines())

    def _path_hits(self, repo):
        output = self._git(
            repo, "rev-list", "--count", "--all", "--", PROHIBITED
        ).stdout
        return int(output.strip())

    def test_rewrite_accepts_hash_version_and_preserves_unrelated_history(self):
        remote, clean_sha = self._make_remote()
        original_refs = set(self._refs(remote))
        mirror = self.root / "rewrite.git"

        result = self._tool("--work", mirror, "--from", remote)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("git-filter-repo 0123456789ab: ok", result.stdout)
        self.assertNotIn(SECRET, result.stdout + result.stderr)
        self.assertEqual(self._path_hits(mirror), 0)
        self.assertEqual(set(self._refs(mirror)), original_refs)
        self.assertEqual(self._refs(mirror)["refs/tags/clean"], clean_sha)
        self.assertEqual(
            self._git(mirror, "show", "refs/heads/main:README.md").stdout,
            "unrelated root history\n",
        )
        self.assertEqual(
            self._git(
                mirror, "show", "refs/heads/main:.calendar-chrome30/keep.txt"
            ).stdout,
            "similar path stays\n",
        )
        self.assertEqual(
            self._git(mirror, "show", "refs/heads/feature:feature.txt").stdout,
            "feature survives\n",
        )

        state = json.loads((mirror / "purge-state.json").read_text())
        self.assertTrue(state["verified"])
        bundle = Path(str(mirror) + ".purge-backup.bundle")
        self.assertTrue(bundle.is_file())
        self.assertEqual(bundle.stat().st_mode & 0o777, 0o600)
        recovered = self.root / "recovered.git"
        self._git(self.root, "clone", "--mirror", str(bundle), str(recovered))
        self.assertGreater(self._path_hits(recovered), 0)

        pushed = self._tool(
            "--push", "--confirmed", "--work", mirror, "--to", remote
        )
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertEqual(self._path_hits(remote), 0)

    def test_dry_run_only_enumerates_and_creates_no_recovery_artifacts(self):
        remote, _ = self._make_remote()
        mirror = self.root / "dry.git"
        result = self._tool("--dry-run", "--work", mirror, "--from", remote)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreater(self._path_hits(mirror), 0)
        self.assertFalse((mirror / "purge-state.json").exists())
        self.assertFalse(Path(str(mirror) + ".purge-backup.bundle").exists())

    def test_verification_failure_blocks_a_noop_rewrite(self):
        remote, _ = self._make_remote()
        mirror = self.root / "unclean.git"
        env = self.env.copy()
        env["FILTER_REPO_TEST_NOOP"] = "1"
        result = self._tool("--work", mirror, "--from", remote, env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("verification failed", result.stderr)
        self.assertGreater(self._path_hits(mirror), 0)
        state = json.loads((mirror / "purge-state.json").read_text())
        self.assertFalse(state["verified"])

    def test_force_push_requires_confirmation_and_honors_ref_leases(self):
        remote, _ = self._make_remote()
        mirror = self.root / "leased.git"
        rewrite = self._tool("--work", mirror, "--from", remote)
        self.assertEqual(rewrite.returncode, 0, rewrite.stderr)

        gated = self._tool("--push", "--work", mirror, "--to", remote)
        self.assertEqual(gated.returncode, 2)
        self.assertIn("needs --confirmed", gated.stderr)

        collaborator = self.root / "collaborator"
        self._git(self.root, "clone", str(remote), str(collaborator))
        self._git(collaborator, "config", "user.name", "Collaborator")
        self._git(collaborator, "config", "user.email", "collab@example.invalid")
        self._write(collaborator, "collaborator.txt", "must not be lost\n")
        self._git(collaborator, "add", "collaborator.txt")
        self._git(collaborator, "commit", "-m", "concurrent collaborator commit")
        self._git(collaborator, "push", "origin", "main")
        refs_before = self._refs(remote)

        rejected = self._tool(
            "--push", "--confirmed", "--work", mirror, "--to", remote
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("PUSH REJECTED", rejected.stderr)
        self.assertEqual(self._refs(remote), refs_before)
        self.assertEqual(
            self._git(remote, "show", "refs/heads/main:collaborator.txt").stdout,
            "must not be lost\n",
        )

    def test_preconditions_refuse_existing_worktree_and_unexpected_remote(self):
        remote, _ = self._make_remote()
        existing = self.root / "already-there"
        existing.mkdir()
        result = self._tool("--work", existing, "--from", remote)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must name a path that does not exist", result.stderr)

        ordinary = self.root / "ordinary"
        self._git(self.root, "clone", str(remote), str(ordinary))
        result = self._tool(
            "--push", "--confirmed", "--work", ordinary, "--to", remote
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing ordinary checkout", result.stderr)

        hostile = "https://alice:embedded-token@example.invalid/not-mahler.git"
        result = self._tool("--work", self.root / "never-created", "--from", hostile)
        self.assertEqual(result.returncode, 2)
        combined = result.stdout + result.stderr
        self.assertNotIn("alice", combined)
        self.assertNotIn("embedded-token", combined)
        self.assertIn("https://example.invalid/not-mahler.git", combined)


if __name__ == "__main__":
    unittest.main()
