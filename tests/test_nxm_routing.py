"""nxm:// routing must be reachable without a command nobody prompts for.

The 0.4.0 headline feature - a click on the website lands in nexus-dl -
is inert on a fresh install. `setup` installs the desktop entry and
prints "Map each game with 'nexus-dl handler map GAME COLLECTION_DIR'."
Neither `setup` nor `sync` ever calls `add_mapping`, so the user has to
read one line and remember it. Most will conclude the feature is broken.

`sync` is the natural place: it is what creates the collection directory
and the state that `add_mapping` validates against. `setup` runs before
any collection exists, so it can only say where the mapping will happen.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import pytest
from click.testing import CliRunner

from nexus_collection_dl import cli as cli_module
from nexus_collection_dl.handler import HandlerConfig
from nexus_collection_dl.service import SyncResult

DOMAIN = "skyrimspecialedition"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Handler config and detection must not touch the machine."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NEXUS_DL_STEAM_ROOT", str(tmp_path / "no-steam"))
    (tmp_path / "home").mkdir()


def _collection(tmp_path: Path, name="My Collection") -> Path:
    """A synced collection directory, as `sync` would have just left it."""
    coll = tmp_path / "root" / name
    coll.mkdir(parents=True)
    (coll / ".nexus-state.json").write_text(json.dumps({
        "collection_url": "u", "collection_name": name, "collection_revision": 1,
        "game_domain": DOMAIN,
        "mods": {"11": {"mod_id": 1, "name": "M", "file_id": 11, "filename": "a.zip",
                        "version": "1", "download_status": "downloaded"}},
    }))
    return coll


class FakeService:
    """A sync that reports success against a prepared collection dir."""

    result: ClassVar[SyncResult | None] = None

    def __init__(self, api_key=None, **kw):
        pass

    def sync(self, *a, **kw):
        assert FakeService.result is not None
        return FakeService.result


def _run_sync(tmp_path: Path, monkeypatch, coll: Path, *, interactive: bool, answers=()):
    FakeService.result = SyncResult(
        mods_downloaded=1, mods_extracted=1, errors=[], load_order_files=[],
        collection_dir=coll,
    )
    monkeypatch.setattr(cli_module, "ModManagerService", FakeService)
    monkeypatch.setattr(cli_module, "_is_interactive", lambda: interactive)
    runner = CliRunner()
    return runner.invoke(
        cli_module.main,
        ["sync", f"https://next.nexusmods.com/{DOMAIN}/collections/x", str(tmp_path / "root")],
        input="".join(a + "\n" for a in answers),
    )


def _mappings() -> dict:
    return HandlerConfig.load().mappings


# ---------------------------------------------------------------------------
# sync offers, and says where, rather than leaving it to a remembered line
# ---------------------------------------------------------------------------


def test_sync_offers_to_map_and_yes_registers_it(tmp_path, monkeypatch):
    coll = _collection(tmp_path)
    r = _run_sync(tmp_path, monkeypatch, coll, interactive=True, answers=("y",))
    assert r.exit_code == 0, r.output
    assert "handler map" in r.output or "map" in r.output.lower(), r.output
    assert [e["path"] for e in _mappings().get(DOMAIN, [])] == [str(coll.resolve())], _mappings()


def test_sync_declining_leaves_no_mapping(tmp_path, monkeypatch):
    coll = _collection(tmp_path)
    r = _run_sync(tmp_path, monkeypatch, coll, interactive=True, answers=("n",))
    assert r.exit_code == 0, r.output
    assert _mappings() == {}, _mappings()


def test_sync_non_interactive_prints_the_command_and_maps_nothing(tmp_path, monkeypatch):
    """A script cannot be asked, but it must not be left guessing either."""
    coll = _collection(tmp_path)
    r = _run_sync(tmp_path, monkeypatch, coll, interactive=False)
    assert r.exit_code == 0, r.output
    flat = " ".join(r.output.split())
    assert "handler map" in flat, r.output
    assert str(coll.resolve()) in flat, r.output
    assert _mappings() == {}, _mappings()


def test_sync_does_not_reoffer_when_already_mapped(tmp_path, monkeypatch):
    coll = _collection(tmp_path)
    _run_sync(tmp_path, monkeypatch, coll, interactive=True, answers=("y",))
    r = _run_sync(tmp_path, monkeypatch, coll, interactive=True, answers=())
    assert r.exit_code == 0, r.output
    assert [e["path"] for e in _mappings().get(DOMAIN, [])] == [str(coll.resolve())]
    # and it does not ask again
    assert "?" not in r.output.split("Successfully synced")[-1][:200], r.output


def test_sync_maps_the_game_the_collection_actually_is(tmp_path, monkeypatch):
    """The key must be the state's own game domain, not a guess."""
    coll = _collection(tmp_path)
    (coll / ".nexus-state.json").write_text(json.dumps({
        "collection_url": "u", "collection_name": "n", "collection_revision": 1,
        "game_domain": "fallout4",
        "mods": {},
    }))
    _run_sync(tmp_path, monkeypatch, coll, interactive=True, answers=("y",))
    assert list(_mappings()) == ["fallout4"], _mappings()
