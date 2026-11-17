"""Tombstone persistence and enforcement tests for mod removal.

Removing a mod records a tombstone keyed by mod_id (the Nexus mod page),
so a collection revision that assigns a new file id to the same page
cannot resurrect a removed mod. Sync and update filter their manifest
input through the tombstones exactly once, so every comparison bucket
sees the same filtered manifest. Explicit re-adds (add_mod, add_nxm
committing outcomes, import commits) clear the tombstone, standalone
--output clears nothing, local negative-id mods and legacy mod_id-0
entries are removed without tombstones, and older state files load
unchanged. All API access is faked and all downloads are mocked; no
network is ever reached.
"""

import io
import json
import time
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from nexus_collection_dl import service as service_module
from nexus_collection_dl import state as state_module
from nexus_collection_dl.manifest import CollectionManifest
from nexus_collection_dl.service import ModManagerService
from nexus_collection_dl.state import STATE_FILENAME, CollectionState

GAME = "skyrimspecialedition"
MOD_ID = 123
FILE_ID = 456
KEY = "SignedKeyValue-abc123_XYZ"
USER_ID = 4242
FUTURE = int(time.time()) + 3600
COLLECTION_URL = f"https://next.nexusmods.com/{GAME}/collections/testcol"
MOD_URL = f"https://www.nexusmods.com/{GAME}/mods/{MOD_ID}"


def uri(mod_id=MOD_ID, file_id=FILE_ID):
    return (
        f"nxm://{GAME}/mods/{mod_id}/files/{file_id}"
        f"?key={KEY}&expires={FUTURE}&user_id={USER_ID}"
    )


def make_zip(name="Data/test.txt", payload=b"hello mods") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, payload)
    return buf.getvalue()


class FakeNexusAPI:
    """In-memory NexusAPI stand-in recording every call."""

    def __init__(self, is_premium=True, files=None, mod_name="Test Mod",
                 collection=None):
        self._is_premium = is_premium
        self._files = files if files is not None else []
        self._mod_name = mod_name
        self._collection = collection
        self.calls = []
        self.session = None

    def validate_key(self):
        self.calls.append("validate_key")
        return {
            "user_id": USER_ID, "name": "tester",
            "is_premium": self._is_premium, "profile_expiry": None,
        }

    def get_mod_info(self, game_domain, mod_id):
        self.calls.append("get_mod_info")
        return {"name": self._mod_name, "mod_id": mod_id}

    def get_mod_files(self, game_domain, mod_id):
        self.calls.append("get_mod_files")
        return list(self._files)

    def get_download_url(self, game_domain, mod_id, file_id, key=None, expires=None):
        self.calls.append("get_download_url")
        return "https://cdn.example/download?token=CDNTOKEN"

    def get_collection_mods(self, game_domain, slug):
        self.calls.append("get_collection_mods")
        return dict(self._collection)

    def get_tracked_mods(self):
        self.calls.append("get_tracked_mods")
        return []

    def track_mod(self, game_domain, mod_id):
        self.calls.append(("track_mod", mod_id))

    def untrack_mod(self, game_domain, mod_id):
        self.calls.append(("untrack_mod", mod_id))


class FakeResponse:
    def __init__(self, content):
        self.content = content
        self.status_code = 200
        self.headers = {"content-length": str(len(content))}

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=8192):
        for i in range(0, len(self.content), 512):
            yield self.content[i:i + 512]

    def close(self):
        pass


class FakeCdnSession:
    def __init__(self, content):
        self.content = content

    def get(self, url, **kwargs):
        return FakeResponse(self.content)


def patch_downloader(monkeypatch, content: bytes):
    """Route every Downloader HTTP call through a fake serving `content`."""
    session = FakeCdnSession(content)

    class PatchedDownloader(service_module.Downloader):
        def __init__(self, api):
            super().__init__(api)
            self.session = session

    monkeypatch.setattr(service_module, "Downloader", PatchedDownloader)
    return session


def patch_download_mods(monkeypatch):
    """Replace Downloader.download_mods with a local writer; record mods.

    Writes real zip content so the extraction step of sync/update
    succeeds exactly as it would for a genuine download.
    """
    recorded: list[dict] = []

    def fake_download_mods(self, game_domain, mods, target_dir,
                           on_complete=None, on_progress=None):
        out = []
        for m in mods:
            p = Path(target_dir) / m["filename"]
            p.write_bytes(make_zip(f"Data/f{m['file_id']}.txt"))
            recorded.append(m)
            out.append((m, p))
        return out

    monkeypatch.setattr(
        service_module.Downloader, "download_mods", fake_download_mods
    )
    return recorded


