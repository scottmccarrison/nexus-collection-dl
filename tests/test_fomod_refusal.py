"""`add` and `import` refuse a FOMOD archive instead of installing every option.

A FOMOD mod ships an installer that offers choices (body type, texture
set, that sort of thing). Extracting one without making those choices
installs every option folder at once, including options the installer
marks as mutually exclusive, and nothing today says so. These cover the
refusal: what is detected, what is not, what the message says, and that
nothing is extracted and nothing is registered.

Detection is tested against real zip and 7z member listings. We have no
real FOMOD archives to draw on (only two loose ModuleConfig.xml files
found elsewhere), so the fixtures are built here and the rule is pinned
rather than surveyed.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from nexus_collection_dl.extractor import (
    ExtractionError,
    FomodChoicesError,
    FomodInstallerError,
    can_enumerate_members,
    find_fomod_installer,
)
from nexus_collection_dl.jsonout import error_code_for


def make_zip(path: Path, entries: dict[str, bytes]) -> Path:
    """Write a real zip archive at path with the given member payloads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, payload in entries.items():
            zf.writestr(name, payload)
    return path


def make_7z(path: Path, entries: dict[str, bytes]) -> Path:
    """Write a real 7z archive, so the second enumeration path is covered."""
    import py7zr

    path.parent.mkdir(parents=True, exist_ok=True)
    with py7zr.SevenZipFile(path, "w") as szf:
        for name, payload in entries.items():
            szf.writef(__import__("io").BytesIO(payload), name)
    return path


# --- detection -------------------------------------------------------------


def test_module_config_is_detected(tmp_path):
    z = make_zip(tmp_path / "m.zip", {
        "fomod/ModuleConfig.xml": b"<config/>",
        "Data/thing.esp": b"x",
    })
    assert find_fomod_installer(z) == "fomod/ModuleConfig.xml"


def test_script_lua_is_detected(tmp_path):
    z = make_zip(tmp_path / "m.zip", {"fomod/script.lua": b"-- fomod"})
    assert find_fomod_installer(z) == "fomod/script.lua"


def test_info_xml_is_detected(tmp_path):
    z = make_zip(tmp_path / "m.zip", {"fomod/info.xml": b"<fomod/>"})
    assert find_fomod_installer(z) == "fomod/info.xml"


def test_wrapper_folder_is_detected(tmp_path):
    """The top-level-only skip logic elsewhere would miss this one."""
    z = make_zip(tmp_path / "m.zip", {
        "Some Mod/fomod/ModuleConfig.xml": b"<config/>",
    })
    assert find_fomod_installer(z) == "Some Mod/fomod/ModuleConfig.xml"


def test_detection_is_case_insensitive(tmp_path):
    z = make_zip(tmp_path / "m.zip", {"FOMOD/Info.XML": b"<fomod/>"})
    assert find_fomod_installer(z) == "FOMOD/Info.XML"


def test_backslash_separators_are_detected(tmp_path):
    """The path rule splits on both separators, as real configs use both."""
    from nexus_collection_dl.extractor import _fomod_marker_from_name

    assert _fomod_marker_from_name("Some Mod\\fomod\\ModuleConfig.xml") is not None
    assert _fomod_marker_from_name("Some Mod/fomod/ModuleConfig.xml") is not None


def test_fomod_folder_without_an_installer_is_not_detected(tmp_path):
    """A mod that ships only fomod/header.png is not an installer."""
    z = make_zip(tmp_path / "m.zip", {
        "fomod/header.png": b"\x89PNG",
        "Data/thing.esp": b"x",
    })
    assert find_fomod_installer(z) is None


def test_plain_archive_is_not_detected(tmp_path):
    z = make_zip(tmp_path / "m.zip", {"Data/thing.esp": b"x"})
    assert find_fomod_installer(z) is None
    assert can_enumerate_members(z) is True


def test_detection_works_on_7z(tmp_path):
    z = make_7z(tmp_path / "m.7z", {
        "fomod/ModuleConfig.xml": b"<config/>",
        "Data/thing.esp": b"x",
    })
    assert find_fomod_installer(z) == "fomod/ModuleConfig.xml"
    assert can_enumerate_members(z) is True


# --- reading the installer out of an archive -------------------------------


