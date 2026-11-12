# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Native handling of `nxm://` individual-file links (the website's "Mod manager download" button) with a new `nexus-dl nxm` command and a `nexus-dl handler` group: `map`/`unmap` bind games to synced collection directories, `install`/`uninstall`/`status` manage an opt-in, user-level desktop entry that remembers the previous default handler and restores it on uninstall
- `nxm` verifies downloads (exact size, manifest MD5 when known) before publishing them and refuses a file that has neither a positive expected size nor a known checksum, so nothing unverifiable is ever written; a retained verified archive (for example from `--no-extract`) makes re-running a link a no-op with zero network, supports `--mods-dir`, standalone `--output` saves, `--no-extract`, `--no-load-order`, and desktop notifications for every click outcome: the registered handler runs headless (`Terminal=false`) and its Exec line passes `--notify`, while interactive terminal runs print inline instead (`--notify/--no-notify` override)
- Link credentials are handled strictly: the `key`/`expires` pair is forwarded to the download-link request only and never stored in state, results, logs, or error messages; the URI `user_id` is a local account identity check and is never sent to the API; expired or credential-less free links fail locally with re-click guidance and no writes; `nxm://` collection links are refused with guidance to use `sync`; nothing is ever deployed automatically
- A machine-local API key store for browser-launched downloads: `nexus-dl handler set-api-key` reads the key from hidden input (never argv, never echoed), validates it locally without any network call, and writes it atomically to `~/.config/nexus-dl/api-key` with 0600 permissions; `handler remove-api-key` deletes only the key file, and `handler status` reports whether a key is available (flag, env, stored, or none) without ever showing values. Key resolution has a single precedence: `--api-key` flag, then `NEXUS_API_KEY`, then the stored key; with none of the three, the existing keyless free/pending flow is unchanged
- `nexus-dl nxm` given a non-nxm input (an `https://` mod-page URL, say) now explains that the link type is not usable, how to get an `nxm://` link from the "Mod manager download" button (right-click, copy link address), and points at `handler install` for click-through; the raw URI and its query are never echoed
- Browser-launched `nxm` runs append one redacted status line (UTC timestamp, success/warn/error outcome, mod/file ids or a static reason code) to `~/.local/state/nexus-dl/nxm.log` (0600, XDG state dir), so click outcomes are visible after the fact without a terminal; lines never carry URIs, query values, or credentials, and logging is best-effort: a broken log never fails a download

### Changed

- All mutating operations (`sync`, `update`, `add`, `add-local`, `import`, `track-sync enable/disable`, and `nxm` downloads) are now serialized per collection with the writer lock - a second concurrent operation fails fast with a busy error instead of racing; previously only deploy/undeploy were serialized. `track-sync push` remains read-only locally and unlocked
- Signed download URLs and credentials are redacted from every error message and log path, and HTTP debug logging is suppressed around signed requests so link credentials cannot leak through urllib3/requests logs
- The API key itself now joins every error-redaction scope in the API client (it travels in a request header, so a hostile endpoint reflecting it in a URL or body cannot surface it), covering keys resolved from the flag, the environment, or the stored key alike
- Docs now follow the real click-through flow: no daemon is involved (the OS dispatches each click to a short-lived nexus-dl process), one-time setup is `uv sync` (`--extra dev` for tests), `sync` a disposable collection directory, `handler set-api-key`, `handler map`, `handler install`, one click to verify download/extract/notification, a second click to verify the re-download path (the default flow deletes the extracted archive, so the zero-network dedupe applies only to retained archives, for example with `--no-extract`), and `handler uninstall` to restore the previous handler; the manual paste flow moved to a footnote with the corrected `read -s -r NXM_LINK` usage (run `read` first, then paste the link on its own line - appending it to the `read` command line hangs)
- Review hardening for the nxm flows: mapped collection directories are canonicalized to absolute paths at map time (relative config entries are dropped), and nxm downloads never replace ANY existing destination entry - publication is an atomic no-replace hard link from the verified temp file, so a concurrently created file, symlink, dangling link, or even the collection's own corrupt archive is never overwritten (remove the file first or use `--output`; a filesystem without hard-link support fails cleanly with nothing written)
- The signed download-link API request refuses redirects (`allow_redirects=False`) and is timeout-bounded, so the apikey header and credential pair can never be forwarded to an arbitrary host or stall the serialized log-suppression lock; log suppression itself is serialized across threads and restores logger levels exactly, including on errors and nested use
- nxm failures are domain-safe end to end: non-JSON API responses (proxy/portal pages) are reported without echoing bodies or URLs, malformed file-size metadata is refused instead of crashing, account verification fails closed when the API response carries no account identifier, the downloaded archive is retained until the state save succeeds (failure recovery, not a dedupe guarantee: after a successful extraction the archive is deleted, so a re-click re-downloads and re-verifies it), and post-save follow-ups (tracked-mod sync, load order) report truthful warnings on a successful result instead of crashing a completed download; explicitly provided secrets are redacted at any length
- Every Nexus API request now carries explicit connect and read timeouts (previously only the signed download-link request did), and requests transport failures (timeouts, connection errors) are translated into redacted domain errors with categorical text only, so nothing reachable from `nxm` can hang unboundedly under the collection lock or escape as a raw traceback
- Collection states with no game domain fail closed everywhere in the nxm flow (`handler map`, routing via both `--mods-dir` and mapped candidates, and the download itself): an empty domain is never a wildcard that accepts links for any game; the error tells you to re-create the state with `nexus-dl sync`
- The desktop entry now runs headless (`Terminal=false`) with `--notify` baked into its Exec line, so click outcomes always surface as desktop notifications instead of depending on whether the launched process happens to have a TTY; every outcome notifies when notifications are on (success, already-downloaded, saved-to-output, usage refusals, and every failure path), and `notify-send` runs under a hard 3-second timeout with non-zero exits reported honestly as failures
- `handler install` is idempotent (the entry is always rewritten, refreshing stale entries from older versions) and performs a best-effort notification-daemon self-test via gdbus, dbus-send, or a timed notify-send probe; if no daemon is reachable it warns visibly and points at `~/.local/state/nexus-dl/nxm.log` as the fallback feedback channel, while missing probe tooling skips silently and never fails the install
- The `nxm` command gained an outer error boundary: any failure, including service construction and unexpected exception types, writes exactly one redacted status-log line (static reason code only) and emits the notification before exiting non-zero, instead of dying as an invisible traceback
- Desktop registration hardening: `xdg-mime` timeouts and subprocess failures surface as actionable errors with install rolled back or uninstall recovery state preserved, the recorded executable must be an existing executable file (also in `status`), `status` queries the current default exactly once, and `notify-send` messages are passed after a `--` option separator

### Fixed

- Extraction staging race: `extract_archive` staged at a predictable shared path (`/tmp/nexus_extract_<archive stem prefix>`) that it recursively deleted before and after each run, so two concurrent extractions of same-stem archives (two nxm clicks, two collections) could delete or mix each other's staged content and publish wrong files. Staging is now a unique hidden `tempfile.mkdtemp` directory (mode 0700) inside the destination directory: same filesystem as the final move (a rename, never a cross-device copy), invisible to the `import`/`deploy` scanners that skip hidden entries, and removed on success and on every failure path

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
