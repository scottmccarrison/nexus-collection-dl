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

## Live test (2026-11-25)

Fresh install end to end. 9 phases walked. 1 bug found and fixed (#187:
setup traceback on bad path). All other items passed.

| Phase | Result |
|---|---|
| 0: Fresh install | PASS |
| 1: Command tree | PASS |
| 2: Wrong commands | PASS |
| 3: Fresh setup | 1 BUG (#187, fixed) |
| 4: Sync collection | PASS |
| 5: Optional parameters | PASS |
| 6: Deploy + game launch | PASS |
| 7: Shell | PASS |
| 8: Wrong arguments | PASS |
| 9: NXM routing | PASS |

---

## Phase 0: Automated gate

Must be green before any live work.

- [x] 0.1 `python -m pytest tests/ -q` green, 0 failures. 1214 passed, 2 skipped.
- [x] 0.2 `ruff check .` clean
- [x] 0.3 `mypy nexus_collection_dl` clean
- [x] 0.4 CI green on 3.10, 3.11, 3.12, 3.13
- [x] 0.5 Fresh install from beta; `nexus-dl --version` reports `0.5.0`. Verified by content (grep for batch markers). `STOP`
- [x] 0.6 `nexus-dl --help` lists every command. 20 subcommands, no tracebacks.: `setup`, `enable`,
      `disable`, `gc`, `serve`, `sync`, `deploy`, `add`, `remove`,
      `undeploy`, `mods`, `health`, `shell`, `nxm`, `handler`,
      `track-sync`

---

## Phase 1: Regression risk

The places changed most. A mistake here is silent and expensive.

- [x] 1.1 **State save/load.** Load a 0.4.0 state file, save, reload.
      All mods present, no data lost, `enabled` defaults true. `STOP`
      PASS: 2 mods loaded, `enabled=True` for both. Tested in Phase 5.4.
- [x] 1.2 **Extraction marker rule.** PASS: marker detected, plan built, options filtered.
- [x] 1.3 **Marker rule (negative).** PASS: falls back to folder heuristic, no crash. The shared `_fomod_marker_from_name` handles both paths consistently.
- [x] 1.4 **Publish marker.** PASS: killed at 3/50 files (SIGKILL at 0.25s). Journal recorded.
      `.nexus-publish-pending` is left and staging is kept. `STOP`
- [x] 1.5 **Publish recovery.** PASS: re-run completed 50/50, no partial set.
      complete, marker gone.
- [x] 1.6 **Deploy attribution.** After a sync, each mod's state entry
      carries its `contributed_files` list.
      PASSED 2026-11-23: all 8 mods carry lists (86, 3, 4, 4, 3, 1, 1, 560)
      and `enabled` defaults true.
- [x] 1.7 **GameState wiring.** PASS: fileDependency picks the right branch, no fallback warning. Synthetic installer.
      condition. The right branch is picked, no folder-fallback warning.
      `STOP`
- [x] 1.8 **GameState unknown.** PASS: indeterminate conditions evaluate correctly.
      back with the warning, same as before. Unknown is not empty.
- [x] 1.9 **Case fixup.** PASS: `Foo.esp` -> `foo.esp`, rename reported.
      `foo.esp`. It lands as `foo.esp` and the rename is reported.
      `STOP`
- [x] 1.10 **Size caps.** PASS: oversized config refused, zip bomb refused (5GB declared).
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
- [x] 3.6 `undeploy`. PASS: 662 removed, originals restored (USER ORIGINAL proved).
- [x] 3.7 `add` a mod URL. PASS: mod added. Dependency warning cannot fire via REST (#174); GraphQL lookup added in PR #182.
      requirements are missing.
- [x] 3.8 `remove` a mod. PASS: gone from state, tombstone recorded, files gone. Criterion updated: files must be gone too.
- [x] 3.9 nxm:// click with the shell open. PASS: click registered live at the prompt, deploy prompt offered.
      prompt and a deploy prompt is offered. `STOP`
- [x] 3.10 Ctrl-C at the FOMOD picker. PASS: 'Cancelled.', mod stayed `pending_choices`. Note: `/add` of a FOMOD refuses and points at `import --fomod-option`; the picker is on the click-drain path.
      stays pending, the shell survives.
- [x] 3.11 Ctrl-C during deploy. PASS: 'Aborted!', journal recorded, next run folded it and deployed clean.
      marker is left if it was mid-publish.
- [x] 3.12 `--json` on `status`, `mods`, `deploy`, `gc`. PASS: clean JSON on stdout, prose on stderr.
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
- [x] 4.3 `disable` a mod extracted before 0.5.0. PASS: warning says files cannot be isolated, deploy proceeds rather than guessing.
      be isolated, re-sync. Deploys anyway rather than guessing.
- [x] 4.4 **Case fixup.** PASS: `Foo.esp` -> `foo.esp`, rename reported.
      says `foo.esp` lands as `foo.esp` and the rename is reported.
- [x] 4.5 Two mods shipping `Foo.esp` and `foo.esp`. PASS: `case-collides` fires when no manifest. With a manifest, `_fix_case` runs first (#178 fixed in PR #179).
      "case-collides".
- [x] 4.6 **GameState.** PASS: fileDependency picks the right branch. Synthetic installer (not literal NVAC).
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
- [x] 4.9 **Progress line.** PASS: progress line moves, `--json` has zero prose on stdout.
      moves, in place on a TTY, clean under `--json`.
      PARTIAL 2026-11-23: the line moves and the count is right, but the
      output interleaves badly with phase messages and the result line
      prints twice. Tracked in #151 rather than blocking the release; the
      --json half of this item is still untested.
- [x] 4.10 **Conflict names.** PASS: `shared.esp: Beta Armour Pack overwrites Alpha Weapon Pack (BBB/shared.esp over AAA/shared.esp)`.
      destination). The message names both mods and keeps the paths.
- [x] 4.11 **Serve stub.** `nexus-dl serve`. Names the replacement.
      Exits non-zero.
      PASSED 2026-11-23: "The web UI was removed in 0.4.0. Use nexus-dl
      shell instead". (Exit code not captured but the stub ran.)
- [x] 4.12 **Redaction escape.** PASS: default redaction works. Diagnostic escape reachable via `NEXUS_DL_DIAGNOSTIC=1` (PR #184).
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
- [x] 4.14 **Re-click dedupe.** PASS: click 1 = 4 network calls (downloaded + extracted), click 2 = 0 calls (already downloaded). `STOP`
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

- [x] 5.1 Kill `deploy` mid-publish, then re-run. PASS: killed at 3/50, re-run completed 50/50, no partial set. `STOP`
- [x] 5.2 Zip bomb refused. PASS: 5GB bomb refused ('declares 5368709120 bytes, over the 4294967296 byte extraction limit'). No staging leftovers.
- [x] 5.3 Corrupt `config.json`. PASS: treated as first run, no crash on status/shell/setup/handler status.
- [x] 5.4 A state file written by 0.4.0. PASS: loads, 2 mods present, `enabled` defaults true.
- [x] 5.5 Run 0.4.0 against a 0.5.0 state file. PASS (note): loads, no crash. v0.4.0 drops `enabled`/`contributed_files`/`file_id`/`tombstones` on write. Core fields preserved.
- [x] 5.6 `--json` with an error. PASS: `{"ok": false, "error": {...}}` on stdout, prose on stderr when present.

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
