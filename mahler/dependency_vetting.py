"""Fail-closed vetting of changed direct npm and PyPI dependencies."""

import json
import re
import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class VettingError(RuntimeError):
    """Dependency evidence is incomplete or could not be obtained."""


@dataclass(frozen=True)
class Dependency:
    ecosystem: str
    name: str
    declaration: str
    version: str | None


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
        return (f"{self.dependency.ecosystem} {self.dependency.name}@"
                f"{self.dependency.version}: first published "
                f"{self.first_published}; publisher {self.publisher}; "
                f"source {self.source}; advisories {advisories}"
                f"{' [BLOCKED]' if self.blocked else ''}")


_RELEVANT = re.compile(
    r"(?:^|/)(?:package\.json|package-lock\.json|npm-shrinkwrap\.json|"
    r"pyproject\.toml|uv\.lock|poetry\.lock|requirements[^/]*\.(?:txt|in)|"
    r"constraints[^/]*\.txt|setup\.py|setup\.cfg|"
    r"pnpm-lock\.yaml|yarn\.lock|Pipfile|Pipfile\.lock)$"
)
_UNSUPPORTED_LOCKS = {
    "pnpm-lock.yaml", "yarn.lock", "Pipfile", "Pipfile.lock",
    "setup.py", "setup.cfg",
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
    key = ecosystem, _canonical(name)
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


def _npm_versions(files):
    versions = {}

    def add(name, version, path):
        key = _canonical(name)
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
                    if version:
                        add(name, version, path)
        else:
            for name, package in data.get("dependencies", {}).items():
                if isinstance(package, dict) and package.get("version"):
                    add(name, package["version"], path)
    return versions


def _python_versions(files):
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
            if key in seen and seen[key] != version:
                seen[key] = None
            else:
                seen[key] = version
    for name, version in seen.items():
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


def changed_dependencies(before, after):
    """Return only added or changed direct dependencies, without network access."""
    for path in set(before) | set(after):
        basename = path.rsplit("/", 1)[-1]
        unsupported = (basename in _UNSUPPORTED_LOCKS
                       or re.fullmatch(r"requirements[^/]*\.in", basename)
                       or re.fullmatch(r"constraints[^/]*\.txt", basename))
        if unsupported and before.get(path) != after.get(path):
            raise VettingError(f"unsupported dependency lockfile change: {path}")
    old_direct, new_direct = _direct_dependencies(before), _direct_dependencies(after)
    old_versions = {"npm": _npm_versions(before), "pypi": _python_versions(before)}
    new_versions = {"npm": _npm_versions(after), "pypi": _python_versions(after)}
    changed = []
    for key, (name, declaration) in new_direct.items():
        old = old_direct.get(key)
        version = new_versions[key[0]].get(key[1])
        old_version = old_versions[key[0]].get(key[1])
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
        raise VettingError(f"registry/advisory lookup returned HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VettingError(f"registry/advisory lookup failed: {exc}") from None


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
        dates = [_date(d) for d in times.values()]
        publisher = ", ".join(m.get("name", "") for m in maintainers
                              if isinstance(m, dict) and m.get("name")) or "unavailable"
        repository = result.get("repository", {})
        source = repository.get("url", "unavailable") if isinstance(repository, dict) else "unavailable"
    else:
        info = result.get("info", {})
        releases = result.get("releases", {})
        if not isinstance(info, dict) or not isinstance(releases, dict):
            raise VettingError(f"invalid registry response for PyPI {dep.name}")
        version_data = ({"exists": True} if dep.version in releases
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
        if isinstance(score, (int, float)):
            found.append("CRITICAL" if score >= 9 else "HIGH" if score >= 7 else "MODERATE")
        elif isinstance(score, str) and score.upper() in levels:
            found.append(score.upper())
        elif isinstance(score, str):
            match = re.search(r"(?:^|/)CVSS:3(?:\.\d)?/.*?AV:", score)
            if match:
                # OSV often supplies the vector but not its numeric score.
                # Treat a vector without a published score conservatively.
                found.append("UNKNOWN")
    return max(found, key=lambda level: levels.get(level, 5), default="UNKNOWN")


def _advisories(dep, opener):
    package = {"name": dep.name, "ecosystem": "npm" if dep.ecosystem == "npm" else "PyPI"}
    payload = json.dumps({"version": dep.version, "package": package}).encode()
    try:
        result = _request_json("https://api.osv.dev/v1/query", data=payload, opener=opener)
    except VettingError as exc:
        raise VettingError(f"{dep.ecosystem} {dep.name}@{dep.version}: {exc}") from None
    if (result is None or not isinstance(result, dict)
            or not isinstance(result.get("vulns"), list)):
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
        if not dependency.version or not re.fullmatch(r"[0-9][A-Za-z0-9.+_-]*", dependency.version):
            raise VettingError(f"unresolved exact version for {dependency.ecosystem} "
                               f"{dependency.name}: {dependency.version or 'missing'}")
        published, publisher, source = _metadata(dependency, opener)
        advisories, advisory_block = _advisories(dependency, opener)
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
