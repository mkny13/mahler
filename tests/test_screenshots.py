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

    def assertRejected(self):
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
                self.assertRejected()

    def test_invalid_paths_and_routes(self):
        for name in ('/tmp/a.png', '../a.png', 'x/../a.png', './a.png', 'x//a.png',
                     'x\\a.png', 'C:a.png', 'a.jpg', '', None, 'a\x00.png'):
            with self.subTest(name=name):
                self.manifest['screenshots'][0]['file'] = name
                self.write_manifest()
                self.assertRejected()
        self.manifest['screenshots'][0]['file'] = 'sign-in.png'
        for route in ('/a?q=secret', '/a#secret', 'https://example.com/a', '//host/a',
                      '', None, '/a\n', '/a\\b'):
            with self.subTest(route=route):
                self.manifest['screenshots'][0]['route'] = route
                self.write_manifest()
                self.assertRejected()

    def test_malformed_and_oversized_manifest(self):
        for data in (b'{', b'[]', b'\xff', b' ' * (ss.MAX_MANIFEST_BYTES + 1),
                     b'{"version":1,"version":1}', b'[' * 2000):
            with self.subTest(data=data[:30]):
                (self.source / 'manifest.json').write_bytes(data)
                self.assertRejected()

    def test_missing_bad_signature_and_oversized_png(self):
        path = self.source / 'sign-in.png'
        path.unlink()
        self.assertRejected()
        for data in (b'not PNG', ss.PNG_SIGNATURE + b'x' * ss.MAX_FILE_BYTES):
            path.write_bytes(data)
            self.assertRejected()

    def test_symlinks_and_non_regular_files(self):
        path = self.source / 'sign-in.png'
        outside = self.base / 'outside.png'
        outside.write_bytes(self.png)
        path.unlink()
        path.symlink_to(outside)
        self.assertRejected()
        path.unlink()
        os.mkfifo(path)
        self.assertRejected()
        path.unlink()
        path.mkdir()
        self.assertRejected()
        (self.source / 'nested').symlink_to(self.base, target_is_directory=True)
        self.manifest['screenshots'][0]['file'] = 'nested/outside.png'
        self.write_manifest()
        self.assertRejected()
        manifest = self.source / 'manifest.json'
        manifest.rename(self.base / 'manifest.json')
        manifest.symlink_to(self.base / 'manifest.json')
        self.assertRejected()

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
            self.assertRejected()
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


