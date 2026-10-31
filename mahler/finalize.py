"""Every exit is a handoff (DESIGN D9).

A run that ended — cleanly, out of quota, timed out or killed — passes
through here exactly once: its log is read for usage and a STATUS line, its
work is snapshotted to a pushed ref, the issue gets a handoff comment, and
the item moves to whatever state its outcome implies.
"""

import json
import os
import re
import subprocess
from datetime import timedelta

from . import ci_blockers, config, desktop, failures, no_change, platforms, prompt, review, router, runner, waits
from .gh import GHError
from .ledger import CONDUCTOR, iso, parse, row_get
from .usage import quota_peers, record_claude_usage

CAPACITY_STOPS = ("quota", "no_credit", "model_unavailable", "auth_denied")

NO_ATTEMPT = ("quota", "no_credit", "preempted", "closed", "parked", "lost-lease", "silent", "handoff",
              "model_unavailable", "resume_rejected", "auth_denied")

# How long a platform whose login a project's runs can't use stays off that
# project (mahler#1211). A refused login doesn't clear by itself, so this is a
# retry promise, not an estimate.
AUTH_DENIED_HOLD_MINUTES = 6 * 60

MAX_OPTIONS = 3
MAX_OPTION_LEN = 40


def split_needs_you(rest):
    """A NEEDS-YOU line's rest, split on its last ` OPTIONS:` (case-insensitive,
    mahler#248) -> (question, options). Options are the `|`-separated choices
    after it: stripped, blanks dropped, capped at 3, each cut to 40 characters.
    With no `OPTIONS:`, the question is the whole rest and options are empty."""
    if not rest:
        return rest, []
    idx = rest.lower().rfind(" options:")
    if idx == -1:
        return rest, []
    question = rest[:idx].rstrip()
    choices = rest[idx + len(" options:"):].split("|")
    options = [c.strip()[:MAX_OPTION_LEN] for c in choices if c.strip()]
    return question, options[:MAX_OPTIONS]


OWNER_CATEGORIES = ("product", "money", "credentials", "data-policy", "irreversible")

_CATEGORY_TAG = re.compile(r"^\s*\[([A-Za-z-]+)\]\s*")


def parse_needs_you_category(rest):
    """A NEEDS-YOU rest -> (category|None, rest_without_tag). The category is
    the lowercased leading `[tag]`; an unknown tag is still reported (and
    stripped) so the caller can refuse it, and no tag gives None (D52)."""
    if not rest:
        return None, rest
    m = _CATEGORY_TAG.match(rest)
    if not m:
        return None, rest
    return m.group(1).lower(), rest[m.end():]


class Ending:
    """One run's ending, as the outcome handlers below need it.

    Built once by `finalize` after the log has been read, then passed to the
    one handler whose rule matches. `saved`, `keep_worktree` and `closed` are
    filled in by `_save_work` before the dispatch.
    """

    def __init__(self, ctx, run, item, pol, log, kind, verb, rest, reason, outcome):
        self.ctx, self.led, self.run, self.item, self.pol = ctx, ctx.led, run, item, pol
        self.log, self.kind = log, kind
        self.verb, self.rest, self.reason, self.outcome = verb, rest, reason, outcome
        self.project, self.number = run["project"], run["number"]
        self.saved, self.keep_worktree, self.closed = None, False, False
        self.no_work_base = None
        self.stale = False          # lease revoked or replaced: leave the item alone
        self.after_commit = []
        self.verify_green = False
        self.blocked_external = False   # prepared before the write lock
        self.blocked_check = (None, "")
        self.duration_mins = None
        started_at = row_get(run, "started_at")
        if started_at:
            try:
                self.duration_mins = (ctx.led.now() - parse(started_at)).total_seconds() / 60.0
            except Exception:                   # noqa: BLE001 — an unreadable timestamp
                pass                            #   must not cost us the finalize

    def set_state(self, state, why, **cols):
        self.led.set_state(self.project, self.number, state, why, **cols)

    def ping(self, title, message, **kw):
        self.defer(self.ctx.ping, title, message, self.project, self.number, **kw)

    def defer(self, call, *args, **kwargs):
        self.after_commit.append(lambda: call(*args, **kwargs))

    def notify(self):
        for call in self.after_commit:
            call()


# ---------- what each ending means ----------

def _retry(e):
    """No usable outcome: another attempt, or `failed` once they run out."""
    retry_or_fail(e.ctx, e.project, e.number, e.item, e.reason, e.outcome,
                  platform=e.run["platform"], duration_mins=e.duration_mins,
                  explore=bool(row_get(e.run, "explore", 0)), run=e.run, defer=e.defer)
    return True


def _decide_key(project, number):
    return f"decide-yourself:{project}#{number}"


def _needs_you(e):
    """The agent asked a question only the owner can answer (DESIGN D13). A
    sort/build/fix question must declare an owner-only category; without one it
    goes back to the agent once per attempt cycle, to decide itself (D52)."""
    category, rest = parse_needs_you_category(e.rest)
    if e.run["role"] in ("sort", "build", "fix"):
        if category in OWNER_CATEGORIES:
            e.rest = rest
        else:
            question, _ = split_needs_you(rest)
            key = _decide_key(e.project, e.number)
            cycle = e.led.get_kv(f"attempt_cycle:{e.project}#{e.number}") or ""
            try:
                prior = json.loads(e.led.get_kv(key) or "{}")
            except ValueError:
                prior = {}
            if prior.get("cycle") != cycle:
                e.led.set_kv(key, json.dumps({"question": question, "cycle": cycle}))
                e.set_state("ready" if e.item["sorted_at"] else "inbox",
                            "agent asked a non-owner question — deciding it (D52)")
                return True
            e.rest = rest
    else:
        e.rest = rest
    return _park_needs_you(e)


def _park_needs_you(e):
    question, options = split_needs_you(e.rest)
    e.set_state("needs_you", question, question=question, options=json.dumps(options))
    e.ping(f"Mahler needs you — {e.project} #{e.number}", question or e.item["title"],
           priority="high", tags="question", console=True)
    return True


# Deliberately full-match a small request grammar. A GitHub mention alone must
# never swallow an unrelated credential request or a mixed owner decision.
_GITHUB_WRITE_OPERATION = (
    r"(?:push (?:the |my )?branch|"
    r"(?:record|post) (?:parent |issue )evidence|"
    r"(?:comment on|edit|update) (?:the )?(?:parent|issue|parent issue)|"
    r"create (?:the )?(?:linked )?(?:P1 )?follow-up)")
_GITHUB_WRITE_REQUEST = re.compile(
    r"(?:please\s+)?(?:grant|restore|provide|enable|permit|allow|authorize)\s+"
    r"(?:the run['’]s\s+)?(?:authorized\s+)?"
    r"(?:github(?:\s+identity|\s+cli)?\s+(?:write|push|comment|issue[- ](?:write|edit))\s+access|"
    r"github\s+cli\s+access|(?:a\s+)?push\s+path)"
    r"(?:\s+for this run)?"
    r"(?:\s+so (?:I can " + _GITHUB_WRITE_OPERATION +
    r"(?: and " + _GITHUB_WRITE_OPERATION + r")*|git push stops returning 403))?"
    r"[.?!]?", re.IGNORECASE)


def _asks_github_write_access(e):
    if e.verb != "NEEDS-YOU" or e.run["role"] not in ("build", "fix"):
        return False
    category, rest = parse_needs_you_category(e.rest)
    if category not in (None, "credentials"):
        return False
    # Include OPTIONS in the match: unknown alternatives may be owner decisions.
    return bool(_GITHUB_WRITE_REQUEST.fullmatch((rest or "").strip()))


def _ended_write_access_ask(e):
    """Saved work already went through the conductor's push path in _save_work."""
    e.led.set_kv(_decide_key(e.project, e.number), None)
    if e.saved or e.item["branch"]:
        e.set_state("verifying", "GitHub writes belong to the conductor — shipping saved work",
                    summary=e.item["summary"] or e.item["title"])
    else:
        e.set_state("ready", "GitHub writes belong to the conductor — retry with local commits")
    return True


# Match unavailable access phrases, never bare words such as token or protection.
_ACCESS_BLOCKER = re.compile(
    r"\bdeployment[ -]protection\b|"
    r"\b(?:login|log[ -]in|sign[ -]in)\s+(?:is\s+)?required\b|"
    r"\b(?:missing|unavailable|needs?|requires?)\s+(?:a\s+|an\s+)?"
    r"(?:(?:database|api|access|auth|authentication|bypass)\s+)?"
    r"(?:credentials?|tokens?|passwords?)\b|"
    r"\b(?:credentials?|tokens?|passwords?)\s+(?:is\s+|are\s+)?"
    r"(?:missing|unavailable|required)\b|"
    r"\baccess\s+(?:is\s+)?(?:denied|required)\b", re.IGNORECASE)


def _access_blocked(e):
    # Do not copy arbitrary agent text into a new question: it could contain
    # secrets unknown to the normal redactor. The preserved handoff identifies
    # the operation; its existing redaction boundary remains unchanged.
    question = (
        f"Who will provide authorized access for the blocked operation in "
        f"{e.project} #{e.number} (run {e.run['id']}'s handoff), and how will it be "
        "supplied outside GitHub through a named secret file or environment variable "
        "to the runtime that needs it? Share only the provider, channel name and "
        "authorized scope, never secret values.")
    if e.item["state"] == "needs_you" and e.item["question"] == question:
        return True
    e.rest = question
    return _park_needs_you(e)


# ---------- BLOCKED on an external precondition ----------

def _norm(text):
    return " ".join((text or "").lower().split())


def _blocked_dependencies(e):
    return ci_blockers.references(e.led, e.project, e.number, e.rest, e.pol['repo'])


def _ended_blocked_dependency(e):
    deps = ci_blockers.record(e, _blocked_dependencies(e))
    e.set_state("verifying" if e.item["pr"] else "ready", e.rest,
                depends=json.dumps(deps), **_fix_charge_refund(e))
    return True


