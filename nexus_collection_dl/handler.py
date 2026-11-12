"""NXM desktop handler: game mappings, routing, and .desktop integration.

Configuration lives under the user's XDG config directory
(``~/.config/nexus-dl/handler.json`` unless ``XDG_CONFIG_HOME`` says
otherwise). The desktop entry, when the user opts in with
``nexus-dl handler install``, lives under the user's XDG data directory
(``~/.local/share/applications`` unless ``XDG_DATA_HOME`` says otherwise).

Nothing here writes system-wide configuration, touches shell startup
files, or runs a registration subprocess unless the user explicitly runs
the install command. All subprocesses run as argv lists (never through a
shell), and all user-visible messages are safe to show on a desktop:
they carry ids, paths, and status only, never raw nxm:// URIs or
credentials.
"""

import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .state import STATE_FILENAME, CollectionState, StateError

CONFIG_DIR_NAME = "nexus-dl"
CONFIG_FILENAME = "handler.json"
DESKTOP_ENTRY_NAME = "nexus-dl-nxm.desktop"
NXM_MIME_TYPE = "x-scheme-handler/nxm"
SUBPROCESS_TIMEOUT = 30

_GAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")
_URL_RE = re.compile(r"https?://\S+")
# nxm:// URIs are stripped by scheme, with or without a query: the
# credentials normally ride in the query, but a query-less URI still
# leaks account/mod identity and a hostile message could carry them in
# the path or userinfo. Case-insensitive: URI schemes are. The negative
# lookbehind anchors at a scheme-token boundary, so a different scheme
# whose name merely ends in "nxm" (e.g. foo-nxm://) is left alone.
_NXM_URI_RE = re.compile(r"(?<![A-Za-z0-9+.-])nxm://\S*", re.IGNORECASE)
# Any-scheme URI carrying a query string: a raw link embeds its
# credentials there, so scheme-agnostic stripping is needed wherever
# free-form exception text can reach the user (the http(s) pattern
# above never matched non-http schemes).
_SCHEME_URI_QUERY_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://\S*\?\S*")

# Characters the Desktop Entry Specification reserves in Exec values.
_EXEC_RESERVED = set(" \t\n\"'\\><~|&;$*?#()`")


class HandlerError(Exception):
    """Base error for handler configuration and desktop integration."""


class NxmRoutingError(HandlerError):
    """Raised when an nxm:// link cannot be routed to a collection dir."""


class DesktopEntryError(HandlerError):
    """Raised when desktop handler install/uninstall/status fails."""


def sanitize_message(text: str) -> str:
    """Strip URL-looking text so signed links never reach logs or notify.

    Three patterns are removed: http(s) URLs (query or not), nxm:// URIs
    in ANY form (with or without a query), and any other scheme's URI
    that carries a query string. A bare non-nxm scheme mention without a
    query ("an ftp:// mirror") is left intact; nxm:// is stripped
    wholesale because its query is exactly where link credentials live
    and no part of a raw link is worth showing.
    """
    out = _URL_RE.sub("[url redacted]", str(text))
    out = _NXM_URI_RE.sub("[url redacted]", out)
    return _SCHEME_URI_QUERY_RE.sub("[url redacted]", out)


def config_dir() -> Path:
    """XDG-aware config directory for nexus-dl."""
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / CONFIG_DIR_NAME
    return Path.home() / ".config" / CONFIG_DIR_NAME


def applications_dir() -> Path:
    """XDG-aware user applications directory for .desktop entries."""
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / "applications"
    return Path.home() / ".local" / "share" / "applications"


def state_dir() -> Path:
    """XDG-aware state directory for nexus-dl (per-user logs)."""
    base = os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base) / CONFIG_DIR_NAME
    return Path.home() / ".local" / "state" / CONFIG_DIR_NAME


NXM_LOG_FILENAME = "nxm.log"
_NXM_LOG_OUTCOMES = frozenset({"success", "warn", "error"})
_NXM_LOG_MAX_DETAIL = 120
# Detail text is rebuilt from this bounded charset: alphanumerics plus a
# few separators. Anything else (quotes, brackets, percent signs,
# newlines, decoded credential bytes) becomes '_', so a log line can
# never carry a URI fragment or secret even if a caller passes one.
_NXM_LOG_DETAIL_EXTRA = " =-_."


def nxm_log_path() -> Path:
    """Location of the browser-launch status log."""
    return state_dir() / NXM_LOG_FILENAME


