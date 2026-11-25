"""Tests for the interactive `nexus-dl shell` REPL.

Boundary-level where it matters (CliRunner through `nexus-dl shell`),
service-level where fidelity matters (the full-session test runs the
REAL service over tmp state so the tombstone and the
payload-untouched guarantees are checked end to end). No network, no
host subprocesses, no real desktop registration. Every fixture secret
is fake and asserted absent from all output.
"""

import io
import json
import re
from pathlib import Path
from typing import ClassVar

import pytest
from click.testing import CliRunner
from rich.console import Console

from nexus_collection_dl import __version__
from nexus_collection_dl import cli as cli_module
from nexus_collection_dl import service as service_module
from nexus_collection_dl import updater as updater_module
from nexus_collection_dl.service import (
    AddResult,
    DeployResult,
    HealthProbes,
    ModManagerService,
    NxmResult,
    PendingDownload,
    SyncResult,
)
from nexus_collection_dl.shell import (
    _HELP_BY_NAME,
    _MAX_INPUT_FAILURES,
    _ShellSession,
    run_shell,
)
from nexus_collection_dl.state import (
    STATE_FILENAME,
    CollectionState,
    StateBusyError,
)

GAME = "skyrimspecialedition"
COLLECTION_URL = f"https://next.nexusmods.com/{GAME}/collections/testcol"
HOSTILE_URI = (
    f"nxm://{GAME}/mods/1/files/2/extra"
    "?key=SUPERSECRETVALUE&expires=1700000001"
)


# ---------------------------------------------------------------------------
# fixtures and helpers
# ---------------------------------------------------------------------------

class FakeShellService:
    """Configurable ModManagerService double recording every call."""

    calls: ClassVar[list] = []
    # Per-method canned results (None means "leave the default").
    add_result = None
    nxm_result = None
    sync_result = None
    remove_result = None
    status_result = None
    # Per-method exceptions to raise instead of returning.
    add_error = None
    nxm_error = None
    sync_error = None
    remove_error = None
    status_error = None
    deploy_error = None
    install_error = None
    # Canned deploy result.
    deploy_result = None
    install_result = None

    def __init__(self, api_key=None, force_free=False):
        self._api_key = api_key

    def add_mod(self, mod_url, mods_dir, file_id=None, no_load_order=False,
                no_extract=False, on_progress=None):
        FakeShellService.calls.append(
            {"method": "add_mod", "mod_url": mod_url, "mods_dir": mods_dir}
        )
        if FakeShellService.add_error is not None:
            raise FakeShellService.add_error
        return FakeShellService.add_result or AddResult(
            mod_name="Test Mod", mod_id=1, file_name="TestMod.zip",
            success=True,
        )

    def add_nxm(self, uri, mods_dir=None, output_dir=None, no_load_order=False,
                no_extract=False, on_progress=None):
        FakeShellService.calls.append(
            {"method": "add_nxm", "uri": uri, "mods_dir": mods_dir}
        )
        if FakeShellService.nxm_error is not None:
            raise FakeShellService.nxm_error
        return FakeShellService.nxm_result or NxmResult(
            success=True, status="downloaded", game_domain=GAME,
            mod_id=1, file_id=2, mod_name="Test Mod",
        )

    def sync(self, collection_url, mods_dir, **kwargs):
        FakeShellService.calls.append(
            {"method": "sync", "collection_url": collection_url,
             "mods_dir": mods_dir,
             "target_dir": kwargs.get("target_dir")}
        )
        if FakeShellService.sync_error is not None:
            raise FakeShellService.sync_error
        return FakeShellService.sync_result or SyncResult(
            mods_downloaded=1, mods_extracted=1, errors=[],
            load_order_files=[],
        )

    def remove_mod(self, mods_dir, *, file_id=None, mod_id=None,
                   no_load_order=False):
        FakeShellService.calls.append(
            {"method": "remove_mod", "mods_dir": mods_dir,
             "file_id": file_id, "mod_id": mod_id}
        )
        if FakeShellService.remove_error is not None:
            raise FakeShellService.remove_error
        return FakeShellService.remove_result

    def install_fomod_picks(self, mods_dir, file_id, picks=None,
                            on_fomod_prompt=None):
        FakeShellService.calls.append(
            {"method": "install_fomod_picks", "mods_dir": mods_dir,
             "file_id": file_id, "picks": picks,
             "on_fomod_prompt": on_fomod_prompt}
        )
        if FakeShellService.install_error is not None:
            raise FakeShellService.install_error
        return FakeShellService.install_result or "Test Mod installed with 1 option choice(s)"

    def deploy(self, mods_dir, game_dir=None, prefix=None, use_copy=False,
               on_progress=None):
        FakeShellService.calls.append(
            {"method": "deploy", "mods_dir": mods_dir,
             "game_dir": game_dir, "use_copy": use_copy}
        )
        if FakeShellService.deploy_error is not None:
            raise FakeShellService.deploy_error
        return FakeShellService.deploy_result or DeployResult(
            deployed_count=3, conflicts=[], errors=[],
            game_dir=str(game_dir or ""), has_sfse=False,
        )

    def get_status(self, mods_dir):
        FakeShellService.calls.append(
            {"method": "get_status", "mods_dir": mods_dir}
        )
        if FakeShellService.status_error is not None:
            raise FakeShellService.status_error
        return FakeShellService.status_result


@pytest.fixture(autouse=True)
def shell_env(monkeypatch, tmp_path):
    """cli_env-style isolation: no network checks, no real notify, pinned
    HOME/XDG so no host state can leak in, wide console for stable
    assertions."""
    FakeShellService.calls = []
    FakeShellService.add_result = None
    FakeShellService.nxm_result = None
    FakeShellService.sync_result = None
    FakeShellService.remove_result = None
    FakeShellService.status_result = None
    FakeShellService.add_error = None
    FakeShellService.nxm_error = None
    FakeShellService.sync_error = None
    FakeShellService.remove_error = None
    FakeShellService.status_error = None
    FakeShellService.deploy_error = None
    FakeShellService.deploy_result = None
    FakeShellService.install_error = None
    FakeShellService.install_result = None

    version_checks = []
    updater_checks = []
    notifications = []
    monkeypatch.setattr(cli_module, "check_for_update",
                        lambda: version_checks.append(1) or None)
    monkeypatch.setattr(updater_module, "check_and_prompt_update",
                        lambda console: updater_checks.append(1))
    monkeypatch.setattr(cli_module, "handler_notify",
                        lambda summary, body="": notifications.append((summary, body)))
    monkeypatch.setattr(cli_module, "notification_daemon_available",
                        lambda: True)
    monkeypatch.setattr(cli_module, "capability_warnings", list)
    monkeypatch.setattr(cli_module.console, "width", 400)  # no line wrapping
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # A machine with a real Steam library must not decide where these
    # tests deploy; root resolution would silently agree with it.
    monkeypatch.setenv("NEXUS_DL_STEAM_ROOT", str(tmp_path / "no-steam"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.delenv("NEXUS_API_KEY", raising=False)
    return {
        "version_checks": version_checks,
        "updater_checks": updater_checks,
        "notifications": notifications,
        "tmp_path": tmp_path,
    }


@pytest.fixture(autouse=True)
def no_host_subprocesses(monkeypatch):
    """Refuse any handler subprocess on the host session."""
    from nexus_collection_dl import handler as _handler_module

    def refuse(*args, **kwargs):
        raise AssertionError(f"host subprocess attempted: {args}")

    monkeypatch.setattr(_handler_module, "_default_runner", refuse)
    monkeypatch.setattr(_handler_module, "_notify_runner", refuse)


def mod_entry(file_id=456, mod_id=123, name="Test Mod", filename=None,
              download_status="downloaded"):
    return {
        "mod_id": mod_id,
        "mod_name": name,
        "file_id": file_id,
        "filename": filename or f"{name.replace(' ', '')}-{file_id}.zip",
        "version": "1.0",
        "optional": False,
        "manual": False,
        "download_status": download_status,
    }


def write_collection(cdir: Path, name="Test Collection", mods=(), url=COLLECTION_URL):
    """Create a synced collection state file under cdir.

    ``url`` is the collection_url recorded in the state; pass "" to
    simulate a state with no stored URL (hand-created or very old).
    """
    cdir.mkdir(parents=True, exist_ok=True)
    state = CollectionState(cdir)
    state.set_collection_info(url, name, 1, GAME)
    for m in mods:
        state.add_mod(m)
    state.save()
    return cdir


def make_root(tmp_path: Path, *collections) -> Path:
    """Root dir holding one real collection dir per entry.

    Entries are (dirname, display name, mods) tuples; a 2-tuple
    (name, mods) uses the name as both the directory and display name.
    """
    root = tmp_path / "root"
    for entry in collections:
        if len(entry) == 2:
            dirname, mods = entry
            name = dirname
        else:
            dirname, name, mods = entry
        write_collection(root / dirname, name=name, mods=mods)
    return root


def run_cli(root: Path, input_text: str, monkeypatch=None,
            service_cls=None) -> "object":
    """Invoke `nexus-dl shell ROOT` with a scripted session."""
    if service_cls is not None:
        monkeypatch.setattr(cli_module, "ModManagerService", service_cls)
    runner = CliRunner()
    return runner.invoke(cli_module.main, ["shell", str(root)], input=input_text)


def run_direct(svc, root: Path, script):
    """Call run_shell with an injected input_fn for interrupt scripting.

    ``script`` items are input lines, exception instances to raise, or
    callables run as side effects before the next line is served (for
    mutating state between commands). Returns (exit_code,
    captured_output).
    """
    buf = io.StringIO()
    console = Console(file=buf, width=200, force_terminal=False)
    steps = iter(script)

    def input_fn(prompt):
        while True:
            try:
                item = next(steps)
            except StopIteration:
                # Script exhausted behaves like end of input, same as
                # the real input() on a closed stream.
                raise EOFError from None
            if isinstance(item, BaseException):
                raise item
            if callable(item):
                # Side effect between commands (e.g. corrupt a state
                # file so the next command hits a real error path).
                item()
                continue
            return item

    code = run_shell(svc, root, input_fn=input_fn, console=console)
    return code, buf.getvalue()


def reload_state(cdir: Path) -> CollectionState:
    state = CollectionState(cdir)
    state.load()
    return state


# ---------------------------------------------------------------------------
# (a) full session over the REAL service
# ---------------------------------------------------------------------------

def test_full_session_collections_use_mods_remove_confirm_yes(shell_env, tmp_path):
    """/collections -> /use -> /mods -> /remove (yes): state updated, the
    removal is tombstoned at the service layer, and what the mod added
    goes with it. Gone means gone."""
    root = make_root(tmp_path, ("Test Collection", [
        mod_entry(file_id=456, mod_id=123, name="Test Mod"),
        mod_entry(file_id=789, mod_id=321, name="Other Mod"),
    ]))
    cdir = root / "Test Collection"
    archive = cdir / "TestMod-456.zip"
    archive.write_bytes(b"PK payload bytes")
    extracted = cdir / "TestMod" / "file.esp"
    extracted.parent.mkdir()
    extracted.write_text("esp content")
    state = reload_state(cdir)
    state.mods[456].contributed_files = ["TestMod/file.esp"]
    state.save()

    result = run_cli(root, "/collections\n/use 1\n/mods\n/remove 456\nyes\n/quit\n")
    assert result.exit_code == 0
    out = result.output

    # The confirmation shows name + id and says what will happen to disk.
    assert "Test Mod" in out
    assert "file_id 456" in out
    assert "mod_id 123" in out
    assert "from disk" in out

    # State updated: the mod is gone, its sibling stays.
    state = reload_state(cdir)
    assert 456 not in state.mods
    assert 789 in state.mods
    # Tombstone written at the service layer.
    assert state.is_removed(123)
    assert not state.is_removed(321)

    # Gone means gone: the files the mod added go with it.
    assert not archive.exists()
    assert not extracted.exists()


# ---------------------------------------------------------------------------
# (b) /remove aborts: "no" and Ctrl-C at the confirm prompt
# ---------------------------------------------------------------------------

def test_remove_confirm_no_makes_no_change(shell_env, tmp_path):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    cdir = root / "Test Collection"

    result = run_cli(root, "/use 1\n/remove 456\nno\n/quit\n")
    assert result.exit_code == 0
    assert "Cancelled; nothing was removed." in result.output

    state = reload_state(cdir)
    assert 456 in state.mods
    assert not state.is_removed(123)


def test_remove_ctrl_c_at_confirm_makes_no_change(shell_env, tmp_path):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    cdir = root / "Test Collection"
    svc = ModManagerService(api_key="unused-key")

    code, out = run_direct(svc, root, [
        "/use 1",
        "/remove 456",
        KeyboardInterrupt(),  # Ctrl-C at the confirm prompt
        "/quit",
    ])
    assert code == 0
    assert "Cancelled; nothing was removed." in out

    state = reload_state(cdir)
    assert 456 in state.mods
    assert not state.is_removed(123)


# ---------------------------------------------------------------------------
# prompt and input-stream resilience: the loop never dies
# ---------------------------------------------------------------------------

def test_ctrl_c_at_main_prompt_cancels_line_and_continues(
    shell_env, tmp_path
):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    svc = ModManagerService(api_key="unused-key")

    code, out = run_direct(svc, root, [
        KeyboardInterrupt(),  # Ctrl-C at the main prompt
        "/help",
        "/quit",
    ])
    assert code == 0
    assert "Cancelled." in out
    # The loop kept going: /help ran after the interrupt.
    assert out.index("Cancelled.") < out.index("Shell commands")


def test_non_utf8_stdin_does_not_traceback(shell_env, tmp_path):
    """A decode failure on piped stdin renders as an error and the shell
    exits cleanly instead of escaping run_shell as a traceback."""
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))

    result = run_cli(root, b"\xff\xfe garbage\n")
    assert result.exit_code == 0
    assert result.exception is None
    out = result.output
    assert "UnicodeDecodeError" in out or "codec can't decode" in out
    assert "Traceback" not in out


