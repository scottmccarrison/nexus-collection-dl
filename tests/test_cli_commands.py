"""CliRunner tests for the collection-facing commands.

Boundary-level: real click invocation with a fake service, checking wiring
and output shape rather than service behaviour (that is covered in
test_service_removal.py and friends). Human and JSON output are both checked.
"""

from __future__ import annotations

import json
from typing import ClassVar

import pytest
from click.testing import CliRunner

from nexus_collection_dl import cli as cli_module
from nexus_collection_dl import updater as updater_module
from nexus_collection_dl.service import (
    CollectionSummary,
    ModSummary,
    RemoveModResult,
)
from nexus_collection_dl.state import StateError


class FakeService:
    remove_result: ClassVar[RemoveModResult | None] = None
    error: ClassVar[BaseException | None] = None

    def __init__(self, api_key=None, force_free=False):
        pass

    def remove_mod(self, mods_dir, **kw):
        FakeService.calls.append(kw)
        if FakeService.error:
            raise FakeService.error
        return FakeService.remove_result or RemoveModResult(
            removed_file_ids=[7], removed_mod_ids=[8], warnings=[]
        )

    calls: ClassVar[list] = []


@pytest.fixture(autouse=True)
def _quiet(monkeypatch, tmp_path):
    FakeService.remove_result = None
    FakeService.error = None
    FakeService.calls = []
    monkeypatch.setattr(cli_module, "check_for_update", lambda: None)
    monkeypatch.setattr(updater_module, "check_and_prompt_update", lambda c: None)
    monkeypatch.setattr(cli_module, "ModManagerService", FakeService)
    monkeypatch.setattr(cli_module.console, "width", 400)
    monkeypatch.chdir(tmp_path)


def invoke(args: list[str], json_mode: bool = False):
    prefix = ["--api-key", "k", "--json"] if json_mode else ["--api-key", "k"]
    return CliRunner().invoke(cli_module.main, prefix + args)


# --- version ---------------------------------------------------------------


def test_version_flag_prints_the_version():
    """`--version` answers without a subcommand, like `--help` does."""
    from nexus_collection_dl import __version__

    r = invoke(["--version"])
    assert r.exit_code == 0, r.output
    assert f"nexus-dl v{__version__}" in r.output


# --- listings: local, no service, no network -------------------------------


def test_collections_empty(tmp_path):
    r = invoke(["collections", str(tmp_path)], json_mode=True)
    d = json.loads(r.output)
    assert d["ok"] is True
    assert d["result"]["collections"] == []
    assert r.exit_code == 0


def test_collections_human_says_none(tmp_path):
    r = invoke(["collections", str(tmp_path)])
    assert r.exit_code == 0
    assert "No collections found" in r.output


def test_games_empty(tmp_path):
    d = json.loads(invoke(["games", str(tmp_path)], json_mode=True).output)
    assert d["result"]["games"] == []


def test_games_rolls_up_collections(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli_module, "__name__", cli_module.__name__
    )  # keep the module importable
    from nexus_collection_dl import service as service_module

    rows = [
        CollectionSummary("A", "skyrim", 3, 1, tmp_path / "a"),
        CollectionSummary("B", "skyrim", 2, 0, tmp_path / "b"),
        CollectionSummary("C", "starfield", 5, 0, tmp_path / "c"),
    ]
    monkeypatch.setattr(service_module, "list_collections", lambda root: rows)
    d = json.loads(invoke(["games", str(tmp_path)], json_mode=True).output)
    games = {g["game_domain"]: g for g in d["result"]["games"]}
    assert games["skyrim"]["collection_count"] == 2
    assert games["skyrim"]["mod_count"] == 5
    assert games["starfield"]["mod_count"] == 5


def test_mods_without_state_fails_cleanly(tmp_path):
    r = invoke(["mods", str(tmp_path)], json_mode=True)
    d = json.loads(r.output)
    assert d["ok"] is False
    assert d["error"]["code"] == "state_error"
    assert r.exit_code == 1


