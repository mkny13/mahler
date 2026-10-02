#!/usr/bin/env python3
"""Run tests with resource leaks and unraisable exceptions as failures.

Usage: python3 -X dev tests/run_strict.py
"""
import gc
import os
import sys
import traceback
import unittest
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    unraisables = []

    def record_unraisable(args):
        # Store text, not objects/tracebacks that can keep leaking resources alive.
        unraisables.append(
            f"{args.err_msg or 'Unraisable exception'}: {args.object!r}\n"
            + "".join(traceback.format_exception(
                args.exc_type, args.exc_value, args.exc_traceback))
        )

    previous_hook = sys.unraisablehook
    try:
        sys.unraisablehook = record_unraisable
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            suite = unittest.TestLoader().discover("tests")
            result = unittest.TextTestRunner(verbosity=1).run(suite)
            # Finalize cycles before checking the hook's records and restoring it.
            del suite
            gc.collect()
    finally:
        sys.unraisablehook = previous_hook

    for report in unraisables:
        print(report, file=sys.stderr)
    if unraisables:
        print(f"FAILED: {len(unraisables)} unraisable exception(s)", file=sys.stderr)
    return 0 if result.wasSuccessful() and not unraisables else 1


if __name__ == "__main__":
    sys.exit(main())
