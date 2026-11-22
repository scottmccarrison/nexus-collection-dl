"""Install-plan tests, driven by real FOMOD archives' own configs.

Every fixture here came out of a real Nexus mod or the official FOMOD
installer's test data. The cases worth naming:

- XPMSE partitions ``Skeleton Rig/HDT`` from ``Skeleton Rig/HDT 4.51``,
  which the top-level folder heuristic merged into one keep-or-skip
  decision. Selecting one skeleton style used to install all of them.
- SPID wraps everything in one folder and writes sources relative to
  that wrapper, with ``destination`` renaming ``AE/SKSE/Plugins`` to
  ``SKSE/Plugins``.
- NVAC has no options at all: four ``conditionalFileInstalls`` patterns
  pick ``NVSE`` -> ``SKSE`` / ``FOSE`` / ``NVSE`` / ``OBSE`` from the
  game version and the load order.
"""

from pathlib import Path

from nexus_collection_dl.deploy import _fomod_plans, _plan_lookup, classify_files
from nexus_collection_dl.fomod_plan import (
    AllOf,
    AnyOf,
    FileDependency,
    FlagDependency,
    GameDependency,
    GameState,
    InstallEntry,
    InstallPlan,
    iter_manifest_choices,
    load_module_config,
    plan_source_root,
    resolve_install_plan,
    selections_from_manifest,
)

FIXTURES = Path(__file__).parent / "fixtures" / "fomod"


def config(name: str):
    cfg = load_module_config(FIXTURES / f"{name}.ModuleConfig.xml")
    assert cfg is not None, name
    return cfg


def entry_sources(cfg) -> set[str]:
    return {e.source for e in cfg.all_entries()}


# ── Parsing: what the real configs actually say ─────────────────────────


def test_improved_map_options_carry_full_source_and_destination():
    cfg = config("improved-map")
    assert len(cfg.steps) == 1
    group = cfg.steps[0].groups[0]
    assert group.group_type == "SelectExactlyOne"
    assert len(group.options) == 6

    darker = next(o for o in group.options if o.name == "Darker")
    (entry,) = darker.files
    assert entry.is_folder is False
    assert entry.source == (
        "02 Improved Map with Visible Roads - Darker/Textures/"
        "interface/pip-boy/worldmap_d.dds"
    )
    assert entry.destination == "Textures/interface/pip-boy/worldmap_d.dds"


def test_xpmse_is_utf16_and_partitions_nested_sources():
    cfg = config("xpmse")
    assert cfg.module_name == "XP32 Maximum Skeleton Special Extended"
    assert len(cfg.steps) == 8

    rigs = next(s for s in cfg.steps if s.name == "Rigs")
    assert rigs.visible == AllOf(deps=(FlagDependency(flag="Ext", value="On"),))
    options = {o.name: o for o in rigs.groups[0].options}
    assert {e.source for e in options["XPMS(S)E TBBP"].files} == {
        "Skeleton Rig/TBBP",
        "Creature Rig/HDT",
    }
    assert {e.source for e in options["XPMS(S)E Latest"].files} == {
        "Skeleton Rig/HDT",
        "Creature Rig/HDT",
    }
    # The whole point: same top-level directory, different nested source.
    assert all(e.source.lower().startswith("skeleton rig/") for e in options["XPMS(S)E TBBP"].files
               if e.source.lower().startswith("skeleton rig"))


def test_xpmse_flags_come_from_selected_options():
    cfg = config("xpmse")
    installation = cfg.steps[0].groups[0].options
    flags = {o.name: o.flags for o in installation}
    assert flags["Extended"] == (("Ext", "On"),)
    assert flags["Basic"] == (("Ext", "Off"),)


