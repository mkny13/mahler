"""D43 account-bound discussion worker and console-owned process supervisor.

No scheduler entry point: only already accepted operator turns are processed.
The worker owns its SQLite connection and flock; each engine process
inherits that lock, preventing recovery before an orphaned engine is stopped.
"""
import argparse
from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import time

from . import config, platforms, usage
from .discuss_tools import TOOL_FIELDS
from .ledger import Ledger, parse

MAX_OUTPUT = 256 * 1024
MAX_TEXT = 64000
PARENT_PID = os.getppid()


class TurnError(Exception):
    def __init__(self, code, message, retryable=True):
        self.error = dict(code=code, message=message, retryable=retryable)
        super().__init__(message)


def stop_process(child):
    """Kill the whole engine/tool group and reap its direct child."""
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait()


def launch_args(executable, mcp, engine_id=None):
    # Checked against installed `claude --help` (2026-10-11). --bare is
    # deliberately absent: it disables subscription OAuth/keychain auth.
    argv = [executable, '-p', '--output-format', 'json', '--restricted',
            '--setting-sources', '', '--settings',
            json.dumps({'disableAllHooks': True, 'enabledPlugins': {}}),
            '--tools', '', '--disable-slash-commands', '--no-chrome',
            '--strict-mcp-config', '--mcp-config', json.dumps(mcp),
            '--permission-mode', 'dontAsk', '--allowedTools',
            ','.join('mcp__discussion__' + name for name in TOOL_FIELDS),
            '--model', 'sonnet', '--system-prompt',
            (Path(config.REPO_ROOT) / 'recipes/discuss.md').read_text()]
    if engine_id:
        argv += ['--resume', engine_id]
    return argv


def engine_result(data, returncode):
    if returncode:
        raise TurnError('engine_error', 'The discussion engine stopped without a reply.')
    try:
        value = json.loads(data)
        if (value.get('type') != 'result' or value.get('is_error') is not False
                or value.get('subtype') != 'success'):
            raise ValueError()
        text, session = value['result'], value['session_id']
        if (not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT
                or not isinstance(session, str) or not session or len(session) > 200):
            raise ValueError()
        return text, session
    except (ValueError, KeyError, TypeError, AttributeError):
        raise TurnError('invalid_result', 'The discussion engine returned an invalid reply.') from None


def run_engine(argv, env, cwd, prompt, deadline, heartbeat, lock_fd=None):
    # Files bound memory while polling bounds disk output and the wall deadline.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err, tempfile.TemporaryFile() as inp:
        inp.write(prompt.encode())
        inp.seek(0)
        child = subprocess.Popen(argv, cwd=cwd, env=env, stdin=inp, stdout=out, stderr=err,
                                 start_new_session=True,
                                 pass_fds=() if lock_fd is None else (lock_fd,))
        receipt = os.environ.get('MAHLER_DISCUSSION_PROCESS_FILE')
        try:
            if receipt:
                Path(receipt).write_text(json.dumps({'worker': os.getpid(), 'engine': child.pid}))
            while child.poll() is None:
                if os.getppid() != PARENT_PID:
                    raise SystemExit(0)
                if time.monotonic() >= deadline:
                    raise TurnError('timeout', 'No reply within 2 minutes.')
                if os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > MAX_OUTPUT:
                    raise TurnError('invalid_result', 'The discussion output exceeded its limit.')
                heartbeat()
                time.sleep(.1)
            if os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > MAX_OUTPUT:
                raise TurnError('invalid_result', 'The discussion output exceeded its limit.')
            out.seek(0)
            return engine_result(out.read(MAX_OUTPUT), child.returncode)
        finally:
            # Also terminate a tool subprocess that outlived the CLI.
            stop_process(child)
            if receipt:
                Path(receipt).unlink(missing_ok=True)


