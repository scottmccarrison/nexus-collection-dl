"""State management for tracking installed mods."""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from typing_extensions import Self

try:
    import fcntl
except ImportError:  # non-POSIX platforms: locking degrades to a no-op
    fcntl = None  # type: ignore[assignment]

STATE_FILENAME = ".nexus-state.json"
LOCK_FILENAME = ".nexus-state.lock"


class StateError(Exception):
    """Raised when state file operations fail."""


class StateBusyError(StateError):
    """Raised when another nexus-dl operation holds the collection lock."""


class LoadOrderRegenError(StateError):
    """Raised when load order regeneration fails after a state change.

    The underlying state change (a removal, an import, a download) is
    already persisted when this fires: callers report it as a warning or
    error on their result instead of rolling back. Subclasses StateError
    so every CLI/web surface that already handles state failures handles
    regeneration failures cleanly too.
    """


class CollectionLock:
    """Advisory lock that serializes state writers per collection directory.

    Uses a non-blocking flock on a lock file inside the mods directory, so
    a second concurrent state-mutating operation (sync, update, add,
    add-local, import, track-sync enable/disable, deploy, undeploy, nxm
    download) for the same collection fails fast with StateBusyError
    instead of interleaving mutations. Writers must load state only after
    acquiring this lock, and no locked method may call another locked
    method (the flock is not reentrant across separate opens). The lock
    file itself is never removed; it carries no state.
    """

    def __init__(self, mods_dir: Path):
        self.lock_path = Path(mods_dir) / LOCK_FILENAME
        self._fd: int | None = None

    def __enter__(self) -> Self:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise StateBusyError(
                f"Another nexus-dl operation is already running for "
                f"{self.lock_path.parent}. Wait for it to finish and try again."
            )
        self._fd = fd
        return self

    def __exit__(self, *exc_info) -> Literal[False]:
        if self._fd is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
        return False


class ModState:
    """Represents the installed state of a single mod FILE.

    NOTE: A single Nexus mod (mod_id) can have multiple files in a collection.
    State is keyed by file_id, not mod_id, to support this correctly.
    """

    def __init__(
        self,
        file_id: int,
        mod_id: int,
        name: str,
        version: str,
        filename: str,
        installed_at: str | None = None,
        optional: bool = False,
        position: int = 0,
        phase: int = 0,
        requirements: list[int] | None = None,
        manual: bool = False,
        download_status: str = "downloaded",
        browser_url: str = "",
    ):
        self.file_id = file_id
        self.mod_id = mod_id
        self.name = name
        self.version = version
        self.filename = filename
        self.installed_at = installed_at or datetime.now(timezone.utc).isoformat()
        self.optional = optional
        self.position = position
        self.phase = phase
        self.requirements = requirements
        self.manual = manual
        self.download_status = download_status
        self.browser_url = browser_url

    def to_dict(self) -> dict[str, Any]:
        return {
            "mod_id": self.mod_id,
            "name": self.name,
            "version": self.version,
            "filename": self.filename,
            "installed_at": self.installed_at,
            "optional": self.optional,
            "position": self.position,
            "phase": self.phase,
            "requirements": self.requirements,
            "manual": self.manual,
            "download_status": self.download_status,
            "browser_url": self.browser_url,
        }

    @classmethod
    def from_dict(cls, file_id: int, data: dict[str, Any]) -> "ModState":
        return cls(
            file_id=file_id,
            mod_id=data.get("mod_id", 0),
            name=data.get("name", ""),
            version=data.get("version", ""),
            filename=data.get("filename", ""),
            installed_at=data.get("installed_at"),
            optional=data.get("optional", False),
            position=data.get("position", 0),
            phase=data.get("phase", 0),
            requirements=data.get("requirements"),
            manual=data.get("manual", False),
            download_status=data.get("download_status", "downloaded"),
            browser_url=data.get("browser_url", ""),
        )


