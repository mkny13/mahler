# TASKS

## Status

2026-09-16 chat session: diagnosed and fixed `sit-stand-walk#2`'s `launch_failed`
("Repository not found" / "Authentication failed" against
`github.com/makastel_ncstate/sit-stand-walk.git`). Root cause: `runner.py`'s raw
`git fetch`/`push` calls (`prepare()`, `catch_up()`, `snapshot()`) and `janitor.py`'s
branch sweep never threaded the project's account-scoped env the way `gh.py`'s `GH`
client already does (D25/D26), so a work-account project's private repo got
fetched/pushed under this machine's default personal git credential helper. Fixed,
tested (857 tests + random-order check green), committed, pushed, and opened as
[mkny13/mahler#306](https://github.com/mkny13/mahler/pull/306). CI auto-fix monitor is
on for this session. Nothing mid-flight.

## Next steps

1. Let #306 merge once CI is green (auto-fix monitor will wake this session if it
   fails; no manual polling needed).
2. Once merged, `sit-stand-walk#2` should be able to launch again on its next tick —
   worth a quick check that it actually does (Mahler self-heals via the daemon's
   own update path, no manual redeploy needed since the daemon runs from
   `~/.mahler/app` pinned to a known-good commit and self-updates on green CI).
3. Not investigated: whether other raw-git call sites exist beyond
   `runner.py`/`janitor.py` (e.g. any future module that shells to `git` directly)
   — worth a grep for `subprocess.*git` without an `env=` the next time this pattern
   comes up.

## Context

- The bug was specific to **work-account projects** (D25: separate GitHub identity,
  `~/.config/gh-work` etc.) — personal-account projects were unaffected because
  `config.run_env()` returns `None` (inherit as-is) for the default account, which is
  what the old code effectively always did.
- The fix follows an existing, documented invariant: `gh.py`'s `_git()` docstring says
  it "goes through the same credential setup as the run's own pushes" — that
  invariant was true for `gh.py` but not for `runner.py`/`janitor.py` until this fix.
- User's own comment mid-diagnosis: "I can't make any work repos public" — correctly
  pushing back on the wrong fix; the actual fix has nothing to do with repo visibility.
