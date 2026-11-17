"""Service-layer tests for collection enumeration and safe mod removal.

list_collections/list_mods/get_collection_url are read-only local scans
(no network), and remove_mod drops state entries only: payload files on
disk are never touched, saves are atomic under the collection lock, and
a load order regeneration failure after the save is reported as a
warning instead of masking the completed removal. Regeneration failures
are exercised via real output write failures (load-order.txt pre-created
as a directory), never by monkeypatching the regeneration helper away.
Everything runs in tmp_path; all state is built locally and no network
is ever reached.
"""

import io
import json
import zipfile
from pathlib import Path

import pytest

from nexus_collection_dl import service as service_module
from nexus_collection_dl import state as state_module
from nexus_collection_dl.manifest import CollectionManifest
from nexus_collection_dl.service import (
    ModManagerService,
    get_collection_url,
    list_collections,
    list_mods,
)
from nexus_collection_dl.state import (
    STATE_FILENAME,
    CollectionLock,
    CollectionState,
    LoadOrderRegenError,
    StateBusyError,
    StateError,
)

GAME = "skyrimspecialedition"
COLLECTION_URL = f"https://next.nexusmods.com/{GAME}/collections/testcol"


def mod_entry(
    file_id=456,
    mod_id=123,
    name="Test Mod",
    filename=None,
    download_status="downloaded",
    manual=False,
):
    return {
        "mod_id": mod_id,
        "mod_name": name,
        "file_id": file_id,
        "filename": filename or f"{name.replace(' ', '')}-{file_id}.zip",
        "version": "1.0",
        "optional": False,
        "manual": manual,
        "download_status": download_status,
    }


def write_state(cdir: Path, collection_name="Test Collection", mods=(),
                url=COLLECTION_URL):
    """Create a collection state file in cdir (which must not exist yet).

    ``url`` is the collection_url recorded in the state; pass "" to
    simulate a state with no stored URL.
    """
    cdir.mkdir(parents=True, exist_ok=True)
    state = CollectionState(cdir)
    state.set_collection_info(url, collection_name, 1, GAME)
    for m in mods:
        state.add_mod(m)
    state.save()
    return cdir


def cached_manifest() -> dict:
    """A minimal cached manifest so load order regeneration writes output."""
    return CollectionManifest(
        mod_rules=[],
        plugins=[],
        plugin_rules=[],
        mod_phases={100: 0, 200: 0},
    ).to_dict()


def make_zip_bytes(name="Data/test.txt", payload=b"hello mods") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, payload)
    return buf.getvalue()


@pytest.fixture()
def svc() -> ModManagerService:
    # remove_mod is purely local; the API key is never used.
    return ModManagerService(api_key="test-key-unused")


# ---------------------------------------------------------------------------
# (a) list_collections: depth-0/1 discovery, invalid states skipped,
#     a collection dir accepted directly as root
# ---------------------------------------------------------------------------

def test_list_collections_scans_root_and_depth1_only(tmp_path):
    root = tmp_path / "collections"
    write_state(root, collection_name="Root Collection",
                mods=[mod_entry(1, 10, "A")])
    write_state(root / "Alpha", collection_name="Alpha Collection",
                mods=[mod_entry(2, 20, "B"), mod_entry(3, 21, "C",
                                                    download_status="pending_download")])
    write_state(root / "Beta", collection_name="Beta Collection")
    # Deeper than depth 1: must NOT be discovered.
    write_state(root / "Alpha" / "Deep", collection_name="Deep Collection")

    found = list_collections(root)

    assert [c.name for c in found] == [
        "Alpha Collection", "Beta Collection", "Root Collection",
    ]
    by_name = {c.name: c for c in found}
    alpha = by_name["Alpha Collection"]
    assert alpha.game_domain == GAME
    assert alpha.mod_count == 2
    assert alpha.pending_count == 1
    assert alpha.path == root / "Alpha"
    assert by_name["Root Collection"].mod_count == 1
    assert by_name["Beta Collection"].mod_count == 0
    assert by_name["Beta Collection"].pending_count == 0


def test_list_collections_skips_invalid_states_silently(tmp_path):
    root = tmp_path / "collections"
    root.mkdir()
    write_state(root / "Good", collection_name="Good Collection")

    broken = root / "Broken"
    broken.mkdir()
    (broken / STATE_FILENAME).write_text("{not valid json")

    bad_keys = root / "BadKeys"
    bad_keys.mkdir()
    (bad_keys / STATE_FILENAME).write_text(json.dumps(
        {"collection_name": "Bad", "mods": {"not-a-number": {}}}
    ))

    not_an_object = root / "NotAnObject"
    not_an_object.mkdir()
    (not_an_object / STATE_FILENAME).write_text("[1, 2, 3]")

    no_state = root / "NoState"
    no_state.mkdir()

    found = list_collections(root)
    assert [c.name for c in found] == ["Good Collection"]


