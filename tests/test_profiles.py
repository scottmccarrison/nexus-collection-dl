"""Profile switching: data model and CRUD (0.6.0).

A profile is a named set of collections and mods for one game.
Switching undeploys the current set and deploys the new one.
"""
from __future__ import annotations

import json

import pytest

# ---------------------------------------------------------------------------
# Detection: what kind of thing did the user pass?
# ---------------------------------------------------------------------------


def test_detect_collection_url():
    from nexus_collection_dl.profiles import detect_ref
    kind, ref = detect_ref("https://next.nexusmods.com/skyrimspecialedition/collections/i9gfac")
    assert kind == "collection"
    assert ref == "i9gfac"


def test_detect_mod_url():
    from nexus_collection_dl.profiles import detect_ref
    kind, ref = detect_ref("https://www.nexusmods.com/skyrimspecialedition/mods/12345")
    assert kind == "mod"
    assert ref == 12345


def test_detect_nxm_url():
    from nexus_collection_dl.profiles import detect_ref
    kind, ref = detect_ref("nxm://skyrimspecialedition/mods/12345/files/67890")
    assert kind == "mod"
    assert ref == 12345


def test_detect_bare_number_is_mod_id():
    from nexus_collection_dl.profiles import detect_ref
    kind, ref = detect_ref("12345")
    assert kind == "mod"
    assert ref == 12345


def test_detect_name_is_collection():
    from nexus_collection_dl.profiles import detect_ref
    kind, ref = detect_ref("HS Player Homes")
    assert kind == "collection"
    assert ref == "HS Player Homes"


# ---------------------------------------------------------------------------
# Profile CRUD
# ---------------------------------------------------------------------------


def test_create_empty_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from nexus_collection_dl.profiles import create_profile, list_profiles
    pid = create_profile("vanilla-plus", game="skyrimspecialedition")
    assert pid == 1
    profiles = list_profiles()
    assert len(profiles) == 1
    assert profiles[0]["name"] == "vanilla-plus"
    assert profiles[0]["collections"] == []
    assert profiles[0]["mods"] == []


