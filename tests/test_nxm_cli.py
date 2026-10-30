"""CliRunner tests for the nxm command and the handler group.

Boundary-level: real click invocation of `nexus-dl`, fake service and
mocked notify/installer. Verifies quiet non-interactive handler mode (no
banner, no update check, no prompts), useful failure codes, and that no
error output ever echoes the raw URI or link credentials. No network, no
live desktop registration, no real notify-send.
"""

from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus_collection_dl import cli as cli_module
from nexus_collection_dl import updater as updater_module
from nexus_collection_dl.api import NexusAPIError
from nexus_collection_dl.handler import DesktopEntryError
from nexus_collection_dl.nxm import parse_nxm_url
from nexus_collection_dl.service import NxmResult
from nexus_collection_dl.state import CollectionState, StateBusyError

GAME = "skyrimspecialedition"
HOSTILE_URI = (
    f"nxm://{GAME}/mods/1/files/2/extra"
    "?key=SUPERSECRETVALUE&expires=1700000001"
)


class FakeService:
    """Stand-in returned by the patched ModManagerService factory."""

    result = None
    error = None
    calls = []

    def __init__(self, api_key=None, force_free=False):
        pass

    def add_nxm(self, uri, mods_dir=None, output_dir=None, no_load_order=False,
                no_extract=False, on_progress=None):
        FakeService.calls.append({
            "uri": uri, "mods_dir": mods_dir, "output_dir": output_dir,
            "no_extract": no_extract, "no_load_order": no_load_order,
        })
        # Run the real strict parser so shape/credential errors surface at
        # the CLI boundary exactly as they do in production (no network).
        parse_nxm_url(uri)
        if FakeService.error is not None:
            raise FakeService.error
        return FakeService.result


class FakeInstaller:
    """Stand-in for DesktopInstaller at the CLI boundary."""

    mode = "ok"
    calls = []

    def __init__(self):
        pass

    def install(self):
        FakeInstaller.calls.append("install")
        if FakeInstaller.mode == "fail":
            raise DesktopEntryError("xdg-mime was not found.")
        return Path("/fake/apps/nexus-dl-nxm.desktop")

    def uninstall(self):
        FakeInstaller.calls.append("uninstall")
        if FakeInstaller.mode == "fail":
            raise DesktopEntryError("Could not restore the previous default.")
        return "NXM handler removed; previous default 'vortex.desktop' restored."

    def status(self):
        FakeInstaller.calls.append("status")
        return {
            "entry_path": Path("/fake/apps/nexus-dl-nxm.desktop"),
            "entry_installed": True,
            "executable": "/fake/venv/bin/nexus-dl",
            "executable_exists": False,
            "current_default": "nexus-dl-nxm.desktop",
            "is_default": True,
            "previous_default": "vortex.desktop",
            "xdg_mime_available": True,
            "mappings": {GAME: ["/fake/collection"]},
        }


@pytest.fixture(autouse=True)
def cli_env(monkeypatch, tmp_path):
    """Isolate every CLI test: no network checks, no real notify, wide console."""
    FakeService.result = None
    FakeService.error = None
    FakeService.calls = []
    FakeInstaller.calls = []
    FakeInstaller.mode = "ok"
    version_checks = []
    updater_checks = []
    notifications = []
    monkeypatch.setattr(cli_module, "check_for_update",
                        lambda: version_checks.append(1) or None)
    monkeypatch.setattr(updater_module, "check_and_prompt_update",
                        lambda console: updater_checks.append(1))
    monkeypatch.setattr(cli_module, "handler_notify",
                        lambda summary, body="": notifications.append((summary, body)))
    monkeypatch.setattr(cli_module, "ModManagerService", FakeService)
    monkeypatch.setattr(cli_module.console, "width", 400)  # no line wrapping
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    return {
        "version_checks": version_checks,
        "updater_checks": updater_checks,
        "notifications": notifications,
        "tmp_path": tmp_path,
    }


def invoke(args):
    return CliRunner().invoke(cli_module.main, ["--api-key", "k"] + args)


def make_collection(tmp_path, game=GAME, name="col"):
    cdir = tmp_path / name
    cdir.mkdir()
    state = CollectionState(cdir)
    state.set_collection_info(
        f"https://next.nexusmods.com/{game}/collections/testcol",
        "Test Collection", 1, game,
    )
    state.save()
    return cdir


# ---------------------------------------------------------------------------
# Quiet, non-interactive handler mode
# ---------------------------------------------------------------------------

def test_nxm_skips_banner_update_check_and_prompt(cli_env):
    result = invoke(["nxm", "not-an-nxm-link", "--no-notify"])
    assert result.exit_code == 1
    assert cli_env["version_checks"] == []      # no GitHub version check
    assert cli_env["updater_checks"] == []      # no interactive updater prompt
    assert "nexus-dl v" not in result.output    # no banner
    assert "Error" in result.output


