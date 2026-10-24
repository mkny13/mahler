"""Fail-closed vetting of changed npm, PyPI, Swift and Gradle dependencies."""

import json
import math
import re
import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree


class VettingError(RuntimeError):
    """Dependency evidence is incomplete or could not be obtained."""


@dataclass(frozen=True)
class Dependency:
    ecosystem: str
    name: str
    declaration: str
    version: str | None
    revision: str | None = None


@dataclass(frozen=True)
class Finding:
    dependency: Dependency
    first_published: str
    publisher: str
    source: str
    advisories: tuple[str, ...]
    blocked: bool
    recent: bool

    def report(self):
        advisories = "; ".join(self.advisories) if self.advisories else "none"
        publication = ("version published" if self.dependency.ecosystem in {"swift", "gradle"}
                       else "first published")
        return (f"{self.dependency.ecosystem} {self.dependency.name}@"
                f"{self.dependency.version}: {publication} "
                f"{self.first_published}; publisher {self.publisher}; "
                f"source {self.source}; advisories {advisories}"
                f"{' [BLOCKED]' if self.blocked else ''}")


_RELEVANT = re.compile(
    r"(?:^|/)(?:package\.json|package-lock\.json|npm-shrinkwrap\.json|"
    r"pyproject\.toml|uv\.lock|poetry\.lock|requirements[^/]*\.(?:txt|in)|"
    r"constraints[^/]*\.txt|setup\.py|setup\.cfg|"
    r"Package\.swift|Package\.resolved|build\.gradle(?:\.kts)?|"
    r"settings\.gradle(?:\.kts)?|gradle\.properties|libs\.versions\.toml|"
    r"gradle\.lockfile|gradle/dependency-locks/[^/]+\.lockfile|pnpm-lock\.yaml|yarn\.lock|Pipfile|Pipfile\.lock)$"
)
_UNSUPPORTED_LOCKS = {
    "pnpm-lock.yaml", "yarn.lock", "Pipfile", "Pipfile.lock",
    "setup.py", "setup.cfg",
}
_GRADLE_MANIFESTS = {
    "build.gradle", "build.gradle.kts",
    "settings.gradle", "settings.gradle.kts",
    "gradle.properties", "libs.versions.toml",
}
_PEP_REQ = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]+\])?\s*(.*)$")


def relevant_path(path):
    return bool(_RELEVANT.search(path))


def _canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _json(text, path):
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise VettingError(f"cannot parse {path}: {exc}") from None


def _add(direct, ecosystem, name, declaration):
    key = ecosystem, (_canonical(name) if ecosystem == "pypi" else name.lower())
    previous = direct.get(key)
    value = (name, str(declaration))
    if previous is not None and previous[1] != value[1]:
        raise VettingError(f"ambiguous direct dependency declaration for {name}")
    if previous is None:
        direct[key] = value


def _package_manifest(text, path, direct):
    data = _json(text, path)
    if not isinstance(data, dict):
        raise VettingError(f"ambiguous dependency manifest {path}")
    if data.get("workspaces"):
        raise VettingError(f"workspace dependency resolution is ambiguous in {path}")
    for group in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        entries = data.get(group, {})
        if entries is None:
            continue
        if not isinstance(entries, dict):
            raise VettingError(f"ambiguous {group} in {path}")
        for name, declaration in entries.items():
            if not isinstance(name, str) or not isinstance(declaration, str):
                raise VettingError(f"ambiguous dependency in {path}")
            _add(direct, "npm", name, declaration)


def _pep_requirement(line, path, direct):
    line = line.split("#", 1)[0].strip()
    if not line or line.startswith(("-r", "--require", "-c", "--constraint")):
        if line.startswith(("-r", "--require", "-c", "--constraint")):
            raise VettingError(f"indirect requirements are ambiguous in {path}")
        return
    if line.startswith(("-", ".", "git+", "http:", "https:")):
        raise VettingError(f"unsupported direct requirement in {path}: {line[:80]}")
    markerless = line.split(";", 1)[0].strip()
    match = _PEP_REQ.fullmatch(markerless)
    if not match:
        raise VettingError(f"cannot identify direct requirement in {path}: {line[:80]}")
    name, spec = match.groups()
    if spec and not re.fullmatch(
            r"(?:\s*(?:===|==|~=|!=|<=|>=|<|>)\s*[A-Za-z0-9.*+!_-]+"
            r"(?:\s*,\s*(?:===|==|~=|!=|<=|>=|<|>)\s*[A-Za-z0-9.*+!_-]+)*)?",
            spec):
        raise VettingError(f"ambiguous version declaration in {path}: {name}")
    _add(direct, "pypi", name, spec.strip())


