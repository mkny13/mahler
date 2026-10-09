import copy
import hashlib
import json
import shlex
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from mahler import config, practices_audit as audit, scheduler, ship
from mahler.gh import GH, GHError
from mahler.ledger import Ledger, iso


GUIDANCE = """# Instructions
mahler claim demo#N before work
mahler heartbeat demo#N while working
mahler ship demo#N after pushing
mahler release demo#N when stopping unfinished
Use mahler/<N>-short-slug and Fixes #N.
Autonomous agents verify, commit and push, then end with a STATUS line.
The conductor opens, reviews and merges the PR.
Verify: python3 -m unittest discover -s tests
"""
WORKFLOW = """name: CI
on:
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: python3 -m unittest discover -s tests
"""


class FakeGH:
    def __init__(self, repo):
        self.repo = repo
        self.issues = {}
        self.calls = []
        self.comments = []
        self.unavailable = False
        self.lookup_error = False
        self.create_error = False
        self.settings = {"default_branch": "main", "allow_squash_merge": True}
        self.protection = {"required_status_checks": {"contexts": ["test"]}}
        self.rules = []

    def default_branch_settings(self):
        if self.unavailable:
            raise GHError("inaccessible")
        return copy.deepcopy(self.settings)

    def branch_protection(self, branch):
        return copy.deepcopy(self.protection)

    def branch_rules(self, branch):
        return copy.deepcopy(self.rules)

    def issue_by_marker(self, marker):
        self.calls.append(("lookup", marker))
        if self.lookup_error and ":report:" not in marker:
            raise GHError("incomplete listing")
        return next((url for url, issue in self.issues.items() if marker in issue["body"]), None)

    def create_issue(self, title, body, labels):
        self.calls.append(("create", title))
        if self.create_error:
            raise GHError("creation failed")
        url = f"https://github.com/{self.repo}/issues/{len(self.issues) + 1}"
        self.issues[url] = {"body": body, "labels": list(labels), "state": "open"}
        return url

    def edit_issue_body(self, number, body):
        self.calls.append(("edit", number))
        self.issues[f"https://github.com/{self.repo}/issues/{number}"]["body"] = body

    def comment(self, number, body):
        self.comments.append((number, body))

    def add_label(self, number, label):
        self.issues[f"https://github.com/{self.repo}/issues/{number}"]["labels"].append(label)

    def issue_state(self, number):
        url = f"https://github.com/{self.repo}/issues/{number}"
        return self.issues.get(url, {}).get("state", "open").upper()

    def close_issue(self, number, comment=None):
        self.calls.append(("close", number))
        url = f"https://github.com/{self.repo}/issues/{number}"
        if url in self.issues:
            self.issues[url]["state"] = "closed"
        if comment:
            self.comment(number, comment)

    def ensure_pass_label(self, name):
        self.calls.append(("label", name))


