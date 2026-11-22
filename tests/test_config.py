"""User-level config: the keystone of the 0.5.0 front door.

Everything the setup milestone builds on sits here. Once a user-level
config exists, first-run detection ("config missing"), the wizard (which
writes it), bare-`nexus-dl` booting the shell, and defaults in
shell/sync/deploy/handler map are all small. Without it, every command
re-asks for its arguments.

Lives next to the API key and handler.json in the XDG config directory.
"""

from __future__ import annotations

import json
from pathlib import Path

from nexus_collection_dl.config import UserConfig


def test_missing_config_is_a_first_run(tmp_path, monkeypatch):
    """No config file means the tool has never been set up.

    That is the signal the front door acts on: boot the wizard, not the
    workbench.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg = UserConfig.load()
    assert cfg.is_configured() is False
    assert cfg.collections_root == ""


def test_unreadable_config_is_a_first_run(tmp_path, monkeypatch):
    """A corrupt config must not crash the tool or look configured.

    Hand-edited or half-written config is a real state; treat it as
    "needs setup" rather than dying on a parse error.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = UserConfig.path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json")

    cfg = UserConfig.load()
    assert cfg.is_configured() is False


def test_path_is_xdg_aware(tmp_path, monkeypatch):
    """XDG_CONFIG_HOME decides where the config lives."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert UserConfig.path() == tmp_path / "nexus-dl" / "config.json"

    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert UserConfig.path() == Path.home() / ".config" / "nexus-dl" / "config.json"


def test_save_and_load_round_trips(tmp_path, monkeypatch):
    """What the wizard writes has to come back exactly."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg = UserConfig(
        collections_root=str(tmp_path / "mods"),
        games=[
            {"domain": "skyrimspecialedition", "game_dir": "/games/skyrim"},
        ],
    )
    cfg.save()

    loaded = UserConfig.load()
    assert loaded.collections_root == str(tmp_path / "mods")
    assert loaded.games == [{"domain": "skyrimspecialedition", "game_dir": "/games/skyrim"}]
    assert loaded.is_configured() is True


def test_collections_root_is_what_makes_it_configured(tmp_path, monkeypatch):
    """Configured means "has a place to put collections", not "has a file".

    A config that exists but names no collections root is still a first
    run: the tool has nowhere to put anything.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg = UserConfig()
    cfg.save()
    assert UserConfig.load().is_configured() is False

    cfg.collections_root = str(tmp_path / "mods")
    cfg.save()
    assert UserConfig.load().is_configured() is True


def test_save_preserves_unknown_keys(tmp_path, monkeypatch):
    """Forward compatibility, same rule as the collection state file.

    A key this version does not model survives a save, so the next new
    key does not disappear when someone runs a slightly older build.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg = UserConfig(collections_root=str(tmp_path / "mods"))
    cfg.save()

    path = UserConfig.path()
    data = json.loads(path.read_text())
    data["future_feature"] = {"enabled": True}
    path.write_text(json.dumps(data))

    UserConfig(collections_root=str(tmp_path / "mods")).save()
    after = json.loads(path.read_text())
    assert after["future_feature"] == {"enabled": True}


def test_save_is_atomic(tmp_path, monkeypatch):
    """A crash mid-write must not leave a half-written config behind.

    Same contract as the collection state file: temp file, fsync,
    rename.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg = UserConfig(collections_root=str(tmp_path / "mods"))
    cfg.save()
    path = UserConfig.path()
    original = path.read_text()

    class Boom:
        pass

    # Constructible but not JSON-serializable: the serialize fails after
    # the temp file is opened, which is the window under test.
    cfg.games = [Boom()]  # type: ignore[list-item]
    try:
        cfg.save()
    except Exception:  # noqa: S110 - the failure is the point of the test
        pass
    assert path.read_text() == original


def test_config_dir_exists_after_save(tmp_path, monkeypatch):
    """Saving creates the directory. The first run has no config dir yet."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert not UserConfig.path().parent.exists()
    UserConfig(collections_root=str(tmp_path / "mods")).save()
    assert UserConfig.path().parent.is_dir()


def test_health_reports_a_first_run(tmp_path, monkeypatch):
    """The health report has to say when the tool has never been set up.

    It is the only thing that looks at the whole installation, so it is
    where a fresh install gets noticed.
    """
    from nexus_collection_dl.service import collect_health

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    checks = collect_health(tmp_path, scan=_empty_scan(tmp_path))
    config_checks = [c for c in checks if "config" in c.label.lower()]
    assert config_checks, [c.label for c in checks]
    assert config_checks[0].status != "OK"


def test_health_reports_a_configured_tool(tmp_path, monkeypatch):
    """Once the wizard has written a collections root, health says so."""
    from nexus_collection_dl.service import collect_health

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    UserConfig(collections_root=str(tmp_path / "mods")).save()
    checks = collect_health(tmp_path, scan=_empty_scan(tmp_path))
    config_checks = [c for c in checks if "config" in c.label.lower()]
    assert config_checks, [c.label for c in checks]
    assert config_checks[0].status == "OK"


def _empty_scan(root: Path):
    from nexus_collection_dl.service import scan_collections

    return scan_collections(root)
