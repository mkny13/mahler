"""Synthetic failure combination and missing-evidence refusal tests for dependency vetting audit (#1632).

Exercises failure combinations among the 5 scanner cases:
1. Malformed registry response
2. Network timeout
3. Unresolved Python dependency range
4. Advisory severity requiring a block
5. Unsupported manifest/lock format
along with caller refusal to proceed when gate evidence is missing or disputed.
"""

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.error import HTTPError, URLError

from mahler import config, dependency_vetting, platforms, scheduler, ship, waits
from mahler.ledger import Ledger, iso
from tests.test_dependency_vetting import NOW, Response
from tests.test_ship import FakeGH


class SecurityDependencyAuditTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["projects"]["x"] = {"path": self.tmp, "repo": "x/y"}
        self.led = Ledger(":memory:", clock=lambda: NOW)
        self.addCleanup(self.led.close)
        later = iso(NOW + timedelta(hours=2))
        for name in ("agy-claude", "agy-gemini", "claude"):
            self.led.record_usage(name, "5h", 10, later)
            self.led.record_usage(name, "weekly", 10, later)
        self.led.upsert_item(
            "x", 5, state="verifying", priority=2,
            title="Dependency audit test item",
            branch="mahler/5-dependency-audit",
            summary="dependency audit test item",
            sorted_at=iso(NOW),
        )
        self.ctx = scheduler.Ctx(self.cfg, self.led, dry_run=False)
        self.gh = FakeGH()
        avail = mock.patch.object(platforms, "available", return_value=True)
        avail.start()
        self.addCleanup(avail.stop)

    def item(self, n=5):
        return self.led.item("x", n)

    def wait_record(self, n=5):
        raw = self.led.get_kv(f"wait:x#{n}")
        return json.loads(raw) if raw else {}

    def test_unresolved_python_range_and_unsupported_lock_combination_fails_closed(self):
        """Case 3 (unresolved range) + Case 5 (unsupported lock format).

        A PR introduces both an unpinned Python range (like sdk_runner's
        claude-agent-sdk>=0.2.165 without a lockfile) and an unsupported lock
        format (yarn.lock). Verifies:
        1. changed_dependencies fails closed on the unsupported lock format before
           resolving versions or invoking any registry network opener.
        2. In isolation without the unsupported lockfile, the unpinned range fails
           closed in vet() with VettingError citing unresolved exact version.
        3. Caller (ship._vet_pr_dependencies) refuses to merge, does not call urlopen,
           and holds the PR with cause 'dependency_vetting'.
        """
        before = {}
        after_combined = {
            "pyproject.toml": '[project]\ndependencies = ["claude-agent-sdk>=0.2.165"]\n',
            "yarn.lock": "# yarn lockfile v1\npackage@^1.0.0:\n  version \"1.0.0\"\n",
        }

        # 1. Verification of changed_dependencies on combined diff:
        with self.assertRaisesRegex(
            dependency_vetting.VettingError,
            r"unsupported dependency lockfile change: yarn\.lock",
        ):
            dependency_vetting.changed_dependencies(before, after_combined)

        # 2. Integration with ship caller on combined diff:
        self.led.upsert_item("x", 5, pr=88)
        mock_opener = mock.Mock()
        with mock.patch.object(self.gh, "dependency_files", return_value=(before, after_combined)), \
             mock.patch.object(ship.dependency_vetting, "urlopen", side_effect=mock_opener):
            allowed = ship._vet_pr_dependencies(
                self.ctx, "x", self.item(), 88, self.gh, self.gh.pr_view(88)
            )

        self.assertFalse(allowed, "Conductor caller must refuse merge on unsupported lockfile")
        mock_opener.assert_not_called()
        self.assertEqual(self.gh.merged, [], "PR must not be merged")
        rec = self.wait_record()
        self.assertEqual(rec.get("cause"), "dependency_vetting")
        self.assertIn("unsupported lockfile yarn.lock", rec.get("reason", ""))

        # 3. Isolated unpinned range without unsupported lock:
        after_range_only = {
            "pyproject.toml": '[project]\ndependencies = ["claude-agent-sdk>=0.2.165"]\n',
        }
        with self.assertRaisesRegex(
            dependency_vetting.VettingError,
            r"unresolved exact version for pypi claude-agent-sdk: missing",
        ):
            dependency_vetting.vet(before, after_range_only, now=NOW, opener=mock_opener)
        mock_opener.assert_not_called()

    def test_malformed_registry_response_and_caller_refusal(self):
        """Case 1 (malformed registry response) plus caller refusal."""
        before = {}
        after = {
            "pyproject.toml": '[project]\ndependencies = ["pkg-beta==2.0.0"]\n',
            "uv.lock": 'version = 1\n\n[[package]]\nname = "pkg-beta"\nversion = "2.0.0"\n',
        }

        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            if "pkg-beta" in request.full_url:
                return Response("<html>502 Bad Gateway</html>", raw=True)
            raise AssertionError(f"Unexpected request URL: {request.full_url}")

        with self.assertRaises(dependency_vetting.VettingError) as cm:
            dependency_vetting.vet(before, after, now=NOW, opener=opener)
        self.assertIn("lookup failed", str(cm.exception))
        self.assertEqual(calls, ["https://pypi.org/pypi/pkg-beta/json"])

        self.led.upsert_item("x", 5, pr=88)
        with mock.patch.object(self.gh, "dependency_files", return_value=(before, after)), \
             mock.patch.object(ship.dependency_vetting, "urlopen", side_effect=opener):
            allowed = ship._vet_pr_dependencies(
                self.ctx, "x", self.item(), 88, self.gh, self.gh.pr_view(88)
            )
        self.assertFalse(allowed, "Conductor must refuse merge when registry/timeout failures occur")
        self.assertEqual(self.gh.merged, [], "PR must remain unmerged")
        rec = self.wait_record()
        self.assertEqual(rec.get("cause"), "dependency_vetting")
        self.assertIn("lookup failed", rec.get("reason", ""))

    def test_network_timeout_and_caller_refusal(self):
        """Case 2 (network timeout) plus caller refusal."""
        before = {}
        after = {
            "pyproject.toml": '[project]\ndependencies = ["pkg-alpha==1.0.0"]\n',
            "uv.lock": 'version = 1\n\n[[package]]\nname = "pkg-alpha"\nversion = "1.0.0"\n',
        }
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            raise TimeoutError("connection timed out to pypi.org")

        with self.assertRaises(dependency_vetting.VettingError) as cm:
            dependency_vetting.vet(before, after, now=NOW, opener=opener)
        self.assertIn("lookup failed", str(cm.exception))
        self.assertEqual(len(calls), 2, "transport retries must both reach the opener")

        self.led.upsert_item("x", 5, pr=88)
        with mock.patch.object(self.gh, "dependency_files", return_value=(before, after)), \
             mock.patch.object(ship.dependency_vetting, "urlopen", side_effect=opener):
            allowed = ship._vet_pr_dependencies(
                self.ctx, "x", self.item(), 88, self.gh, self.gh.pr_view(88)
            )
        self.assertFalse(allowed)
        self.assertEqual(self.gh.merged, [])

    def test_cargo_only_change_is_a_documented_gate_bypass(self):
        """Cargo.toml/Cargo.lock are outside relevant_path and return no evidence.

        This is a characterization of the concrete unsupported-format gap. It is
        not a clean-result contract: the synthetic Cargo-only change must remain
        visible as unsupported coverage rather than be interpreted as scanned.
        """
        before = {"Cargo.toml": '[package]\nname = "demo"\n'}
        after = {
            "Cargo.toml": '[package]\nname = "demo"\nversion = "2.0.0"\n',
            "Cargo.lock": 'version = 3\n[[package]]\nname = "demo"\nversion = "2.0.0"\n',
        }
        for path in after:
            self.assertFalse(
                dependency_vetting.relevant_path(path),
                f"{path} must remain visibly outside scanner relevant_path",
            )
        self.assertEqual(
            dependency_vetting.changed_dependencies(before, after),
            [],
            "Current implementation returns no findings for unsupported Cargo formats",
        )

    def test_advisory_block_and_caller_missing_evidence_refusal(self):
        """Case 4 (advisory severity requiring block) + Missing Evidence Refusal.

        Verifies:
        1. When OSV returns a CRITICAL advisory for an exact-pinned dependency,
           vet() marks finding.blocked=True and returns a blocking reason.
        2. ship._vet_pr_dependencies halts merge on advisory block.
        3. When adversarial review findings state conductor dependency-vetting gate
           evidence is missing or disputed, ship._review_triggered_fix refuses to
           launch a code fix agent, holds the PR with kind='evidence', and preserves
           caller refusal safety invariants.
        """
        # Part 1: Advisory Block
        before = {}
        after = {
            "pyproject.toml": '[project]\ndependencies = ["vulnerable-tool==1.2.0"]\n',
            "uv.lock": 'version = 1\n\n[[package]]\nname = "vulnerable-tool"\nversion = "1.2.0"\n',
        }

        def opener(request, timeout):
            if "pypi.org" in request.full_url:
                return Response({
                    "info": {"version": "1.2.0", "author": "Test Author"},
                    "releases": {"1.2.0": [{"upload_time_iso_8601": "2020-01-01T00:00:00Z"}]},
                })
            if request.full_url == "https://api.osv.dev/v1/query":
                return Response({
                    "vulns": [
                        {
                            "id": "GHSA-test-critical",
                            "database_specific": {"severity": "CRITICAL"},
                            "summary": "Remote Code Execution via deserialization",
                        }
                    ]
                })
            raise AssertionError(request.full_url)

        findings, blocked = dependency_vetting.vet(before, after, now=NOW, opener=opener)
        self.assertEqual(len(findings), 1)
        self.assertTrue(findings[0].blocked)
        self.assertIsNotNone(blocked)
        self.assertIn("high/critical advisory", blocked)

        self.led.upsert_item("x", 5, pr=88)
        with mock.patch.object(self.gh, "dependency_files", return_value=(before, after)), \
             mock.patch.object(ship.dependency_vetting, "urlopen", side_effect=opener):
            allowed = ship._vet_pr_dependencies(
                self.ctx, "x", self.item(), 88, self.gh, self.gh.pr_view(88)
            )
        self.assertFalse(allowed)
        self.assertEqual(self.gh.merged, [])

        # Part 2: Caller refusal when gate evidence is missing/disputed
        self.led.upsert_item("x", 5, pr=88, labels=json.dumps(["size:m"]))
        review_findings = (
            "PR lacks required evidence from the conductor's dependency-vetting gate. "
            "Gate evidence was missing or absent on head abc123."
        )
        self.led.set_kv(
            "review:x#5",
            json.dumps({"sha": self.gh.head_sha, "pr": 88, "verdict": "fail", "findings": review_findings}),
        )

        with mock.patch.object(self.ctx, "gh", return_value=self.gh), \
             mock.patch.object(ship, "start") as mock_start:
            ship._review_triggered_fix(
                self.ctx, "x", self.item(), 88, self.gh.pr_view(88), review_findings
            )
            mock_start.assert_not_called()

        self.assertEqual(self.item()["state"], "waiting")
        rec = self.wait_record()
        self.assertEqual(rec.get("cause"), "dependency_vetting")
        self.assertEqual(rec.get("wake", {}).get("kind"), "evidence")
        self.assertIn("missing or disputed", rec.get("reason", ""))
        self.assertIsNone(self.led.lease("x", 5))

    def test_sdk_runner_pyproject_unpinned_range_fails_closed_without_lockfile(self):
        """Audit test targeting the exact sdk_runner/pyproject.toml declaration.

        sdk_runner/pyproject.toml declares:
            dependencies = ["claude-agent-sdk>=0.2.165"]
            build-system.requires = ["setuptools>=68"]
        without providing uv.lock or poetry.lock.

        Verifies that if this manifest is processed through Mahler's dependency
        vetting scanner without an exact lockfile, it fails closed before any
        outbound network queries.
        """
        before = {}
        after = {
            "sdk_runner/pyproject.toml": (
                '[project]\n'
                'name = "mahler-sdk-runner"\n'
                'version = "0.1.0"\n'
                'dependencies = ["claude-agent-sdk>=0.2.165"]\n\n'
                '[build-system]\n'
                'requires = ["setuptools>=68"]\n'
                'build-backend = "setuptools.build_meta"\n'
            )
        }

        mock_opener = mock.Mock()
        # Vetting should detect unresolved version for both dependencies:
        with self.assertRaises(dependency_vetting.VettingError) as cm:
            dependency_vetting.vet(before, after, now=NOW, opener=mock_opener)

        self.assertIn("unresolved exact version", str(cm.exception))
        mock_opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
