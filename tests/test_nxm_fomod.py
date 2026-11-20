"""The click path must not extract a FOMOD mod without its option choices.

A click is a one-shot from a browser: nobody is at the terminal to answer
the installer, and the installer's own defaults install nothing at all for
the common all-Options-Optional mod. So the archive is kept and the mod
waits for choices, which `import --fomod-option` supplies.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from nexus_collection_dl.fomod_select import Pick
from nexus_collection_dl.service import ModManagerService
from nexus_collection_dl.state import CollectionState
from tests.test_fomod_selection import CONFIG
from tests.test_nxm_service import (
    FILE_ID,
    FakeNexusAPI,
    files_meta,
    make_collection,
    make_zip,
    patch_downloader,
    pending_entry,
    uri,
)


@pytest.fixture()
def svc():
    return ModManagerService(api_key="test-key-unused")


def make_fomod_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("fomod/ModuleConfig.xml", CONFIG)
        zf.writestr("a/thing.esp", b"a")
        zf.writestr("b/thing.esp", b"b")
    return buf.getvalue()


def make_script_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("fomod/info.xml", b"<fomod/>")
        zf.writestr("fomod/script.cs", b"// script")
        zf.writestr("Data/thing.esp", b"x")
    return buf.getvalue()


def _ready(svc, tmp_path, monkeypatch, content: bytes) -> Path:
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    svc._api = FakeNexusAPI(files=files_meta(content))
    patch_downloader(monkeypatch, content)
    return cdir


def _status(cdir: Path, file_id: int = FILE_ID) -> str:
    state = CollectionState(cdir)
    state.load()
    return state.mods[file_id].download_status


def test_click_does_not_extract_a_fomod_that_needs_choices(svc, tmp_path, monkeypatch):
    cdir = _ready(svc, tmp_path, monkeypatch, make_fomod_zip())

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert result.success is False
    assert result.download_status == "pending_choices"
    assert "--fomod-option" in result.error
    # Nothing extracted, and the archive (the thing of value) is kept.
    assert not (cdir / "a").exists()
    assert not (cdir / "b").exists()
    assert (cdir / "TestMod-1.0.zip").exists()
    assert _status(cdir) == "pending_choices"


def test_reclick_of_a_pending_fomod_does_not_extract_it(svc, tmp_path, monkeypatch):
    cdir = _ready(svc, tmp_path, monkeypatch, make_fomod_zip())
    svc.add_nxm(uri(), mods_dir=cdir)

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert result.success is False
    assert result.download_status == "pending_choices"
    assert "--fomod-option" in result.error
    assert not (cdir / "a").exists()
    assert _status(cdir) == "pending_choices"


def test_click_refuses_a_script_based_installer(svc, tmp_path, monkeypatch):
    cdir = _ready(svc, tmp_path, monkeypatch, make_script_zip())

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert result.success is False
    assert "script" in result.error
    assert not (cdir / "Data").exists()
    assert (cdir / "TestMod-1.0.zip").exists()


def test_click_still_extracts_a_plain_archive(svc, tmp_path, monkeypatch):
    cdir = _ready(svc, tmp_path, monkeypatch, make_zip())

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert result.success, result.error
    assert result.extracted is True
    assert (cdir / "Data" / "test.txt").exists()
    assert _status(cdir) == "downloaded"


def test_import_with_picks_finishes_a_clicked_fomod(svc, tmp_path, monkeypatch):
    """The resolution path: click to get it, import with choices to install it."""
    cdir = _ready(svc, tmp_path, monkeypatch, make_fomod_zip())
    svc.add_nxm(uri(), mods_dir=cdir)
    assert _status(cdir) == "pending_choices"

    result = svc.import_downloads(
        cdir, no_load_order=True, fomod_picks=[Pick("", "G", "OptB")]
    )

    assert not result.errors, result.errors
    assert result.extracted == 1
    assert (cdir / "b" / "thing.esp").exists()
    assert _status(cdir) == "downloaded"
    state = CollectionState(cdir)
    state.load()
    assert state.mods[FILE_ID].fomod_picks == [
        {"step": "Step1", "group": "G", "option": "OptB"}
    ]


def test_pending_choices_counts_as_pending(svc, tmp_path, monkeypatch):
    cdir = _ready(svc, tmp_path, monkeypatch, make_fomod_zip())
    svc.add_nxm(uri(), mods_dir=cdir)

    state = CollectionState(cdir)
    state.load()
    assert [m.file_id for m in state.get_pending_mods()] == [FILE_ID]


def test_pending_choices_is_a_real_event_outcome(tmp_path, monkeypatch):
    """The token must not be coerced to 'error' by the writer's allowlist."""
    from nexus_collection_dl.handler import _EVENTS_OUTCOMES, append_nxm_event

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert "pending_choices" in _EVENTS_OUTCOMES

    cdir = make_collection(tmp_path)
    append_nxm_event(
        cdir,
        {
            "outcome": "pending_choices",
            "mod_name": "Test Mod",
            "file_id": FILE_ID,
            "mod_id": 123,
            "collection_name": "collection",
        },
    )
    line = (cdir / ".nexus-events.jsonl").read_text()
    assert '"outcome": "pending_choices"' in line or '"outcome":"pending_choices"' in line