def test_handler_group_is_quiet_too(cli_env):
    result = invoke(["handler", "status"])
    assert cli_env["version_checks"] == []
    assert cli_env["updater_checks"] == []
    assert "nexus-dl v" not in result.output


def test_regular_commands_still_run_update_checks(cli_env, tmp_path):
    result = invoke(["status", str(tmp_path)])  # no state -> clean failure
    assert result.exit_code == 1
    assert cli_env["version_checks"] == [1]
    assert cli_env["updater_checks"] == [1]


def test_nxm_missing_argument_fails_cleanly(cli_env):
    result = invoke(["nxm"])
    assert result.exit_code == 2                # click usage error
    assert "Usage" in result.output
    assert cli_env["version_checks"] == []      # still quiet


# ---------------------------------------------------------------------------
# Error output never echoes the raw URI or credentials
# ---------------------------------------------------------------------------

def test_malformed_uri_error_never_echoes_secrets(cli_env):
    result = invoke(["nxm", HOSTILE_URI, "--no-notify"])
    assert result.exit_code == 1
    assert "SUPERSECRETVALUE" not in result.output
    assert "1700000001" not in result.output
    assert HOSTILE_URI not in result.output


def test_duplicate_credential_uri_error_never_echoes_secrets(cli_env):
    uri = f"nxm://{GAME}/mods/1/files/2?key=SECRETAAAAAA&key=SECRETBBBBBB&expires=1893456000"
    result = invoke(["nxm", uri, "--no-notify"])
    assert result.exit_code == 1
    assert "SECRETAAAAAA" not in result.output
    assert "SECRETBBBBBB" not in result.output
    assert "duplicate" in result.output


def test_http_failure_message_is_pre_redacted_at_cli(cli_env):
    FakeService.error = NexusAPIError(
        "The Nexus API rejected the request (HTTP 400). [redacted]"
    )
    result = invoke(["nxm", f"nxm://{GAME}/mods/1/files/2", "--no-notify"])
    assert result.exit_code == 1
    assert "HTTP 400" in result.output
    assert "[redacted]" in result.output


# ---------------------------------------------------------------------------
# Outcomes and notifications
# ---------------------------------------------------------------------------

def test_nxm_success_prints_and_notifies(cli_env):
    FakeService.result = NxmResult(
        success=True, status="downloaded", game_domain=GAME, mod_id=1,
        file_id=2, mod_name="Test Mod", filename="TestMod.zip",
        collection_dir=cli_env["tmp_path"] / "col", extracted=True,
    )
    result = invoke(["nxm", f"nxm://{GAME}/mods/1/files/2", "--notify"])
    assert result.exit_code == 0, result.output
    assert "Downloaded 'Test Mod'" in result.output
    assert "nexus-dl deploy" in result.output       # explicit: no auto deploy
    assert cli_env["notifications"] == [
        ("nexus-dl: download complete", "Downloaded Test Mod")
    ]


def test_nxm_notify_default_on_without_terminal(cli_env):
    # CliRunner stdout is not a TTY, mimicking a desktop-launched process
    FakeService.result = NxmResult(
        success=True, status="already_downloaded", game_domain=GAME,
        mod_id=1, file_id=2, mod_name="Test Mod",
    )
    result = invoke(["nxm", f"nxm://{GAME}/mods/1/files/2"])
    assert result.exit_code == 0
    assert "already downloaded" in result.output
    assert cli_env["notifications"] == [
        ("nexus-dl: download complete", "Already downloaded: Test Mod")
    ]


def test_nxm_no_notify_flag_suppresses(cli_env):
    FakeService.result = NxmResult(
        success=True, status="saved_to_output", game_domain=GAME, mod_id=1,
        file_id=2, filename="TestMod.zip",
        output_path=cli_env["tmp_path"] / "out" / "TestMod.zip",
    )
    result = invoke(["nxm", f"nxm://{GAME}/mods/1/files/2", "--no-notify"])
    assert result.exit_code == 0
    assert "Saved" in result.output
    assert cli_env["notifications"] == []


def test_nxm_failure_result_shows_reclick_hint_and_notifies_redacted(cli_env):
    FakeService.result = NxmResult(
        success=False, status="error", game_domain=GAME, mod_id=1, file_id=2,
        error=(
            "This link carries no download credentials; see "
            "https://www.nexusmods.com/example?key=SECRETVALUE1"
        ),
        re_click_required=True,
    )
    result = invoke(["nxm", f"nxm://{GAME}/mods/1/files/2"])
    assert result.exit_code == 1
    assert "Mod manager download" in result.output  # actionable re-click line
    summary, body = cli_env["notifications"][0]
    assert summary == "nexus-dl: download failed"
    assert "SECRETVALUE1" not in body
    assert "[url redacted]" in body               # notify body is sanitized


def test_nxm_busy_lock_surfaces_as_clean_failure(cli_env):
    FakeService.error = StateBusyError(
        "Another nexus-dl operation is already running for /x. Wait for it "
        "to finish and try again."
    )
    result = invoke(["nxm", f"nxm://{GAME}/mods/1/files/2", "--no-notify"])
    assert result.exit_code == 1
    assert "already running" in result.output


