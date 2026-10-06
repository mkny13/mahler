"""What a run is told: recipe rendering, handoff text and CI context.

Kept apart from runner.py (mahler#70): the runner owns worktrees and
processes, the prompt owns the words. `build()` turns a prepared worktree
(runner.prepare) into the single string the CLI is launched with.
"""

import os
import re
import string

from . import config, gh as gh_module
from .ledger import row_get

RECIPES = os.path.join(config.REPO_ROOT, "recipes")


def design_input_key(project, number, pr, run_id):
    return f"designinput:{project}#{number}:{pr}:{run_id}"


# The in-app What's New contract (DESIGN D31, mahler#358), injected into a
# build prompt only when the issue itself asks for that work (mahler#569).
# Most builds never touch a release feed; keeping the ~170-word block out of
# unrelated prompts saves input tokens without weakening the contract.
WHATS_NEW_GUIDANCE = """12. **In-app What's New contract:** When (and only when) the issue explicitly asks for an
    in-app What's New surface or release feed:
    - Follow DESIGN D31's schema v1 JSON contract (`schema_version`, `project`, `releases`
      with `version`, `checkpoint_sha`, `published_at`, `remote_url`, `sections`, `maintenance`).
    - Keep transport strictly read-only: apps consume the feed; they never publish releases or
      write read/acknowledgement state back to Mahler.
    - Client acknowledgement is local to each app installation, SemVer-based (store the highest
      acknowledged version, show newer releases), and marked read only after the user views or
      dismisses the surface.
    - Missing, unreachable, or malformed feed responses must degrade gracefully and never block
      app startup.
    - Preserve the app's native design conventions (e.g. web, SwiftUI, Compose, CLI); do not force
      foreign UI paradigms.
    - Hide maintenance details initially: render features and fixes prominently; keep maintenance
      collapsed or secondary so operational chores do not clutter user-facing notes.
    - Exclude operational data: the feed provides release metadata only; never consume or display
      issue comments, run logs, credentials, or UAT notes.
    - Add automated tests in the app covering JSON payload parsing, SemVer comparison, offline fallback,
      and local acknowledgement read-state persistence.
    Do not add What's New UI or feed consumption to tasks that do not explicitly request it.
"""

# Explicit What's New / release-feed asks only: "what's new", "whats new",
# "what is new", "release feed", "release notes feed" — but not a plain
# "release", "release notes", or "new release", which most unrelated issues
# mention somewhere.
_WHATS_NEW_RE = re.compile(
    r"what(?:['\u2019]?s|[\s-]*is)[\s-]*new"
    r"|release[\s-]+(?:notes?[\s-]+)?feed",
    re.IGNORECASE)


def needs_whats_new(item):
    """True when the cached issue title or body explicitly asks for an
    in-app What's New surface or release feed (mahler#569). Decided from
    the ledger's cached text, so it never needs a network call."""
    return any(_WHATS_NEW_RE.search(t) for t in
               (row_get(item, "title", ""), row_get(item, "issue_body", ""))
               if t)


def render(recipe, **vars):
    with open(os.path.join(RECIPES, f"{recipe}.md"), encoding="utf-8") as fh:
        return string.Template(fh.read()).safe_substitute(**vars)


def pr_ci_toolchain_handoff(ctx, project, item):
    """The PR's changed CI/toolchain paths, or an explicit lookup fallback."""
    gh = ctx.gh(project)
    try:
        paths = gh.pr_ci_toolchain_files(item["pr"])
    except gh_module.GHError as e:
        ctx.say(f"{project}#{item['number']}: couldn't fetch the PR's changed "
                f"CI/toolchain paths — {e}")
        return ("- changed CI/toolchain files in the PR: unavailable; inspect "
                f"`gh pr diff {item['pr']} -R {gh.repo} --name-only` manually before "
                "editing")
    if not paths:
        return "- changed CI/toolchain files in the PR: none found"
    return ("- changed CI/toolchain files in the PR:\n" +
            "\n".join(f"  - `{path}`" for path in paths))


