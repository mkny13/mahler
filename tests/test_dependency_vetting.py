import json
import unittest
from datetime import datetime, timedelta, timezone
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
    def __init__(self, body, raw=False):
        self.body = body.encode() if raw else json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
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

    def test_transitive_npm_lock_changes_are_vetted(self):
        for lockname in ("package-lock.json", "npm-shrinkwrap.json"):
            for location in ("node_modules/safe-package",
                             "node_modules/parent/node_modules/safe-package"):
                for old_version in (None, "1.2.2"):
                    with self.subTest(lock=lockname, location=location, old=old_version):
                        def files(version):
                            packages = {location: {"version": version}} if version else {}
                            return {"sync/package.json": '{"dependencies": {}}',
                                    "sync/" + lockname: json.dumps({"packages": packages})}
                        opener, calls = self.opener()
                        findings, blocked = vetting.vet(
                            files(old_version), files("1.2.3"), now=NOW, opener=opener)
                        self.assertIsNone(blocked)
                        self.assertEqual(len(findings), 1)
                        self.assertIn("Publisher", findings[0].report())
                        self.assertEqual(len(calls), 2)

    def test_transitive_npm_metadata_drops_are_ignored(self):
        before = {"package-lock.json": json.dumps({"packages": {
            "node_modules/undici": {"version": "7.30.2", "libc": ["glibc"],
                                    "cpu": ["x64"], "os": ["linux"], "integrity": "old"}}})}
        after = {"package-lock.json": json.dumps({"packages": {
            "node_modules/undici": {"version": "7.30.2"}}})}
        self.assertEqual(vetting.changed_dependencies(before, after), [])

    def test_transitive_python_lock_changes_are_vetted(self):
        for lockname in ("uv.lock", "poetry.lock"):
            with self.subTest(lock=lockname):
                after = {lockname: '[[package]]\nname = "requests"\nversion = "2.31.0"\n'}
                before = {lockname: after[lockname].replace("2.31.0", "2.30.0")}
                opener, calls = self.opener()
                findings, blocked = vetting.vet(before, after, now=NOW, opener=opener)
                self.assertIsNone(blocked)
                self.assertEqual(len(findings), 1)
                self.assertIn("Publisher", findings[0].report())
                self.assertEqual(len(calls), 2)

    def test_transitive_custom_registry_is_refused(self):
        for files in (
            {"package-lock.json": json.dumps({"packages": {"node_modules/pkg": {
                "version": "1.2.3", "resolved": "https://other.test/pkg.tgz"}}})},
            {"uv.lock": '[[package]]\nname = "pkg"\nversion = "1.2.3"\n'
                        'source = { registry = "https://other.test/simple" }\n'},
        ):
            opener = mock.Mock()
            with self.assertRaises(vetting.VettingError):
                vetting.vet({}, files, opener=opener)
            opener.assert_not_called()

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

    def test_cvss_vectors_classify_low_medium_and_high(self):
        prefix = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/"
        for impact, expected in (("C:L/I:N/A:N", "MODERATE"),
                                 ("C:H/I:H/A:H", "CRITICAL")):
            with self.subTest(impact=impact):
                advisory = {"severity": [{"type": "CVSS_V3", "score": prefix + impact}]}
                self.assertEqual(vetting._severity(advisory), expected)

    def test_workspace_lock_changes_are_not_silently_ignored(self):
        before = {"package.json": '{"workspaces": ["packages/*"]}',
                  "package-lock.json": '{"packages": {}}',
                  "packages/app/package.json": '{"dependencies": {"foo": "*"}}'}
        after = dict(before)
        after["package-lock.json"] = '{"packages": {"node_modules/foo": {"version": "2.0.0"}}}'
        with self.assertRaises(vetting.VettingError):
            vetting.changed_dependencies(before, after)

    def test_pypi_empty_release_is_not_an_existing_version(self):
        def opener(request, timeout):
            return Response({"info": {}, "releases": {"2.31.0": [],
                "1.0": [{"upload_time_iso_8601": "2020-01-01T00:00:00Z"}]}})
        with self.assertRaises(vetting.VettingError):
            vetting.vet({}, python_files(), now=NOW, opener=opener)

    def test_npm_version_publisher_and_repository_are_reported(self):
        def opener(request, timeout):
            if request.full_url == "https://api.osv.dev/v1/query":
                return Response({})
            return Response({"versions": {"1.2.3": {
                "_npmUser": {"name": "release-publisher"},
                "repository": {"url": "https://example.test/version-source"}}},
                "time": {"created": "2020-01-01T00:00:00Z"}})
        report = vetting.vet({}, npm_files(), now=NOW, opener=opener)[0][0].report()
        self.assertIn("release-publisher", report)
        self.assertIn("https://example.test/version-source", report)

    def test_unsupported_lockfile_change_is_ambiguous(self):
        with self.assertRaisesRegex(vetting.VettingError, "unsupported dependency lockfile"):
            vetting.changed_dependencies({}, {"yarn.lock": "# yarn lockfile v1"})




