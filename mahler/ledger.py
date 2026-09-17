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

import itertools
import json
import os
import re
import socket
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone

from .config import MAINTENANCE_PASSES, ensure_private_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    project          TEXT NOT NULL,
    number           INTEGER NOT NULL,
    title            TEXT,
    state            TEXT NOT NULL DEFAULT 'inbox',
    labels           TEXT NOT NULL DEFAULT '[]',
    priority         INTEGER NOT NULL DEFAULT 2,
    depends          TEXT NOT NULL DEFAULT '[]',
    files            TEXT NOT NULL DEFAULT '[]',   -- `## Plan` Files: list (mahler#210)
    pin              TEXT,
    branch           TEXT,
    pr               INTEGER,             -- the PR the conductor opened (D18)
    summary          TEXT,                -- the agent's one-line DONE summary
    question         TEXT,                -- the NEEDS-YOU question, sans OPTIONS (mahler#248)
    options          TEXT NOT NULL DEFAULT '[]',  -- its answer choices, JSON list (mahler#248)
    attempts         INTEGER NOT NULL DEFAULT 0,
    setup_fails      INTEGER NOT NULL DEFAULT 0,
    esc_tier         INTEGER NOT NULL DEFAULT 0,
    esc_fails        INTEGER NOT NULL DEFAULT 0,
    epoch            INTEGER NOT NULL DEFAULT 0,
    parent           INTEGER,             -- parent issue number if part of a sub-issue
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
    capacity     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (project, number)
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    project     TEXT NOT NULL,
    number      INTEGER NOT NULL,
    role        TEXT NOT NULL,           -- sort | build
    platform    TEXT NOT NULL,
    size        TEXT,                    -- s | m | l at launch, from effective_size (mahler#207)
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
    model       TEXT,                    -- the modelID a stateless route actually used (mahler#141)
    est_mins    REAL,                    -- predicted duration at launch (mahler#59)
    actual_mins REAL,                    -- actual duration on completion (mahler#59)
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
CREATE TABLE IF NOT EXISTS console_actions (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, project TEXT, number INTEGER,
    payload TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
    due_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
    done_at TEXT, result TEXT
);
CREATE INDEX IF NOT EXISTS idx_console_actions_due ON console_actions(status, due_at);
CREATE TABLE IF NOT EXISTS uat (
    project     TEXT NOT NULL,
    number      INTEGER NOT NULL,
    pr          INTEGER,                -- the PR the change shipped in (D18)
    sha         TEXT,                   -- the merge commit
    title       TEXT,
    needs       TEXT,                   -- the 'Needs a human to check' list, verbatim
    shipped_at  TEXT,
    verdict     TEXT,                   -- pass | fail, once you've checked
    verdict_at  TEXT,
    bug         INTEGER,                -- the p1 bug a fail filed
    note        TEXT,                   -- the fail's note
    PRIMARY KEY (project, number)
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS releases (
    id             INTEGER PRIMARY KEY,
    project        TEXT NOT NULL,
    version        TEXT NOT NULL,
    checkpoint_sha TEXT,
    state          TEXT NOT NULL DEFAULT 'published',
    created_at     TEXT NOT NULL,
    published_at   TEXT,
    updated_at     TEXT,
    notes          TEXT NOT NULL DEFAULT '',
    remote_url     TEXT,
    UNIQUE (project, version)
);
CREATE TABLE IF NOT EXISTS release_items (
    project     TEXT NOT NULL,
    number      INTEGER NOT NULL,
    pr          INTEGER,
    title       TEXT,
    summary     TEXT,
    merge_sha   TEXT,
    labels      TEXT NOT NULL DEFAULT '[]',
    shipped_at  TEXT NOT NULL,
    release_id  INTEGER,
    PRIMARY KEY (project, number),
    FOREIGN KEY (release_id) REFERENCES releases(id)
);

-- Query indexes (issue #89): every column below is part of the original
-- schema, so these are safe on existing databases — executescript runs at
-- every Ledger init and IF NOT EXISTS makes it idempotent. Tables with a
-- composite PK (leases, usage, counters, kv) don't need more: their PK
-- index already serves every lookup they get.
CREATE INDEX IF NOT EXISTS idx_items_project_state
    ON items(project, state, priority, number);   -- items(project, states) ORDER BY priority
CREATE INDEX IF NOT EXISTS idx_items_state
    ON items(state, priority, number);            -- state-only filters (digest, done stats)
CREATE INDEX IF NOT EXISTS idx_runs_status_project
    ON runs(status, project);                     -- active_runs
CREATE INDEX IF NOT EXISTS idx_runs_item_status
    ON runs(project, number, status);             -- orphan checks' EXISTS subqueries
CREATE INDEX IF NOT EXISTS idx_runs_ended
    ON runs(status, ended_at);                    -- ended-run averages (run stats)
CREATE INDEX IF NOT EXISTS idx_events_kind_at
    ON events(kind, at);                          -- digest: kind + at >= cutoff
CREATE INDEX IF NOT EXISTS idx_events_item
    ON events(project, number);                   -- item-scoped event lookups
CREATE INDEX IF NOT EXISTS idx_releases_project
    ON releases(project, id);
CREATE INDEX IF NOT EXISTS idx_release_items_unreleased
    ON release_items(project, release_id);
CREATE INDEX IF NOT EXISTS idx_release_items_release
    ON release_items(release_id);
"""

STATES = ("inbox", "ready", "working", "verifying", "needs_you", "parked", "failed",
          "parent", "done")

CONDUCTOR = "conductor"          # the lease holder that ships (DESIGN D18)


def row_get(row, key, default=None):
    """A column off an item/run row, whatever the row type, never None."""
    if row is None:
        return default
    if hasattr(row, "get"):
        val = row.get(key, default)
        return default if val is None else val
    try:
        val = row[key]
        return default if val is None else val
    except (IndexError, KeyError):
        return default


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
            ensure_private_dir(os.path.dirname(path) or ".")
        # thread_safe=True lets a server thread use a connection made on the
        # main thread (the console server does this); callers must then serialise
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
        for col, ddl in (("pr", "INTEGER"), ("summary", "TEXT"),
                         ("setup_fails", "INTEGER NOT NULL DEFAULT 0"),
                         ("parent", "INTEGER"),
                         ("esc_tier", "INTEGER NOT NULL DEFAULT 0"),
                         ("esc_fails", "INTEGER NOT NULL DEFAULT 0"),
                         ("files", "TEXT NOT NULL DEFAULT '[]'"),
                         ("question", "TEXT"),
                         ("options", "TEXT NOT NULL DEFAULT '[]'")):
            if col not in cols:
                self.con.execute(f"ALTER TABLE items ADD COLUMN {col} {ddl}")
        run_cols = {r["name"] for r in self.con.execute("PRAGMA table_info(runs)")}
        if "nudged" not in run_cols:
            self.con.execute("ALTER TABLE runs ADD COLUMN nudged INTEGER NOT NULL DEFAULT 0")
        if "model" not in run_cols:
            self.con.execute("ALTER TABLE runs ADD COLUMN model TEXT")
        if "est_mins" not in run_cols:
            self.con.execute("ALTER TABLE runs ADD COLUMN est_mins REAL")
        if "actual_mins" not in run_cols:
            self.con.execute("ALTER TABLE runs ADD COLUMN actual_mins REAL")
        if "size" not in run_cols:
            self.con.execute("ALTER TABLE runs ADD COLUMN size TEXT")
        lease_cols = {r["name"] for r in self.con.execute("PRAGMA table_info(leases)")}
        if "capacity" not in lease_cols:
            self.con.execute("ALTER TABLE leases ADD COLUMN capacity INTEGER NOT NULL DEFAULT 1")
        rel_cols = {r["name"] for r in self.con.execute("PRAGMA table_info(releases)")}
        for col, ddl in (("checkpoint_sha", "TEXT"), ("state", "TEXT NOT NULL DEFAULT 'published'"),
                         ("created_at", "TEXT"), ("published_at", "TEXT"),
                         ("updated_at", "TEXT"), ("notes", "TEXT NOT NULL DEFAULT ''"),
                         ("remote_url", "TEXT")):
            if col not in rel_cols:
                self.con.execute(f"ALTER TABLE releases ADD COLUMN {col} {ddl}")
        item_rel_cols = {r["name"] for r in self.con.execute("PRAGMA table_info(release_items)")}
        for col, ddl in (("pr", "INTEGER"), ("title", "TEXT"), ("summary", "TEXT"),
                         ("merge_sha", "TEXT"), ("labels", "TEXT NOT NULL DEFAULT '[]'"),
                         ("shipped_at", "TEXT"), ("release_id", "INTEGER")):
            if col not in item_rel_cols:
                self.con.execute(f"ALTER TABLE release_items ADD COLUMN {col} {ddl}")
        # migrate legacy 'tracking' state to 'parent'
        self.con.execute("UPDATE items SET state = 'parent' WHERE state = 'tracking'")
        if path != ":memory:":
            # 0600 on the database and its WAL/SHM sidecars (issue #75): the
            # file is created umask-masked, so chmod explicitly — same pattern
            # as backup.py's dumps. Fixes a file created loose by an older
            # version too.
            for side in (path, path + "-wal", path + "-shm"):
                try:
                    os.chmod(side, 0o600)
                except OSError:
                    pass
        self.clock = clock

    def now(self):
        return self.clock()

    def close(self):
        """Close the DB handle. Idempotent (sqlite3 tolerates re-close).
        Call from a shutdown path only — never mid-tick: Ledger is the
        lease authority (DESIGN D6)."""
        con = getattr(self, "con", None)
        if con is not None:
            con.close()

    def __del__(self):
        # Safety net: the daemon/CLI close explicitly, but tests (and any
        # caller that drops a Ledger) must never leak the connection —
        # issue #68's ResourceWarning must stay quiet.
        try:
            self.close()
        except Exception:
            pass

    # ---------- console outbox (D27) ----------

    def queue_action(self, kind, project=None, number=None, payload=None, delay_seconds=0):
        now = self.now()
        return self.con.execute(
            "INSERT INTO console_actions(kind,project,number,payload,created_at,due_at) "
            "VALUES(?,?,?,?,?,?)", (kind, project, number, json.dumps(payload or {}),
                                    iso(now), iso(now + timedelta(seconds=delay_seconds)))).lastrowid

    def cancel_action(self, id):
        return bool(self.con.execute(
            "UPDATE console_actions SET status='cancelled',done_at=? "
            "WHERE id=? AND status='pending'", (iso(self.now()), id)).rowcount)

    def due_actions(self, limit=20):
        return self.q("SELECT * FROM console_actions WHERE status='pending' AND due_at<=? "
                      "ORDER BY due_at,id LIMIT ?", (iso(self.now()), limit))

    def pending_actions(self, kind=None):
        return self.q("SELECT * FROM console_actions WHERE status='pending'" +
                      (" AND kind=?" if kind is not None else "") + " ORDER BY created_at,id",
                      (kind,) if kind is not None else ())

    def finish_action(self, id, status, result=None):
        if status not in ("done", "failed", "cancelled", "skipped"):
            raise ValueError("invalid action status")
        self.con.execute("UPDATE console_actions SET status=?,done_at=?,result=? "
                         "WHERE id=? AND status='pending'",
                         (status, iso(self.now()), result, id))

    # ---------- the UAT queue (D10, D27) ----------

    def add_uat(self, project, number, pr, sha, title, needs):
        self.con.execute(
            "INSERT OR IGNORE INTO uat(project,number,pr,sha,title,needs,shipped_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (project, number, pr, sha, title, needs, iso(self.now())))

    def uat(self, project, number):
        return self.q1("SELECT * FROM uat WHERE project=? AND number=?", (project, number))

    def pending_uat(self):
        """Rows with no verdict yet, newest shipment first."""
        return self.q("SELECT * FROM uat WHERE verdict IS NULL "
                      "ORDER BY shipped_at DESC, number DESC")

    def set_uat_verdict(self, project, number, verdict, bug=None, note=None):
        """Record pass or fail exactly once: a row that already has a verdict
        stays as it is, so a double-tap can't overwrite a recorded one."""
        return bool(self.con.execute(
            "UPDATE uat SET verdict=?,verdict_at=?,bug=?,note=? "
            "WHERE project=? AND number=? AND verdict IS NULL",
            (verdict, iso(self.now()), bug, note, project, number)).rowcount)

    # ---------- releases and rolling draft (DESIGN D31) ----------

    def snapshot_release_item(self, project, number, pr=None, title=None,
                              summary=None, merge_sha=None, labels=None, shipped_at=None):
        """Snapshot a shipped issue into the unreleased draft.
        Idempotent: retries do not duplicate or error, and an item assigned
        to a completed release is never overwritten back to unreleased."""
        labels_json = labels if isinstance(labels, str) else json.dumps(labels or [])
        ts = shipped_at or iso(self.now())
        self.con.execute(
            "INSERT INTO release_items (project, number, pr, title, summary, merge_sha, labels, shipped_at, release_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL) "
            "ON CONFLICT(project, number) DO UPDATE SET "
            "pr=coalesce(excluded.pr, release_items.pr), "
            "title=coalesce(excluded.title, release_items.title), "
            "summary=coalesce(excluded.summary, release_items.summary), "
            "merge_sha=coalesce(excluded.merge_sha, release_items.merge_sha), "
            "labels=case when excluded.labels != '[]' then excluded.labels else release_items.labels end "
            "WHERE release_items.release_id IS NULL",
            (project, number, pr, title, summary, merge_sha, labels_json, ts))

    def unreleased_items(self, project):
        """Shipped items not yet included in any release, oldest shipment first."""
        return self.q("SELECT * FROM release_items WHERE project=? AND release_id IS NULL "
                      "ORDER BY shipped_at ASC, number ASC", (project,))

    def release_item(self, project, number):
        """Get a single release item by project and number."""
        return self.q1("SELECT * FROM release_items WHERE project=? AND number=?", (project, number))

    def create_release(self, project, version, checkpoint_sha=None, notes="",
                       state="published", published_at=None, remote_url=None,
                       item_numbers=None):
        """Create a release and assign unreleased items to it.
        If item_numbers is None, assigns all currently unreleased items for the project.
        Returns the new release row."""
        created_at = iso(self.now())
        pub_at = published_at if published_at is not None else (created_at if state == "published" else None)
        with self._tx():
            cur = self.con.execute(
                "INSERT INTO releases (project, version, checkpoint_sha, state, created_at, published_at, notes, remote_url) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (project, version, checkpoint_sha, state, created_at, pub_at, notes or "", remote_url))
            rel_id = cur.lastrowid
            if item_numbers is not None:
                if item_numbers:
                    placeholders = ",".join("?" for _ in item_numbers)
                    self.con.execute(
                        f"UPDATE release_items SET release_id=? WHERE project=? AND release_id IS NULL AND number IN ({placeholders})",
                        (rel_id, project, *item_numbers))
            else:
                self.con.execute(
                    "UPDATE release_items SET release_id=? WHERE project=? AND release_id IS NULL",
                    (rel_id, project))
        return self.get_release(project, release_id=rel_id)

    def get_release(self, project, version=None, release_id=None):
        if release_id is not None:
            return self.q1("SELECT * FROM releases WHERE id=?", (release_id,))
        if version is not None:
            return self.q1("SELECT * FROM releases WHERE project=? AND version=?", (project, version))
        return None

    def latest_release(self, project):
        return self.q1("SELECT * FROM releases WHERE project=? ORDER BY id DESC LIMIT 1", (project,))

    def list_releases(self, project):
        return self.q("SELECT * FROM releases WHERE project=? ORDER BY id DESC", (project,))

    def release_items_for_release(self, release_id):
        return self.q("SELECT * FROM release_items WHERE release_id=? ORDER BY shipped_at ASC, number ASC", (release_id,))

    # ---------- plumbing ----------

    def _tx(self):
        return _Tx(self.con)

    def q(self, sql, args=()):
        return self.con.execute(sql, args).fetchall()

    def q1(self, sql, args=()):
        return self.con.execute(sql, args).fetchone()

    def event(self, kind, project=None, number=None, detail=None, passes=None):
        selected_passes = passes
        self.con.execute(
            "INSERT INTO events (at, project, number, kind, detail) VALUES (?,?,?,?,?)",
            (iso(self.now()), project, number, kind,
             detail if isinstance(detail, str) or detail is None else json.dumps(detail)))
        if kind == "shipped" and project:
            self.record_shipped(project, MAINTENANCE_PASSES if selected_passes is None
                                else selected_passes)

    # ---------- maintenance checkpoints ----------

    @staticmethod
    def _maintenance_counter_name(pass_name):
        return f"maintenance:{pass_name}:merged_since"

    @staticmethod
    def _maintenance_timestamp_key(project, pass_name):
        return f"maintenance:{project}:{pass_name}:last_filed_at"

    def maintenance_checkpoint(self, project, pass_name):
        row = self.q1("SELECT value FROM counters WHERE project=? AND name=?",
                      (project, self._maintenance_counter_name(pass_name)))
        return {"last_filed_at": self.get_kv(
                    self._maintenance_timestamp_key(project, pass_name)),
                "merged_since": row["value"] if row else 0}

    def set_maintenance_checkpoint(self, project, pass_name, last_filed_at=None,
                                   merged_since=0):
        timestamp = (iso(self.now()) if last_filed_at is None
                     else last_filed_at if isinstance(last_filed_at, str)
                     else iso(last_filed_at))
        with self._tx():
            self.set_kv(self._maintenance_timestamp_key(project, pass_name), timestamp)
            self.con.execute(
                "INSERT OR REPLACE INTO counters (project, name, value) VALUES (?,?,?)",
                (project, self._maintenance_counter_name(pass_name), merged_since))
        return self.maintenance_checkpoint(project, pass_name)

    def reset_maintenance(self, project, pass_name):
        return self.set_maintenance_checkpoint(project, pass_name)

    def increment_maintenance_merged(self, project, pass_name, amount=1):
        with self._tx():
            self.con.execute(
                "INSERT OR IGNORE INTO counters (project, name, value) VALUES (?,?,0)",
                (project, self._maintenance_counter_name(pass_name)))
            self.con.execute(
                "UPDATE counters SET value=value+? WHERE project=? AND name=?",
                (amount, project, self._maintenance_counter_name(pass_name)))
        return self.maintenance_checkpoint(project, pass_name)["merged_since"]

    def record_shipped(self, project, passes=None):
        selected = MAINTENANCE_PASSES if passes is None else passes
        if isinstance(selected, str):
            selected = (selected,)
        for pass_name in dict.fromkeys(selected):
            self.increment_maintenance_merged(project, pass_name)

    def maintenance_due(self, project, pass_name, cadence_days=30,
                        merged_threshold=20, policy=None):
        if policy is not None:
            policy = policy.get("maintenance", policy)
            cadence_days = policy.get("cadence_days", cadence_days)
            merged_threshold = policy.get("merged_threshold", merged_threshold)
        checkpoint = self.maintenance_checkpoint(project, pass_name)
        last_filed_at = parse(checkpoint["last_filed_at"])
        return (last_filed_at is None
                or self.now() - last_filed_at >= timedelta(days=cadence_days)
                or checkpoint["merged_since"] >= merged_threshold)

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
        if state == "done" and (cur is None or cur["state"] != "done"):
            runs = self.q("SELECT actual_mins, started_at, ended_at FROM runs WHERE project=? AND number=? AND status='ended'",
                          (project, number))
            total_mins = 0.0
            for r in runs:
                if r["actual_mins"] is not None:
                    total_mins += r["actual_mins"]
                elif r["started_at"] and r["ended_at"]:
                    st = parse(r["started_at"])
                    en = parse(r["ended_at"])
                    if st and en:
                        total_mins += max(0.0, (en - st).total_seconds() / 60.0)
            ests = self.estimates()
            est = self.issue_estimate(ests, project)
            self.event("issue_done_stats", project, number, {
                "total_actual_mins": round(total_mins, 2),
                "predicted_issue_mins": round(est, 2),
                "runs_count": len(runs),
            })
        return self.item(project, number)

    # ---------- leases ----------

    def lease(self, project, number, live_only=True):
        r = self.q1("SELECT * FROM leases WHERE project=? AND number=?", (project, number))
        if r and live_only and parse(r["expires_at"]) <= self.now():
            return None
        return r

    def claim(self, project, number, holder, kind, ttl_minutes, platform=None,
              run_id=None, steal=False, max_parallel=None, capacity=True,
              handoff_from=None):
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
                    "UPDATE leases SET heartbeat_at=?, expires_at=?, capacity=? "
                    "WHERE project=? AND number=?",
                    (iso(now), iso(now + timedelta(minutes=ttl_minutes)), int(capacity),
                     project, number))
                info["renewed"] = True
                return self.lease(project, number, live_only=False), info
            if live:
                handed_off = (handoff_from is not None
                              and cur["holder"] == handoff_from[0]
                              and cur["epoch"] == int(handoff_from[1]))
                human = kind in ("interactive", "primary")
                if handed_off:
                    info["handed_off_from"] = dict(cur)
                elif cur["kind"] == "auto" and human:
                    info["preempted"] = dict(cur)
                    if cur["run_id"]:
                        self.con.execute("UPDATE runs SET yield_at=? WHERE id=? AND yield_at IS NULL",
                                         (iso(now), cur["run_id"]))
                elif cur["kind"] != "auto" and human and steal:
                    info["stolen_from"] = dict(cur)
                else:
                    info["held_by"] = dict(cur)
                    return None, info
            if capacity and max_parallel is not None:
                occupied = self.q(
                    "SELECT * FROM leases WHERE project=? AND number<>? "
                    "AND capacity=1 AND expires_at>?",
                    (project, number, iso(now)))
                if len(occupied) >= int(max_parallel):
                    info["at_capacity"] = [dict(row) for row in occupied]
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
                " run_id, acquired_at, heartbeat_at, expires_at, capacity) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (project, number, holder, kind, platform, epoch, run_id, iso(now), iso(now),
                 iso(now + timedelta(minutes=ttl_minutes)), int(capacity)))
            self.event("lease", project, number,
                       {"holder": holder, "kind": kind, "platform": platform, "epoch": epoch,
                        **({"preempted": info["preempted"]["holder"]} if "preempted" in info else {})})
        return self.lease(project, number, live_only=False), info

    def heartbeat(self, project, number, holder, epoch, ttl_minutes):
        """Extend a lease we still hold. False means it was taken over or reaped."""
        now = self.now()
        cur = self.con.execute(
            "UPDATE leases SET heartbeat_at=?, expires_at=? "
            "WHERE project=? AND number=? AND holder=? AND epoch=?",
            (iso(now), iso(now + timedelta(minutes=ttl_minutes)), project, number, holder, epoch))
        return cur.rowcount == 1

    def release(self, project, number, holder=None, epoch=None, to_state="ready", why=None):
        with self._tx():
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
                if to_state is not None:
                    item = self.item(project, number)
                    if item and item["state"] == "working":
                        self.set_state(project, number, to_state,
                                       why=why or (f"released by {holder}" if holder else "lease released"))
            return n == 1

    def lease_check(self, project, number, epoch):
        """Fencing check: is `epoch` still the live lease on this item?"""
        cur = self.lease(project, number)
        return cur is not None and cur["epoch"] == int(epoch)

    def expired_leases(self):
        now = self.now()
        return [r for r in self.q("SELECT * FROM leases") if parse(r["expires_at"]) <= now]

    def orphan_working_items(self, project=None):
        """Items in 'working' with no lease row and no active run."""
        sql = """
            SELECT i.* FROM items i
            WHERE i.state = 'working'
              AND NOT EXISTS (
                  SELECT 1 FROM leases l
                  WHERE l.project = i.project AND l.number = i.number
              )
              AND NOT EXISTS (
                  SELECT 1 FROM runs r
                  WHERE r.project = i.project AND r.number = i.number
                    AND r.status IN ('running', 'stopping')
              )
        """
        args = []
        if project:
            sql += " AND i.project = ?"
            args.append(project)
        sql += " ORDER BY i.priority, i.number"
        return self.q(sql, args)

    def orphan_lease_rows(self, project=None):
        """Lease rows held on items that are in 'ready' or 'done' with no active run."""
        sql = """
            SELECT l.*, i.state AS item_state FROM leases l
            JOIN items i ON l.project = i.project AND l.number = i.number
            WHERE i.state IN ('ready', 'done')
              AND NOT EXISTS (
                  SELECT 1 FROM runs r
                  WHERE r.project = l.project AND r.number = l.number
                    AND r.status IN ('running', 'stopping')
              )
        """
        args = []
        if project:
            sql += " AND l.project = ?"
            args.append(project)
        sql += " ORDER BY l.project, l.number"
        return self.q(sql, args)

    # ---------- runs ----------

    def create_run(self, **cols):
        cols.setdefault("status", "running")
        cols.setdefault("started_at", iso(self.now()))
        if "est_mins" not in cols and cols.get("platform") and cols.get("role"):
            ests = self.estimates()
            cols["est_mins"] = round(
                self.run_estimate(ests, cols["platform"], cols["role"], cols.get("size")), 2)
        keys = list(cols)
        cur = self.con.execute(
            f"INSERT INTO runs ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
            tuple(cols.values()))
        return cur.lastrowid

    def update_run(self, run_id, **cols):
        if cols.get("ended_at") and "actual_mins" not in cols:
            r = self.run(run_id)
            if r and r["started_at"]:
                started = parse(r["started_at"])
                ended = parse(cols["ended_at"])
                if started and ended:
                    cols["actual_mins"] = round(max(0.0, (ended - started).total_seconds() / 60.0), 2)
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

    def last_run(self, project, number):
        return self.q1(
            "SELECT * FROM runs WHERE project=? AND number=? ORDER BY id DESC LIMIT 1",
            (project, number))

    def platform_outcomes(self, since=None):
        """Per-platform build/fix run outcomes (mahler#206): {platform:
        {"runs": n, "done": n, "needs_you": n, "by_size": {size: {"runs": n, "done": n}}}}
        — the observed half of the periodic platform-tier/capability audit, alongside
        `platform_escalations` below. `since` (a datetime) restricts to runs
        started at/after it; None is all-time."""
        sql = ("SELECT platform, size, outcome, COUNT(*) c FROM runs "
               "WHERE role IN ('build','fix') AND status='ended'")
        args = []
        if since is not None:
            sql += " AND started_at >= ?"
            args.append(iso(since))
        sql += " GROUP BY platform, size, outcome"
        stats = {}
        for row in self.q(sql, args):
            s = stats.setdefault(row["platform"], {"runs": 0, "done": 0, "needs_you": 0, "by_size": {}})
            sz = row["size"]
            sz_dict = s["by_size"].setdefault(sz, {"runs": 0, "done": 0})
            
            s["runs"] += row["c"]
            sz_dict["runs"] += row["c"]
            if row["outcome"] == "DONE":
                s["done"] += row["c"]
                sz_dict["done"] += row["c"]
            elif row["outcome"] == "NEEDS-YOU":
                s["needs_you"] += row["c"]
        return stats

    def platform_escalations(self, since=None):
        """Count of `escalated` events attributable to a platform (mahler#206)
        — ship.py/finalize.py record the platform whose run preceded the
        escalation in the event's JSON detail. Events logged before that
        detail carried a platform (a plain string, pre-mahler#206) have no
        `platform` key and are silently skipped."""
        sql = "SELECT detail FROM events WHERE kind='escalated'"
        args = []
        if since is not None:
            sql += " AND at >= ?"
            args.append(iso(since))
        counts = {}
        for row in self.q(sql, args):
            try:
                detail = json.loads(row["detail"]) if row["detail"] else None
            except json.JSONDecodeError:
                continue
            platform = detail.get("platform") if isinstance(detail, dict) else None
            if platform:
                counts[platform] = counts.get(platform, 0) + 1
        return counts

    # ---------- usage ----------

    def record_usage(self, platform, window, used_pct, resets_at=None, sampled_at=None):
        self.con.execute(
            "INSERT OR REPLACE INTO usage (platform, window, used_pct, resets_at, sampled_at)"
            " VALUES (?,?,?,?,?)",
            (platform, window, float(used_pct), resets_at, sampled_at or iso(self.now())))

    def usage(self, platform):
        return {r["window"]: dict(r) for r in
                self.q("SELECT * FROM usage WHERE platform=?", (platform,))}

    def clear_usage(self, platform, windows):
        """Drop `platform`'s readings for `windows` — how a backoff or hold is
        cleared by hand (the console's Clear backoff, D27). An unmetered
        platform with no row is available again; a metered one reads as
        unknown until the next probe, which D8 still treats as over the line."""
        windows = list(windows)
        if not windows:
            return 0
        cur = self.con.execute(
            f"DELETE FROM usage WHERE platform=? AND window IN ({','.join('?' * len(windows))})",
            (platform, *windows))
        return cur.rowcount

    def estimates(self):
        rows = self.q("""
            SELECT platform, role, count(*) as c, 
                   avg((julianday(ended_at) - julianday(started_at))*24*60) as avg_mins
            FROM runs WHERE ended_at IS NOT NULL AND status='ended' GROUP BY platform, role
        """)
        size_rows = self.q("""
            SELECT platform, role, size, count(*) as c,
                   avg((julianday(ended_at) - julianday(started_at))*24*60) as avg_mins
            FROM runs WHERE ended_at IS NOT NULL AND status='ended' AND size IS NOT NULL
            GROUP BY platform, role, size
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
        by_prs = {(r["platform"], r["role"], r["size"]): r["avg_mins"]
                  for r in size_rows if r["c"] >= 3}
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

        factor = self.calibration_factor()

        return {
            "run_avg": by_pr,
            "run_avg_size": by_prs,
            "plat_avg": by_p,
            "global_run_avg": global_avg,
            "proj_issue_avg": proj_issue_avg,
            "global_issue_avg": global_issue,
            "calibration_factor": factor,
        }

    def calibration_factor(self):
        cal = self.get_kv("estimate_calibration")
        if cal:
            try:
                data = json.loads(cal)
                return float(data.get("factor", 1.0))
            except Exception:
                pass
        return 1.0

    def calibration_stats(self):
        cal = self.get_kv("estimate_calibration")
        if cal:
            try:
                return json.loads(cal)
            except Exception:
                pass
        return None

    def calibrate_estimates(self, window=20):
        """Compare recent predicted vs actual durations and calculate a calibration factor.
        Clamps the factor between 0.5 and 2.0 to guard against wild outliers (mahler#59).
        """
        rows = self.q("""
            SELECT est_mins, actual_mins, platform, role
            FROM runs
            WHERE status = 'ended' AND est_mins IS NOT NULL AND actual_mins IS NOT NULL AND est_mins > 0
            ORDER BY ended_at DESC, id DESC
            LIMIT ?
        """, (window,))
        if not rows:
            return None

        total_est = sum(r["est_mins"] for r in rows)
        total_act = sum(r["actual_mins"] for r in rows)
        count = len(rows)

        if total_est <= 0 or count == 0:
            return None

        raw_factor = total_act / total_est
        factor = max(0.5, min(2.0, raw_factor))
        mae = sum(abs(r["actual_mins"] - r["est_mins"]) for r in rows) / count

        stats = {
            "factor": round(factor, 3),
            "raw_factor": round(raw_factor, 3),
            "mae": round(mae, 2),
            "samples": count,
            "calibrated_at": iso(self.now()),
        }
        self.set_kv("estimate_calibration", json.dumps(stats))
        self.event("estimate_calibration", detail=stats)
        return stats

    def run_estimate(self, ests, platform, role, size=None):
        raw = (
            (ests.get("run_avg_size", {}).get((platform, role, size)) if size else None)
            or ests["run_avg"].get((platform, role))
            or ests["plat_avg"].get(platform)
            or ests["global_run_avg"]
        )
        return raw * ests.get("calibration_factor", 1.0)

    def issue_estimate(self, ests, project):
        raw = ests["proj_issue_avg"].get(project) or ests["global_issue_avg"]
        return raw * ests.get("calibration_factor", 1.0)


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


_SP_COUNTER = itertools.count(1)


class _Tx:
    def __init__(self, con):
        self.con = con
        self._sp = None

    def __enter__(self):
        if getattr(self.con, "in_transaction", False):
            self._sp = f"tx_{next(_SP_COUNTER)}"
            self.con.execute(f"SAVEPOINT {self._sp}")
        else:
            self.con.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, *_):
        if self._sp:
            if exc_type:
                self.con.execute(f"ROLLBACK TO {self._sp}")
            self.con.execute(f"RELEASE {self._sp}")
            return False
        self.con.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


class RemoteLedgerError(RuntimeError):
    """The canonical lease host could not return a trustworthy answer."""


class RoutedLedger:
    """Keep all state local except project-scoped lease operations (DESIGN D24).

    A configured remote is fail-closed: an unreachable or malformed response
    never grants, renews, or validates a lease. Non-lease methods always go to
    the local SQLite ledger.
    """

    def __init__(self, local, cfg, run=subprocess.run):
        self.local = local
        self.cfg = cfg
        self._run = run
        self._errors = {}

    def close(self):
        self.local.close()

    def __getattr__(self, name):
        return getattr(self.local, name)

    def _remote(self, project):
        value = self.cfg.get("projects", {}).get(project, {}).get("remote_ledger")
        if not value:
            return None
        if isinstance(value, str):
            return {"host": value}
        return value if isinstance(value, dict) else {"invalid": True}

    def _call(self, project, operation, **args):
        remote = self._remote(project)
        if remote is None:
            raise AssertionError("remote call requested for a local project")
        host = remote.get("host", "")
        command = remote.get("command", "~/.mahler/app/bin/mahler")
        try:
            timeout = int(remote.get("connect_timeout_seconds", 5))
        except (TypeError, ValueError) as exc:
            raise RemoteLedgerError("invalid remote_ledger timeout") from exc
        command_argv = [command] if isinstance(command, str) else command
        if (not isinstance(host, str) or not host or host.startswith("-")
                or not re.fullmatch(r"[A-Za-z0-9_.:@-]+", host)
                or not isinstance(command_argv, list) or not 1 <= len(command_argv) <= 8
                or any(not isinstance(arg, str) or not arg or arg.startswith("-")
                       or not re.fullmatch(r"[A-Za-z0-9_./~+:-]+", arg)
                       for arg in command_argv)
                or not 1 <= timeout <= 60):
            raise RemoteLedgerError("invalid remote_ledger host or command")
        request = {"version": 1, "operation": operation, "project": project, **args}
        argv = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}",
                "-o", "ConnectionAttempts=1", host, *command_argv, "ledger-remote-op"]
        try:
            proc = self._run(argv, input=json.dumps(request), capture_output=True,
                             text=True, timeout=timeout + 5)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RemoteLedgerError(f"remote ledger unavailable: {exc}") from exc
        if proc.returncode:
            detail = (proc.stderr or proc.stdout or "ssh failed").strip()[:300]
            raise RemoteLedgerError(f"remote ledger unavailable: {detail}")
        try:
            response = json.loads(proc.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RemoteLedgerError("remote ledger returned malformed JSON") from exc
        if not isinstance(response, dict) or response.get("version") != 1:
            raise RemoteLedgerError("remote ledger returned an invalid response")
        if not response.get("ok"):
            raise RemoteLedgerError(str(response.get("error") or "remote operation failed"))
        self._errors.pop(project, None)
        return response.get("result")

    def remote_error(self, project):
        return self._errors.get(project)

    def _remember(self, project, exc):
        self._errors[project] = str(exc)

    def _holder(self, project, holder):
        """Make laptop holder ids globally distinct from canonical-host ids."""
        if holder is None or not self._remote(project):
            return holder
        remote = self._remote(project)
        client = remote.get("client_id") or socket.gethostname().split(".", 1)[0]
        if not isinstance(client, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", client):
            raise RemoteLedgerError("invalid remote_ledger client_id")
        qualified = f"{client}/{holder}"
        return holder if holder.startswith(f"{client}/") else qualified

    def lease(self, project, number, live_only=True):
        if not self._remote(project):
            return self.local.lease(project, number, live_only=live_only)
        try:
            return self._call(project, "lease", number=number, live_only=live_only)
        except RemoteLedgerError as exc:
            self._remember(project, exc)
            return None

    def lease_rows(self, project=None):
        """Live leases for status/hooks, including configured remote projects."""
        remote_projects = {name for name in self.cfg.get("projects", {})
                           if self._remote(name)}
        sql, args = "SELECT * FROM leases", ()
        if project:
            sql, args = sql + " WHERE project=?", (project,)
        rows = [dict(row) for row in self.local.q(sql, args)
                if row["project"] not in remote_projects]
        names = [project] if project else sorted(remote_projects)
        for name in names:
            if name not in remote_projects:
                continue
            # Claims make the local item working; conductor leases make it
            # verifying. Limit SSH round trips to those plausible live leases.
            for item in self.local.items(name, ["working", "verifying"]):
                lease = self.lease(name, item["number"])
                if lease:
                    rows.append(lease)
        return rows

    def expired_leases(self):
        """The canonical host, not this laptop, expires remote-project rows."""
        remote_projects = {name for name in self.cfg.get("projects", {})
                           if self._remote(name)}
        return [row for row in self.local.expired_leases()
                if row["project"] not in remote_projects]

    def claim(self, project, number, holder, kind, ttl_minutes, platform=None,
              run_id=None, steal=False, max_parallel=None, capacity=True,
              handoff_from=None):
        limit = (config_project(self.cfg, project).get("max_parallel")
                 if capacity else None)
        if not self._remote(project):
            return self.local.claim(
                project, number, holder, kind, ttl_minutes, platform=platform,
                run_id=run_id, steal=steal, max_parallel=limit, capacity=capacity,
                handoff_from=handoff_from)
        try:
            result = self._call(
                project, "claim", number=number, holder=self._holder(project, holder), kind=kind,
                ttl_minutes=ttl_minutes, platform=platform, steal=steal,
                capacity=capacity,
                handoff_from=([self._holder(project, handoff_from[0]), handoff_from[1]]
                              if handoff_from else None))
            return result["lease"], result["info"]
        except RemoteLedgerError as exc:
            self._remember(project, exc)
            return None, {"unavailable": str(exc)}

    def heartbeat(self, project, number, holder, epoch, ttl_minutes):
        if not self._remote(project):
            return self.local.heartbeat(project, number, holder, epoch, ttl_minutes)
        try:
            return bool(self._call(project, "heartbeat", number=number,
                                   holder=self._holder(project, holder),
                                   epoch=epoch, ttl_minutes=ttl_minutes))
        except RemoteLedgerError as exc:
            self._remember(project, exc)
            return False

    def release(self, project, number, holder=None, epoch=None, to_state="ready", why=None):
        if not self._remote(project):
            return self.local.release(project, number, holder=holder, epoch=epoch,
                                      to_state=to_state, why=why)
        try:
            kwargs = {"number": number, "holder": self._holder(project, holder), "epoch": epoch}
            if to_state != "ready":
                kwargs["to_state"] = to_state
            if why is not None:
                kwargs["why"] = why
            return bool(self._call(project, "release", **kwargs))
        except RemoteLedgerError as exc:
            self._remember(project, exc)
            return False

    def lease_check(self, project, number, epoch):
        if not self._remote(project):
            return self.local.lease_check(project, number, epoch)
        try:
            return bool(self._call(project, "lease_check", number=number, epoch=epoch))
        except RemoteLedgerError as exc:
            self._remember(project, exc)
            return False


def config_project(cfg, project):
    """Small local helper avoids importing config (and a module cycle)."""
    defaults = cfg.get("defaults", {})
    return {**defaults, **cfg.get("projects", {}).get(project, {})}


def remote_lease_operation(request, cfg, led):
    """Validate and execute one stdin/stdout JSON operation on canonical SQLite."""
    if not isinstance(request, dict) or request.get("version") != 1:
        raise ValueError("unsupported remote ledger protocol version")
    operation = request.get("operation")
    if operation not in {"lease", "claim", "heartbeat", "release", "lease_check"}:
        raise ValueError("unsupported remote ledger operation")
    project = request.get("project")
    project_cfg = cfg.get("projects", {}).get(project, {})
    if not isinstance(project, str) or not project_cfg.get("enabled"):
        raise ValueError("project is not enabled on the canonical ledger host")
    number = request.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise ValueError("number must be a positive integer")
    if operation == "lease":
        live_only = request.get("live_only", True)
        if not isinstance(live_only, bool):
            raise ValueError("live_only must be boolean")
        lease = led.lease(project, number, live_only=live_only)
        return dict(lease) if lease else None
    if operation == "claim":
        holder, kind, ttl = (request.get("holder"), request.get("kind"),
                             request.get("ttl_minutes"))
        if (not isinstance(holder, str) or not holder or len(holder) > 200
                or kind not in {"auto", "interactive", "primary"}
                or not isinstance(ttl, int) or isinstance(ttl, bool) or not 1 <= ttl <= 1440):
            raise ValueError("invalid claim arguments")
        capacity, steal = request.get("capacity", True), request.get("steal", False)
        platform = request.get("platform")
        if (not isinstance(capacity, bool) or not isinstance(steal, bool)
                or (platform is not None and not isinstance(platform, str))):
            raise ValueError("invalid claim arguments")
        handoff = request.get("handoff_from")
        if handoff is not None:
            if (not isinstance(handoff, list) or len(handoff) != 2
                    or not isinstance(handoff[0], str) or not isinstance(handoff[1], int)):
                raise ValueError("invalid handoff_from")
            handoff = tuple(handoff)
        lease, info = led.claim(
            project, number, holder, kind, ttl, platform=platform, steal=steal,
            max_parallel=config_project(cfg, project).get("max_parallel") if capacity else None,
            capacity=capacity, handoff_from=handoff)
        return {"lease": dict(lease) if lease else None, "info": info}
    if operation == "heartbeat":
        holder, epoch, ttl = (request.get("holder"), request.get("epoch"),
                              request.get("ttl_minutes"))
        if (not isinstance(holder, str) or not holder or len(holder) > 200
                or not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0
                or not isinstance(ttl, int) or isinstance(ttl, bool) or not 1 <= ttl <= 1440):
            raise ValueError("invalid heartbeat arguments")
        return led.heartbeat(project, number, holder, epoch, ttl)
    if operation == "release":
        holder, epoch = request.get("holder"), request.get("epoch")
        if ((holder is not None and (not isinstance(holder, str) or len(holder) > 200))
                or (epoch is not None and (not isinstance(epoch, int)
                                            or isinstance(epoch, bool) or epoch < 0))):
            raise ValueError("invalid release arguments")
        to_state = request.get("to_state", "ready")
        if to_state is not None and to_state not in STATES:
            raise ValueError("invalid to_state")
        why = request.get("why")
        if why is not None and (not isinstance(why, str) or len(why) > 500):
            raise ValueError("invalid why")
        return led.release(project, number, holder=holder, epoch=epoch,
                           to_state=to_state, why=why)
    epoch = request.get("epoch")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
        raise ValueError("invalid lease_check arguments")
    return led.lease_check(project, number, epoch)
