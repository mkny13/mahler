import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from mahler import config, practices_audit as audit
from mahler.gh import GH, GHError


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
        if self.lookup_error:
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

    def test_six_pass_outcomes_and_citations(self):
        findings = self.results()
        self.assertEqual({check: "pass" for check in config.PRACTICES_AUDIT_CHECKS},
                         {check: f.state for check, f in findings.items()})
        self.assertIn("ci.yml:8", str(findings["ci-tests"].evidence))
        self.assertIn("heuristics do not prove absence", findings["tracked-secrets"].reason)

    def test_build_only_is_not_test_coverage(self):
        self.write(".github/workflows/ci.yml", WORKFLOW.replace(
            "python3 -m unittest discover -s tests", "npm run build"))
        self.write("package.json", json.dumps({"scripts": {"build": "next build"}}))
        self.assertEqual(self.results()["ci-tests"].state, "gap")

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
        self.assertEqual(self.results()["ci-tests"].state, "unknown")

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

    def test_unavailable_tracked_inventory_and_nontext_are_explicit_unknowns(self):
        with patch.object(audit._Tree, "tracked", side_effect=OSError("secret diagnostic")):
            finding = self.results()["tracked-secrets"]
            self.assertEqual(finding.state, "unknown")
            self.assertNotIn("secret diagnostic", repr(finding))
        self.write("binary", "\0binary")
        self.assertEqual(self.results()["tracked-secrets"].state, "unknown")

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

    def test_scope_labels_anchor_first_links_and_closed_marker_reuse(self):
        self.pol.update(scope="label", scope_label="custom")
        self.write("CLAUDE.md", "Diverged\n")
        result = audit.scan_project(self.pol, self.gh)
        first = audit.file_audit(self.cfg(), (result,), lambda _: self.gh)
        proposal = first.proposals["demo/agent-instructions"]
        self.assertIn(first.report_url, self.gh.issues[proposal]["body"])
        self.assertEqual(self.gh.calls[1], ("create", "Cross-project practices audit"))
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

    def test_report_failure_prevents_proposals(self):
        self.gh.create_error = True
        self.write("CLAUDE.md", "Diverged\n")
        with self.assertRaises(GHError):
            audit.file_audit(self.cfg(), (audit.scan_project(self.pol, self.gh),), lambda _: self.gh)
        self.assertFalse(self.gh.comments)
        self.assertFalse(any(call[0] == "lookup" for call in self.gh.calls))

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