def pnpm_files(version="4.0.0", *, schema="9.0", group="dependencies",
               suffix="", resolution="{integrity: sha512-test}"):
    """Reduced pnpm diff modeled on the admitted groundwork#413 bump."""
    key = ("/" if schema == "6.0" else "") + "web-push@" + version
    return {
        "package.json": json.dumps({group: {"web-push": "^" + version}}),
        "pnpm-lock.yaml": (
            f"lockfileVersion: '{schema}'\n\nsettings:\n  autoInstallPeers: true\n"
            f"\nimporters:\n  .:\n    {group}:\n      web-push:\n"
            f"        specifier: ^{version}\n        version: {version}{suffix}\n"
            f"\npackages:\n  {key}{suffix if schema == '6.0' else ''}:\n"
            f"    resolution: {resolution}\n    engines: {{node: '>= 16'}}\n"
            "\nsnapshots: {}\n"),
    }


class PnpmTests(unittest.TestCase):
    def test_addition_bump_and_peer_suffix(self):
        for schema in ("6.0", "9.0"):
            for group in ("dependencies", "devDependencies", "optionalDependencies"):
                for suffix in ("", "(supports-color@7.2.0)"):
                    with self.subTest(schema=schema, group=group, suffix=suffix):
                        after = pnpm_files(schema=schema, group=group, suffix=suffix)
                        for before in ({}, pnpm_files("3.6.7", schema=schema, group=group)):
                            deps = vetting.changed_dependencies(before, after)
                            self.assertEqual([(d.name, d.version) for d in deps],
                                             [("web-push", "4.0.0")])
                        self.assertEqual(vetting.changed_dependencies(after, after), [])

    def test_v6_root_dependencies(self):
        files = pnpm_files(schema="6.0")
        text = files["pnpm-lock.yaml"].replace("importers:\n  .:\n", "")
        files["pnpm-lock.yaml"] = "\n".join(
            line[4:] if line.startswith("    ") and not line.startswith("    resolution:")
            and not line.startswith("    engines:") else line for line in text.splitlines())
        self.assertEqual(vetting.changed_dependencies({}, files)[0].version, "4.0.0")

    def test_registry_and_osv_pipeline(self):
        for recent, vulnerable in ((False, False), (True, False), (False, True)):
            calls = []
            def opener(request, timeout):
                calls.append(request.full_url)
                if request.full_url == "https://registry.npmjs.org/web-push":
                    date = "2026-10-19T00:00:00Z" if recent else "2020-01-01T00:00:00Z"
                    return Response({"versions": {"4.0.0": {"version": "4.0.0"}},
                                     "time": {"created": date, "4.0.0": date}})
                self.assertEqual(json.loads(request.data)["version"], "4.0.0")
                return Response({"vulns": [{"id": "OSV-test", "database_specific": {
                    "severity": "HIGH"}}]} if vulnerable else {})
            findings, reason = vetting.vet(pnpm_files("3.6.7"), pnpm_files(),
                                          now=NOW, opener=opener)
            self.assertEqual(bool(reason), recent or vulnerable)
            self.assertEqual(len(findings), 1)
            self.assertEqual(len(calls), 2)

    def test_sources_and_ambiguous_layouts_fail_before_network(self):
        cases = []
        for source in ("https://other.test/pkg.tgz", "git+https://github.com/a/b",
                       "link:../pkg", "file:../pkg", "workspace:*"):
            cases.append(pnpm_files(resolution="{tarball: " + source + "}"))
            files = pnpm_files()
            files["pnpm-lock.yaml"] = files["pnpm-lock.yaml"].replace(
                "version: 4.0.0", "version: " + source)
            cases.append(files)
        for old, new in (("  .:", "  .: {}\n  other:"),
                         ("{integrity: sha512-test}", "{}"),
                         ("lockfileVersion: '9.0'", "lockfileVersion: '5.4'"),
                         ("        version: 4.0.0", "        version: 4.0.0\n        version: 3.6.7")):
            files = pnpm_files()
            files["pnpm-lock.yaml"] = files["pnpm-lock.yaml"].replace(old, new)
            cases.append(files)
        for files in cases:
            with self.subTest(lock=files["pnpm-lock.yaml"]):
                opener = mock.Mock()
                with self.assertRaises(vetting.VettingError):
                    vetting.vet({}, files, opener=opener)
                opener.assert_not_called()

    def test_standard_tarball_and_lock_only_bump(self):
        after = pnpm_files(resolution="{tarball: https://registry.npmjs.org/web-push/-/web-push-4.0.0.tgz}")
        before = pnpm_files("3.6.7")
        del before["package.json"], after["package.json"]
        self.assertEqual(vetting.changed_dependencies(before, after)[0].version, "4.0.0")


def swift_files(version="1.2.3", schema=3):
    pin = {"identity": "demo", "kind": "remoteSourceControl",
           "location": "https://github.com/example/demo.git",
           "state": {"version": version, "revision": "a" * 40}}
    if schema == 1:
        pin = {"package": "Demo", "repositoryURL": pin["location"], "state": pin["state"]}
        data = {"version": 1, "object": {"pins": [pin]}}
    else:
        data = {"version": schema, "pins": [pin]}
    return {"Package.resolved": json.dumps(data)}


def gradle_files(version="1.2.3"):
    return {"gradle.lockfile": f"# Gradle lock\norg.example:demo:{version}=runtimeClasspath\nempty=compileClasspath\n"}


