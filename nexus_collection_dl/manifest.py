"""Download and parse collection bundle manifest (collection.json)."""

import json
import tempfile
from pathlib import Path
from typing import Any

import requests

from .extractor import extract_archive


class ManifestError(Exception):
    """Raised when manifest parsing fails."""
    pass


class CollectionManifest:
    """Parsed collection.json from a collection bundle."""

    def __init__(
        self,
        mod_rules: list[dict[str, Any]],
        plugins: list[dict[str, Any]],
        plugin_rules: list[dict[str, Any]],
        mod_phases: dict[int, int],  # mod_id -> phase
    ):
        self.mod_rules = mod_rules
        self.plugins = plugins
        self.plugin_rules = plugin_rules
        self.mod_phases = mod_phases

    def to_dict(self) -> dict[str, Any]:
        """Serialize for state storage."""
        return {
            "mod_rules": self.mod_rules,
            "plugins": self.plugins,
            "plugin_rules": self.plugin_rules,
            "mod_phases": {str(k): v for k, v in self.mod_phases.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CollectionManifest":
        """Deserialize from state storage."""
        return cls(
            mod_rules=data.get("mod_rules", []),
            plugins=data.get("plugins", []),
            plugin_rules=data.get("plugin_rules", []),
            mod_phases={int(k): v for k, v in data.get("mod_phases", {}).items()},
        )


def download_and_parse_manifest(download_url: str) -> CollectionManifest:
    """
    Download collection bundle, extract it, and parse collection.json.

    The bundle is an archive (usually .7z) containing collection.json
    with mod rules, plugin order, and phase assignments.
    """
    with tempfile.TemporaryDirectory(prefix="nexus-manifest-") as tmp_dir:
        tmp_path = Path(tmp_dir)

        # Download the bundle
        bundle_path = tmp_path / "bundle.7z"
        response = requests.get(download_url, stream=True)
        response.raise_for_status()
        with open(bundle_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        # Extract the bundle
        extract_dir = tmp_path / "extracted"
        extract_dir.mkdir()
        try:
            extract_archive(bundle_path, extract_dir)
        except Exception as e:
            raise ManifestError(f"Failed to extract collection bundle: {e}")

        # Find and parse collection.json
        collection_json = _find_collection_json(extract_dir)
        if collection_json is None:
            raise ManifestError(
                "collection.json not found in bundle. "
                f"Contents: {[p.name for p in extract_dir.rglob('*') if p.is_file()]}"
            )

        try:
            with open(collection_json) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            raise ManifestError(f"Failed to parse collection.json: {e}")

        return _parse_collection_json(data)


def _find_collection_json(extract_dir: Path) -> Path | None:
    """Find collection.json in extracted bundle (may be nested)."""
    for path in extract_dir.rglob("collection.json"):
        return path
    return None


def _parse_collection_json(data: dict[str, Any]) -> CollectionManifest:
    """Parse collection.json into a CollectionManifest."""
    # Extract mod rules (before/after/requires/conflicts between mods)
    mod_rules = data.get("modRules", [])

    # Extract plugin list with enabled status
    # plugins is a list of {"filename": "foo.esp", "enabled": true}
    plugins = data.get("plugins", [])

    # Extract plugin rules (load before/after between plugins)
    plugin_rules = data.get("pluginRules", [])

    # Build mod_id -> phase mapping from the mods array
    mod_phases: dict[int, int] = {}
    for mod_entry in data.get("mods", []):
        # Each mod entry has a "source" with "modId" and optionally a "phase"
        source = mod_entry.get("source", {})
        mod_id = source.get("modId")
        phase = mod_entry.get("phase", 0)
        if mod_id is not None:
            mod_phases[int(mod_id)] = int(phase)

    return CollectionManifest(
        mod_rules=mod_rules,
        plugins=plugins,
        plugin_rules=plugin_rules,
        mod_phases=mod_phases,
    )
