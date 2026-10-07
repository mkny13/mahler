"""Keep agent safeguards and outcome contracts in the rendered recipes."""

import sqlite3
import unittest

from mahler import prompt, config


class SortRecipeTests(unittest.TestCase):
    def setUp(self):
        self.values = dict(number=238, repo="example/project", title="Sort safeguards",
                           worktree="/tmp/example-worktree", rules="Sample project rules",
                           sizing="")
        self.rendered = prompt.render("sort", **self.values)
        self.text = " ".join(self.rendered.split())

    def test_preserves_relationships_and_planned_sections(self):
        self.assertIn("Keep existing `Part of #N` and `Depends on: #N` lines, "
                      "each on its own unquoted line", self.text)
        self.assertIn("never inside the original-request quote", self.text)
        self.assertIn("Quote only the owner's free-form words", self.text)
        self.assertIn("if the body already has this shape, edit its sections in place",
                      self.text)

    def test_done_when_checklist_bounds_scope_and_checks_feasibility(self):
        for fragment in (
            "Done-when checklist — apply to this issue and each sub-issue before marking ready",
            "- [ ] Bounded and checkable", "a named finite list or a measurable threshold",
            "a defined scope and verification method",
            'Never use "every" / "all" over an open-ended set',
            "- [ ] Feasible and consistent",
            "the goal, acceptance criteria and constraints can be satisfied together",
            'identifiers contradicts "no app source changes"',
            "end NEEDS-YOU with concise options",
            "- [ ] Prerequisites explicit",
            "split needed prerequisite work into its own mergeable issue with a test",
            "add an unquoted `Depends on: #N` line to the dependent issue",
            "Do not use a dependency to silently override an owner's constraint",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.text)

    def test_bug_only_escape_sections(self):
        for fragment in (
            "For `type:bug` issues only (including bug sub-issues)",
            "between `## Plan` and `## Done when`",
            "## Escape cause", "Why existing gates missed this bug",
            "## Check that now catches it", "how to exercise it",
            "If no feasible mechanical check exists, explicitly explain why here",
            "Non-bug issues keep the standard shape above unchanged",
        ):
            self.assertIn(fragment, self.text)
        standard = self.rendered.split("exactly this shape", 1)[1].split(
            "For `type:bug`", 1)[0]
        self.assertNotIn("## Escape cause", standard)
        self.assertNotIn("## Check that now catches it", standard)

    def test_sub_issues_require_something_to_ship(self):
        self.assertIn("Every sub-issue must end in a commit", self.text)
        self.assertIn("DONE with nothing to push counts as a failed attempt", self.text)
        self.assertIn("Do GitHub-only housekeeping or writing", self.text)
        self.assertIn("in this planning run, record each action in an issue comment, "
                      "and file no sub-issue for it", self.text)

    def test_template_values_and_status_contract(self):
        for name, value in self.values.items():
            with self.subTest(variable=name):
                # We skip checking $sizing since it is explicitly empty for self.rendered
                if name == "sizing":
                    continue
                self.assertNotIn(f"${name}", self.rendered)
                self.assertIn(str(value), self.rendered)
        self.assertNotIn("$", self.rendered)
        for status in ("READY", "SPLIT", "NEEDS-YOU"):
            with self.subTest(status=status):
                self.assertTrue(any(line == f"STATUS: {status}" or
                                    line.startswith(f"STATUS: {status} ")
                                    for line in self.rendered.splitlines()))


