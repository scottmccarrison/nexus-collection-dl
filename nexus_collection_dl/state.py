"""State management for tracking installed mods."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_FILENAME = ".nexus-state.json"


class StateError(Exception):
    """Raised when state file operations fail."""
    pass


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
        self.game_dir: str = ""
        self.proton_prefix: str = ""
        self.deployed_files: list[dict] = []
        self.deployed_at: str | None = None
        self.track_sync_enabled: bool = False

    def exists(self) -> bool:
        """Check if state file exists."""
        return self.state_file.exists()

    def load(self) -> None:
        """Load state from file."""
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
        for file_id_str, mod_data in data.get("mods", {}).items():
            file_id = int(file_id_str)
            self.mods[file_id] = ModState.from_dict(file_id, mod_data)

    def save(self) -> None:
        """Save state to file."""
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
            "game_dir": self.game_dir,
            "proton_prefix": self.proton_prefix,
            "deployed_files": self.deployed_files,
            "deployed_at": self.deployed_at,
            "track_sync_enabled": self.track_sync_enabled,
        }

        with open(self.state_file, "w") as f:
            json.dump(data, f, indent=2)

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
        collection_mod_ids = {m["mod_id"] for m in collection_mods}
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