def test_read_member_reads_a_7z_module_config(tmp_path):
    """The 7z read path: py7zr has no ``read()``, so this is the path the
    broken call turned into a silent 'could not read it'."""
    from nexus_collection_dl.extractor import read_archive_member

    z = make_7z(tmp_path / "m.7z", {
        "fomod/ModuleConfig.xml": b"<config/>",
        "Data/thing.esp": b"x",
    })
    assert read_archive_member(z, "fomod/ModuleConfig.xml") == b"<config/>"


def test_read_member_matches_the_member_name_loosely(tmp_path):
    from nexus_collection_dl.extractor import read_archive_member

    z = make_7z(tmp_path / "m.7z", {
        "Some Mod/fomod/ModuleConfig.xml": b"<config/>",
    })
    assert read_archive_member(z, r"some mod\fomod\moduleconfig.xml") == b"<config/>"


def test_read_member_falls_back_to_system_7z(tmp_path, monkeypatch):
    """Archives py7zr cannot decompress still yield their member bytes."""
    import subprocess

    import py7zr

    from nexus_collection_dl import extractor as extractor_module
    from nexus_collection_dl.extractor import read_archive_member

    z = make_7z(tmp_path / "m.7z", {"fomod/ModuleConfig.xml": b"<config/>"})

    def unsupported(self, *a, **kw):
        raise py7zr.exceptions.UnsupportedCompressionMethodError("bcj2", "bcj2")

    monkeypatch.setattr(py7zr.SevenZipFile, "extract", unsupported)

    seen: list = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"<config/>")

    monkeypatch.setattr(extractor_module.subprocess, "run", fake_run)
    assert read_archive_member(z, "fomod/ModuleConfig.xml") == b"<config/>"
    assert seen and "7z" in Path(seen[0][0]).name
    assert "-so" in seen[0]


def test_read_member_returns_none_when_nothing_can_read_it(tmp_path, monkeypatch):
    import py7zr

    from nexus_collection_dl import extractor as extractor_module
    from nexus_collection_dl.extractor import read_archive_member

    z = make_7z(tmp_path / "m.7z", {"fomod/ModuleConfig.xml": b"<config/>"})
    monkeypatch.setattr(
        py7zr.SevenZipFile, "extract",
        lambda self, *a, **kw: (_ for _ in ()).throw(
            py7zr.exceptions.UnsupportedCompressionMethodError("bcj2", "bcj2")
        ),
    )
    monkeypatch.setattr(extractor_module.shutil, "which", lambda name: None)
    assert read_archive_member(z, "fomod/ModuleConfig.xml") is None


# --- the exception and its error code --------------------------------------


def test_exception_message_names_the_member_and_the_kept_path(tmp_path):
    z = make_zip(tmp_path / "m.zip", {"fomod/script.cs": b"// script"})
    exc = FomodInstallerError("fomod/script.cs", z)
    text = str(exc)
    assert "fomod/script.cs" in text
    assert str(z) in text
    assert "script" in text
    assert "mutually exclusive" in text
    # No em dashes, no AI-tell words, per the writing rules.
    assert "\u2014" not in text


def test_choices_error_names_the_flags_to_pass(tmp_path):
    exc = FomodChoicesError("TestMod-1.0.zip")
    text = str(exc)
    assert "TestMod-1.0.zip" in text
    assert "--fomod-option" in text
    assert "--fomod-config" in text
    assert "\u2014" not in text


def test_error_code_is_fomod_installer_not_extraction_error():
    """The isinstance walk is ordered; the subclass must win."""
    exc = FomodInstallerError("fomod/ModuleConfig.xml", Path("/tmp/x.zip"))
    assert error_code_for(exc) == "fomod_installer"
    assert error_code_for(exc) != "extraction_error"
    # And the parent still maps the way it always did.
    assert error_code_for(ExtractionError("nope")) == "extraction_error"


def test_fomod_installer_error_is_an_extraction_error():
    """Subclassing is what lets the existing add/import handlers cope."""
    assert issubclass(FomodInstallerError, ExtractionError)


# --- the service-level check -----------------------------------------------


def _svc(tmp_path, monkeypatch, is_premium=True):
    from nexus_collection_dl.service import ModManagerService
    from tests.test_service_regressions import FakeNexusAPI, _state

    _state(tmp_path)
    svc = ModManagerService(api_key="test-key-unused")
    svc._api = FakeNexusAPI(is_premium=is_premium)
    return svc


