"""Batch G: small enhancements from the 0.5.0 backlog.

#129: Enderal / Enderal SE get a libloot GameType.
#128: installer XML is not stored inline in the state file.
#162: the shell can change the collections root.
"""
from __future__ import annotations

import json

# ---------------------------------------------------------------------------
# #129: Enderal GameType
# ---------------------------------------------------------------------------


def test_enderal_has_a_loot_game_type():
    from nexus_collection_dl.loot_sort import LOOT_GAME_MAP
    assert LOOT_GAME_MAP["enderal"]["game_type"] == "Enderal"
    assert LOOT_GAME_MAP["enderalspecialedition"]["game_type"] == "EnderalSE"


# ---------------------------------------------------------------------------
# #128: installer XML is not inline in the state file
# ---------------------------------------------------------------------------


def test_state_file_does_not_store_full_installer_xml(tmp_path):
    """A FOMOD mod's ModuleConfig.xml must not be serialized inline."""
    from nexus_collection_dl.state import CollectionState

    state = CollectionState(tmp_path)
    state.set_collection_info("u", "n", 1, "skyrimspecialedition")
    state.add_mod({
        "mod_id": 1, "mod_name": "FOMOD Mod", "file_id": 1,
        "filename": "m.zip", "version": "1", "phase": 0,
        "enabled": True, "optional": False,
        "contributed_files": [], "requirements": [],
    })
    state.mods[1].fomod_config = {
        "xml": "<config><module name=\"test\">" + "<step/>" * 100 + "</config>",
        "member": "fomod/ModuleConfig.xml",
    }
    state.save()

    raw = (tmp_path / ".nexus-state.json").read_text()
    # The raw XML string must not appear in the state file
    assert "<step/>" not in raw, "full installer XML must not be inline"
    # But the metadata must survive
    data = json.loads(raw)
    mod = next(iter(data["mods"].values()))
    assert mod.get("fomod_config", {}).get("member") == "fomod/ModuleConfig.xml"


# ---------------------------------------------------------------------------
# #162: the shell can change the collections root
# ---------------------------------------------------------------------------


def test_shell_config_command_exists():
    """The shell must have a /config command."""
    from nexus_collection_dl.shell import _ShellSession
    assert "/config" in _ShellSession._COMMANDS, (
        f"shell commands: {list(_ShellSession._COMMANDS.keys())}"
    )