def test_list_collections_accepts_a_collection_dir_as_root(tmp_path):
    cdir = write_state(tmp_path / "solo", collection_name="Solo",
                       mods=[mod_entry(1, 10, "A")])

    found = list_collections(cdir)

    assert len(found) == 1
    assert found[0].name == "Solo"
    assert found[0].path == cdir
    assert found[0].mod_count == 1


def test_list_collections_empty_name_falls_back_to_dir_name(tmp_path):
    root = tmp_path / "collections"
    write_state(root / "FallsBack", collection_name="")

    found = list_collections(root)
    assert [c.name for c in found] == ["FallsBack"]


# ---------------------------------------------------------------------------
# (a2) get_collection_url: the URL recorded in state, "" when none,
#      StateError on an unreadable state
# ---------------------------------------------------------------------------

def test_get_collection_url_returns_the_recorded_url(tmp_path):
    cdir = write_state(tmp_path / "Coll", collection_name="Coll")

    assert get_collection_url(cdir) == COLLECTION_URL


def test_get_collection_url_empty_when_state_records_none(tmp_path):
    cdir = write_state(tmp_path / "Coll", collection_name="Coll", url="")

    assert get_collection_url(cdir) == ""


def test_get_collection_url_raises_state_error_on_unreadable_state(tmp_path):
    cdir = tmp_path / "Broken"
    cdir.mkdir()
    (cdir / STATE_FILENAME).write_text("{not valid json")

    with pytest.raises(StateError):
        get_collection_url(cdir)


# ---------------------------------------------------------------------------
# (b) list_mods: local fields accurate, sorted by name
# ---------------------------------------------------------------------------

def test_list_mods_reports_local_fields_sorted_by_name(tmp_path):
    cdir = write_state(tmp_path / "col", mods=[
        mod_entry(555, 100, "Zeta Mod", filename="z.zip", download_status="downloaded"),
        mod_entry(556, 100, "Zeta Mod", filename="z2.zip",
                  download_status="pending_download"),
        mod_entry(777, 200, "Alpha Mod", filename="a.zip",
                  download_status="downloaded"),
    ])

    mods = list_mods(cdir)

    assert [m.name for m in mods] == ["Alpha Mod", "Zeta Mod", "Zeta Mod"]
    first = mods[0]
    assert (first.file_id, first.mod_id) == (777, 200)
    assert first.version == "1.0"
    assert first.filename == "a.zip"
    assert first.download_status == "downloaded"
    # Ties on name break by file_id for determinism.
    assert [m.file_id for m in mods[1:]] == [555, 556]
    assert mods[2].download_status == "pending_download"


def test_list_mods_raises_state_error_without_state(tmp_path):
    with pytest.raises(StateError):
        list_mods(tmp_path / "missing")


# ---------------------------------------------------------------------------
# (c)-(f) remove_mod: identity handling
# ---------------------------------------------------------------------------

def test_remove_by_file_id_persists_across_reload(svc, tmp_path):
    cdir = write_state(tmp_path / "col", mods=[
        mod_entry(555, 100, "Alpha Mod"),
        mod_entry(777, 200, "Beta Mod"),
    ])

    result = svc.remove_mod(cdir, file_id=555, no_load_order=True)

    assert result.removed_file_ids == [555]
    assert result.removed_mod_ids == [100]
    assert result.warnings == []

    state = CollectionState(cdir)
    state.load()
    assert 555 not in state.mods
    assert 777 in state.mods


def test_remove_by_mod_id_removes_all_its_files(svc, tmp_path):
    cdir = write_state(tmp_path / "col", mods=[
        mod_entry(555, 100, "Alpha Mod"),
        mod_entry(556, 100, "Alpha Mod"),
        mod_entry(777, 200, "Beta Mod"),
    ])

    result = svc.remove_mod(cdir, mod_id=100, no_load_order=True)

    assert result.removed_file_ids == [555, 556]
    assert result.removed_mod_ids == [100]

    state = CollectionState(cdir)
    state.load()
    assert set(state.mods) == {777}