def _pyproject(text, path, direct):
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, TypeError) as exc:
        raise VettingError(f"cannot parse {path}: {exc}") from None
    project = data.get("project", {})
    if set(project.get("dynamic", [])) & {"dependencies", "optional-dependencies"}:
        raise VettingError(f"dynamic dependencies are ambiguous in {path}")
    for entries in data.get("dependency-groups", {}).values():
        if not isinstance(entries, list):
            raise VettingError(f"ambiguous dependency group in {path}")
        for requirement in entries:
            if isinstance(requirement, dict) and set(requirement) == {"include-group"}:
                # Every group is inspected, so inclusion adds no new requirements.
                if requirement["include-group"] not in data["dependency-groups"]:
                    raise VettingError(f"missing dependency group in {path}")
                continue
            _pep_requirement(requirement, path, direct)
    if (data.get("tool", {}).get("uv", {}).get("sources")
            or data.get("tool", {}).get("uv", {}).get("workspace")):
        raise VettingError(f"custom Python dependency sources are ambiguous in {path}")
    if isinstance(project, dict):
        for requirement in project.get("dependencies", []):
            _pep_requirement(requirement, path, direct)
        for entries in project.get("optional-dependencies", {}).values():
            for requirement in entries:
                _pep_requirement(requirement, path, direct)
    build_system = data.get("build-system", {})
    if not isinstance(build_system, dict):
        raise VettingError(f"ambiguous build-system in {path}")
    build_requirements = build_system.get("requires", [])
    if not isinstance(build_requirements, list):
        raise VettingError(f"ambiguous build-system requires in {path}")
    for requirement in build_requirements:
        if not isinstance(requirement, str):
            raise VettingError(f"ambiguous build-system requirement in {path}")
        _pep_requirement(requirement, path, direct)
    poetry = data.get("tool", {}).get("poetry", {})
    groups = [poetry.get("dependencies", {}), poetry.get("dev-dependencies", {})]
    groups.extend(g.get("dependencies", {}) for g in poetry.get("group", {}).values())
    for entries in groups:
        if not isinstance(entries, dict):
            raise VettingError(f"ambiguous Poetry dependencies in {path}")
        for name, spec in entries.items():
            if name.lower() == "python":
                continue
            if isinstance(spec, dict):
                if set(spec) & {"git", "path", "url", "source"}:
                    raise VettingError(f"custom Poetry source for {name} in {path}")
                spec = spec.get("version")
            if not isinstance(spec, str):
                raise VettingError(f"ambiguous Poetry dependency {name} in {path}")
            _add(direct, "pypi", name, spec)


def _direct_dependencies(files):
    direct = {}
    for path, text in files.items():
        if path in ("package.json",) or path.endswith("/package.json"):
            _package_manifest(text, path, direct)
        elif path.rsplit("/", 1)[-1] == "pyproject.toml":
            _pyproject(text, path, direct)
        elif re.fullmatch(r"requirements[^/]*\.txt", path.rsplit("/", 1)[-1]):
            for line in text.splitlines():
                _pep_requirement(line, path, direct)
    return direct


def _npm_source(package, name, path):
    if not isinstance(package, dict):
        raise VettingError(f"ambiguous npm resolution for {name} in {path}")
    resolved = package.get("resolved")
    if (package.get("link") or package.get("name", name) != name
            or (resolved and (urlsplit(resolved).scheme != "https"
                              or urlsplit(resolved).netloc != "registry.npmjs.org"))):
        raise VettingError(f"non-registry npm resolution for {name} in {path}")


def _npm_versions(files, direct):
    versions = {}

    def add(name, version, path):
        key = name.lower()
        if ("npm", key) not in direct:
            return
        if key in versions and versions[key] != version:
            raise VettingError(f"ambiguous lockfile resolution for npm {name}: {path}")
        versions[key] = version

    for path, text in files.items():
        if path.rsplit("/", 1)[-1] not in ("package-lock.json", "npm-shrinkwrap.json"):
            continue
        data = _json(text, path)
        if not isinstance(data, dict):
            raise VettingError(f"ambiguous lockfile {path}")
        packages = data.get("packages")
        if isinstance(packages, dict):
            for key, package in packages.items():
                if key.startswith("node_modules/") and key.count("node_modules/") == 1:
                    name = key[len("node_modules/"):]
                    version = package.get("version") if isinstance(package, dict) else None
                    if ("npm", name.lower()) in direct:
                        _npm_source(package, name, path)
                    if version:
                        add(name, version, path)
        else:
            for name, package in data.get("dependencies", {}).items():
                if isinstance(package, dict) and package.get("version"):
                    if ("npm", name.lower()) in direct:
                        _npm_source(package, name, path)
                    add(name, package["version"], path)
    return versions


