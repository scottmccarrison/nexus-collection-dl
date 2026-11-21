# nexus-dl command reference

Every command with its exact signature, arguments, flags, and the JSON
`result` shape produced by `--json`. Field lists are the real result-object
fields; the "Result object fields" section at the bottom documents each one.

## Invocation

```
nexus-dl [--api-key KEY] [--json] <command> [args] [flags]
```

Global options (before the subcommand):

| Option | Meaning |
|---|---|
| `--api-key KEY` | Nexus Mods API key for this run. Resolution order is `--api-key`, then the `NEXUS_API_KEY` environment variable, then the stored key (`nexus-dl handler set-api-key`). |
| `--json` | Emit the machine-readable envelope on stdout and nothing else. Human diagnostics move to stderr. Must come **before** the subcommand. |

**JSON mode is not available** for `shell`, `nxm`, or `handler`.
Passing `--json` to them (or to any command in the wrong position) is a usage
error: text on stderr, exit code 2, no JSON.

**Directory semantics.** `sync COLLECTION_URL MODS_DIR` treats `MODS_DIR` as a
parent: it creates `MODS_DIR/<collection name>/` (the name is sanitized) and
puts the state file there. Every other command's `MODS_DIR` is that collection
directory itself: the folder containing `.nexus-state.json`. `collections`,
`games`, and `health` take an optional `ROOT_DIR` (default: the current
directory); a collection is `ROOT_DIR` itself or an immediate subdirectory
holding `.nexus-state.json`.

## Commands

### sync

```
nexus-dl sync COLLECTION_URL MODS_DIR [--skip-optional] [--no-load-order] [--no-extract]
```

| Argument / flag | Meaning |
|---|---|
| `COLLECTION_URL` | Nexus Mods collection URL |
| `MODS_DIR` | Parent directory; the collection is created as a subfolder of it |
| `--skip-optional` | Skip optional mods |
| `--no-load-order` | Skip load order generation |
| `--no-extract` | Keep downloaded archives without extracting |

Resumable: re-running skips already-downloaded mods. On a free account the
result carries `pending_downloads` instead of downloaded mods.

`result`: SyncResult.

### update

```
nexus-dl update MODS_DIR [--skip-optional] [--dry-run] [--no-load-order] [--no-extract]
```

| Argument / flag | Meaning |
|---|---|
| `MODS_DIR` | Collection directory (must exist) |
| `--skip-optional` | Skip optional mods |
| `--dry-run` | Show what would change, download nothing |
| `--no-load-order` | Skip load order generation |
| `--no-extract` | Keep downloaded archives without extracting |

`result` (normal run): UpdateResult.

`result` (`--dry-run`):

| Field | Type | Meaning |
|---|---|---|
| `dry_run` | `true` | Marker |
| `status` | StatusResult | The full comparison, same shape as `nexus-dl status` |
| `new` | list of str | Names of mods that would be installed |
| `updates` | list of str | Names of mods that would be updated |
| `removed` | list of str | Names of mods no longer in the collection |

### status

```
nexus-dl status MODS_DIR
```

Compares installed state against the collection's latest revision (needs the
network for the revision check; falls back to listing installed mods when the
API is unreachable). `result`: StatusResult.

### load-order

```
nexus-dl load-order MODS_DIR
```

Regenerates `load-order.txt` (and `plugins.txt` for Bethesda games) from the
cached manifest without downloading anything.

`result`:

| Field | Type | Meaning |
|---|---|---|
| `load_order_files` | list of str | File names written |

### deploy

```
nexus-dl deploy MODS_DIR [--game-dir D] [--prefix P] [--copy] [--dry-run]
```

| Argument / flag | Meaning |
|---|---|
| `MODS_DIR` | Collection directory |
| `--game-dir D` | Game install directory (auto-detected from Steam when omitted) |
| `--prefix P` | Proton/Wine prefix (auto-detected when omitted) |
| `--copy` | Copy files instead of symlinking |
| `--dry-run` | Report the deploy plan, change nothing |

Deployment writes into the game directory and replaces files there
(pre-existing files are backed up under `.nexus-backups/`; `undeploy` restores
them). Always run `--dry-run` first.

`result` (normal run): DeployResult.

`result` (`--dry-run`):

| Field | Type | Meaning |
|---|---|---|
| `dry_run` | `true` | Marker |
| `game_root_files` | int | Files that would go to the game root |
| `data_files` | int | Files that would go to the game's Data directory |
| `skipped` | int | Entries the classifier skipped |
| `total_files` | int | Total files that would be deployed |

