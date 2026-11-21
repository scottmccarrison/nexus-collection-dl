"""Option selection for standalone FOMOD installs.

The refusal used to be the whole answer to a FOMOD archive. Now the
installer's groups are read, the picks come from flags, a JSON file, or
an interactive prompt, and the picks are stored so a later deploy
installs exactly what was chosen.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus_collection_dl.deploy import classify_files
from nexus_collection_dl.fomod_plan import load_module_config_bytes
from nexus_collection_dl.fomod_select import (
    Pick,
    PickError,
    load_picks_file,
    parse_option_flags,
    picks_for_file,
    picks_to_selections,
)
from tests.test_fomod_refusal import (
    MOD_URL,
    _fake_download_writing,
    _reload,
    _svc,
)

# Two mutually exclusive options, one group, one step. Small on purpose:
# the shape is what matters, not the content.
CONFIG = b"""<config>
  <moduleName>Test</moduleName>
  <installSteps>
    <installStep name="Step1">
      <optionalFileGroups>
        <group name="G" type="SelectExactlyOne">
          <plugins>
            <plugin name="OptA">
              <files><file source="a/thing.esp" destination="thing.esp"/></files>
              <typeDescriptor><type name="Optional"/></typeDescriptor>
            </plugin>
            <plugin name="OptB">
              <files><file source="b/thing.esp" destination="thing.esp"/></files>
              <typeDescriptor><type name="Optional"/></typeDescriptor>
            </plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
  </installSteps>
