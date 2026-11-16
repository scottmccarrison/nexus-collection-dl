"""Contract tests for the libloot API surface that loot_sort.py drives.

The previous implementation called loot.create_game_handle(),
Database.load_lists() and Database.sort_plugins(). None of those exist in
current libloot, and the GameType member names were derived from display names
("Skyrim Special Edition" -> "SkyrimSpecialEdition" rather than "SkyrimSE").
Both faults were invisible at runtime because the call sits behind a catch-all
that falls back to collection order, so these tests pin the call shape rather
than libloot's sorting behaviour.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from nexus_collection_dl import loot_sort


class _FakeDatabase:
    def __init__(self, calls: list) -> None:
        self._calls = calls

    def load_masterlist(self, path: str) -> None:
        self._calls.append(("load_masterlist", path))


class _FakeGame:
    def __init__(self, calls: list, sorted_result: list[str]) -> None:
        self._calls = calls
        self._sorted_result = sorted_result

    def database(self) -> _FakeDatabase:
        self._calls.append(("database",))
        return _FakeDatabase(self._calls)

    def load_plugins(self, paths: list[str]) -> None:
        self._calls.append(("load_plugins", list(paths)))

    def sort_plugins(self, names: list[str]) -> list[str]:
        self._calls.append(("sort_plugins", list(names)))
        return self._sorted_result


class _FakeGameFactory:
    def __init__(self, calls: list, sorted_result: list[str]) -> None:
        self._calls = calls
        self._sorted_result = sorted_result

    def __call__(self, game_type: object, game_path: str, local_path: str) -> _FakeGame:
        # The temp dirs the caller built only exist for the duration of the
        # call, so record their state here rather than after the fact.
        self._calls.append(
            (
                "Game",
                game_type,
                game_path,
                local_path,
                Path(game_path).is_dir(),
                Path(local_path).is_dir(),
            )
        )
        return _FakeGame(self._calls, self._sorted_result)


def _install_fake_loot(
    monkeypatch: pytest.MonkeyPatch,
    calls: list,
    sorted_result: list[str],
    members: tuple[str, ...] = (),
) -> types.ModuleType:
    fake = types.ModuleType("loot")
    fake.GameType = types.SimpleNamespace(**{name: f"gt:{name}" for name in members})
    fake.Game = _FakeGameFactory(calls, sorted_result)
    monkeypatch.setitem(sys.modules, "loot", fake)
    return fake


def _make_mods_dir(tmp_path: Path, names: list[str]) -> Path:
    mods = tmp_path / "mods"
    mods.mkdir()
    for name in names:
        (mods / name).write_bytes(b"")
    return mods


def _pin_masterlist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Route download_masterlist to a local file so no test hits the network."""
    masterlist = tmp_path / "masterlist.yaml"
    masterlist.write_text("masterlist\n")
    monkeypatch.setattr(loot_sort, "download_masterlist", lambda game_domain: masterlist)
    return masterlist


def test_sort_plugins_drives_current_libloot_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list = []
    _install_fake_loot(
        monkeypatch,
        calls,
        sorted_result=["aaa_first.esp", "zzz_last.esp"],
        members=("SkyrimSE",),
    )
    masterlist = _pin_masterlist(monkeypatch, tmp_path)
    mods_dir = _make_mods_dir(tmp_path, ["zzz_last.esp", "aaa_first.esp"])

    result = loot_sort.sort_plugins_with_loot(
        "skyrimspecialedition", mods_dir, ["zzz_last.esp", "aaa_first.esp"]
    )

    assert result == ["aaa_first.esp", "zzz_last.esp"]

    # The GameType must be the exact enum member, looked up by name.
    game_calls = [c for c in calls if c[0] == "Game"]
    assert len(game_calls) == 1
    _, game_type, game_path, local_path, game_dir_built, local_dir_built = game_calls[0]
    assert game_type == "gt:SkyrimSE"
    assert game_path != local_path
    assert game_dir_built
    assert local_dir_built

    # load_masterlist happens before load_plugins, and sort_plugins cannot be
    # called before the plugins are loaded.
    kinds = [c[0] for c in calls]
    assert kinds.index("load_masterlist") < kinds.index("load_plugins")
    assert kinds.index("load_plugins") < kinds.index("sort_plugins")

    load_masterlist = next(c for c in calls if c[0] == "load_masterlist")
    assert load_masterlist[1] == str(masterlist)

    loaded = next(c for c in calls if c[0] == "load_plugins")[1]
    assert sorted(Path(p).name for p in loaded) == ["aaa_first.esp", "zzz_last.esp"]

    sorted_names = next(c for c in calls if c[0] == "sort_plugins")[1]
    assert sorted_names == ["aaa_first.esp", "zzz_last.esp"]


def test_game_type_is_resolved_by_exact_enum_member_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Skyrim SE must map to SkyrimSE, which is not derivable from its display name."""
    calls: list = []
    _install_fake_loot(monkeypatch, calls, sorted_result=[], members=("SkyrimSE",))
    _pin_masterlist(monkeypatch, tmp_path)
    mods_dir = _make_mods_dir(tmp_path, ["a.esp"])

    loot_sort.sort_plugins_with_loot("skyrimspecialedition", mods_dir, ["a.esp"])

    game_call = next(c for c in calls if c[0] == "Game")
    assert game_call[1] == "gt:SkyrimSE"


def test_every_mapped_game_type_exists_on_libloot() -> None:
    """Drift guard: the map names real GameType members, checked against real bindings."""
    loot = pytest.importorskip("loot")
    for domain, info in loot_sort.LOOT_GAME_MAP.items():
        name = info["game_type"]
        if not name:
            continue
        assert hasattr(loot.GameType, name), f"{domain}: GameType.{name} is not a member"


def test_unsorted_game_falls_back_to_collection_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Enderal has a masterlist repo but no GameType, so it returns the input unchanged."""
    calls: list = []
    _install_fake_loot(monkeypatch, calls, sorted_result=["a.esp"], members=())
    _pin_masterlist(monkeypatch, tmp_path)
    mods_dir = _make_mods_dir(tmp_path, ["a.esp"])
    existing = ["a.esp", "b.esp"]

    result = loot_sort.sort_plugins_with_loot("enderal", mods_dir, existing)

    assert result is existing
    assert not any(c[0] == "Game" for c in calls)


def test_unknown_game_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list = []
    _install_fake_loot(monkeypatch, calls, sorted_result=["a.esp"], members=("SkyrimSE",))
    _pin_masterlist(monkeypatch, tmp_path)
    mods_dir = _make_mods_dir(tmp_path, ["a.esp"])

    assert loot_sort.sort_plugins_with_loot("stardewvalley", mods_dir, ["a.esp"]) is None


def test_missing_libloot_api_degrades_without_raising(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the bindings drift again, sort reports "unavailable" rather than exploding mid-deploy.

    None is the signal service.py uses for "no LOOT order"; it then falls back
    to collection order via merge_plugin_orders.
    """
    fake = types.ModuleType("loot")
    fake.GameType = types.SimpleNamespace(SkyrimSE="gt:SkyrimSE")
    monkeypatch.setitem(sys.modules, "loot", fake)
    _pin_masterlist(monkeypatch, tmp_path)
    mods_dir = _make_mods_dir(tmp_path, ["a.esp"])

    assert loot_sort.sort_plugins_with_loot("skyrimspecialedition", mods_dir, ["a.esp"]) is None
