# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.2.1] - 2026-10-29

### Added

- Safe tracked deployment - files that `deploy` replaces are backed up byte-for-byte (permissions and symlink targets included) in a private `.nexus-backups/` directory inside the collection folder, and `undeploy` restores those originals
- Every deployed file is fingerprinted (SHA256 for copies, exact link target for symlinks) and verified before removal or replacement - a file changed outside nexus-dl is refused with its path reported, never silently destroyed
- Crash recovery - deploy/undeploy mutations are journaled to `.nexus-journal.jsonl`, so an interrupted run resumes deterministically on the next run without guessing and without deleting unrecognized files
- Deploy/undeploy runs are serialized per collection with a lock file - a second concurrent run fails fast with a clear busy error
- Downloads verify the exact expected byte size and the cached manifest MD5 (when known) before the file is published - truncated, corrupt, or wrong downloads are rejected and never clobber an existing file
- Partial deploy/undeploy failures are now visible - the CLI exits non-zero and prints the refused/failed paths, and the web UI surfaces them in the task result, instead of reporting a clean success

### Changed

- `plugins.txt` and game INI writes go through the same backup and ownership tracking as mod files - the original config files are restored on `undeploy`, and neither is written when the deployment itself failed
- Redeploy plans and preflights before touching the existing deployment, leaves a working deployment in place when there is nothing to deploy, and stops when any previously deployed file cannot be safely removed
- Downloads stream to uniquely named temp files with request timeouts, validate API filenames as a single safe path component (traversal, absolute paths, separators, and dot names are refused, never rewritten), and never include signed download URLs in error messages
- `sync` prefetches the collection manifest once before downloading, so MD5 verification applies from the very first sync; if the manifest cannot be fetched, downloads fall back to exact-size-only verification
- Recovery and undeploy refuse to act on records pointing outside the configured game/prefix directories, through symlinked parents, or at backup references that are not exact SHA256 blob names - such entries are kept for review with their backups retained, and a restore replaces the deployed file only after the staged copy re-verifies against the recorded hash
- State saves are atomic (unique temp file, fsync, rename) - a crash mid-write can no longer corrupt `.nexus-state.json`
- Pre-0.2.1 deployment records carry no fingerprints or backups - removing them requires a content match against the staging source, and originals destroyed by older versions cannot be restored
- The legacy REST API size field (KB) is no longer treated as an exact byte count for download verification
- The API User-Agent reports the real package version instead of a hardcoded 0.1.0
- Wheels now package only `nexus_collection_dl` - the tests directory is excluded from distribution artifacts

### Fixed

- `import` matching for browser-downloaded files - deterministic matching using exact archive names, verified content hashes, and the Nexus `-modid-version-timestamp` filename suffix instead of fuzzy word matching
- `import` verifies name matches against the manifest content hash when one is known - a stale, corrupt, or wrong same-named download is rejected with an error instead of being imported
- Files saved under an archive name but holding invalid content (error pages, truncated downloads) stay pending with a validation error instead of counting as downloaded
- `import` saves state after each successful mod before cleaning up the archive, so a mid-run failure no longer loses progress for already-imported mods
- Ambiguous or unverifiable downloads now stay pending with an actionable error instead of silently importing the wrong file
- Each downloaded file can only fulfill one pending mod per import, and internal state/config files plus generated load-order/plugins output are no longer match candidates
- `add`, `add-local`, and tracked-mod sync were broken by the file_id-keyed state introduced in PR #55 - `add` looked the registration up by mod_id (a KeyError whenever mod_id differs from file_id), repeated `add-local` calls could collide on synthetic keys, and track sync compared file_id keys against Nexus mod IDs (multi-file mods were tracked wrong); all three now use the correct keys

### Removed

- `tqdm` dependency - import progress now goes through the existing progress callback

The browser-download import matching in this release builds on contributions from Anish (aajunk42).

## [0.2.0] - 2026-03-02

### Added

- Per-collection subdirectories - each collection gets its own named subfolder in the mods directory
- File conflict reporting after sync and deploy - shows which mods overwrote shared files
- Automatic version check - prints an upgrade notice if a newer release is available on GitHub
- `--no-extract` flag for sync, update, add, and import - keeps archives intact for external mod managers
- Resumable sync - re-running sync skips already-downloaded mods

### Fixed

- Download progress display - shows one active bar instead of stacking all completed bars
- Incremental state saves after each mod download for abort resilience
- Collection subdirectory naming uses display name instead of slug

## [0.1.0] - 2026-02-08

### Added

- `sync` command - download entire Nexus Mods collections via CLI
- `update` command - check for and download mod updates
- `deploy` / `undeploy` commands - symlink or copy mods into the game directory
- `add` command - download individual mods by URL
- `add-local` command - register manually placed mods
- `import` command - match browser-downloaded files to pending mods (free account workflow)
- `load-order` command - regenerate load order from cached manifest
- `track-sync` command - sync Nexus tracked mods to match local loadout
- `status` command - show installed mods and available updates
- `serve` command - local web UI for browser-based mod management
- Free and Premium account support
- LOOT integration for Bethesda game plugin sorting
- Docker support with included libloot
