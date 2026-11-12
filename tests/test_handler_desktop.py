"""Desktop integration tests: Exec escaping, entry content, install lifecycle.

All subprocesses are fake argv-list runners (never shell strings) and all
paths are task-scoped tmp dirs; no live desktop registration happens. The
generated entry is additionally checked with desktop-file-validate when
the host has it (reported as a skip otherwise).
"""

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus_collection_dl import handler as handler_module
from nexus_collection_dl.handler import (
    CONFIG_FILENAME,
    DESKTOP_ENTRY_NAME,
    NXM_MIME_TYPE,
    DesktopEntryError,
    DesktopInstaller,
    HandlerConfig,
    HandlerError,
    NxmRoutingError,
    add_mapping,
    applications_dir,
    build_desktop_entry_content,
    config_dir,
    escape_exec_argument,
    notify,
    remove_mapping,
    resolve_collection_dir,
)
from nexus_collection_dl.state import CollectionState

GAME = "skyrimspecialedition"


class FakeRunner:
    """Records argv lists; scripts per-program failures/absence."""

    def __init__(self, query_default="", fail=(), missing=()):
        self.calls = []
        self.query_default = query_default
        self.fail = set(fail)
        self.missing = set(missing)

    def __call__(self, argv):
        # Hard guarantee for the whole suite: argv lists only, never a
        # shell string handed to shell=True.
        assert isinstance(argv, (list, tuple)), f"runner got non-argv: {argv!r}"
        assert all(isinstance(a, str) for a in argv)
        self.calls.append(list(argv))
        prog = argv[0]
        if prog in self.missing:
            raise FileNotFoundError(prog)
        rc = 1 if prog in self.fail else 0
        stdout = self.query_default if argv[:2] == ["xdg-mime", "query"] else ""
        return SimpleNamespace(returncode=rc, stdout=stdout,
                               stderr="stderr-detail" if rc else "")

    def has_call(self, *argv):
        return list(argv) in self.calls


@pytest.fixture()
def exe(tmp_path):
    path = tmp_path / "bin" / "nexus-dl"
    path.parent.mkdir()
    path.write_text("#!/bin/sh\necho nexus-dl\n")
    path.chmod(0o755)
    return path


@pytest.fixture()
def cfg_path(tmp_path):
    return tmp_path / "config" / CONFIG_FILENAME


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
# Exec escaping: both layers (backslash escapes + quoting) and %% percents
# ---------------------------------------------------------------------------

def test_plain_path_needs_no_escaping():
    assert escape_exec_argument("/usr/bin/nexus-dl") == "/usr/bin/nexus-dl"


def test_space_wraps_argument_in_double_quotes():
    assert escape_exec_argument("/opt/my apps/nexus-dl") == '"/opt/my apps/nexus-dl"'


def test_apostrophe_quotes_argument_without_backslash():
    # A single quote is reserved, so the whole argument is double-quoted,
    # but inside the quotes it stays literal (no ambiguous \' escape).
    out = escape_exec_argument("/opt/it's/nexus-dl")
    assert out == '"/opt/it\'s/nexus-dl"'
    assert "\\" not in out


def test_double_quote_dollar_backtick_backslash_are_escaped_inside_quotes():
    assert escape_exec_argument('/opt/say"hi/nexus-dl') == '"/opt/say\\"hi/nexus-dl"'
    assert escape_exec_argument("/opt/$HOME/nexus-dl") == '"/opt/\\$HOME/nexus-dl"'
    assert escape_exec_argument("/opt/a`b/nexus-dl") == '"/opt/a\\`b/nexus-dl"'
    assert escape_exec_argument("/opt/a\\b/nexus-dl") == '"/opt/a\\\\b/nexus-dl"'


def test_percent_becomes_double_percent_literal():
    # Unquoted when nothing else is reserved...
    assert escape_exec_argument("/opt/100%/nexus-dl") == "/opt/100%%/nexus-dl"
    # ...and still %% inside quotes.
    assert escape_exec_argument("/opt/100% my/nexus-dl") == '"/opt/100%% my/nexus-dl"'


@pytest.mark.parametrize("path", [
    "/opt/a;b/nexus-dl",
    "/opt/a&b/nexus-dl",
    "/opt/~a/nexus-dl",
    "/opt/a|b/nexus-dl",
    "/opt/a<b/nexus-dl",
    "/opt/a*b/nexus-dl",
    "/opt/a?b/nexus-dl",
    "/opt/a#b/nexus-dl",
    "/opt/a(b)/nexus-dl",
])
def test_other_reserved_chars_rely_on_quoting_alone(path):
    out = escape_exec_argument(path)
    assert out == f'"{path}"'          # quoted, no backslashes invented
    assert "\\" not in out


@pytest.mark.parametrize("bad", [
    "",                       # empty
    "/opt/a\nb/nexus-dl",     # newline: unrepresentable in an Exec value
    "/opt/a\x00b/nexus-dl",   # NUL
    "/opt/a\x7fb/nexus-dl",   # DEL
])
def test_unrepresentable_paths_rejected_safely(bad):
    with pytest.raises(DesktopEntryError):
        escape_exec_argument(bad)


# ---------------------------------------------------------------------------
# Entry content: %u must stay a standalone unquoted field code
# ---------------------------------------------------------------------------

def test_entry_content_exec_line(exe):
    content = build_desktop_entry_content(exe)
    exec_line = next(l for l in content.splitlines() if l.startswith("Exec="))
    assert exec_line == f"Exec={exe} nxm %u --notify"
    # Headless: no terminal window for a click, and the notification is
    # baked into the Exec line so it never depends on stdout being a TTY.
    assert "Terminal=false" in content
    assert "Terminal=true" not in content
    assert f"MimeType={NXM_MIME_TYPE};" in content
    assert "[Desktop Entry]" in content
    assert "Type=Application" in content


