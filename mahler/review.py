"""Classified review status payloads and durable follow-up intake (D11).

STATUS: REVIEW-PASS/REVIEW-FAIL {"findings": [...]}, on one line. Each finding
has severity (blocking/follow-up), category, location, scenario, consequence,
and done_when (an exact acceptance line for scope/spec blockers). Legacy prose
remains readable; JSON-looking malformed output is never legacy approval.
"""

import hashlib
import json
import re

from . import screenshot_delivery
from .gh import GHError


def parse(verdict, text, issue_body=""):
    text = (text or "").strip()
    if not text:
        raise ValueError("missing review findings")
    if not text.startswith(("{", "[", "```")):
        return None  # Legacy notes/blockers retain their original meaning.
    payload = json.loads(text)
    if not isinstance(payload, dict) or set(payload) != {"findings"}:
        raise ValueError("expected findings object")
    findings = payload["findings"]
    if not isinstance(findings, list):
        raise ValueError("expected findings list")
    section = re.search(r"^## Done when\s*\n(.*?)(?=^## |\Z)", issue_body,
                        re.MULTILINE | re.DOTALL | re.IGNORECASE)
    criteria = {line.strip() for line in section[1].splitlines()} if section else set()
    for f in findings:
        if not isinstance(f, dict) or any(not isinstance(f.get(k), str) or not f[k].strip()
                for k in ("severity", "category", "location", "scenario", "consequence")):
            raise ValueError("incomplete finding")
        if f["severity"] not in {"blocking", "follow-up"}:
            raise ValueError("unknown severity")
        if f["category"] not in {"scope", "spec", "behavior", "security", "data-loss", "hardening", "testing"}:
            raise ValueError("unknown category")
        if f["severity"] == "blocking" and f["category"] in {"scope", "spec"}:
            if (not isinstance(f.get("done_when"), str) or not f["done_when"].strip()
                    or f["done_when"] not in criteria):
                raise ValueError("scope/spec blocker needs an exact Done-when line")
    if (verdict == "fail") != any(f["severity"] == "blocking" for f in findings):
        raise ValueError("verdict contradicts findings")
    return findings


def render(findings):
    return " | ".join(f"{f['location']}: [{f['severity']}/{f['category']}] "
                      f"{f['scenario']} — {f['consequence']}"
                      + (f" (Done when: {f['done_when']})" if f.get("done_when") else "")
                      for f in findings)


def window(led, project, number, pr):
    """PR-scoped evidence survives re-shipping and process restarts."""
    return json.loads(led.get_kv(f"reviewrounds:{project}#{number}:{pr}") or "[]")


def round_number(rounds, sha):
    return next((i for i, r in enumerate(rounds, 1) if r["sha"] == sha), len(rounds) + 1)


def record_green(ctx, project, item, pr, info):
    """Called only with a current-head usable verdict and freshly green CI.

    Alternate reviewers update evidence, never the distinct-head round count.
    Keep all usable verdicts for that head so a retry cannot erase evidence.
    """
    rounds = window(ctx.led, project, item["number"], pr)
    number = round_number(rounds, info["sha"])
    if number > len(rounds):
        rounds.append({"sha": info["sha"], "reviews": []})
    evidence = {k: info.get(k) for k in ("run_id", "verdict", "classified", "findings")}
    if evidence not in rounds[number - 1]["reviews"]:
        rounds[number - 1]["reviews"].append(evidence)
    if not ctx.dry_run:
        ctx.led.set_kv(f"reviewrounds:{project}#{item['number']}:{pr}", json.dumps(rounds))
    return number


def start_context(ctx, project, item, pr, sha):
    rounds = window(ctx.led, project, item["number"], pr)
    number = round_number(rounds, sha)
    policy = ctx.policy(project)
    threshold = policy["review_green_rounds"]
    operating_context = policy["review_context"]
    context = ("Project operating context (review_context):\n" + operating_context + "\n"
               if operating_context else
               "Project operating context: unspecified (review_context is empty); "
               "do not assume the personal-project context.\n")
    return (context + f"Green review round: {number}; review_green_rounds: {threshold} "
            "(inclusive; provisional until this head has a usable verdict and green CI).\n"
            + ("Convergence threshold reached: only new substantiated blockers or still-"
               "reproducible unresolved blockers may fail review.\n" if number >= threshold else "")
            + "Verify earlier fixes first. Compare category, scenario and consequence, not just "
            "file names. Do not revive fixed findings without new evidence. Apply the recipe's "
            "operating-context calibration to data-loss claims. Security, normal-flow data loss, "
            "regressions and unsatisfied acceptance checks remain blockers. Other actionable "
            "findings are follow-ups; explain their nonblocking consequence.\n"
            + "Prior green review evidence (old findings are not proof of a current defect):\n"
            + json.dumps(rounds) + "\n"
            + screenshot_delivery.review_context(ctx, project, pr, sha))


def same_findings(before, current):
    """Conservative identity/evidence comparison for alternate-review routing.

    Location alone cannot establish that two classified defects are the same.
    Text changes are new evidence; uncertainty stays in the normal blocker flow.
    """
    def signatures(findings):
        return {tuple(f.get(k, "") for k in
                      ("category", "location", "scenario", "consequence", "done_when"))
                for f in findings if f["severity"] == "blocking"}
    return bool(signatures(current)) and signatures(current) <= signatures(before)


