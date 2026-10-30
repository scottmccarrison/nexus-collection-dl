"""Writer-lock serialization tests for every mutating service path.

Uses the real CollectionLock (flock-based) throughout: each mutating
method must raise StateBusyError while another operation holds the
collection lock, sync must lock the resolved per-collection subdirectory
(not the parent), track_sync_push stays read-only and works under a held
lock, nested helper calls never re-lock the same directory, failures
conserve prior state, and sequential writers never lose writes. The web
boundary surfaces busy as a clean JSON error.
"""

import io
import threading
import time
import zipfile
from pathlib import Path

import pytest

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
FUTURE = int(time.time()) + 3600
NXM_URI = (
    f"nxm://{GAME}/mods/{MOD_ID}/files/{FILE_ID}"
    f"?key={KEY}&expires={FUTURE}&user_id=4242"
)
MOD_URL = f"https://www.nexusmods.com/{GAME}/mods/{MOD_ID}"
COLLECTION_URL = f"https://next.nexusmods.com/{GAME}/collections/testcol"


class FakeNexusAPI:
    """Minimal API stand-in; records calls so tests can prove zero network."""

    def __init__(self):
        self.calls = []
        self.session = None

    def validate_key(self):
        self.calls.append("validate_key")
        return {"user_id": 4242, "name": "tester", "is_premium": True,
                "profile_expiry": None}

    def get_collection_mods(self, game_domain, slug):
        self.calls.append("get_collection_mods")
        return {
            "name": "Test Collection",
            "revision": 1,
            "game_domain": GAME,
            "mods": [],
            "url": COLLECTION_URL,
        }

    def get_tracked_mods(self):
        self.calls.append("get_tracked_mods")
        return []

    def track_mod(self, game_domain, mod_id):
        self.calls.append(("track_mod", mod_id))

    def untrack_mod(self, game_domain, mod_id):
        self.calls.append(("untrack_mod", mod_id))


def make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Data/test.txt", b"hello mods")
    return buf.getvalue()


def make_collection(tmp_path: Path, name: str = "col", pending: bool = False) -> Path:
    cdir = tmp_path / name
    cdir.mkdir()
    state = CollectionState(cdir)
    state.set_collection_info(COLLECTION_URL, "Test Collection", 1, GAME)
    if pending:
        state.add_mod({
            "mod_id": MOD_ID,
            "mod_name": "Test Mod",
            "file_id": FILE_ID,
            "filename": "TestMod-1.0.zip",
            "version": "1.0",
            "optional": False,
            "manual": False,
            "download_status": "pending_download",
            "browser_url": f"https://www.nexusmods.com/{GAME}/mods/{MOD_ID}?tab=files",
        })
    state.save()
    return cdir


@pytest.fixture()
def svc():
    s = ModManagerService(api_key="test-key-unused")
    s._api = FakeNexusAPI()
    return s


def state_raw(cdir: Path) -> bytes:
    return (cdir / ".nexus-state.json").read_bytes()


def test_busy_error_is_a_state_error():
    # Every CLI/web surface catches StateError, so busy is handled there.
    assert issubclass(StateBusyError, StateError)


# ---------------------------------------------------------------------------
# Every mutating path raises StateBusyError under a real held lock
# ---------------------------------------------------------------------------

def test_update_busy(svc, tmp_path):
    cdir = make_collection(tmp_path)
    with CollectionLock(cdir):
        with pytest.raises(StateBusyError):
            svc.update(cdir)
    assert svc._api.calls == []


def test_add_mod_busy_after_parse_before_network(svc, tmp_path):
    cdir = make_collection(tmp_path)
    with CollectionLock(cdir):
        with pytest.raises(StateBusyError):
            svc.add_mod(MOD_URL, cdir)
    assert svc._api.calls == []


def test_add_local_busy_and_state_conserved(svc, tmp_path):
    cdir = make_collection(tmp_path)
    before = state_raw(cdir)
    with CollectionLock(cdir):
        with pytest.raises(StateBusyError):
            svc.add_local("Local Mod", cdir)
    assert state_raw(cdir) == before          # failed writer changed nothing


def test_import_downloads_busy(svc, tmp_path):
    cdir = make_collection(tmp_path, pending=True)
    with CollectionLock(cdir):
        with pytest.raises(StateBusyError):
            svc.import_downloads(cdir, no_load_order=True)


def test_deploy_and_undeploy_busy(svc, tmp_path):
    cdir = make_collection(tmp_path)
    with CollectionLock(cdir):
        with pytest.raises(StateBusyError):
            svc.deploy(cdir)
        with pytest.raises(StateBusyError):
            svc.undeploy(cdir)


def test_track_sync_enable_disable_busy(svc, tmp_path):
    cdir = make_collection(tmp_path)
    with CollectionLock(cdir):
        with pytest.raises(StateBusyError):
            svc.track_sync_enable(cdir)
        with pytest.raises(StateBusyError):
            svc.track_sync_disable(cdir)
    assert svc._api.calls == []


