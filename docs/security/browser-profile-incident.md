# Chromium profile history-purge runbook

This is the operator procedure for mahler#589. It removes only
`.calendar-chrome3/` from the public `mkny13/mahler` repository's branches and
tags. It does not inspect or print profile contents. The rewrite changes commit
IDs and is a coordinated incident operation, not routine repository maintenance.

Do not run the publish step from an autonomous agent. A privileged interactive
operator runs it only after the tool and this runbook have merged to `main` and
CI is green.

## Meaning of `--confirmed`

Passing `--push --confirmed` asserts all of the following:

- mahler#588 and this tooling are merged;
- Mahler is paused, every active run has drained, and every open Mahler PR has
  been merged or deliberately closed;
- the current remote branch and tag names and object IDs were recorded;
- affected browser sessions, cookies, tokens, and stored credentials have been
  invalidated or rotated without copying their values into a ticket or log;
- the synthetic rehearsal and the local rewrite both passed, and the recovery
  bundle is stored in a private directory;
- nobody will push from an old clone until every clone and worktree is replaced.

If any statement is false, do not pass `--confirmed`.

## 1. Pause and drain

1. Confirm mahler#588 and #589 are on `main` with green CI.
2. Run `mahler pause`. This stops new launches but lets existing work finish.
3. Use `mahler status` until no run is active. Inspect
   `gh pr list -R mkny13/mahler --state open`; let the conductor finish each
   in-flight change or deliberately close it. Do not rewrite while another
   branch may still be published.
4. Stop the daemon and console so neither fetches, updates, or starts work during
   clone replacement:

   ```sh
   launchctl bootout gui/$(id -u)/local.mahler
   launchctl bootout gui/$(id -u)/local.mahler.serve
   ```

   A "service not found" response for the optional console is harmless. Do not
   restart either service until the safe-resume checklist is complete.
5. Inventory every clone and linked worktree on the Mini and any other machine.
   Record unpushed work now. After publication, none of these repositories may
   push or be reused; fetching into them is not sufficient because their object
   databases retain the exposed history.

## 2. Record and rehearse

Use a private incident directory outside every clone. The recovery bundle
contains the original exposed objects and must stay private.

```sh
INCIDENT_DIR="$HOME/mahler-purge-incident-YYYYMMDD"
install -d -m 700 "$INCIDENT_DIR"
git ls-remote --heads --tags https://github.com/mkny13/mahler.git \
  | sort > "$INCIDENT_DIR/remote-refs.before"
chmod 600 "$INCIDENT_DIR/remote-refs.before"
git filter-repo --version
python3 -m unittest tests.test_purge_calendar_profile_history
```

The focused test is a complete disposable rehearsal: it creates synthetic
branches and tags, removes only `.calendar-chrome3/`, preserves similarly named
and unrelated history, verifies the recovery bundle, proves the confirmation
gate, and proves a concurrent collaborator push is rejected by the ref leases.
Upstream `git-filter-repo --version` normally prints a hexadecimal source hash;
that is expected.

Record the before file's hash and keep both it and the bundle private:

```sh
shasum -a 256 "$INCIDENT_DIR/remote-refs.before"
```

## 3. Build and verify the rewritten mirror

Run the merged tool from a trusted fresh checkout. The work path must not exist;
the tool creates a bare mirror, writes a mode-0600 recovery bundle beside it,
rewrites locally, and checks every local branch and tag. It performs no remote
write in this mode.

```sh
bin/purge-calendar-profile-history \
  --work "$INCIDENT_DIR/rewrite.git"
```

Expected final messages say that no reachable local branch or tag contains
`.calendar-chrome3/` and identify these artifacts:

- `$INCIDENT_DIR/rewrite.git` — the verified rewritten mirror;
- `$INCIDENT_DIR/rewrite.git.purge-backup.bundle` — the original history for
  incident rollback only;
- `$INCIDENT_DIR/rewrite.git/purge-state.json` — original per-ref object IDs,
  counts, and verification state.

Stop if verification fails, a ref vanishes, an unaffected ref changes, or the
bundle does not verify. The remote is still untouched. Preserve the log and
bundle, discard only the disposable rewritten mirror, and diagnose before
starting again from a new path.

## 4. Publish once, with leases

Re-run the remote inventory immediately before publishing and compare it with
the recorded file. Any changed, added, or removed branch or tag means work is
not drained: stop, preserve the collaborator's work, and create a new rewrite
from a fresh mirror.

```sh
git ls-remote --heads --tags https://github.com/mkny13/mahler.git \
  | sort > "$INCIDENT_DIR/remote-refs.now"
cmp "$INCIDENT_DIR/remote-refs.before" "$INCIDENT_DIR/remote-refs.now"
```

After rechecking the meaning of `--confirmed`, publish:

