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


_BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".ico", ".webp", ".pdf",
    ".gif", ".bmp", ".tiff", ".woff", ".woff2", ".ttf", ".eot",
    ".mp3", ".mp4", ".mov", ".zip", ".tar", ".gz",
}

_ASSIGNMENT = re.compile(
    r"""(?ix)\b(?P<key>[\w-]*(?:token|secret|password|api[_-]?key|private[_-]?key))\b
        \??["']?\s*(?P<sep>[:=])\s*["']?(?P<val>[^\s"'`,;}]+)""")
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")
_PLACEHOLDER = re.compile(
    r"""(?ix)^(?:\$|<|redacted|changeme|example|dummy|placeholder|none|null|nil|"""
    r"""undefined|false|true|hunter2|mock|sample|fake|test(?:ing)?|temp|default|"""
    r"""empty|anon|anonymous)""")
_SCHEMA_DEF = re.compile(
    r"""(?ix)^(?:(?:\w+\.)?(?:text|varchar|char|string|uuid|integer|int|serial|"""
    r"""boolean|bool|timestamp|datetime|date|json|jsonb|blob|binary|customtype|citext|"""
    r"""columndefinition|field|column)\s*\(|models\.|column\()""")
_ENV_LOOKUP = re.compile(
    r"""(?ix)^(?:process\.env|os\.environ|os\.getenv|configparser\.get|"""
    r"""config\.get|conf\.get|cfg\.get|settings\.get|options\.get|params\.get|"""
    r"""system\.getenv|env(?:\.get|\(|\[)|ENV(?:\[|\.fetch)|"""
    r"""(?:req|request)\.headers|c\.env|context\.env|\w+\.get\(|\w+\.getenv\()""")
_TYPE_ANNOTATION = re.compile(
    r"""(?ix)^\??(?:string|str|optional|secretstr|charfield|textfield|text|"""
    r"""bytes|bytearray|bytestring|any|unknown|never|void|boolean|bool|int|integer|"""
    r"""number|float|char|character|data)(?:[?!|\[\]<>\s].*)?$""")


def _is_local_url(val):
    v = val.lower()
    return v.startswith((
        "http://localhost", "https://localhost",
        "http://127.0.0.1", "https://127.0.0.1",
        "http://0.0.0.0", "ws://localhost", "wss://localhost",
    ))


def _is_dict_var_or_property(key, val):
    clean_k = re.sub(r"[^a-z0-9]", "", key.lower())
    clean_v = re.sub(r"[^a-z0-9]", "", val.lower())
    if clean_k == clean_v:
        return True
    if clean_v in ("password", "token", "secret", "apikey", "privatekey", "key"):
        return True
    if re.match(
        r"""(?ix)^(?:this|self|req|request|res|response|data|body|user|account|"""
        r"""client|auth|credentials|params|item|row|payload)\.""", val
    ):
        return True
    if re.match(r"""(?ix)^(?:params|options|data|headers|payload|req|request)\[""", val):
        return True
    return False


def _is_candidate_assignment(m):
    key = m.group("key")
    sep = m.group("sep")
    raw_val = m.group("val")
    val = raw_val.rstrip(")]}>:;,")
    if not val:
        return False
    if _PLACEHOLDER.match(val):
        return False
    if _is_local_url(val):
        return False
    if _SCHEMA_DEF.match(val):
        return False
    if _ENV_LOOKUP.match(val):
        return False
    if sep == ":" and _TYPE_ANNOTATION.match(val):
        return False
    if _is_dict_var_or_property(key, val):
        return False
    return True


def _has_credential_shape(text):
    if not text or not text.strip():
        return False
    for shape in redact._TOKEN_SHAPES:
        if shape.search(text):
            return True
    for m in redact._URL_CREDS.finditer(text):
        cred = m.group(2)
        if not _PLACEHOLDER.match(cred) and not _is_local_url(m.group(0)):
            return True
    return False