def test_input_failure_renders_error_and_recovers(shell_env, tmp_path):
    """One bad read is survivable: the failure renders through the
    redacted path and later lines still run."""
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    svc = ModManagerService(api_key="unused-key")

    code, out = run_direct(svc, root, [
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        "/help",
        "/quit",
    ])
    assert code == 0
    assert "codec can't decode" in out
    assert out.index("codec can't decode") < out.index("Shell commands")


def test_repeated_input_failures_exit_cleanly(shell_env, tmp_path):
    """A stream that cannot produce a single line ends the session
    cleanly after a bounded number of attempts, not an endless spin."""
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    svc = ModManagerService(api_key="unused-key")
    buf = io.StringIO()
    console = Console(file=buf, width=200, force_terminal=False)
    reads = []

    def broken_input(prompt):
        reads.append(prompt)
        raise OSError("Input/output error")

    code = run_shell(svc, root, input_fn=broken_input, console=console)
    out = buf.getvalue()
    assert code == 0
    # Bounded attempts, then a clean exit with no "Goodbye" (that
    # belongs to the Ctrl-D/quit paths only).
    assert len(reads) == _MAX_INPUT_FAILURES
    assert "Input/output error" in out
    assert "Input is unusable" in out
    assert "Goodbye." not in out


# ---------------------------------------------------------------------------
# (c) bare nxm:// line routes to add with explicit mods_dir; no echo
# ---------------------------------------------------------------------------

def test_bare_nxm_line_routes_to_add_and_never_echoes_the_link(
    shell_env, tmp_path, monkeypatch
):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    cdir = root / "Test Collection"
    # The result deliberately reflects the link in a warning so the
    # redaction scope is exercised even on the success path.
    FakeShellService.nxm_result = NxmResult(
        success=True, status="downloaded", game_domain=GAME,
        mod_id=1, file_id=2, mod_name="Test Mod",
        warnings=[f"note about {HOSTILE_URI}"],
    )

    result = run_cli(
        root, f"/use 1\n{HOSTILE_URI}\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output

    # Routed to add_nxm with the current collection path explicitly.
    calls = [c for c in FakeShellService.calls if c["method"] == "add_nxm"]
    assert len(calls) == 1
    assert calls[0]["uri"] == HOSTILE_URI
    assert calls[0]["mods_dir"] == cdir

    # Link query values (and the link whole) absent from ALL output.
    assert "SUPERSECRETVALUE" not in out
    assert "1700000001" not in out
    assert HOSTILE_URI not in out
    assert "Downloaded" in out  # the operation itself succeeded


# ---------------------------------------------------------------------------
# (d) add success=False renders as failure, never as success
# ---------------------------------------------------------------------------

def test_add_failure_renders_as_failure_without_success_text(
    shell_env, tmp_path, monkeypatch
):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    FakeShellService.add_result = AddResult(
        mod_name="", mod_id=1, file_name="",
        success=False, error="Network exploded",
    )

    result = run_cli(
        root,
        "/use 1\n/add https://www.nexusmods.com/starfield/mods/1\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert "Network exploded" in out
    assert "Added" not in out
    assert "manual mod" not in out


# ---------------------------------------------------------------------------
# (e) unknown command recovery
# ---------------------------------------------------------------------------

def test_unknown_command_recovers_with_help_hint(shell_env, tmp_path):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))

    result = run_cli(root, "/frobnicate\n/quit\n")
    assert result.exit_code == 0
    assert "try /help" in result.output


def test_unknown_bare_input_recovers_with_help_hint(shell_env, tmp_path):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))

    result = run_cli(root, "hello world\n/quit\n")
    assert result.exit_code == 0
    assert "try /help" in result.output


# ---------------------------------------------------------------------------
# (f) busy-lock error keeps the loop alive
# ---------------------------------------------------------------------------

def test_busy_lock_error_keeps_loop_alive(shell_env, tmp_path, monkeypatch):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    FakeShellService.status_error = StateBusyError(
        "Another nexus-dl operation is already running for "
        f"{root / 'Test Collection'}. Wait for it to finish and try again."
    )

    result = run_cli(
        root, "/use 1\n/status 1\n/help\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert "Another nexus-dl operation is already running" in out
    # The loop kept going: /help ran after the failure.
    assert out.index("Another nexus-dl operation") < out.index("Shell commands")


# ---------------------------------------------------------------------------
# (g) Ctrl-D exits 0
# ---------------------------------------------------------------------------

def test_ctrl_d_exits_zero_with_goodbye(shell_env, tmp_path):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))

    result = run_cli(root, "/collections\n")
    assert result.exit_code == 0
    assert "Goodbye." in result.output

    result = run_cli(root, "")
    assert result.exit_code == 0
    assert "Goodbye." in result.output


# ---------------------------------------------------------------------------
# (h) KeyboardInterrupt during a service call never claims success
# ---------------------------------------------------------------------------

def test_keyboard_interrupt_during_service_call_reports_interruption(
    shell_env, tmp_path, monkeypatch
):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    FakeShellService.status_error = KeyboardInterrupt()

    result = run_cli(
        root, "/use 1\n/status 1\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert (
        "interrupted; the operation may have partially completed; "
        "check /status"
    ) in out
    # Never claim success.
    assert "Collection:" not in out
    assert "Installed revision" not in out


# ---------------------------------------------------------------------------
# (i) shell skips the banner and the update prompt
# ---------------------------------------------------------------------------

def test_shell_skips_banner_and_update_prompt(shell_env, tmp_path):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))

    result = run_cli(root, "/quit\n")
    assert result.exit_code == 0
    assert shell_env["version_checks"] == []
    assert shell_env["updater_checks"] == []
    assert f"nexus-dl v{__version__}" not in result.output


# ---------------------------------------------------------------------------
# (j) /add without /use errors clearly
# ---------------------------------------------------------------------------