def test_profile_ids_increment(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from nexus_collection_dl.profiles import create_profile
    p1 = create_profile("first", game="skyrimspecialedition")
    p2 = create_profile("second", game="skyrimspecialedition")
    assert p1 == 1
    assert p2 == 2


def test_add_collection_to_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from nexus_collection_dl.profiles import add_to_profile, create_profile, get_profile
    pid = create_profile("test", game="skyrimspecialedition")
    add_to_profile(pid, "collection", "HS Player Homes")
    p = get_profile(pid)
    assert p["collections"] == ["HS Player Homes"]


def test_add_mod_to_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from nexus_collection_dl.profiles import add_to_profile, create_profile, get_profile
    pid = create_profile("test", game="skyrimspecialedition")
    add_to_profile(pid, "mod", 12345)
    p = get_profile(pid)
    assert p["mods"] == [12345]


def test_remove_from_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from nexus_collection_dl.profiles import (
        add_to_profile,
        create_profile,
        get_profile,
        remove_from_profile,
    )
    pid = create_profile("test", game="skyrimspecialedition")
    add_to_profile(pid, "collection", "HS Player Homes")
    add_to_profile(pid, "mod", 12345)
    remove_from_profile(pid, "collection", "HS Player Homes")
    p = get_profile(pid)
    assert p["collections"] == []
    assert p["mods"] == [12345]


def test_duplicate_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from nexus_collection_dl.profiles import (
        add_to_profile,
        create_profile,
        duplicate_profile,
        get_profile,
    )
    pid = create_profile("original", game="skyrimspecialedition")
    add_to_profile(pid, "collection", "HS Player Homes")
    add_to_profile(pid, "mod", 12345)
    new_id = duplicate_profile(pid, "copy")
    p = get_profile(new_id)
    assert p["name"] == "copy"
    assert p["collections"] == ["HS Player Homes"]
    assert p["mods"] == [12345]
    # Original unchanged
    orig = get_profile(pid)
    assert orig["name"] == "original"


def test_delete_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from nexus_collection_dl.profiles import (
        create_profile,
        delete_profile,
        list_profiles,
    )
    pid = create_profile("doomed", game="skyrimspecialedition")
    assert len(list_profiles()) == 1
    delete_profile(pid)
    assert len(list_profiles()) == 0


def test_resolve_profile_by_name(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from nexus_collection_dl.profiles import create_profile, resolve_profile
    pid = create_profile("my-profile", game="skyrimspecialedition")
    assert resolve_profile("my-profile") == pid
    assert resolve_profile(str(pid)) == pid
    assert resolve_profile(pid) == pid


def test_resolve_profile_unknown_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from nexus_collection_dl.profiles import ProfileError, resolve_profile
    with pytest.raises(ProfileError):
        resolve_profile("nope")


def test_add_duplicate_collection_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from nexus_collection_dl.profiles import add_to_profile, create_profile, get_profile
    pid = create_profile("test", game="skyrimspecialedition")
    add_to_profile(pid, "collection", "A")
    add_to_profile(pid, "collection", "A")
    p = get_profile(pid)
    assert p["collections"] == ["A"]


# ---------------------------------------------------------------------------
# Profile deploy integration
# ---------------------------------------------------------------------------


def test_use_profile_deploys_collections(tmp_path, monkeypatch):
    """Switching to a profile deploys its collections."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from nexus_collection_dl.profiles import (
        add_to_profile,
        create_profile,
        use_profile,
    )

    # Set up a collection with content
    root = tmp_path / "collections"
    root.mkdir()
    coll = root / "My Collection"
    coll.mkdir()
    (coll / "Data").mkdir()
    (coll / "Data" / "m.esp").write_text("x")
    state_file = coll / ".nexus-state.json"
    state_file.write_text(json.dumps({
        "collection_url": "u", "collection_name": "My Collection",
        "collection_revision": 1, "game_domain": "skyrimspecialedition",
        "mods": {"1": {"mod_id": 1, "name": "M", "file_id": 1,
                        "filename": "m.zip", "version": "1", "phase": 0,
                        "enabled": True, "optional": False,
                        "contributed_files": ["Data/m.esp"], "requirements": []}},
        "tombstones": {},
    }))

    game = tmp_path / "game"
    game.mkdir()
    (game / "pfx" / "drive_c" / "users" / "steamuser").mkdir(parents=True)

    pid = create_profile("test", game="skyrimspecialedition", game_dir=str(game))
    add_to_profile(pid, "collection", "My Collection")

    result = use_profile(pid, collections_root=root, game_dir=game)
    assert result["deployed"] > 0, result
    assert (game / "Data" / "m.esp").exists()


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


def test_cli_profiles_list_create_delete(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from click.testing import CliRunner

    from nexus_collection_dl import cli as cli_module

    r = CliRunner().invoke(cli_module.main, ["profiles", "list"])
    assert r.exit_code == 0
    assert "No profiles" in r.output

    r = CliRunner().invoke(cli_module.main, ["profiles", "create", "my-profile"])
    assert r.exit_code == 0
    assert "created" in r.output.lower()

    r = CliRunner().invoke(cli_module.main, ["profiles", "list"])
    assert r.exit_code == 0
    assert "my-profile" in r.output

    r = CliRunner().invoke(cli_module.main, ["profiles", "delete", "1"])
    assert r.exit_code == 0
    assert "deleted" in r.output.lower()


def test_cli_profiles_add_remove(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from click.testing import CliRunner

    from nexus_collection_dl import cli as cli_module

    CliRunner().invoke(cli_module.main, ["profiles", "create", "test"])

    r = CliRunner().invoke(cli_module.main, ["profiles", "add", "1", "12345"])
    assert r.exit_code == 0
    assert "Added mod" in r.output

    r = CliRunner().invoke(cli_module.main, ["profiles", "add", "1", "My Collection"])
    assert r.exit_code == 0
    assert "Added collection" in r.output

    r = CliRunner().invoke(cli_module.main, ["profiles", "remove", "1", "12345"])
    assert r.exit_code == 0
    assert "Removed mod" in r.output


def test_cli_profiles_duplicate(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from click.testing import CliRunner

    from nexus_collection_dl import cli as cli_module

    CliRunner().invoke(cli_module.main, ["profiles", "create", "original"])
    CliRunner().invoke(cli_module.main, ["profiles", "add", "1", "12345"])

    r = CliRunner().invoke(cli_module.main, ["profiles", "duplicate", "1", "my-copy"])
    assert r.exit_code == 0
    assert "created" in r.output.lower()


def test_cli_profiles_json(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from click.testing import CliRunner

    from nexus_collection_dl import cli as cli_module

    CliRunner().invoke(cli_module.main, ["profiles", "create", "json-test"])

    r = CliRunner().invoke(cli_module.main, ["--json", "profiles", "list"])
    assert r.exit_code == 0
    import json
    data = json.loads(r.output)
    assert data["ok"] is True
    assert len(data["result"]["profiles"]) == 1