def test_entry_content_percent_u_unquoted_with_quoted_exe(tmp_path):
    tricky = tmp_path / "my apps" / "it's $100%" / "nexus-dl"
    tricky.parent.mkdir(parents=True)
    tricky.write_text("#!/bin/sh\n")
    content = build_desktop_entry_content(tricky)
    exec_line = next(l for l in content.splitlines() if l.startswith("Exec="))
    # Executable part quoted; %u standalone and unquoted (--notify follows)
    assert exec_line.endswith(" nxm %u --notify")
    assert '"%' not in exec_line and '%u"' not in exec_line
    quoted = exec_line[len("Exec="):]
    assert quoted.startswith('"') and quoted.count('"') == 2  # only around the exe
    assert "\\$100%%" in quoted and "it's" in quoted and "my apps" in quoted


def test_entry_rejects_relative_executable():
    with pytest.raises(DesktopEntryError):
        build_desktop_entry_content("bin/nexus-dl")


# ---------------------------------------------------------------------------
# Install lifecycle
# ---------------------------------------------------------------------------

def test_fresh_install_writes_entry_and_registers(exe, cfg_path, tmp_path):
    runner = FakeRunner(query_default="")
    cfg = HandlerConfig(cfg_path)
    inst = DesktopInstaller(config=cfg, runner=runner,
                            entry_dir=tmp_path / "apps", executable=exe)
    entry = inst.install()

    assert entry == tmp_path / "apps" / DESKTOP_ENTRY_NAME
    assert entry.read_text() == build_desktop_entry_content(exe.resolve())
    assert runner.has_call("xdg-mime", "query", "default", NXM_MIME_TYPE)
    assert runner.has_call("xdg-mime", "default", DESKTOP_ENTRY_NAME, NXM_MIME_TYPE)
    assert runner.has_call("update-desktop-database", str(tmp_path / "apps"))
    # No previous default existed: recorded as None, never as ourselves
    assert cfg.desktop["previous_default"] is None
    assert cfg.desktop["installed"] is True
    assert cfg.desktop["executable"] == str(exe.resolve())
    assert cfg_path.exists()                      # config persisted


def test_install_saves_previous_default_once(exe, cfg_path, tmp_path):
    runner = FakeRunner(query_default="vortex-nxm.desktop")
    cfg = HandlerConfig(cfg_path)
    DesktopInstaller(config=cfg, runner=runner,
                     entry_dir=tmp_path / "apps", executable=exe).install()
    assert cfg.desktop["previous_default"] == "vortex-nxm.desktop"

    # Repeated install while we are already the default must NOT overwrite
    # the saved previous default with our own entry name.
    cfg2 = HandlerConfig.load(cfg_path)
    runner2 = FakeRunner(query_default=DESKTOP_ENTRY_NAME)
    DesktopInstaller(config=cfg2, runner=runner2,
                     entry_dir=tmp_path / "apps", executable=exe).install()
    assert cfg2.desktop["previous_default"] == "vortex-nxm.desktop"
    assert (tmp_path / "apps" / DESKTOP_ENTRY_NAME).exists()


def test_install_rolls_back_on_registration_failure(exe, cfg_path, tmp_path):
    runner = FakeRunner(query_default="vortex-nxm.desktop", fail={"xdg-mime"})
    cfg = HandlerConfig(cfg_path)
    inst = DesktopInstaller(config=cfg, runner=runner,
                            entry_dir=tmp_path / "apps", executable=exe)
    with pytest.raises(DesktopEntryError):
        inst.install()
    assert not (tmp_path / "apps" / DESKTOP_ENTRY_NAME).exists()  # entry removed
    assert not cfg_path.exists()                                  # nothing persisted


def test_install_rolls_back_when_xdg_mime_missing(exe, cfg_path, tmp_path):
    runner = FakeRunner(missing={"xdg-mime"})
    cfg = HandlerConfig(cfg_path)
    inst = DesktopInstaller(config=cfg, runner=runner,
                            entry_dir=tmp_path / "apps", executable=exe)
    with pytest.raises(DesktopEntryError, match="xdg-mime"):
        inst.install()
    assert not (tmp_path / "apps" / DESKTOP_ENTRY_NAME).exists()
    assert not cfg_path.exists()


def test_install_tolerates_missing_update_desktop_database(exe, cfg_path, tmp_path):
    runner = FakeRunner(missing={"update-desktop-database"})
    cfg = HandlerConfig(cfg_path)
    entry = DesktopInstaller(config=cfg, runner=runner,
                             entry_dir=tmp_path / "apps", executable=exe).install()
    assert entry.exists()
    assert cfg.desktop["installed"] is True


def test_install_rejects_unrepresentable_executable_before_writing(cfg_path, tmp_path):
    runner = FakeRunner()
    cfg = HandlerConfig(cfg_path)
    bad_exe = tmp_path / "bad\nnexus-dl"
    bad_exe.write_text("#!/bin/sh\n")
    inst = DesktopInstaller(config=cfg, runner=runner,
                            entry_dir=tmp_path / "apps", executable=bad_exe)
    with pytest.raises(DesktopEntryError):
        inst.install()
    assert runner.calls == []                     # nothing ran
    assert not (tmp_path / "apps").exists()       # nothing created
    assert not cfg_path.exists()


