"""#176(b): a handler mapping follows a moved collection.

The mapping stores an absolute path once. Rename or move the collection
folder and the path goes stale. The fix stores the collection name
alongside the path, so when the path goes stale the parent directory is
searched for a collection with that name.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus_collection_dl.handler import (
    HandlerConfig,
    NxmRoutingError,
    add_mapping,
    resolve_collection_dir,
)
from nexus_collection_dl.state import CollectionState

GAME = "skyrimspecialedition"


def _make_collection(parent: Path, name: str, game: str = GAME) -> Path:
    cdir = parent / name
    cdir.mkdir()
    state = CollectionState(cdir)
    state.set_collection_info(
        f"https://next.nexusmods.com/{game}/collections/testcol",
        name, 1, game,
    )
    state.save()
    return cdir


def _map(cdir: Path) -> HandlerConfig:
    return add_mapping(GAME, cdir)


def test_a_moved_mapping_follows_the_collection(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    root = tmp_path / "collections"
    root.mkdir()
    cdir = _make_collection(root, "HS Player Homes")
    _map(cdir)

    # user renames the folder
    moved = root / "HS Homes"
    cdir.rename(moved)

    result = resolve_collection_dir(GAME)
    assert result == moved, f"expected {moved}, got {result}"


def test_a_moved_mapping_picks_the_right_collection(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    root = tmp_path / "collections"
    root.mkdir()
    cdir = _make_collection(root, "HS Player Homes")
    _make_collection(root, "Other Mod")
    _map(cdir)

    # user renames the mapped one
    moved = root / "HS Homes"
    cdir.rename(moved)

    result = resolve_collection_dir(GAME)
    assert result == moved, f"expected {moved}, got {result}"


def test_a_stale_mapping_with_no_name_still_reports(tmp_path, monkeypatch):
    """Old-format string entries have no name to search by; report as before."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    root = tmp_path / "collections"
    root.mkdir()
    gone = root / "moved-away"
    gone.mkdir()
    (gone / ".nexus-state.json").write_text(json.dumps({
        "collection_url": "u", "collection_name": "n",
        "collection_revision": 1, "game_domain": GAME, "mods": {},
    }))
    cfg = HandlerConfig.load()
    cfg.mappings[GAME] = [str(gone)]
    cfg.save()

    import shutil
    shutil.rmtree(gone)

    with pytest.raises(NxmRoutingError, match="nothing usable"):
        resolve_collection_dir(GAME)


def test_a_moved_mapping_reports_when_nothing_matches(tmp_path, monkeypatch):
    """A name that cannot be found anywhere is still a stale mapping."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    root = tmp_path / "collections"
    root.mkdir()
    cdir = _make_collection(root, "HS Player Homes")
    _map(cdir)

    # collection is deleted entirely
    import shutil
    shutil.rmtree(cdir)

    with pytest.raises(NxmRoutingError, match="nothing usable"):
        resolve_collection_dir(GAME)
