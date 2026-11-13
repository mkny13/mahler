"""Atlas: cached project inventory (mahler#1532). Fully isolated: temp dirs only."""
import contextlib
import io
import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from mahler import atlas, cli, config, scheduler
from mahler.ledger import Ledger, iso

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


def git(path, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_GLOBAL="/dev/null")
    subprocess.run(["git", "-C", path, *args], check=True, capture_output=True, env=env)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = os.path.realpath(self.tmp.name)
        self.home = os.path.join(t, "home")
        self.root = os.path.join(t, "scripts")
        self.hist = os.path.join(self.home, ".claude", "projects")
        for d in (self.home, self.root, self.hist):
            os.makedirs(d)
        env = patch.dict(os.environ, {"HOME": self.home})
        env.start()
        self.addCleanup(env.stop)
        tmp = patch.object(atlas, "TEMP_ROOTS", ())
        tmp.start()
        self.addCleanup(tmp.stop)
        self.cfg = config.load(os.path.join(t, "none.toml"))
        self.cfg["atlas"] = {"scan_roots": [self.root], "ignore": [], "phase": {}}
        self.led = Ledger(":memory:")
        self.addCleanup(self.led.close)

    def folder(self, name, files=(), root=None):
        p = os.path.join(root or self.root, name)
        os.makedirs(p, exist_ok=True)
        for f in files:
            open(os.path.join(p, f), "w").write("x")
        return p

    def session(self, cwd, n=1, sub="proj", lines=None):
        d = os.path.join(self.hist, sub)
        os.makedirs(d, exist_ok=True)
        for i in range(n):
            with open(os.path.join(d, f"s{len(os.listdir(d))}.jsonl"), "w") as fh:
                for ln in lines or [json.dumps({"type": "x"}), json.dumps({"cwd": cwd})]:
                    fh.write(ln + "\n")

    def scan(self, **kw):
        return {r["name"]: r for r in atlas.scan(self.cfg, self.led, NOW, **kw)}

    def managed(self, name="mg", path=None, **extra):
        self.cfg["projects"][name] = {"enabled": True, "repo": f"o/{name}", "path": path or self.folder(name), **extra}


class PhaseTests(Base):
    def test_unmanaged_phases_and_threshold(self):
        self.folder("two", ["a.py", "b.py", "n.md", "t.TXT"])
        self.folder("three", ["a.py", "b.py", "c.py"])
        g = self.folder("gitty", [])
        subprocess.run(["git", "init", "-q", g], check=True)
        r = self.scan()
        self.assertEqual(r["two"]["phase"], "idea")
        self.assertIsNone(r["two"]["commits"])
        self.assertEqual(r["three"]["phase"], "prototype")
        self.assertEqual(r["gitty"]["commits"], 0)
        self.assertEqual(r["gitty"]["phase"], "prototype")

    def test_git_commits_and_dates(self):
        g = self.folder("repo", ["a"])
        subprocess.run(["git", "init", "-q", g], check=True)
        git(g, "add", "."); git(g, "commit", "-qm", "one")
        r = self.scan()["repo"]
        self.assertEqual(r["commits"], 1)
        self.assertTrue(r["last_touched"].endswith("+00:00"))

    def test_session_only_is_idea_without_touching_folder(self):
        outside = os.path.join(self.home, "Documents", "meal")
        self.session(outside, n=2)
        real_stat, real_ls = os.stat, os.scandir
        def bad(p, *a, **k):
            if str(p).startswith(outside):
                raise AssertionError("touched " + str(p))
            return real_stat(p, *a, **k)
        def badls(p=".", *a, **k):
            if str(p).startswith(outside):
                raise AssertionError("listed " + str(p))
            return real_ls(p, *a, **k)
        with patch("os.stat", bad), patch("os.scandir", badls), \
                patch("os.path.realpath", lambda p, *a, **k: (_ for _ in ()).throw(AssertionError(p))
                      if str(p).startswith(outside) else os.path.abspath(p)):
            r = self.scan()["meal"]
        self.assertEqual((r["phase"], r["sessions"], r["managed"]), ("idea", 2, False))
        self.assertIsNone(r["commits"])

    def test_managed_phases(self):
        self.managed("a"); self.managed("b"); self.managed("c")
        self.led.upsert_item("b", 1, state="done")
        self.led.upsert_item("c", 1, state="done")
        self.led.upsert_item("c", 2, state="ready")
        r = self.scan()
        self.assertEqual(r["a"]["phase"], "onboarded")
        self.assertEqual(r["b"]["phase"], "steady")
        self.assertEqual(r["c"]["phase"], "building")
        self.assertEqual((r["c"]["open"], r["c"]["done"]), (1, 1))
        self.assertFalse(r["a"]["work"])

    def test_runs7_makes_building_and_boundaries(self):
        self.managed("b")
        self.led.upsert_item("b", 1, state="done")
        for delta in (timedelta(days=7), timedelta(days=7, seconds=1), timedelta(0)):
            self.led.create_run(project="b", number=1, role="build", platform="p", epoch=1,
                                status="ended", started_at=iso(NOW - delta))
        r = self.scan()["b"]
        self.assertEqual(r["runs7"], 2)
        self.assertEqual(r["phase"], "building")

    def test_work_flag(self):
        self.cfg["accounts"]["work"] = {}
        self.managed("w", gh_account="work")
        self.assertTrue(self.scan()["w"]["work"])

    def test_override_path_beats_name(self):
        p = self.folder("x", ["a", "b", "c", "d"])
        self.cfg["atlas"]["phase"] = {"x": "idea", p: "steady"}
        self.assertEqual(self.scan()["x"]["phase"], "steady")
        self.cfg["atlas"]["phase"] = {"x": "idea"}
        self.assertEqual(self.scan()["x"]["phase"], "idea")


