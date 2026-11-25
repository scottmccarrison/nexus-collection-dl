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
    Steam detection is pointed at nothing so the machine's real library
    cannot add prompts this test does not expect.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("NEXUS_DL_STEAM_ROOT", str(tmp_path / "no-steam"))
    monkeypatch.setattr(cli_module, "_is_interactive", lambda: True)
    _fake_installer(monkeypatch)

    (tmp_path / "mods").mkdir()
    r = CliRunner().invoke(
        cli_module.main,
        ["setup"],
        input=f"{tmp_path / 'mods'}\nn\n",
    )
    assert r.exit_code == 0, r.output
    cfg = UserConfig.load()
    assert cfg.collections_root == str(tmp_path / "mods")


def test_setup_interactive_offers_detected_games(tmp_path, monkeypatch):
    """Detection has to reach the user: what is installed gets offered."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(cli_module, "_is_interactive", lambda: True)
    _fake_installer(monkeypatch)
    steam = _fake_steam(tmp_path, {"489830": "Skyrim Special Edition"})
    monkeypatch.setenv("NEXUS_DL_STEAM_ROOT", str(steam))

    (tmp_path / "mods").mkdir()
    r = CliRunner().invoke(
        cli_module.main,
        ["setup"],
        input=f"{tmp_path / 'mods'}\ny\nn\n",
    )
    assert r.exit_code == 0, r.output
    cfg = UserConfig.load()
    assert cfg.games and cfg.games[0]["domain"] == "skyrimspecialedition"


# ---------------------------------------------------------------------------
# handler install as part of setup
# ---------------------------------------------------------------------------


def _fake_installer(monkeypatch):
    """A DesktopInstaller whose install() records rather than writes."""
    calls: list[str] = []
    import nexus_collection_dl.handler as handler_module

    class FakeInstaller:
        def install(self):
            calls.append("install")
            return "/tmp/fake/nexus-dl-nxm.desktop"

    monkeypatch.setattr(handler_module, "DesktopInstaller", FakeInstaller)
    return calls


def test_setup_non_interactive_does_not_install_the_handler_by_default(
    tmp_path, monkeypatch
):
    """Side-effecting, so it is opt-in. A harness gets what it asked for.

    Writing a desktop entry and flipping a MIME default is not a thing
    to do because a flag was absent. The default is to leave the system
    alone.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    calls = _fake_installer(monkeypatch)

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
    assert not calls, "the handler was installed without being asked for"


def test_setup_non_interactive_installs_the_handler_when_asked(
    tmp_path, monkeypatch
):
    """The opt-in flag is how a harness asks."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    calls = _fake_installer(monkeypatch)

    r = CliRunner().invoke(
        cli_module.main,
        [
            "setup",
            "--non-interactive",
            "--collections-root",
            str(tmp_path / "mods"),
            "--install-handler",
        ],
    )
    assert r.exit_code == 0, r.output
    assert calls == ["install"]


def test_setup_interactive_offers_the_handler(tmp_path, monkeypatch):
    """It has to reach a person: the handler is the 0.4.0 headline feature
    and it was opt-in-by-documentation, which is exactly the gap the
    front door exists to close.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("NEXUS_DL_STEAM_ROOT", str(tmp_path / "no-steam"))
    monkeypatch.setattr(cli_module, "_is_interactive", lambda: True)
    calls = _fake_installer(monkeypatch)
    (tmp_path / "mods").mkdir()

    r = CliRunner().invoke(
        cli_module.main,
        ["setup"],
        input=f"{tmp_path / 'mods'}\ny\n",
    )
    assert r.exit_code == 0, r.output
    assert "handler" in r.output.lower() or "nxm" in r.output.lower()
    assert calls == ["install"]