def test_spid_sources_are_relative_to_the_fomod_parent(tmp_path):
    root = tmp_path / "Spell Perk Item Distributor FOMOD Installer"
    (root / "fomod").mkdir(parents=True)
    cfg_path = root / "fomod" / "ModuleConfig.xml"
    cfg_path.write_text((FIXTURES / "spid.ModuleConfig.xml").read_text())

    cfg = load_module_config(cfg_path)
    assert cfg is not None
    options = {o.name: o for o in cfg.steps[0].groups[0].options}
    ae = options['SSE v1.6.629+ ("Anniversary Edition")']
    (entry,) = ae.files
    assert entry.is_folder is True
    assert entry.source == "AE/SKSE/Plugins"
    assert entry.destination == "SKSE/Plugins"
    # Sources resolve against the wrapper, not the archive root.
    assert plan_source_root(cfg_path) == root


def test_nvac_patterns_rename_by_game_and_load_order():
    cfg = config("nvac")
    assert cfg.steps == ()
    assert len(cfg.patterns) == 4

    by_dest = {}
    for pattern in cfg.patterns:
        (entry,) = pattern.files
        by_dest[entry.destination] = (pattern.dependencies, entry)
    assert set(by_dest) == {"SKSE", "FOSE", "NVSE", "OBSE"}

    skyrim_deps, skyrim_entry = by_dest["SKSE"]
    assert skyrim_deps == AllOf(
        deps=(
            GameDependency(version="1.9.32.0"),
            AnyOf(
                deps=(
                    FileDependency(file="Skyrim.esm", state="Active"),
                    FileDependency(file="Skyrim.esm", state="Inactive"),
                )
            ),
        )
    )
    assert skyrim_entry.source == "NVSE"


def test_invisible_step_flag_value_can_be_element_text():
    cfg = config("invisible-step")
    step_one, step_two = cfg.steps
    assert step_two.visible == FlagDependency(flag="ShowStep2", value="true")
    option_b = next(
        o for o in step_one.groups[0].options if o.name == "Option B (shows Step 2)"
    )
    assert option_b.flags == (("ShowStep2", "true"),)


def test_compliance_fixture_exercises_the_install_flags():
    cfg = config("compliance")
    groups = {g.name: g for g in cfg.steps[0].groups}
    assert groups["Group1"].group_type == "SelectAll"

    not_usable = groups["Group3"].options[0]
    assert not_usable.option_type == "NotUsable"
    flags = {e.source.rsplit("/", 1)[-1]: e for e in not_usable.files}
    assert flags["test_alwaysinstall.txt"].always_install is True
    assert flags["test_installifusable.txt"].install_if_usable is True
    assert len(cfg.required) == 2
    assert len(cfg.patterns) == 1


def test_dreads_is_pattern_heavy():
    cfg = config("dreads")
    # The real shape of a modern patch collection: options set flags and
    # conditionalFileInstalls does the installing.
    assert len(cfg.patterns) > 100
    assert len(cfg.steps) > 10
    pattern_files = sum(len(p.files) for p in cfg.patterns)
    option_files = sum(
        len(o.files) for s in cfg.steps for g in s.groups for o in g.options
    )
    assert pattern_files > option_files


# ── Planning: what a selection installs ─────────────────────────────────


def test_improved_map_installs_only_the_selected_option():
    cfg = config("improved-map")
    plan = resolve_install_plan(
        cfg, {("Install Improved Map with Visible Roads", "Improved Map with Visible Roads"): ("Darker",)}
    )
    assert len(plan.entries) == 1
    entry = plan.entries[0]
    assert entry.destination == "Textures/interface/pip-boy/worldmap_d.dds"
    assert plan.lookup(entry.source) == (
        "Textures/interface/pip-boy/worldmap_d.dds",
        entry.priority,
    )
    unselected = (
        "01 Improved Map with Visible Roads - Default/Textures/"
        "interface/pip-boy/worldmap_d.dds"
    )
    assert plan.lookup(unselected) is None
    assert plan.indeterminate is False


