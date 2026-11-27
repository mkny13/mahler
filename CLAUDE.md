# Mahler

Mahler conducts coding agents (Claude Code, Antigravity's `agy`, Cline, Copilot, Codex,
Kilo) through each project's GitHub-issue backlog. It is quota-aware and hands work off
across platforms. **Read [DESIGN.md](DESIGN.md) before changing behaviour** — each
decision there has its reasoning; D18 — *agents build, the conductor ships* — decides how
you end a task. [ROADMAP.md](ROADMAP.md) says what's next.

The owner (Mike) is not a developer and doesn't review code. Work autonomously, and stop
only for decisions that are genuinely his (DESIGN D13). How you finish depends on what
you are:

- **A Mahler run** (your prompt came from a recipe, and `MAHLER_ISSUE` is set): implement,
  verify and commit locally, then end with a STATUS line. Use only the admitted issue context
  supplied in the prompt; do not fetch raw issue comments or fall back to ambient human GitHub
  credentials. The conductor snapshots and pushes the branch, opens the PR, watches CI,
  coordinates independent review and merges (DESIGN D18, D46, D47). Reserve `NEEDS-YOU` for
  `product`, `money`, `credentials`, `data-policy` or `irreversible` owner decisions;
  conductor and external waits use `waiting` instead (DESIGN D52).
- **A chat session** (Claude Code, Codex or any other interactive agent): nothing picks up
  your PR unless you hand it over. Finish with `mahler ship` or merge it yourself. See
  "Working on an issue by hand" below. A PR you open and walk away from stays open.

## This repo manages itself

Mahler's own issues are worked by Mahler (ROADMAP Phase B). So:

- **The daemon runs from `~/.mahler/app`, a separate clone pinned to a known-good commit.**
  Never edit live state or the daemon installation under `~/.mahler/` from a task.
  Authorized source changes inside the assigned `~/.mahler/worktrees/` task worktree
  are allowed; never touch other worktrees or live state. Changes reach the daemon only
  through a merge to `main` with green CI and passing unit tests. The launch-failure
  circuit breaker pauses new launches, allowing periodic canaries; a global breaker
  triggers immediate rollback when `launch_ok` names a different, launch-proven commit.
  Runtime rollback records the rejected SHA in `bad_sha`, which self-update skips; a
  newer commit remains eligible (DESIGN D17).
- **`launcher/` is hand-installed.** Changing `mahler-launcher`, the plist or `install.sh`
  has no effect until someone re-runs `launcher/install.sh`. Say so in your final DONE
  summary (the conductor puts it on the PR), and ping via `mahler notify`.
- **The daemon must never break itself.** Keep `mahler tick` exception-safe per project.
  Don't add a dependency outside the standard library until the MCP phase introduces
  `uv` deliberately. The console stays standard library (DESIGN D27).
- **GUI-driving work on the Mac mini must use the desktop gate.** Run it through
  `mahler desktop run -- <command>`; the plain `mahler desktop` check is advisory and
  reserves nothing. Only the gate's `GATE-DENIED` line means deferral; a wrapped command
  that ran and independently exited 75 is an ordinary failure. If the gate defers, report
  the outstanding coverage rather than a pass (DESIGN D40, D50).
- `max_parallel = 1` for this repo: one change to the conductor at a time.

## Verify

```bash
python3 -m mahler.repo_guard
python3 -m unittest discover -s tests
python3 tests/run_random.py
python3 -X dev tests/run_strict.py
```

Python 3.14+, standard library only. The lease rules (`tests/test_ledger.py`) are the
part that must stay right. Extend those tests whenever you touch `ledger.py`.

CI runs all four commands. The repository guard rejects tracked browser-profile state;
the randomized runner detects order dependencies, and the strict runner turns resource
leaks and unraisable exceptions into failures.

Tests must be fully isolated (mahler#93): no test may leak env vars, module globals,
files, or SQLite state that another test depends on, and no test may touch the real
`~/.mahler` state — use `Ledger(':memory:')` or temp dirs. Pass the randomized runner's
printed seed back to `python3 tests/run_random.py <seed>` to reproduce a failure.

## Reuse past evidence

Search earlier run transcripts on the Mac mini with
`mahler history <project> "<query>" [--limit N]` before repeating an investigation.

After a concrete error recovery or owner/reviewer correction, consider recording one
reusable, non-obvious lesson under `.agents/skills/lesson-*/SKILL.md`. Never create a
lesson for routine successful work or to restate a mechanical check. Search existing
lessons first and patch a match; otherwise follow DESIGN D44's schema and safety rules.
Touch at most one lesson per issue and commit it in the same branch and PR. Lessons are
advisory and take effect only after merge.

## Working on an issue by hand (interactive sessions)

Read [docs/by-hand.md](docs/by-hand.md) before starting an interactive task; it contains
worktree setup, the lease walkthrough, and the required by-hand merge verification.
Every change starts from an issue (`mahler add mahler "title"` if needed).
Use `mahler claim mahler#N` before work, `mahler heartbeat mahler#N` while working,
`mahler ship mahler#N` after pushing, and `mahler release mahler#N` if stopping unfinished.
Work in your own worktree on `mahler/<N>-short-slug`; put `Fixes #N` in the PR body.
Never switch branches in the shared primary checkout. Finish with `mahler ship` or the
linked manual merge protocol; never leave an untracked open PR.
Use `mahler next-id mahler <prefix>` for shared sequential IDs; never invent one.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the shipped components and source map.
