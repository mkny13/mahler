"""The desktop gate (D40): parsing, thresholds, windows, CLI launch suppression and
real-process mutual exclusion. `ioreg` and `caffeinate` are never run: the probes
and the display wake are stubbed."""

import io
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, time as dtime
from pathlib import Path
from unittest.mock import patch

from mahler import cli, config, desktop

REPO = str(Path(__file__).resolve().parents[1])
MIN = 60 * 10**9


def cfg_with(defaults=None, projects=None):
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "config.toml"
        p.write_text(config.dumps_toml({"defaults": defaults or {}, "projects": projects or {}}))
        return config.load(p)


REAL_PROBE_SESSION = desktop.probe_session
REAL_PASSWORD_FREE = desktop.probe_password_free
_PATCHES = []


def setUpModule():
    # An unlocked console session unless a test says otherwise.
    for name, value in (("probe_session", lambda: False), ("wake_display", lambda: None),
                        ("probe_password_free", lambda: False)):
        p = patch.object(desktop, name, value)
        p.start()
        _PATCHES.append(p)


def tearDownModule():
    while _PATCHES:
        _PATCHES.pop().stop()


def console(**user):
    base = {"kCGSSessionOnConsoleKey": True, "kCGSSessionUserIDKey": 501,
            "kCGSessionLoginDoneKey": True}
    return {"IOConsoleLocked": False, "IOConsoleUsers": [{**base, **user}]}


def probe(ns):
    return lambda: ns


def deferring(msg):
    def p():
        raise desktop.Deferred(msg)
    return p


class StateCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = tmp.name
        patcher = patch.object(config, "STATE", self.state)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MAHLER_PROJECT", None)

    def run_main(self, argv, **kw):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = desktop.main(argv, **kw)
        return code, out.getvalue(), err.getvalue()


