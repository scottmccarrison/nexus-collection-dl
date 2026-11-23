# 0.5.0 Release Test Plan

## Scope

The headline is the front door (`setup` plus bare `nexus-dl` opening
the shell), followed by correctness and shell UX work, and the
download-manager change (PR #153: extraction keeps the archive, gc
buys the disk back). Everything here landed on `beta` after v0.4.0.
The existing test suite is the floor, not the ceiling: this plan is the
part a test cannot see.

**Every live item has a pass criterion.** Check it off or it did not
happen. Items marked `STOP` mean the release does not ship until they
pass.

Where a step was re-run after a later change, both results are kept: the
first pass and the re-run. A re-run note supersedes the earlier number
when the behaviour changed (for example `Skipped: 4` became
`Skipped: 12` once archives were kept).

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
- [x] 1.6 **Deploy attribution.** After a sync, each mod's state entry
      carries its `contributed_files` list.
      PASSED 2026-11-23: all 8 mods carry lists (86, 3, 4, 4, 3, 1, 1, 560)
      and `enabled` defaults true.
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

**Before anything else, isolate the config.** The tool reads its config
from `$XDG_CONFIG_HOME/nexus-dl/config.json`. Point that at a fresh
empty directory so the tool sees a first run and your real config is
never touched. This has to happen before setup has ever run in the
session.

```bash
mkdir -p /tmp/nexus-050-fresh
export XDG_CONFIG_HOME=/tmp/nexus-050-fresh
nexus-dl health          # must say: User config  WARN  not configured (first run)
```

If `health` reports `OK` with a collections root, the variable did not
take. Stop and fix that before continuing. Undo afterwards with
`unset XDG_CONFIG_HOME`; everything lives under `/tmp/nexus-050-fresh`.

- [x] 2.1 `nexus-dl health` with `XDG_CONFIG_HOME=/tmp/nexus-050-fresh`
      reports "User config: not configured (first run)". `STOP`
      PASSED 2026-11-22 on home-pc, v0.5.0 from the release worktree.
- [x] 2.2 Bare `nexus-dl` in a terminal. The setup wizard runs, detects
      the Steam root and installed games, asks for the collections root
      with a default, offers each game, offers the nxm handler. `STOP`
      PASSED 2026-11-23: wizard ran, found Skyrim SE, asked for root.
- [x] 2.3 Accept the defaults. `/tmp/nexus-050-fresh/nexus-dl/config.json`
      written with the collections root and games. Handler installed or
      declined.
      PASSED 2026-11-23: config has root + Skyrim SE + game_dir; handler
      installed at ~/.local/share/applications/nexus-dl-nxm.desktop.
- [x] 2.4 Bare `nexus-dl` again. **The shell opens** over the
      collections root. Not usage. Not the wizard. `STOP`
      PASSED 2026-11-23: `nexus-dl shell. Type /help for commands` and a
      prompt. /quit exits cleanly.
- [x] 2.5 `echo "" | nexus-dl`. Prints usage. **No hang, no REPL.**
      `STOP`
      PASSED 2026-11-23: full usage block, immediate return to the prompt.
- [x] 2.6 `nexus-dl > /tmp/out`. Prints usage. No REPL.
      PASSED 2026-11-23: usage in the file, immediate return.
- [x] 2.7 `nexus-dl --json` bare. Refused with a clear message. No REPL.
      PASSED 2026-11-23: "--json needs a command: bare invocation is
      interactive and this output is meant to be parsed".
- [x] 2.8 `nexus-dl setup --non-interactive --collections-root ... --game
      domain=path`. No prompts at all. Config written.
      PASSED 2026-11-23: "Configured. Collections root: ..." with zero prompts.
- [x] 2.9 `nexus-dl setup --non-interactive` with no root. Loud error.
      No config written.
      PASSED 2026-11-23: "--non-interactive needs --collections-root: with
      no prompts there is nothing to guess from".
- [x] 2.10 `nexus-dl handler status` after setup. Shows an existing
      executable. `health` agrees.
      PASSED 2026-11-23: handler went from WARN (dead worktree venv) to OK
      "installed and the default nxm:// handler". User config OK.

**2.4 and 2.5 are the two that matter most.** The shell is the
product; a pipe must never get a REPL.

