# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.4.0] - 2026-11-21

The FOMOD release. A browser click on a mod with an installer can now end with
that mod's chosen options in the game directory, and standalone `add` and
`import` make the same choices. **This release has had no field use.** What was
exercised is the test suite and a scripted smoke run against real Nexus archives
and one real collection; the browser click chain, free-account downloads, and
Windows were not part of that. "Known gaps" at the end names what else is
deliberately not in this release.

### Added

- A click on a FOMOD mod now asks which options to install, and a running
  `nexus-dl shell` places the result in the game directory in the same sitting.
  At 0.3.0 a click on a mod with an installer (`fomod/ModuleConfig.xml`)
  extracted every option folder at once, which is the wrong content, and for
  the common installer where every option is `Optional` it was **nothing at
  all**: measured against real configs, the installer's own defaults install 0
  of 83 entries for XPMSE, 0 of 6 for Improved Map, 0 of 2 for SPID. A click
  now stops before extraction. With a shell running, the click drain offers the
  installer's groups one at a time in install order (the same walk `add` uses,
  driven by the same code, on the shell's plain input lines), records the picks
  on the mod, and offers the deploy that places the new files straight after:
  website to game directory in one sitting. Declining leaves the mod waiting and
  nothing is installed from a guess. With no shell running, the click keeps the
  archive and records the mod as `pending_choices`, and
  `nexus-dl import <dir> --fomod-option 'Group:Option'` (or `--fomod-config`,
  or the prompts at a terminal) finishes it. A re-click of a mod waiting on
  choices says so instead of trying again. Script-based installers (FOMOD 1.0,
  `fomod/script.cs`) have no options to pick and are refused with the reason.
  `pending_choices` joins `pending_download` everywhere a pending state is
  reported: the `mods` table, `import`'s matching, the click-event `outcome`
  tokens, and the shell's event line.

- Standalone `add` and `import` make FOMOD option choices instead of extracting
  every option at once. When an archive carries a readable installer, its groups
  are presented and only the chosen options install. Three ways to choose:
  `--fomod-option 'Group:Option'` (repeatable, `'Step:Group:Option'` when a
  group name appears in more than one step), `--fomod-config <file.json>` for
  names that contain a colon and for batch imports (each pick may carry a
  `"file"` key scoping it to one archive), and the prompts, which run only at a
  terminal when no picks were passed. **The picks are stored in the collection
  state**, so a later `deploy` installs exactly what was chosen rather than the
  installer's defaults. Re-adding a mod keeps the stored picks as far as they
  still name real options in a changed installer and asks only about the rest.
  With no picks and nobody to ask (a pipe, `--json`, a script), the archive is
  refused rather than extracted to every option at once, with a message naming
  the flags to pass; that refusal has its own error code, `fomod_choices`, so a
  harness can tell "give me choices" apart from "I cannot read this installer",
  and a pick that names nothing the installer offers is refused the same way
  rather than silently matched onto something similar. Script-based installers
  have no groups to pick from and are refused under `fomod_installer`.
  `--no-extract` is unchanged as the escape hatch. Flags that cannot apply (a
  mod with no installer, or `--no-extract`) say so in the result's `warnings`
  instead of vanishing silently.

- `--json` before the subcommand switches any non-interactive command to
  machine-readable output: stdout carries exactly one JSON object and nothing
  else, and the envelope is `{"ok", "result", "error"}` with a stable
  snake_case `error.code` on failure. Wrong invocations stay click usage
  errors (exit 2), and `shell`, `nxm`, and `handler` do not support `--json`.
- Five CLI commands closing the coverage gap with the interactive shell:
  `remove`, `collections`, `games`, `mods`, and `health`. `remove` takes
  exactly one of `--file-id`/`--mod-id`, never touches payload files on disk,
  tombstones positive Nexus mod ids so `sync`/`update` skip them until an
  explicit re-add, and never prompts (the shell's `/remove` keeps its
  confirmation). `collections` and `games` list from a local scan, `mods` lists
  a collection's tracked files from local state only, and `health` reports API
  key source (never the value), nxm handler state, notification capability,
  unreadable states, and write-lock state, exiting 0 even when a check reports
  WARN or ERROR because it is a report and not a gate.
- An agent skill at `.agents/skills/nexus-dl/` for AI harnesses that discover
  skills in the checkout: intent-to-command routing, safe defaults (read before
  write, `deploy --dry-run` before `deploy`, confirm before `remove`, never
  invent mod/file ids), the `--json` envelope contract, and error triage.