### undeploy

```
nexus-dl undeploy MODS_DIR
```

Removes deployed files from the game directory and restores backed-up
originals. Does not touch the collection's own files.

`result`:

| Field | Type | Meaning |
|---|---|---|
| `removed` | int | Number of deployed files removed |

### add

```
nexus-dl add MOD_URL MODS_DIR [--file-id N] [--no-load-order] [--no-extract]
```

| Argument / flag | Meaning |
|---|---|
| `MOD_URL` | Nexus Mods mod page URL |
| `MODS_DIR` | Collection directory |
| `--file-id N` | Specific file id to download (omit to auto-select the main file) |
| `--no-load-order` | Skip load order regeneration |
| `--no-extract` | Keep the archive without extracting |
| `--fomod-option 'Group:Option'` | One FOMOD choice, repeatable. `'Step:Group:Option'` when a group name appears in more than one step |
| `--fomod-config FILE` | JSON list of picks (`{"group", "option", "step"?, "file"?}`), for names with a colon or for batch imports |

Registers the mod as a manual mod (phase 999): `update` never flags it as
removed from the collection. `result`: AddResult. When the run itself fails
partway (`success: false`), the envelope reports `ok: false` with
`error.code` `operation_failed` and still carries the AddResult in `result`.

**FOMOD archives need option choices.** If the archive carries a
FOMOD installer (`fomod/ModuleConfig.xml`) it needs option choices first,
and extracting it without them installs every option folder at once. Pass
the picks with `--fomod-option`/`--fomod-config`, or run at a terminal and
answer the prompts. The picks are stored in state and a later `deploy`
installs exactly those options. With no picks and nobody to ask (`--json`,
a pipe), `add` raises `fomod_choices` and keeps the archive; `import`
records it per file and leaves that item pending. A pick that names
nothing the installer offers is `fomod_choices` too, and is never silently
matched onto something similar. Script-based installers (FOMOD 1.0,
`fomod/script.cs`) have no groups to choose from and raise
`fomod_installer`. `--no-extract` still registers the mod and leaves the
extraction to you.

### add-local

```
nexus-dl add-local NAME MODS_DIR [--no-load-order]
```

Registers a mod that is already present in the collection directory (a local
synthetic mod).

`result`:

| Field | Type | Meaning |
|---|---|---|
| `mod_id` | int | Synthetic id assigned (negative) |
| `name` | str | The name given |

### import

```
nexus-dl import MODS_DIR [--no-load-order] [--no-extract]
```

Imports manually downloaded archives (the free-account flow) for pending
mods. `result`: ImportResult.

### track-sync

```
nexus-dl track-sync enable MODS_DIR
nexus-dl track-sync disable MODS_DIR
nexus-dl track-sync push MODS_DIR
```

Manages Nexus-side tracked-mod sync for the collection. `enable` turns it on
and runs an initial sync; `disable` turns it off and leaves existing tracked
mods unchanged; `push` is a one-shot sync regardless of the flag.

`result` (`enable`, `push`):

| Field | Type | Meaning |
|---|---|---|
| `tracked` | int | Mods tracked on Nexus after the run |
| `untracked` | int | Mods untracked on Nexus after the run |

`result` (`disable`):

| Field | Type | Meaning |
|---|---|---|
| `enabled` | `false` | Marker that track sync is off |

### remove

```
nexus-dl remove MODS_DIR (--file-id N | --mod-id N) [--no-load-order] [--dry-run]
```

| Argument / flag | Meaning |
|---|---|
| `MODS_DIR` | Collection directory |
| `--file-id N` | Remove by Nexus file id (required, exactly one of the two ids) |
| `--mod-id N` | Remove by Nexus mod id (required, exactly one of the two ids) |
| `--no-load-order` | Skip load order regeneration |
| `--dry-run` | Show what would be removed, change nothing |

Removes the whole mod from collection state: `--file-id N` resolves to its mod
page, and every state entry of that mod goes. It never deletes files on disk.
A tombstone keeps the mod out of future `sync`/`update` runs (for positive
Nexus mod ids); explicitly re-adding the mod clears it. Unknown ids raise
`state_error` before anything changes; passing zero or both ids is
`invalid_argument`.

`result` (normal run): RemoveModResult.

`result` (`--dry-run`):

| Field | Type | Meaning |
|---|---|---|
| `dry_run` | `true` | Marker |
| `mods` | list of ModSummary | The tracked mod entries that would be removed |

### collections

```
nexus-dl collections [ROOT_DIR]
```

