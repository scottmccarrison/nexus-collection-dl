"""Service-level tests for add_nxm: routing, credentials, dedupe, integrity.

All API access is faked and all HTTP is mocked; no network. Verifies the
approved flow: local expiry with zero requests, user_id identity checks
with no writes, keyless free links failing with actionable guidance,
pending -> download -> extract, proven-duplicate no-ops with zero network,
artifact-gone retries, integrity failures preserving prior state, archive
collisions refused, --output standalone drops without state, credential
redaction in results and state files, and lock contention.
"""

import hashlib
import io
import json
import time
import zipfile
from pathlib import Path

import pytest
import requests

from nexus_collection_dl import service as service_module
from nexus_collection_dl.api import NexusAPI, NexusAPIError
from nexus_collection_dl.downloader import Downloader
from nexus_collection_dl.handler import NxmRoutingError, add_mapping
from nexus_collection_dl.nxm import NxmCollectionLinkError
from nexus_collection_dl.service import ModManagerService
from nexus_collection_dl.state import (
    CollectionLock,
    CollectionState,
    StateBusyError,
    StateError,
)

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


def test_duplicate_with_corrupt_archive_refuses_to_replace(svc, tmp_path, monkeypatch):
    """A corrupt state-owned archive is never replaced by the nxm flow.

    Publication is no-clobber even for this file's own tracked archive:
    the publish fails with actionable guidance, the on-disk file and the
    state stay untouched, and no temp download is left behind. The user
    removes the corrupt file (or uses --output) and re-clicks; the
    artifact-gone path then re-downloads cleanly.
    """
    zip_bytes = make_zip()
    entry = pending_entry()
    entry["download_status"] = "downloaded"
    cdir = make_collection(tmp_path, mods=[entry])
    (cdir / "TestMod-1.0.zip").write_bytes(b"garbage-not-a-zip")
    fake = FakeNexusAPI(files=files_meta(zip_bytes))
    svc._api = fake
    patch_downloader(monkeypatch, zip_bytes)
    before = state_raw(cdir)

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert not result.success
    assert "refusing to overwrite" in result.error
    assert "--output" in result.error                 # actionable guidance
    assert f"nexus-dl import {cdir}" in result.error  # names the import recovery
    assert (cdir / "TestMod-1.0.zip").read_bytes() == b"garbage-not-a-zip"
    assert state_raw(cdir) == before                  # entry still downloaded
    assert not [p for p in cdir.iterdir() if p.name.startswith(".nexus-download-")]
    assert KEY not in result.error and str(FUTURE) not in result.error


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


def test_extraction_failure_regresses_downloaded_entry_to_pending(
    svc, tmp_path, monkeypatch
):
    """A previously-downloaded entry must not strand a fresh archive.

    The entry says 'downloaded' but its artifact is gone, so the link
    re-downloads and extraction then fails. A failed extraction retains
    the archive on disk, and dedupe to already_downloaded applies only
    to such retained valid archives: without the regression to pending,
    the entry would stay 'downloaded' with an unextracted archive,
    re-clicks would dedupe to already_downloaded forever (zero network,
    nothing extracted), and import only processes pending entries.
    """
    zip_bytes = make_zip()
    entry = pending_entry()
    entry["download_status"] = "downloaded"
    entry["browser_url"] = ""
    cdir = make_collection(tmp_path, mods=[entry])
    # No archive on disk: the flag alone must not short-circuit dedupe.
    fake = FakeNexusAPI(files=files_meta(zip_bytes))
    svc._api = fake
    patch_downloader(monkeypatch, zip_bytes)

    real_extract = service_module.extract_archive
    calls = []

    def flaky_extract(path, target):
        calls.append(Path(path))
        if len(calls) == 1:
            raise service_module.ExtractionError("boom")
        return real_extract(path, target)

    monkeypatch.setattr(service_module, "extract_archive", flaky_extract)

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert not result.success
    assert "extraction failed" in result.error
    assert f"nexus-dl import {cdir}" in result.error
    # The verified archive is retained and the entry regressed to pending
    assert (cdir / "TestMod-1.0.zip").read_bytes() == zip_bytes
    state = CollectionState(cdir)
    state.load()
    assert state.mods[FILE_ID].download_status == "pending_download"

    # The advised import now finishes the job: extraction succeeds on
    # the second attempt, the entry is downloaded, the archive is gone.
    import_result = svc.import_downloads(cdir)
    assert import_result.matched == 1
    assert import_result.extracted == 1
    assert import_result.still_pending == 0
    assert import_result.errors == []
    assert (cdir / "Data" / "test.txt").exists()
    assert not (cdir / "TestMod-1.0.zip").exists()
    state = CollectionState(cdir)
    state.load()
    assert state.mods[FILE_ID].download_status == "downloaded"


