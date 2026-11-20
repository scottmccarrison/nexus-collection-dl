# FOMOD test fixtures

Real `ModuleConfig.xml` files, used to pin the folder-filtering behaviour
against real-world shapes rather than against guesses about the schema.

## Where these came from

Both were found on disk at
`<worktrees>/.artifacts/nexus-harness-test/[Xirconia] More Quests/`, inside
an already-extracted Skyrim SE mod tree left over from earlier smoke
testing of the `nxm://` click path. They were not collected as fixtures;
they are what happened to be available, and the sample is small.

- `frozen-in-time.ModuleConfig.xml` - from a mod the tree labels "Frozen in
  Time". Three options (`UUNP`, `CBBE`, `Vanilla`) in one
  `SelectExactlyOne` group, each installing **two** folders
  (`uunp\textures` and `uunp\meshes`, and so on), plus an eleven-entry
  `requiredInstallFiles` block all under `core\`.
- `landscape-fixes.ModuleConfig.xml` - from "Landscape Fixes For Grass
  Mods". Three single-plugin groups using `SelectAtLeastOne` and
  `SelectAny`, each plugin installing **one** `<file>` rather than a
  `<folder>`, and one plugin with a `dependencyType` block containing an
  `Or` of `fileDependency` entries.

Between them these two expose the shape bug directly: one config's
plugins use `<folder>` sources (`uunp\textures`) and the other's use
`<file>` sources (`01 Main Mod\Foo.esp`), and the old parser stored those
in different shapes.

## What they are

These are the installer **config** files: which option groups exist and
which archive paths each option installs. They are not mod content and
carry no assets, textures, meshes, or plugins.

## Third-party fixtures

`gofomod-*.xml` come from the go-fomod project's own `testdata/`
directory (`github.com/Rethunk-Tech/go-fomod`), fetched from the raw
GitHub URLs. They are small and synthetic, but they were written against
the real schema and they cover shapes neither Skyrim config does:

- `gofomod-choose-one.xml` - `SelectExactlyOne` with `<file>` sources
  under option-named directories (`alpha/manifest.json`).
- `gofomod-flags.xml` - plugins carrying only `<conditionFlags>` and no
  `<files>` at all. Neither option installs a folder of its own.
- `gofomod-folder.xml` - `requiredInstallFiles` and no install steps.
- `gofomod-required.xml` - sources with **no directory component**
  (`addon.txt`, `core.txt` at the archive root).

Between them these pinned two further behaviours: a `<folder>` source
with no separator is a top-level directory (`<folder source="pack">`)
while a `<file>` source with no separator is a root-level file and has
no directory to skip, and a plugin with no `<files>` is a valid
selection that installs nothing rather than a reason to reject the
config.

## What they do not cover

Two real configs, from one mod tree, both UTF-16LE, plus go-fomod's four
small synthetic ones. Present in a fixture but **ignored by the parser**:
`conditionFlags` (go-fomod-flags.xml exercises it; we read no folder for
such a plugin and do not evaluate the flag). Absent from every fixture
and so unevidenced: `flagDependency`, `visible`,
`conditionalFileInstalls`, `SelectAll`, `alwaysInstall`,
`installIfUsable`, `gameDependency`, and FOMOD 1.0 (`fomod/script.lua`
plus `info.xml`). For those the schema is the only evidence, which is not
evidence about how they show up in real archives.

Two real FOMOD **archives** (a zip or 7z with one of these inside) are
still missing. That is the gap that makes the folder-layout question -
how `source` paths map to top-level directories after extraction - a
reasoned inference from these configs rather than something observed.