def append_nxm_log(outcome: str, detail: str = "") -> bool:
    """Append one redacted status line to the nxm log. Best-effort.

    Browser-launched downloads may have no visible terminal, so the log
    gives a minimal after-the-fact trail of what a click did. A line is
    a UTC timestamp, an outcome token (success/warn/error; anything
    else is recorded as error), and a short safe detail built from
    caller-provided mod/file ids or static reason codes. Detail text is
    URL-stripped and charset-restricted before the write: never a raw
    URI, query value, or credential. Returns True when the line was
    written; every failure returns False instead of raising, so logging
    can never fail a download.
    """
    token = outcome if outcome in _NXM_LOG_OUTCOMES else "error"
    cleaned = sanitize_message(str(detail))
    safe = "".join(
        c if (c.isalnum() or c in _NXM_LOG_DETAIL_EXTRA) else "_"
        for c in cleaned
    )[:_NXM_LOG_MAX_DETAIL].strip()
    line = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line += f" {token}"
    if safe:
        line += f" {safe}"
    line += "\n"
    path = nxm_log_path()
    try:
        # XDG convention: the per-app state directory is private.
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # O_APPEND keeps concurrent clicks from interleaving partial
        # lines; the explicit fchmod holds 0600 even if the file
        # already existed with looser permissions.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        # Best-effort by contract: a broken log must never break nxm.
        return False
    return True


def _atomic_write_text(path: Path, content: str) -> None:
    """Write a text file atomically (temp file + fsync + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


class HandlerConfig:
    """Persistent handler configuration: game mappings + desktop record."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else config_dir() / CONFIG_FILENAME
        self.mappings: dict[str, list[str]] = {}
        self.desktop: dict[str, Any] = {}

    @classmethod
    def load(cls, path: Path | None = None) -> "HandlerConfig":
        """Load config from disk; a missing file yields empty config."""
        cfg = cls(path)
        try:
            raw = cfg.path.read_text()
        except FileNotFoundError:
            return cfg
        except OSError as e:
            raise HandlerError(f"Could not read handler config {cfg.path}: {e}")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise HandlerError(f"Handler config {cfg.path} is corrupt: {e}")
        if not isinstance(data, dict):
            raise HandlerError(f"Handler config {cfg.path} is corrupt.")
        mappings = data.get("mappings")
        if isinstance(mappings, dict):
            for game, dirs in mappings.items():
                if isinstance(game, str) and isinstance(dirs, list):
                    cfg.mappings[game.strip().lower()] = [
                        # Absolute paths only: a relative entry (from a
                        # hand-edited or pre-canonicalization config) would
                        # resolve against whatever cwd the desktop-launched
                        # handler happens to run in, so it is dropped and
                        # the game reads as unmapped instead.
                        d for d in dirs
                        if isinstance(d, str) and Path(d).is_absolute()
                    ]
        desktop = data.get("desktop")
        if isinstance(desktop, dict):
            cfg.desktop = desktop
        return cfg

    def save(self) -> None:
        """Persist config atomically."""
        data = {"version": 1, "mappings": self.mappings, "desktop": self.desktop}
        _atomic_write_text(self.path, json.dumps(data, indent=2) + "\n")


def _load_collection_state(directory: Path) -> CollectionState:
    """Load a collection state, converting failures into HandlerError."""
    state = CollectionState(directory)
    try:
        state.load()
    except StateError as e:
        raise HandlerError(f"Could not read collection state in {directory}: {e}")
    return state


def add_mapping(game_domain: str, collection_dir: Path | str) -> HandlerConfig:
    """Map a game domain to a synced collection directory.

    Validates before writing anything: the game domain must be well
    formed, the directory must exist and hold a synced collection state,
    and the collection's own game domain must be present and match the
    mapping key. A state with no game domain is rejected instead of
    treated as a wildcard: it could never be checked against a link's
    game, so routing cross-game downloads into it must be impossible.
    """
    game = str(game_domain).strip().lower()
    if not _GAME_RE.match(game):
        raise HandlerError(f"Invalid game domain: {game_domain!r}")
    directory = Path(collection_dir).expanduser()
    if not directory.is_dir():
        raise HandlerError(f"{directory} is not an existing directory.")
    # Canonicalize to an absolute, symlink-resolved path NOW, against the
    # mapping command's own cwd. The desktop handler later runs with an
    # arbitrary cwd (browser-launched), so a stored relative path would
    # resolve against the wrong directory.
    directory = directory.resolve()
    if not (directory / STATE_FILENAME).exists():
        raise HandlerError(
            f"{directory} is not a synced collection (no {STATE_FILENAME}). "
            "Run 'nexus-dl sync' there first."
        )
    state = _load_collection_state(directory)
    if not state.game_domain:
        raise HandlerError(
            f"The collection state in {directory} has no game domain; run "
            "'nexus-dl sync' to create it properly."
        )
    if state.game_domain.lower() != game:
        raise HandlerError(
            f"{directory} holds a '{state.game_domain}' collection and cannot "
            f"be mapped to '{game}'."
        )
    cfg = HandlerConfig.load()
    dirs = cfg.mappings.setdefault(game, [])
    entry = str(directory)
    if entry not in dirs:
        dirs.append(entry)
    cfg.save()
    return cfg


def remove_mapping(game_domain: str) -> bool:
    """Remove all mappings for a game. Returns True if one existed."""
    game = str(game_domain).strip().lower()
    cfg = HandlerConfig.load()
    if game not in cfg.mappings:
        return False
    del cfg.mappings[game]
    cfg.save()
    return True