def _safe(text):
    return redact.redact(_ASSIGNMENT.sub(
        lambda m: m.group(0).replace(m.group("val"), redact.MARK) if _is_candidate_assignment(m) else m.group(0),
        str(text)))


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
    # Repository test scripts run by node, and apps exposing a self-test mode.
    script = next((x for x in w[1:] if not x.startswith("-")), "")
    if w[0] == "node" and re.search(r"(?:^|[/_.-])tests?(?:[/_.-]|$)|run_tests?", script):
        return True
    if "--self-test" in w:
        return True
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
    if "--self-test" in words and _is_test(command):
        return rows, False
    script = None
    nested = None
    if words[0] in ("npm", "pnpm", "yarn"):
        if words[1:2] in (["ci"], ["install"], ["audit"]):
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
    elif words[0].startswith("./") and words[0] != "./gradlew":
        script = words[0]
    if not script:
        return rows, words[0] in (
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
        parts = _verify_parts(line, separators=";")
        if parts is None:
            uncertain = True
            continue
        for part in parts:
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


@dataclass(frozen=True)
class _Command:
    cmd: str
    cite: str
    uncertain: bool = False
    reasons: tuple[str, ...] = ()
    uncertain_cites: tuple[str, ...] = ()

    def __iter__(self):
        return iter((self.cmd, self.cite))

    def __getitem__(self, index):
        return (self.cmd, self.cite)[index]

    def __len__(self):
        return 2


class _WorkflowResult(tuple):
    def __new__(cls, rows, unknown, jobs, structural_uncertainty, is_auxiliary=False):
        return super().__new__(cls, (rows, unknown, jobs, structural_uncertainty))

    def __init__(self, rows, unknown, jobs, structural_uncertainty, is_auxiliary=False):
        self.is_auxiliary = is_auxiliary


def _workflow(tree, path):
    """Extract literal PR workflow run steps and static job names, not YAML names."""
    text = tree.read(path)
    lines = [_yaml_line(line) for line in text.splitlines()]
    clean_text = "\n".join(lines)
    trigger = re.search(r"(?m)^(?:on|['\"]on['\"]):\s*(.*)$", clean_text)
    if not trigger:
        return _WorkflowResult([], True, [], True)
    start = trigger.start()
    next_root = re.search(r"(?m)^[^\s#][^:\n]*:", clean_text[trigger.end():])
    end = trigger.end() + next_root.start() if next_root else len(clean_text)
    on = clean_text[start:end]
    if not re.search(r"\bpull_request\b", on):
        return _WorkflowResult([], False, [], False)

    pr_filtered, pr_filter_cite = False, ""
    is_auxiliary = False
    pr_match = re.search(r"(?m)^([ \t]*)pull_request\s*(?::[ \t]*(.*))?$", on)
    if pr_match:
        pr_indent = len(pr_match.group(1))
        pr_inline = (pr_match.group(2) or "").strip()
        if pr_inline and pr_inline not in ("{}", "~", "null"):
            pr_filtered = True
            line_no = clean_text[:start + pr_match.start()].count("\n") + 1
            pr_filter_cite = f"{path}:{line_no}: PR trigger filter"
        else:
            on_lines = on[pr_match.end():].splitlines()
            offset = start + pr_match.end()
            pr_filter_lines = []
            for o_line in on_lines:
                stripped = o_line.strip()
                if stripped:
                    indent = len(o_line) - len(o_line.lstrip())
                    if indent <= pr_indent:
                        break
                    pr_filter_lines.append(o_line)
                    if re.search(r"^[ \t]*(?:branches|branches-ignore|types)\s*:", o_line):
                        pr_filtered = True
                        line_no = clean_text[:offset].count("\n") + 1
                        pr_filter_cite = f"{path}:{line_no}: PR trigger filter"
                offset += len(o_line) + 1

            pr_filter_text = "\n".join(pr_filter_lines)
            types_match = re.search(r"(?m)^[ \t]*types[ \t]*:[ \t]*(.*)$", pr_filter_text)
            if types_match:
                inline_val = types_match.group(1).strip()
                types = []
                if inline_val.startswith("[") and inline_val.endswith("]"):
                    types = re.findall(r"[\w-]+", inline_val)
                elif not inline_val:
                    types_sublines = pr_filter_text[types_match.end():].splitlines()
                    for t_line in types_sublines:
                        if not t_line.strip():
                            continue
                        item = re.match(r"^[ \t]*-[ \t]*([\w-]+)", t_line)
                        if item:
                            types.append(item.group(1))
                        else:
                            break
                if types and not any(t in ("opened", "synchronize", "reopened") for t in types):
                    is_auxiliary = True
                    pr_filtered = False

    wf_anchors = bool(re.search(r"[&*][\w-]+", clean_text))
    wf_bad_jobs = bool(re.search(r"(?m)^jobs:[ \t]*\S", clean_text))
    wf_has_reusable = bool(re.search(r"^\s*uses:\s*[^ \n]*\.github/workflows/", clean_text, re.M))
    wf_has_changed_cwd = bool(re.search(r"(?m)^(?:\s{0,2})working-directory:\s*(\S+)", clean_text))
    wf_defaults_cwd = bool(re.search(r"(?m)^defaults:\s*\n(?:\s+.*\n)*?\s+working-directory:\s*(\S+)", clean_text))
    wf_cwd_cite = ""
    if wf_has_changed_cwd:
        m = re.search(r"(?m)^(?:\s{0,2})working-directory:\s*(\S+)", clean_text)
        wf_cwd_cite = f"{path}:{clean_text[:m.start()].count('\n') + 1}: changed working directory"
    elif wf_defaults_cwd:
        m = re.search(r"(?m)^\s+working-directory:\s*(\S+)", clean_text)
        wf_cwd_cite = f"{path}:{clean_text[:m.start()].count('\n') + 1}: changed working directory"

    jobs_match = re.search(r"(?m)^jobs:\s*$", clean_text)
    if not jobs_match:
        return [], True, [], True
    jobs_idx = clean_text[:jobs_match.start()].count("\n")

    rows, jobs = [], []
    current_job = ""
    job_has_condition = False
    job_cond_cite = ""
    job_has_changed_cwd = bool(wf_cwd_cite)
    job_cwd_cite = wf_cwd_cite
    job_has_continue_on_error = False
    job_continue_cite = ""
    job_preceding_uncertain = False
    job_preceding_uncertain_cite = ""
    in_steps = False

    i = jobs_idx + 1
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        if re.match(r"^[^\s#][^:\n]*:", line):
            break

        job_match = re.match(r"^  ([\w-]+):\s*$", line)
        if job_match and job_match.group(1) != "pull_request":
            current_job = job_match.group(1)
            job_has_condition = False
            job_cond_cite = ""
            job_has_changed_cwd = bool(wf_cwd_cite)
            job_cwd_cite = wf_cwd_cite
            job_has_continue_on_error = False
            job_continue_cite = ""
            job_preceding_uncertain = False
            job_preceding_uncertain_cite = ""
            in_steps = False
            i += 1
            continue

        if not current_job:
            i += 1
            continue

        if not in_steps:
            name_match = re.match(r"^    name:\s*(.+)", line)
            if name_match:
                current_job = name_match.group(1).strip("'\"")
                i += 1
                continue

            cond_match = re.match(r"^    if:\s*(.+)", line)
            if cond_match:
                job_has_condition = True
                job_cond_cite = f"{path}:{i+1}: job condition"
                i += 1
                continue

            cwd_match = re.match(r"^\s+working-directory:\s*(.+)", line)
            if cwd_match:
                job_has_changed_cwd = True
                job_cwd_cite = f"{path}:{i+1}: changed working directory"
                i += 1
                continue

            continue_match = re.match(r"^\s+continue-on-error:\s*(?:true|'true'|\"true\")\s*$", line, re.IGNORECASE)
            if continue_match:
                job_has_continue_on_error = True
                job_continue_cite = f"{path}:{i+1}: continue-on-error: true"
                i += 1
                continue

            if re.match(r"^    uses:\s*([^ \n]*\.github/workflows/.*)", line):
                wf_has_reusable = True
                i += 1
                continue

            if re.match(r"^    steps:\s*$", line):
                in_steps = True
                i += 1
                continue

            i += 1
            continue

        step_match = re.match(r"^(\s*)-\s*(.*)$", line)
        if not step_match:
            i += 1
            continue

        step_indent = len(step_match.group(1))
        step_lines = [(i, line)]
        next_i = i + 1
        while next_i < len(lines):
            next_line = lines[next_i]
            if not next_line.strip():
                next_i += 1
                continue
            next_indent = len(next_line) - len(next_line.lstrip())
            if next_indent <= 2:
                break
            if re.match(r"^\s*-\s+", next_line) and next_indent <= step_indent:
                break
            step_lines.append((next_i, next_line))
            next_i += 1

        step_has_condition = False
        step_cond_cite = ""
        step_has_changed_cwd = False
        step_cwd_cite = ""
        step_has_continue_on_error = False
        step_continue_cite = ""
        step_has_reusable = False
        step_reusable_cite = ""
        step_run_cmd = None
        step_run_line = None

        j = 0
        while j < len(step_lines):
            s_idx, s_text = step_lines[j]
            if re.search(r"(?:^|\s)(?:-\s*)?if:\s*(.+)", s_text):
                step_has_condition = True
                step_cond_cite = f"{path}:{s_idx+1}: step condition"
            if re.search(r"(?:^|\s)(?:-\s*)?working-directory:\s*(.+)", s_text):
                step_has_changed_cwd = True
                step_cwd_cite = f"{path}:{s_idx+1}: changed working directory"
            if re.match(r"^\s*(?:-\s*)?continue-on-error:\s*(?:true|'true'|\"true\")\s*$", s_text, re.IGNORECASE):
                step_has_continue_on_error = True
                step_continue_cite = f"{path}:{s_idx+1}: continue-on-error: true"
            if re.search(r"uses:\s*[^ \n]*\.github/workflows/", s_text):
                step_has_reusable = True
                step_reusable_cite = f"{path}:{s_idx+1}: reusable workflow"

            run_match = re.match(r"^(\s*)(?:-\s*)?run:\s*(.*)$", s_text)
            if run_match:
                step_run_line = s_idx + 1
                cmd_val = run_match.group(2).strip()
                if cmd_val in ("|", "|-", "|+", ">", ">-", ">+"):
                    run_indent = len(run_match.group(1)) + (2 if s_text.lstrip().startswith("-") else 0)
                    body = []
                    while j + 1 < len(step_lines):
                        f_idx, f_text = step_lines[j + 1]
                        if f_text.strip() and len(f_text) - len(f_text.lstrip()) <= run_indent:
                            break
                        j += 1
                        body.append(f_text.strip())
                    cmd_val = "\n".join(body) if cmd_val.startswith("|") else " ".join(body)
                else:
                    if len(cmd_val) >= 2 and cmd_val[0] in "'\"" and cmd_val[-1] == cmd_val[0]:
                        cmd_val = cmd_val[1:-1]
                step_run_cmd = cmd_val
            j += 1

        step_had_uncertainty = (
            step_has_condition or step_has_changed_cwd or
            step_has_continue_on_error or step_has_reusable
        )
        step_uncertain_cite = (
            step_cond_cite or step_continue_cite or step_cwd_cite or step_reusable_cite or
            (f"{path}:{step_lines[0][0]+1}: conditional step" if step_had_uncertainty else "")
        )

        if step_run_cmd is not None:
            parts = _verify_parts(step_run_cmd, separators=";\n")
            syntax_unknown = parts is None
            for part in parts if parts is not None else [step_run_cmd]:
                if not part or part.startswith("#"):
                    continue
                cmd_dynamic = "${{" in part
                expanded, expand_unknown = _expand(tree, part)
                unknown = expand_unknown or syntax_unknown

                for cmd, cite in expanded:
                    cmd_uncertain = False
                    cmd_reasons = []
                    cmd_cites = []

                    if job_has_condition:
                        cmd_uncertain = True
                        cmd_reasons.append("Verify command execution is conditional (job condition).")
                        cmd_cites.append(job_cond_cite)
                    elif step_has_condition:
                        cmd_uncertain = True
                        cmd_reasons.append("Verify command execution is conditional (step condition).")
                        cmd_cites.append(step_cond_cite)
                    elif job_has_continue_on_error:
                        cmd_uncertain = True
                        cmd_reasons.append("Verify command execution allows failure (job continue-on-error).")
                        cmd_cites.append(job_continue_cite)
                    elif step_has_continue_on_error:
                        cmd_uncertain = True
                        cmd_reasons.append("Verify command execution allows failure (step continue-on-error).")
                        cmd_cites.append(step_continue_cite)
                    elif pr_filtered:
                        cmd_uncertain = True
                        cmd_reasons.append("Workflow pull_request trigger has filters.")
                        cmd_cites.append(pr_filter_cite)
                    elif job_has_changed_cwd or step_has_changed_cwd:
                        cmd_uncertain = True
                        cmd_reasons.append("Verify command runs in a changed working directory.")
                        cmd_cites.append(step_cwd_cite or job_cwd_cite)
                    elif job_preceding_uncertain:
                        cmd_uncertain = True
                        cmd_reasons.append("Unresolved execution precedes verify command.")
                        cmd_cites.append(job_preceding_uncertain_cite)
                    elif cmd_dynamic or unknown:
                        cmd_uncertain = True
                        cmd_reasons.append("Verify command uses dynamic expansion or evaluation.")
                        cmd_cites.append(f"{path}:{step_run_line}: dynamic command")

                    full_cite = f"{path}:{step_run_line} job={current_job} run: {cmd}" + (f" -> {cite}" if cite else "")
                    cmd_obj = _Command(
                        cmd=cmd,
                        cite=full_cite,
                        uncertain=cmd_uncertain,
                        reasons=tuple(cmd_reasons),
                        uncertain_cites=tuple(cmd_cites)
                    )
                    rows.append(cmd_obj)
                    if _is_test(cmd) and current_job not in jobs:
                        jobs.append(current_job)

                    if expand_unknown:
                        step_had_uncertainty = True
                        step_uncertain_cite = f"{path}:{step_run_line}: unresolved execution: {cmd}"

        if step_had_uncertainty:
            job_preceding_uncertain = True
            job_preceding_uncertain_cite = step_uncertain_cite

        i = next_i

    structural_uncertainty = (
        pr_filtered or wf_anchors or wf_bad_jobs or wf_has_reusable or not rows
    )
    tests = [r for r in rows if _is_test(r.cmd)]
    proven_tests = [r for r in tests if not r.uncertain]
    cmd_uncertainty = any(r.uncertain for r in tests) if proven_tests else any(r.uncertain for r in rows)
    return _WorkflowResult(
        rows,
        structural_uncertainty or cmd_uncertainty,
        jobs,
        structural_uncertainty,
        is_auxiliary=is_auxiliary,
    )


def _ci(tree):
    rows, jobs, errors = [], [], []
    workflows = sorted((tree.root / ".github/workflows").glob("*.y*ml"))
    primary_results = []
    aux_results = []
    for path in workflows:
        name = str(path.relative_to(tree.root))
        try:
            res = _workflow(tree, name)
            if getattr(res, "is_auxiliary", False):
                aux_results.append((name, res))
            else:
                primary_results.append((name, res))
        except (OSError, UnicodeError):
            errors.append(f"{name}: unreadable workflow")

    target_results = primary_results if primary_results else aux_results
    for name, res in target_results:
        commands, unknown, names, structural_unknown = res
        rows.extend(commands)
        jobs.extend(names)

    uncertain = any(res[1] for _, res in target_results) or bool(errors)
    structural_uncertainty = any(res[3] for _, res in target_results) or bool(errors)

    tests = [row for row in rows if _is_test(row.cmd)]
    proven_tests = [row for row in tests if not row.uncertain]
    state = ("pass" if proven_tests and not structural_uncertainty else
             "unknown" if uncertain else "gap")
    evidence = [cite for _, cite in rows] + errors
    if not evidence:
        evidence = [".github/workflows: no literal PR run commands found"]
    reason = ("PR CI invokes test commands; test quality is not assessed." if state == "pass" else
              "No proven unconditional PR test execution; workflow semantics need review."
              if state == "unknown" else
              "No test invocation found in PR CI (a build alone is insufficient).")
    return _finding("ci-tests", state, reason, evidence), rows, sorted(set(jobs)), uncertain


def _verify_parts(command, *, separators=""):
    """Split literal AND chains, retaining shell quoting for _words().

    CI extraction can also allow semicolon/newline command lists.

    Unsupported shell operators/expansions remain unproven, never executed.
    """
    parts, start, quote, escaped = [], 0, None, False
    i = 0
    while i < len(command):
        char = command[i]
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
            elif quote == '"' and char in "$`":
                return None
        elif char in ("'", '"'):
            quote = char
        elif command[i:i + 2] == "&&":
            parts.append(command[start:i].strip())
            i += 2
            while i < len(command) and command[i].isspace():
                i += 1
            start = i
            continue
        elif char in separators:
            parts.append(command[start:i].strip())
            start = i + 1
        elif char in "&|;<>()[]{}$`#\n\r":
            return None
        i += 1
    if quote or escaped:
        return None
    parts.append(command[start:].strip())
    for part in parts:
        words = _words(part)
        if not words or words[0] in (
            "if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done",
            "case", "esac", "in", "function", "!", "eval", "source", ".",
        ):
            return None
    return parts


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
    parts = _verify_parts(command)
    documented = False
    for path, text in docs:
        for number, line in enumerate(text.splitlines(), 1):
            if command in line:
                documented = True
                evidence.append(f"{path}:{number}: {command}")

    component_matches = []
    for part in parts or ():
        expected = _words(part)
        component_matches.append([
            entry for entry in commands
            if _words(entry[0])[:len(expected)] == expected
        ])
    matched_entries = [entry for matches in component_matches for entry in matches]
    evidence += [f"effective verify: {command}", *(entry[1] for entry in matched_entries)]
    missing = [part for part, matches in zip(parts or (), component_matches) if not matches]
    evidence += [f"Missing verify component: {part}" for part in missing]

    if not documented:
        state = "gap"
        reason = "Verify command is absent from " + (
            "documentation and PR CI." if not matched_entries else "documentation.")
        return _finding("verify-command", state, reason, evidence)

    if parts is None:
        return _finding("verify-command", "unknown",
                        "Verify command has unsupported or malformed shell syntax.", evidence)

    if missing:
        state = "unknown" if ci_unknown else "gap"
        if state == "unknown":
            has_reusable = False
            try:
                for wf in (tree.root / ".github/workflows").glob("*.y*ml"):
                    if re.search(r"uses:\s*[^ \n]*\.github/workflows/", tree.read(str(wf.relative_to(tree.root)))):
                        has_reusable = True
                        break
            except (OSError, UnicodeError):
                pass
            if has_reusable:
                reason = "Verify command is absent from literal PR CI commands (workflow uses reusable workflows)."
            else:
                reason = "Verify command is absent from literal PR CI commands."
        else:
            reason = "Verify command is absent from literal PR CI commands."
        return _finding("verify-command", state, reason, evidence)

    if all(any(not getattr(entry, "uncertain", False) for entry in matches)
           for matches in component_matches):
        state = "pass"
        reason = "Documented effective verify command is invoked by PR CI."
    else:
        state = "unknown"
        reasons = []
        uncertain_cites = []
        for entry in matched_entries:
            for r in getattr(entry, "reasons", ()):
                if r not in reasons:
                    reasons.append(r)
            for c in getattr(entry, "uncertain_cites", ()):
                if c not in uncertain_cites and c not in evidence:
                    uncertain_cites.append(c)
        reason = reasons[0] if reasons else "Documented effective verify command execution path cannot be proven."
        evidence = evidence + uncertain_cites

    return _finding("verify-command", state, reason, evidence)


VALID_DETECTORS = ("credential-assignment", "credential-shape", "private-key")
REVIEWS_PATH = ".mahler/secret-reviews.json"


def _load_secret_reviews(tree):
    try:
        resolved = tree.root / REVIEWS_PATH
        if not resolved.resolve().is_relative_to(tree.root):
            return {}, [f"{REVIEWS_PATH}: unsafe path"]
        if resolved.stat().st_size > 2_000_000:
            return {}, [f"{REVIEWS_PATH}: exceeds 2 MB scan limit"]
        data = resolved.read_bytes()
        text = data.decode("utf-8")
    except FileNotFoundError:
        return {}, []
    except (OSError, UnicodeError):
        return {}, [f"{REVIEWS_PATH}: cannot inspect tracked content"]

    try:
        manifest = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}, [f"{REVIEWS_PATH}: malformed schema"]

    if not isinstance(manifest, dict):
        return {}, [f"{REVIEWS_PATH}: malformed schema"]
    if manifest.get("version") != 1:
        return {}, [f"{REVIEWS_PATH}: malformed schema"]
    if set(manifest.keys()) != {"version", "reviews"}:
        return {}, [f"{REVIEWS_PATH}: malformed schema"]
    reviews_list = manifest.get("reviews")
    if not isinstance(reviews_list, list):
        return {}, [f"{REVIEWS_PATH}: malformed schema"]

    valid_reviews = {}
    errors = []
    seen_keys = set()
    duplicate_keys = set()

    for entry in reviews_list:
        if not isinstance(entry, dict) or set(entry.keys()) != {"path", "sha256", "line", "detector", "rationale"}:
            errors.append(f"{REVIEWS_PATH}: malformed schema")
            continue
        rel_path = entry.get("path")
        sha256 = entry.get("sha256")
        line = entry.get("line")
        detector = entry.get("detector")
        rationale = entry.get("rationale")

        if not isinstance(rel_path, str) or not rel_path.strip():
            errors.append(f"{REVIEWS_PATH}: malformed schema")
            continue
        path_obj = Path(rel_path)
        if path_obj.is_absolute() or any(p == ".." for p in path_obj.parts):
            errors.append(f"{REVIEWS_PATH}: unsafe path: {rel_path}")
            continue
        try:
            entry_resolved = (tree.root / rel_path).resolve()
            if not entry_resolved.is_relative_to(tree.root):
                errors.append(f"{REVIEWS_PATH}: unsafe path: {rel_path}")
                continue
        except (OSError, ValueError):
            errors.append(f"{REVIEWS_PATH}: unsafe path: {rel_path}")
            continue

        if not (isinstance(sha256, str) and len(sha256) == 64 and all(c in "0123456789abcdefABCDEF" for c in sha256)):
            errors.append(f"{REVIEWS_PATH}: malformed schema")
            continue

        if not (isinstance(line, int) and not isinstance(line, bool) and line >= 1):
            errors.append(f"{REVIEWS_PATH}: malformed schema")
            continue

        if detector not in VALID_DETECTORS:
            errors.append(f"{REVIEWS_PATH}: invalid detector ID: {detector}")
            continue

        if not (isinstance(rationale, str) and bool(rationale.strip())):
            errors.append(f"{REVIEWS_PATH}: malformed schema")
            continue

        key = (rel_path, line, detector)
        if key in seen_keys:
            duplicate_keys.add(key)
            errors.append(f"{REVIEWS_PATH}: duplicate entry: {rel_path}:{line}: detector={detector}")
            continue
        seen_keys.add(key)
        valid_reviews[key] = {
            "sha256": sha256.lower(),
            "rationale": rationale,
        }

    for dup in duplicate_keys:
        valid_reviews.pop(dup, None)

    return valid_reviews, errors