def test_resolve_executable_requires_real_absolute_path(tmp_path, monkeypatch, cfg_path):
    monkeypatch.setattr(handler_module.shutil, "which", lambda name: None)
    inst = DesktopInstaller(config=HandlerConfig(cfg_path), runner=FakeRunner(),
                            entry_dir=tmp_path / "apps")
    with pytest.raises(DesktopEntryError, match="PATH"):
        inst.resolve_executable()

    ghost = tmp_path / "moved-venv" / "bin" / "nexus-dl"
    inst2 = DesktopInstaller(config=HandlerConfig(cfg_path), runner=FakeRunner(),
                             entry_dir=tmp_path / "apps", executable=ghost)
    with pytest.raises(DesktopEntryError, match="not an existing file"):
        inst2.resolve_executable()


# ---------------------------------------------------------------------------
# Uninstall: restore only what is ours, never a later user choice
# ---------------------------------------------------------------------------

def _installed_env(tmp_path, cfg_path, previous=None):
    apps = tmp_path / "apps"
    apps.mkdir()
    entry = apps / DESKTOP_ENTRY_NAME
    entry.write_text("[Desktop Entry]\n")
    cfg = HandlerConfig(cfg_path)
    cfg.desktop = {"installed": True, "entry_path": str(entry),
                   "executable": "/usr/bin/nexus-dl", "previous_default": previous}
    return apps, entry, cfg


def test_uninstall_restores_previous_when_still_ours(tmp_path, cfg_path):
    _apps, entry, cfg = _installed_env(tmp_path, cfg_path, previous="vortex-nxm.desktop")
    runner = FakeRunner(query_default=DESKTOP_ENTRY_NAME)
    inst = DesktopInstaller(config=cfg, runner=runner, entry_dir=tmp_path / "apps")

    message = inst.uninstall()

    assert runner.has_call("xdg-mime", "default", "vortex-nxm.desktop", NXM_MIME_TYPE)
    assert not entry.exists()
    assert "restored" in message
    assert cfg.desktop == {}


def test_uninstall_never_overrides_later_user_choice(tmp_path, cfg_path):
    _apps, entry, cfg = _installed_env(tmp_path, cfg_path, previous="vortex-nxm.desktop")
    runner = FakeRunner(query_default="some-other-app.desktop")
    inst = DesktopInstaller(config=cfg, runner=runner, entry_dir=tmp_path / "apps")

    message = inst.uninstall()

    assert not runner.has_call("xdg-mime", "default", "vortex-nxm.desktop", NXM_MIME_TYPE)
    assert not any(c[:2] == ["xdg-mime", "default"] for c in runner.calls)
    assert not entry.exists()
    assert message == "NXM handler removed."


def test_uninstall_without_saved_previous_restores_nothing(tmp_path, cfg_path):
    _apps, entry, cfg = _installed_env(tmp_path, cfg_path, previous=None)
    runner = FakeRunner(query_default=DESKTOP_ENTRY_NAME)
    inst = DesktopInstaller(config=cfg, runner=runner, entry_dir=tmp_path / "apps")

    inst.uninstall()

    assert not any(c[:2] == ["xdg-mime", "default"] for c in runner.calls)
    assert not entry.exists()


def test_uninstall_restore_failure_retains_recovery_state(tmp_path, cfg_path):
    _apps, entry, cfg = _installed_env(tmp_path, cfg_path, previous="vortex-nxm.desktop")
    runner = FakeRunner(query_default=DESKTOP_ENTRY_NAME, fail={"xdg-mime"})
    inst = DesktopInstaller(config=cfg, runner=runner, entry_dir=tmp_path / "apps")

    with pytest.raises(DesktopEntryError):
        inst.uninstall()

    # Retryable: the entry stays and the saved previous default is intact.
    assert entry.exists()
    assert cfg.desktop["previous_default"] == "vortex-nxm.desktop"


def test_uninstall_when_not_installed_is_honest_noop(tmp_path, cfg_path):
    cfg = HandlerConfig(cfg_path)
    runner = FakeRunner(query_default="some-other-app.desktop")
    inst = DesktopInstaller(config=cfg, runner=runner, entry_dir=tmp_path / "apps")

    message = inst.uninstall()

    assert "not installed" in message
    assert not any(c[:2] == ["xdg-mime", "default"] for c in runner.calls)


def test_uninstall_without_xdg_mime_removes_entry_only(tmp_path, cfg_path):
    _apps, entry, cfg = _installed_env(tmp_path, cfg_path, previous="vortex-nxm.desktop")
    runner = FakeRunner(missing={"xdg-mime"})
    inst = DesktopInstaller(config=cfg, runner=runner, entry_dir=tmp_path / "apps")

    # Current default is unknown (query unavailable): remove our entry but
    # do not attempt any default change.
    message = inst.uninstall()

    assert not entry.exists()
    assert not any(c[:2] == ["xdg-mime", "default"] for c in runner.calls)
    assert message == "NXM handler removed."


# ---------------------------------------------------------------------------
# Status diagnostics
# ---------------------------------------------------------------------------

def test_status_reports_installed_default_and_executable(tmp_path, cfg_path, exe):
    _apps, _entry, cfg = _installed_env(tmp_path, cfg_path, previous="vortex-nxm.desktop")
    cfg.desktop["executable"] = str(exe)
    runner = FakeRunner(query_default=DESKTOP_ENTRY_NAME)
    inst = DesktopInstaller(config=cfg, runner=runner, entry_dir=tmp_path / "apps")

    info = inst.status()

    assert info["entry_installed"] is True
    assert info["is_default"] is True
    assert info["current_default"] == DESKTOP_ENTRY_NAME
    assert info["executable_exists"] is True
    assert info["previous_default"] == "vortex-nxm.desktop"