- A per-collection click-events file (`<mods_dir>/.nexus-events.jsonl`) so a
  running shell can learn that a browser click landed a mod. The `nxm` command
  appends one compact JSON line per click attempt (no URIs, keys, or
  credentials), with an `outcome` token and enough context to report it.
  `NxmResult` gains `download_status` so a recoverable pending entry is
  distinguishable from a flat failure. Append is `O_APPEND`; rotation is
  best-effort under the collection lock; a broken event file never fails a
  download.
- The interactive shell drains click events at each prompt boundary and reports
  them in one block. Three clicks on one collection produce one report and one
  `Deploy '<name>' now? [yes/no]: ` prompt, not three. Only `downloaded` and
  `extracted_from_archive` events trigger the deploy prompt. Pre-existing
  events are not replayed; only collections under the shell's root are visible.
  Live-while-idle polling (events arriving while the user sits at the prompt)
  is not included; the drain fires at prompt boundaries only.
- `/deploy [<index|name>]` in the interactive shell, resolving a collection
  exactly like `/use` (or the current selection). The shared deploy flow shows
  dry-run counts, asks for confirmation, then deploys and renders the result:
  errors first, conflicts, warnings, and a `Deploy incomplete` line on partial
  failure instead of a clean success. Ctrl-C, Ctrl-D, and any input failure at
  the confirm prompt cancel the deploy. Nothing touches the game directory
  without an explicit `yes`.
- `nexus-dl --version` prints `nexus-dl v<version>` and exits 0. It answers
  without a subcommand, like `--help`, and is plain text.

### Changed

- Mods with a FOMOD installer deploy from an **install plan** instead of a
  top-level folder filter. The plan is computed from the mod's
  `ModuleConfig.xml`: `requiredInstallFiles`, plus the entries of the options
  the collection manifest or the user picked, plus every
  `conditionalFileInstalls` pattern whose dependencies hold. Each entry is a
  (source, destination) pair at full path depth, so what lands is what the
  installer asked for and where it asked for it. Four things the old filter
  could not do, each found by running it against real Nexus archives:

  1. Options that partition nested paths inside a shared top-level directory.
     XPMSE (mod 1988) splits `Skeleton Rig\HDT` from `Skeleton Rig\HDT 4.51`
     from `Skeleton Rig\TBBP`; the old filter worked on top-level names, so
     picking one skeleton style installed all three.
  2. `source` entries that carry a `destination` renaming or moving the
     content. NVAC (mod 53635) installs `<folder source="NVSE"
     destination="SKSE"/>`; the old filter ignored `destination`.
  3. Sources written relative to the `fomod/` directory's parent rather than
     the archive root. SPID (mod 36869) wraps everything in one folder and
     writes `AE/SKSE/Plugins` relative to that wrapper; the old filter could
     not see the option directories and installed both game versions.
  4. `conditionalFileInstalls` patterns, which is where a modern config does
     most of its installing (in one real patch collection, 254 of 264 sources
     live inside patterns). `flagDependency` is evaluated against the flags the
     selected options set. `fileDependency` and `gameDependency` are evaluated
     when the caller supplies game state, and **no caller supplies game state
     yet**, so those two condition types are undecidable at deploy: the mod
     falls back to the folder-based install and says so per mod rather than
     installing a guessed branch. Options marked `alwaysInstall` install
     regardless of the pick, `installIfUsable` installs when the option is
     usable, and step-level `<visible>` conditions hide steps whose flags are
     not set.

  **When a condition cannot be evaluated, nothing is guessed.** A multi-game
  installer like NVAC picks `SKSE`/`FOSE`/`NVSE`/`OBSE` from the game version
  and the load order; with no game state those patterns are undecidable, and
  the mod keeps the old folder behaviour instead of a randomly chosen branch.
  The same applies to an unreadable config, which is also the only case the old
  folder filter still serves. Non-FOMOD mods are untouched. Installer configs
  are decoded in whatever encoding the author wrote them: UTF-8, UTF-16 with
  or without a BOM, in either byte order.

