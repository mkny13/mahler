"""The standalone HTTPS smoke command's #617 report contract."""
import contextlib
import io
import os
from pathlib import Path
import re
import runpy
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import URLError
from urllib.request import Request

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'post_merge_smoke.py'


class PostMergeSmokeTests(unittest.TestCase):
    def run_smoke(self, env=None, status=200, error=None):
        output = io.StringIO()
        response = MagicMock()
        response.__enter__.return_value.status = status
        opener = MagicMock()
        opener.open.return_value = response
        opener.open.side_effect = error
        with patch.dict(os.environ, env if env is not None else {
            'MAHLER_TAG': 'v1', 'MAHLER_ARTIFACT_REF': 'https://localhost/build'
        }, clear=True), patch('urllib.request.build_opener', return_value=opener), \
                contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as exit_result:
                runpy.run_path(str(SCRIPT), run_name='__main__')
        return exit_result.exception.code, output.getvalue(), opener

    def test_success_report(self):
        for status in (200, 204, 299):
            code, output, opener = self.run_smoke(status=status)
            self.assertEqual(code, 0)
            self.assertEqual(output, 'Tag: v1\nSmoke: PASS\n')
            self.assertEqual(re.findall(r'^Tag:[ \t]*(.*)$', output, re.M), ['v1'])
            self.assertEqual(re.findall(r'^Smoke:[ \t]*(.*)$', output, re.M), ['PASS'])
            self.assertLessEqual(opener.open.call_args.kwargs['timeout'], 10)

    def test_failures_never_emit_pass_or_sensitive_data(self):
        for kwargs in ({'status': 500}, {'status': 301}, {'error': URLError('secret')},
                       {'env': {}}, {'env': {'MAHLER_TAG': 'v1'}},
                       *({'env': {'MAHLER_TAG': 'v1', 'MAHLER_ARTIFACT_REF': url}}
                         for url in ('http://localhost', 'https://user:secret@localhost',
                                     'https://', 'https://localhost\nsecret')),
                       {'env': {'MAHLER_TAG': 'v1\nSmoke: PASS',
                                'MAHLER_ARTIFACT_REF': 'https://localhost'}}):
            with self.subTest(kwargs=kwargs):
                code, output, _ = self.run_smoke(**kwargs)
                self.assertNotEqual(code, 0)
                self.assertEqual(output, 'Smoke: FAIL\n')

    def test_redirects_require_https_without_credentials(self):
        namespace = runpy.run_path(str(SCRIPT))
        handler = namespace['HTTPSRedirectHandler']()
        request = Request('https://localhost/start')
        for url in ('http://localhost/end', 'https://user:secret@localhost/end'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                handler.redirect_request(request, None, 302, 'Found', {}, url)
        redirected = handler.redirect_request(request, None, 302, 'Found', {},
                                              'https://localhost/end')
        self.assertEqual(redirected.full_url, 'https://localhost/end')
