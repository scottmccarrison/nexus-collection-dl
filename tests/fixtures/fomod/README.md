# FOMOD test fixtures

Real `ModuleConfig.xml` files, used to pin the install-plan behaviour
against real-world shapes rather than against guesses about the schema.

## What they are

These are the installer **config** files: which option groups exist and
which archive paths each option installs, with what `destination`. They
are not mod content and carry no assets, textures, meshes, or plugins.

## On-disk finds

Both were found at `<worktrees>/.artifacts/nexus-harness-test/[Xirconia]
More Quests/`, inside an already-extracted Skyrim SE mod tree left over
from earlier smoke testing of the `nxm://` click path. They are what
happened to be available, and the sample is small.

- `frozen-in-time.ModuleConfig.xml` - from a mod the tree labels "Frozen
  in Time". Three options (`UUNP`, `CBBE`, `Vanilla`) in one
  `SelectExactlyOne` group, each installing **two** folders
  (`uunp\textures` and `uunp\meshes`, and so on), plus an eleven-entry
  `requiredInstallFiles` block all under `core\`.
- `landscape-fixes.ModuleConfig.xml` - from "Landscape Fixes For Grass
  Mods". Three single-plugin groups using `SelectAtLeastOne` and
  `SelectAny`, each plugin installing **one** `<file>` rather than a
  `<folder>`, and one plugin with a `dependencyType` block containing an
  `Or` of `fileDependency` entries.

Between them these two expose a shape difference directly: one config's
plugins use `<folder>` sources (`uunp\textures`) and the other's use
`<file>` sources (`01 Main Mod\Foo.esp`).

## go-fomod test data

`gofomod-*.xml` come from the go-fomod project's own `testdata/`
directory (`github.com/Rethunk-Tech/go-fomod`), fetched from the raw
GitHub URLs. They are small and synthetic, but they were written against
the real schema and they cover shapes neither Skyrim config does:

- `gofomod-choose-one.xml` - `SelectExactlyOne` with `<file>` sources
  under option-named directories (`alpha/manifest.json`).
- `gofomod-flags.xml` - plugins carrying only `<conditionFlags>` and no
  `<files>` at all. Neither option installs a folder of its own. The
  flag value is element **text** (`<flag name="x">y</flag>`), not an
  attribute, which is how real configs write it too.
- `gofomod-folder.xml` - `requiredInstallFiles` and no install steps.
- `gofomod-required.xml` - sources with **no directory component**
  (`addon.txt`, `core.txt` at the archive root).

Between them these pinned two further behaviours: a `<folder>` source
with no separator is a top-level directory (`<folder source="pack">`)
while a `<file>` source with no separator is a root-level file, and a
plugin with no `<files>` is a valid selection that installs nothing
rather than a reason to reject the config.

## Real Nexus archives

`improved-map`, `xpmse`, `spid`, `nvac`, `compliance`, and
`invisible-step` are the configs out of the `Nexus-Mods/fomod-installer`
project's own `test/TestData/Data/` directory (`test/TestData/Data/`),
which ships real Nexus-downloaded mod archives as the official
installer's test data. The archives themselves are not committed; the
configs are what the planner reads, and the deploy tests build matching
directory trees.

- `improved-map.ModuleConfig.xml` - Fallout 4, mod 1215, "Improved Map
  with Visible Roads 2.0-1215-2-0.zip". Six numbered option folders at
  the archive root (`01 Improved Map with Visible Roads - Default` and
  so on), one `<file>` each, and `destination` equal to the source
  minus the option folder. This is the one shape the old top-level
  folder heuristic handled correctly end to end.
- `xpmse.ModuleConfig.xml` - Skyrim SE, mod 1988, "XP32 Maximum Skeleton
  Special Extended". UTF-16LE. 83 `<folder>` sources nested two or three
  deep under shared top-levels, every `destination` empty. Options
  partition `Skeleton Rig\HDT` from `Skeleton Rig\HDT 4.51` and
  `Skeleton Rig\TBBP`, and `Animations\<category>\<style>`, which a
  top-level keep-or-skip decision merges into one. Install-step
  visibility is flag-gated (`Ext`, `MCM`) and the flags come from
  options picked in earlier steps.
- `spid.ModuleConfig.xml` - Skyrim SE, mod 36869, "Spell Perk Item
  Distributor". The archive wraps everything in one folder
  (`Spell Perk Item Distributor FOMOD Installer/`) and writes sources
  relative to that wrapper (`AE/SKSE/Plugins`), with `destination`
  renaming it to `SKSE/Plugins`. Two version options (`AE`, `SE`) that
  must not both install.
- `nvac.ModuleConfig.xml` - Fallout NV, mod 53635, "NVAC - New Vegas
  Anti Crash". No options at all: four `conditionalFileInstalls`
  patterns choose `NVSE` -> `SKSE` / `FOSE` / `NVSE` / `OBSE` from
  `gameDependency` versions and `fileDependency` load-order states,
  including nested `dependencies operator="Or"`. The rename is the
  whole install.
- `compliance.ModuleConfig.xml` - from `FomodComplianceTests.zip`, a
  synthetic suite the installer project uses to check spec compliance.
  `SelectAll` and `SelectAtMostOne` groups, `Required` / `Optional` /
  `NotUsable` types, `alwaysInstall` and `installIfUsable` entries, and
  the same plugin name reused across groups.
- `invisible-step.ModuleConfig.xml` - from `InvisibleStepTest.zip`,
  synthetic. Step 2 is gated on a flag that only Option B of step 1
  sets, and the flag value is element text.

Two of the same directory's archives (`The Mod Configuration Menu` and
`CSharpTestCase.zip`) are FOMOD 1.0 installers (`fomod/script.cs` plus
`info.xml`, no `ModuleConfig.xml`). Those are what the archive detector
refuses; nothing parses them.

## dreads

`dreads.ModuleConfig.xml` is the whole config of
`dreadflopp/dreads_modlist_patches`, a real Skyrim patch collection on
GitHub (`github.com/dreadflopp/dreads_modlist_patches`), fetched from
the raw URL. 216 KB, 33 install steps, 398 `<pattern>` blocks, 665
`flagDependency` conditions. 237 of its 264 sources are root-level
`<file>` entries and 254 of them live inside `conditionalFileInstalls`
patterns rather than under any plugin. It is the evidence that
conditional installs are a first-class real shape and not an edge.

## What they do not cover

The old gaps are closed: `flagDependency`, `<visible>`,
`conditionalFileInstalls`, `SelectAll`, `alwaysInstall`,
`installIfUsable`, `gameDependency`, and `fileDependency` all appear in
these fixtures now, and the `source` -> `destination` mapping has been
observed in real archives rather than inferred. Still missing:

- FOMOD 1.0 (`fomod/script.lua`) beyond the `info.xml` refusal. Real
  `script.cs` samples exist in the fomod-installer test data; no
  `script.lua` sample exists in anything we have found.
- A second wrapped archive. The "sources are relative to the `fomod/`
  directory's parent" rule rests on SPID alone. It matches how other
  mod managers resolve sources, but one archive is one archive.
- Group types `SelectAtLeastOne` (present only in the two on-disk finds)
  and any `dependencyType` patterns beyond SPID's. XPMSE uses
  `SelectExactlyOne`, `SelectAtMostOne`, and `SelectAny`.
- Other game families. SSE, F4, and FNV are covered; Oblivion, LE, and
  the non-Bethesda games are not.