def ci_handoff(ctx, project, item, branch, tail=150):
    """The fix prompt's CI context (D18, mahler#18): the failing-log tail of
    the latest failed run on the branch — or the commands to fetch it, when
    that lookup fails right now. It also surfaces PR CI/toolchain changes first."""
    gh = ctx.gh(project)
    run_id, log = None, ""
    try:
        run_id, log = gh.failed_run_log(branch, tail)
    except gh_module.GHError as e:
        ctx.say(f"{project}#{item['number']}: couldn't fetch the CI log for the fix "
                f"run — {e}")
    lines = [pr_ci_toolchain_handoff(ctx, project, item),
             f"- the PR (#{item['pr']})'s CI is red, and this branch is the PR's head "
             "branch: push your fixes to it, and each push re-runs CI"]
    if run_id:
        lines.append(f"- run {run_id} is the latest failed one "
                     f"(full log: `gh run view {run_id} -R {gh.repo} --log-failed`)")
        if log:
            lines += ["- its failing-log tail:", "", "```", log, "```"]
    else:
        lines += [f"- find the latest failed run and its failing-log tail:",
                  f"  `gh run list -R {gh.repo} --branch {branch} --status failure "
                  f"--limit 1 --json databaseId`",
                  f"  `gh run view <run-id> -R {gh.repo} --log-failed | tail -{tail}`"]
    return "\n".join(lines)


def handoff_text(base, replayed, kept):
    """What a build is told about the work an earlier run left (D19).

    `replayed` is False when the branch starts clean from base — nothing to
    say. Otherwise `kept` is None when the saved work rebased onto current
    base, or the ref the old tip was parked on when it no longer applies.
    """
    if not replayed:
        return ""
    if kept is None:
        return (f"- earlier work on this item is already in your branch, replayed onto "
                f"current `origin/{base}`. First run the test/verify command: if it passes, "
                f"commit, push, and end with STATUS: DONE immediately. Otherwise run "
                f"`git log --oneline origin/{base}..HEAD` and read the latest "
                f"`mahler:agent handoff` comment on the issue before continuing")
    return (f"- earlier work on this item no longer applies to current "
            f"`origin/{base}`, so your branch starts fresh from it. The old work is "
            f"on `{kept}`: read `git log -p origin/{base}..origin/{kept}` and the "
            f"latest `mahler:agent handoff` comment, then redo what still fits")


def build(ctx, project, item, role, platform, prep, context=None):
    """The full prompt for a prepared run (runner.prepare's return value).

    `context` overrides the default handoff text when the caller already
    knows why this run started — e.g. ship.py starting a fix round from a
    failed review's findings, rather than red CI (D11)."""
    pol = ctx.policy(project)
    base = pol.get("base", "main")
    if role == "design":
        if not item["pr"] or not prep.get("head_sha") or not context:
            raise ValueError("a design run requires a PR, resolved head, and review history")
        return render(
            "design", number=item["number"], title=item["title"], repo=pol["repo"],
            worktree=prep["worktree"], branch=prep["branch"] or "", base=base,
            platform=platform, pr=item["pr"], head=prep["head_sha"],
            handoff=context, rules=(
                "\nProject rules and review context:\n" + pol["rules"].strip() + "\n")
            if pol.get("rules") else "")

    if role == "fix":
        handoff = (ci_handoff(ctx, project, item, prep["branch"]) if context is None
                   else pr_ci_toolchain_handoff(ctx, project, item) + "\n" + context)
    else:
        handoff = (context if context is not None else
                   handoff_text(base, prep["replayed"], prep["kept"]))

    sizing = ""
    if role == "sort" and config.size_target_of(pol) == "s":
        sizing = ("\nSizing for this project (from Mahler's config): its daytime builder takes only "
                  "size:s, and size:m waits for scarce, off-peak capacity. Aim for size:s. If the work "
                  "would be size:m but splits cleanly into sequential, single-responsibility size:s pieces, "
                  "split it as in rule 5, even though it isn't size:l. Each piece is one mergeable PR with "
                  "its own test, chained with `Depends on: #N`. Keep size:m only when a split would leave a "
                  "broken or untested intermediate state, or when it touches security/credential boundaries. "
                  "Such an item waits for a larger builder. When you split a size:l item, prefer size:s "
                  "sub-issues too.\n")

    # mahler#569: only builds whose issue explicitly asks for a What's New
    # surface or release feed carry the contract; the build recipe consumes
    # `$whats_new` (other recipes don't have the placeholder, and ignore it).
    whats_new = WHATS_NEW_GUIDANCE if needs_whats_new(item) else ""

    return render(role, number=item["number"], title=item["title"], repo=pol["repo"],
                  worktree=prep["worktree"], branch=prep["branch"] or "", base=base,
                  platform=platform, pr=row_get(item, "pr", ""),
                  verify=pol.get("verify") or "the project's tests (see CLAUDE.md)",
                  mahler=config.MAHLER_BIN, handoff=handoff, sizing=sizing,
                  whats_new=whats_new,
                  rules=("\nProject rules (from Mahler's config — these override anything else):\n"
                         + pol["rules"].strip() + "\n") if pol.get("rules") else "")
