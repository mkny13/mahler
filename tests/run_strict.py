#!/usr/bin/env python3
"""Run tests with resource leaks and unraisable exceptions as failures.

Usage: python3 -X dev tests/run_strict.py [--start-directory DIR]
"""
import argparse
import gc
import os
import sys
import traceback
import unittest
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "start_directory",
        nargs="?",
        default=None,
        help="Directory to start test discovery (default: tests)",
    )
    parser.add_argument(
        "--start-directory",
        "-s",
        dest="start_dir_flag",
        default=None,
        help="Directory to start test discovery",
    )
    args = parser.parse_args(argv)
    return args.start_dir_flag or args.start_directory or "tests"


def main(argv=None):
    start_dir = parse_args(argv)
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
            suite = unittest.TestLoader().discover(start_dir)
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