def test_unknown_ids_raise_state_error_and_change_nothing(svc, tmp_path):
    cdir = write_state(tmp_path / "col", mods=[mod_entry(555, 100, "Alpha Mod")])
    before = (cdir / STATE_FILENAME).read_bytes()

    with pytest.raises(StateError):
        svc.remove_mod(cdir, mod_id=999, no_load_order=True)
    with pytest.raises(StateError):
        svc.remove_mod(cdir, file_id=999, no_load_order=True)

    assert (cdir / STATE_FILENAME).read_bytes() == before


def test_both_or_neither_identity_raises_value_error(svc, tmp_path):
    cdir = write_state(tmp_path / "col", mods=[mod_entry(555, 100, "Alpha Mod")])
    before = (cdir / STATE_FILENAME).read_bytes()

    with pytest.raises(ValueError):
        svc.remove_mod(cdir, file_id=555, mod_id=100, no_load_order=True)
    with pytest.raises(ValueError):
        svc.remove_mod(cdir, no_load_order=True)

    assert (cdir / STATE_FILENAME).read_bytes() == before


# ---------------------------------------------------------------------------
# (g)-(i) remove_mod: payload safety and load order follow-ups
# ---------------------------------------------------------------------------

def snapshot_payloads(cdir: Path):
    """Bytes + mtime of every file except internal state/lock files."""
    out = {}
    for p in sorted(cdir.rglob("*")):
        if p.is_file() and p.name not in (STATE_FILENAME, ".nexus-state.lock"):
            st = p.stat()
            out[str(p.relative_to(cdir))] = (p.read_bytes(), st.st_mtime_ns)
    return out


def test_removal_never_touches_payload_files(svc, tmp_path):
    cdir = write_state(tmp_path / "col", mods=[
        mod_entry(555, 100, "Alpha Mod", filename="alpha.zip"),
    ])
    (cdir / "Data").mkdir()
    (cdir / "Data" / "test.txt").write_bytes(b"extracted payload")
    (cdir / "alpha.zip").write_bytes(b"archive payload bytes")
    before = snapshot_payloads(cdir)

    result = svc.remove_mod(cdir, mod_id=100)

    assert result.removed_file_ids == [555]
    assert snapshot_payloads(cdir) == before


def test_removal_regenerates_load_order_from_cached_manifest(svc, tmp_path):
    cdir = write_state(tmp_path / "col", mods=[
        mod_entry(555, 100, "Alpha Mod"),
        mod_entry(777, 200, "Beta Mod"),
    ])
    state = CollectionState(cdir)
    state.load()
    state.manifest_data = cached_manifest()
    state.save()
    (cdir / "payload.bin").write_bytes(b"keep me")
    before = snapshot_payloads(cdir)

    result = svc.remove_mod(cdir, mod_id=100)

    assert result.warnings == []
    load_order = (cdir / "load-order.txt").read_text()
    assert "[200:777]" in load_order
    assert "Beta Mod" in load_order
    assert "[100:555]" not in load_order
    assert "Alpha Mod" not in load_order
    # The removal is persisted and payload files are untouched (load-order.txt
    # is generated metadata, not payload, so it is new here).
    state = CollectionState(cdir)
    state.load()
    assert set(state.mods) == {777}
    after = snapshot_payloads(cdir)
    assert all(after.get(k) == v for k, v in before.items())
    assert (cdir / "payload.bin").read_bytes() == b"keep me"


def test_regen_write_failure_after_save_warns_but_keeps_removal(svc, tmp_path):
    cdir = write_state(tmp_path / "col", mods=[
        mod_entry(555, 100, "Alpha Mod"),
        mod_entry(777, 200, "Beta Mod"),
    ])
    state = CollectionState(cdir)
    state.load()
    state.manifest_data = cached_manifest()
    state.save()
    # Real output write failure: load-order.txt exists as a directory, so
    # the generator's write fails on the actual regen path.
    (cdir / "load-order.txt").mkdir()

    result = svc.remove_mod(cdir, mod_id=100)

    # The removal itself succeeded and is persisted; the warning says so
    # honestly and points at the retry command.
    assert result.removed_file_ids == [555]
    assert len(result.warnings) == 1
    assert "was saved" in result.warnings[0]
    assert "load order regeneration failed" in result.warnings[0]
    assert f"nexus-dl load-order {cdir}" in result.warnings[0]

    state = CollectionState(cdir)
    state.load()
    assert set(state.mods) == {777}