class SwiftGradleTests(unittest.TestCase):
    POM = ("<project xmlns='http://maven.apache.org/POM/4.0.0'><groupId>org.example</groupId>"
           "<artifactId>demo</artifactId><organization><name>Example Org</name></organization>"
           "<scm><url>https://github.com/example/demo</url></scm></project>")

    def opener(self, date="2020-01-01T00:00:00Z", vulns=(), broken=None, pom=POM,
               selected=None, history=True):
        """`date` is the first publication; `selected` the pinned version's (default: same)."""
        selected = selected or date
        def stamp_of(value):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000 if value else None

        def open_request(request, timeout):
            url = request.full_url
            if url.endswith("maven-metadata.xml") or url.startswith("https://dl.google.com/"):
                raise HTTPError(url, 404, "not found", None, None)
            if broken and broken in url:
                raise URLError("offline")
            if url.endswith(".pom"):
                if pom is None:
                    raise HTTPError(url, 404, "not found", None, None)
                return Response(pom, raw=True)
            if url == "https://api.osv.dev/v1/query":
                payload = json.loads(request.data)
                self.assertIn(payload["package"]["ecosystem"], ("SwiftURL", "Maven"))
                self.assertEqual(payload["version"], "1.2.3")
                return Response({"vulns": list(vulns)})
            if "/commits/" in url:
                return Response({"sha": "a" * 40})
            if "/releases/tags/" in url:
                return Response({"tag_name": "1.2.3", "published_at": selected, "draft": False})
            if "/releases?" in url:
                if history is not True:
                    return Response(history)
                return Response([{"tag_name": "1.2.3", "published_at": selected, "draft": False},
                                 {"tag_name": "1.0.0", "published_at": date, "draft": False},
                                 {"tag_name": "2.0.0-rc", "published_at": None, "draft": True}])
            if url.startswith("https://api.github.com/repos/"):
                return Response({"full_name": "example/demo", "html_url": "https://github.com/example/demo",
                                 "owner": {"login": "example"}})
            if url.startswith("https://search.maven.org/solrsearch/select?"):
                if history is not True:
                    return Response(history)
                docs = [{"g": "org.example", "a": "demo", "v": "1.2.3", "timestamp": stamp_of(selected)}]
                if date != selected:
                    docs.append({"g": "org.example", "a": "demo", "v": "1.0.0", "timestamp": stamp_of(date)})
                return Response({"response": {"numFound": len(docs), "docs": docs}})
            raise AssertionError(url)
        return open_request

    def test_additions_versions_and_unchanged_entries(self):
        for files, ecosystem in ((swift_files, "swift"), (gradle_files, "gradle")):
            with self.subTest(ecosystem=ecosystem):
                for before in ({}, files("1.2.2")):
                    deps = vetting.changed_dependencies(before, files())
                    self.assertEqual([(d.ecosystem, d.version) for d in deps], [(ecosystem, "1.2.3")])
                after = {p: t + "\n" for p, t in files().items()}
                opener = mock.Mock()
                self.assertEqual(vetting.vet(files(), after, opener=opener), ((), None))
                opener.assert_not_called()

    def test_manifest_plus_lock_updates_pass_and_manifest_only_fails(self):
        # Swift bump with both Package.swift and Package.resolved
        swift_before = swift_files("1.2.2")
        swift_before["Package.swift"] = '.package(url: "https://github.com/example/demo", from: "1.2.2")'
        swift_after = swift_files("1.2.3")
        swift_after["Package.swift"] = '.package(url: "https://github.com/example/demo", from: "1.2.3")'
        deps = vetting.changed_dependencies(swift_before, swift_after)
        self.assertEqual([(d.ecosystem, d.version) for d in deps], [("swift", "1.2.3")])
        findings, reason = vetting.vet(swift_before, swift_after, now=NOW, opener=self.opener())
        self.assertIsNone(reason)
        self.assertEqual(len(findings), 1)

        # Gradle bump with both build.gradle and gradle.lockfile
        gradle_before = gradle_files("1.2.2")
        gradle_before["build.gradle"] = 'dependencies { implementation "org.example:demo:1.2.2" }'
        gradle_after = gradle_files("1.2.3")
        gradle_after["build.gradle"] = 'dependencies { implementation "org.example:demo:1.2.3" }'
        deps = vetting.changed_dependencies(gradle_before, gradle_after)
        self.assertEqual([(d.ecosystem, d.version) for d in deps], [("gradle", "1.2.3")])
        findings, reason = vetting.vet(gradle_before, gradle_after, now=NOW, opener=self.opener())
        self.assertIsNone(reason)
        self.assertEqual(len(findings), 1)

        # Gradle bump with both libs.versions.toml and gradle/dependency-locks
        gradle_lock_before = {"gradle/dependency-locks/runtime.lockfile": "org.example:demo:1.2.2\n",
                              "gradle/libs.versions.toml": '[versions]\ndemo = "1.2.2"\n'}
        gradle_lock_after = {"gradle/dependency-locks/runtime.lockfile": "org.example:demo:1.2.3\n",
                             "gradle/libs.versions.toml": '[versions]\ndemo = "1.2.3"\n'}
        deps = vetting.changed_dependencies(gradle_lock_before, gradle_lock_after)
        self.assertEqual([(d.ecosystem, d.version) for d in deps], [("gradle", "1.2.3")])

        # Swift and Gradle removals with both manifest and lockfile
        swift_removed = {"Package.swift": "// empty", "Package.resolved": '{"version": 2, "pins": []}'}
        self.assertEqual(vetting.changed_dependencies(swift_before, swift_removed), [])
        gradle_removed = {"build.gradle": "// empty", "gradle.lockfile": "# empty\n"}
        self.assertEqual(vetting.changed_dependencies(gradle_before, gradle_removed), [])

        # Manifest change with unchanged lockfile fails closed
        swift_stale = dict(swift_before)
        swift_stale["Package.swift"] = swift_after["Package.swift"]
        with self.assertRaises(vetting.VettingError):
            vetting.changed_dependencies(swift_before, swift_stale)

        gradle_stale = dict(gradle_before)
        gradle_stale["build.gradle"] = gradle_after["build.gradle"]
        with self.assertRaises(vetting.VettingError):
            vetting.changed_dependencies(gradle_before, gradle_stale)

    def test_swift_schemas_and_gradle_legacy_lock(self):
        for schema in (1, 2, 3):
            self.assertEqual(len(vetting.changed_dependencies({}, swift_files(schema=schema))), 1)
        self.assertEqual(len(vetting.changed_dependencies({}, {
            "gradle/dependency-locks/runtimeClasspath.lockfile": "org.example:demo:1.2.3\n"})), 1)

    def test_paths_enter_shared_gate(self):
        for path in ("Package.swift", "app/Package.resolved", "build.gradle", "build.gradle.kts",
                     "gradle.lockfile", "gradle/dependency-locks/test.lockfile", "gradle/libs.versions.toml"):
            self.assertTrue(vetting.relevant_path(path), f"Expected dependency path: {path}")

    def test_clean_metadata_and_advisory_evidence(self):
        for files in (swift_files, gradle_files):
            findings, reason = vetting.vet({}, files(), now=NOW, opener=self.opener())
            self.assertIsNone(reason)
            self.assertEqual(len(findings), 1)
            report = findings[0].report()
            for value in ("1.2.3", "2020-01-01", "example", "advisories none"):
                self.assertIn(value, report)

    def test_recent_and_advisories_block(self):
        for files in (swift_files, gradle_files):
            for severity in ("HIGH", "CRITICAL", "LOW"):
                findings, reason = vetting.vet({}, files(), now=NOW, opener=self.opener(vulns=[
                    {"id": "OSV-test", "database_specific": {"severity": severity}}]))
                self.assertEqual(bool(reason), severity != "LOW")
                self.assertIn("OSV-test", findings[0].report())
            findings, reason = vetting.vet({}, files(), now=NOW,
                                          opener=self.opener(date="2026-10-19T00:00:00Z"))
            self.assertIn("less than 14 days", reason)
            self.assertTrue(findings[0].recent)

    def test_established_package_with_recent_version_passes_new_package_blocks(self):
        for files in (swift_files, gradle_files):
            with self.subTest(files=files.__name__):
                findings, reason = vetting.vet({}, files(), now=NOW, opener=self.opener(
                    date="2020-01-01T00:00:00Z", selected="2026-10-19T00:00:00Z"))
                self.assertIsNone(reason)
                self.assertFalse(findings[0].recent)
                self.assertIn("first published 2020-01-01", findings[0].report())
                findings, reason = vetting.vet({}, files(), now=NOW, opener=self.opener(
                    date="2026-10-18T00:00:00Z", selected="2026-10-19T00:00:00Z"))
                self.assertIn("less than 14 days", reason)
                self.assertTrue(findings[0].recent)

    def test_invalid_or_missing_release_history_fails_closed(self):
        bad = {
            swift_files: [None, [], {}, "x", [{"draft": False, "published_at": "nope"}],
                          [{"draft": False}], [{"published_at": "2020-01-01T00:00:00Z"}],
                          [{"draft": True, "published_at": None}] * 100],
            gradle_files: [None, {}, {"response": {"numFound": 0, "docs": []}},
                           {"response": {"numFound": 2, "docs": [
                               {"g": "org.example", "a": "demo", "v": "1.2.3", "timestamp": 1}]}},
                           {"response": {"numFound": 2, "docs": [
                               {"g": "org.example", "a": "demo", "v": "1.2.3", "timestamp": 1.6e12},
                               {"g": "org.example", "a": "demo", "v": "1.0.0", "timestamp": "x"}]}},
                           {"response": {"numFound": 1, "docs": [
                               {"g": "other", "a": "demo", "v": "1.2.3", "timestamp": 1.6e12}]}}],
        }
        for files, histories in bad.items():
            for history in histories:
                with self.subTest(files=files.__name__, history=history):
                    with self.assertRaises(vetting.VettingError):
                        vetting.vet({}, files(), now=NOW, opener=self.opener(history=history))

    def gradle_report(self, pom):
        findings, _ = vetting.vet({}, gradle_files(), now=NOW, opener=self.opener(pom=pom))
        return findings[0].report()

    def test_gradle_pom_publisher_and_source(self):
        report = self.gradle_report(self.POM)
        self.assertIn("publisher Example Org; source https://github.com/example/demo", report)
        devs = self.POM.replace("<organization><name>Example Org</name></organization>",
                                "<developers><developer><name>Ada</name></developer>"
                                "<developer><id>bob</id></developer></developers>")
        self.assertIn("publisher Ada, bob;", self.gradle_report(devs))

    def test_gradle_pom_fallback_when_unavailable_or_untrusted(self):
        pom_url = "https://repo1.maven.org/maven2/org/example/demo/1.2.3/demo-1.2.3.pom"
        wrong = self.POM.replace("<artifactId>demo", "<artifactId>other")
        other_group = self.POM.replace("<groupId>org.example", "<groupId>evil.example")
        for label, pom in (("missing", None), ("malformed", "<project>"),
                           ("entity", "<!DOCTYPE x [<!ENTITY a 'b'>]><project/>"),
                           ("empty", "<project><artifactId>demo</artifactId></project>"),
                           ("wrong artifact", wrong), ("wrong group", other_group)):
            with self.subTest(label):
                report = self.gradle_report(pom)
                self.assertIn("publisher Maven Central namespace org.example", report)
                self.assertIn("source " + pom_url, report)

    def test_gradle_pom_server_error_blocks(self):
        clean = self.opener()
        def opener(request, timeout):
            if request.full_url.endswith(".pom"):
                raise HTTPError(request.full_url, 500, "boom", None, None)
            return clean(request, timeout)
        with self.assertRaises(vetting.VettingError):
            vetting.vet({}, gradle_files(), now=NOW, opener=opener)

    def test_missing_metadata_and_endpoint_failures(self):
        for files, endpoint in ((swift_files, "api.github.com"), (gradle_files, "search.maven.org")):
            for opener in (self.opener(date=None), self.opener(broken=endpoint),
                           self.opener(broken="api.osv.dev"), lambda request, timeout: Response({}),
                           lambda request, timeout: Response(None)):
                with self.subTest(files=files.__name__, opener=opener):
                    with self.assertRaises(vetting.VettingError):
                        vetting.vet({}, files(), now=NOW, opener=opener)

    def test_malformed_unresolved_pins_and_manifest_only_changes(self):
        cases = [swift_files(None), swift_files("main"), gradle_files("1.+"),
                 {"Package.resolved": '{"version": 3, "pins": {}}'},
                 {"gradle.lockfile": "not-a-coordinate=runtimeClasspath"},
                 {"Package.swift": '.package(url: "https://github.com/example/demo", from: "1.0.0")'},
                 {"build.gradle": 'dependencies { implementation "org.example:demo:1.+" }'}]
        for files in cases:
            with self.subTest(files=files):
                opener = mock.Mock()
                with self.assertRaises(vetting.VettingError):
                    vetting.vet({}, files, opener=opener)
                opener.assert_not_called()

    def test_swift_revision_must_match_release(self):
        files = swift_files()
        files["Package.resolved"] = files["Package.resolved"].replace("a" * 40, "b" * 40)
        with self.assertRaises(vetting.VettingError):
            vetting.vet({}, files, opener=self.opener())

    def test_invalid_revision_and_version_types_fail_before_network(self):
        for version in (123, [], "1.2", "1.2.3/evil"):
            with self.subTest(version=version):
                opener = mock.Mock()
                with self.assertRaises(vetting.VettingError):
                    vetting.vet({}, swift_files(version), opener=opener)
                opener.assert_not_called()

    def test_wrong_artifact_or_source_metadata_blocks(self):
        for files in (swift_files, gradle_files):
            clean = self.opener()
            def opener(request, timeout):
                response = clean(request, timeout)
                data = json.loads(response.body)
                if "full_name" in data:
                    data["full_name"] = "other/repo"
                if "response" in data:
                    data["response"]["docs"][0]["v"] = "9.0.0"
                return Response(data)
            with self.assertRaises(vetting.VettingError):
                vetting.vet({}, files(), opener=opener)

    def test_missing_source_and_release_http_404_blocks(self):
        for target in ("/repos/example/demo", "/releases/tags/", "search.maven.org"):
            clean = self.opener()
            def opener(request, timeout):
                if target in request.full_url:
                    raise HTTPError(request.full_url, 404, "not found", None, None)
                return clean(request, timeout)
            files = gradle_files() if target == "search.maven.org" else swift_files()
            with self.assertRaises(vetting.VettingError):
                vetting.vet({}, files, opener=opener)


