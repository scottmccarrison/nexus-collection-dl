"""Typed-wrong input at the command boundary.

The typed-hostile half of the command-surface pass landed in #167 and
#171 (state-derived paths and roots). This is the typed-wrong half: a
person makes a typo or points a flag at the wrong kind of thing.

Two rules:

- A root must be an existing directory before anything trusts it. A
  flag naming a path is a statement about where to act, so a path that
  is not there is an error, not an invitation to fall back. And a value
  that is not a directory must never change how the deploy runs.
- An id that is not a real id is not a match. 0 is the "unknown" marker
  from_dict leaves behind, not a Nexus mod page, and matching it turns
  one careless flag into a bulk operation on exactly the entries least
  able to speak for themselves.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus_collection_dl import cli as cli_module
from nexus_collection_dl.service import (
    ModManagerService,
    resolve_game_root,
    resolve_proton_prefix,
)
from nexus_collection_dl.state import CollectionState

DOMAIN = "skyrimspecialedition"


@pytest.fixture(autouse=True)
def no_machine(monkeypatch, tmp_path):
    """No user config, no Steam: only what the test sets up can speak."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-config"))
    monkeypatch.setenv("NEXUS_DL_STEAM_ROOT", str(tmp_path / "no-steam"))


def _collection(tmp_path: Path) -> Path:
    mods = tmp_path / "col"
    (mods / "ModA").mkdir(parents=True)
    (mods / "ModA" / "plugin.esp").write_bytes(b"esp")
    state = CollectionState(mods)
    state.set_collection_info("u", "n", 1, DOMAIN)
    state.save()
    return mods


def _svc() -> ModManagerService:
    return ModManagerService(api_key="test-key-unused")


# ---------------------------------------------------------------------------
# 1. A root must be an existing directory before it is trusted
# ---------------------------------------------------------------------------


def test_resolve_rejects_an_explicit_game_dir_that_does_not_exist(tmp_path):
    """The user named it. A path that is not there is wrong, not absent."""
    root = resolve_game_root(tmp_path / "nope", "", DOMAIN)
    assert root.problem, f"accepted a nonexistent --game-dir: {root}"
    assert "nope" in root.problem


