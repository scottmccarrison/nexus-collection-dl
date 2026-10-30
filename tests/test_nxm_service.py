"""Service-level tests for add_nxm: routing, credentials, dedupe, integrity.

All API access is faked and all HTTP is mocked; no network. Verifies the
approved flow: local expiry with zero requests, user_id identity checks
with no writes, keyless free links failing with actionable guidance,
pending -> download -> extract, proven-duplicate no-ops with zero network,
artifact-gone retries, integrity failures preserving prior state, archive
collisions refused, --output standalone drops without state, credential
redaction in results and state files, and lock contention.
"""

import io
import time
import zipfile
from pathlib import Path

import pytest

from nexus_collection_dl import service as service_module
from nexus_collection_dl.downloader import Downloader
from nexus_collection_dl.handler import NxmRoutingError, add_mapping
from nexus_collection_dl.nxm import NxmCollectionLinkError
from nexus_collection_dl.service import ModManagerService
from nexus_collection_dl.state import CollectionLock, CollectionState, StateBusyError

GAME = "skyrimspecialedition"
MOD_ID = 123
FILE_ID = 456
KEY = "SignedKeyValue-abc123_XYZ"
USER_ID = 4242
FUTURE = int(time.time()) + 3600
PAST = int(time.time()) - 3600

SIGNED_URI = (
    f"nxm://{GAME}/mods/{MOD_ID}/files/{FILE_ID}"
    f"?key={KEY}&expires={{expires}}&user_id={{user_id}}"
)


def uri(expires=FUTURE, user_id=USER_ID, with_key=True):
    if not with_key:
        return f"nxm://{GAME}/mods/{MOD_ID}/files/{FILE_ID}"
    return SIGNED_URI.format(expires=expires, user_id=user_id)


def make_zip(name="Data/test.txt", payload=b"hello mods") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, payload)
    return buf.getvalue()


class FakeNexusAPI:
    """In-memory NexusAPI stand-in recording every call."""

    def __init__(self, is_premium=False, user_id=USER_ID, profile_expiry=None,
                 files=None, mod_name="Test Mod"):
        self._is_premium = is_premium
        self._user_id = user_id
        self._profile_expiry = profile_expiry
        self._files = files if files is not None else []
        self._mod_name = mod_name
        self.calls = []
        self.download_link_kwargs = []
        self.track_calls = []
        self.untrack_calls = []
        self.session = None

    def validate_key(self):
        self.calls.append("validate_key")
        return {
            "user_id": self._user_id,
            "name": "tester",
            "is_premium": self._is_premium,
            "profile_expiry": self._profile_expiry,
        }

    def get_mod_files(self, game_domain, mod_id):
        self.calls.append("get_mod_files")
        return list(self._files)

    def get_mod_info(self, game_domain, mod_id):
        self.calls.append("get_mod_info")
        return {"name": self._mod_name, "mod_id": mod_id}

    def get_download_url(self, game_domain, mod_id, file_id, key=None, expires=None):
        self.calls.append("get_download_url")
        self.download_link_kwargs.append({"key": key, "expires": expires})
        return "https://cdn.example/download?token=CDNTOKEN"

    def get_tracked_mods(self):
        self.calls.append("get_tracked_mods")
        return []

    def track_mod(self, game_domain, mod_id):
        self.track_calls.append((game_domain, mod_id))

    def untrack_mod(self, game_domain, mod_id):
        self.untrack_calls.append((game_domain, mod_id))


class FakeResponse:
    def __init__(self, content):
        self.content = content
        self.status_code = 200
        self.headers = {"content-length": str(len(content))}
        self.closed = False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=8192):
        for i in range(0, len(self.content), 512):
            yield self.content[i:i + 512]

    def close(self):
        self.closed = True


class FakeCdnSession:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.content)


def patch_downloader(monkeypatch, content: bytes) -> FakeCdnSession:
    """Route every Downloader's HTTP through a fake serving `content`."""
    session = FakeCdnSession(content)

    class PatchedDownloader(Downloader):
        def __init__(self, api):
            super().__init__(api)
            self.session = session

    monkeypatch.setattr(service_module, "Downloader", PatchedDownloader)
    return session


def make_collection(tmp_path: Path, game=GAME, name="collection", mods=None) -> Path:
    cdir = tmp_path / name
    cdir.mkdir()
    state = CollectionState(cdir)
    state.set_collection_info(
        f"https://next.nexusmods.com/{game}/collections/testcol",
        "Test Collection", 1, game,
    )
    for m in mods or []:
        state.add_mod(m)
    state.save()
    return cdir


