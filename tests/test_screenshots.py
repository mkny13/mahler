"""Untrusted screenshot contract and private persistence, entirely temporary."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from mahler import screenshots as ss


class ScreenshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.source = self.base / 'source'
        self.source.mkdir()
        self.root = self.base / 'screenshots'
        self.sha = 'a' * 40
        self.png = ss.PNG_SIGNATURE + b'fixture'
        (self.source / 'sign-in.png').write_bytes(self.png)
        self.manifest = {'version': 1, 'sha': self.sha,
                         'screenshots': [{'route': '/sign-in', 'file': 'sign-in.png'}]}
        self.write_manifest()

    def write_manifest(self):
        (self.source / 'manifest.json').write_text(json.dumps(self.manifest))

    def reject(self):
        with self.assertRaises((ss.InvalidScreenshot, OSError)):
            ss.store(self.source, 'app', 12, self.sha, root=self.root)
        self.assertFalse(self.root.exists())

    def test_round_trip_survives_source_removal_and_is_private(self):
        self.manifest['ignored'] = 'never copied'
        self.write_manifest()
        (self.source / 'unlisted.png').write_bytes(self.png)
        with patch('mahler.config.STATE', str(self.base)):
            target = ss.store(self.source, 'app', 12, self.sha)
            shutil.rmtree(self.source)
            directory, manifest = ss.lookup('app', 12, self.sha)
        self.assertEqual(directory, target)
        self.assertEqual(manifest['sha'], self.sha)
        self.assertNotIn('ignored', manifest)
        self.assertEqual((target / 'sign-in.png').read_bytes(), self.png)
        self.assertFalse((target / 'unlisted.png').exists())
        for path in [self.root, *self.root.rglob('*')]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)
        self.assertIsNone(ss.lookup('app', 12, 'b' * 40, root=self.root))

    def test_nested_files_and_existing_head(self):
        (self.source / 'nested').mkdir()
        (self.source / 'sign-in.png').rename(self.source / 'nested/sign-in.png')
        self.manifest['screenshots'][0]['file'] = 'nested/sign-in.png'
        self.write_manifest()
        ss.store(self.source, 'app', 12, self.sha, root=self.root)
        with self.assertRaises(FileExistsError):
            ss.store(self.source, 'app', 12, self.sha, root=self.root)
        self.assertEqual(ss.lookup('app', 12, self.sha, root=self.root)[1], self.manifest)

    def test_invalid_manifest_values(self):
        original = json.dumps(self.manifest)
        cases = [('version', v) for v in (True, 0, 2, '1', 1.0, None)]
        cases += [('sha', 'b' * 40), ('screenshots', []), ('screenshots', {}),
                  ('screenshots', [None]), ('screenshots', self.manifest['screenshots'] * 11),
                  ('screenshots', self.manifest['screenshots'] * 2)]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                self.manifest = json.loads(original)
                self.manifest[key] = value
                self.write_manifest()
                self.reject()

    def test_invalid_paths_and_routes(self):
        for name in ('/tmp/a.png', '../a.png', 'x/../a.png', './a.png', 'x//a.png',
                     'x\\a.png', 'C:a.png', 'a.jpg', '', None, 'a\x00.png'):
            with self.subTest(name=name):
                self.manifest['screenshots'][0]['file'] = name
                self.write_manifest()
                self.reject()
        self.manifest['screenshots'][0]['file'] = 'sign-in.png'
        for route in ('/a?q=secret', '/a#secret', 'https://example.com/a', '//host/a',
                      '', None, '/a\n', '/a\\b'):
            with self.subTest(route=route):
                self.manifest['screenshots'][0]['route'] = route
                self.write_manifest()
                self.reject()

    def test_malformed_and_oversized_manifest(self):
        for data in (b'{', b'[]', b'\xff', b' ' * (ss.MAX_MANIFEST_BYTES + 1),
                     b'{"version":1,"version":1}', b'[' * 2000):
            with self.subTest(data=data[:30]):
                (self.source / 'manifest.json').write_bytes(data)
                self.reject()

    def test_missing_bad_signature_and_oversized_png(self):
        path = self.source / 'sign-in.png'
        path.unlink()
        self.reject()
        for data in (b'not PNG', ss.PNG_SIGNATURE + b'x' * ss.MAX_FILE_BYTES):
            path.write_bytes(data)
            self.reject()

    def test_symlinks_and_non_regular_files(self):
        path = self.source / 'sign-in.png'
        outside = self.base / 'outside.png'
        outside.write_bytes(self.png)
        path.unlink()
        path.symlink_to(outside)
        self.reject()
        path.unlink()
        os.mkfifo(path)
        self.reject()
        path.unlink()
        path.mkdir()
        self.reject()
        (self.source / 'nested').symlink_to(self.base, target_is_directory=True)
        self.manifest['screenshots'][0]['file'] = 'nested/outside.png'
        self.write_manifest()
        self.reject()
        manifest = self.source / 'manifest.json'
        manifest.rename(self.base / 'manifest.json')
        manifest.symlink_to(self.base / 'manifest.json')
        self.reject()

    def test_total_bound_and_exact_limits(self):
        self.manifest['screenshots'] = []
        for i in range(10):
            name = f'{i}.png'
            (self.source / name).write_bytes(ss.PNG_SIGNATURE + b'x' * (ss.MAX_FILE_BYTES - 8))
            self.manifest['screenshots'].append({'route': f'/{i}', 'file': name})
        self.write_manifest()
        # At 10 x 2 MiB both file and aggregate maxima are inclusive.
        self.assertEqual(ss.read_manifest(self.source, self.sha), self.manifest)
        with patch.object(ss, 'MAX_TOTAL_BYTES', ss.MAX_TOTAL_BYTES - 1):
            self.reject()
        raw = (self.source / 'manifest.json').read_bytes()
        (self.source / 'manifest.json').write_bytes(raw + b' ' * (ss.MAX_MANIFEST_BYTES - len(raw)))
        self.assertEqual(ss.read_manifest(self.source, self.sha), self.manifest)

    def test_storage_keys_and_symlinks_cannot_escape(self):
        for project, pr, sha in (('../escape', 12, self.sha), ('app', True, self.sha),
                                 ('app', 0, self.sha), ('app', 12, '../escape')):
            with self.assertRaises(ss.InvalidScreenshot):
                ss.store(self.source, project, pr, sha, root=self.root)
        outside = self.base / 'outside'
        outside.mkdir()
        self.root.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            ss.store(self.source, 'app', 12, self.sha, root=self.root)
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
