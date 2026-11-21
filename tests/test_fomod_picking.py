"""The shared picker walk and the service method that installs with picks.

Both front-ends (the CLI's click prompts and the shell's input lines)
drive the same walk, so the walk itself is tested with a scripted asker
and the install is tested against a real archive.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from nexus_collection_dl.extractor import FomodChoicesError
from nexus_collection_dl.fomod_plan import load_module_config
from nexus_collection_dl.fomod_select import Pick, PickError, prompt_for_picks
from nexus_collection_dl.service import ModManagerService
from nexus_collection_dl.state import CollectionState
from tests.test_fomod_selection import CONFIG

FIXTURES = Path(__file__).parent / "fixtures" / "fomod"


class ScriptedAsker:
    """An asker that answers from a script and records what it showed."""

    def __init__(self, ones=(), many=()):
        self.ones = list(ones)
        self.many = list(many)
        self.shown: list[str] = []

    def tell(self, text: str) -> None:
        self.shown.append(text)

    def choose_one(self, prompt: str, count: int, allow_none: bool):
        return self.ones.pop(0)

    def choose_many(self, prompt: str, count: int, at_least_one: bool):
        return self.many.pop(0)


def test_the_walk_asks_in_order_and_records_the_picks():
    config = load_module_config(FIXTURES / "invisible-step.ModuleConfig.xml")
    assert config is not None
    asker = ScriptedAsker(ones=[1])

    picks = prompt_for_picks(config, "TestMod.zip", [], asker)

    # Option A is the first (Recommended) option of step 1.
    assert picks == [Pick("Step1", "Group1", "Option A (hides Step 2)")]
    assert any("Group1" in line for line in asker.shown)


def test_a_flag_gated_step_is_only_asked_once_the_flag_is_set():
    config = load_module_config(FIXTURES / "invisible-step.ModuleConfig.xml")
    assert config is not None

    # Picking Option B (index 2) sets ShowStep2, which reveals step 2.
    asker = ScriptedAsker(ones=[2, 1])
    picks = prompt_for_picks(config, "TestMod.zip", [], asker)
    assert [p.option for p in picks] == [
        "Option B (shows Step 2)",
        "Invisible Option",
    ]

    # Picking Option A sets nothing, so step 2 is never asked about.
    asker = ScriptedAsker(ones=[1])
    picks = prompt_for_picks(config, "TestMod.zip", [], asker)
    assert [p.option for p in picks] == ["Option A (hides Step 2)"]


def test_kept_picks_are_not_asked_again():
    config = load_module_config(FIXTURES / "invisible-step.ModuleConfig.xml")
    assert config is not None
    kept = [Pick("Step1", "Group1", "Option A (hides Step 2)")]

    asker = ScriptedAsker(ones=[])
    picks = prompt_for_picks(config, "TestMod.zip", kept, asker)

    # Nothing was asked (the script is empty and unused) and the kept
    # pick is not re-collected as a fresh answer.
    assert picks == []
    assert not any("Group1" in line for line in asker.shown)


# ── the service method that finishes a clicked FOMOD ────────────────────


def _collection_with_archive(tmp_path: Path, content: bytes, status="pending_choices"):
    mods = tmp_path / "mods"
    mods.mkdir()
    state = CollectionState(mods)
    state.set_collection_info("u", "n", 1, "skyrimspecialedition")
    state.add_mod(
        {
            "mod_id": 1,
            "mod_name": "Test Mod",
            "file_id": 11,
            "filename": "TestMod-1.0.zip",
            "version": "1",
            "optional": False,
            "manual": True,
            "download_status": status,
        }
    )
    state.save()
    (mods / "TestMod-1.0.zip").write_bytes(content)
    return mods


def _fomod_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("fomod/ModuleConfig.xml", CONFIG)
        zf.writestr("a/thing.esp", b"a")
        zf.writestr("b/thing.esp", b"b")
    return buf.getvalue()


def _svc() -> ModManagerService:
    return ModManagerService(api_key="test-key-unused")


def test_install_fomod_picks_extracts_and_stores_them(tmp_path):
    mods = _collection_with_archive(tmp_path, _fomod_zip())

    report = _svc().install_fomod_picks(
        mods, 11, picks=[Pick("", "G", "OptB")]
    )

    assert "1 option choice" in report
    assert (mods / "b" / "thing.esp").exists()
    assert not (mods / "TestMod-1.0.zip").exists()
    state = CollectionState(mods)
    state.load()
    entry = state.mods[11]
    assert entry.download_status == "downloaded"
    assert entry.fomod_picks == [{"step": "Step1", "group": "G", "option": "OptB"}]


def test_install_fomod_picks_records_the_config_for_later_deploys(tmp_path):
    """This extract must record the installer config like every other one.

    Extraction merges archives into one tree, so the record is what lets a
    later deploy plan for this mod even after another mod overwrites the
    tree's copy of the config. The picker path has its own extract and is
    the easy one to leave out.
    """
    mods = _collection_with_archive(tmp_path, _fomod_zip())

    _svc().install_fomod_picks(mods, 11, picks=[Pick("", "G", "OptB")])

    state = CollectionState(mods)
    state.load()
    entry = state.mods[11]
    assert entry.fomod_config is not None, "the picker path lost the config record"
    assert entry.fomod_config["member"].lower().endswith("moduleconfig.xml")
    assert "<config" in entry.fomod_config["xml"]


def test_install_fomod_picks_can_ask(tmp_path):
    mods = _collection_with_archive(tmp_path, _fomod_zip())
    asked: list = []

    def prompt(config, archive_name, kept):
        asked.append(archive_name)
        return [Pick("Step1", "G", "OptA")]

    report = _svc().install_fomod_picks(mods, 11, on_fomod_prompt=prompt)

    assert asked == ["TestMod-1.0.zip"]
    assert (mods / "a" / "thing.esp").exists()
    assert "1 option choice" in report


def test_install_fomod_picks_rejects_an_unknown_option(tmp_path):
    mods = _collection_with_archive(tmp_path, _fomod_zip())

    with pytest.raises(PickError):
        _svc().install_fomod_picks(mods, 11, picks=[Pick("", "G", "Nope")])

    # Nothing extracted and the archive is kept.
    assert not (mods / "a").exists()
    assert (mods / "TestMod-1.0.zip").exists()


def test_install_fomod_picks_without_any_picks_refuses(tmp_path):
    mods = _collection_with_archive(tmp_path, _fomod_zip())

    with pytest.raises(FomodChoicesError):
        _svc().install_fomod_picks(mods, 11)

    assert (mods / "TestMod-1.0.zip").exists()


def test_install_fomod_picks_needs_the_archive_on_disk(tmp_path):
    mods = _collection_with_archive(tmp_path, _fomod_zip())
    (mods / "TestMod-1.0.zip").unlink()

    from nexus_collection_dl.state import StateError

    with pytest.raises(StateError):
        _svc().install_fomod_picks(mods, 11, picks=[Pick("", "G", "OptA")])