def pending_entry(file_id=FILE_ID, mod_id=MOD_ID, filename="TestMod-1.0.zip"):
    return {
        "mod_id": mod_id,
        "mod_name": "Test Mod",
        "file_id": file_id,
        "filename": filename,
        "version": "1.0",
        "optional": False,
        "manual": False,
        "download_status": "pending_download",
        "browser_url": f"https://www.nexusmods.com/{GAME}/mods/{mod_id}?tab=files",
    }


def files_meta(zip_bytes, file_id=FILE_ID, filename="TestMod-1.0.zip"):
    return [{
        "file_id": file_id,
        "name": "Main File",
        "file_name": filename,
        "version": "1.0",
        "size_in_bytes": len(zip_bytes),
        "category_id": 1,
    }]


@pytest.fixture()
def svc():
    return ModManagerService(api_key="test-key-unused")


@pytest.fixture()
def xdg_config(tmp_path, monkeypatch):
    config_home = tmp_path / "xdg-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    return config_home


def state_raw(cdir: Path) -> bytes:
    return (cdir / ".nexus-state.json").read_bytes()


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_free_signed_link_downloads_pending_entry(svc, tmp_path, monkeypatch):
    zip_bytes = make_zip()
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    fake = FakeNexusAPI(files=files_meta(zip_bytes))
    svc._api = fake
    patch_downloader(monkeypatch, zip_bytes)
    before = state_raw(cdir)

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert result.success, result.error
    assert result.status == "downloaded"
    assert result.extracted is True
    # The credential pair is redeemed exactly as issued; user_id is never sent
    assert fake.download_link_kwargs == [{"key": KEY, "expires": FUTURE}]
    # Extraction happened and the archive was cleaned up
    assert (cdir / "Data" / "test.txt").exists()
    assert not (cdir / "TestMod-1.0.zip").exists()
    # State flipped to downloaded with the browser URL cleared
    state = CollectionState(cdir)
    state.load()
    assert state.mods[FILE_ID].download_status == "downloaded"
    assert state.mods[FILE_ID].browser_url == ""
    # Nothing deployed automatically
    assert state.deployed_files == []
    assert state.deployed_at is None
    # Credentials never reach the state file (before != after, but clean)
    raw = state_raw(cdir)
    assert raw != before
    assert KEY.encode() not in raw
    assert str(FUTURE).encode() not in raw
    assert b"user_id" not in raw
    # ...nor into the result object
    assert KEY not in str(result.__dict__) and KEY not in repr(result)


def test_premium_unsigned_link_registers_new_manual_entry(svc, tmp_path, monkeypatch):
    zip_bytes = make_zip()
    cdir = make_collection(tmp_path)
    fake = FakeNexusAPI(is_premium=True, files=files_meta(zip_bytes))
    svc._api = fake
    patch_downloader(monkeypatch, zip_bytes)

    result = svc.add_nxm(uri(with_key=False), mods_dir=cdir)

    assert result.success, result.error
    assert fake.download_link_kwargs == [{"key": None, "expires": None}]
    state = CollectionState(cdir)
    state.load()
    entry = state.mods[FILE_ID]               # keyed by file_id
    assert MOD_ID not in state.mods           # never keyed by mod_id
    assert entry.mod_id == MOD_ID
    assert entry.manual is True
    assert entry.phase == 999
    assert entry.download_status == "downloaded"
    assert entry.name == "Test Mod"           # from get_mod_info


def test_premium_link_may_still_carry_a_pair(svc, tmp_path, monkeypatch):
    zip_bytes = make_zip()
    cdir = make_collection(tmp_path)
    fake = FakeNexusAPI(is_premium=True, files=files_meta(zip_bytes))
    svc._api = fake
    patch_downloader(monkeypatch, zip_bytes)

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert result.success, result.error
    assert fake.download_link_kwargs == [{"key": KEY, "expires": FUTURE}]


def test_no_extract_keeps_verified_archive(svc, tmp_path, monkeypatch):
    zip_bytes = make_zip()
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    fake = FakeNexusAPI(files=files_meta(zip_bytes))
    svc._api = fake
    patch_downloader(monkeypatch, zip_bytes)

    result = svc.add_nxm(uri(), mods_dir=cdir, no_extract=True)

    assert result.success, result.error
    assert result.extracted is False
    assert (cdir / "TestMod-1.0.zip").read_bytes() == zip_bytes
    state = CollectionState(cdir)
    state.load()
    assert state.mods[FILE_ID].download_status == "downloaded"


