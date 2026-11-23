"""`nexus-dl setup`: the wizard that writes the user-level config.

The keystone exists (UserConfig). This is the thing that writes it, and
the thing bare `nexus-dl` boots on a fresh install.

Two shapes:

- Interactive: detect what it can (Steam root, installed games), ask for
  the rest, write the config.
- Non-interactive: flags only, no prompts, no detection surprises. That
  is what a harness or a script needs, and it is also what makes this
  testable.

Detection is a real sweep: Steam library folders, appmanifest files, the
domain map. A game that is installed gets found; one that is not does
not appear.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from nexus_collection_dl import cli as cli_module
from nexus_collection_dl.config import UserConfig

# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _fake_steam(tmp_path: Path, installed: dict[str, str]) -> Path:
    """A Steam root with a library holding the given app_id -> installdir.

    `installed` maps the Steam app id to the install directory name,
    which is exactly what a real appmanifest carries.
    """
    root = tmp_path / "steam"
    library = tmp_path / "gamelib"
    (library / "steamapps" / "common").mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    (root / "config" / "libraryfolders.vdf").write_text(
        f'"libraryfolders"\n{{\n  "0"\n  {{\n    "path"  "{library}"\n  }}\n}}\n'
    )
    for app_id, installdir in installed.items():
        (library / "steamapps" / f"appmanifest_{app_id}.acf").write_text(
            f'"AppState"\n{{\n  "installdir"  "{installdir}"\n}}\n'
        )
        (library / "steamapps" / "common" / installdir).mkdir(exist_ok=True)
    return root


def test_installed_games_sweep_finds_what_is_installed(tmp_path):
    """The wizard can only offer games that are actually here."""
    from nexus_collection_dl.steam import list_installed_games

    _fake_steam(tmp_path, {"489830": "Skyrim Special Edition", "377160": "Fallout 4"})

    games = list_installed_games(steam_root=tmp_path / "steam")
    domains = {g["domain"] for g in games}
    assert domains == {"skyrimspecialedition", "fallout4"}


def test_installed_games_sweep_ignores_unknown_app_ids(tmp_path):
    """A game with no Nexus domain mapping is not something we can offer.

    Steam holds every game the user owns. Only the ones this tool can
    actually manage belong in the wizard.
    """
    from nexus_collection_dl.steam import list_installed_games

    _fake_steam(tmp_path, {"489830": "Skyrim Special Edition", "999999": "Some Game"})
    games = list_installed_games(steam_root=tmp_path / "steam")
    assert {g["domain"] for g in games} == {"skyrimspecialedition"}


def test_installed_games_sweep_reports_the_game_dir(tmp_path):
    """The wizard has to write a path that exists, not just a name."""
    from nexus_collection_dl.steam import list_installed_games

    _fake_steam(tmp_path, {"489830": "Skyrim Special Edition"})
    games = list_installed_games(steam_root=tmp_path / "steam")
    (skyrim,) = games
    assert Path(skyrim["game_dir"]).name == "Skyrim Special Edition"
    assert Path(skyrim["game_dir"]).is_dir()


def test_installed_games_with_no_steam_is_empty_not_an_error(tmp_path):
    """A machine without Steam is a normal state, not a failure."""
    from nexus_collection_dl.steam import list_installed_games

    games = list_installed_games(steam_root=tmp_path / "nope")
    assert games == []


# ---------------------------------------------------------------------------
# Non-interactive setup
# ---------------------------------------------------------------------------


def test_setup_non_interactive_writes_the_config(tmp_path, monkeypatch):
    """Flags in, config out, no prompts. The harness path."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    r = CliRunner().invoke(
        cli_module.main,
        [
            "setup",
            "--non-interactive",
            "--collections-root",
            str(tmp_path / "mods"),
        ],
    )
    assert r.exit_code == 0, r.output
    cfg = UserConfig.load()
    assert cfg.is_configured()
    assert cfg.collections_root == str(tmp_path / "mods")


def test_setup_non_interactive_records_games(tmp_path, monkeypatch):
    """Games come in as flags too: domain=path, repeatable."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    r = CliRunner().invoke(
        cli_module.main,
        [
            "setup",
            "--non-interactive",
            "--collections-root",
            str(tmp_path / "mods"),
            "--game",
            f"skyrimspecialedition={tmp_path / 'skyrim'}",
        ],
    )
    assert r.exit_code == 0, r.output
    cfg = UserConfig.load()
    assert cfg.games == [
        {"domain": "skyrimspecialedition", "game_dir": str(tmp_path / "skyrim")}
    ]


def test_setup_non_interactive_without_a_root_fails_loudly(tmp_path, monkeypatch):
    """No prompts means no guesses. If the flag is missing, say so."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    r = CliRunner().invoke(cli_module.main, ["setup", "--non-interactive"])
    assert r.exit_code != 0
    assert "collections" in r.output.lower()
    assert not UserConfig.load().is_configured()


def test_setup_never_prompts_in_non_interactive_mode(tmp_path, monkeypatch):
    """The whole point of the flag. A harness must never be asked anything."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    import click

    real_prompt = click.prompt

    def boom(*a, **k):
        raise AssertionError("prompted in non-interactive mode")

    monkeypatch.setattr(click, "prompt", boom)
    r = CliRunner().invoke(
        cli_module.main,
        [
            "setup",
            "--non-interactive",
            "--collections-root",
            str(tmp_path / "mods"),
        ],
    )
    assert r.exit_code == 0, r.output
    click.prompt = real_prompt


# ---------------------------------------------------------------------------
# Interactive setup
# ---------------------------------------------------------------------------


def test_setup_interactive_asks_and_writes(tmp_path, monkeypatch):
    """Answers in, config out.

    The prompt flow is what a person sees. CliRunner feeds the answers.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(cli_module, "_is_interactive", lambda: True)

    r = CliRunner().invoke(
        cli_module.main,
        ["setup"],
        input=f"{tmp_path / 'mods'}\n\n",
    )
    assert r.exit_code == 0, r.output
    cfg = UserConfig.load()
    assert cfg.collections_root == str(tmp_path / "mods")


def test_setup_interactive_offers_detected_games(tmp_path, monkeypatch):
    """Detection has to reach the user: what is installed gets offered."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(cli_module, "_is_interactive", lambda: True)
    steam = _fake_steam(tmp_path, {"489830": "Skyrim Special Edition"})
    monkeypatch.setenv("NEXUS_DL_STEAM_ROOT", str(steam))

    r = CliRunner().invoke(
        cli_module.main,
        ["setup"],
        input=f"{tmp_path / 'mods'}\ny\n\n",
    )
    assert r.exit_code == 0, r.output
    cfg = UserConfig.load()
    assert cfg.games and cfg.games[0]["domain"] == "skyrimspecialedition"