def test_extraction_failure_pending_entry_stays_pending(svc, tmp_path, monkeypatch):
    """An already-pending entry keeps its prior state semantics: no
    regression save is attempted and the on-disk state is untouched."""
    zip_bytes = make_zip()
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    fake = FakeNexusAPI(files=files_meta(zip_bytes))
    svc._api = fake
    patch_downloader(monkeypatch, zip_bytes)

    def boom(path, target):
        raise service_module.ExtractionError("boom")

    monkeypatch.setattr(service_module, "extract_archive", boom)

    def no_save(self):
        raise AssertionError("pending entries must not trigger a save")

    monkeypatch.setattr(CollectionState, "save", no_save)
    before = state_raw(cdir)

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert not result.success
    assert "extraction failed" in result.error
    assert state_raw(cdir) == before
    assert (cdir / "TestMod-1.0.zip").read_bytes() == zip_bytes


def test_extraction_failure_save_error_keeps_prior_state(svc, tmp_path, monkeypatch):
    """When the regression save itself fails, the honest extraction
    error is still reported, the save failure is disclosed, and the
    prior on-disk state stands unchanged (entry remains 'downloaded')."""
    zip_bytes = make_zip()
    entry = pending_entry()
    entry["download_status"] = "downloaded"
    entry["browser_url"] = ""
    cdir = make_collection(tmp_path, mods=[entry])
    fake = FakeNexusAPI(files=files_meta(zip_bytes))
    svc._api = fake
    patch_downloader(monkeypatch, zip_bytes)

    def boom(path, target):
        raise service_module.ExtractionError("boom")

    monkeypatch.setattr(service_module, "extract_archive", boom)

    def failing_save(self):
        raise StateError("disk on fire")

    monkeypatch.setattr(CollectionState, "save", failing_save)
    before = state_raw(cdir)

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert not result.success
    assert "extraction failed" in result.error   # primary error intact
    assert "disk on fire" in result.error        # save failure disclosed
    assert "remains marked as downloaded" in result.error
    assert state_raw(cdir) == before             # prior state stands
    assert (cdir / "TestMod-1.0.zip").read_bytes() == zip_bytes


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


# ---------------------------------------------------------------------------
# Bugcheck regressions: race-safe publish, save-failure retention,
# warnings, malformed metadata, fail-closed account verification
# ---------------------------------------------------------------------------

def test_output_refuses_existing_regular_file_before_download(svc, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "TestMod-1.0.zip").write_bytes(b"sentinel-original")
    fake = FakeNexusAPI(files=files_meta(make_zip()))
    svc._api = fake

    result = svc.add_nxm(uri(), output_dir=out)

    assert not result.success
    assert "already exists" in result.error
    assert "get_download_url" not in fake.calls     # refused before download
    assert (out / "TestMod-1.0.zip").read_bytes() == b"sentinel-original"