def _fake_download_writing(payloads: dict[str, dict[str, bytes]]):
    """download_mods stand-in that writes a real archive per mod filename."""

    def fake(self, game_domain, mods, target_dir, on_complete=None,
             on_progress=None, show_progress=True):
        out = []
        for m in mods:
            p = Path(target_dir) / m["filename"]
            make_zip(p, payloads[m["filename"]])
            out.append((m, p))
        return out

    return fake


MOD_URL = "https://www.nexusmods.com/skyrimspecialedition/mods/100"


def test_add_needs_choices_for_a_fomod_archive(tmp_path, monkeypatch):
    """No picks and nobody to ask: refused, not extracted to every option."""
    from nexus_collection_dl import service as service_module

    svc = _svc(tmp_path, monkeypatch)
    monkeypatch.setattr(
        service_module.Downloader, "download_mods",
        _fake_download_writing({
            "TestMod-1.0.zip": {
                "fomod/ModuleConfig.xml": b"<config/>",
                "Data/thing.esp": b"x",
            },
        }),
    )
    with pytest.raises(FomodChoicesError) as excinfo:
        svc.add_mod(MOD_URL, tmp_path, no_load_order=True)

    assert "--fomod-option" in str(excinfo.value)
    # Nothing extracted.
    assert not (tmp_path / "Data").exists()
    # The archive is kept.
    assert (tmp_path / "TestMod-1.0.zip").exists()
    # Nothing registered.
    state = _reload(tmp_path)
    assert state.mods == {}


def test_add_refuses_without_ever_calling_extract(tmp_path, monkeypatch):
    """The refusal happens before extraction, not after a partial one.

    add_mod downloads exactly one file today, so a multi-file pre-pass is
    defensive; what is load-bearing is that extract_archive is never
    reached when a FOMOD installer's choices cannot be settled.
    """
    from nexus_collection_dl import service as service_module

    svc = _svc(tmp_path, monkeypatch)
    monkeypatch.setattr(
        service_module.Downloader, "download_mods",
        _fake_download_writing({
            "TestMod-1.0.zip": {"fomod/ModuleConfig.xml": b"<config/>"},
        }),
    )
    calls: list = []

    def recording_extract(archive_path, target_dir):
        calls.append(archive_path)
        raise AssertionError("extract_archive must not be reached")

    monkeypatch.setattr(service_module, "extract_archive", recording_extract)
    with pytest.raises(FomodChoicesError):
        svc.add_mod(MOD_URL, tmp_path, no_load_order=True)
    assert calls == []


def test_add_refuses_a_script_based_installer(tmp_path, monkeypatch):
    """Script installers have no groups to pick from and still refuse."""
    from nexus_collection_dl import service as service_module

    svc = _svc(tmp_path, monkeypatch)
    monkeypatch.setattr(
        service_module.Downloader, "download_mods",
        _fake_download_writing({
            "TestMod-1.0.zip": {
                "fomod/info.xml": b"<fomod/>",
                "fomod/script.cs": b"// script",
                "Data/thing.esp": b"x",
            },
        }),
    )
    with pytest.raises(FomodInstallerError) as excinfo:
        svc.add_mod(MOD_URL, tmp_path, no_load_order=True)

    assert "script" in str(excinfo.value)
    assert (tmp_path / "TestMod-1.0.zip").exists()
    assert not (tmp_path / "Data").exists()


def test_add_extracts_a_plain_archive_unchanged(tmp_path, monkeypatch):
    """No regression: a mod without a FOMOD installer installs as before."""
    from nexus_collection_dl import service as service_module

    svc = _svc(tmp_path, monkeypatch)
    monkeypatch.setattr(
        service_module.Downloader, "download_mods",
        _fake_download_writing({
            "TestMod-1.0.zip": {"Data/thing.esp": b"x"},
        }),
    )
    result = svc.add_mod(MOD_URL, tmp_path, no_load_order=True)
    assert result.success, result.error
    assert (tmp_path / "Data" / "thing.esp").exists()
    assert not (tmp_path / "TestMod-1.0.zip").exists()