class SessionTests(StateCase):
    """mahler#1172: a locked screen or missing GUI session defers, it never launches."""

    def test_session_reading(self):
        self.assertFalse(desktop.parse_session(console(), 501))
        self.assertFalse(desktop.parse_session(console(CGSSessionScreenIsLocked=False), 501))
        self.assertTrue(desktop.parse_session(console(CGSSessionScreenIsLocked=True), 501))
        self.assertTrue(desktop.parse_session({**console(), "IOConsoleLocked": True}, 501))

    def test_missing_or_unreadable_session_denies(self):
        cases = {
            "nobody": {"IOConsoleUsers": []},
            "off console": console(kCGSSessionOnConsoleKey=False),
            "other user": console(kCGSSessionUserIDKey=502),
            "login pending": console(kCGSessionLoginDoneKey=False),
            "malformed lock": console(CGSSessionScreenIsLocked="Yes"),
            "no console lock key": {k: v for k, v in console().items() if k != "IOConsoleLocked"},
            "malformed console lock": {**console(), "IOConsoleLocked": "No"},
            "malformed root": ["not", "a", "dict"],
            "malformed users": {"IOConsoleUsers": "mike"},
            "ambiguous": {"IOConsoleUsers": console()["IOConsoleUsers"] * 2},
        }
        for label, root in cases.items():
            with self.subTest(label), self.assertRaises(desktop.Deferred):
                desktop.parse_session(root, 501)

    def test_lock_needing_a_password_defers(self):
        with self.assertRaisesRegex(desktop.Deferred, "^locked"):
            desktop.check(15, None, probe=probe(20 * MIN), session=lambda: True,
                          password_free=lambda: False)

    def test_password_free_shield_is_a_sleeping_display(self):
        # 2026-10-20: "Require password" = Never, yet the display-sleep shield
        # reported CGSSessionScreenIsLocked=true; any user activity lifted it.
        reason = desktop.check(15, None, probe=probe(20 * MIN), session=lambda: True,
                               password_free=lambda: True)
        self.assertIn("display asleep", reason)

    def test_password_free_probe_reads_sysadminctl(self):
        run = subprocess.CompletedProcess
        with patch.object(sys, "platform", "darwin"):
            for out, want in (("2026 sysadminctl[1] screenLock is off\n", True),
                              ("screenLock delay is immediate\n", False), ("", False)):
                with self.subTest(out), patch.object(subprocess, "run",
                                                     return_value=run([], 0, "", out)):
                    self.assertIs(REAL_PASSWORD_FREE(), want)
            with patch.object(subprocess, "run", side_effect=OSError("nope")):
                self.assertFalse(REAL_PASSWORD_FREE())
        with patch.object(sys, "platform", "linux"):
            self.assertFalse(REAL_PASSWORD_FREE())

    def test_probe_failures_deny(self):
        real = REAL_PROBE_SESSION
        with patch.object(sys, "platform", "linux"), self.assertRaises(desktop.Deferred):
            real()
        with patch.object(sys, "platform", "darwin"):
            for kw in ({"side_effect": subprocess.TimeoutExpired("ioreg", 5)},
                       {"side_effect": OSError("nope")},
                       {"return_value": subprocess.CompletedProcess([], 1, b"", b"")},
                       {"return_value": subprocess.CompletedProcess([], 0, b"<plist", b"")}):
                with self.subTest(kw), patch.object(subprocess, "run", **kw), \
                        self.assertRaises(desktop.Deferred):
                    real()

    def run_flagged(self, readings, password_free=True):
        """Run with the session probe returning `readings` in turn."""
        flag = os.path.join(self.state, "ran")
        cmd = [sys.executable, "-c", f"open({flag!r}, 'w')"]
        seen, order = iter(readings), []
        with patch.object(desktop, "probe_session", lambda: next(seen)), \
                patch.object(desktop, "probe_password_free", lambda: password_free), \
                patch.object(desktop, "wake_display", lambda: order.append("wake")), \
                patch.object(desktop.time, "sleep", lambda s: order.append(f"sleep {s}")):
            code, _, err = self.run_main(["run", "--"] + cmd, cfg=cfg_with(),
                                         probe=probe(20 * MIN))
        return code, err, os.path.exists(flag), order

    def test_locked_with_password_spawns_nothing(self):
        code, err, ran, order = self.run_flagged([True], password_free=False)
        self.assertEqual((code, ran, order), (desktop.EX_DEFERRED, False, []))
        self.assertIn("mahler desktop: GATE-DENIED locked", err)

    def test_sleeping_display_is_woken_then_run(self):
        code, _, ran, order = self.run_flagged([True, False])
        self.assertEqual((code, ran), (0, True))
        self.assertEqual(order, ["wake", f"sleep {desktop.WAKE_SETTLE}"])

    def test_shield_that_stays_up_after_wake_spawns_nothing(self):
        code, err, ran, _ = self.run_flagged([True, True])
        self.assertEqual((code, ran), (desktop.EX_DEFERRED, False))
        self.assertIn("stayed locked", err)

    def test_open_screen_wakes_without_waiting(self):
        code, _, ran, order = self.run_flagged([False])
        self.assertEqual((code, ran, order), (0, True, ["wake"]))


class ParseTests(unittest.TestCase):
    def test_parses_nonnegative_integer(self):
        text = '  | |   "HIDIdleTime" = 123456789\n'
        self.assertEqual(desktop.parse_idle_ns(text), 123456789)
        self.assertEqual(desktop.parse_idle_ns('"HIDIdleTime" = 0'), 0)
        self.assertEqual(desktop.parse_idle_ns('"HIDIdleTime" = 5\n"HIDIdleTime" = 5'), 5)

    def test_bad_readings_deny(self):
        for text in ("", "nothing here", '"HIDIdleTime" = -5', '"HIDIdleTime" = abc',
                     '"HIDIdleTime" = 1.5', '"HIDIdleTime" = 1\n"HIDIdleTime" = 2'):
            with self.subTest(text=text), self.assertRaises(desktop.Deferred) as cm:
                desktop.parse_idle_ns(text)
            self.assertNotIn("\n", str(cm.exception))

    def test_probe_failures_deny(self):
        with patch.object(desktop.sys, "platform", "linux"), self.assertRaises(desktop.Deferred):
            desktop.probe_idle_ns()
        with patch.object(desktop.sys, "platform", "darwin"):
            for effect in (subprocess.TimeoutExpired("ioreg", 5), FileNotFoundError("x")):
                with patch.object(desktop.subprocess, "run", side_effect=effect), \
                        self.assertRaises(desktop.Deferred):
                    desktop.probe_idle_ns()
            bad = subprocess.CompletedProcess([], 1, "", "")
            with patch.object(desktop.subprocess, "run", return_value=bad), \
                    self.assertRaises(desktop.Deferred):
                desktop.probe_idle_ns()


