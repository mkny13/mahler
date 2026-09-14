"""One tick: watchdog → GitHub sync → lease expiry → usage → schedule → labels.

Run every 60s by launchd (through launcher/mahler-launcher). An exclusive
flock makes overlapping ticks exit at once — agents outlive the tick that
launched them, exactly as in dispatch.

This module is the entry only: each pass it calls lives in its own module
(mahler#70) — watchdog.py, sync.py, finalize.py, ship.py, tick.py, usage.py.
"""

import fcntl
import os
import sys

from . import backup, config, digest, janitor, notify
from .gh import GH, GHError
from .ship import ship
from .sync import close_finished_parents, mirror_labels, sync
from .tick import expire, queue_maintenance, schedule
from .usage import compute_burst, refresh_usage
from .watchdog import watchdog


class Ctx:
    def __init__(self, cfg, led, dry_run=False, hot_hold=True):
        self.cfg, self.led, self.dry_run, self.hot_hold = cfg, led, dry_run, hot_hold
        self.lines = []
        self._gh = {}
        self._labels = {}          # (project, number) -> labels from this tick's sync
        self.burst_lines = None    # D23: set by compute_burst during this tick

    def policy(self, project):
        return config.project_policy(self.cfg, project)

    def gh(self, project):
        pol = self.policy(project)
        repo = pol["repo"]
        if repo not in self._gh:
            self._gh[repo] = GH(repo, env=config.run_env(self.cfg, config.gh_account_of(pol)))
        return self._gh[repo]

    def say(self, msg):
        self.lines.append(msg)

    def url(self, project, number):
        return f"https://github.com/{self.policy(project)['repo']}/issues/{number}"

    def ping(self, title, message="", project=None, number=None, priority="default", tags=""):
        if self.dry_run:
            return
        notify.send(self.cfg, title, message,
                    click=self.url(project, number) if project else None,
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
    projects = [p for p in config.enabled_projects(ctx.cfg) if _project_ok(ctx, p)]
    compute_burst(ctx, projects)    # D23: before watchdog so running runs
    watchdog(ctx)                   #   see burst lines too
    for p in projects:
        try:
            sync(ctx, p["name"])
        except GHError as e:
            ctx.say(f"{p['name']}: GitHub sync failed — {e}")
    expire(ctx)
    close_finished_parents(ctx, projects)
    if ctx.led.paused():
        ctx.say("paused — not starting anything (mahler resume)")
    else:
        refresh_usage(ctx, projects)
        queue_maintenance(ctx, projects)
        schedule(ctx, projects)
        ship(ctx, projects)
    for p in projects:
        mirror_labels(ctx, p["name"])
    if not ctx.dry_run:
        for p in config.enabled_projects(ctx.cfg):     # backups run even while paused
            for spec in p.get("backups") or []:
                backup.run(ctx, p["name"], spec)
    digest.maybe_send(ctx)                  # informational: also runs while paused
    janitor.maybe_run(ctx)                  # daily sweep (mahler#7): also while paused
    return ctx.lines


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
    ctx = Ctx(cfg, led, dry_run=dry_run, hot_hold=hot_hold)
    try:
        tick(ctx)
    finally:
        led.close()
    for line in ctx.lines:
        print(line)
    sys.stdout.flush()
    return 0