def test_status_flags_moved_executable(tmp_path, cfg_path):
    _apps, _entry, cfg = _installed_env(tmp_path, cfg_path)
    cfg.desktop["executable"] = str(tmp_path / "gone" / "venv" / "bin" / "nexus-dl")
    runner = FakeRunner(query_default=DESKTOP_ENTRY_NAME)

    info = DesktopInstaller(config=cfg, runner=runner,
                            entry_dir=tmp_path / "apps").status()

    assert info["executable_exists"] is False   # venv moved: reinstall needed


def test_status_without_xdg_mime(tmp_path, cfg_path, monkeypatch):
    monkeypatch.setattr(handler_module.shutil, "which", lambda name: None)
    cfg = HandlerConfig(cfg_path)
    runner = FakeRunner(missing={"xdg-mime"})

    info = DesktopInstaller(config=cfg, runner=runner,
                            entry_dir=tmp_path / "apps").status()

    assert info["xdg_mime_available"] is False
    assert info["current_default"] is None
    assert info["is_default"] is False


# ---------------------------------------------------------------------------
# Notifications (mocked; sanitized)
# ---------------------------------------------------------------------------

def test_notify_uses_argv_and_sanitizes_body(monkeypatch):
    monkeypatch.setattr(handler_module.shutil, "which",
                        lambda name: "/usr/bin/notify-send" if name == "notify-send" else None)
    runner = FakeRunner()
    ok = notify("nexus-dl: download complete",
                "Saved Mod.zip from https://cdn.example/x?key=SECRET123",
                runner=runner)
    assert ok is True
    assert runner.calls == [[
        "notify-send", "-a", "nexus-dl", "--", "nexus-dl: download complete",
        "Saved Mod.zip from [url redacted]",
    ]]


def test_notify_absent_tool_is_silent_false(monkeypatch):
    monkeypatch.setattr(handler_module.shutil, "which", lambda name: None)
    runner = FakeRunner()
    assert notify("sum", "body", runner=runner) is False
    assert runner.calls == []


def test_notify_failure_never_raises(monkeypatch):
    monkeypatch.setattr(handler_module.shutil, "which",
                        lambda name: "/usr/bin/notify-send" if name == "notify-send" else None)
    runner = FakeRunner(missing={"notify-send"})
    assert notify("sum", "body", runner=runner) is False


# ---------------------------------------------------------------------------
# Mappings and routing: validation, no writes on failure
# ---------------------------------------------------------------------------

def test_add_mapping_rejects_invalid_game(tmp_path, cfg_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_path.parent))
    cdir = make_collection(tmp_path)
    with pytest.raises(HandlerError):
        add_mapping("bad_game!", cdir)
    assert not cfg_path.exists()


def test_add_mapping_requires_synced_collection(tmp_path, cfg_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_path.parent))
    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(HandlerError, match="sync"):
        add_mapping(GAME, bare)
    assert not cfg_path.exists()               # failure writes nothing


def test_add_mapping_rejects_game_mismatch(tmp_path, cfg_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_path.parent))
    cdir = make_collection(tmp_path, game="starfield")
    with pytest.raises(HandlerError, match="starfield"):
        add_mapping(GAME, cdir)
    assert not cfg_path.exists()


def test_mapping_roundtrip_idempotent(tmp_path, cfg_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_path.parent))
    cdir = make_collection(tmp_path)
    add_mapping(GAME, cdir)
    add_mapping(GAME, cdir)                    # idempotent
    cfg = HandlerConfig.load()
    assert cfg.mappings[GAME] == [str(cdir)]
    assert remove_mapping(GAME) is True
    assert remove_mapping(GAME) is False
    assert HandlerConfig.load().mappings == {}


def test_resolve_explicit_dir_validation(tmp_path, monkeypatch, cfg_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_path.parent))
    cdir = make_collection(tmp_path)
    assert resolve_collection_dir(GAME, cdir) == cdir

    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(NxmRoutingError, match="sync"):
        resolve_collection_dir(GAME, bare)
    assert list(bare.iterdir()) == []          # no state/lock created

    other = make_collection(tmp_path, game="starfield", name="other")
    with pytest.raises(NxmRoutingError, match="starfield"):
        resolve_collection_dir(GAME, other)


def test_resolve_unmapped_creates_nothing(tmp_path, monkeypatch, cfg_path):
    config_home = cfg_path.parent
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    with pytest.raises(NxmRoutingError, match="handler map"):
        resolve_collection_dir(GAME)
    assert not config_home.exists()            # no config file or dirs created


def test_resolve_single_and_ambiguous_mappings(tmp_path, monkeypatch, cfg_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_path.parent))
    cdir_a = make_collection(tmp_path, name="col-a")
    add_mapping(GAME, cdir_a)
    assert resolve_collection_dir(GAME) == cdir_a

    cdir_b = make_collection(tmp_path, name="col-b")
    add_mapping(GAME, cdir_b)
    with pytest.raises(NxmRoutingError) as excinfo:
        resolve_collection_dir(GAME)
    message = str(excinfo.value)
    assert str(cdir_a) in message and str(cdir_b) in message
    assert "--mods-dir" in message


def test_resolve_filters_stale_mapping(tmp_path, monkeypatch, cfg_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_path.parent))
    cdir = make_collection(tmp_path)
    add_mapping(GAME, cdir)
    (cdir / ".nexus-state.json").unlink()      # collection vanished
    with pytest.raises(NxmRoutingError, match="handler map"):
        resolve_collection_dir(GAME)


# ---------------------------------------------------------------------------
# XDG awareness
# ---------------------------------------------------------------------------

