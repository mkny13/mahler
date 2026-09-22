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
  verify, commit and push to your branch, then end with a STATUS line. The conductor opens
  the PR, watches CI and merges (DESIGN D18).
- **A chat session** (Claude Code, Codex or any other interactive agent): nothing picks up
  your PR unless you hand it over. Finish with `mahler ship` or merge it yourself. See
  "Working on an issue by hand" below. A PR you open and walk away from stays open.

## This repo manages itself

Mahler's own issues are worked by Mahler (ROADMAP Phase B). So:

- **The daemon runs from `~/.mahler/app`, a separate clone pinned to a known-good commit.**
  Never edit anything under `~/.mahler/` from a task — changes reach the daemon only
  through a merge to `main` with green CI (the launcher self-updates, and rolls back after
  two failed ticks).
- **`launcher/` is hand-installed.** Changing `mahler-launcher`, the plist or `install.sh`
  has no effect until someone re-runs `launcher/install.sh`. Say so in your final DONE
  summary (the conductor puts it on the PR), and ping via `mahler notify`.
- **The daemon must never break itself.** Keep `mahler tick` exception-safe per project.
  Don't add a dependency outside the standard library until the MCP phase introduces
  `uv` deliberately. The console stays standard library (DESIGN D27).
- `max_parallel = 1` for this repo: one change to the conductor at a time.

## Verify

```bash
python3 -m unittest discover -s tests
```

Python 3.12+, standard library only. The lease rules (`tests/test_ledger.py`) are the
part that must stay right. Extend those tests whenever you touch `ledger.py`.

Tests must be fully isolated (mahler#93): no test may leak env vars, module globals,
files, or SQLite state that another test depends on, and no test may touch the real
`~/.mahler` state — use `Ledger(':memory:')` or temp dirs. Check order-independence
with `python3 tests/run_random.py <seed>` (omit the seed for a random one).

## Working on an issue by hand (interactive sessions)

If you're a chat session, not a Mahler run, take part in the lease protocol (DESIGN D6).
**Every change starts from an issue.** If there isn't one, file it (`mahler add mahler
"title"`) before you write code. A PR with no issue is invisible to the conductor.

```bash
mahler claim mahler#12        # before you start. If an agent held it, you win and it hands over
mahler heartbeat mahler#12    # if you've been quiet a while (leases lapse after 30 min idle)
mahler ship mahler#12         # when it's pushed: the conductor opens/watches the PR and merges it
mahler release mahler#12      # when you stop without finishing
```

**Finish every task one of two ways**, never by leaving a PR open:

1. **`mahler ship <project>#N`** (the default). Run it after you push. It hands your
   branch, or its open PR (`--pr X`), to the conductor, which watches CI, starts a fix run
   if CI goes red, and squash-merges on green (D18). It adds `Fixes #N` to the PR if it's
   missing, and releases your claim.
2. **Merge it yourself**, following the protocol under "Before merging by hand" below.

Use branch `mahler/<N>-short-slug` and put `Fixes #N` in the PR body. An open PR that no
item tracks gets you a "PR nobody is shipping" ping after 2 hours.

**Work in your own worktree, never by switching branches in `~/Mahler`.** Several sessions
often share that checkout. A `git checkout` or `reset` there carries off or wipes whatever
another session has uncommitted. This happened on 2026-09-12 (mahler#27).

```bash
git -C ~/Mahler worktree add ../Mahler-12 -b mahler/12-short-slug origin/main
```

**Before merging by hand**, inspect the PR's actual `baseRefName`, `headRefOid`, and
checks (`gh pr view <pr> --json baseRefName,headRefOid,statusCheckRollup`). Record the
checked head SHA and target; require successful checks (or no checks configured).
Fetch that head, then fetch the actual target with an explicit refspec:

```bash
git fetch origin <checked-head-sha>
git fetch origin +refs/heads/<base>:refs/remotes/origin/<base>
git rev-parse refs/remotes/origin/<base>
git merge-base --is-ancestor <fetched-base-sha> <checked-head-sha>
```

If ancestry exits 1, rebase/repush onto current base and wait for fresh checks on the
new head. Any lookup/Git error means stop the merge and retry verification. Re-read
the PR to confirm the same head, target and acceptable checks, repeat the base fetch
and ancestry check immediately before merging, then use
`gh pr merge <pr> --squash --match-head-commit <checked-head-sha>`.
This detects observed drift; an independent base push after the check remains a race
(D19). Build agents still stop at their push (D18).

Other useful commands: `mahler status`, `mahler usage --probe`, `mahler pause` / `resume`,
`mahler release <project>` (preview or `--version X.Y.Z --publish`),
`mahler add mahler "title"`, `mahler next-id mahler <prefix>` (shared sequential IDs,
e.g. decision numbers — never invent one).

## Layout

- `mahler/`:
  - `ledger.py`: SQLite leases, runs, usage, events
  - `scheduler.py`: the tick's entry — `Ctx`, the lock, the order of the passes
  - one module per pass: `watchdog.py` (process health, heartbeats), `sync.py`
    (GitHub in, labels out), `finalize.py` (every exit is a handoff),
    `ship.py` (the conductor's PR/CI/merge pass), `tick.py` (maintenance,
    lease expiry, scheduling, `start`), `usage.py` (quota readings, D23 burst),
    `platform_audit.py` (D20's periodic self-audit of Mahler's own platform
    tier/capability assumptions, mahler#206 — not a managed-project pass)
  - `runner.py`: worktrees, launch, snapshot — `prompt.py` writes what a run is told
  - `router.py`: quota policy
  - `platforms.py`: CLI adapters and usage readers
  - `gh.py`: GitHub — the conductor's push/PR/CI/merge machinery (D18)
  - `cli.py`: the command line
  - `console/`: the operator console (DESIGN D27) — `state.py` (everything it shows,
    copy included), `page.py` (phone and desktop layouts), `actions.py` (its writes),
    `console.css`, `console.js`; the spec and screenshots are in `docs/console/`
  - the rest: `config.py` (paths and config), `notify.py` (ntfy pings), `serve.py`
    (serves the console), `mcp.py` (MCP server), `digest.py` (daily digest),
    `janitor.py` (stale worktree/old-ref cleanup), `backup.py` (database backups),
    `presence.py` (human-session detection), `redact.py` (credential redaction),
    `version.py` (version info)
- `recipes/`: the prompts runs receive (`sort.md`, `build.md`, `fix.md`). The STATUS-line
  contract at the end of each is parsed by `platforms.status_line`.
- `launcher/`: the stable launcher, launchd plist and installer.