- Error and warning text in results (the `errors`/`warnings`/`error` fields of
  deploy, undeploy, sync, update, import, add, remove, and nxm outcomes) now
  carries a short category in place of raw OS and unexpected-exception text, so
  an exception message can no longer carry an absolute filesystem path or
  reflected request detail out of the process. Refused and failed paths and
  their authored refusal reasons still appear exactly as before; what changed
  is the exception suffix beside them (`no such file or directory (ENOENT)`,
  `unexpected error (<TypeName>)`). What you lose: the CLI's error lines no
  longer show raw OS wording such as `[Errno 2] No such file or directory:
  '/path'`. The path is still in the authored part of the message when it
  matters, and the `--json` envelope's `error.message` is unchanged, because
  that is the documented diagnostic surface.

### Removed

- The local web UI, `nexus-dl serve`, and the `nexus-dl-web` console script.
  `nexus-dl shell` is the intended driver and the CLI covers the same
  operations over the same service layer; `--json` gives harnesses
  machine-readable output. **What is gone:** the browser dashboard and live
  progress over Server-Sent Events. The CLI's Rich progress bar and phase text
  lines are the replacement; that polish has no terminal equivalent yet. If you
  want it back, say so and it becomes work on the CLI's progress rather than a
  restored web app. `flask` and its dependencies are no longer installed
  alongside the tool. The `0.3.0` README already carried the intent, and the
  shell has covered the workflows since this release's click-event drain and
  `/deploy`.

### Security

- Archive extraction refuses traversal names, symlink members, and tool-owned
  files. Member names are validated before extraction (no `..` segments, no
  absolute or drive-letter paths, backslashes normalised so a Windows-written
  zip lays out its directories instead of writing literal-backslash filenames),
  symlink members are refused rather than recreated, and the staging tree is
  walked after extraction and before anything is merged, which covers the
  system `7z`/`unar`/`unrar` fallbacks as well. An archive that plants a
  `.nexus-*` name at the collection root (a forged `.nexus-journal.jsonl`
  beside the real one, say, which recovery would otherwise honour) is refused
  with a clear error instead of merged beside the real state. The `py7zr`
  floor moves past CVE-2022-44900.

### Fixed

- Deploy keeps the stored FOMOD options that still match instead of dropping
  all of them when one is stale. One renamed option after a mod update used to
  erase every pick, install the installer's defaults (nothing at all for an
  all-Optional installer), and report success. Now the still-matching picks
  install, the rest are named in a `warnings` entry, and an installer whose
  options were matched by name from stored choices (the legacy path for
  collections extracted before option records existed) says so rather than
  looking recorded. When the install plan cannot be evaluated without game
  state and the folder-based install runs instead, that is reported per mod.
  Dry-run and `--json` carry the same warnings as the deploy result.
- `deploy` no longer ignores `--game-dir` and `--prefix`. The pre-deploy checks
  read what a previous run happened to record instead of the values this
  deploy was given, so a first deploy of a manually configured game refused to
  run even with `--game-dir` on the command line. The parameter now wins over
  state, auto-detection last, and every check reports on the effective values.
- `deploy` and `undeploy` count the same files. A deploy reported writes, not
  destinations: two entries naming one destination counted twice, so
  `Deployed 904 files` landed 763 symlinks. Deploy counts destinations and
  undeploy counts mod files, so both report what is in the game directory, and
  the dry run plans with the same picks and recorded configs the real deploy
  uses.
- `health` and `/status` no longer call the nxm handler OK while its executable
  is missing. They checked only that the desktop entry was registered, so an
  entry pointing at a deleted virtualenv read `installed and the default
  nxm:// handler`; they now check the executable too and name the path they did
  not find.
- `deploy` stops writing packaging cruft into the game directory: `meta.ini`
  (Vortex metadata a mod author packed by accident), `.gitignore`, and licence
  text join `readme*` on the skip list. And a deploy conflict between two files
  with one name now names the source paths, which is the part that differs,
  instead of `overwritten by CapeAF_0.nif (was CapeAF_0.nif)`.
- The deploy-time FOMOD folder filter's wrong installs are fixed. Four things
  were wrong with it, all found by checking it against real installer configs:
  `<folder>` and `<file>` sources were recorded in different shapes so folder
  installs could never match the skip set; only the first folder of a plugin
  was read, so unselected options still deployed half their content; the
  name-matching fallback matched each numbered folder against each mod
  independently, so one mod's choices could skip another mod's folders; and
  the numbered-option pattern required a dash or underscore where real archives
  use a plain space (`01 Main Mod`), which could deploy a mod's files under its
  option wrapper directory. Space, dash, and underscore are all accepted now.
  The filter itself is now only the fallback for an unreadable config; readable
  installers deploy from the install plan above.

