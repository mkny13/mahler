"""Redact credential-shaped strings before they leave the machine (issue #76).

Mahler publishes machine-local text in three places: GitHub comments (setup.log
tails, agent handoffs), ntfy pings, and its own CLI output. Any of that text
can pick up a secret that leaked into it earlier — a setup step that echoes its
environment (`set -x`), an agent that debugs with `echo $GH_TOKEN`, an error
message that embedded a connection URL. This module is the last filter before
such text is posted.

Only high-confidence shapes are masked — URLs with embedded credentials,
assignments to credential-named variables, and recognisable token formats — so
that debugging information survives. Everything else passes through untouched.
This is not a fence against arbitrary secrets; it is a backstop for the audit
in issue #76, which confirmed that Mahler itself never puts a secret in a log,
argv, exception, or notification in the first place.
"""

import re

MARK = "<redacted>"

# URLs with embedded credentials: scheme://user:password@host — the password
# (and only it) is masked, so the address stays debuggable.
_URL_CREDS = re.compile(
    r"([a-z][a-z0-9+.-]*://[^\s:/@]+):([^\s/@]+)@", re.IGNORECASE)

# Assignments to variables whose names say credential — the ones config.py
# strips from run environments (GH_TOKEN and friends), plus anything whose name
# ends in TOKEN / SECRET / PASSWORD / API_KEY. The suffix rule matters because
# accounts may declare arbitrary env (a project's own password variable, for
# instance), so a fixed name list can never be exhaustive.
_ENV_ASSIGN = re.compile(
    r"\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY|APIKEY))\s*=\s*\S+",
    re.IGNORECASE)

# Bare token shapes, for when a value is echoed without its variable name.
_TOKEN_SHAPES = (
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bsk-(?:proj|svcacct)-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bpypi-[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)


def redact(text):
    """Mask credential-shaped substrings of `text` -> str (None passes through)."""
    if not text:
        return text
    text = _URL_CREDS.sub(r"\1:" + MARK + "@", text)
    text = _ENV_ASSIGN.sub(r"\1=" + MARK, text)
    for shape in _TOKEN_SHAPES:
        text = shape.sub(MARK, text)
    return text