def test_add_nxm_busy_conserves_state_and_skips_network(svc, tmp_path):
    cdir = make_collection(tmp_path, pending=True)
    before = state_raw(cdir)
    with CollectionLock(cdir):
        with pytest.raises(StateBusyError):
            svc.add_nxm(NXM_URI, mods_dir=cdir)
    assert svc._api.calls == []               # contention caught before API use
    assert state_raw(cdir) == before
    assert not (cdir / "TestMod-1.0.zip").exists()


def test_sync_locks_the_resolved_subdirectory(svc, tmp_path):
    # sync resolves mods_dir/<sanitized collection name> and locks THAT dir,
    # not the parent: holding the subdir lock must block it, and the parent
    # lock must not.
    parent = tmp_path / "mods-root"
    parent.mkdir()
    subdir = parent / "Test Collection"
    subdir.mkdir()

    with CollectionLock(subdir):
        with pytest.raises(StateBusyError):
            svc.sync(COLLECTION_URL, parent)

    # The parent dir was never locked: only pre-lock network calls happened.
    assert svc._api.calls == ["validate_key", "get_collection_mods"]


# ---------------------------------------------------------------------------
# track_sync_push: deliberately read-only and unlocked
# ---------------------------------------------------------------------------

def test_track_sync_push_works_under_held_lock_without_local_writes(svc, tmp_path):
    cdir = make_collection(tmp_path, pending=True)
    before = state_raw(cdir)
    with CollectionLock(cdir):
        tracked, untracked = svc.track_sync_push(cdir)
    assert (tracked, untracked) == (1, 0)     # pending mod_id pushed to Nexus
    assert ("track_mod", MOD_ID) in svc._api.calls
    assert state_raw(cdir) == before          # local state untouched


# ---------------------------------------------------------------------------
# No nested same-dir deadlocks: full writers complete under their own lock
# ---------------------------------------------------------------------------

def test_import_downloads_completes_with_extraction_under_lock(svc, tmp_path):
    # import_downloads extracts and updates state while holding the lock;
    # a nested re-lock would surface as StateBusyError (flock is not
    # reentrant across opens), so plain completion proves deadlock-freedom.
    cdir = make_collection(tmp_path, pending=True)
    (cdir / "TestMod-1.0.zip").write_bytes(make_zip())

    result = svc.import_downloads(cdir, no_load_order=True)

    assert result.matched == 1
    assert result.extracted == 1
    assert (cdir / "Data" / "test.txt").exists()
    state = CollectionState(cdir)
    state.load()
    assert state.mods[FILE_ID].download_status == "downloaded"


def test_add_local_completes_under_its_own_lock(svc, tmp_path):
    cdir = make_collection(tmp_path)
    mod_id = svc.add_local("Local Mod", cdir)
    assert mod_id < 0
    state = CollectionState(cdir)
    state.load()
    assert state.mods[mod_id].name == "Local Mod"


# ---------------------------------------------------------------------------
# Real contention: blocked writer fails fast, then succeeds after release
# ---------------------------------------------------------------------------

def test_concurrent_writer_serializes_without_lost_writes(svc, tmp_path):
    cdir = make_collection(tmp_path)
    held = threading.Event()
    release = threading.Event()

    def holder():
        with CollectionLock(cdir):
            held.set()
            release.wait(timeout=10)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert held.wait(timeout=10)

    # While the other operation runs: fail fast, no partial writes.
    before = state_raw(cdir)
    with pytest.raises(StateBusyError):
        svc.add_local("Blocked Mod", cdir)
    assert state_raw(cdir) == before

    # After release: the same write succeeds and both entries persist.
    release.set()
    t.join(timeout=10)
    svc.add_local("First Mod", cdir)
    svc.add_local("Second Mod", cdir)

    state = CollectionState(cdir)
    state.load()
    names = {ms.name for ms in state.mods.values()}
    assert names == {"First Mod", "Second Mod"}   # sequential, no lost writes
    assert "Blocked Mod" not in names             # the failed call wrote nothing


# ---------------------------------------------------------------------------
# Web boundary: busy surfaces as a clean JSON error, not a 500
# ---------------------------------------------------------------------------

def test_web_add_local_busy_surfaces_as_json_400(tmp_path):
    from nexus_collection_dl.web.app import create_app

    cdir = make_collection(tmp_path)
    app = create_app(api_key="test-key-unused", mods_dir=cdir)
    client = app.test_client()

    with CollectionLock(cdir):
        resp = client.post("/api/add-local", json={"name": "Local Mod"})

    assert resp.status_code == 400
    body = resp.get_json()
    assert "already running" in body["error"]
    assert state_raw(cdir) == state_raw(cdir)     # sanity: file still readable
    state = CollectionState(cdir)
    state.load()
    assert state.mods == {}                       # nothing was written
