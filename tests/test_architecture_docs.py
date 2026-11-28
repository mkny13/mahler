"""Repository-only guards for the current architecture map and its references."""

import argparse
import json
from io import StringIO
from unittest.mock import patch
from pathlib import Path
import re
import unittest
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = ('ARCHITECTURE.md', 'ROADMAP.md', 'README.md', 'DESIGN.md')


def decision_claim_matches(architecture, design):
    numbers = re.findall(r'^### D(\d+)\b', design, re.MULTILINE)
    claim = re.search(r'decision record \(D1[–-]D(\d+)\)', architecture)
    return bool(numbers and claim and int(claim[1]) == max(map(int, numbers)))


def local_links(markdown):
    # Inline links/images and reference definitions; ignore fenced examples.
    markdown = re.sub(r'```.*?```', '', markdown, flags=re.DOTALL)
    targets = re.findall(r'\[[^\]\n]*\]\(\s*(<[^>]+>|[^\s)]+)', markdown)
    targets += re.findall(r'^\s*\[[^\]]+\]:\s*(<[^>]+>|\S+)', markdown, re.MULTILINE)
    for target in targets:
        url = urlsplit(target.strip('<>'))
        if not url.scheme and not url.netloc and url.path:
            yield unquote(url.path)


def component_paths(architecture):
    section = architecture.split('## Components\n', 1)[1].split('\n## ', 1)[0]
    for row in section.splitlines():
        if row.startswith('|'):
            cells = row.split('|')
            if len(cells) > 3:
                yield from re.findall(r'`([^`]+)`', cells[2])


class ArchitectureDocsTests(unittest.TestCase):
    def test_highest_decision_is_current(self):
        self.assertTrue(decision_claim_matches(
            (ROOT / 'ARCHITECTURE.md').read_text(), (ROOT / 'DESIGN.md').read_text()),
            'ARCHITECTURE decision record range must end at DESIGN’s highest decision')

    def test_repository_relative_links_resolve(self):
        for name in DOCUMENTS:
            paths = list(local_links((ROOT / name).read_text()))
            self.assertTrue(paths, f'{name}: expected repository-relative links')
            for path in paths:
                with self.subTest(document=name, path=path):
                    self.assertTrue((ROOT / path).exists(), f'{name}: broken link {path}')

    def test_shipped_component_paths_exist(self):
        paths = list(component_paths((ROOT / 'ARCHITECTURE.md').read_text()))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path=path):
                self.assertTrue((ROOT / path).exists(), f'Missing component: {path}')

    def test_stale_claim_is_rejected_even_with_later_decision_out_of_order(self):
        design = '### D52 — latest\n### D15 — historical\n'
        self.assertFalse(decision_claim_matches('decision record (D1–D36)', design))
        self.assertTrue(decision_claim_matches('decision record (D1–D52)', design))

    def test_link_guard_retains_broken_targets_and_ignores_external_links(self):
        links = list(local_links('[a](missing.md#anchor) [b](https://example.com/x) '
                                 '[c](#section)\n[d]: <docs/missing%20file.md>\n'))
        self.assertEqual(links, ['missing.md', 'docs/missing file.md'])
        self.assertTrue(all(not (ROOT / path).exists() for path in links))

    def test_component_guard_retains_missing_paths(self):
        paths = list(component_paths('## Components\n| Test | `mahler/missing.py` | role |\n'
                                     '## Next\n'))
        self.assertEqual(paths, ['mahler/missing.py'])
        self.assertFalse((ROOT / paths[0]).exists())

    def test_cli_inventory_matches_parser(self):
        from mahler.cli import build_parser
        parser = build_parser()
        commands = next(a.choices for a in parser._actions
                        if isinstance(a, argparse._SubParsersAction))
        architecture = (ROOT / 'ARCHITECTURE.md').read_text()
        section = architecture.split('### CLI command inventory\n', 1)[1].split('\n## ', 1)[0]
        documented = re.findall(r'`([a-z][a-z-]+)`', section)
        self.assertEqual(set(documented), set(commands))
        self.assertEqual(len(documented), len(set(documented)))

    def test_mcp_inventory_matches_protocol(self):
        from mahler.ledger import Ledger
        from mahler.mcp import serve
        request = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        output = StringIO()
        led = Ledger(':memory:')
        self.addCleanup(led.close)
        with patch('sys.stdin', StringIO(request + '\n')), patch('sys.stdout', output):
            serve({}, led)
        names = {tool['name'] for tool in json.loads(output.getvalue())['result']['tools']}
        architecture = (ROOT / 'ARCHITECTURE.md').read_text()
        row = next(line for line in architecture.splitlines() if line.startswith('| MCP server |'))
        documented = re.findall(r'`([^`]+)`', row.split('|')[3])
        self.assertEqual(set(documented), names)
        self.assertEqual(len(documented), len(names))

    def test_design_inventory_is_preserved(self):
        # Existing historical IDs, including allocation gaps, must survive reconciliation.
        numbers = list(map(int, re.findall(r'^### D(\d+)\b',
                                         (ROOT / 'DESIGN.md').read_text(), re.MULTILINE)))
        expected = set(range(1, 53)) - {32, 37, 38, 42, 48, 49}
        self.assertTrue(expected <= set(numbers))
        self.assertEqual(len(numbers), len(set(numbers)))