class CheckTests(unittest.TestCase):
    def test_threshold_boundary(self):
        self.assertIn("free", desktop.check(15, None, probe=probe(15 * MIN)))
        with self.assertRaises(desktop.Deferred) as cm:
            desktop.check(15, None, probe=probe(15 * MIN - 1))
        self.assertEqual(str(cm.exception), "in use: idle 15.0m < 15m")
        with self.assertRaises(desktop.Deferred) as cm:
            desktop.check(15, None, probe=probe(12 * 10**9))
        self.assertEqual(str(cm.exception), "in use: idle 12s < 15m")
        self.assertIn("free", desktop.check(0.5, None, probe=probe(30 * 10**9)))

    def test_ordinary_daytime_ignores_window(self):
        noon = lambda: datetime(2026, 1, 1, 12, 0)
        w = config.parse_gui_window("00:00-06:00")
        self.assertIn("free", desktop.check(15, w, probe=probe(20 * MIN), now=noon))

    def test_scheduled_window_edges(self):
        def at(h, m):
            return lambda: datetime(2026, 1, 1, h, m)
        w = config.parse_gui_window("00:00-06:00")
        ok = probe(20 * MIN)
        for t, allowed in (((0, 0), True), ((5, 59), True), ((6, 0), False), ((23, 59), False)):
            with self.subTest(t=t):
                if allowed:
                    desktop.check(15, w, True, ok, at(*t))
                else:
                    with self.assertRaises(desktop.Deferred):
                        desktop.check(15, w, True, ok, at(*t))

    def test_cross_midnight_and_empty_window(self):
        w = config.parse_gui_window("22:00-02:00")
        ok = probe(20 * MIN)
        # 2026-01-01 was Thursday
        for t, allowed in (((22, 0), True), ((1, 59), True), ((2, 0), False), ((21, 59), False)):
            with self.subTest(t=t):
                self.assertEqual(desktop.in_window(w, datetime(2026, 1, 1, *t)), allowed)
        noon = lambda: datetime(2026, 1, 1, 12, 0)
        self.assertIn("free", desktop.check(15, None, True, ok, noon))

    def test_cross_midnight_with_weekday_filter_judged_by_start_day(self):
        # Fri 22:00-02:00 window
        w = config.parse_gui_window("Fri 22:00-02:00")
        # 2026-01-02 was Friday (weekday 4), 2026-01-03 was Saturday (weekday 5)
        # Friday 22:00 -> in window (started Friday)
        self.assertTrue(desktop.in_window(w, datetime(2026, 1, 2, 22, 0)))
        # Friday 23:59 -> in window (started Friday)
        self.assertTrue(desktop.in_window(w, datetime(2026, 1, 2, 23, 59)))
        # Saturday 00:00 -> in window (range started Friday)
        self.assertTrue(desktop.in_window(w, datetime(2026, 1, 3, 0, 0)))
        # Saturday 01:59 -> in window (range started Friday)
        self.assertTrue(desktop.in_window(w, datetime(2026, 1, 3, 1, 59)))
        # Saturday 02:00 -> out of window (past end)
        self.assertFalse(desktop.in_window(w, datetime(2026, 1, 3, 2, 0)))
        # Saturday 22:00 -> out of window (would start Saturday, not Friday)
        self.assertFalse(desktop.in_window(w, datetime(2026, 1, 3, 22, 0)))
        # Sunday 01:00 -> out of window (would start Saturday, not Friday)
        self.assertFalse(desktop.in_window(w, datetime(2026, 1, 4, 1, 0)))
        # Friday 01:00 -> out of window (would start Thursday, not Friday)
        self.assertFalse(desktop.in_window(w, datetime(2026, 1, 2, 1, 0)))

        # Mon-Fri 22:00-02:00
        w_mf = config.parse_gui_window("Mon-Fri 22:00-02:00")
        # Friday 22:00 -> in window
        self.assertTrue(desktop.in_window(w_mf, datetime(2026, 1, 2, 22, 0)))
        # Saturday 01:00 -> in window (started Friday)
        self.assertTrue(desktop.in_window(w_mf, datetime(2026, 1, 3, 1, 0)))
        # Saturday 22:00 -> out (starts Saturday)
        self.assertFalse(desktop.in_window(w_mf, datetime(2026, 1, 3, 22, 0)))
        # Sunday 01:00 -> out (started Saturday)
        self.assertFalse(desktop.in_window(w_mf, datetime(2026, 1, 4, 1, 0)))
        # Sunday 22:00 -> out (starts Sunday)
        self.assertFalse(desktop.in_window(w_mf, datetime(2026, 1, 4, 22, 0)))
        # Monday 01:00 -> out (started Sunday)
        self.assertFalse(desktop.in_window(w_mf, datetime(2026, 1, 5, 1, 0)))
        # Monday 22:00 -> in window (starts Monday)
        self.assertTrue(desktop.in_window(w_mf, datetime(2026, 1, 5, 22, 0)))

    def test_multi_window_any_match_and_deferred_message(self):
        w = config.parse_gui_window("00:00-06:00, Mon-Fri 11:30-13:30")
        ok = probe(20 * MIN)
        # 2026-01-06 was Tuesday
        # Tuesday 03:00 -> in window (matches 00:00-06:00)
        self.assertIn("free", desktop.check(15, w, True, ok, lambda: datetime(2026, 1, 6, 3, 0)))
        # Tuesday 12:00 -> in window (matches Mon-Fri 11:30-13:30)
        self.assertIn("free", desktop.check(15, w, True, ok, lambda: datetime(2026, 1, 6, 12, 0)))
        # Saturday 03:00 (2026-01-10) -> in window (matches 00:00-06:00)
        self.assertIn("free", desktop.check(15, w, True, ok, lambda: datetime(2026, 1, 10, 3, 0)))
        # Saturday 12:00 -> outside (Mon-Fri 11:30-13:30 does not match Saturday)
        with self.assertRaises(desktop.Deferred) as cm:
            desktop.check(15, w, True, ok, lambda: datetime(2026, 1, 10, 12, 0))
        self.assertEqual(str(cm.exception),
                         "outside preferred windows 00:00-06:00, Mon-Fri 11:30-13:30 (now Sat 12:00)")
        # Tuesday 09:12 -> outside both
        with self.assertRaises(desktop.Deferred) as cm:
            desktop.check(15, w, True, ok, lambda: datetime(2026, 1, 6, 9, 12))
        self.assertEqual(str(cm.exception),
                         "outside preferred windows 00:00-06:00, Mon-Fri 11:30-13:30 (now Tue 09:12)")

    def test_window_checked_before_probe(self):
        calls = []
        w = config.parse_gui_window("00:00-06:00")
        with self.assertRaises(desktop.Deferred):
            desktop.check(15, w, True, lambda: calls.append(1) or 10**18,
                          lambda: datetime(2026, 1, 1, 12, 0))
        self.assertEqual(calls, [])


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg_with({"gui_idle_minutes": 20},
                            {"app": {"gui_idle_minutes": 5, "gui_window": "01:00-02:00"}})
        patcher = patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("MAHLER_PROJECT", None)

    def test_resolution_order(self):
        self.assertEqual(desktop.settings(self.cfg)[0], 20)
        self.assertEqual(desktop.settings(self.cfg, "app")[0], 5)
        os.environ["MAHLER_PROJECT"] = "app"
        self.assertEqual(desktop.settings(self.cfg)[0], 5)
        self.assertEqual(desktop.settings(self.cfg, "app")[1],
                         ((None, dtime(1, 0), dtime(2, 0)),))

    def test_project_flag_beats_env(self):
        cfg = cfg_with({}, {"a": {"gui_idle_minutes": 1}, "b": {"gui_idle_minutes": 2}})
        os.environ["MAHLER_PROJECT"] = "a"
        self.assertEqual(desktop.settings(cfg, "b")[0], 2)

    def test_unknown_project_fails(self):
        with self.assertRaises(ValueError):
            desktop.settings(self.cfg, "nope")
        os.environ["MAHLER_PROJECT"] = "nope"
        with self.assertRaises(ValueError):
            desktop.settings(self.cfg)

    def test_defaults(self):
        cfg = cfg_with()
        self.assertEqual(desktop.settings(cfg),
                         (15, ((None, dtime(0, 0), dtime(6, 0)),
                               (frozenset({0, 1, 2, 3, 4}), dtime(11, 30), dtime(13, 30)))))


