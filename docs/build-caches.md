# Build caches and disk hygiene (macOS host)

Mahler runs agents in isolated git worktrees. Xcode's default
`~/Library/Developer/Xcode/DerivedData` keys build artifacts by project path, so each
new worktree leaves another cache on the internal disk. On 2026-09-30, 24
`CouchTour-<hash>` folders created over 14 days occupied about 16 GB (700–830 MB each),
and the Mac mini's internal disk reached 99% full.

## Policy and fresh-machine setup

Use Xcode's relative DerivedData location so each project builds into its own
`<project>/DerivedData` inside the worktree. For phish-in, worktrees live under
`/Volumes/ExtSSD160/.mahler-worktrees`, placing these artifacts on external storage.
A single shared `-derivedDataPath` was rejected: concurrent agents would contend for
one index/build database. Separate worktrees need separate build output directories.

Apply these preferences as the macOS user who runs the builds:

```bash
defaults write com.apple.dt.Xcode IDEDerivedDataPathMode -int 1
defaults write com.apple.dt.Xcode IDECustomDerivedDataLocation -string DerivedData
```

These are **per-user machine preferences**, not repository settings. Re-apply them
on a fresh machine or for a different build user. An explicit `-derivedDataPath`
on a build command overrides this policy; keep any such path local to the worktree.

The host's global Git ignore file, `~/.config/git/ignore`, includes
`**/DerivedData/` to keep build output out of ordinary Git adds. On a fresh machine,
create the directory and add the rule if missing:

```bash
mkdir -p ~/.config/git
touch ~/.config/git/ignore
grep -qxF '**/DerivedData/' ~/.config/git/ignore || printf '%s\n' '**/DerivedData/' >> ~/.config/git/ignore
```

If Git uses a custom `core.excludesFile` or `XDG_CONFIG_HOME`, add the same rule to
that active global ignore file instead.

## Verify the build location

From the Xcode project directory in a worktree, run:

```bash
xcodebuild -showBuildSettings | grep OBJROOT
```

Supply the project's usual `-project` or `-workspace` and `-scheme` arguments when
needed. For example, from a couch-tour worktree:

```bash
xcodebuild -project macos/CouchTour.xcodeproj -scheme CouchTour -showBuildSettings | grep OBJROOT
```

Check that `OBJROOT` points inside that worktree's `DerivedData`, rather than
`~/Library/Developer/Xcode/DerivedData` or another worktree. Repeat this check after
changing Xcode versions or build-command overrides.

## Worktree lifecycle

[`runner.remove_worktree()`](../mahler/runner.py) checks that the worktree is inside
the configured worktree root, then calls `git worktree remove --force` followed by
`shutil.rmtree(..., ignore_errors=True)` to remove any remaining directory. This
also discards ignored and untracked build output such as `DerivedData` inside the
worktree. Global DerivedData folders from earlier builds are outside that cleanup
boundary and remain until separately cleaned up.

## Weekly cleanup

The host maintenance script
`/Volumes/ExtSSD160/scripts/cache-cleanup/weekly-cache-cleanup.sh` handles:

- Legacy global `~/Library/Developer/Xcode/DerivedData` caches left from before the
  relative-path policy, reclaiming accumulated space on the internal disk.
- `DerivedData` and `macos/build` in worktrees idle more than 3 days.
- npm caches and `_npx`, and Gradle's `build-cache-1`.
- Runaway log truncation, including Claude remote-control and Cline hub logs.

The script is host configuration, maintained outside this repository. Changing the
Xcode preferences does not move or delete old global caches; weekly maintenance
complements cleanup when worktrees are removed. On a fresh machine, restore the
maintenance script and its weekly schedule separately from the preferences above.
