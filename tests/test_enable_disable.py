"""#44: a mod can be disabled without being removed.

Vortex can turn a mod off and on. Here a mod was either synced or gone:
the only way to stop it deploying was `remove`, which drops it from state
and records a tombstone so `sync` will not bring it back. That is not
"temporarily off", that is "gone".

An `enabled` flag on state, skipped at deploy, toggled from the CLI and
the shell. Removing a mod is still `remove`; this is the in-between.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from nexus_collection_dl import cli as cli_module
from nexus_collection_dl.config import UserConfig  # noqa: F401
from nexus_collection_dl.state import CollectionState


def _state_with(tmp_path: Path, **fields) -> CollectionState:
    mods = tmp_path / "mods"
    mods.mkdir(parents=True, exist_ok=True)
    state = CollectionState(mods)
    state.set_collection_info("u", "n", 1, "skyrimspecialedition")
    state.add_mod(
        {
            "mod_id": fields.get("mod_id", 1),
            "mod_name": fields.get("name", "Body"),
            "file_id": fields.get("file_id", 11),
            "filename": fields.get("filename", "body.zip"),
            "version": "1",
        }
    )
    return state


def test_mod_state_carries_an_enabled_flag(tmp_path):
    """The flag has to live in state, or it cannot survive a restart."""
    state = _state_with(tmp_path)
    state.save()
    loaded = CollectionState(state.mods_dir)
    loaded.load()
    assert loaded.mods[11].enabled is True


def test_a_mod_can_be_disabled(tmp_path):
    state = _state_with(tmp_path)
    state.mods[11].enabled = False
    state.save()
    loaded = CollectionState(state.mods_dir)
    loaded.load()
    assert loaded.mods[11].enabled is False


def test_enabled_defaults_to_true_for_old_state(tmp_path):
    """State written before this flag must load as enabled.

    A mod the user has not disabled is enabled. Dropping the key on load
    would silently turn mods off.
    """
    mods = tmp_path / "mods"
    state = _state_with(tmp_path)
    state.save()
    path = mods / ".nexus-state.json"
    data = json.loads(path.read_text())
    data["mods"]["11"].pop("enabled", None)
    path.write_text(json.dumps(data))

    loaded = CollectionState(mods)
    loaded.load()
    assert loaded.mods[11].enabled is True


def test_cli_disable_turns_a_mod_off(tmp_path):
    state = _state_with(tmp_path)
    state.save()
    r = CliRunner().invoke(
        cli_module.main, ["disable", str(state.mods_dir), "--file-id", "11"]
    )
    assert r.exit_code == 0, r.output
    loaded = CollectionState(state.mods_dir)
    loaded.load()
    assert loaded.mods[11].enabled is False


def test_cli_enable_turns_a_mod_on(tmp_path):
    state = _state_with(tmp_path)
    state.mods[11].enabled = False
    state.save()
    r = CliRunner().invoke(
        cli_module.main, ["enable", str(state.mods_dir), "--file-id", "11"]
    )
    assert r.exit_code == 0, r.output
    loaded = CollectionState(state.mods_dir)
    loaded.load()
    assert loaded.mods[11].enabled is True


def test_cli_disable_without_a_match_fails_loudly(tmp_path):
    state = _state_with(tmp_path)
    state.save()
    r = CliRunner().invoke(
        cli_module.main, ["disable", str(state.mods_dir), "--file-id", "999"]
    )
    assert r.exit_code != 0


def test_disabled_mod_is_not_deployed(tmp_path):
    """The point of the flag: off means the files do not land.

    Not removal: the state entry stays, the picks stay, and turning it
    back on deploys it again.
    """
    from nexus_collection_dl.service import ModManagerService

    mods = tmp_path / "mods"
    (mods / "a").mkdir(parents=True)
    (mods / "a" / "one.esp").write_bytes(b"1")
    game = tmp_path / "game"
    game.mkdir()
    state = _state_with(tmp_path)
    state.mods[11].enabled = False
    # Attribution recorded at extract time: without it a disabled mod
    # cannot be told apart from its neighbours in the merged tree.
    state.mods[11].contributed_files = ["a/one.esp"]
    state.save()

    svc = ModManagerService(api_key="test-key-unused")
    result = svc.deploy(mods, game_dir=game, use_copy=True)
    assert result.deployed_count == 0, result


def test_enabled_mod_still_deploys(tmp_path):
    """The default path must not change."""
    from nexus_collection_dl.service import ModManagerService

    mods = tmp_path / "mods"
    (mods / "a").mkdir(parents=True)
    (mods / "a" / "one.esp").write_bytes(b"1")
    game = tmp_path / "game"
    game.mkdir()
    state = _state_with(tmp_path)
    state.mods[11].contributed_files = ["a/one.esp"]
    state.save()

    svc = ModManagerService(api_key="test-key-unused")
    result = svc.deploy(mods, game_dir=game, use_copy=True)
    assert result.deployed_count == 1, result
