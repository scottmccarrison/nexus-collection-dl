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
    keys = []  # api_key each instance was constructed with (never printed)

    def __init__(self, api_key=None, force_free=False):
        FakeService.keys.append(api_key)

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
    FakeService.keys = []
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
    # Pin the state dir too: nxm runs append their status log there, and
    # tests must never write into the host's ~/.local/state.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    # Key resolution must be test-controlled: a NEXUS_API_KEY exported on
    # the host (e.g. from .bashrc) must never leak into CLI tests.
    monkeypatch.delenv("NEXUS_API_KEY", raising=False)
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


# ---------------------------------------------------------------------------
# Bugcheck regressions: host isolation, warnings, timeout surfacing
# ---------------------------------------------------------------------------

import subprocess as _sp  # noqa: E402
from types import SimpleNamespace  # noqa: E402


@pytest.fixture(autouse=True)
def no_host_subprocesses(monkeypatch):
    """Never spawn real xdg/notify tools from CLI tests, even read-only."""
    from nexus_collection_dl import handler as _handler_module

    def refuse(argv):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(_handler_module, "_default_runner", refuse)


def test_nxm_success_prints_warnings_and_still_succeeds(cli_env):
    FakeService.result = NxmResult(
        success=True, status="downloaded", game_domain=GAME, mod_id=1,
        file_id=2, mod_name="Test Mod",
        collection_dir=cli_env["tmp_path"] / "col",
        warnings=["Load order regeneration failed: boom. Run "
                  "'nexus-dl load-order /x' to retry."],
    )
    result = invoke(["nxm", f"nxm://{GAME}/mods/1/files/2", "--no-notify"])
    assert result.exit_code == 0, result.output
    assert "Warning:" in result.output
    assert "nexus-dl load-order" in result.output


