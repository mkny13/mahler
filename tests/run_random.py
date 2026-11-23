#!/usr/bin/env python3
"""Run the whole test suite in a random order (mahler#93).

Usage:
    python3 tests/run_random.py            # picks a random seed, prints it
    python3 tests/run_random.py <seed>     # reproduce a failure

Every test must pass in any order: no test may leak state (env vars, module
globals, the filesystem, SQLite) that another test depends on.
"""
import argparse
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def all_cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from all_cases(item)
        else:
            yield item


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "positional",
        nargs="*",
        help="Optional seed (integer) and/or discovery start directory",
    )
    parser.add_argument(
        "--start-directory",
        "-s",
        dest="start_dir_flag",
        default=None,
        help="Directory to start test discovery (default: tests)",
    )
    args = parser.parse_args(argv)

    seed = None
    start_dir = args.start_dir_flag

    for arg in args.positional:
        try:
            val = int(arg)
            if seed is None:
                seed = val
            elif start_dir is None:
                start_dir = arg
        except ValueError:
            if start_dir is None:
                start_dir = arg

    if seed is None:
        seed = random.randrange(2**31)
    if start_dir is None:
        start_dir = "tests"

    return seed, start_dir


def main(argv=None):
    seed, start_dir = parse_args(argv)
    print(f"random order seed: {seed}")
    rng = random.Random(seed)
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir)
    cases = list(all_cases(suite))
    rng.shuffle(cases)
    result = unittest.TextTestRunner(verbosity=1).run(unittest.TestSuite(cases))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