def test_xdg_dirs_respect_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert config_dir() == tmp_path / "cfg" / "nexus-dl"
    assert applications_dir() == tmp_path / "data" / "applications"
    assert handler_module.state_dir() == tmp_path / "state" / "nexus-dl"
    assert handler_module.nxm_log_path() == (
        tmp_path / "state" / "nexus-dl" / "nxm.log"
    )


def test_xdg_dirs_default_to_home(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config_dir() == tmp_path / ".config" / "nexus-dl"
    assert applications_dir() == tmp_path / ".local" / "share" / "applications"
    assert handler_module.state_dir() == (
        tmp_path / ".local" / "state" / "nexus-dl"
    )


# ---------------------------------------------------------------------------
# desktop-file-validate against generated entries (task-scoped, not live)
# ---------------------------------------------------------------------------

HAS_VALIDATOR = shutil.which("desktop-file-validate") is not None


@pytest.mark.skipif(not HAS_VALIDATOR, reason="desktop-file-validate not installed")
def test_generated_entry_passes_desktop_file_validate(tmp_path, exe):
    entry = tmp_path / DESKTOP_ENTRY_NAME
    entry.write_text(build_desktop_entry_content(exe))
    proc = subprocess.run(
        ["desktop-file-validate", str(entry)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.skipif(not HAS_VALIDATOR, reason="desktop-file-validate not installed")
def test_tricky_executable_entry_passes_desktop_file_validate(tmp_path):
    tricky = tmp_path / "my apps" / "it's $100% & co" / "nexus-dl"
    tricky.parent.mkdir(parents=True)
    tricky.write_text("#!/bin/sh\n")
    entry = tmp_path / DESKTOP_ENTRY_NAME
    entry.write_text(build_desktop_entry_content(tricky))
    proc = subprocess.run(
        ["desktop-file-validate", str(entry)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# Bugcheck regressions: canonical mappings, subprocess timeouts, X_OK,
# single status query
# ---------------------------------------------------------------------------

import json  # noqa: E402
import os  # noqa: E402


class TimeoutRunner:
    """argv runner that times out on 'xdg-mime default' registration calls."""

    def __init__(self, query_default=""):
        self.calls = []
        self.query_default = query_default

    def __call__(self, argv):
        assert isinstance(argv, (list, tuple))
        assert all(isinstance(a, str) for a in argv)
        self.calls.append(list(argv))
        if argv[:2] == ["xdg-mime", "default"]:
            raise subprocess.TimeoutExpired(cmd=argv[0], timeout=30)
        if argv[:2] == ["xdg-mime", "query"]:
            return SimpleNamespace(returncode=0, stdout=self.query_default, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_install_timeout_rolls_back_with_domain_error(exe, cfg_path, tmp_path):
    runner = TimeoutRunner(query_default="vortex-nxm.desktop")
    cfg = HandlerConfig(cfg_path)
    inst = DesktopInstaller(config=cfg, runner=runner,
                            entry_dir=tmp_path / "apps", executable=exe)
    with pytest.raises(DesktopEntryError):
        inst.install()
    assert not (tmp_path / "apps" / DESKTOP_ENTRY_NAME).exists()
    assert not cfg_path.exists()                    # nothing persisted


def test_uninstall_restore_timeout_keeps_recovery_state(tmp_path, cfg_path):
    _apps, entry, cfg = _installed_env(tmp_path, cfg_path, previous="vortex-nxm.desktop")
    runner = TimeoutRunner(query_default=DESKTOP_ENTRY_NAME)
    inst = DesktopInstaller(config=cfg, runner=runner, entry_dir=tmp_path / "apps")
    with pytest.raises(DesktopEntryError):
        inst.uninstall()
    assert entry.exists()                           # retryable
    assert cfg.desktop["previous_default"] == "vortex-nxm.desktop"


def test_install_rejects_non_executable_file(cfg_path, tmp_path):
    bad = tmp_path / "notexe"
    bad.write_text("#!/bin/sh\n")
    bad.chmod(0o644)
    inst = DesktopInstaller(config=HandlerConfig(cfg_path), runner=FakeRunner(),
                            entry_dir=tmp_path / "apps", executable=bad)
    with pytest.raises(DesktopEntryError, match="not executable"):
        inst.install()
    assert not cfg_path.exists()


def test_status_queries_default_exactly_once(tmp_path, cfg_path):
    _apps, _entry, cfg = _installed_env(tmp_path, cfg_path)
    runner = FakeRunner(query_default=DESKTOP_ENTRY_NAME)
    info = DesktopInstaller(config=cfg, runner=runner,
                            entry_dir=tmp_path / "apps").status()
    queries = [c for c in runner.calls if c[:2] == ["xdg-mime", "query"]]
    assert len(queries) == 1
    assert info["is_default"] is True


def test_relative_mapping_resolves_against_map_time_cwd(tmp_path, cfg_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_path.parent))
    cdir = make_collection(tmp_path)
    cwd_a = tmp_path / "cwd-a"
    cwd_a.mkdir()
    monkeypatch.chdir(cwd_a)
    add_mapping(GAME, os.path.relpath(cdir, cwd_a))
    stored = HandlerConfig.load().mappings[GAME][0]
    assert os.path.isabs(stored)
    assert Path(stored) == cdir.resolve()
    # A desktop launch from an unrelated cwd must hit the same target
    cwd_b = tmp_path / "cwd-b"
    cwd_b.mkdir()
    monkeypatch.chdir(cwd_b)
    assert resolve_collection_dir(GAME) == Path(stored)


def test_relative_config_entries_are_dropped(tmp_path, cfg_path, monkeypatch):
    config_home = cfg_path.parent
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    # HandlerConfig.load() reads the XDG default location, not the
    # explicit fixture path: config_home/nexus-dl/<CONFIG_FILENAME>.
    default_cfg = config_home / "nexus-dl" / CONFIG_FILENAME
    default_cfg.parent.mkdir(parents=True, exist_ok=True)
    default_cfg.write_text(json.dumps(
        {"version": 1, "mappings": {GAME: ["relative/dir"]}, "desktop": {}}
    ))
    assert HandlerConfig.load().mappings[GAME] == []
    make_collection(tmp_path)                       # valid, but not validly mapped
    with pytest.raises(NxmRoutingError, match="handler map"):
        resolve_collection_dir(GAME)


# ---------------------------------------------------------------------------
# nxm status log: XDG state dir, 0600, redacted by construction, best-effort
# ---------------------------------------------------------------------------

def test_append_nxm_log_writes_0600_line(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    ok = handler_module.append_nxm_log(
        "success", "mod=1 file=2 status=downloaded"
    )
    assert ok is True
    log_file = tmp_path / "state" / "nexus-dl" / "nxm.log"
    assert log_file.exists()
    assert log_file.stat().st_mode & 0o777 == 0o600
    # Per-app state directory is private (no group/other bits).
    assert log_file.parent.stat().st_mode & 0o077 == 0
    lines = log_file.read_text().splitlines()
    assert len(lines) == 1
    assert " success mod=1 file=2 status=downloaded" in lines[0]
    # UTC timestamp prefix
    assert lines[0][:4].isdigit() and "Z success" in lines[0]


def test_append_nxm_log_appends_without_clobbering(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    handler_module.append_nxm_log("success", "mod=1 file=2")
    handler_module.append_nxm_log("error", "reason=api")
    lines = (tmp_path / "state" / "nexus-dl" / "nxm.log").read_text().splitlines()
    assert len(lines) == 2
    assert " success " in lines[0]
    assert " error reason=api" in lines[1]


def test_append_nxm_log_strips_urls_and_restricts_charset(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    handler_module.append_nxm_log(
        "error",
        "reason=x https://evil.example/?key=SECRETVALUE1 a[b]c\nd=e",
    )
    content = (tmp_path / "state" / "nexus-dl" / "nxm.log").read_text()
    assert "evil.example" not in content
    assert "SECRETVALUE1" not in content
    assert "[" not in content and "]" not in content
    # An injected newline cannot forge a second log line.
    assert content.count("\n") == 1


def test_append_nxm_log_coerces_unknown_outcome(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    handler_module.append_nxm_log("bogus", "reason=x")
    content = (tmp_path / "state" / "nexus-dl" / "nxm.log").read_text()
    assert " error " in content
    assert "bogus" not in content


def test_append_nxm_log_best_effort_on_os_failure(tmp_path, monkeypatch):
    """A state dir that cannot be created returns False, never raises."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocker))
    assert handler_module.append_nxm_log("success", "mod=1 file=2") is False


# ---------------------------------------------------------------------------
# Empty game_domain states fail closed: never a wildcard for any game
# ---------------------------------------------------------------------------

def make_empty_domain_collection(tmp_path, name="empty"):
    """A collection dir whose state carries no game domain."""
    cdir = tmp_path / name
    cdir.mkdir()
    state = CollectionState(cdir)
    state.collection_name = "No Domain"
    state.save()                     # game_domain stays ""
    return cdir


def test_add_mapping_rejects_empty_game_domain(tmp_path, cfg_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_path.parent))
    cdir = make_empty_domain_collection(tmp_path)
    with pytest.raises(HandlerError) as excinfo:
        add_mapping(GAME, cdir)
    assert "no game domain" in str(excinfo.value)
    assert "nexus-dl sync" in str(excinfo.value)   # actionable
    assert not cfg_path.exists()                   # failure writes nothing


def test_resolve_explicit_dir_rejects_empty_game_domain(tmp_path, monkeypatch, cfg_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_path.parent))
    cdir = make_empty_domain_collection(tmp_path)
    with pytest.raises(NxmRoutingError) as excinfo:
        resolve_collection_dir(GAME, cdir)
    assert "no game domain" in str(excinfo.value)
    assert "nexus-dl sync" in str(excinfo.value)


def test_resolve_mapped_empty_game_domain_rejected(tmp_path, monkeypatch, cfg_path):
    """A mapped collection whose state later loses its domain must not
    silently accept links for any game."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_path.parent))
    cdir = make_collection(tmp_path)
    add_mapping(GAME, cdir)
    state = CollectionState(cdir)
    state.load()
    state.game_domain = ""
    state.save()
    with pytest.raises(NxmRoutingError) as excinfo:
        resolve_collection_dir(GAME)
    assert "no game domain" in str(excinfo.value)
    assert str(cdir) in str(excinfo.value)         # says which dir to fix


# ---------------------------------------------------------------------------
# Notification hardening: bounded notify, honest results, daemon probing
# ---------------------------------------------------------------------------

def _notify_send_only(monkeypatch):
    monkeypatch.setattr(
        handler_module.shutil, "which",
        lambda name: "/usr/bin/notify-send" if name == "notify-send" else None,
    )


def test_notify_nonzero_exit_reports_false(monkeypatch):
    """A daemon that rejects the notification is reported honestly."""
    _notify_send_only(monkeypatch)
    runner = FakeRunner(fail={"notify-send"})
    assert notify("sum", "body", runner=runner) is False


def test_notify_hang_is_time_bounded_and_false(monkeypatch):
    """A hanging notifier never blocks the caller beyond the timeout."""
    _notify_send_only(monkeypatch)

    def hanging_runner(argv):
        raise subprocess.TimeoutExpired(
            cmd=argv[0], timeout=handler_module.NOTIFY_TIMEOUT
        )

    assert notify("sum", "body", runner=hanging_runner) is False
    assert handler_module.NOTIFY_TIMEOUT <= 3          # hard, short bound


def test_notify_default_runner_uses_hard_timeout(monkeypatch):
    _notify_send_only(monkeypatch)
    seen = {}

    def recording_run(argv, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(handler_module.subprocess, "run", recording_run)
    assert notify("sum") is True
    assert seen["timeout"] == handler_module.NOTIFY_TIMEOUT
    assert seen["shell"] is False


def test_daemon_probe_gdbus_owner_found(monkeypatch):
    monkeypatch.setattr(
        handler_module.shutil, "which",
        lambda name: "/usr/bin/gdbus" if name == "gdbus" else None,
    )
    runner = FakeRunner()                              # exit 0
    assert handler_module.notification_daemon_available(runner=runner) is True
    call = runner.calls[0]
    assert call[0] == "/usr/bin/gdbus"
    assert "org.freedesktop.DBus.GetNameOwner" in call
    assert "org.freedesktop.Notifications" in call


def test_daemon_probe_gdbus_no_owner(monkeypatch):
    monkeypatch.setattr(
        handler_module.shutil, "which",
        lambda name: "/usr/bin/gdbus" if name == "gdbus" else None,
    )
    runner = FakeRunner(fail={"/usr/bin/gdbus"})       # name unowned
    assert handler_module.notification_daemon_available(runner=runner) is False


def test_daemon_probe_falls_back_to_dbus_send(monkeypatch):
    monkeypatch.setattr(
        handler_module.shutil, "which",
        lambda name: "/usr/bin/dbus-send" if name == "dbus-send" else None,
    )
    runner = FakeRunner(fail={"/usr/bin/dbus-send"})
    assert handler_module.notification_daemon_available(runner=runner) is False
    assert runner.calls[0][0] == "/usr/bin/dbus-send"


def test_daemon_probe_notify_send_last_resort(monkeypatch):
    _notify_send_only(monkeypatch)
    assert handler_module.notification_daemon_available(
        runner=FakeRunner()
    ) is True
    assert handler_module.notification_daemon_available(
        runner=FakeRunner(fail={"notify-send"})
    ) is False


def test_daemon_probe_no_tooling_is_none(monkeypatch):
    """Missing probe tools skip silently: never an install failure."""
    monkeypatch.setattr(handler_module.shutil, "which", lambda name: None)
    assert handler_module.notification_daemon_available(
        runner=FakeRunner()
    ) is None


def test_daemon_probe_broken_tool_skips_silently(monkeypatch):
    monkeypatch.setattr(
        handler_module.shutil, "which",
        lambda name: "/usr/bin/gdbus" if name == "gdbus" else None,
    )

    def exploding(argv):
        raise subprocess.TimeoutExpired(cmd=argv[0], timeout=3)

    # gdbus times out, no other tool exists: no answer, no failure
    assert handler_module.notification_daemon_available(runner=exploding) is None


# ---------------------------------------------------------------------------
# Install idempotence: a stale entry from an older version is rewritten
# ---------------------------------------------------------------------------

def test_install_rerun_rewrites_stale_entry(exe, cfg_path, tmp_path):
    apps = tmp_path / "apps"
    apps.mkdir()
    stale = apps / DESKTOP_ENTRY_NAME
    stale.write_text(
        "[Desktop Entry]\nType=Application\n"
        "Exec=/old/venv/bin/nexus-dl nxm %u\nTerminal=true\n"
    )
    runner = FakeRunner(query_default=DESKTOP_ENTRY_NAME)
    cfg = HandlerConfig(cfg_path)
    cfg.desktop = {"installed": True, "entry_path": str(stale),
                   "executable": "/old/venv/bin/nexus-dl",
                   "previous_default": None}
    inst = DesktopInstaller(config=cfg, runner=runner,
                            entry_dir=apps, executable=exe)

    entry = inst.install()

    content = entry.read_text()
    assert content == build_desktop_entry_content(exe.resolve())
    assert "Terminal=true" not in content              # stale shape gone
    assert "Terminal=false" in content
    assert "--notify" in content
    assert "/old/venv" not in content
    # The saved previous default survives the reinstall untouched
    assert cfg.desktop["previous_default"] is None


# ---------------------------------------------------------------------------
# C2: sanitize_message strips any-scheme URIs carrying a query
# ---------------------------------------------------------------------------

def test_sanitize_message_strips_nxm_uri_with_query():
    text = "boom nxm://skyrimspecialedition/mods/1/files/2?key=SECRETVALUE&expires=9 end"
    out = handler_module.sanitize_message(text)
    assert "SECRETVALUE" not in out
    assert "nxm://" not in out
    assert "key=" not in out
    assert out == "boom [url redacted] end"


def test_sanitize_message_strips_bare_nxm_mentions():
    # Security-driven change (supersedes the earlier "bare nxm:// kept"
    # expectation): nxm:// URIs are stripped by scheme whether or not
    # they carry a query, because the query is where link credentials
    # live and no part of a raw link is worth the risk of a near-miss.
    text = "Collection nxm:// links are not supported."
    out = handler_module.sanitize_message(text)
    assert "nxm://" not in out
    assert out == "Collection [url redacted] links are not supported."


def test_sanitize_message_keeps_other_bare_scheme_mentions():
    text = "an ftp:// mirror and a steam://game link"
    assert handler_module.sanitize_message(text) == text


def test_sanitize_message_strips_http_urls_as_before():
    assert handler_module.sanitize_message(
        "see https://x.example/page and http://y.example"
    ) == "see [url redacted] and [url redacted]"


# ---------------------------------------------------------------------------
# C4: a failed reinstall over a working installation preserves it
# ---------------------------------------------------------------------------

class FailOnDefaultRunner(FakeRunner):
    """Query succeeds; the 'xdg-mime default' registration step fails."""

    def __call__(self, argv):
        if argv[:2] == ["xdg-mime", "default"]:
            self.calls.append(list(argv))
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        return super().__call__(argv)


def test_reinstall_failure_preserves_working_installation(exe, cfg_path, tmp_path):
    apps = tmp_path / "apps"
    apps.mkdir()
    entry_file = apps / DESKTOP_ENTRY_NAME
    prior_content = build_desktop_entry_content("/usr/bin/nexus-dl")
    entry_file.write_text(prior_content)
    cfg = HandlerConfig(cfg_path)
    cfg.desktop = {"installed": True, "entry_path": str(entry_file),
                   "executable": "/usr/bin/nexus-dl",
                   "previous_default": "vortex-nxm.desktop"}
    runner = FailOnDefaultRunner(query_default=DESKTOP_ENTRY_NAME)
    inst = DesktopInstaller(config=cfg, runner=runner,
                            entry_dir=apps, executable=exe)

    with pytest.raises(DesktopEntryError):
        inst.install()

    # The working entry is restored to its prior contents, not deleted
    assert entry_file.exists()
    assert entry_file.read_text() == prior_content
    # The MIME default was NOT flipped to the recorded previous handler:
    # every 'xdg-mime default' call targets our entry (left as found).
    default_calls = [c for c in runner.calls if c[:2] == ["xdg-mime", "default"]]
    assert default_calls
    assert all(c[2] == DESKTOP_ENTRY_NAME for c in default_calls)
    assert "vortex-nxm.desktop" not in [c[2] for c in default_calls]
    # Nothing persisted from the failed run
    assert not cfg_path.exists()


def test_fresh_install_failure_still_removes_entry(exe, cfg_path, tmp_path):
    """Regression pin: with no prior entry, rollback still deletes the
    entry this run created and restores the default found at start."""
    runner = FakeRunner(query_default="vortex-nxm.desktop", fail={"xdg-mime"})
    cfg = HandlerConfig(cfg_path)
    inst = DesktopInstaller(config=cfg, runner=runner,
                            entry_dir=tmp_path / "apps", executable=exe)
    with pytest.raises(DesktopEntryError):
        inst.install()
    assert not (tmp_path / "apps" / DESKTOP_ENTRY_NAME).exists()
    assert not cfg_path.exists()


# ---------------------------------------------------------------------------
# D1/D3: rollback never destroys what it cannot snapshot and never strands
# the MIME default pointing at a deleted entry
# ---------------------------------------------------------------------------

def test_install_aborts_when_existing_entry_unreadable(exe, cfg_path, tmp_path,
                                                       monkeypatch):
    """A pre-existing entry that cannot be read aborts the install before
    ANY mutation: never replace what cannot be snapshotted for rollback."""
    apps = tmp_path / "apps"
    apps.mkdir()
    entry_file = apps / DESKTOP_ENTRY_NAME
    entry_file.write_bytes(b"prior bytes on disk")

    real_read_text = Path.read_text

    def unreadable(self, *args, **kwargs):
        if self == entry_file:
            raise PermissionError(13, "Permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)

    runner = FakeRunner(query_default=DESKTOP_ENTRY_NAME)
    cfg = HandlerConfig(cfg_path)
    inst = DesktopInstaller(config=cfg, runner=runner,
                            entry_dir=apps, executable=exe)

    with pytest.raises(DesktopEntryError, match="could not"):
        inst.install()

    assert entry_file.read_bytes() == b"prior bytes on disk"   # untouched
    # Nothing was changed: no default registration, no config write
    assert not [c for c in runner.calls if c[:2] == ["xdg-mime", "default"]]
    assert not cfg_path.exists()


def test_failed_install_without_prior_default_keeps_registered_entry(
    exe, cfg_path, tmp_path, monkeypatch
):
    """Registration succeeded but the config save failed and there was no
    prior default: the entry must be KEPT, because the MIME default now
    points at it and xdg-mime cannot unset a default. Deleting it would
    strand a dangling registration with no working handler."""
    runner = FakeRunner(query_default="")            # nothing registered before
    cfg = HandlerConfig(cfg_path)

    def fail_save(self):
        raise HandlerError("Could not write handler config: disk on fire")

    monkeypatch.setattr(HandlerConfig, "save", fail_save)
    inst = DesktopInstaller(config=cfg, runner=runner,
                            entry_dir=tmp_path / "apps", executable=exe)

    with pytest.raises(DesktopEntryError) as excinfo:
        inst.install()

    entry = tmp_path / "apps" / DESKTOP_ENTRY_NAME
    assert entry.exists()                            # kept, not stranded
    assert entry.read_text() == build_desktop_entry_content(exe.resolve())
    message = str(excinfo.value)
    assert "KEPT" in message                         # honest about the state
    assert "disk on fire" in message                 # real failure disclosed
    assert "handler uninstall" in message            # way out is named
    # The default was registered as ours and never flipped elsewhere
    assert runner.has_call("xdg-mime", "default", DESKTOP_ENTRY_NAME,
                           NXM_MIME_TYPE)
    assert not [c for c in runner.calls
                if c[:2] == ["xdg-mime", "default"]
                and c[2] != DESKTOP_ENTRY_NAME]
    assert not cfg_path.exists()                     # config not saved