def test_output_refuses_symlink_and_dangling_before_download(svc, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    other = tmp_path / "other.zip"
    other.write_bytes(b"sentinel-target")
    link = out / "TestMod-1.0.zip"
    link.symlink_to(other)
    fake = FakeNexusAPI(files=files_meta(make_zip()))
    svc._api = fake

    result = svc.add_nxm(uri(), output_dir=out)
    assert not result.success
    assert "get_download_url" not in fake.calls
    assert other.read_bytes() == b"sentinel-target"

    link.unlink()
    link.symlink_to(tmp_path / "nowhere.zip")       # dangling symlink
    result = svc.add_nxm(uri(), output_dir=out)
    assert not result.success
    assert "get_download_url" not in fake.calls
    assert link.is_symlink()                        # dangling link preserved


def test_output_concurrent_creator_wins_and_is_preserved(svc, tmp_path, monkeypatch):
    zip_bytes = make_zip()
    out = tmp_path / "out"
    out.mkdir()
    fake = FakeNexusAPI(files=files_meta(zip_bytes))
    svc._api = fake

    class RacingSession(FakeCdnSession):
        def get(self, url, **kwargs):
            # External writer creates the destination mid-download
            (out / "TestMod-1.0.zip").write_bytes(b"external-sentinel")
            return super().get(url, **kwargs)

    session = RacingSession(zip_bytes)

    class PatchedDownloader(Downloader):
        def __init__(self, api):
            super().__init__(api)
            self.session = session

    monkeypatch.setattr(service_module, "Downloader", PatchedDownloader)

    result = svc.add_nxm(uri(), output_dir=out)

    assert not result.success
    assert "refusing to overwrite" in result.error
    assert (out / "TestMod-1.0.zip").read_bytes() == b"external-sentinel"
    assert KEY not in result.error and str(FUTURE) not in result.error
    assert not [p for p in out.iterdir() if p.name.startswith(".nexus-download-")]


def test_output_hardlink_unsupported_fails_safely(svc, tmp_path, monkeypatch):
    zip_bytes = make_zip()
    out = tmp_path / "out"
    out.mkdir()
    fake = FakeNexusAPI(files=files_meta(zip_bytes))
    svc._api = fake
    patch_downloader(monkeypatch, zip_bytes)

    import nexus_collection_dl.downloader as downloader_module

    def no_links(src, dst, **kwargs):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(downloader_module.os, "link", no_links)

    result = svc.add_nxm(uri(), output_dir=out)

    assert not result.success
    assert "does not support" in result.error
    assert list(out.iterdir()) == []                # nothing published or left


def test_state_save_failure_preserves_archive_and_pending(svc, tmp_path, monkeypatch):
    zip_bytes = make_zip()
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    fake = FakeNexusAPI(files=files_meta(zip_bytes))
    svc._api = fake
    patch_downloader(monkeypatch, zip_bytes)
    before = state_raw(cdir)

    def fail_save(self):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(service_module.CollectionState, "save", fail_save)

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert not result.success
    assert "could not be saved" in result.error
    assert "import" in result.error
    assert (cdir / "TestMod-1.0.zip").read_bytes() == zip_bytes   # archive kept
    assert (cdir / "Data" / "test.txt").exists()    # honest about extraction
    assert state_raw(cdir) == before                # on-disk state unchanged
    state = CollectionState(cdir)
    state.load()
    assert state.mods[FILE_ID].download_status == "pending_download"


def test_load_order_failure_yields_success_with_warning(svc, tmp_path, monkeypatch):
    zip_bytes = make_zip()
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    fake = FakeNexusAPI(files=files_meta(zip_bytes))
    svc._api = fake
    patch_downloader(monkeypatch, zip_bytes)

    def boom(self, target, state):
        raise RuntimeError("loot exploded")

    monkeypatch.setattr(
        service_module.ModManagerService, "_regen_load_order_from_state", boom
    )

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert result.success, result.error
    assert result.status == "downloaded"
    assert len(result.warnings) == 1
    assert "Load order regeneration failed" in result.warnings[0]
    assert "nexus-dl load-order" in result.warnings[0]
    state = CollectionState(cdir)
    state.load()
    assert state.mods[FILE_ID].download_status == "downloaded"


def test_malformed_size_metadata_fails_without_download(svc, tmp_path):
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    meta = files_meta(make_zip())
    meta[0]["size_in_bytes"] = "twelve-ish"
    fake = FakeNexusAPI(files=meta)
    svc._api = fake

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert not result.success
    assert "malformed" in result.error
    assert "get_download_url" not in fake.calls


def test_missing_account_id_fails_closed(svc, tmp_path):
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    fake = FakeNexusAPI(files=files_meta(make_zip()))
    fake._user_id = None                            # no account id in response
    svc._api = fake
    before = state_raw(cdir)

    result = svc.add_nxm(uri(), mods_dir=cdir)      # link carries user_id

    assert not result.success
    assert "Could not verify" in result.error
    assert "get_download_url" not in fake.calls
    assert state_raw(cdir) == before


def test_nonjson_api_response_at_service_boundary(svc, tmp_path):
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    from nexus_collection_dl.api import NexusAPI
    from nexus_collection_dl.api import NexusAPIError as ApiError

    class HtmlResponse:
        status_code = 200
        headers = {}
        url = "https://api.nexusmods.com/v1/validate.json"

        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    class HtmlSession:
        headers = {}

        def get(self, url, **kwargs):
            return HtmlResponse()

    real = NexusAPI(api_key="test-key")
    real.session = HtmlSession()
    svc._api = real

    with pytest.raises(ApiError) as excinfo:
        svc.add_nxm(uri(), mods_dir=cdir)
    msg = str(excinfo.value)
    assert "non-JSON" in msg
    assert "<html>" not in msg


# ---------------------------------------------------------------------------
# Adversarial-review hardening: bounded translated transport errors,
# publish-time no-clobber, verify-or-refuse, empty-domain fail-closed
# ---------------------------------------------------------------------------

def _json_response(payload, status=200):
    """Real requests.Response carrying JSON, for fake transport sessions."""
    r = requests.Response()
    r.status_code = status
    r.url = "https://api.nexusmods.com/v1/x"
    r.reason = "Status"
    r.headers = {}
    r._content = json.dumps(payload).encode()
    return r


def test_api_timeout_fails_bounded_state_untouched_lock_released(svc, tmp_path):
    """A stalling API endpoint fails within the request timeout instead
    of hanging forever under the collection lock; the raw requests
    exception text (which embeds the URL and query) never surfaces."""
    cdir = make_collection(tmp_path, mods=[pending_entry()])

    class TimeoutSession:
        headers = {}

        def get(self, url, **kwargs):
            # Every request in the nxm flow must carry an explicit timeout
            assert kwargs.get("timeout") is not None
            raise requests.Timeout(
                f"Read timed out. (url={url}?key={KEY}&expires={FUTURE})"
            )

    real = NexusAPI(api_key="test-key")
    real.session = TimeoutSession()
    real._min_request_interval = 0
    svc._api = real
    before = state_raw(cdir)

    with pytest.raises(NexusAPIError) as excinfo:
        svc.add_nxm(uri(), mods_dir=cdir)

    msg = str(excinfo.value)
    assert "timed out" in msg
    assert KEY not in msg and str(FUTURE) not in msg
    assert "key=" not in msg and "http" not in msg.lower()
    assert state_raw(cdir) == before                  # state untouched
    with CollectionLock(cdir):                        # lock was released
        pass


def test_metadata_connection_error_translated_and_redacted(svc, tmp_path):
    """Transport failures after account validation (get_mod_files) are
    translated into the domain error the CLI handlers understand."""
    cdir = make_collection(tmp_path, mods=[pending_entry()])

    class FlakySession:
        headers = {}

        def get(self, url, **kwargs):
            assert kwargs.get("timeout") is not None
            if "validate" in url:
                return _json_response({"user_id": USER_ID, "is_premium": False})
            raise requests.ConnectionError(
                "HTTPSConnectionPool(host='api.nexusmods.com', port=443): "
                f"Max retries exceeded with url: {url}?key={KEY}"
            )

    real = NexusAPI(api_key="test-key")
    real.session = FlakySession()
    real._min_request_interval = 0
    svc._api = real
    before = state_raw(cdir)

    with pytest.raises(NexusAPIError) as excinfo:
        svc.add_nxm(uri(), mods_dir=cdir)

    msg = str(excinfo.value)
    assert "connection" in msg.lower()
    assert "https://" not in msg and "api.nexusmods.com" not in msg
    assert KEY not in msg and "key=" not in msg
    assert state_raw(cdir) == before
    with CollectionLock(cdir):
        pass


def test_destination_created_mid_download_is_never_overwritten(
    svc, tmp_path, monkeypatch
):
    """Collection-flow TOCTOU: an external writer creates the destination
    after the precheck; the publish refuses, preserves the external file,
    and cleans up the temp download."""
    zip_bytes = make_zip()
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    fake = FakeNexusAPI(files=files_meta(zip_bytes))
    svc._api = fake

    class RacingSession(FakeCdnSession):
        def get(self, url, **kwargs):
            # External writer creates the destination mid-download
            (cdir / "TestMod-1.0.zip").write_bytes(b"external-sentinel")
            return super().get(url, **kwargs)

    session = RacingSession(zip_bytes)

    class PatchedDownloader(Downloader):
        def __init__(self, api):
            super().__init__(api)
            self.session = session

    monkeypatch.setattr(service_module, "Downloader", PatchedDownloader)
    before = state_raw(cdir)

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert not result.success
    assert "refusing to overwrite" in result.error
    assert "--output" in result.error
    assert (cdir / "TestMod-1.0.zip").read_bytes() == b"external-sentinel"
    assert not [p for p in cdir.iterdir() if p.name.startswith(".nexus-download-")]
    assert state_raw(cdir) == before                  # entry still pending
    assert KEY not in result.error and str(FUTURE) not in result.error


def test_zero_size_without_md5_refused_without_writes(svc, tmp_path, monkeypatch):
    """No positive byte size and no cached checksum means nothing could
    be verified: refuse before any download and write nothing."""
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    meta = files_meta(make_zip())
    meta[0]["size_in_bytes"] = 0
    fake = FakeNexusAPI(files=meta)
    svc._api = fake
    patch_downloader(monkeypatch, make_zip())
    before = state_raw(cdir)

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert not result.success
    assert "could not be verified" in result.error
    assert "nothing was written" in result.error
    assert "get_download_url" not in fake.calls
    assert state_raw(cdir) == before
    assert not (cdir / "TestMod-1.0.zip").exists()


def test_missing_size_without_md5_refused_without_writes(svc, tmp_path, monkeypatch):
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    meta = files_meta(make_zip())
    meta[0]["size_in_bytes"] = None
    fake = FakeNexusAPI(files=meta)
    svc._api = fake
    patch_downloader(monkeypatch, make_zip())

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert not result.success
    assert "could not be verified" in result.error
    assert "get_download_url" not in fake.calls


def test_zero_size_with_cached_manifest_md5_proceeds(svc, tmp_path, monkeypatch):
    """MD5-only verification is sufficient: the cached manifest checksum
    lets the download proceed (and be verified) with no byte size."""
    zip_bytes = make_zip()
    md5 = hashlib.md5(zip_bytes).hexdigest()
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    state = CollectionState(cdir)
    state.load()
    state.manifest_data = {
        "mod_rules": [{
            "source": {
                "modId": MOD_ID,
                "fileID": FILE_ID,
                "fileMD5": md5,
                "logicalFileName": "TestMod-1.0.zip",
            },
        }]
    }
    state.save()
    meta = files_meta(zip_bytes)
    meta[0]["size_in_bytes"] = 0
    fake = FakeNexusAPI(files=meta)
    svc._api = fake
    patch_downloader(monkeypatch, zip_bytes)

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert result.success, result.error
    assert result.status == "downloaded"
    state = CollectionState(cdir)
    state.load()
    assert state.mods[FILE_ID].download_status == "downloaded"


def test_output_zero_size_refused_without_writes(svc, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    meta = files_meta(make_zip())
    meta[0]["size_in_bytes"] = None
    fake = FakeNexusAPI(files=meta)
    svc._api = fake

    result = svc.add_nxm(uri(), output_dir=out)

    assert not result.success
    assert "could not be verified" in result.error
    assert "get_download_url" not in fake.calls
    assert list(out.iterdir()) == []


def _make_empty_domain_state(cdir: Path) -> Path:
    """A collection dir whose state carries no game domain."""
    cdir.mkdir(parents=True, exist_ok=True)
    state = CollectionState(cdir)
    state.collection_name = "No Domain"
    state.save()                      # game_domain stays ""
    return cdir


def test_empty_domain_state_refused_via_mods_dir(svc, tmp_path):
    cdir = _make_empty_domain_state(tmp_path / "col")
    fake = FakeNexusAPI(files=files_meta(make_zip()))
    svc._api = fake

    with pytest.raises(NxmRoutingError, match="no game domain"):
        svc.add_nxm(uri(), mods_dir=cdir)
    assert fake.calls == []           # refused before any network


def test_empty_domain_state_refused_via_mapping(svc, tmp_path, xdg_config):
    cdir = make_collection(tmp_path)
    add_mapping(GAME, cdir)
    # The mapped collection's state loses its domain afterwards
    state = CollectionState(cdir)
    state.load()
    state.game_domain = ""
    state.save()
    fake = FakeNexusAPI(files=files_meta(make_zip()))
    svc._api = fake

    with pytest.raises(NxmRoutingError, match="no game domain"):
        svc.add_nxm(uri())
    assert fake.calls == []


def test_service_side_empty_domain_check_fails_closed(svc, tmp_path, monkeypatch):
    """Defense in depth: even with routing bypassed, add_nxm refuses a
    no-domain state under the lock, with zero network."""
    cdir = _make_empty_domain_state(tmp_path / "col")
    monkeypatch.setattr(
        service_module, "resolve_collection_dir",
        lambda game, explicit=None: cdir,
    )
    fake = FakeNexusAPI(files=files_meta(make_zip()))
    svc._api = fake

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert not result.success
    assert "no game domain" in result.error
    assert "nexus-dl sync" in result.error
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Re-click behavior, end to end: the default flow deletes the extracted
# archive, so a re-click re-downloads (verified); the zero-network
# already_downloaded dedupe applies only to retained archives.
# ---------------------------------------------------------------------------

def test_reclick_after_extraction_redownloads_verified(svc, tmp_path, monkeypatch):
    """Default flow: download + extract deletes the archive, so the same
    link clicked again performs a fresh verified download instead of
    deduping. State stays consistent across both runs."""
    zip_bytes = make_zip()
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    fake = FakeNexusAPI(files=files_meta(zip_bytes))
    svc._api = fake
    session = patch_downloader(monkeypatch, zip_bytes)

    first = svc.add_nxm(uri(), mods_dir=cdir)
    assert first.success and first.status == "downloaded"
    assert first.extracted is True
    assert not (cdir / "TestMod-1.0.zip").exists()      # archive deleted
    assert (cdir / "Data" / "test.txt").exists()
    downloads_after_first = fake.calls.count("get_download_url")
    sessions_after_first = len(session.calls)
    assert downloads_after_first == 1

    second = svc.add_nxm(uri(), mods_dir=cdir)
    assert second.success, second.error
    # NOT a dedupe: a real second download happened and was verified
    assert second.status == "downloaded"
    assert fake.calls.count("get_download_url") == downloads_after_first + 1
    assert len(session.calls) == sessions_after_first + 1
    assert (cdir / "Data" / "test.txt").read_bytes() == b"hello mods"
    assert not (cdir / "TestMod-1.0.zip").exists()      # cleaned up again
    state = CollectionState(cdir)
    state.load()
    assert state.mods[FILE_ID].download_status == "downloaded"
    assert not [p for p in cdir.iterdir() if p.name.startswith(".nexus-download-")]


def test_reclick_with_retained_archive_dedupes_zero_network(
    svc, tmp_path, monkeypatch
):
    """--no-extract retains the verified archive, so the second click is
    the real dedupe path: already_downloaded with zero network calls."""
    zip_bytes = make_zip()
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    fake = FakeNexusAPI(files=files_meta(zip_bytes))
    svc._api = fake
    session = patch_downloader(monkeypatch, zip_bytes)

    first = svc.add_nxm(uri(), mods_dir=cdir, no_extract=True)
    assert first.success and first.status == "downloaded"
    assert first.extracted is False
    assert (cdir / "TestMod-1.0.zip").read_bytes() == zip_bytes  # retained
    calls_after_first = list(fake.calls)
    sessions_after_first = len(session.calls)

    second = svc.add_nxm(uri(), mods_dir=cdir)
    assert second.success
    assert second.status == "already_downloaded"
    assert fake.calls == calls_after_first              # zero further calls
    assert len(session.calls) == sessions_after_first   # zero CDN traffic
    assert (cdir / "TestMod-1.0.zip").read_bytes() == zip_bytes


# ---------------------------------------------------------------------------
# C1: a malformed cached checksum never counts as a known MD5
# ---------------------------------------------------------------------------

def _state_with_manifest_md5(cdir: Path, md5: str) -> None:
    state = CollectionState(cdir)
    state.load()
    state.manifest_data = {
        "mod_rules": [{
            "source": {
                "modId": MOD_ID,
                "fileID": FILE_ID,
                "fileMD5": md5,
                "logicalFileName": "TestMod-1.0.zip",
            },
        }]
    }
    state.save()


def test_expected_md5_maps_skips_malformed_hashes():
    good = "a" * 32
    data = {
        "mod_rules": [
            {"source": {"modId": 1, "fileID": 2, "fileMD5": "not-a-hash",
                        "logicalFileName": "bad.zip"}},
            {"source": {"modId": 3, "fileID": 4, "fileMD5": good.upper(),
                        "logicalFileName": "good.zip"}},
        ]
    }
    by_identity, by_name = service_module._expected_md5_maps(data)
    assert by_identity == {(3, 4): good}        # malformed dropped, case folded
    assert by_name == {"good.zip": good}


def test_zero_size_with_malformed_md5_refused_without_writes(
    svc, tmp_path, monkeypatch
):
    """size=0 + fileMD5='not-a-hash' must refuse: a malformed hash would
    be silently skipped by the downloader, publishing unchecked content."""
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    _state_with_manifest_md5(cdir, "not-a-hash")
    meta = files_meta(make_zip())
    meta[0]["size_in_bytes"] = 0
    fake = FakeNexusAPI(files=meta)
    svc._api = fake
    patch_downloader(monkeypatch, make_zip())
    before = state_raw(cdir)

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert not result.success
    assert "could not be verified" in result.error
    assert "nothing was written" in result.error
    assert "get_download_url" not in fake.calls
    assert state_raw(cdir) == before
    assert not (cdir / "TestMod-1.0.zip").exists()


def test_malformed_md5_with_positive_size_proceeds_on_size(
    svc, tmp_path, monkeypatch
):
    """A malformed cached hash is ignored (never enforced as a checksum);
    the positive byte size alone verifies the download."""
    zip_bytes = make_zip()
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    _state_with_manifest_md5(cdir, "not-a-hash")
    fake = FakeNexusAPI(files=files_meta(zip_bytes))    # size = len(zip_bytes)
    svc._api = fake
    patch_downloader(monkeypatch, zip_bytes)

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert result.success, result.error
    assert result.status == "downloaded"


def test_zero_size_md5_mismatch_refused(svc, tmp_path, monkeypatch):
    """MD5-only verification is real: wrong content under a well-formed
    cached hash is refused, not published."""
    zip_bytes = make_zip()
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    _state_with_manifest_md5(cdir, hashlib.md5(zip_bytes).hexdigest())
    meta = files_meta(zip_bytes)
    meta[0]["size_in_bytes"] = 0                        # size check off
    fake = FakeNexusAPI(files=meta)
    svc._api = fake
    tampered = make_zip(payload=b"tampered payload")
    patch_downloader(monkeypatch, tampered)             # serves wrong content
    before = state_raw(cdir)

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert not result.success
    assert "MD5 mismatch" in result.error
    assert state_raw(cdir) == before
    assert not (cdir / "TestMod-1.0.zip").exists()


# ---------------------------------------------------------------------------
# C3: re-click recovery for a pending entry with a retained archive
# ---------------------------------------------------------------------------

def test_pending_retained_archive_completes_extraction_without_redownload(
    svc, tmp_path, monkeypatch
):
    """A failed extraction leaves the entry pending with the verified
    archive retained; a re-click must finish the extraction from that
    archive with no download traffic instead of re-downloading into the
    strict no-clobber refusal. (fake.calls stays empty because the
    collection has track-sync disabled; with it enabled the post-save
    tracked-mod sync may make API calls, but never a file download.)"""
    zip_bytes = make_zip()
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    (cdir / "TestMod-1.0.zip").write_bytes(zip_bytes)   # retained archive
    fake = FakeNexusAPI(files=files_meta(zip_bytes))
    svc._api = fake
    session = patch_downloader(monkeypatch, zip_bytes)

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert result.success, result.error
    assert result.status == "extracted_from_archive"
    assert result.extracted is True
    assert fake.calls == []                             # zero API calls
    assert session.calls == []                          # zero CDN calls
    assert (cdir / "Data" / "test.txt").read_bytes() == b"hello mods"
    # The archive was not downloaded by this run, so it is kept; the entry
    # is now downloaded and a further re-click dedupes with zero network.
    assert (cdir / "TestMod-1.0.zip").read_bytes() == zip_bytes
    state = CollectionState(cdir)
    state.load()
    assert state.mods[FILE_ID].download_status == "downloaded"

    second = svc.add_nxm(uri(), mods_dir=cdir)
    assert second.success
    assert second.status == "already_downloaded"
    assert fake.calls == []


def test_pending_retained_corrupt_archive_refused_with_guidance(
    svc, tmp_path, monkeypatch
):
    """A retained archive that fails sanity is never extracted and never
    overwritten: the download path's strict no-clobber publish refuses
    with actionable guidance and the file stays byte-identical."""
    zip_bytes = make_zip()
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    (cdir / "TestMod-1.0.zip").write_bytes(b"garbage-not-a-zip")
    fake = FakeNexusAPI(files=files_meta(zip_bytes))
    svc._api = fake
    patch_downloader(monkeypatch, zip_bytes)
    before = state_raw(cdir)

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert not result.success
    assert "refusing to overwrite" in result.error
    assert "Remove the file" in result.error
    assert f"nexus-dl import {cdir}" in result.error  # import recovery named
    assert (cdir / "TestMod-1.0.zip").read_bytes() == b"garbage-not-a-zip"
    assert state_raw(cdir) == before                    # still pending
    assert not [p for p in cdir.iterdir() if p.name.startswith(".nexus-download-")]


def test_pending_retained_archive_extraction_failure_stays_pending(
    svc, tmp_path, monkeypatch
):
    """When the retained archive itself fails to extract, the entry stays
    pending, the archive is kept, and the error points at import."""
    zip_bytes = make_zip()
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    (cdir / "TestMod-1.0.zip").write_bytes(zip_bytes)
    fake = FakeNexusAPI(files=files_meta(zip_bytes))
    svc._api = fake
    patch_downloader(monkeypatch, zip_bytes)

    def boom(path, target):
        raise service_module.ExtractionError("codec exploded")

    monkeypatch.setattr(service_module, "extract_archive", boom)
    before = state_raw(cdir)

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert not result.success
    assert "could not be extracted" in result.error
    assert "codec exploded" in result.error
    assert f"nexus-dl import {cdir}" in result.error
    assert fake.calls == []                             # still zero network
    assert state_raw(cdir) == before
    assert (cdir / "TestMod-1.0.zip").read_bytes() == zip_bytes


# ---------------------------------------------------------------------------
# D4: recovery-path polish: offline integrity preflight, failure branches
# ---------------------------------------------------------------------------

def test_recovery_verifies_retained_archive_against_cached_md5(
    svc, tmp_path, monkeypatch
):
    """When the cached manifest knows a well-formed hash for this
    identity, the retained archive must match it before the no-download
    recovery runs (the state stores no expected byte size, so the hash
    is the offline integrity datum)."""
    zip_bytes = make_zip()
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    (cdir / "TestMod-1.0.zip").write_bytes(zip_bytes)
    _state_with_manifest_md5(cdir, hashlib.md5(zip_bytes).hexdigest())
    fake = FakeNexusAPI(files=files_meta(zip_bytes))
    svc._api = fake
    session = patch_downloader(monkeypatch, zip_bytes)

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert result.success, result.error
    assert result.status == "extracted_from_archive"
    assert fake.calls == [] and session.calls == []
    assert (cdir / "Data" / "test.txt").exists()


def test_recovery_rejects_retained_archive_failing_cached_md5(
    svc, tmp_path, monkeypatch
):
    """A retained archive whose bytes contradict the cached manifest hash
    is not trusted: nothing is extracted from it. The fresh download then
    fails verification itself (served bytes match neither the hash nor
    the size), so the retained file and the state stay untouched."""
    zip_bytes = make_zip()
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    (cdir / "TestMod-1.0.zip").write_bytes(zip_bytes)
    other_md5 = hashlib.md5(b"something else entirely").hexdigest()
    _state_with_manifest_md5(cdir, other_md5)
    fake = FakeNexusAPI(files=files_meta(zip_bytes))
    svc._api = fake
    patch_downloader(monkeypatch, zip_bytes)
    before = state_raw(cdir)

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert not result.success
    assert "MD5 mismatch" in result.error
    assert not (cdir / "Data" / "test.txt").exists()   # never extracted
    assert (cdir / "TestMod-1.0.zip").read_bytes() == zip_bytes
    assert state_raw(cdir) == before                   # still pending
    assert not [p for p in cdir.iterdir() if p.name.startswith(".nexus-download-")]


def test_recovery_hash_mismatch_verified_download_still_refused(
    svc, tmp_path, monkeypatch
):
    """Even when the fresh download fully verifies against the cached
    hash and size, the untrusted retained archive is never replaced: the
    strict no-clobber publish refuses with import guidance."""
    retained = make_zip()
    replacement = b"something else entirely"
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    (cdir / "TestMod-1.0.zip").write_bytes(retained)
    _state_with_manifest_md5(cdir, hashlib.md5(replacement).hexdigest())
    meta = files_meta(retained)
    meta[0]["size_in_bytes"] = len(replacement)        # matches the payload
    fake = FakeNexusAPI(files=meta)
    svc._api = fake
    patch_downloader(monkeypatch, replacement)
    before = state_raw(cdir)

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert not result.success
    assert "refusing to overwrite" in result.error
    assert f"nexus-dl import {cdir}" in result.error
    assert (cdir / "TestMod-1.0.zip").read_bytes() == retained
    assert state_raw(cdir) == before
    assert not [p for p in cdir.iterdir() if p.name.startswith(".nexus-download-")]


def test_recovery_save_failure_reports_and_keeps_everything(
    svc, tmp_path, monkeypatch
):
    """Recovery failure branch: extraction succeeded but the completion
    could not be persisted. Never claim completion; extracted files and
    the archive stay, on-disk state stays pending."""
    zip_bytes = make_zip()
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    (cdir / "TestMod-1.0.zip").write_bytes(zip_bytes)
    fake = FakeNexusAPI(files=files_meta(zip_bytes))
    svc._api = fake
    patch_downloader(monkeypatch, zip_bytes)
    before = state_raw(cdir)

    def fail_save(self):
        raise StateError("disk on fire")

    monkeypatch.setattr(CollectionState, "save", fail_save)

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert not result.success
    assert "could not be saved" in result.error
    assert "disk on fire" in result.error
    assert f"nexus-dl import {cdir}" in result.error
    assert fake.calls == []                            # no download traffic
    assert (cdir / "Data" / "test.txt").exists()       # honest about extraction
    assert (cdir / "TestMod-1.0.zip").read_bytes() == zip_bytes   # archive kept
    assert state_raw(cdir) == before                   # on-disk still pending


def test_recovery_warnings_are_truthful_on_success(svc, tmp_path, monkeypatch):
    """Recovery warning branches: tracked-sync and load-order failures
    after the persisted completion are truthful warnings on a successful
    result, never crashes."""
    zip_bytes = make_zip()
    cdir = make_collection(tmp_path, mods=[pending_entry()])
    (cdir / "TestMod-1.0.zip").write_bytes(zip_bytes)
    fake = FakeNexusAPI(files=files_meta(zip_bytes))
    svc._api = fake
    patch_downloader(monkeypatch, zip_bytes)

    def track_boom(self, state):
        raise RuntimeError("track exploded")

    def lo_boom(self, target, state):
        raise RuntimeError("loot exploded")

    monkeypatch.setattr(
        service_module.ModManagerService, "_maybe_sync_tracked", track_boom
    )
    monkeypatch.setattr(
        service_module.ModManagerService, "_regen_load_order_from_state", lo_boom
    )

    result = svc.add_nxm(uri(), mods_dir=cdir)

    assert result.success, result.error
    assert result.status == "extracted_from_archive"
    assert len(result.warnings) == 2
    assert any("Tracked-mod sync failed" in w for w in result.warnings)
    assert any("Load order regeneration failed" in w for w in result.warnings)
    state = CollectionState(cdir)
    state.load()
    assert state.mods[FILE_ID].download_status == "downloaded"