def state_entry(file_id=FILE_ID, mod_id=MOD_ID, name="Test Mod",
                filename="TestMod-1.0.zip", download_status="downloaded",
                manual=False):
    return {
        "mod_id": mod_id,
        "mod_name": name,
        "file_id": file_id,
        "filename": filename,
        "version": "1.0",
        "optional": False,
        "manual": manual,
        "download_status": download_status,
    }


def manifest_entry(mod_id, file_id, name, optional=False):
    return {
        "mod_id": mod_id,
        "file_id": file_id,
        "mod_name": name,
        "filename": f"{name.replace(' ', '')}-{file_id}.zip",
        "version": "1.0",
        "optional": optional,
        "size_bytes": 10,
    }


def collection_data(mods, revision=2, download_link=None):
    return {
        "name": "Test Collection",
        "revision": revision,
        "game_domain": GAME,
        "mods": mods,
        "url": COLLECTION_URL,
        "download_link": download_link,
    }


def write_state(cdir: Path, mods=(), collection_name="Test Collection"):
    cdir.mkdir(parents=True, exist_ok=True)
    state = CollectionState(cdir)
    state.set_collection_info(COLLECTION_URL, collection_name, 1, GAME)
    for m in mods:
        state.add_mod(m)
    state.save()
    return cdir


def reload_state(cdir: Path) -> CollectionState:
    state = CollectionState(cdir)
    state.load()
    return state


def state_raw(cdir: Path) -> bytes:
    return (cdir / STATE_FILENAME).read_bytes()


@pytest.fixture()
def svc() -> ModManagerService:
    # The API key is never used: tests replace svc._api with a fake.
    return ModManagerService(api_key="test-key-unused")


# ---------------------------------------------------------------------------
# (l) Tombstone persistence: int keys in memory, string keys on disk,
#     old-format states load unchanged
# ---------------------------------------------------------------------------

def test_tombstone_persists_with_int_keys(svc, tmp_path):
    cdir = write_state(tmp_path / "col", mods=[state_entry()])
    svc.remove_mod(cdir, mod_id=MOD_ID, no_load_order=True)

    raw = json.loads((cdir / STATE_FILENAME).read_text())
    assert "removed_mods" in raw
    assert "123" in raw["removed_mods"]          # JSON keys are strings
    tomb = raw["removed_mods"]["123"]
    assert tomb["name"] == "Test Mod"
    datetime.fromisoformat(tomb["removed_at"])   # valid iso8601 timestamp

    state = reload_state(cdir)
    assert set(state.removed_mods) == {123}      # int keys restored
    assert state.is_removed(123)
    assert not state.is_removed(456)
    assert state.removed_mods[123]["name"] == "Test Mod"


def test_mark_and_clear_removed_helpers(tmp_path):
    state = CollectionState(tmp_path)
    assert not state.is_removed(123)

    state.mark_removed(123, "Test Mod")
    assert state.is_removed(123)
    assert set(state.removed_mods[123]) == {"removed_at", "name"}
    datetime.fromisoformat(state.removed_mods[123]["removed_at"])

    state.clear_removed(123)
    assert not state.is_removed(123)
    state.clear_removed(123)                     # clearing twice is safe


def test_old_format_state_loads_unchanged(tmp_path):
    # A state written before tombstones existed has no removed_mods key.
    cdir = write_state(tmp_path / "col", mods=[state_entry()])
    raw = json.loads((cdir / STATE_FILENAME).read_text())
    del raw["removed_mods"]
    (cdir / STATE_FILENAME).write_text(json.dumps(raw))

    state = reload_state(cdir)
    assert state.removed_mods == {}
    assert FILE_ID in state.mods                 # mods load unchanged

    # Saving preserves both the mods and the (still empty) tombstones.
    state.save()
    again = reload_state(cdir)
    assert again.removed_mods == {}
    assert FILE_ID in again.mods


# ---------------------------------------------------------------------------
# (m) Sync skips tombstoned mods and reports DISTINCT mod_ids:
#     free-account direct registration and premium empty-state download
# ---------------------------------------------------------------------------