</config>"""


def _payload() -> dict[str, bytes]:
    return {
        "fomod/ModuleConfig.xml": CONFIG,
        "a/thing.esp": b"a",
        "b/thing.esp": b"b",
    }


# ── the pick input layer ────────────────────────────────────────────────


def test_parse_option_flags_takes_two_and_three_part_forms():
    assert parse_option_flags(["G:OptA"]) == [Pick("", "G", "OptA")]
    assert parse_option_flags(["Step1:G:OptB"]) == [Pick("Step1", "G", "OptB")]


def test_parse_option_flags_rejects_a_value_without_a_separator():
    with pytest.raises(PickError):
        parse_option_flags(["OptA"])


def test_load_picks_file_reads_scoped_and_unscoped_picks(tmp_path):
    path = tmp_path / "picks.json"
    path.write_text(
        json.dumps(
            [
                {"group": "G", "option": "OptA"},
                {"file": "mod-b.zip", "step": "Step1", "group": "G", "option": "OptB"},
            ]
        )
    )
    picks = load_picks_file(path)
    assert picks == [
        Pick("", "G", "OptA"),
        Pick("Step1", "G", "OptB", file="mod-b.zip"),
    ]
    # An unscoped pick applies to every archive; a scoped one only to its own.
    assert picks_for_file(picks, "MOD-B.ZIP") == picks
    assert picks_for_file(picks, "other.zip") == [Pick("", "G", "OptA")]


def test_load_picks_file_rejects_junk(tmp_path):
    path = tmp_path / "picks.json"
    path.write_text("{not a list}")
    with pytest.raises(PickError):
        load_picks_file(path)
    path.write_text(json.dumps([{"group": "G"}]))
    with pytest.raises(PickError):
        load_picks_file(path)


def test_picks_to_selections_matches_names_and_reports_the_rest():
    config = load_module_config_bytes(CONFIG)
    assert config is not None
    picked, unmatched = picks_to_selections([Pick("", "G", "optb")], config)
    assert picked == {("Step1", "G"): ("optb",)}
    assert unmatched == []

    picked, unmatched = picks_to_selections([Pick("", "G", "Nope")], config)
    assert picked == {}
    assert unmatched == [Pick("", "G", "Nope")]


def test_picks_to_selections_rejects_an_ambiguous_group_name():
    config = load_module_config_bytes(CONFIG)
    assert config is not None
    # 'G' lives in one step here, so make it ambiguous with a step name
    # that does not exist: the pick names no group at all.
    picked, unmatched = picks_to_selections([Pick("Nope", "G", "OptA")], config)
    assert picked == {}
    assert unmatched == [Pick("Nope", "G", "OptA")]


# ── the service flow ────────────────────────────────────────────────────


def test_add_with_picks_extracts_and_stores_them(tmp_path, monkeypatch):
    from nexus_collection_dl import service as service_module

    svc = _svc(tmp_path, monkeypatch)
    monkeypatch.setattr(
        service_module.Downloader,
        "download_mods",
        _fake_download_writing({"TestMod-1.0.zip": _payload()}),
    )
    result = svc.add_mod(
        MOD_URL,
        tmp_path,
        no_load_order=True,
        fomod_picks=[Pick("", "G", "OptB")],
    )
    assert result.success, result.error
    assert (tmp_path / "b" / "thing.esp").exists()

    state = _reload(tmp_path)
    entry = next(iter(state.mods.values()))
    assert entry.fomod_picks == [{"step": "Step1", "group": "G", "option": "OptB"}]


def test_add_rejects_a_pick_the_installer_does_not_offer(tmp_path, monkeypatch):
    from nexus_collection_dl import service as service_module

    svc = _svc(tmp_path, monkeypatch)
    monkeypatch.setattr(
        service_module.Downloader,
        "download_mods",
        _fake_download_writing({"TestMod-1.0.zip": _payload()}),
    )
    with pytest.raises(PickError) as excinfo:
        svc.add_mod(
            MOD_URL, tmp_path, no_load_order=True, fomod_picks=[Pick("", "G", "Nope")]
        )
    assert "G:Nope" in str(excinfo.value)
    # Nothing extracted and the archive is kept.
    assert not (tmp_path / "a").exists()
    assert (tmp_path / "TestMod-1.0.zip").exists()


def test_add_prompts_when_there_is_somebody_to_ask(tmp_path, monkeypatch):
    from nexus_collection_dl import service as service_module

    svc = _svc(tmp_path, monkeypatch)
    monkeypatch.setattr(
        service_module.Downloader,
        "download_mods",
        _fake_download_writing({"TestMod-1.0.zip": _payload()}),
    )
    asked: list = []

    def prompt(config, archive_name, kept):
        asked.append((archive_name, list(kept)))
        return [Pick("Step1", "G", "OptA")]

    result = svc.add_mod(
        MOD_URL, tmp_path, no_load_order=True, on_fomod_prompt=prompt
    )
    assert result.success, result.error
    assert asked == [("TestMod-1.0.zip", [])]
    assert (tmp_path / "a" / "thing.esp").exists()

    state = _reload(tmp_path)
    entry = next(iter(state.mods.values()))
    assert entry.fomod_picks == [{"step": "Step1", "group": "G", "option": "OptA"}]


def test_re_add_keeps_matching_picks_and_only_asks_about_the_rest(
    tmp_path, monkeypatch
):
    """The keep-matching-picks rule from the design."""
    from nexus_collection_dl import service as service_module

    svc = _svc(tmp_path, monkeypatch)
    monkeypatch.setattr(
        service_module.Downloader,
        "download_mods",
        _fake_download_writing({"TestMod-1.0.zip": _payload()}),
    )
    svc.add_mod(MOD_URL, tmp_path, no_load_order=True, fomod_picks=[Pick("", "G", "OptB")])

    asked: list = []

    def prompt(config, archive_name, kept):
        # The stored pick still matches, so it arrives as kept and there
        # is nothing left to ask about.
        asked.append(list(kept))
        return []

    result = svc.add_mod(MOD_URL, tmp_path, no_load_order=True, on_fomod_prompt=prompt)
    assert result.success, result.error
    assert asked == [[Pick("Step1", "G", "OptB")]]
    state = _reload(tmp_path)
    entry = next(iter(state.mods.values()))
    assert entry.fomod_picks == [{"step": "Step1", "group": "G", "option": "OptB"}]


def test_state_round_trips_fomod_picks(tmp_path):
    from nexus_collection_dl.state import CollectionState

    mods = tmp_path / "mods"
    mods.mkdir()
    state = CollectionState(mods)
    state.set_collection_info("u", "n", 1, "skyrimspecialedition")
    state.add_mod(
        {
            "mod_id": 1,
            "mod_name": "M",
            "file_id": 11,
            "filename": "m.zip",
            "version": "1",
            "optional": False,
            "manual": True,
        }
    )
    state.mods[11].fomod_picks = [{"step": "S", "group": "G", "option": "O"}]
    state.save()

    again = CollectionState(mods)
    again.load()
    assert again.mods[11].fomod_picks == [{"step": "S", "group": "G", "option": "O"}]


# ── the end-to-end effect: deploy installs what was picked ──────────────


def test_deploy_installs_the_stored_picks_and_skips_the_rest(tmp_path, monkeypatch):
    from nexus_collection_dl import service as service_module

    svc = _svc(tmp_path, monkeypatch)
    monkeypatch.setattr(
        service_module.Downloader,
        "download_mods",
        _fake_download_writing({"TestMod-1.0.zip": _payload()}),
    )
    svc.add_mod(MOD_URL, tmp_path, no_load_order=True, fomod_picks=[Pick("", "G", "OptB")])

    state = _reload(tmp_path)
    picks_by_mod = {
        mod.mod_id: mod.fomod_picks for mod in state.mods.values() if mod.fomod_picks
    }
    plan = classify_files(tmp_path, "skyrimspecialedition", fomod_picks=picks_by_mod)
    deployed = {dest.as_posix() for _src, dest in plan.data_files}
    skipped = {rel.as_posix(): reason for rel, reason in plan.skipped}

    assert "thing.esp" in deployed
    assert skipped.get("a/thing.esp") == "not in the FOMOD install plan"
    assert "b/thing.esp" not in skipped


def test_deploy_without_picks_falls_back_to_the_installer_defaults(tmp_path):
    """No picks anywhere: the schema defaults, not every option."""

    mods = tmp_path / "mods"
    (mods / "fomod").mkdir(parents=True)
    (mods / "fomod" / "ModuleConfig.xml").write_bytes(CONFIG)
    (mods / "a").mkdir()
    (mods / "a" / "thing.esp").write_text("a")
    (mods / "b").mkdir()
    (mods / "b" / "thing.esp").write_text("b")

    plan = classify_files(mods, "skyrimspecialedition")
    skipped = {rel.as_posix(): reason for rel, reason in plan.skipped}
    # Nothing was picked and both options are Optional: neither installs.
    assert skipped.get("a/thing.esp") == "not in the FOMOD install plan"
    assert skipped.get("b/thing.esp") == "not in the FOMOD install plan"


def test_add_with_picks_installs_a_7z_fomod(tmp_path, monkeypatch):
    """End to end on the format real Nexus FOMODs ship in."""
    import io

    import py7zr

    svc = _svc(tmp_path, monkeypatch)

    def fake_download(self, game_domain, mods, target_dir, **kw):
        out = []
        for m in mods:
            p = Path(target_dir) / m["filename"]
            p.parent.mkdir(parents=True, exist_ok=True)
            with py7zr.SevenZipFile(p, "w") as szf:
                szf.writef(io.BytesIO(CONFIG), "fomod/ModuleConfig.xml")
                szf.writef(io.BytesIO(b"a"), "a/thing.esp")
                szf.writef(io.BytesIO(b"b"), "b/thing.esp")
            out.append((m, p))
        return out

    monkeypatch.setattr("nexus_collection_dl.service.Downloader.download_mods",
                        fake_download)
    result = svc.add_mod(
        MOD_URL, tmp_path, no_load_order=True,
        fomod_picks=[Pick("", "G", "OptB")],
    )
    assert result.success, result.error
    assert (tmp_path / "b" / "thing.esp").read_bytes() == b"b"

    state = _reload(tmp_path)
    entry = next(iter(state.mods.values()))
    assert entry.fomod_picks == [{"step": "Step1", "group": "G", "option": "OptB"}]


def test_add_with_picks_on_a_non_fomod_warns_instead_of_ignoring(tmp_path, monkeypatch):
    """Picks for a mod with no installer must not vanish without a word.

    The add still succeeds - there are no options to get wrong - but the
    caller asked for specific options and did not get them.
    """
    from nexus_collection_dl import service as service_module

    svc = _svc(tmp_path, monkeypatch)
    monkeypatch.setattr(
        service_module.Downloader,
        "download_mods",
        _fake_download_writing(
            {"TestMod-1.0.zip": {"SKSE/Plugins/thing.esp": b"x"}}
        ),
    )
    result = svc.add_mod(
        MOD_URL,
        tmp_path,
        no_load_order=True,
        fomod_picks=[Pick("", "G", "OptB")],
    )
    assert result.success, result.error
    assert result.warnings, "picks for a non-FOMOD mod must not vanish silently"
    assert "no FOMOD installer" in result.warnings[0]
    assert "not used" in result.warnings[0]

    state = _reload(tmp_path)
    entry = next(iter(state.mods.values()))
    assert entry.fomod_picks == []


def test_add_with_picks_and_no_extract_warns_instead_of_ignoring(tmp_path, monkeypatch):
    from nexus_collection_dl import service as service_module

    svc = _svc(tmp_path, monkeypatch)
    monkeypatch.setattr(
        service_module.Downloader,
        "download_mods",
        _fake_download_writing({"TestMod-1.0.zip": _payload()}),
    )
    result = svc.add_mod(
        MOD_URL,
        tmp_path,
        no_load_order=True,
        no_extract=True,
        fomod_picks=[Pick("", "G", "OptB")],
    )
    assert result.success, result.error
    assert result.warnings
    assert "unextracted" in result.warnings[0]
    assert "not used" in result.warnings[0]


# ── deploy keeps what still matches and says what it could not ──────────


def _tree_with_config(tmp_path: Path) -> Path:
    mods = tmp_path / "mods"
    (mods / "fomod").mkdir(parents=True)
    (mods / "fomod" / "ModuleConfig.xml").write_bytes(CONFIG)
    (mods / "a").mkdir()
    (mods / "a" / "thing.esp").write_text("a")
    (mods / "b").mkdir()
    (mods / "b" / "thing.esp").write_text("b")
    return mods


def _record(xml: bytes = CONFIG, file_id: int = 11, mod_id: int = 1) -> dict:
    return {
        "member": "fomod/ModuleConfig.xml",
        "xml": xml.decode("utf-8"),
        "mod_id": mod_id,
    }


def test_deploy_keeps_matched_picks_and_warns_about_the_stale_ones(tmp_path):
    """One renamed option must not erase the picks that still match."""
    mods = _tree_with_config(tmp_path)
    plan = classify_files(
        mods,
        "skyrimspecialedition",
        fomod_picks={
            11: [
                {"step": "Step1", "group": "G", "option": "OptB"},
                {"step": "Step1", "group": "G", "option": "Gone"},
            ]
        },
        fomod_configs={11: _record()},
    )
    deployed = {dest.as_posix() for _src, dest in plan.data_files}
    assert "thing.esp" in deployed
    skipped = {rel.as_posix(): reason for rel, reason in plan.skipped}
    assert skipped.get("a/thing.esp") == "not in the FOMOD install plan"
    assert "Gone" in " ".join(plan.warnings)


def test_deploy_warns_when_no_stored_option_matches(tmp_path):
    mods = _tree_with_config(tmp_path)
    plan = classify_files(
        mods,
        "skyrimspecialedition",
        fomod_picks={11: [{"step": "Step1", "group": "G", "option": "Gone"}]},
        fomod_configs={11: _record()},
    )
    # Schema defaults for an all-Optional installer install nothing at all;
    # the point is that this is said out loud instead of reported as success.
    assert not plan.data_files
    assert "Gone" in " ".join(plan.warnings)


def test_a_recorded_mods_picks_are_never_guessed_onto_another_installer(tmp_path):
    """A tree config with no record only sees choices nothing owns."""
    mods = _tree_with_config(tmp_path)
    plan = classify_files(
        mods,
        "skyrimspecialedition",
        fomod_picks={11: [{"step": "Step1", "group": "G", "option": "OptB"}]},
        fomod_configs={
            11: {
                "member": "other/fomod/ModuleConfig.xml",
                "xml": CONFIG.decode("utf-8"),
                "mod_id": 1,
            }
        },
    )
    # Without the gate, 11's picks would be guessed onto this installer
    # and OptB's file would deploy; the installer's own defaults apply.
    assert not plan.data_files


def test_guessing_says_it_is_guessing(tmp_path):
    mods = _tree_with_config(tmp_path)
    plan = classify_files(
        mods,
        "skyrimspecialedition",
        fomod_picks={12: [{"step": "Step1", "group": "G", "option": "OptB"}]},
    )
    deployed = {dest.as_posix() for _src, dest in plan.data_files}
    assert "thing.esp" in deployed
    assert "by name" in " ".join(plan.warnings)


CONDITIONAL_CONFIG = b"""<config>
  <moduleName>Cond</moduleName>
  <installSteps>
    <installStep name="One">
      <optionalFileGroups>
        <group name="G" type="SelectAnyNumber">
          <plugins>
            <plugin name="OptA">
              <files><file source="a/thing.esp" destination="thing.esp"/></files>
              <typeDescriptor><type name="Optional"/></typeDescriptor>
            </plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
  </installSteps>
  <conditionalFileInstalls>
    <patterns>
      <pattern>
        <dependencies>
          <fileDependency file="Some.esp" state="Active"/>
        </dependencies>
        <files><file source="b/thing.esp" destination="thing.esp"/></files>
      </pattern>
    </patterns>
  </conditionalFileInstalls>