class SizeTargetTests(unittest.TestCase):
    def test_size_target_of(self):
        # returns "s" for account = "work"
        self.assertEqual(config.size_target_of({"account": "work"}), "s")
        # "" for default personal project
        self.assertEqual(config.size_target_of({}), "")
        self.assertEqual(config.size_target_of({"account": "personal"}), "")
        # "" for accounts = ["personal", "work"]
        self.assertEqual(config.size_target_of({"accounts": ["personal", "work"]}), "")
        # explicit value when one is set
        self.assertEqual(config.size_target_of({"account": "personal", "size_target": "s"}), "s")
        self.assertEqual(config.size_target_of({"account": "work", "size_target": ""}), "")
        self.assertEqual(config.size_target_of({"account": "work", "size_target": "invalid"}), "")

    def test_prompts(self):
        class DummyCtx:
            def policy(self, proj):
                if proj == "work-proj":
                    return {"repo": "a/b", "rules": "", "account": "work"}
                return {"repo": "a/b", "rules": ""}
        ctx = DummyCtx()
        item = {"number": 1, "title": "T", "pr": 2}
        prep = {"worktree": "/tmp", "branch": "b", "replayed": False, "kept": None}
        
        work_sort = prompt.build(ctx, "work-proj", item, "sort", "claude", prep)
        pers_sort = prompt.build(ctx, "pers-proj", item, "sort", "claude", prep)
        
        self.assertIn("Sizing for this project (from Mahler's config)", work_sort)
        self.assertNotIn("Sizing for this project", pers_sort)
        self.assertNotIn("$sizing", work_sort)
        self.assertNotIn("$sizing", pers_sort)
        
        work_build = prompt.build(ctx, "work-proj", item, "build", "claude", prep)
        pers_build = prompt.build(ctx, "pers-proj", item, "build", "claude", prep)
        
        self.assertNotIn("Sizing for this project", work_build)
        self.assertNotIn("Sizing for this project", pers_build)


    def test_build_accepts_a_sqlite_row(self):
        # The scheduler hands prompt.build a sqlite3.Row, which has no .get
        # (mahler#409: every launch failed for ~18h while this test used dicts).
        class DummyCtx:
            def policy(self, proj):
                return {"repo": "a/b", "rules": ""}
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        prep = {"worktree": "/tmp", "branch": "b", "replayed": False, "kept": None}
        for pr in (None, 7):
            item = con.execute("SELECT 1 AS number, 'T' AS title, ? AS pr", (pr,)).fetchone()
            for role in ("sort", "build"):
                with self.subTest(pr=pr, role=role):
                    self.assertIn("a/b", prompt.build(DummyCtx(), "p", item, role, "claude", prep))
        con.close()

