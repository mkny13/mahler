"""Check #922's design artifacts; runtime enforcement belongs to #923–#926."""
import json
from pathlib import Path
import re
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


class DiscussContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = (ROOT / 'docs/console/discuss.md').read_text()
        cls.recipe = (ROOT / 'recipes/discuss.md').read_text()

    def example(self, name):
        match = re.search(r'```json ' + name + r'\n(.*?)\n```', self.spec, re.S)
        self.assertIsNotNone(match, name)
        return json.loads(match[1])

    def test_versioned_terminal_examples(self):
        for outcome in ('success', 'error'):
            with self.subTest(outcome=outcome):
                value = self.example('result-' + outcome + '-v1')
                fields = {'version', 'assistant_text', 'engine_session_id',
                          'confirmed_issue_urls', 'status', 'error'}
                self.assertEqual(set(value), fields)
                self.assertEqual(value['version'], 1)
                self.assertIs(type(value['version']), int)
                self.assertEqual(value['status'], outcome)
                self.assertIsInstance(value['assistant_text'], str)
                session = value['engine_session_id']
                self.assertTrue(session is None or isinstance(session, str) and session)
                self.assertIsInstance(value['confirmed_issue_urls'], list)
                for url in value['confirmed_issue_urls']:
                    self.assertRegex(url, r'^https://github.com/example/project/issues/[1-9][0-9]*$')
                if outcome == 'success':
                    self.assertIsNone(value['error'])
                else:
                    self.assertEqual(set(value['error']), {'code', 'message', 'retryable'})
                    self.assertEqual(value['error']['code'], 'timeout')
                    self.assertIsInstance(value['error']['message'], str)
                    self.assertIs(value['error']['retryable'], True)
                for field in fields:
                    self.assertIn(field, self.recipe)

    def test_limits_and_recipe_boundary(self):
        self.assertEqual(self.example('limits-v1'), {
            'turn_timeout_seconds': 120, 'max_user_characters': 8000,
            'max_session_turns': 100, 'max_active_turns_per_session': 1})
        compact = ' '.join(self.recipe.split())
        for phrase in ('120-second', '8,000-character', '100-turn',
                       'one active turn per session', 'immutable on resume',
                       'Retries are explicit', 'Never code or merge',
                       'restricted tool server, not the prompt alone',
                       'Preserve owner answers', 'No paid fallback',
                       'confirmed partial writes', 'project\'s GitHub identity',
                       '<!-- mahler:agent -->'):
            self.assertIn(phrase, compact)
        for tool in ('read_repo_file', 'list_repo_files', 'list_items', 'get_item',
                     'add_item', 'edit_item', 'answer_item'):
            self.assertIn(tool, self.recipe)
            self.assertIn('`' + tool + '`', self.spec)

    def test_mockups_are_static_and_cover_states_at_both_widths(self):
        ns = '{http://www.w3.org/2000/svg}'
        for device, width in (('phone', 390), ('desktop', 1440)):
            path = ROOT / f'docs/console/screens/discuss-{device}.svg'
            svg = ET.parse(path).getroot()
            self.assertEqual(svg.tag, ns + 'svg')
            self.assertEqual(int(svg.attrib['width']), width)
            self.assertEqual(int(svg.attrib['viewBox'].split()[2]), width)
            ids = {node.get('id') for node in svg.iter()}
            self.assertTrue({'triage', 'capture', 'waiting', 'reply', 'quota', 'failure'} <= ids)
            self.assertIsNotNone(svg.find(ns + 'title'))
            self.assertIsNotNone(svg.find(ns + 'desc'))
            for node in svg.iter():
                self.assertNotIn(node.tag, {ns + 'script', ns + 'foreignObject', ns + 'image'})
                self.assertFalse(any(key.startswith('on') for key in node.attrib))
            self.assertIn(f'screens/discuss-{device}.svg', self.spec)


if __name__ == '__main__':
    unittest.main()