def test_add_without_use_errors_clearly(shell_env, tmp_path, monkeypatch):
    # Two collections: startup stays unselected (auto-select fires
    # only when the root holds exactly one).
    root = make_root(
        tmp_path,
        ("Another Collection", []),
        ("Test Collection", [mod_entry()]),
    )

    result = run_cli(
        root,
        "/add https://www.nexusmods.com/starfield/mods/1\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    assert "use /use <index|name> first" in result.output
    # Nothing was sent to the service.
    assert FakeShellService.calls == []


# ---------------------------------------------------------------------------
# (k) a reflected credential in an unexpected exception is redacted
# ---------------------------------------------------------------------------

def test_reflected_credential_in_unexpected_exception_is_redacted(
    shell_env, tmp_path, monkeypatch
):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    FakeShellService.nxm_error = RuntimeError(
        f"boom: leaked {HOSTILE_URI} and bare SUPERSECRETVALUE "
        "and expires 1700000001"
    )

    result = run_cli(
        root, f"/use 1\n{HOSTILE_URI}\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert "SUPERSECRETVALUE" not in out
    assert "1700000001" not in out
    assert HOSTILE_URI not in out
    # The failure itself is still reported.
    assert "Error:" in out
    assert "[redacted]" in out


# ---------------------------------------------------------------------------
# command coverage: collections, games, use, mods, sync, remove, status
# ---------------------------------------------------------------------------

def test_collections_games_mods_tables(shell_env, tmp_path):
    root = make_root(
        tmp_path,
        ("Alpha", [mod_entry(file_id=1, mod_id=10, name="A Mod")]),
        ("Beta", [
            mod_entry(file_id=2, mod_id=20, name="B Mod"),
            mod_entry(file_id=3, mod_id=21, name="B Mod Two",
                      download_status="pending_download"),
        ]),
    )

    result = run_cli(
        root,
        "/collections\n/games\n/use Alpha\n/mods\n/use Beta\n/mods\n/quit\n",
    )
    assert result.exit_code == 0
    out = result.output
    # /collections: index, name, game, mods, pending
    assert "Alpha" in out and "Beta" in out
    assert str(root) in out
    # /games: per-game collection and mod totals
    assert GAME in out
    # /mods rows show file ids and statuses per selected collection
    assert "A Mod" in out
    assert "B Mod" in out
    assert "B Mod Two" in out
    assert "pending_download" in out
    assert "downloaded" in out


def test_use_by_index_name_and_ambiguous_names(shell_env, tmp_path):
    root = make_root(
        tmp_path,
        ("dup-a", "Dup", [mod_entry(file_id=11, mod_id=110, name="D1")]),
        ("dup-b", "Dup", [mod_entry(file_id=12, mod_id=120, name="D2")]),
        ("test-collection", "Test Collection", [mod_entry()]),
    )

    # Case-insensitive unique match resolves.
    result = run_cli(root, "/use test collection\n/quit\n")
    assert result.exit_code == 0
    assert "Using 'Test Collection'" in result.output

    # Duplicate display names fail with indexes listed.
    result = run_cli(root, "/use Dup\n/quit\n")
    assert result.exit_code == 0
    assert "pick one by index" in result.output
    # The listing shows every collection with its index.
    for index in ("1:", "2:", "3:"):
        assert index in result.output

    # An index selects exactly one even when names collide.
    result = run_cli(root, "/use 2\n/quit\n")
    assert result.exit_code == 0
    assert "Using 'Dup'" in result.output

    # Unknown selector lists the indexes.
    result = run_cli(root, "/use Nope\n/quit\n")
    assert result.exit_code == 0
    assert "no collection named 'Nope'" in result.output
    assert "1: " in result.output


def test_use_prefers_exact_case_sensitive_match(shell_env, tmp_path):
    root = make_root(
        tmp_path,
        ("ci", "dup", [mod_entry(file_id=11, mod_id=110)]),
        ("cs", "Dup", [mod_entry(file_id=12, mod_id=120)]),
    )
    result = run_cli(root, "/use Dup\n/quit\n")
    assert result.exit_code == 0
    # The exact-case entry (directory cs) wins over the case-insensitive one.
    assert f"Using 'Dup' ({root / 'cs'})" in result.output


def test_help_shows_every_commands_arguments(shell_env, tmp_path):
    """The /help table must show each command's arguments: rich markup
    must not swallow bracketed arguments like /mods [collection], and
    /sync must show its optional target form."""
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))

    result = run_cli(root, "/help\n/quit\n")
    assert result.exit_code == 0
    out = result.output
    assert "/mods [collection]" in out
    assert "/sync [<index|name|url>]" in out