def _secrets(tree):
    candidates, errors = [], []
    try:
        paths = tree.tracked()
    except (OSError, UnicodeError):
        return _finding("tracked-secrets", "unknown", "Tracked inventory unavailable.",
                        ["git ls-files -z"])

    valid_reviews, review_errors = _load_secret_reviews(tree)
    errors.extend(review_errors)
    matched_reviews = set()
    reviewed_count = 0

    for path in paths:
        try:
            resolved = tree.root / path
            if not resolved.resolve().is_relative_to(tree.root):
                raise OSError("external symlink")
            if Path(path).suffix.lower() in _BINARY_EXTENSIONS:
                resolved.stat()
                continue
            if resolved.stat().st_size > 2_000_000:
                errors.append(f"{path}: exceeds 2 MB scan limit")
                continue
            data = resolved.read_bytes()
            if b"\0" in data:
                continue
            text = data.decode("utf-8")
        except (OSError, UnicodeError):
            errors.append(f"{path}: cannot inspect tracked content")
            continue

        file_sha256 = hashlib.sha256(data).hexdigest()
        for number, line in enumerate(text.splitlines(), 1):
            detectors = []
            if _PRIVATE_KEY.search(line):
                detectors.append("private-key")
            if any(_is_candidate_assignment(m) for m in _ASSIGNMENT.finditer(line)):
                detectors.append("credential-assignment")
            without_assignments = _ASSIGNMENT.sub(
                lambda m: "" if _is_candidate_assignment(m) else m.group(0), line)
            if _has_credential_shape(without_assignments):
                detectors.append("credential-shape")
            for detector in detectors:
                key = (path, number, detector)
                if key in valid_reviews and valid_reviews[key]["sha256"] == file_sha256:
                    matched_reviews.add(key)
                    reviewed_count += 1
                else:
                    candidates.append(f"{path}:{number}: detector={detector}; context={redact.MARK}")

    for key in sorted(valid_reviews.keys()):
        if key not in matched_reviews:
            r_path, r_line, r_detector = key
            errors.append(f"{REVIEWS_PATH}: stale review: {r_path}:{r_line}: detector={r_detector}")

    reviewed_evidence = [
        f"{REVIEWS_PATH}: {reviewed_count} reviewed non-secret candidate{'s' if reviewed_count != 1 else ''}"
    ] if reviewed_count else []

    if candidates:
        state = "gap"
        reason = "Suspected credentials require review."
        evidence = candidates + errors + reviewed_evidence
    elif errors:
        state = "unknown"
        reason = "Some tracked files could not be inspected."
        evidence = errors + reviewed_evidence
    else:
        state = "pass"
        reason = (
            f"No unresolved candidates in inspected tracked text ({reviewed_count} reviewed); "
            "heuristics do not prove absence of credentials or private data. Git history is not inspected."
            if reviewed_count else
            "No candidates in inspected tracked text; heuristics do not prove absence "
            "of credentials or private data. Git history is not inspected."
        )
        evidence = reviewed_evidence + ["git ls-files -z: tracked text inspected"]

    return _finding("tracked-secrets", state, reason, evidence)


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
    gated = any(name in contexts for name in jobs)
    # Unrelated workflow uncertainty must not hide an established required test gate.
    if settings.get("allow_squash_merge") is not True or not jobs or (ci_unknown and not gated):
        return _finding("branch-protection", "unknown",
                        "Cannot establish squash/test-gate compatibility.", evidence)
    if not gated:
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