def test_health_reports_checks_and_exits_zero(tmp_path):
    r = invoke(["health", str(tmp_path)], json_mode=True)
    d = json.loads(r.output)
    assert d["ok"] is True
    assert isinstance(d["result"]["checks"], list)
    assert r.exit_code == 0  # a report, not a gate


def test_health_human_renders_one_line_per_check(tmp_path):
    r = invoke(["health", str(tmp_path)])
    assert r.exit_code == 0
    assert "OK" in r.output or "WARN" in r.output or "ERROR" in r.output


# --- remove ----------------------------------------------------------------


def test_remove_requires_exactly_one_id(tmp_path):
    for args in ([], ["--file-id", "1", "--mod-id", "2"]):
        r = invoke(["remove", str(tmp_path)] + args, json_mode=True)
        d = json.loads(r.output)
        assert d["ok"] is False
        assert d["error"]["code"] == "invalid_argument"
        assert r.exit_code == 1


def test_remove_passes_the_id_through(tmp_path):
    r = invoke(["remove", str(tmp_path), "--file-id", "7"], json_mode=True)
    d = json.loads(r.output)
    assert d["ok"] is True
    assert d["result"]["removed_file_ids"] == [7]
    assert FakeService.calls[-1]["file_id"] == 7
    assert FakeService.calls[-1]["mod_id"] is None
    assert r.exit_code == 0


def test_remove_mod_id_form(tmp_path):
    invoke(["remove", str(tmp_path), "--mod-id", "8"], json_mode=True)
    assert FakeService.calls[-1]["mod_id"] == 8
    assert FakeService.calls[-1]["file_id"] is None


def test_remove_unknown_id_reports_state_error(tmp_path):
    FakeService.error = StateError("no matching mod")
    r = invoke(["remove", str(tmp_path), "--file-id", "9"], json_mode=True)
    d = json.loads(r.output)
    assert d["ok"] is False
    assert d["error"]["code"] == "state_error"
    assert r.exit_code == 1


def test_remove_dry_run_changes_nothing(tmp_path, monkeypatch):
    from nexus_collection_dl import service as service_module

    monkeypatch.setattr(
        service_module,
        "list_mods",
        lambda mods_dir: [ModSummary(7, 8, "Mod", "1.0", "m.zip", "downloaded")],
    )
    r = invoke(["remove", str(tmp_path), "--file-id", "7", "--dry-run"], json_mode=True)
    d = json.loads(r.output)
    assert d["ok"] is True
    assert d["result"]["dry_run"] is True
    assert d["result"]["mods"][0]["file_id"] == 7
    assert FakeService.calls == []  # nothing was mutated


def test_remove_dry_run_with_no_match_fails(tmp_path, monkeypatch):
    from nexus_collection_dl import service as service_module

    monkeypatch.setattr(service_module, "list_mods", lambda mods_dir: [])
    r = invoke(["remove", str(tmp_path), "--file-id", "7", "--dry-run"], json_mode=True)
    d = json.loads(r.output)
    assert d["ok"] is False
    assert d["error"]["code"] == "state_error"
    assert FakeService.calls == []


def test_remove_human_prints_counts(tmp_path):
    FakeService.remove_result = RemoveModResult(
        removed_file_ids=[7], removed_mod_ids=[], warnings=["something minor"]
    )
    r = invoke(["remove", str(tmp_path), "--file-id", "7"])
    assert "Removed 1 mod(s)" in r.output
    assert "something minor" in r.output


