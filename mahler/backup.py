"""Database backups (DESIGN D12) — a Mahler job, never an agent.

Each project may declare backups in ~/.mahler/config.toml:

    [[projects.groundwork.backups]]
    name = "neon-prod"
    kind = "postgres"
    env_file = "/Volumes/ExtSSD160/scripts/groundwork/.env.vercel-prod"
    url_var = "DATABASE_URL_UNPOOLED"

Once a day (first tick after `hour`, default 03:00 local) each one is dumped
with pg_dump --format=custom, checked with `pg_restore --list`, and pruned to
14 daily / 8 weekly / 12 monthly. The connection string is read from the env
file at run time, handed to pg_dump as PG* environment variables (never on a
command line other processes can see), and scrubbed from any error text. Dumps are
medical data: files are 0600 in 0700 directories, on the SSD that Time Machine
and Backblaze both cover.

Mahler's own ledger (mahler#533) gets the same daily treatment, from the tick
itself rather than a project spec: `sqlite3.Connection.backup()` (safe to run
against the live WAL database) into ~/.mahler/backups/mahler/, verified with
`PRAGMA integrity_check` on the copy, pruned to the same 14/8/12 policy, and
optionally mirrored to an off-disk `[backup] copy_to` folder. `mahler
restore-ledger` (cli.py) reverses it.
"""

import os
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlparse

from . import config

ROOT = "/Volumes/ExtSSD160/mahler-backups"
STAMP = "%Y%m%d-%H%M%S"
FILE_RE = re.compile(r"^(?P<name>.+)-(?P<stamp>\d{8}-\d{6})\.dump$")

LEDGER_ROOT = os.path.join(config.STATE, "backups", "mahler")
LEDGER_FILE_RE = re.compile(r"^mahler-(?P<stamp>\d{8}-\d{6})\.db$")


class BackupError(RuntimeError):
    pass