_EXEMPTION_KW = re.compile(
    r"\b(?:exempt(?:ion|ions|ed)?|exception[s]?|false[ -]positive[s]?|"
    r"waiv(?:e|er|ers|ed)?)\b",
    re.I
)
_NEGATED_EXEMPTION_KW = re.compile(
    r"\b(?:"
    r"(?:no|without)\s+(?:an?\s+)?(?:(?:explicit|documented|approved|granted)\s+)?"
    r"(?:exemption[s]?|exception[s]?|false[ -]positive[s]?|waiver[s]?)|"
    r"(?:not|never)\s+(?:(?:explicitly|currently)\s+)?(?:be\s+)?"
    r"(?:exempt(?:ion|ions|ed)?|exception[s]?|false[ -]positive[s]?|"
    r"waiv(?:e|er|ers|ed)?)|"
    r"(?:prohibit(?:s|ed|ing)?|forbid(?:s|den|ding)?|disallow(?:s|ed|ing)?|den(?:y|ies|ied)|reject(?:s|ed)?|ban(?:s|ned)?)"
    r"\s+(?:all\s+|any\s+)?(?:exemption[s]?|exception[s]?|false[ -]positive[s]?|waiver[s]?)|"
    r"(?:exemption[s]?|exception[s]?|false[ -]positive[s]?|waiver[s]?)"
    r"(?:\s+(?:for|to|of|from)\s+[\w`-]+(?:\s+[\w`-]+)?)?\s+"
    r"(?:(?:are|is|were|was|will\s+be|must\s+be|shall\s+be|may\s+not\s+be|cannot\s+be|can\s+not\s+be|"
    r"aren't|isn't)\s+)?(?:\w+\s+)?"
    r"(?:prohibited|forbidden|disallowed|denied|rejected|banned|not\s+(?:allowed|permitted|granted|accepted|approved))"
    r")\b",
    re.I,
)


