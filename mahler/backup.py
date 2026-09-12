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
"""

import os
import re
import shutil
import subprocess
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlparse

ROOT = "/Volumes/ExtSSD160/mahler-backups"
STAMP = "%Y%m%d-%H%M%S"
FILE_RE = re.compile(r"^(?P<name>.+)-(?P<stamp>\d{8}-\d{6})\.dump$")


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


def prune(project, name, root=ROOT, **policy):
    dest = os.path.join(root, project)
    if not os.path.isdir(dest):
        return []
    files = {}
    for f in os.listdir(dest):
        m = FILE_RE.match(f)
        if m and m.group("name") == name:
            files[datetime.strptime(m.group("stamp"), STAMP)] = os.path.join(dest, f)
    keep = keep_set(files, **policy)
    removed = []
    for stamp, path in files.items():
        if stamp not in keep:
            _rm(path)
            removed.append(path)
    return removed


def due(led, project, spec, now=None):
    """Once a day, after spec['hour'] (default 3) local time."""
    now = now or datetime.now()
    if now.hour < spec.get("hour", 3):
        return False
    failed = led.get_kv(f"backup:{project}:{spec['name']}:failed_at")
    if failed and (now - datetime.fromisoformat(failed)).total_seconds() < 3600:
        return False                      # retry hourly, not every tick (and ping)
    last = led.get_kv(f"backup:{project}:{spec['name']}")
    return not last or datetime.fromisoformat(last) < now.replace(
        hour=spec.get("hour", 3), minute=0, second=0, microsecond=0)


def run(ctx, project, spec, force=False):
    """Back up one store if due (or forced). Pings on failure. -> result or None."""
    led = ctx.led
    if not force and not due(led, project, spec):
        return None
    key = f"backup:{project}:{spec['name']}"
    try:
        if spec.get("kind", "postgres") != "postgres":
            raise BackupError(f"unsupported kind {spec.get('kind')!r}")
        res = backup_postgres(project, spec, root=spec.get("root", ROOT))
        removed = prune(project, spec["name"], root=spec.get("root", ROOT))
    except (BackupError, OSError, subprocess.SubprocessError, KeyError) as e:
        led.set_kv(f"{key}:failed_at", datetime.now().isoformat())
        led.event("backup_failed", project, None, f"{spec.get('name')}: {e}"[:500])
        ctx.say(f"{project}: backup {spec.get('name')} FAILED — {e}")
        ctx.ping(f"Backup failed — {project} {spec.get('name')}", str(e)[:300],
                 priority="high", tags="warning")
        return None
    led.set_kv(key, datetime.now().isoformat())
    led.event("backup", project, None,
              f"{spec['name']}: {res['bytes']} bytes, {res['entries']} entries, pruned {len(removed)}")
    ctx.say(f"{project}: backed up {spec['name']} ({res['bytes'] // 1024} KB, {res['entries']} entries)")
    return res