def test_deploy_dry_run_carries_the_plan_warnings(tmp_path):
    """Dry run describes the real deployment, warnings included."""
    from nexus_collection_dl.state import CollectionState

    config = b"""<config>
      <moduleName>T</moduleName>
      <installSteps><installStep name="S1">
        <optionalFileGroups><group name="G" type="SelectExactlyOne">
          <plugins><plugin name="OptA">
            <files><file source="a/thing.esp" destination="thing.esp"/></files>
            <typeDescriptor><type name="Optional"/></typeDescriptor>
          </plugin></plugins>
        </group></optionalFileGroups>
      </installStep></installSteps>
    </config>"""

    mods = tmp_path / "mods"
    (mods / "fomod").mkdir(parents=True)
    (mods / "fomod" / "ModuleConfig.xml").write_bytes(config)
    (mods / "a").mkdir()
    (mods / "a" / "thing.esp").write_text("a")

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
    state.mods[11].fomod_picks = [{"step": "S1", "group": "G", "option": "Gone"}]
    state.mods[11].fomod_config = {
        "member": "fomod/ModuleConfig.xml",
        "xml": config.decode("utf-8"),
    }
    state.save()

    r = invoke(
        ["deploy", str(mods), "--game-dir", str(tmp_path / "game"), "--dry-run"],
        json_mode=True,
    )
    assert r.exit_code == 0
    d = json.loads(r.output)
    assert d["ok"] is True
    assert any("Gone" in w for w in d["result"]["warnings"])


def test_list_mods_surfaces_fomod_picks(tmp_path):
    """Stored option picks must reach the listing, not just the state file.

    The picks drive the deploy plan, so a user who cannot see them cannot
    tell what they chose without reading .nexus-state.json by hand.
    """
    from nexus_collection_dl.service import list_mods
    from nexus_collection_dl.state import CollectionState

    mods = tmp_path / "mods"
    mods.mkdir()
    state = CollectionState(mods)
    state.set_collection_info("u", "n", 1, "skyrimspecialedition")
    state.add_mod(
        {
            "mod_id": 1,
            "mod_name": "Body",
            "file_id": 11,
            "filename": "body.zip",
            "version": "1",
        }
    )
    state.mods[11].fomod_picks = [
        {"step": "S1", "group": "Body", "option": "CBBE"},
        {"step": "S1", "group": "Textures", "option": "4K"},
    ]
    state.save()

    (summary,) = list_mods(mods)
    assert summary.fomod_picks == [
        {"step": "S1", "group": "Body", "option": "CBBE"},
        {"step": "S1", "group": "Textures", "option": "4K"},
    ]


def test_mods_table_shows_the_option_names(tmp_path):
    """The human table has to name the chosen options.

    A column of option names is what answers "what did I pick for this
    mod" from the CLI.
    """
    from nexus_collection_dl.state import CollectionState

    mods = tmp_path / "mods"
    mods.mkdir()
    state = CollectionState(mods)
    state.set_collection_info("u", "n", 1, "skyrimspecialedition")
    state.add_mod(
        {
            "mod_id": 1,
            "mod_name": "Body",
            "file_id": 11,
            "filename": "body.zip",
            "version": "1",
        }
    )
    state.mods[11].fomod_picks = [{"step": "S1", "group": "Body", "option": "CBBE"}]
    state.save()

    r = invoke(["mods", str(mods)])
    assert r.exit_code == 0
    assert "CBBE" in r.output


def test_mods_table_truncates_a_long_pick_list(tmp_path):
    """Many picks must not blow the table apart.

    A mod with a dozen options should read as the first few plus a count,
    not as one endless cell.
    """
    from nexus_collection_dl.state import CollectionState

    mods = tmp_path / "mods"
    mods.mkdir()
    state = CollectionState(mods)
    state.set_collection_info("u", "n", 1, "skyrimspecialedition")
    state.add_mod(
        {
            "mod_id": 1,
            "mod_name": "Big",
            "file_id": 11,
            "filename": "big.zip",
            "version": "1",
        }
    )
    state.mods[11].fomod_picks = [
        {"step": "S", "group": f"G{i}", "option": f"OptionNumber{i:02d}"}
        for i in range(12)
    ]
    state.save()

    r = invoke(["mods", str(mods)])
    assert r.exit_code == 0
    assert "OptionNumber00" in r.output
    assert "OptionNumber11" not in r.output
    assert "more" in r.output