class BuildRecipeTests(unittest.TestCase):
    def setUp(self):
        self.values = dict(
            number=358,
            repo="mkny13/example",
            title="Define What's New contract",
            platform="agy-gemini",
            worktree="/tmp/worktree",
            branch="mahler/358-whats-new",
            handoff="",
            verify="python3 -m unittest",
            base="main",
            rules="",
            # mahler#569: the contract is now a template value; a relevant
            # build passes it (its title asks for the What's New contract).
            whats_new=prompt.WHATS_NEW_GUIDANCE,
        )
        self.rendered = prompt.render("build", **self.values)
        self.text = " ".join(self.rendered.split())

    def test_bug_completion_requires_exercised_check_or_documented_exception(self):
        for fragment in (
            "For a `type:bug` fix", "## Escape cause", "## Check that now catches it",
            "Do not report `STATUS: DONE` unless the named catching check exists "
            "and has been exercised, or the issue explicitly records why no feasible "
            "mechanical check exists",
            "tests, CI steps, lints, contract checks, and smoke journeys",
            "required bug-fix scope", "check command/path and its result",
            "A missing section is not an exemption",
            "does not waive rule 2's verification of the fix",
        ):
            self.assertIn(fragment, self.text)

    def test_material_spec_contradiction_requires_owner_resolution(self):
        owner_rule = self.text.split("5. Stop only", 1)[1].split("6. If", 1)[0]
        for fragment in (
            "A material spec contradiction is an owner decision",
            "cannot be met without violating another requirement or explicit constraint",
            "stop the conflicting work",
            "Do not implement one side and silently ignore the other",
            "State the conflicting requirements and why they cannot both hold in the issue comment",
            "`STATUS: NEEDS-YOU` question and two or three concise `OPTIONS:`",
            "First check authoritative owner comments for an existing resolution",
            "routine technical choices remain yours to make",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, owner_rule)

    def test_whats_new_contract_guidance_present(self):
        # Explicit trigger and scoping rule
        self.assertIn("12. **In-app What's New contract:**", self.text)
        self.assertIn("When (and only when) the issue explicitly asks for an in-app What's New surface or release feed", self.text)
        self.assertIn("Do not add What's New UI or feed consumption to tasks that do not explicitly request it", self.text)

        # Versioning and schema
        self.assertIn("Follow DESIGN D31's schema v1 JSON contract", self.text)
        self.assertIn("schema_version", self.text)
        self.assertIn("SemVer-based", self.text)
        self.assertIn("store the highest acknowledged version, show newer releases", self.text)

        # Read-only transport and local acknowledgement
        self.assertIn("Keep transport strictly read-only: apps consume the feed; they never publish releases or write read/acknowledgement state back to Mahler", self.text)
        self.assertIn("Client acknowledgement is local to each app installation", self.text)
        self.assertIn("marked read only after the user views or dismisses the surface", self.text)

        # Fault tolerance: must not block startup
        self.assertIn("Missing, unreachable, or malformed feed responses must degrade gracefully and never block app startup", self.text)

        # Native design conventions
        self.assertIn("Preserve the app's native design conventions", self.text)

        # Maintenance visibility collapsed/hidden by default
        self.assertIn("Hide maintenance details initially: render features and fixes prominently; keep maintenance collapsed or secondary", self.text)

        # Operational privacy boundaries
        self.assertIn("Exclude operational data: the feed provides release metadata only; never consume or display issue comments, run logs, credentials, or UAT notes", self.text)

        # Automated testing requirements
        self.assertIn("Add automated tests in the app covering JSON payload parsing, SemVer comparison, offline fallback, and local acknowledgement read-state persistence", self.text)

    def test_unrelated_status_output_contract_unaffected(self):
        # The STATUS line contract must remain exact and untampered
        status_lines = [line.strip() for line in self.rendered.splitlines() if line.startswith("STATUS:")]
        self.assertEqual(status_lines, [
            "STATUS: DONE <one-line summary of what changed>",
            "STATUS: NEEDS-YOU <the question, on one line> [OPTIONS: <choice> | <choice>]",
            "STATUS: BLOCKED <reason>",
            "STATUS: YIELDED <handoff summary>",
        ])
        for line in status_lines:
            self.assertNotIn("What's New", line)
            self.assertNotIn("release", line)

    def test_unattended_run_keeps_working_without_trading_verification_for_speed(self):
        self.assertIn("Time matters here: do not spend time that can be avoided; the "
                      "earlier a correct, verified result is obtained, the better", self.text)
        self.assertIn("Never trade verification for speed", self.text)
        for early_stop in (
                "a summary that announces the next step without taking it",
                "an offer to continue unless the owner prefers otherwise",
                "a list of owner decisions when none blocks the remaining work",
                "a long turn or completed milestone that merely feels like a good place "
                "to report",
        ):
            with self.subTest(early_stop=early_stop):
                self.assertIn(early_stop, self.text)
        self.assertIn("Put status notes and recommendations in the same message as your "
                      "next action", self.text)
        self.assertIn("carry on with everything that does not depend on the owner's answer",
                      self.text)
        self.assertIn("The wanted stops are the recipe's final STATUS lines", self.text)
        self.assertIn("`NEEDS-YOU` and `BLOCKED` remain reserved for the cases defined in "
                      "rules 5–6, where nothing can move without the owner", self.text)
        self.assertIn("does not override the destructive-action cautions in rule 7",
                      self.text)

    def test_template_values_substituted_without_leftover_variables(self):
        for k, v in self.values.items():
            if k in ("handoff", "rules"):
                continue
            with self.subTest(variable=k):
                self.assertNotIn(f"${k}", self.rendered)
                self.assertIn(str(v), self.rendered)
        self.assertNotIn("$", self.rendered)


