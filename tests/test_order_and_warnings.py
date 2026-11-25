"""Order and warning fidelity at the deploy boundary.

Three defects found in the 0.5.0 gate, all of the same shape: a value is
computed correctly and then dropped, reordered, or duplicated on its way
to the user.

- the collection manifest's plugin order is read under the wrong key, so
  the generated plugins.txt is empty and the deploy-time fallback writes
  the mod order instead - a master can land after its dependents
- every plan warning reaches the user twice, because the deploy result
  is seeded with plan.warnings and the service merges both lists
- the case-collides warning cannot fire when the case fixup runs first,
  since the fixup collapses the pair it is meant to see
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus_collection_dl.loadorder import LoadOrderGenerator
from nexus_collection_dl.service import ModManagerService

DOMAIN = "skyrimspecialedition"


@pytest.fixture(autouse=True)
def no_machine(monkeypatch, tmp_path):
    """Neither the user config nor the machine's Steam may speak here.

    These tests are about what the state and manifest alone produce. A
    real Proton prefix resolves the game state and the conditional
    warning disappears - which is how this test passed for the wrong
    reason once already.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-config"))
    monkeypatch.setenv("NEXUS_DL_STEAM_ROOT", str(tmp_path / "no-steam"))


def _state(mods: Path, game: Path, mods_by_id: dict, manifest: dict | None = None) -> None:
    mods.mkdir(parents=True, exist_ok=True)
    (mods / ".nexus-state.json").write_text(json.dumps({
        "collection_url": "u", "collection_name": "n", "collection_revision": 1,
        "game_domain": DOMAIN, "game_dir": str(game),
        "manifest_data": manifest or {"mods": []},
        "mods": mods_by_id,
    }))


# ---------------------------------------------------------------------------
# 1. The manifest's plugin order must reach plugins.txt
# ---------------------------------------------------------------------------


def test_write_plugins_honours_the_manifest_order_by_name():
    """Manifest entries carry `name`, not `filename`. Both must work."""
    gen = LoadOrderGenerator.__new__(LoadOrderGenerator)
    gen.game_domain = DOMAIN

    class P:
        def __init__(self):
            self.plugins = [
                {"name": "HSResources.esl", "enabled": True},
                {"name": "HSPlayerHomes - Breezehome.esp", "enabled": True},
            ]

    gen.manifest = P()
    out = Path("/tmp/plugins-by-name.txt")
    gen._write_plugins(out)
    body = [l for l in out.read_text().splitlines() if l.strip() and not l.startswith("#")]
    assert body == ["*HSResources.esl", "*HSPlayerHomes - Breezehome.esp"], body


def test_write_plugins_honours_the_manifest_order_by_filename():
    """And the `filename` key too, for manifests that carry it."""
    gen = LoadOrderGenerator.__new__(LoadOrderGenerator)
    gen.game_domain = DOMAIN

    class P:
        def __init__(self):
            self.plugins = [{"filename": "a.esl", "enabled": True},
                            {"filename": "b.esp", "enabled": False}]

    gen.manifest = P()
    out = Path("/tmp/plugins-by-filename.txt")
    gen._write_plugins(out)
    body = [l for l in out.read_text().splitlines() if l.strip() and not l.startswith("#")]
    assert body == ["*a.esl", "b.esp"], body


