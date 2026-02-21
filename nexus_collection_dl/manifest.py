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
        logical_name_to_mod_id: dict[str, int] | None = None,
    ):
        self.mod_rules = mod_rules
        self.plugins = plugins
        self.plugin_rules = plugin_rules
        self.mod_phases = mod_phases
        # Maps logicalFilename -> modId for resolving mod rules
        self.logical_name_to_mod_id = logical_name_to_mod_id or {}

    def to_dict(self) -> dict[str, Any]:
        """Serialize for state storage."""
        return {
            "mod_rules": self.mod_rules,
            "plugins": self.plugins,
            "plugin_rules": self.plugin_rules,
            "mod_phases": {str(k): v for k, v in self.mod_phases.items()},
            "logical_name_to_mod_id": self.logical_name_to_mod_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CollectionManifest":
        """Deserialize from state storage."""
        return cls(
            mod_rules=data.get("mod_rules", []),
            plugins=data.get("plugins", []),
            plugin_rules=data.get("plugin_rules", []),
            mod_phases={int(k): v for k, v in data.get("mod_phases", {}).items()},
            logical_name_to_mod_id={
                k: int(v)
                for k, v in data.get("logical_name_to_mod_id", {}).items()
            },
        )


NEXUS_API_BASE = "https://api.nexusmods.com"


def download_and_parse_manifest(
    download_url: str,
    session: requests.Session | None = None,
) -> CollectionManifest:
    """
    Download collection bundle, extract it, and parse collection.json.

    The bundle is an archive (usually .7z) containing collection.json
    with mod rules, plugin order, and phase assignments.

    Args:
        download_url: Full URL or relative API path (e.g. /v2/collections/...)
        session: Authenticated requests session (required for relative API paths)
    """
    # Resolve relative API paths to the download_link endpoint
    if download_url.startswith("/"):
        download_url = f"{NEXUS_API_BASE}{download_url}"

    http = session or requests.Session()

    with tempfile.TemporaryDirectory(prefix="nexus-manifest-") as tmp_dir:
        tmp_path = Path(tmp_dir)

        # The download_link endpoint returns JSON with CDN URLs, not the file itself
        cdn_url = _resolve_download_url(download_url, http)

        # Download the actual bundle from CDN
        bundle_path = tmp_path / "bundle.7z"
        response = requests.get(cdn_url, stream=True)
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


def _resolve_download_url(api_url: str, session: requests.Session) -> str:
    """Resolve a Nexus download_link endpoint to a CDN URL."""
    response = session.get(api_url)
    response.raise_for_status()
    data = response.json()

    links = data.get("download_links", [])
    if not links:
        raise ManifestError(f"No download links returned from {api_url}")

    return links[0]["URI"]


def _find_collection_json(extract_dir: Path) -> Path | None:
    """Find collection.json in extracted bundle (may be nested)."""
    for path in extract_dir.rglob("collection.json"):
        return path
    return None


def _parse_collection_json(data: dict[str, Any]) -> CollectionManifest:
    """Parse collection.json into a CollectionManifest."""
    # Extract mod rules (before/after/requires/conflicts between mods)
    mod_rules = data.get("modRules", [])

    # Extract plugin rules (load before/after between plugins)
    plugin_rules = data.get("pluginRules", [])

    # Build mod_id -> phase mapping and logicalFilename -> modId lookup
    mod_phases: dict[int, int] = {}
    logical_name_to_mod_id: dict[str, int] = {}
    for mod_entry in data.get("mods", []):
        source = mod_entry.get("source", {})
        mod_id = source.get("modId")
        phase = mod_entry.get("phase", 0)
        if mod_id is not None:
            mod_phases[int(mod_id)] = int(phase)
            # Map logicalFilename to modId for resolving mod rules
            logical = source.get("logicalFilename", "")
            if logical:
                logical_name_to_mod_id[logical] = int(mod_id)

    # Plugin load order: prefer "loadOrder" (has actual ESM/ESP entries with
    # enabled status) over "plugins" (often empty)
    load_order = data.get("loadOrder", [])
    plugins_raw = data.get("plugins", [])

    plugins: list[dict[str, Any]] = []
    if load_order:
        # loadOrder entries: {"enabled": true, "id": "foo.esm", "name": "foo.esm", ...}
        for entry in load_order:
            plugins.append({
                "filename": entry.get("name", entry.get("id", "")),
                "enabled": entry.get("enabled", True),
            })
    elif plugins_raw:
        # Fallback to plugins array if loadOrder is empty
        plugins = plugins_raw

    return CollectionManifest(
        mod_rules=mod_rules,
        plugins=plugins,
        plugin_rules=plugin_rules,
        mod_phases=mod_phases,
        logical_name_to_mod_id=logical_name_to_mod_id,
    )