class GradleCatalogTests(unittest.TestCase):
    @staticmethod
    def files(version="23.0.0"):
        return {"gradle/libs.versions.toml": (
            '[versions]\ncast = "' + version + '"\n[libraries]\n'
            'cast = { module = "com.google.android.gms:play-services-cast-framework", version.ref = "cast" }\n')}

    def test_catalog_add_bump_unchanged_removal(self):
        for before in ({}, self.files("22.3.1")):
            deps = vetting.changed_dependencies(before, self.files())
            self.assertEqual([(d.name, d.version) for d in deps],
                             [("com.google.android.gms:play-services-cast-framework", "23.0.0")])
        self.assertEqual(vetting.changed_dependencies(self.files(), self.files()), [])
        self.assertEqual(vetting.changed_dependencies(self.files(), {}), [])
        formatted = {p: t + "\n# comment" for p, t in self.files().items()}
        self.assertEqual(vetting.changed_dependencies(self.files(), formatted), [])

    def test_catalog_bump_ignores_unchanged_bom_managed_aliases(self):
        def catalog(cast_version):
            return {"gradle/libs.versions.toml": (
                '[versions]\n'
                f'cast = "{cast_version}"\n'
                'compose-bom = "2026.10.00"\n'
                '[libraries]\n'
                'androidx-bom = { module = "androidx.compose:compose-bom", version.ref = "compose-bom" }\n'
                'androidx-ui = { group = "androidx.compose.ui", name = "ui" }\n'
                'cast = { module = "com.google.android.gms:play-services-cast-framework", version.ref = "cast" }\n')}

        deps = vetting.changed_dependencies(catalog("22.3.1"), catalog("23.0.0"))
        self.assertEqual([(dep.name, dep.version) for dep in deps],
                         [("com.google.android.gms:play-services-cast-framework", "23.0.0")])

    def test_catalog_bump_ignores_unchanged_exact_version_with_v_prefix(self):
        def catalog(cast_version):
            return {"gradle/libs.versions.toml": (
                '[versions]\n'
                f'cast = "{cast_version}"\n'
                'newpipe = "v0.26.5"\n'
                '[libraries]\n'
                'cast = { module = "com.google.android.gms:play-services-cast-framework", version.ref = "cast" }\n'
                'newpipe = { module = "com.github.TeamNewPipe:NewPipeExtractor", version.ref = "newpipe" }\n')}

        deps = vetting.changed_dependencies(catalog("22.3.1"), catalog("23.0.0"))
        self.assertEqual([(dep.name, dep.version) for dep in deps],
                         [("com.google.android.gms:play-services-cast-framework", "23.0.0")])

    def test_catalog_forms_and_ambiguous_versions(self):
        for entry in ('{ group="org.example", name="demo", version="1.2.3" }',
                      '"org.example:demo:1.2.3"'):
            deps = vetting.changed_dependencies({}, {"gradle/libs.versions.toml":
                                                     '[libraries]\ndemo = ' + entry})
            self.assertEqual(deps[0].version, "1.2.3")
        for version in ("1.+", "latest.release", "1.0-SNAPSHOT"):
            with self.assertRaises(vetting.VettingError):
                vetting.changed_dependencies({}, self.files(version))
        with self.assertRaises(vetting.VettingError):
            vetting.changed_dependencies({}, {"gradle/libs.versions.toml":
                '[libraries]\nx = { module="org.example:demo", version.ref="missing" }'})

    def google_opener(self, *, bad=None, vulnerable=False):
        def opener(request, timeout):
            url = request.full_url
            if url.startswith(("https://repo1.maven.org/", "https://plugins.gradle.org/")):
                raise HTTPError(url, 404, "not found", None, None)
            if "search.maven.org" in url:
                return Response({"response": {"numFound": 0, "docs": []}})
            if url.endswith("maven-metadata.xml"):
                return Response(bad or '<metadata><groupId>com.google.android.gms</groupId>'
                    '<artifactId>play-services-cast-framework</artifactId><versioning><versions>'
                    '<version>9.2.0</version><version>23.0.0</version></versions>'
                    '<lastUpdated>20261019000000</lastUpdated></versioning></metadata>', raw=True)
            if request.get_method() == "HEAD":
                response = Response("")
                response.headers = {"Last-Modified": "Wed, 01 Jan 2020 00:00:00 GMT"}
                return response
            if url.endswith(".pom"):
                return Response('<project><groupId>com.google.android.gms</groupId>'
                    '<artifactId>play-services-cast-framework</artifactId>'
                    '<organization><name>Google</name></organization>'
                    '<scm><url>https://example.test/cast</url></scm></project>', raw=True)
            if "api.osv.dev" in url:
                self.assertEqual(json.loads(request.data)["package"], {
                    "ecosystem": "Maven", "name": "com.google.android.gms:play-services-cast-framework"})
                return Response({"vulns": [{"id": "OSV-test", "database_specific": {
                    "severity": "HIGH"}}]} if vulnerable else {})
            raise AssertionError(url)
        return opener

    def test_google_maven_catalog_vetting(self):
        findings, reason = vetting.vet(self.files("22.3.1"), self.files(),
                                      now=NOW, opener=self.google_opener())
        self.assertIsNone(reason)
        self.assertIn("2020-01-01", findings[0].first_published)
        self.assertEqual(findings[0].publisher, "Google")
        self.assertEqual(findings[0].source, "https://example.test/cast")
        _, reason = vetting.vet({}, self.files(), now=NOW,
                               opener=self.google_opener(vulnerable=True))
        self.assertTrue(reason)

    def test_google_missing_or_mismatched_history_fails_closed(self):
        for bad in ("<metadata/>", "<broken", '<!DOCTYPE x><metadata/>'):
            with self.assertRaises(vetting.VettingError):
                vetting.vet({}, self.files(), now=NOW, opener=self.google_opener(bad=bad))