def test_metadata_refreshed_on_existing_entry(svc, tmp_path, monkeypatch):
    zip_bytes = make_zip()
    entry = pending_entry(filename="", )
    entry["version"] = ""
    cdir = make_collection(tmp_path, mods=[entry])
    fake = FakeNexusAPI(files=files_meta(zip_bytes))
    svc._api = fake
    patch_downloader(monkeypatch, zip_bytes)

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert result.success, result.error
    state = CollectionState(cdir)
    state.load()
    assert state.mods[FILE_ID].filename == "TestMod-1.0.zip"
    assert state.mods[FILE_ID].version == "1.0"


# ---------------------------------------------------------------------------
# Dedupe: proven local duplicates are no-ops; gone artifacts are retried
# ---------------------------------------------------------------------------

def test_proven_duplicate_is_noop_without_network(svc, tmp_path):
    zip_bytes = make_zip()
    entry = pending_entry()
    entry["download_status"] = "downloaded"
    entry["browser_url"] = ""
    cdir = make_collection(tmp_path, mods=[entry])
    (cdir / "TestMod-1.0.zip").write_bytes(zip_bytes)
    fake = FakeNexusAPI(files=files_meta(zip_bytes))
    svc._api = fake

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert result.success
    assert result.status == "already_downloaded"
    assert fake.calls == []                   # zero network for proven dupes


def test_duplicate_with_corrupt_archive_redownloads(svc, tmp_path, monkeypatch):
    zip_bytes = make_zip()
    entry = pending_entry()
    entry["download_status"] = "downloaded"
    cdir = make_collection(tmp_path, mods=[entry])
    (cdir / "TestMod-1.0.zip").write_bytes(b"garbage-not-a-zip")
    fake = FakeNexusAPI(files=files_meta(zip_bytes))
    svc._api = fake
    patch_downloader(monkeypatch, zip_bytes)

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert result.success, result.error
    assert result.status == "downloaded"      # state flag was not trusted
    assert "get_download_url" in fake.calls


def test_artifact_gone_triggers_retry(svc, tmp_path, monkeypatch):
    zip_bytes = make_zip()
    entry = pending_entry()
    entry["download_status"] = "downloaded"
    cdir = make_collection(tmp_path, mods=[entry])
    # No archive on disk: the flag alone must not short-circuit
    fake = FakeNexusAPI(files=files_meta(zip_bytes))
    svc._api = fake
    patch_downloader(monkeypatch, zip_bytes)

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert result.success, result.error
    assert result.status == "downloaded"
    assert "get_download_url" in fake.calls


# ---------------------------------------------------------------------------
# Credential and account rules
# ---------------------------------------------------------------------------

def test_expired_pair_fails_locally_with_zero_requests(svc, tmp_path):
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    fake = FakeNexusAPI(files=files_meta(make_zip()))
    svc._api = fake
    before = state_raw(cdir)

    result = svc.add_nxm(uri(expires=PAST), mods_dir=cdir)

    assert not result.success
    assert result.re_click_required
    assert "expired" in result.error
    assert fake.calls == []                   # no network at all
    assert state_raw(cdir) == before          # no mutation


def test_mismatched_account_fails_without_writes_or_download(svc, tmp_path):
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    fake = FakeNexusAPI(user_id=999, files=files_meta(make_zip()))
    svc._api = fake
    before = state_raw(cdir)

    result = svc.add_nxm(uri(user_id=USER_ID), mods_dir=cdir)

    assert not result.success
    assert result.re_click_required
    assert "different Nexus account" in result.error
    assert "get_download_url" not in fake.calls
    assert state_raw(cdir) == before
    assert not (cdir / "TestMod-1.0.zip").exists()


def test_keyless_free_link_gets_actionable_reclick(svc, tmp_path):
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    fake = FakeNexusAPI(files=files_meta(make_zip()))
    svc._api = fake
    before = state_raw(cdir)

    result = svc.add_nxm(uri(with_key=False), mods_dir=cdir)

    assert not result.success
    assert result.re_click_required
    assert "Mod manager download" in result.error
    assert f"nexus-dl import {cdir}" in result.error
    assert "get_download_url" not in fake.calls
    assert state_raw(cdir) == before          # no pending registration side effects


def test_lapsed_premium_treated_as_free(svc, tmp_path):
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    fake = FakeNexusAPI(is_premium=True, profile_expiry=PAST,
                        files=files_meta(make_zip()))
    svc._api = fake

    result = svc.add_nxm(uri(with_key=False), mods_dir=cdir)

    assert not result.success
    assert result.re_click_required
    assert "get_download_url" not in fake.calls


# ---------------------------------------------------------------------------
# Consistency: game, identity, collisions
# ---------------------------------------------------------------------------

