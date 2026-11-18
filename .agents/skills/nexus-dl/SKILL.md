---
name: nexus-dl
description: >
  Operate nexus-collection-dl (nexus-dl) from an AI harness: sync Nexus Mods
  collections, check status, update, add/remove mods, deploy/undeploy, and
  consume --json machine-readable output. Use when the user says "nexus-dl",
  "sync this collection", "nexus collection", "update my mods", "deploy mods",
  "remove a mod", "nexus-dl --json", invokes /nexus-dl, or gives a
  nexusmods.com collection/mod URL to download. Not for browsing the Nexus
  website, account billing, or authoring mods.
version: 0.1.0
license: MIT
platforms: [linux, macos]
metadata:
  args: "<command> [args]"
---

# nexus-dl: Nexus Mods collections from an AI harness

This skill teaches an agent to drive the `nexus-dl` CLI: download and update
Nexus Mods collections, manage the mod set, deploy into a game directory, and
parse the `--json` machine-readable output. It is about operating the tool,
not about the Nexus website.

## 1. When to use this skill

Use it whenever the user wants nexus-collection-dl to do something: sync or
update a collection, inspect what is installed, add or remove a mod, deploy or
undeploy files into a game install, or run the local health checks. Typical
signals: "nexus-dl", "nexus collection", "sync this collection", "update my
mods", "deploy mods", "remove a mod", "nexus-dl --json", `/nexus-dl`, or a
nexusmods.com collection/mod URL to download.

Do not use it for browsing the Nexus website, account or billing questions, or
authoring mods.

## 2. Command routing table

`<dir>` below is a *collection directory*: the folder holding
`.nexus-state.json`. `sync` creates one as a named subfolder of the directory
you give it (so `sync <url> ~/mods/starfield` produces
`~/mods/starfield/<Collection Name>/`, and that subfolder is `<dir>` for every
other command). Never pass the parent to `status`/`update`/`deploy`/`remove`.

| User intent | Command |
|---|---|
| "what's installed?" / "what's the status?" | `nexus-dl --json status <dir>` |
| "anything to update?" / "what would change?" | `nexus-dl --json update <dir> --dry-run` |
| "make it match the collection" | `nexus-dl --json update <dir>` (existing collection vs its latest revision). Use `nexus-dl --json sync <collection-url> <parent-dir>` to (re)download a whole collection from its URL (resumable) |
| "get rid of mod X" | `nexus-dl --json mods <dir>` to find the `file_id`, then `nexus-dl --json remove <dir> --file-id N` (confirm with the user first) |
| "install into the game" | `nexus-dl --json deploy <dir> --dry-run` first, then `nexus-dl --json deploy <dir>` |
| "put the game back" / "uninstall the deployed files" | `nexus-dl --json undeploy <dir>` |
| "what collections do I have?" | `nexus-dl --json collections [ROOT_DIR]` (ROOT_DIR defaults to the current directory) |
| "what games do I have collections for?" | `nexus-dl --json games [ROOT_DIR]` |
| "is my setup healthy?" | `nexus-dl --json health [ROOT_DIR]` |
| "add this mod" | `nexus-dl --json add <mod-page-url> <dir>` (add `--file-id N` to pick a specific file) |
| "the load order is stale" | `nexus-dl --json load-order <dir>` |
| "I downloaded the files by hand (free account)" | save the archives into `<dir>`, then `nexus-dl --json import <dir>` |

`serve`, `shell`, `nxm`, and `handler` exist but do not accept `--json`, and
`shell` is interactive. An agent should not use them; see
[references/commands.md](references/commands.md) for their signatures.

## 3. Safe defaults

These rules are the most important part of this skill.

1. **Read before you write.** Before any command that changes state
   (`sync`, `update`, `add`, `add-local`, `import`, `remove`, `track-sync`,
   `deploy`, `undeploy`), run a read: `nexus-dl --json status <dir>` or
   `nexus-dl --json update <dir> --dry-run`. Report what you found, then act.
