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
    """Represents the installed state of a mod."""

    def __init__(
        self,
        mod_id: int,
        name: str,
        file_id: int,
        version: str,
        filename: str,
        installed_at: str | None = None,
        optional: bool = False,
        position: int = 0,
        phase: int = 0,
        requirements: list[int] | None = None,
        manual: bool = False,
    ):
        self.mod_id = mod_id
        self.name = name
        self.file_id = file_id
        self.version = version
        self.filename = filename
        self.installed_at = installed_at or datetime.now(timezone.utc).isoformat()
        self.optional = optional
        self.position = position
        self.phase = phase
        self.requirements = requirements
        self.manual = manual

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "file_id": self.file_id,
            "version": self.version,
            "filename": self.filename,
            "installed_at": self.installed_at,
            "optional": self.optional,
            "position": self.position,
            "phase": self.phase,
            "requirements": self.requirements,
            "manual": self.manual,
        }

    @classmethod
    def from_dict(cls, mod_id: int, data: dict[str, Any]) -> "ModState":
        return cls(
            mod_id=mod_id,
            name=data.get("name", ""),
            file_id=data.get("file_id", 0),
            version=data.get("version", ""),
            filename=data.get("filename", ""),
            installed_at=data.get("installed_at"),
            optional=data.get("optional", False),
            position=data.get("position", 0),
            phase=data.get("phase", 0),
            requirements=data.get("requirements"),
            manual=data.get("manual", False),
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
        self.mods: dict[int, ModState] = {}
        self.game_dir: str = ""
        self.proton_prefix: str = ""
        self.deployed_files: list[dict] = []
        self.deployed_at: str | None = None

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

        self.mods = {}
        for mod_id_str, mod_data in data.get("mods", {}).items():
            mod_id = int(mod_id_str)
            self.mods[mod_id] = ModState.from_dict(mod_id, mod_data)

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
            "mods": {str(mod_id): mod.to_dict() for mod_id, mod in self.mods.items()},
            "game_dir": self.game_dir,
            "proton_prefix": self.proton_prefix,
            "deployed_files": self.deployed_files,
            "deployed_at": self.deployed_at,
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
        """Add or update a mod in the state."""
        mod_id = mod_info["mod_id"]
        self.mods[mod_id] = ModState(
            mod_id=mod_id,
            name=mod_info["mod_name"],
            file_id=mod_info["file_id"],
            version=mod_info["version"] or "",
            filename=mod_info["filename"],
            optional=mod_info.get("optional", False),
            manual=mod_info.get("manual", False),
        )

    def remove_mod(self, mod_id: int) -> None:
        """Remove a mod from the state."""
        self.mods.pop(mod_id, None)

    def get_mod(self, mod_id: int) -> ModState | None:
        """Get mod state by ID."""
        return self.mods.get(mod_id)

    def compare_with_collection(
        self, collection_mods: list[dict[str, Any]]
    ) -> tuple[list[dict], list[dict], list[dict], list[int]]:
        """
        Compare installed mods with collection.

        Returns:
            - to_install: mods in collection but not installed
            - to_update: mods with different file_id (new version)
            - up_to_date: mods that match
            - to_remove: mod IDs installed but not in collection
        """
        collection_mod_ids = {m["mod_id"] for m in collection_mods}
        installed_mod_ids = set(self.mods.keys())

        to_install = []
        to_update = []
        up_to_date = []

        for mod in collection_mods:
            mod_id = mod["mod_id"]
            installed = self.mods.get(mod_id)

            if installed is None:
                to_install.append(mod)
            elif installed.file_id != mod["file_id"]:
                to_update.append(mod)
            else:
                up_to_date.append(mod)

        manual_mod_ids = {mid for mid, ms in self.mods.items() if ms.manual}
        to_remove = list(installed_mod_ids - collection_mod_ids - manual_mod_ids)

        return to_install, to_update, up_to_date, to_remove
