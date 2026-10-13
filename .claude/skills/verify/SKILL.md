---
name: verify
description: Run Mahler's four CLAUDE.md verify commands in order and report the first failure.
disable-model-invocation: true
---

Run these from the repo root, one at a time, in this order. Stop at the first
non-zero exit and report which command failed with its relevant output. Do not
run the later commands after a failure.

1. `python3 -m mahler.repo_guard`
2. `python3 -m unittest discover -s tests`
3. `python3 tests/run_random.py`
4. `python3 -X dev tests/run_strict.py`

If all four exit 0, say so in one line. If the randomized runner fails, include
the seed it printed (reproduce with `python3 tests/run_random.py <seed>`).