def test_setup_interactive_can_decline_the_handler(tmp_path, monkeypatch):
    """Declining is a real answer. Nothing gets written."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("NEXUS_DL_STEAM_ROOT", str(tmp_path / "no-steam"))
    monkeypatch.setattr(cli_module, "_is_interactive", lambda: True)
    calls = _fake_installer(monkeypatch)
    (tmp_path / "mods").mkdir()

    r = CliRunner().invoke(
        cli_module.main,
        ["setup"],
        input=f"{tmp_path / 'mods'}\nn\n",
    )
    assert r.exit_code == 0, r.output
    assert not calls


def test_handler_install_is_idempotent_across_setup_runs(tmp_path, monkeypatch):
    """Re-running setup must not fight the user or fail.

    DesktopInstaller().install() rewrites the entry and is documented as
    idempotent. Setup leaning on that means re-running is safe.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    calls = _fake_installer(monkeypatch)

    for _ in range(2):
        r = CliRunner().invoke(
            cli_module.main,
            [
                "setup",
                "--non-interactive",
                "--collections-root",
                str(tmp_path / "mods"),
                "--install-handler",
            ],
        )
        assert r.exit_code == 0, r.output
    assert calls == ["install", "install"]


# ---------------------------------------------------------------------------
# The collections root has to be a real directory
#
# The prompt took any string. Typing /quit wrote /quit into config and
# reported "Configured", and the next run opened the shell against a
# root that is not a directory.
# ---------------------------------------------------------------------------


def test_setup_rejects_a_root_that_is_a_file(tmp_path, monkeypatch):
    """A file is not a collections root. Say so and ask again."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("NEXUS_DL_STEAM_ROOT", str(tmp_path / "no-steam"))
    monkeypatch.setattr(cli_module, "_is_interactive", lambda: True)
    _fake_installer(monkeypatch)

    not_a_dir = tmp_path / "notes.txt"
    not_a_dir.write_text("hello")
    good = tmp_path / "mods"
    good.mkdir()

    r = CliRunner().invoke(
        cli_module.main,
        ["setup"],
        input=f"{not_a_dir}\n{good}\nn\n",
    )
    assert r.exit_code == 0, r.output
    assert "not a directory" in r.output.lower()
    cfg = UserConfig.load()
    assert cfg.collections_root == str(good)


def test_setup_confirms_before_creating_a_missing_root(tmp_path, monkeypatch):
    """A path that does not exist is not silently created.

    This is the /quit case: the answer is shown back and the user gets
    to say no before anything is written.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("NEXUS_DL_STEAM_ROOT", str(tmp_path / "no-steam"))
    monkeypatch.setattr(cli_module, "_is_interactive", lambda: True)
    _fake_installer(monkeypatch)

    good = tmp_path / "mods"
    good.mkdir()

    r = CliRunner().invoke(
        cli_module.main,
        ["setup"],
        input=f"/quit\nn\n{good}\nn\n",
    )
    assert r.exit_code == 0, r.output
    assert "/quit" in r.output
    assert "does not exist" in r.output.lower()
    cfg = UserConfig.load()
    assert cfg.collections_root == str(good)
    assert not Path("/quit").exists(), "the typo must not be created"


def test_setup_creates_the_root_when_the_user_says_so(tmp_path, monkeypatch):
    """Creating is allowed, it just has to be asked for."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("NEXUS_DL_STEAM_ROOT", str(tmp_path / "no-steam"))
    monkeypatch.setattr(cli_module, "_is_interactive", lambda: True)
    _fake_installer(monkeypatch)

    target = tmp_path / "new" / "mods"

    r = CliRunner().invoke(
        cli_module.main,
        ["setup"],
        input=f"{target}\ny\nn\n",
    )
    assert r.exit_code == 0, r.output
    assert target.is_dir()
    cfg = UserConfig.load()
    assert cfg.collections_root == str(target)


def test_setup_non_interactive_rejects_a_root_that_is_a_file(tmp_path, monkeypatch):
    """The harness path validates too: no prompts, but no nonsense."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    not_a_dir = tmp_path / "notes.txt"
    not_a_dir.write_text("hello")

    r = CliRunner().invoke(
        cli_module.main,
        ["setup", "--non-interactive", "--collections-root", str(not_a_dir)],
    )
    assert r.exit_code != 0
    assert "not a directory" in r.output.lower()
    assert not UserConfig.load().is_configured()