### Known gaps and what was not verified

- **No field use** (see the top). Specifically unverified: free-account
  downloads (the API download path needs a premium key), Windows, and the
  browser click chain end to end.
- `deploy` has no confirmation of any kind, on purpose: the shell's `/deploy`
  is the only confirm and nothing is ever deployed automatically. A harness
  driving the CLI deploys non-interactively and that is the intended path.
- A deploy conflict names source paths, not the owning mods. Once files are
  extracted into the merged tree, which mod produced which file is not
  recorded.
- `fileDependency` and `gameDependency` installer conditions fall back to the
  folder-based install with a per-mod warning until deploy can see the game's
  load order.
- Queued for 0.4.1: the full installer XML lives in the state file (a few KB
  to about 60KB per FOMOD mod, rewritten on each save); installer configs have
  no size cap before parsing; error-text scrubbing has no diagnostic escape for
  a maintainer reading a bug report; the removed `serve` command reports
  "command not found" rather than a deprecation stub; the staging/game-dir
  overlap guard compares path strings rather than resolved paths; the legacy
  tree scan and the archive marker check disagree on how deep a
  `ModuleConfig.xml` may live.
- Upgrading from 0.3.0: state files load unchanged. Installers extracted
  before this release carry no option records, so deploy matches their options
  by name from stored choices and warns that it is doing so. Downgrading to
  0.3.0 after a 0.4.0 deploy is untested.

## [0.3.0] - 2026-11-17

### Added