class CliTests(StateCase):
    def test_advisory_check(self):
        code, out, _ = self.run_main([], cfg=cfg_with(), probe=probe(20 * MIN))
        self.assertEqual(code, 0)
        self.assertIn("free", out)

    def test_advisory_reserves_nothing(self):
        self.run_main([], cfg=cfg_with(), probe=probe(20 * MIN))
        fd = desktop._open_lock()
        self.assertIsNotNone(fd)
        os.close(fd)

    def spawn_marker(self):
        marker = os.path.join(self.state, "ran")
        return marker, [sys.executable, "-c", f"open({marker!r}, 'w').close()"]

    def test_denials_spawn_nothing(self):
        cases = {
            "idle": probe(MIN),
            "missing": deferring("idle unknown: no HIDIdleTime reading"),
            "negative": lambda: desktop.parse_idle_ns('"HIDIdleTime" = -1'),
            "malformed": lambda: desktop.parse_idle_ns('"HIDIdleTime" = x'),
            "timeout": deferring("idle unknown: ioreg timed out after 5s"),
            "unsupported": deferring("unsupported host"),
        }
        for name, p in cases.items():
            marker, cmd = self.spawn_marker()
            with self.subTest(name):
                code, _, err = self.run_main(["run", "--"] + cmd, cfg=cfg_with(), probe=p)
                self.assertEqual(code, 75)
                self.assertEqual(len(err.strip().splitlines()), 1)
                self.assertFalse(os.path.exists(marker))

    def test_outside_scheduled_window_spawns_nothing(self):
        marker, cmd = self.spawn_marker()
        code, _, err = self.run_main(["run", "--scheduled", "--"] + cmd, cfg=cfg_with(),
                                     probe=probe(20 * MIN),
                                     now=lambda: datetime(2026, 1, 1, 9, 0))
        self.assertEqual(code, 75)
        self.assertIn("preferred window", err)
        self.assertFalse(os.path.exists(marker))

    def test_invalid_invocation_and_config(self):
        marker, cmd = self.spawn_marker()
        for argv in (["run"], ["run", "--project", "nope", "--"] + cmd, ["--"] + cmd,
                     ["bogus"]):
            with self.subTest(argv=argv):
                code, _, _ = self.run_main(argv, cfg=cfg_with(), probe=probe(20 * MIN))
                self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(marker))
        with patch.object(config, "CONFIG_PATH", os.path.join(self.state, "c.toml")):
            Path(self.state, "c.toml").write_text("[defaults]\ngui_idle_minutes = 0\n")
            with patch.object(config, "load", side_effect=ValueError("bad")), \
                    redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["desktop", "run", "--"] + cmd), 2)
        self.assertFalse(os.path.exists(marker))

    def test_held_lock_spawns_nothing(self):
        fd = desktop._open_lock()
        self.addCleanup(os.close, fd)
        marker, cmd = self.spawn_marker()
        code, _, err = self.run_main(["run", "--"] + cmd, cfg=cfg_with(), probe=probe(20 * MIN))
        self.assertEqual(code, 75)
        self.assertIn("lock", err)
        self.assertFalse(os.path.exists(marker))
        code, _, _ = self.run_main([], cfg=cfg_with(), probe=probe(20 * MIN))
        self.assertEqual(code, 75)

    def test_args_status_and_single_probe(self):
        calls = []
        out_file = os.path.join(self.state, "argv")
        script = ("import sys; open(sys.argv[1], 'w').write(repr(sys.argv[2:])); "
                  "sys.exit(7)")
        code, _, err = self.run_main(
            ["run", "--", sys.executable, "-c", script, out_file, "a b", "--flag", "-x"],
            cfg=cfg_with(), probe=lambda: calls.append(1) or 20 * MIN)
        self.assertEqual(code, 7)
        self.assertEqual(calls, [1])
        self.assertEqual(Path(out_file).read_text(), repr(["a b", "--flag", "-x"]))
        self.assertIn("command ran and exited 7", err)

    def test_lock_released_after_exit_failure_and_spawn_error(self):
        ok = probe(20 * MIN)
        for cmd, expect in (([sys.executable, "-c", "pass"], 0),
                            ([sys.executable, "-c", "raise SystemExit(3)"], 3),
                            (["/nonexistent/definitely-missing"], 127)):
            with self.subTest(cmd=cmd[-1]):
                code, _, _ = self.run_main(["run", "--"] + cmd, cfg=cfg_with(), probe=ok)
                self.assertEqual(code, expect)
                fd = desktop._open_lock()
                self.assertIsNotNone(fd)
                os.close(fd)

    def test_project_identity_via_env(self):
        cfg = cfg_with({}, {"a": {"gui_idle_minutes": 1}})
        os.environ["MAHLER_PROJECT"] = "a"
        code, _, _ = self.run_main([], cfg=cfg, probe=probe(2 * MIN))
        self.assertEqual(code, 0)


