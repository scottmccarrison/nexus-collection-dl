# 0.5.0 Release Test Plan

## Scope

12 commits, 35 files, 4368 insertions since v0.4.0. The headline is the
front door (`setup` plus bare `nexus-dl` opening the shell). Everything
else is correctness and shell UX. The existing test suite is the floor,
not the ceiling: this plan is the part a test cannot see.

**Every live item has a pass criterion.** Check it off or it did not
happen. Items marked `STOP` mean the release does not ship until they
pass.

---

## Phase 0: Automated gate

Must be green before any live work.

- [ ] 0.1 `python -m pytest tests/ -q` green, 0 failures
- [ ] 0.2 `ruff check .` clean
- [ ] 0.3 `mypy nexus_collection_dl` clean
- [ ] 0.4 CI green on 3.10, 3.11, 3.12, 3.13
- [ ] 0.5 Fresh venv install from the branch; `nexus-dl --version` reports `0.5.0` `STOP`
- [ ] 0.6 `nexus-dl --help` lists every command: `setup`, `enable`,
      `disable`, `gc`, `serve`, `sync`, `deploy`, `add`, `remove`,
      `undeploy`, `mods`, `health`, `shell`, `nxm`, `handler`,
      `track-sync`

---

## Phase 1: Regression risk

The places changed most. A mistake here is silent and expensive.

- [ ] 1.1 **State save/load.** Load a 0.4.0 state file, save, reload.
      All mods present, no data lost, `enabled` defaults true. `STOP`
- [ ] 1.2 **Extraction marker rule.** Extract a FOMOD with
      `fomod/ModuleConfig.xml`. Plan built, options filtered.
- [ ] 1.3 **Marker rule (negative).** Extract a mod with
      `SomeMod/ModuleConfig.xml` and no `fomod` directory. Falls back to
      the folder heuristic, no crash. This layout is deliberately not a
      marker any more.
- [ ] 1.4 **Publish marker.** Interrupt a deploy mid-publish.
      `.nexus-publish-pending` is left and staging is kept. `STOP`
- [ ] 1.5 **Publish recovery.** Re-run extraction after 1.4. Files
      complete, marker gone.
- [ ] 1.6 **Deploy attribution.** After a sync, each mod's state entry
      carries its `contributed_files` list.
- [ ] 1.7 **GameState wiring.** Deploy a FOMOD with a `fileDependency`
      condition. The right branch is picked, no folder-fallback warning.
      `STOP`
- [ ] 1.8 **GameState unknown.** Deploy with no `plugins.txt`. Falls
      back with the warning, same as before. Unknown is not empty.
- [ ] 1.9 **Case fixup.** Deploy `Foo.esp` where the manifest says
      `foo.esp`. It lands as `foo.esp` and the rename is reported.
      `STOP`
- [ ] 1.10 **Size caps.** An oversized installer config and a
      decompression bomb are both refused with a message naming the
      limit.

---

## Phase 2: The front door

The 0.5.0 product bet. Most likely to be wrong in ways tests cannot see.

- [ ] 2.1 Point `XDG_CONFIG_HOME` at an empty dir. `nexus-dl health`
      reports "User config: not configured (first run)".
- [ ] 2.2 Bare `nexus-dl` in a terminal. The setup wizard runs, detects
      the Steam root and installed games, asks for the collections root
      with a default, offers each game, offers the nxm handler. `STOP`
- [ ] 2.3 Accept the defaults. `config.json` written with the
      collections root and games. Handler installed or declined.
- [ ] 2.4 Bare `nexus-dl` again. **The shell opens** over the
      collections root. Not usage. Not the wizard. `STOP`
- [ ] 2.5 `echo "" | nexus-dl`. Prints usage. **No hang, no REPL.**
      `STOP`
- [ ] 2.6 `nexus-dl > /tmp/out`. Prints usage. No REPL.
- [ ] 2.7 `nexus-dl --json` bare. Refused with a clear message. No REPL.
- [ ] 2.8 `nexus-dl setup --non-interactive --collections-root ... --game
      domain=path`. No prompts at all. Config written.
- [ ] 2.9 `nexus-dl setup --non-interactive` with no root. Loud error.
      No config written.
- [ ] 2.10 `nexus-dl handler status` after setup. Shows an existing
      executable. `health` agrees.

**2.4 and 2.5 are the two that matter most.** The shell is the
product; a pipe must never get a REPL.

---

## Phase 3: Core journeys

Must not regress.

- [ ] 3.1 `sync` a real collection. All mods downloaded, extracted,
      state written.
- [ ] 3.2 `deploy --dry-run`. Counts match expectations. No files
      written.
- [ ] 3.3 `deploy` to the real game dir. Files land. The progress line
      moves during the write loop.