class CaptureTests(unittest.TestCase):
    real_worktree = staticmethod(ss._worktree)
    def setUp(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from mahler.ledger import Ledger
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.led = Ledger(':memory:')
        self.addCleanup(self.led.close)
        self.sha = 'a' * 40
        self.policy = {'screenshot': 'capture', 'screenshot_environment': 'Preview'}
        self.gh = Mock()
        self.gh.screenshot_preview.return_value = 'https://preview.example/?signed=secret'
        self.gh.pr_view.return_value = {'headRefOid': self.sha}
        self.ctx = SimpleNamespace(led=self.led, policy=lambda _: self.policy,
                                   gh=lambda _: self.gh, dry_run=False, say=Mock())
        self.paths = []
        for target, value in [('mahler.config.STATE', self.tmp.name),
                              ('mahler.screenshots._worktree', self.tmp.name)]:
            patcher = patch(target, return_value=value) if target.endswith('_worktree') else patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def command(self, command, cwd, env, timeout):
        output = Path(env['MAHLER_SCREENSHOT_DIR'])
        self.paths.extend([output, Path(env['MAHLER_SCREENSHOT_PROFILE_DIR'])])
        self.assertNotEqual(*self.paths[-2:])
        (output / 'a.png').write_bytes(ss.PNG_SIGNATURE)
        (output / 'manifest.json').write_text(json.dumps({
            'version': 1, 'sha': env['MAHLER_SCREENSHOT_SHA'],
            'screenshots': [{'route': '/private-route', 'file': 'a.png'}]}))
        return 0

    def capture(self):
        ss.capture(self.ctx, 'app', {'number': 1}, 12, {'headRefOid': self.sha})
        raw = self.led.get_kv(f'screenshot:app:12:{self.sha}')
        for secret in ('signed', 'secret', 'private-route', 'https:'):
            self.assertNotIn(secret, raw or '')
        self.assertTrue(all(not p.exists() for p in self.paths))
        return json.loads(raw) if raw else None

    def test_success_once_and_new_head(self):
        with patch.object(ss, '_command', side_effect=self.command) as command:
            self.assertEqual(self.capture()['state'], 'success')
            self.capture()
            self.assertEqual(command.call_count, 1)
            self.sha = 'b' * 40
            self.gh.pr_view.return_value = {'headRefOid': self.sha}
            self.assertEqual(self.capture()['state'], 'success')
            self.assertEqual(command.call_count, 2)
        self.assertIsNotNone(ss.lookup('app', 12, self.sha, root=Path(self.tmp.name) / 'screenshots'))

    def test_disabled(self):
        self.policy['screenshot'] = ''
        with patch.object(ss, '_command') as command:
            self.assertIsNone(self.capture())
            command.assert_not_called()
            self.gh.screenshot_preview.assert_not_called()

    def test_unavailable_and_failures_are_terminal(self):
        import subprocess
        cases = ['preview', 'checkout', 'nonzero', 'missing_command', 'invalid', 'timeout',
                 'drift', 'discovery', 'storage']
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                self.sha = f'{index:040x}'
                self.gh.pr_view.return_value = {'headRefOid': self.sha if case != 'drift' else 'f' * 40}
                self.gh.screenshot_preview.side_effect = RuntimeError('secret') if case == 'discovery' else None
                self.gh.screenshot_preview.return_value = None if case == 'preview' else 'https://preview.example'
                def run(*args):
                    self.command(*args)
                    if case == 'timeout':
                        raise subprocess.TimeoutExpired('secret', 1)
                    if case == 'invalid':
                        (self.paths[-2] / 'manifest.json').write_text('{}')
                    return 1 if case == 'nonzero' else 127 if case == 'missing_command' else 0
                with patch.object(ss, '_worktree', return_value=None if case == 'checkout' else self.tmp.name), \
                        patch.object(ss, '_command', side_effect=run) as command, \
                        patch.object(ss, 'store', side_effect=OSError('secret') if case == 'storage' else None):
                    result = self.capture()
                    self.assertNotEqual(result['state'], 'success')
                    calls = command.call_count
                    self.capture()
                    self.assertEqual(command.call_count, calls)

    def test_interrupted_and_persistence_failure(self):
        key = f'screenshot:app:12:{self.sha}'
        self.led.set_kv(key, json.dumps({'state': 'unavailable', 'reason': 'interrupted'}))
        with patch.object(ss, '_command') as command:
            self.capture()
            command.assert_not_called()
        with patch.object(self.led, 'get_kv', side_effect=RuntimeError('secret')):
            ss.capture(self.ctx, 'app', {'number': 1}, 12, {'headRefOid': self.sha})

    def test_timeout_kills_group_and_reaps(self):
        import signal
        import subprocess
        from unittest.mock import Mock
        proc = Mock(pid=123)
        proc.wait.side_effect = [subprocess.TimeoutExpired('capture', 1), 0]
        proc.__enter__ = Mock(return_value=proc)
        proc.__exit__ = Mock(return_value=False)
        with patch.object(ss.subprocess, 'Popen', return_value=proc) as popen, \
                patch.object(ss.os, 'killpg') as kill:
            with self.assertRaises(subprocess.TimeoutExpired):
                ss._command('capture', self.tmp.name, {}, 1)
        kill.assert_called_once_with(123, signal.SIGKILL)
        self.assertEqual(proc.wait.call_count, 2)
        self.assertTrue(popen.call_args.kwargs['start_new_session'])

    def test_worktree_requires_exact_head_and_build_role(self):
        self.led.create_run(project='app', number=1, role='build', platform='test', epoch=1, status='ended', worktree=self.tmp.name)
        with patch.object(ss.subprocess, 'run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = self.sha + '\n'
            self.assertEqual(self.real_worktree(self.led, 'app', 1, self.sha), self.tmp.name)
            run.return_value.stdout = 'wrong'
            self.assertIsNone(self.real_worktree(self.led, 'app', 1, self.sha))