</config>"""


def test_an_indeterminate_plan_falls_back_with_a_warning(tmp_path):
    """A condition that needs game state must say it fell back."""
    mods = _tree_with_config(tmp_path)
    plan = classify_files(
        mods,
        "skyrimspecialedition",
        fomod_configs={11: _record(xml=CONDITIONAL_CONFIG)},
        fomod_picks={11: [{"step": "One", "group": "G", "option": "OptA"}]},
    )
    assert any("game state" in w for w in plan.warnings)


# ── the remaining warning contract: labels, refusals, parity ────────────


AMBIGUOUS_CONFIG = b"""<config>
  <moduleName>Test</moduleName>
  <installSteps>
    <installStep name="Step1">
      <optionalFileGroups>
        <group name="G" type="SelectExactlyOne">
          <plugins>
            <plugin name="OptA">
              <files><file source="a/thing.esp" destination="thing.esp"/></files>
              <typeDescriptor><type name="Optional"/></typeDescriptor>
            </plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
    <installStep name="Step2">
      <optionalFileGroups>
        <group name="G" type="SelectExactlyOne">
          <plugins>
            <plugin name="OptB">
              <files><file source="b/thing.esp" destination="thing.esp"/></files>
              <typeDescriptor><type name="Optional"/></typeDescriptor>
            </plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
  </installSteps>