class WhatsNewMatchTests(unittest.TestCase):
    """The injector matches explicit What's New / release-feed asks only
    (mahler#569)."""

    def test_explicit_asks_match(self):
        for text in ("Add a What's New surface",
                     "Build the in-app WHAT'S NEW panel",
                     "Render what’s new from a feed",          # curly apostrophe
                     "Consume the whats new feed offline",
                     "Show what is new after each update",
                     "Expose a release feed for the app",
                     "Ship a Release-Notes Feed endpoint",
                     "release notes feed in the console"):
            with self.subTest(text=text):
                self.assertTrue(prompt.needs_whats_new({"title": text, "issue_body": ""}))
                self.assertTrue(prompt.needs_whats_new(
                    {"title": "Console polish", "issue_body": f"Please {text}."}))

    def test_unrelated_text_does_not_match(self):
        for text in ("", "Release 2.0", "draft the release notes",
                     "cut a new release and tag the build",
                     "the release script needs tidy-up",
                     "Explore what new ideas could fit",
                     "what to do next"):
            with self.subTest(text=text):
                self.assertFalse(prompt.needs_whats_new({"title": text, "issue_body": ""}))
                self.assertFalse(prompt.needs_whats_new(
                    {"title": "Console polish", "issue_body": text}))

    def test_missing_or_none_body_and_row_shapes(self):
        # sqlite Row without an issue_body column (mahler#409's lesson), and
        # a NULL cached body, must not blow up or trigger.
        self.assertFalse(prompt.needs_whats_new({"title": "T"}))
        self.assertFalse(prompt.needs_whats_new({"title": "T", "issue_body": None}))
        self.assertTrue(prompt.needs_whats_new(None) is False)
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        item = con.execute("SELECT 'Add a What''s New surface' AS title, NULL AS issue_body"
                           ).fetchone()
        self.assertTrue(prompt.needs_whats_new(item))
        con.close()


class BuildPromptWhatsNewInjectionTests(unittest.TestCase):
    """prompt.build injects the What's New contract only for relevant issues
    (mahler#569), with the STATUS-line contract untouched either way."""

    STATUS_LINES = [
        "STATUS: DONE <one-line summary of what changed>",
        "STATUS: NEEDS-YOU <the question, on one line> [OPTIONS: <choice> | <choice>]",
        "STATUS: BLOCKED <reason>",
        "STATUS: YIELDED <handoff summary>",
    ]

    def setUp(self):
        class DummyCtx:
            def policy(self, proj):
                return {"repo": "a/b", "rules": ""}
        self.ctx = DummyCtx()
        self.prep = {"worktree": "/tmp", "branch": "b", "replayed": False, "kept": None}
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.addCleanup(self.con.close)

    def rendered(self, title, body=""):
        item = self.con.execute(
            "SELECT 7 AS number, ? AS title, ? AS issue_body, NULL AS pr",
            (title, body)).fetchone()
        return prompt.build(self.ctx, "p", item, "build", "claude", self.prep)

    def status_lines(self, rendered):
        return [line.strip() for line in rendered.splitlines()
                if line.startswith("STATUS:")]

    def test_relevant_issue_gets_the_full_contract(self):
        for title, body in (("Add a What's New surface to the app", ""),
                            ("Console polish", "Consume the release feed per D31.")):
            with self.subTest(title=title, body=body):
                rendered = self.rendered(title, body)
                text = " ".join(rendered.split())
                self.assertIn("When (and only when) the issue explicitly asks for an "
                              "in-app What's New surface or release feed", text)
                self.assertIn("Do not add What's New UI or feed consumption to tasks "
                              "that do not explicitly request it", text)
                self.assertEqual(self.status_lines(rendered), self.STATUS_LINES)
                self.assertNotIn("$", rendered)

    def test_unrelated_issue_gets_none_of_the_contract(self):
        rendered = self.rendered("Tidy the release script",
                                 "Bump the changelog and cut a tag.")
        text = " ".join(rendered.split())
        for fragment in ("What's New", "release feed", "schema v1", "DESIGN D31",
                         "SemVer", "acknowledged version"):
            self.assertNotIn(fragment, text)
        # No unresolved template variables either.
        self.assertNotIn("$", rendered)
        self.assertEqual(self.status_lines(rendered), self.STATUS_LINES)


