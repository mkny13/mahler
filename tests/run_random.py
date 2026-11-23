#!/usr/bin/env python3
"""Run the whole test suite in a random order (mahler#93).

Usage:
    python3 tests/run_random.py            # picks a random seed, prints it
    python3 tests/run_random.py <seed>     # reproduce a failure
    python3 tests/run_random.py <seed> --start-directory DIR

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
        "seed",
        type=int,
        nargs="?",
        default=None,
        help="seed to reproduce a prior order",
    )
    parser.add_argument(
        "--start-directory",
        "-s",
        default="tests",
        help="directory from which to discover tests (default: tests)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    seed = args.seed if args.seed is not None else random.randrange(2**31)
    print(f"random order seed: {seed}")
    rng = random.Random(seed)
    loader = unittest.TestLoader()
    suite = loader.discover(args.start_directory)
    cases = list(all_cases(suite))
    rng.shuffle(cases)
    result = unittest.TextTestRunner(verbosity=1).run(unittest.TestSuite(cases))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
