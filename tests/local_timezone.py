"""Scoped system timezone for display tests; restore libc and the environment."""
import os
import time
from contextlib import contextmanager
from unittest.mock import patch


@contextmanager
def local_timezone(name):
    try:
        with patch.dict(os.environ, {"TZ": name}):
            time.tzset()
            yield
    finally:
        time.tzset()