- Native handling of `nxm://` individual-file links (the website's "Mod manager download" button) with a new `nexus-dl nxm` command and a `nexus-dl handler` group: `map`/`unmap` bind games to synced collection directories, `install`/`uninstall`/`status` manage an opt-in, user-level desktop entry that remembers the previous default handler and restores it on uninstall
- `nxm` verifies downloads (exact size, manifest MD5 when known) before publishing them and refuses a file that has neither a positive expected size nor a well-formed known checksum, so nothing unverifiable is ever written; a retained verified archive makes re-running a link trigger no download traffic (an already-downloaded entry reports "already downloaded" with zero network calls, and an entry whose extraction failed finishes extracting from the archive, verified against the cached manifest hash when known; with track-sync enabled a background tracked-mod sync may still make API calls), supports `--mods-dir`, standalone `--output` saves, `--no-extract`, `--no-load-order`, and desktop notifications for every click outcome: the registered handler runs headless (`Terminal=false`) and its Exec line passes `--notify`, while interactive terminal runs print inline instead (`--notify/--no-notify` override)
- Link credentials are handled strictly: the `key`/`expires` pair is forwarded to the download-link request only and never stored in state, results, logs, or error messages; the URI `user_id` is a local account identity check and is never sent to the API; expired or credential-less free links fail locally with re-click guidance and no writes; `nxm://` collection links are refused with guidance to use `sync`; nothing is ever deployed automatically
- A machine-local API key store for browser-launched downloads: `nexus-dl handler set-api-key` reads the key from hidden input (never argv, never echoed), validates it locally without any network call, and writes it atomically to `~/.config/nexus-dl/api-key` with 0600 permissions; `handler remove-api-key` deletes only the key file, and `handler status` reports whether a key is available (flag, env, stored, or none) without ever showing values. Key resolution has a single precedence: `--api-key` flag, then `NEXUS_API_KEY`, then the stored key; with none of the three, the existing keyless free/pending flow is unchanged
- `nexus-dl nxm` given a non-nxm input (an `https://` mod-page URL, say) now explains that the link type is not usable and what to do instead (`nexus-dl add <mod page URL>`; the "Mod manager download" click hands `nxm://` links straight to the registered handler and the site offers no copyable link), and points at `handler install` for click-through; the raw URI and its query are never echoed
- Browser-launched `nxm` runs append one redacted status line (UTC timestamp, success/warn/error outcome, mod/file ids or a static reason code) to `~/.local/state/nexus-dl/nxm.log` (0600, XDG state dir), so click outcomes are visible after the fact without a terminal; lines never carry URIs, query values, or credentials, and logging is best-effort: a broken log never fails a download
- Per-command help in the interactive shell: every command accepts a bare `--help` as its only argument (and `/help <command>` as an alias) and prints its form, one-line summary, and a few usage lines with one realistic example instead of executing; help triggers only when `--help` is the entire argument, so an argument that merely contains that text is never swallowed (a collection literally named `--help` is still reachable by index), and a bare `--help` line shows the command list
- The interactive shell keeps the current collection visible and sticky: the prompt is `nexus-dl [Name]> ` with the selected collection (names sanitized so odd characters cannot garble it) and plain `nexus-dl>` otherwise, a root with exactly one collection selects it at startup with a one-line note, bare `/use` reports the current selection (or `none selected`) plus the collection list without changing anything, and an explicit `/use` remembers the selection per root in `<ROOT>/.nexus-shell.json` (one value: the collection directory name, no secrets, written only on explicit `/use`); the next shell on that root restores it when the directory still holds a readable state file, and anything stale or corrupt is ignored silently

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
- The desktop entry now runs headless (`Terminal=false`) with `--notify` baked into its Exec line, so click outcomes are reported as desktop notifications instead of depending on whether the launched process happens to have a TTY; every outcome notifies when notifications are on (success, already-downloaded, saved-to-output, usage refusals, and every failure path), `notify-send` runs under a hard 3-second timeout with non-zero exits reported honestly as failures, and when `notify-send` is not installed the notification is skipped and `~/.local/state/nexus-dl/nxm.log` remains the only feedback channel
- `handler install` is idempotent (the entry is always rewritten, refreshing stale entries from older versions) and performs a best-effort notification-daemon self-test via gdbus, dbus-send, or a timed notify-send probe; if no daemon is reachable it warns visibly and points at `~/.local/state/nexus-dl/nxm.log` as the fallback feedback channel, while missing probe tooling skips silently and never fails the install
- The `nxm` command gained an outer error boundary: any failure, including service construction and unexpected exception types, writes exactly one redacted status-log line (static reason code only) and emits the notification before exiting non-zero, instead of dying as an invisible traceback
- The interactive shell reworded the re-add case on the premium `https://` `/add`, free-account pending-download, and `nxm://` downloaded/extracted report paths (it previously fired only on the first; the `nxm://` already-downloaded and saved-to-output reports make no manual-mod claim either way): a mod the current collection's cached manifest already knows reports `Re-added '<name>' to '<collection>'; it is a collection member, not a manual mod` instead of the misleading manual-mod wording, because the next sync folds it back into the collection-managed set either way (reporting only; behavior unchanged)
- Ctrl-C during an interactive-shell service call now reports that the operation may have partially completed and points at `/status`, instead of claiming nothing was marked done (services save incrementally, so that claim could be false)
- Desktop registration hardening: `xdg-mime` timeouts and subprocess failures surface as actionable errors with install rolled back or uninstall recovery state preserved, the recorded executable must be an existing executable file (also in `status`), `status` queries the current default exactly once, and `notify-send` messages are passed after a `--` option separator
- `/status` in the interactive shell splits into two forms. Bare `/status` is now a tool-wide local report that works with no collection selected and never touches the network: a per-game overview with totals, removal tombstone counts per collection, and a health report (API key source, never the value; nxm handler install and default state; the desktop notification-display switch with the exact `gsettings` fix when it is off and silence when the desktop is unknown; `xdg-mime`/`notify-send` capabilities; unreadable state files that the listings skip; and write-lock state). Every check is guarded and a failing check renders as a WARN/ERROR line instead of a traceback. The per-collection revision comparison moved to `/status <index|name>`, which keeps the old behavior and adds local details (mod/pending/tombstone counts, the stored collection URL with its query stripped for display, and the state path). The lock check never reports stale locks and says why in its line: the writer lock is a kernel flock released when its process exits and the lock file is never removed, so the read-only `/proc/locks` probe reports locks in use as running operations, never as stale

### Fixed

- Re-clicking after a failed extraction recovers again: a pending entry whose verified archive is retained on disk now finishes its extraction from that archive without re-downloading (reported as `extracted_from_archive`; no download traffic, though a background tracked-mod sync may still make API calls when track-sync is enabled; the archive is kept, so later clicks dedupe as already-downloaded), restoring the extraction-failure recovery intent that no-replace publication had broken by refusing the guaranteed re-download. A retained archive that fails the sanity checks (including the cached manifest hash, when one is known) is still never overwritten: the download path's strict no-clobber publish refuses with actionable guidance naming `nexus-dl import`
- A malformed cached manifest checksum can no longer masquerade as a known MD5: only well-formed 32-hex-digit hashes count, validated at manifest ingestion, at attachment, and again at the nxm verify-or-refuse gate, so a file with no positive size and a malformed cached hash is refused with nothing written instead of publishing unverified; a malformed hash alongside a positive size still verifies on size alone
- The nxm unexpected-error boundary redacts the full secret scope: the resolved API key and the raw URI's key/expires values are redacted literally, and any-scheme URIs carrying a query (including nxm://, previously only http(s)) are stripped from both the terminal text and the notification body
- A failed `handler install` re-run over a working installation no longer destroys it: rollback restores the prior entry contents and leaves the MIME default exactly as found, instead of deleting the working entry and flipping the default to the recorded previous handler
- `handler install` rollback can no longer destroy or strand state in two more edge cases: a pre-existing desktop entry that cannot be read aborts the install before anything is changed (never replace what cannot be snapshotted for rollback), and a failure after registration succeeded with no other default to restore KEEPS the entry with an honest error, because xdg-mime cannot unset a default and deleting the entry would leave the MIME registration dangling
- `/sync` in the interactive shell to an already-resolved collection (bare or index/name) now updates that collection's OWN directory, via a new additive `service.sync` `target_dir` parameter; the destination was previously derived from the API's collection name, so a renamed directory or a root-as-collection layout silently synced a different directory than the one selected. URL-form `sync` and CLI callers keep the name-derived destination
- Collection selectors in the interactive shell (`/use`, `/sync` targets, `/status <target>`, `/mods <collection>`) resolve an exact case-sensitive name FIRST, then an ASCII-digit index, then a unique case-insensitive name: a collection named `2024` is reachable by name again, and unicode digits are never read as an index (previously an all-digits selector was an index first, silently shadowing a same-named collection)
- Two `/status` round review fixes: a non-string `game_domain` in a hand-edited state can no longer abort the overview before the health section (display values are coerced with a never-raising helper, so every section renders), and `scan_collections`' skip reason survives whitespace-only exception text (the reason slicing could raise `IndexError`, which would have hidden every collection for `list_collections`' callers too since it delegates)
- A non-string `collection_name` (or mod name) in hand-edited state no longer crashes the shell's collection scan: the scan/list layer coerces state-derived names to text at the source, so `/collections`, `/use`, and bare `/status` render instead of dying in the failure path
- State- and user-derived text echoed by the interactive shell is scrubbed of terminal control characters (ESC, newline, BEL in a hand-edited name or a hostile input echo), covering table cells, `/use` echoes, selector echoes in error messages, and the `/remove` confirmation prompt; rich-markup escaping is unchanged
- Routine shell output scrubbing keeps the minimum-length floor for bare prose, matching the tiered redaction design: a short absorbed `key=` value from a typed link no longer garbles ordinary output like `Synced 1 mods`, while short secrets are still redacted inside URI queries and percent-encoded runs
- The shell `/sync` report no longer drops `SyncResult.conflicts` and tracked/untracked counts silently: conflicts render as a warning plus `file: winner overwrote loser` lines (capped at 20), exactly like the CLI sync command
- `get_status` (which also feeds `update --dry-run`) now applies the same service-level tombstone filter as sync/update: a removed mod page never appears as installable or missing, and the skipped count surfaces in `/status <index|name>` output
- Load order regeneration wraps every failure class (cached-manifest parse errors, loot-path write errors) as `LoadOrderRegenError`, so nothing escapes as a raw exception after a state change has already committed
- State loading raises `StateError` on a present-but-wrong `removed_mods` (or `mods`) shape instead of silently dropping tombstones, which risked resurrecting removed mods on the next save; states without the key still load unchanged
- The manifest download link check rejects a whitespace-only URI like an empty one
- nxm error-text redaction hardened at the unexpected-failure boundary: nxm:// URIs are stripped by scheme with or without a query (a query-less link still leaks identity), and boundary redaction now covers percent-encoded (both hex cases) and case-varied forms of the API key and link key/expires values, not just their exact raw shapes
- Extraction staging race: `extract_archive` staged at a predictable shared path (`/tmp/nexus_extract_<archive stem prefix>`) that it recursively deleted before and after each run, so two concurrent extractions of same-stem archives (two nxm clicks, two collections) could delete or mix each other's staged content and publish wrong files. Staging is now a unique hidden `tempfile.mkdtemp` directory (mode 0700) inside the destination directory: same filesystem as the final move (a rename, never a cross-device copy), invisible to the `import`/`deploy` scanners that skip hidden entries, and removed on success and on every failure path

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