def _python_versions(files, direct):
    versions = {}
    seen = {}
    for path, text in files.items():
        basename = path.rsplit("/", 1)[-1]
        if basename not in ("uv.lock", "poetry.lock"):
            continue
        if basename == "uv.lock":
            try:
                packages = tomllib.loads(text).get("package", [])
            except tomllib.TOMLDecodeError as exc:
                raise VettingError(f"cannot parse {path}: {exc}") from None
        else:
            try:
                packages = tomllib.loads(text).get("package", [])
            except tomllib.TOMLDecodeError as exc:
                raise VettingError(f"cannot parse {path}: {exc}") from None
        for package in packages:
            if not isinstance(package, dict):
                raise VettingError(f"ambiguous package entry in {path}")
            name, version = package.get("name"), package.get("version")
            if not isinstance(name, str) or not isinstance(version, str):
                raise VettingError(f"unresolved package version in {path}")
            key = _canonical(name)
            if ("pypi", key) not in direct:
                continue
            source = package.get("source", {})
            if source and source != {"registry": "https://pypi.org/simple"}:
                raise VettingError(f"non-registry Python resolution for {name} in {path}")
            if key in seen and (seen[key] is None or seen[key] != version):
                seen[key] = None
            else:
                seen[key] = version
    for name, version in seen.items():
        if version is None:
            raise VettingError(f"ambiguous lockfile resolution for PyPI {name}")
        if version:
            versions[name] = version
    for path, text in files.items():
        if re.fullmatch(r"requirements[^/]*\.txt", path.rsplit("/", 1)[-1]):
            for line in text.splitlines():
                candidate = line.split("#", 1)[0].split(";", 1)[0].strip()
                match = _PEP_REQ.fullmatch(candidate)
                if not match:
                    continue
                name, spec = match.groups()
                pinned = re.fullmatch(r"\s*(?:===|==)\s*([0-9][A-Za-z0-9.+_-]*)\s*", spec)
                if pinned and _canonical(name) not in versions:
                    versions[_canonical(name)] = pinned.group(1)
    return versions


def _release_tuple(version):
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", version, re.IGNORECASE)
    if not match:
        return None
    return tuple(map(int, match.group(1).split(".")))


def _compare_releases(left, right):
    width = max(len(left), len(right))
    left = left + (0,) * (width - len(left))
    right = right + (0,) * (width - len(right))
    return (left > right) - (left < right)


def _pep_spec_matches(spec, version):
    if not spec:
        return True
    candidate = _release_tuple(version)
    if candidate is None:
        raise VettingError(f"ambiguous locked Python version: {version}")
    for clause in spec.split(","):
        match = re.fullmatch(
            r"\s*(===|==|!=|~=|<=|>=|<|>)\s*([0-9]+(?:\.[0-9]+)*(?:\.\*)?)\s*",
            clause)
        if not match:
            raise VettingError(f"ambiguous Python version declaration: {spec}")
        operator, bound_text = match.groups()
        wildcard = bound_text.endswith(".*")
        bound = _release_tuple(bound_text[:-2] if wildcard else bound_text)
        if bound is None:
            raise VettingError(f"ambiguous Python version declaration: {spec}")
        comparison = _compare_releases(candidate, bound)
        if wildcard:
            matches = (len(candidate) >= len(bound)
                       and candidate[:len(bound)] == bound)
            if operator == "!=":
                matches = not matches
            elif operator not in ("==", "==="):
                raise VettingError(f"ambiguous Python version declaration: {spec}")
        elif operator in ("==", "==="):
            matches = comparison == 0
        elif operator == "!=":
            matches = comparison != 0
        elif operator == ">=":
            matches = comparison >= 0
        elif operator == "<=":
            matches = comparison <= 0
        elif operator == ">":
            matches = comparison > 0
        elif operator == "<":
            matches = comparison < 0
        else:
            matches = comparison >= 0
            if len(bound) < 2:
                raise VettingError(f"ambiguous compatible-release declaration: {spec}")
            upper = bound[:-2] + (bound[-2] + 1,)
            matches &= _compare_releases(candidate, upper) < 0
        if not matches:
            return False
    return True


