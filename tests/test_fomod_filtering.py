"""Deploy-time FOMOD folder filtering.

``build_fomod_skip_set`` works out which top-level directories belong to
FOMOD options the user did NOT pick, so ``deploy`` can leave them out.
It is a heuristic over an already-merged extraction tree, and these tests
pin the three bugs that made it produce wrong installs, plus the limits
it still has.

The two real ``ModuleConfig.xml`` files under ``tests/fixtures/fomod/``
are the ground truth for the parsing half. Between them they cover the
shape that broke: one installs through ``<folder>`` sources
(``uunp\\textures``) and the other through ``<file>`` sources
(``01 Main Mod\\Foo.esp``), and the old parser stored those two in
different shapes so the skip-set match could only ever hit one of them.
"""

from __future__ import annotations

from pathlib import Path

from nexus_collection_dl.fomod import (
    _get_required_folders,
    build_fomod_skip_set,
    parse_module_config,
    resolve_selected_folders,
)

FIXTURES = Path(__file__).parent / "fixtures" / "fomod"
FROZEN = FIXTURES / "frozen-in-time.ModuleConfig.xml"
LANDSCAPE = FIXTURES / "landscape-fixes.ModuleConfig.xml"


def write_config(path: Path, body: str) -> Path:
    """Write a ModuleConfig.xml. UTF-8; the parser also handles UTF-16."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def choices(*option_names: str) -> dict:
    """A manifest-shaped choices blob selecting the named options.

    The shape is the one a live Nexus collection stores:
    ``{"type": "fomod", "options": [{"name": <step>, "groups": [{"name":
    <group>, "choices": [{"name": <option>, "idx": N}]}]}]}``.
    """
    return {
        "type": "fomod",
        "options": [
            {
                "name": "",
                "groups": [
                    {
                        "name": "G",
                        "choices": [
                            {"name": n, "idx": i + 1}
                            for i, n in enumerate(option_names)
                        ],
                    }
                ],
            }
        ],
    }


# --- the real fixtures -----------------------------------------------------


def test_frozen_in_time_maps_each_option_to_its_top_level():
    """Two folders per plugin, both under one top-level.

    The old parser kept only the first folder and kept it unsplit, so it
    returned ``'uunp\\textures'`` - a value that can never match the
    top-level name ``'uunp'`` the deploy filter compares against.
    """
    mapping = parse_module_config(FROZEN)
    assert set(mapping) == {"uunp", "cbbe", "vanilla"}
    assert mapping["uunp"] == {"uunp"}
    assert mapping["cbbe"] == {"cbbe"}
    assert mapping["vanilla"] == {"vanilla"}
    # The mesh folders are not dropped: they reduce to the same top-level
    # as the texture folders, and nothing is lost by that.
    for dirs in mapping.values():
        for d in dirs:
            assert "\\" not in d and "/" not in d


def test_frozen_in_time_required_folders_are_the_core_tree():
    assert _get_required_folders(FROZEN) == {"core"}


def test_landscape_fixes_maps_file_sources_to_top_level():
    """``<file source="01 Main Mod\\Foo.esp">`` -> ``'01 main mod'``."""
    mapping = parse_module_config(LANDSCAPE)
    assert mapping["landscape fixes for grass mods.esp"] == {"01 main mod"}
    assert mapping["landscape for grass mods - military camps.esp"] == {
        "02 military camps"
    }
    assert mapping["generic grass limiter.esp"] == {"03 generic grass limiter"}


def test_both_real_configs_parse_without_raising():
    """Encoding is the thing most likely to blow up on real input."""
    assert parse_module_config(FROZEN)
    assert parse_module_config(LANDSCAPE)


# --- the real manifest shape -----------------------------------------------


def test_real_manifest_shape_selects_folders():
    """A live collection's stored picks against Frozen in Time's config.

    Verbatim from a real collection's state (mod 39732). The step name
    is empty, as it is in the config itself.
    """
    picks = {
        "type": "fomod",
        "options": [
            {
                "name": "",
                "groups": [
                    {
                        "name": "Body Textures",
                        "choices": [{"name": "CBBE", "idx": 1}],
                    }
                ],
            }
        ],
    }
    mapping = parse_module_config(FROZEN)
    assert resolve_selected_folders(picks, mapping) == {"cbbe"}


def test_real_manifest_shape_skips_the_unpicked_options(tmp_path):
    write_config(tmp_path / "ModuleConfig.xml", FROZEN.read_text(encoding="utf-16"))
    for d in ("uunp", "cbbe", "vanilla"):
        (tmp_path / d).mkdir()

    picks = {
        "type": "fomod",
        "options": [
            {
                "name": "",
                "groups": [
                    {
                        "name": "Body Textures",
                        "choices": [{"name": "CBBE", "idx": 1}],
                    }
                ],
            }
        ],
    }
    skip = build_fomod_skip_set(tmp_path, {39732: picks})
    assert skip == {"uunp", "vanilla"}, f"CBBE is picked; skip got {skip}"


# --- bug 1: <folder> and <file> must produce the same shape ----------------


def test_folder_and_file_sources_reach_the_same_shape(tmp_path):
    """A plugin installing through <folder> must not be unmatchable."""
    cfg = write_config(tmp_path / "ModuleConfig.xml", """
    <config>
      <installSteps><installStep name=""><optionalFileGroups>
        <group name="G" type="SelectExactlyOne">
          <plugins><plugin name="FolderOpt">
            <files><folder source="uunp\\textures" destination="textures"/></files>
            <typeDescriptor><type name="Optional"/></typeDescriptor>
          </plugin>
          <plugin name="FileOpt">
            <files><file source="01 Main Mod\\a.esp" destination="a.esp"/></files>
            <typeDescriptor><type name="Optional"/></typeDescriptor>
          </plugin>
          </plugins>
        </group>
      </optionalFileGroups></installStep></installSteps>
    </config>
    """)
    mapping = parse_module_config(cfg)
    assert mapping["folderopt"] == {"uunp"}
    assert mapping["fileopt"] == {"01 main mod"}


def test_skip_set_can_match_a_folder_sourced_option(tmp_path):
    """The end-to-end case bug 1 broke: the option actually gets skipped."""
    write_config(tmp_path / "ModuleConfig.xml", """
    <config>
      <installSteps><installStep name=""><optionalFileGroups>
        <group name="G" type="SelectExactlyOne">
          <plugins><plugin name="UUNP">
            <files><folder source="uunp\\textures" destination="textures"/></files>
          </plugin>
          <plugin name="CBBE">
            <files><folder source="cbbe\\textures" destination="textures"/></files>
          </plugin>
          </plugins>
        </group>
      </optionalFileGroups></installStep></installSteps>
    </config>
    """)
    for d in ("uunp", "cbbe"):
        (tmp_path / d).mkdir()

    skip = build_fomod_skip_set(tmp_path, {1: choices("UUNP")})
    assert skip == {"cbbe"}, f"unselected CBBE must be skipped, got {skip}"


# --- bug 2: every folder of a plugin, not just the first -------------------


def test_multi_folder_plugin_collects_every_top_level(tmp_path):
    """A plugin installing to two different top-levels must lose neither.

    The old parser broke out of the loop after the first ``<folder>``, so
    a plugin with ``textures\\a`` and ``meshes\\b`` only ever contributed
    ``textures`` - and the ``meshes`` tree of an unselected option
    deployed anyway.
    """
    cfg = write_config(tmp_path / "ModuleConfig.xml", """
    <config>
      <installSteps><installStep name=""><optionalFileGroups>
        <group name="G" type="SelectExactlyOne">
          <plugins><plugin name="Both">
            <files>
              <folder source="textures\\a" destination="textures"/>
              <folder source="meshes\\b" destination="meshes"/>
            </files>
          </plugin>
          <plugin name="Other">
            <files><folder source="other\\c" destination="c"/></files>
          </plugin>
          </plugins>
        </group>
      </optionalFileGroups></installStep></installSteps>
    </config>
    """)
    mapping = parse_module_config(cfg)
    assert mapping["both"] == {"textures", "meshes"}
    assert mapping["other"] == {"other"}


def test_skip_set_covers_every_folder_of_an_unselected_option(tmp_path):
    write_config(tmp_path / "ModuleConfig.xml", """
    <config>
      <installSteps><installStep name=""><optionalFileGroups>
        <group name="G" type="SelectExactlyOne">
          <plugins><plugin name="Both">
            <files>
              <folder source="textures\\a" destination="textures"/>
              <folder source="meshes\\b" destination="meshes"/>
            </files>
          </plugin>
          <plugin name="Kept">
            <files><folder source="kept\\c" destination="c"/></files>
          </plugin>
          </plugins>
        </group>
      </optionalFileGroups></installStep></installSteps>
    </config>
    """)
    for d in ("textures", "meshes", "kept"):
        (tmp_path / d).mkdir()

    skip = build_fomod_skip_set(tmp_path, {1: choices("Kept")})
    assert skip == {"textures", "meshes"}


# --- bug 3: one mod's choices must not skip another mod's folders ----------


def test_fallback_does_not_skip_another_mods_selected_folder(tmp_path):
    """The cross-mod bug.

    Two mods with choices and no usable config. ``01 Main Mod`` is what
    mod A picked and ``02 Textures`` is what mod B picked. The old code
    matched each numbered folder against each mod independently, so
    ``01 Main Mod`` was skipped while mod B was being processed and
    ``02 Textures`` was skipped while mod A was.
    """
    (tmp_path / "01 Main Mod").mkdir()
    (tmp_path / "02 Textures").mkdir()
    (tmp_path / "03 Optional").mkdir()

    skip = build_fomod_skip_set(tmp_path, {
        1: choices("Main Mod"),
        2: choices("Textures"),
    })
    assert "01 Main Mod" not in skip, "mod A selected this"
    assert "02 Textures" not in skip, "mod B selected this"
    assert "03 Optional" in skip, "nobody selected this"


def test_fallback_still_skips_a_folder_no_mod_selected(tmp_path):
    (tmp_path / "01 Main Mod").mkdir()
    (tmp_path / "99 Unrelated").mkdir()

    skip = build_fomod_skip_set(tmp_path, {1: choices("Main Mod")})
    assert skip == {"99 Unrelated"}


# --- required files are never skipped --------------------------------------


def test_required_folders_are_never_skipped(tmp_path):
    write_config(tmp_path / "ModuleConfig.xml", """
    <config>
      <requiredInstallFiles>
        <folder source="core\\data" destination="data"/>
        <file source="core\\thing.esp" destination="thing.esp"/>
      </requiredInstallFiles>
      <installSteps><installStep name=""><optionalFileGroups>
        <group name="G" type="SelectExactlyOne">
          <plugins><plugin name="Opt">
            <files><folder source="opt\\a" destination="a"/></files>
          </plugin>
          </plugins>
        </group>
      </optionalFileGroups></installStep></installSteps>
    </config>
    """)
    for d in ("core", "opt"):
        (tmp_path / d).mkdir()

    skip = build_fomod_skip_set(tmp_path, {1: choices("Opt")})
    assert skip == set(), "core is required and opt is selected"


# --- the limits, pinned rather than hidden ---------------------------------


def test_options_sharing_a_top_level_cannot_be_separated(tmp_path):
    """Documented limit: the filter works on top-level dirs only.

    Both options install from the same top-level ``assets`` tree, so
    there is no top-level name that means "unselected" here. The filter
    returns nothing rather than guessing, and the residual wrong install
    is what option support exists to fix.
    """
    write_config(tmp_path / "ModuleConfig.xml", """
    <config>
      <installSteps><installStep name=""><optionalFileGroups>
        <group name="G" type="SelectExactlyOne">
          <plugins><plugin name="A">
            <files><folder source="assets\\a" destination="a"/></files>
          </plugin>
          <plugin name="B">
            <files><folder source="assets\\b" destination="b"/></files>
          </plugin>
          </plugins>
        </group>
      </optionalFileGroups></installStep></installSteps>
    </config>
    """)
    (tmp_path / "assets").mkdir()

    skip = build_fomod_skip_set(tmp_path, {1: choices("A")})
    assert "assets" not in skip


def test_fuzzy_fallback_can_keep_a_folder_it_should_skip(tmp_path):
    """Documented limit of the fallback: substring matching both ways.

    ``01 Textures`` is unselected for its mod, but it substring-matches
    the selected name ``Textures and Meshes``, so it is kept. This is the
    opposite error to bug 3 and is the safer of the two; it is still
    wrong, and pinning it means the behaviour cannot drift silently.
    """
    (tmp_path / "01 Textures").mkdir()
    (tmp_path / "02 Other").mkdir()

    skip = build_fomod_skip_set(tmp_path, {1: choices("Textures and Meshes")})
    assert "01 Textures" not in skip
    assert "02 Other" in skip


# --- no-regression cases ---------------------------------------------------


def test_no_choices_means_nothing_is_skipped(tmp_path):
    (tmp_path / "01 Anything").mkdir()
    assert build_fomod_skip_set(tmp_path, {}) == set()


def test_selected_option_is_kept(tmp_path):
    write_config(tmp_path / "ModuleConfig.xml", """
    <config>
      <installSteps><installStep name=""><optionalFileGroups>
        <group name="G" type="SelectExactlyOne">
          <plugins><plugin name="Pick">
            <files><folder source="pick\\a" destination="a"/></files>
          </plugin>
          </plugins>
        </group>
      </optionalFileGroups></installStep></installSteps>
    </config>
    """)
    (tmp_path / "pick").mkdir()
    assert build_fomod_skip_set(tmp_path, {1: choices("Pick")}) == set()


def test_unreadable_config_falls_through_to_the_fallback(tmp_path):
    """A config the parser cannot read must not crash the deploy."""
    write_config(tmp_path / "ModuleConfig.xml", "not xml at all <<<")
    (tmp_path / "01 Picked").mkdir()
    (tmp_path / "02 Other").mkdir()

    skip = build_fomod_skip_set(tmp_path, {1: choices("Picked")})
    assert "01 Picked" not in skip
    assert "02 Other" in skip


def test_resolve_selected_folders_is_case_insensitive(tmp_path):
    cfg = write_config(tmp_path / "ModuleConfig.xml", """
    <config>
      <installSteps><installStep name=""><optionalFileGroups>
        <group name="G" type="SelectExactlyOne">
          <plugins><plugin name="UUNP">
            <files><folder source="uunp\\t" destination="t"/></files>
          </plugin>
          </plugins>
        </group>
      </optionalFileGroups></installStep></installSteps>
    </config>
    """)
    mapping = parse_module_config(cfg)
    assert resolve_selected_folders(choices("uunp"), mapping) == {"uunp"}
    assert resolve_selected_folders(choices("UUNP"), mapping) == {"uunp"}


# --- the numbered-prefix strip (deploy path resolution) --------------------
#
# The same regex family decides whether a leading `01 ...` directory is an
# option wrapper to strip when computing a file's game-relative path. It
# used to require a dash or underscore, so the standard `01 Main Mod` form
# was NOT stripped and files landed under the wrapper instead of at the
# game root.


def test_numbered_wrapper_is_stripped_from_the_dest_path():
    """A file under `01 Main Mod/Data/` deploys as `Data/` content.

    The wrapper is stripped before classification, so the file lands as
    the data file it is (`thing.esp`) rather than at the literal
    `01 Main Mod/Data/thing.esp` game path.
    """
    import tempfile

    from nexus_collection_dl.deploy import classify_files

    with tempfile.TemporaryDirectory() as td:
        mods = Path(td)
        (mods / "01 Main Mod" / "Data").mkdir(parents=True)
        (mods / "01 Main Mod" / "Data" / "thing.esp").write_bytes(b"x")
        (mods / "02 Optional" / "Data").mkdir(parents=True)
        (mods / "02 Optional" / "Data" / "other.esp").write_bytes(b"x")

        plan = classify_files(mods, "skyrimspecialedition")

    dests = [str(dest) for _src, dest in plan.game_root_files + plan.data_files]
    # The wrapper names must not survive into the destination path.
    assert not any("Main Mod" in d or "Optional" in d for d in dests), dests
    assert "thing.esp" in dests, dests
    assert "other.esp" in dests, dests


def test_numbered_prefix_needs_a_separator():
    """`01Main` is a folder that happens to start with digits, not an
    option wrapper."""
    from nexus_collection_dl.deploy import NUMBERED_OPTION_RE

    assert NUMBERED_OPTION_RE.match("01 Main Mod")
    assert NUMBERED_OPTION_RE.match("01 - Bright")
    assert NUMBERED_OPTION_RE.match("01_Optional")
    assert NUMBERED_OPTION_RE.match("01  Two Spaces")
    assert not NUMBERED_OPTION_RE.match("01Main")
    assert not NUMBERED_OPTION_RE.match("2024")
    assert not NUMBERED_OPTION_RE.match("01")


# --- third-party fixtures (go-fomod's own test data) -----------------------
#
# `tests/fixtures/fomod/gofomod-*.xml` are from the go-fomod project's
# testdata directory. They are small and synthetic, but they were written
# against the real schema and they cover shapes neither of the two real
# Skyrim configs does: `conditionFlags`-only plugins and sources with no
# directory component.

GOFOMOD = FIXTURES


def test_gofomod_choose_one_maps_file_sources_to_dirs():
    mapping = parse_module_config(GOFOMOD / "gofomod-choose-one.xml")
    assert mapping == {"alpha": {"alpha"}, "beta": {"beta"}}


def test_gofomod_folder_only_required_install():
    """requiredInstallFiles with no install steps at all."""
    cfg = GOFOMOD / "gofomod-folder.xml"
    assert parse_module_config(cfg) == {}
    assert _get_required_folders(cfg) == {"pack"}


def test_gofomod_required_root_level_sources_contribute_no_dirs():
    """`addon.txt` and `core.txt` sit at the archive root.

    There is no top-level directory to skip for one of those, so they
    must contribute nothing rather than a directory name that could
    never match.
    """
    cfg = GOFOMOD / "gofomod-required.xml"
    mapping = parse_module_config(cfg)
    assert mapping == {"addon": set()}, mapping
    assert _get_required_folders(cfg) == set()


def test_condition_flags_only_options_are_folder_less(tmp_path):
    """A plugin carrying only `conditionFlags` installs no folder.

    flags.xml has both options in that shape. The parser must not invent
    folders for them, and the skip set must be empty.
    """
    cfg = GOFOMOD / "gofomod-flags.xml"
    mapping = parse_module_config(cfg)
    assert mapping == {"on": set(), "off": set()}, mapping
    assert build_fomod_skip_set(tmp_path, {1: choices("On")}) == set()


def test_selecting_a_folder_less_option_still_handled_by_the_config(tmp_path):
    """Config-to-mod matching must not depend on mapping to folders.

    Both options here install no folder of their own, so the selection
    maps to nothing. That is still a match: the config is the right one,
    and there is simply nothing to skip. The old code treated "nothing
    mapped" as "wrong config" and pushed the selection into the fuzzy
    fallback.
    """
    write_config(tmp_path / "ModuleConfig.xml", """
    <config>
      <installSteps><installStep name=""><optionalFileGroups>
        <group name="G" type="SelectExactlyOne">
          <plugins>
            <plugin name="On"><conditionFlags><flag name="extra">yes</flag></conditionFlags></plugin>
            <plugin name="Off"><conditionFlags><flag name="extra">no</flag></conditionFlags></plugin>
          </plugins>
        </group>
      </optionalFileGroups></installStep></installSteps>
    </config>
    """)
    # A numbered folder the fuzzy fallback would otherwise skip.
    (tmp_path / "01 Unrelated").mkdir()

    skip = build_fomod_skip_set(tmp_path, {1: choices("On")})
    assert skip == set(), (
        "the config matched, so the fallback must not run; got " f"{skip}"
    )