def remember(e, info, findings):
    """Archive by run before updating the current verdict; retries retain URLs."""
    key = f"reviewresults:{e.project}#{e.number}"
    records = json.loads(e.led.get_kv(key) or "{}")
    rid = str(e.run["id"])
    if rid not in records:
        rows = []
        for f in findings or []:
            marker = hashlib.sha256(json.dumps([e.project, e.number, f], sort_keys=True).encode()).hexdigest()
            rows.append({**f, "marker": f"<!-- mahler:review-follow-up:{marker} -->"})
        records[rid] = {"run_id": e.run["id"], "sha": info.get("sha"),
                        "pr": e.item["pr"], "findings": rows, "text": e.rest,
                        "reason": "Classified as nonblocking: no substantiated merge blocker "
                                  "in this finding; tracked independently to keep review scope bounded."}
        e.led.set_kv(key, json.dumps(records))


def file_followups(ctx, project, item):
    """Retry intake before merge; remote exact markers recover lost local writes."""
    if ctx.dry_run:
        return False
    key = f"reviewresults:{project}#{item['number']}"
    records = json.loads(ctx.led.get_kv(key) or "{}")
    gh = ctx.gh(project)
    pol = ctx.policy(project)
    for record in records.values():
        if record["pr"] != item["pr"]:
            continue
        for f in record["findings"]:
            if f["severity"] != "follow-up" or f.get("linked"):
                continue
            try:
                if not f.get("url"):
                    url = gh.issue_by_marker(f["marker"])
                    if not url:
                        repo = pol["repo"]
                        body = (f"{f['marker']}\n\n## Problem / goal\n\n{render([f])}\n\n"
                                f"Source issue: https://github.com/{repo}/issues/{item['number']}\n"
                                f"Source PR: https://github.com/{repo}/pull/{record['pr']}\n"
                                f"Review run: {record['run_id']}; reviewed head: {record['sha']}\n")
                        labels = ["mahler:inbox", "type:chore", "p2"]
                        if pol.get("scope") == "label":
                            labels.append(pol["scope_label"])
                        url = gh.create_issue(f"Review follow-up: {f['location']} — {f['scenario']}"[:240], body, labels)
                        if not url:
                            raise GHError("follow-up creation returned no URL")
                    f["url"] = url
                    ctx.led.set_kv(key, json.dumps(records))
                gh.comment(record["pr"], f"Review follow-up: {f['url']}\n\n{render([f])}\n\n"
                           + record.get("reason", "Classified as nonblocking by the independent reviewer."))
                f["linked"] = True
                ctx.led.set_kv(key, json.dumps(records))
            except GHError as err:
                ctx.say(f"{project}#{item['number']}: follow-up filing pending — {err}")
                return False
    return True


def effective(binding, pr=None, sha=None, run=None, *, project=None, number=None, required=False):
    """Derive effective review status over PR/head identity, launch binding, run lifecycle, and verdict evidence.

    Returns:
        "pass"     - terminal pass verdict matching identity (PR and SHA)
        "fail"     - terminal fail verdict matching identity (PR and SHA)
        "pending"  - active (running/stopping) review run matching identity, item, and role
        "required" - if required is True and no matching verdict or active run exists
        "unknown"  - if required is False and no matching verdict or active run exists
    """
    if isinstance(binding, str):
        try:
            binding = json.loads(binding)
        except (ValueError, TypeError):
            binding = {}
    if not isinstance(binding, dict):
        binding = {}

    binding_sha = binding.get("sha")
    binding_pr = binding.get("pr")

    # Terminal verdict evidence: pass/fail only for matching identity.
    # Reject terminal verdicts when any supplied current identity component lacks
    # an equal corresponding bound value (missing bound fields do not match as wildcards).
    term_sha_matches = bool(not sha or (binding_sha is not None and binding_sha == sha))
    term_pr_matches = bool(pr is None or (binding_pr is not None and str(binding_pr) == str(pr)))
    verdict = binding.get("verdict")
    if term_sha_matches and term_pr_matches and verdict in ("pass", "fail"):
        return verdict

    # Active pending: only for a matching active (running/stopping) review run.
    sha_matches = bool(not sha or not binding_sha or binding_sha == sha)
    pr_matches = bool(pr is None or binding_pr is None or str(binding_pr) == str(pr))
    identity_matches = sha_matches and pr_matches

    if run is not None and identity_matches:
        run_dict = dict(run) if not isinstance(run, dict) else run
        run_status = run_dict.get("status")
        run_role = run_dict.get("role")
        run_id = run_dict.get("id")
        run_proj = run_dict.get("project")
        run_num = run_dict.get("number")

        role_matches = (run_role == "review")
        status_matches = (run_status in ("running", "stopping"))
        item_matches = ((project is None or run_proj is None or run_proj == project)
                        and (number is None or run_num is None or run_num == number))

        bound_run_id = binding.get("run_id")
        if bound_run_id is not None:
            id_matches = (run_id is not None and str(run_id) == str(bound_run_id))
        else:
            id_matches = bool(verdict == "pending")

        if role_matches and status_matches and item_matches and id_matches:
            return "pending"

    return "required" if required else "unknown"


effective_review = effective


def effective_for_item(led, project, number, pr=None, sha=None, required=False):
    """Convenience helper reading the launch binding and bound run from the ledger."""
    key = f"review:{project}#{number}"
    raw = led.get_kv(key)
    try:
        binding = json.loads(raw or "null") if raw else {}
    except (ValueError, TypeError):
        binding = {}
    if not isinstance(binding, dict):
        binding = {}
    run = None
    rid = binding.get("run_id")
    if rid is not None:
        run = led.run(rid)
    if not run and binding.get("verdict") == "pending":
        active = [r for r in led.active_runs(project)
                  if r["number"] == number and r["role"] == "review"]
        run = active[0] if active else None
    return effective(binding, pr=pr, sha=sha, run=run,
                     project=project, number=number, required=required)