def resolve_collection_dir(
    game_domain: str,
    explicit_dir: Path | None = None,
    config: HandlerConfig | None = None,
) -> Path:
    """Resolve the collection directory an nxm:// link downloads into.

    Read-only: no directories, files, or state are ever created here. An
    explicit --mods-dir must be an existing directory holding a synced
    collection whose game matches the link. Without one, mapped
    candidates are filtered to real, game-matching collections; zero or
    multiple candidates are actionable failures instead of guesses. A
    state with no game domain fails closed on both paths: it is never a
    wildcard that accepts links for any game.
    """
    game = str(game_domain).strip().lower()

    if explicit_dir is not None:
        directory = Path(explicit_dir)
        if not directory.is_dir():
            raise NxmRoutingError(f"{directory} is not an existing directory.")
        if not (directory / STATE_FILENAME).exists():
            raise NxmRoutingError(
                f"{directory} is not a synced collection (no {STATE_FILENAME}). "
                "Point --mods-dir at a collection directory created by "
                "'nexus-dl sync'."
            )
        state = _load_collection_state(directory)
        if not state.game_domain:
            raise NxmRoutingError(
                f"The collection state in {directory} has no game domain; "
                "run 'nexus-dl sync' there to create it properly."
            )
        if state.game_domain.lower() != game:
            raise NxmRoutingError(
                f"{directory} holds a '{state.game_domain}' collection, but "
                f"the link is for '{game}'."
            )
        return directory

    cfg = config if config is not None else HandlerConfig.load()
    candidates: list[Path] = []
    for entry in cfg.mappings.get(game, []):
        directory = Path(entry)
        if not (directory / STATE_FILENAME).exists():
            continue
        try:
            state = _load_collection_state(directory)
        except HandlerError:
            continue
        if not state.game_domain:
            raise NxmRoutingError(
                f"The collection state in {directory} has no game domain; "
                "run 'nexus-dl sync' there to create it properly."
            )
        if state.game_domain.lower() != game:
            continue
        candidates.append(directory)

    if not candidates:
        raise NxmRoutingError(
            f"No collection directory is mapped for '{game}'. Map one with:\n"
            f"  nexus-dl handler map {game} <collection-dir>\n"
            "or pass --mods-dir <collection-dir> to this command."
        )
    if len(candidates) > 1:
        listing = "\n".join(f"  {c}" for c in candidates)
        raise NxmRoutingError(
            f"Multiple collection directories are mapped for '{game}':\n"
            f"{listing}\n"
            "Choose one with --mods-dir, or remove a mapping with "
            "'nexus-dl handler unmap'."
        )
    return candidates[0]


def escape_exec_argument(value: str) -> str:
    """Escape one Exec argument per the Desktop Entry Specification.

    Two escaping layers apply and both are handled here: backslash
    escaping of the reserved characters (``$`` and the backtick stay
    escaped even inside quotes) and percent escaping (a literal ``%`` is
    written ``%%`` so it is never read as a field code). The argument is
    wrapped in double quotes when it contains characters that need
    quoting; a bare space inside quotes needs no backslash. Control
    characters have no representation at all in an Exec value, so paths
    containing them are rejected instead of mangled into something that
    would execute differently than it reads.
    """
    if not value:
        raise DesktopEntryError("Executable path is empty.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise DesktopEntryError(
            "Executable path contains control characters and cannot be "
            "written to a desktop entry."
        )
    needs_quotes = any(c in _EXEC_RESERVED for c in value)
    out: list[str] = []
    for ch in value:
        if ch == "%":
            out.append("%%")
        elif ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch in ("$", "`"):
            out.append("\\" + ch)
        else:
            # A literal single quote inside double quotes is valid per the
            # spec, and quoting covers the remaining reserved characters
            # (; & ~ | < > * ? # ( ) and space). The only escapes produced
            # are \" \\ \$ \` and the %% literal percent, so no ambiguous
            # backslash sequences ever reach a launcher.
            out.append(ch)
    body = "".join(out)
    return f'"{body}"' if needs_quotes else body


def build_desktop_entry_content(executable: Path | str) -> str:
    """Build the .desktop entry content for the nxm scheme handler.

    The entry runs headless (``Terminal=false``): a launched click never
    opens a terminal window, so the outcome must be visible another way.
    The Exec line therefore passes ``--notify`` explicitly, making the
    desktop notification deterministic instead of dependent on whether
    stdout happens to be a TTY. ``%u`` must stay a standalone unquoted
    field code (the desktop launcher substitutes the clicked URI there),
    so only the executable path is ever quoted or escaped.
    """
    exe = str(Path(executable))
    if not os.path.isabs(exe):
        raise DesktopEntryError(f"Executable path must be absolute: {exe}")
    escaped = escape_exec_argument(exe)
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.0\n"
        "Name=nexus-dl NXM handler\n"
        "Comment=Download Nexus Mods files from nxm:// links\n"
        f"Exec={escaped} nxm %u --notify\n"
        "Terminal=false\n"
        "NoDisplay=true\n"
        "StartupNotify=false\n"
        f"MimeType={NXM_MIME_TYPE};\n"
        "Categories=Network;\n"
    )


