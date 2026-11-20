# Error codes

Every `--json` failure envelope carries a stable `code` alongside the real
exception class name in `type` and the human message in `message` (plus an
optional `hint` with a specific remedy). Triage by `code`; fall back to
`message` for detail, and treat an unknown `code` as a generic failure.

One code is not an exception mapping: `operation_failed` marks a command that
ran but whose own result reports failure (see below). Wrong invocations are
not in the envelope at all: they exit 2 with plain text on stderr.

| code | What causes it | What to do |
|---|---|---|
| `state_error` | Collection state is missing or unusable: `<dir>` has no `.nexus-state.json`, the state file is present but broken (including a wrong-shaped `removed_mods`/`mods` key), an id passed to `remove` does not exist, or a state save failed | Confirm `<dir>` is the collection subfolder `sync` created (it holds `.nexus-state.json`); find collections with `nexus-dl --json collections <parent>`. For "run sync first" cases, `sync`. For a save failure, the prior state file is unchanged; re-run |
| `state_busy` | Another nexus-dl operation holds this collection's writer lock (a second concurrent `sync`/`update`/`add`/`remove`/`deploy` and friends) | Wait for the other operation to finish, then retry. Never delete `.nexus-state.lock`: the lock is a kernel flock released when its process exits, so the file lingering is normal |
| `load_order_regen_error` | A state change succeeded but regenerating the load order afterwards failed (cached manifest unparseable, LOOT or write failure) | The state change is saved and stands. Re-run `nexus-dl load-order <dir>`; if it keeps failing, the `message` names the underlying cause |
| `nexus_api_error` | The Nexus API refused or failed the request: missing or invalid API key, transport failure (translated, redacted), or an API-side error | Check the key source with `nexus-dl --json health` (it names flag/env/stored/none, never the value). Retry once; if it persists, report the `message` (it is already redacted) |
| `nexus_premium_required` | The account is free and the operation needs Premium (direct file downloads) | Use the free-account flow: `sync` (it lists manual download links in `pending_downloads`), fetch the files by hand into `<dir>`, then `nexus-dl --json import <dir>` |
| `nexus_rate_limited` | The API is rate limiting the account | Wait and retry. Batch work into fewer calls (`update` instead of many `add` runs) to stay under the limit |
| `download_error` | A mod file failed to download (network, CDN, or the free-account countdown page) | Re-run the command: downloads are resumable and completed mods are skipped. On a free account, fetch the file by hand and `import` |
| `deploy_error` | A deploy was refused or failed: a destination is reached through a symlinked parent (refused, fail closed), a destination entry is something the deploy will not overwrite, or a write failed | Read the `message` for the path. Fix the named path (or deploy to a different `--game-dir`) and retry. `deploy --dry-run` first to see the plan |
| `undeploy_error` | Undeploy could not safely remove everything, typically because a deployed file was modified outside nexus-dl after deployment (removal is refused rather than destroying the edit) | Inspect the named paths. Restore or move the edited files yourself, or keep them and know that undeploy left them in place |
| `extraction_error` | An archive failed to extract (corrupt download, unsupported format, missing `unrar` for RAR) | Re-run to re-download the archive; install the `unrar` system package for RAR collections; as a workaround, `--no-extract` keeps archives intact |
| `nxm_parse_error` | An `nxm://` link could not be parsed | Get a fresh link from the site's "Mod manager download" button; do not edit links by hand |
| `nxm_scheme_error` | `nexus-dl nxm` was given a non-nxm input (e.g. an `https://` mod page URL) | Use `nexus-dl add <mod page URL> <dir>` for page URLs; `nxm` only takes `nxm://` links |
| `nxm_collection_link_error` | The `nxm://` link is collection-shaped, which this tool does not download via `nxm` | Use `nexus-dl sync <collection URL> <parent-dir>` for collections |
| `nxm_routing_error` | The link could not be routed to a collection directory (no usable game mapping, empty game domain in state, ambiguous candidates) | Map the game to the collection subfolder with `nexus-dl handler map <game> <collection dir>`, or pass `--mods-dir <dir>` explicitly |
| `manifest_error` | The collection manifest could not be downloaded or parsed | Retry; if it persists, the collection's manifest may be broken on Nexus's side. `update`/`sync` report this per run |
| `load_order_error` | Load order generation failed (LOOT sorting or writing the output files) | Retry `nexus-dl load-order <dir>`. Without LOOT the collection author's order is used; check `nexus-dl --json health` for tool issues |
| `collection_parse_error` | A collection URL or collection metadata could not be parsed | Check the URL: it must be a Nexus Mods collection link for a supported game |
| `mod_parse_error` | A mod page URL or mod metadata could not be parsed | Check the URL: it must be a Nexus Mods mod page link (`.../mods/<id>`) |
| `key_store_error` | The stored API key at `~/.config/nexus-dl/api-key` could not be read, written, or removed (permissions or disk trouble). Messages carry paths and OS detail only, never key values | Check the file's permissions (it is written 0600) and the directory. Re-store with `nexus-dl handler set-api-key`, or clear with `nexus-dl handler remove-api-key` |
| `handler_error` | Desktop handler configuration or integration failed (`handler install`/`uninstall`/`map`/`status`, xdg-mime problems) | Read the `message`; `nexus-dl handler status` reports current state. These commands are not `--json`-able; run them for their human output |
| `desktop_entry_error` | The `.desktop` entry itself could not be installed, read, or rolled back (a handler-install subclass) | Inspect `~/.local/share/applications/` and re-run `nexus-dl handler install`; `handler uninstall` restores the previous default |
| `invalid_argument` | Domain-level validation failed, e.g. `remove` given zero or both of `--file-id`/`--mod-id` | Fix the invocation. The `hint` (when present) names the lookup command to use |
| `not_found` | A required path was not found (e.g. no game directory could be located for `deploy`) | Pass `--game-dir` explicitly, or check the Steam install |
| `operation_failed` | The command ran but its own result reports failure: `add` completed with `success: false` (and similar result-reported failures). The envelope keeps the `result` object alongside the error | Read `result` (for `add`: `error` text, `pending_download` for the free-account path) and act on that; do not re-run blindly |

## Result error text (not error codes)

A result's `errors`/`warnings`/`error` fields are a diagnostic record, not
the `error.code` above. They carry the deliberate part: which path, which
refusal reason. The exception suffix beside it is a short category, never
raw OS or unexpected-exception text, because those fields get copied into
whatever output a caller builds (`--json` envelopes, logs, harness
transcripts) and arbitrary exception text should not travel with them.

- `OSError` becomes the fixed OS phrase plus the symbolic errno name, for
  example `no such file or directory (ENOENT)`. No filename, no
  `[Errno N]` framing.
- An exception raised outside this package becomes
  `unexpected error (<TypeName>)`.
- An exception raised inside this package keeps its authored message,
  so the refusal reasons you see (`not a regular file or symlink`,
  `changed outside`) are exactly as the code wrote them.

The `--json` envelope's `error.message` is a different thing: it is the
documented diagnostic surface and may carry redacted exception detail.
Read `error.code` to branch on; read `message` to explain to a human.