def _blocked_is_external(e):
    """A build/fix BLOCKED that names a gate outside the code: its reason is
    the stop-gate text the issue itself prescribes, or repeats the previous
    build/fix run's BLOCKED reason on the same base (mahler#1080)."""
    reason = _norm(e.rest)
    if not reason:
        return False
    if reason in _norm(e.item["issue_body"] if e.item is not None else ""):
        return True
    prev = e.led.last_run(e.project, e.number, roles=("build", "fix"),
                          exclude_run_id=e.run["id"])
    return bool(prev and _norm(row_get(prev, "outcome")) == _norm(e.outcome)
                and (row_get(prev, "base_ref") or None) == (row_get(e.run, "base_ref") or None))


_PR_REF = re.compile(r"(?:\bPRs?\s*)?#(\d+)")
_BRANCH_REF = re.compile(r"\bbranch\s+[`'\"]?([\w][\w./-]*)", re.IGNORECASE)
_TAG_REF = re.compile(r"\btag\s+[`'\"]?([\w][\w./-]*)", re.IGNORECASE)
_RELEASE_WORD = re.compile(r"\b(?:release|beta)\b", re.IGNORECASE)


def _check_blocked_condition(ctx, project, number, reason):
    """-> (met, detail). met is None when the reason names nothing checkable,
    True/False when every named condition holds / one does not (or the check
    could not be made)."""
    reason = reason or ""
    prs = [int(n) for n in _PR_REF.findall(reason) if int(n) != number]
    branches = [b for b in _BRANCH_REF.findall(reason) if b.lower() not in ("exists", "must")]
    tags = [t for t in _TAG_REF.findall(reason)
            if t.lower() not in ("exists", "must", "is", "has", "needs", "should")]
    # A tag name can itself contain "release" or "beta". Only prose outside
    # the named tag can request a separate GitHub Release.
    want_release = bool(_RELEASE_WORD.search(_TAG_REF.sub("", reason)))
    if not (prs or branches or tags or want_release):
        return None, "no checkable condition"
    gh = ctx.gh(project)
    try:
        merged_at = None
        for pr in prs:
            view = gh.pr_view(pr)
            if view.get("state") != "MERGED":
                return False, f"PR #{pr} is not merged"
            merged_at = max(merged_at or "", view.get("mergedAt") or "") or None
        for branch in branches:
            gh.branch_sha(branch)
        for tag in tags:
            if not gh.get_tag_sha(tag):
                return False, f"tag {tag} does not exist yet"
        if want_release:
            rel = gh.latest_release()
            if not rel:
                return False, "no release or tag exists yet"
            if merged_at and (rel.get("publishedAt") or "") <= merged_at:
                return False, "no release newer than the merged PR yet"
    except (GHError, ValueError, KeyError) as exc:
        return False, f"condition not confirmable: {exc}"
    return True, "named condition is met"


def _ended_blocked_external(e):
    """Park without charging an attempt or escalating tiers. A condition that
    is already met means the block is stale: back to ready."""
    met, detail = e.blocked_check
    key = f"blocked:{e.project}#{e.number}"
    if met:
        e.led.set_kv(key, None)
        e.set_state("ready", f"blocked condition met ({detail})")
        return True
    previous = json.loads(e.led.get_kv(key) or "{}")
    record = {"reason": e.rest, "since": previous.get("since") or iso(e.led.now()),
              "checkable": met is not None}
    e.led.set_kv(key, json.dumps(record))
    e.set_state("parked", f"blocked on an external precondition: {e.rest}")
    if previous.get("reason") != e.rest:
        e.ping(f"Blocked — {e.project} #{e.number}",
               f"{e.rest} (waiting; /mahler go resumes)", priority="low")
    return True


# ---------- BLOCKED on the desktop gate (mahler#1172) ----------

# A GUI step the task cannot finish without (a smoke run) found the gate closed.
# Only the gate's own denial marker counts, so a GUI child that itself exited 75
# (or prose about a locked screen) keeps the ordinary BLOCKED handling. Waiting
# says nothing about the work, so it parks without an attempt until the gate
# would open again.
_GUI_DEFERRED = re.compile(rf"\b{desktop.DENIED_MARKER}\b")


def _fix_charge_refund(e):
    """Counters to restore when a PR fix run (charged at launch by ship.py)
    never got to work; {} when nothing was charged or it was already moved on."""
    counters = ("attempts", "esc_tier", "esc_fails")
    charge = json.loads(e.led.get_kv(f"fix-charge:{e.run['id']}") or "{}")
    before, after = charge.get("before") or {}, charge.get("after") or {}
    if (e.run["role"] != "fix" or set(before) != set(counters) or set(after) != set(counters)
            or any(e.item[k] != after[k] for k in counters)):
        return {}
    e.led.set_kv(f"fix-charge:{e.run['id']}", None)
    e.led.set_kv(f"{charge['cycle']}:charged", None)   # the relaunch charges once
    return before


def _ended_gui_deferred(e):
    key = f"blocked:{e.project}#{e.number}"
    previous = json.loads(e.led.get_kv(key) or "{}")
    # A PR's fix run resumes its verify cycle (ship.py), not a fresh build.
    resume = "verifying" if e.run["role"] == "fix" and e.item["pr"] else "ready"
    record = {"reason": e.rest, "since": previous.get("since") or iso(e.led.now()),
              "checkable": False, "gui": True, "resume": resume}
    e.led.set_kv(key, json.dumps(record))
    e.set_state("parked", f"GUI step deferred until the desktop gate opens: {e.rest}",
                **_fix_charge_refund(e))
    if not previous.get("gui"):
        e.ping(f"GUI run deferred — {e.project} #{e.number}",
               f"{e.rest} (retries when the mini is idle, unlocked and inside gui_window; "
               "/mahler go resumes now)", priority="low")
    return True


def _gui_gate_open(ctx, project):
    """-> (open, detail): the D40 gate as a --scheduled run would see it now."""
    try:
        pol = ctx.policy(project)
        window = config.parse_gui_window(pol["gui_window"])
        return True, desktop.run_check(pol["gui_idle_minutes"], window, scheduled=True)
    except desktop.Deferred as exc:
        return False, str(exc)
    except (KeyError, ValueError) as exc:
        return False, f"gate settings unreadable: {exc}"


def recheck_blocked(ctx, projects):
    """Tick pass: move parked items back to ready once the checkable
    condition in their BLOCKED reason is met, or, for a deferred GUI step,
    once the desktop gate would open."""
    if ctx.dry_run:
        return
    led = ctx.led
    for pol in projects:
        project = pol["name"]
        for item in led.items(project, ["parked"]):
            n = item["number"]
            key = f"blocked:{project}#{n}"
            try:
                record = json.loads(led.get_kv(key) or "null")
                if record and record.get("gui"):
                    met, detail = _gui_gate_open(ctx, project)
                elif not record or not record.get("checkable"):
                    continue
                else:
                    met, detail = _check_blocked_condition(ctx, project, n, record["reason"])
                if met:
                    with led._tx():
                        cur = led.item(project, n)
                        if cur["state"] == "parked" and led.get_kv(key):
                            led.set_kv(key, None)
                            back = (record.get("resume") or "ready") if cur["pr"] else "ready"
                            led.set_state(project, n, back,
                                          f"blocked condition met ({detail})")
            except Exception as exc:            # noqa: BLE001 — never break the tick
                ctx.say(f"{project}#{n}: blocked recheck failed: {type(exc).__name__}")


def _sorted_ready(e):
    e.set_state("ready", "sorted", sorted_at=iso(e.led.now()))
    return True


def _sorted_split(e):
    e.set_state("parent", "split into sub-issues")
    # Children fetched during planning need reconsidering before scheduling,
    # even when their GitHub content has not changed (D21, mahler#236).
    e.led.set_kv(f"etag:{e.project}", None)
    return True


SORT_OUTCOMES = {"READY": _sorted_ready, "SPLIT": _sorted_split, "NEEDS-YOU": _needs_you}


# ---------- plan-only design outcomes ----------

def _design_record_key(project, number, pr, sha):
    return f"design:{project}#{number}:{pr}:{sha}"


def _design_payload(rest):
    """Validate the finite plan or followups emitted by recipes/design.md."""
    try:
        data = json.loads(rest)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("head"), str):
        return None
    disposition = data.get("disposition")
    if disposition not in ("fix", "followups"):
        return None
        
    if disposition == "fix":
        if set(data) != {"head", "disposition", "plan"}:
            return None
        plan = data["plan"]
        if not isinstance(plan, dict) or set(plan) != {"summary", "files", "steps", "tests"}:
            return None
        if (not isinstance(plan["summary"], str) or not plan["summary"].strip()
                or not isinstance(plan["tests"], str) or not plan["tests"].strip()):
            return None
        for key in ("files", "steps"):
            values = plan[key]
            if (not isinstance(values, list) or not values or len(values) > 40
                    or any(not isinstance(value, str) or not value.strip() for value in values)):
                return None
        return data

    if disposition == "followups":
        if set(data) != {"head", "disposition", "justification", "findings"}:
            return None
        justification = data["justification"]
        if not isinstance(justification, str) or not justification.strip():
            return None
        findings = data["findings"]
        if not isinstance(findings, list) or not findings:
            return None
            
        text_to_check = justification.lower()
        for finding in findings:
            if not isinstance(finding, dict) or set(finding) != {"severity", "category", "location", "scenario", "consequence"}:
                return None
            if any(not isinstance(v, str) or not v.strip() for v in finding.values()):
                return None
            text_to_check += " " + " ".join(str(v).lower() for v in finding.values())
        normalized = re.sub(r"[\s_-]+", " ", text_to_check)
        if (re.search(r'\bsecurity\b', normalized)
                or re.search(r'\bdone when\b', normalized)
                or re.search(r'\bdata loss\b', normalized)
                or re.search(r'\b(credentials?|secrets?|passwords?|api keys?|tokens?)\b', normalized)):
            return None
        return data
        
    return None