def _default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess:
    """Run a helper subprocess from an argv list, never through a shell."""
    return subprocess.run(
        list(argv),
        shell=False,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )


# Seconds: hard bound for notification subprocesses. A hung or broken
# notification daemon must never delay a command's exit by more than this.
NOTIFY_TIMEOUT = 3


def _notify_runner(argv: Sequence[str]) -> subprocess.CompletedProcess:
    """Run notify-send (or a daemon probe) under the hard NOTIFY_TIMEOUT."""
    return subprocess.run(
        list(argv),
        shell=False,
        capture_output=True,
        text=True,
        timeout=NOTIFY_TIMEOUT,
    )


def notify(summary: str, body: str = "", runner: Callable | None = None) -> bool:
    """Best-effort desktop notification via notify-send. Never raises.

    Messages are URL-stripped so a signed link can never reach the
    notification daemon even if a caller passes one by mistake. The
    subprocess is hard-bounded to NOTIFY_TIMEOUT seconds; a missing
    tool, a timeout, or a non-zero exit all report False instead of
    failing the caller or delaying its exit.
    """
    if shutil.which("notify-send") is None:
        return False
    # "--" ends option parsing: message text can never be reinterpreted
    # as a notify-send flag, whatever a caller passes.
    argv = ["notify-send", "-a", "nexus-dl", "--", sanitize_message(summary)]
    if body:
        argv.append(sanitize_message(body))
    try:
        proc = (runner or _notify_runner)(argv)
    except Exception:
        return False
    # A non-zero exit means the daemon rejected or never received the
    # notification: report the failure honestly, still without raising.
    return getattr(proc, "returncode", 1) == 0


def notification_daemon_available(runner: Callable | None = None) -> bool | None:
    """Best-effort: is a notification daemon reachable in this session?

    Returns True or False when a probe produced a definitive answer, and
    None when no probe tooling exists (callers must skip silently: a
    missing probe is never an install failure). Probes, in order: gdbus
    or dbus-send asking the session bus who owns
    org.freedesktop.Notifications, then a timed notify-send whose exit
    status reflects daemon reachability. Never raises.
    """
    run = runner or _notify_runner
    for tool in ("gdbus", "dbus-send"):
        path = shutil.which(tool)
        if not path:
            continue
        if tool == "gdbus":
            argv = [
                path, "call", "--session",
                "--dest", "org.freedesktop.DBus",
                "--object-path", "/org/freedesktop/DBus",
                "--method", "org.freedesktop.DBus.GetNameOwner",
                "org.freedesktop.Notifications",
            ]
        else:
            argv = [
                path, "--session", "--print-reply",
                "--dest=org.freedesktop.DBus", "/org/freedesktop/DBus",
                "org.freedesktop.DBus.GetNameOwner",
                "string:org.freedesktop.Notifications",
            ]
        try:
            proc = run(argv)
        except Exception:
            continue  # a broken probe tool must not decide the answer
        # GetNameOwner exits non-zero when the name is unowned (or there
        # is no session bus at all): either way no daemon is reachable.
        return getattr(proc, "returncode", 1) == 0
    if shutil.which("notify-send") is not None:
        # No bus tooling: fall back to a real (visible, harmless)
        # notify-send whose exit status reflects daemon reachability.
        return notify(
            "nexus-dl: notification test",
            "If you can see this, nxm:// click outcomes will notify you.",
            runner=runner,
        )
    return None


# Notification-display switches for the desktops where a silently-false
# one is known, with the exact fix command. XDG_CURRENT_DESKTOP picks
# WHICH switch to read; desktop identity never gates install (KDE,
# tiling, and unknown desktops run fine and simply get no settings
# warning), and the warning names the switch and the fix instead of
# hiding the setting.
_NOTIFICATION_SETTINGS: dict[str, tuple[str, str, str]] = {
    "cinnamon": (
        "org.cinnamon.desktop.notifications",
        "display-notifications",
        "gsettings set org.cinnamon.desktop.notifications"
        " display-notifications true",
    ),
    "gnome": (
        "org.gnome.desktop.notifications",
        "show-banners",
        "gsettings set org.gnome.desktop.notifications show-banners true",
    ),
}


def _desktop_notification_setting(
    environ: Mapping[str, str] | None = None,
) -> tuple[str, str, str] | None:
    """This desktop's notification-display switch, or None if unknown.

    Substring match because XDG_CURRENT_DESKTOP can hold a colon
    separated list ("ubuntu:GNOME") or a prefixed name ("X-Cinnamon").
    """
    env = os.environ if environ is None else environ
    desktop = (env.get("XDG_CURRENT_DESKTOP") or "").lower()
    if "cinnamon" in desktop:
        return _NOTIFICATION_SETTINGS["cinnamon"]
    if "gnome" in desktop:
        return _NOTIFICATION_SETTINGS["gnome"]
    return None