def test_regen_helper_raises_on_output_write_failure(svc, tmp_path):
    # The helper surfaces real generation/write failures as
    # LoadOrderRegenError (a StateError) instead of silently returning [].
    cdir = write_state(tmp_path / "col", mods=[
        mod_entry(555, 100, "Alpha Mod"),
    ])
    state = CollectionState(cdir)
    state.load()
    state.manifest_data = cached_manifest()
    state.save()
    (cdir / "load-order.txt").mkdir()

    with pytest.raises(LoadOrderRegenError):
        svc._regen_load_order_from_state(cdir, state)


def test_import_reports_regen_write_failure_honestly(svc, tmp_path):
    # import commits the imported mod first, then reports a real
    # regeneration failure on its result instead of swallowing it.
    cdir = write_state(tmp_path / "col", mods=[
        mod_entry(555, 100, "Alpha Mod", filename="AlphaMod.zip",
                  download_status="pending_download"),
    ])
    (cdir / "AlphaMod.zip").write_bytes(make_zip_bytes())
    state = CollectionState(cdir)
    state.load()
    state.manifest_data = cached_manifest()
    state.save()
    (cdir / "load-order.txt").mkdir()

    result = svc.import_downloads(cdir)

    assert result.matched == 1
    assert any("Load order regeneration failed" in e for e in result.errors)
    state = CollectionState(cdir)
    state.load()
    assert state.mods[555].download_status == "downloaded"


# ---------------------------------------------------------------------------
# (j)-(k) remove_mod: lock contention and save failure
# ---------------------------------------------------------------------------

def test_busy_lock_raises_state_busy_error(svc, tmp_path):
    cdir = write_state(tmp_path / "col", mods=[mod_entry(555, 100, "Alpha Mod")])
    before = (cdir / STATE_FILENAME).read_bytes()

    with CollectionLock(cdir), pytest.raises(StateBusyError):
        svc.remove_mod(cdir, mod_id=100, no_load_order=True)

    assert (cdir / STATE_FILENAME).read_bytes() == before


def test_save_failure_raises_state_error_prior_state_standing(
    svc, tmp_path, monkeypatch
):
    cdir = write_state(tmp_path / "col", mods=[mod_entry(555, 100, "Alpha Mod")])
    before = (cdir / STATE_FILENAME).read_bytes()

    def boom(*args, **kwargs):
        raise TypeError("injected serialization failure")

    monkeypatch.setattr(state_module.json, "dump", boom)

    with pytest.raises(StateError):
        svc.remove_mod(cdir, mod_id=100, no_load_order=True)

    # Atomic save: the original state file stands and no temp files leak.
    assert (cdir / STATE_FILENAME).read_bytes() == before
    leftovers = [p for p in cdir.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    # json.load is untouched by the json.dump patch: reload proves the
    # entry is still tracked in the standing state file.
    state = CollectionState(cdir)
    state.load()
    assert 555 in state.mods


# ---------------------------------------------------------------------------
# Round B: every regen failure surfaces as LoadOrderRegenError
# ---------------------------------------------------------------------------

def test_regen_wraps_manifest_parse_failures(svc, tmp_path):
    # CollectionManifest.from_dict raising ValueError used to escape
    # raw, after the caller's state change had already committed.
    cdir = write_state(tmp_path / "col", mods=[mod_entry(555, 100, "Alpha Mod")])
    state = CollectionState(cdir)
    state.load()
    state.manifest_data = {"mod_rules": [], "mod_phases": {"not-a-number": 1}}
    state.save()

    with pytest.raises(LoadOrderRegenError):
        svc._regen_load_order_from_state(cdir, state)


def test_regen_wraps_loot_write_failures(svc, tmp_path, monkeypatch):
    # write_loot_plugins_txt raising OSError on the loot path used to
    # escape raw too; the wrapper catches every failure class.
    cdir = write_state(tmp_path / "col", mods=[mod_entry(555, 100, "Alpha Mod")])
    state = CollectionState(cdir)
    state.load()
    state.manifest_data = cached_manifest()
    state.save()
    monkeypatch.setattr(service_module, "is_bethesda_game", lambda g: True)
    monkeypatch.setattr(service_module, "is_loot_available", lambda: True)
    monkeypatch.setattr(
        service_module, "sort_plugins_with_loot", lambda *a, **k: None
    )
    monkeypatch.setattr(
        service_module, "merge_plugin_orders", lambda *a, **k: []
    )

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(service_module, "write_loot_plugins_txt", boom)

    with pytest.raises(LoadOrderRegenError):
        svc._regen_load_order_from_state(cdir, state)