def _has_exemption_language(text):
    return bool(_EXEMPTION_KW.search(_NEGATED_EXEMPTION_KW.sub("", text)))


def _extract_toml_rationale(val):
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, dict):
        return str(val.get("rationale") or val.get("reason") or val.get("issue")
                   or val.get("decision") or val).strip()
    return "Documented exemption"


def _project_toml_exemptions(tree):
    exemptions = {}
    try:
        text = tree.read(".mahler/project.toml")
        data = tomllib.loads(text)
    except Exception:
        return exemptions

    practices = data.get("practices")
    if not isinstance(practices, dict):
        return exemptions

    ex_table = practices.get("exemptions")
    if isinstance(ex_table, dict):
        for check, val in ex_table.items():
            if check in config.PRACTICES_AUDIT_CHECKS:
                exemptions[check] = (
                    ".mahler/project.toml: [practices.exemptions]",
                    _extract_toml_rationale(val),
                )
    elif isinstance(ex_table, (list, tuple)):
        for check in ex_table:
            if isinstance(check, str) and check in config.PRACTICES_AUDIT_CHECKS:
                exemptions[check] = (
                    ".mahler/project.toml: [practices.exemptions]",
                    "Documented exemption in project.toml",
                )

    ex_list = practices.get("exempt")
    if isinstance(ex_list, (list, tuple)):
        for check in ex_list:
            if isinstance(check, str) and check in config.PRACTICES_AUDIT_CHECKS and check not in exemptions:
                exemptions[check] = (
                    ".mahler/project.toml: [practices.exempt]",
                    "Documented exemption in project.toml",
                )
    elif isinstance(ex_list, dict):
        for check, val in ex_list.items():
            if check in config.PRACTICES_AUDIT_CHECKS and check not in exemptions:
                exemptions[check] = (
                    ".mahler/project.toml: [practices.exempt]",
                    _extract_toml_rationale(val),
                )

    for check in config.PRACTICES_AUDIT_CHECKS:
        if check in practices and check not in exemptions:
            val = practices[check]
            exemptions[check] = (
                f".mahler/project.toml: [practices.{check}]",
                _extract_toml_rationale(val),
            )

    return exemptions