Lists collections under `ROOT_DIR` (default: current directory). Read-only
and offline: reflects local state only. Unreadable collections are skipped.

`result`:

| Field | Type | Meaning |
|---|---|---|
| `root` | str | The root directory scanned |
| `collections` | list of CollectionSummary | Collections found, sorted by name |

### games

```
nexus-dl games [ROOT_DIR]
```

Rolls the same scan up by game domain. Read-only and offline.

`result`:

| Field | Type | Meaning |
|---|---|---|
| `root` | str | The root directory scanned |
| `games` | list of GameSummary | Per-game totals, sorted by game domain |

### mods

```
nexus-dl mods MODS_DIR
```

Lists tracked mod files from local state only (read-only, no network). This
is where `remove`'s `--file-id` / `--mod-id` values come from.

`result`:

| Field | Type | Meaning |
|---|---|---|
| `mods` | list of ModSummary | Tracked mod files, sorted by name |

### health

```
nexus-dl health [ROOT_DIR]
```

Runs local health checks: API key source (never the value), nxm handler
state, desktop notification capability, unreadable state files, and lock
state. Read-only and offline. This is a report, not a gate: it exits 0 even
when a check reports WARN or ERROR, so read the per-check `status` fields
rather than the exit code.

`result`:

| Field | Type | Meaning |
|---|---|---|
| `root` | str | The root directory scanned |
| `checks` | list of HealthCheck | One entry per check |

### shell, nxm, handler (no JSON)

These do not accept `--json`. Signatures for completeness:

```
nexus-dl shell [ROOT_DIR]                 # interactive REPL
nexus-dl nxm NXM_URI [--mods-dir D] [--output D] [--no-extract] [--no-load-order] [--notify/--no-notify]
nexus-dl handler map GAME COLLECTION_DIR
nexus-dl handler unmap GAME
nexus-dl handler set-api-key
nexus-dl handler remove-api-key
nexus-dl handler status
nexus-dl handler install
nexus-dl handler uninstall
```

`nxm` handles a single `nxm://` "Mod manager download" link; link credentials
are used for the request only and are never stored or echoed. `handler`
installs/manages the desktop handler and the stored API key.

## Result object fields

Serialized as plain JSON objects (paths become strings).

### ModStatus

Items of `StatusResult.mods`.

| Field | Type | Meaning |
|---|---|---|
| `mod_id` | int | Nexus mod page id |
| `name` | str | Mod name |
| `version` | str | Installed version (may be empty) |
| `file_id` | int | Nexus file id |
| `optional` | bool | Collection marks it optional |
| `manual` | bool | Added manually (not a collection member) |
| `phase` | int | Load phase (999 for manual mods) |
| `status` | str | One of `up_to_date`, `update_available`, `not_installed`, `removed`, `installed` (offline fallback), `manual`, `pending_download` |

### StatusResult

| Field | Type | Meaning |
|---|---|---|
| `collection_name` | str | From state |
| `collection_url` | str | Stored collection URL |
| `game_domain` | str | Nexus game domain (e.g. `starfield`) |
| `installed_revision` | int | Collection revision installed |
| `latest_revision` | int or null | Latest revision; null when the API is unreachable |
| `mods` | list of ModStatus | Per-mod comparison |
| `deployed_at` | str or null | Timestamp of the last deploy |
| `deployed_file_count` | int | Files currently deployed |
| `game_dir` | str | Game directory used by deploy |
| `track_sync_enabled` | bool | Track-sync flag |
| `removed_skipped` | int | Distinct mod ids skipped as tombstoned (0 when the API was unreachable) |

### PendingDownload

| Field | Type | Meaning |
|---|---|---|
| `mod_id` | int | Nexus mod page id |
| `mod_name` | str | Mod name |
| `file_id` | int | Nexus file id |
| `filename` | str | File name to download by hand |
| `size_bytes` | int | Expected size |
| `browser_url` | str | Manual download link (free-account flow) |

### SyncResult

| Field | Type | Meaning |
|---|---|---|
| `mods_downloaded` | int | Mods downloaded |
| `mods_extracted` | int | Archives extracted |
| `errors` | list of str | Per-mod errors (the run still completes) |
| `load_order_files` | list of str | Load order files written |
| `tracked` | int | Tracked on Nexus (track-sync) |
| `untracked` | int | Untracked on Nexus (track-sync) |
| `pending_downloads` | list of PendingDownload | Free-account mods to fetch by hand |
| `collection_dir` | str or null | The collection directory used (the created subfolder) |
| `conflicts` | list of FileConflict | Same-path writes (last mod wins) |
| `removed_skipped` | int | Distinct tombstoned mod ids skipped |