def _npm_range_matches(spec, version):
    candidate = _release_tuple(version)
    if candidate is None:
        raise VettingError(f"ambiguous locked npm version: {version}")
    for alternative in spec.split("||"):
        tokens = alternative.strip().split()
        if not tokens:
            raise VettingError(f"ambiguous npm version declaration: {spec}")
        matches = True
        for token in tokens:
            if token in ("*", "x", "X"):
                continue
            match = re.fullmatch(r"(\^|~|>=|<=|>|<|=)?(v?\d+(?:\.\d+)*|v?\d+(?:\.\d+)*\.[xX*])", token)
            if not match:
                raise VettingError(f"ambiguous npm version declaration: {spec}")
            operator, bound_text = match.groups()
            wildcard = bound_text.lower().endswith((".x", ".*"))
            bound = _release_tuple(bound_text[:-2] if wildcard else bound_text)
            if bound is None:
                raise VettingError(f"ambiguous npm version declaration: {spec}")
            if wildcard:
                matches &= (len(candidate) >= len(bound)
                            and candidate[:len(bound)] == bound)
                continue
            comparison = _compare_releases(candidate, bound)
            if operator in (None, "="):
                matches &= comparison == 0
            elif operator == ">=":
                matches &= comparison >= 0
            elif operator == "<=":
                matches &= comparison <= 0
            elif operator == ">":
                matches &= comparison > 0
            elif operator == "<":
                matches &= comparison < 0
            elif operator == "~":
                upper = bound[:1] + (bound[1] + 1,) if len(bound) > 1 else (bound[0] + 1,)
                matches &= comparison >= 0 and _compare_releases(candidate, upper) < 0
            elif operator == "^":
                if bound[0] > 0:
                    upper = (bound[0] + 1,)
                elif len(bound) > 1 and bound[1] > 0:
                    upper = (0, bound[1] + 1)
                else:
                    upper = bound[:2] + (bound[2] + 1,) if len(bound) > 2 else bound + (1,)
                matches &= comparison >= 0 and _compare_releases(candidate, upper) < 0
        if matches:
            return True
    return False


def _version_matches(dependency):
    if dependency.ecosystem == "npm":
        return _npm_range_matches(dependency.declaration, dependency.version)
    return _pep_spec_matches(dependency.declaration, dependency.version)


def _resolved_dependencies(path, text):
    """Read resolved pins without executing Swift or Gradle build scripts.

    These lock formats do not label direct dependencies. Compare pins individually
    so unchanged entries/configuration-only churn never trigger network checks.
    """
    pins = {}
    if path.endswith("Package.resolved"):
        data = _json(text, path)
        schema = data.get("version")
        if schema not in (1, 2, 3):
            raise VettingError(f"unsupported Swift lock schema in {path}")
        entries = data.get("object", {}).get("pins") if schema == 1 else data.get("pins")
        if not isinstance(entries, list):
            raise VettingError(f"invalid Swift pins in {path}")
        for pin in entries:
            if schema != 1 and pin.get("kind") != "remoteSourceControl":
                raise VettingError(f"unsupported Swift source in {path}")
            source = pin.get("repositoryURL" if schema == 1 else "location")
            if not isinstance(source, str) or not re.fullmatch(
                    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source):
                raise VettingError(f"unsupported Swift repository in {path}: {source}")
            source = source.removesuffix(".git")
            state = pin.get("state", {})
            dep = Dependency("swift", source, source, state.get("version"), state.get("revision"))
            if source in pins:
                raise VettingError(f"duplicate Swift pin in {path}: {source}")
            pins[source] = dep
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("empty="):
                continue
            coordinate = line.split("=", 1)[0]
            match = re.fullmatch(r"([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+):([0-9][A-Za-z0-9._-]*)", coordinate)
            if not match or match.group(3).upper().endswith("-SNAPSHOT"):
                raise VettingError(f"unresolved Gradle coordinate in {path}: {coordinate}")
            group, artifact, version = match.groups()
            name = f"{group}:{artifact}"
            if name in pins:
                raise VettingError(f"duplicate Gradle pin in {path}: {name}")
            pins[name] = Dependency("gradle", name, coordinate, version)
    return pins


def _project_dir(path):
    if path.startswith("gradle/dependency-locks/"):
        return ""
    if "/gradle/dependency-locks/" in path:
        return path.split("/gradle/dependency-locks/")[0]
    if path == "gradle/libs.versions.toml":
        return ""
    if path.endswith("/gradle/libs.versions.toml"):
        return path[:-len("/gradle/libs.versions.toml")]
    return path.rpartition("/")[0]


def _changed_resolved_dependencies(before, after):
    changed = {}
    for path, text in after.items():
        if not (path.endswith("Package.resolved")
                or path.rsplit("/", 1)[-1] == "gradle.lockfile"
                or re.search(r"(?:^|/)gradle/dependency-locks/[^/]+\.lockfile$", path)
                ) or before.get(path) == text:
            continue
        new = _resolved_dependencies(path, text)
        old = _resolved_dependencies(path, before[path]) if path in before else {}
        for name, dep in new.items():
            if old.get(name) != dep:
                key = (dep.ecosystem, dep.name)
                if key in changed and (changed[key].version != dep.version or changed[key].revision != dep.revision):
                    raise VettingError(f"ambiguous lockfile resolution for {dep.ecosystem} {dep.name}: {path}")
                changed[key] = dep
    return list(changed.values())