class FixRecipeTests(unittest.TestCase):
    def setUp(self):
        rendered = prompt.render(
            "fix", number=565, repo="example/project", title="CI failure",
            platform="codex", worktree="/tmp/example-worktree",
            branch="mahler/565-ci-failure", handoff="", rules="",
            verify="python3 -m unittest", base="main",
        )
        self.text = " ".join(rendered.split())

    def test_unrelated_technical_failure_is_retryable(self):
        diagnosis = self.text.split("1. ", 1)[1].split("2. ", 1)[0]
        self.assertIn("Diagnose before changing anything", diagnosis)
        self.assertIn("If the failure is unrelated to this PR", diagnosis)
        self.assertIn("runner, network, or infrastructure failure", diagnosis)
        self.assertIn("end with `STATUS: BLOCKED <reason>`", diagnosis)
        self.assertIn("concise diagnostic reason so the conductor can retry", diagnosis)
        self.assertNotIn("NEEDS-YOU", diagnosis)
        self.assertIn("Do not make speculative changes to unrelated code or "
                      "shotgun-fix the failure", diagnosis)

    def test_needs_you_is_reserved_for_owner_decisions(self):
        owner_rule = self.text.split("6. ", 1)[1].split("7. ", 1)[0]
        self.assertIn("Stop only for a decision genuinely only the owner can make "
                      "(product intent, credentials, payment, accounts, destructive data)",
                      owner_rule)
        self.assertIn("Post it as an issue comment", owner_rule)
        self.assertIn("STATUS: NEEDS-YOU <the question, on one line>", owner_rule)

    def test_real_fixes_still_require_verification_and_push(self):
        self.assertIn("Verify before every push:** `python3 -m unittest`", self.text)
        self.assertIn("When the failure is fixed and `python3 -m unittest` passes, "
                      "commit, push, and end with `STATUS: DONE", self.text)

    def test_unattended_run_keeps_working_without_trading_verification_for_speed(self):
        self.assertIn("Time matters here: do not spend time that can be avoided; the "
                      "earlier a correct, verified result is obtained, the better", self.text)
        self.assertIn("Never trade verification for speed", self.text)
        for early_stop in (
                "a summary that announces the next step without taking it",
                "an offer to continue unless the owner prefers otherwise",
                "a list of owner decisions when none blocks the remaining work",
                "a long turn or completed milestone that merely feels like a good place "
                "to report",
        ):
            with self.subTest(early_stop=early_stop):
                self.assertIn(early_stop, self.text)
        self.assertIn("Put status notes and recommendations in the same message as your "
                      "next action", self.text)
        self.assertIn("carry on with everything that does not depend on the owner's answer",
                      self.text)
        self.assertIn("The wanted stops are the recipe's final STATUS lines", self.text)
        self.assertIn("`NEEDS-YOU` remains reserved for owner decisions under rule 6",
                      self.text)
        self.assertIn("`BLOCKED` remains available for unrelated runner, network, or "
                      "infrastructure failures under rule 1 so the conductor can retry, "
                      "and for owner-dependent blockers under rule 7", self.text)
        self.assertIn("does not override the destructive-action cautions in rule 8",
                      self.text)

    def test_fix_recipe_checks_pr_toolchain_changes_before_editing(self):
        for fragment in (
            "Start with the changed CI/toolchain file list above",
            "Compare the PR's changes against `main`",
            "`.github/workflows/**`", "`project.yml`/XcodeGen",
            "Gradle/SDK versions", "`package.json` engines", "lockfiles",
            "identify whether the failing source was changed by the PR",
            "latest green run of the same job on `main`",
            "Never change production code solely to satisfy a different or older "
            "toolchain than base CI uses",
            "Do not loosen tests or timeouts as a guess",
            "if GitHub cannot provide the diff or the cause remains unclear, end with "
            "`STATUS: BLOCKED`",
            "state the evidence needed rather than guessing",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.text)


class ReviewOperatingContextTests(unittest.TestCase):
    def test_personal_context_calibrates_loss_without_weakening_blockers(self):
        text = " ".join(prompt.render("review", handoff="").split())
        for fragment in (
            "Use the effective `review_context` in the handoff",
            "one owner controls all devices and upgrades them together",
            "loss requiring mixed-version clients, staggered-upgrade/staggered-rollout sequences, "
            "or multi-user-only behavior is a **follow-up**, unless you demonstrate silent, "
            "unrecoverable loss in the normal single-user flow",
            "An override replaces the personal context; an empty context clears it",
            "Security defects and demonstrated normal-flow data loss remain **blocking**",
            "does not change the issue's acceptance criteria",
            "an explicit Done-when requirement for mixed-version or multi-user support still applies",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)


class ReviewEvidenceTests(unittest.TestCase):
    def test_description_only_evidence_is_nonblocking(self):
        text = " ".join(prompt.render("review", handoff="").split())
        for fragment in (
            "PR-description content alone is never blocking",
            "Required evidence such as audit output belongs in the repository or CI",
            "a PR-description-only evidence request is a follow-up",
            "does not demote demonstrated code, security, or actual acceptance failures",
        ):
            self.assertIn(fragment, text)