def test_sync_free_skips_tombstoned_mod_counts_distinct(svc, tmp_path):
    # Mod 123 has TWO files in the manifest: one tombstone skips both and
    # the report counts one distinct mod_id.
    cdir = write_state(tmp_path / "Test Collection")
    state = reload_state(cdir)
    state.mark_removed(MOD_ID, "Test Mod")
    state.save()

    mods = [
        manifest_entry(123, 456, "Test Mod"),
        manifest_entry(123, 457, "Test Mod"),
        manifest_entry(200, 777, "Other Mod"),
    ]
    svc._api = FakeNexusAPI(is_premium=False, collection=collection_data(mods))

    result = svc.sync(COLLECTION_URL, tmp_path, no_load_order=True)

    assert result.removed_skipped == 1
    assert result.collection_dir == cdir
    state = reload_state(cdir)
    assert set(state.mods) == {777}              # only the live mod registered
    assert state.mods[777].download_status == "pending_download"
    assert state.is_removed(MOD_ID)              # tombstone untouched


def test_sync_premium_empty_state_downloads_only_live_mods(svc, tmp_path,
                                                           monkeypatch):
    # After removing the only installed mod the state is empty, so premium
    # sync takes the full-download path. The tombstoned page (with a NEW
    # file id from a revision bump) must be filtered out of that download.
    cdir = write_state(tmp_path / "Test Collection",
                       mods=[state_entry(456, 123, "Test Mod")])
    svc.remove_mod(cdir, mod_id=MOD_ID, no_load_order=True)
    assert reload_state(cdir).mods == {}

    mods = [
        manifest_entry(123, 999, "Test Mod"),    # revision bumped file id
        manifest_entry(200, 777, "Other Mod"),
    ]
    svc._api = FakeNexusAPI(is_premium=True, collection=collection_data(mods))
    recorded = patch_download_mods(monkeypatch)

    result = svc.sync(COLLECTION_URL, tmp_path, no_load_order=True)

    assert result.removed_skipped == 1
    assert [m["file_id"] for m in recorded] == [777]
    state = reload_state(cdir)
    assert set(state.mods) == {777}
    assert state.mods[777].download_status == "downloaded"
    assert state.is_removed(MOD_ID)              # tombstone survives sync


def test_sync_premium_nonempty_state_skips_tombstoned_mod(svc, tmp_path,
                                                          monkeypatch):
    # The other premium branch: state already has mods, so sync compares
    # against the collection. The comparison must see the same filtered
    # manifest input as every other consumer.
    cdir = _state_with_live_mod_and_tombstone(tmp_path / "Test Collection")
    mods = [
        manifest_entry(123, 999, "Test Mod"),
        manifest_entry(200, 777, "Other Mod"),
        manifest_entry(300, 888, "New Mod"),
    ]
    svc._api = FakeNexusAPI(is_premium=True, collection=collection_data(mods))
    recorded = patch_download_mods(monkeypatch)

    result = svc.sync(COLLECTION_URL, tmp_path, no_load_order=True)

    assert result.removed_skipped == 1
    assert [m["file_id"] for m in recorded] == [888]   # only the new mod
    state = reload_state(cdir)
    assert set(state.mods) == {777, 888}
    assert all(ms.mod_id != 123 for ms in state.mods.values())
    assert state.is_removed(MOD_ID)


# ---------------------------------------------------------------------------
# (n) Update skips tombstoned mods in both account modes
# ---------------------------------------------------------------------------

def _state_with_live_mod_and_tombstone(cdir: Path) -> Path:
    write_state(cdir, mods=[
        state_entry(456, 123, "Test Mod", "TestMod-1.0.zip"),
        state_entry(777, 200, "Other Mod", "OtherMod-1.0.zip"),
    ])
    state = reload_state(cdir)
    state.mark_removed(123, "Test Mod")
    state.mods.pop(456)                          # what remove_mod persists
    state.save()
    return cdir


def test_update_free_skips_tombstoned_mod(svc, tmp_path):
    cdir = _state_with_live_mod_and_tombstone(tmp_path / "col")
    mods = [
        manifest_entry(123, 999, "Test Mod"),
        manifest_entry(200, 777, "Other Mod"),
    ]
    svc._api = FakeNexusAPI(is_premium=False, collection=collection_data(mods))

    result = svc.update(cdir, no_load_order=True)

    assert result.removed_skipped == 1
    assert result.to_install == []
    assert result.to_update == []
    assert [m["file_id"] for m in result.up_to_date] == [777]
    assert result.pending_downloads == []        # nothing registered for 999
    state = reload_state(cdir)
    assert set(state.mods) == {777}


