"""One tick: watchdog → GitHub sync → lease expiry → usage → schedule → labels.

Run every 60s by launchd (through launcher/mahler-launcher). An exclusive
flock makes overlapping ticks exit at once — agents outlive the tick that
launched them, exactly as in dispatch.

This module is the entry only: each pass it calls lives in its own module
(mahler#70) — watchdog.py, sync.py, finalize.py, ship.py, tick.py, usage.py.
"""

import json
import fcntl
import os
import re
import sys

from . import backup, capacity, config, digest, failures, janitor, notify, platform_audit, practices_audit
from .console import outbox
from .gh import project_client, GHError
from .ledger import iso
from .ship import ship
from .sync import close_finished_parents, mirror_labels, sync
from .tick import expire, queue_maintenance, schedule
from .usage import compute_burst, refresh_usage
from .watchdog import watchdog
from .warmup import relearn, relearn_due, warmup_pass
from . import post_merge, resets


class Ctx:
    def __init__(self, cfg, led, dry_run=False, hot_hold=True):
        self.cfg, self.led, self.dry_run, self.hot_hold = cfg, led, dry_run, hot_hold
        self.lines = []
        self.holds = []
        self.passes_filed = set()  # projects with a pass filed (or dry-run queued) this tick
        self._gh = {}
        self._labels = {}          # (project, number) -> labels from this tick's sync
        self._scorecard_rows = None
        self.burst_lines = None    # D23: set by compute_burst during this tick
        self.capacity = capacity.Observer(self)   # mahler#734: this tick's waiting work

    @property
    def scorecard_rows(self):
        from . import scorecard
        if self._scorecard_rows is None:
            self._scorecard_rows = scorecard.table(self.led, self.cfg)
        return self._scorecard_rows

    def policy(self, project):
        return config.project_policy(self.cfg, project)

    def gh(self, project):
        pol = self.policy(project)
        repo = pol["repo"]
        if repo not in self._gh:
            self._gh[repo] = project_client(self.cfg, pol)
        return self._gh[repo]

    def say(self, msg):
        self.lines.append(msg)

    def hold(self, kind, **data):
        self.holds.append({"kind": kind, **data})

    def url(self, project, number):
        return f"https://github.com/{self.policy(project)['repo']}/issues/{number}"

    def _console_url(self, project, number):
        """The deep link into the console for one item (mahler#257): when
        `serve.public_url` is set, a needs-you ping opens the console on that
        item in Triage; otherwise the click stays the GitHub issue."""
        base = (self.cfg.get("serve") or {}).get("public_url") or ""
        if base:
            return f"{base.rstrip('/')}/#needs/{project}/{number}"
        return self.url(project, number)

    def ping(self, title, message="", project=None, number=None, priority="default",
             tags="", console=False):
        if self.dry_run:
            return
        click = self._console_url(project, number) if (console and project) else \
            (self.url(project, number) if project else None)
        notify.send(self.cfg, title, message,
                    click=click,
                    priority=priority, tags=tags)


