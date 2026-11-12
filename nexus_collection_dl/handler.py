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
from typing import Any, Callable, Sequence

from .state import STATE_FILENAME, CollectionState, StateError

CONFIG_DIR_NAME = "nexus-dl"
CONFIG_FILENAME = "handler.json"
DESKTOP_ENTRY_NAME = "nexus-dl-nxm.desktop"
NXM_MIME_TYPE = "x-scheme-handler/nxm"
SUBPROCESS_TIMEOUT = 30

_GAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")
_URL_RE = re.compile(r"https?://\S+")
# Any-scheme URI carrying a query string: a raw nxm:// link embeds its
# key/expires credentials there, so scheme-agnostic stripping is needed
# wherever free-form exception text can reach the user (the http(s)
# pattern above never matched nxm:// URIs).
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

    Two patterns are removed: http(s) URLs (query or not) and any-scheme
    URIs that carry a query string, notably raw nxm:// links whose query
    holds the key/expires credentials. A bare scheme mention without a
    query ("nxm:// links ...") is left intact.
    """
    out = _URL_RE.sub("[url redacted]", str(text))
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
    ) -> None:
        """Undo a partial install, leaving the system as this run found it.

        The MIME default is restored to the value observed when THIS
        install began (best-effort), never to the recorded previous
        default: a reinstall over a working installation of ours must not
        flip the default away on failure. An entry file that already
        existed is restored to its prior contents, so a failed re-run
        cannot destroy a working entry; only an entry this run created is
        deleted.
        """
        if isinstance(default_at_start, str) and default_at_start:
            with contextlib.suppress(Exception):
                self._runner(
                    ["xdg-mime", "default", default_at_start, NXM_MIME_TYPE]
                )
        if wrote_entry:
            if prior_content is not None:
                with contextlib.suppress(Exception):
                    _atomic_write_text(self.entry_path, prior_content)
            else:
                with contextlib.suppress(OSError):
                    self.entry_path.unlink()

    def install(self) -> Path:
        """Install the desktop entry and register it as the nxm handler.

        Honest and rollback-safe: any registration failure removes the
        entry file again, best-effort restores the previous default, and
        leaves the on-disk config untouched.
        """
        exe = self.resolve_executable()
        # Raises before any write when the path cannot be represented.
        content = build_desktop_entry_content(exe)
        self.entry_dir.mkdir(parents=True, exist_ok=True)

        current = self._query_default()
        # Capture what a re-run must not destroy: the existing entry's
        # contents and the default as found. Rollback restores exactly
        # these, so a failed reinstall over a working installation leaves
        # the working entry in place and the MIME default unchanged.
        prior_content: str | None = None
        if self.entry_path.exists():
            with contextlib.suppress(OSError):
                prior_content = self.entry_path.read_text()
        desktop = self.config.desktop
        # Save the previous default exactly once: a reinstall while we are
        # already the default must not record ourselves as "previous".
        if "previous_default" not in desktop:
            desktop["previous_default"] = None if current == DESKTOP_ENTRY_NAME else current

        wrote_entry = False
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
                kind = "timed out" if isinstance(e, subprocess.TimeoutExpired) else "failed"
                raise DesktopEntryError(
                    f"Registering the nxm:// default handler {kind}"
                    f" ({type(e).__name__}). The desktop entry was rolled "
                    "back; you can retry 'nexus-dl handler install' or "
                    f"register {self.entry_path} with your desktop "
                    "environment manually."
                )
            if getattr(proc, "returncode", 0) != 0:
                detail = (getattr(proc, "stderr", "") or "").strip()
                raise DesktopEntryError(
                    "xdg-mime default failed"
                    + (f": {sanitize_message(detail)}" if detail else ".")
                )
            desktop.update(
                {
                    "installed": True,
                    "entry_path": str(self.entry_path),
                    "executable": str(exe),
                    "installed_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            self.config.save()
        except (DesktopEntryError, HandlerError, OSError, subprocess.SubprocessError):
            self._rollback(wrote_entry, prior_content, current)
            raise
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