class ReviewRecipeMaskingTests(unittest.TestCase):
    def test_review_recipe_warns_about_masked_tool_output(self):
        text = " ".join(prompt.render(
            "review", number=628, repo="example/project", title="t", platform="copilot",
            pr=1, branch="b", worktree="/tmp/w", handoff="", rules="").split())
        for fragment in (
            "`******` is usually output masking, not file content",
            "do not fail on it",
            "`grep -c` for the literal asterisks",
            "run the relevant tests",
            "Only a literal confirmed that way is a finding",
        ):
            self.assertIn(fragment, text)


class ReviewRecipeToolchainTests(unittest.TestCase):
    def test_review_recipe_blocks_unrequested_toolchain_downgrades(self):
        text = " ".join(prompt.render(
            "review", number=681, repo="example/project", title="t", platform="copilot",
            pr=519, branch="b", worktree="/tmp/w", handoff="", rules="").split())
        for fragment in (
            "Compare CI/toolchain setup with the base",
            "removal or downgrade of CI/toolchain setup",
            "workflow toolchain-selection steps", "XcodeGen `project.yml`",
            "Gradle/SDK versions", "package engines", "lockfiles",
            "is a blocking finding unless the issue explicitly requested that change",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)


class FixPromptToolchainHandoffTests(unittest.TestCase):
    class Context:
        def __init__(self, gh):
            self._gh = gh
            self.messages = []

        def gh(self, project):
            return self._gh

        def say(self, message):
            self.messages.append(message)

    def setUp(self):
        from unittest.mock import MagicMock
        self.gh = MagicMock()
        self.gh.repo = "example/project"
        self.gh.failed_run_log.return_value = (41, "BUILD FAILED")
        self.ctx = self.Context(self.gh)
        self.item = {"number": 681, "pr": 519}

    def test_changed_config_files_appear_before_failed_log(self):
        self.gh.pr_ci_toolchain_files.return_value = [
            ".github/workflows/macos-tests.yml", "project.yml"]
        rendered = prompt.ci_handoff(self.ctx, "p", self.item, "mahler/681-fix")
        self.assertLess(rendered.index(".github/workflows/macos-tests.yml"),
                        rendered.index("BUILD FAILED"))
        self.assertIn("`project.yml`", rendered)

    def test_no_changed_config_files_is_explicit(self):
        self.gh.pr_ci_toolchain_files.return_value = []
        rendered = prompt.ci_handoff(self.ctx, "p", self.item, "mahler/681-fix")
        self.assertIn("changed CI/toolchain files in the PR: none found", rendered)

    def test_unavailable_config_list_is_nonfatal_and_actionable(self):
        from mahler.gh import GHError
        self.gh.pr_ci_toolchain_files.side_effect = GHError("offline")
        rendered = prompt.ci_handoff(self.ctx, "p", self.item, "mahler/681-fix")
        self.assertIn("changed CI/toolchain files in the PR: unavailable", rendered)
        self.assertIn("gh pr diff 519 -R example/project --name-only", rendered)
        self.assertIn("BUILD FAILED", rendered)
        self.assertEqual(len(self.ctx.messages), 1)

    def test_review_fix_context_also_gets_changed_config_files(self):
        self.gh.pr_ci_toolchain_files.return_value = [".github/workflows/ci.yml"]
        prep = {"worktree": "/tmp/w", "branch": "mahler/681-fix",
                "replayed": False, "kept": None}

        class BuildContext(FixPromptToolchainHandoffTests.Context):
            def policy(self, project):
                return {"repo": "example/project", "rules": ""}

        item = dict(self.item, title="CI fix")
        rendered = prompt.build(BuildContext(self.gh), "p", item, "fix", "claude",
                                prep, context="Review finding: test failure")
        self.assertLess(rendered.index(".github/workflows/ci.yml"),
                        rendered.index("Review finding: test failure"))