def test_update_premium_skips_tombstoned_mod(svc, tmp_path, monkeypatch):
    cdir = _state_with_live_mod_and_tombstone(tmp_path / "col")
    mods = [
        manifest_entry(123, 999, "Test Mod"),
        manifest_entry(200, 777, "Other Mod"),
    ]
    svc._api = FakeNexusAPI(is_premium=True, collection=collection_data(mods))
    recorded = patch_download_mods(monkeypatch)

    result = svc.update(cdir, no_load_order=True)

    assert result.removed_skipped == 1
    assert result.to_install == [] and result.to_update == []
    assert recorded == []                        # zero downloads for 999
    state = reload_state(cdir)
    assert set(state.mods) == {777}


# ---------------------------------------------------------------------------
# (o) Revision bump regression: a NEW file_id for a tombstoned mod page is
#     still skipped (the core case mod_id keying exists for)
# ---------------------------------------------------------------------------

def test_revision_bump_with_new_file_id_still_skipped(svc, tmp_path):
    cdir = write_state(tmp_path / "Test Collection",
                       mods=[state_entry(456, 123, "Test Mod")])
    svc.remove_mod(cdir, file_id=FILE_ID, no_load_order=True)

    # The revision assigned NEW file id 999 to the same page. A
    # file_id-keyed tombstone would miss it and reinstall the mod.
    mods = [
        manifest_entry(123, 999, "Test Mod"),
        manifest_entry(200, 777, "Other Mod"),
    ]
    svc._api = FakeNexusAPI(is_premium=False, collection=collection_data(mods))

    sync_result = svc.sync(COLLECTION_URL, tmp_path, no_load_order=True)
    assert sync_result.removed_skipped == 1

    update_result = svc.update(cdir, no_load_order=True, skip_optional=True)
    assert update_result.removed_skipped == 1

    state = reload_state(cdir)
    assert 999 not in state.mods                 # never registered
    assert all(ms.mod_id != 123 for ms in state.mods.values())
    assert state.is_removed(123)


# ---------------------------------------------------------------------------
# (p) Clearing rules: explicit re-adds clear the tombstone; standalone
#     --output clears nothing
# ---------------------------------------------------------------------------

def test_add_mod_clears_tombstone(svc, tmp_path):
    cdir = write_state(tmp_path / "col")
    state = reload_state(cdir)
    state.mark_removed(MOD_ID, "Test Mod")
    state.save()

    svc._api = FakeNexusAPI(is_premium=False, files=[{
        "file_id": 555, "name": "Main File", "file_name": "TestMod-1.0.zip",
        "version": "1.0", "size_in_bytes": 10, "category_id": 1,
    }])
    result = svc.add_mod(MOD_URL, cdir)

    assert result.success, result.error
    state = reload_state(cdir)
    assert not state.is_removed(MOD_ID)          # explicit re-add clears
    assert 555 in state.mods


def test_add_nxm_extraction_failure_pending_commit_clears(svc, tmp_path,
                                                          monkeypatch):
    zip_bytes = make_zip()
    cdir = write_state(tmp_path / "col")
    state = reload_state(cdir)
    state.mark_removed(MOD_ID, "Test Mod")
    state.save()

    svc._api = FakeNexusAPI(files=[{
        "file_id": FILE_ID, "name": "Main File",
        "file_name": "TestMod-1.0.zip", "version": "1.0",
        "size_in_bytes": len(zip_bytes), "category_id": 1,
    }])
    patch_downloader(monkeypatch, zip_bytes)

    def boom(path, target):
        raise service_module.ExtractionError("boom")

    monkeypatch.setattr(service_module, "extract_archive", boom)

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert not result.success
    assert "import" in result.error
    state = reload_state(cdir)
    # The pending entry was committed and the tombstone cleared with it,
    # so 'nexus-dl import' can finish the recovery.
    assert state.mods[FILE_ID].download_status == "pending_download"
    assert not state.is_removed(MOD_ID)
    assert (cdir / "TestMod-1.0.zip").exists()   # archive kept for import


