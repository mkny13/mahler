# Working on Mahler issues by hand

For interactive sessions. Autonomous runs follow [the repository instructions](../CLAUDE.md)
and leave shipping to the conductor (DESIGN D18).

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

Put it under the worktree root, never in the home directory. The default is
`~/.mahler/worktrees` (the runner's default, which the janitor also scans). To keep
worktrees elsewhere, such as an external volume, set `worktree_root` in your config and use
the same directory here.

```bash
WT=~/.mahler/worktrees   # or your configured worktree_root
git -C <your-mahler-checkout> worktree add "$WT/mahler-12" -b mahler/12-short-slug origin/main
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
(D19). Build agents verify and commit locally; the conductor pushes and ships (D18).

Other useful commands: `mahler status`, `mahler usage --probe`, `mahler pause` / `resume`,
`mahler release <project>` (preview or `--version X.Y.Z --publish`),
`mahler add mahler "title" --body … [--label type:goal]`, `mahler next-id mahler <prefix>` (shared sequential IDs,
e.g. decision numbers — never invent one).