def test_master_is_not_ordered_after_its_dependents(tmp_path):
    """The end-to-end shape: a manifest that puts the master first keeps it first.

    The manifest is the collection author's statement of intent. The
    generated plugins.txt must not silently reorder it into the mod-level
    (phase + name) sort, which is what put HSResources.esl 7th.
    """
    mods, game = tmp_path / "col", tmp_path / "game"
    game.mkdir()
    (mods / "AAA").mkdir(parents=True)
    (mods / "AAA" / "master.esl").write_bytes(b"m")
    (mods / "AAA" / "dependent.esp").write_bytes(b"d")
    _state(mods, game, {
        "11": {"mod_id": 1, "name": "Mod", "file_id": 11, "filename": "a.zip",
               "version": "1", "download_status": "extracted",
               "contributed_files": ["AAA/master.esl", "AAA/dependent.esp"]},
    }, manifest={"mods": [],
                 "plugins": [{"name": "master.esl", "enabled": True},
                             {"name": "dependent.esp", "enabled": True}]})

    pfx = tmp_path / "pfx"
    pfx.mkdir()
    ModManagerService(api_key="test-key-unused").deploy(
        mods, game_dir=game, prefix=pfx, use_copy=True
    )

    written = (mods / "plugins.txt").read_text()
    body = [l.strip().lstrip("*") for l in written.splitlines()
            if l.strip() and not l.startswith("#")]
    assert body[0] == "master.esl", f"master was reordered: {body}"
    assert body == ["master.esl", "dependent.esp"], body


# ---------------------------------------------------------------------------
# 2. A plan warning is reported once
# ---------------------------------------------------------------------------


def test_a_plan_warning_reaches_the_result_exactly_once(tmp_path):
    mods, game = tmp_path / "col", tmp_path / "game"
    game.mkdir()
    (mods / "AAA").mkdir(parents=True)
    (mods / "AAA" / "one.esp").write_bytes(b"x")
    # a conditional FOMOD with no game state -> one plan warning
    cfg = """<config><moduleName>T</moduleName>
      <conditionalFileInstalls><patterns><pattern>
        <dependencies><fileDependency file="Master.esm" state="Active"/></dependencies>
        <files><file source="a/one.esp" destination="Data/one.esp"/></files>
      </pattern></patterns></conditionalFileInstalls></config>"""
    (mods / "AAA" / "fomod").mkdir()
    (mods / "AAA" / "fomod" / "ModuleConfig.xml").write_text(cfg)
    _state(mods, game, {
        "11": {"mod_id": 1, "name": "M", "file_id": 11, "filename": "a.zip",
               "version": "1", "download_status": "extracted",
               "contributed_files": ["AAA/one.esp", "AAA/fomod/ModuleConfig.xml"],
               "fomod_config": {"member": "AAA/fomod/ModuleConfig.xml", "xml": cfg}},
    })

    r = ModManagerService(api_key="test-key-unused").deploy(mods, game_dir=game, use_copy=True)
    fallbacks = [w for w in r.warnings if "could not be evaluated" in w]
    assert len(fallbacks) == 1, f"the same warning was reported {len(fallbacks)} times: {r.warnings}"


# ---------------------------------------------------------------------------
# 3. A case collision is reported as one
# ---------------------------------------------------------------------------


def test_case_collides_is_reported_even_when_the_fixup_normalises(tmp_path):
    """The fixup collapses Foo.esp -> foo.esp; the check must still see the pair.

    Two mods ship case-variant names at one destination and the manifest
    names one of them. The collision is the thing the user needs to know;
    an overwrite message that names a file the plan no longer contains is
    not that.
    """
    mods, game = tmp_path / "col", tmp_path / "game"
    game.mkdir()
    mods.mkdir()
    (mods / "Foo.esp").write_bytes(b"AAA")
    (mods / "foo.esp").write_bytes(b"BBB")
    _state(mods, game, {
        "11": {"mod_id": 1, "name": "Alpha", "file_id": 11, "filename": "a.zip",
               "version": "1", "download_status": "extracted",
               "contributed_files": ["Foo.esp"]},
        "12": {"mod_id": 2, "name": "Beta", "file_id": 12, "filename": "b.zip",
               "version": "1", "download_status": "extracted",
               "contributed_files": ["foo.esp"]},
    }, manifest={"mods": [],
                 "plugins": [{"filename": "foo.esp", "name": "foo.esp"}]})

    r = ModManagerService(api_key="test-key-unused").deploy(mods, game_dir=game, use_copy=True)

    collides = [w for w in r.warnings if "case-collides" in w]
    assert collides, f"a case collision was not reported as one: {r.warnings}"
    # both originals must be named
    assert any("Foo.esp" in w and "foo.esp" in w for w in collides), collides
