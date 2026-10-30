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
    assert exec_line == f"Exec={exe} nxm %u"
    assert f"MimeType={NXM_MIME_TYPE};" in content
    assert "[Desktop Entry]" in content
    assert "Type=Application" in content


def test_entry_content_percent_u_unquoted_with_quoted_exe(tmp_path):
    tricky = tmp_path / "my apps" / "it's $100%" / "nexus-dl"
    tricky.parent.mkdir(parents=True)
    tricky.write_text("#!/bin/sh\n")
    content = build_desktop_entry_content(tricky)
    exec_line = next(l for l in content.splitlines() if l.startswith("Exec="))
    # Executable part quoted; %u standalone, unquoted, last token
    assert exec_line.endswith(" nxm %u")
    assert '"%' not in exec_line and "%u\"" not in exec_line
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
    assert config_dir() == tmp_path / "cfg" / "nexus-dl"
    assert applications_dir() == tmp_path / "data" / "applications"


def test_xdg_dirs_default_to_home(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config_dir() == tmp_path / ".config" / "nexus-dl"
    assert applications_dir() == tmp_path / ".local" / "share" / "applications"


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