def test_add_nxm_downloaded_clears_tombstone(svc, tmp_path, monkeypatch):
    zip_bytes = make_zip()
    cdir = write_state(tmp_path / "col")
    state = reload_state(cdir)
    state.mark_removed(MOD_ID, "Test Mod")
    state.save()

    svc._api = FakeNexusAPI(files=[{
        "file_id": FILE_ID, "name": "Main File",
        "file_name": "TestMod-1.0.zip", "version": "1.0",
        "size_in_bytes": len(zip_bytes), "category_id": 1,
    }])
    patch_downloader(monkeypatch, zip_bytes)

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert result.success and result.status == "downloaded"
    state = reload_state(cdir)
    assert state.mods[FILE_ID].download_status == "downloaded"
    assert not state.is_removed(MOD_ID)


def test_add_nxm_already_downloaded_clears_tombstone(svc, tmp_path):
    zip_bytes = make_zip()
    cdir = write_state(tmp_path / "col", mods=[
        state_entry(download_status="downloaded", filename="TestMod-1.0.zip"),
    ])
    (cdir / "TestMod-1.0.zip").write_bytes(zip_bytes)
    state = reload_state(cdir)
    state.mark_removed(MOD_ID, "Test Mod")
    state.save()
    before = state_raw(cdir)

    svc._api = FakeNexusAPI()
    result = svc.add_nxm(uri(), mods_dir=cdir)

    # Proven duplicate with a stale tombstone: re-clicking the link is
    # explicit re-add intent, so only the tombstone is cleared (and only
    # then is state written at all).
    assert result.success and result.status == "already_downloaded"
    assert svc._api.calls == []                  # zero network
    assert state_raw(cdir) != before             # the clear was persisted
    assert not reload_state(cdir).is_removed(MOD_ID)


def test_add_nxm_extracted_from_archive_clears_tombstone(svc, tmp_path):
    zip_bytes = make_zip()
    cdir = write_state(tmp_path / "col", mods=[
        state_entry(download_status="pending_download",
                    filename="TestMod-1.0.zip"),
    ])
    (cdir / "TestMod-1.0.zip").write_bytes(zip_bytes)
    state = reload_state(cdir)
    state.mark_removed(MOD_ID, "Test Mod")
    state.save()

    svc._api = FakeNexusAPI()
    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert result.success and result.status == "extracted_from_archive"
    state = reload_state(cdir)
    assert state.mods[FILE_ID].download_status == "downloaded"
    assert not state.is_removed(MOD_ID)


def test_add_nxm_output_clears_nothing(svc, tmp_path, monkeypatch):
    zip_bytes = make_zip()
    cdir = write_state(tmp_path / "col")
    state = reload_state(cdir)
    state.mark_removed(MOD_ID, "Test Mod")
    state.save()
    before = state_raw(cdir)

    out = tmp_path / "out"
    out.mkdir()
    svc._api = FakeNexusAPI(files=[{
        "file_id": FILE_ID, "name": "Main File",
        "file_name": "TestMod-1.0.zip", "version": "1.0",
        "size_in_bytes": len(zip_bytes), "category_id": 1,
    }])
    patch_downloader(monkeypatch, zip_bytes)

    result = svc.add_nxm(uri(), output_dir=out)

    # Standalone --output has no collection state and clears nothing.
    assert result.success and result.status == "saved_to_output"
    assert (out / "TestMod-1.0.zip").exists()
    assert state_raw(cdir) == before
    assert reload_state(cdir).is_removed(MOD_ID)


def _failing_save(captured):
    """CollectionState.save replacement: records the state, then fails."""

    def boom(self):
        captured.append(self)
        raise OSError(28, "injected disk full")

    return boom


