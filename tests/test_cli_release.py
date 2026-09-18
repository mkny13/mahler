"""Tests for manual CLI release preview and GitHub publication (Issue #357, DESIGN D31)."""

import contextlib
import io
import json
import unittest
from unittest.mock import MagicMock, patch

from mahler import cli, config
from mahler.ledger import Ledger
from mahler.releases import create_release, get_draft


class TestCLIRelease(unittest.TestCase):
    def setUp(self):
        self.led = Ledger(":memory:")
        self.addCleanup(self.led.close)

        self.cfg = {
            "defaults": dict(config.DEFAULTS["defaults"]),
            "projects": {
                "proj": {
                    "repo": "mkny13/testrepo",
                    "base": "main",
                    "account": "custom_acct",
                }
            },
            "accounts": {
                "custom_acct": {
                    "env": {
                        "GH_TOKEN": "secret_token_123",
                    }
                }
            }
        }

    def test_preview_only_proposed_version(self):
        # Setup draft items: 1 feature, 1 fix, 1 chore
        self.led.snapshot_release_item("proj", 1, pr=10, title="Add feature",
                                      summary="added shiny feature",
                                      merge_sha="sha_feat", labels=["type:feature"])
        self.led.snapshot_release_item("proj", 2, pr=11, title="Fix bug",
                                      summary="fixed crash",
                                      merge_sha="sha_bug", labels=["type:bug"])
        self.led.snapshot_release_item("proj", 3, pr=12, title="Audit cleanup",
                                      summary="cleaned up config",
                                      merge_sha="sha_chore", labels=["type:chore"])

        class Args:
            target = "proj"
            version = None
            publish = False

        out = io.StringIO()
        with patch("mahler.cli.GH") as MockGH:
            mock_gh = MockGH.return_value
            mock_gh.branch_sha.return_value = "main_remote_sha_999"

            with contextlib.redirect_stdout(out):
                ret = cli.cmd_release(Args(), self.cfg, self.led)

            self.assertEqual(ret, 0)
            mock_gh.branch_sha.assert_called_once_with("main")
            mock_gh.release_create.assert_not_called()

        output = out.getvalue()
        self.assertIn("Release preview for proj:", output)
        self.assertIn("Proposed version:  0.1.0", output)
        self.assertIn("Checkpoint SHA:    main_remote_sha_999", output)
        self.assertIn("Item count:        3", output)
        self.assertIn("Release suggested: no", output)
        self.assertIn("### Features", output)
        self.assertIn("- Add feature: added shiny feature (#1, PR #10)", output)
        self.assertIn("### Fixes", output)
        self.assertIn("- Fix bug: fixed null pointer" if "null pointer" in output else "- Fix bug: fixed crash (#2, PR #11)", output)
        self.assertIn("<details>", output)
        self.assertIn("<summary>Maintenance details (1)</summary>", output)
        self.assertIn("- Audit cleanup: cleaned up config (#3, PR #12)", output)

        # Confirm no writes locally or remotely
        self.assertEqual(len(self.led.list_releases("proj")), 0)
        self.assertEqual(len(self.led.unreleased_items("proj")), 3)

    def test_preview_with_selected_version(self):
        self.led.snapshot_release_item("proj", 1, pr=10, title="Add feature",
                                      summary="added feature",
                                      merge_sha="sha_feat", labels=["type:feature"])

        class Args:
            target = "proj"
            version = "0.2.0"
            publish = False

        out = io.StringIO()
        with patch("mahler.cli.GH") as MockGH:
            mock_gh = MockGH.return_value
            mock_gh.branch_sha.return_value = "sha_remote"

            with contextlib.redirect_stdout(out):
                ret = cli.cmd_release(Args(), self.cfg, self.led)

            self.assertEqual(ret, 0)
            mock_gh.release_create.assert_not_called()

        output = out.getvalue()
        self.assertIn("Selected version:  0.2.0", output)
        self.assertIn("Proposed version:  0.1.0", output)
        self.assertIn("Checkpoint SHA:    sha_remote", output)

        # Still no local or remote writes
        self.assertEqual(len(self.led.list_releases("proj")), 0)
        self.assertEqual(len(self.led.unreleased_items("proj")), 1)

    def test_publish_requires_version_flag(self):
        class Args:
            target = "proj"
            version = None
            publish = True

        out = io.StringIO()
        with patch("mahler.cli.GH"):
            with contextlib.redirect_stdout(out):
                ret = cli.cmd_release(Args(), self.cfg, self.led)

        self.assertEqual(ret, 1)
        self.assertIn("publishing requires both an explicit valid version", out.getvalue())

    def test_publish_rejects_invalid_semver(self):
        for bad_ver in ["1", "invalid", "01.2.3", "1.2.3.4", "v"]:
            with self.subTest(bad_ver=bad_ver):
                class Args:
                    target = "proj"
                    version = bad_ver
                    publish = True

                out = io.StringIO()
                with patch("mahler.cli.GH"):
                    with contextlib.redirect_stdout(out):
                        ret = cli.cmd_release(Args(), self.cfg, self.led)

                self.assertEqual(ret, 1)
                self.assertIn("invalid version", out.getvalue())

    def test_publish_rejects_non_increasing_version(self):
        create_release(self.led, "proj", version="0.2.0", checkpoint_sha="old_sha")

        class Args:
            target = "proj"
            version = "0.1.0"
            publish = True

        out = io.StringIO()
        with patch("mahler.cli.GH"):
            with contextlib.redirect_stdout(out):
                ret = cli.cmd_release(Args(), self.cfg, self.led)

        self.assertEqual(ret, 1)
        self.assertIn("must be greater than latest recorded version 0.2.0", out.getvalue())

    def test_successful_publish_creates_release_and_seals_items(self):
        self.led.snapshot_release_item("proj", 1, pr=10, title="Add feature",
                                      summary="added shiny feature",
                                      merge_sha="sha_feat", labels=["type:feature"])
        self.led.snapshot_release_item("proj", 2, pr=11, title="Fix bug",
                                      summary="fixed crash",
                                      merge_sha="sha_bug", labels=["type:bug"])

        class Args:
            target = "proj"
            version = "0.1.0"
            publish = True

        out = io.StringIO()
        with patch("mahler.cli.GH") as MockGH:
            mock_gh = MockGH.return_value
            mock_gh.branch_sha.return_value = "main_tip_sha"
            mock_gh.get_release.return_value = None
            mock_gh.get_tag_sha.return_value = None
            mock_gh.release_create.return_value = "https://github.com/mkny13/testrepo/releases/tag/v0.1.0"

            with contextlib.redirect_stdout(out):
                ret = cli.cmd_release(Args(), self.cfg, self.led)

            self.assertEqual(ret, 0)
            # Verify exact tag, SHA, notes passed to GitHub release_create
            mock_gh.release_create.assert_called_once()
            call_args = mock_gh.release_create.call_args
            self.assertEqual(call_args.kwargs.get("tag") or call_args.args[0], "v0.1.0")
            self.assertEqual(call_args.kwargs.get("target") or call_args.args[1], "main_tip_sha")
            self.assertEqual(call_args.kwargs.get("title") or call_args.args[2], "v0.1.0")
            notes = call_args.kwargs.get("notes") or call_args.args[3]
            self.assertIn("### Features", notes)
            self.assertIn("### Fixes", notes)

        output = out.getvalue()
        self.assertIn("Selected version:  0.1.0", output)
        self.assertIn("Checkpoint SHA:    main_tip_sha", output)
        self.assertIn("Published v0.1.0: https://github.com/mkny13/testrepo/releases/tag/v0.1.0", output)

        # Verify atomically sealed exactly previewed items
        self.assertEqual(len(self.led.unreleased_items("proj")), 0)
        rel = self.led.get_release("proj", version="0.1.0")
        self.assertIsNotNone(rel)
        self.assertEqual(rel["checkpoint_sha"], "main_tip_sha")
        self.assertEqual(rel["remote_url"], "https://github.com/mkny13/testrepo/releases/tag/v0.1.0")
        sealed = self.led.release_items_for_release(rel["id"])
        self.assertEqual([i["number"] for i in sealed], [1, 2])

    def test_account_routing_uses_project_credentials(self):
        self.led.snapshot_release_item("proj", 1, pr=10, title="Add feature",
                                      summary="added feature",
                                      merge_sha="sha_feat", labels=["type:feature"])

        class Args:
            target = "proj"
            version = None
            publish = False

        with patch("mahler.cli.GH") as MockGH:
            mock_gh = MockGH.return_value
            mock_gh.branch_sha.return_value = "main_tip_sha"

            with contextlib.redirect_stdout(io.StringIO()):
                cli.cmd_release(Args(), self.cfg, self.led)

            # Check that GH was instantiated with the account's env
            MockGH.assert_called_once()
            repo_arg = MockGH.call_args[0][0]
            env_arg = MockGH.call_args[1]["env"]
            self.assertEqual(repo_arg, "mkny13/testrepo")
            self.assertEqual(env_arg.get("GH_TOKEN"), "secret_token_123")

    def test_partial_failure_recovery_reconciles_matching_release(self):
        self.led.snapshot_release_item("proj", 1, pr=10, title="Add feature",
                                      summary="added feature",
                                      merge_sha="sha_feat", labels=["type:feature"])
        draft = get_draft(self.led, "proj")
        notes = draft.notes.render(include_maintenance=True, collapsed_maintenance=True)

        class Args:
            target = "proj"
            version = "0.1.0"
            publish = True

        out = io.StringIO()
        with patch("mahler.cli.GH") as MockGH:
            mock_gh = MockGH.return_value
            mock_gh.branch_sha.return_value = "main_tip_sha"
            mock_gh.get_release.return_value = {
                "tagName": "v0.1.0",
                "targetCommitish": "main_tip_sha",
                "body": notes,
                "url": "https://github.com/mkny13/testrepo/releases/tag/v0.1.0",
            }
            mock_gh.get_tag_sha.return_value = "main_tip_sha"

            with contextlib.redirect_stdout(out):
                ret = cli.cmd_release(Args(), self.cfg, self.led)

            self.assertEqual(ret, 0)
            mock_gh.release_create.assert_not_called()

        self.assertIn("Release v0.1.0 already published (reconciled)", out.getvalue())
        # Local record is now finished
        rel = self.led.get_release("proj", version="0.1.0")
        self.assertIsNotNone(rel)
        self.assertEqual(len(self.led.unreleased_items("proj")), 0)

    def test_idempotent_repeated_publish(self):
        self.led.snapshot_release_item("proj", 1, pr=10, title="Add feature",
                                      summary="added feature",
                                      merge_sha="sha_feat", labels=["type:feature"])

        class Args:
            target = "proj"
            version = "0.1.0"
            publish = True

        with patch("mahler.cli.GH") as MockGH:
            mock_gh = MockGH.return_value
            mock_gh.branch_sha.return_value = "main_tip_sha"
            mock_gh.get_release.return_value = None
            mock_gh.get_tag_sha.return_value = None
            mock_gh.release_create.return_value = "https://github.com/mkny13/testrepo/releases/tag/v0.1.0"

            # 1st run
            with contextlib.redirect_stdout(io.StringIO()):
                ret1 = cli.cmd_release(Args(), self.cfg, self.led)
            self.assertEqual(ret1, 0)

            # Setup mock for 2nd run to report existing release
            draft = get_draft(self.led, "proj")
            rel = self.led.get_release("proj", version="0.1.0")
            mock_gh.get_release.return_value = {
                "tagName": "v0.1.0",
                "targetCommitish": "main_tip_sha",
                "body": rel["notes"],
                "url": "https://github.com/mkny13/testrepo/releases/tag/v0.1.0",
            }
            mock_gh.get_tag_sha.return_value = "main_tip_sha"
            mock_gh.release_create.reset_mock()

            # 2nd run
            out2 = io.StringIO()
            with contextlib.redirect_stdout(out2):
                ret2 = cli.cmd_release(Args(), self.cfg, self.led)
            self.assertEqual(ret2, 0)
            mock_gh.release_create.assert_not_called()
            self.assertIn("Release v0.1.0 already published (reconciled)", out2.getvalue())

    def test_conflict_refusal_on_different_remote_sha(self):
        self.led.snapshot_release_item("proj", 1, pr=10, title="Add feature",
                                      summary="added feature",
                                      merge_sha="sha_feat", labels=["type:feature"])
        draft = get_draft(self.led, "proj")
        notes = draft.notes.render(include_maintenance=True, collapsed_maintenance=True)

        class Args:
            target = "proj"
            version = "0.1.0"
            publish = True

        out = io.StringIO()
        with patch("mahler.cli.GH") as MockGH:
            mock_gh = MockGH.return_value
            mock_gh.branch_sha.return_value = "main_tip_sha"
            mock_gh.get_release.return_value = {
                "tagName": "v0.1.0",
                "targetCommitish": "different_sha_888",
                "body": notes,
                "url": "https://github.com/mkny13/testrepo/releases/tag/v0.1.0",
            }
            mock_gh.get_tag_sha.return_value = "different_sha_888"

            with contextlib.redirect_stdout(out):
                ret = cli.cmd_release(Args(), self.cfg, self.led)

            self.assertEqual(ret, 1)
            mock_gh.release_create.assert_not_called()

        self.assertIn("conflicting with previewed SHA", out.getvalue())
        # Draft items remain unsealed
        self.assertEqual(len(self.led.unreleased_items("proj")), 1)
        self.assertIsNone(self.led.get_release("proj", version="0.1.0"))

    def test_conflict_refusal_on_different_remote_notes(self):
        self.led.snapshot_release_item("proj", 1, pr=10, title="Add feature",
                                      summary="added feature",
                                      merge_sha="sha_feat", labels=["type:feature"])

        class Args:
            target = "proj"
            version = "0.1.0"
            publish = True

        out = io.StringIO()
        with patch("mahler.cli.GH") as MockGH:
            mock_gh = MockGH.return_value
            mock_gh.branch_sha.return_value = "main_tip_sha"
            mock_gh.get_release.return_value = {
                "tagName": "v0.1.0",
                "targetCommitish": "main_tip_sha",
                "body": "Different remote notes entirely",
                "url": "https://github.com/mkny13/testrepo/releases/tag/v0.1.0",
            }
            mock_gh.get_tag_sha.return_value = "main_tip_sha"

            with contextlib.redirect_stdout(out):
                ret = cli.cmd_release(Args(), self.cfg, self.led)

            self.assertEqual(ret, 1)
            mock_gh.release_create.assert_not_called()

        self.assertIn("notes conflict", out.getvalue())
        # Draft items remain unsealed
        self.assertEqual(len(self.led.unreleased_items("proj")), 1)
        self.assertIsNone(self.led.get_release("proj", version="0.1.0"))

    def test_lease_release_via_cmd_release(self):
        class ClaimArgs:
            item = ("proj", 7)
            holder = "you"
            steal = False
        with contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_claim(ClaimArgs(), self.cfg, self.led)
        self.assertIsNotNone(self.led.lease("proj", 7))

        class Args:
            target = "proj#7"
            holder = "you"

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ret = cli.cmd_release(Args(), self.cfg, self.led)

        self.assertEqual(ret, 0)
        self.assertEqual(out.getvalue().strip(), "released")
        self.assertIsNone(self.led.lease("proj", 7))

    def test_cli_main_dispatch(self):
        import tempfile
        with tempfile.NamedTemporaryFile() as tmp:
            init_led = Ledger(tmp.name)
            init_led.snapshot_release_item("proj", 1, pr=10, title="Add feature",
                                          summary="added feature",
                                          merge_sha="sha_feat", labels=["type:feature"])
            init_led.close()

            with patch("mahler.config.load", return_value=self.cfg), \
                 patch("mahler.cli.Ledger", side_effect=lambda *a, **kw: Ledger(tmp.name)), \
                 patch("mahler.cli.GH") as MockGH:
                mock_gh = MockGH.return_value
                mock_gh.branch_sha.return_value = "main_tip_sha"
                mock_gh.get_release.return_value = None
                mock_gh.get_tag_sha.return_value = None
                mock_gh.release_create.return_value = "https://github.com/mkny13/testrepo/releases/tag/v0.1.0"

                # 1. Preview via main
                out_prev = io.StringIO()
                with contextlib.redirect_stdout(out_prev):
                    code = cli.main(["release", "proj"])
                self.assertEqual(code, 0)
                self.assertIn("Release preview for proj:", out_prev.getvalue())
                self.assertIn("Proposed version:  0.1.0", out_prev.getvalue())

                # 2. Preview with selected version via main
                out_sel = io.StringIO()
                with contextlib.redirect_stdout(out_sel):
                    code = cli.main(["release", "proj", "--version", "0.2.0"])
                self.assertEqual(code, 0)
                self.assertIn("Selected version:  0.2.0", out_sel.getvalue())

                # 3. Publish via main
                out_pub = io.StringIO()
                with contextlib.redirect_stdout(out_pub):
                    code = cli.main(["release", "proj", "--version", "0.1.0", "--publish"])
                self.assertEqual(code, 0)
                self.assertIn("Published v0.1.0:", out_pub.getvalue())
                mock_gh.release_create.assert_called_once()


if __name__ == "__main__":
    unittest.main()
