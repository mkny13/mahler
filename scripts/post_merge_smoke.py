#!/usr/bin/env python3
"""Fetch an HTTPS artifact and emit only the #617 smoke report."""
import os
import sys
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener


def validate_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or '@' in parsed.netloc or '\\' in url
            or any(ord(c) <= 32 or ord(c) == 127 for c in url)):
        raise ValueError('invalid artifact URL')


class HTTPSRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def main():
    try:
        tag = os.environ['MAHLER_TAG']
        url = os.environ['MAHLER_ARTIFACT_REF']
        if not tag.strip() or any(ord(c) < 32 or ord(c) == 127 for c in tag):
            raise ValueError('invalid tag')
        validate_url(url)
        with build_opener(HTTPSRedirectHandler()).open(url, timeout=10) as response:
            if not 200 <= response.status < 300:
                raise ValueError('HTTP failure')
    except Exception:
        print('Smoke: FAIL')
        return 1
    print(f'Tag: {tag}')
    print('Smoke: PASS')
    return 0


if __name__ == '__main__':
    sys.exit(main())