def test_add_nxm_regression_save_failure_restores_status_and_tombstone(
        svc, tmp_path, monkeypatch):
    # Extraction-failure regression commit + save failure: the entry is
    # flipped downloaded -> pending in memory, but the save fails, so BOTH
    # the status and the tombstone are restored in memory and the on-disk
    # state stands. The result reports failure and never claims success.
    zip_bytes = make_zip()
    cdir = write_state(tmp_path / "col", mods=[
        state_entry(download_status="downloaded", filename="TestMod-1.0.zip"),
    ])
    # No archive on disk: the flag alone must not short-circuit dedupe, so
    # the link re-downloads and the extraction failure takes the regression
    # branch.
    state = reload_state(cdir)
    state.mark_removed(MOD_ID, "Test Mod")       # crafted stale combo
    state.save()
    before = state_raw(cdir)

    svc._api = FakeNexusAPI(files=[{
        "file_id": FILE_ID, "name": "Main File",
        "file_name": "TestMod-1.0.zip", "version": "1.0",
        "size_in_bytes": len(zip_bytes), "category_id": 1,
    }])
    patch_downloader(monkeypatch, zip_bytes)

    def boom(path, target):
        raise service_module.ExtractionError("boom")

    monkeypatch.setattr(service_module, "extract_archive", boom)
    captured: list = []
    monkeypatch.setattr(state_module.CollectionState, "save",
                        _failing_save(captured))

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert not result.success and result.status == "error"
    assert "extraction failed" in result.error
    assert "remains marked as downloaded" in result.error
    assert state_raw(cdir) == before             # prior state bytes stand
    assert len(captured) == 1                    # exactly the one failed save
    st = captured[0]
    assert st.mods[FILE_ID].download_status == "downloaded"   # restored
    assert st.is_removed(MOD_ID)                              # restored


def test_add_nxm_pending_commit_save_failure_returns_error(svc, tmp_path,
                                                           monkeypatch):
    # The new-entry pending commit guards its save like the sibling
    # branches: a save failure returns a failed NxmResult instead of
    # raising a raw error out of add_nxm, and says that import cannot see
    # an unrecorded download.
    zip_bytes = make_zip()
    cdir = write_state(tmp_path / "col")
    state = reload_state(cdir)
    state.mark_removed(MOD_ID, "Test Mod")
    state.save()
    before = state_raw(cdir)

    svc._api = FakeNexusAPI(files=[{
        "file_id": FILE_ID, "name": "Main File",
        "file_name": "TestMod-1.0.zip", "version": "1.0",
        "size_in_bytes": len(zip_bytes), "category_id": 1,
    }])
    patch_downloader(monkeypatch, zip_bytes)

    def boom(path, target):
        raise service_module.ExtractionError("boom")

    monkeypatch.setattr(service_module, "extract_archive", boom)
    monkeypatch.setattr(state_module.CollectionState, "save", _failing_save([]))

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert not result.success and result.status == "error"
    assert "extraction failed" in result.error
    assert "could not be updated" in result.error
    assert "click the download link again" in result.error
    assert state_raw(cdir) == before             # prior state bytes stand


# ---------------------------------------------------------------------------
# (q) Import never resurrects tombstoned mods, but completes recovery for
#     a pending entry committed by an extraction-failed re-click (whose
#     tombstone was cleared at commit time)
# ---------------------------------------------------------------------------

def test_import_does_not_resurrect_tombstoned_mod(svc, tmp_path):
    zip_bytes = make_zip()
    cdir = write_state(tmp_path / "col", mods=[
        state_entry(download_status="pending_download",
                    filename="TestMod-1.0.zip"),
    ])
    (cdir / "TestMod-1.0.zip").write_bytes(zip_bytes)
    state = reload_state(cdir)
    state.mark_removed(MOD_ID, "Test Mod")       # stale pending + tombstone
    state.save()
    state_raw(cdir)

    result = svc.import_downloads(cdir, no_load_order=True)

    assert result.matched == 0 and result.extracted == 0
    state = reload_state(cdir)
    assert state.mods[FILE_ID].download_status == "pending_download"
    assert state.is_removed(MOD_ID)              # tombstone intact
    assert (cdir / "TestMod-1.0.zip").read_bytes() == zip_bytes
    assert not (cdir / "Data").exists()          # nothing extracted


def test_import_completes_recovery_after_pending_commit(svc, tmp_path,
                                                        monkeypatch):
    # Full recovery chain: remove (tombstone) -> re-click fails extraction
    # (pending committed, tombstone cleared) -> import finishes the job.
    zip_bytes = make_zip()
    cdir = write_state(tmp_path / "col", mods=[state_entry()])
    svc.remove_mod(cdir, mod_id=MOD_ID, no_load_order=True)

    svc._api = FakeNexusAPI(files=[{
        "file_id": FILE_ID, "name": "Main File",
        "file_name": "TestMod-1.0.zip", "version": "1.0",
        "size_in_bytes": len(zip_bytes), "category_id": 1,
    }])
    patch_downloader(monkeypatch, zip_bytes)

    def boom(path, target):
        raise service_module.ExtractionError("boom")

    monkeypatch.setattr(service_module, "extract_archive", boom)
    failed = svc.add_nxm(uri(), mods_dir=cdir)
    assert not failed.success
    monkeypatch.undo()                           # extraction works again

    result = svc.import_downloads(cdir, no_load_order=True)

    assert result.matched == 1 and result.extracted == 1
    state = reload_state(cdir)
    assert state.mods[FILE_ID].download_status == "downloaded"
    assert not state.is_removed(MOD_ID)          # cleared at pending commit