def test_game_mismatch_refused_with_zero_requests(svc, tmp_path):
    cdir = make_collection(tmp_path, game="starfield", mods=[pending_entry()])
    fake = FakeNexusAPI(files=files_meta(make_zip()))
    svc._api = fake
    before = state_raw(cdir)

    with pytest.raises(NxmRoutingError) as excinfo:
        svc.add_nxm(uri(), mods_dir=cdir)

    assert "starfield" in str(excinfo.value)
    assert fake.calls == []
    assert state_raw(cdir) == before


def test_mismatched_mod_id_for_existing_file_refused(svc, tmp_path):
    cdir = make_collection(tmp_path, mods=[pending_entry(mod_id=999)])
    fake = FakeNexusAPI(files=files_meta(make_zip()))
    svc._api = fake
    before = state_raw(cdir)

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert not result.success
    assert "already registered under mod 999" in result.error
    assert fake.calls == []                   # refused before any network
    assert state_raw(cdir) == before


def test_archive_collision_with_other_file_id_refused(svc, tmp_path):
    zip_bytes = make_zip()
    other = pending_entry(file_id=777, mod_id=888)
    other["download_status"] = "downloaded"
    cdir = make_collection(tmp_path, mods=[other])
    fake = FakeNexusAPI(files=files_meta(zip_bytes))
    svc._api = fake

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert not result.success
    assert "belongs to file 777" in result.error
    assert "get_download_url" not in fake.calls


def test_untracked_existing_archive_not_overwritten(svc, tmp_path):
    zip_bytes = make_zip()
    cdir = make_collection(tmp_path)            # no entry for FILE_ID
    (cdir / "TestMod-1.0.zip").write_bytes(b"someone elses file")
    fake = FakeNexusAPI(files=files_meta(zip_bytes))
    svc._api = fake

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert not result.success
    assert "Refusing to overwrite" in result.error
    assert "get_download_url" not in fake.calls
    assert (cdir / "TestMod-1.0.zip").read_bytes() == b"someone elses file"


# ---------------------------------------------------------------------------
# Failure handling: integrity, extraction
# ---------------------------------------------------------------------------

def test_truncated_download_preserves_state_and_files(svc, tmp_path, monkeypatch):
    zip_bytes = make_zip()
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    fake = FakeNexusAPI(files=files_meta(zip_bytes))
    svc._api = fake
    patch_downloader(monkeypatch, zip_bytes[:-5])  # truncated payload
    before = state_raw(cdir)

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert not result.success
    assert "size mismatch" in result.error
    assert state_raw(cdir) == before           # still pending, unchanged
    assert not (cdir / "TestMod-1.0.zip").exists()
    assert not [p for p in cdir.iterdir() if p.name.startswith(".nexus-download-")]


def test_extraction_failure_never_claims_completion(svc, tmp_path, monkeypatch):
    zip_bytes = make_zip()
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    fake = FakeNexusAPI(files=files_meta(zip_bytes))
    svc._api = fake
    patch_downloader(monkeypatch, zip_bytes)

    def boom(path, target):
        raise service_module.ExtractionError("boom")

    monkeypatch.setattr(service_module, "extract_archive", boom)

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert not result.success
    assert "extraction failed" in result.error
    assert f"nexus-dl import {cdir}" in result.error
    # The verified archive is kept and the entry stays pending
    assert (cdir / "TestMod-1.0.zip").read_bytes() == zip_bytes
    state = CollectionState(cdir)
    state.load()
    assert state.mods[FILE_ID].download_status == "pending_download"


def test_extraction_failure_registers_new_entry_as_pending(svc, tmp_path, monkeypatch):
    zip_bytes = make_zip()
    cdir = make_collection(tmp_path)
    fake = FakeNexusAPI(is_premium=True, files=files_meta(zip_bytes))
    svc._api = fake
    patch_downloader(monkeypatch, zip_bytes)

    def boom(path, target):
        raise service_module.ExtractionError("boom")

    monkeypatch.setattr(service_module, "extract_archive", boom)

    result = svc.add_nxm(uri(with_key=False), mods_dir=cdir)

    assert not result.success
    state = CollectionState(cdir)
    state.load()
    entry = state.mods[FILE_ID]
    assert entry.download_status == "pending_download"
    assert entry.phase == 999
    assert (cdir / "TestMod-1.0.zip").exists()


def test_missing_file_on_mod_reported(svc, tmp_path):
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    fake = FakeNexusAPI(files=files_meta(make_zip(), file_id=99999))
    svc._api = fake

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert not result.success
    assert "not found on mod" in result.error
    assert "get_download_url" not in fake.calls