class CodingScopeRecipeTests(unittest.TestCase):
    def test_verification_scope_and_review_in_both_rendered_prompts(self):
        for role, blocker in (("build", 6), ("fix", 7)):
            with self.subTest(role=role):
                rendered = prompt.render(
                    role, number=578, repo="example/project", title="Prompt guidance",
                    platform="codex", worktree="/tmp/example-worktree",
                    branch="mahler/578-guidance", handoff="", rules="",
                    verify="python3 -m unittest", base="main", whats_new="",
                )
                text = " ".join(rendered.split())
                verify = text.split("2. **Verify", 1)[1].split("3. **Finish", 1)[0]
                scope = text.split("3. **Finish", 1)[1].split(
                    "3a. " if role == "build" else "4. ", 1)[0]
                for fragment in (
                    "Run a check that exercises the change",
                    "Neither a syntax-only check nor a command that failed to start "
                    "counts as verification",
                    "If declared dependencies are missing, install them using the "
                    "project's own package manager and lockfile",
                    "`npm install` or `pip install -r requirements.txt`",
                    "never use `sudo` or the system package manager unless explicitly "
                    "instructed otherwise",
                    "Do not report `STATUS: DONE` when no real check exercised the change",
                    "name the missing check and why it could not run in your final message",
                    "do not claim the change is verified",
                    f"Use the existing owner-dependent blocker ending in rule {blocker} "
                    "only when that rule applies",
                    "inability to run a check alone is not a new `BLOCKED` case",
                ):
                    with self.subTest(fragment=fragment):
                        self.assertIn(fragment, verify)
                for fragment in (
                    "Once the issue's work is complete and its checks pass, checkpoint, "
                    "push, and report",
                    "without adding unrequested features, tests, files, documentation, "
                    "or refactors",
                    "Tests required by the issue's \"Done when\" checks or the repo's "
                    '`AGENTS.md` are requested work',
                    "Do not initiate extra review or hardening rounds",
                    "or spawn reviewer sub-agents unless the issue asks for a review",
                    "Mahler's conductor owns independent review through `recipes/review.md`",
                    "Mention useful additions or a deeper review in the final message "
                    "before the STATUS line instead of undertaking them",
                ):
                    with self.subTest(fragment=fragment):
                        self.assertIn(fragment, scope)
                owner_rule = text.split(f"{blocker}. If you genuinely cannot proceed", 1)[1]
                self.assertTrue(owner_rule.startswith(
                    " (missing access, an environment only the owner can fix)"))
                summary = "changed" if role == "build" else "you fixed"
                self.assertEqual(rendered.splitlines()[-4:], [
                    f"STATUS: DONE <one-line summary of what {summary}>",
                    "STATUS: NEEDS-YOU <the question, on one line> "
                    "[OPTIONS: <choice> | <choice>]",
                    "STATUS: BLOCKED <reason>",
                    "STATUS: YIELDED <handoff summary>",
                ])


class DesignRecipeTests(unittest.TestCase):
    def test_contract_is_plan_only_and_returns_structured_dispositions(self):
        rendered = prompt.render(
            "design", number=714, repo="mkny13/mahler", title="Design",
            platform="claude", worktree="/tmp/wt", branch="mahler/714-design",
            base="main", pr=42, head="a" * 40, handoff="complete review history",
            rules="effective project rules")
        compact = " ".join(rendered.split())
        for fragment in (
            "read-only checkout", "Do not edit files", "create or amend commits, push",
            "write to GitHub", "complete supplied review history", "STATUS: DESIGNED",
            '"disposition":"fix"', '"disposition":"followups"', '"files":["path"]',
            '"steps":["..."]', '"tests":"..."', "security finding",
            "unsatisfied Done-when criterion", "silent unrecoverable data loss",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, compact)
        for value in ("714", "mkny13/mahler", "42", "a" * 40,
                      "complete review history", "effective project rules"):
            self.assertIn(value, rendered)

    def test_design_prompt_includes_required_pr_context(self):
        class DummyCtx:
            def policy(self, project):
                return {"repo": "mkny13/mahler", "rules": "project rules",
                        "base": "main"}

        item = {"number": 714, "title": "Design", "pr": 42}
        prep = {"worktree": "/tmp/wt", "branch": "mahler/714-design",
                "head_sha": "a" * 40}
        text = prompt.build(DummyCtx(), "mahler", item, "design", "claude",
                            prep, context="all review findings")
        for value in ("issue #714", "PR #42", "a" * 40, "all review findings",
                      "project rules"):
            self.assertIn(value, text)
        with self.assertRaisesRegex(ValueError, "review history"):
            prompt.build(DummyCtx(), "mahler", item, "design", "claude", prep)


if __name__ == "__main__":
    unittest.main()