# ---------------------------------------------------------------------------
# (r) Comparison buckets stay consistent; stale pending + tombstone is
#     surfaced (to_remove) and never resurrected
# ---------------------------------------------------------------------------

def test_comparison_buckets_exclude_tombstoned_ids(svc, tmp_path):
    cdir = _state_with_live_mod_and_tombstone(tmp_path / "col")
    mods = [
        manifest_entry(123, 999, "Test Mod"),
        manifest_entry(200, 777, "Other Mod"),
        manifest_entry(300, 888, "New Mod"),
    ]
    svc._api = FakeNexusAPI(is_premium=False, collection=collection_data(mods))

    result = svc.update(cdir, no_load_order=True)

    # to_install excludes the tombstoned id entirely; the invariant holds:
    # no state entry exists (or gets created) for a tombstoned mod.
    assert [m["file_id"] for m in result.to_install] == [888]
    assert result.to_update == []
    assert [m["file_id"] for m in result.up_to_date] == [777]
    assert result.removed_skipped == 1
    state = reload_state(cdir)
    assert all(ms.mod_id != 123 for ms in state.mods.values())


def test_stale_pending_plus_tombstone_handled(svc, tmp_path):
    # Crafted state: a pending entry survived alongside its tombstone.
    # Update must not reinstall the page and must surface the stale file
    # in to_remove; the entry stays pending and the tombstone stays.
    cdir = write_state(tmp_path / "col", mods=[
        state_entry(456, 123, "Test Mod", "TestMod-1.0.zip",
                    download_status="pending_download"),
        state_entry(777, 200, "Other Mod", "OtherMod-1.0.zip"),
    ])
    state = reload_state(cdir)
    state.mark_removed(MOD_ID, "Test Mod")
    state.save()

    mods = [
        manifest_entry(123, 999, "Test Mod"),
        manifest_entry(200, 777, "Other Mod"),
    ]
    svc._api = FakeNexusAPI(is_premium=False, collection=collection_data(mods))

    result = svc.update(cdir, no_load_order=True)

    assert result.to_install == [] and result.to_update == []
    assert result.to_remove == [FILE_ID]         # stale remnant surfaced
    assert result.removed_skipped == 1
    state = reload_state(cdir)
    assert state.mods[FILE_ID].download_status == "pending_download"
    assert state.is_removed(MOD_ID)
    assert 999 not in state.mods                 # never re-registered


# ---------------------------------------------------------------------------
# (s) Load order regeneration (sync/update and remove_mod's own regen)
#     excludes removed mods from generated metadata
# ---------------------------------------------------------------------------

def plugin_lines(text):
    """Non-comment, non-empty lines of a generated plugins.txt."""
    return [ln for ln in text.splitlines() if ln and not ln.startswith("#")]


