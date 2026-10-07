"""D39 read-only evidence and proposal engine. Scheduling belongs to the caller.

Workflow inspection deliberately supports a small, literal YAML/shell subset.
Unresolved expressions, reusable workflows and conditional execution are review
questions, not evidence that tests ran. Repository commands are never executed.
"""

import hashlib
import json
import re
import shlex
import subprocess
import tomllib
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from . import config, redact
from .gh import GHError
from .ledger import parse

TITLE = "Cross-project practices audit"
REPORT_URL_KEY = "practices_audit:report_url"
REPORT_MARKER_KEY = "practices_audit:report_marker"


@dataclass(frozen=True)
class Finding:
    check: str
    state: str
    reason: str
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class ProjectAudit:
    project: str
    repo: str
    findings: tuple[Finding, ...]


@dataclass(frozen=True)
class Filing:
    report_url: str
    proposals: dict[str, str]
    errors: tuple[str, ...]


_ASSIGNMENT = re.compile(
    r"""(?ix)\b[\w-]*(?:token|secret|password|api[_-]?key|private[_-]?key)\b
        ["']?\s*[:=]\s*["']?([^\s"'`,;}]+)""")
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")
_PLACEHOLDER = re.compile(
    r"^(?:\$|<|redacted|changeme|example|dummy|placeholder|none|null|false|true)",
    re.IGNORECASE)


def _safe(text):
    return redact.redact(_ASSIGNMENT.sub(
        lambda m: m.group(0).replace(m.group(1), redact.MARK), str(text)))


def _finding(check, state, reason, evidence):
    return Finding(check, state, _safe(reason), tuple(_safe(e) for e in evidence))