def _prepare_design(e):
    """Accept only a result for the checkout and the current live PR head."""
    try:
        data = _design_payload(e.rest)
        if data is None or not e.item["pr"]:
            return None
        input_key = prompt.design_input_key(e.project, e.number, e.item["pr"], e.run["id"])
        source = json.loads(e.led.get_kv(input_key) or "{}")
        expected = source.get("head")
        evidence = source.get("evidence")
        checkout_head = runner.git(e.run["worktree"], "rev-parse", "HEAD")
        # Design is read-only: any uncommitted change rejects the result.
        if runner.git(e.run["worktree"], "status", "--porcelain").strip():
            return None
        view = e.ctx.gh(e.project).pr_view(e.item["pr"])
        current = view.get("headRefOid")
        if not isinstance(evidence, str) or not evidence.strip():
            return None
        if (view.get("state") != "OPEN" or not isinstance(expected, str)
                or data["head"] != expected or checkout_head != expected or current != expected):
            return None
        return {"data": data, "head": expected, "evidence": evidence}
    except (GHError, runner.GitError, OSError, TypeError, ValueError, AttributeError):
        return None


def _design_result(e, prepared):
    if prepared is None:
        e.set_state("verifying", "design run produced no usable current-head result — retrying")
        return True
    data = prepared["data"]
    result = {
        "pr": e.item["pr"], "head": prepared["head"], "run_id": e.run["id"],
        "source_review_evidence": prepared["evidence"],
        "disposition": data["disposition"],
    }
    if data["disposition"] == "fix":
        result["plan"] = data["plan"]
    elif data["disposition"] == "followups":
        result["justification"] = data["justification"]
        result["findings"] = data["findings"]
        
    e.led.set_kv(_design_record_key(e.project, e.number, e.item["pr"], prepared["head"]),
                 json.dumps(result, sort_keys=True))
    e.set_state("verifying", "design result recorded — conductor can consume it")
    return True


# ---------- review outcomes (DESIGN D11) ----------
#
# A review run never touches the worktree (recipes/review.md is read-only),
# so it skips _save_work/_dispatch entirely, the same way sort does — there
# is nothing to snapshot and no handoff comment to post. Its own comment goes
# straight to the PR (D18: the conductor posts it, in code, so a run that
# times out mid-review still leaves its verdict where the human can see it).
# The item's ledger state stays "verifying" throughout: ship.py's merge gate
# reads the kv verdict below to decide whether to merge or start a fix round.

def _review_kv_key(project, number):
    return f"review:{project}#{number}"


def _update_review_kv(e, **fields):
    key = _review_kv_key(e.project, e.number)
    cur = json.loads(e.led.get_kv(key) or "{}")
    cur.update(fields)
    e.led.set_kv(key, json.dumps(cur))
    if fields.get("verdict") in {"pass", "fail"}:
        e.led.event("review_verdict", e.project, e.number, {
            "verdict": fields["verdict"], "review_run": e.run["id"],
            "reviewed_sha": cur.get("sha")})


def _post_review_comment(e, passed, findings=""):
    pr = e.item["pr"]
    if not pr:
        return
    if passed:
        # Non-blocking notes ride along on a pass (recipes/review.md rule 4).
        notes = [n.strip() for n in (findings or "").split("|") if n.strip()]
        if notes and notes != ["no findings"]:
            lines = [f"**Review** — {e.run['platform']} found no blocking issues. "
                     "Notes (not required for merge):", ""]
            body = "\n".join(lines + [f"- {n}" for n in notes])
        else:
            body = f"**Review** — {e.run['platform']} found no blocking issues."
    else:
        lines = [f"**Review** — {e.run['platform']} found blocking issues; "
                 "the conductor will assess the next fix round:", ""]
        lines += [f"- {f.strip()}" for f in (findings or "").split("|") if f.strip()]
        body = "\n".join(lines)
    try:
        e.ctx.gh(e.project).comment(pr, body)
    except GHError as err:
        e.ctx.say(f"{e.project}#{e.number}: couldn't post the review comment — {err}")


def _prepare_review(e, verdict):
    """Fetch acceptance criteria and parse the verdict before taking the lock."""
    try:
        body = e.ctx.gh(e.project).issue_body(e.number) if (e.rest or "").lstrip().startswith("{") else ""
        return True, review.parse(verdict, e.rest, body)
    except (ValueError, GHError):
        return False, None


def _classified_review(e, verdict, prepared, defer):
    info = json.loads(e.led.get_kv(_review_kv_key(e.project, e.number)) or "{}")
    if (info.get("run_id") not in (None, e.run["id"])
            or info.get("pr", e.item["pr"]) != e.item["pr"]):
        return False  # A late finalization must not overwrite a newer review.
    valid, findings = prepared if prepared is not None else _prepare_review(e, verdict)
    if not valid:
        _review_inconclusive(e, count=False)   # parse-rejected: not the reviewer's failure
        return False
    review.remember(e, info, findings)
    _update_review_kv(e, run_id=e.run["id"], classified=findings)
    if findings is not None:
        defer(_post_review_comment, e, passed=verdict == "pass", findings=review.render(findings))
        e.review_blockers = review.render([f for f in findings if f["severity"] == "blocking"])
    return True


def _review_passed(e, prepared=None, defer=None):
    defer = defer or (lambda call, *args, **kwargs: call(*args, **kwargs))
    if not _classified_review(e, "pass", prepared, defer):
        return True
    if json.loads(e.led.get_kv(_review_kv_key(e.project, e.number)) or "{}").get("classified") is None:
        defer(_post_review_comment, e, passed=True, findings=e.rest)
    _update_review_kv(e, verdict="pass")
    history = json.loads(e.led.get_kv(f"reviewfindings:{e.project}#{e.number}") or "[]")
    e.led.set_kv(f"reviewconvergence:{e.project}#{e.number}", str(len(history)))
    e.set_state("verifying", "review passed — the conductor ships it")
    return True


def _review_failed(e, prepared=None, defer=None):
    defer = defer or (lambda call, *args, **kwargs: call(*args, **kwargs))
    if not _classified_review(e, "fail", prepared, defer):
        return True
    blockers = getattr(e, "review_blockers", e.rest)
    key = f"reviewfindings:{e.project}#{e.number}"
    history = json.loads(e.led.get_kv(key) or "[]")
    # Finalization can be retried after a partial tick. A run is one review
    # round even if its outcome handler executes more than once.
    if not any(round_["run_id"] == e.run["id"] for round_ in history):
        review = json.loads(e.led.get_kv(_review_kv_key(e.project, e.number)) or "{}")
        history.append({"sha": review.get("sha"), "findings": blockers or "",
                        "at": iso(e.led.now()), "run_id": e.run["id"],
                        "platform": e.run["platform"]})
        if review.get("classified") is not None:
            history[-1]["classified"] = review["classified"]
        history[-1]["pr"] = e.item["pr"]
        e.led.set_kv(key, json.dumps(history))
    if json.loads(e.led.get_kv(_review_kv_key(e.project, e.number)) or "{}").get("classified") is None:
        defer(_post_review_comment, e, passed=False, findings=e.rest)
    _update_review_kv(e, verdict="fail", findings=blockers or "")
    e.set_state("verifying", "review found blocking issues — the conductor starts a fix")
    return True


REVIEW_INCONCLUSIVE_CAP = 3


def _inconclusive_wait(e, reason, sha):
    """Reviews keep ending without a verdict: wait (D52), never an owner ping. The
    wake resets the count and prefers a reviewer outside the inconclusive set."""
    waits.hold(e.led, e.project, e.number, "review_inconclusive", reason,
               resume="verifying", wake={"sha": sha})


def _review_inconclusive(e, count=True):
    """No usable verdict (crash, timeout, quota, an unmatched STATUS verb):
    clear any stale kv record so ship.py's gate starts a fresh review run
    next tick, instead of either merging on a verdict never reached or
    waiting forever on one that will never arrive."""
    # DESIGN D18: a missing STATUS line is never turned into a verdict by
    # parsing findings; the reviewer did not commit to pass/fail.
    info = json.loads(e.led.get_kv(_review_kv_key(e.project, e.number)) or "{}")
    if info.get("run_id") not in (None, e.run["id"]):
        return True
    # Finalization can replay after the review record is cleared; a run that
    # already hit the cap keeps its question instead of reverting to verifying.
    capped_key = f"reviewinconclusive_capped:{e.project}#{e.number}"
    try:
        capped = json.loads(e.led.get_kv(capped_key) or "{}")
    except ValueError:
        capped = {}
    if capped.get("run_id") == e.run["id"]:
        _inconclusive_wait(e, capped["question"], capped.get("sha"))
        return True
    sha = info.get("sha")
    seen = []
    if sha:
        key = f"reviewinconclusive:{e.project}#{e.number}:{sha}"
        try:
            seen = json.loads(e.led.get_kv(key) or "[]")
        except ValueError:
            seen = []
        if e.run["id"] not in [r.get("run_id") for r in seen]:
            if count and getattr(e, "reason", None) not in (*CAPACITY_STOPS, "silent"):
                seen.append({"run_id": e.run["id"], "platform": e.run["platform"]})
                e.led.set_kv(key, json.dumps(seen))
    e.led.set_kv(_review_kv_key(e.project, e.number), None)
    if not count or getattr(e, "reason", None) in (*CAPACITY_STOPS, "silent"):
        # Launch/credit/quota failures and parse-rejected verdicts say nothing about
        # the PR head; they must not push it toward the cap.
        e.set_state("verifying", f"review run ended without a verdict ({e.outcome}) — retrying")
        return True
    if len(seen) == REVIEW_INCONCLUSIVE_CAP and sha:
        # Try a stronger reviewer once before parking (ship._review_preference).
        e.led.set_kv(f"reviewescalate:{e.project}#{e.number}:{sha}", "1")
        e.set_state("verifying", "reviews ended without a verdict — retrying with a stronger reviewer")
        return True
    if len(seen) > REVIEW_INCONCLUSIVE_CAP or (len(seen) >= REVIEW_INCONCLUSIVE_CAP and not sha):
        names = ", ".join(sorted({r["platform"] for r in seen if r.get("platform")}))
        reason = (f"{len(seen)} reviews of this PR head ended without a verdict "
                  f"(platforms: {names}); retrying later with another reviewer")
        e.led.set_kv(capped_key, json.dumps({"run_id": e.run["id"], "question": reason,
                                             "sha": sha}))
        _inconclusive_wait(e, reason, sha)
        return True
    e.set_state("verifying", f"review run ended without a verdict ({e.outcome}) — retrying")
    return True