- [ ] 3.4 Launch the game. Mods load. Modded Breezehome is still there.
      `STOP`
- [ ] 3.5 `mods`. Table shows name, version, ids, status, **and Picks**.
- [ ] 3.6 `undeploy`. Files removed, originals restored.
- [ ] 3.7 `add` a mod URL. Mod added. A dependency warning fires if
      requirements are missing.
- [ ] 3.8 `remove` a mod. Gone from state, tombstone recorded.
- [ ] 3.9 nxm:// click with the shell open. The click registers at the
      prompt and a deploy prompt is offered. `STOP`
- [ ] 3.10 Ctrl-C at the FOMOD picker. The whole walk aborts, the mod
      stays pending, the shell survives.
- [ ] 3.11 Ctrl-C during deploy. Nothing half-installed. A recovery
      marker is left if it was mid-publish.
- [ ] 3.12 `--json` on `status`, `mods`, `deploy`, `gc`. Each parses as
      JSON. No prose mixed in.

---

## Phase 4: New functionality

- [ ] 4.1 **Enable/disable.** `disable` a mod, `deploy`. Its files do
      not land. State entry and picks stay. `STOP`
- [ ] 4.2 `enable` it, `deploy`. Files land again.
- [ ] 4.3 `disable` a mod extracted before 0.5.0. Warning: files cannot
      be isolated, re-sync. Deploys anyway rather than guessing.
- [ ] 4.4 **Case fixup.** A mod shipping `Foo.esp` where the manifest
      says `foo.esp` lands as `foo.esp` and the rename is reported.
- [ ] 4.5 Two mods shipping `Foo.esp` and `foo.esp`. Warning:
      "case-collides".
- [ ] 4.6 **GameState.** Deploy NVAC or another `fileDependency`
      installer. The right branch is picked. No folder-fallback warning.
      `STOP`
- [ ] 4.7 **Archive GC.** `nexus-dl gc`. Dry run lists reclaimable and
      kept with reasons. Nothing deleted.
- [ ] 4.8 `nexus-dl gc --apply`. Archives deleted. `.nexus-backups/`
      untouched.
- [ ] 4.9 **Progress line.** `deploy` a large collection. The line
      moves, in place on a TTY, clean under `--json`.
- [ ] 4.10 **Conflict names.** Force a conflict (two mods, one
      destination). The message names both mods and keeps the paths.
- [ ] 4.11 **Serve stub.** `nexus-dl serve`. Names the replacement.
      Exits non-zero.
- [ ] 4.12 **Redaction escape.** Trigger an error with a secret in it.
      Default output is redacted. Diagnostic mode shows the detail.

---

## Phase 5: Boundary and failure cases

Some of these deliberately disrupt state. Run last.

- [ ] 5.1 Kill `deploy` mid-publish, then re-run. Recovery finishes the
      job. No partial set. `STOP`
- [ ] 5.2 Disk-full during extract, or a simulated bomb. Refused
      cleanly. Staging cleaned up.
- [ ] 5.3 Corrupt `config.json`. Treated as a first run. No crash.
- [ ] 5.4 A state file written by 0.4.0. Loads. Mods present. `enabled`
      defaults true.
- [ ] 5.5 Run 0.4.0 against a 0.5.0 state file. Loads, unknown keys
      preserved. No crash.
- [ ] 5.6 `--json` with an error. `{"ok": false, "error": {...}}` on
      stdout. Prose on stderr.

---

## Not tested, and why

| Not tested | Why |
|---|---|
| Windows | No Windows machine. Tracked in #131. |
| Free-account downloads | Needs a free account. Tracked in #131. |
| `unar` / `unrar` extraction fallbacks | Not installed here. Tracked in #131. |
| Desktops other than Cinnamon | Only this machine. Tracked in #131. |
| `game_version` / `gameDependency` | Deliberately not implemented. Named in the code. |
| Downgrade to 0.4.0 after a 0.5.0 deploy | Needs a real downgrade run. Tracked in #131. |

---

## Order of execution

1. **Phase 0** (automated, minutes). Stop if anything fails.
2. **Phase 2** (the front door) while the config is still clean. Once
   setup has run, 2.1 is no longer reproducible.
3. **Phases 1, 3, 4** against the live collection and game dir.
4. **Phase 5** last, because some of it disrupts state on purpose.

---

## Known open items at release time

Two items from the 0.4.0 "queued for 0.4.1" list are still open and are
**not** blockers:

- **#128** The full installer XML lives in the state file. Perf only;
  bites large collections.
- **#116** Name pre-validation for the system-tool extraction fallbacks.
  A narrow security residual; the realistic half is already covered by
  the staging snapshot guard.

Both are 0.5.1 candidates. The release notes must name them rather than
implying the queued list is fully discharged.