```sh
bin/purge-calendar-profile-history --push --confirmed \
  --work "$INCIDENT_DIR/rewrite.git"
```

The tool sends one atomic push containing only branches and tags, with an
explicit `--force-with-lease=<ref>:<old-id>` for every ref. It never combines
those leases with unconditional `--force`. If a collaborator moved a ref, the
whole push is rejected and no ref changes.

Protected branches, repository rules, or hidden refs can reject the push. That
is a stop condition: leave Mahler paused and contact the repository owner and
GitHub Support with the rejection and the before/after ref inventory. Do not
disable a rule, broaden a bypass, push refs one at a time, add `--force`, or try
to update GitHub's hidden pull-request refs directly.

## 5. Verify GitHub and request Support cleanup

Clone the published repository into a second new mirror. Do not verify from the
rewrite mirror or an old clone.

```sh
git clone --mirror https://github.com/mkny13/mahler.git \
  "$INCIDENT_DIR/verify.git"
git -C "$INCIDENT_DIR/verify.git" rev-list --count --all -- \
  ".calendar-chrome3/"
git -C "$INCIDENT_DIR/verify.git" fsck --full
git ls-remote --heads --tags https://github.com/mkny13/mahler.git \
  | sort > "$INCIDENT_DIR/remote-refs.after"
```

The count must be exactly `0`. The after inventory must contain the same branch
and tag names as before, with rewritten IDs only where the prohibited path was
reachable. If not, keep all services stopped and use the rollback section.

Open a private GitHub Support request for sensitive-data removal. Identify the
repository, the path `.calendar-chrome3/`, the rewrite time, and affected commit
and pull-request references by ID only. Ask Support to purge cached commit/blob
views and unreachable objects and to remove or dereference affected pull-request
refs. Never paste browser-profile values or attach the recovery bundle. Treat
the incident as open until Support confirms cleanup.

## 6. Replace every clone and worktree

Every pre-rewrite clone—including `~/.mahler/app`, the primary `Mahler` clone,
all Mahler-created worktrees, and clones on other machines—is stale. Do not run
`git pull`, copy its `.git` directory, or push from it. Rename it out of service,
clone afresh into an empty path, and compare the new clone's `origin/main` with
GitHub. Preserve a stale clone only while extracting reviewed, unpushed patches;
apply those patches to a fresh clone and review them for the prohibited path.

For the daemon clone, keep `~/.mahler/config.toml`, the ledger, logs, and launcher
in place; only replace `~/.mahler/app` with a fresh clone. The services must
still be stopped. For example, move the stale app to a clearly quarantined path,
clone `mkny13/mahler` anew as `~/.mahler/app`, and detach it at `origin/main`.
Once the fresh clone and any recovered patch are verified, delete the stale
clone and every linked worktree so they cannot reintroduce old objects. Apply
the same replacement procedure to the primary clone and every other machine.

Before deleting anything, confirm its path explicitly and confirm all needed
unpushed work was recovered. Do not use a recursive delete against `$HOME`, `~`,
a workspace root, or an unresolved variable.

## 7. Rollback

- Before publication, or after any rejected atomic push, the remote is
  unchanged. Keep the bundle, choose a new empty work path, and rerun after the
  cause is fixed.
- After publication, prefer forward repair: keep Mahler stopped, start with the
  recovery bundle in a private disposable mirror, correct the purge procedure,
  remove the prohibited path again, and publish with exact leases against the
  current rewritten ref IDs.
- Restoring the original refs from the bundle re-exposes the Chromium history.
  Do that only as an incident-owner decision coordinated with GitHub Support.
  Use a fresh bare clone of the bundle, compare every current remote ref, and
  require an exact per-ref `--force-with-lease`; never use plain `--force` or
  `--mirror`. Repeat remote verification and Support cleanup afterward.

The bundle is not an ordinary long-term backup. Retain it only under the
incident retention decision, with mode 0600 and access limited to the operator.

## 8. Safe resume

Resume only when all of these are true:

- fresh remote verification reports zero prohibited-path commits;
- GitHub Support has the cache and pull-request-ref cleanup request (and any
  required protected-ref action) in hand;
- affected sessions and credentials are confirmed invalidated or rotated;
- `~/.mahler/app`, the primary clone, every worktree, and every other clone were
  replaced, and no stale repository can push;
- the fresh daemon clone passes `python3 -m unittest discover -s tests`;
- `git status`, `git remote -v`, and `git rev-parse HEAD` in each active clone
  match the intended clean repository and do not expose credential-bearing
  remote URLs.

Restart the daemon (and the optional console if installed), then resume
scheduling:

```sh
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/local.mahler.plist"
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/local.mahler.serve.plist"
mahler resume
mahler status
```

Watch the first ticks and pushes. Any reference to an old commit ID or a push
from a stale clone is a new incident: pause again immediately.