REVIEW_OUTCOMES = {"REVIEW-PASS": _review_passed, "REVIEW-FAIL": _review_failed,
                   "NEEDS-YOU": _needs_you}


def _no_work_key(e):
    return f"no_work_done:{e.project}#{e.number}"


def _repeated_no_work(e):
    if e.run["role"] != "build" or not e.no_work_base:
        return False
    key = _no_work_key(e)
    signature = [e.no_work_base, " ".join((e.rest or "").split())]
    record = json.loads(e.led.get_kv(key) or "{}")
    if record.get("signature") != signature:
        record = {"signature": signature, "runs": [], "surfaced": False}
    if e.run["id"] not in record["runs"]:
        record["runs"].append(e.run["id"])
    repeated = len(record["runs"]) >= 2
    if repeated:
        reason = ("Repeated no-work DONE: two distinct builds left clean worktrees "
                  "with zero commits ahead of the same base and the same explanation. "
                  "Automatic retries paused pending independent verification.")
        e.set_state("parked", reason)
        if not record["surfaced"]:
            e.defer(_surface_no_work, e, key, record, reason)
    e.led.set_kv(key, json.dumps(record))
    return repeated


def _surface_no_work(e, key, record, reason):
    """A failed delivery leaves finalization pending; retry only unsent steps."""
    if not record.get("commented"):
        gh = e.ctx.gh(e.project)
        marker = f"<!-- mahler:no-work-done run={record['runs'][1]} -->"
        # A failed response may still have submitted the comment. Recover its
        # receipt before posting again, including after a daemon restart.
        if not any(marker in comment["body"] for comment in gh.issue_comments(e.number)):
            gh.comment(e.number,
                       "<!-- mahler:agent -->\n" + marker
                       + "\n**Repeated no-work DONE — parked**\n\n"
                       + reason + f"\n\nBase: `{record['signature'][0]}`. "
                       + f"Runs: {', '.join(map(str, record['runs']))}."
                       + f"\n\nExplanation: {record['signature'][1]}")
        record["commented"] = True
        e.led.set_kv(key, json.dumps(record))
    e.ctx.ping(f"No-work builds paused — {e.project} #{e.number}", reason,
               e.project, e.number, priority="low")
    record["surfaced"] = True
    e.led.set_kv(key, json.dumps(record))


def _parent_evidence_recorded(e):
    """A parent-verify build's DONE must carry the evidence block (mahler#1267).

    The conductor, not the run, writes to GitHub: record the parsed block for
    `sync.post_parent_evidence`, once per run. -> False (with the outcome set)
    when the block is missing or malformed, so the item retries instead of
    closing silently. Other items pass through untouched.
    """
    from . import sync
    if row_get(e.run, "role") != "build" or e.item is None:
        return True
    parent = sync.parent_verify_parent(e.item)
    if parent is None:
        return True
    try:
        entries = sync.parse_parent_evidence(e.log.get("final") or e.log.get("last_text") or "")
    except ValueError as exc:
        e.outcome = f"parent-verify evidence for #{parent} unusable: {exc}"
        return False
    key = sync.evidence_key(e.project, e.number, e.run["id"])
    if not e.led.get_kv(key):
        e.led.set_kv(key, json.dumps({"parent": parent, "verify": e.number, "run": e.run["id"],
                                      "entries": entries, "done": False}))
    return True


def _ended_done(e):
    """D18: the run's own job is finished. The conductor (code, not another
    agent) ships it from here: push, PR, CI, merge. A DONE build is a success,
    not a failed attempt — no attempt is counted, and the item leaves the ready
    queue into `verifying`."""
    e.led.set_kv(_decide_key(e.project, e.number), None)
    if e.run["role"] == "fix":
        # A fix's DONE goes back to verifying on the same PR: the new SHA
        # re-triggers CI. The summary stays the build's.
        e.set_state("verifying", "fix pushed — CI re-runs on the new SHA")
        e.ping(f"Fix pushed — {e.project} #{e.number}",
               f"{e.run['platform']} ended DONE; CI re-runs on the PR", priority="low")
        return True
    if not _parent_evidence_recorded(e):
        return _retry(e)                        # parent-verify DONE without usable evidence
    if _repeated_no_work(e):
        return True
    stale_closed_pr = False
    if e.no_work_base and e.item["pr"]:
        try:
            view = e.ctx.gh(e.project).pr_view(e.item["pr"])
            stale_closed_pr = (view.get("state") == "CLOSED" and not view.get("mergedAt"))
        except (GHError, ValueError) as exc:
            e.ctx.say(f"{e.project}#{e.number}: couldn't confirm whether PR "
                      f"#{e.item['pr']} is closed — preserving the regular ship path: {exc}")
    if e.no_work_base and (not e.item["pr"] or stale_closed_pr):
        if stale_closed_pr:
            e.led.upsert_item(e.project, e.number, pr=None, branch=None)
            e.led.set_kv(f"ship_snapshot:{e.project}#{e.number}", None)
        no_change.begin(e)
        return True
    if not (e.saved or e.item["branch"]):
        return _retry(e)                        # DONE, but nothing to ship
    e.set_state("verifying", "build done — the conductor ships it",
                summary=e.rest or e.item["title"])
    e.ping(f"Build finished — {e.project} #{e.number}",
           f"{e.run['platform']} ended DONE; the conductor opens the PR next",
           priority="low")
    return True


def _ended_parked(e):
    e.set_state("parked", "parked while running")
    return True


def _ended_preempted(e):
    cur = e.led.lease(e.project, e.number)
    if cur and cur["kind"] == "interactive":
        e.set_state("working", "handed to your session")
    else:
        e.set_state("ready", "handoff (preempted)")
    e.ping(f"Handoff to you — {e.project} #{e.number}",
           f"{e.run['platform']} stepped aside; its work is on "
           f"{e.saved['ref'] if e.saved else 'nothing new to save'}", priority="low")
    return True


def _ended_out_of_reach(e):
    """Quota or a lost lease: the item goes back in the queue for whichever
    platform can afford it next (D9)."""
    state = ("verifying" if e.reason in CAPACITY_STOPS
             and e.run["role"] == "fix" and e.item["pr"] else "ready")
    e.set_state(state, f"handoff ({e.reason})")
    # Credit exhaustion is deliberately quiet until it lasts a day. The
    # platform-level alert is emitted by _record_credit_failure, not once per
    # item that happened to be leased when the outage was discovered.
    if e.reason not in ("no_credit", "model_unavailable", "auth_denied"):
        e.ping(f"Handoff — {e.project} #{e.number}",
               f"{e.run['platform']} stopped ({e.reason}); next platform picks it up",
               priority="low")
    return True


def _ended_unconfirmed(e):
    """D18 fallback: no STATUS line (or a timeout after DONE), but the branch
    may still be done — both when the run exited cleanly without STATUS and
    when it timed out with green tests on committed or uncommitted work
    (mahler#145). A Cline run gets one resume before we give up (mahler#17).
    -> False when that nudge restarted it, so finalize leaves it alone."""
    if e.verify_green:
        _apply_verify_fallback(e)
        return True                             # handled — state set to verifying
    if e.reason is None and _try_cline_nudge(e.ctx, e.run, e.kind, e.log, e.pol):
        return False                            # alive again — finalized when it ends
    if e.ctx.led.get_kv(f"verify_failed:{e.run['id']}"):
        e.reason = "verify_failed"
    return _retry(e)


# Tried in order: the first rule that matches decides the ending. The order is
# the contract — a NEEDS-YOU question outranks the reason the run stopped, and
# a confirmed yield outranks watchdog stop reasons. The verify-green fallback
# is the last word before an attempt is counted.
ENDINGS = (
    (_asks_github_write_access, _ended_write_access_ask),
    (lambda e: e.verb == "NEEDS-YOU", _needs_you),
    (lambda e: e.verb == "DONE" and e.reason in (None, "quota"), _ended_done),
    (lambda e: e.verb == "YIELDED", _ended_preempted),
    (lambda e: e.reason == "parked", _ended_parked),
    (lambda e: e.reason == "preempted", _ended_preempted),
    (lambda e: e.reason in (*CAPACITY_STOPS, "lost-lease", "handoff"), _ended_out_of_reach),
    (lambda e: e.verb == "BLOCKED" and e.run["role"] in ("build", "fix")
     and _GUI_DEFERRED.search(e.rest or ""), _ended_gui_deferred),
    (lambda e: e.verb == "BLOCKED" and e.run["role"] in ("build", "fix")
     and _ACCESS_BLOCKER.search(e.rest or ""), _access_blocked),
    (lambda e: e.verb == "BLOCKED" and e.run["role"] in ("build", "fix")
     and _blocked_dependencies(e), _ended_blocked_dependency),
    (lambda e: e.verb == "BLOCKED" and e.run["role"] in ("build", "fix")
     and e.blocked_external, _ended_blocked_external),
    (lambda e: e.verb == "BLOCKED", _retry),
    (lambda e: (e.verb is None or e.verb == "DONE") and e.reason in (None, "timeout"),
     _ended_unconfirmed),
)


def _dispatch(e):
    """-> False when the run is alive again and must not be closed out."""
    for matches, handle in ENDINGS:
        if matches(e):
            return handle(e)
    return _retry(e)


# ---------- finalize ----------

def _credit_state(led, platform):
    try:
        state = json.loads(led.get_kv(f"credit_state:{platform}") or "{}")
    except (TypeError, ValueError):
        state = {}
    return state if isinstance(state, dict) else {}