def process_turn(cfg, led, turn, *, runner=run_engine, lock_fd=None, config_path=config.CONFIG_PATH):
    token = led.claim_chat_turn(turn['id'])
    if not token:
        led.recover_chat_turns()
        return
    try:
        session = led.chat_session(turn['session_id'])
        pol = config.project_policy(cfg, session['project'])
        if (session['project'] not in cfg['projects'] or not pol.get('enabled', True)
                or pol['repo'] != session['repo'] or config.accounts_of(pol) != [session['account']]):
            raise TurnError('binding_mismatch', 'Project/account configuration changed. Start a new discussion.', False)
        work = session['account'] != config.DEFAULT_ACCOUNT
        expected_engine = 'agent-sdk' if work else 'claude'
        if session['engine'] != expected_engine:
            raise TurnError('binding_mismatch', 'Discussion engine changed. Start a new discussion.', False)
        if not work:
            platform, samples = usage.discussion_capacity(cfg, led)
            if platform:
                usage.record_discussion_probe_usage(SimpleNamespace(cfg=cfg, led=led), samples, platform)
            executable = platforms.claude_exe()
            if not platform or not executable:
                raise TurnError('quota_unavailable', 'Personal subscription login or quota is unavailable.')
        prior = [json.loads(t['result']) for t in led.chat_turns(session['id']) if t['result']]
        engine_id = next((r['engine_session_id'] for r in reversed(prior) if r['engine_session_id']), None)
        mutations = led.q('SELECT operation_id,kind,payload,status FROM chat_mutations WHERE session_id=?',
                          (session['id'],))
        prompt = json.dumps({'message': turn['user_text'], 'retry_of': turn['retry_of'],
                             'existing_mutations': [dict(m) for m in mutations]}, ensure_ascii=False)
        if len(prompt.encode()) > MAX_OUTPUT:
            raise TurnError('invalid_result', 'Discussion retry context exceeds its limit.')
        # A stable empty cwd preserves Claude's project-scoped resume storage.
        directory = str(Path(led.path).resolve().parent / 'discussion-sessions' / session['id'])
        config.ensure_private_dir(directory)
        # Explicit absolute module bootstrap works from this empty working directory.
        bootstrap = 'import sys; sys.path.insert(0, ' + repr(config.REPO_ROOT) + '); from mahler.discuss import main; main()'
        mcp = {'mcpServers': {'discussion': {'command': sys.executable, 'args': [
            '-c', bootstrap, '--db', str(led.path), '--config', str(config_path), '--mcp', session['id'], str(turn['id']), token]}}}
        remaining = (parse(turn['deadline_at']) - led.now()).total_seconds()
        if remaining <= 0:
            raise TurnError('timeout', 'No reply within 2 minutes.')
        if work:
            from . import discuss_credits
            # The trusted issue server resolves account identity paths against
            # the worker's home, independently of the SDK's empty session HOME.
            mcp['mcpServers']['discussion']['env'] = {'HOME': config.HOME}
            with discuss_credits.authorization(cfg, led, session, turn, token) as (executable, pconf, env, heartbeat):
                # Gateway setup also consumes the accepted turn's deadline.
                remaining = (parse(turn['deadline_at']) - led.now()).total_seconds()
                if remaining <= 0:
                    raise TurnError('timeout', 'No reply within 2 minutes.')
                text, engine_id = runner(discuss_credits.launch_args(executable, pconf, mcp, directory, engine_id),
                    env, directory, prompt, time.monotonic() + remaining, heartbeat, lock_fd)
                heartbeat(force=True)
        else:
            usage.record_discussion_usage(cfg, led, platform, turn['id'])
            text, engine_id = runner(launch_args(executable, mcp, engine_id), config.discussion_env(),
                directory, prompt, time.monotonic() + remaining,
                lambda: led.heartbeat_chat_turn(turn['id'], token), lock_fd)
        led.complete_chat_turn(turn['id'], token, assistant_text=text, engine_session_id=engine_id)
    except Exception as exc:
        error = exc.error if isinstance(exc, TurnError) else dict(
            code='engine_error', message='The discussion worker failed. Your message is saved.', retryable=True)
        if parse(turn['deadline_at']) <= led.now():
            led.recover_chat_turns()
        else:
            led.complete_chat_turn(turn['id'], token, assistant_text=error['message'], error=error)


def worker(db, load_cfg=config.load, stop=None, config_path=config.CONFIG_PATH):
    stop = stop or threading.Event()
    with open(str(db) + '.discussion.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 75
        with closing(Ledger(db)) as led:
            # The exclusive lock proves the old worker and its engines are gone.
            led.recover_chat_turns(stale_seconds=0)
            while not stop.is_set():
                if os.getppid() != PARENT_PID:
                    break
                try:
                    led.recover_chat_turns()
                    turn = led.q1("SELECT * FROM chat_turns WHERE status='queued' ORDER BY id LIMIT 1")
                    if turn:
                        process_turn(load_cfg(), led, turn, lock_fd=lock.fileno(), config_path=config_path)
                except Exception:
                    led.set_kv('discussion:worker', 'Worker error; retrying queue polling.')
                stop.wait(1)
    return 0


class Supervisor:
    """One child per console, bounded restart backoff, no model calls here."""
    def __init__(self, db):
        self.db = str(db)
        self.stop = threading.Event()
        self.child = None
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def run(self):
        delay = 1
        with closing(Ledger(self.db)) as led:
            while not self.stop.is_set():
                try:
                    env = dict(config.discussion_env(), MAHLER_DISCUSSION_PROCESS_FILE=self.db + '.discussion-process')
                    self.child = subprocess.Popen([sys.executable, '-m', 'mahler.discuss', '--db', self.db, '--config', config.CONFIG_PATH],
                        cwd=config.REPO_ROOT, env=env, start_new_session=True)
                    led.set_kv('discussion:worker', 'Running')
                    while self.child.poll() is None and not self.stop.wait(.2):
                        pass
                    if self.stop.is_set():
                        self.child.terminate()
                        try:
                            self.child.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            stop_process(self.child)
                        self.clean_engine()
                        stopped = threading.Event()
                        stopped.set()
                        worker(self.db, stop=stopped)
                        break
                    self.child.wait()
                    self.clean_engine()
                    led.set_kv('discussion:worker', 'Worker exited; restarting.')
                    led.event('discussion_worker_failed', detail='Worker exited; restarting.')
                except Exception:
                    led.set_kv('discussion:worker', 'Worker start failed; retrying.')
                    led.event('discussion_worker_failed', detail='Worker start failed; retrying.')
                if self.stop.wait(delay):
                    break
                delay = min(delay * 2, 30)

    def clean_engine(self):
        path = Path(self.db + '.discussion-process')
        try:
            record = json.loads(path.read_text())
            if record['worker'] == self.child.pid:
                try:
                    os.killpg(record['engine'], signal.SIGKILL)
                except ProcessLookupError:
                    pass
                path.unlink(missing_ok=True)
        except (OSError, ValueError, KeyError):
            pass

    def close(self):
        self.stop.set()
        self.thread.join()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default=config.DB_PATH)
    parser.add_argument('--mcp', nargs=3)
    parser.add_argument('--config', default=config.CONFIG_PATH)
    args = parser.parse_args()
    if args.mcp:
        from .mcp import serve_discussion
        with closing(Ledger(args.db)) as led:
            serve_discussion(config.load(args.config), led, args.mcp[0], int(args.mcp[1]), args.mcp[2])
        return
    # Raising through run_engine's finally reaps the engine/tool group on shutdown.
    def shutdown(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    raise SystemExit(worker(args.db, lambda: config.load(args.config), config_path=args.config))


if __name__ == '__main__':
    main()
