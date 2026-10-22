"""The desktop gate (DESIGN D40): GUI-driving work runs only when the Mac mini is free.

`mahler desktop` is an advisory check; `mahler desktop run -- <cmd>` is the
authoritative launch path. The wrapper takes one host-wide flock, then makes a
single fresh idle (and, with --scheduled, preferred-window) check, then runs the
command in the foreground. The child inherits the lock descriptor, so a killed
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
import json
from datetime import datetime, timedelta, timezone

from . import config

EX_OK = 0
EX_INVALID = 2
EX_DEFERRED = 75   # EX_TEMPFAIL: gate closed or lock held; nothing was launched
IOREG_TIMEOUT = 5
SESSION_TIMEOUT = 5
LOCK_NAME = "desktop.lock"
_IDLE_RE = re.compile(r'"HIDIdleTime"\s*=\s*(\S+)')


DENIED_MARKER = "GATE-DENIED no-child"   # one stderr line: the gate said no, nothing launched
CATEGORIES = ("locked", "unavailable", "busy", "idle", "outside-window")


class Deferred(Exception):
    """The gate is closed; the message is the one-line reason."""

    def __init__(self, message, category="unavailable"):
        super().__init__(message)
        self.category = category


def lock_path():
    return os.path.join(config.STATE, LOCK_NAME)


def probe_idle_ns():
    """HIDIdleTime in nanoseconds from `ioreg -c IOHIDSystem`; raises Deferred
    for every failure so that a missing reading never grants permission."""
    if sys.platform != "darwin":
        raise Deferred("unsupported host: HID idle time is only readable on macOS", "unavailable")
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True,
                             timeout=IOREG_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise Deferred(f"idle unknown: ioreg timed out after {IOREG_TIMEOUT}s", "idle") from None
    except OSError as e:
        raise Deferred(f"idle unknown: ioreg failed to run ({e.strerror or e})", "idle") from None
    if out.returncode != 0:
        raise Deferred(f"idle unknown: ioreg exited {out.returncode}", "idle")
    return parse_idle_ns(out.stdout)


def parse_idle_ns(text):
    values = set()
    for raw in _IDLE_RE.findall(text or ""):
        if not raw.isascii() or not raw.isdigit():
            raise Deferred("idle unknown: malformed HIDIdleTime reading", "idle")
        values.add(int(raw))
    if not values:
        raise Deferred("idle unknown: no HIDIdleTime reading", "idle")
    if len(values) > 1:
        raise Deferred("idle unknown: ambiguous HIDIdleTime readings", "idle")
    return values.pop()


def probe_session():
    """None when exactly one on-console GUI session is explicitly unlocked;
    raises Deferred (never logging the session dictionary) for anything else."""
    if sys.platform != "darwin":
        raise Deferred("unsupported host: GUI session state is only readable on macOS")
    try:
        out = subprocess.run(["ioreg", "-n", "Root", "-d1", "-a"], capture_output=True,
                             timeout=SESSION_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise Deferred(f"session unknown: ioreg timed out after {SESSION_TIMEOUT}s") from None
    except OSError as e:
        raise Deferred(f"session unknown: ioreg failed to run ({e.strerror or e})") from None
    if out.returncode != 0:
        raise Deferred(f"session unknown: ioreg exited {out.returncode}")
    return parse_session(out.stdout)


def parse_session(data):
    """Evaluate `ioreg -n Root -d1 -a` plist bytes; see probe_session."""
    try:
        root = plistlib.loads(data)
    except Exception:                                   # noqa: BLE001 — any bad plist denies
        raise Deferred("session unknown: unreadable console session data") from None
    roots = root if isinstance(root, list) else [root]
    entries = [r["IOConsoleUsers"] for r in roots if isinstance(r, dict) and "IOConsoleUsers" in r]
    if len(entries) != 1 or not isinstance(entries[0], list):
        raise Deferred("session unknown: no unique console session list")
    on_console = [u for u in entries[0]
                  if isinstance(u, dict) and u.get("kCGSSessionOnConsoleKey") is True]
    if len(on_console) != 1:
        raise Deferred("session unavailable: no unique on-console GUI session")
    locked = on_console[0].get("CGSSessionScreenIsLocked")
    if locked is True:
        raise Deferred("session locked: the console screen is locked", "locked")
    if locked is not False:
        raise Deferred("session unknown: no explicit unlocked evidence")
    return None


def in_window(window, t):
    start, end = window
    return start <= t < end if start < end else (t >= start or t < end)


def _fmt_idle(seconds):
    return f"{seconds:.0f}s" if seconds < 120 else f"{seconds / 60:.1f}m"


def check(minutes, window, scheduled=False, probe=None, now=None, session=None):
    """Return a one-line 'ok' reason, or raise Deferred. Reads the clock and the
    probes at most once each; `session` (a probe_session-style callable) is
    consulted after the window and before idle."""
    probe = probe or probe_idle_ns
    if scheduled and window:
        t = (now or datetime.now)().time()
        if not in_window(window, t):
            raise Deferred(f"outside preferred window {window[0]:%H:%M}-{window[1]:%H:%M} "
                           f"(now {t:%H:%M})", "outside-window")
    if session is not None:
        session()
    idle = probe() / 1e9
    need = minutes * 60
    if idle < need:
        raise Deferred(f"in use: idle {_fmt_idle(idle)} < {minutes:g}m", "idle")
    return f"free: idle {_fmt_idle(idle)} >= {minutes:g}m"


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
    return Deferred("busy: another GUI run holds the desktop lock", "busy")


def run_check(minutes, window, scheduled=False, probe=None, now=None, session=None):
    """Advisory: lock available + gate open; reserves nothing."""
    fd = _open_lock()
    if fd is None:
        raise _lock_busy()
    os.close(fd)
    return check(minutes, window, scheduled, probe, now, session)


def run_command(argv, minutes, window, scheduled=False, probe=None, now=None, session=None):
    """Lock, check once (session included), run `argv` in the foreground, return
    its exit status."""
    fd = _open_lock()
    if fd is None:
        raise _lock_busy()
    try:
        check(minutes, window, scheduled, probe, now, session)
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


def main(argv, cfg=None, probe=None, now=None, session=None):
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
    session = session or probe_session
    try:
        if a.mode == "run":
            return run_command(command, minutes, window, a.scheduled, probe, now, session)
        print(run_check(minutes, window, a.scheduled, probe, now, session))
        return EX_OK
    except Deferred as e:
        print(f"mahler desktop: {DENIED_MARKER} category={e.category}: {e}", file=sys.stderr)
        return EX_DEFERRED


# ---------- item-level GUI waits (D40) ----------

def next_window_start(window, now):
    """The first start of `window` strictly after `now`, in host-local calendar
    time, as an aware datetime. Call with an aware `now`."""
    start = window[0]
    day = now.astimezone().date()
    for _ in range(4):
        cand = datetime.combine(day, start).astimezone()
        if cand > now:
            return cand
        day += timedelta(days=1)
    raise ValueError("no future window start")


def gui_wait_blocks(led, pol, project, number):
    """True while the item's persisted GUI wait forbids launching it. A
    configuration hold that now has a valid window becomes a timed wait for the
    next future start rather than releasing the item."""
    rec = led.gui_wait(project, number)
    if not rec:
        return False
    if rec.get("hold"):
        window = None
        try:
            window = config.parse_gui_window(pol["gui_window"])
        except ValueError:
            pass
        if not window:
            return True
        rec = {**rec, "hold": False, "retry_at": next_window_start(window, led.now()).astimezone(
            timezone.utc).isoformat()}
        led.set_gui_wait(project, number, rec)
    return led.now() < parse_ts(rec["retry_at"])


def parse_ts(value):
    return datetime.fromisoformat(value)


def plan_wait(led, pol):
    """(retry_at ISO or None, hold) for a denial happening now."""
    try:
        window = config.parse_gui_window(pol["gui_window"])
    except ValueError:
        window = None
    if not window:
        return None, True
    return next_window_start(window, led.now()).astimezone(timezone.utc).isoformat(), False