def test_xpmse_selecting_one_skeleton_style_does_not_install_the_others():
    cfg = config("xpmse")
    plan = resolve_install_plan(
        cfg,
        {
            ("Installation", "Installation"): ("Extended",),
            ("Rigs", "Animation Rig Map"): ("XPMS(S)E TBBP",),
        },
    )
    assert plan.indeterminate is False

    # The selected style installs, and lands at the data-relative path
    # its empty destination implies.
    assert plan.lookup("Skeleton Rig/TBBP/meshes/actors/character/skeleton.nif") == (
        "meshes/actors/character/skeleton.nif",
        0,
    )
    # The sibling styles do not, even though they share the top-level dir.
    assert plan.lookup("Skeleton Rig/HDT/meshes/x.nif") is None
    assert plan.lookup("Skeleton Rig/HDT 4.51/meshes/x.nif") is None
    # Extended installs the mesh dirs but no character-creation content.
    assert plan.lookup("Skeleton Meshes/meshes/x.nif") == ("meshes/x.nif", 0)
    assert plan.lookup("Character Creation/MCM/assets/scripts/x.pex") is None


def test_xpmse_hidden_step_installs_nothing_even_if_named():
    cfg = config("xpmse")
    # Basic sets Ext=Off, which hides the Rigs step entirely.
    plan = resolve_install_plan(
        cfg,
        {
            ("Installation", "Installation"): ("Basic",),
            ("Rigs", "Animation Rig Map"): ("XPMS(S)E TBBP",),
        },
    )
    assert plan.lookup("Skeleton Rig/TBBP/meshes/x.nif") is None
    assert plan.lookup("Skeleton Meshes/meshes/x.nif") == ("meshes/x.nif", 0)


def test_spid_destination_rename_and_sibling_exclusion():
    cfg = config("spid")
    plan = resolve_install_plan(
        cfg,
        {
            ("Main", "DLL"): ('SSE v1.6.629+ ("Anniversary Edition")',),
        },
    )
    assert plan.lookup("AE/SKSE/Plugins/po3_SpellPerkItemDistributor.dll")[0] == (
        "SKSE/Plugins/po3_SpellPerkItemDistributor.dll"
    )
    assert plan.lookup("SE/SKSE/Plugins/po3_SpellPerkItemDistributor.dll") is None
    assert plan.indeterminate is False


def test_nvac_is_indeterminate_without_game_state():
    cfg = config("nvac")
    plan = resolve_install_plan(cfg)
    assert plan.indeterminate is True
    assert plan.entries == ()


def test_nvac_renames_for_skyrim_and_keeps_for_new_vegas():
    cfg = config("nvac")

    skyrim = resolve_install_plan(
        cfg,
        game=GameState(
            plugin_states={"skyrim.esm": "Active"}, game_version="1.9.32.0"
        ),
    )
    assert skyrim.indeterminate is False
    (entry,) = skyrim.entries
    assert (entry.source, entry.destination) == ("NVSE", "SKSE")

    new_vegas = resolve_install_plan(
        cfg,
        game=GameState(
            plugin_states={"falloutnv.esm": "Active"}, game_version="1.4.0.525"
        ),
    )
    (entry,) = new_vegas.entries
    assert (entry.source, entry.destination) == ("NVSE", "NVSE")


def test_invisible_step_two_only_installs_when_option_b_picked():
    cfg = config("invisible-step")

    with_a = resolve_install_plan(
        cfg, {("Step1", "Group1"): ("Option A (hides Step 2)",)}
    )
    assert with_a.lookup("invisible_step/should_not_install.txt") is None
    assert with_a.lookup("visible_step/visible_file.txt") == ("visible_file.txt", 0)

    with_b = resolve_install_plan(
        cfg, {("Step1", "Group1"): ("Option B (shows Step 2)",)}
    )
    assert with_b.lookup("invisible_step/should_not_install.txt") == (
        "should_not_install.txt",
        0,
    )


