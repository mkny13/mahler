"""The desktop gate (D40): parsing, thresholds, windows, CLI launch suppression and
real-process mutual exclusion. `ioreg` is never run: the probe is stubbed."""

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
        for t, allowed in (((22, 0), True), ((1, 59), True), ((2, 0), False), ((21, 59), False)):
            with self.subTest(t=t):
                self.assertEqual(desktop.in_window(w, dtime(*t)), allowed)
        noon = lambda: datetime(2026, 1, 1, 12, 0)
        self.assertIn("free", desktop.check(15, None, True, ok, noon))

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
                         (dtime(1, 0), dtime(2, 0)))

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
        self.assertEqual(desktop.settings(cfg), (15, (dtime(0, 0), dtime(6, 0))))


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
                                     now=lambda: datetime(2026, 1, 1, 12, 0))
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