WRAPPER = """
import sys
from mahler import desktop
desktop.probe_idle_ns = lambda: 10**18
desktop.probe_session = lambda: False
desktop.wake_display = lambda: None
desktop.probe_password_free = lambda: False
sys.exit(desktop.main(sys.argv[1:]))
"""


class ProcessTests(unittest.TestCase):
    """Real OS processes sharing one state directory."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = tmp.name
        self.env = {**os.environ, "MAHLER_HOME": self.state, "PYTHONPATH": REPO}
        self.env.pop("MAHLER_PROJECT", None)
        Path(self.state, "config.toml").write_text(
            "[projects.a]\nenabled = false\n[projects.b]\nenabled = false\n")

    def wrapper(self, project, *cmd, pipes=True):
        # A killed wrapper's child keeps inherited pipes open, so communicate()
        # would wait for the child; pass pipes=False when that matters.
        io_ = subprocess.PIPE if pipes else subprocess.DEVNULL
        return subprocess.Popen([sys.executable, "-c", WRAPPER, "run", "--project", project,
                                 "--", *cmd], env=self.env, stdout=io_, stderr=io_, text=True)

    def child_script(self, name, seconds):
        start = os.path.join(self.state, name + ".start")
        code = (f"import os, time; open({start!r}, 'w').write(str(os.getpid())); "
                f"time.sleep({seconds})")
        return start, [sys.executable, "-c", code]

    def wait_for(self, path, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if os.path.exists(path) and Path(path).read_text().strip():
                return int(Path(path).read_text())
            time.sleep(0.05)
        self.fail(f"{path} never appeared")

    def test_second_wrapper_defers_while_first_runs(self):
        start, cmd = self.child_script("first", 3)
        first = self.wrapper("a", *cmd)
        self.addCleanup(first.kill)
        self.wait_for(start)
        marker = os.path.join(self.state, "second.ran")
        second = self.wrapper("b", sys.executable, "-c", f"open({marker!r}, 'w').close()")
        _, err = second.communicate(timeout=20)
        self.assertEqual(second.returncode, 75)
        self.assertIn("lock", err)
        self.assertFalse(os.path.exists(marker))
        first.communicate(timeout=20)
        self.assertEqual(first.returncode, 0)
        third = self.wrapper("b", sys.executable, "-c", "pass")
        third.communicate(timeout=20)
        self.assertEqual(third.returncode, 0)

    def test_terminated_wrapper_keeps_lock_until_child_ends(self):
        start, cmd = self.child_script("kid", 3)
        first = self.wrapper("a", *cmd, pipes=False)
        self.addCleanup(first.kill)
        child_pid = self.wait_for(start)
        self.addCleanup(lambda: subprocess.run(["kill", str(child_pid)], capture_output=True))
        first.kill()  # SIGKILL: no chance to forward or release anything
        first.wait(timeout=10)
        probe_run = self.wrapper("b", sys.executable, "-c", "pass")
        probe_run.communicate(timeout=20)
        self.assertEqual(probe_run.returncode, 75)
        os.kill(child_pid, signal.SIGTERM)
        deadline = time.time() + 10
        while time.time() < deadline:
            again = self.wrapper("b", sys.executable, "-c", "pass")
            again.communicate(timeout=20)
            if again.returncode == 0:
                return
            time.sleep(0.1)
        self.fail("lock never freed after the child ended")

    def test_sigterm_is_forwarded_and_status_reported(self):
        start, cmd = self.child_script("fwd", 30)
        first = self.wrapper("a", *cmd)
        self.addCleanup(first.kill)
        self.wait_for(start)
        first.send_signal(signal.SIGTERM)
        first.communicate(timeout=20)
        self.assertEqual(first.returncode, 128 + signal.SIGTERM)
        again = self.wrapper("b", sys.executable, "-c", "pass")
        again.communicate(timeout=20)
        self.assertEqual(again.returncode, 0)


if __name__ == "__main__":
    unittest.main()