class TestPracticesAudit(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", self.temp.name], check=True, capture_output=True)
        self.pol = {"name": "demo", "repo": "owner/demo", "path": self.temp.name,
                    "enabled": True, "verify": "python3 -m unittest discover -s tests"}
        self.gh = FakeGH("owner/demo")
        self.now = datetime(2026, 10, 7, tzinfo=timezone.utc)
        self.led = Ledger(":memory:", clock=lambda: self.now)
        self.addCleanup(self.led.close)
        self.write("AGENTS.md", GUIDANCE)
        self.write("CLAUDE.md", GUIDANCE)
        self.write(".github/workflows/ci.yml", WORKFLOW)
        self.write(".mahler/project.toml",
                   '[verify]\nfast = "python3 -m unittest discover -s tests"\n')

    def write(self, name, text, tracked=True):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if tracked:
            subprocess.run(["git", "-C", self.temp.name, "add", "--", name],
                           check=True, capture_output=True)

    def results(self):
        result = audit.scan_project(self.pol, self.gh)
        self.assertEqual(tuple(f.check for f in result.findings), config.PRACTICES_AUDIT_CHECKS)
        self.assertTrue(all(f.evidence for f in result.findings))
        return {f.check: f for f in result.findings}

    def cfg(self):
        return {"defaults": {}, "projects": {"demo": self.pol},
                "practices_audit": {"project": "demo"}}

    def context(self, dry_run=False):
        ctx = scheduler.Ctx(self.cfg(), self.led, dry_run=dry_run)
        ctx._gh["owner/demo"] = self.gh
        return ctx

    def test_six_pass_outcomes_and_citations(self):
        findings = self.results()
        self.assertEqual({check: "pass" for check in config.PRACTICES_AUDIT_CHECKS},
                         {check: f.state for check, f in findings.items()})
        self.assertIn("ci.yml:8", str(findings["ci-tests"].evidence))
        self.assertIn("heuristics do not prove absence", findings["tracked-secrets"].reason)

    def test_report_evidence_is_bounded_with_omission_count(self):
        finding = audit.Finding("ci-tests", "unknown", "review semantics",
                                tuple(f"citation-{i}-" + "x" * 400 for i in range(300)))
        result = audit.ProjectAudit("demo", "owner/demo", (finding,) * 6)
        body = audit._report((result,), {}, [], [])
        self.assertLess(len(body), 10_000)
        self.assertIn("298 additional evidence entries omitted", body)
        detail = audit._details(finding)
        self.assertLess(len(detail), 50_000)
        self.assertIn("260 additional evidence entries omitted", detail)

    def test_build_only_is_not_test_coverage(self):
        self.write(".github/workflows/ci.yml", WORKFLOW.replace(
            "python3 -m unittest discover -s tests", "npm run build"))
        self.write("package.json", json.dumps({"scripts": {"build": "next build"}}))
        self.assertEqual(self.results()["ci-tests"].state, "gap")

    def test_pytest_parallel_workers_are_test_execution(self):
        for command in ("pytest -n auto", "python3 -m pytest -n 2"):
            with self.subTest(command=command):
                self.assertTrue(audit._is_test(command))
                self.write(".github/workflows/ci.yml", WORKFLOW.replace(
                    "python3 -m unittest discover -s tests", command))
                finding = self.results()["ci-tests"]
                self.assertEqual(finding.state, "pass")
                self.assertIn(f".github/workflows/ci.yml:8 job=test run: {command}",
                              finding.evidence)

    def test_pytest_non_execution_options_are_not_test_evidence(self):
        for option in ("--help", "-h", "--version", "--collect-only", "--dry-run"):
            for runner in ("pytest", "python3 -m pytest"):
                command = f"{runner} -n auto {option}"
                with self.subTest(command=command):
                    self.assertFalse(audit._is_test(command))
                    self.write(".github/workflows/ci.yml", WORKFLOW.replace(
                        "python3 -m unittest discover -s tests", command))
                    self.assertEqual(self.results()["ci-tests"].state, "gap")
        self.assertFalse(audit._is_test("go test -n ./..."))

    def test_named_test_workflow_and_echo_do_not_prove_tests(self):
        self.write(".github/workflows/ci.yml", WORKFLOW.replace(
            "python3 -m unittest discover -s tests", "echo pytest"))
        self.assertEqual(self.results()["ci-tests"].state, "gap")

    def test_comments_help_and_setup_are_not_test_evidence(self):
        self.write(".github/workflows/ci.yml", WORKFLOW.replace(
            "pull_request:", "push: # pull_request"))
        self.assertEqual(self.results()["ci-tests"].state, "gap")
        self.write(".github/workflows/ci.yml", WORKFLOW.replace(
            "- run: python3 -m unittest discover -s tests",
            "- run: npm ci\n      - run: pytest --help"))
        self.assertEqual(self.results()["ci-tests"].state, "gap")
        self.write(".github/workflows/ci.yml", WORKFLOW.replace(
            "- run: python3", "- run: npm ci\n      - run: python3"))
        self.assertEqual(self.results()["ci-tests"].state, "pass")

    def test_no_workflow_and_push_only(self):
        (self.root / ".github/workflows/ci.yml").unlink()
        self.assertEqual(self.results()["ci-tests"].state, "gap")
        self.write(".github/workflows/ci.yml", WORKFLOW.replace("pull_request:", "push:"))
        self.assertEqual(self.results()["ci-tests"].state, "gap")

    def test_conditional_and_reusable_workflows_are_unknown(self):
        for workflow in (WORKFLOW.replace("runs-on:", "if: false\n    runs-on:"),
                         "on: [pull_request]\njobs:\n  tests:\n"
                         "    uses: owner/repo/.github/workflows/test.yml@main\n"):
            with self.subTest(workflow=workflow):
                self.write(".github/workflows/ci.yml", workflow)
                self.assertEqual(self.results()["ci-tests"].state, "unknown")

    def test_pr_trigger_filters_remain_unknown(self):
        for key, value in (
            ("branches", "[main]"),
            ("branches-ignore", "[main]"),
            ("types", "[opened]"),
        ):
            with self.subTest(filter=key):
                workflow = WORKFLOW.replace(
                    "  pull_request:\n",
                    f"  pull_request:\n    {key}: {value}\n")
                self.write(".github/workflows/ci.yml", workflow)
                self.assertEqual(self.results()["ci-tests"].state, "unknown")

    def test_pr_paths_filters_pass(self):
        for key, value in (
            ("paths", '["**.py"]'),
            ("paths-ignore", '["docs/**"]'),
        ):
            with self.subTest(filter=key):
                workflow = WORKFLOW.replace(
                    "  pull_request:\n",
                    f"  pull_request:\n    {key}: {value}\n")
                self.write(".github/workflows/ci.yml", workflow)
                tree = audit._Tree(self.temp.name)
                rows, unknown, jobs, struct = audit._workflow(tree, ".github/workflows/ci.yml")
                self.assertFalse(unknown)
                self.assertFalse(struct)
                self.assertEqual(self.results()["ci-tests"].state, "pass")

    def test_workflow_concurrency_expression_passes(self):
        workflow = (
            "name: CI\n"
            "concurrency:\n"
            "  group: ${{ github.workflow }}-${{ github.ref }}\n"
            "on:\n"
            "  pull_request:\n"
            "jobs:\n"
            "  test:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - name: Log ref ${{ github.ref }}\n"
            "        run: echo ${{ github.sha }}\n"
            "      - run: python3 -m unittest discover -s tests\n"
        )
        self.write(".github/workflows/ci.yml", workflow)
        tree = audit._Tree(self.temp.name)
        rows, unknown, jobs, struct = audit._workflow(tree, ".github/workflows/ci.yml")
        self.assertFalse(unknown)
        self.assertFalse(struct)
        finding = self.results()["ci-tests"]
        self.assertEqual(finding.state, "pass")

    def test_auxiliary_pr_cleanup_workflow_does_not_override_primary_ci(self):
        cleanup_wf = (
            "name: Cleanup\n"
            "on:\n"
            "  pull_request:\n"
            "    types: [closed]\n"
            "jobs:\n"
            "  cleanup:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - run: echo cleanup\n"
        )
        self.write(".github/workflows/cleanup.yml", cleanup_wf)
        tree = audit._Tree(self.temp.name)
        res = audit._workflow(tree, ".github/workflows/cleanup.yml")
        self.assertTrue(getattr(res, "is_auxiliary", False))
        finding = self.results()["ci-tests"]
        self.assertEqual(finding.state, "pass")

        # Repository with only the auxiliary cleanup workflow evaluates to gap
        (self.root / ".github/workflows/ci.yml").unlink()
        finding_alone = self.results()["ci-tests"]
        self.assertEqual(finding_alone.state, "gap")

    def test_step_condition_and_expression_test_execution_are_unknown(self):
        workflows = (
            WORKFLOW.replace(
                "      - run:", "      - if: success()\n        run:"),
            WORKFLOW.replace(
                "python3 -m unittest discover -s tests",
                "${{ inputs.test_command }}"),
        )
        for workflow in workflows:
            with self.subTest(workflow=workflow):
                self.write(".github/workflows/ci.yml", workflow)
                self.assertEqual(self.results()["ci-tests"].state, "unknown")

    def test_multiline_commands_and_inline_trigger(self):
        self.write(".github/workflows/ci.yml",
                   WORKFLOW.replace("on:\n  pull_request:", "on: [push, pull_request]")
                   .replace("- run: python3", "- run: |\n          echo setup\n          python3"))
        self.assertEqual(self.results()["ci-tests"].state, "pass")

    def test_referenced_package_script_and_shell_runner(self):
        self.write(".github/workflows/ci.yml", WORKFLOW.replace(
            "python3 -m unittest discover -s tests", "npm test"))
        self.write("package.json", json.dumps({"scripts": {"test": "sh scripts/check.sh"}}))
        self.write("scripts/check.sh", "#!/bin/sh\npython3 -m unittest discover -s tests\n")
        results = self.results()
        self.assertEqual(results["ci-tests"].state, "pass")
        self.assertEqual(results["verify-command"].state, "pass")
        self.assertIn("package.json:1 -> scripts/check.sh:2", str(results["ci-tests"].evidence))

    def test_unresolved_script_is_unknown(self):
        self.write(".github/workflows/ci.yml", WORKFLOW.replace(
            "python3 -m unittest discover -s tests", "./missing.sh"))
        self.assertEqual(self.results()["ci-tests"].state, "unknown")
        self.write(".github/workflows/ci.yml", WORKFLOW.replace(
            "python3 -m unittest discover -s tests", "python3 fake.py"))
        self.write("fake.py", "# unittest.main() is not execution evidence\n")
        findings = self.results()
        self.assertEqual(findings["ci-tests"].state, "unknown")
        self.assertEqual(findings["verify-command"].state, "unknown")

    def test_identical_and_pointer_instructions_pass(self):
        for pointer in ("See AGENTS.md\n", "@AGENTS.md\n", "Read [instructions](AGENTS.md).\n"):
            with self.subTest(pointer=pointer):
                self.write("CLAUDE.md", pointer)
                self.assertEqual(self.results()["agent-instructions"].state, "pass")

    def test_divergent_and_missing_agents_fail(self):
        self.write("CLAUDE.md", "# Different instructions\n")
        self.assertEqual(self.results()["agent-instructions"].state, "gap")
        (self.root / "AGENTS.md").unlink()
        self.assertEqual(self.results()["agent-instructions"].state, "gap")

    def test_effective_guidance_pointer_and_cycle(self):
        self.write("docs/canonical.md", GUIDANCE)
        self.write("AGENTS.md", "See docs/canonical.md\n")
        self.write("CLAUDE.md", "See AGENTS.md\n")
        self.assertEqual(self.results()["mahler-guidance"].state, "pass")
        self.write("docs/canonical.md", "See ../AGENTS.md\n")
        self.assertEqual(self.results()["mahler-guidance"].state, "unknown")
        self.write("AGENTS.md", "See missing.md\n")
        self.assertEqual(self.results()["agent-instructions"].state, "unknown")

    def test_stale_project_guidance_and_conductor_contract_fail(self):
        self.write("AGENTS.md", GUIDANCE.replace("demo#", "old#").replace(
            "The conductor opens, reviews and merges the PR.", "Open your own PR."))
        finding = self.results()["mahler-guidance"]
        self.assertEqual(finding.state, "gap")
        self.assertIn("mahler claim demo#", finding.reason)
        self.assertIn("conductor", finding.reason)

    def test_verify_absent_from_docs_and_ci_is_gap(self):
        self.pol["verify"] = "python3 scripts/verify.py"
        finding = self.results()["verify-command"]
        self.assertEqual(finding.state, "gap")
        self.assertIn("documentation and PR CI", finding.reason)

    def test_project_toml_is_documentation_and_python_alias_is_consistent(self):
        self.pol["verify"] = ""
        self.write("AGENTS.md", GUIDANCE.replace("Verify: python3 -m unittest discover -s tests", ""))
        self.write(".github/workflows/ci.yml", WORKFLOW.replace("python3", "python") + "\n")
        self.assertEqual(self.results()["verify-command"].state, "pass")

    def test_compound_verify_multiline_same_step_and_toml_fallback(self):
        command = "cargo test && swift test"
        self.write(".mahler/project.toml", '[verify]\nfast = "' + command + '"\n')
        self.write(".github/workflows/ci.yml", WORKFLOW.replace(
            "- run: python3 -m unittest discover -s tests",
            "- run: |\n          cargo test &&\n          swift test"))
        self.pol["verify"] = ""
        finding = self.results()["verify-command"]
        self.assertEqual(finding.state, "pass")
        self.assertTrue(any("cargo test" in e and "run:" in e for e in finding.evidence))
        self.assertTrue(any("swift test" in e and "run:" in e for e in finding.evidence))

    def test_compound_verify_separate_jobs_and_uncertainty(self):
        command = "cargo test&&swift test"
        self.pol["verify"] = command
        self.write("AGENTS.md", GUIDANCE + command + "\n")
        workflow = WORKFLOW.replace("python3 -m unittest discover -s tests", "cargo test") + (
            "  mac:\n    runs-on: macos-latest\n    steps:\n      - run: swift test\n")
        self.write(".github/workflows/ci.yml", workflow)
        finding = self.results()["verify-command"]
        self.assertEqual(finding.state, "pass")
        for job, component in (("test", "cargo test"), ("mac", "swift test")):
            self.assertTrue(any(
                f"job={job} run:" in e and component in e for e in finding.evidence
            ))
        self.write(".github/workflows/ci.yml", workflow.replace(
            "  mac:\n", "  mac:\n    if: github.event_name == 'pull_request'\n"))
        self.assertEqual(self.results()["verify-command"].state, "unknown")
        self.write(".github/workflows/ci.yml", workflow)
        self.write("AGENTS.md", GUIDANCE + "cargo test\nswift test\n")
        self.assertEqual(self.results()["verify-command"].state, "gap")

    def test_compound_verify_script_components_and_missing_component(self):
        command = "pnpm typecheck && pnpm lint && pnpm test"
        self.pol["verify"] = command
        self.write("AGENTS.md", GUIDANCE + command + "\n")
        self.write("package.json", json.dumps({"scripts": {
            "check": "sh checks.sh", "typecheck": "tsc --noEmit",
            "lint": "eslint .", "test": "vitest run"}}))
        self.write("checks.sh", command + "\n")
        self.write(".github/workflows/ci.yml", WORKFLOW.replace(
            "python3 -m unittest discover -s tests", "pnpm check"))
        finding = self.results()["verify-command"]
        self.assertEqual(finding.state, "pass")
        self.assertTrue(any("checks.sh:1" in e for e in finding.evidence))
        self.write("checks.sh", "pnpm typecheck && pnpm test\n")
        finding = self.results()["verify-command"]
        self.assertEqual(finding.state, "gap")
        self.assertIn("pnpm lint", "\n".join(finding.evidence))

    def test_literal_operator_chains_through_workflow_and_scripts(self):
        for command in ("echo 'a&&b' && test x", 'echo "a&&b"&&test x',
                        r"echo a\&\&b&&test x", "test x&&echo 'a&&b'"):
            for source in ("workflow", "shell", "package"):
                with self.subTest(command=command, source=source):
                    self.pol["verify"] = command
                    self.write("AGENTS.md", GUIDANCE + command + "\n")
                    self.write("checks.sh", command + "\n")
                    self.write("package.json", json.dumps({"scripts": {"check": command}}))
                    invocation = {"workflow": command, "shell": "sh checks.sh",
                                  "package": "pnpm check"}[source]
                    self.write(".github/workflows/ci.yml", WORKFLOW.replace(
                        "python3 -m unittest discover -s tests", invocation))
                    self.assertEqual(self.results()["verify-command"].state, "pass")

    def test_invalid_ci_chains_do_not_prove_components(self):
        for command in ("test x &&", "&& test x", "test x && && echo ok",
                        "test x || echo ok", "test x | cat", "test x; ; echo ok"):
            for source in ("workflow", "shell", "package"):
                with self.subTest(command=command, source=source):
                    self.pol["verify"] = "test x"
                    self.write("AGENTS.md", GUIDANCE + "test x\n")
                    self.write("checks.sh", command + "\n")
                    self.write("package.json", json.dumps({"scripts": {"check": command}}))
                    invocation = {"workflow": command, "shell": "sh checks.sh",
                                  "package": "pnpm check"}[source]
                    self.write(".github/workflows/ci.yml", WORKFLOW.replace(
                        "python3 -m unittest discover -s tests", invocation))
                    self.assertNotEqual(self.results()["verify-command"].state, "pass")

    def test_literal_verify_chain_parser(self):
        for command, parts in (
            ("a&&b", ["a", "b"]),
            ("echo 'a&&b' && test x", ["echo 'a&&b'", "test x"]),
            ('echo "a&&b" && test x', ['echo "a&&b"', "test x"]),
            (r"echo a\&\&b && test x", [r"echo a\&\&b", "test x"]),
            ("JAVA_HOME=/jdk gradle test && swift test",
             ["JAVA_HOME=/jdk gradle test", "swift test"]),
        ):
            with self.subTest(command=command):
                self.assertEqual(audit._verify_parts(command), parts)
        for command in ("", "a &&", "&& a", "a && && b", "a && 'b", "a || b",
                        "a | b", "a; b", "a & b", "a > b", "a\nb", "echo $(test)",
                        "if true", "echo `test`", "a # comment"):
            with self.subTest(command=command):
                self.assertIsNone(audit._verify_parts(command))
                self.pol["verify"] = command
                self.write("AGENTS.md", GUIDANCE + command + "\n")
                self.write(".github/workflows/ci.yml", WORKFLOW.replace(
                    "python3 -m unittest discover -s tests", command))
                self.assertNotEqual(self.results()["verify-command"].state, "pass")

    def test_missing_verify_and_invalid_contract(self):
        self.pol["verify"] = ""
        (self.root / ".mahler/project.toml").unlink()
        self.assertEqual(self.results()["verify-command"].state, "gap")
        self.write(".mahler/project.toml", "[invalid")
        self.assertEqual(self.results()["verify-command"].state, "unknown")

    def test_secret_evidence_never_contains_candidate_values(self):
        candidates = ["ghp_" + "a" * 36, "very-private-password", "unrelated-secret-on-same-line"]
        self.write("credentials.env", f'PASSWORD: "{candidates[1]}" # {candidates[2]}\n'
                   f'{candidates[0]}\n-----BEGIN PRIVATE KEY-----\n')
        finding = self.results()["tracked-secrets"]
        self.assertEqual(finding.state, "gap")
        self.assertIn("credentials.env:1: detector=credential-assignment; context=<redacted>",
                      finding.evidence)
        self.assertIn("detector=credential-shape", str(finding.evidence))
        self.assertIn("detector=private-key", str(finding.evidence))
        for value in candidates:
            self.assertNotIn(value, repr(finding))
        filing = audit.file_audit(self.cfg(), (audit.scan_project(self.pol, self.gh),),
                                 lambda _: self.gh)
        self.assertTrue(filing.report_url)
        published = repr(self.gh.issues) + repr(self.gh.comments)
        for value in candidates:
            self.assertNotIn(value, published)

    def test_untracked_secret_is_not_scanned(self):
        self.write("local.env", "PASSWORD=local-private-value", tracked=False)
        self.assertEqual(self.results()["tracked-secrets"].state, "pass")

    def test_unavailable_tracked_inventory_and_corrupt_content_are_explicit_unknowns(self):
        with patch.object(audit._Tree, "tracked", side_effect=OSError("secret diagnostic")):
            finding = self.results()["tracked-secrets"]
            self.assertEqual(finding.state, "unknown")
            self.assertNotIn("secret diagnostic", repr(finding))
        corrupt = self.root / "corrupted.txt"
        corrupt.write_bytes(b"\xff\xfe\xfd")
        subprocess.run(["git", "-C", self.temp.name, "add", "--", "corrupted.txt"], check=True)
        finding = self.results()["tracked-secrets"]
        self.assertEqual(finding.state, "unknown")
        self.assertIn("corrupted.txt: cannot inspect tracked content", finding.evidence)

    def test_exact_review_match_suppresses_candidate(self):
        content = f"{chr(84)}OKEN=synthetic-token-123\n"
        self.write("reviewed.env", content)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        manifest = {
            "version": 1,
            "reviews": [
                {
                    "path": "reviewed.env",
                    "sha256": digest,
                    "line": 1,
                    "detector": "credential-assignment",
                    "rationale": "synthetic test fixture in temporary repository",
                }
            ],
        }
        self.write(".mahler/secret-reviews.json", json.dumps(manifest, indent=2))
        finding = self.results()["tracked-secrets"]
        self.assertEqual(finding.state, "pass")
        self.assertIn(".mahler/secret-reviews.json: 1 reviewed non-secret candidate", finding.evidence)
        self.assertIn("heuristics do not prove absence", finding.reason)
        self.assertNotIn("synthetic-token-123", repr(finding))

    def test_candidate_changed_in_same_file_remains_gap(self):
        content = f"# header line\n{chr(84)}OKEN=synthetic-token-123\n"
        self.write("reviewed.env", content)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        manifest = {
            "version": 1,
            "reviews": [
                {
                    "path": "reviewed.env",
                    "sha256": digest,
                    "line": 1,
                    "detector": "credential-assignment",
                    "rationale": "synthetic test fixture line 1 review",
                }
            ],
        }
        self.write(".mahler/secret-reviews.json", json.dumps(manifest, indent=2))
        finding = self.results()["tracked-secrets"]
        self.assertEqual(finding.state, "gap")
        self.assertIn("reviewed.env:2: detector=credential-assignment; context=<redacted>", finding.evidence)
        self.assertIn(".mahler/secret-reviews.json: stale review: reviewed.env:1: detector=credential-assignment",
                      finding.evidence)

    def test_new_candidate_in_reviewed_file_remains_gap(self):
        content = f"{chr(84)}OKEN=first-token\n{chr(80)}ASSWORD=second-secret\n"
        self.write("reviewed.env", content)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        manifest = {
            "version": 1,
            "reviews": [
                {
                    "path": "reviewed.env",
                    "sha256": digest,
                    "line": 1,
                    "detector": "credential-assignment",
                    "rationale": "first token reviewed",
                }
            ],
        }
        self.write(".mahler/secret-reviews.json", json.dumps(manifest, indent=2))
        finding = self.results()["tracked-secrets"]
        self.assertEqual(finding.state, "gap")
        self.assertIn("reviewed.env:2: detector=credential-assignment; context=<redacted>", finding.evidence)
        self.assertIn(".mahler/secret-reviews.json: 1 reviewed non-secret candidate", finding.evidence)
        self.assertNotIn("reviewed.env:1:", str(finding.evidence))

    def test_same_candidate_in_different_file_remains_gap(self):
        content = f"{chr(84)}OKEN=synthetic-token\n"
        self.write("file_a.env", content)
        self.write("file_b.env", content)
        digest_a = hashlib.sha256(content.encode("utf-8")).hexdigest()
        manifest = {
            "version": 1,
            "reviews": [
                {
                    "path": "file_a.env",
                    "sha256": digest_a,
                    "line": 1,
                    "detector": "credential-assignment",
                    "rationale": "file a reviewed",
                }
            ],
        }
        self.write(".mahler/secret-reviews.json", json.dumps(manifest, indent=2))
        finding = self.results()["tracked-secrets"]
        self.assertEqual(finding.state, "gap")
        self.assertIn("file_b.env:1: detector=credential-assignment; context=<redacted>", finding.evidence)
        self.assertNotIn("file_a.env:1:", str(finding.evidence))

    def test_stale_digest_remains_gap_and_reports_stale_metadata(self):
        content = f"{chr(84)}OKEN=synthetic-token\n"
        self.write("reviewed.env", content)
        manifest = {
            "version": 1,
            "reviews": [
                {
                    "path": "reviewed.env",
                    "sha256": "0" * 64,
                    "line": 1,
                    "detector": "credential-assignment",
                    "rationale": "stale digest test",
                }
            ],
        }
        self.write(".mahler/secret-reviews.json", json.dumps(manifest, indent=2))
        finding = self.results()["tracked-secrets"]
        self.assertEqual(finding.state, "gap")
        self.assertIn("reviewed.env:1: detector=credential-assignment; context=<redacted>", finding.evidence)
        self.assertIn(".mahler/secret-reviews.json: stale review: reviewed.env:1: detector=credential-assignment",
                      finding.evidence)

    def test_malformed_duplicate_and_unsafe_path_review_data(self):
        # 1. Malformed JSON
        self.write(".mahler/secret-reviews.json", "not valid json {")
        finding = self.results()["tracked-secrets"]
        self.assertEqual(finding.state, "unknown")
        self.assertIn(".mahler/secret-reviews.json: malformed schema", finding.evidence)

        # 2. Malformed schema (unsupported version or missing fields)
        self.write(".mahler/secret-reviews.json", json.dumps({"version": 99, "reviews": []}))
        finding = self.results()["tracked-secrets"]
        self.assertEqual(finding.state, "unknown")
        self.assertIn(".mahler/secret-reviews.json: malformed schema", finding.evidence)

        # 3. Unsafe path in review entry
        self.write(".mahler/secret-reviews.json", json.dumps({
            "version": 1,
            "reviews": [{
                "path": "../outside.env",
                "sha256": "a" * 64,
                "line": 1,
                "detector": "credential-assignment",
                "rationale": "unsafe path test",
            }],
        }))
        finding = self.results()["tracked-secrets"]
        self.assertEqual(finding.state, "unknown")
        self.assertIn(".mahler/secret-reviews.json: unsafe path: ../outside.env", finding.evidence)

        # 4. Invalid detector ID
        self.write(".mahler/secret-reviews.json", json.dumps({
            "version": 1,
            "reviews": [{
                "path": "file.env",
                "sha256": "a" * 64,
                "line": 1,
                "detector": "invalid-detector-id",
                "rationale": "invalid detector test",
            }],
        }))
        finding = self.results()["tracked-secrets"]
        self.assertEqual(finding.state, "unknown")
        self.assertIn(".mahler/secret-reviews.json: invalid detector ID: invalid-detector-id", finding.evidence)

        # 5. Duplicate entries
        dup_entry = {
            "path": "file.env",
            "sha256": "a" * 64,
            "line": 1,
            "detector": "credential-assignment",
            "rationale": "duplicate review test",
        }
        self.write(".mahler/secret-reviews.json", json.dumps({
            "version": 1,
            "reviews": [dup_entry, dup_entry],
        }))
        finding = self.results()["tracked-secrets"]
        self.assertEqual(finding.state, "unknown")
        self.assertIn(".mahler/secret-reviews.json: duplicate entry: file.env:1: detector=credential-assignment",
                      finding.evidence)

    def test_missing_manifest_retains_current_behavior(self):
        manifest_path = self.root / ".mahler/secret-reviews.json"
        if manifest_path.exists():
            manifest_path.unlink()
        content = f"{chr(84)}OKEN=synthetic-token\n"
        self.write("secret.env", content)
        finding = self.results()["tracked-secrets"]
        self.assertEqual(finding.state, "gap")
        self.assertIn("secret.env:1: detector=credential-assignment; context=<redacted>", finding.evidence)

    def test_binary_file_alongside_reviewed_text_passes(self):
        content = f"{chr(84)}OKEN=synthetic-token\n"
        self.write("reviewed.env", content)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        manifest = {
            "version": 1,
            "reviews": [
                {
                    "path": "reviewed.env",
                    "sha256": digest,
                    "line": 1,
                    "detector": "credential-assignment",
                    "rationale": "valid review alongside binary file",
                }
            ],
        }
        self.write(".mahler/secret-reviews.json", json.dumps(manifest, indent=2))
        self.write("screenshot.png", "data\0bytes")
        finding = self.results()["tracked-secrets"]
        self.assertEqual(finding.state, "pass")
        self.assertNotIn("binary file not inspected", str(finding.evidence))
        self.assertIn(".mahler/secret-reviews.json: 1 reviewed non-secret candidate", finding.evidence)

    def test_tracked_binary_assets_and_clean_text_return_pass(self):
        self.write("screenshot.png", "png\0data")
        self.write("icon.ico", "ico\0data")
        self.write("doc.pdf", "pdf\0data")
        self.write("clean.txt", "normal application code\n")
        finding = self.results()["tracked-secrets"]
        self.assertEqual(finding.state, "pass")
        self.assertNotIn("binary file not inspected", str(finding.evidence))

    def test_drizzle_schema_definitions_return_pass(self):
        schema = (
            "import { pgTable, text, varchar } from 'drizzle-orm/pg-core';\n\n"
            "export const users = pgTable('users', {\n"
            '  password: text("password"),\n'
            '  token: varchar("token"),\n'
            "});\n"
        )
        self.write("src/schema.ts", schema)
        finding = self.results()["tracked-secrets"]
        self.assertEqual(finding.state, "pass")

    def test_language_type_annotations_return_pass(self):
        annotations = (
            "interface AuthProps {\n"
            "  password: String;\n"
            "  token: string;\n"
            "  authToken: String?;\n"
            "}\n\n"
            "fun authenticate(password: String, token: string) {}\n\n"
            "def login(password: str, token: str):\n"
            "  pass\n"
        )
        self.write("src/types.ts", annotations)
        finding = self.results()["tracked-secrets"]
        self.assertEqual(finding.state, "pass")

    def test_environment_and_config_lookups_return_pass(self):
        lookups = (
            "token = process.env.TOKEN\n"
            'api_key = configparser.get("section", "api_key")\n'
            'secret = os.environ["TOKEN"]\n'
            'conf_key = config.get("api_key")\n'
        )
        self.write("src/config.py", lookups)
        finding = self.results()["tracked-secrets"]
        self.assertEqual(finding.state, "pass")

    def test_dict_keys_and_mock_fixtures_return_pass(self):
        code = (
            'user = {"password": password}\n'
            'data = {"token": token}\n'
            'payload = {"password": user.password}\n'
            'mock_pw = "hunter2"\n'
            'mock_tok = "mock-token"\n'
            'local_url = "http://localhost:3000/token"\n'
        )
        self.write("src/fixtures.py", code)
        finding = self.results()["tracked-secrets"]
        self.assertEqual(finding.state, "pass")

    def test_actual_credential_token_and_private_key_report_gap_with_redacted_evidence(self):
        raw_token = "ghp_123456789012345678901234567890123456"
        raw_password = "super-secret-password-xyz"
        content = (
            f'TOKEN = "{raw_token}"\n'
            f'PASSWORD = "{raw_password}"\n'
            "-----BEGIN PRIVATE KEY-----\nMIIEvgIBADANBgkqhkiG9w0BAQEFAASC\n"
        )
        self.write("src/credentials.env", content)
        finding = self.results()["tracked-secrets"]
        self.assertEqual(finding.state, "gap")
        self.assertIn("src/credentials.env:1: detector=credential-assignment; context=<redacted>", finding.evidence)
        self.assertIn("src/credentials.env:3: detector=private-key; context=<redacted>", finding.evidence)
        self.assertNotIn(raw_token, repr(finding))
        self.assertNotIn(raw_password, repr(finding))

    def test_synthetic_detectors_and_candidate_safe_in_filing(self):
        pk = "-----" + "BEGIN PRIVATE KEY-----"
        candidates = ["ghp_" + "b" * 36, "synthetic-password-xyz", pk]
        content = f"{chr(80)}ASSWORD={candidates[1]}\n{candidates[0]}\n{candidates[2]}\n"
        self.write("synthetic.env", content)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        manifest = {
            "version": 1,
            "reviews": [
                {
                    "path": "synthetic.env",
                    "sha256": digest,
                    "line": 1,
                    "detector": "credential-assignment",
                    "rationale": "reviewed assignment",
                },
                {
                    "path": "synthetic.env",
                    "sha256": digest,
                    "line": 2,
                    "detector": "credential-shape",
                    "rationale": "reviewed shape",
                },
                {
                    "path": "synthetic.env",
                    "sha256": digest,
                    "line": 3,
                    "detector": "private-key",
                    "rationale": "reviewed private key",
                },
            ],
        }
        self.write(".mahler/secret-reviews.json", json.dumps(manifest, indent=2))
        finding = self.results()["tracked-secrets"]
        self.assertEqual(finding.state, "pass")
        self.assertIn(".mahler/secret-reviews.json: 3 reviewed non-secret candidates", finding.evidence)
        for val in candidates:
            self.assertNotIn(val, repr(finding))


    def test_external_instruction_pointer_is_not_followed(self):
        self.write("AGENTS.md", "See ../external.md\n")
        self.assertEqual(self.results()["agent-instructions"].state, "unknown")

    def test_unavailable_branch_metadata_is_unknown(self):
        self.gh.unavailable = True
        self.assertEqual(self.results()["branch-protection"].state, "unknown")
        self.gh.unavailable = False
        with patch.object(self.gh, "branch_protection", side_effect=GHError("HTTP 404")):
            self.assertEqual(self.results()["branch-protection"].state, "unknown")
        with patch.object(self.gh, "branch_rules", side_effect=GHError("HTTP 403")):
            self.assertEqual(self.results()["branch-protection"].state, "unknown")

    def test_branch_settings_squash_and_test_gate(self):
        self.gh.settings["allow_squash_merge"] = False
        self.assertEqual(self.results()["branch-protection"].state, "gap")
        self.gh.settings["allow_squash_merge"] = True
        self.gh.protection = {"required_status_checks": {"contexts": ["build"]}}
        self.assertEqual(self.results()["branch-protection"].state, "gap")
        self.gh.rules = [{"type": "required_status_checks", "parameters": {
            "required_status_checks": [{"context": "test"}]}}]
        self.assertEqual(self.results()["branch-protection"].state, "pass")
        self.gh.protection["required_pull_request_reviews"] = {"required_approving_review_count": 1}
        self.assertEqual(self.results()["branch-protection"].state, "unknown")

    def test_npm_test_node_script_runner_is_recognized(self):
        self.write("package.json", json.dumps({"scripts": {"test": "node scripts/run_tests.js"}}))
        self.write(".github/workflows/ci.yml", WORKFLOW.replace(
            "python3 -m unittest discover -s tests", "npm test"))
        self.assertEqual(self.results()["ci-tests"].state, "pass")

    def test_self_test_binary_step_is_recognized(self):
        self.write(".github/workflows/ci.yml", WORKFLOW.replace(
            "python3 -m unittest discover -s tests", "./build/App --self-test"))
        self.assertEqual(self.results()["ci-tests"].state, "pass")
        self.assertTrue(audit._is_test("./build/App --self-test"))
        self.assertFalse(audit._is_test("node scripts/build.js"))

    def test_protection_passes_with_script_test_job_context(self):
        self.write("package.json", json.dumps({"scripts": {"test": "node scripts/run_tests.js"}}))
        self.write(".github/workflows/ci.yml", WORKFLOW.replace(
            "python3 -m unittest discover -s tests", "npm test"))
        self.assertEqual(self.results()["branch-protection"].state, "pass")

    def test_scope_labels_anchor_first_links_and_closed_marker_reuse(self):
        self.pol.update(scope="label", scope_label="custom")
        self.write("CLAUDE.md", "Diverged\n")
        result = audit.scan_project(self.pol, self.gh)
        first = audit.file_audit(self.cfg(), (result,), lambda _: self.gh)
        proposal = first.proposals["demo/agent-instructions"]
        self.assertIn(first.report_url, self.gh.issues[proposal]["body"])
        self.assertEqual(self.gh.calls[2], ("create", audit.TITLE))
        for issue in self.gh.issues.values():
            self.assertTrue(set(["custom", "type:chore", "size:m", "p2"]) <= set(issue["labels"]))
        self.assertIn("pass:practices-audit", self.gh.issues[first.report_url]["labels"])
        self.assertNotIn("pass:practices-audit", self.gh.issues[proposal]["labels"])
        self.assertIn("python3 -c", self.gh.issues[proposal]["body"])
        self.assertIn(audit.proposal_marker("demo", "agent-instructions"), self.gh.issues[proposal]["body"])
        for state in ("open", "closed"):
            with self.subTest(state=state):
                self.gh.issues[proposal]["state"] = state
                second = audit.file_audit(self.cfg(), (result,), lambda _: self.gh,
                                          report_url=first.report_url)
                self.assertEqual(second.proposals, first.proposals)
                self.assertEqual(len(self.gh.issues), 2)
                self.assertEqual(self.gh.issues[proposal]["state"], state)
                self.assertIn(first.report_url, self.gh.comments[-1][1])
        self.assertIn(proposal, self.gh.issues[first.report_url]["body"])

    def test_proposal_acceptance_scans_current_worktree(self):
        self.write("CLAUDE.md", "Diverged\n")
        result = audit.scan_project(self.pol, self.gh)
        filing = audit.file_audit(self.cfg(), (result,), lambda _: self.gh)
        body = self.gh.issues[filing.proposals["demo/agent-instructions"]]["body"]
        command = body.split("```sh\n", 1)[1].split("\n```", 1)[0]
        executable, flag, code = shlex.split(command)
        self.assertEqual((executable, flag), ("python3", "-c"))
        with tempfile.TemporaryDirectory() as worktree:
            root = Path(worktree)
            (root / "AGENTS.md").write_text(GUIDANCE)
            (root / "CLAUDE.md").write_text("Diverged\n")
            with patch("pathlib.Path.cwd", return_value=root), \
                    patch("mahler.gh.GH", return_value=self.gh) as client, \
                    patch("builtins.print"):
                with self.assertRaises(AssertionError):
                    exec(code, {})
                (root / "CLAUDE.md").write_text("See AGENTS.md\n")
                exec(code, {})
                client.assert_called_with(self.pol["repo"])
        self.assertEqual(self.results()["agent-instructions"].state, "gap")

    def test_lookup_failure_never_creates_duplicate_and_other_projects_proceed(self):
        self.write("CLAUDE.md", "Diverged\n")
        self.gh.lookup_error = True
        cfg = self.cfg()
        cfg["projects"]["other"] = {**self.pol, "name": "other", "repo": "owner/other"}
        other = FakeGH("owner/other")
        clients = {"demo": self.gh, "other": other}
        results = audit.scan(cfg, clients.__getitem__)
        filing = audit.file_audit(cfg, results, clients.__getitem__)
        self.assertTrue(filing.errors)
        self.assertEqual(len(self.gh.issues), 1)
        self.assertTrue(other.issues)
        self.assertIn("retry required", self.gh.issues[filing.report_url]["body"])

    def test_scan_failure_is_unknown_and_does_not_stop_next_project(self):
        cfg = self.cfg()
        other = {**self.pol, "name": "other", "repo": "owner/other"}
        cfg["projects"]["other"] = other
        calls = []

        def scan_project(pol, _gh):
            calls.append(pol["name"])
            if pol["name"] == "demo":
                raise GHError("metadata unavailable")
            return audit.ProjectAudit("other", "owner/other", tuple(
                audit.Finding(check, "pass", "ok", ("fixture",))
                for check in config.PRACTICES_AUDIT_CHECKS))

        with patch.object(audit, "scan_project", side_effect=scan_project):
            results = audit.scan(cfg, lambda _: self.gh)
        self.assertEqual(calls, ["demo", "other"])
        self.assertEqual(
            {finding.state for finding in results[0].findings}, {"unknown"})
        self.assertEqual({finding.state for finding in results[1].findings}, {"pass"})

    def test_due_filing_resets_checkpoint_and_passes_tick_gate(self):
        ctx = self.context()
        audit.queue(ctx, [self.pol])
        self.assertIn("demo", ctx.passes_filed)
        self.assertIn("queuing practices-audit", ctx.lines[0])
        checkpoint = self.led.maintenance_checkpoint("demo", config.PRACTICES_AUDIT_PASS)
        self.assertEqual(checkpoint["last_filed_at"], iso(self.now))
        self.assertEqual(checkpoint["merged_since"], 0)
        self.assertFalse(self.led.maintenance_due(
            "demo", config.PRACTICES_AUDIT_PASS,
            policy=config.practices_audit_policy(ctx.cfg)))
        self.assertIn("<!-- mahler:practices-audit:report:", self.gh.issues[
            "https://github.com/owner/demo/issues/1"]["body"])

    def test_report_marker_recovers_created_report_without_saved_url(self):
        result = audit.scan_project(self.pol, self.gh)
        marker = "<!-- mahler:practices-audit:report:retry -->"
        first = audit.file_audit(self.cfg(), (result,), lambda _: self.gh,
                                 report_marker=marker)
        second = audit.file_audit(self.cfg(), (result,), lambda _: self.gh,
                                  report_marker=marker)
        self.assertEqual(second.report_url, first.report_url)
        self.assertEqual(len(self.gh.issues), 1)

    def test_dry_run_names_due_audit_without_writes_or_checkpoint_changes(self):
        ctx = self.context(dry_run=True)
        before = self.led.maintenance_checkpoint("demo", config.PRACTICES_AUDIT_PASS)
        audit.queue(ctx, [self.pol])
        self.assertTrue(any("practices-audit" in line for line in ctx.lines))
        self.assertEqual(ctx.passes_filed, {"demo"})
        self.assertFalse(self.gh.calls)
        self.assertEqual(self.led.maintenance_checkpoint(
            "demo", config.PRACTICES_AUDIT_PASS), before)
        self.assertIsNone(self.led.get_kv(audit.REPORT_MARKER_KEY))

    def test_cadence_volume_and_cooldown_gates(self):
        ctx = self.context()
        self.led.set_maintenance_checkpoint(
            "demo", config.PRACTICES_AUDIT_PASS,
            last_filed_at=self.now - timedelta(days=1), merged_since=0)
        with patch.object(audit, "scan") as scan:
            audit.queue(ctx, [self.pol])
            scan.assert_not_called()

        self.led.set_maintenance_checkpoint(
            "demo", config.PRACTICES_AUDIT_PASS,
            last_filed_at=self.now - timedelta(days=3), merged_since=20)
        with patch.object(audit, "scan") as scan:
            audit.queue(ctx, [self.pol])
            scan.assert_not_called()
        self.assertTrue(self.led.maintenance_due(
            "demo", config.PRACTICES_AUDIT_PASS,
            policy=config.practices_audit_policy(ctx.cfg)))

        self.led.set_maintenance_checkpoint(
            "demo", config.PRACTICES_AUDIT_PASS,
            last_filed_at=self.now - timedelta(days=15), merged_since=20)
        with patch.object(audit, "scan", return_value=()) as scan, \
                patch.object(audit, "file_audit",
                             return_value=audit.Filing("report", {}, ())):
            audit.queue(ctx, [self.pol])
            scan.assert_called_once()
        self.assertEqual(self.led.maintenance_checkpoint(
            "demo", config.PRACTICES_AUDIT_PASS)["last_filed_at"], iso(self.now))

    def test_elapsed_cadence_queues_without_merged_volume(self):
        ctx = self.context()
        self.led.set_maintenance_checkpoint(
            "demo", config.PRACTICES_AUDIT_PASS,
            last_filed_at=self.now - timedelta(days=31), merged_since=0)
        with patch.object(audit, "scan", return_value=()) as scan, \
                patch.object(audit, "file_audit",
                             return_value=audit.Filing("report", {}, ())):
            audit.queue(ctx, [self.pol])
        scan.assert_called_once()
        self.assertEqual(self.led.maintenance_checkpoint(
            "demo", config.PRACTICES_AUDIT_PASS)["last_filed_at"], iso(self.now))

    def test_disabled_or_unmanaged_anchor_skips(self):
        ctx = self.context()
        ctx.cfg["practices_audit"]["enabled"] = False
        audit.queue(ctx, [self.pol])
        self.assertFalse(self.gh.calls)
        ctx.cfg["practices_audit"]["enabled"] = True
        ctx.cfg["projects"]["demo"]["enabled"] = False
        audit.queue(ctx, [self.pol])
        self.assertFalse(self.gh.calls)
        self.assertIsNone(self.led.maintenance_checkpoint(
            "demo", config.PRACTICES_AUDIT_PASS)["last_filed_at"])

    def test_open_pass_blocks_audit_filing(self):
        self.led.upsert_item("demo", 99, labels=json.dumps(["pass:security"]),
                             state="ready")
        audit.queue(self.context(), [self.pol])
        self.assertFalse(self.gh.calls)
        self.assertIsNone(self.led.maintenance_checkpoint(
            "demo", config.PRACTICES_AUDIT_PASS)["last_filed_at"])

    def test_partial_filing_persists_identity_then_resets_after_retry(self):
        ctx = self.context()
        partial = audit.Filing("https://github.com/owner/demo/issues/10", {}, ("retry",))
        with patch.object(audit, "scan", return_value=()), \
                patch.object(audit, "file_audit", return_value=partial):
            audit.queue(ctx, [self.pol])
        self.assertEqual(self.led.get_kv(audit.REPORT_URL_KEY), partial.report_url)
        marker = self.led.get_kv(audit.REPORT_MARKER_KEY)
        self.assertTrue(marker)
        self.assertIsNone(self.led.maintenance_checkpoint(
            "demo", config.PRACTICES_AUDIT_PASS)["last_filed_at"])

        ctx = self.context()
        with patch.object(audit, "scan", return_value=()), \
                patch.object(audit, "file_audit",
                             return_value=audit.Filing(partial.report_url, {}, ())) as retry:
            audit.queue(ctx, [self.pol])
        retry.assert_called_once_with(
            ctx.cfg, (), ctx.gh, report_url=partial.report_url,
            report_marker=marker)
        self.assertEqual(self.led.maintenance_checkpoint(
            "demo", config.PRACTICES_AUDIT_PASS)["last_filed_at"], iso(self.now))
        self.assertEqual(self.led.get_kv(audit.REPORT_URL_KEY), "")
        self.assertEqual(self.led.get_kv(audit.REPORT_MARKER_KEY), "")

    def test_shipped_volume_counts_only_enabled_anchor(self):
        cfg = self.cfg()
        cfg["projects"]["other"] = {**self.pol, "name": "other", "repo": "owner/other"}
        self.led.upsert_item("demo", 1, title="anchor")
        self.led.upsert_item("other", 2, title="other")
        ctx = MagicMock(cfg=cfg, led=self.led)
        ctx.gh.return_value.comment = MagicMock()
        with patch.object(ship, "record_uat_if_needed", return_value=""), \
                patch.object(ship, "record_release_item_if_needed"), \
                patch.object(ship, "mirror_shipped"):
            ship._shipped(ctx, "demo", 1, 10, self.led.item("demo", 1),
                          {"state": "MERGED"})
            ship._shipped(ctx, "other", 2, 11, self.led.item("other", 2),
                          {"state": "MERGED"})
        self.assertEqual(self.led.maintenance_checkpoint(
            "demo", config.PRACTICES_AUDIT_PASS)["merged_since"], 1)
        self.assertEqual(self.led.maintenance_checkpoint(
            "other", config.PRACTICES_AUDIT_PASS)["merged_since"], 0)

    def test_report_failure_prevents_proposals(self):
        self.gh.create_error = True
        self.write("CLAUDE.md", "Diverged\n")
        with self.assertRaises(GHError):
            audit.file_audit(self.cfg(), (audit.scan_project(self.pol, self.gh),), lambda _: self.gh)
        self.assertFalse(self.gh.comments)
        lookups = [call for call in self.gh.calls if call[0] == "lookup"]
        self.assertEqual(len(lookups), 1)
        self.assertIn(":report:", lookups[0][1])

    def test_report_update_failure_retains_identity_for_retry(self):
        self.write("CLAUDE.md", "Diverged\n")
        result = audit.scan_project(self.pol, self.gh)
        with patch.object(self.gh, "edit_issue_body", side_effect=GHError("offline")):
            first = audit.file_audit(self.cfg(), (result,), lambda _: self.gh)
        self.assertTrue(first.report_url)
        self.assertIn("anchor report update failed; retry required", first.errors)
        second = audit.file_audit(self.cfg(), (result,), lambda _: self.gh,
                                  report_url=first.report_url)
        self.assertEqual(second.proposals, first.proposals)
        self.assertFalse(second.errors)
        self.assertEqual(len(self.gh.issues), 2)

    def test_missing_anchor_and_disabled_inventory(self):
        cfg = self.cfg()
        cfg["projects"]["demo"]["enabled"] = False
        self.assertEqual(audit.scan(cfg, lambda _: self.fail("disabled project read")), ())
        with self.assertRaises(ValueError):
            audit.file_audit(cfg, (), lambda _: self.fail("disabled anchor read"))

    def test_scanner_filer_and_dry_run_never_modify_repository_or_config(self):
        cfg = self.cfg()
        before = copy.deepcopy(cfg)
        self.write("CLAUDE.md", "Diverged\n")
        files = {str(p.relative_to(self.root)): p.read_bytes()
                 for p in self.root.rglob("*") if p.is_file()}
        settings = copy.deepcopy(self.gh.settings)
        protection = copy.deepcopy(self.gh.protection)
        results = audit.scan(cfg, lambda _: self.gh)
        audit.file_audit(cfg, results, lambda _: self.gh, dry_run=True)
        self.assertFalse(self.gh.calls)
        audit.file_audit(cfg, results, lambda _: self.gh, skips=("snapshot/repo not enabled",))
        after = {str(p.relative_to(self.root)): p.read_bytes()
                 for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(files, after)
        self.assertEqual(cfg, before)
        self.assertEqual(settings, self.gh.settings)
        self.assertEqual(protection, self.gh.protection)

    def test_executable_acceptance_check(self):
        command = audit._acceptance(self.pol, "agent-instructions")
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("state='pass'", result.stdout)

    def test_mahler_shaped_fixture_passes_and_cites_workflow_command(self):
        mahler_workflow = (
            "name: ci\n"
            "on:\n"
            "  pull_request:\n"
            "  merge_group:\n"
            "  push:\n"
            "    branches: [main]\n"
            "jobs:\n"
            "  test:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: actions/checkout@v4\n"
            "      - uses: actions/setup-python@v5\n"
            "        with:\n"
            "          python-version: '3.12'\n"
            "      - run: python -m mahler.repo_guard\n"
            "      - run: python -m unittest discover -s tests -v\n"
            "      - run: python tests/run_random.py\n"
            "      - run: python -X dev tests/run_strict.py\n"
        )
        self.write(".github/workflows/ci.yml", mahler_workflow)
        self.write("tests/run_random.py", "# random order test runner\n")
        self.write("tests/run_strict.py", "# strict test runner\n")
        findings = self.results()
        self.assertEqual(findings["ci-tests"].state, "pass")
        self.assertIn(".github/workflows/ci.yml:16 job=test run: python -m unittest discover -s tests -v",
                      findings["ci-tests"].evidence)
        finding = findings["verify-command"]
        self.assertEqual(finding.state, "pass")
        self.assertEqual(finding.reason, "Documented effective verify command is invoked by PR CI.")
        self.assertIn(".github/workflows/ci.yml:16 job=test run: python -m unittest discover -s tests -v",
                      finding.evidence)

    def test_verify_command_provenance_matrix(self):
        # 1. push-only branch restriction
        with self.subTest(case="push-only branch restriction"):
            wf = (
                "name: CI\n"
                "on:\n"
                "  pull_request:\n"
                "  push:\n"
                "    branches: [main]\n"
                "jobs:\n"
                "  test:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - run: python3 -m unittest discover -s tests\n"
            )
            self.write(".github/workflows/ci.yml", wf)
            f = self.results()["verify-command"]
            self.assertEqual(f.state, "pass")
            self.assertEqual(f.reason, "Documented effective verify command is invoked by PR CI.")

        # 2. opaque Python step after verify
        with self.subTest(case="opaque Python step after verify"):
            wf = (
                "name: CI\n"
                "on:\n"
                "  pull_request:\n"
                "jobs:\n"
                "  test:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - run: python3 -m unittest discover -s tests\n"
                "      - run: python script.py\n"
            )
            self.write(".github/workflows/ci.yml", wf)
            self.write("script.py", "# opaque\n")
            f = self.results()["verify-command"]
            self.assertEqual(f.state, "pass")
            self.assertEqual(f.reason, "Documented effective verify command is invoked by PR CI.")

        # 3. unrelated unresolved workflow
        with self.subTest(case="unrelated unresolved workflow"):
            wf1 = (
                "name: CI\n"
                "on:\n"
                "  pull_request:\n"
                "jobs:\n"
                "  test:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - run: python3 -m unittest discover -s tests\n"
            )
            wf2 = (
                "name: Unrelated\n"
                "on:\n"
                "  pull_request:\n"
                "jobs:\n"
                "  other:\n"
                "    if: false\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - run: echo other\n"
            )
            self.write(".github/workflows/ci.yml", wf1)
            self.write(".github/workflows/other.yml", wf2)
            f = self.results()["verify-command"]
            self.assertEqual(f.state, "pass")
            self.assertEqual(f.reason, "Documented effective verify command is invoked by PR CI.")
            (self.root / ".github/workflows/other.yml").unlink()

        # 4. job-level condition on verify
        with self.subTest(case="job-level condition on verify"):
            wf = (
                "name: CI\n"
                "on:\n"
                "  pull_request:\n"
                "jobs:\n"
                "  test:\n"
                "    if: github.event.pull_request.draft == false\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - run: python3 -m unittest discover -s tests\n"
            )
            self.write(".github/workflows/ci.yml", wf)
            f = self.results()["verify-command"]
            self.assertEqual(f.state, "unknown")
            self.assertIn("job condition", f.reason)
            self.assertTrue(any("job condition" in e for e in f.evidence))

        # 5. step-level condition on verify
        with self.subTest(case="step-level condition on verify"):
            wf = (
                "name: CI\n"
                "on:\n"
                "  pull_request:\n"
                "jobs:\n"
                "  test:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - if: success()\n"
                "        run: python3 -m unittest discover -s tests\n"
            )
            self.write(".github/workflows/ci.yml", wf)
            f = self.results()["verify-command"]
            self.assertEqual(f.state, "unknown")
            self.assertIn("step condition", f.reason)
            self.assertTrue(any("step condition" in e for e in f.evidence))

        # 6. PR branch/path filter
        with self.subTest(case="PR branch/path filter"):
            wf = (
                "name: CI\n"
                "on:\n"
                "  pull_request:\n"
                "    branches: [main]\n"
                "jobs:\n"
                "  test:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - run: python3 -m unittest discover -s tests\n"
            )
            self.write(".github/workflows/ci.yml", wf)
            f = self.results()["verify-command"]
            self.assertEqual(f.state, "unknown")
            self.assertIn("trigger has filters", f.reason)
            self.assertTrue(any("PR trigger filter" in e for e in f.evidence))

        # 7. unresolved step before verify
        with self.subTest(case="unresolved step before verify"):
            wf = (
                "name: CI\n"
                "on:\n"
                "  pull_request:\n"
                "jobs:\n"
                "  test:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - run: ./missing.sh\n"
                "      - run: python3 -m unittest discover -s tests\n"
            )
            self.write(".github/workflows/ci.yml", wf)
            f = self.results()["verify-command"]
            self.assertEqual(f.state, "unknown")
            self.assertIn("Unresolved execution precedes", f.reason)
            self.assertTrue(any("unresolved execution" in e for e in f.evidence))

        # 8. changed working directory for verify
        with self.subTest(case="changed working directory for verify"):
            wf = (
                "name: CI\n"
                "on:\n"
                "  pull_request:\n"
                "jobs:\n"
                "  test:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - working-directory: subdir\n"
                "        run: python3 -m unittest discover -s tests\n"
            )
            self.write(".github/workflows/ci.yml", wf)
            f = self.results()["verify-command"]
            self.assertEqual(f.state, "unknown")
            self.assertIn("changed working directory", f.reason)
            self.assertTrue(any("changed working directory" in e for e in f.evidence))

        # 9. dynamic verify command
        with self.subTest(case="dynamic verify command"):
            wf = (
                "name: CI\n"
                "on:\n"
                "  pull_request:\n"
                "jobs:\n"
                "  test:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - run: python3 -m unittest discover -s tests ${{ env.ARGS }}\n"
            )
            self.write(".github/workflows/ci.yml", wf)
            f = self.results()["verify-command"]
            self.assertEqual(f.state, "unknown")
            self.assertIn("dynamic expansion", f.reason)
            self.assertTrue(any("dynamic command" in e for e in f.evidence))

        # 10. reusable-only workflow
        with self.subTest(case="reusable-only workflow"):
            wf = (
                "name: CI\n"
                "on:\n"
                "  pull_request:\n"
                "jobs:\n"
                "  test:\n"
                "    uses: owner/repo/.github/workflows/reusable.yml@main\n"
            )
            self.write(".github/workflows/ci.yml", wf)
            f = self.results()["verify-command"]
            self.assertEqual(f.state, "unknown")
            self.assertIn("reusable workflows", f.reason)

        # 11. absent documentation
        with self.subTest(case="absent documentation"):
            self.write(".github/workflows/ci.yml", WORKFLOW)
            self.pol["verify"] = "python3 -m unittest discover -s tests"
            self.write("AGENTS.md", "# No verify contract\n")
            self.write("CLAUDE.md", "# No verify contract\n")
            self.write(".mahler/project.toml", '[release]\nhow = "merge"\n')
            f = self.results()["verify-command"]
            self.assertEqual(f.state, "gap")
            self.assertEqual(f.reason, "Verify command is absent from documentation.")

        # 12. absent literal verify invocation
        with self.subTest(case="absent literal verify invocation"):
            self.write("AGENTS.md", GUIDANCE)
            self.write("CLAUDE.md", GUIDANCE)
            self.write(".mahler/project.toml", '[verify]\nfast = "python3 -m unittest discover -s tests"\n')
            self.pol["verify"] = "python3 -m unittest discover -s tests"
            wf = (
                "name: CI\n"
                "on:\n"
                "  pull_request:\n"
                "jobs:\n"
                "  test:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - run: echo no-tests-here\n"
            )
            self.write(".github/workflows/ci.yml", wf)
            f = self.results()["verify-command"]
            self.assertEqual(f.state, "gap")
            self.assertEqual(f.reason, "Verify command is absent from literal PR CI commands.")

        # 13. step-level continue-on-error on verify
        with self.subTest(case="step-level continue-on-error on verify"):
            wf = (
                "name: CI\n"
                "on:\n"
                "  pull_request:\n"
                "jobs:\n"
                "  test:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - continue-on-error: true\n"
                "        run: python3 -m unittest discover -s tests\n"
            )
            self.write(".github/workflows/ci.yml", wf)
            f = self.results()["verify-command"]
            self.assertEqual(f.state, "unknown")
            self.assertIn("continue-on-error", f.reason)
            self.assertTrue(any(".github/workflows/ci.yml:8: continue-on-error: true" in e for e in f.evidence))

        # 14. job-level continue-on-error on verify
        with self.subTest(case="job-level continue-on-error on verify"):
            wf = (
                "name: CI\n"
                "on:\n"
                "  pull_request:\n"
                "jobs:\n"
                "  test:\n"
                "    continue-on-error: true\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - run: python3 -m unittest discover -s tests\n"
            )
            self.write(".github/workflows/ci.yml", wf)
            f = self.results()["verify-command"]
            self.assertEqual(f.state, "unknown")
            self.assertIn("continue-on-error", f.reason)
            self.assertTrue(any(".github/workflows/ci.yml:6: continue-on-error: true" in e for e in f.evidence))

        # 15. step-level continue-on-error false on verify (control)
        with self.subTest(case="step-level continue-on-error false on verify"):
            wf = (
                "name: CI\n"
                "on:\n"
                "  pull_request:\n"
                "jobs:\n"
                "  test:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - continue-on-error: false\n"
                "        run: python3 -m unittest discover -s tests\n"
            )
            self.write(".github/workflows/ci.yml", wf)
            f = self.results()["verify-command"]
            self.assertEqual(f.state, "pass")
            self.assertEqual(f.reason, "Documented effective verify command is invoked by PR CI.")

        # 16. job-level continue-on-error false on verify (control)
        with self.subTest(case="job-level continue-on-error false on verify"):
            wf = (
                "name: CI\n"
                "on:\n"
                "  pull_request:\n"
                "jobs:\n"
                "  test:\n"
                "    continue-on-error: false\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - run: python3 -m unittest discover -s tests\n"
            )
            self.write(".github/workflows/ci.yml", wf)
            f = self.results()["verify-command"]
            self.assertEqual(f.state, "pass")
            self.assertEqual(f.reason, "Documented effective verify command is invoked by PR CI.")

        # 17. step-level continue-on-error true after run on verify
        with self.subTest(case="step-level continue-on-error true after run on verify"):
            wf = (
                "name: CI\n"
                "on:\n"
                "  pull_request:\n"
                "jobs:\n"
                "  test:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - run: python3 -m unittest discover -s tests\n"
                "        continue-on-error: true\n"
            )
            self.write(".github/workflows/ci.yml", wf)
            f = self.results()["verify-command"]
            self.assertEqual(f.state, "unknown")
            self.assertIn("continue-on-error", f.reason)
            self.assertTrue(any(".github/workflows/ci.yml:9: continue-on-error: true" in e for e in f.evidence))

    def test_proposal_body_includes_false_positive_resolution_contract(self):
        self.write("CLAUDE.md", "Diverged\n")
        result = audit.scan_project(self.pol, self.gh)
        filing = audit.file_audit(self.cfg(), (result,), lambda _: self.gh)
        proposal_url = filing.proposals["demo/agent-instructions"]
        body = self.gh.issues[proposal_url]["body"]
        self.assertIn("Resolving verified scanner false positives", body)
        self.assertIn("docs/DECISIONS.md", body)
        self.assertIn("DECISIONS.md", body)
        self.assertIn("AGENTS.md", body)
        self.assertIn(".mahler/project.toml", body)
        self.assertIn("mkny13/mahler", body)
        self.assertIn("needs_you", body)

    def test_documented_decisions_in_decisions_md_resolve_cleanly(self):
        self.write("CLAUDE.md", "Diverged\n")
        findings = self.results()
        self.assertEqual(findings["agent-instructions"].state, "gap")

        self.write("DECISIONS.md", (
            "# Architectural Decisions\n\n"
            "### D12 — agent-instructions exemption\n"
            "CLAUDE.md is maintained separately for tool compatibility; "
            "scanner limitation tracked in mkny13/mahler#806.\n"
        ))
        resolved = self.results()
        self.assertEqual(resolved["agent-instructions"].state, "pass")
        self.assertIn("Documented architectural decision or exemption",
                      resolved["agent-instructions"].reason)
        self.assertIn("DECISIONS.md", resolved["agent-instructions"].evidence[0])
        self.assertIn("mkny13/mahler#806", resolved["agent-instructions"].reason)

    def test_incidental_decision_mention_does_not_suppress_tracked_secrets(self):
        self.write("credentials.env", 'PASSWORD: "very-private-password"\n')
        self.write("DECISIONS.md", (
            "# Architectural Decisions\n\n"
            "### D39 — Cross-project practices audit: evidence and proposals only\n"
            "#### Known gaps\n"
            "The tracked-secrets check does not inspect Git history.\n"
        ))
        findings = self.results()
        self.assertEqual(findings["tracked-secrets"].state, "gap")

        self.write("DECISIONS.md", (
            "# Architectural Decisions\n\n"
            "### No exemption for tracked-secrets\n"
            "The tracked-secrets check remains enforced.\n"
        ))
        enforced = self.results()
        self.assertEqual(enforced["tracked-secrets"].state, "gap")

        self.write("DECISIONS.md", (
            "# Architectural Decisions\n\n"
            "### tracked-secrets policy\n"
            "Exemptions for tracked-secrets are prohibited.\n"
        ))
        prohibited = self.results()
        self.assertEqual(prohibited["tracked-secrets"].state, "gap")

        for line in ("The tracked-secrets check does not support exemptions.",
                     "The tracked-secrets check doesn't allow exemptions.",
                     "The tracked-secrets check does not permit any exemptions."):
            self.write("DECISIONS.md", (
                "# Architectural Decisions\n\n### tracked-secrets policy\n" + line + "\n"))
            self.assertEqual(self.results()["tracked-secrets"].state, "gap", line)

        # Explicit exemption or false-positive wording in DECISIONS.md still resolves the finding
        self.write("DECISIONS.md", (
            "# Architectural Decisions\n\n"
            "### D40 — tracked-secrets exemption\n"
            "Exempt: credentials.env contains synthetic test tokens; "
            "scanner false positive tracked in mkny13/mahler#837.\n"
        ))
        resolved = self.results()
        self.assertEqual(resolved["tracked-secrets"].state, "pass")
        self.assertIn("Documented architectural decision or exemption",
                      resolved["tracked-secrets"].reason)
        self.assertIn("DECISIONS.md", resolved["tracked-secrets"].evidence[0])

    def test_project_toml_exemption_resolves_finding_cleanly(self):
        self.gh.settings["allow_squash_merge"] = False
        findings = self.results()
        self.assertEqual(findings["branch-protection"].state, "gap")

        self.write(".mahler/project.toml", (
            '[verify]\nfast = "python3 -m unittest discover -s tests"\n\n'
            '[practices.exemptions]\n'
            'branch-protection = "Solo developer repository with direct commits"\n'
        ))
        resolved = self.results()
        self.assertEqual(resolved["branch-protection"].state, "pass")
        self.assertIn(".mahler/project.toml", resolved["branch-protection"].evidence[0])
        self.assertIn("Solo developer repository", resolved["branch-protection"].reason)

    def test_agents_md_exemption_resolves_finding_cleanly(self):
        self.write(".github/workflows/ci.yml", "name: CI\non: pull_request\njobs: {}\n")
        findings = self.results()
        self.assertIn(findings["ci-tests"].state, ("gap", "unknown"))

        self.write("AGENTS.md", (
            GUIDANCE + "\n## Practices exemptions\n"
            "- `ci-tests`: tests run on external builder; scanner false positive tracked in mkny13/mahler#803.\n"
        ))
        resolved = self.results()
        self.assertEqual(resolved["ci-tests"].state, "pass")
        self.assertIn("AGENTS.md", resolved["ci-tests"].evidence[0])
        self.assertIn("external builder", resolved["ci-tests"].reason)

    def test_proposal_acceptance_passes_with_documented_decision(self):
        self.write("CLAUDE.md", "Diverged\n")
        result = audit.scan_project(self.pol, self.gh)
        filing = audit.file_audit(self.cfg(), (result,), lambda _: self.gh)
        body = self.gh.issues[filing.proposals["demo/agent-instructions"]]["body"]
        command = body.split("```sh\n", 1)[1].split("\n```", 1)[0]
        executable, flag, code = shlex.split(command)
        self.assertEqual((executable, flag), ("python3", "-c"))

        with tempfile.TemporaryDirectory() as worktree:
            root = Path(worktree)
            (root / "AGENTS.md").write_text(GUIDANCE)
            (root / "CLAUDE.md").write_text("Diverged\n")
            (root / "DECISIONS.md").write_text(
                "# Decisions\n\n### D42 — agent-instructions exemption\n"
                "Exempt due to custom tool requirements (mkny13/mahler#806).\n"
            )
            with patch("pathlib.Path.cwd", return_value=root), \
                    patch("mahler.gh.GH", return_value=self.gh), \
                    patch("builtins.print"):
                exec(code, {})

    def test_audit_rerun_cleanly_reconciles_resolved_proposals(self):
        self.write("CLAUDE.md", "Diverged\n")
        first_result = audit.scan_project(self.pol, self.gh)
        self.assertEqual(first_result.findings[1].state, "gap")
        first = audit.file_audit(self.cfg(), (first_result,), lambda _: self.gh)
        proposal_url = first.proposals["demo/agent-instructions"]
        self.assertEqual(len(self.gh.issues), 2)
        self.assertEqual(self.gh.issues[proposal_url]["state"], "open")

        # Now repo documents exemption
        self.write("DECISIONS.md", (
            "# Decisions\n\n### D1 — agent-instructions exemption\n"
            "Scanner false positive tracked in mkny13/mahler#806.\n"
        ))
        second_result = audit.scan_project(self.pol, self.gh)
        self.assertEqual(second_result.findings[1].state, "pass")

        # Re-run file_audit
        second = audit.file_audit(self.cfg(), (second_result,), lambda _: self.gh,
                                  report_url=first.report_url)
        # Reuses marker without creating duplicate issues
        self.assertEqual(len(self.gh.issues), 2)
        self.assertEqual(second.proposals["demo/agent-instructions"], proposal_url)
        # Marks proposal resolved (closed with comment)
        self.assertEqual(self.gh.issues[proposal_url]["state"], "closed")
        self.assertTrue(any("resolved" in c[1].lower() for c in self.gh.comments if c[0] == 2))
        self.assertFalse(second.errors)

        # Third run when already closed does not re-comment
        comment_count = len(self.gh.comments)
        third = audit.file_audit(self.cfg(), (second_result,), lambda _: self.gh,
                                 report_url=first.report_url)
        self.assertEqual(len(self.gh.issues), 2)
        self.assertEqual(len(self.gh.comments), comment_count)
        self.assertEqual(third.proposals["demo/agent-instructions"], proposal_url)


class SchedulerOrderingTests(unittest.TestCase):
    def test_practices_audit_follows_platform_audit_before_scheduling(self):
        now = datetime(2026, 10, 7, tzinfo=timezone.utc)
        led = Ledger(":memory:", clock=lambda: now)
        self.addCleanup(led.close)
        ctx = scheduler.Ctx({"defaults": {}, "projects": {}}, led, dry_run=True)
        order = []
        with patch("mahler.scheduler.config.enabled_projects", return_value=[]), \
                patch("mahler.scheduler.outbox.drain"), \
                patch("mahler.scheduler.compute_burst"), \
                patch("mahler.scheduler.watchdog"), \
                patch("mahler.scheduler.failures.backfill"), \
                patch("mahler.scheduler.expire"), \
                patch("mahler.scheduler.close_finished_parents"), \
                patch("mahler.scheduler.refresh_usage"), \
                patch("mahler.scheduler.resets.spend_banked"), \
                patch("mahler.scheduler.relearn_due", return_value=False), \
                patch("mahler.scheduler.warmup_pass"), \
                patch("mahler.scheduler.queue_maintenance",
                      side_effect=lambda *_: order.append("maintenance")), \
                patch("mahler.scheduler.platform_audit.queue",
                      side_effect=lambda *_: order.append("platform")), \
                patch("mahler.scheduler.practices_audit.queue",
                      side_effect=lambda *_: order.append("practices")), \
                patch("mahler.scheduler.schedule",
                      side_effect=lambda *_: order.append("schedule")), \
                patch("mahler.scheduler.ship"), \
                patch("mahler.scheduler.mirror_labels"), \
                patch("mahler.scheduler.backup.run_ledger"), \
                patch("mahler.scheduler.digest.maybe_send"), \
                patch("mahler.scheduler.janitor.maybe_run"):
            scheduler.tick(ctx)
        self.assertLess(order.index("maintenance"), order.index("platform"))
        self.assertLess(order.index("platform"), order.index("practices"))
        self.assertLess(order.index("practices"), order.index("schedule"))

    def test_scan_exception_is_reported_without_aborting_tick(self):
        now = datetime(2026, 10, 7, tzinfo=timezone.utc)
        led = Ledger(":memory:", clock=lambda: now)
        self.addCleanup(led.close)
        cfg = {"defaults": {}, "projects": {},
               "practices_audit": {"project": "mahler"}}
        ctx = scheduler.Ctx(cfg, led)
        project = {"name": "mahler", "repo": "mkny13/mahler", "path": "/tmp"}
        with ExitStack() as stack:
            stack.enter_context(patch(
                "mahler.scheduler.config.enabled_projects", return_value=[project]))
            stack.enter_context(patch("mahler.scheduler._project_ok", return_value=True))
            for target in (
                    "outbox.drain", "compute_burst", "watchdog", "sync",
                    "failures.backfill", "expire", "close_finished_parents",
                    "refresh_usage", "resets.spend_banked", "warmup_pass",
                    "queue_maintenance", "platform_audit.queue", "ship",
                    "mirror_labels", "backup.run_ledger", "digest.maybe_send",
                    "janitor.maybe_run"):
                stack.enter_context(patch(f"mahler.scheduler.{target}"))
            stack.enter_context(patch("mahler.scheduler.relearn_due", return_value=False))
            stack.enter_context(patch(
                "mahler.practices_audit.scan", side_effect=RuntimeError("scan failure")))
            schedule = stack.enter_context(patch("mahler.scheduler.schedule"))
            scheduler.tick(ctx)
        schedule.assert_called_once_with(ctx, [project])
        self.assertTrue(any("practices-audit pass failed — scan failure" in line
                            for line in ctx.lines))


class TestPracticesGHReads(unittest.TestCase):
    def setUp(self):
        self.gh = GH("owner/repo")
        self.gh._gh = MagicMock()

    def test_read_only_endpoints_and_branch_encoding(self):
        self.gh._gh.return_value = '{"default_branch": "release/main"}'
        self.assertEqual(self.gh.default_branch_settings()["default_branch"], "release/main")
        self.gh._gh.assert_called_with("api", "repos/owner/repo")
        self.gh.branch_protection("release/main")
        self.gh._gh.assert_called_with("api", "repos/owner/repo/branches/release%2Fmain/protection")
        self.gh.branch_rules("release/main")
        self.gh._gh.assert_called_with("api", "repos/owner/repo/rules/branches/release%2Fmain")

    def test_all_pages_open_closed_reuse_and_lookup_error(self):
        self.gh._gh.return_value = json.dumps([
            [{"number": 1, "body": "marker", "pull_request": {}, "html_url": "pr"}],
            [{"number": 2, "body": "marker", "state": "closed", "html_url": "issue"}]])
        self.assertEqual(self.gh.issue_by_marker("marker"), "issue")
        self.gh._gh.assert_called_with("api", "--paginate", "--slurp",
                                      "repos/owner/repo/issues?state=all&per_page=100")
        self.gh._gh.side_effect = GHError("failed page")
        with self.assertRaises(GHError):
            self.gh.issue_by_marker("marker")

    def test_issue_body_update(self):
        self.gh.edit_issue_body(12, "report")
        self.gh._gh.assert_called_with("issue", "edit", "12", "-R", "owner/repo",
                                      "--body-file", "-", input="report")


if __name__ == "__main__":
    unittest.main()


class FilingIdentityTests(unittest.TestCase):
    def test_real_factory_separates_anchor_and_proposal_and_resumes(self):
        import os
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["projects"] = {
            "mahler": {"enabled": True, "repo": "personal/mahler", "path": "/fake"},
            "work": {"enabled": True, "repo": "work/app", "path": "/fake",
                     "gh_account": "work"}}
        cfg["accounts"] = {"work": {"env": {"GH_CONFIG_DIR": "/fake/work-gh"}}}
        cfg["github_app"] = {"app_id": 123, "installation_id": 456,
                             "private_key_path": "/fake/key.pem"}
        result = audit.ProjectAudit("work", "work/app", (
            audit.Finding("agent-instructions", "gap", "Missing guidance", ("AGENTS.md",)),))
        issues = {"personal/mahler": [], "work/app": []}
        operations = []

        def run(argv, **kwargs):
            if argv[1] == "api":
                repo = argv[-1].split("repos/", 1)[1].split("/issues", 1)[0]
                output = json.dumps([issues[repo]])
                operation = "lookup"
            else:
                repo = argv[argv.index("-R") + 1]
                operation = argv[2]
                output = ""
                if argv[1:3] == ["issue", "create"]:
                    output = f"https://github.com/{repo}/issues/{len(issues[repo]) + 1}"
                    issues[repo].append({"html_url": output, "body": kwargs["input"],
                                         "state": "closed"})
            env = kwargs["env"]
            if repo == "work/app":
                self.assertEqual(env["GH_CONFIG_DIR"], "/fake/work-gh")
                for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_HOST"):
                    self.assertNotIn(key, env)
            else:
                self.assertEqual(env["GH_TOKEN"], "fake-app-token")
            operations.append((repo, operation))
            return subprocess.CompletedProcess(argv, 0, output, "")

        led = Ledger(":memory:")
        self.addCleanup(led.close)
        with tempfile.TemporaryDirectory() as tmp, patch.object(config, "STATE", tmp), \
                patch.dict(os.environ, {key: "daemon" for key in config.GH_IDENTITY_VARS}), \
                patch("mahler.github_app.Installation", autospec=True) as app, \
                patch("mahler.gh.subprocess.run", side_effect=run):
            app.return_value.token.return_value = "fake-app-token"
            first = audit.file_audit(cfg, (result,), scheduler.Ctx(cfg, led).gh)
            self.assertEqual(first.errors, ())
            self.assertEqual(app.call_count, 1)  # personal only
            second = audit.file_audit(cfg, (result,), scheduler.Ctx(cfg, led).gh,
                                      report_url=first.report_url)
            self.assertEqual(second.errors, ())
            self.assertEqual(second.proposals, first.proposals)
            self.assertEqual(app.call_count, 2)  # fresh personal client only
        self.assertEqual([len(rows) for rows in issues.values()], [1, 1])
        self.assertIn(("personal/mahler", "lookup"), operations)
        self.assertIn(("work/app", "lookup"), operations)
        self.assertIn(("work/app", "comment"), operations)
