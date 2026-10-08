"""Detached, exact-merge deploy/live/smoke checks (D11).

Commands receive MAHLER_PROJECT, MAHLER_ISSUE, MAHLER_MERGE_SHA,
MAHLER_TAG (the full SHA when no release tag is known), MAHLER_ARTIFACT_REF,
MAHLER_ENVIRONMENT and MAHLER_POST_MERGE_PHASE. Values are environment data,
never shell interpolation. The project gh_account selects the deployment login.
Live stdout is JSON: {"sha": "<full merge SHA>", "tag": "<supplied tag>"}.
The live command may wait for readiness; it must resolve a tag to the exact SHA.
Smoke stdout is the #617 report, with one Tag: and one Smoke: PASS/FAIL line.
Commands must target the supplied identity, not the checkout's moving HEAD.
GUI commands must themselves use `mahler desktop run -- ...` (D40).

A private, exclusively created phase directory is the durable launch intent.
A crash in the launch gap fails at the original deadline instead of relaunching.
The detached worker bounds runtime, discards stderr and publishes its result
atomically. Only a minimal sanitized PASS report is retained for D10; no raw
command output, environment or commands are persisted by this module.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import uuid

MAX_OUTPUT = 65536


def _read(path):
    with open(path, encoding="utf-8") as stream:
        value = stream.read(MAX_OUTPUT + 1)
    if len(value) > MAX_OUTPUT:
        raise ValueError("oversize result")
    return value


def _identity(row):
    return {k: row[k] for k in ("project", "number", "merge_sha", "phase", "tag")}


def _directory(row):
    from . import config
    key = json.dumps([row["project"], row["number"], row["merge_sha"]])
    return Path(config.STATE) / "post-merge" / hashlib.sha256(key.encode()).hexdigest() / row["phase"]


def _change(ctx, row, **values):
    return ctx.led.update_post_merge_check(
        row["project"], row["number"], row["merge_sha"],
        expected_phase=row["phase"], phase=values.pop("phase", row["phase"]), **values)


def _remediate(ctx, row, policy):
    project, number, sha = row["project"], row["number"], row["merge_sha"]
    rem_key = f"post-merge-remediation:{project}:{number}:{sha}"
    if ctx.led.get_kv(rem_key):
        return
    from .ledger import row_get
    uat = ctx.led.uat(project, number)
    pr = (row_get(uat, "pr") if uat else None) or (row_get(ctx.led.item(project, number), "pr") if ctx.led.item(project, number) else None)
    if not pr:
        ev = ctx.led.q1("SELECT detail FROM events WHERE project=? AND number=? AND kind='shipped' ORDER BY id DESC LIMIT 1", (project, number))
        if ev:
            try:
                pr = json.loads(ev["detail"]).get("pr")
            except (json.JSONDecodeError, AttributeError):
                pass
    if not pr and hasattr(ctx.gh(project), "pr_for_commit"):
        pr = ctx.gh(project).pr_for_commit(sha)

    from . import failures
    failures.report_post_merge(ctx, project, number, sha, row["phase"],
                               row_get(row, "failure_code", ""), pr=pr, policy=policy)

    if policy.get("post_merge", {}).get("auto_revert", False) and pr:
        from .console.revert import revert
        revert(ctx, project, number, pr, source="post_merge", recover=True)

    ctx.led.set_kv(rem_key, "done")


def _fail(ctx, row, code, policy=None):
    _change(ctx, row, status="FAIL", failure_code=code,
            summary=f"Post-merge {row['phase']}: {code}")
    policy = policy or (ctx.policy(row["project"]) if hasattr(ctx, "policy") else None)
    if policy:
        try:
            updated = ctx.led.post_merge_check(row["project"], row["number"], row["merge_sha"])
            _remediate(ctx, updated or {**row, "status": "FAIL", "failure_code": code}, policy)
        except Exception:
            ctx.say(f"{row['project']}#{row['number']}: post-merge failure remediation unavailable")


def register(ctx, project, number, view):
    """Only confirmed merge metadata can create an execution identity."""
    from .ship import pr_merged
    if ctx.dry_run or not pr_merged(view):
        return
    try:
        sha = (view.get("mergeCommit") or {}).get("oid") or ""
        ctx.led.create_post_merge_check(project, number, sha, ctx.policy(project), tag=sha)
    except Exception:
        ctx.say(f"{project}#{number}: post-merge registration unavailable")


def _launch(ctx, row, policy, directory, command):
    from . import config
    from .ledger import parse
    env = dict(config.run_env(ctx.cfg, config.gh_account_of(policy)) or os.environ)
    env.update({"MAHLER_PROJECT": row["project"], "MAHLER_ISSUE": str(row["number"]),
                "MAHLER_MERGE_SHA": row["merge_sha"], "MAHLER_TAG": row["tag"],
                "MAHLER_ARTIFACT_REF": row["artifact_ref"],
                "MAHLER_ENVIRONMENT": row["target"], "MAHLER_POST_MERGE_PHASE": row["phase"]})
    config.ensure_private_dir(directory.parent)
    directory.mkdir(mode=0o700)  # exclusive intent: never reuse a phase's files
    token = uuid.uuid4().hex
    manifest = {"identity": _identity(row), "token": token}
    (directory / "intent.json").write_text(json.dumps(manifest))
    remaining = (parse(row["deadline_at"]) - ctx.led.now()).total_seconds()
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), str(directory),
         str(max(0.01, remaining)), command], cwd=policy["path"], env=env,
        start_new_session=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Reap without waiting in the tick (also avoids Popen ResourceWarnings).
    threading.Thread(target=proc.wait, daemon=True).start()


def _poll(ctx, row, directory):
    result_path = directory / "result.json"
    if not result_path.exists():
        return
    intent = json.loads(_read(directory / "intent.json"))
    result = json.loads(_read(result_path))
    if (intent.get("identity") != _identity(row) or result.get("identity") != _identity(row)
            or result.get("token") != intent.get("token")):
        _fail(ctx, row, "stale_result")
        return
    if result.get("error"):
        _fail(ctx, row, "command_failed")
    elif row["phase"] == "deploy":
        _change(ctx, row, phase="live")
    elif row["phase"] == "live":
        if result.get("sha") != row["merge_sha"] or result.get("tag") != row["tag"]:
            _fail(ctx, row, "identity_mismatch")
        else:
            _change(ctx, row, phase="smoke", live_sha=row["merge_sha"])
    elif result.get("pass") is not True or result.get("tag") != row["tag"]:
        _fail(ctx, row, "invalid_report")
    else:
        # Reconstruct only the validated minimum, never persist private journey details.
        report = directory / "report.md"
        report.write_text(f"Tag: {row['tag']}\nSmoke: PASS\n")
        _change(ctx, row, status="PASS", summary="Exact live identity and smoke PASS",
                evidence_ref=str(report))


def _observe_live(ctx, row, policy):
    from . import config, launch_health
    from .ledger import parse
    remaining = (parse(row["deadline_at"]) - ctx.led.now()).total_seconds()
    timeout = min(5, max(.01, remaining / 2))
    try:
        if policy["post_merge"]["live_strategy"] == "mahler":
            artifact = launch_health.exact_live(
                row["merge_sha"], timeout=timeout,
                env=config.run_env(ctx.cfg, config.gh_account_of(policy)))
        else:
            artifact = ctx.gh(row["project"]).exact_preview(
                row["merge_sha"], row["target"], timeout=timeout)
    except Exception:
        # Provider/auth/JSON failures are retryable, without retaining payloads.
        return
    if ctx.led.now() >= parse(row["deadline_at"]):
        _fail(ctx, row, "timeout")
    elif artifact:
        _change(ctx, row, phase="smoke", live_sha=row["merge_sha"], artifact_ref=artifact)


def _advance(ctx, row, policy):
    from .ledger import parse
    if ctx.led.now() >= parse(row["deadline_at"]):
        _fail(ctx, row, "timeout")
        return
    if row["phase"] == "deploy" and policy["post_merge"]["deploy_strategy"] == "watch":
        _change(ctx, row, phase="live")
        return
    if row["phase"] == "live" and policy["post_merge"]["live_strategy"] != "command":
        _observe_live(ctx, row, policy)
        return
    command = (policy.get("smoke") if row["phase"] == "smoke"
               else policy["post_merge"][row["phase"] + "_command"])
    if not command:
        _fail(ctx, row, "smoke_disabled" if row["phase"] == "smoke" else "command_missing")
        return
    directory = _directory(row)
    if directory.exists():
        _poll(ctx, row, directory)
    else:
        _launch(ctx, row, policy, directory, command)


def _deliver(ctx, row):
    """Recover an uncertain post by looking up its exact immutable reference.

    D10's normal sync pass alone accepts the attributed GitHub comment.
    """
    from .gh import completion_evidence
    key = "post-merge-evidence:" + str(_directory(row))
    if ctx.led.get_kv(key):
        return
    uat = ctx.led.uat(row["project"], row["number"])
    latest = ctx.led.q1("SELECT merge_sha FROM post_merge_checks WHERE project=? AND number=? "
                        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                        (row["project"], row["number"]))
    if (not uat or uat["sha"] != row["merge_sha"]
            or latest["merge_sha"] != row["merge_sha"]):
        return
    body = "Smoke: PASS report=" + Path(row["evidence_ref"]).as_uri()
    gh = ctx.gh(row["project"])
    for comment in gh.issue_comments(row["number"]):
        if comment.get("body", "").strip() == body and completion_evidence(comment, uat["shipped_at"]):
            ctx.led.set_kv(key, "posted")
            return
    gh.comment_create(row["number"], body)
    ctx.led.set_kv(key, "posted")


def advance(ctx, projects):
    """One launch/poll per check, isolated per project and per check."""
    if ctx.dry_run:
        return
    for project in projects:
        name = project["name"]
        try:
            policy = ctx.policy(name)
            if not policy.get("enabled") or not policy["post_merge"]["enabled"]:
                continue
            # Recover registration after a crash following the shipped transaction.
            for uat in ctx.led.q("SELECT u.* FROM uat u JOIN items i USING(project,number) "
                                 "WHERE u.project=? AND i.state='shipped'", (name,)):
                register(ctx, name, uat["number"], {"state": "MERGED", "mergeCommit": {"oid": uat["sha"]}})
            for row in ctx.led.q("SELECT * FROM post_merge_checks WHERE project=? "
                                 "AND status IN ('pending','PASS','FAIL')", (name,)):
                try:
                    if row["status"] == "PASS":
                        _deliver(ctx, row)
                    elif row["status"] == "FAIL":
                        _remediate(ctx, row, policy)
                    else:
                        _advance(ctx, row, policy)
                except Exception:
                    if row["status"] == "pending":
                        _fail(ctx, row, "execution_error", policy=policy)
                    ctx.say(f"{name}#{row['number']}: post-merge action unavailable")
        except Exception:
            ctx.say(f"{name}: post-merge pass unavailable")


def _worker(directory, timeout, command):
    """Standalone child: blocking work happens here, never inside a tick."""
    directory = Path(directory)
    intent = json.loads(_read(directory / "intent.json"))
    result = dict(intent)
    proc = None
    output = b""
    try:
        proc = subprocess.Popen(["/bin/sh", "-c", command], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        def kill():
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                # Some hosts deny group signals after the shell exits.
                proc.kill()
        timer = threading.Timer(timeout, kill)
        timer.start()
        try:
            with proc.stdout:
                output = proc.stdout.read(MAX_OUTPUT + 1)
            if len(output) > MAX_OUTPUT:
                kill()
                raise ValueError("oversize")
            if proc.wait() != 0:
                raise ValueError("exit")
        finally:
            timer.cancel()
            kill()  # remove background descendants too
            proc.wait()
            if output:
                try:
                    (directory / "output.log").write_bytes(output[:MAX_OUTPUT])
                except OSError:
                    pass
        text = output.decode("utf-8")
        phase = intent["identity"]["phase"]
        if phase == "live":
            live = json.loads(text)
            if (live.get("sha") == intent["identity"]["merge_sha"]
                    and live.get("tag") == intent["identity"]["tag"]):
                result.update(sha=live["sha"], tag=live["tag"])
        elif phase == "smoke":
            tags = re.findall(r"^Tag:[ \t]*(.*)$", text, re.M)
            verdicts = re.findall(r"^Smoke:[ \t]*(.*)$", text, re.M)
            valid = tags == [intent["identity"]["tag"]] and verdicts == ["PASS"]
            result.update(tag=intent["identity"]["tag"] if valid else None,
                          **{"pass": valid})
    except Exception:
        if output:
            try:
                (directory / "output.log").write_bytes(output[:MAX_OUTPUT])
            except OSError:
                pass
        result = {**intent, "error": "command_failed"}
    temporary = directory / "result.tmp"
    temporary.write_text(json.dumps(result))
    temporary.replace(directory / "result.json")


if __name__ == "__main__":
    os.umask(0o077)
    _worker(sys.argv[1], float(sys.argv[2]), sys.argv[3])
