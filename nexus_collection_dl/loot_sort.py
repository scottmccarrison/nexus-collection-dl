"""Optional LOOT integration for Bethesda game plugin sorting."""

import os
import tempfile
from pathlib import Path
from typing import Any

import requests

# Map Nexus game domains to LOOT game identifiers and masterlist repos.
# `game_type` is the exact libloot GameType member name; it cannot be derived
# from the display name (Skyrim Special Edition is `SkyrimSE`, not
# `SkyrimSpecialEdition`).
LOOT_GAME_MAP: dict[str, dict[str, str]] = {
    "starfield": {"game_type": "Starfield", "repo": "starfield"},
    "skyrimspecialedition": {"game_type": "SkyrimSE", "repo": "skyrimse"},
    "skyrim": {"game_type": "Skyrim", "repo": "skyrim"},
    "fallout4": {"game_type": "Fallout4", "repo": "fallout4"},
    "falloutnv": {"game_type": "FalloutNV", "repo": "falloutnv"},
    "fallout3": {"game_type": "Fallout3", "repo": "fallout3"},
    "oblivion": {"game_type": "Oblivion", "repo": "oblivion"},
    "morrowind": {"game_type": "Morrowind", "repo": "morrowind"},
    "fallout4vr": {"game_type": "Fallout4VR", "repo": "fallout4vr"},
    "skyrimvr": {"game_type": "SkyrimVR", "repo": "skyrimvr"},
    "enderal": {"game_type": "Enderal", "repo": "enderal"},
    "enderalspecialedition": {"game_type": "EnderalSE", "repo": "enderalse"},
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


def loot_support(game_domain: str) -> tuple[bool, str]:
    """Whether LOOT can sort this game, and why not when it cannot.

    The answer, not a guess. Enderal and Enderal SE have a masterlist
    but no libloot ``GameType``, so they cannot be sorted and fall back
    to the collection order. That fallback used to be silent: the user
    had no idea LOOT had not run. Every non-sortable case now carries a
    reason a caller can print.

    Returns ``(can_sort, reason)``. ``reason`` is empty exactly when
    ``can_sort`` is True.
    """
    if not is_loot_available():
        return False, "libloot is not installed, so plugins are not LOOT-sorted"
    domain = game_domain.lower()
    if domain not in LOOT_GAME_MAP:
        return False, f"{game_domain} is not a LOOT-supported game"
    game_type_name = LOOT_GAME_MAP[domain].get("game_type", "")
    if not game_type_name:
        return (
            False,
            (
                f"LOOT has no game type for {game_domain}; plugins stay in "
                "collection order"
            ),
        )
    try:
        import loot
    except Exception:
        return False, "the libloot bindings could not be imported"
    if getattr(loot.GameType, game_type_name, None) is None:
        return (
            False,
            (
                f"this libloot build has no {game_type_name} game type; "
                "plugins stay in collection order"
            ),
        )
    return True, ""


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

        # Enderal has a masterlist but no libloot GameType, so it cannot be sorted.
        game_type_name = game_info.get("game_type", "")
        if not game_type_name:
            return existing_plugins

        # Create a temporary game directory structure for LOOT
        with tempfile.TemporaryDirectory(prefix="nexus-loot-") as tmp_dir:
            tmp_path = Path(tmp_dir)
            data_dir = tmp_path / "Data"
            data_dir.mkdir()
            # libloot needs a local app data folder as well as the game folder,
            # and will not fall back to detecting one inside a temp dir.
            local_dir = tmp_path / "local"
            local_dir.mkdir()

            # Symlink plugin files into the Data directory, keeping the names
            # LOOT will be asked to sort.
            loaded_plugins: list[str] = []
            for plugin_name in found_plugins:
                for plugin_path in mods_dir.rglob(plugin_name):
                    if plugin_path.is_file():
                        link = data_dir / plugin_name
                        if not link.exists():
                            link.symlink_to(plugin_path)
                        loaded_plugins.append(plugin_name)
                        break

            if not loaded_plugins:
                return existing_plugins

            game_type = getattr(loot.GameType, game_type_name, None)
            if game_type is None:
                return existing_plugins

            # libloot 0.29 exposes loot.Game and game.database(). The older
            # binding this was written against used create_game_handle(),
            # Database.load_lists() and Database.sort_plugins(), none of which
            # exist any more, so the old call sequence silently sorted nothing.
            game = loot.Game(game_type, str(tmp_path), str(local_dir))

            if masterlist_path and masterlist_path.exists():
                game.database().load_masterlist(str(masterlist_path))

            # sort_plugins raises PluginNotLoadedError on names that were not
            # loaded first, so load before sorting.
            game.load_plugins([str(data_dir / name) for name in loaded_plugins])
            return game.sort_plugins(loaded_plugins)

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