---

## Phase 3: Core journeys

Must not regress.

- [x] 3.1 `sync` a real collection. All mods downloaded, extracted,
      state written.
      PASSED 2026-11-23: 8/8 mods synced and extracted, load-order.txt and
      plugins.txt written, no extraction errors.
      RE-RUN 2026-11-23 after download-manager (PR #153), fresh
      environment at /home/scott/nexus-live-collections: 8/8 again, and
      the 8 downloaded archives are now KEPT (8 extension-less
      containers, 152178901 bytes). 662 payload files, every one
      attributed. This is the new contract: extraction keeps the archive
      so gc can buy the disk back.
- [x] 3.2 `deploy --dry-run`. Counts match expectations. No files
      written.
      PASSED 2026-11-23: 0 root, 662 data, 4 skipped.
      RE-RUN 2026-11-23 after download-manager: 0 root, 662 data,
      Skipped 12 (was 4; the delta is exactly the 8 kept archives, now
      refused as download containers).
- [x] 3.3 `deploy` to the real game dir. Files land. The progress line
      moves during the write loop.
      PASSED 2026-11-23: 662 deployed, 0 conflicts, 0 errors. Progress line
      moved. NOTE: the output is visually messy (line paints, then a phase
      message cuts across it, then the line paints again, and "Deployed
      662 files" prints twice). Feature works; presentation is #151.
      RE-RUN 2026-11-23 after download-manager, to a scratch game dir
      (/home/scott/nexus-live-game) so the working deployment stayed
      untouched: 662 deployed, 0 conflicts, 0 errors. See 4.15 for the
      archive-skip proof. The real game dir was deliberately NOT touched
      this round; 3.4 still stands from the earlier pass and is the
      regression check to re-run before release.
- [x] 3.4 Launch the game. Mods load. Modded Breezehome is still there.
      `STOP`
      PASSED 2026-11-23: game loaded into the modded Breezehome.
      NOT RE-RUN after download-manager. The deploy-skip is proved on
      disk (4.15), but "the game loads the files" is only proved by a
      launch. Re-run before the tag.
- [x] 3.5 `mods`. Table shows name, version, ids, status, **and Picks**.
      PASSED 2026-11-23: Picks column present, all `-` (no FOMOD mods in
      this collection). Populated case still to test.
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
      PARTIAL 2026-11-23: `--json mods` is valid JSON. `status`, `deploy`,
      `gc` still to check.

---

## Phase 4: New functionality

- [x] 4.1 **Enable/disable.** `disable` a mod, `deploy`. Its files do
      not land. State entry and picks stay. `STOP`
      PASSED 2026-11-23: Breezehome (3 files) off -> 659 deployed. State
      entry intact. (Test plan said 661; the code was right at 659.)
- [x] 4.2 `enable` it, `deploy`. Files land again.
      PASSED 2026-11-23: back to 662 deployed, 0 conflicts, 0 errors.
- [ ] 4.3 `disable` a mod extracted before 0.5.0. Warning: files cannot
      be isolated, re-sync. Deploys anyway rather than guessing.
- [ ] 4.4 **Case fixup.** A mod shipping `Foo.esp` where the manifest
      says `foo.esp` lands as `foo.esp` and the rename is reported.
- [ ] 4.5 Two mods shipping `Foo.esp` and `foo.esp`. Warning:
      "case-collides".
- [ ] 4.6 **GameState.** Deploy NVAC or another `fileDependency`
      installer. The right branch is picked. No folder-fallback warning.
      `STOP`
- [x] 4.7 **Archive GC.** `nexus-dl gc`. Dry run lists reclaimable and
      kept with reasons. Nothing deleted.
      PASSED 2026-11-23 (second attempt): `add --no-extract` retained
      `Banners at Whiteruns Gates-1057-1-2.7z`; gc kept it with "no
      extracted payload recorded for this mod" and deleted nothing.
      NOTE: `add --no-extract` printed `Extracting...` although nothing
      was extracted. Misleading progress line, not a functional bug.
      RE-RUN 2026-11-23 after download-manager, fresh environment: gc
      listed all 8 sync-named (extension-less) archives as reclaimable,
      145.1 MB, and deleted nothing. This is the reclaim rule; the
      earlier pass only proved the keep rule. The keep rule still holds
      in the same run: `add --no-extract` was kept with "the archive is
      the only copy of this mod".
- [x] 4.8 `nexus-dl gc --apply`. Archives deleted. `.nexus-backups/`
      untouched.
      PASSED 2026-11-23 after download-manager: freed 145.1 MB, 0
      download containers left outside backups (checked by content
      sniffing, not just suffix), 662/662 payload files intact,
      `.nexus-backups/` untouched.
- [ ] 4.9 **Progress line.** `deploy` a large collection. The line
      moves, in place on a TTY, clean under `--json`.
      PARTIAL 2026-11-23: the line moves and the count is right, but the
      output interleaves badly with phase messages and the result line
      prints twice. Tracked in #151 rather than blocking the release; the
      --json half of this item is still untested.
- [ ] 4.10 **Conflict names.** Force a conflict (two mods, one
      destination). The message names both mods and keeps the paths.
- [x] 4.11 **Serve stub.** `nexus-dl serve`. Names the replacement.
      Exits non-zero.
      PASSED 2026-11-23: "The web UI was removed in 0.4.0. Use nexus-dl
      shell instead". (Exit code not captured but the stub ran.)
- [ ] 4.12 **Redaction escape.** Trigger an error with a secret in it.
      Default output is redacted. Diagnostic mode shows the detail.

---

## Phase 4b: Download-manager behaviour (PR #153)

Extraction used to delete the archive, so gc had nothing to reclaim:
the everyday path threw away its own input. Now every extract path
keeps the verified archive, gc buys the disk back, and the tool points
at the reclaimable disk while the user is working. Run these after 4.7
and 4.8.

- [x] 4.13 **Visibility nudge.** After `sync` (and `add`, `import`,
      `update`, `deploy`), when gc would reclaim over 100 MB, one line
      names the size and the command. Silent below 100 MB and in
      `--json`.
      PASSED 2026-11-23, fresh environment: after the 8-mod sync,
      `145.1 MB of downloaded archives can be reclaimed. Run 'nexus-dl gc
      ... --apply'.` The 145.1 MB matched the archive bytes on disk to
      the byte (152178901). Fired again after `deploy`, as designed.
- [ ] 4.14 **Re-click dedupe.** Click the same nxm "Mod manager
      download" link twice. The first downloads and extracts; the second
      reports `already_downloaded` with **zero** network traffic. `STOP`
      NOT RUN: needs a real browser click. This is the 0.4.0 headline
      path and the whole point of keeping the archive, so it is proved
      live or it is not proved. Covered by
      `test_reclick_after_extraction_dedupes_zero_network`, which is not
      the same thing.
- [x] 4.15 **Deploy skips download containers.** After deploy, the game
      dir holds no archive: not `.zip`/`.7z`/`.rar`, and not the
      extension-less names sync gives them.
      PASSED 2026-11-23, scratch game dir: 0 containers by suffix AND 0
      by content sniffing over the whole tree. 662 files landed, all 8
      plugins present. This bug appeared the moment archives were kept:
      the fallback classifier shipped any unknown root file into `Data/`.
- [x] 4.16 **Quiet gc is informative.** `gc` with nothing to reclaim
      explains what it looked for, instead of "Nothing to report".
      PASSED 2026-11-23: "No archives to reclaim here. Downloads keep
      their archive so this command can buy the disk back once the files
      are extracted." Same message on a directory with no state at all.
- [x] 4.17 **The `--no-extract` loop closes.** `add --no-extract`, then
      `add` again without it, then `gc --apply`. The archive is kept as
      the only copy, extraction keeps it too, and gc reclaims it once the
      payload is on disk.
      PASSED 2026-11-23: kept ("the archive is the only copy of this
      mod") -> extracted (archive still on disk) -> gc reclaimed 1.6 MB,
      4/4 payload intact. Banners' attribution went from `[]` to 4 real
      extracted paths. Before PR #153 this was a dead end: the second add
      deleted the archive and gc had nothing to do.

### Known presentation issue (not a blocker)

`add --no-extract` prints `Extracting...` even though nothing is
extracted. Misleading progress line. Noted in 4.7; worth a small fix
but not a release blocker.

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