2. **Always `deploy --dry-run` before `deploy`.** Deployment writes into the
   game directory and replaces files there. The dry run lists what would be
   written and changes nothing. Do not deploy without showing the user the
   preview first.
3. **`remove` is state-only, but confirm with the user first.** It never
   deletes files on disk, and re-adding the mod restores it, but it drops the
   mod from the collection and keeps it out through future `sync`/`update`.
   Always `remove <dir> --file-id N --dry-run` (or show the `mods` row) and
   get a yes before running the real removal.
4. **Never invent ids.** Look up `file_id` and `mod_id` values with
   `nexus-dl --json mods <dir>`. Guessing an id either fails cleanly or
   removes the wrong mod.
5. **`--json` goes before the subcommand.** `nexus-dl --json status <dir>`,
   not `nexus-dl status --json <dir>`. The flag belongs to the `nexus-dl`
   group; after the subcommand it is a usage error.
6. **`remove` drops the whole mod**, not just one file: `--file-id N` is
   resolved to its mod page and every state entry of that mod is removed.
   Preview with `--dry-run` before doing it.
7. **Do not hand-edit state files** (`.nexus-state.json` and friends) and do
   not delete `.nexus-backups/`. See
   [references/state-files.md](references/state-files.md).

## 4. Read-then-write workflow

One worked example, start to finish. The user says: *"Bring my Starfield
collection up to date, take 'Better Textures' out of it, and install the
result into the game."*

`<dir>` is a synced collection directory (it holds `.nexus-state.json`), say
`~/mods/starfield/My Collection`. Work in `--json` mode throughout and parse
the envelope.

**Step 1: see what is there (read).**

```bash
nexus-dl --json status ~/mods/starfield/My\ Collection
```

Check `result.collection_name`, `result.installed_revision` vs
`result.latest_revision`, and the `result.mods` statuses.

**Step 2: preview the update (read).**

```bash
nexus-dl --json update ~/mods/starfield/My\ Collection --dry-run
```

The result carries `dry_run: true`, the full `status`, and `new` / `updates` /
`removed` name lists. Show the user what would change and get the go-ahead.

**Step 3: apply the update (write).**

```bash
nexus-dl --json update ~/mods/starfield/My\ Collection
```

Check `result.downloaded`, `result.to_update`, `result.errors` (an empty list
means a clean run), and `result.new_revision`.

**Step 4: find the mod to remove (read).**

```bash
nexus-dl --json mods ~/mods/starfield/My\ Collection
```

Find the `Better Textures` row and note its `file_id`. Show the user the name,
version, and id, and ask for confirmation to remove it.

**Step 5: preview the removal (read).**

```bash
nexus-dl --json remove ~/mods/starfield/My\ Collection --file-id 12345 --dry-run
```

The result lists the mod entries that would go (`mods`). If it is not the
intended mod, stop and re-check the id.

**Step 6: remove it (write).**

```bash
nexus-dl --json remove ~/mods/starfield/My\ Collection --file-id 12345
```

`result.removed_file_ids` / `result.removed_mod_ids` report what left the
state file. `result.warnings` is non-empty when the removal was saved but the
load order could not be regenerated; the message names the retry command.

**Step 7: preview the deploy (read), then deploy (write).**

```bash
nexus-dl --json deploy ~/mods/starfield/My\ Collection --dry-run
# show the user the counts, get confirmation
nexus-dl --json deploy ~/mods/starfield/My\ Collection
```

The dry run reports `game_root_files`, `data_files`, `skipped`, and
`total_files`. The real run's `result.errors`, `result.conflicts`, and
`result.warnings` deserve a mention in the final report even when the deploy
succeeded.

**Step 8: report.** Summarize what changed (revision, mods added/updated,
mod removed, files deployed) in plain language. Do not paste raw JSON at the
user unless they ask.

## 5. JSON mode

Pass `--json` **before** the subcommand on any non-interactive command. stdout
then carries exactly one JSON object (pretty-printed) and nothing else; human
diagnostics and progress output go to stderr, so
`nexus-dl --json status <dir> | jq .` is always safe.