def take_lock():
    config.ensure_private_dir(config.STATE)   # 0700, even if created loose earlier (#75)
    fh = open(config.LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.chmod(config.LOCK_PATH, 0o600)     # creation is umask-masked (#75)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def tick(ctx):
    ctx.holds = []
    ctx._scorecard_rows = None
    projects = [p for p in config.enabled_projects(ctx.cfg) if _project_ok(ctx, p)]
    outbox.drain(ctx)
    compute_burst(ctx, projects)    # D23: before watchdog so running runs
    watchdog(ctx)                   #   see burst lines too
    for p in projects:
        try:
            sync(ctx, p["name"])
        except GHError as e:
            ctx.say(f"{p['name']}: GitHub sync failed — {e}")
            _sync_access_result(ctx, p, e)
        else:
            _sync_access_result(ctx, p)
    failures.backfill(ctx, projects)
    expire(ctx)
    close_finished_parents(ctx, projects)
    if ctx.led.paused():
        ctx.say("paused — not starting anything (mahler resume)")
        ctx.hold("paused")
        capacity.of(ctx).observe_paused(projects)
    else:
        refresh_usage(ctx, projects)
        resets.spend_banked(ctx, projects)
        try:
            if relearn_due(ctx.led):
                relearn(ctx)          # mahler#536: daily learned-warm-up data pass
        except Exception as e:
            ctx.say(f"warmup relearn failed — {e}")
        warmup_pass(ctx)
        queue_maintenance(ctx, projects)
        platform_audit.queue(ctx, projects)
        practices_audit.queue(ctx, projects)
        schedule(ctx, projects)
    record_holds(ctx)
    if not ctx.led.paused():
        ship(ctx, projects)
        post_merge.advance(ctx, projects)
    capacity.of(ctx).flush()
    for p in projects:
        mirror_labels(ctx, p["name"])
    if not ctx.dry_run:
        for p in config.enabled_projects(ctx.cfg):     # backups run even while paused
            for spec in p.get("backups") or []:
                try:
                    backup.run(ctx, p["name"], spec)
                except Exception as e:
                    ctx.say(f"{p['name']}: backup failed — {e}")
        try:
            backup.run_ledger(ctx)                     # mahler#533: also while paused
        except Exception as e:
            ctx.say(f"mahler: ledger backup failed — {e}")
    digest.maybe_send(ctx)                  # informational: also runs while paused
    janitor.maybe_run(ctx)                  # daily sweep (mahler#7): also while paused
    return ctx.lines


def _sync_access_result(ctx, project, error=None):
    """Alert once per repository-access outage, surviving ticks and restarts."""
    if ctx.dry_run:
        return
    name = project["name"]
    key = f"sync_access:{name}"
    try:
        previous = ctx.led.get_kv(key)
        if error is None:
            if previous:
                ctx.led.set_kv(key, "")
            return
        state = json.loads(previous) if previous else {"failures": 0, "notified": False}
        matching = re.search(
            r"\bHTTP\s+404\b|\bnot found\b|\bcould not resolve to a repository\b",
            str(error), re.IGNORECASE)
        state["failures"] = min(2, state["failures"] + 1) if matching else 0
        alert = state["failures"] == 2 and not state["notified"]
        if alert:
            state["notified"] = True
        # Latch before attempting delivery, so a failed notification cannot
        # repeatedly ping on each tick. Only successful sync re-arms it.
        ctx.led.set_kv(key, json.dumps(state))
        if alert:
            ctx.ping(f"{name}: GitHub repository unreachable",
                     f"Cannot sync {project['repo']} for two consecutive ticks. "
                     "Check the project's GitHub login and repository access.",
                     priority="high", tags="warning")
    except Exception:  # diagnostics must never block another project's sync
        ctx.say(f"{name}: could not record or notify GitHub access outage")


def record_holds(ctx):
    """Diagnostic writes must never prevent shipping or the rest of a tick."""
    if ctx.dry_run:
        return
    try:
        ctx.led.set_kv("schedule_holds", json.dumps({
            "at": iso(ctx.led.now()), "holds": ctx.holds[:200]}))
    except Exception as e:
        ctx.say(f"couldn't record schedule holds — {e}")


def _project_ok(ctx, p):
    if not p.get("repo") or not p.get("path"):
        ctx.say(f"{p['name']}: needs both repo and path in config")
        return False
    if not os.path.isdir(os.path.join(p["path"], ".git")):
        ctx.say(f"{p['name']}: {p['path']} not reachable (drive unmounted?) — skipping")
        return False
    return True


def main_tick(cfg, led, dry_run=False, hot_hold=True):
    lock = take_lock()
    if lock is None:
        print("mahler: another tick is running; exiting")
        return 0
    try:
        try:
            ctx = Ctx(cfg, led, dry_run=dry_run, hot_hold=hot_hold)
            tick(ctx)
            # Inspect the breaker before closing the ledger. CLI returns this code
            # to the stable launcher even when all tick passes survived.
            from .launch_health import tick_exit_code
            rc = tick_exit_code(led)
        finally:
            led.close()
        for line in ctx.lines:
            print(line)
        sys.stdout.flush()
        return rc
    finally:
        lock.close()