def test_nxm_collection_link_prints_sync_guidance(cli_env):
    result = invoke(["nxm", f"nxm://{GAME}/collections/cool-list/revisions/4",
                     "--no-notify"])
    assert result.exit_code == 1
    assert "nexus-dl sync" in result.output
    assert (
        "https://next.nexusmods.com/skyrimspecialedition/collections/cool-list"
        in result.output
    )


def test_nxm_rejects_mods_dir_and_output_together(cli_env):
    tmp_path = cli_env["tmp_path"]
    mods = tmp_path / "mods"
    out = tmp_path / "out"
    mods.mkdir()
    out.mkdir()
    result = invoke(["nxm", f"nxm://{GAME}/mods/1/files/2",
                     "--mods-dir", str(mods), "--output", str(out), "--no-notify"])
    assert result.exit_code == 1
    assert "cannot be combined" in result.output
    assert FakeService.calls == []                # refused before the service


def test_nxm_passes_options_through_to_service(cli_env):
    tmp_path = cli_env["tmp_path"]
    mods = make_collection(tmp_path)
    FakeService.result = NxmResult(
        success=True, status="downloaded", game_domain=GAME, mod_id=1,
        file_id=2, mod_name="Test Mod", collection_dir=mods,
    )
    result = invoke(["nxm", f"nxm://{GAME}/mods/1/files/2",
                     "--mods-dir", str(mods), "--no-extract",
                     "--no-load-order", "--no-notify"])
    assert result.exit_code == 0, result.output
    call = FakeService.calls[0]
    assert call["mods_dir"] == mods
    assert call["no_extract"] is True
    assert call["no_load_order"] is True


# ---------------------------------------------------------------------------
# Handler group commands
# ---------------------------------------------------------------------------

def test_handler_map_requires_synced_collection(cli_env):
    bare = cli_env["tmp_path"] / "bare"
    bare.mkdir()
    result = invoke(["handler", "map", GAME, str(bare)])
    assert result.exit_code == 1
    assert "sync" in result.output
    assert not (cli_env["tmp_path"] / "xdg-config").exists()  # nothing written


def test_handler_map_status_unmap_roundtrip(cli_env):
    tmp_path = cli_env["tmp_path"]
    cdir = make_collection(tmp_path)

    result = invoke(["handler", "map", GAME, str(cdir)])
    assert result.exit_code == 0, result.output
    assert "Mapped" in result.output
    config_file = tmp_path / "xdg-config" / "nexus-dl" / "handler.json"
    assert config_file.exists()
    assert str(cdir) in config_file.read_text()

    result = invoke(["handler", "status"])
    assert result.exit_code == 0
    assert GAME in result.output
    assert str(cdir) in result.output
    assert "not installed" in result.output

    result = invoke(["handler", "unmap", GAME])
    assert result.exit_code == 0
    assert "Removed mapping" in result.output

    result = invoke(["handler", "unmap", GAME])
    assert result.exit_code == 0
    assert "No mapping exists" in result.output


def test_handler_map_rejects_wrong_game_collection(cli_env):
    cdir = make_collection(cli_env["tmp_path"], game="starfield")
    result = invoke(["handler", "map", GAME, str(cdir)])
    assert result.exit_code == 1
    assert "starfield" in result.output


def test_handler_install_and_uninstall_cli(cli_env, monkeypatch):
    monkeypatch.setattr(cli_module, "DesktopInstaller", FakeInstaller)

    result = invoke(["handler", "install"])
    assert result.exit_code == 0, result.output
    assert "NXM handler installed" in result.output
    assert FakeInstaller.calls == ["install"]

    result = invoke(["handler", "uninstall"])
    assert result.exit_code == 0
    assert "restored" in result.output
    assert FakeInstaller.calls == ["install", "uninstall"]


def test_handler_install_failure_exits_nonzero(cli_env, monkeypatch):
    FakeInstaller.mode = "fail"
    monkeypatch.setattr(cli_module, "DesktopInstaller", FakeInstaller)

    result = invoke(["handler", "install"])
    assert result.exit_code == 1
    assert "xdg-mime was not found" in result.output

    result = invoke(["handler", "uninstall"])
    assert result.exit_code == 1
    assert "Could not restore" in result.output


def test_handler_status_shows_diagnostics(cli_env, monkeypatch):
    monkeypatch.setattr(cli_module, "DesktopInstaller", FakeInstaller)
    result = invoke(["handler", "status"])
    assert result.exit_code == 0
    assert "installed" in result.output
    assert "nexus-dl-nxm.desktop" in result.output
    # Moved-virtualenv diagnostic: recorded executable no longer exists
    assert "no longer exists" in result.output
    assert "handler install" in result.output
    assert "vortex.desktop" in result.output      # previous default shown