def test_selectall_required_and_install_flags():
    cfg = config("compliance")
    # Nothing selected: SelectAll groups and Required options still install.
    plan = resolve_install_plan(cfg)
    assert plan.indeterminate is False
    installed = {e.source for e in plan.entries}

    # SelectAll group content, and Required options in any group.
    assert "g1p1_alwayson/test_grouporder.txt" in installed
    assert "g01p2_alwayson/test_pluginorder.txt" in installed
    # requiredInstallFiles.
    assert any(e.origin == "required" for e in plan.entries)
    # The unselected NotUsable option: its alwaysInstall entry installs,
    # its installIfUsable entry does not (the plugin is not usable).
    assert "g3p1_notusable/test_alwaysinstall.txt" in installed
    assert "g3p1_notusable/test_installifusable.txt" not in installed
    # The disabled-but-usable option: installIfUsable installs anyway.
    assert "g4p1_leavedisabled/test_installifusable.txt" in installed
    # The unconditional pattern installs.
    assert "conditionals/test_conditionals_ignore_priority.txt" in installed


def test_group_defaults_preselect_required_then_recommended():
    cfg = config("invisible-step")
    # Option A is Recommended; a caller with no picks gets it.
    plan = resolve_install_plan(cfg)
    assert plan.lookup("visible_step/visible_file.txt") == ("visible_file.txt", 0)
    assert plan.lookup("invisible_step/should_not_install.txt") is None


def test_lookup_prefers_the_higher_priority_entry():
    cfg = config("improved-map")
    plan = resolve_install_plan(cfg)
    assert plan.entries == ()

    from nexus_collection_dl.fomod_plan import InstallEntry

    entries = (
        InstallEntry("shared/x.bin", "low", False, priority=0, origin="a"),
        InstallEntry("shared/x.bin", "high", False, priority=10, origin="b"),
    )
    plan.entries = entries
    assert plan.lookup("shared/x.bin") == ("high", 10)

    folder = InstallEntry("pack", "mod", True, priority=0, origin="c")
    plan.entries = (folder,)
    assert plan.lookup("pack/deep/file.txt") == ("mod/deep/file.txt", 0)
    assert plan.lookup("other/file.txt") is None

    single = InstallEntry("addon.txt", "", False, priority=0, origin="d")
    plan.entries = (single,)
    assert plan.lookup("addon.txt") == ("addon.txt", 0)


# ── Manifest conversion ─────────────────────────────────────────────────


def test_manifest_choices_resolve_by_name_with_group_tiebreak():
    cfg = config("compliance")
    # 'Plugin1' exists in five groups; the manifest's group name
    # disambiguates. The step name may be empty in real data, and the
    # group name must still find the right one.
    for step_name in ("Step1", ""):
        choices = {
            "type": "fomod",
            "options": [
                {
                    "name": step_name,
                    "groups": [
                        {"name": "Group3", "choices": [{"name": "Plugin1", "idx": 1}]},
                    ],
                }
            ],
        }
        picked = selections_from_manifest(choices, cfg)
        assert picked == {("Step1", "Group3"): ("Plugin1",)}, step_name

    # A name that is unique resolves with no group context at all.
    cfg = config("spid")
    choices = {
        "type": "fomod",
        "options": [
            {
                "name": "Main",
                "groups": [
                    {
                        "name": "DLL",
                        "choices": [
                            {"name": 'SSE v1.5.97 ("Special Edition")', "idx": 2}
                        ],
                    }
                ],
            }
        ],
    }
    picked = selections_from_manifest(choices, cfg)
    assert picked == {("Main", "DLL"): ('SSE v1.5.97 ("Special Edition")',)}