class CollectionState:
    """Manages the state file for a collection."""

    def __init__(self, mods_dir: Path):
        self.mods_dir = Path(mods_dir)
        self.state_file = self.mods_dir / STATE_FILENAME
        self.collection_url: str = ""
        self.collection_name: str = ""
        self.collection_revision: int = 0
        self.game_domain: str = ""
        self.mod_rules: list = []
        self.manifest_data: dict | None = None
        # Key: file_id (int) -> ModState
        # Using file_id as key ensures all files from multi-file mods are tracked.
        self.mods: dict[int, ModState] = {}
        # Tombstones for removed Nexus mod pages, keyed by mod_id (int),
        # never by file_id: a collection revision assigns new file ids to
        # updated mods, so a file_id-keyed tombstone would silently stop
        # matching and let the next revision resurrect a removed mod.
        # Values are display info only: {"removed_at": iso8601, "name": str}.
        self.removed_mods: dict[int, dict] = {}
        self.game_dir: str = ""
        self.proton_prefix: str = ""
        self.deployed_files: list[dict] = []
        self.deployed_at: str | None = None
        self.track_sync_enabled: bool = False

    def exists(self) -> bool:
        """Check if state file exists."""
        return self.state_file.exists()

    def load(self) -> None:
        """Load state from file.

        Tolerant of older formats: a state file without a removed_mods
        key (written before tombstones existed) loads unchanged.
        Downgrade caveat: older versions ignore the removed_mods key and
        drop it on save, so running an older build can resurrect removed
        mods at the next sync/update.
        """
        if not self.state_file.exists():
            raise StateError(f"No state file found at {self.state_file}")

        try:
            with open(self.state_file) as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise StateError(f"Invalid state file: {e}")

        self.collection_url = data.get("collection_url", "")
        self.collection_name = data.get("collection_name", "")
        self.collection_revision = data.get("collection_revision", 0)
        self.game_domain = data.get("game_domain", "")
        self.mod_rules = data.get("mod_rules", [])
        self.manifest_data = data.get("manifest_data")
        self.game_dir = data.get("game_dir", "")
        self.proton_prefix = data.get("proton_prefix", "")
        self.deployed_files = data.get("deployed_files", [])
        self.deployed_at = data.get("deployed_at")
        self.track_sync_enabled = data.get("track_sync_enabled", False)

        self.mods = {}
        raw_mods = data.get("mods", {})
        # A bad shape raises instead of silently losing mod entries:
        # same strictness as the removed_mods check below.
        if not isinstance(raw_mods, dict):
            raise StateError("Invalid state file: mods must be a JSON object")
        for file_id_str, mod_data in raw_mods.items():
            file_id = int(file_id_str)
            self.mods[file_id] = ModState.from_dict(file_id, mod_data)

        # Tombstones restore with int keys exactly like the mods dict above.
        # The key is missing from older states: they load unchanged with no
        # tombstones. A present-but-wrong shape raises instead of being
        # silently dropped: dropping it would resurrect removed mods on
        # the next save.
        self.removed_mods = {}
        raw_removed = data.get("removed_mods")
        if raw_removed is not None and not isinstance(raw_removed, dict):
            raise StateError(
                "Invalid state file: removed_mods must be a JSON object"
            )
        if isinstance(raw_removed, dict):
            for mod_id_str, removed_data in raw_removed.items():
                self.removed_mods[int(mod_id_str)] = removed_data

    def save(self) -> None:
        """Save state atomically: unique temp file, fsync, then rename.

        The previous state file stays intact until the new one is fully
        written and fsynced, so a crash or serialization failure mid-write
        can never leave a truncated or empty state file behind.
        """
        self.mods_dir.mkdir(parents=True, exist_ok=True)

        data = {
            "collection_url": self.collection_url,
            "collection_name": self.collection_name,
            "collection_revision": self.collection_revision,
            "game_domain": self.game_domain,
            "mod_rules": self.mod_rules,
            "manifest_data": self.manifest_data,
            # Key by file_id so each file from a multi-file mod is tracked
            "mods": {str(file_id): mod.to_dict() for file_id, mod in self.mods.items()},
            # Tombstones persist as "removed_mods" keyed by mod_id (see
            # __init__). JSON object keys are strings; load() converts
            # them back to int the same way the mods dict is restored.
            "removed_mods": {
                str(mod_id): info for mod_id, info in self.removed_mods.items()
            },
            "game_dir": self.game_dir,
            "proton_prefix": self.proton_prefix,
            "deployed_files": self.deployed_files,
            "deployed_at": self.deployed_at,
            "track_sync_enabled": self.track_sync_enabled,
        }

        # Temp file lives in the same directory as the target so the final
        # os.replace() is guaranteed to stay on one filesystem.
        fd, tmp_name = tempfile.mkstemp(
            dir=self.mods_dir, prefix=f"{STATE_FILENAME}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, self.state_file)
            # Make the rename itself durable where the platform allows it.
            try:
                dir_fd = os.open(self.mods_dir, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        except BaseException:
            # Serialization or write failure: drop the temp file and let the
            # original state file stand untouched.
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def set_collection_info(
        self, url: str, name: str, revision: int, game_domain: str
    ) -> None:
        """Set collection metadata."""
        self.collection_url = url
        self.collection_name = name
        self.collection_revision = revision
        self.game_domain = game_domain

    def add_mod(self, mod_info: dict[str, Any]) -> None:
        """Add or update a mod file in the state (keyed by file_id)."""
        file_id = mod_info["file_id"]
        self.mods[file_id] = ModState(
            file_id=file_id,
            mod_id=mod_info["mod_id"],
            name=mod_info["mod_name"],
            version=mod_info["version"] or "",
            filename=mod_info["filename"],
            optional=mod_info.get("optional", False),
            manual=mod_info.get("manual", False),
            download_status=mod_info.get("download_status", "downloaded"),
            browser_url=mod_info.get("browser_url", ""),
            phase=mod_info.get("phase", 0),
            requirements=mod_info.get("requirements"),
        )

    def get_pending_mods(self) -> list[ModState]:
        """Return mods with pending_download status."""
        return [ms for ms in self.mods.values() if ms.download_status == "pending_download"]

    def remove_mod_file(self, file_id: int) -> None:
        """Remove a specific file from the state."""
        self.mods.pop(file_id, None)

    def remove_mod(self, mod_id: int) -> None:
        """Remove ALL files for a given mod_id from the state."""
        to_remove = [fid for fid, ms in self.mods.items() if ms.mod_id == mod_id]
        for fid in to_remove:
            del self.mods[fid]

    # -- removal tombstones ------------------------------------------------

    def is_removed(self, mod_id: int) -> bool:
        """True when this Nexus mod page (mod_id) is tombstoned as removed."""
        return mod_id in self.removed_mods

    def mark_removed(self, mod_id: int, name: str) -> None:
        """Tombstone a Nexus mod page as removed.

        Tombstones follow the Nexus mod PAGE (mod_id), not a file:
        collection revisions assign new file ids to updated mods, and a
        file_id-keyed tombstone would silently stop matching and let the
        next revision resurrect a removed mod. If the curator replaces
        the removed mod with a different page (a new mod_id), the
        replacement installs normally: the tombstone covers only the page
        the user removed. Values carry display info only
        (removed_at, name).
        """
        self.removed_mods[mod_id] = {
            "removed_at": datetime.now(timezone.utc).isoformat(),
            "name": name,
        }

    def clear_removed(self, mod_id: int) -> None:
        """Clear the tombstone for mod_id (an explicit re-add of the mod)."""
        self.removed_mods.pop(mod_id, None)

    def get_mod(self, mod_id: int) -> ModState | None:
        """Get first mod state by mod_id (for backwards compat)."""
        for ms in self.mods.values():
            if ms.mod_id == mod_id:
                return ms
        return None

    def get_file(self, file_id: int) -> ModState | None:
        """Get mod state by file_id."""
        return self.mods.get(file_id)

    def get_downloaded_file_ids(self) -> set[int]:
        """Return set of all downloaded file_ids."""
        return {fid for fid, ms in self.mods.items()
                if ms.download_status == "downloaded"}

    def get_downloaded_mod_ids(self) -> set[int]:
        """Return set of mod_ids that have at least one downloaded file."""
        return {ms.mod_id for ms in self.mods.values()
                if ms.download_status == "downloaded"}

    def compare_with_collection(
        self, collection_mods: list[dict[str, Any]]
    ) -> tuple[list[dict], list[dict], list[dict], list[int]]:
        """
        Compare installed mod FILES with collection.

        Since state is now keyed by file_id, we compare file_ids directly.
        A mod with a new file_id = update available.

        Returns:
            - to_install: files in collection but not installed
            - to_update: files with matching mod_id but different file_id
            - up_to_date: files that match exactly
            - to_remove: file_ids installed but not in collection (excluding manual)
        """
        collection_file_ids = {m["file_id"] for m in collection_mods}
        {m["mod_id"] for m in collection_mods}
        installed_file_ids = set(self.mods.keys())

        to_install = []
        to_update = []
        up_to_date = []

        # Build mod_id -> installed file_ids mapping
        mod_id_to_installed_files: dict[int, list[int]] = {}
        for fid, ms in self.mods.items():
            mod_id_to_installed_files.setdefault(ms.mod_id, []).append(fid)

        for mod in collection_mods:
            file_id = mod["file_id"]
            mod_id = mod["mod_id"]

            if file_id in installed_file_ids:
                up_to_date.append(mod)
            elif mod_id in mod_id_to_installed_files:
                # Same mod but different file — update available
                to_update.append(mod)
            else:
                to_install.append(mod)

        manual_file_ids = {fid for fid, ms in self.mods.items() if ms.manual}
        to_remove = list(installed_file_ids - collection_file_ids - manual_file_ids)

        return to_install, to_update, up_to_date, to_remove
