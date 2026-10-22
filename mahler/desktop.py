"""The desktop gate (DESIGN D40): GUI-driving work runs only when the Mac mini is free.

`mahler desktop` is an advisory check; `mahler desktop run -- <cmd>` is the
authoritative launch path. The wrapper takes one host-wide flock, then makes a
single fresh idle, console-session (unlocked) and, with --scheduled,
preferred-window check, then runs the command in the foreground. The child inherits the lock descriptor, so a killed
wrapper cannot free the lock while the command still drives the desktop.
"""

import argparse
import fcntl
import os
import plistlib
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, time as dtime

from . import config

EX_OK = 0
EX_INVALID = 2
EX_DEFERRED = 75   # EX_TEMPFAIL: gate closed or lock held; nothing was launched
# Printed with every denial. A child that itself exits 75 never prints it, so a
# run's BLOCKED reason can prove that nothing was launched (mahler#1172).
DENIED_MARKER = "GATE-DENIED"
IOREG_TIMEOUT = 5
LOCK_NAME = "desktop.lock"
_IDLE_RE = re.compile(r'"HIDIdleTime"\s*=\s*(\S+)')


class Deferred(Exception):
    """The gate is closed; the message is the one-line reason."""


def lock_path():
    return os.path.join(config.STATE, LOCK_NAME)


def probe_idle_ns():
    """HIDIdleTime in nanoseconds from `ioreg -c IOHIDSystem`; raises Deferred
    for every failure so that a missing reading never grants permission."""
    if sys.platform != "darwin":
        raise Deferred("unsupported host: HID idle time is only readable on macOS")
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True,
                             timeout=IOREG_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise Deferred(f"idle unknown: ioreg timed out after {IOREG_TIMEOUT}s") from None
    except OSError as e:
        raise Deferred(f"idle unknown: ioreg failed to run ({e.strerror or e})") from None
    if out.returncode != 0:
        raise Deferred(f"idle unknown: ioreg exited {out.returncode}")
    return parse_idle_ns(out.stdout)


def parse_idle_ns(text):
    values = set()
    for raw in _IDLE_RE.findall(text or ""):
        if not raw.isascii() or not raw.isdigit():
            raise Deferred("idle unknown: malformed HIDIdleTime reading")
        values.add(int(raw))
    if not values:
        raise Deferred("idle unknown: no HIDIdleTime reading")
    if len(values) > 1:
        raise Deferred("idle unknown: ambiguous HIDIdleTime readings")
    return values.pop()