class _Tree:
    def __init__(self, path):
        self.root = Path(path).resolve()

    def read(self, name):
        path = self.root / name
        if not path.resolve().is_relative_to(self.root):
            raise OSError("outside repository")
        return path.read_text(encoding="utf-8")

    def tracked(self):
        try:
            result = subprocess.run(
                ["git", "-C", str(self.root), "ls-files", "-z"],
                capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            raise OSError("tracked-file inventory unavailable") from None
        if result.returncode:
            raise OSError("tracked-file inventory unavailable")
        return result.stdout.decode("utf-8").split("\0")[:-1]


def _pointer(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    match = re.search(r"\[[^]]+\]\(([^)]+\.md)\)|(?:@|`)([^`\s]+\.md)`?", lines[0])
    if match:
        return match.group(1) or match.group(2)
    match = re.fullmatch(r"(?:See|Read|Follow)\s+([\w./-]+\.md)\.?", lines[0], re.I)
    return match.group(1) if match else None


def _effective(tree, name, seen=()):
    if name in seen or len(seen) >= 8:
        raise OSError("cyclic instruction pointer")
    text = tree.read(name)
    target = _pointer(text)
    if target:
        return _effective(tree, str(Path(name).parent / target), (*seen, name))
    return name, text


def _instructions(tree, project):
    evidence = []
    try:
        source, agents = _effective(tree, "AGENTS.md")
        evidence.append(f"AGENTS.md resolves to {source}")
    except FileNotFoundError:
        exists = (tree.root / "AGENTS.md").exists()
        state = "unknown" if exists else "gap"
        reason = "AGENTS.md pointer target is missing." if exists else "AGENTS.md is missing."
        return tuple(_finding(check, state, reason, ["AGENTS.md"])
                     for check in ("agent-instructions", "mahler-guidance"))
    except (OSError, UnicodeError):
        return tuple(_finding(c, "unknown", "Instruction source cannot be read or resolved.",
                              ["AGENTS.md"]) for c in ("agent-instructions", "mahler-guidance"))
    try:
        claude_source, claude = _effective(tree, "CLAUDE.md")
        evidence.append(f"CLAUDE.md resolves to {claude_source}")
        same = claude.strip() == agents.strip()
        instructions = _finding("agent-instructions", "pass" if same else "gap",
                                "Effective instructions agree." if same else
                                "AGENTS.md and CLAUDE.md diverge.", evidence)
    except FileNotFoundError:
        exists = (tree.root / "CLAUDE.md").exists()
        instructions = _finding("agent-instructions", "unknown" if exists else "pass",
                                "CLAUDE.md pointer target is missing." if exists else
                                "AGENTS.md is the sole instruction source; no CLAUDE.md.", evidence)
    except (OSError, UnicodeError):
        instructions = _finding("agent-instructions", "unknown",
                                "CLAUDE.md cannot be read or resolved.", evidence)
    normalized = re.sub(r"\s+", " ", agents)
    markers = [f"mahler {verb} {project}#" for verb in ("claim", "heartbeat", "ship", "release")]
    markers += ["mahler/<N>-short-slug", "Fixes #N", "STATUS", "verify", "commit", "push"]
    missing = [marker for marker in markers if marker not in normalized]
    conductor = re.search(r"conductor.{0,100}(?:opens|open).{0,100}"
                          r"(?:reviews|review|watches).{0,100}(?:merges|merge)",
                          normalized, re.I)
    if not conductor:
        missing.append("conductor opens/reviews/merges")
    citations = [f"{source}:{i}: canonical guidance marker"
                 for i, line in enumerate(agents.splitlines(), 1)
                 if any(marker in line for marker in markers)]
    guidance = _finding("mahler-guidance", "gap" if missing else "pass",
                        "Missing/stale markers: " + ", ".join(missing) if missing else
                        "Canonical participation markers present.",
                        [*evidence, *citations])
    return instructions, guidance


def _words(command):
    try:
        words = shlex.split(command, comments=True)
    except ValueError:
        return []
    if words and re.fullmatch(r"python(?:3(?:\.\d+)?)?", words[0]):
        words[0] = "python"
    return words


def _is_test(command):
    w = _words(command)
    if not w:
        return False
    if any(x in w for x in ("--help", "-h", "--version", "--collect-only", "--dry-run")):
        return False
    # pytest-xdist uses -n for workers, unlike dry-run flags in other runners.
    pytest = w[0] == "pytest" or w[:3] == ["python", "-m", "pytest"]
    if "-n" in w and not pytest:
        return False
    return bool(
        w[0] in ("pytest", "nosetests", "jest", "vitest", "mocha")
        or w[:3] in (["python", "-m", "unittest"], ["python", "-m", "pytest"])
        or w[:2] in (["go", "test"], ["cargo", "test"], ["swift", "test"],
                     ["dotnet", "test"], ["bundle", "exec"])
        and (w[:2] != ["bundle", "exec"] or w[2:3] == ["rspec"])
        or w[0] in ("gradle", "./gradlew") and any(
            re.fullmatch(r"(?:[\w:]*:)?(?:test|test\w+|connected\w*Test)", x) for x in w[1:])
        or w[0] == "xcodebuild" and "test" in w[1:]
        or w[:3] == ["npx", "playwright", "test"])


def _expand(tree, command, seen=()):
    """Literal commands and referenced package/shell/Python scripts, with citations."""
    rows = [(command, "")]
    if command in seen or len(seen) >= 5:
        return rows, True
    words = _words(command)
    if not words:
        return rows, True
    script = None
    nested = None
    if words[0] in ("npm", "pnpm", "yarn"):
        if words[1:2] in (["ci"], ["install"]):
            return rows, False
        key = words[2] if len(words) > 2 and words[1] == "run" else (
            words[1] if len(words) > 1 else "")
        try:
            scripts = json.loads(tree.read("package.json")).get("scripts", {})
            nested = scripts.get(key)
        except (OSError, UnicodeError, ValueError):
            return rows, True
        if not isinstance(nested, str):
            return rows, True
        script = "package.json"
    elif words[0] in ("bash", "sh", "python") and len(words) > 1 and not words[1].startswith("-"):
        script = words[1]
    elif words[0].startswith("./"):
        script = words[0]
    if not script:
        return rows, "${{" in command or words[0] in (
            "make", "just", "if", "then", "else", "for", "while", "case", "eval", "source", ".")
    try:
        text = nested if nested is not None else tree.read(script)
    except (OSError, UnicodeError):
        return rows, True
    if script.endswith(".py"):
        # Arbitrary Python control flow cannot prove a test runner is executed.
        return rows, True
    uncertain = bool(re.search(r"(?m)^\s*(?:if|for|while|case)\b", text))
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        for part in re.split(r"\s*(?:&&|;)\s*", line):
            expanded, unknown = _expand(tree, part, (*seen, command))
            rows.extend((cmd, f"{script}:{number}" + (f" -> {cite}" if cite else ""))
                        for cmd, cite in expanded)
            uncertain |= unknown
    return rows, uncertain


def _yaml_line(line):
    quote = None
    escaped = False
    for i, char in enumerate(line):
        if escaped:
            escaped = False
        elif char == "\\" and quote == '"':
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line


def _workflow(tree, path):
    """Extract literal PR workflow run steps and static job names, not YAML names."""
    text = tree.read(path)
    lines = [_yaml_line(line) for line in text.splitlines()]
    text = "\n".join(lines)
    trigger = re.search(r"(?m)^(?:on|['\"]on['\"]):\s*(.*)$", text)
    if not trigger:
        return [], True, []
    start = trigger.start()
    next_root = re.search(r"(?m)^[^\s#][^:\n]*:", text[trigger.end():])
    end = trigger.end() + next_root.start() if next_root else len(text)
    on = text[start:end]
    if not re.search(r"\bpull_request\b", on):
        return [], False, []
    rows, jobs = [], []
    job = ""
    uncertain = bool(re.search(r"\$\{\{|^\s*(?:-\s*)?(?:if|continue-on-error|working-directory):"
                               r"|^\s*uses:\s*[^ \n]*\.github/workflows/"
                               r"|[&*][\w-]+", text, re.M))
    if re.search(r"(?m)^jobs:[ \t]*\S", text):
        uncertain = True
    if re.search(r"^\s+(?:paths|paths-ignore|branches|branches-ignore|types):", on, re.M):
        uncertain = True
    i = 0
    while i < len(lines):
        line = lines[i]
        match = re.match(r"^  ([\w-]+):\s*$", line)
        if match and match.group(1) != "pull_request":
            job = match.group(1)
        name = re.match(r"^    name:\s*(.+)", line)
        if name:
            job = name.group(1).strip("'\"")
        run = re.match(r"^(\s*)(?:-\s*)?run:\s*(.*)$", line)
        if run:
            number = i + 1
            command = run.group(2).strip()
            if command in ("|", "|-", "|+", ">", ">-", ">+"):
                indent = len(run.group(1)) + (2 if line.lstrip().startswith("-") else 0)
                body = []
                while i + 1 < len(lines):
                    following = lines[i + 1]
                    if following.strip() and len(following) - len(following.lstrip()) <= indent:
                        break
                    i += 1
                    body.append(following.strip())
                command = "\n".join(body) if command.startswith("|") else " ".join(body)
            else:
                command = command.strip("'\"")
            for part in re.split(r"\s*(?:&&|;|\n)\s*", command):
                if not part or part.startswith("#"):
                    continue
                expanded, unknown = _expand(tree, part)
                uncertain |= unknown
                for cmd, cite in expanded:
                    rows.append((cmd, f"{path}:{number} job={job} run: {cmd}"
                                 + (f" -> {cite}" if cite else "")))
                    if _is_test(cmd) and job not in jobs:
                        jobs.append(job)
        i += 1
    if not rows:
        uncertain = True
    return rows, uncertain, jobs


def _ci(tree):
    rows, jobs, errors = [], [], []
    workflows = sorted((tree.root / ".github/workflows").glob("*.y*ml"))
    uncertain = False
    for path in workflows:
        name = str(path.relative_to(tree.root))
        try:
            commands, unknown, names = _workflow(tree, name)
            rows.extend(commands)
            jobs.extend(names)
            uncertain |= unknown
        except (OSError, UnicodeError):
            errors.append(f"{name}: unreadable workflow")
            uncertain = True
    tests = [(cmd, cite) for cmd, cite in rows if _is_test(cmd)]
    state = "unknown" if uncertain else ("pass" if tests else "gap")
    evidence = [cite for _, cite in rows] + errors
    if not evidence:
        evidence = [".github/workflows: no literal PR run commands found"]
    reason = ("No proven unconditional PR test execution; workflow semantics need review."
              if uncertain else
              "PR CI invokes test commands; test quality is not assessed." if tests else
              "No test invocation found in PR CI (a build alone is insufficient).")
    return _finding("ci-tests", state, reason, evidence), rows, sorted(set(jobs)), uncertain


def _verify(tree, pol, commands, ci_unknown):
    evidence, docs = [], []
    for name in ("AGENTS.md", "CLAUDE.md"):
        try:
            source, text = _effective(tree, name)
            docs.append((source, text))
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError):
            evidence.append(f"{name}: unreadable documentation")
    local = ""
    try:
        text = tree.read(".mahler/project.toml")
        docs.append((".mahler/project.toml", text))
        verify = tomllib.loads(text).get("verify", {})
        local = verify.get("fast", "") if isinstance(verify, dict) else ""
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return _finding("verify-command", "unknown", "Verify contract cannot be read.",
                        [".mahler/project.toml"])
    command = pol.get("verify") or local
    if not command:
        return _finding("verify-command", "gap", "No effective verify command is defined.",
                        [".mahler/project.toml [verify].fast; project policy verify"])
    expected = _words(command)
    documented = False
    for path, text in docs:
        for number, line in enumerate(text.splitlines(), 1):
            if command in line:
                documented = True
                evidence.append(f"{path}:{number}: {command}")
    matched = [cite for cmd, cite in commands if expected and _words(cmd)[:len(expected)] == expected]
    evidence += [f"effective verify: {command}", *matched]
    if not documented or not matched:
        state = "unknown" if documented and ci_unknown else "gap"
        reason = "Verify command is absent from " + (
            "documentation and PR CI." if not documented and not matched else
            "documentation." if not documented else "literal PR CI commands.")
    else:
        state = "unknown" if ci_unknown else "pass"
        reason = "Documented effective verify command is invoked by PR CI."
    return _finding("verify-command", state, reason, evidence)


def _secrets(tree):
    candidates, errors = [], []
    try:
        paths = tree.tracked()
    except (OSError, UnicodeError):
        return _finding("tracked-secrets", "unknown", "Tracked inventory unavailable.",
                        ["git ls-files -z"])
    for path in paths:
        try:
            resolved = tree.root / path
            if not resolved.resolve().is_relative_to(tree.root):
                raise OSError("external symlink")
            if resolved.stat().st_size > 2_000_000:
                errors.append(f"{path}: exceeds 2 MB scan limit")
                continue
            data = resolved.read_bytes()
            if b"\0" in data:
                errors.append(f"{path}: binary file not inspected")
                continue
            text = data.decode("utf-8")
        except (OSError, UnicodeError):
            errors.append(f"{path}: cannot inspect tracked content")
            continue
        for number, line in enumerate(text.splitlines(), 1):
            detectors = []
            if _PRIVATE_KEY.search(line):
                detectors.append("private-key")
            if any(not _PLACEHOLDER.match(m.group(1)) for m in _ASSIGNMENT.finditer(line)):
                detectors.append("credential-assignment")
            without_assignments = _ASSIGNMENT.sub("", line)
            if redact.redact(without_assignments) != without_assignments:
                detectors.append("credential-shape")
            for detector in detectors:
                candidates.append(f"{path}:{number}: detector={detector}; context={redact.MARK}")
    return _finding("tracked-secrets", "gap" if candidates else "unknown" if errors else "pass",
                    "Suspected credentials require review." if candidates else
                    "Some tracked files could not be inspected." if errors else
                    "No candidates in inspected tracked text; heuristics do not prove absence "
                    "of credentials or private data. Git history is not inspected.",
                    candidates + errors or ["git ls-files -z: tracked text inspected"])


def _protection(gh, jobs, ci_unknown):
    evidence = ["GitHub API: repository default_branch/allow_squash_merge",
                "GitHub API: branches/<default>/protection; rules/branches/<default>"]
    try:
        settings = gh.default_branch_settings()
        branch = settings.get("default_branch")
        if not branch:
            raise ValueError("missing default branch")
        evidence.append(f"default_branch={branch}; allow_squash_merge={settings.get('allow_squash_merge')}")
        protection = gh.branch_protection(branch)
        rules = gh.branch_rules(branch)
        status = protection.get("required_status_checks") or {}
        contexts = list(status.get("contexts") or [])
        contexts += [c["context"] for c in status.get("checks", [])]
        for rule in rules:
            if rule.get("type") == "required_status_checks":
                contexts += [c["context"] for c in rule["parameters"]["required_status_checks"]]
        contexts = sorted(set(contexts))
        evidence.append("required checks: " + (", ".join(contexts) or "(none)"))
    except (GHError, ValueError, TypeError, KeyError, AttributeError):
        return _finding("branch-protection", "unknown",
                        "Default-branch protection metadata is unavailable or unsupported.", evidence)
    if settings.get("allow_squash_merge") is False or not contexts:
        return _finding("branch-protection", "gap",
                        "Squash merging is disabled or no required checks gate the default branch.", evidence)
    if settings.get("allow_squash_merge") is not True or ci_unknown or not jobs:
        return _finding("branch-protection", "unknown",
                        "Cannot establish squash/test-gate compatibility.", evidence)
    if not any(name in contexts for name in jobs):
        return _finding("branch-protection", "gap",
                        "No identified PR test job is a required check.", [*evidence, f"test jobs: {jobs}"])
    incompatible = [r.get("type") for r in rules if r.get("type") in
                    ("required_signatures", "required_deployments", "pull_request")]
    if protection.get("required_pull_request_reviews") or incompatible:
        return _finding("branch-protection", "unknown",
                        "Additional review/signature/deployment rules need D18 compatibility review.",
                        [*evidence, f"additional rules: {incompatible}"])
    return _finding("branch-protection", "pass",
                    "Squash merging enabled and a PR test check is required.", evidence)


def scan_project(pol, gh):
    """Exactly six outcomes, including explicit unknowns for unreadable evidence."""
    tree = _Tree(pol["path"])
    if not tree.root.is_dir():
        return ProjectAudit(pol["name"], pol["repo"], tuple(
            _finding(check, "unknown", "Configured repository path is unavailable.",
                     ["configured project path"])
            for check in config.PRACTICES_AUDIT_CHECKS))
    ci, commands, jobs, uncertain = _ci(tree)
    instructions, guidance = _instructions(tree, pol["name"])
    findings = (ci, instructions, guidance, _verify(tree, pol, commands, uncertain),
                _secrets(tree), _protection(gh, jobs, uncertain))
    return ProjectAudit(pol["name"], pol["repo"], findings)


def scan(cfg, client):
    """Use only the enabled configured inventory; client(project) selects identity."""
    results = []
    for pol in config.enabled_projects(cfg):
        try:
            results.append(scan_project(pol, client(pol["name"])))
        except (OSError, GHError, ValueError):
            results.append(ProjectAudit(pol["name"], pol["repo"], tuple(
                _finding(check, "unknown", "Project evidence collection failed.",
                         [f"configured project: {pol['name']}"])
                for check in config.PRACTICES_AUDIT_CHECKS)))
    return tuple(results)


def proposal_marker(project, check):
    if check not in config.PRACTICES_AUDIT_CHECKS:
        raise ValueError("unknown practices check")
    identity = hashlib.sha256(json.dumps([project, check]).encode()).hexdigest()
    return f"<!-- mahler:practices-audit:{identity} -->"


_CHANGES = {
    "ci-tests": "Add explicit test execution to pull-request CI, not just a build.",
    "agent-instructions": "Make AGENTS.md authoritative; use an identical CLAUDE.md or a local pointer.",
    "mahler-guidance": "Update effective agent instructions to the D39 canonical participation contract.",
    "verify-command": "Document the effective verify command and invoke the same command in PR CI.",
    "tracked-secrets": "Review candidates locally; remove real credentials/private data and rotate exposed credentials.",
    "branch-protection": "Review default-branch settings; enable squash merge and require the PR test check.",
}


def _labels(pol, report=False):
    labels = ["type:chore", "size:m", "p2"]
    if report:
        labels.append("pass:practices-audit")
    if pol.get("scope") == "label":
        labels.append(pol["scope_label"])
    return list(dict.fromkeys(labels))


def _has_open_pass(led, project):
    for item in led.items(project):
        if item["state"] == "done":
            continue
        labels = json.loads(item["labels"] or "[]")
        title = (item["title"] or "").strip().lower()
        if any(label.startswith("pass:") for label in labels) or title == TITLE.lower():
            return True
    return False


def _cooling_down(led, project, policy):
    filed_at = led.maintenance_checkpoint(
        project, config.PRACTICES_AUDIT_PASS)["last_filed_at"]
    filed_at = parse(filed_at)
    return filed_at is not None and led.now() - filed_at < timedelta(
        days=policy["cooldown_days"])


def queue(ctx, projects):
    """Queue or resume the global practices audit without breaking the tick."""
    try:
        _queue(ctx, projects)
    except Exception as exc:  # noqa: BLE001 — an audit must not break the tick
        project = config.practices_audit_policy(ctx.cfg)["project"]
        ctx.say(f"{project}: practices-audit pass failed — {exc}")


def _queue(ctx, _projects):
    led = ctx.led
    policy = config.practices_audit_policy(ctx.cfg)
    if not policy["enabled"]:
        return
    anchor = policy["project"]
    inventory = config.enabled_projects(ctx.cfg)
    if anchor not in {project["name"] for project in inventory}:
        return
    if anchor in ctx.passes_filed:
        return

    report_url = led.get_kv(REPORT_URL_KEY)
    marker = led.get_kv(REPORT_MARKER_KEY)
    if not report_url and not marker:
        if not led.maintenance_due(anchor, config.PRACTICES_AUDIT_PASS, policy=policy):
            return
        if _cooling_down(led, anchor, policy) or _has_open_pass(led, anchor):
            return
        marker = f"<!-- mahler:practices-audit:report:{uuid.uuid4().hex} -->"
        if not ctx.dry_run:
            led.set_kv(REPORT_MARKER_KEY, marker)

    ctx.say(f"{anchor}: queuing {config.PRACTICES_AUDIT_PASS} pass")
    if ctx.dry_run:
        ctx.passes_filed.add(anchor)
        return

    ctx.passes_filed.add(anchor)
    results = scan(ctx.cfg, ctx.gh)
    filing = file_audit(ctx.cfg, results, ctx.gh, report_url=report_url,
                        report_marker=marker)
    if filing.report_url:
        led.set_kv(REPORT_URL_KEY, filing.report_url)
    if filing.errors:
        ctx.say(f"{anchor}: practices-audit filing incomplete — "
                + "; ".join(filing.errors))
        return

    led.set_maintenance_checkpoint(anchor, config.PRACTICES_AUDIT_PASS)
    led.set_kv(REPORT_URL_KEY, "")
    led.set_kv(REPORT_MARKER_KEY, "")


def _details(finding, *, evidence_limit=40, evidence_char_limit=1000):
    evidence = []
    for item in finding.evidence[:evidence_limit]:
        text = item[:evidence_char_limit]
        if len(item) > evidence_char_limit:
            text += " [truncated]"
        evidence.append(f"- {text}")
    omitted = len(finding.evidence) - len(evidence)
    if omitted:
        evidence.append(f"- {omitted} additional evidence entries omitted")
    return f"**{finding.check}: {finding.state}** — {finding.reason}\n\n" + "\n".join(evidence)


def _acceptance(pol, check):
    # This invokes the installed scanner only; it never runs repository commands.
    policy = {k: pol.get(k, "") for k in ("name", "repo", "verify")}
    code = ("import json; from mahler.practices_audit import scan_project; "
            "from mahler.gh import GH; from pathlib import Path; "
            f"p=json.loads({json.dumps(policy)!r}); "
            "p['path']=str(Path.cwd()); r=scan_project(p, GH(p['repo'])); "
            f"f=next(f for f in r.findings if f.check == {check!r}); "
            "print(f); assert f.state == 'pass', f.reason")
    return "python3 -c " + shlex.quote(code)


def _report(results, proposals, errors, skips, marker=""):
    body = [marker, "## Practices audit",
            "Read-only mechanical evidence; proposals are not automatic fixes.",
            "## Inventory"]
    for result in results:
        body += [f"### {result.project} ({result.repo})",
                 *[_details(f, evidence_limit=2, evidence_char_limit=240)
                   for f in result.findings]]
    body += ["## Proposals", *[f"- {key}: {url}" for key, url in proposals.items()],
             "## Skips", *(skips or ["None."]),
             "## Errors", *(errors or ["None."])]
    return _safe("\n\n".join(body))


def file_audit(cfg, results, client, *, report_url=None, report_marker=None,
               skips=(), dry_run=False):
    """File the anchor first; resume partial work with its URL on subsequent calls.

    Durable proposal markers survive restarts and closed issues. The scheduling
    caller owns persistence of report_url and errors, not this read-only engine.
    Lookup failure is never absence; unrelated projects still proceed.
    """
    anchor = config.practices_audit_policy(cfg)["project"]
    pol = config.project_policy(cfg, anchor)
    if not pol.get("enabled") or anchor not in cfg["projects"]:
        raise ValueError("practices audit anchor is missing or disabled")
    inventory = {p["name"]: p for p in config.enabled_projects(cfg)}
    results = tuple(results)
    if any(r.project not in inventory or inventory[r.project]["repo"] != r.repo for r in results):
        raise ValueError("audit result is outside the enabled inventory")
    if dry_run:
        return Filing(report_url or "", {}, ("Dry run: no GitHub writes.",))
    anchor_gh = client(anchor)
    report_marker = (report_marker
                     or f"<!-- mahler:practices-audit:report:{uuid.uuid4().hex} -->")
    if not report_url:
        anchor_gh.ensure_pass_label(config.PRACTICES_AUDIT_PASS)
        report_url = anchor_gh.issue_by_marker(report_marker)
        if not report_url:
            report_url = anchor_gh.create_issue(
                TITLE, _report(results, {}, ["Proposal filing pending."], skips, report_marker),
                _labels(pol, report=True))
        if not report_url:
            raise GHError("practices audit report creation returned no URL")
    proposals, errors = {}, []
    for result in results:
        project = inventory[result.project]
        for finding in result.findings:
            if finding.state == "pass":
                continue
            key = f"{result.project}/{finding.check}"
            marker = proposal_marker(result.project, finding.check)
            body = _safe(
                f"{marker}\n\nAudit report: {report_url}\n\n## Evidence\n\n{_details(finding)}"
                f"\n\n## Recommended repository change\n\n{_CHANGES[finding.check]}"
                "\n\n## Done when\n\nThe read-only check passes; review heuristic candidates "
                "locally without publishing credential values. Run from the root of the "
                "worktree containing your changes, with Mahler installed.\n\n"
                f"```sh\n{_acceptance(project, finding.check)}\n```\n")
            try:
                gh = client(result.project)
                url = gh.issue_by_marker(marker)
                if url:
                    proposals[key] = url
                    number = int(url.rstrip("/").rsplit("/", 1)[1])
                    gh.comment(number, body)
                    for label in _labels(project):
                        gh.add_label(number, label)
                else:
                    url = gh.create_issue(f"Practices: {finding.check}", body, _labels(project))
                    if not url:
                        raise GHError("proposal creation returned no URL")
                proposals[key] = url
            except (GHError, ValueError):
                errors.append(f"{key}: proposal filing failed; retry required")
    try:
        anchor_gh.edit_issue_body(int(report_url.rstrip("/").rsplit("/", 1)[1]),
                                  _report(results, proposals, errors, skips, report_marker))
    except (GHError, ValueError):
        errors.append("anchor report update failed; retry required")
    return Filing(report_url, proposals, tuple(errors))