def _backoff_until(ctx, run, log):
    """When a quota error named its own reset time, honour it (mahler#124);
    otherwise wait the platform's flat backoff. None when quota wasn't hit."""
    if not log["quota_hit"]:
        return None
    pconf = ctx.cfg["platforms"][run["platform"]]
    if log.get("credit_exhausted"):
        # The first retry is in one hour, then 2h, 4h ... capped at a day.
        # A reset time named by the provider wins, as it does for daily caps.
        state = _credit_state(ctx.led, run["platform"])
        failures = int(state.get("failures") or 0) + 1
        minutes = log["retry_after"] if log["retry_after"] is not None else min(24 * 60, 60 * (2 ** (failures - 1)))
    else:
        minutes = log["retry_after"] if log["retry_after"] is not None \
            else pconf.get("backoff_minutes", 60)
    return iso(ctx.led.now() + timedelta(minutes=minutes))


def _record_credit_failure(ctx, run, log):
    """Persist the platform's credit outage without spending item attempts."""
    led, platform, now = ctx.led, run["platform"], ctx.led.now()
    state = _credit_state(led, platform)
    if not state.get("since"):
        state["since"] = iso(now)
    state["failures"] = int(state.get("failures") or 0) + 1
    raw = (log.get("last_error") or log.get("last_text") or "").strip()
    match = re.search(r"balance(?:\s+is)?\s+(\$-\d+(?:\.\d+)?)", raw, re.IGNORECASE)
    state["balance"] = match.group(1) if match else raw[-240:]
    state.setdefault("alerted", False)
    since = parse(state["since"])
    if since and not state["alerted"] and now - since >= timedelta(hours=24):
        detail = (f"{platform} has been out of credits since {state['since']} "
                  f"(balance {state['balance'] or 'unavailable'}); still retrying every 24h")
        led.event("credit_exhausted", detail=detail)
        ctx.ping(f"Mahler: {platform} is out of credits", detail, priority="high", tags="warning")
        state["alerted"] = True
    led.set_kv(f"credit_state:{platform}", json.dumps(state))


def _record_credit_recovery(ctx, platform):
    """Clear an outage after a real successful startup and notify once."""
    led = ctx.led
    state = _credit_state(led, platform)
    if not state:
        return
    led.clear_usage(platform, [router.HOLD])
    led.set_kv(f"hold_reason:{platform}", None)
    led.set_kv(f"credit_state:{platform}", "{}")
    led.event("credit_recovered", detail=f"{platform} recovered from an out-of-credit outage")
    if state.get("alerted"):
        ctx.ping(f"Mahler: {platform} credits recovered",
                 f"{platform} passed startup and is back in rotation.", priority="high", tags="white_check_mark")


def _record_run_usage(ctx, run, kind, log):
    """Everything the run's own log says about what it spent."""
    led, until = ctx.led, _backoff_until(ctx, run, log)
    if kind == "claude":
        # one reading covers every platform sharing that Claude login (D21, D25)
        record_claude_usage(ctx, log["usage"], backoff_until=until, platform=run["platform"])
        if log.get("credit_exhausted"):
            led.record_usage(run["platform"], router.HOLD, 100.0, until)
            led.set_kv(f"hold_reason:{run['platform']}", "no_credit")
        return
    for w, pct, resets in log["usage"]:
        led.record_usage(run["platform"], w, pct, resets)
    if log.get("credit_exhausted"):
        until = _backoff_until(ctx, run, log)
        led.record_usage(run["platform"], router.HOLD, 100.0, until)
        led.set_kv(f"hold_reason:{run['platform']}", "no_credit")
    if until:
        # Variants share the login's quota, even when it has no usage probe.
        for peer in quota_peers(ctx.cfg, run["platform"]):
            for w in ctx.cfg["platforms"][peer].get("windows", router.WINDOWS):
                led.record_usage(peer, w, 100.0, until)


def _save_work(e):
    """Whatever the run left becomes a pushed ref and a handoff comment (D9) —
    unless its own merge already closed the issue, in which case it is done.

    A stale run still snapshots and reports its work before cleanup, but cannot
    update the item whose lease epoch superseded it.
    """
    ctx = e.ctx
    if not e.stale:
        try:
            e.closed = ctx.gh(e.project).issue_state(e.number) == "CLOSED"
        except GHError:
            pass
        _mark_stale(e)
        if e.closed and not e.stale:
            return
    if not e.run["worktree"]:
        return
    try:
        e.saved = runner.snapshot(e.pol["path"], e.run["worktree"], e.run["id"], e.number,
                                  e.pol.get("base", "main"),
                                  env=config.run_env(ctx.cfg, config.gh_account_of(e.pol)))
    except runner.GitError as err:
        e.keep_worktree = True
        ctx.say(f"{e.project}#{e.number}: snapshot failed, keeping worktree — {err}")
    if (not e.saved and not e.keep_worktree and not e.stale
            and e.run["role"] == "build" and e.verb == "DONE"
            and e.reason in (None, "quota")):
        try:
            e.no_work_base = runner.no_work_base(
                e.run["worktree"], e.pol.get("base", "main"),
                env=config.run_env(ctx.cfg, config.gh_account_of(e.pol)))
            # An unsaved dirty tree must survive cleanup even if snapshot
            # returned None (for example, a base moved during the probe).
            if e.no_work_base is None:
                e.keep_worktree = True
        except runner.GitError as err:
            e.keep_worktree = True
            ctx.say(f"{e.project}#{e.number}: no-work evidence unknown — {err}")
    _handoff_comment(ctx, e.run, e.item, e.reason, e.outcome, e.saved, e.log, e.keep_worktree)


def _close_the_books(e, code):
    """The lease, the run row and the worktree, once the ending is decided."""
    ctx, led, run = e.ctx, e.led, e.run
    # Keep item ownership and fencing through the handoff, but release run
    # capacity immediately. Watching a PR does not execute a run (D19, D24).
    # On transport failure the old lease is left to expire.
    with led._tx():
        _mark_stale(e)
        if e.stale:
            pass                        # the current holder's lease is not ours to move or release
        elif led.item(e.project, e.number)["state"] == "verifying":
            transferred, info = led.claim(
                e.project, e.number, CONDUCTOR, "auto", e.pol["auto_lease_minutes"],
                capacity=False, handoff_from=(f"run:{run['id']}", run["epoch"]))
            if transferred is None:
                detail = info.get("unavailable") or "canonical lease transfer refused"
                ctx.say(f"{e.project}#{e.number}: {detail}; existing lease left to expire safely")
        else:
            led.release(e.project, e.number, holder=f"run:{run['id']}", epoch=run["epoch"])
    update_cols = {"status": "ended", "outcome": e.outcome, "exit_code": code,
                   "ended_at": iso(led.now())}
    if not run["stop_reason"] and e.reason:     # mahler#124: record why it stopped
        update_cols["stop_reason"] = e.reason
    from .run_usage import columns
    update_cols.update(columns(e.log, run, ctx.cfg))
    led.update_run(run["id"], **update_cols)
    from . import history
    final_run = led.q1("SELECT * FROM runs WHERE id=?", (run["id"],))
    history.index_run(led, final_run,
                      base=f"origin/{e.pol.get('base', 'main')}")
    try:
        final_item = led.item(e.project, e.number)
        history.record_run_note(led, final_run, final_item, e.pol, e.verb, e.saved,
                                e.log.get("tool_counts"))
    except Exception as exc:
        ctx.say(f"{e.project}#{e.number}: run note unavailable: {type(exc).__name__}")
    _check_estimate_calibration(ctx)
    if not e.keep_worktree and run["worktree"]:
        runner.remove_worktree(e.pol["path"], run["worktree"], run["branch"],
                               runner.worktree_root(e.pol))


def _fenced_out(led, run, item):
    """True when another holder's epoch has superseded this run's epoch."""
    epoch = int(run["epoch"])
    cur = led.item(run["project"], run["number"])
    if cur is not None and int(cur["epoch"] or 0) > epoch:
        return True
    lease = led.lease(run["project"], run["number"])
    return lease is not None and lease["epoch"] != epoch


def _mark_stale(e):
    """Remember and report a superseded epoch once, even across slow I/O."""
    if not e.stale and _fenced_out(e.led, e.run, e.item):
        e.stale = True
        e.led.event("stale_transition_dropped", e.project, e.number, {
            "run": e.run["id"], "role": e.run["role"], "epoch": e.run["epoch"],
            "outcome": e.outcome,
            "preserved_state": e.led.item(e.project, e.number)["state"]})


def _prepare_ci_retry(e):
    if (e.stale or e.reason or e.run["role"] != "fix" or e.verb != "BLOCKED"
            or not e.rest.startswith("CI-RETRY ") or not e.rest[9:].strip()):
        return None
    led = e.led
    attribution = json.loads(led.get_kv(f"ci-fix:{e.run['id']}") or "{}")
    pr, sha = attribution.get("pr"), attribution.get("sha")
    if not pr or not sha or e.item["pr"] != pr or attribution.get("refunded"):
        return None
    latest = led.last_run(e.project, e.number, roles=("build", "fix"))
    cycle = attribution.get("cycle")
    counters = {"attempts", "esc_tier", "esc_fails"}
    if (cycle != f"red:{e.project}#{e.number}:{pr}:{sha}"
            or set(attribution.get("before", {})) != counters
            or set(attribution.get("after", {})) != counters):
        return None
    key = f"ci-rerun:{e.project}#{e.number}:{pr}:{sha}"
    if (not latest or latest["id"] != e.run["id"] or led.get_kv(key)
            or not led.get_kv(cycle) or not led.get_kv(f"{cycle}:charged")
            or any(e.item[k] != v for k, v in attribution["after"].items())):
        return None
    try:
        gh = e.ctx.gh(e.project)
        view = gh.pr_view(pr)
        if (view.get("state") != "OPEN" or view.get("headRefOid") != sha
                or runner.git(e.run["worktree"], "rev-parse", "HEAD") != sha
                or runner.git(e.run["worktree"], "status", "--porcelain", "--untracked-files=all")):
            return None
        runs = gh.ci_retry_runs(sha, view.get("baseRefName"), reported=True)
        if not runs:
            return None
        return key, attribution, runs
    except (GHError, runner.GitError, ValueError):
        return None