def test_handler_install_timeout_is_clean_cli_failure(cli_env, monkeypatch):
    from nexus_collection_dl.handler import DesktopInstaller as RealInstaller
    from nexus_collection_dl.handler import HandlerConfig

    tmp_path = cli_env["tmp_path"]
    cfg_file = tmp_path / "xdg-config" / "nexus-dl" / "handler.json"
    exe = tmp_path / "nexus-dl-exe"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)

    class TimeoutRunner:
        def __call__(self, argv):
            if argv[:2] == ["xdg-mime", "query"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if argv[:2] == ["xdg-mime", "default"]:
                raise _sp.TimeoutExpired(cmd=argv[0], timeout=30)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    def factory():
        return RealInstaller(config=HandlerConfig(cfg_file),
                             runner=TimeoutRunner(),
                             entry_dir=tmp_path / "apps", executable=exe)

    monkeypatch.setattr(cli_module, "DesktopInstaller", factory)
    result = invoke(["handler", "install"])
    assert result.exit_code == 1
    assert "timed out" in result.output
    assert "Traceback" not in result.output
    assert not (tmp_path / "apps" / "nexus-dl-nxm.desktop").exists()
    assert not cfg_file.exists()


# ---------------------------------------------------------------------------
# Stored API key: hidden-input set/remove, status source, desktop-launch
# resolution, and CLI precedence (flag > env > stored)
# ---------------------------------------------------------------------------

from nexus_collection_dl.keystore import remove_api_key, store_api_key  # noqa: E402

STORED_SECRET = "StoredSentinel-abc123_XYZ"


def invoke_bare(args, **kwargs):
    """Invoke without --api-key: key resolution is up to env/stored."""
    return CliRunner().invoke(cli_module.main, args, **kwargs)


def stored_key_file(cli_env):
    return cli_env["tmp_path"] / "xdg-config" / "nexus-dl" / "api-key"


def test_handler_set_api_key_stores_hidden_input(cli_env):
    result = invoke_bare(["handler", "set-api-key"], input=STORED_SECRET + "\n")
    assert result.exit_code == 0, result.output
    key_file = stored_key_file(cli_env)
    assert key_file.exists()
    assert key_file.read_text() == STORED_SECRET + "\n"
    assert key_file.stat().st_mode & 0o777 == 0o600
    # The value is never echoed, but the file is reported by path.
    assert STORED_SECRET not in result.output
    assert str(key_file) in result.output
    assert "0600" in result.output


def test_handler_set_api_key_rejects_implausible_input(cli_env):
    result = invoke_bare(["handler", "set-api-key"], input="abc12\n")
    assert result.exit_code == 1
    assert "too short" in result.output
    assert "abc12" not in result.output          # rejected value never echoed
    assert not stored_key_file(cli_env).exists()  # nothing written


def test_handler_set_api_key_rejects_whitespace_input(cli_env):
    result = invoke_bare(["handler", "set-api-key"], input="Spaced Value-123\n")
    assert result.exit_code == 1
    assert "Spaced Value" not in result.output
    assert not stored_key_file(cli_env).exists()


def test_handler_remove_api_key_roundtrip(cli_env):
    store_api_key(STORED_SECRET)
    result = invoke_bare(["handler", "remove-api-key"])
    assert result.exit_code == 0
    assert "Removed the stored API key" in result.output
    assert STORED_SECRET not in result.output
    assert not stored_key_file(cli_env).exists()

    result = invoke_bare(["handler", "remove-api-key"])
    assert result.exit_code == 0
    assert "No stored API key" in result.output


def test_handler_remove_api_key_preserves_mappings(cli_env):
    cdir = make_collection(cli_env["tmp_path"])
    result = invoke(["handler", "map", GAME, str(cdir)])
    assert result.exit_code == 0, result.output
    store_api_key(STORED_SECRET)

    result = invoke_bare(["handler", "remove-api-key"])
    assert result.exit_code == 0
    cfg = cli_env["tmp_path"] / "xdg-config" / "nexus-dl" / "handler.json"
    assert cfg.exists()                          # whole config NOT deleted
    assert GAME in cfg.read_text()               # mapping survived


def test_handler_status_reports_key_source_without_values(cli_env, monkeypatch):
    runner = CliRunner()

    # Explicit flag
    result = runner.invoke(
        cli_module.main,
        ["--api-key", "FlagSentinel-abc123", "handler", "status"],
    )
    assert result.exit_code == 0, result.output
    assert "API key" in result.output
    assert "--api-key flag" in result.output
    assert "FlagSentinel-abc123" not in result.output

    # Environment only
    monkeypatch.setenv("NEXUS_API_KEY", "EnvSentinel-abc123")
    result = runner.invoke(cli_module.main, ["handler", "status"])
    assert result.exit_code == 0
    assert "NEXUS_API_KEY" in result.output
    assert "EnvSentinel-abc123" not in result.output
    monkeypatch.delenv("NEXUS_API_KEY")

    # Stored key only
    store_api_key(STORED_SECRET)
    result = runner.invoke(cli_module.main, ["handler", "status"])
    assert result.exit_code == 0
    assert "stored" in result.output
    assert str(stored_key_file(cli_env)) in result.output  # path by name
    assert STORED_SECRET not in result.output              # value never shown

    # None
    remove_api_key()
    result = runner.invoke(cli_module.main, ["handler", "status"])
    assert result.exit_code == 0
    assert "not set" in result.output
    assert "set-api-key" in result.output


def test_nxm_uses_stored_key_with_bare_desktop_env(cli_env):
    """Browser-launched simulation: no flag, no NEXUS_API_KEY (fixture
    removes it), stored key present. The stored key must reach the
    service, and nothing may echo it."""
    store_api_key(STORED_SECRET)
    FakeService.result = NxmResult(
        success=True, status="already_downloaded", game_domain=GAME,
        mod_id=1, file_id=2, mod_name="Test Mod",
    )
    result = invoke_bare(["nxm", f"nxm://{GAME}/mods/1/files/2", "--no-notify"])
    assert result.exit_code == 0, result.output
    assert FakeService.keys == [STORED_SECRET]
    assert STORED_SECRET not in result.output


def test_cli_key_precedence_flag_env_stored(cli_env, monkeypatch):
    store_api_key(STORED_SECRET)
    monkeypatch.setenv("NEXUS_API_KEY", "EnvSentinel-abc123")
    FakeService.result = NxmResult(
        success=True, status="already_downloaded", game_domain=GAME,
        mod_id=1, file_id=2, mod_name="Test Mod",
    )
    uri = f"nxm://{GAME}/mods/1/files/2"
    runner = CliRunner()

    runner.invoke(cli_module.main,
                  ["--api-key", "FlagSentinel-abc123", "nxm", uri, "--no-notify"])
    assert FakeService.keys[-1] == "FlagSentinel-abc123"

    runner.invoke(cli_module.main, ["nxm", uri, "--no-notify"])
    assert FakeService.keys[-1] == "EnvSentinel-abc123"

    monkeypatch.delenv("NEXUS_API_KEY")
    runner.invoke(cli_module.main, ["nxm", uri, "--no-notify"])
    assert FakeService.keys[-1] == STORED_SECRET

    # No key anywhere: unchanged keyless behavior (service gets None).
    remove_api_key()
    runner.invoke(cli_module.main, ["nxm", uri, "--no-notify"])
    assert FakeService.keys[-1] is None


def test_nxm_non_nxm_input_explains_link_type(cli_env):
    url = (
        "https://www.nexusmods.com/skyrimspecialedition/mods/123"
        "?tab=files&queryToken=SECRETQUERY"
    )
    result = invoke(["nxm", url])  # default notify: stdout is not a TTY
    assert result.exit_code == 1
    assert "not an nxm:// link" in result.output
    assert "not usable" in result.output
    assert "Mod manager download" in result.output   # how to get the link
    assert "copy link address" in result.output      # right-click copy
    assert "handler install" in result.output        # click-through setup
    # Never echoes the raw URI, its host, or its query.
    assert url not in result.output
    assert "nexusmods.com" not in result.output
    assert "SECRETQUERY" not in result.output
    assert "queryToken" not in result.output
    # The desktop notification carries the safe first line only.
    summary, body = cli_env["notifications"][0]
    assert summary == "nexus-dl: download failed"
    assert "nexusmods.com" not in body
    assert "SECRETQUERY" not in body


def test_nxm_non_nxm_scheme_error_for_other_schemes(cli_env):
    result = invoke(["nxm", "ftp://example.invalid/mod.zip", "--no-notify"])
    assert result.exit_code == 1
    assert "ftp:// link" in result.output
    assert "example.invalid" not in result.output
    assert "mod.zip" not in result.output


# ---------------------------------------------------------------------------
# nxm status log: one redacted line per run, best-effort, 0600
# ---------------------------------------------------------------------------

LOG_KEY = "LogSentinelKey-abc123_XYZ"
LOG_URI = f"nxm://{GAME}/mods/123/files/456?key={LOG_KEY}&expires=1893456000"


def nxm_log_file(cli_env):
    return cli_env["tmp_path"] / "xdg-state" / "nexus-dl" / "nxm.log"


def assert_log_is_redacted(content):
    assert LOG_KEY not in content
    assert "key=" not in content
    assert "http" not in content.lower()
    assert "nxm://" not in content
    assert GAME not in content


def test_nxm_success_appends_redacted_log_line(cli_env):
    FakeService.result = NxmResult(
        success=True, status="downloaded", game_domain=GAME, mod_id=123,
        file_id=456, mod_name="Test Mod",
        collection_dir=cli_env["tmp_path"] / "col", extracted=True,
    )
    result = invoke(["nxm", LOG_URI, "--no-notify"])
    assert result.exit_code == 0, result.output
    log_file = nxm_log_file(cli_env)
    assert log_file.exists()
    assert log_file.stat().st_mode & 0o777 == 0o600
    content = log_file.read_text()
    lines = content.splitlines()
    assert len(lines) == 1                     # exactly one line per run
    assert " success " in lines[0]
    assert "mod=123" in lines[0]
    assert "file=456" in lines[0]
    assert "status=downloaded" in lines[0]
    assert lines[0].endswith("Z") or "Z " in lines[0]  # UTC timestamp present
    assert_log_is_redacted(content)


def test_nxm_api_failure_appends_redacted_log_line(cli_env):
    FakeService.error = NexusAPIError(
        "The Nexus API rejected the API key (HTTP 401). Check NEXUS_API_KEY."
    )
    result = invoke(["nxm", LOG_URI, "--no-notify"])
    assert result.exit_code == 1
    content = nxm_log_file(cli_env).read_text()
    assert " error " in content
    assert "reason=api" in content
    assert_log_is_redacted(content)


def test_nxm_parse_failure_logs_static_reason(cli_env):
    result = invoke(["nxm", f"nxm://{GAME}/mods/1/files/2/extra?key={LOG_KEY}"
                     "&expires=1893456000", "--no-notify"])
    assert result.exit_code == 1
    content = nxm_log_file(cli_env).read_text()
    assert " error " in content
    assert "reason=parse" in content
    assert_log_is_redacted(content)


def test_nxm_result_failure_logs_reason_and_ids(cli_env):
    FakeService.result = NxmResult(
        success=False, status="error", game_domain=GAME, mod_id=7,
        file_id=8, error="This download link has expired.",
        re_click_required=True,
    )
    result = invoke(["nxm", f"nxm://{GAME}/mods/7/files/8", "--no-notify"])
    assert result.exit_code == 1
    content = nxm_log_file(cli_env).read_text()
    assert " error " in content
    assert "reason=re_click_required" in content
    assert "mod=7" in content and "file=8" in content
    # Free-form error text never reaches the log.
    assert "expired" not in content


def test_nxm_warnings_log_warn_outcome(cli_env):
    FakeService.result = NxmResult(
        success=True, status="downloaded", game_domain=GAME, mod_id=1,
        file_id=2, mod_name="Test Mod",
        collection_dir=cli_env["tmp_path"] / "col",
        warnings=["Load order regeneration failed: boom."],
    )
    result = invoke(["nxm", f"nxm://{GAME}/mods/1/files/2", "--no-notify"])
    assert result.exit_code == 0, result.output
    content = nxm_log_file(cli_env).read_text()
    assert " warn " in content
    assert "status=downloaded" in content


def test_nxm_log_failure_never_fails_the_command(cli_env, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("log exploded")

    monkeypatch.setattr(cli_module, "append_nxm_log", boom)
    FakeService.result = NxmResult(
        success=True, status="already_downloaded", game_domain=GAME,
        mod_id=1, file_id=2, mod_name="Test Mod",
    )
    result = invoke(["nxm", f"nxm://{GAME}/mods/1/files/2", "--no-notify"])
    assert result.exit_code == 0, result.output
    assert "already downloaded" in result.output