def notifications_enabled_in_desktop(
    runner: Callable | None = None,
    environ: Mapping[str, str] | None = None,
) -> bool | None:
    """Best-effort: does this desktop actually display notifications?

    Reads the desktop's own gsettings switch where one is known
    (Cinnamon's ``display-notifications``, GNOME's ``show-banners``).
    Returns False when the switch reads false, True when it reads true,
    and None when the desktop is unknown, gsettings is missing, or the
    value cannot be read: callers must answer None with silence, never
    a warning and never a refusal. Never raises.
    """
    spec = _desktop_notification_setting(environ)
    if spec is None or shutil.which("gsettings") is None:
        return None
    schema, key, _fix = spec
    try:
        proc = (runner or _notify_runner)(["gsettings", "get", schema, key])
    except Exception:
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None  # schema absent or dconf unreadable: say nothing
    out = (getattr(proc, "stdout", "") or "").strip().lower()
    if out in ("true", "false"):
        return out == "true"
    return None


def capability_warnings(
    runner: Callable | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Capability probe for 'nexus-dl handler install': warnings only.

    Probes what the click-through flow actually needs: xdg-mime (to
    register the default handler), notify-send (to report click
    outcomes), and the desktop's notification-display switch where one
    is known. Every finding is a warning, never a refusal: a degraded
    handler is still usable, desktop identity is never a gate, and an
    unknown or unreadable setting is not a finding. Subprocess probes
    are argv lists bounded by NOTIFY_TIMEOUT and never raise; missing
    probe tooling is never a warning either.
    """
    run = runner or _notify_runner
    warnings: list[str] = []

    # (a) xdg-mime: presence plus one harmless read-only query. Install
    # itself stops with an error when this fails, so the warning says
    # exactly that instead of promising a degraded install.
    if shutil.which("xdg-mime") is None:
        warnings.append(
            "xdg-mime was not found: 'nexus-dl handler install' cannot "
            "register nexus-dl as the default nxm:// handler and will "
            "stop with an error. Install xdg-utils (or your "
            "distribution's equivalent), or register the desktop entry "
            "with your desktop environment manually."
        )
    else:
        try:
            proc = run(["xdg-mime", "query", "default", "text/plain"])
            functional = getattr(proc, "returncode", 1) == 0
        except Exception:
            functional = False
        if not functional:
            warnings.append(
                "xdg-mime is present but did not answer a test query: "
                "default-handler registration will likely fail and "
                "'nexus-dl handler install' will stop with an error. "
                "Check your xdg-utils installation, or register the "
                "desktop entry with your desktop environment manually."
            )

    # (b) notify-send: the only click-feedback channel besides the log.
    if shutil.which("notify-send") is None:
        warnings.append(
            "notify-send was not found: nxm:// clicks will still "
            "download and extract, but they will complete silently with "
            "no desktop notification. Install libnotify (or your "
            "distribution's equivalent), or check "
            f"{nxm_log_path()} after clicking; it records every run."
        )

    # (c) the desktop's own notification-display switch, only where one
    # is known and readable.
    spec = _desktop_notification_setting(environ)
    if spec is not None and notifications_enabled_in_desktop(
        runner=run, environ=environ
    ) is False:
        schema, key, fix = spec
        warnings.append(
            f"notifications are disabled in this desktop's settings: "
            f"{schema} {key} is false, so nxm:// clicks will complete "
            "silently even though everything else works. Enable it "
            f"with '{fix}', or in your desktop's notification settings."
        )
    return warnings


class DesktopInstaller:
    """Opt-in user-level registration of nexus-dl as the nxm:// handler.

    Install saves the previous default handler once (idempotent across
    reinstalls) so uninstall can restore it, but uninstall only restores
    that default while our entry is still the current one: a later user
    choice is never overwritten. All subprocesses are argv lists; the
    runner is injectable for tests.
    """

    def __init__(
        self,
        config: HandlerConfig | None = None,
        runner: Callable | None = None,
        entry_dir: Path | None = None,
        executable: Path | None = None,
    ):
        self.config = config if config is not None else HandlerConfig.load()
        self._runner = runner or _default_runner
        self.entry_dir = Path(entry_dir) if entry_dir is not None else applications_dir()
        self._executable = executable

    @property
    def entry_path(self) -> Path:
        return self.entry_dir / DESKTOP_ENTRY_NAME

    def resolve_executable(self) -> Path:
        """Find a stable absolute nexus-dl executable path.

        The desktop entry outlives the shell that installed it, so a
        relative or PATH-dependent reference is not enough: the resolved
        absolute path is recorded and verified.
        """
        if self._executable is not None:
            exe = Path(self._executable)
        else:
            found = shutil.which("nexus-dl")
            if not found:
                raise DesktopEntryError(
                    "Could not find nexus-dl on PATH. Activate the "
                    "virtualenv or install the package so a stable absolute "
                    "path exists, then re-run 'nexus-dl handler install'."
                )
            exe = Path(found)
        exe = exe.resolve()
        if not exe.is_file():
            raise DesktopEntryError(f"{exe} is not an existing file.")
        if not os.access(exe, os.X_OK):
            raise DesktopEntryError(
                f"{exe} exists but is not executable. Fix its permissions "
                "or reinstall nexus-dl, then re-run 'nexus-dl handler "
                "install'."
            )
        return exe

    def _query_default(self) -> str | None:
        """Current default handler for the nxm scheme, or None."""
        try:
            proc = self._runner(["xdg-mime", "query", "default", NXM_MIME_TYPE])
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            return None
        out = (getattr(proc, "stdout", "") or "").strip()
        return out or None

    def _refresh_database(self) -> None:
        """Best-effort update-desktop-database for the user entry dir."""
        try:
            self._runner(["update-desktop-database", str(self.entry_dir)])
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            pass

    def _rollback(
        self,
        wrote_entry: bool,
        prior_content: str | None = None,
        default_at_start: str | None = None,
        registered: bool = False,
        registration_uncertain: bool = False,
    ) -> str:
        """Undo a partial install, leaving the system as this run found it.

        The MIME default is restored to the value observed when THIS
        install began (best-effort), never to the recorded previous
        default: a reinstall over a working installation of ours must not
        flip the default away on failure. An entry file that already
        existed is restored to its prior contents, so a failed re-run
        cannot destroy a working entry; only an entry this run created is
        deleted.

        A run-created entry is NEVER deleted while the effective MIME
        default still points at it: xdg-mime cannot unset a default, so
        deleting would strand a dangling registration with no working
        handler. The effective default is queried after the restore
        attempt when possible; when the query is unavailable, a
        successful registration (or an indeterminate one, e.g. a timeout
        that may have applied) wins over the missing answer and the
        entry is kept. Returns a keep-reason string ("" when the entry
        was not kept) so the caller can report the state honestly:
        "query-confirmed" when a live query showed the effective default
        points at this run's entry, "registered" when this run's
        registration succeeded but could not be re-queried, and
        "indeterminate" when only a timed-out registration with no query
        answer keeps the entry.
        """
        if isinstance(default_at_start, str) and default_at_start:
            try:
                self._runner(
                    ["xdg-mime", "default", default_at_start, NXM_MIME_TYPE]
                )
            except Exception:
                pass  # best-effort; the query below decides what is safe
        if not wrote_entry:
            return ""
        if prior_content is not None:
            with contextlib.suppress(Exception):
                _atomic_write_text(self.entry_path, prior_content)
            return ""
        # Decide from the best evidence available whether the effective
        # default points at our entry: a live query first, inference from
        # this run's outcome when the query cannot run.
        query_ran = False
        effective: str | None = None
        try:
            proc = self._runner(
                ["xdg-mime", "query", "default", NXM_MIME_TYPE]
            )
            query_ran = getattr(proc, "returncode", 1) == 0
            out = (getattr(proc, "stdout", "") or "").strip()
            effective = out or None
        except Exception:
            query_ran = False
        if query_ran and effective is not None:
            if effective == DESKTOP_ENTRY_NAME:
                return "query-confirmed"
        elif registered:
            # A confirmed registration outranks an unavailable or empty
            # query answer: fail safe and keep the entry.
            return "registered"
        elif registration_uncertain and not query_ran:
            # Only an indeterminate registration (timeout) with no query
            # available keeps the entry; otherwise this run never became
            # the default and deleting is safe.
            return "indeterminate"
        with contextlib.suppress(OSError):
            self.entry_path.unlink()
        return ""

    def install(self) -> Path:
        """Install the desktop entry and register it as the nxm handler.

        Honest and rollback-safe: any registration failure removes the
        entry file again, best-effort restores the default exactly as
        this run found it, and leaves the on-disk config untouched. A
        pre-existing entry that cannot be read aborts the install before
        anything is written: never replace what cannot be snapshotted
        for rollback. When the failure happens after registration
        succeeded and there is no other default to restore, the entry is
        kept instead of deleted so the MIME default never points at a
        missing file, and the error says so.
        """
        exe = self.resolve_executable()
        # Raises before any write when the path cannot be represented.
        content = build_desktop_entry_content(exe)
        self.entry_dir.mkdir(parents=True, exist_ok=True)

        # Capture what a re-run must not destroy: the existing entry's
        # contents, read BEFORE anything is modified. A symlink is
        # refused outright: exists()/read_text() follow the link, so a
        # dangling link would read as absent (and be replaced), and a
        # readable link's target content would be restored as a regular
        # file, destroying the link either way. An unreadable entry is a
        # hard abort too: never replace what cannot be snapshotted for
        # rollback.
        prior_content: str | None = None
        if self.entry_path.is_symlink():
            raise DesktopEntryError(
                f"The existing desktop entry {self.entry_path} is a "
                "symlink. Refusing to replace it: a dangling link reads "
                "as absent and a readable link's target cannot be "
                "restored as a link by rollback. Remove the symlink (or "
                "replace it with a regular file), then re-run 'nexus-dl "
                "handler install'. Nothing was changed."
            )
        if self.entry_path.exists():
            try:
                prior_content = self.entry_path.read_text()
            except (OSError, ValueError) as e:
                detail = getattr(e, "strerror", None) or type(e).__name__
                raise DesktopEntryError(
                    f"The existing desktop entry {self.entry_path} could "
                    f"not be read ({detail}). Refusing to replace an entry "
                    "that cannot be backed up for rollback. Fix its "
                    "permissions or remove it manually, then re-run "
                    "'nexus-dl handler install'. Nothing was changed."
                ) from None

        current = self._query_default()
        desktop = self.config.desktop
        # Save the previous default exactly once: a reinstall while we are
        # already the default must not record ourselves as "previous".
        if "previous_default" not in desktop:
            desktop["previous_default"] = None if current == DESKTOP_ENTRY_NAME else current

        wrote_entry = False
        registered = False
        registration_uncertain = False
        try:
            _atomic_write_text(self.entry_path, content)
            wrote_entry = True
            self._refresh_database()
            try:
                proc = self._runner(
                    ["xdg-mime", "default", DESKTOP_ENTRY_NAME, NXM_MIME_TYPE]
                )
            except FileNotFoundError:
                raise DesktopEntryError(
                    "xdg-mime was not found. Install xdg-utils, or register "
                    f"{self.entry_path} with your desktop environment manually."
                )
            except (subprocess.SubprocessError, OSError) as e:
                # TimeoutExpired and other subprocess failures become
                # domain errors so the rollback below runs and the CLI
                # prints an actionable message instead of a traceback.
                # The message states only what is known HERE; what the
                # rollback actually did is reported by the outer handler,
                # because a timeout may end in a kept entry rather than
                # a removal.
                if isinstance(e, subprocess.TimeoutExpired):
                    registration_uncertain = True
                    raise DesktopEntryError(
                        "Registering the nxm:// default handler timed out"
                        f" ({type(e).__name__}). Whether the registration "
                        "applied before the timeout is unknown."
                    )
                raise DesktopEntryError(
                    "Registering the nxm:// default handler failed"
                    f" ({type(e).__name__})."
                )
            if getattr(proc, "returncode", 0) != 0:
                detail = (getattr(proc, "stderr", "") or "").strip()
                raise DesktopEntryError(
                    "xdg-mime default failed"
                    + (f": {sanitize_message(detail)}" if detail else ".")
                )
            registered = True
            desktop.update(
                {
                    "installed": True,
                    "entry_path": str(self.entry_path),
                    "executable": str(exe),
                    "installed_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            self.config.save()
        except (DesktopEntryError, HandlerError, OSError, subprocess.SubprocessError) as e:
            keep_reason = self._rollback(
                wrote_entry, prior_content, current,
                registered, registration_uncertain,
            )
            base = sanitize_message(str(e)).strip()
            if base and not base.endswith("."):
                base += "."
            if keep_reason:
                # The entry was kept because the effective default
                # points at it, or could not be confirmed to point
                # elsewhere. State exactly which situation applies and
                # what uninstall will and will not clear. Only the
                # indeterminate note mentions a timeout or an
                # unconfirmed outcome; a live query confirmation
                # outranks whatever the registration attempt did.
                if keep_reason == "query-confirmed":
                    registration_note = (
                        "the effective nxm:// default points at it"
                    )
                    strand_note = (
                        "deleting the entry would strand the "
                        "registration with no handler to run"
                    )
                elif keep_reason == "registered":
                    registration_note = (
                        "this run registered it as the nxm:// default"
                    )
                    strand_note = (
                        "deleting the entry would strand the "
                        "registration with no handler to run"
                    )
                else:  # "indeterminate"
                    registration_note = (
                        "the registration attempt timed out, so whether "
                        "the default points at it could not be "
                        "confirmed either way"
                    )
                    strand_note = (
                        "deleting the entry while the default may point "
                        "at it would strand the registration with no "
                        "handler to run"
                    )
                prior = desktop.get("previous_default")
                if isinstance(prior, str) and prior:
                    uninstall_note = (
                        "it removes the entry and restores the recorded "
                        f"previous default ('{prior}') as long as "
                        "nexus-dl is still the current one."
                    )
                else:
                    uninstall_note = (
                        "it removes the entry, and because no previous "
                        "default was recorded the nxm:// registration "
                        "will remain as an inert reference to the removed "
                        "entry (launchers ignore missing entries) until "
                        "another handler is chosen or nexus-dl is "
                        "reinstalled."
                    )
                raise DesktopEntryError(
                    f"The install did not complete: {base} The desktop "
                    f"entry at {self.entry_path} was KEPT: "
                    f"{registration_note}, and {strand_note} (xdg-mime "
                    "cannot unset a default). The handler configuration "
                    "was not saved. Fix the underlying problem "
                    "(permissions, disk space) and re-run 'nexus-dl "
                    "handler install' to finish. To back out instead, "
                    f"run 'nexus-dl handler uninstall': {uninstall_note}"
                ) from e
            if wrote_entry:
                if prior_content is not None:
                    entry_note = (
                        "the desktop entry was restored to its prior "
                        "contents (best effort)"
                    )
                elif self.entry_path.exists():
                    # The rollback's unlink is best effort; report the
                    # file's real state instead of assuming removal.
                    entry_note = (
                        "the desktop entry could not be removed and "
                        f"still exists at {self.entry_path}"
                    )
                else:
                    entry_note = "the desktop entry was removed"
                if isinstance(current, str) and current:
                    default_note = (
                        "the MIME default was restored toward the "
                        "value found at the start of this run"
                    )
                else:
                    default_note = (
                        "no MIME default was found at the start of "
                        "this run, so none was restored"
                    )
                outcome = (
                    f" The partial install was rolled back: "
                    f"{entry_note}, and {default_note}. Retry 'nexus-dl "
                    "handler install' once the problem is fixed."
                )
            else:
                outcome = " Nothing was written or registered."
            raise DesktopEntryError(f"{base}{outcome}") from e
        return self.entry_path

    def uninstall(self) -> str:
        """Remove the desktop entry; restore the saved default only if ours.

        Returns a short human-readable summary. Never touches the default
        handler when another application has since taken over, and never
        modifies unrelated mimeapps entries.
        """
        desktop = self.config.desktop
        current = self._query_default()
        restored: str | None = None

        if current == DESKTOP_ENTRY_NAME:
            prior = desktop.get("previous_default")
            if isinstance(prior, str) and prior:
                try:
                    proc = self._runner(["xdg-mime", "default", prior, NXM_MIME_TYPE])
                except FileNotFoundError:
                    raise DesktopEntryError(
                        "xdg-mime was not found; the previous default handler "
                        "could not be restored. The desktop entry was left in "
                        "place so uninstall can be retried."
                    )
                except (subprocess.SubprocessError, OSError):
                    # TimeoutExpired etc: keep the recovery state intact
                    # (entry + saved previous default) so uninstall is
                    # retryable, and surface a domain error, not a traceback.
                    raise DesktopEntryError(
                        "Could not restore the previous default handler "
                        f"({prior}): xdg-mime failed or timed out. The "
                        "desktop entry was left in place so uninstall can "
                        "be retried."
                    )
                if getattr(proc, "returncode", 0) != 0:
                    raise DesktopEntryError(
                        "Could not restore the previous default handler "
                        f"({prior}). The desktop entry was left in place so "
                        "uninstall can be retried."
                    )
                restored = prior
            # No saved previous default: leave mimeapps alone. A dangling
            # reference to the removed entry is ignored by the spec.

        existed = self.entry_path.exists()
        if existed:
            try:
                self.entry_path.unlink()
            except OSError as e:
                raise DesktopEntryError(f"Could not remove {self.entry_path}: {e}")
            self._refresh_database()

        self.config.desktop = {}
        self.config.save()

        if not existed and current != DESKTOP_ENTRY_NAME:
            return "NXM handler was not installed; nothing to remove."
        if restored:
            return f"NXM handler removed; previous default '{restored}' restored."
        if current == DESKTOP_ENTRY_NAME:
            # Honest about what removal cannot do: xdg-mime has no unset,
            # and no previous default was recorded to restore, so the
            # scheme registration outlives the entry as an inert
            # reference (launchers ignore entries whose file is gone).
            return (
                "NXM handler removed. No previous default was recorded "
                "and xdg-mime cannot unset one, so the nxm:// "
                "registration may still reference the removed entry. "
                "The reference is inert (launchers ignore missing "
                "entries); choosing another handler in your desktop "
                "settings or re-running 'nexus-dl handler install' "
                "replaces it."
            )
        return "NXM handler removed."

    def status(self) -> dict[str, Any]:
        """Read-only diagnostics for 'nexus-dl handler status'."""
        desktop = self.config.desktop
        exe = desktop.get("executable")
        # One query per status call: the default handler is read exactly
        # once and reused, so diagnostics never spawn duplicate xdg-mime
        # subprocesses or report inconsistent snapshots.
        current = self._query_default()
        exe_path = Path(str(exe)) if isinstance(exe, str) and exe else None
        exe_ok = bool(
            exe_path
            and exe_path.is_file()
            and os.access(exe_path, os.X_OK)
        )
        return {
            "entry_path": self.entry_path,
            "entry_installed": self.entry_path.exists(),
            "executable": exe if isinstance(exe, str) else None,
            "executable_exists": exe_ok,
            "current_default": current,
            "is_default": current == DESKTOP_ENTRY_NAME,
            "previous_default": desktop.get("previous_default"),
            "xdg_mime_available": shutil.which("xdg-mime") is not None,
            "mappings": {g: list(d) for g, d in self.config.mappings.items()},
        }
