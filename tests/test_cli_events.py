"""Isolated CLI event history, including literal SQL filters and hostile detail."""
import io
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from mahler import cli
from mahler.ledger import Ledger, iso


class EventsTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
        self.led = Ledger(':memory:', clock=lambda: self.now)
        self.addCleanup(self.led.close)

    def event(self, kind, minutes=0, project='p', detail='detail'):
        self.led.con.execute('INSERT INTO events(at, project, number, kind, detail) VALUES(?,?,?,?,?)',
                             (iso(self.now - timedelta(minutes=minutes)), project, 12, kind, detail))

    def run_events(self, **kwargs):
        args = dict(project=None, kind=None, since=None, limit=50)
        args.update(kwargs)
        out = io.StringIO()
        with patch('sys.stdout', out):
            code = cli.cmd_events(SimpleNamespace(**args), {}, self.led)
        self.assertEqual(code, 0)
        return out.getvalue().splitlines()

    def test_composed_filters_and_newest_first(self):
        self.event('console_old', 61)
        self.event('console_other', project='other')
        self.event('consoleXwrong')
        self.event('console_new', 1)
        self.event('console_boundary', 60)
        self.event('console_newest')
        lines = self.run_events(project='p', kind=['console_'], since='1h', limit=2)
        self.assertEqual(len(lines), 3)
        self.assertIn('console_newest', lines[1])
        self.assertIn('console_new', lines[2])
        self.assertIn('p#12', lines[1])
        self.assertIn('console_boundary', '\n'.join(self.run_events(kind=['console_'], since='1h')))

    def test_exact_repeated_and_literal_filters(self):
        for kind in ('console_a', 'console_ab', 'other', "x%'_kind"):
            self.event(kind)
        lines = self.run_events(kind=['console_a', 'other'])
        self.assertEqual(len(lines), 3)
        self.assertNotIn('console_ab', '\n'.join(lines))
        self.assertIn("x%'_kind", '\n'.join(self.run_events(kind=["x%'_"])))
        self.assertEqual(self.run_events(project="' OR 1=1 --"), ['No matching events.'])

    def test_empty_and_limit_bounds(self):
        self.assertEqual(self.run_events(), ['No matching events.'])
        for _ in range(1002):
            self.event('test')
        self.assertEqual(len(self.run_events()), 51)
        self.assertEqual(len(self.run_events(limit=-10)), 2)
        self.assertEqual(len(self.run_events(limit=100000)), 1001)

    def test_bounded_single_line_and_null_detail(self):
        self.event('test', detail='hello\n\tworld\r\x1b[2J' + 'x' * 300)
        self.event('test', detail=None)
        lines = self.run_events()
        self.assertEqual(len(lines), 3)
        self.assertIn('hello world', lines[2])
        self.assertNotIn('\x1b', lines[2])
        self.assertTrue(lines[2].endswith('…'))
        self.assertLessEqual(len(lines[2]), 251)

    def test_invalid_duration(self):
        with patch('sys.stderr', io.StringIO()) as err:
            result = cli.cmd_events(SimpleNamespace(project=None, kind=None, since='yesterday', limit=50), {}, self.led)
        self.assertEqual(result, 2)
        self.assertIn('bad duration', err.getvalue())

    def test_parser_wiring(self):
        with patch.object(cli.config, 'load', return_value={}), patch.object(cli, 'Ledger', return_value=self.led), patch.object(cli, 'RoutedLedger', side_effect=lambda led, cfg: led), patch('sys.stdout', io.StringIO()) as out:
            self.assertEqual(cli.main(['events', '--since', '1h30m', '--kind', 'console_', '--kind', 'test', '--project', 'p', '-n', '5']), 0)
        self.assertIn('No matching events.', out.getvalue())