def probe_session():
    """Raise Deferred unless this user owns the console GUI session, read from
    `ioreg -n Root -d1 -a` (mahler#1172); return True when the screen is
    shielded (locked) and False when it is open. A locked screen passes the
    idle check, so without this a GUI run starts and fails on the lock screen."""
    if sys.platform != "darwin":
        raise Deferred("unsupported host: the console session is only readable on macOS")
    try:
        out = subprocess.run(["ioreg", "-n", "Root", "-d1", "-a"], capture_output=True,
                             timeout=IOREG_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise Deferred(f"session unknown: ioreg timed out after {IOREG_TIMEOUT}s") from None
    except OSError as e:
        raise Deferred(f"session unknown: ioreg failed to run ({e.strerror or e})") from None
    if out.returncode != 0:
        raise Deferred(f"session unknown: ioreg exited {out.returncode}")
    try:
        root = plistlib.loads(out.stdout)
    except Exception:                           # noqa: BLE001 — any bad plist denies
        raise Deferred("session unknown: malformed ioreg output") from None
    return parse_session(root, os.getuid())


def parse_session(root, uid):
    if not isinstance(root, dict) or not isinstance(root.get("IOConsoleUsers", []), list):
        raise Deferred("session unknown: malformed console session reading")
    on_console = [u for u in root.get("IOConsoleUsers", [])
                  if isinstance(u, dict) and u.get("kCGSSessionOnConsoleKey") is True]
    if not on_console:
        raise Deferred("no GUI session: nobody is logged in at the console")
    if len(on_console) > 1:
        raise Deferred("session unknown: ambiguous console sessions")
    user = on_console[0]
    if user.get("kCGSSessionUserIDKey") != uid:
        raise Deferred("no GUI session: the console belongs to another user")
    if user.get("kCGSessionLoginDoneKey") is not True:
        raise Deferred("no GUI session: console login has not finished")
    # IOConsoleLocked is always present; CGSSessionScreenIsLocked only while locked.
    console_locked, screen_locked = root.get("IOConsoleLocked"), user.get("CGSSessionScreenIsLocked")
    if not isinstance(console_locked, bool) or screen_locked not in (None, True, False):
        raise Deferred("session unknown: missing or malformed screen-lock reading")
    return console_locked or screen_locked is True


def probe_password_free():
    """True when waking needs no password (`sysadminctl -screenLock status`
    says off). A sleeping display still raises the lock shield then, and any
    user activity lifts it. Every failure reads as False (a password needed)."""
    if sys.platform != "darwin":
        return False
    try:
        out = subprocess.run(["sysadminctl", "-screenLock", "status"], capture_output=True,
                             text=True, timeout=IOREG_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(re.search(r"\bscreenLock is off\b", (out.stdout or "") + (out.stderr or "")))


def wake_display():
    """Best effort: declare user activity so a sleeping display wakes and a
    password-free lock shield lifts before the GUI run starts."""
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(["caffeinate", "-u", "-t", "2"], timeout=IOREG_TIMEOUT,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass


def _match_single_window(win, dt):
    days, start, end = (None, win[0], win[1]) if len(win) == 2 else win
    t = dt.time() if isinstance(dt, datetime) else dt
    weekday = dt.weekday() if isinstance(dt, datetime) else None

    if start < end:
        if not (start <= t < end):
            return False
        return days is None or (weekday is not None and weekday in days)
    else:  # cross-midnight: start > end
        if t >= start:
            start_day = weekday
            return days is None or (start_day is not None and start_day in days)
        if t < end:
            start_day = (weekday - 1) % 7 if weekday is not None else None
            return days is None or (start_day is not None and start_day in days)
        return False


def in_window(windows, dt):
    """True if dt falls inside any range in windows.
    windows is a tuple/list of (days, start, end) tuples (or a single window tuple).
    Cross-midnight ranges with weekday filters are judged by the start day."""
    if not windows:
        return False
    if isinstance(windows, tuple) and len(windows) in (2, 3) and isinstance(windows[-1], (datetime, dtime)):
        windows = (windows,)
    for win in windows:
        if _match_single_window(win, dt):
            return True
    return False


def _fmt_idle(seconds):
    return f"{seconds:.0f}s" if seconds < 120 else f"{seconds / 60:.1f}m"


def _check(minutes, window, scheduled, probe, now, session, password_free):
    """-> (reason, shielded); raises Deferred."""
    probe = probe or probe_idle_ns
    session = session or probe_session
    password_free = password_free or probe_password_free
    if scheduled and window:
        dt = now() if callable(now) else (now or datetime.now())
        if not in_window(window, dt):
            raise Deferred(f"outside preferred windows {config.format_gui_window(window)} "
                           f"(now {config._DAY_ABBR[dt.weekday()]} {dt:%H:%M})")
    idle = probe() / 1e9
    need = minutes * 60
    if idle < need:
        raise Deferred(f"in use: idle {_fmt_idle(idle)} < {minutes:g}m")
    reason = f"free: idle {_fmt_idle(idle)} >= {minutes:g}m"
    if not session():
        return reason, False
    if not password_free():
        raise Deferred("locked: the screen is locked and waking it needs a password")
    return reason + " (display asleep; the lock shield lifts on wake)", True


def check(minutes, window, scheduled=False, probe=None, now=None, session=None,
          password_free=None):
    """Return a one-line 'ok' reason, or raise Deferred. Reads the clock and
    each probe at most once. A shielded screen that needs no password counts as
    open: it is only a sleeping display, and `run` wakes it before launching."""
    return _check(minutes, window, scheduled, probe, now, session, password_free)[0]


WAKE_SETTLE = 3     # seconds for loginwindow to lift the shield after a wake


def settings(cfg, project=None):
    """(minutes, window) after resolving --project, MAHLER_PROJECT, then globals.
    Raises ValueError for an unknown project."""
    name = project or os.environ.get("MAHLER_PROJECT")
    if name:
        if name not in cfg["projects"]:
            raise ValueError(f"unknown project {name!r}")
        pol = config.project_policy(cfg, name)
    else:
        pol = cfg["defaults"]
    return pol["gui_idle_minutes"], config.parse_gui_window(pol["gui_window"])


def _open_lock():
    config.ensure_private_dir(config.STATE)
    fd = os.open(lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _lock_busy():
    return Deferred("busy: another GUI run holds the desktop lock")


def run_check(minutes, window, scheduled=False, probe=None, now=None):
    """Advisory: lock available + gate open; reserves nothing."""
    fd = _open_lock()
    if fd is None:
        raise _lock_busy()
    os.close(fd)
    return check(minutes, window, scheduled, probe, now)


def run_command(argv, minutes, window, scheduled=False, probe=None, now=None):
    """Lock, check once, run `argv` in the foreground, return its exit status."""
    fd = _open_lock()
    if fd is None:
        raise _lock_busy()
    try:
        _, shielded = _check(minutes, window, scheduled, probe, now, None, None)
        wake_display()
        if shielded:
            time.sleep(WAKE_SETTLE)
            if probe_session():
                raise Deferred("locked: the screen stayed locked after waking the display")
        os.set_inheritable(fd, True)
        try:
            child = subprocess.Popen(argv, pass_fds=(fd,))
        except OSError as e:
            code = 127 if isinstance(e, FileNotFoundError) else 126
            print(f"mahler desktop: failed to launch {argv[0]!r}: {e.strerror or e}",
                  file=sys.stderr)
            return code
        sigs = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
        old = {s: signal.signal(s, lambda n, _f: child.poll() is None and child.send_signal(n))
               for s in sigs}
        try:
            while True:
                try:
                    status = child.wait()
                    break
                except InterruptedError:
                    continue
        finally:
            for s, h in old.items():
                signal.signal(s, h)
        code = 128 - status if status < 0 else status
        if code:
            print(f"mahler desktop: command ran and exited {code}", file=sys.stderr)
        return code
    finally:
        os.close(fd)


def main(argv, cfg=None, probe=None, now=None):
    """Entry for `mahler desktop ...`; returns the process exit status."""
    argv = list(argv)
    command = None
    if "--" in argv:
        i = argv.index("--")
        argv, command = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(prog="mahler desktop", add_help=True,
                                 description="gate GUI-driving work on the mini being idle (D40)")
    ap.add_argument("mode", nargs="?", choices=("run",), help="run: lock, check, then run -- CMD")
    ap.add_argument("--project")
    ap.add_argument("--scheduled", action="store_true",
                    help="also require the preferred window (gui_window)")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return EX_OK if e.code in (0, None) else EX_INVALID
    if a.mode == "run":
        if not command:
            print("mahler desktop: run needs a command after --", file=sys.stderr)
            return EX_INVALID
    elif command is not None:
        print("mahler desktop: a command needs `run`", file=sys.stderr)
        return EX_INVALID
    try:
        minutes, window = settings(cfg if cfg is not None else config.load(), a.project)
    except ValueError as e:
        print(f"mahler desktop: {e}", file=sys.stderr)
        return EX_INVALID
    try:
        if a.mode == "run":
            return run_command(command, minutes, window, a.scheduled, probe, now)
        print(run_check(minutes, window, a.scheduled, probe, now))
        return EX_OK
    except Deferred as e:
        print(f"mahler desktop: {DENIED_MARKER} {e}", file=sys.stderr)
        return EX_DEFERRED