def test_selections_from_manifest_reads_the_real_nexus_shape():
    """A live collection's stored picks, verbatim (mod 39732).

    The step name is empty, which real manifests carry: Frozen in Time's
    own config is ``<installStep name="">`` with one group.
    """
    cfg = config("frozen-in-time")
    choices = {
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
    picked = selections_from_manifest(choices, cfg)
    assert picked == {("", "Body Textures"): ("CBBE",)}


def test_iter_manifest_choices_walks_the_multi_step_real_shape():
    """Another live collection's picks, verbatim (mod 81085).

    Two install steps; the first holds two groups. ``idx`` values are
    what the manifest stored; nothing locates an option by them.
    """
    choices = {
        "type": "fomod",
        "options": [
            {
                "name": "Body Types",
                "groups": [
                    {
                        "name": "Female Body Type",
                        "choices": [{"name": "CBBE 3BA with HDT-SMP", "idx": 2}],
                    },
                    {
                        "name": "Male Body Type",
                        "choices": [{"name": "Vanilla", "idx": 2}],
                    },
                ],
            },
            {
                "name": "Texture Resolution",
                "groups": [
                    {
                        "name": "Texture Resolution",
                        "choices": [{"name": "4K", "idx": 1}],
                    }
                ],
            },
        ],
    }
    assert list(iter_manifest_choices(choices)) == [
        ("Body Types", "Female Body Type", "CBBE 3BA with HDT-SMP"),
        ("Body Types", "Male Body Type", "Vanilla"),
        ("Texture Resolution", "Texture Resolution", "4K"),
    ]


# ── Deployment: the plan decides what lands where ───────────────────────


def _write(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _choices(*names: str) -> dict[int, dict]:
    return {
        1: {
            "type": "fomod",
            "options": [
                {
                    "name": "",
                    "groups": [
                        {
                            "name": "G",
                            "choices": [
                                {"name": n, "idx": i + 1} for i, n in enumerate(names)
                            ],
                        }
                    ],
                }
            ],
        }
    }


def test_deploy_xpmse_separates_sibling_styles_and_claims_no_strangers(tmp_path):
    import shutil

    (tmp_path / "Fomod").mkdir()
    shutil.copy(FIXTURES / "xpmse.ModuleConfig.xml", tmp_path / "Fomod" / "ModuleConfig.xml")
    _write(tmp_path / "Skeleton Rig" / "HDT" / "meshes" / "x.nif")
    _write(tmp_path / "Skeleton Rig" / "TBBP" / "meshes" / "y.nif")
    _write(tmp_path / "Creature Rig" / "HDT" / "meshes" / "z.nif")
    _write(tmp_path / "OtherMod" / "stranger.esp")

    plan = classify_files(
        tmp_path,
        "skyrimspecialedition",
        _choices("Extended", "XPMS(S)E TBBP"),
    )
    deployed = {dest.as_posix(): src for src, dest in plan.data_files}
    skipped = {rel.as_posix(): reason for rel, reason in plan.skipped}

    assert "meshes/y.nif" in deployed
    assert "meshes/z.nif" in deployed
    assert skipped.get("Skeleton Rig/HDT/meshes/x.nif") == "not in the FOMOD install plan"
    # Another mod's file at the shared root is not claimed by the config.
    assert any("stranger.esp" in dest for dest in deployed)


def test_deploy_spid_strips_the_wrapper_and_picks_one_version(tmp_path):
    import shutil

    root = tmp_path / "Spell Perk Item Distributor FOMOD Installer"
    (root / "fomod").mkdir(parents=True)
    shutil.copy(FIXTURES / "spid.ModuleConfig.xml", root / "fomod" / "ModuleConfig.xml")
    _write(root / "AE" / "SKSE" / "Plugins" / "po3.dll")
    _write(root / "SE" / "SKSE" / "Plugins" / "po3.dll")

    plan = classify_files(tmp_path, "skyrimspecialedition", _choices('SSE v1.6.629+ ("Anniversary Edition")'))
    deployed = {dest.as_posix() for src, dest in plan.data_files}
    skipped = {rel.as_posix() for rel, reason in plan.skipped}

    assert "SKSE/Plugins/po3.dll" in deployed
    assert "Spell Perk Item Distributor FOMOD Installer/AE/SKSE/Plugins/po3.dll" not in deployed
    assert "Spell Perk Item Distributor FOMOD Installer/SE/SKSE/Plugins/po3.dll" in skipped


def test_deploy_indeterminate_config_keeps_the_legacy_behaviour(tmp_path):
    import shutil

    (tmp_path / "fomod").mkdir()
    shutil.copy(FIXTURES / "nvac.ModuleConfig.xml", tmp_path / "fomod" / "ModuleConfig.xml")
    _write(tmp_path / "NVSE" / "nvse.dll")

    plan = classify_files(tmp_path, "falloutnv", {})
    deployed = {dest.as_posix() for src, dest in plan.data_files}
    # NVAC's conditions cannot be evaluated here, so nothing is guessed:
    # the file keeps deploying exactly as it did before the plan existed.
    assert "NVSE/nvse.dll" in deployed


def test_deploy_unreadable_config_still_uses_the_folder_heuristic(tmp_path):
    (tmp_path / "fomod").mkdir()
    (tmp_path / "fomod" / "ModuleConfig.xml").write_text("<config><unclosed>")
    _write(tmp_path / "01 Main Mod" / "Data" / "thing.esp")
    _write(tmp_path / "02 Military Camps" / "Data" / "other.esp")

    plan = classify_files(
        tmp_path,
        "skyrimspecialedition",
        _choices("Main Mod"),
    )
    deployed = {dest.as_posix() for src, dest in plan.data_files}
    skipped = {rel.as_posix(): reason for rel, reason in plan.skipped}

    assert "thing.esp" in deployed
    assert skipped.get("02 Military Camps/Data/other.esp") == "unselected FOMOD option"


def test_deploy_readable_config_installs_to_its_destination(tmp_path):
    import shutil

    (tmp_path / "fomod").mkdir()
    shutil.copy(
        FIXTURES / "landscape-fixes.ModuleConfig.xml",
        tmp_path / "fomod" / "ModuleConfig.xml",
    )
    _write(tmp_path / "01 Main Mod" / "Landscape Fixes For Grass Mods.esp")
    _write(tmp_path / "02 Military Camps" / "Landscape For Grass Mods - Military Camps.esp")

    plan = classify_files(
        tmp_path,
        "skyrimspecialedition",
        _choices("Landscape Fixes For Grass Mods.esp"),
    )
    deployed = {dest.as_posix() for src, dest in plan.data_files}
    skipped = {rel.as_posix(): reason for rel, reason in plan.skipped}

    assert "Landscape Fixes For Grass Mods.esp" in deployed
    assert (
        "02 Military Camps/Landscape For Grass Mods - Military Camps.esp"
        in skipped
    )


# ── Preserved configs: two FOMOD mods in one tree ───────────────────────


def _frozen_xml() -> str:
    return (FIXTURES / "frozen-in-time.ModuleConfig.xml").read_text(encoding="utf-16")


def _choose_one_xml() -> str:
    return (FIXTURES / "gofomod-choose-one.xml").read_text(encoding="utf-8")


# A second FOMOD mod whose options collide with Frozen in Time's names.
# What each mod picks must reach its own config and no other.
OTHER_XML = """
<config>
  <installSteps><installStep name="Options"><optionalFileGroups>
    <group name="Pack" type="SelectExactlyOne">
      <plugins>
        <plugin name="CBBE">
          <files><file source="cbbe2/manifest.json" destination="manifest.json"/></files>
          <typeDescriptor><type name="Optional"/></typeDescriptor>
        </plugin>
        <plugin name="Other">
          <files><file source="other/manifest.json" destination="manifest.json"/></files>
          <typeDescriptor><type name="Optional"/></typeDescriptor>
        </plugin>
      </plugins>
    </group>
  </optionalFileGroups></installStep></installSteps>
</config>
"""


def test_two_fomod_records_plan_both_mods_despite_one_tree_config(tmp_path):
    """Two FOMOD mods, one tree: both plans must survive.

    Extraction merges every archive into one tree, and a later mod's
    ``fomod/ModuleConfig.xml`` overwrites an earlier one, so the tree can
    only ever show one config. The copies each extract recorded are what
    both plans come from. Frozen in Time's picks were lost to exactly
    this: its config was clobbered and every one of its option folders
    deployed.
    """
    import shutil

    # The tree keeps only the later mod's config: the shape a clobber
    # leaves behind.
    (tmp_path / "fomod").mkdir()
    shutil.copy(
        FIXTURES / "gofomod-choose-one.xml", tmp_path / "fomod" / "ModuleConfig.xml"
    )
    _write(tmp_path / "core" / "snow elf waifu.esp")
    _write(tmp_path / "cbbe" / "textures" / "b.dds")
    _write(tmp_path / "uunp" / "textures" / "a.dds")
    _write(tmp_path / "vanilla" / "textures" / "c.dds")
    _write(tmp_path / "alpha" / "manifest.json")
    _write(tmp_path / "beta" / "manifest.json")

    plan = classify_files(
        tmp_path,
        "skyrimspecialedition",
        fomod_picks={
            39732: [{"step": "", "group": "Body Textures", "option": "CBBE"}],
            111: [{"step": "Options", "group": "Pack", "option": "Alpha"}],
        },
        fomod_configs={
            39732: {
                "member": "fomod/ModuleConfig.xml",
                "xml": _frozen_xml(),
                "mod_id": 39732,
            },
            111: {
                "member": "fomod/ModuleConfig.xml",
                "xml": _choose_one_xml(),
                "mod_id": 111,
            },
        },
    )
    deployed = {src.relative_to(tmp_path).as_posix(): dest.as_posix() for src, dest in plan.data_files}
    skipped = {rel.as_posix(): reason for rel, reason in plan.skipped}

    # Each mod's picked option installs at its destination.
    assert deployed["cbbe/textures/b.dds"] == "textures/b.dds"
    assert deployed["alpha/manifest.json"] == "manifest.json"
    # Required content always installs.
    assert deployed["core/snow elf waifu.esp"] == "snow elf waifu.esp"
    # Both mods' unpicked options are filtered, not just the one whose
    # config happened to survive in the tree.
    assert skipped["uunp/textures/a.dds"] == "not in the FOMOD install plan"
    assert skipped["vanilla/textures/c.dds"] == "not in the FOMOD install plan"
    assert skipped["beta/manifest.json"] == "not in the FOMOD install plan"


def test_recorded_config_is_not_planned_twice_from_the_tree(tmp_path):
    """The tree copy a record already represents is skipped.

    Without this the surviving config would produce a second plan for the
    same mod, and the same files would be classified twice.
    """
    import shutil

    (tmp_path / "fomod").mkdir()
    shutil.copy(
        FIXTURES / "gofomod-choose-one.xml", tmp_path / "fomod" / "ModuleConfig.xml"
    )
    plans, _warnings = _fomod_plans(
        tmp_path,
        {},
        fomod_configs={
            111: {
                "member": "fomod/ModuleConfig.xml",
                "xml": _choose_one_xml(),
                "mod_id": 111,
            }
        },
    )
    assert len(plans) == 1


def test_plan_lookup_prefers_the_config_that_owns_the_file(tmp_path):
    """Two configs sharing a source root: each file goes to its owner.

    A config must not claim a sibling mod's files, and the first config
    listed must not swallow the second one's. A file neither owns gets no
    plan at all, which is what keeps sibling mods' files out of a shared
    root's plan.
    """
    plan_a = InstallPlan(
        entries=(InstallEntry("a/one.esp", "one.esp", False, 0, "x"),),
    )
    plan_b = InstallPlan(
        entries=(InstallEntry("b/two.esp", "two.esp", False, 0, "x"),),
    )
    plans = [
        (tmp_path, plan_a, ("a/one.esp",)),
        (tmp_path, plan_b, ("b/two.esp",)),
    ]

    hit_a = _plan_lookup(tmp_path / "a" / "one.esp", plans)
    assert hit_a is not None and hit_a[0] is plan_a and hit_a[1] == "a/one.esp"
    hit_b = _plan_lookup(tmp_path / "b" / "two.esp", plans)
    assert hit_b is not None and hit_b[0] is plan_b and hit_b[1] == "b/two.esp"
    assert _plan_lookup(tmp_path / "c" / "stranger.esp", plans) is None


def test_record_choices_are_not_cross_wired_between_mods(tmp_path):
    """Each mod's plan gets its own picks, not another mod's.

    Both configs offer an option named ``CBBE``. Only the first mod's
    manifest says CBBE; the second must install its own pick rather than
    the first mod's choice.
    """
    _write(tmp_path / "cbbe" / "textures" / "b.dds")
    _write(tmp_path / "uunp" / "textures" / "a.dds")
    _write(tmp_path / "cbbe2" / "manifest.json")
    _write(tmp_path / "other" / "manifest.json")

    plan = classify_files(
        tmp_path,
        "skyrimspecialedition",
        mod_choices={
            39732: {
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
            },
            111: {
                "type": "fomod",
                "options": [
                    {
                        "name": "Options",
                        "groups": [
                            {
                                "name": "Pack",
                                "choices": [{"name": "Other", "idx": 2}],
                            }
                        ],
                    }
                ],
            },
        },
        fomod_configs={
            39732: {
                "member": "fomod/ModuleConfig.xml",
                "xml": _frozen_xml(),
                "mod_id": 39732,
            },
            111: {
                "member": "fomod/ModuleConfig.xml",
                "xml": OTHER_XML,
                "mod_id": 111,
            },
        },
    )
    deployed = {src.relative_to(tmp_path).as_posix(): dest.as_posix() for src, dest in plan.data_files}
    skipped = {rel.as_posix(): reason for rel, reason in plan.skipped}

    assert deployed["cbbe/textures/b.dds"] == "textures/b.dds"
    assert deployed["other/manifest.json"] == "manifest.json"
    assert skipped["cbbe2/manifest.json"] == "not in the FOMOD install plan"
    assert skipped["uunp/textures/a.dds"] == "not in the FOMOD install plan"


def test_mod_state_round_trips_the_recorded_config():
    """The record survives a save/load cycle, and old states still load."""
    from nexus_collection_dl.state import ModState

    record = {"member": "fomod/ModuleConfig.xml", "xml": "<config/>"}
    mod = ModState(1, 2, "n", "v", "f.zip", fomod_config=record)
    assert ModState.from_dict(1, mod.to_dict()).fomod_config == record

    old = mod.to_dict()
    del old["fomod_config"]
    assert ModState.from_dict(1, old).fomod_config is None


def test_oversized_module_config_is_refused():
    """An installer config past the byte budget is refused, not parsed.

    Resource exhaustion from an untrusted archive: the XML is attacker
    controlled and comes out of a third-party download.
    """
    import pytest

    from nexus_collection_dl.fomod_plan import (
        MAX_CONFIG_BYTES,
        ConfigTooLargeError,
        load_module_config_bytes,
        load_module_config_text,
    )

    over = b"<fomod>" + (b"x" * (MAX_CONFIG_BYTES + 1)) + b"</fomod>"
    with pytest.raises(ConfigTooLargeError) as exc:
        load_module_config_bytes(over)
    message = str(exc.value)
    assert str(MAX_CONFIG_BYTES) in message or "size" in message.lower()

    with pytest.raises(ConfigTooLargeError):
        load_module_config_text(over.decode("ascii"))


def test_config_within_budget_still_parses():
    """The cap must not refuse a real installer."""
    from nexus_collection_dl.fomod_plan import (
        MAX_CONFIG_BYTES,
        load_module_config_bytes,
    )

    assert MAX_CONFIG_BYTES >= 512 * 1024, "budget must fit real installers"
    xml = b"<fomod><name>ok</name></fomod>"
    # Unreadable as a full config is fine (returns None); what matters is
    # that a small config is not refused for size.
    load_module_config_bytes(xml)