def _changed_project_dependencies(before, after):
    """Return only added or changed direct dependencies, without network access."""
    for path in set(before) | set(after):
        basename = path.rsplit("/", 1)[-1]
        unsupported = (basename in _UNSUPPORTED_LOCKS
                       or re.fullmatch(r"requirements[^/]*\.in", basename)
                       or re.fullmatch(r"constraints[^/]*\.txt", basename))
        if unsupported and before.get(path) != after.get(path):
            raise VettingError(f"unsupported dependency lockfile change: {path}")

    swift_manifests = [p for p in set(before) | set(after)
                       if p.endswith("Package.swift") and before.get(p) != after.get(p)]
    if swift_manifests:
        swift_locks = [p for p in set(before) | set(after)
                       if p.endswith("Package.resolved") and p in after
                       and before.get(p) != after.get(p)]
        has_resolved_change = False
        for lock in swift_locks:
            old_pins = _resolved_dependencies(lock, before[lock]) if lock in before else {}
            new_pins = _resolved_dependencies(lock, after[lock])
            if old_pins != new_pins:
                has_resolved_change = True
                break
        if not has_resolved_change:
            raise VettingError(f"unresolved Swift dependency manifest change without lockfile: {swift_manifests[0]}")

    gradle_manifests = [p for p in set(before) | set(after)
                        if p.rsplit("/", 1)[-1] in _GRADLE_MANIFESTS
                        and before.get(p) != after.get(p)]
    if gradle_manifests:
        gradle_locks = [p for p in set(before) | set(after)
                        if (p.rsplit("/", 1)[-1] == "gradle.lockfile"
                            or re.search(r"(?:^|/)gradle/dependency-locks/[^/]+\.lockfile$", p))
                        and p in after and before.get(p) != after.get(p)]
        has_resolved_change = False
        for lock in gradle_locks:
            old_pins = _resolved_dependencies(lock, before[lock]) if lock in before else {}
            new_pins = _resolved_dependencies(lock, after[lock])
            if old_pins != new_pins:
                has_resolved_change = True
                break
        if not has_resolved_change:
            raise VettingError(f"unresolved Gradle dependency manifest change without lockfile: {gradle_manifests[0]}")

    old_direct, new_direct = _direct_dependencies(before), _direct_dependencies(after)
    old_versions = {"npm": _npm_versions(before, old_direct), "pypi": _python_versions(before, old_direct)}
    new_versions = {"npm": _npm_versions(after, new_direct), "pypi": _python_versions(after, new_direct)}
    changed = _changed_resolved_dependencies(before, after)
    for key, (name, declaration) in new_direct.items():
        old = old_direct.get(key)
        version = new_versions[key[0]].get(key[1])
        old_version = old_versions[key[0]].get(key[1])
        # Exact declarations are sufficient when there is no lock evidence.
        pin = r"(?:===|==)\s*([0-9][A-Za-z0-9.+_-]*)" if key[0] == "pypi" else r"=?([0-9]+\.[0-9]+\.[0-9]+)"
        if version is None:
            match = re.fullmatch(pin, declaration.strip())
            version = match.group(1) if match else None
        if old_version is None and old:
            match = re.fullmatch(pin, old[1].strip())
            old_version = match.group(1) if match else None
        if old is None or old[1] != declaration or old_version != version:
            if key[0] == "npm" and declaration.strip().lower().startswith(
                    ("file:", "link:", "workspace:", "git:", "git+", "github:", "https:")):
                raise VettingError(f"unsupported npm dependency source for {name}: {declaration}")
            dependency = Dependency(key[0], name, declaration, version)
            if version and not _version_matches(dependency):
                raise VettingError(
                    f"lockfile version {version} does not satisfy {key[0]} "
                    f"dependency {name} declaration {declaration}")
            changed.append(dependency)
    return sorted(changed, key=lambda dep: (dep.ecosystem, _canonical(dep.name)))


def changed_dependencies(before, after):
    """Compare each manifest directory separately; never borrow another project's lock.

    Unsupported workspace/custom-source layouts fail closed when they cannot be
    resolved locally. Unchanged directories need no parsing or registry traffic.
    """
    try:
        directories = {_project_dir(path) for path in set(before) | set(after)}
        changed = []
        for directory in sorted(directories):
            old = {p: t for p, t in before.items() if _project_dir(p) == directory}
            new = {p: t for p, t in after.items() if _project_dir(p) == directory}
            if old != new:
                changed.extend(_changed_project_dependencies(old, new))
        return changed
    except (TypeError, AttributeError, KeyError, ValueError) as exc:
        raise VettingError(f"ambiguous dependency evidence: {exc}") from None