def load_env(path):
    env = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
            if m:
                env[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return env


def tool(name, spec):
    """Prefer an explicit path, then Homebrew's libpq (current client), then PATH."""
    for p in (spec.get(name), f"/opt/homebrew/opt/libpq/bin/{name}", shutil.which(name)):
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    raise BackupError(f"{name} not found")


def libpq_env(url):
    """postgres://user:pw@host:port/db?sslmode=… -> PG* environment variables, so the
    password never appears in a process's argument list."""
    u = urlparse(url)
    if u.scheme not in ("postgres", "postgresql"):
        raise BackupError("connection string is not a postgres:// URL")
    env = {"PGHOST": u.hostname or "", "PGPORT": str(u.port or 5432),
           "PGUSER": unquote(u.username or ""), "PGPASSWORD": unquote(u.password or ""),
           "PGDATABASE": unquote(u.path.lstrip("/"))}
    q = parse_qs(u.query)
    for param, var in (("sslmode", "PGSSLMODE"), ("channel_binding", "PGCHANNELBINDING"),
                       ("options", "PGOPTIONS")):
        if q.get(param):
            env[var] = q[param][0]
    return {k: v for k, v in env.items() if v}


def _scrub(text, secret):
    text = text or ""
    if secret:
        text = text.replace(secret, "<url>")
    return re.sub(r"postgres(?:ql)?://\S+", "<url>", text)


def backup_postgres(project, spec, root=ROOT, now=None):
    """-> {'path', 'bytes', 'entries'}; raises BackupError."""
    now = now or datetime.now()
    env = load_env(spec["env_file"])
    url = env.get(spec.get("url_var", "DATABASE_URL"))
    if not url:
        raise BackupError(f"{spec.get('url_var', 'DATABASE_URL')} not set in {spec['env_file']}")
    dest = os.path.join(root, project)
    os.makedirs(dest, mode=0o700, exist_ok=True)
    # makedirs won't tighten a directory that already exists too loose (D12:
    # dumps are 0600 in 0700 directories).
    os.chmod(dest, 0o700)
    final = os.path.join(dest, f"{spec['name']}-{now.strftime(STAMP)}.dump")
    tmp = final + ".partial"
    run_env = dict(os.environ, PGCONNECT_TIMEOUT="20", **libpq_env(url))
    old_umask = os.umask(0o077)
    try:
        r = subprocess.run([tool("pg_dump", spec), "--format=custom", "--no-owner",
                            "--no-privileges", "--file", tmp],
                           capture_output=True, text=True, timeout=1800, env=run_env)
    finally:
        os.umask(old_umask)
    if r.returncode != 0 or not os.path.exists(tmp):
        _rm(tmp)
        raise BackupError(f"pg_dump failed: {_scrub(r.stderr, url).strip()[-400:]}")
    listing = subprocess.run([tool("pg_restore", spec), "--list", tmp],
                             capture_output=True, text=True, timeout=300)
    entries = len([l for l in listing.stdout.splitlines() if l and not l.startswith(";")])
    if listing.returncode != 0 or entries == 0:
        _rm(tmp)
        raise BackupError(f"dump unreadable: {_scrub(listing.stderr, url).strip()[-300:]}")
    os.replace(tmp, final)
    os.chmod(final, 0o600)
    return {"path": final, "bytes": os.path.getsize(final), "entries": entries}


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


def keep_set(stamps, daily=14, weekly=8, monthly=12):
    """Which backup timestamps to keep: newest per day for `daily` days, newest
    per ISO week for `weekly` weeks, newest per month for `monthly` months."""
    stamps = sorted(stamps, reverse=True)
    keep = set()
    for key, limit in ((lambda d: d.date(), daily),
                       (lambda d: d.isocalendar()[:2], weekly),
                       (lambda d: (d.year, d.month), monthly)):
        seen = []
        for s in stamps:
            k = key(s)
            if k not in seen:
                seen.append(k)
                if len(seen) > limit:
                    break
                keep.add(s)
    return keep


def _prune_files(files, **policy):
    """files: {stamp: path}. Deletes whatever keep_set() doesn't keep. -> removed paths."""
    keep = keep_set(files, **policy)
    removed = []
    for stamp, path in files.items():
        if stamp not in keep:
            _rm(path)
            removed.append(path)
    return removed


def prune(project, name, root=ROOT, **policy):
    dest = os.path.join(root, project)
    if not os.path.isdir(dest):
        return []
    files = {}
    for f in os.listdir(dest):
        m = FILE_RE.match(f)
        if m and m.group("name") == name:
            files[datetime.strptime(m.group("stamp"), STAMP)] = os.path.join(dest, f)
    return _prune_files(files, **policy)


def due(led, project, spec, now=None):
    """Once a day, after spec['hour'] (default 3) local time."""
    now = now or datetime.now()
    name = spec.get("name") or "<unnamed>"
    failed = led.get_kv(f"backup:{project}:{name}:failed_at")
    if failed and (now - datetime.fromisoformat(failed)).total_seconds() < 3600:
        return False                      # retry hourly, not every tick (and ping)
    if now.hour < spec.get("hour", 3):
        return False
    last = led.get_kv(f"backup:{project}:{spec['name']}")
    return not last or datetime.fromisoformat(last) < now.replace(
        hour=spec.get("hour", 3), minute=0, second=0, microsecond=0)


def run(ctx, project, spec, force=False):
    """Back up one store if due (or forced). Pings on failure. -> result or None."""
    led = ctx.led
    # Non-table entries follow the same unnamed failure/backoff path.
    spec = spec if isinstance(spec, dict) else {}
    name = spec.get("name") or "<unnamed>"
    key = f"backup:{project}:{name}"
    try:
        if not force and not due(led, project, spec):
            return None
        if not isinstance(spec.get("name"), str) or not spec["name"]:
            raise BackupError("backup name must be a non-empty string")
        if spec.get("kind", "postgres") != "postgres":
            raise BackupError(f"unsupported kind {spec.get('kind')!r}")
        res = backup_postgres(project, spec, root=spec.get("root", ROOT))
        removed = prune(project, spec["name"], root=spec.get("root", ROOT))
    except (BackupError, OSError, subprocess.SubprocessError, KeyError,
            ValueError, TypeError, AttributeError, OverflowError) as e:
        led.set_kv(f"{key}:failed_at", datetime.now().isoformat())
        led.event("backup_failed", project, None, f"{name}: {e}"[:500])
        ctx.say(f"{project}: backup {name} FAILED — {e}")
        ctx.ping(f"Backup failed — {project} {name}", str(e)[:300],
                 priority="high", tags="warning")
        return None
    led.set_kv(key, datetime.now().isoformat())
    led.event("backup", project, None,
              f"{spec['name']}: {res['bytes']} bytes, {res['entries']} entries, pruned {len(removed)}")
    ctx.say(f"{project}: backed up {spec['name']} ({res['bytes'] // 1024} KB, {res['entries']} entries)")
    return res


# ---------- Mahler's own ledger (mahler#533) ----------

def integrity_check(path):
    """PRAGMA integrity_check on a standalone sqlite file. -> 'ok' or the first
    problem line. A separate connection, so it never disturbs a live handle."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        row = con.execute("PRAGMA integrity_check").fetchone()
        return row[0] if row else "no result"
    finally:
        con.close()


def backup_ledger(led_path, root=None, now=None):
    """Online copy of the live ledger via sqlite3.Connection.backup() (safe
    while the daemon writes it), verified with integrity_check() on the copy.
    -> {'path', 'bytes'}; raises BackupError."""
    if led_path == ":memory:" or not os.path.isfile(led_path):
        raise BackupError(f"no ledger file at {led_path!r}")
    now = now or datetime.now()
    root = root or LEDGER_ROOT
    os.makedirs(root, mode=0o700, exist_ok=True)
    # makedirs won't tighten a directory that already exists too loose (D12).
    os.chmod(root, 0o700)
    final = os.path.join(root, f"mahler-{now.strftime(STAMP)}.db")
    tmp = final + ".partial"
    old_umask = os.umask(0o077)
    try:
        src = sqlite3.connect(led_path, timeout=10)
        try:
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    finally:
        os.umask(old_umask)
    result = integrity_check(tmp)
    if result != "ok":
        _rm(tmp)
        raise BackupError(f"integrity check failed: {result}")
    os.replace(tmp, final)
    os.chmod(final, 0o600)
    return {"path": final, "bytes": os.path.getsize(final)}


def prune_ledger(root=None, **policy):
    root = root or LEDGER_ROOT
    if not os.path.isdir(root):
        return []
    files = {}
    for f in os.listdir(root):
        m = LEDGER_FILE_RE.match(f)
        if m:
            files[datetime.strptime(m.group("stamp"), STAMP)] = os.path.join(root, f)
    return _prune_files(files, **policy)


def optimize_and_checkpoint(led):
    """Cheap daily upkeep on the live ledger: refresh the query planner's
    stats and fold the WAL back into the main file so it doesn't grow
    unbounded. Run only after a successful backup, on the live connection."""
    led.con.execute("PRAGMA optimize")
    led.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def copy_off_disk(path, copy_to):
    """Mirror the just-verified backup to an off-mini folder (e.g. iCloud or
    Google Drive). Raises OSError on failure — the caller pings, but a failed
    mirror never invalidates the on-disk backup that already passed
    integrity_check."""
    os.makedirs(copy_to, exist_ok=True)
    shutil.copy2(path, os.path.join(copy_to, os.path.basename(path)))


def run_ledger(ctx, led_path=None, root=None, force=False):
    """Back up Mahler's own ledger if due (or forced). Never raises — the
    daemon must never break itself (DESIGN, cli.py's project rules).
    -> result or None."""
    led = ctx.led
    led_path = led_path or led.path
    pol = ctx.cfg.get("backup") or {}
    spec = {"name": "ledger", "hour": pol.get("hour", 3)}
    key = "backup:mahler:ledger"
    copy_err = None
    try:
        if not force and not due(led, "mahler", spec):
            return None
        res = backup_ledger(led_path, root=root)
        removed = prune_ledger(root=root)
        optimize_and_checkpoint(led)
        copy_to = pol.get("copy_to")
        if copy_to:
            try:
                copy_off_disk(res["path"], copy_to)
            except OSError as e:
                copy_err = e
    except (BackupError, OSError, sqlite3.Error) as e:
        led.set_kv(f"{key}:failed_at", datetime.now().isoformat())
        led.event("backup_failed", "mahler", None, f"ledger: {e}"[:500])
        ctx.say(f"mahler: ledger backup FAILED — {e}")
        ctx.ping("Backup failed — mahler ledger", str(e)[:300],
                 priority="high", tags="warning")
        return None
    led.set_kv(key, datetime.now().isoformat())
    led.set_kv(f"{key}:bytes", str(res["bytes"]))
    led.event("backup", "mahler", None, f"ledger: {res['bytes']} bytes, pruned {len(removed)}")
    ctx.say(f"mahler: backed up ledger ({res['bytes'] // 1024} KB, pruned {len(removed)})")
    if copy_err is not None:
        ctx.say(f"mahler: ledger off-disk copy failed — {copy_err}")
        ctx.ping("Ledger off-disk copy failed", str(copy_err)[:300],
                 priority="default", tags="warning")
    return res