def test_load_order_regen_excludes_removed_mods(svc, tmp_path, monkeypatch):
    # plugins.txt is NOT mod-scoped (documented behavior): collection.json
    # plugin entries carry no mod identity (loadorder._write_plugins lists
    # manifest.plugins filenames only), so a removed mod's plugins
    # legitimately persist there, and mod-level exclusion is guaranteed
    # for load-order.txt (asserted below). What the regen run guarantees
    # for plugins.txt is a rebuild from the CURRENT collection metadata,
    # asserted below by changing the manifest's plugin list and requiring
    # the output to follow instead of keeping stale content.
    manifest_holder = {"plugins": [{"filename": "Core.esm", "enabled": True}]}

    def fake_fetch(url, session=None):
        return CollectionManifest.from_dict({
            "mod_rules": [],
            "plugins": manifest_holder["plugins"],
            "plugin_rules": [],
            "mod_phases": {"100": 0, "200": 0, "300": 0},
        })

    monkeypatch.setattr(
        service_module, "download_and_parse_manifest", fake_fetch
    )
    patch_download_mods(monkeypatch)

    mods = [
        manifest_entry(100, 555, "Alpha Mod"),
        manifest_entry(200, 777, "Beta Mod"),
    ]
    svc._api = FakeNexusAPI(
        is_premium=True,
        collection=collection_data(mods, download_link="https://example.invalid/m"),
    )
    cdir = tmp_path / "Test Collection"

    svc.sync(COLLECTION_URL, tmp_path)
    load_order = (cdir / "load-order.txt").read_text()
    assert "[100:555]" in load_order and "[200:777]" in load_order
    assert plugin_lines((cdir / "plugins.txt").read_text()) == ["*Core.esm"]

    # remove_mod's own post-save regen uses the remaining state mods.
    svc.remove_mod(cdir, mod_id=100)
    load_order = (cdir / "load-order.txt").read_text()
    assert "[200:777]" in load_order
    assert "[100:555]" not in load_order
    assert "Alpha Mod" not in load_order

    # sync/update regen reads the FILTERED manifest input, so the removed
    # page stays out even though the manifest still lists it.
    manifest_holder["plugins"] = [
        {"filename": "Core.esm", "enabled": True},
        {"filename": "Late.esp", "enabled": True},
    ]
    mods = [
        manifest_entry(100, 555, "Alpha Mod"),
        manifest_entry(200, 777, "Beta Mod"),
        manifest_entry(300, 888, "Gamma Mod"),
    ]
    svc._api = FakeNexusAPI(
        is_premium=True,
        collection=collection_data(mods, download_link="https://example.invalid/m"),
    )
    svc.update(cdir)
    load_order = (cdir / "load-order.txt").read_text()
    assert "[200:777]" in load_order and "[300:888]" in load_order
    assert "[100:555]" not in load_order
    assert "Alpha Mod" not in load_order

    # plugins.txt was rebuilt from THIS run's collection metadata (the
    # plugin list changed between runs): that rebuild is the real
    # regeneration guarantee for the plugins output.
    assert plugin_lines((cdir / "plugins.txt").read_text()) == [
        "*Core.esm", "*Late.esp",
    ]


# ---------------------------------------------------------------------------
# (t) Local negative-id mods and legacy mod_id-0 entries are removed
#     WITHOUT a tombstone
# ---------------------------------------------------------------------------

def test_local_negative_id_removed_without_tombstone(svc, tmp_path):
    cdir = write_state(tmp_path / "col")
    synthetic_id = svc.add_local("Local Mod", cdir, no_load_order=True)
    assert synthetic_id < 0

    result = svc.remove_mod(cdir, mod_id=synthetic_id, no_load_order=True)

    assert result.removed_mod_ids == [synthetic_id]
    state = reload_state(cdir)
    assert state.mods == {}
    assert state.removed_mods == {}              # no tombstone for local ids


def test_legacy_mod_id_zero_removed_without_tombstone(svc, tmp_path):
    cdir = write_state(tmp_path / "col", mods=[
        {"file_id": 7, "mod_id": 0, "mod_name": "Legacy Mod",
         "filename": "legacy.zip", "version": ""},
    ])

    result = svc.remove_mod(cdir, file_id=7, no_load_order=True)

    assert result.removed_mod_ids == [0]
    state = reload_state(cdir)
    assert state.mods == {}
    assert state.removed_mods == {}              # id 0 is never tombstoned


# ---------------------------------------------------------------------------
# (u) Page replacement: a NEW mod_id for a tombstoned slot's content
#     installs (tombstones follow the mod page, not the content)
# ---------------------------------------------------------------------------

def test_replacement_page_installs(svc, tmp_path):
    cdir = write_state(tmp_path / "Test Collection",
                       mods=[state_entry(456, 123, "Test Mod")])
    svc.remove_mod(cdir, mod_id=MOD_ID, no_load_order=True)

    # The curator replaced the removed page with a different page.
    mods = [manifest_entry(789, 4321, "Test Mod Reborn")]
    svc._api = FakeNexusAPI(is_premium=False, collection=collection_data(mods))

    result = svc.sync(COLLECTION_URL, tmp_path, no_load_order=True)

    assert result.removed_skipped == 0           # 789 is not tombstoned
    state = reload_state(cdir)
    assert 4321 in state.mods
    assert state.mods[4321].mod_id == 789
    assert state.is_removed(123)                 # old page stays removed