def _apply_ci_retry(e, prepared):
    key, attribution, runs = prepared
    led = e.led
    current = led.item(e.project, e.number)
    if (led.get_kv(key) or any(current[k] != v for k, v in attribution["after"].items())):
        _retry(e)
        return
    led.set_kv(key, json.dumps({"runs": runs, "since": iso(led.now()), "fix_run": e.run["id"]}))
    attribution["refunded"] = True
    led.set_kv(f"ci-fix:{e.run['id']}", json.dumps(attribution))
    led.set_kv(attribution["cycle"], None)
    led.set_kv(attribution["cycle"] + ":charged", None)
    led.set_kv(f"ci:{e.project}#{e.number}:{attribution['pr']}",
               json.dumps({"sha": attribution["sha"], "since": iso(led.now())}))
    e.set_state("verifying", "confirmed CI-RETRY; conductor reruns failed jobs",
                branch=e.run["branch"], **attribution["before"])


def _record_lessons(ctx, run, log):
    """D44 usage evidence: reported lessons count only if they were in this
    run's launch inventory. Never raises; diagnostics carry no lesson text."""
    if ctx.dry_run:
        return
    try:
        names, bad = platforms.lessons_report(log["final"] or log["last_text"])
        done, unknown = ctx.led.record_lesson_citations(
            run["project"], config.account_of(ctx.cfg["platforms"][run["platform"]]),
            run["id"], names)
        if bad or unknown:
            ctx.say(f"{run['project']}#{run['number']}: ignored {bad + unknown} "
                    f"unusable lesson report entries")
    except Exception as e:                      # noqa: BLE001 — never fail finalization
        ctx.say(f"{run['project']}#{run['number']}: lesson usage not recorded: {type(e).__name__}")


def finalize(ctx, run):
    """A run that ended passes through here exactly once."""
    led = ctx.led
    project, n = run["project"], run["number"]
    recorded = led.run(run["id"])
    if recorded is not None and recorded["status"] == "ended":
        if recorded["stop_reason"] == "resume_rejected":
            return
        no_work = json.loads(led.get_kv(f"no_work_done:{project}#{n}") or "{}")
        if run["id"] in no_work.get("runs", []):
            return
    pol = ctx.policy(project)
    item = led.item(project, n)
    pconf = ctx.cfg["platforms"].get(run["platform"]) or {}
    kind = pconf["kind"]
    run_dict = dict(run)
    model = run_dict.get("model") or pconf.get("sort_model" if run_dict.get("role") == "sort" else "build_model") or pconf.get("model")
    log = platforms.read_log(run["log_path"], kind, model=model)
    _record_run_usage(ctx, run, kind, log)
    if pconf.get("api_credits") and not ctx.dry_run:
        from . import api_credits
        api_credits.after_run(ctx, run, pconf, log)    # D41: exhausted / key rejected
        api_credits.revoke_run_token(pconf, run["id"], led=ctx.led)  # the gateway forgets this run's token
    session_id = log.get("session_id")
    if not ctx.dry_run and isinstance(session_id, str) and session_id.strip():
        led.update_run(run["id"], session_id=session_id)

    verb, rest = platforms.status_line(log["final"] or log["last_text"])
    _record_lessons(ctx, run, log)
    code = runner.exit_code(run)
    setup_failed = code == 97 and run["role"] == "build"   # setup died before the agent ran
    reason = run["stop_reason"] or ("auth_denied" if _auth_denied(log, code, verb) else None) or \
             ("no_credit" if log.get("credit_exhausted")
                                     or log.get("auth_failed") else
                                     "quota" if log["quota_hit"] else None) or \
             ("model_unavailable" if _model_unavailable_fast(run, log, code, verb) else None) or \
             ("setup-failed" if setup_failed else None) or \
             ("silent" if not verb and (not run["log_path"] or not run["status_path"]
                                         or (not run["pid"] and not run["worktree"])) else None)
    outcome = ("setup failed" if setup_failed
               else verb or (f"exit {code}" if code else "no status line"))
    if verb == "BLOCKED" and rest and not setup_failed:
        outcome = f"{verb} {rest}"
    if log.get("credit_exhausted"):
        _record_credit_failure(ctx, run, log)
    elif reason is None and (log.get("ok") is True or verb in {
            "DONE", "READY", "SPLIT", "REVIEW-PASS", "DESIGNED"}):
        _record_credit_recovery(ctx, run["platform"])
    ctx.say(f"{project}#{n}: run {run['id']} ({run['role']} on {run['platform']}) ended — "
            f"{outcome}{f' [{reason}]' if reason else ''}")
    if ctx.dry_run:
        return
    if reason == "silent":
        _hold_platform(ctx, run)
    elif reason == "model_unavailable":
        _hold_model_unavailable(ctx, run, log)
    elif reason == "auth_denied":
        _hold_auth_denied(ctx, run, log)
    ending = Ending(ctx, run, item, pol, log, kind, verb, rest, reason, outcome)
    _mark_stale(ending)
    resume = json.loads(led.get_kv(f"resume-run:{run['id']}") or "{}")
    if (run["role"] == "fix" and resume and not reason and not verb
            and code != 0 and log.get("ok") is not True
            and platforms.resume_rejected(log)):
        ending.reason = "resume_rejected"
        with led._tx():
            _mark_stale(ending)
            if not ending.stale:
                ending.set_state("verifying", "saved session unavailable — fresh fix next")
        _close_the_books(ending, code)
        return
    if setup_failed:
        if not ending.stale and _setup_failure(ending):
            return
        _save_work(ending)
        _close_the_books(ending, code)
        return

    prepared_ci_retry = _prepare_ci_retry(ending)

    # Snapshot pushes and handoff comments can take long enough for an
    # interactive claim to replace us. Do them before taking the write lock.
    if run["role"] not in ("sort", "review", "design"):
        _save_work(ending)

    # Prepare slow work without holding SQLite's single writer lock. The
    # transaction below must recheck the epoch after these calls return.
    _mark_stale(ending)
    if not ending.stale and not ending.closed:
        if run["role"] == "design" and verb == "DESIGNED":
            prepared_design = _prepare_design(ending)
        elif (run["role"] == "review" and verb in ("REVIEW-PASS", "REVIEW-FAIL")
                and (item["pr"] or not no_change.read(led, project, n))):
            prepared_review = _prepare_review(ending, "pass" if verb == "REVIEW-PASS" else "fail")
        elif run["role"] not in ("sort", "review", "design"):
            if (verb == "BLOCKED" and run["role"] in ("build", "fix") and rest
                    and not _ACCESS_BLOCKER.search(rest) and not _GUI_DEFERRED.search(rest)):
                ending.blocked_external = _blocked_is_external(ending)
                if ending.blocked_external and not _blocked_dependencies(ending):
                    ending.blocked_check = _check_blocked_condition(ctx, project, n, rest)
            handler = next((handle for matches, handle in ENDINGS if matches(ending)), _retry)
            if handler is _ended_unconfirmed and not log.get("gateway_failed"):
                ending.verify_green = _try_verify_fallback(ctx, run, pol, ending.saved, item, log, code)

    # Claim uses BEGIN IMMEDIATE too: checking the epoch and applying the
    # outcome in one transaction prevents a claim between the check and any
    # of the handler's item writes (including retry counters and branches).
    with led._tx():
        _mark_stale(ending)
        if not ending.stale:
            led.reset_setup_fails(project, n)
            if ending.saved:
                led.set_kv(_no_work_key(ending), None)
                led.set_kv(no_change.key(project, n), None)
                led.upsert_item(project, n, branch=ending.saved["ref"])
            if ending.closed:
                ending.set_state("done", outcome)
                if verb == "MERGED":
                    ending.ping(f"Shipped — {project} #{n}", item["title"], tags="rocket")
            elif run["role"] == "sort":
                SORT_OUTCOMES.get(verb, _retry)(ending)
            elif run["role"] == "review":
                if not item["pr"] and no_change.read(led, project, n):
                    no_change.finish(ending)
                elif verb in ("REVIEW-PASS", "REVIEW-FAIL"):
                    REVIEW_OUTCOMES[verb](ending, prepared_review, ending.defer)
                else:
                    REVIEW_OUTCOMES.get(verb, _review_inconclusive)(ending)
            elif run["role"] == "design":
                _design_result(ending, prepared_design if verb == "DESIGNED" else None)
            elif prepared_ci_retry:
                _apply_ci_retry(ending, prepared_ci_retry)
            elif not _dispatch(ending):
                return              # resumed; finalizes again when it ends
    ending.notify()
    _close_the_books(ending, code)


def _check_estimate_calibration(ctx):
    """Periodically compare predicted vs actual durations and refine estimates (mahler#59)."""
    est_cfg = ctx.cfg.get("estimates", {})
    interval = est_cfg.get("calibration_interval", 10)
    window = est_cfg.get("calibration_window", 20)

    cur = int(ctx.led.get_kv("runs_since_calibration") or "0") + 1
    if cur >= interval:
        stats = ctx.led.calibrate_estimates(window=window)
        ctx.led.set_kv("runs_since_calibration", "0")
        if stats:
            ctx.say(f"Calibrated time estimates over {stats['samples']} runs: "
                    f"factor={stats['factor']:.2f}, MAE={stats['mae']}m")
    else:
        ctx.led.set_kv("runs_since_calibration", str(cur))


MODEL_UNAVAILABLE_GRACE_SECONDS = 120


def _model_unavailable_fast(run, log, code, verb):
    """A model-rejection error (issue #420) counts only when the run failed
    fast — within MODEL_UNAVAILABLE_GRACE_SECONDS of starting. A model that
    was accepted and later hit trouble mid-run is a different problem, and
    must not block the variant on a coincidental late error."""
    if (not log.get("model_unavailable") or code == 0
            or log.get("ok") is True or verb == "DONE"):
        return False
    started_at = row_get(run, "started_at")
    if not started_at:
        return False
    try:
        # The wrapper writes this file when the process exits. Watchdog
        # polling and finalization may happen much later.
        exited_at = os.stat(run["status_path"]).st_mtime
        elapsed = exited_at - parse(started_at).timestamp()
    except (OSError, KeyError, TypeError, ValueError):
        return False
    return 0 <= elapsed < MODEL_UNAVAILABLE_GRACE_SECONDS


