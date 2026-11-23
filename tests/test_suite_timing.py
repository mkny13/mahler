"""Deterministic timing math and isolated runner integration checks."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import run_timing


class TimingTests(unittest.TestCase):
    def test_aggregation_and_stable_top_twenty(self):
        samples = [(f'case{i:02}', float(i)) for i in range(25)] + [('case24', 1.0)]
        durations = run_timing.aggregate(samples)
        self.assertEqual(durations['case24'], 25.0)
        self.assertEqual(len(durations), 25)
        self.assertEqual(run_timing.slowest(durations)[0], ('case24', 25.0))
        self.assertEqual(len(run_timing.slowest(durations)), 20)
        self.assertEqual(run_timing.slowest({'z': 1, 'a': 1}), [('a', 1), ('z', 1)])

    def test_comparison_thresholds_and_membership(self):
        old = {'removed': 1, 'relative_only': .01, 'absolute_only': 2,
               'both': 1, 'exact_relative': 2, 'exact_absolute': 0, 'zero': 0}
        new = {'added': 2, 'relative_only': .02, 'absolute_only': 2.2,
               'both': 1.6, 'exact_relative': 3, 'exact_absolute': .1, 'zero': .2}
        report = run_timing.compare(old, new)
        self.assertEqual(report['added'], ['added'])
        self.assertEqual(report['removed'], ['removed'])
        self.assertEqual([row['id'] for row in report['common'] if row['advisory']],
                         ['both', 'zero'])
        both = next(row for row in report['common'] if row['id'] == 'both')
        self.assertAlmostEqual(both['delta_seconds'], .6)
        self.assertAlmostEqual(both['percent_change'], 60)
        self.assertIsNone(next(row for row in report['common'] if row['id'] == 'zero')['percent_change'])

    def test_result_measures_test_lifecycle_with_synthetic_clock(self):
        case = unittest.FunctionTestCase(lambda: None)
        result = run_timing.TimingResult(io.StringIO(), True, 0)
        with patch.object(run_timing.time, 'perf_counter', side_effect=[10, 12.5]):
            case.run(result)
        self.assertEqual(result.samples, [(case.id(), 2.5)])
        self.assertTrue(result.wasSuccessful())

    def test_cli_json_discovery_once_and_failure_propagation(self):
        script = Path(run_timing.__file__).resolve()
        for body, code in [('self.assertTrue(True)', 0), ('self.fail("fixture")', 1),
                           ('raise RuntimeError("fixture")', 1)]:
            with self.subTest(body=body), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'test_fixture.py').write_text(
                    'import unittest\nfrom pathlib import Path\n'
                    'class Fixture(unittest.TestCase):\n'
                    '    def test_case(self):\n        ' + body + '\n'
                    'def load_tests(loader, tests, pattern):\n'
                    '    p = Path(__file__).with_suffix(".count")\n'
                    '    p.write_text(p.read_text() + "x" if p.exists() else "x")\n'
                    '    return tests\n')
                artifact = root / 'report.json'
                previous = root / 'previous.json'
                previous.write_text(json.dumps({'durations': {'test_fixture.Fixture.test_case': 0}}))
                command = [sys.executable, str(script), '--start-directory', directory,
                           '--json', str(artifact), '--compare', str(previous)]
                completed = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(completed.returncode, code, completed.stderr)
                report = json.loads(artifact.read_text())
                self.assertEqual(report['test_count'], 1)
                self.assertEqual(report['successful'], code == 0)
                self.assertEqual(list(report['durations']), ['test_fixture.Fixture.test_case'])
                self.assertEqual((root / 'test_fixture.count').read_text(), 'x')
                self.assertIn('revision', report['runtime'])
                self.assertIn('python', report['runtime'])
                self.assertIn('os', report['runtime'])
                self.assertIn('suite_seconds', report)
                self.assertIn('test_fixture.Fixture.test_case', completed.stdout)
                self.assertIn('Advisory', completed.stdout)