Success envelope:

```json
{
  "ok": true,
  "result": { },
  "error": null
}
```

Failure envelope:

```json
{
  "ok": false,
  "result": null,
  "error": {
    "code": "state_error",
    "type": "StateError",
    "message": "No state file found in /mods",
    "hint": "Run 'sync' first to download a collection."
  }
}
```

- `ok` is true exactly when the process exits 0. Treat a non-zero exit as a
  failure even if you somehow got a partial object.
- `error.hint` is present only when the command has a specific remedy to
  suggest.
- A partial failure keeps its `result`: when `add` completes with
  `success: false`, the envelope has `ok: false`,
  `error.code: "operation_failed"`, and the `AddResult` object in `result`, so
  you get both the outcome and the detail. Exit code is 1.
- **Wrong invocation is not JSON.** Misusing the CLI (unknown flag, missing
  argument, `--json` after the subcommand, `--json` on `serve`/`shell`/`nxm`/
  `handler`) is a usage error: plain text on stderr, exit code 2. Read stderr;
  exit 2 means you called it wrong, not that the operation failed.
- `result` is always an object, never a bare list.

`error.code` values: `state_error`, `state_busy`, `load_order_regen_error`,
`nexus_api_error`, `nexus_premium_required`, `nexus_rate_limited`,
`download_error`, `deploy_error`, `undeploy_error`, `extraction_error`,
`nxm_parse_error`, `manifest_error`, `load_order_error`,
`collection_parse_error`, `mod_parse_error`, `key_store_error`,
`handler_error`, `invalid_argument`, `operation_failed`. A few more specific
codes exist for nxm/handler flows. `error.type` always carries the real
exception class name; treat an unknown `code` as a generic failure and use
`message`. Full triage: [references/errors.md](references/errors.md).

Per-command `result` shapes: [references/commands.md](references/commands.md).

## 6. State files

Each collection directory holds `.nexus-state.json` (the tracked mod set,
tombstones, cached manifest), `.nexus-state.lock` (a live-operation lock),
and, after deployments, `.nexus-journal.jsonl` and `.nexus-backups/`
(deploy recovery; the backups hold the game files `deploy` replaced).
Per-user config lives in `~/.config/nexus-dl/` (API key, nxm handler config)
and `~/.local/state/nexus-dl/nxm.log`. Treat all of these as tool-managed:
read them for diagnosis, change them only through `nexus-dl` commands, and
never print or edit the API key. Details and the `NEXUS_API_KEY` / `XDG_*`
environment variables: [references/state-files.md](references/state-files.md).

## 7. Error triage (top codes)

| code | Likely cause | What to do |
|---|---|---|
| `state_error` | `<dir>` is not a collection directory, has no `.nexus-state.json`, or holds an id that does not exist | Confirm the path (it must be the subfolder `sync` created), run `nexus-dl --json collections <parent>` to find the right one, or `sync` first |
| `state_busy` | Another nexus-dl operation holds the collection's write lock | Wait for it to finish and retry; do not delete the lock file |
| `nexus_api_error` | Missing/invalid API key, network failure, or the API refused the request | Check `nexus-dl --json health` for the key source (never the value) and the `message`; retry once, then report |
| `nexus_premium_required` | The account is free and the operation needs Premium (direct downloads) | Use the free-account flow: `sync` lists manual download links, fetch the files yourself, `import` them |
| `download_error` | A mod file failed to download | Re-run the command (downloads are resumable); if it persists, the file may be hidden behind the free-account countdown timer |

Everything else (rate limits, extraction, deploy, load order, handler codes):
[references/errors.md](references/errors.md).

## 8. References

| File | Contents |
|---|---|
| [references/commands.md](references/commands.md) | Every command: signature, arguments, flags, and the exact JSON `result` fields |
| [references/errors.md](references/errors.md) | Error `code` -> cause -> remedy table |
| [references/state-files.md](references/state-files.md) | State/config file paths, what each is, and what an agent may safely do with it |