def _request_json(url, *, data=None, opener=None):
    opener = opener or urlopen
    headers = {"Accept": "application/json", "User-Agent": "Mahler dependency vetting"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers)
    try:
        with opener(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            exc.close()
            return None
        exc.close()
        raise VettingError(f"registry/advisory lookup returned HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VettingError(f"registry/advisory lookup failed: {exc}") from None


_POM_LIMIT = 1_000_000


def _date(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _metadata(dep, opener):
    if dep.ecosystem in {"swift", "gradle"}:
        return _resolved_metadata(dep, opener)
    if dep.ecosystem == "npm":
        url = f"https://registry.npmjs.org/{quote(dep.name, safe='')}"
    else:
        url = f"https://pypi.org/pypi/{quote(dep.name, safe='')}/json"
    try:
        result = _request_json(url, opener=opener)
    except VettingError as exc:
        raise VettingError(f"{dep.ecosystem} {dep.name}@{dep.version}: {exc}") from None
    if result is None:
        raise VettingError(f"package does not exist: {dep.ecosystem} {dep.name}@{dep.version}")
    if not isinstance(result, dict):
        raise VettingError(f"invalid registry response for {dep.ecosystem} {dep.name}")
    if dep.ecosystem == "npm":
        versions = result.get("versions", {})
        times = result.get("time", {})
        maintainers = result.get("maintainers", [])
        if not isinstance(versions, dict) or not isinstance(times, dict) or not isinstance(maintainers, list):
            raise VettingError(f"invalid registry response for npm {dep.name}")
        version_data = versions.get(dep.version)
        dates = [_date(d) for key, d in times.items() if key != "modified"]
        publisher = ", ".join(m.get("name", "") for m in maintainers
                              if isinstance(m, dict) and m.get("name")) or "unavailable"
        if isinstance(version_data, dict):
            user = version_data.get("_npmUser")
            if isinstance(user, dict) and user.get("name"):
                publisher = str(user["name"])
        repository = (version_data.get("repository") if isinstance(version_data, dict)
                      else None) or result.get("repository", {})
        source = repository.get("url", "unavailable") if isinstance(repository, dict) else str(repository)
    else:
        info = result.get("info", {})
        releases = result.get("releases", {})
        if not isinstance(info, dict) or not isinstance(releases, dict):
            raise VettingError(f"invalid registry response for PyPI {dep.name}")
        version_data = ({"exists": True} if isinstance(releases.get(dep.version), list) and releases[dep.version]
                        else None)
        dates = [_date(upload.get("upload_time_iso_8601") or upload.get("upload_time"))
                 for uploads in releases.values() if isinstance(uploads, list)
                 for upload in uploads if isinstance(upload, dict)]
        publisher = info.get("author") or info.get("maintainer") or "unavailable"
        urls = info.get("project_urls") or {}
        if not isinstance(urls, dict):
            raise VettingError(f"invalid source metadata for PyPI {dep.name}")
        source = next((value for key, value in urls.items()
                       if "source" in key.lower() or "repository" in key.lower()),
                      info.get("home_page") or "unavailable")
    if not isinstance(version_data, dict):
        raise VettingError(f"unresolved registry version: {dep.ecosystem} {dep.name}@{dep.version}")
    first = min((d for d in dates if d), default=None)
    if first is None:
        raise VettingError(f"missing first-publication date: {dep.ecosystem} {dep.name}@{dep.version}")
    return first, str(publisher), str(source)


def _request_text(url, opener):
    request = Request(url, headers={"User-Agent": "Mahler dependency vetting"})
    try:
        with opener(request, timeout=20) as response:
            return response.read(_POM_LIMIT + 1).decode("utf-8")
    except HTTPError as exc:
        code = exc.code
        exc.close()
        if code == 404:
            return None
        raise VettingError(f"registry/advisory lookup returned HTTP {code}") from None
    except (URLError, TimeoutError, OSError, UnicodeError) as exc:
        raise VettingError(f"registry/advisory lookup failed: {exc}") from None


def _pom_metadata(pom_url, group, artifact, opener):
    """Publisher and source declared in the artifact's POM, else Maven fallbacks.

    The POM is only trusted when it names the requested artifact. An absent,
    oversized, unparsable or mismatched POM falls back to the namespace and the
    POM URL, which the review recipe treats as unverified metadata.
    """
    fallback = (f"Maven Central namespace {group}", pom_url)
    text = _request_text(pom_url, opener)
    if text is None or len(text) > _POM_LIMIT or "<!DOCTYPE" in text or "<!ENTITY" in text:
        return fallback
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return fallback

    def child(node, name):
        for element in node:
            if element.tag.rpartition("}")[2] == name:
                return element
        return None

    def value(node, *path):
        for name in path:
            node = child(node, name) if node is not None else None
        return (node.text or "").strip() if node is not None and node.text else ""

    declared_group = value(root, "groupId") or value(root, "parent", "groupId")
    if value(root, "artifactId") != artifact or (declared_group and declared_group != group):
        return fallback
    developers = child(root, "developers")
    names = [value(developer, "name") or value(developer, "id")
             for developer in (developers if developers is not None else ())]
    publisher = (value(root, "organization", "name")
                 or ", ".join(n for n in names if n) or fallback[0])
    source = value(root, "scm", "url") or value(root, "scm", "connection") or fallback[1]
    return publisher, source


def _resolved_metadata(dep, opener):
    if dep.ecosystem == "swift":
        repository = dep.name.removeprefix("https://github.com/")
        base = f"https://api.github.com/repos/{repository}"
        repo = _request_json(base, opener=opener)
        if (not isinstance(repo, dict) or repo.get("full_name", "").lower() != repository.lower()
                or not repo.get("owner", {}).get("login")):
            raise VettingError(f"missing Swift source metadata: {dep.name}")
        release = None
        for tag in (dep.version, "v" + dep.version):
            release = _request_json(base + "/releases/tags/" + quote(tag, safe=""), opener=opener)
            if release is not None:
                break
        if (not isinstance(release, dict) or release.get("draft") is not False
                or release.get("tag_name") not in {dep.version, "v" + dep.version}):
            raise VettingError(f"missing Swift release metadata: {dep.name}@{dep.version}")
        published = _date(release.get("published_at"))
        commit = _request_json(base + "/commits/" + quote(release["tag_name"], safe=""),
                               opener=opener)
        if not isinstance(commit, dict) or commit.get("sha") != dep.revision:
            raise VettingError(f"Swift revision does not match release: {dep.name}@{dep.version}")
        publisher, source = repo["owner"]["login"], dep.name
    else:
        group, artifact = dep.name.split(":")
        query = f'g:"{group}" AND a:"{artifact}" AND v:"{dep.version}"'
        url = "https://search.maven.org/solrsearch/select?q=" + quote(query, safe="") + "&core=gav&rows=2&wt=json"
        result = _request_json(url, opener=opener)
        response = result.get("response", {}) if isinstance(result, dict) else {}
        docs = response.get("docs", [])
        if response.get("numFound") != 1 or not isinstance(docs, list) or len(docs) != 1:
            raise VettingError(f"missing Maven artifact metadata: {dep.name}@{dep.version}")
        doc = docs[0]
        if (doc.get("g"), doc.get("a"), doc.get("v")) != (group, artifact, dep.version):
            raise VettingError(f"mismatched Maven artifact metadata: {dep.name}@{dep.version}")
        stamp = doc.get("timestamp")
        published = None
        if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and math.isfinite(stamp) and stamp > 0:
            try:
                published = datetime.fromtimestamp(stamp / 1000, timezone.utc)
            except (ValueError, OverflowError, OSError):
                pass
        pom_url = ("https://repo1.maven.org/maven2/" + group.replace(".", "/")
                   + f"/{artifact}/{dep.version}/{artifact}-{dep.version}.pom")
        publisher, source = _pom_metadata(pom_url, group, artifact, opener)
    if published is None:
        raise VettingError(f"missing publication date: {dep.ecosystem} {dep.name}@{dep.version}")
    return published, publisher, source


def _cvss3(vector):
    """CVSS 3.0/3.1 base score (FIRST specification section 7.1).

    Unknown/malformed vectors remain ambiguous and cannot authorize a merge.
    https://www.first.org/cvss/v3.1/specification-document
    """
    try:
        parts = vector.split("/")[1:]
        metrics = dict(part.split(":") for part in parts)
        if len(metrics) != len(parts) or metrics["S"] not in {"U", "C"}:
            return None
        changed = metrics["S"] == "C"
        av = {"N": .85, "A": .62, "L": .55, "P": .2}[metrics["AV"]]
        ac = {"L": .77, "H": .44}[metrics["AC"]]
        pr = {"N": .85, "L": .68 if changed else .62,
              "H": .5 if changed else .27}[metrics["PR"]]
        ui = {"N": .85, "R": .62}[metrics["UI"]]
        impact_values = {"N": 0, "L": .22, "H": .56}
        iss = 1 - math.prod(1 - impact_values[metrics[k]] for k in ("C", "I", "A"))
        impact = 7.52 * (iss - .029) - 3.25 * (iss - .02) ** 15 if changed else 6.42 * iss
        if impact <= 0:
            return 0
        total = min((1.08 if changed else 1) * (impact + 8.22 * av * ac * pr * ui), 10)
        return math.ceil(round(total, 5) * 10) / 10
    except (KeyError, ValueError, TypeError):
        return None


def _severity(advisory):
    levels = {"NONE": 0, "LOW": 1, "MODERATE": 2, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
    found = []
    database = advisory.get("database_specific") or {}
    ecosystem = advisory.get("ecosystem_specific") or {}
    if not isinstance(database, dict) or not isinstance(ecosystem, dict):
        raise VettingError(f"invalid severity evidence for {advisory.get('id', 'advisory')}")
    for field in (database.get("severity"), ecosystem.get("severity")):
        if isinstance(field, str) and field.upper() in levels:
            found.append(field.upper())
    severities = advisory.get("severity", [])
    if not isinstance(severities, list):
        raise VettingError(f"invalid severity evidence for {advisory.get('id', 'advisory')}")
    for item in severities:
        if not isinstance(item, dict):
            raise VettingError(f"invalid severity evidence for {advisory.get('id', 'advisory')}")
        score = item.get("score")
        if isinstance(score, str) and score.upper() in levels:
            found.append(score.upper())
            continue
        if isinstance(score, str) and score.startswith(("CVSS:3.0/", "CVSS:3.1/")):
            score = _cvss3(score)
        try:
            numeric = float(score)
        except (ValueError, TypeError):
            found.append("UNKNOWN")
            continue
        if not math.isfinite(numeric) or not 0 <= numeric <= 10:
            found.append("UNKNOWN")
        else:
            found.append("CRITICAL" if numeric >= 9 else "HIGH" if numeric >= 7
                         else "MODERATE" if numeric >= 4 else "LOW" if numeric > 0 else "NONE")
    return max(found, key=lambda level: levels.get(level, 5), default="UNKNOWN")


def _advisories(dep, opener):
    package = {"name": dep.name, "ecosystem": {"npm": "npm", "pypi": "PyPI", "swift": "SwiftURL", "gradle": "Maven"}[dep.ecosystem]}
    payload = json.dumps({"version": dep.version, "package": package}).encode()
    try:
        result = _request_json("https://api.osv.dev/v1/query", data=payload, opener=opener)
    except VettingError as exc:
        raise VettingError(f"{dep.ecosystem} {dep.name}@{dep.version}: {exc}") from None
    if (result is None or not isinstance(result, dict)
            or not isinstance(result.get("vulns", []), list)
            or result.get("next_page_token")):
        raise VettingError(f"invalid OSV response for {dep.ecosystem} {dep.name}@{dep.version}")
    details = []
    blocked = False
    for advisory in result.get("vulns", []):
        if not isinstance(advisory, dict) or not advisory.get("id"):
            raise VettingError(f"ambiguous OSV advisory for {dep.name}@{dep.version}")
        level = _severity(advisory)
        details.append(f"{advisory['id']} ({level.lower()})")
        blocked |= level in {"HIGH", "CRITICAL", "UNKNOWN"}
    return tuple(details), blocked


def vet(before, after, *, now=None, opener=None):
    """Vet changed direct dependencies; return (findings, blocking reason)."""
    dependencies = changed_dependencies(before, after)
    if not dependencies:
        return (), None
    now = now or datetime.now(timezone.utc)
    findings = []
    for dependency in dependencies:
        if (not isinstance(dependency.version, str)
                or not re.fullmatch(r"[0-9][A-Za-z0-9.+_-]*", dependency.version)):
            raise VettingError(f"unresolved exact version for {dependency.ecosystem} "
                               f"{dependency.name}: {dependency.version or 'missing'}")
        if dependency.ecosystem == "swift" and (
                not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?(?:\+[A-Za-z0-9.-]+)?",
                                 dependency.version)
                or not isinstance(dependency.revision, str)
                or not re.fullmatch(r"[0-9a-f]{40}", dependency.revision)):
            raise VettingError(f"unresolved Swift version/revision: {dependency.name}")
    for dependency in dependencies:
        try:
            published, publisher, source = _metadata(dependency, opener)
            advisories, advisory_block = _advisories(dependency, opener)
        except (TypeError, AttributeError, KeyError, ValueError) as exc:
            raise VettingError(f"{dependency.ecosystem} {dependency.name}@{dependency.version}: "
                               f"invalid registry/advisory evidence: {exc}") from None
        recent = published > now - timedelta(days=14)
        blocked = recent or advisory_block
        findings.append(Finding(dependency, published.isoformat(),
                                publisher, source, advisories, blocked, recent))
    reasons = []
    for finding in findings:
        if finding.dependency.version is None:
            reasons.append(f"unresolved version: {finding.dependency.name}")
        if finding.recent:
            reasons.append(f"published less than 14 days ago: {finding.dependency.name}")
        if finding.blocked and any("(high)" in a or "(critical)" in a or "(unknown)" in a
                                   for a in finding.advisories):
            reasons.append(f"high/critical advisory: {finding.dependency.name}")
    return tuple(findings), "; ".join(reasons) or None