def _scan_markdown_exemptions(tree, rel_path):
    exemptions = {}
    try:
        text = tree.read(rel_path)
    except (FileNotFoundError, OSError, UnicodeError):
        return exemptions

    lines = text.splitlines()
    current_heading = ""

    for idx, raw_line in enumerate(lines, 1):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            current_heading = line.lstrip("#").strip()

        for check in config.PRACTICES_AUDIT_CHECKS:
            if check in exemptions:
                continue
            pat = r"""(?i)(?:^|[\s`"'(])""" + re.escape(check) + r"""(?:$|[\s`"')/:,-])"""
            alt_pat = r"""(?i)(?:^|[\s`"'(])""" + re.escape(check.replace("-", "_")) + r"""(?:$|[\s`"')/:,-])"""
            matches_check = bool(re.search(pat, line) or re.search(alt_pat, line))
            matches_heading_check = bool(re.search(pat, current_heading) or re.search(alt_pat, current_heading))

            if matches_check or matches_heading_check:
                context_text = f"{current_heading} {line}"
                if _has_exemption_language(context_text):
                    rationale = line.lstrip("-*# \t")
                    colon_idx = rationale.find(":")
                    if colon_idx != -1 and colon_idx < len(check) + 5:
                        rationale = rationale[colon_idx + 1:].strip()
                    if line.startswith("#"):
                        for next_line in lines[idx:idx + 3]:
                            nl = next_line.strip()
                            if nl and not nl.startswith("#"):
                                rationale = f"{line.lstrip('#').strip()} — {nl}"
                                break
                    if not rationale:
                        rationale = current_heading or "Documented decision or exemption"
                    cite = f"{rel_path}:{idx}: {line[:120]}"
                    exemptions[check] = (cite, rationale)

    return exemptions


