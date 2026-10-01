# Build caches and disk hygiene (macOS host)

Mahler runs several agents at once, each in its own git worktree of the same repo. Build
tools key their caches by project *path*, so every worktree gets its own copy. On
2026-09-30 this filled the internal disk: Xcode's default
`~/Library/Developer/Xcode/DerivedData` held 24 `CouchTour-<hash>` folders (about 16 GB,
700-830 MB each) created in 14 days, and the Mac mini hit 99% full.

## Policy: DerivedData lives inside the worktree

Xcode is set machine-wide to "Relative to Workspace":

```bash
defaults write com.apple.dt.Xcode IDEDerivedDataPathMode -int 1
defaults write com.apple.dt.Xcode IDECustomDerivedDataLocation -string "DerivedData"
```

Builds now land in `<project dir>/DerivedData`, and for `phish-in` that is under
`/Volumes/ExtSSD160/.mahler-worktrees`, off the internal disk. The folder is deleted with
the worktree (`git worktree remove --force`).

Verify (should print a path inside the project, not `~/Library`):

```bash
xcodebuild -project macos/CouchTour.xcodeproj -scheme CouchTour -showBuildSettings | grep OBJROOT
```

This is a per-user machine setting, **not in any repo**. Re-apply it on a fresh machine
and re-check after each Xcode update (checked on Xcode 27.0).

### Why not one shared path

A single fixed `-derivedDataPath` per project was rejected. Parallel builds share one
build database and fail with `build.db: database is locked`. One folder per worktree is
the safe choice; the cost is a cold first build in each new worktree.

### Caveats

- An explicit `-derivedDataPath` overrides the setting. `macos/scripts/install.sh`,
  `install-beta.sh` and CI pass `-derivedDataPath macos/build`, so a worktree that
  installs and also builds keeps two ~500 MB folders.
- SwiftPM (`swift build/test`) ignores the setting and uses `Packages/*/.build`.
- Worktrees outside `worktree_root` (`~/.gemini/antigravity/worktrees`,
  `~/.cline/worktrees`) still build on the internal disk.
- Hard-coded `~/Library/Developer/Xcode/DerivedData` paths in tools will look in the wrong
  place.
- Untested idea: Xcode 26+ shared compilation cache (`COMPILATION_CACHE_ENABLE_CACHING=YES`
  with `COMPILATION_CACHE_CAS_PATH` on the SSD and `COMPILATION_CACHE_LIMIT_SIZE` set).
  Absolute paths in outputs may limit hits across worktrees. Measure before relying on it.

## Git and backups

- `~/.config/git/ignore` contains `**/DerivedData/` so agents cannot commit it. Repos
  already ignore `build/`.
- Time Machine: `/Volumes/ExtSSD160/.mahler-worktrees` is excluded (done by hand in
  System Settings; `tmutil addexclusion` needs Full Disk Access). Without it every
  worktree's build output is backed up to the Synology. Check with
  `tmutil isexcluded /Volumes/ExtSSD160/.mahler-worktrees`.

## Weekly cleanup

`/Volumes/ExtSSD160/scripts/cache-cleanup/weekly-cache-cleanup.sh` (symlinked from
`~/bin/weekly-cache-cleanup.sh`, run by launchd Sundays 03:00 with `--apply`) now also:

- removes `DerivedData` and `macos/build` in worktrees idle more than 3 days,
- clears the npm cache and `_npx`, and Gradle's `build-cache-1`,
- truncates `claude-remote-control.out.log` and cline's `hub-daemon.log` above 100 MB.

Logs go to `~/Library/Logs/weekly-cache-cleanup/`. Run it without `--apply` for a dry run.
If launchd reports a missing script, the symlink is broken (this caused the missed runs of
2026-09-20 and 09-27).
