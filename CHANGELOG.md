# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.2.2] - 2026-11-16

### Fixed

- LOOT plugin sorting never worked and now does. `sort_plugins_with_loot` called `loot.create_game_handle()`, `Database.load_lists()` and `Database.sort_plugins()`, none of which current libloot exposes, and derived `GameType` member names from display names, so Skyrim Special Edition looked up `SkyrimSpecialEdition` rather than `SkyrimSE` and returned the collection order before making any call at all. Every path fell back to the collection author's plugin order, so a deploy with libloot installed was indistinguishable from one without it. Sorting now drives `loot.Game`, `Database.load_masterlist`, `Game.load_plugins` and `Game.sort_plugins`, and `LOOT_GAME_MAP` names the exact enum members
- libloot wheels were not being built or attached to releases. The workflow failed on `v0.2.1`, `v0.3.0-beta.1` and `v0.3.0-beta.2`, each time for a different reason: the manylinux build image has no `apt-get` and its CentOS 7 mirrors are gone; `rustup` in that image configures no default toolchain so `cargo` refuses to run; `maturin` wrote wheels to the Cargo workspace root rather than the crate's `target/` and, with no `-i`, built only for the image's Python 3.9; JavaScript actions (artifact and release upload) run inside the job container and need glibc 2.28 which that image does not have; `GITHUB_TOKEN` lacked `contents: write`, which GitHub reports as `Test artifacts are not permitted here. (HTTP 422)`; and the image's entrypoint is `maturin`, so `docker run ... bash` ran `maturin bash`. Releases now carry cp310-cp313 manylinux wheels
- `typing_extensions` is a declared dependency. It was previously only present transitively, so `pip install` on Python 3.11 and later could fail at import while `uv run` looked fine

### Changed

- Web UI HTTP error responses return a fixed category string instead of the exception message: `another operation is already running for this collection`, `collection state is not usable`, `the collection manifest could not be parsed`, `the Nexus API rejected the request`, or `internal error`. Exception text is no longer reflected into `{"error": ...}` payloads. Task result payloads can still carry error strings collected while a deploy ran, and the CLI remains the diagnostic surface
- A busy collection reports `another operation is already running for this collection` instead of being folded into the general unusable-state error
- Enderal and Enderal SE are called out as not LOOT-sortable. libloot has no `GameType` for either, so both take the collection author's plugin order; they remain supported for `plugins.txt` otherwise

### Removed

- The `loot` optional dependency extra. `pip install nexus-dl[loot]` resolved to `loot`, an unrelated 2014 GitHub CLI helper; the libloot bindings are not published to PyPI at all, so the extra installed the wrong package into a venv meant to sort plugins. Install the bindings with `setup.sh` (builds from source) or a wheel from a release

### Security

- All 7 open Dependabot advisories are closed by bumping five locked dependencies: certifi 2026.7.22 to 2026.10.23, brotli 1.2.0 to 1.2.1, brotlicffi 1.2.0.2 to 1.2.1.0, urllib3 2.7.0 to 2.8.0, and Werkzeug 3.1.8 to 3.1.9. urllib3 accounts for three of the seven

### Added

- ruff and mypy run in CI alongside the Python 3.10-3.13 test matrix, and pull requests get a template
- README gains a "Stable vs beta" section describing the two release channels

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
