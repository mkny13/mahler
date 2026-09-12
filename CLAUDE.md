# Mahler

Mahler conducts coding agents (Claude Code, Antigravity's `agy`, later Cline) through
each project's GitHub-issue backlog. It is quota-aware, and it hands work off across
platforms. **Read [DESIGN.md](DESIGN.md) before changing behaviour.** Each decision there
(D1–D17) has its reasoning. [ROADMAP.md](ROADMAP.md) says what's next.

The owner (Mike) is not a developer and doesn't review code. Work autonomously: branch,
commit, PR, CI, merge. Stop only for decisions that are genuinely his (DESIGN D13).

## This repo manages itself

Mahler's own issues are worked by Mahler (ROADMAP Phase B). So:

- **The daemon runs from `~/.mahler/app`, a separate clone pinned to a known-good commit.**
  Never edit anything under `~/.mahler/` from a task. Changes reach the daemon only through
  a merge to `main` with green CI (the launcher self-updates, and rolls back after two
  failed ticks).
- **`launcher/` is hand-installed.** Changing `mahler-launcher`, the plist or `install.sh`
  has no effect until someone re-runs `launcher/install.sh`. Say so in the PR, and ping
  via `mahler notify`.
- **The daemon must never break itself.** Keep `mahler tick` exception-safe per project.
  Don't add a dependency that isn't in the standard library until the console/MCP phase
  introduces `uv` deliberately.
- `max_parallel = 1` for this repo: one change to the conductor at a time.

## Verify

```bash
python3 -m unittest discover -s tests
```

Python 3.12+, standard library only. The lease rules (`tests/test_ledger.py`) are the
part that must stay right. Extend those tests whenever you touch `ledger.py`.

## Working on an issue by hand (interactive sessions)

If you're a chat session, not a Mahler run, take part in the lease protocol (DESIGN D6):

```bash
mahler claim mahler#12        # before you start. If an agent held it, you win and it hands over
mahler heartbeat mahler#12    # if you've been quiet a while (leases lapse after 30 min idle)
mahler release mahler#12      # when you stop without finishing
```

Other useful commands: `mahler status`, `mahler usage --probe`, `mahler pause` / `resume`,
`mahler add mahler "title"`.

## Layout

- `mahler/`:
  - `ledger.py`: SQLite leases, runs, usage, events
  - `scheduler.py`: the tick
  - `runner.py`: worktrees, launch, snapshot
  - `router.py`: quota policy
  - `platforms.py`: CLI adapters and usage readers
  - `gh.py`: GitHub
  - `cli.py`: the command line
- `recipes/`: the prompts runs receive (`sort.md`, `build.md`). The STATUS-line contract at
  the end of each is parsed by `platforms.status_line`.
- `launcher/`: the stable launcher, launchd plist and installer.
