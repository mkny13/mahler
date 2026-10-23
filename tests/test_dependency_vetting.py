import json
import unittest
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from unittest import mock

from mahler import dependency_vetting as vetting


NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


def npm_files(declaration="^1.2.0", version="1.2.3"):
    return {
        "package.json": json.dumps({"dependencies": {"safe-package": declaration}}),
        "package-lock.json": json.dumps({
            "lockfileVersion": 3,
            "packages": {"": {"dependencies": {"safe-package": declaration}},
                         "node_modules/safe-package": {"version": version}},
        }),
    }


def python_files(declaration="requests>=2.30", version="2.31.0"):
    return {
        "pyproject.toml": "[project]\ndependencies = [\"" + declaration + "\"]\n",
        "uv.lock": 'version = 1\n\n[[package]]\nname = "requests"\nversion = "' + version + '"\n',
    }


class Response:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


class DependencyVettingTests(unittest.TestCase):
    def opener(self, *, npm_date="2020-01-01T00:00:00Z", pypi_date="2020-01-01T00:00:00Z",
               vulns=()):
        calls = []

        def open_request(request, timeout):
            calls.append(request.full_url)
            if request.full_url.startswith("https://registry.npmjs.org/"):
                return Response({
                    "versions": {"1.2.3": {"version": "1.2.3"}},
                    "time": {"created": npm_date, "1.2.3": npm_date},
                    "maintainers": [{"name": "Publisher"}],
                    "repository": {"url": "https://example.test/source"},
                })
            if request.full_url.startswith("https://pypi.org/"):
                return Response({
                    "info": {"version": "99.0", "author": "Publisher",
                             "project_urls": {"Source": "https://example.test/source"}},
                    "releases": {"2.31.0": [{"upload_time_iso_8601": pypi_date}]},
                })
            if request.full_url == "https://api.osv.dev/v1/query":
                return Response({"vulns": list(vulns)})
            raise AssertionError(request.full_url)

        return open_request, calls

    def test_npm_addition_uses_exact_lock_resolution_and_reports_metadata(self):
        opener, calls = self.opener()
        findings, blocked = vetting.vet({}, npm_files(), now=NOW, opener=opener)
        self.assertIsNone(blocked)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].dependency.version, "1.2.3")
        report = findings[0].report()
        self.assertIn("Publisher", report)
        self.assertIn("https://example.test/source", report)
        self.assertIn("2020-01-01", report)
        self.assertIn("advisories none", report)
        self.assertEqual(len(calls), 2)

    def test_pypi_version_change_is_resolved_from_lock_and_vetted(self):
        before = python_files(version="2.30.0")
        after = python_files(version="2.31.0")
        before["uv.lock"] = before["uv.lock"].replace("2.31.0", "2.30.0")
        opener, _ = self.opener()
        findings, blocked = vetting.vet(before, after, now=NOW, opener=opener)
        self.assertIsNone(blocked)
        self.assertEqual(findings[0].dependency.ecosystem, "pypi")
        self.assertEqual(findings[0].dependency.version, "2.31.0")

    def test_pypi_addition_uses_lock_resolution(self):
        before = {}
        after = {
            "pyproject.toml": "[project]\ndependencies = [\"requests>=2.30\"]\n",
            "poetry.lock": '[[package]]\nname = "requests"\nversion = "2.31.0"\n',
        }
        opener, _ = self.opener()
        findings, blocked = vetting.vet(before, after, now=NOW, opener=opener)
        self.assertIsNone(blocked)
        self.assertEqual(findings[0].dependency.version, "2.31.0")

    def test_pypi_build_system_addition_is_vetted(self):
        after = python_files()
        after["pyproject.toml"] = (
            '[build-system]\nrequires = ["requests>=2.30"]\n'
            'build-backend = "requests.build"\n'
        )
        opener, calls = self.opener()
        findings, blocked = vetting.vet({}, after, now=NOW, opener=opener)
        self.assertIsNone(blocked)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].dependency.name, "requests")
        self.assertEqual(findings[0].dependency.version, "2.31.0")
        self.assertEqual(calls, [
            "https://pypi.org/pypi/requests/json",
            "https://api.osv.dev/v1/query",
        ])

    def test_malformed_build_system_requires_fails_closed(self):
        after = {"pyproject.toml": '[build-system]\nrequires = "requests==2.31.0"\n'}
        with self.assertRaisesRegex(vetting.VettingError,
                                    "ambiguous build-system requires"):
            vetting.changed_dependencies({}, after)

    def test_npm_version_change_uses_new_lock_resolution(self):
        before = npm_files(version="1.2.2")
        after = npm_files(version="1.2.3")
        opener, _ = self.opener()
        findings, blocked = vetting.vet(before, after, now=NOW, opener=opener)
        self.assertIsNone(blocked)
        self.assertEqual(findings[0].dependency.version, "1.2.3")

    def test_new_version_published_under_fourteen_days_blocks(self):
        opener, _ = self.opener(npm_date="2026-10-15T00:00:00Z")
        findings, blocked = vetting.vet({}, npm_files(), now=NOW, opener=opener)
        self.assertTrue(findings[0].blocked)
        self.assertIn("less than 14 days", blocked)

    def test_high_and_critical_advisories_block_but_low_does_not(self):
        for severity, expected_block in (("HIGH", True), ("CRITICAL", True), ("LOW", False)):
            with self.subTest(severity=severity):
                advisory = {"id": "OSV-1", "database_specific": {"severity": severity}}
                opener, _ = self.opener(vulns=[advisory])
                findings, blocked = vetting.vet({}, npm_files(), now=NOW, opener=opener)
                self.assertEqual(bool(blocked), expected_block)
                self.assertEqual(findings[0].blocked, expected_block)
                self.assertIn(f"OSV-1 ({severity.lower()})", findings[0].report())

    def test_nonexistent_package_fails_closed(self):
        def missing(request, timeout):
            raise HTTPError(request.full_url, 404, "not found", None, None)

        with self.assertRaisesRegex(vetting.VettingError, "does not exist"):
            vetting.vet({}, npm_files(), now=NOW, opener=missing)

    def test_missing_lock_resolution_fails_closed_without_registry_calls(self):
        files = {"package.json": json.dumps({"dependencies": {"safe-package": "^1.2.0"}})}
        opener = mock.Mock()
        with self.assertRaisesRegex(vetting.VettingError, "unresolved exact version"):
            vetting.vet({}, files, now=NOW, opener=opener)
        opener.assert_not_called()

    def test_stale_python_lock_version_fails_before_registry_calls(self):
        before = {
            "pyproject.toml": '[project]\ndependencies = ["demo==1.0"]\n',
            "uv.lock": 'version = 1\n\n[[package]]\nname = "demo"\nversion = "1.0"\n',
        }
        after = {
            "pyproject.toml": '[project]\ndependencies = ["demo==2.0"]\n',
            "uv.lock": 'version = 1\n\n[[package]]\nname = "demo"\nversion = "1.0"\n',
        }
        opener = mock.Mock()
        with self.assertRaisesRegex(
                vetting.VettingError, r"lockfile version 1\.0 does not satisfy pypi dependency demo"):
            vetting.vet(before, after, now=NOW, opener=opener)
        opener.assert_not_called()

    def test_stale_npm_lock_version_fails_before_registry_calls(self):
        after = npm_files(declaration="^2.0.0", version="1.9.0")
        opener = mock.Mock()
        with self.assertRaisesRegex(
                vetting.VettingError, r"lockfile version 1\.9\.0 does not satisfy npm dependency"):
            vetting.vet({}, after, now=NOW, opener=opener)
        opener.assert_not_called()

    def test_conflicting_direct_declarations_fail_closed(self):
        after = {
            "pyproject.toml": "[project]\ndependencies = [\"safe_package>=1.0\"]\n",
            "requirements-dev.txt": "safe-package==1.2.3\n",
            "package-lock.json": json.dumps({
                "packages": {"node_modules/safe-package": {"version": "1.2.3"}}}),
        }
        with self.assertRaisesRegex(vetting.VettingError, "ambiguous direct dependency"):
            vetting.changed_dependencies({}, after)

    def test_registry_failure_fails_closed(self):
        def unavailable(request, timeout):
            raise URLError("offline")

        with self.assertRaisesRegex(vetting.VettingError, "lookup failed"):
            vetting.vet({}, npm_files(), now=NOW, opener=unavailable)

    def test_osv_failure_fails_closed(self):
        def unavailable(request, timeout):
            if request.full_url == "https://api.osv.dev/v1/query":
                raise URLError("OSV offline")
            return Response({
                "versions": {"1.2.3": {"version": "1.2.3"}},
                "time": {"created": "2020-01-01T00:00:00Z"},
                "maintainers": [], "repository": {},
            })

        with self.assertRaisesRegex(vetting.VettingError, "OSV offline"):
            vetting.vet({}, npm_files(), now=NOW, opener=unavailable)

    def test_unchanged_dependency_set_makes_no_registry_calls(self):
        opener = mock.Mock()
        findings, blocked = vetting.vet(npm_files(), npm_files(), now=NOW, opener=opener)
        self.assertEqual(findings, ())
        self.assertIsNone(blocked)
        opener.assert_not_called()

    def test_osv_empty_object_is_clean(self):
        registry, _ = self.opener()
        def opener(request, timeout):
            if request.full_url == "https://api.osv.dev/v1/query":
                return Response({})
            return registry(request, timeout)
        self.assertIsNone(vetting.vet({}, npm_files(), now=NOW, opener=opener)[1])

    def test_incomplete_osv_page_fails_closed(self):
        registry, _ = self.opener()
        def opener(request, timeout):
            if request.full_url == "https://api.osv.dev/v1/query":
                return Response({"vulns": [], "next_page_token": "more"})
            return registry(request, timeout)
        with self.assertRaises(vetting.VettingError):
            vetting.vet({}, npm_files(), now=NOW, opener=opener)

    def test_exact_python_build_requirement_without_lock(self):
        files = {"pyproject.toml": '[build-system]\nrequires = ["requests==2.31.0"]\n'}
        opener, _ = self.opener()
        self.assertIsNone(vetting.vet({}, files, now=NOW, opener=opener)[1])

    def test_dependency_groups_are_direct_dependencies(self):
        files = python_files()
        files["pyproject.toml"] = '[dependency-groups]\ntest = ["requests>=2.30"]\n'
        deps = vetting.changed_dependencies({}, files)
        self.assertEqual([d.name for d in deps], ["requests"])

    def test_dynamic_dependencies_are_ambiguous(self):
        with self.assertRaises(vetting.VettingError):
            vetting.changed_dependencies({}, {
                "pyproject.toml": '[project]\ndynamic = ["dependencies"]\n'})

    def test_python_ambiguous_lock_does_not_recover_on_third_entry(self):
        files = python_files()
        files["uv.lock"] += ('\n[[package]]\nname = "requests"\nversion = "2.32.0"\n'
                             '\n[[package]]\nname = "requests"\nversion = "2.31.0"\n')
        with self.assertRaises(vetting.VettingError):
            vetting.vet({}, files, now=NOW, opener=mock.Mock())

    def test_unrelated_project_lock_cannot_resolve_dependency(self):
        files = npm_files()
        files["other/package-lock.json"] = files.pop("package-lock.json")
        with self.assertRaises(vetting.VettingError):
            vetting.vet({}, files, now=NOW, opener=mock.Mock())

    def test_npm_names_do_not_use_python_normalization(self):
        files = npm_files()
        manifest = json.loads(files["package.json"])
        manifest["dependencies"]["safe_package"] = "^1.2.0"
        files["package.json"] = json.dumps(manifest)
        with self.assertRaises(vetting.VettingError):
            vetting.vet({}, files, now=NOW, opener=mock.Mock())

    def test_non_registry_lock_sources_are_ambiguous(self):
        for source in ({"link": True}, {"resolved": "https://evil.test/package.tgz"}):
            with self.subTest(source=source):
                files = npm_files()
                lock = json.loads(files["package-lock.json"])
                lock["packages"]["node_modules/safe-package"].update(source)
                files["package-lock.json"] = json.dumps(lock)
                with self.assertRaises(vetting.VettingError):
                    vetting.vet({}, files, now=NOW, opener=mock.Mock())

    def test_unchanged_unsupported_files_do_not_block_unrelated_edit(self):
        before = {"setup.py": "some dynamic code", **npm_files()}
        after = dict(before)
        after["package.json"] = after["package.json"].replace('{', '{ ', 1)
        opener = mock.Mock()
        self.assertEqual(vetting.vet(before, after, opener=opener), ((), None))
        opener.assert_not_called()

    def test_unsupported_lockfile_change_is_ambiguous(self):
        with self.assertRaisesRegex(vetting.VettingError, "unsupported dependency lockfile"):
            vetting.changed_dependencies({}, {"pnpm-lock.yaml": "lockfileVersion: 9"})


if __name__ == "__main__":
    unittest.main()
