"""Optional LOOT integration for Bethesda game plugin sorting."""

import os
import tempfile
from pathlib import Path
from typing import Any

import requests

# Map Nexus game domains to LOOT game identifiers and masterlist repos
LOOT_GAME_MAP: dict[str, dict[str, str]] = {
    "starfield": {"loot_game": "Starfield", "repo": "starfield"},
    "skyrimspecialedition": {"loot_game": "Skyrim Special Edition", "repo": "skyrimse"},
    "skyrim": {"loot_game": "Skyrim", "repo": "skyrim"},
    "fallout4": {"loot_game": "Fallout 4", "repo": "fallout4"},
    "falloutnv": {"loot_game": "Fallout: New Vegas", "repo": "falloutnv"},
    "fallout3": {"loot_game": "Fallout 3", "repo": "fallout3"},
    "oblivion": {"loot_game": "Oblivion", "repo": "oblivion"},
    "morrowind": {"loot_game": "Morrowind", "repo": "morrowind"},
    "fallout4vr": {"loot_game": "Fallout 4 VR", "repo": "fallout4vr"},
    "skyrimvr": {"loot_game": "Skyrim VR", "repo": "skyrimvr"},
    "enderal": {"loot_game": "Enderal: Forgotten Stories", "repo": "enderal"},
    "enderalspecialedition": {"loot_game": "Enderal: Forgotten Stories (Special Edition)", "repo": "enderalse"},
}

MASTERLIST_CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "nexus-dl" / "masterlists"

PLUGIN_EXTENSIONS = {".esp", ".esm", ".esl"}


def is_loot_available() -> bool:
    """Check if libloot Python bindings are installed."""
    try:
        import loot  # noqa: F401
        return True
    except ImportError:
        return False


def is_bethesda_game(game_domain: str) -> bool:
    """Check if a game domain is a supported Bethesda game."""
    return game_domain.lower() in LOOT_GAME_MAP


def find_plugins(mods_dir: Path) -> list[str]:
    """
    Find all ESP/ESM/ESL plugin files in the mods directory.

    Returns list of plugin filenames (not full paths).
    """
    plugins = []
    for path in mods_dir.rglob("*"):
        if path.suffix.lower() in PLUGIN_EXTENSIONS and path.is_file():
            plugins.append(path.name)
    return sorted(set(plugins))


def download_masterlist(game_domain: str) -> Path | None:
    """
    Download LOOT masterlist for a game, with caching.

    Returns path to masterlist YAML, or None on failure.
    """
    game_domain = game_domain.lower()
    if game_domain not in LOOT_GAME_MAP:
        return None

    repo = LOOT_GAME_MAP[game_domain]["repo"]
    cache_path = MASTERLIST_CACHE_DIR / f"{repo}.yaml"

    # Use cached version if it exists and is less than 24 hours old
    if cache_path.exists():
        import time
        age_hours = (time.time() - cache_path.stat().st_mtime) / 3600
        if age_hours < 24:
            return cache_path

    # Download from GitHub
    url = f"https://raw.githubusercontent.com/loot/{repo}/v0.21/masterlist.yaml"
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(response.content)
        return cache_path
    except Exception:
        # Try without version tag
        try:
            url = f"https://raw.githubusercontent.com/loot/{repo}/master/masterlist.yaml"
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(response.content)
            return cache_path
        except Exception:
            # Return cached version if available, even if stale
            if cache_path.exists():
                return cache_path
            return None


def sort_plugins_with_loot(
    game_domain: str,
    mods_dir: Path,
    existing_plugins: list[str] | None = None,
) -> list[str] | None:
    """
    Sort plugins using libloot.

    Args:
        game_domain: Nexus game domain
        mods_dir: Directory containing mod files
        existing_plugins: Optional pre-existing plugin list (from collection metadata)

    Returns:
        Sorted list of plugin filenames, or None if LOOT is unavailable.
    """
    if not is_loot_available():
        return None

    game_domain = game_domain.lower()
    if game_domain not in LOOT_GAME_MAP:
        return None

    try:
        import loot

        game_info = LOOT_GAME_MAP[game_domain]

        # Download masterlist
        masterlist_path = download_masterlist(game_domain)

        # Find all plugins in the mods directory
        found_plugins = find_plugins(mods_dir)
        if not found_plugins:
            return existing_plugins

        # Create a temporary game directory structure for LOOT
        with tempfile.TemporaryDirectory(prefix="nexus-loot-") as tmp_dir:
            tmp_path = Path(tmp_dir)
            data_dir = tmp_path / "Data"
            data_dir.mkdir()

            # Symlink plugin files into the Data directory
            for plugin_name in found_plugins:
                for plugin_path in mods_dir.rglob(plugin_name):
                    if plugin_path.is_file():
                        link = data_dir / plugin_name
                        if not link.exists():
                            link.symlink_to(plugin_path)
                        break

            # Initialize LOOT
            game_type = getattr(loot.GameType, game_info["loot_game"].replace(" ", "").replace(":", "").replace("'", ""), None)
            if game_type is None:
                # Try common attribute names
                for attr_name in dir(loot.GameType):
                    if attr_name.lower().replace("_", "") == game_domain.replace("_", ""):
                        game_type = getattr(loot.GameType, attr_name)
                        break

            if game_type is None:
                return existing_plugins

            db = loot.create_game_handle(game_type, str(tmp_path))

            if masterlist_path and masterlist_path.exists():
                db.load_lists(str(masterlist_path), "")

            # Get sorted plugins
            sorted_plugins = db.sort_plugins(found_plugins)
            return sorted_plugins

    except Exception:
        # LOOT sorting failed — return None to fall back to collection order
        return None


def merge_plugin_orders(
    collection_plugins: list[dict[str, Any]],
    loot_sorted: list[str] | None,
) -> list[str]:
    """
    Merge LOOT-sorted plugins with collection plugin metadata.

    If LOOT sorting succeeded, use LOOT order but preserve enabled/disabled status
    from collection. If LOOT failed, use collection order.
    """
    if loot_sorted:
        # Build enabled set from collection
        enabled = set()
        for p in collection_plugins:
            if p.get("enabled", True):
                enabled.add(p.get("filename", "").lower())

        result = []
        for plugin in loot_sorted:
            prefix = "*" if plugin.lower() in enabled else ""
            result.append(f"{prefix}{plugin}")
        return result
    else:
        # Fall back to collection order
        result = []
        for p in collection_plugins:
            filename = p.get("filename", "")
            if not filename:
                continue
            prefix = "*" if p.get("enabled", True) else ""
            result.append(f"{prefix}{filename}")
        return result


def write_loot_plugins_txt(
    plugins: list[str],
    path: Path,
    game_domain: str,
    used_loot: bool,
) -> None:
    """Write the final plugins.txt with LOOT-sorted or collection-based order."""
    source = "LOOT-sorted" if used_loot else "collection metadata"
    lines = [
        f"# Plugin Load Order ({source})",
        "# Generated by nexus-collection-dl",
        f"# Game: {game_domain}",
        "#",
        "# Plugins prefixed with * are enabled.",
        "",
    ]
    lines.extend(plugins)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