class MavenRepositoryTests(unittest.TestCase):
    def fixture(self, *, portal=False, missing=False, metadata=None, date="Wed, 01 Jan 2020 00:00:00 GMT",
                indexed_old=False, marker=False):
        group = "com.google.devtools.ksp" if marker else "org.example"
        artifact = "com.google.devtools.ksp.gradle.plugin" if marker else "demo"
        version = "2.3.14" if marker else "1.2.3"
        files = {"gradle.lockfile": f"{group}:{artifact}:{version}=runtimeClasspath\n"}
        base = "https://plugins.gradle.org/m2/" if portal else "https://repo1.maven.org/maven2/"
        calls = []
        def opener(request, timeout):
            url = request.full_url
            calls.append((url, request.get_method()))
            if "search.maven.org" in url:
                docs = ([{"g": group, "a": artifact, "v": "1.0.0", "timestamp": 1577836800000}]
                        if indexed_old else [])
                return Response({"response": {"numFound": len(docs), "docs": docs}})
            if "api.osv.dev" in url:
                return Response({})
            if missing or not url.startswith(base):
                raise HTTPError(url, 404, "not found", None, None)
            if url.endswith("maven-metadata.xml"):
                return Response(metadata if metadata is not None else
                    f"<metadata><groupId>{group}</groupId><artifactId>{artifact}</artifactId>"
                    f"<versioning><versions><version>{version}</version></versions>"
                    "</versioning></metadata>", raw=True)
            if request.get_method() == "HEAD":
                response = Response("")
                response.headers = {"Last-Modified": date}
                return response
            if url.endswith(".pom"):
                return Response(f"<project><groupId>{group}</groupId><artifactId>{artifact}</artifactId>"
                                f"<version>{version}</version></project>", raw=True)
            raise AssertionError(url)
        return files, opener, calls, base

    def test_unindexed_central_and_portal_versions(self):
        for options in ({}, {"indexed_old": True}, {"marker": True},
                        {"portal": True, "marker": True}):
            with self.subTest(options=options):
                files, opener, calls, base = self.fixture(**options)
                findings, reason = vetting.vet({}, files, now=NOW, opener=opener)
                self.assertIsNone(reason)
                self.assertIn("2020-01-01", findings[0].first_published)
                self.assertTrue(findings[0].source.startswith(base))
                repository = "Gradle Plugin Portal" if options.get("portal") else "Maven Central"
                self.assertIn(repository, findings[0].publisher)
                self.assertTrue(any(url.startswith(base) and method == "HEAD" for url, method in calls))

    def test_missing_or_invalid_repository_evidence_blocks(self):
        for options in ({"missing": True}, {"metadata": "<broken"},
                        {"metadata": "<metadata/>"}, {"date": ""},
                        {"metadata": '<!DOCTYPE x><metadata/>'},
                        {"metadata": "<metadata><groupId>other</groupId><artifactId>demo</artifactId>"
                         "<versioning><versions><version>1.2.3</version></versions></versioning></metadata>"},
                        {"metadata": "<metadata><groupId>org.example</groupId><artifactId>demo</artifactId>"
                         "<versioning><versions><version>1.2.2</version></versions></versioning></metadata>"}):
            with self.subTest(options=options):
                files, opener, _, _ = self.fixture(**options)
                with self.assertRaises(vetting.VettingError):
                    vetting.vet({}, files, now=NOW, opener=opener)

    def test_transport_retry_recovers_and_is_bounded_at_each_request_kind(self):
        for target in ("search.maven.org", "maven-metadata.xml", ".pom"):
            for recover in (True, False):
                with self.subTest(target=target, recover=recover):
                    files, clean, _, _ = self.fixture()
                    attempts = []
                    def opener(request, timeout):
                        if target in request.full_url and (target != ".pom" or request.get_method() == "HEAD"):
                            attempts.append(request.full_url)
                            if not recover or len(attempts) == 1:
                                raise TimeoutError("registry timed out")
                        return clean(request, timeout)
                    if recover:
                        _, reason = vetting.vet({}, files, now=NOW, opener=opener)
                        self.assertIsNone(reason)
                    else:
                        with self.assertRaises(vetting.VettingError):
                            vetting.vet({}, files, now=NOW, opener=opener)
                    self.assertEqual(len(attempts), 2)