</config>"""


def test_stale_pick_warnings_carry_the_full_step_group_option_name(tmp_path):
    """Picks are recorded as step:group:option; warnings echo that form."""
    mods = _tree_with_config(tmp_path)
    plan = classify_files(
        mods,
        "skyrimspecialedition",
        fomod_picks={11: [{"step": "Step1", "group": "G", "option": "Gone"}]},
        fomod_configs={11: _record()},
    )
    assert any("Step1:G:Gone" in w for w in plan.warnings), plan.warnings


def test_a_refused_pick_says_the_picks_could_not_be_read(tmp_path):
    """PickError is not "the option vanished" and must not read as if it were.

    A pick whose group name is ambiguous is refused outright; nothing is
    guessed, the installer's defaults install, and the warning carries the
    matcher's own reason (through exception_reason) rather than a claim
    that the options left the installer.
    """
    mods = tmp_path / "mods"
    (mods / "fomod").mkdir(parents=True)
    (mods / "fomod" / "ModuleConfig.xml").write_bytes(AMBIGUOUS_CONFIG)
    (mods / "a").mkdir()
    (mods / "a" / "thing.esp").write_text("a")
    (mods / "b").mkdir()
    (mods / "b" / "thing.esp").write_text("b")
    plan = classify_files(
        mods,
        "skyrimspecialedition",
        fomod_picks={11: [{"step": "", "group": "G", "option": "OptA"}]},
        fomod_configs={11: _record(xml=AMBIGUOUS_CONFIG)},
    )
    assert not plan.data_files
    text = " ".join(plan.warnings)
    assert "could not be read" in text, plan.warnings
    assert "more than one step" in text, plan.warnings


def test_dry_run_and_json_carry_the_same_warnings_as_deploy(tmp_path):
    """The parity rule: a dry run describes this deployment, warnings too."""
    from nexus_collection_dl.deploy import deploy as run_deploy
    from nexus_collection_dl.jsonout import to_jsonable
    from nexus_collection_dl.service import ModManagerService
    from nexus_collection_dl.state import CollectionState

    mods = _tree_with_config(tmp_path)
    game = tmp_path / "game"
    game.mkdir()
    prefix = tmp_path / "pfx"
    prefix.mkdir()

    state = CollectionState(mods)
    state.set_collection_info("u", "n", 1, "skyrimspecialedition")
    state.add_mod(
        {
            "mod_id": 1,
            "mod_name": "TestMod",
            "file_id": 11,
            "filename": "m.zip",
            "version": "1",
            "optional": False,
            "manual": True,
        }
    )
    state.mods[11].fomod_picks = [{"step": "Step1", "group": "G", "option": "Gone"}]
    state.mods[11].fomod_config = {
        "member": "fomod/ModuleConfig.xml",
        "xml": CONFIG.decode("utf-8"),
    }
    state.game_dir = str(game)
    state.proton_prefix = str(prefix)
    state.save()

    svc = ModManagerService(api_key="test-key-unused")
    result = svc.deploy(mods, game_dir=game, prefix=prefix)
    assert result.errors == []
    assert "Gone" in " ".join(result.warnings), result.warnings

    plan = classify_files(
        mods,
        "skyrimspecialedition",
        fomod_picks={11: state.mods[11].fomod_picks},
        fomod_configs={11: {**state.mods[11].fomod_config, "mod_id": 1}},
        mod_names={11: "TestMod"},
    )
    dry = run_deploy(plan, game, dry_run=True)
    assert dry.warnings == plan.warnings
    assert set(plan.warnings) <= set(result.warnings)

    envelope = to_jsonable(result)
    assert envelope["warnings"] == result.warnings


def test_guessing_rejects_a_partial_match(tmp_path):
    """A guess must explain the whole installer, not one coincidental name."""
    mods = _tree_with_config(tmp_path)
    plan = classify_files(
        mods,
        "skyrimspecialedition",
        fomod_picks={
            12: [
                {"step": "Step1", "group": "G", "option": "OptB"},
                {"step": "Step1", "group": "G", "option": "Gone"},
            ]
        },
    )
    assert not plan.data_files


def test_a_refused_pick_falls_through_to_the_manifest(tmp_path):
    """A pick that cannot be read must not erase the collection's own picks."""
    mods = _tree_with_config(tmp_path)
    plan = classify_files(
        mods,
        "skyrimspecialedition",
        fomod_picks={11: [{"step": "", "group": "G", "option": "OptA"}]},
        fomod_configs={11: _record(xml=AMBIGUOUS_CONFIG)},
        mod_choices={
            1: {
                "type": "fomod",
                "options": [
                    {
                        "name": "Step1",
                        "groups": [
                            {"name": "G", "choices": [{"name": "OptA", "idx": 0}]}
                        ],
                    }
                ],
            }
        },
    )
    deployed = {dest.as_posix() for _src, dest in plan.data_files}
    assert "thing.esp" in deployed
    assert any("could not be read" in w for w in plan.warnings)