def test_sync_lists_per_mod_errors_before_success_text(
    shell_env, tmp_path, monkeypatch
):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    FakeShellService.sync_result = SyncResult(
        mods_downloaded=3, mods_extracted=2,
        errors=["mod 5 failed: bad archive"],
        load_order_files=["load-order.txt"],
        collection_dir=root / "Synced Collection",
    )

    url = "https://www.nexusmods.com/skyrimspecialedition/collections/abc"
    result = run_cli(
        root, f"/sync {url}\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert "mod 5 failed: bad archive" in out
    assert "Synced 3 mods" in out
    # Errors render before any success text.
    assert out.index("mod 5 failed") < out.index("Synced 3 mods")
    # The URL form still syncs exactly the URL given, into a new
    # directory under the root (no pinned destination).
    assert FakeShellService.calls == [
        {"method": "sync", "collection_url": url, "mods_dir": root,
         "target_dir": None}
    ]


def test_sync_offers_to_map_the_game_for_nxm_clicks(shell_env, tmp_path, monkeypatch):
    """The shell is the product; a shell sync must offer the routing too.

    `sync` is what creates the collection directory and state that a
    handler mapping validates against, so it is the moment the mapping
    can be made without the user going looking for a command.
    """
    from nexus_collection_dl.handler import HandlerConfig

    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    coll = root / "Synced Collection"
    coll.mkdir()
    (coll / ".nexus-state.json").write_text(json.dumps({
        "collection_url": "u", "collection_name": "Synced Collection",
        "collection_revision": 1,
        "game_domain": GAME,
        "mods": {"11": {"mod_id": 1, "name": "M", "file_id": 11,
                        "filename": "a.zip", "version": "1",
                        "download_status": "downloaded"}},
    }))
    FakeShellService.sync_result = SyncResult(
        mods_downloaded=1, mods_extracted=1, errors=[],
        load_order_files=["load-order.txt"], collection_dir=coll,
    )

    url = "https://next.nexusmods.com/skyrimspecialedition/collections/abc"
    result = run_cli(
        root, f"/sync {url}\nyes\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    assert "nxm://" in result.output, result.output
    assert HandlerConfig.load().mappings.get(GAME) == [str(coll.resolve())], \
        HandlerConfig.load().mappings


# ---------------------------------------------------------------------------
# /sync targets: URL, index/name, and resync of the current collection
# ---------------------------------------------------------------------------

def test_sync_without_target_resyncs_current_from_stored_url(
    shell_env, tmp_path, monkeypatch
):
    """/sync with no argument resyncs the current collection from the
    URL its state recorded, with no trip back to nexusmods.com."""
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))

    result = run_cli(
        root, "/use 1\n/sync\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    # The service received the stored URL, not some re-found one, and
    # the destination is pinned to the collection's own directory.
    assert FakeShellService.calls == [
        {"method": "sync", "collection_url": COLLECTION_URL, "mods_dir": root,
         "target_dir": root / "Test Collection"}
    ]
    assert "Synced 1 mods" in result.output


def test_sync_without_target_and_no_current_is_a_clear_error(
    shell_env, tmp_path, monkeypatch
):
    # Two collections: startup stays unselected (auto-select fires
    # only when the root holds exactly one).
    root = make_root(
        tmp_path,
        ("Another Collection", []),
        ("Test Collection", [mod_entry()]),
    )

    result = run_cli(
        root, "/sync\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert "no collection selected yet" in out
    # The error shows the new optional form.
    assert "usage: /sync [<index|name|url>]" in out
    assert FakeShellService.calls == []


def test_sync_by_index_and_by_name_uses_stored_url(
    shell_env, tmp_path, monkeypatch
):
    # Each collection records its OWN URL: a shared default URL would
    # let wrong-collection resolution pass unnoticed.
    url_clean = f"https://next.nexusmods.com/{GAME}/collections/clean-vis"
    url_test = f"https://next.nexusmods.com/{GAME}/collections/test-col"
    root = tmp_path / "root"
    write_collection(root / "clean-visuals", name="Clean Visuals", url=url_clean)
    write_collection(root / "test-collection", name="Test Collection",
                     mods=[mod_entry()], url=url_test)

    # By index (indexes follow the /collections listing order).
    result = run_cli(
        root, "/sync 2\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    assert FakeShellService.calls == [
        {"method": "sync", "collection_url": url_test, "mods_dir": root,
         "target_dir": root / "test-collection"}
    ]

    # By name: unique case-insensitive match, exactly like /use.
    FakeShellService.calls = []
    result = run_cli(
        root, "/sync test collection\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    assert FakeShellService.calls == [
        {"method": "sync", "collection_url": url_test, "mods_dir": root,
         "target_dir": root / "test-collection"}
    ]


def test_sync_to_a_renamed_collection_updates_its_own_directory(
    shell_env, tmp_path, monkeypatch
):
    """The resolved collection's OWN directory is the destination even
    when its directory name differs from the API's sanitized name."""
    url = f"https://next.nexusmods.com/{GAME}/collections/renamed-col"
    root = tmp_path / "root"
    write_collection(root / "renamed-v1", name="Test Collection",
                     mods=[mod_entry()], url=url)

    result = run_cli(
        root, "/sync 1\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    assert FakeShellService.calls == [
        {"method": "sync", "collection_url": url, "mods_dir": root,
         "target_dir": root / "renamed-v1"}
    ]


def test_sync_by_ambiguous_name_lists_indexes(
    shell_env, tmp_path, monkeypatch
):
    root = make_root(
        tmp_path,
        ("dup-a", "Dup", []),
        ("dup-b", "Dup", []),
    )

    result = run_cli(
        root, "/sync Dup\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert "'Dup' matches 2 collections" in out
    assert "pick one by index" in out
    assert "1: " in out and "2: " in out
    assert FakeShellService.calls == []


def test_sync_resync_without_stored_url_requires_explicit_url(
    shell_env, tmp_path, monkeypatch
):
    """A state with no collection_url fails honestly and points at the
    URL form instead of guessing where the collection came from."""
    root = tmp_path / "root"
    write_collection(root / "no-url", name="No Url", mods=[mod_entry()], url="")

    result = run_cli(
        root, "/use 1\n/sync\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert "no collection URL is stored for 'No Url'" in out
    assert "pass it explicitly" in out
    assert "usage: /sync <collection-url>" in out
    assert FakeShellService.calls == []


def test_remove_by_mod_id_prefix(shell_env, tmp_path, monkeypatch):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    cdir = root / "Test Collection"
    from nexus_collection_dl.service import RemoveModResult
    FakeShellService.remove_result = RemoveModResult(
        removed_file_ids=[456], removed_mod_ids=[123], warnings=[]
    )

    result = run_cli(
        root, "/use 1\n/remove mod:123\nyes\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert "mod_id 123" in out
    assert "Removed 'Test Mod'" in out
    calls = [c for c in FakeShellService.calls if c["method"] == "remove_mod"]
    assert calls == [{"method": "remove_mod", "mods_dir": cdir,
                      "file_id": None, "mod_id": 123}]


def test_remove_empty_ids_result_still_renders(shell_env, tmp_path, monkeypatch):
    """The removal summary renders from the result defensively: an
    empty-ids result must not turn a completed removal into a
    traceback."""
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    from nexus_collection_dl.service import RemoveModResult
    FakeShellService.remove_result = RemoveModResult(
        removed_file_ids=[], removed_mod_ids=[], warnings=[]
    )

    result = run_cli(
        root, "/use 1\n/remove 456\nyes\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert "Removed 'Test Mod'" in out
    assert "list index out of range" not in out


def test_remove_unknown_id_surfaces_state_error(shell_env, tmp_path):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    cdir = root / "Test Collection"

    result = run_cli(root, "/use 1\n/remove 9999\n/quit\n")
    assert result.exit_code == 0
    # The service's own StateError wording, surfaced clearly.
    assert f"No mod file with file_id 9999 in {cdir}" in result.output
    state = reload_state(cdir)
    assert 456 in state.mods


def test_remove_rejects_non_numeric_id(shell_env, tmp_path):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    result = run_cli(root, "/use 1\n/remove banana\n/quit\n")
    assert result.exit_code == 0
    assert "file_id must be a whole number" in result.output


def test_status_renders_revision_and_mod_table(
    shell_env, tmp_path, monkeypatch
):
    """/status <target> keeps the revision comparison (network) and adds
    the collection's local details: counts, stored URL, state path."""
    from nexus_collection_dl.service import ModStatus, StatusResult

    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    # Tombstone one mod page so the local detail counts it.
    state = reload_state(root / "Test Collection")
    state.mark_removed(123, "Test Mod")
    state.save()
    FakeShellService.status_result = StatusResult(
        collection_name="Test Collection",
        collection_url=COLLECTION_URL,
        game_domain=GAME,
        installed_revision=1,
        latest_revision=2,
        mods=[ModStatus(mod_id=123, name="Test Mod", version="1.0",
                        file_id=456, optional=False, manual=False,
                        phase=0, status="update_available")],
        deployed_at=None,
        deployed_file_count=0,
        game_dir="",
        track_sync_enabled=False,
    )

    result = run_cli(
        root, "/use 1\n/status 1\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert "Collection: Test Collection" in out
    assert "update available" in out
    assert "Test Mod" in out
    assert "update_available" in out
    # Local details alongside the network comparison.
    assert f"State path: {root / 'Test Collection' / STATE_FILENAME}" in out
    assert f"Collection URL: {COLLECTION_URL}" in out
    assert "Local: 1 mod(s), 0 pending, 1 removed (tombstoned)" in out
    # The revision check ran exactly once, for the target collection.
    assert FakeShellService.calls == [
        {"method": "get_status", "mods_dir": root / "Test Collection"}
    ]


# ---------------------------------------------------------------------------
# error resilience: the loop never dies
# ---------------------------------------------------------------------------

def test_domain_error_mid_command_keeps_loop_alive(shell_env, tmp_path):
    """A real domain error path: StateError from list_mods on a corrupt
    state file renders and the loop keeps serving commands.

    (Corrupting the listing is not enough: list_collections skips broken
    state files internally, so the corruption must land after /use and
    surface through a command that reads the state directly.)
    """
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    state_file = root / "Test Collection" / STATE_FILENAME

    def corrupt_state():
        state_file.write_text("{ not json")

    svc = ModManagerService(api_key="unused-key")
    code, out = run_direct(svc, root, [
        "/use 1",
        corrupt_state,
        "/mods",
        "/help",
        "/quit",
    ])
    assert code == 0
    assert "Invalid state file" in out
    # The loop kept going: /help ran after the failure.
    assert out.index("Invalid state file") < out.index("Shell commands")


def test_oserror_mid_command_renders_and_continues(
    shell_env, tmp_path, monkeypatch
):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    FakeShellService.status_error = OSError("No such device or address")

    result = run_cli(
        root, "/use 1\n/status 1\n/help\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert "No such device or address" in out
    # The loop kept going: /help ran after the failure.
    assert out.index("No such device or address") < out.index("Shell commands")


def test_add_rejects_unsupported_scheme(shell_env, tmp_path, monkeypatch):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    result = run_cli(
        root, "/use 1\n/add ftp://example.com/mod.zip\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    assert "http(s) mod page URL or an nxm link" in result.output
    assert FakeShellService.calls == []


def test_nxm_result_failure_renders_before_success_text(
    shell_env, tmp_path, monkeypatch
):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    FakeShellService.nxm_result = NxmResult(
        success=False, status="error", game_domain=GAME,
        mod_id=1, file_id=2,
        error="This download link has expired.",
        re_click_required=True,
    )

    result = run_cli(
        root, f"/use 1\n{HOSTILE_URI}\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert "This download link has expired." in out
    assert "again, then retry" in out
    assert "Downloaded" not in out
    assert "SUPERSECRETVALUE" not in out


def test_nxm_error_status_never_renders_as_success(
    shell_env, tmp_path, monkeypatch
):
    """An internally inconsistent result (success=True with an "error"
    status) must never print success text."""
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    FakeShellService.nxm_result = NxmResult(
        success=True, status="error", game_domain=GAME,
        mod_id=1, file_id=2,
        error="rejected by the server",
    )

    result = run_cli(
        root, f"/use 1\n{HOSTILE_URI}\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert "rejected by the server" in out
    assert "Downloaded" not in out
    assert "already downloaded" not in out


def test_nxm_success_warnings_render(shell_env, tmp_path, monkeypatch):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    FakeShellService.nxm_result = NxmResult(
        success=True, status="downloaded", game_domain=GAME,
        mod_id=1, file_id=2, mod_name="Test Mod",
        warnings=["load order regeneration failed"],
    )

    result = run_cli(
        root, f"/use 1\n{HOSTILE_URI}\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    assert "Warning:" in result.output
    assert "load order regeneration failed" in result.output


def test_add_pending_download_renders_manual_instructions(
    shell_env, tmp_path, monkeypatch
):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    FakeShellService.add_result = AddResult(
        mod_name="Manual Mod", mod_id=7, file_name="Manual-1.0.zip",
        success=True,
        pending_download=PendingDownload(
            mod_id=7, mod_name="Manual Mod", file_id=70,
            filename="Manual-1.0.zip", size_bytes=1024,
            browser_url="https://www.nexusmods.com/starfield/mods/7",
        ),
    )

    result = run_cli(
        root,
        "/use 1\n/add https://www.nexusmods.com/starfield/mods/7\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert "download manually" in out
    assert "Manual Mod" in out
    assert "Added" not in out  # pending is not a completed add


def test_quit_exit_and_eof_all_leave_zero(shell_env, tmp_path):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    for script in ("/quit\n", "/exit\n", ""):
        result = run_cli(root, script)
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# per-command help: <command> --help and /help <command>
# ---------------------------------------------------------------------------

def test_add_help_shows_usage_and_executes_nothing(shell_env, tmp_path, monkeypatch):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    result = run_cli(
        root, "/add --help\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert "/add <url>" in out
    assert "add one mod (https:// mod page or nxm:// link)" in out
    assert "/add https://www.nexusmods.com/skyrimspecialedition/mods/12345" in out
    assert "/add nxm://skyrimspecialedition/mods/12345/files/67890" in out
    # Help renders instead of executing.
    assert FakeShellService.calls == []


def test_sync_help_shows_three_target_forms_and_executes_nothing(
    shell_env, tmp_path, monkeypatch
):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    result = run_cli(
        root, "/sync --help\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert "/sync [<index|name|url>]" in out
    assert (
        "/sync https://next.nexusmods.com/skyrimspecialedition/collections/abc123"
    ) in out
    assert "/sync 2" in out
    assert FakeShellService.calls == []


def test_remove_help_shows_usage_and_executes_nothing(
    shell_env, tmp_path, monkeypatch
):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    result = run_cli(
        root, "/remove --help\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert "/remove <file_id|mod:id>" in out
    assert "/remove 456" in out
    assert "/remove mod:123" in out
    assert FakeShellService.calls == []


def test_help_command_alias_shows_the_same_help(shell_env, tmp_path, monkeypatch):
    """/help add renders the /add help and executes nothing."""
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    result = run_cli(
        root, "/help add\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert "/add <url>" in out
    assert "/add https://www.nexusmods.com/skyrimspecialedition/mods/12345" in out
    assert FakeShellService.calls == []


def test_help_specs_cover_every_registered_command():
    """Every command in the registry (aliases included) has help text."""
    assert set(_HELP_BY_NAME) == {
        command.lstrip("/") for command in _ShellSession._COMMANDS
    }


@pytest.mark.parametrize("command", sorted(_ShellSession._COMMANDS))
def test_every_registered_command_has_a_help_path(
    shell_env, tmp_path, monkeypatch, command
):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    result = run_cli(
        root, f"{command} --help\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert "Error:" not in out
    assert _HELP_BY_NAME[command.lstrip("/")].form in out
    assert FakeShellService.calls == []


def test_bare_help_line_shows_the_command_list(shell_env, tmp_path):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    result = run_cli(root, "--help\n/quit\n")
    assert result.exit_code == 0
    out = result.output
    assert "Shell commands" in out
    assert "/add <url>" in out
    assert "Unknown input" not in out


def test_unknown_command_help_flag_uses_the_usual_path(
    shell_env, tmp_path, monkeypatch
):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    result = run_cli(
        root, "/frobnicate --help\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    assert "Unknown command '/frobnicate'" in result.output
    assert "try /help" in result.output
    assert FakeShellService.calls == []


def test_help_flag_only_triggers_as_the_sole_argument(
    shell_env, tmp_path, monkeypatch
):
    """--help must never swallow a real argument that merely contains it."""
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    url = "https://next.nexusmods.com/games/collections/--help"
    result = run_cli(
        root, f"/sync {url}\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    # The URL synced as data; it was not answered with help text.
    assert FakeShellService.calls == [
        {"method": "sync", "collection_url": url, "mods_dir": root,
         "target_dir": None}
    ]
    assert "usage:" not in result.output


def test_collection_named_help_flag_is_reachable_by_index(shell_env, tmp_path):
    root = tmp_path / "root"
    write_collection(root / "weird", name="--help")
    write_collection(root / "normal", name="Normal")

    # /use --help shows help instead of selecting (documented collision).
    result = run_cli(root, "/use --help\n/quit\n")
    assert "usage:" in result.output
    assert "Using" not in result.output

    # The index form still selects the collection named "--help".
    result = run_cli(root, "/use 1\n/quit\n")
    assert "Using '--help'" in result.output


# ---------------------------------------------------------------------------
# bare /status: tool-wide local report (no network, no selection needed)
# ---------------------------------------------------------------------------

def fake_health_probes(**overrides):
    """Deterministic HealthProbes: no host probes, nothing to warn about."""
    fields = {
        "api_key_source": lambda: ("stored", None),
        "handler_status": lambda: {
            "entry_installed": True,
            "executable_exists": True,
            "is_default": True,
            "current_default": "nexus-dl-nxm.desktop",
            "xdg_mime_available": True,
        },
        "notifications_enabled": lambda: None,
        "notification_setting": lambda: None,
        "capability_warnings": list,
        "lock_state": lambda d: "free",
    }
    fields.update(overrides)
    return HealthProbes(**fields)


def use_fake_probes(monkeypatch, **overrides):
    """Route collect_health's host probes through deterministic fakes."""
    monkeypatch.setattr(
        service_module, "default_health_probes",
        lambda: fake_health_probes(**overrides),
    )


def test_bare_status_is_local_only_and_needs_no_collection(
    shell_env, tmp_path, monkeypatch
):
    root = make_root(
        tmp_path,
        ("Alpha Pack", [
            mod_entry(file_id=1, mod_id=10, name="A Mod"),
            mod_entry(file_id=2, mod_id=20, name="B Mod",
                      download_status="pending_download"),
        ]),
        ("Beta Pack", [mod_entry(file_id=3, mod_id=30, name="C Mod")]),
    )
    use_fake_probes(monkeypatch)
    result = run_cli(
        root, "/status\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    # No collection selected and no network: get_status must not run.
    assert FakeShellService.calls == []
    # (a) overview and totals
    assert "Games and collections" in out
    assert GAME in out
    assert "Total: 1 game(s), 2 collection(s), 3 mod(s), 1 pending." in out
    # (b) removal summary renders per collection
    assert "Tombstoned mods" in out
    # (c) health lines
    assert "Health" in out
    assert "API key: stored (keystore)" in out
    assert "nxm handler: installed and the default nxm:// handler" in out


def test_bare_status_removal_summary_counts_tombstones(
    shell_env, tmp_path, monkeypatch
):
    root = make_root(
        tmp_path,
        ("Alpha Pack", [mod_entry(file_id=1, mod_id=10, name="A Mod")]),
        ("Beta Pack", [mod_entry(file_id=2, mod_id=20, name="B Mod")]),
    )
    state = reload_state(root / "Alpha Pack")
    state.mark_removed(10, "A Mod")
    state.mark_removed(11, "Other Removed")
    state.save()
    use_fake_probes(monkeypatch)
    result = run_cli(
        root, "/status\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert "Tombstoned mods" in out
    assert "Total removed (tombstoned): 2 mod(s)." in out
    assert re.search(r"Alpha Pack.*\b2\b", out)
    assert re.search(r"Beta Pack.*\b0\b", out)


def test_bare_status_health_reports_missing_key_and_handler(
    shell_env, tmp_path, monkeypatch
):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    use_fake_probes(
        monkeypatch,
        api_key_source=lambda: (None, None),
        handler_status=lambda: {
            "entry_installed": False, "is_default": False,
            "current_default": None, "xdg_mime_available": True,
        },
    )
    result = run_cli(
        root, "/status\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert "WARN API key: not set" in out
    assert "handler set-api-key" in out
    assert "WARN nxm handler: not installed" in out
    assert "nexus-dl handler install" in out
    assert "API key: stored" not in out


def test_status_health_warns_with_gsettings_fix_when_notifications_disabled(
    shell_env, tmp_path, monkeypatch
):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    fix = ("gsettings set org.cinnamon.desktop.notifications"
           " display-notifications true")
    use_fake_probes(
        monkeypatch,
        notification_setting=lambda: (
            "org.cinnamon.desktop.notifications", "display-notifications", fix,
        ),
        notifications_enabled=lambda: False,
    )
    result = run_cli(
        root, "/status\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert "WARN Notifications: disabled" in out
    assert fix in out


def test_status_health_is_silent_for_an_unknown_desktop(
    shell_env, tmp_path, monkeypatch
):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    use_fake_probes(monkeypatch)  # notification_setting -> None: unknown
    result = run_cli(
        root, "/status\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert "Notifications" not in out
    assert "gsettings" not in out


def test_status_health_surfaces_invalid_state_files(
    shell_env, tmp_path, monkeypatch
):
    root = make_root(tmp_path, ("Good Collection", [mod_entry()]))
    broken = root / "Broken Dir"
    broken.mkdir()
    (broken / STATE_FILENAME).write_text("{ not json")
    use_fake_probes(monkeypatch)
    result = run_cli(
        root, "/status\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert "WARN State files: 1 unreadable state file(s)" in out
    assert "Broken Dir" in out
    # The healthy collection still renders in the overview.
    assert GAME in out


def test_status_health_check_failure_renders_error_and_status_completes(
    shell_env, tmp_path, monkeypatch
):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))

    def boom():
        raise RuntimeError("probe exploded")

    use_fake_probes(monkeypatch, handler_status=boom)
    result = run_cli(
        root, "/status\n/games\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert "ERROR nxm handler: check failed (RuntimeError" in out
    # A failing check is a line, not a traceback, and the other checks
    # and commands still ran.
    assert "Traceback" not in out
    assert "API key: stored (keystore)" in out
    assert "Games" in out


def test_status_output_never_carries_the_api_key(
    shell_env, tmp_path, monkeypatch
):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    use_fake_probes(monkeypatch, api_key_source=lambda: ("flag", None))
    svc = FakeShellService(api_key="SUPERSECRETKEYVALUE")
    code, out = run_direct(svc, root, ["/status", "/quit"])
    assert code == 0
    assert "API key: passed to this session" in out
    assert "SUPERSECRETKEYVALUE" not in out


def test_bare_status_works_on_an_empty_root(shell_env, tmp_path, monkeypatch):
    root = tmp_path / "empty"
    root.mkdir()
    use_fake_probes(monkeypatch)
    result = run_cli(
        root, "/status\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert "No collections found under" in out
    assert "Health" in out
    assert FakeShellService.calls == []


def test_status_help_documents_the_tool_wide_default(shell_env, tmp_path):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    result = run_cli(root, "/status --help\n/quit\n")
    assert result.exit_code == 0
    out = result.output
    assert "/status [<index|name>]" in out
    assert "tool-wide" in out
    assert "needs network" in out


# ---------------------------------------------------------------------------
# current collection: prompt, auto-select, bare /use, remembered selection
# ---------------------------------------------------------------------------

def run_capture_prompts(svc, root, script):
    """run_shell with an input_fn that records every prompt string."""
    prompts = []
    buf = io.StringIO()
    console = Console(file=buf, width=200, force_terminal=False)
    steps = iter(script)

    def input_fn(prompt):
        prompts.append(prompt)
        try:
            return next(steps)
        except StopIteration:
            raise EOFError from None

    code = run_shell(svc, root, input_fn=input_fn, console=console)
    return code, buf.getvalue(), prompts


def test_prompt_shows_the_current_collection_and_auto_selects_one(
    shell_env, tmp_path
):
    root = make_root(tmp_path, ("Solo Pack", [mod_entry()]))
    code, out, prompts = run_capture_prompts(FakeShellService(), root, ["/quit"])
    assert code == 0
    # Exactly one collection: selected at startup with a note.
    assert "Using 'Solo Pack' (only collection here)." in out
    assert prompts[0] == "nexus-dl [Solo Pack]> "


def test_prompt_is_plain_until_a_collection_is_selected(shell_env, tmp_path):
    root = make_root(tmp_path, ("Alpha", []), ("Beta", []))
    code, _out, prompts = run_capture_prompts(
        FakeShellService(), root, ["/use Beta", "/quit"]
    )
    assert code == 0
    # Two collections: no auto-select, plain prompt until /use.
    assert prompts[0] == "nexus-dl> "
    assert prompts[1] == "nexus-dl [Beta]> "


def test_prompt_sanitizes_a_weird_collection_name(shell_env, tmp_path):
    root = tmp_path / "root"
    write_collection(root / "weird", name="Weird\nName] [x")
    code, _out, prompts = run_capture_prompts(
        FakeShellService(), root, ["/quit"]
    )
    assert code == 0
    # Control characters collapse to spaces; brackets are plain text.
    assert "\n" not in prompts[0]
    assert prompts[0] == "nexus-dl [Weird Name] [x]> "


def test_bare_use_reports_the_selection_and_the_list(shell_env, tmp_path):
    root = make_root(tmp_path, ("Alpha", [mod_entry()]), ("Beta", []))
    # Bare /use alone never writes the memory file (only explicit
    # selection does).
    code, out, prompts = run_capture_prompts(
        FakeShellService(), root, ["/use", "/quit"]
    )
    assert code == 0
    assert "Current collection: none selected." in out
    assert not (root / ".nexus-shell.json").exists()

    code, out, prompts = run_capture_prompts(
        FakeShellService(), root, ["/use Beta", "/use", "/quit"]
    )
    assert code == 0
    assert "Current collection: 'Beta'" in out
    # The same collection table as /collections renders alongside.
    assert "Collections under" in out
    assert "Alpha" in out and "Beta" in out
    # Bare /use changed nothing: prompts before and after match.
    assert prompts[0] == "nexus-dl> "
    assert prompts[1] == prompts[2] == "nexus-dl [Beta]> "


def test_explicit_use_remembers_the_selection(shell_env, tmp_path):
    root = make_root(tmp_path, ("Alpha", []), ("Beta", []))
    memory = root / ".nexus-shell.json"
    code, _out, _ = run_capture_prompts(
        FakeShellService(), root, ["/use Beta", "/quit"]
    )
    assert code == 0
    assert json.loads(memory.read_text()) == {"current": "Beta"}


def test_auto_select_never_writes_the_memory(shell_env, tmp_path):
    root = make_root(tmp_path, ("Solo Pack", []))
    code, out, _ = run_capture_prompts(FakeShellService(), root, ["/quit"])
    assert code == 0
    assert "only collection here" in out
    assert not (root / ".nexus-shell.json").exists()


def test_startup_restores_the_remembered_selection(shell_env, tmp_path):
    root = make_root(tmp_path, ("Alpha", []), ("Beta", []))
    memory = root / ".nexus-shell.json"
    memory.write_text(json.dumps({"current": "Beta"}))
    before = memory.read_bytes()
    code, out, prompts = run_capture_prompts(FakeShellService(), root, ["/quit"])
    assert code == 0
    assert prompts[0] == "nexus-dl [Beta]> "
    assert "Using 'Beta'" in out
    # Restore never rewrites the memory file: bytes are identical.
    assert memory.read_bytes() == before


def test_startup_restores_a_root_level_collection(shell_env, tmp_path):
    root = tmp_path / "root"
    write_collection(root, name="Root Collection")
    memory = root / ".nexus-shell.json"
    memory.write_text(json.dumps({"current": "root"}))
    code, _out, prompts = run_capture_prompts(FakeShellService(), root, ["/quit"])
    assert code == 0
    assert prompts[0] == "nexus-dl [Root Collection]> "


def test_stale_remembered_selection_is_ignored_silently(shell_env, tmp_path):
    root = make_root(tmp_path, ("Alpha", []), ("Beta", []))
    (root / ".nexus-shell.json").write_text(json.dumps({"current": "gone"}))
    code, out, prompts = run_capture_prompts(FakeShellService(), root, ["/quit"])
    assert code == 0
    assert prompts[0] == "nexus-dl> "
    assert "Error:" not in out and "Warning:" not in out


def test_corrupt_remembered_selection_is_ignored_silently(shell_env, tmp_path):
    root = make_root(tmp_path, ("Alpha", []), ("Beta", []))
    (root / ".nexus-shell.json").write_text("{ not json")
    code, out, prompts = run_capture_prompts(FakeShellService(), root, ["/quit"])
    assert code == 0
    assert prompts[0] == "nexus-dl> "
    assert "Error:" not in out and "Warning:" not in out


# ---------------------------------------------------------------------------
# selector precedence: exact name first, then ASCII-digit index
# ---------------------------------------------------------------------------

def test_digit_named_collection_wins_over_index(shell_env, tmp_path):
    root = tmp_path / "root"
    write_collection(root / "a", name="2024")
    write_collection(root / "b", name="Alpha")

    # Exact case-sensitive name match beats index interpretation.
    result = run_cli(root, "/use 2024\n/quit\n")
    assert result.exit_code == 0
    assert "Using '2024'" in result.output

    # Index interpretation still applies when no name matches.
    result = run_cli(root, "/use 2\n/quit\n")
    assert result.exit_code == 0
    assert "Using 'Alpha'" in result.output


def test_unicode_digits_are_never_an_index(shell_env, tmp_path):
    root = tmp_path / "root"
    write_collection(root / "a", name="2024")
    write_collection(root / "b", name="Alpha")

    # "\u0662" (Arabic-Indic two) is decimal but not ASCII: never an
    # index, so it falls through to name matching and fails cleanly.
    result = run_cli(root, "/use \u0662\n/quit\n")
    assert result.exit_code == 0
    assert "no collection named" in result.output


# ---------------------------------------------------------------------------
# re-add of a collection member vs a genuinely manual mod
# ---------------------------------------------------------------------------

def test_re_adding_a_collection_member_reports_re_added(
    shell_env, tmp_path, monkeypatch
):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    # The cached manifest knows mod 123 as a collection member.
    state = reload_state(root / "Test Collection")
    state.manifest_data = {
        "mod_rules": [{"source": {"modId": 123, "fileID": 456}}],
    }
    state.save()
    FakeShellService.add_result = AddResult(
        mod_name="Test Mod", mod_id=123, file_name="TestMod-456.zip",
        success=True,
    )

    result = run_cli(
        root,
        "/use 1\n/add https://www.nexusmods.com/starfield/mods/123\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert "Re-added 'Test Mod' to 'Test Collection'" in out
    assert "not a manual mod" in out
    assert "as manual mod" not in out


def test_adding_an_unlisted_mod_keeps_the_manual_wording(
    shell_env, tmp_path, monkeypatch
):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    # No cached manifest: membership cannot include the new mod.
    FakeShellService.add_result = AddResult(
        mod_name="Fresh Mod", mod_id=999, file_name="Fresh.zip",
        success=True,
    )

    result = run_cli(
        root,
        "/use 1\n/add https://www.nexusmods.com/starfield/mods/999\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert "Added 'Fresh Mod' as manual mod (phase 999)." in out
    assert "Re-added" not in out


# ---------------------------------------------------------------------------
# /status sections render independently even with bad state values
# ---------------------------------------------------------------------------

def test_status_renders_every_section_with_a_non_string_game_domain(
    shell_env, tmp_path, monkeypatch
):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    state = reload_state(root / "Test Collection")
    state.game_domain = 42
    state.save()
    use_fake_probes(monkeypatch)

    result = run_cli(
        root, "/status\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    # Every section rendered despite the non-string game domain.
    assert "Games and collections" in out
    assert "Tombstoned mods" in out
    assert "API key: stored (keystore)" in out


# ---------------------------------------------------------------------------
# Round B hardening: bad state shapes, membership wording, control chars,
# short-secret tiering, sync reporting
# ---------------------------------------------------------------------------

def test_non_string_collection_names_never_break_the_listing(
    shell_env, tmp_path
):
    root = tmp_path / "root"
    write_collection(root / "numeric", name=42)
    write_collection(root / "other", name="Other")

    result = run_cli(root, "/collections\n/use 1\n/status\n/quit\n")
    assert result.exit_code == 0
    out = result.output
    # The coerced name renders and every /status section completes.
    assert "42" in out
    assert "Games and collections" in out
    assert "Tombstoned mods" in out
    assert "API key" in out


def test_re_adding_a_member_on_a_free_account_reports_re_added(
    shell_env, tmp_path, monkeypatch
):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    state = reload_state(root / "Test Collection")
    state.manifest_data = {
        "mod_rules": [{"source": {"modId": 123, "fileID": 456}}],
    }
    state.save()
    FakeShellService.add_result = AddResult(
        mod_name="Test Mod", mod_id=123, file_name="TestMod-456.zip",
        success=True,
        pending_download=PendingDownload(
            mod_id=123, mod_name="Test Mod", file_id=456,
            filename="TestMod-456.zip", size_bytes=10,
            browser_url="https://www.nexusmods.com/starfield/mods/123",
        ),
    )

    result = run_cli(
        root,
        "/use 1\n/add https://www.nexusmods.com/starfield/mods/123\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    # Membership wording shows on the pending path too, and the manual
    # download instructions still follow.
    assert "Re-added 'Test Mod' to 'Test Collection'" in out
    assert "not a manual mod" in out
    assert "download manually" in out


def test_nxm_add_of_a_member_reports_re_added(shell_env, tmp_path, monkeypatch):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    state = reload_state(root / "Test Collection")
    state.manifest_data = {
        "mod_rules": [{"source": {"modId": 123, "fileID": 456}}],
    }
    state.save()
    FakeShellService.nxm_result = NxmResult(
        success=True, status="downloaded", game_domain=GAME,
        mod_id=123, file_id=2, mod_name="Test Mod",
        collection_dir=root / "Test Collection",
    )

    result = run_cli(
        root,
        "/use 1\nnxm://skyrimspecialedition/mods/123/files/2/extra\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert "Re-added 'Test Mod' to 'Test Collection'" in out
    assert "Downloaded 'Test Mod'" in out


def test_control_characters_never_reach_the_terminal(shell_env, tmp_path):
    evil = "Evil\x1b[2JName\nX\x07Y"
    root = tmp_path / "root"
    write_collection(
        root / "weird", name=evil, mods=[mod_entry(name="M\x07od\nX")]
    )
    code, out, prompts = run_capture_prompts(
        FakeShellService(), root,
        ["/use", "/use 1", "/evil\x07cmd", "/help evil\x07name",
         "/remove 456", "no", "/quit"],
    )
    assert code == 0
    # Raw control bytes never reach the terminal: not in output, not in
    # any prompt (including the /remove confirm prompt).
    assert "\x1b" not in out
    assert "\x07" not in out
    for p in prompts:
        assert "\x1b" not in p
        assert "\x07" not in p
        assert "\n" not in p
    # The name renders collapsed to one plain line everywhere.
    assert "Evil [2JName X Y" in out
    # Hostile typed input is collapsed in the unknown-command echoes
    # (both the /run_line path and the /help <command> path).
    assert "Unknown command '/evil cmd'" in out
    assert "Unknown command 'evil name'" in out
    # The confirm prompt (an input() string, recorded by input_fn)
    # shows the collapsed mod name too.
    assert "Remove 'M od X'? [yes/no]: " in prompts


def test_control_chars_in_name_bearing_error_lines(shell_env, tmp_path):
    evil = "Bad\x1bName\x07!"
    root = tmp_path / "root"
    # No mods (hits the "No mods tracked in" line) and no stored URL
    # (hits the "no collection URL is stored for" line).
    write_collection(root / "bad", name=evil, url="")

    code, out, _ = run_capture_prompts(
        FakeShellService(), root, ["/use 1", "/mods", "/sync", "/quit"],
    )
    assert code == 0
    assert "\x1b" not in out
    assert "\x07" not in out
    assert "No mods tracked in 'Bad Name !'." in out
    assert "no collection URL is stored for 'Bad Name !'" in out


def test_control_chars_in_root_url_and_typed_input(shell_env, tmp_path):
    """Root/path echoes, the stored-URL display, typed numeric input, and
    the health report all collapse control characters."""
    # Hostile root name with no collections: the resolver's error.
    empty_root = tmp_path / "evil\x07root"
    empty_root.mkdir()
    code, out, _ = run_capture_prompts(
        FakeShellService(), empty_root, ["/use nope", "/quit"],
    )
    assert code == 0
    assert "\x07" not in out
    assert "no collections found under" in out

    # Hostile stored URL (query stripped for display, path collapsed),
    # a broken state dir with a hostile name (the health report lists
    # it), and typed numeric input carrying a BEL.
    root = tmp_path / "root"
    write_collection(
        root / "good", name="Good",
        url="https://example.com/evil\x07path?q=1",
    )
    bad = root / "bad\x07dir"
    bad.mkdir(parents=True)
    (bad / ".nexus-state.json").write_text("{not json")
    from nexus_collection_dl.service import StatusResult
    FakeShellService.status_result = StatusResult(
        collection_name="Good",
        collection_url="",
        game_domain=GAME,
        installed_revision=1,
        latest_revision=None,
        mods=[],
        deployed_at=None,
        deployed_file_count=0,
        game_dir="",
        track_sync_enabled=False,
    )
    code, out, _ = run_capture_prompts(
        FakeShellService(), root,
        ["/use 1", "/status 1", "/remove ban\x07ana", "/status", "/quit"],
    )
    assert code == 0
    assert "\x07" not in out
    assert "Collection URL: https://example.com/evil path" in out
    assert "must be a whole number, got 'ban ana'" in out
    assert "State files" in out
    assert "bad dir" in out


def test_short_absorbed_secret_does_not_garble_prose(
    shell_env, tmp_path, monkeypatch
):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    result = run_cli(
        root,
        "https://example.com/?key=ZQXJ&expires=1\n"
        f"/sync {COLLECTION_URL}\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    # The one-character value absorbed from the link keeps the length
    # floor in bare prose: routine output is not garbled.
    assert "Synced 1 mods (1 extracted)." in out


def test_short_absorbed_secret_still_scrubs_urls(
    shell_env, tmp_path, monkeypatch
):
    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    FakeShellService.add_result = AddResult(
        mod_name="M", mod_id=1, file_name="f.zip", success=True,
        pending_download=PendingDownload(
            mod_id=1, mod_name="M", file_id=2, filename="f.zip",
            size_bytes=1,
            browser_url="https://example.com/dl?key=ZQXJ&expires=9W8V",
        ),
    )
    result = run_cli(
        root, "https://example.com/?key=ZQXJ&expires=9W8V\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    # Short secrets keep the floor only for bare prose: inside a URL
    # query they are still redacted.
    assert "ZQXJ" not in out
    assert "9W8V" not in out
    assert "https://example.com/dl?" in out


def test_sync_renders_conflicts_and_tracked_counts(
    shell_env, tmp_path, monkeypatch
):
    from nexus_collection_dl.service import FileConflict

    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    FakeShellService.sync_result = SyncResult(
        mods_downloaded=1, mods_extracted=1, errors=[], load_order_files=[],
        tracked=2, untracked=1,
        conflicts=[
            FileConflict(
                file_path="meshes/a.nif", winner="New Mod", loser="Old Mod"
            ),
        ],
    )
    result = run_cli(
        root, f"/sync {COLLECTION_URL}\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    out = result.output
    assert "Tracked 2, untracked 1 on Nexus." in out
    assert "1 file conflict(s) (last mod wins):" in out
    assert "meshes/a.nif: New Mod overwrote Old Mod" in out


def test_status_target_surfaces_removed_skipped(
    shell_env, tmp_path, monkeypatch
):
    from nexus_collection_dl.service import StatusResult

    root = make_root(tmp_path, ("Test Collection", [mod_entry()]))
    FakeShellService.status_result = StatusResult(
        collection_name="Test Collection",
        collection_url=COLLECTION_URL,
        game_domain=GAME,
        installed_revision=1,
        latest_revision=1,
        mods=[],
        deployed_at=None,
        deployed_file_count=0,
        game_dir="",
        track_sync_enabled=False,
        removed_skipped=2,
    )
    result = run_cli(
        root, "/status 1\n/quit\n",
        monkeypatch=monkeypatch, service_cls=FakeShellService,
    )
    assert result.exit_code == 0
    assert "Removed mods skipped: 2 (tombstoned)." in result.output


@pytest.mark.parametrize("value", ["../../etc", "/etc", "sub/dir", "."])
def test_traversal_shaped_memory_values_are_ignored(
    shell_env, tmp_path, value
):
    """A remembered value that is not a plain child-directory name can
    never select anything: matching is by exact collection path, so
    traversal or absolute values fall through to a silent ignore."""
    root = make_root(tmp_path, ("Alpha", []), ("Beta", []))
    (root / ".nexus-shell.json").write_text(json.dumps({"current": value}))
    code, out, prompts = run_capture_prompts(FakeShellService(), root, ["/quit"])
    assert code == 0
    assert prompts[0] == "nexus-dl> "
    assert "Error:" not in out and "Warning:" not in out


# ---------------------------------------------------------------------------
# click-event drain
# ---------------------------------------------------------------------------


def _write_event(coll_dir: Path, **overrides):
    """Append one event line to a collection's events file."""
    from nexus_collection_dl.handler import append_nxm_event

    defaults = {
        "outcome": "downloaded",
        "collection_name": coll_dir.name,
        "collection_dir": str(coll_dir),
        "mod_id": 123,
        "file_id": 456,
        "mod_name": "SkyUI",
        "filename": "SkyUI.7z",
        "extracted": True,
    }
    defaults.update(overrides)
    append_nxm_event(coll_dir, defaults)


def run_events(svc, root, script, waiter=None):
    """run_shell with prompt recording and callable side effects."""
    prompts = []
    buf = io.StringIO()
    console = Console(file=buf, width=200, force_terminal=False)
    steps = iter(script)

    def input_fn(prompt):
        prompts.append(prompt)
        while True:
            try:
                item = next(steps)
            except StopIteration:
                raise EOFError from None
            if isinstance(item, BaseException):
                raise item
            if callable(item):
                item()
                continue
            return item

    code = run_shell(
        svc, root, input_fn=input_fn, console=console, waiter=waiter
    )
    return code, buf.getvalue(), prompts


def test_event_reported_at_next_prompt_boundary(shell_env, tmp_path):
    root = make_root(tmp_path, ("Clean Visuals", []))
    coll = root / "Clean Visuals"

    def write_ev():
        _write_event(coll)

    code, out, _ = run_events(FakeShellService(), root, [
        "",
        write_ev,
        "",
    ])
    assert code == 0
    assert "Click activity while the shell was running" in out
    assert "SkyUI" in out


def test_tail_seed_skips_preexisting_events(shell_env, tmp_path):
    root = make_root(tmp_path, ("Clean Visuals", []))
    coll = root / "Clean Visuals"
    _write_event(coll)

    code, out, _ = run_events(FakeShellService(), root, ["/quit"])
    assert code == 0
    assert "Click activity" not in out


def test_drained_events_not_re_reported(shell_env, tmp_path):
    root = make_root(tmp_path, ("Clean Visuals", []))
    coll = root / "Clean Visuals"

    def write_ev():
        _write_event(coll)

    code, out, _ = run_events(FakeShellService(), root, [
        write_ev,
        "",
        "",
    ])
    assert code == 0
    assert out.count("Click activity") == 1
    assert out.count("SkyUI") == 1


def test_rotation_resets_offset(shell_env, tmp_path):
    root = make_root(tmp_path, ("Clean Visuals", []))
    coll = root / "Clean Visuals"

    def write_and_rotate():
        _write_event(coll, mod_name="First")
        ev_path = coll / ".nexus-events.jsonl"
        ev_path.rename(ev_path.with_name(ev_path.name + ".1"))

    def write_new():
        _write_event(coll, mod_name="Second")

    code, out, _ = run_events(FakeShellService(), root, [
        write_and_rotate,
        "",
        write_new,
        "",
    ])
    assert code == 0
    assert "Second" in out
    assert "First" not in out


def test_grouping_one_prompt_per_collection(shell_env, tmp_path):
    root = make_root(tmp_path, ("Clean Visuals", []))
    coll = root / "Clean Visuals"

    def write_three():
        for i in range(3):
            _write_event(coll, mod_name=f"Mod{i}")

    code, out, prompts = run_events(FakeShellService(), root, [
        write_three,
        "no",
    ])
    assert code == 0
    assert out.count("Click activity") == 1
    deploy_prompts = [p for p in prompts if "Deploy" in p]
    assert len(deploy_prompts) == 1


def test_non_deployable_outcomes_report_without_prompting(
    shell_env, tmp_path
):
    root = make_root(tmp_path, ("Clean Visuals", []))
    coll = root / "Clean Visuals"

    def write_mixed():
        _write_event(coll, outcome="error", reason="nope")
        _write_event(coll, outcome="already_downloaded", mod_name="SKSE64")
        _write_event(coll, outcome="pending_download", mod_name="SkyUI")

    code, out, prompts = run_events(FakeShellService(), root, [
        write_mixed,
        "",
    ])
    assert code == 0
    assert "failed" in out
    assert "already downloaded" in out
    assert "awaiting import" in out
    deploy_prompts = [p for p in prompts if "Deploy" in p]
    assert len(deploy_prompts) == 0


def test_vanished_collection_dir_noted_no_prompt(shell_env, tmp_path):
    root = make_root(tmp_path, ("CollA", []))
    coll = root / "CollA"

    def write_then_rename():
        # The events file moves with the rename, but collection_dir in
        # the event still points at the old path.
        _write_event(coll, collection_dir=str(root / "Missing"))
        coll.rename(root / "CollB")

    code, out, prompts = run_events(FakeShellService(), root, [
        write_then_rename,
        "",
    ])
    assert code == 0
    assert "directory gone" in out
    deploy_prompts = [p for p in prompts if "Deploy" in p]
    assert len(deploy_prompts) == 0


# --- deploy flow -----------------------------------------------------------


def _setup_deploy_collection(tmp_path, name="Clean Visuals"):
    """Root with one collection that has a game_dir and a deployable file.

    The game directory is established through the user config, not
    through the state file: state alone is no longer a source of truth
    for where a deploy may write. This is what a configured user looks
    like, so the deploy flow does not stop to ask.
    """
    root = make_root(tmp_path, (name, []))
    coll = root / name
    game = tmp_path / "game"
    game.mkdir()
    state = CollectionState(coll)
    state.load()
    state.game_dir = str(game)
    state.save()
    cfg_dir = tmp_path / "xdg-config" / "nexus-dl"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(json.dumps({
        "collections_root": str(root),
        "games": [{"domain": GAME, "game_dir": str(game)}],
        "defaults": {},
    }))
    # One deployable file (not hidden).
    (coll / "Data").mkdir()
    (coll / "Data" / "test.esp").write_bytes(b"fake")
    return root, coll, game


def test_deploy_dry_run_then_cancel(shell_env, tmp_path):
    root, coll, _game = _setup_deploy_collection(tmp_path)

    def write_ev():
        _write_event(coll)

    code, out, _prompts = run_events(FakeShellService(), root, [
        write_ev,
        "",       # no-op so the drain fires before the next input
        "yes",    # drain prompt: enter deploy flow
        "no",     # deploy confirm: cancel
    ])
    assert code == 0
    assert "Would deploy" in out
    assert "Deploy cancelled." in out
    assert FakeShellService.calls == []


def test_deploy_dry_run_then_confirm_deploys_once(shell_env, tmp_path):
    root, coll, _game = _setup_deploy_collection(tmp_path)

    def write_ev():
        _write_event(coll)

    code, out, _ = run_events(FakeShellService(), root, [
        write_ev,
        "",       # no-op so the drain fires
        "yes",    # drain prompt
        "yes",    # deploy confirm
    ])
    assert code == 0
    deploy_calls = [c for c in FakeShellService.calls if c["method"] == "deploy"]
    assert len(deploy_calls) == 1
    assert "Deployed" in out


def test_keyboard_interrupt_at_deploy_prompt_cancels(shell_env, tmp_path):
    root, coll, _game = _setup_deploy_collection(tmp_path)

    def write_ev():
        _write_event(coll)

    code, out, _ = run_events(FakeShellService(), root, [
        write_ev,
        "",                     # no-op so the drain fires
        "yes",                  # drain prompt
        KeyboardInterrupt(),    # deploy confirm
    ])
    assert code == 0
    assert "Deploy cancelled." in out
    assert FakeShellService.calls == []


def test_deploy_no_at_drain_prompt_skips_flow(shell_env, tmp_path):
    root, coll, _game = _setup_deploy_collection(tmp_path)

    def write_ev():
        _write_event(coll)

    code, out, _ = run_events(FakeShellService(), root, [
        write_ev,
        "",     # no-op so the drain fires
        "no",   # drain prompt: skip
    ])
    assert code == 0
    assert "Would deploy" not in out
    assert FakeShellService.calls == []


def test_drain_offers_fomod_choices_and_installs(shell_env, tmp_path):
    root, coll, _game = _setup_deploy_collection(tmp_path)

    def write_ev():
        _write_event(coll, outcome="pending_choices", extracted=False)

    code, out, prompts = run_events(FakeShellService(), root, [
        write_ev,
        "",      # no-op so the drain fires
        "yes",   # Choose now?
        "no",    # the deploy prompt that follows the install
    ])
    assert code == 0
    assert any("1 FOMOD mod in" in p and "need option choices" in p for p in prompts)
    installs = [c for c in FakeShellService.calls if c["method"] == "install_fomod_picks"]
    assert len(installs) == 1
    assert installs[0]["file_id"] == 456
    # The install landed, so the deploy question follows and the report
    # says what happened.
    assert any("Deploy" in p for p in prompts)
    assert "installed with 1 option choice(s)" in out


def test_drain_declining_fomod_choices_installs_nothing(shell_env, tmp_path):
    root, coll, _game = _setup_deploy_collection(tmp_path)

    def write_ev():
        _write_event(coll, outcome="pending_choices", extracted=False)

    code, out, _ = run_events(FakeShellService(), root, [
        write_ev,
        "",
        "no",   # Choose now? no
    ])
    assert code == 0
    assert FakeShellService.calls == []
    assert "Would deploy" not in out


def test_drain_fomod_install_failure_reports_and_continues(shell_env, tmp_path):
    root, coll, _game = _setup_deploy_collection(tmp_path)

    def write_ev():
        _write_event(coll, outcome="pending_choices", extracted=False)

    FakeShellService.install_error = ValueError("no such FOMOD option: G:Nope")
    code, out, _ = run_events(FakeShellService(), root, [
        write_ev,
        "",
        "yes",
    ])
    assert code == 0
    assert "no such FOMOD option" in out
    assert "Would deploy" not in out


# --- /deploy command -------------------------------------------------------


def test_deploy_command_uses_current(shell_env, tmp_path):
    root, _coll, _game = _setup_deploy_collection(tmp_path)
    code, _out, _ = run_events(FakeShellService(), root, [
        "/deploy",
        "yes",
    ])
    assert code == 0
    deploy_calls = [c for c in FakeShellService.calls if c["method"] == "deploy"]
    assert len(deploy_calls) == 1


def test_deploy_command_no_selection_errors(shell_env, tmp_path):
    root = make_root(tmp_path, ("Alpha", []), ("Beta", []))
    code, out, _ = run_events(FakeShellService(), root, ["/deploy"])
    assert code == 0
    assert "no collection selected" in out


def test_deploy_command_partial_failure_no_success_line(
    shell_env, tmp_path
):
    root, _coll, game = _setup_deploy_collection(tmp_path)
    FakeShellService.deploy_result = DeployResult(
        deployed_count=2, conflicts=[], errors=["file locked"],
        game_dir=str(game), has_sfse=False,
    )
    code, out, _ = run_events(FakeShellService(), root, [
        "/deploy",
        "yes",
    ])
    assert code == 0
    assert "Deploy incomplete" in out
    assert "Deployed" not in out


def test_deploy_command_conflicts_truncate_at_10(shell_env, tmp_path):
    root, _coll, game = _setup_deploy_collection(tmp_path)
    FakeShellService.deploy_result = DeployResult(
        deployed_count=5,
        conflicts=[f"conflict-{i}" for i in range(15)],
        errors=[],
        game_dir=str(game),
        has_sfse=False,
    )
    code, out, _ = run_events(FakeShellService(), root, [
        "/deploy",
        "yes",
    ])
    assert code == 0
    assert "conflict-0" in out
    assert "conflict-9" in out
    assert "conflict-10" not in out
    assert "and 5 more" in out


def test_live_drain_surfaces_clicks_without_a_keystroke(shell_env, tmp_path):
    """A click landing while idle is handled before anyone types.

    The waiter models the idle wait: a tick where the click arrives and
    nobody has typed, then input readiness. The drain must fire on the
    tick - no Enter, no keystroke at all - and start the flow.
    """
    root = make_root(tmp_path, ("Clean Visuals", []))
    coll = root / "Clean Visuals"
    ticks = {"n": 0}

    def waiter():
        ticks["n"] += 1
        if ticks["n"] == 1:
            _write_event(coll)  # the click lands while the user is idle
            return False
        return True

    code, out, _prompts = run_events(
        FakeShellService(), root, [""], waiter=waiter
    )
    assert code == 0
    assert ticks["n"] >= 2
    assert "Click activity while the shell was running" in out
    assert "SkyUI" in out


def test_live_drain_redraws_the_prompt_after_an_idle_report(shell_env, tmp_path):
    """The prompt line survives an idle-time report."""
    root = make_root(tmp_path, ("Clean Visuals", []))
    coll = root / "Clean Visuals"
    ticks = {"n": 0}

    def waiter():
        ticks["n"] += 1
        if ticks["n"] == 1:
            _write_event(coll)
            return False
        return True

    code, out, _prompts = run_events(
        FakeShellService(), root, [""], waiter=waiter
    )
    assert code == 0
    # Drawn once up front and once again after the tick's report.
    assert out.count("nexus-dl [Clean Visuals]>") >= 2


def test_ctrl_c_at_fomod_picker_aborts_the_walk(shell_env, tmp_path):
    """Ctrl-C at a FOMOD group prompt aborts the entire walk: no picks
    are recorded and the mod stays pending. The walk must not trudge
    through every remaining group after the user cancels."""
    root = make_root(tmp_path, ("Clean Visuals", []))
    coll = root / "Clean Visuals"

    def write_ev():
        _write_event(coll, outcome="pending_choices")

    ticks = {"n": 0}

    def waiter():
        ticks["n"] += 1
        if ticks["n"] == 1:
            write_ev()
            return False
        return True

    # Drain prompt: "Choose now? [yes/no]:" -> yes
    # Then the FOMOD walk's first "Pick one:" -> Ctrl-C (abort)
    code, out, _prompts = run_events(
        FakeShellService(),
        root,
        ["yes", KeyboardInterrupt()],
        waiter=waiter,
    )
    assert code == 0  # shell survives
    # The walk was aborted: no install_fomod_picks call with real picks
    installs = [
        c for c in FakeShellService.calls if c["method"] == "install_fomod_picks"
    ]
    # Either no install call at all, or one with empty picks
    for call in installs:
        assert not call.get("picks"), "abort must not install picks"
    # The shell is still usable
    assert "nexus-dl" in out