def test_add_refuses_an_uninspectable_archive(tmp_path, monkeypatch):
    """We cannot tell, so we do not guess. --no-extract is the escape hatch."""
    from nexus_collection_dl import service as service_module

    svc = _svc(tmp_path, monkeypatch)

    def fake_download(self, game_domain, mods, target_dir, **kw):
        out = []
        for m in mods:
            p = Path(target_dir) / m["filename"]
            p.write_bytes(b"not an archive we can list")
            out.append((m, p))
        return out

    monkeypatch.setattr(service_module.Downloader, "download_mods", fake_download)
    with pytest.raises(ExtractionError) as excinfo:
        svc.add_mod(MOD_URL, tmp_path, no_load_order=True)

    text = str(excinfo.value)
    assert "--no-extract" in text
    assert "could not inspect" in text
    assert not (tmp_path / "Data").exists()


def test_add_no_extract_still_works_on_a_fomod_archive(tmp_path, monkeypatch):
    """The escape hatch does what it says: registers without extracting."""
    from nexus_collection_dl import service as service_module

    svc = _svc(tmp_path, monkeypatch)
    monkeypatch.setattr(
        service_module.Downloader, "download_mods",
        _fake_download_writing({
            "TestMod-1.0.zip": {"fomod/ModuleConfig.xml": b"<config/>"},
        }),
    )
    result = svc.add_mod(MOD_URL, tmp_path, no_load_order=True, no_extract=True)
    assert result.success, result.error
    assert (tmp_path / "TestMod-1.0.zip").exists()


def _reload(tmp_path):
    from nexus_collection_dl.state import CollectionState

    state = CollectionState(tmp_path)
    state.load()
    return state


# --- import records and continues -----------------------------------------


def test_import_records_the_refusal_and_leaves_the_item_pending(
    tmp_path, monkeypatch
):

    svc = _svc(tmp_path, monkeypatch)
    # Two mods already pending, one of them FOMOD.
    state = _reload(tmp_path)
    state.add_mod({
        "mod_id": 1, "mod_name": "Fomod Mod", "file_id": 11,
        "filename": "FomodMod-1.0.zip", "version": "1.0",
        "optional": False, "manual": True,
        "download_status": "pending_download",
    })
    state.add_mod({
        "mod_id": 2, "mod_name": "Plain Mod", "file_id": 22,
        "filename": "PlainMod-1.0.zip", "version": "1.0",
        "optional": False, "manual": True,
        "download_status": "pending_download",
    })
    state.save()

    make_zip(tmp_path / "FomodMod-1.0.zip", {
        "fomod/ModuleConfig.xml": b"<config/>",
    })
    make_zip(tmp_path / "PlainMod-1.0.zip", {"Data/plain.esp": b"x"})

    result = svc.import_downloads(tmp_path, no_load_order=True)

    # The FOMOD one is named, the plain one went through.
    assert any("Fomod Mod" in e for e in result.errors), result.errors
    assert result.matched == 2
    assert result.extracted == 1

    # FOMOD item still pending, plain one downloaded.
    state = _reload(tmp_path)
    assert state.mods[11].download_status == "pending_download"
    assert state.mods[22].download_status == "downloaded"

    # The FOMOD archive is kept; the plain one was consumed.
    assert (tmp_path / "FomodMod-1.0.zip").exists()
    assert not (tmp_path / "PlainMod-1.0.zip").exists()


# --- the CLI surface -------------------------------------------------------


def test_cli_add_reports_fomod_choices_code(tmp_path, monkeypatch):
    import json

    from click.testing import CliRunner

    from nexus_collection_dl import cli as cli_module
    from nexus_collection_dl import service as service_module

    svc = _svc(tmp_path, monkeypatch)
    monkeypatch.setattr(
        service_module.Downloader, "download_mods",
        _fake_download_writing({
            "TestMod-1.0.zip": {"fomod/ModuleConfig.xml": b"<config/>"},
        }),
    )

    class _FixedService:
        def __init__(self, api_key=None, force_free=False):
            self._api_key = api_key

        def add_mod(self, *a, **kw):
            return svc.add_mod(*a, **kw)

    monkeypatch.setattr(cli_module, "ModManagerService", _FixedService)
    monkeypatch.setattr(cli_module, "check_for_update", lambda: None)

    r = CliRunner().invoke(cli_module.main, [
        "--api-key", "k", "--json", "add", MOD_URL, str(tmp_path),
    ])
    assert r.exit_code == 1, (r.exit_code, r.exception, r.output)
    from tests.test_cli_json import stdout_of

    envelope = json.loads(stdout_of(r))
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "fomod_choices"
    assert "--fomod-option" in envelope["error"]["message"]