def _hold_platform(ctx, run):
    """A run that never printed anything says more about the machine than the
    task: the next run on that platform would most likely block the same way.
    Stop starting runs there for a while and tell the owner what to look for."""
    pconf = ctx.cfg["platforms"][run["platform"]]
    until = ctx.led.now() + timedelta(minutes=pconf.get("backoff_minutes", 60))
    ctx.led.record_usage(run["platform"], router.HOLD, 100.0, iso(until))
    ctx.led.set_kv(f"hold_reason:{run['platform']}", "silent")
    ctx.ping(f"Mahler: {run['platform']} runs are stuck at startup",
             f"Run {run['id']} printed nothing for {ctx.policy(run['project'])['startup_timeout_minutes']} "
             "min. Usually a macOS permission dialog is waiting on the Mac mini (e.g. Documents "
             "access for python3 after a Homebrew upgrade): click Allow. "
             f"{run['platform']} is on hold until {until.astimezone():%H:%M}.",
             run["project"], run["number"], priority="high", tags="warning")


def _auth_denied(log, code, verb):
    """The CLI's login was refused (mahler#1211) and the run produced nothing:
    a failed exit with no STATUS line. A run that got going and reported is
    judged on its report instead."""
    return bool(log.get("auth_denied") and code != 0
                and log.get("ok") is not True and not verb)


AUTH_HOLD_DAYS = 365   # "lasting": lifted by a working probe or `mahler unhold`


def _hold_auth_platform(ctx, run, log):
    """A machine-wide CLI login has expired (Cline, mahler#1485): hold the
    platform for every project until a probe shows it works again or the owner
    runs `mahler unhold`. One ping, only when the hold is new."""
    led, platform = ctx.led, run["platform"]
    fresh = led.get_kv(f"hold_reason:{platform}") != "auth"
    until = led.now() + timedelta(days=AUTH_HOLD_DAYS)
    led.record_usage(platform, router.HOLD, 100.0, resets_at=iso(until))
    led.set_kv(f"hold_reason:{platform}", "auth")
    led.event("auth_denied", run["project"], run["number"],
              {"platform": platform, "run": run["id"], "scope": "platform"})
    if fresh:
        ctx.ping(f"Mahler: {platform} needs you to log in again",
                 f"Run {run['id']} ended with the CLI's login expired. Run `cline auth` "
                 f"on the Mac mini; {platform} is blocked for every project until a "
                 "probe shows the login works (`mahler usage --probe`) or `mahler unhold "
                 f"{platform}`.",
                 run["project"], run["number"], priority="high", tags="warning")


def _hold_auth_denied(ctx, run, log):
    """Hold the platform for this project only: its login works elsewhere
    (work-copilot on work projects), so a global hold would idle it there."""
    if ctx.cfg["platforms"].get(run["platform"], {}).get("kind") == "cline":
        return _hold_auth_platform(ctx, run, log)
    led, platform, project = ctx.led, run["platform"], run["project"]
    fresh = router.project_hold(led, project, platform) is None
    until = led.now() + timedelta(minutes=AUTH_DENIED_HOLD_MINUTES)
    led.set_kv(router.project_hold_key(project, platform), iso(until))
    led.event("auth_denied", project, run["number"],
              {"platform": platform, "run": run["id"], "until": iso(until)})
    if fresh:
        detail = (log.get("last_error") or "access denied").strip()[-240:]
        ctx.ping(f"Mahler: {platform} is refused on {project}",
                 f"Run {run['id']} failed at startup: the CLI's login was refused "
                 f"({detail}). {platform} is off {project} until "
                 f"{until.astimezone():%H:%M}; other projects keep using it.",
                 project, run["number"], tags="warning")


def _hold_model_unavailable(ctx, run, log=None):
    """Reject this launch selection until its model configuration changes."""
    from . import model_availability
    record, fresh = model_availability.reject(
        ctx.led, run["platform"], ctx.cfg["platforms"][run["platform"]], run, log or {})
    if fresh:
        ctx.ping(f"Mahler: {run['platform']}'s model is unavailable",
                 f"Run {run['id']} failed fast — the CLI rejected its configured model. "
                 f"Repair the model or effort configuration to enable this selection.\n"
                 f"{record['diagnostic']}\nAvailable models: {record['available_models']}",
                 run["project"], run["number"], tags="warning")


def _did_no_work(ctx, run, log, code):
    """An errored run whose worktree HEAD never moved from launch and holds no
    uncommitted changes did no work (mahler#1485): old green commits on the
    branch must not count as this run's build."""
    if code == 0 and (log or {}).get("ok", True):
        return False
    head = ctx.led.get_kv(f"launch_head:{run['id']}")
    if not isinstance(head, str) or not head:
        return False
    return (runner.head_sha(run["worktree"]) == head
            and not runner.worktree_dirty(run["worktree"]))


def _try_verify_fallback(ctx, run, pol, saved, item, log=None, code=0):
    """D18 verify-green fallback: no STATUS line, but if the branch has commits
    ahead of base (or uncommitted changes in saved snapshot) *and* the project's
    verify command passes in the worktree, treat as DONE. Returns True if the
    fallback can be applied after rechecking the epoch."""
    led = ctx.led
    project, n = run["project"], run["number"]
    base = pol.get("base", "main")
    verify_cmd = pol.get("verify")
    if not verify_cmd:
        return False
    if _did_no_work(ctx, run, log, code):
        ctx.say(f"{project}#{n}: run errored and made no new work — no verify-green fallback")
        return False
    ahead = runner.commits_ahead(run["worktree"], base)
    if ahead == 0 and saved and saved.get("ahead", 0) > 0:
        ahead = saved["ahead"]
    if ahead == 0:
        ctx.say(f"{project}#{n}: no STATUS line, no commits ahead of {base} — failed attempt")
        return False
    ctx.say(f"{project}#{n}: no STATUS line but {ahead} commit(s) ahead — running verify")
    ok = runner.verify_in_worktree(run["worktree"], verify_cmd,
                                   timeout=pol.get("verify_timeout", 120),
                                   output_path=os.path.join(os.path.dirname(run["log_path"]), "verify.log"))
    if not ok:
        led.set_kv(f"verify_failed:{run['id']}", "1")
        ctx.say(f"{project}#{n}: verify failed in worktree — failed attempt")
        return False
    return bool((saved["ref"] if saved else None) or item["branch"])


def _apply_verify_fallback(e):
    """Apply prepared verification only after the final atomic epoch check."""
    e.ctx.say(f"{e.project}#{e.number}: verify green — treating as DONE (agent didn't confirm)")
    e.set_state("verifying", "verify-green fallback — agent didn't confirm",
                summary=e.item["title"])
    e.led.set_kv(f"unconfirmed:{e.project}#{e.number}", "1")
    e.ping(f"Build finished (fallback) — {e.project} #{e.number}",
           f"{e.run['platform']} didn't end with STATUS: DONE, but "
           "verify passes; the conductor opens the PR next", priority="low")


RESUME_CAP = 2


def _try_cline_nudge(ctx, run, kind, log, pol):
    """Resume a free builder (Cline or Kilo) when it stopped prematurely or hit
    a transient network drop (mahler#17, mahler#426).

    Verified resume commands (mahler#426):
    - Cline 3.0.64:
      Exact commands tested:
        1. `cline --id <sid> --cwd <wt> --json --auto-approve true -t <secs> < <resume_file>`
        2. `cline --id <sid> --json "prompt"`
      Both exit 1 immediately with:
        {"type":"error","message":"JSON output mode requires a prompt argument or piped stdin (interactive mode is unsupported)"}
      because `--id` sets `interactive = true` in the CLI parser before stdin is checked.
      Stdin redirection does not bypass this check because `r.interactive` is checked
      before stdin is read.
      Therefore, Cline resume falls back to a fresh `cline` run in the same worktree with
      the resume prompt as positional argument and stdin redirection:
        `cline --cwd <wt> --json --auto-approve true -t <secs> [-m <model>] <prompt> < <resume_file>`
      The worktree preserves all accumulated work across resumes.
    - Kilo 7.6.2:
      Exact command tested:
        `kilo run <prompt> --session <id> --dir <wt> --auto --format json [-m <model>] < <resume_file>`
      Resumes the session with the same model and directory. When the error is
      "session not found" (or no sessionID was recorded in the log), it falls back to:
        `kilo run <prompt> --dir <wt> --auto --format json [-m <model>] < <resume_file>`

    Resumes under two conditions:
    1. Clean exit (exit 0) with no STATUS line (e.g. premature stop before commit/push).
       For Cline: finishReason == 'completed' (log["ok"] is True).
       For Kilo: exit code 0.
    2. Transient network error: the log's last error matches known drop patterns
       ("Network connection lost", "The socket connection was closed unexpectedly",
       "The operation timed out", "session not found").

    Quota hits and daily-cap errors never resume (they go to quota hold).
    Resumes are capped at RESUME_CAP (2) per run.

    Returns True if a resume was started (the run stays alive).
    """
    if kind not in ("cline", "kilo"):
        return False
    if log.get("quota_hit") or log.get("auth_failed"):
        return False
    pconf = ctx.cfg["platforms"].get(run["platform"]) or {}
    if pconf.get("api_credits") and router.usage_state(ctx.led, run["platform"], pconf)[0] != "ok":
        return False            # D41: a nudge spends credits too; only within the line
    nudged = int(dict(run).get("nudged") or 0)
    if nudged >= RESUME_CAP:
        return False

    code = runner.exit_code(run)
    last_err = (log.get("last_error") or "") + " " + (log.get("last_text") or "")
    net_err = platforms.is_network_error(log.get("last_error")) or platforms.is_network_error(log.get("last_text"))

    if net_err:
        pass
    elif code == 0:
        if kind == "cline" and not log.get("ok"):
            return False
    else:
        return False

    ctx.say(f"{run['project']}#{run['number']}: {kind} ended without STATUS "
            f"({'network error' if net_err else 'clean exit'}) — resuming ({nudged + 1}/{RESUME_CAP})")
    if ctx.dry_run:
        return True

    led = ctx.led
    next_nudged = nudged + 1
    run_dir = os.path.join(config.RUNS_DIR, str(run["id"]))
    config.ensure_private_dir(run_dir)
    resume_file = os.path.join(run_dir, f"resume-{next_nudged}.md")
    nudge_prompt = ("You stopped before finishing. Carry on with the next step of "
                    "your instructions, and end with the STATUS line. "
                    "If your work is done, commit it locally first; the conductor snapshots "
                    "and pushes the branch.")
    with open(resume_file, "w") as fh:
        fh.write(nudge_prompt)

    wt = run["worktree"]
    pconf = ctx.cfg["platforms"][run["platform"]]
    timeout_mins = pol.get("run_timeout_minutes", 60)

    session_id = None
    if kind == "kilo":
        if "session not found" not in last_err.lower():
            session_id = log.get("session_id")
    # For cline, session_id stays None (verified that --id rejects --json mode in 3.0.64)

    argv = platforms.resume_argv_for(pconf, nudge_prompt, wt, run["role"], timeout_mins, session_id=session_id)
    env = runner.run_env(ctx, run["project"], run["number"], run["platform"], run["id"], run["epoch"])

    pid = runner.spawn(argv, wt, run["log_path"], run["status_path"], env=env,
                       append=True, stdin_path=resume_file)
    led.update_run(run["id"], nudged=next_nudged, status="running", pid=pid)
    led.heartbeat(run["project"], run["number"], f"run:{run['id']}", run["epoch"],
                  pol["auto_lease_minutes"])
    return True


