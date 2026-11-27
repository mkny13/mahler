"""Repository-only contract for the fresh-user onboarding documents."""

from pathlib import Path
import re
import tempfile
import tomllib
import unittest

from mahler import cli, config
from tests.test_architecture_docs import local_links

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = ('README.md', 'config.example.toml', 'docs/by-hand.md')
MARKDOWN = ('README.md', 'docs/by-hand.md')
OPERATOR_PATHS = ('/Users/mike', '/Volumes/ExtSSD160')


def documented_commands(markdown):
    """Top-level subcommands in fenced shell blocks and inline code spans."""
    commands = set()
    fenced = []
    for match in re.finditer(r'```([^\n]*)\n(.*?)```', markdown, re.DOTALL):
        if match.group(1).strip() in {'bash', 'sh'}:
            fenced.append(match.group(2))
    snippets = [line for block in fenced for line in block.splitlines()]
    unfenced = re.sub(r'```.*?```', '', markdown, flags=re.DOTALL)
    snippets += re.findall(r'`([^`\n]+)`', unfenced)
    for snippet in snippets:
        words = snippet.strip().split('#')[0].split()
        if len(words) > 1 and words[0] == 'mahler' and re.fullmatch(r'[a-z][a-z0-9-]*', words[1]):
            commands.add(words[1])
    return commands


def parser_commands():
    parser = cli.build_parser()
    for action in parser._actions:
        if getattr(action, 'choices', None) and hasattr(action, '_name_parser_map'):
            return set(action.choices)
    raise AssertionError('no subcommands found')


class OnboardingDocsTests(unittest.TestCase):
    def test_local_links_resolve(self):
        for name in MARKDOWN:
            base = (ROOT / name).parent
            for path in local_links((ROOT / name).read_text()):
                with self.subTest(document=name, path=path):
                    self.assertTrue((base / path).exists(), f'{name}: broken link {path}')

    def test_documented_commands_exist(self):
        known = parser_commands()
        for name in MARKDOWN:
            found = documented_commands((ROOT / name).read_text())
            self.assertGreater(len(found), 0, name)
            for command in sorted(found):
                with self.subTest(document=name, command=command):
                    self.assertIn(command, known)

    def test_command_extractor_flags_unknown_command(self):
        self.assertEqual(documented_commands('Run `mahler nonesuch --x` now'), {'nonesuch'})
        self.assertNotIn('nonesuch', parser_commands())

    def test_command_extractor_handles_shell_block_after_non_shell_block(self):
        markdown = """```toml
[project]
name = \"example\"
```
```bash
mahler labels
mahler tick
```"""
        self.assertEqual(documented_commands(markdown), {'labels', 'tick'})

    def test_no_operator_specific_paths(self):
        for name in DOCUMENTS:
            text = (ROOT / name).read_text()
            for bad in OPERATOR_PATHS:
                with self.subTest(document=name, path=bad):
                    self.assertNotIn(bad, text)

    def test_example_config_loads_with_disabled_example_project(self):
        cfg = config.load(str(ROOT / 'config.example.toml'))
        project = cfg['projects']['example']
        self.assertIs(project['enabled'], False)
        self.assertIn('repo', project)
        self.assertIn('path', project)

    def test_readme_project_snippet_loads(self):
        readme = (ROOT / 'README.md').read_text()
        section = readme.split('## Configure a project', 1)[1]
        snippet = re.search(r'```toml\n(.*?)```', section, re.DOTALL).group(1)
        example = (ROOT / 'config.example.toml').read_text()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.toml'
            path.write_text(snippet)
            cfg = config.load(str(path))
        self.assertIn('myapp', cfg['projects'])
        for key in tomllib.loads(snippet)['projects']['myapp']:
            pattern = re.compile(rf'^#?\s*{re.escape(key)}\s*=', re.MULTILINE)
            self.assertRegex(example, pattern, key)


if __name__ == '__main__':
    unittest.main()