# ---------------------------------------------------------------------------
# --output: standalone verified archive drop, no state
# ---------------------------------------------------------------------------

def test_output_saves_archive_without_state(svc, tmp_path, monkeypatch):
    zip_bytes = make_zip()
    out = tmp_path / "out"
    out.mkdir()
    fake = FakeNexusAPI(files=files_meta(zip_bytes))
    svc._api = fake
    patch_downloader(monkeypatch, zip_bytes)

    result = svc.add_nxm(uri(), output_dir=out)

    assert result.success, result.error
    assert result.status == "saved_to_output"
    assert result.output_path == out / "TestMod-1.0.zip"
    assert result.output_path.read_bytes() == zip_bytes
    assert list(out.iterdir()) == [out / "TestMod-1.0.zip"]  # nothing else created
    assert not (out / ".nexus-state.json").exists()


def test_output_rejects_non_archive_payload(svc, tmp_path, monkeypatch):
    html = b"<html>error page</html>"
    out = tmp_path / "out"
    out.mkdir()
    fake = FakeNexusAPI(files=files_meta(html))  # size matches the html payload
    svc._api = fake
    patch_downloader(monkeypatch, html)

    result = svc.add_nxm(uri(), output_dir=out)

    assert not result.success
    assert "archive" in result.error
    assert list(out.iterdir()) == []           # dropped payload removed


def test_output_requires_existing_directory(svc, tmp_path):
    fake = FakeNexusAPI(files=files_meta(make_zip()))
    svc._api = fake

    with pytest.raises(NxmRoutingError):
        svc.add_nxm(uri(), output_dir=tmp_path / "missing")
    assert not (tmp_path / "missing").exists()


# ---------------------------------------------------------------------------
# Routing and locking
# ---------------------------------------------------------------------------

def test_unmapped_game_fails_without_creating_anything(svc, tmp_path, xdg_config):
    fake = FakeNexusAPI(files=files_meta(make_zip()))
    svc._api = fake

    with pytest.raises(NxmRoutingError) as excinfo:
        svc.add_nxm(uri())

    assert "handler map" in str(excinfo.value)
    assert fake.calls == []
    assert not xdg_config.exists()             # no config written
    assert list(tmp_path.iterdir()) == []      # nothing else created either


def test_mapped_collection_used_without_mods_dir(svc, tmp_path, monkeypatch, xdg_config):
    zip_bytes = make_zip()
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    add_mapping(GAME, cdir)
    fake = FakeNexusAPI(files=files_meta(zip_bytes))
    svc._api = fake
    patch_downloader(monkeypatch, zip_bytes)

    result = svc.add_nxm(uri())                # no --mods-dir

    assert result.success, result.error
    assert result.collection_dir == cdir


def test_ambiguous_mapping_is_actionable_failure(svc, tmp_path, xdg_config):
    cdir_a = make_collection(tmp_path, name="col-a", mods=[pending_entry()])
    cdir_b = make_collection(tmp_path, name="col-b", mods=[pending_entry()])
    add_mapping(GAME, cdir_a)
    add_mapping(GAME, cdir_b)
    fake = FakeNexusAPI(files=files_meta(make_zip()))
    svc._api = fake

    with pytest.raises(NxmRoutingError) as excinfo:
        svc.add_nxm(uri())

    message = str(excinfo.value)
    assert str(cdir_a) in message and str(cdir_b) in message
    assert "--mods-dir" in message
    assert fake.calls == []


def test_explicit_dir_without_state_refused_no_creation(svc, tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    fake = FakeNexusAPI(files=files_meta(make_zip()))
    svc._api = fake

    with pytest.raises(NxmRoutingError):
        svc.add_nxm(uri(), mods_dir=bare)

    assert list(bare.iterdir()) == []          # no state or lock file created
    assert fake.calls == []


def test_busy_lock_blocks_nxm_before_network(svc, tmp_path):
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    fake = FakeNexusAPI(files=files_meta(make_zip()))
    svc._api = fake

    with CollectionLock(cdir):
        with pytest.raises(StateBusyError):
            svc.add_nxm(uri(), mods_dir=cdir)
    assert fake.calls == []


def test_collection_link_recognized_with_sync_guidance(svc, tmp_path):
    cdir = make_collection(tmp_path)
    fake = FakeNexusAPI()
    svc._api = fake

    with pytest.raises(NxmCollectionLinkError) as excinfo:
        svc.add_nxm(f"nxm://{GAME}/collections/cool-list/revisions/4", mods_dir=cdir)

    assert "nexus-dl sync" in str(excinfo.value)
    assert fake.calls == []