### UpdateResult

| Field | Type | Meaning |
|---|---|---|
| `to_install` | list of object | Manifest mod dicts the run would/does install (keys include `mod_id`, `mod_name`, `file_id`, optionally `version`, `optional`) |
| `to_update` | list of object | Same shape, mods with a newer version |
| `up_to_date` | list of object | Same shape, mods already current |
| `to_remove` | list of int | File ids no longer in the collection |
| `downloaded` | int | Mods downloaded by this run |
| `errors` | list of str | Per-mod errors |
| `new_revision` | int | Collection revision after the run |
| `load_order_files` | list of str | Load order files written |
| `tracked` | int | Tracked on Nexus (track-sync) |
| `untracked` | int | Untracked on Nexus (track-sync) |
| `pending_downloads` | list of PendingDownload | Free-account mods to fetch by hand |
| `removed_skipped` | int | Distinct tombstoned mod ids skipped |

### DeployResult

| Field | Type | Meaning |
|---|---|---|
| `deployed_count` | int | Files deployed |
| `conflicts` | list of str | Destination paths written by more than one mod |
| `errors` | list of str | Per-file errors |
| `game_dir` | str | Game directory written to |
| `has_sfse` | bool | Collection includes SFSE (script extender) |
| `skipped` | int | Entries skipped by the classifier |
| `warnings` | list of str | Non-fatal warnings (pre-deploy checks and reports) |

### AddResult

| Field | Type | Meaning |
|---|---|---|
| `mod_name` | str | Mod name |
| `mod_id` | int | Nexus mod page id |
| `file_name` | str | File downloaded or selected |
| `success` | bool | False when the add did not complete |
| `error` | str | Error text when `success` is false (empty otherwise) |
| `pending_download` | PendingDownload or null | Set for free-account pending mods |
| `warnings` | list of str | Non-fatal warnings (e.g. FOMOD option choices passed for a mod with no FOMOD installer) |

### ImportResult

| Field | Type | Meaning |
|---|---|---|
| `matched` | int | Archives matched to pending mods |
| `extracted` | int | Archives extracted |
| `still_pending` | int | Mods still awaiting a file |
| `errors` | list of str | Per-file errors |
| `load_order_files` | list of str | Load order files written |
| `warnings` | list of str | Non-fatal warnings (e.g. FOMOD option choices passed for a mod with no FOMOD installer) |

### RemoveModResult

| Field | Type | Meaning |
|---|---|---|
| `removed_file_ids` | list of int | File entries removed from state |
| `removed_mod_ids` | list of int | The mod page id removed |
| `warnings` | list of str | Non-fatal follow-up failures (e.g. load order regeneration after the removal was saved) |

### CollectionSummary

| Field | Type | Meaning |
|---|---|---|
| `name` | str | Collection name (or directory name when state has none) |
| `game_domain` | str | Nexus game domain |
| `mod_count` | int | Tracked mods |
| `pending_count` | int | Mods pending download |
| `path` | str | Collection directory |

### GameSummary

| Field | Type | Meaning |
|---|---|---|
| `game_domain` | str | Nexus game domain |
| `collection_count` | int | Collections for the game |
| `mod_count` | int | Total tracked mods across them |

### ModSummary

| Field | Type | Meaning |
|---|---|---|
| `file_id` | int | Nexus file id (the `remove --file-id` value) |
| `mod_id` | int | Nexus mod page id (the `remove --mod-id` value) |
| `name` | str | Mod name |
| `version` | str | Version |
| `filename` | str | File name |
| `download_status` | str | `downloaded`, `pending_download` (nothing usable on disk), or `pending_choices` (the FOMOD archive is on disk and needs `import --fomod-option` before it can be extracted) |

### HealthCheck

| Field | Type | Meaning |
|---|---|---|
| `label` | str | Check name |
| `status` | str | `OK`, `WARN`, or `ERROR` (ERROR means the check itself failed) |
| `detail` | str | What was observed |
| `remedy` | str or null | Suggested fix when one applies |

### Dataclasses not exposed in --json output

Read from the service layer but never part of a `--json` envelope:

- **ModFileInfo** (`file_id`, `name`, `version`, `category`, `size_bytes`):
  available files for a mod, used when `add` cannot auto-select a file.
- **DeployWarning** (`code`, `message`, `severity`): internal pre-deploy
  checks; only their `message` strings appear, in `DeployResult.warnings`.
- **NxmResult**: the outcome of one `nxm://` link; `nxm` has no `--json`.