ECOSYSTEM_FILES = {"npm": npm_files, "pypi": python_files,
                   "swift": swift_files, "gradle": gradle_files}
BLOCKERS = ("nonexistent", "recent", "high", "critical", "identity",
            "version", "registry", "osv")


def completion_fixture(ecosystem, scenario="clean", date="2020-01-01T00:00:00Z"):
    """Fresh manifest and HTTP fixtures shared by checker and real ship tests."""
    files = ECOSYSTEM_FILES[ecosystem]()
    if scenario == "identity":
        files = {
            "npm": {"package.json": '{"dependencies":{"demo":"github:example/demo"}}'},
            "pypi": {"requirements.txt": "https://example.test/unknown.whl"},
            "swift": {"Package.resolved": json.dumps({"version": 3, "pins": [{
                "kind": "remoteSourceControl", "state": {"version": "1.2.3"}}]})},
            "gradle": {"gradle.lockfile": "unknown=runtimeClasspath"},
        }[ecosystem]
    elif scenario == "version":
        if ecosystem == "npm":
            del files["package-lock.json"]
        elif ecosystem == "pypi":
            del files["uv.lock"]
        elif ecosystem == "swift":
            files = swift_files(None)
        else:
            files = gradle_files("1.+")
    if scenario == "recent":
        date = "2026-10-07T12:00:00Z"  # exactly 13 days before NOW
    vulns = ([{"id": "OSV-completion", "database_specific": {"severity": scenario.upper()}}]
             if scenario in ("high", "critical") else [])
    basic, _ = DependencyVettingTests().opener(npm_date=date, pypi_date=date, vulns=vulns)
    resolved = SwiftGradleTests().opener(date=date, vulns=vulns)

    def opener(request, timeout):
        osv = request.full_url == "https://api.osv.dev/v1/query"
        if scenario == "nonexistent" and not osv:
            raise HTTPError(request.full_url, 404, "not found", None, None)
        if (scenario == "registry" and not osv) or (scenario == "osv" and osv):
            raise URLError("fixture lookup unavailable")
        return (basic if ecosystem in ("npm", "pypi") else resolved)(request, timeout)

    return files, opener


