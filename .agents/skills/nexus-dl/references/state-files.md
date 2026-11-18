# State, config, and log files

Everything nexus-dl persists on disk, what it is, and what an agent may
safely do with it. Rule of thumb: these files are tool-managed. Read them for
diagnosis; change them only through `nexus-dl` commands; never print secrets.

## Per-collection files (inside the collection directory)

The collection directory is the folder holding `.nexus-state.json` (created
by `sync` as a named subfolder of the directory you give it).

### `<mods_dir>/.nexus-state.json`

The collection state: collection name/URL/game domain/revision, one entry per
tracked mod file (ids, versions, download status, phase, manual flag), the
`removed_mods` tombstone map that keeps removed mods out of future
`sync`/`update` runs, the track-sync flag, the cached collection manifest,
and saved deploy settings. Written atomically (temp file plus rename).

- **Read?** Yes. Useful for diagnosis (`mods`, `status`, and the listings all
  read it).
- **Modify or delete?** No. Hand edits can break invariants (a wrong-shaped
  key is refused at load; a dropped tombstone resurrects a removed mod), and
  deleting it loses all tracking, including removals. Use the commands
  (`add`, `remove`, `update`, ...) to change state.

### `<mods_dir>/.nexus-state.lock`

The writer lock. Mutating operations take it (a kernel flock) for the
duration; a second concurrent mutation fails with `state_busy`. The file
itself is never removed: the flock is released when the holding process
exits, so a lingering lock file is normal and cannot be stale.

- **Read?** Checking that it exists is fine (`nexus-dl --json health` reports
  lock state).
- **Modify or delete?** No. Never delete or edit it. If a mutation reports
  `state_busy`, wait for the other operation and retry.

### `<mods_dir>/.nexus-journal.jsonl`

The deploy/undeploy journal: one line per mutation, written before and after
it happens, so an interrupted deploy or undeploy resumes cleanly on the next
run instead of guessing.

- **Read?** Yes (diagnostics of an interrupted deploy).
- **Modify or delete?** No, not while a deployment is unfinished or deployed
  files remain. Leave it alone in normal use.

### `<mods_dir>/.nexus-backups/`

Byte-for-byte backups (permissions and symlink targets included) of files
that existed at deploy destinations before nexus-dl replaced them. Blobs are
content-addressed by SHA256 name. `undeploy` restores the originals from
here.

- **Read?** Listing is fine.
- **Modify or delete?** Never delete this directory or its contents while
  anything is deployed: doing so permanently loses the original game files
  that `deploy` replaced. `undeploy` needs it.

## Per-user files

### `~/.config/nexus-dl/api-key`

The stored Nexus Mods API key, written 0600 by `nexus-dl handler set-api-key`
(hidden input, never argv, never echoed). Key resolution order for every
command: `--api-key` flag, then the `NEXUS_API_KEY` environment variable,
then this file. Browser-launched `nxm://` runs have no shell environment, so
this is what authenticates them.

- **Read?** **No.** Never read, print, quote, or log its contents. It is a
  credential. Reference it by name and path only.
- **Modify or delete?** Only through `nexus-dl handler set-api-key` /
  `nexus-dl handler remove-api-key`. Do not hand-edit.

### `~/.config/nexus-dl/handler.json`

nxm-handler configuration: game domain to collection directory mappings
(from `nexus-dl handler map`) and the saved state needed to restore the
previous default handler on `handler uninstall`. Paths and flags, no
secrets.

- **Read?** Yes (to see mappings).
- **Modify or delete?** Only through the `nexus-dl handler` commands.

### `~/.local/state/nexus-dl/nxm.log`

One redacted status line per `nxm://` click: UTC timestamp, outcome
(success/warn/error), mod/file ids or a static reason code. Never carries
URIs, query values, or credentials. Append-only, written best-effort (a
broken log never fails a download), 0600.

- **Read?** Yes. This is the fallback feedback channel for headless clicks.
- **Modify or delete?** Do not edit it. Deleting it only loses click history
  and is otherwise harmless.

### `<ROOT>/.nexus-shell.json`

The interactive shell's remembered "current collection" for a root: one
non-secret value, the collection's directory name. Written only on an
explicit `/use`; startup restores it when still valid and ignores anything
stale or corrupt.

- **Read?** Yes (trivial).
- **Modify or delete?** Editing is pointless. Deleting is safe: the shell
  just starts with no selection.

## Environment variables

| Variable | Effect |
|---|---|
| `NEXUS_API_KEY` | API key for this run, when no `--api-key` flag is given and (at resolution time) above the stored key |
| `XDG_CONFIG_HOME` | Relocates the config directory: `api-key` and `handler.json` live in `$XDG_CONFIG_HOME/nexus-dl/` instead of `~/.config/nexus-dl/` |
| `XDG_DATA_HOME` | Relocates the user applications directory, where `nexus-dl handler install` puts its `.desktop` entry (`$XDG_DATA_HOME/applications/` instead of `~/.local/share/applications/`) |
| `XDG_STATE_HOME` | Relocates the state directory: `nxm.log` lives in `$XDG_STATE_HOME/nexus-dl/` instead of `~/.local/state/nexus-dl/` |
