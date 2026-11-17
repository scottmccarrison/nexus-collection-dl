"""End-to-end LOOT sorting over real plugin files.

test_loot_sort.py pins the call shape against a stub, which catches calling an
API libloot does not have but proves nothing about whether libloot will
actually sort. This closes that gap with real plugins and a real sort.

Skipped unless the libloot bindings are installed, which they are not in CI:
these wheels are not on PyPI and are only built by .github/workflows/build-libloot.yml.
So this is a local proof, not a CI gate. See tests/fixtures/libloot/README.md.

The fixtures carry documented master/dependent edges, so a sort is verifiable
by feeding them in the wrong order and checking the dependencies come out
respected. The assertions below are the dependency invariants, not a hardcoded
order: how ties are broken is libloot's business.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from nexus_collection_dl import loot_sort

pytest.importorskip("loot", reason="libloot bindings are not installed")

FIXTURES = Path(__file__).parent / "fixtures" / "libloot"

# Children first, master last: a sort that respects dependencies must change this.
SHUFFLED = [
    "Blank - Master Dependent.esp",
    "Blank - Plugin Dependent.esp",
    "Blank.esp",
    "Blank.esm",
]


@pytest.fixture()
def mods_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    mods = tmp_path / "mods"
    mods.mkdir()
    for name in SHUFFLED:
        shutil.copy(FIXTURES / name, mods / name)
    # No network in tests. The dependency edges live in the plugin headers, so
    # a missing masterlist does not change what is being asserted here.
    monkeypatch.setattr(loot_sort, "download_masterlist", lambda game_domain: None)
    return mods


def test_real_plugins_sort_by_dependency(mods_dir: Path) -> None:
    result = loot_sort.sort_plugins_with_loot("skyrimspecialedition", mods_dir, list(SHUFFLED))

    assert result is not None, "LOOT reported itself unavailable"
    assert sorted(result) == sorted(SHUFFLED), "sort must not drop or invent plugins"
    assert result != SHUFFLED, "sort must actually reorder a deliberately wrong input"

    # Blank.esm is the master that Blank - Master Dependent.esp overrides.
    assert result.index("Blank.esm") < result.index("Blank - Master Dependent.esp")
    # Blank.esp is the plugin that Blank - Plugin Dependent.esp overrides.
    assert result.index("Blank.esp") < result.index("Blank - Plugin Dependent.esp")
    # The master sorts ahead of the plugins that depend on nothing it holds.
    assert result.index("Blank.esm") < result.index("Blank.esp")


def test_fixture_plugins_are_valid(mods_dir: Path) -> None:
    """Guard against the fixtures rotting or being replaced with malformed files."""
    loot = pytest.importorskip("loot")

    game_dir = mods_dir.parent / "game"
    (game_dir / "Data").mkdir(parents=True)
    (game_dir / "local").mkdir(parents=True)
    for name in SHUFFLED:
        shutil.copy(FIXTURES / name, game_dir / "Data" / name)

    game = loot.Game(loot.GameType.SkyrimSE, str(game_dir), str(game_dir / "local"))
    for name in SHUFFLED:
        assert game.is_valid_plugin(str(game_dir / "Data" / name)), f"{name} is not a valid plugin"