class DiscoveryTests(Base):
    def test_symlink_dedupe_and_sessions_merge(self):
        real = self.folder("real", ["a.md"])
        os.symlink(real, os.path.join(self.root, "alias"))
        self.session(real, n=2); self.session(os.path.join(self.root, "alias"))
        out = atlas.scan(self.cfg, self.led, NOW)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["key"], real)
        self.assertEqual(out[0]["sessions"], 3)

    def test_escaping_symlink_not_followed(self):
        outside = os.path.join(os.path.dirname(self.root), "elsewhere")
        os.makedirs(outside)
        for i in "abc":
            open(os.path.join(outside, i), "w").write("x")
        os.symlink(outside, os.path.join(self.root, "esc"))
        real_ls = os.scandir
        def badls(p=".", *a, **k):
            if str(p).startswith(outside):
                raise AssertionError("listed escaping target")
            return real_ls(p, *a, **k)
        with patch("os.scandir", badls):
            r = self.scan()["esc"]
        self.assertEqual(r["phase"], "idea")

    def test_managed_exact_merge_and_descendant_exclusion(self):
        mp = self.folder("mg")
        self.managed("mg", path=mp)
        self.session(mp, n=2); self.session(os.path.join(mp, "sub")); self.session(mp + "/.claude/worktrees/w1")
        out = atlas.scan(self.cfg, self.led, NOW)
        self.assertEqual([(r["name"], r["managed"], r["sessions"]) for r in out], [("mg", True, 3)])

    def test_worktree_root_excluded(self):
        wt = os.path.join(self.home, "wts")
        self.cfg["projects"]["m"] = {"enabled": True, "repo": "o/m", "path": self.folder("m"), "worktree_root": wt}
        self.session(os.path.join(wt, "m", "1-run2"))
        self.assertEqual([r["name"] for r in atlas.scan(self.cfg, self.led, NOW)], ["m"])

    def test_builtin_exclusions(self):
        with patch.object(atlas, "TEMP_ROOTS", ("/private/tmp", "/tmp", "/var/folders", "/private/var/folders")):
            for p in ("/private/tmp/a", "/tmp/b", "/var/folders/x/y", "/private/var/folders/z",
                      os.path.join(self.home, ".mahler", "w"), os.path.join(self.home, ".cline", "worktrees", "q"),
                      "/v/.mahler-worktrees/p", "/v/scratch-workspaces/p", self.home):
                self.session(p)
            self.session("/elsewhere/keep")
            self.assertEqual([r["name"] for r in atlas.scan(self.cfg, self.led, NOW)], ["keep"])

    def test_ignore_by_name_and_path(self):
        self.folder("a"); self.folder("b"); self.folder("c")
        self.cfg["atlas"]["ignore"] = ["a", os.path.join(self.root, "b")]
        self.assertEqual(sorted(self.scan()), ["c"])
        self.managed("a"); 
        self.assertIn("a", self.scan())      # ignore filters discoveries, not managed

    def test_malformed_sources(self):
        self.session("", lines=["not json", json.dumps({"cwd": "relative"})])
        self.session("", lines=[])
        self.cfg["atlas"]["scan_roots"] = [self.root, os.path.join(self.root, "missing")]
        os.makedirs(os.path.join(self.hist, "d.jsonl"))   # dir named like a session
        self.assertEqual(atlas.scan(self.cfg, self.led, NOW), [])

    def test_desc_and_dates(self):
        p = self.folder("d", ["x.py"])
        open(os.path.join(p, "README.md"), "w").write("# Title\n\nA tiny tool. It does more.\n")
        self.session(p)
        os.utime(os.path.join(self.hist, "proj", "s0.jsonl"), (1700000000, 1700000000))
        r = self.scan()["d"]
        self.assertEqual(r["desc"], "A tiny tool.")
        self.assertIsNotNone(r["last_touched"])
        self.assertEqual(r["sessions"], 1)

    def test_git_timeout_gives_none(self):
        g = self.folder("g", ["a"])
        subprocess.run(["git", "init", "-q", g], check=True)
        def boom(*a, **k):
            raise subprocess.TimeoutExpired("git", 5)
        with patch("mahler.atlas.subprocess.run", boom):
            r = self.scan()["g"]
        self.assertIsNone(r["commits"])