def test_resolve_rejects_an_explicit_game_dir_that_is_a_file(tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    root = resolve_game_root(f, "", DOMAIN)
    assert root.problem, f"accepted a file as --game-dir: {root}"


def test_resolve_rejects_an_explicit_prefix_that_does_not_exist(tmp_path):
    root = resolve_proton_prefix(tmp_path / "nope", "", DOMAIN)
    assert root.problem, f"accepted a nonexistent --prefix: {root}"


def test_resolve_rejects_an_explicit_prefix_that_is_a_file(tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    root = resolve_proton_prefix(f, "", DOMAIN)
    assert root.problem, f"accepted a file as --prefix: {root}"


def test_resolve_falls_through_when_the_config_names_a_missing_dir(tmp_path):
    """A stale config entry is not the user naming a path this run.

    It should be reported and the next source tried, not stop the run
    the way a bad explicit flag does.
    """
    cfg_dir = tmp_path / "cfg" / "nexus-dl"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text(json.dumps({
        "collections_root": str(tmp_path),
        "games": [{"domain": DOMAIN, "game_dir": str(tmp_path / "gone")}],
        "defaults": {},
    }))
    monkeypatch_path = tmp_path / "cfg"
    import os
    os.environ["XDG_CONFIG_HOME"] = str(monkeypatch_path)
    root = resolve_game_root(None, "", DOMAIN)
    assert root.problem is None
    assert root.source != "config", "stale config entry was trusted"


# ---------------------------------------------------------------------------
# 2. A value that is not a directory must not change how the deploy runs
# ---------------------------------------------------------------------------


def test_deploy_with_a_nonexistent_prefix_reports_it_and_changes_nothing(tmp_path):
    """The reported bug: a bad path silently forced copy mode.

    It must say the prefix is wrong, and it must not create a Proton
    tree under a directory that does not exist.
    """
    mods = _collection(tmp_path)
    game = tmp_path / "game"
    game.mkdir()
    bogus = tmp_path / "nope"

    result = _svc().deploy(mods, game_dir=game, prefix=bogus, use_copy=False)

    assert result.errors, f"no error for a nonexistent prefix: {result}"
    assert any("prefix" in e.lower() for e in result.errors), result.errors
    assert not any("copy mode" in w.lower() for w in result.warnings), result.warnings
    assert not bogus.exists(), f"created a tree under a prefix that does not exist: {bogus}"
    assert result.deployed_count == 0


def test_deploy_with_a_prefix_that_is_a_file_reports_it(tmp_path):
    mods = _collection(tmp_path)
    game = tmp_path / "game"
    game.mkdir()
    f = tmp_path / "afile"
    f.write_text("x")

    result = _svc().deploy(mods, game_dir=game, prefix=f, use_copy=False)

    assert result.errors, f"no error for a file as prefix: {result}"
    assert result.deployed_count == 0


def test_deploy_with_a_real_prefix_still_forces_copy_mode(tmp_path):
    """The fix must not break the Proton behaviour it is guarding."""
    mods = _collection(tmp_path)
    game = tmp_path / "game"
    game.mkdir()
    pfx = tmp_path / "pfx"
    pfx.mkdir()

    result = _svc().deploy(mods, game_dir=game, prefix=pfx, use_copy=False)

    assert result.errors == [], result.errors
    assert any("copy mode" in w.lower() for w in result.warnings), result.warnings


def test_deploy_with_no_prefix_at_all_keeps_symlinks(tmp_path):
    """A missing prefix is normal (non-Proton games), not an error."""
    mods = _collection(tmp_path)
    game = tmp_path / "game"
    game.mkdir()

    result = _svc().deploy(mods, game_dir=game, use_copy=False)

    assert result.errors == [], result.errors
    assert not any("copy mode" in w.lower() for w in result.warnings), result.warnings


# ---------------------------------------------------------------------------
# 3. A path argument that will be created must not already be a file
# ---------------------------------------------------------------------------


def test_sync_refuses_a_mods_dir_that_is_a_file(tmp_path, monkeypatch):
    """It used to reach mkdir and leak NotADirectoryError at the user."""

    from nexus_collection_dl.api import NexusAPI

    victim = tmp_path / "afile.txt"
    victim.write_text("i am a file")

    monkeypatch.setattr(NexusAPI, "validate_key", lambda self: {"isPremium": True})
    monkeypatch.setattr(
        NexusAPI, "get_collection_mods",
        lambda self, d, s: {"name": "Some Collection", "mod_rules": [], "mods": []},
    )

    r = CliRunner().invoke(cli_module.main, [
        "sync",
        f"https://next.nexusmods.com/{DOMAIN}/collections/x",
        str(victim),
    ])

    assert r.exit_code != 0
    assert not isinstance(
        r.exception, (FileExistsError, NotADirectoryError)
    ), f"raw {type(r.exception).__name__} reached the user"
    assert "Error" in r.output or "not a directory" in r.output.lower(), r.output


# ---------------------------------------------------------------------------
# 4. 0 is not a matchable mod id
# ---------------------------------------------------------------------------


def _legacy_state(mods: Path) -> None:
    """Two entries whose mod_id is 0 or missing: the from_dict default."""
    (mods / "ModA").mkdir(parents=True)
    (mods / "ModA" / "a.esp").write_bytes(b"x")
    (mods / ".nexus-state.json").write_text(json.dumps({
        "collection_url": "u", "collection_name": "n", "collection_revision": 1,
        "game_domain": DOMAIN,
        "mods": {
            "11": {"mod_id": 0, "mod_name": "Legacy One", "file_id": 11,
                   "filename": "one.zip", "version": "1",
                   "download_status": "downloaded",
                   "contributed_files": ["ModA/a.esp"]},
            "12": {"mod_name": "Legacy Two", "file_id": 12,
                   "filename": "two.zip", "version": "1",
                   "download_status": "downloaded"},
        },
    }))


def test_disable_mod_id_zero_does_not_flip_legacy_entries(tmp_path):
    mods = tmp_path / "col"
    mods.mkdir()
    _legacy_state(mods)

    r = CliRunner().invoke(cli_module.main, ["disable", str(mods), "--mod-id", "0"])

    assert r.exit_code != 0, f"accepted --mod-id 0: {r.output}"
    state = CollectionState(mods)
    state.load()
    assert all(m.enabled for m in state.mods.values()), "flipped legacy entries"


def test_enable_mod_id_zero_is_refused(tmp_path):
    mods = tmp_path / "col"
    mods.mkdir()
    _legacy_state(mods)

    r = CliRunner().invoke(cli_module.main, ["enable", str(mods), "--mod-id", "0"])

    assert r.exit_code != 0, f"accepted --mod-id 0: {r.output}"
    state = CollectionState(mods)
    state.load()
    assert all(m.enabled for m in state.mods.values())


def test_remove_mod_id_zero_does_not_bulk_delete(tmp_path):
    """The same match shape sits in remove. Worse there: it deletes."""
    mods = tmp_path / "col"
    mods.mkdir()
    _legacy_state(mods)

    r = CliRunner().invoke(cli_module.main, ["remove", str(mods), "--mod-id", "0"])

    assert r.exit_code != 0, f"accepted --mod-id 0: {r.output}"
    assert (mods / "ModA" / "a.esp").exists(), "bulk-deleted through --mod-id 0"
    state = CollectionState(mods)
    state.load()
    assert len(state.mods) == 2, "removed legacy entries"


def test_a_real_mod_id_still_matches(tmp_path):
    """The fix must not stop real ids working."""
    mods = tmp_path / "col"
    mods.mkdir()
    (mods / "ModA").mkdir()
    (mods / "ModA" / "a.esp").write_bytes(b"x")
    (mods / ".nexus-state.json").write_text(json.dumps({
        "collection_url": "u", "collection_name": "n", "collection_revision": 1,
        "game_domain": DOMAIN,
        "mods": {
            "11": {"mod_id": 55, "mod_name": "Real Mod", "file_id": 11,
                   "filename": "one.zip", "version": "1",
                   "download_status": "downloaded",
                   "contributed_files": ["ModA/a.esp"]},
        },
    }))

    r = CliRunner().invoke(cli_module.main, ["disable", str(mods), "--mod-id", "55"])

    assert r.exit_code == 0, r.output
    state = CollectionState(mods)
    state.load()
    assert not state.mods[11].enabled, "a real mod id did not match"