class CompletionEvidenceTests(unittest.TestCase):
    def test_report_fields_for_every_ecosystem(self):
        identities = {"npm": "safe-package@1.2.3", "pypi": "requests@2.31.0",
                      "swift": "https://github.com/example/demo@1.2.3",
                      "gradle": "org.example:demo@1.2.3"}
        metadata = {"npm": ("Publisher", "https://example.test/source"),
                    "pypi": ("Publisher", "https://example.test/source"),
                    "swift": ("example", "https://github.com/example/demo"),
                    "gradle": ("Example Org", "https://github.com/example/demo")}
        for ecosystem in ECOSYSTEM_FILES:
            for scenario in ("clean", "high", "critical"):
                with self.subTest(ecosystem=ecosystem, scenario=scenario):
                    files, opener = completion_fixture(ecosystem, scenario)
                    findings, reason = vetting.vet({}, files, now=NOW, opener=opener)
                    self.assertEqual(bool(reason), scenario != "clean")
                    report = findings[0].report()
                    for value in (ecosystem, identities[ecosystem], *metadata[ecosystem],
                                  "2020-01-01T00:00:00+00:00", "publisher", "source",
                                  "advisories none" if scenario == "clean"
                                  else f"OSV-completion ({scenario})"):
                        self.assertIn(value, report)

    def test_missing_optional_publisher_source_are_reported_as_unavailable(self):
        for ecosystem in ("npm", "pypi"):
            with self.subTest(ecosystem=ecosystem):
                files, clean = completion_fixture(ecosystem)
                def opener(request, timeout):
                    data = json.loads(clean(request, timeout).body)
                    for key in ("maintainers", "repository"):
                        data.pop(key, None)
                    if "info" in data:
                        data["info"] = {}
                    return Response(data)
                findings, reason = vetting.vet({}, files, now=NOW, opener=opener)
                self.assertIsNone(reason)  # legitimacy remains a review judgment
                self.assertIn("publisher unavailable; source unavailable", findings[0].report())

    def test_publication_age_and_eligible_time(self):
        # 6 days ago: recent, age-only block, eligible ~8 days from NOW
        six_days_ago = (NOW - timedelta(days=6)).isoformat()
        files, opener = completion_fixture("npm", date=six_days_ago)
        res = vetting.vet({}, files, now=NOW, opener=opener)
        findings, reason = res[0], res[1]
        self.assertEqual(len(findings), 1)
        self.assertTrue(findings[0].recent)
        self.assertTrue(findings[0].blocked)
        self.assertIn("published less than 14 days ago: safe-package", reason)
        self.assertNotIn("high/critical advisory", reason)
        expected_eligible = (NOW - timedelta(days=6) + vetting.MIN_AGE).isoformat()
        self.assertEqual(findings[0].eligible_at, expected_eligible)
        self.assertEqual(getattr(res, "eligible_at", None), expected_eligible)

        # 13 days ago: recent, age-only block, eligible ~1 day from NOW
        thirteen_days_ago = (NOW - timedelta(days=13)).isoformat()
        files13, opener13 = completion_fixture("npm", date=thirteen_days_ago)
        res13 = vetting.vet({}, files13, now=NOW, opener=opener13)
        self.assertTrue(res13[0][0].recent)
        expected_eligible13 = (NOW - timedelta(days=13) + vetting.MIN_AGE).isoformat()
        self.assertEqual(res13[0][0].eligible_at, expected_eligible13)

        # Exactly 14 days ago: not recent, not blocked by age
        fourteen_days_ago = (NOW - timedelta(days=14)).isoformat()
        files14, opener14 = completion_fixture("npm", date=fourteen_days_ago)
        res14 = vetting.vet({}, files14, now=NOW, opener=opener14)
        self.assertFalse(res14[0][0].recent)
        self.assertFalse(res14[0][0].blocked)
        self.assertIsNone(res14[1])
        self.assertIsNone(res14[0][0].eligible_at)
        self.assertIsNone(getattr(res14, "eligible_at", None))

        # 6 days ago with high advisory: recent and blocked, but eligible_at is None due to advisory
        files_vuln, opener_vuln = completion_fixture("npm", scenario="high", date=six_days_ago)
        res_vuln = vetting.vet({}, files_vuln, now=NOW, opener=opener_vuln)
        self.assertTrue(res_vuln[0][0].recent)
        self.assertTrue(res_vuln[0][0].blocked)
        self.assertIn("high/critical advisory", res_vuln[1])
        self.assertIn("published less than 14 days ago", res_vuln[1])
        self.assertIsNone(res_vuln[0][0].eligible_at)
        self.assertIsNone(getattr(res_vuln, "eligible_at", None))

    def test_transient_error_classification(self):
        files, _ = completion_fixture("npm")
        def timeout_opener(request, timeout):
            raise TimeoutError("connection timed out")

        with self.assertRaises(vetting.VettingError) as cm:
            vetting.vet({}, files, now=NOW, opener=timeout_opener)
        self.assertTrue(vetting.is_transient_error(cm.exception))
        self.assertTrue(getattr(cm.exception, "transient", False))



if __name__ == "__main__":
    unittest.main()
