"""Every extract path must record which files a mod contributed.

The extraction tree is merged: every mod's files land in one directory.
Without extract-time attribution a disabled mod cannot be told apart
from its neighbours at deploy time, and gc cannot judge whether a mod's
archive is redundant. `sync` records it. `add`, `import`, `update`, the
nxm click path, and the FOMOD picks finisher must too.

These tests build state the way the real flows do: `contributed_files`
starts empty and is only correct if the extract path wrote it.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from click.testing import CliRunner

from nexus_collection_dl import cli as cli_module
from nexus_collection_dl import service as service_module
from nexus_collection_dl.fomod_select import Pick
from nexus_collection_dl.service import ModManagerService
from nexus_collection_dl.state import CollectionState


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, payload in entries.items():
            zf.writestr(name, payload)
    return buf.getvalue()


def _make_zip(path: Path, entries: dict[str, bytes]) -> Path:
    path.write_bytes(_zip_bytes(entries))
    return path


def _reload(mods_dir: Path) -> CollectionState:
    state = CollectionState(mods_dir)
    state.load()
    return state


# -- import ---------------------------------------------------------------


def test_import_records_contributed_files(tmp_path):
    """import extracts for the user; it must say what it extracted.

    Without the list the mod cannot be disabled or judged redundant.
    """
    archive = _make_zip(
        tmp_path / "Foo-1234-1-0.zip",
        {"mod/plugin.esp": b"p", "mod/extra.esp": b"e"},
    )
    state = CollectionState(tmp_path)
    state.set_collection_info("u", "n", 1, "skyrimspecialedition")
    state.add_mod(
        {
            "mod_id": 1234,
            "mod_name": "Foo",
            "file_id": 10,
            "filename": "Foo-1234-1-0.zip",
            "version": "1.0",
            "download_status": "pending_download",
        }
    )
    state.save()

    svc = ModManagerService(api_key="test-key-unused")
    result = svc.import_downloads(tmp_path, no_load_order=True)

    assert result.extracted == 1, result.errors
    assert not archive.exists()
    mod = _reload(tmp_path).mods[10]
    assert sorted(mod.contributed_files) == ["mod/extra.esp", "mod/plugin.esp"]


def test_import_without_extraction_records_the_file_itself(tmp_path):
    """A loose file download is its own payload and must be attributed."""
    (tmp_path / "Solo-5-1-0.esp").write_bytes(b"esp")
    state = CollectionState(tmp_path)
    state.set_collection_info("u", "n", 1, "skyrimspecialedition")
    state.add_mod(
        {
            "mod_id": 5,
            "mod_name": "Solo",
            "file_id": 50,
            "filename": "Solo-5-1-0.esp",
            "version": "1.0",
            "download_status": "pending_download",
        }
    )
    state.save()

    svc = ModManagerService(api_key="test-key-unused")
    result = svc.import_downloads(tmp_path, no_load_order=True)

    assert result.matched == 1, result.errors
    mod = _reload(tmp_path).mods[50]
    assert mod.contributed_files == ["Solo-5-1-0.esp"]


# -- add ------------------------------------------------------------------


def _fake_download_writing(payloads: dict[str, dict[str, bytes]]):
    """download_mods stand-in that writes a real archive per mod filename."""

    def fake(self, game_domain, mods, target_dir, on_complete=None,
             on_progress=None, show_progress=True):
        out = []
        for m in mods:
            p = Path(target_dir) / m["filename"]
            _make_zip(p, payloads[m["filename"]])
            out.append((m, p))
        return out

    return fake


MOD_URL = "https://www.nexusmods.com/skyrimspecialedition/mods/100"


def _add_svc(tmp_path):
    from tests.test_service_regressions import FakeNexusAPI, _state

    _state(tmp_path)
    svc = ModManagerService(api_key="test-key-unused")
    svc._api = FakeNexusAPI(is_premium=True)
    return svc


def test_add_mod_records_contributed_files(tmp_path, monkeypatch):
    svc = _add_svc(tmp_path)
    monkeypatch.setattr(
        service_module.Downloader, "download_mods",
        _fake_download_writing({
            "TestMod-1.0.zip": {"a/one.esp": b"x", "a/two.esp": b"y"},
        }),
    )

    result = svc.add_mod(MOD_URL, tmp_path, no_load_order=True)

    assert result.success, result.error
    mod = _reload(tmp_path).mods[555]
    assert sorted(mod.contributed_files) == ["a/one.esp", "a/two.esp"]


# -- add_nxm (the click path) ---------------------------------------------


class _FakeNexusAPI:
    def __init__(self, files, mod_name="Test Mod"):
        self._files = files
        self._mod_name = mod_name
        self.session = None

    def validate_key(self):
        return {"user_id": 1, "name": "tester", "is_premium": True}

    def get_mod_files(self, game_domain, mod_id):
        return list(self._files)

    def get_mod_info(self, game_domain, mod_id):
        return {"name": self._mod_name, "mod_id": mod_id}

    def get_download_url(self, game_domain, mod_id, file_id, key=None, expires=None):
        return "https://cdn.example/download"

    def get_tracked_mods(self):
        return []


class _FakeResponse:
    def __init__(self, content):
        self.content = content
        self.status_code = 200
        self.headers = {"content-length": str(len(content))}

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=8192):
        yield self.content

    def close(self):
        pass


class _FakeSession:
    def __init__(self, content):
        self.content = content

    def get(self, url, **kwargs):
        return _FakeResponse(self.content)


def test_add_nxm_records_contributed_files(tmp_path, monkeypatch):
    """The click path is the 0.4.0 headline. It must attribute like sync."""
    zip_bytes = _zip_bytes({"Data/thing.esp": b"x", "Data/other.esp": b"y"})
    cdir = tmp_path / "col"
    cdir.mkdir()
    state = CollectionState(cdir)
    state.set_collection_info(
        "https://next.nexusmods.com/skyrimspecialedition/collections/t",
        "Test Collection", 1, "skyrimspecialedition",
    )
    state.save()

    session = _FakeSession(zip_bytes)

    class PatchedDownloader(service_module.Downloader):
        def __init__(self, api):
            super().__init__(api)
            self.session = session

    monkeypatch.setattr(service_module, "Downloader", PatchedDownloader)
    svc = ModManagerService(api_key="test-key-unused")
    svc._api = _FakeNexusAPI(files=[{
        "file_id": 456,
        "name": "Main File",
        "file_name": "TestMod-1.0.zip",
        "version": "1.0",
        "size_in_bytes": len(zip_bytes),
        "category_id": 1,
    }])

    result = svc.add_nxm(
        "nxm://skyrimspecialedition/mods/123/files/456?key=k&expires=9999999999&user_id=1",
        mods_dir=cdir,
    )

    assert result.success, result.error_message
    mod = _reload(cdir).mods[456]
    assert sorted(mod.contributed_files) == ["Data/other.esp", "Data/thing.esp"]


# -- install_fomod_picks (the retained-archive finisher) -------------------


def test_install_fomod_picks_records_contributed_files(tmp_path):
    """Finishing a kept FOMOD must attribute the files it just published."""
    from tests.test_fomod_selection import CONFIG

    mods = tmp_path / "mods"
    mods.mkdir()
    _make_zip(mods / "TestMod-1.0.zip", {
        "fomod/ModuleConfig.xml": CONFIG,
        "a/thing.esp": b"a",
        "b/thing.esp": b"b",
    })
    state = CollectionState(mods)
    state.set_collection_info("u", "n", 1, "skyrimspecialedition")
    state.add_mod(
        {
            "mod_id": 1,
            "mod_name": "Test Mod",
            "file_id": 11,
            "filename": "TestMod-1.0.zip",
            "version": "1",
            "manual": True,
            "download_status": "pending_choices",
        }
    )
    state.save()

    svc = ModManagerService(api_key="test-key-unused")
    svc.install_fomod_picks(mods, 11, picks=[Pick("", "G", "OptB")])

    mod = _reload(mods).mods[11]
    assert "b/thing.esp" in mod.contributed_files


# -- update ----------------------------------------------------------------


def test_update_records_contributed_files(tmp_path, monkeypatch):
    """update downloads and extracts a new revision; it must attribute too."""
    from tests.test_service_regressions import FakeNexusAPI
    from tests.test_service_tombstones import (
        collection_data,
        manifest_entry,
        reload_state,
        write_state,
    )

    cdir = write_state(tmp_path / "col", mods=[{
        "mod_id": 123,
        "mod_name": "Test Mod",
        "file_id": 456,
        "filename": "TestMod-1.0.zip",
        "version": "1.0",
        "download_status": "downloaded",
    }])

    def fake_download_mods(self, game_domain, mods, target_dir,
                           on_complete=None, on_progress=None,
                           show_progress=True):
        out = []
        for m in mods:
            p = Path(target_dir) / m["filename"]
            _make_zip(p, {"Data/new.esp": b"n"})
            out.append((m, p))
        return out

    monkeypatch.setattr(
        service_module.Downloader, "download_mods", fake_download_mods
    )
    svc = ModManagerService(api_key="test-key-unused")
    svc._api = FakeNexusAPI(
        is_premium=True,
        collection=collection_data([
            manifest_entry(123, 999, "Test Mod"),
        ]),
    )

    result = svc.update(cdir, no_load_order=True)

    assert result.to_update or result.to_install, result
    mod = reload_state(cdir).mods[999]
    assert mod.contributed_files == ["Data/new.esp"]


# -- the user-facing consequence -------------------------------------------


def test_disabled_mod_added_by_import_is_not_deployed(tmp_path):
    """The point of attribution: off means the files do not land.

    A mod the user added by hand and then disabled must stay out of the
    game directory. Without attribution its files cannot be isolated and
    the disable is a no-op.
    """
    mods = tmp_path / "mods"
    mods.mkdir()
    _make_zip(mods / "Foo-1234-1-0.zip", {"a/one.esp": b"1"})
    state = CollectionState(mods)
    state.set_collection_info("u", "n", 1, "skyrimspecialedition")
    state.add_mod(
        {
            "mod_id": 1234,
            "mod_name": "Foo",
            "file_id": 10,
            "filename": "Foo-1234-1-0.zip",
            "version": "1.0",
            "download_status": "pending_download",
        }
    )
    state.save()

    svc = ModManagerService(api_key="test-key-unused")
    result = svc.import_downloads(mods, no_load_order=True)
    assert result.extracted == 1, result.errors

    r = CliRunner().invoke(
        cli_module.main, ["disable", str(mods), "--file-id", "10"]
    )
    assert r.exit_code == 0, r.output

    game = tmp_path / "game"
    game.mkdir()
    deploy = svc.deploy(mods, game_dir=game, use_copy=True)
    assert deploy.deployed_count == 0, deploy