_try_nudge = _try_cline_nudge


def _cline_session_id(worktree):
    """Find the Cline session ID whose cwd matches the run's worktree.
    `cline history --json` lists sessions with their cwd."""
    try:
        r = subprocess.run([platforms.cline_exe(), "history", "--json"],
                           capture_output=True, text=True, timeout=15)
        for entry in json.loads(r.stdout):
            if entry.get("cwd") == worktree:
                return entry.get("sessionId") or entry.get("id")
    except (subprocess.SubprocessError, OSError, ValueError, TypeError):
        pass
    return None


SETUP_FAIL_CAP = 2                                   # consecutive setup failures before needs_you


def _setup_failure(e):
    """Record a setup failure atomically, then report and clean up unlocked."""
    ctx, led, run, item = e.ctx, e.led, e.run, e.item
    project, n = e.project, e.number
    tail = runner.setup_tail(run, lines=30)
    with led._tx():
        _mark_stale(e)
        if e.stale:
            return False
        fails = led.bump_setup_fails(project, n)
        stuck = fails >= SETUP_FAIL_CAP
        if stuck:
            reason = f"setup failed {fails} times in a row — the environment, not the task"
            waits.hold(led, project, n, "setup", reason,
                       resume="ready" if item["sorted_at"] else "inbox")
        else:
            e.set_state("ready" if item["sorted_at"] else "inbox",
                        f"setup failed (failure {fails} of {SETUP_FAIL_CAP}) — retrying")
        led.release(project, n, holder=f"run:{run['id']}", epoch=run["epoch"])
        led.update_run(run["id"], status="ended", outcome="setup failed", exit_code=97,
                       ended_at=iso(led.now()))
    _setup_failed_comment(ctx, run, fails, tail, stuck)
    if stuck:
        failures.report(ctx, project, n, "setup_failed", run=run, output=tail)
    else:
        ctx.ping(f"Setup failed — {project} #{n}",
                 f"run {run['id']}: setup failed ({fails}/{SETUP_FAIL_CAP}); retrying.",
                 project, n, priority="low")
    runner.remove_worktree(e.pol["path"], run["worktree"], run["branch"],
                           runner.worktree_root(e.pol))
    return True


def _setup_failed_comment(ctx, run, fails, tail, stuck):
    why = ("this looks like a broken environment, not the task — Mahler waits and "
           "retries on its own with backoff; comment `/mahler go` to retry now" if stuck
           else f"retrying (consecutive setup failures capped at {SETUP_FAIL_CAP})")
    lines = [f"<!-- mahler:handoff run={run['id']} epoch={run['epoch']} "
             f"from={run['platform']} reason=setup-failed -->",
             f"**Setup failed** — run {run['id']} stopped during the project's setup step, "
             f"before the agent started (exit 97). {fails} in a row: {why}", ""]
    if tail:
        lines += ["Last 30 lines of setup.log:", "", "```", tail, "```"]
    else:
        lines.append("(setup.log was empty or missing)")
    try:
        ctx.gh(run["project"]).comment(run["number"], "\n".join(lines))
    except GHError as e:
        ctx.say(f"#{run['number']}: couldn't post setup-failure comment — {e}")


def retry_or_fail(ctx, project, n, item, reason, outcome, platform=None, duration_mins=None, explore=False, run=None, defer=None):
    led = ctx.led
    if explore:
        led.set_state(project, n, "ready" if item["sorted_at"] else "inbox",
                      "exploration failed — retry on the normal route")
        return
    if reason in NO_ATTEMPT:
        led.set_state(project, n, "ready" if item["sorted_at"] else "inbox", f"retry ({reason})")
        return
    attempts = item["attempts"] + 1

    cur_tier = row_get(item, "esc_tier", 0)
    cur_fails = row_get(item, "esc_fails", 0)
    new_tier = cur_tier
    new_fails = cur_fails + 1

    pconf = ctx.cfg.get("platforms", {}).get(platform, {}) if platform else {}
    run_tier = router.tier_of(pconf) if platform else 1

    # Overrun heuristic: size:s run took >= 10m on tier 1 and failed
    labels = json.loads(row_get(item, "labels", "[]"))
    size = next((l.split(":", 1)[1] for l in labels if l.startswith("size:")), None)
    overrun = (run_tier == 1 and size == "s" and duration_mins is not None and duration_mins >= 10.0)

    if overrun:
        new_tier = router.cap_escalation(ctx.cfg, ctx.policy(project),
                                          max(cur_tier, run_tier) + 1, size,
                                          role="build", pin=row_get(item, "pin"))
        new_fails = 0
        ctx.say(f"{project}#{n}: escalated to tier {new_tier} — size:s overrun on tier {run_tier} ({int(duration_mins)}m >= 10m)")
        led.event("escalated", project, n, {"tier_from": cur_tier, "tier_to": new_tier,
                  "platform": platform, "reason": f"duration overrun {int(duration_mins)}m"})
    elif new_fails >= 2:
        new_tier = router.cap_escalation(ctx.cfg, ctx.policy(project),
                                          max(cur_tier, run_tier) + 1, size,
                                          role="build", pin=row_get(item, "pin"))
        new_fails = 0
        ctx.say(f"{project}#{n}: escalated to tier {new_tier} after 2 failures on tier <= {max(cur_tier, run_tier)}")
        led.event("escalated", project, n, {"tier_from": cur_tier, "tier_to": new_tier,
                  "platform": platform, "reason": "2 failures"})

    if attempts >= ctx.policy(project)["max_attempts"]:
        led.set_state(project, n, "failed", f"{attempts} failed attempts — last: {outcome}",
                      attempts=attempts, esc_tier=new_tier, esc_fails=new_fails)
        send = defer or (lambda call, *args, **kwargs: call(*args, **kwargs))
        send(failures.report, ctx, project, n, reason or outcome, run=run)
        send(ctx.ping, f"Stuck — {project} #{n}",
                 f"{attempts} attempts failed ({outcome}). Comment `/mahler go` to retry.",
                 project, n, priority="high", tags="warning")
    else:
        back = "ready" if item["sorted_at"] else "inbox"
        led.set_state(project, n, back, f"attempt {attempts} failed: {outcome}",
                      attempts=attempts, esc_tier=new_tier, esc_fails=new_fails)


REASON_TEXT = {
    "quota": "quota line reached", "preempted": "you took over in a session",
    "hung": "no progress for too long", "timeout": "hit the time limit",
    "silent": "never started — printed nothing",
    "closed": "the issue was closed", "parked": "parked", "lost-lease": "lost its lease",
    "setup-failed": "the project's setup step failed (exit 97)",
    "model_unavailable": "the CLI rejected its model",
    "no_credit": "the platform is out of credits",
    "auth_denied": "the platform's login was refused for this project",
}


def _handoff_comment(ctx, run, item, reason, outcome, saved, log, kept):
    mins = int((ctx.led.now() - parse(run["started_at"])).total_seconds() // 60)
    why = REASON_TEXT.get(reason, f"ended: {outcome}")
    lines = [f"<!-- mahler:agent handoff run={run['id']} epoch={run['epoch']} "
             f"from={run['platform']} reason={reason or 'ended'} -->",
             f"**Handoff** — {run['platform']} stopped after {mins} min ({why}).", ""]
    if saved:
        lines.append(f"Work saved: `{saved['ref']}` @ {saved['sha']} — {saved['ahead']} commit(s) "
                     f"ahead of base{'; ' + saved['stat'] if saved['stat'] else ''}. "
                     "The next run starts from there.")
    elif kept:
        lines.append(f"Couldn't push a snapshot; the worktree was kept at `{run['worktree']}`.")
    else:
        lines.append("Nothing new to save.")
    tail = (log["last_text"] or "").strip()
    if tail:
        quoted = "\n".join("> " + l for l in tail[-700:].splitlines()[-12:])
        lines += ["", "Agent's last words:", quoted]
    try:
        ctx.gh(run["project"]).comment(run["number"], "\n".join(lines))
    except GHError as e:
        ctx.say(f"#{run['number']}: couldn't post handoff comment — {e}")
