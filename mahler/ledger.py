"""The ledger: Mahler's execution state (DESIGN D5, D6).

GitHub owns item *content*; this SQLite file owns who holds what right now,
runs, quota samples and the audit log. It is the only lock authority, which is
the one property GitHub labels, lock files and flock can't provide: an atomic
compare-and-set claim whose record outlives the process that took it.

Lease rules (D6), all enforced inside one IMMEDIATE transaction:
  * a live lease held by someone else blocks a claim, except that an
    interactive claim pre-empts an autonomous one (you win) — the displaced
    run is flagged to yield;
  * interactive vs interactive requires an explicit steal;
  * auto vs auto never succeeds (so double assignment is impossible);
  * every grant bumps the item's epoch, a fencing token: a run holding an old
    epoch fails lease_check and must not push or merge.
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    project          TEXT NOT NULL,
    number           INTEGER NOT NULL,
    title            TEXT,
    state            TEXT NOT NULL DEFAULT 'inbox',
    labels           TEXT NOT NULL DEFAULT '[]',
    priority         INTEGER NOT NULL DEFAULT 2,
    depends          TEXT NOT NULL DEFAULT '[]',
    pin              TEXT,
    branch           TEXT,
    pr               INTEGER,             -- the PR the conductor opened (D18)
    summary          TEXT,                -- the agent's one-line DONE summary
    attempts         INTEGER NOT NULL DEFAULT 0,
    setup_fails      INTEGER NOT NULL DEFAULT 0,
    epoch            INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT,
    sorted_at        TEXT,
    state_changed_at TEXT,
    last_comment_at  TEXT,
    mirror           TEXT,
    PRIMARY KEY (project, number)
);
CREATE TABLE IF NOT EXISTS leases (
    project      TEXT NOT NULL,
    number       INTEGER NOT NULL,
    holder       TEXT NOT NULL,
    kind         TEXT NOT NULL,          -- auto | interactive | primary
    platform     TEXT,
    epoch        INTEGER NOT NULL,
    run_id       INTEGER,
    acquired_at  TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    PRIMARY KEY (project, number)
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    project     TEXT NOT NULL,
    number      INTEGER NOT NULL,
    role        TEXT NOT NULL,           -- sort | build
    platform    TEXT NOT NULL,
    epoch       INTEGER NOT NULL,
    pid         INTEGER,
    worktree    TEXT,
    branch      TEXT,
    base_ref    TEXT,
    log_path    TEXT,
    status_path TEXT,
    status      TEXT NOT NULL,           -- running | stopping | ended
    stop_reason TEXT,                    -- quota | preempted | hung | timeout | closed | parked
    outcome     TEXT,
    exit_code   INTEGER,
    yield_at    TEXT,
    nudged      INTEGER NOT NULL DEFAULT 0,
    started_at  TEXT NOT NULL,
    ended_at    TEXT
);
CREATE TABLE IF NOT EXISTS usage (
    platform   TEXT NOT NULL,
    window     TEXT NOT NULL,            -- 5h | weekly
    used_pct   REAL NOT NULL,
    resets_at  TEXT,
    sampled_at TEXT NOT NULL,
    PRIMARY KEY (platform, window)
);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY,
    at      TEXT NOT NULL,
    project TEXT,
    number  INTEGER,
    kind    TEXT NOT NULL,
    detail  TEXT
);
CREATE TABLE IF NOT EXISTS counters (
    project TEXT NOT NULL,
    name    TEXT NOT NULL,
    value   INTEGER NOT NULL,
    PRIMARY KEY (project, name)
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""

STATES = ("inbox", "ready", "working", "verifying", "needs_you", "parked", "failed",
          "parent", "done")


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def parse(s):
    if not s:
        return None
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Ledger:
    def __init__(self, path, clock=utcnow, thread_safe=False):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path), exist_ok=True)
        # thread_safe=True lets a server thread use a connection made on the
        # main thread (the status page does this); callers must then serialise
        # access around one connection, which mahler.serve does with a lock.
        self.path = path
        self.con = sqlite3.connect(path, isolation_level=None, timeout=10,
                                   check_same_thread=not thread_safe)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA busy_timeout=10000")
        self.con.executescript(SCHEMA)
        # columns added after the daemon's DB already existed
        cols = {r["name"] for r in self.con.execute("PRAGMA table_info(items)")}
        for col, ddl in (("pr", "INTEGER"), ("summary", "TEXT"), ("setup_fails", "INTEGER NOT NULL DEFAULT 0")):
            if col not in cols:
                self.con.execute(f"ALTER TABLE items ADD COLUMN {col} {ddl}")
        run_cols = {r["name"] for r in self.con.execute("PRAGMA table_info(runs)")}
        if "nudged" not in run_cols:
            self.con.execute("ALTER TABLE runs ADD COLUMN nudged INTEGER NOT NULL DEFAULT 0")
        # migrate legacy 'tracking' state to 'parent'
        self.con.execute("UPDATE items SET state = 'parent' WHERE state = 'tracking'")
        self.clock = clock

    def now(self):
        return self.clock()

    # ---------- plumbing ----------

    def _tx(self):
        return _Tx(self.con)

    def q(self, sql, args=()):
        return self.con.execute(sql, args).fetchall()

    def q1(self, sql, args=()):
        return self.con.execute(sql, args).fetchone()

    def event(self, kind, project=None, number=None, detail=None):
        self.con.execute(
            "INSERT INTO events (at, project, number, kind, detail) VALUES (?,?,?,?,?)",
            (iso(self.now()), project, number, kind,
             detail if isinstance(detail, str) or detail is None else json.dumps(detail)))

    # ---------- kv ----------

    def get_kv(self, key, default=None):
        r = self.q1("SELECT value FROM kv WHERE key=?", (key,))
        return r["value"] if r else default

    def set_kv(self, key, value):
        self.con.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)", (key, value))

    def paused(self):
        return self.get_kv("paused") == "1"

    # ---------- items ----------

    def item(self, project, number):
        return self.q1("SELECT * FROM items WHERE project=? AND number=?", (project, number))

    def items(self, project=None, states=None):
        sql, args = "SELECT * FROM items WHERE 1=1", []
        if project:
            sql += " AND project=?"
            args.append(project)
        if states:
            sql += f" AND state IN ({','.join('?' * len(states))})"
            args.extend(states)
        return self.q(sql + " ORDER BY priority, number", args)

    def upsert_item(self, project, number, **fields):
        cur = self.item(project, number)
        if cur is None:
            fields.setdefault("state", "inbox")
            fields.setdefault("state_changed_at", iso(self.now()))
            cols = ["project", "number", *fields]
            self.con.execute(
                f"INSERT INTO items ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                (project, number, *fields.values()))
        elif fields:
            sets = ",".join(f"{k}=?" for k in fields)
            self.con.execute(f"UPDATE items SET {sets} WHERE project=? AND number=?",
                             (*fields.values(), project, number))
        return self.item(project, number)

    def set_state(self, project, number, state, why=None, **extra):
        assert state in STATES, state
        cur = self.item(project, number)
        if cur is not None and cur["state"] == state and not extra:
            return cur
        self.upsert_item(project, number, state=state,
                         state_changed_at=iso(self.now()), **extra)
        self.event("state", project, number,
                   f"{cur['state'] if cur else None} -> {state}" + (f" ({why})" if why else ""))
        return self.item(project, number)

    # ---------- leases ----------

    def lease(self, project, number, live_only=True):
        r = self.q1("SELECT * FROM leases WHERE project=? AND number=?", (project, number))
        if r and live_only and parse(r["expires_at"]) <= self.now():
            return None
        return r

    def claim(self, project, number, holder, kind, ttl_minutes, platform=None,
              run_id=None, steal=False):
        """Atomically take (or renew) the lease on an item.

        Returns (lease_row, info) on success, (None, info) when refused.
        info carries 'held_by' on refusal and 'preempted' (the displaced
        lease row) when an interactive claim displaced an autonomous run.
        """
        now = self.now()
        with self._tx():
            cur = self.q1("SELECT * FROM leases WHERE project=? AND number=?",
                          (project, number))
            live = cur is not None and parse(cur["expires_at"]) > now
            info = {}
            if live and cur["holder"] == holder:
                self.con.execute(
                    "UPDATE leases SET heartbeat_at=?, expires_at=? WHERE project=? AND number=?",
                    (iso(now), iso(now + timedelta(minutes=ttl_minutes)), project, number))
                info["renewed"] = True
                return self.lease(project, number, live_only=False), info
            if live:
                human = kind in ("interactive", "primary")
                if cur["kind"] == "auto" and human:
                    info["preempted"] = dict(cur)
                    if cur["run_id"]:
                        self.con.execute("UPDATE runs SET yield_at=? WHERE id=? AND yield_at IS NULL",
                                         (iso(now), cur["run_id"]))
                elif cur["kind"] != "auto" and human and steal:
                    info["stolen_from"] = dict(cur)
                else:
                    info["held_by"] = dict(cur)
                    return None, info
            item = self.item(project, number)
            if item is None:
                self.upsert_item(project, number)
                item = self.item(project, number)
            epoch = item["epoch"] + 1
            self.con.execute("UPDATE items SET epoch=? WHERE project=? AND number=?",
                             (epoch, project, number))
            self.con.execute(
                "INSERT OR REPLACE INTO leases (project, number, holder, kind, platform, epoch,"
                " run_id, acquired_at, heartbeat_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (project, number, holder, kind, platform, epoch, run_id, iso(now), iso(now),
                 iso(now + timedelta(minutes=ttl_minutes))))
            self.event("lease", project, number,
                       {"holder": holder, "kind": kind, "platform": platform, "epoch": epoch,
                        **({"preempted": info["preempted"]["holder"]} if "preempted" in info else {})})
        return self.lease(project, number, live_only=False), info

    def attach_run(self, project, number, epoch, run_id):
        self.con.execute("UPDATE leases SET run_id=? WHERE project=? AND number=? AND epoch=?",
                         (run_id, project, number, epoch))

    def heartbeat(self, project, number, holder, epoch, ttl_minutes):
        """Extend a lease we still hold. False means it was taken over or reaped."""
        now = self.now()
        cur = self.con.execute(
            "UPDATE leases SET heartbeat_at=?, expires_at=? "
            "WHERE project=? AND number=? AND holder=? AND epoch=?",
            (iso(now), iso(now + timedelta(minutes=ttl_minutes)), project, number, holder, epoch))
        return cur.rowcount == 1

    def release(self, project, number, holder=None, epoch=None):
        sql, args = "DELETE FROM leases WHERE project=? AND number=?", [project, number]
        if holder is not None:
            sql += " AND holder=?"
            args.append(holder)
        if epoch is not None:
            sql += " AND epoch=?"
            args.append(epoch)
        n = self.con.execute(sql, args).rowcount
        if n:
            self.event("release", project, number, {"holder": holder, "epoch": epoch})
        return n == 1

    def lease_check(self, project, number, epoch):
        """Fencing check: is `epoch` still the live lease on this item?"""
        cur = self.lease(project, number)
        return cur is not None and cur["epoch"] == int(epoch)

    def expired_leases(self):
        now = self.now()
        return [r for r in self.q("SELECT * FROM leases") if parse(r["expires_at"]) <= now]

    # ---------- runs ----------

    def create_run(self, **cols):
        cols.setdefault("status", "running")
        cols.setdefault("started_at", iso(self.now()))
        keys = list(cols)
        cur = self.con.execute(
            f"INSERT INTO runs ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
            tuple(cols.values()))
        return cur.lastrowid

    def update_run(self, run_id, **cols):
        sets = ",".join(f"{k}=?" for k in cols)
        self.con.execute(f"UPDATE runs SET {sets} WHERE id=?", (*cols.values(), run_id))

    def run(self, run_id):
        return self.q1("SELECT * FROM runs WHERE id=?", (run_id,))

    def active_runs(self, project=None):
        sql = "SELECT * FROM runs WHERE status IN ('running','stopping')"
        args = ()
        if project:
            sql += " AND project=?"
            args = (project,)
        return self.q(sql, args)

    # ---------- usage ----------

    def record_usage(self, platform, window, used_pct, resets_at=None, sampled_at=None):
        self.con.execute(
            "INSERT OR REPLACE INTO usage (platform, window, used_pct, resets_at, sampled_at)"
            " VALUES (?,?,?,?,?)",
            (platform, window, float(used_pct), resets_at, sampled_at or iso(self.now())))

    def usage(self, platform):
        return {r["window"]: dict(r) for r in
                self.q("SELECT * FROM usage WHERE platform=?", (platform,))}

    def estimates(self):
        rows = self.q("""
            SELECT platform, role, count(*) as c, 
                   avg((julianday(ended_at) - julianday(started_at))*24*60) as avg_mins
            FROM runs WHERE ended_at IS NOT NULL AND status='ended' GROUP BY platform, role
        """)
        plat_rows = self.q("""
            SELECT platform, count(*) as c, avg((julianday(ended_at) - julianday(started_at))*24*60) as avg_mins
            FROM runs WHERE ended_at IS NOT NULL AND status='ended' GROUP BY platform
        """)
        global_row = self.q1("""
            SELECT avg((julianday(ended_at) - julianday(started_at))*24*60) as avg_mins
            FROM runs WHERE ended_at IS NOT NULL AND status='ended'
        """)
        global_avg = global_row["avg_mins"] if (global_row and global_row["avg_mins"]) else 15.0

        by_pr = {(r["platform"], r["role"]): r["avg_mins"] for r in rows if r["c"] >= 3}
        by_p = {r["platform"]: r["avg_mins"] for r in plat_rows if r["c"] >= 3}

        issue_avg = self.q("""
            SELECT items.project, avg(t.issue_mins) as avg_mins
            FROM items
            JOIN (
                SELECT project, number, sum((julianday(ended_at) - julianday(started_at))*24*60) as issue_mins
                FROM runs WHERE ended_at IS NOT NULL AND status='ended' GROUP BY project, number
            ) t ON items.project = t.project AND items.number = t.number
            WHERE items.state = 'done' GROUP BY items.project
        """)
        proj_issue_avg = {r["project"]: r["avg_mins"] for r in issue_avg}
        
        global_issue_row = self.q1("""
            SELECT avg(t.issue_mins) as avg_mins
            FROM items
            JOIN (
                SELECT project, number, sum((julianday(ended_at) - julianday(started_at))*24*60) as issue_mins
                FROM runs WHERE ended_at IS NOT NULL AND status='ended' GROUP BY project, number
            ) t ON items.project = t.project AND items.number = t.number
            WHERE items.state = 'done'
        """)
        global_issue = global_issue_row["avg_mins"] if (global_issue_row and global_issue_row["avg_mins"]) else 30.0

        return {
            "run_avg": by_pr,
            "plat_avg": by_p,
            "global_run_avg": global_avg,
            "proj_issue_avg": proj_issue_avg,
            "global_issue_avg": global_issue,
        }

    def run_estimate(self, ests, platform, role):
        return ests["run_avg"].get((platform, role)) or ests["plat_avg"].get(platform) or ests["global_run_avg"]

    def issue_estimate(self, ests, project):
        return ests["proj_issue_avg"].get(project) or ests["global_issue_avg"]


    # ---------- setup failures (issue #8) ----------

    def bump_setup_fails(self, project, number):
        """Count one more consecutive setup failure (exit 97); return the new count."""
        with self._tx():
            self.con.execute(
                "UPDATE items SET setup_fails=setup_fails+1 WHERE project=? AND number=?",
                (project, number))
        r = self.item(project, number)
        return r["setup_fails"] if r else 0

    def reset_setup_fails(self, project, number):
        """Setup succeeded (or you said go) — the consecutive count starts over."""
        self.con.execute(
            "UPDATE items SET setup_fails=0 WHERE project=? AND number=? AND setup_fails<>0",
            (project, number))

    # ---------- counters ----------

    def next_id(self, project, name, floor=0):
        """Atomically hand out the next number in a per-project sequence —
        e.g. phish-in-app's Dnnn decision IDs, which parallel branches used to
        collide on (DESIGN D6). `floor` seeds it past numbers already in use."""
        with self._tx():
            r = self.q1("SELECT value FROM counters WHERE project=? AND name=?", (project, name))
            value = max((r["value"] if r else 0), floor) + 1
            self.con.execute("INSERT OR REPLACE INTO counters (project, name, value) VALUES (?,?,?)",
                             (project, name, value))
        return value


class _Tx:
    def __init__(self, con):
        self.con = con

    def __enter__(self):
        self.con.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, *_):
        self.con.execute("ROLLBACK" if exc_type else "COMMIT")
        return False
