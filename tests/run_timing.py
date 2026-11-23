#!/usr/bin/env python3
"""Advisory unittest timing; no elapsed-time merge gate.

python3 tests/run_timing.py --json baseline.json
python3 tests/run_timing.py --compare baseline.json --json current.json

Suite time includes discovery and class/module fixtures. Individual times include
setUp, the method, tearDown and cleanups, but exclude class/module fixtures.
Subtests belong to their parent; repeated IDs are summed. JSON is opt-in.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def aggregate(samples):
    durations = {}
    for test_id, seconds in samples:
        durations[test_id] = durations.get(test_id, 0.0) + seconds
    return dict(sorted(durations.items()))


def slowest(durations):
    return sorted(durations.items(), key=lambda item: (-item[1], item[0]))[:20]


def compare(previous, current):
    common = []
    for test_id in sorted(previous.keys() & current.keys()):
        before, after = previous[test_id], current[test_id]
        delta = after - before
        common.append({'id': test_id, 'previous_seconds': before,
                       'current_seconds': after, 'delta_seconds': delta,
                       'percent_change': 100 * delta / before if before else None,
                       'advisory': delta > 0.1 and after > before * 1.5})
    return {'added': sorted(current.keys() - previous.keys()),
            'removed': sorted(previous.keys() - current.keys()), 'common': common}


class TimingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.samples = []

    def startTest(self, test):
        super().startTest(test)
        self._started = time.perf_counter()

    def stopTest(self, test):
        self.samples.append((test.id(), time.perf_counter() - self._started))
        super().stopTest(test)


def runtime_metadata():
    def git(*args):
        try:
            return subprocess.check_output(['git', *args], cwd=ROOT, text=True,
                                           stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError):
            return None
    status = git('status', '--porcelain', '--untracked-files=no')
    return {'revision': git('rev-parse', 'HEAD'),
            'tracked_changes': bool(status) if status is not None else None,
            'python': sys.version, 'implementation': platform.python_implementation(),
            'os': platform.platform(), 'machine': platform.machine()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start-directory', default=str(ROOT / 'tests'))
    parser.add_argument('--pattern', default='test*.py')
    parser.add_argument('--json', type=Path, help='explicit output artifact path')
    parser.add_argument('--compare', type=Path, help='previous timing JSON artifact')
    args = parser.parse_args(argv)
    previous = None
    if args.compare:
        previous = json.loads(args.compare.read_text())['durations']
    runtime = runtime_metadata()
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    suite = unittest.TestLoader().discover(args.start_directory, pattern=args.pattern)
    result = unittest.TextTestRunner(resultclass=TimingResult, verbosity=1).run(suite)
    elapsed = time.perf_counter() - started
    durations = aggregate(result.samples)
    report = {'schema_version': 1, 'runtime': runtime, 'started_at': started_at,
              'discovery': {'directory': args.start_directory, 'pattern': args.pattern},
              'suite_seconds': elapsed, 'test_count': result.testsRun,
              'successful': result.wasSuccessful(), 'failures': len(result.failures),
              'errors': len(result.errors), 'skipped': len(result.skipped),
              'durations': durations}
    print(f'Suite: {result.testsRun} tests in {elapsed:.6f}s (including discovery)')
    print('Slowest tests (up to 20):')
    for test_id, seconds in slowest(durations):
        print(f'  {seconds:.6f}s {test_id}')
    if previous is not None:
        comparison = report['comparison'] = compare(previous, durations)
        print('Advisory comparison (elapsed time never determines exit status):')
        for kind in ('added', 'removed'):
            print(f'  {kind}: ' + (', '.join(comparison[kind]) or '(none)'))
        for row in comparison['common']:
            percent = row['percent_change']
            relative = f'{percent:+.1f}%' if percent is not None else 'n/a (zero baseline)'
            flag = ' ADVISORY' if row['advisory'] else ''
            print(f"  {row['delta_seconds']:+.6f}s {relative} {row['id']}{flag}")
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
