"""Regression tests for state keying and sync integrity (bugcheck baseline).

State is keyed by file_id (since PR #55), but add/add-local/track-sync code
kept indexing by mod_id or synthetic keys - causing KeyErrors, colliding
local registrations, and wrong tracked-mod sets. Also covers the 0.2.1
first-sync manifest prefetch (MD5 verification from the first download) and
state persistence for empty/all-optional collections. All API access is
faked; no network.
"""

from pathlib import Path

import pytest

from nexus_collection_dl import service as service_module
from nexus_collection_dl.manifest import CollectionManifest
from nexus_collection_dl.service import ModManagerService
from nexus_collection_dl.state import CollectionState

DOMAIN = "skyrimspecialedition"
MOD_URL = f"https://www.nexusmods.com/{DOMAIN}/mods/100"
COLLECTION_URL = f"https://next.nexusmods.com/{DOMAIN}/collections/testcol"

FILES = [
    {
        "file_id": 555,
        "name": "Main File",
        "file_name": "TestMod-1.0.zip",
        "version": "1.0",
        "size_in_bytes": 1234,
        "is_archived": False,
    },
]


class FakeNexusAPI:
    """Minimal in-memory stand-in for NexusAPI."""

    def __init__(self, is_premium=False, files=None, tracked=None,
                 collection=None):
        self._is_premium = is_premium
        self._files = FILES if files is None else files
        self._tracked = tracked or []
        self._collection = collection
        self.track_calls = []
        self.untrack_calls = []
        self.session = None  # never used: manifest fetch is monkeypatched

    def validate_key(self):
        return {"name": "tester", "is_premium": self._is_premium}

    def get_mod_info(self, game_domain, mod_id):
        return {"name": "Test Mod", "mod_id": mod_id}

    def get_mod_files(self, game_domain, mod_id):
        return list(self._files)

    def get_tracked_mods(self):
        return list(self._tracked)

    def track_mod(self, game_domain, mod_id):
        self.track_calls.append((game_domain, mod_id))

    def untrack_mod(self, game_domain, mod_id):
        self.untrack_calls.append((game_domain, mod_id))

    def get_collection_mods(self, game_domain, slug):
        return dict(self._collection)


@pytest.fixture()
def svc() -> ModManagerService:
    # The API key is never used: every test replaces svc._api with a fake
    return ModManagerService(api_key="test-key-unused")


def _state(tmp_path: Path) -> CollectionState:
    state = CollectionState(tmp_path)
    state.set_collection_info(COLLECTION_URL, "Test Collection", 1, DOMAIN)
    state.save()
    return state


# ---------------------------------------------------------------------------
# add: state is keyed by file_id, never by mod_id
# ---------------------------------------------------------------------------

def test_add_mod_free_registers_state_under_file_id(svc, tmp_path):
    _state(tmp_path)
    svc._api = FakeNexusAPI(is_premium=False)
    result = svc.add_mod(MOD_URL, tmp_path)
    assert result.success, result.error
    state = CollectionState(tmp_path)
    state.load()
    assert 555 in state.mods             # keyed by the selected file_id
    assert 100 not in state.mods         # never keyed by mod_id
    assert state.mods[555].mod_id == 100
    assert state.mods[555].phase == 999  # the lookup that used to KeyError
    assert result.pending_download is not None
    assert result.pending_download.file_id == 555


def test_add_mod_premium_registers_state_under_file_id(svc, tmp_path, monkeypatch):
    _state(tmp_path)
    svc._api = FakeNexusAPI(is_premium=True)

    def fake_download_mods(self, game_domain, mods, target_dir,
                           on_complete=None, on_progress=None):
        out = []
        for m in mods:
            p = Path(target_dir) / m["filename"]
            p.write_bytes(b"zip bytes")
            out.append((m, p))
        return out

    monkeypatch.setattr(
        service_module.Downloader, "download_mods", fake_download_mods
    )
    result = svc.add_mod(MOD_URL, tmp_path, no_load_order=True, no_extract=True)
    assert result.success, result.error
    state = CollectionState(tmp_path)
    state.load()
    assert 555 in state.mods and 100 not in state.mods
    assert state.mods[555].mod_id == 100
    assert state.mods[555].phase == 999
    assert state.mods[555].manual


# ---------------------------------------------------------------------------
# add-local: repeated calls get unique, valid keys
# ---------------------------------------------------------------------------

def test_add_local_repeated_calls_get_unique_keys(svc, tmp_path):
    _state(tmp_path)
    # A nexus-registered mod already occupies file key 555 / mod id 100
    svc._api = FakeNexusAPI(is_premium=False)
    assert svc.add_mod(MOD_URL, tmp_path).success

    ids = [svc.add_local(f"Local {i}", tmp_path, no_load_order=True)
           for i in range(3)]
    assert len(set(ids)) == 3
    assert all(i < 0 for i in ids)

    state = CollectionState(tmp_path)
    state.load()
    assert 555 in state.mods  # the earlier registration survived
    for i in ids:
        assert i in state.mods            # state keyed by the synthetic id
        assert state.mods[i].mod_id == i  # mod_id lookup key is valid
        assert state.mods[i].phase == 999  # never a KeyError
        assert state.mods[i].manual


# ---------------------------------------------------------------------------
# tracked sync: compare mod_ids, not file_id keys
# ---------------------------------------------------------------------------