class CacheTests(Base):
    def test_cache_roundtrip_and_first_seen(self):
        self.assertEqual(atlas.load(self.led), [])
        self.folder("a")
        atlas.scan(self.cfg, self.led, NOW)
        raw = json.loads(self.led.get_kv("atlas:scan"))
        self.assertEqual(raw["scanned_at"], iso(NOW))
        later = NOW + timedelta(days=1)
        atlas.scan(self.cfg, self.led, later)
        self.assertEqual(atlas.load(self.led)[0]["first_seen"], iso(NOW))

    def test_malformed_cache_loads_empty(self):
        for bad in ("nope", "[]", '{"projects": 3}'):
            self.led.set_kv("atlas:scan", bad)
            self.assertEqual(atlas.load(self.led), [])

    def test_deadline_interrupts_stalled_source_and_keeps_cache(self):
        self.folder("a")
        atlas.scan(self.cfg, self.led, NOW)
        before = self.led.get_kv("atlas:scan")
        prev = signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.setitimer(signal.ITIMER_REAL, 100)
        try:
            with patch.object(atlas, "_list_children", lambda *a: time.sleep(30)):
                t = time.monotonic()
                with self.assertRaises(atlas.ScanError):
                    atlas.scan(self.cfg, self.led, NOW, budget=0.3)
                self.assertLess(time.monotonic() - t, 3)
            remaining = signal.getitimer(signal.ITIMER_REAL)[0]
            self.assertGreater(remaining, 50)
            self.assertIs(signal.getsignal(signal.SIGALRM), signal.SIG_DFL)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, prev)
        self.assertEqual(self.led.get_kv("atlas:scan"), before)


class TickTests(Base):
    def ctx(self, dry=False):
        return scheduler.Ctx(self.cfg, self.led, dry_run=dry)

    def test_maybe_scan_hourly_even_after_failure(self):
        calls = []
        def fail(*a, **k):
            calls.append(1); raise RuntimeError("boom")
        c = self.ctx()
        with patch.object(atlas, "scan", fail):
            atlas.maybe_scan(c)                      # swallowed
            atlas.maybe_scan(self.ctx())             # restart: persisted throttle
            self.assertEqual(len(calls), 1)
            self.led.clock = lambda: self.led_now() + timedelta(seconds=3599)
            atlas.maybe_scan(c); self.assertEqual(len(calls), 1)
            self.led.clock = lambda: self.led_now() + timedelta(seconds=3600)
            atlas.maybe_scan(c); self.assertEqual(len(calls), 2)

    def led_now(self):
        return datetime.fromisoformat(self.led.get_kv("atlas:last_attempt"))

    def test_dry_run_does_nothing(self):
        with patch.object(atlas, "scan") as s:
            atlas.maybe_scan(self.ctx(dry=True))
        s.assert_not_called()
        self.assertIsNone(self.led.get_kv("atlas:last_attempt"))

    def test_tick_runs_atlas_even_when_paused_and_survives_failure(self):
        self.led.set_kv("paused", "1")
        with patch.object(atlas, "scan", side_effect=RuntimeError("x")) as s, \
                patch.object(scheduler.janitor, "maybe_run") as later:
            scheduler.tick(self.ctx())
        s.assert_called_once()
        later.assert_called_once()


class CliTests(Base):
    def run_cli(self, *argv):
        a = cli.build_parser().parse_args(["atlas", *argv])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = a.fn(a, self.cfg, self.led)
        return rc, out.getvalue(), err.getvalue()

    def test_show_empty_then_scan_then_show(self):
        rc, out, _ = self.run_cli("show")
        self.assertEqual(rc, 0); self.assertIn("no atlas cache", out)
        self.folder("zed", ["a.md"])
        rc, out, _ = self.run_cli("scan")
        self.assertEqual(rc, 0); self.assertIn("zed", out); self.assertIn("idea", out)
        with patch.object(atlas, "scan", side_effect=AssertionError("no discovery")):
            rc, out, _ = self.run_cli("show")
        self.assertIn("zed", out)

    def test_forced_scan_failure_exits_nonzero_with_stale_cache(self):
        self.folder("zed")
        self.run_cli("scan")
        with patch.object(atlas, "scan", side_effect=atlas.ScanError("too slow")):
            rc, out, err = self.run_cli("scan")
        self.assertEqual(rc, 1)
        self.assertIn("too slow", err); self.assertIn("stale", out + err); self.assertIn("zed", out)


if __name__ == "__main__":
    unittest.main()
