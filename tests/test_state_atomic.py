"""Tests for atomic state saving and per-collection locking.

Everything runs against temporary directories; no network access involved.
"""

import json
import os
from pathlib import Path

import pytest

from nexus_collection_dl import state as state_module
from nexus_collection_dl.state import (
    CollectionLock,
    CollectionState,
    StateBusyError,
)


def _make_state(mods_dir: Path) -> CollectionState:
    mods_dir.mkdir(parents=True, exist_ok=True)
    state = CollectionState(mods_dir)
    state.set_collection_info(
        "https://next.nexusmods.com/skyrimspecialedition/collections/test",
        "Test Collection",
        1,
        "skyrimspecialedition",
    )
    return state


def test_save_load_roundtrip(tmp_path: Path):
    mods = tmp_path / "mods"
    state = _make_state(mods)
    state.add_mod({
        "file_id": 1, "mod_id": 10, "mod_name": "A", "version": "1.0",
        "filename": "a.zip",
    })
    state.deployed_files = [{"src": "/s", "dest": "/d", "method": "copy"}]
    state.save()

    loaded = CollectionState(mods)
    loaded.load()
    assert loaded.collection_name == "Test Collection"
    assert loaded.mods[1].mod_id == 10
    assert loaded.deployed_files == [{"src": "/s", "dest": "/d", "method": "copy"}]


def test_save_failure_preserves_original_state_file(tmp_path: Path, monkeypatch):
    """A serialization failure mid-write must leave the old state intact."""
    mods = tmp_path / "mods"
    state = _make_state(mods)
    state.save()
    original_bytes = (mods / ".nexus-state.json").read_bytes()

    def boom(*args, **kwargs):
        raise TypeError("injected serialization failure")

    monkeypatch.setattr(state_module.json, "dump", boom)
    state.collection_name = "Changed"
    with pytest.raises(TypeError):
        state.save()

    assert (mods / ".nexus-state.json").read_bytes() == original_bytes
    # No temp files left behind
    leftovers = [p for p in mods.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_save_failure_before_replace_never_touches_state_file(tmp_path: Path, monkeypatch):
    """Failure at the rename step keeps the original and drops the temp."""
    mods = tmp_path / "mods"
    state = _make_state(mods)
    state.save()
    original_bytes = (mods / ".nexus-state.json").read_bytes()

    captured: list[str] = []
    real_replace = os.replace

    def failing_replace(src, dst):
        captured.append(str(src))
        raise OSError(28, "injected disk full")

    monkeypatch.setattr(state_module.os, "replace", failing_replace)
    with pytest.raises(OSError):
        state.save()

    assert (mods / ".nexus-state.json").read_bytes() == original_bytes
    assert len(captured) == 1
    assert not Path(captured[0]).exists()  # temp cleaned up
    monkeypatch.setattr(state_module.os, "replace", real_replace)


def test_save_uses_unique_temp_names(tmp_path: Path, monkeypatch):
    """Two saves must stage through different temp files (no clobbering)."""
    mods = tmp_path / "mods"
    state = _make_state(mods)

    names: list[str] = []
    real_replace = state_module.os.replace

    def recording_replace(src, dst):
        names.append(Path(str(src)).name)
        return real_replace(src, dst)

    monkeypatch.setattr(state_module.os, "replace", recording_replace)
    state.save()
    state.collection_name = "Second"
    state.save()

    assert len(names) == 2
    assert names[0] != names[1]
    assert all(n.startswith(".nexus-state.json.") for n in names)


def test_lock_blocks_second_holder(tmp_path: Path):
    mods = tmp_path / "mods"
    mods.mkdir()
    with CollectionLock(mods):
        with pytest.raises(StateBusyError):
            with CollectionLock(mods):
                pass  # pragma: no cover
    # Released: reacquiring works
    with CollectionLock(mods):
        pass


def test_lock_is_scoped_per_directory(tmp_path: Path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    with CollectionLock(a):
        with CollectionLock(b):
            pass  # different collection dirs do not block each other