def _load_exemptions(tree):
    exemptions = {}
    exemptions.update(_project_toml_exemptions(tree))
    for doc in ("docs/DECISIONS.md", "DECISIONS.md", "AGENTS.md"):
        doc_exemptions = _scan_markdown_exemptions(tree, doc)
        for check, val in doc_exemptions.items():
            if check not in exemptions:
                exemptions[check] = val
    try:
        source, _ = _effective(tree, "AGENTS.md")
        if source not in ("AGENTS.md", "docs/DECISIONS.md", "DECISIONS.md"):
            doc_exemptions = _scan_markdown_exemptions(tree, source)
            for check, val in doc_exemptions.items():
                if check not in exemptions:
                    exemptions[check] = val
    except (FileNotFoundError, OSError, UnicodeError):
        pass
    return exemptions


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
    raw_findings = (ci, instructions, guidance, _verify(tree, pol, commands, uncertain),
                    _secrets(tree), _protection(gh, jobs, uncertain))
    exemptions = _load_exemptions(tree)
    findings = []
    for f in raw_findings:
        if f.state != "pass" and f.check in exemptions:
            cite, rationale = exemptions[f.check]
            findings.append(_finding(
                f.check, "pass",
                f"Documented architectural decision or exemption accepts {f.check}: {rationale}",
                (cite, *f.evidence),
            ))
        else:
            findings.append(f)
    return ProjectAudit(pol["name"], pol["repo"], tuple(findings))


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
            key = f"{result.project}/{finding.check}"
            marker = proposal_marker(result.project, finding.check)
            if finding.state == "pass":
                try:
                    gh = client(result.project)
                    url = gh.issue_by_marker(marker)
                    if url:
                        number = int(url.rstrip("/").rsplit("/", 1)[1])
                        is_closed = False
                        if hasattr(gh, "issue_state"):
                            try:
                                is_closed = gh.issue_state(number).lower() == "closed"
                            except (GHError, ValueError, KeyError):
                                is_closed = False
                        if not is_closed:
                            close_comment = _safe(
                                f"<!-- mahler:practices-audit:resolved -->\n\n"
                                f"Practices check `{finding.check}` is now resolved ({finding.state}) — {finding.reason}."
                            )
                            if hasattr(gh, "close_issue"):
                                gh.close_issue(number, comment=close_comment)
                            else:
                                gh.comment(number, close_comment)
                        proposals[key] = url
                except (GHError, ValueError):
                    errors.append(f"{key}: proposal resolution failed; retry required")
                continue
            body = _safe(
                f"{marker}\n\nAudit report: {report_url}\n\n## Evidence\n\n{_details(finding)}"
                f"\n\n## Recommended repository change\n\n{_CHANGES[finding.check]}"
                "\n\n## Done when\n\nThe read-only check passes; review heuristic candidates "
                "locally without publishing credential values. Run from the root of the "
                "worktree containing your changes, with Mahler installed.\n\n"
                f"```sh\n{_acceptance(project, finding.check)}\n```\n\n"
                "### Resolving verified scanner false positives\n\n"
                "If investigation confirms the repository satisfies the practice in substance "
                "or the finding is a scanner limitation/false positive:\n"
                "- **Documented architectural decision or exemption**: Record the rationale in "
                "`docs/DECISIONS.md`, `DECISIONS.md`, `AGENTS.md`, or `.mahler/project.toml` "
                "(e.g. `[practices.exemptions]`). The acceptance check above will recognize "
                "documented decisions and pass cleanly.\n"
                "- **Scanner issue tracking**: If the scanner has a bug or limitation, record the "
                "finding rationale and file/reference a Mahler tracking issue in `mkny13/mahler`. "
                "Close or resolve the proposal cleanly rather than escalating to `needs_you`.\n")
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
