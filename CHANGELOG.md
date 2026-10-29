# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed

- `import` matching for browser-downloaded files - deterministic matching using exact archive names, verified content hashes, and the Nexus `-modid-version-timestamp` filename suffix instead of fuzzy word matching
- Ambiguous or unverifiable downloads now stay pending with an actionable error instead of silently importing the wrong file
- Each downloaded file can only fulfill one pending mod per import, and internal state/config files are no longer match candidates

### Removed

- `tqdm` dependency - import progress now goes through the existing progress callback

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