def test_sync_tracked_uses_mod_ids_not_file_keys(svc, tmp_path):
    state = _state(tmp_path)
    # mod 100 contributes two files; mod 200 contributes one
    state.add_mod({"mod_id": 100, "mod_name": "A", "file_id": 555,
                   "filename": "a1.zip", "version": ""})
    state.add_mod({"mod_id": 100, "mod_name": "A", "file_id": 556,
                   "filename": "a2.zip", "version": ""})
    state.add_mod({"mod_id": 200, "mod_name": "B", "file_id": 777,
                   "filename": "b.zip", "version": ""})
    api = FakeNexusAPI(tracked=[
        {"mod_id": 100, "domain_name": DOMAIN},      # already tracked
        {"mod_id": 999, "domain_name": DOMAIN},      # stale remote entry
        {"mod_id": 777, "domain_name": "fallout4"},  # other game: ignored
    ])
    svc._api = api

    to_track, to_untrack = svc._sync_tracked_mods(state)

    assert (to_track, to_untrack) == (1, 1)
    assert api.track_calls == [(DOMAIN, 200)]
    assert api.untrack_calls == [(DOMAIN, 999)]


# ---------------------------------------------------------------------------
# sync: manifest prefetch for first-sync MD5, empty-collection persistence
# ---------------------------------------------------------------------------

def _collection_data(mods, download_link=None):
    return {
        "name": "Test Collection",
        "revision": 3,
        "game_domain": DOMAIN,
        "mods": mods,
        "download_link": download_link,
    }


def test_sync_all_optional_skipped_persists_state(svc, tmp_path):
    mods = [{"mod_id": 1, "file_id": 2, "mod_name": "Opt",
             "filename": "opt.zip", "optional": True}]
    svc._api = FakeNexusAPI(is_premium=False, collection=_collection_data(mods))
    result = svc.sync(COLLECTION_URL, tmp_path, skip_optional=True)
    assert result.mods_downloaded == 0 and result.pending_downloads == []
    assert result.collection_dir is not None
    state = CollectionState(result.collection_dir)
    assert state.exists()
    state.load()
    assert state.collection_name == "Test Collection"
    assert state.collection_revision == 3
    assert state.game_domain == DOMAIN


def test_sync_first_run_prefetches_manifest_for_md5(svc, tmp_path, monkeypatch):
    """First sync fetches the manifest once before downloading so integrity
    checks have MD5s from the very first file, and caches it in state."""
    mods = [{"mod_id": 100, "file_id": 555, "mod_name": "M",
             "filename": "M-1.0.zip", "size_bytes": 10, "optional": False}]
    svc._api = FakeNexusAPI(
        is_premium=True,
        collection=_collection_data(
            mods, download_link="https://example.invalid/m.json"
        ),
    )

    fetches = []
    manifest = CollectionManifest.from_dict({
        "mod_rules": [{
            "source": {"modId": 100, "fileID": 555,
                       "fileMD5": "ab" * 16,
                       "logicalFileName": "M-1.0.zip"},
        }],
    })

    def fake_fetch(url, session=None):
        fetches.append(url)
        return manifest

    monkeypatch.setattr(
        service_module, "download_and_parse_manifest", fake_fetch
    )

    captured = {}

    def fake_download_mods(self, game_domain, mods, target_dir,
                           on_complete=None, on_progress=None):
        captured["mods"] = [dict(m) for m in mods]
        return []

    monkeypatch.setattr(
        service_module.Downloader, "download_mods", fake_download_mods
    )

    result = svc.sync(
        COLLECTION_URL, tmp_path, no_load_order=True, no_extract=True
    )

    assert fetches == ["https://example.invalid/m.json"]  # exactly one fetch
    assert captured["mods"][0].get("expected_md5") == "ab" * 16
    state = CollectionState(result.collection_dir)
    state.load()
    assert state.manifest_data is not None  # cached for future syncs


# ---------------------------------------------------------------------------
# CLI: pending-download instructions point at the collection subdirectory
# ---------------------------------------------------------------------------

def test_cli_pending_instructions_point_at_collection_dir(tmp_path, monkeypatch):
    """Real CliRunner run of `sync`: the generated save/import instructions
    must name the per-collection subdirectory, not the parent mods dir."""
    from click.testing import CliRunner

    from nexus_collection_dl import cli as cli_module
    from nexus_collection_dl.service import PendingDownload, SyncResult

    mods = tmp_path / "mods"
    mods.mkdir()
    collection_dir = mods / "My Collection"
    collection_dir.mkdir()

    class FakeService:
        def __init__(self, api_key=None, force_free=False):
            pass

        def sync(self, collection_url, mods_dir, skip_optional=False,
                 no_load_order=False, no_extract=False, on_progress=None):
            return SyncResult(
                mods_downloaded=0, mods_extracted=0, errors=[],
                load_order_files=[],
                pending_downloads=[PendingDownload(
                    mod_id=1, mod_name="Manual Mod", file_id=2,
                    filename="Manual-1.0.zip", size_bytes=1024,
                    browser_url=(
                        "https://www.nexusmods.com/skyrimspecialedition"
                        "/mods/1?tab=files&file_id=2"
                    ),
                )],
                collection_dir=collection_dir,
            )

    monkeypatch.setattr(cli_module, "ModManagerService", FakeService)
    monkeypatch.setattr(cli_module, "check_for_update", lambda: None)
    monkeypatch.setattr(cli_module.console, "width", 400)  # no line wrapping

    result = CliRunner().invoke(
        cli_module.main,
        ["--api-key", "k", "--skip-update-check", "sync",
         COLLECTION_URL, str(mods)],
    )
    assert result.exit_code == 0, result.output
    assert "Manual-1.0.zip" in result.output
    assert f"save them to {collection_dir}" in result.output
    assert f"nexus-dl import {collection_dir}" in result.output
    # The parent mods dir alone must never be the instructed target
    assert f"nexus-dl import {mods}\n" not in result.output
