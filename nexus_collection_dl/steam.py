"""Steam library detection and game path resolution."""

import os
import re
from pathlib import Path

# Map Nexus game domains to Steam app IDs
STEAM_APP_IDS = {
    "starfield": "1716740",
    "skyrimspecialedition": "489830",
    "fallout4": "377160",
    "falloutnewvegas": "22380",
    "fallout3": "22300",
    "baldursgate3": "1086940",
    "oblivion": "22330",
    "morrowind": "22320",
    "enderal": "933480",
    "enderalspecialedition": "976620",
}

# Common Steam install locations on Linux
STEAM_PATHS = [
    Path.home() / ".steam" / "debian-installation",
    Path.home() / ".steam" / "steam",
    Path.home() / ".local" / "share" / "Steam",
    Path("/usr/share/steam"),
]


def find_steam_root() -> Path | None:
    """Find the Steam installation root directory."""
    for path in STEAM_PATHS:
        vdf = path / "config" / "libraryfolders.vdf"
        if vdf.exists():
            return path
    return None


def parse_library_folders(steam_root: Path) -> list[Path]:
    """Parse libraryfolders.vdf to get all Steam library paths."""
    vdf_path = steam_root / "config" / "libraryfolders.vdf"
    if not vdf_path.exists():
        return []

    text = vdf_path.read_text()
    # Match "path" values in Valve KV1 format
    paths = []
    for match in re.finditer(r'"path"\s+"([^"]+)"', text):
        lib_path = Path(match.group(1))
        if lib_path.exists():
            paths.append(lib_path)

    return paths


def find_game_dir(game_domain: str) -> Path | None:
    """Find the game install directory by searching Steam libraries."""
    app_id = STEAM_APP_IDS.get(game_domain)
    if not app_id:
        return None

    steam_root = find_steam_root()
    if not steam_root:
        return None

    libraries = parse_library_folders(steam_root)
    for lib_path in libraries:
        manifest = lib_path / "steamapps" / f"appmanifest_{app_id}.acf"
        if manifest.exists():
            # Parse installdir from manifest
            text = manifest.read_text()
            match = re.search(r'"installdir"\s+"([^"]+)"', text)
            if match:
                game_dir = lib_path / "steamapps" / "common" / match.group(1)
                if game_dir.exists():
                    return game_dir

    return None


def find_proton_prefix(game_domain: str) -> Path | None:
    """Find the Proton/Wine prefix for a game."""
    app_id = STEAM_APP_IDS.get(game_domain)
    if not app_id:
        return None

    steam_root = find_steam_root()
    if not steam_root:
        return None

    libraries = parse_library_folders(steam_root)
    for lib_path in libraries:
        prefix = lib_path / "steamapps" / "compatdata" / app_id / "pfx"
        if prefix.exists():
            return prefix

    return None


def list_installed_games(steam_root: Path | None = None) -> list[dict]:
    """Games this tool can manage that are actually installed here.

    A real sweep, not a wishlist: every Steam library folder's
    ``appmanifest_*.acf``, mapped back through ``STEAM_APP_IDS``. A game
    Steam holds that has no Nexus domain mapping is not something this
    tool can manage, so it does not appear.

    ``steam_root`` overrides detection. ``NEXUS_DL_STEAM_ROOT`` overrides
    it too, which is how tests and unusual installs reach the sweep
    without touching the machine's real Steam.

    Returns [{"domain", "game_dir", "name"}], sorted by name. A machine
    with no Steam is a normal state and yields [].
    """
    root = steam_root
    if root is None:
        env = os.environ.get("NEXUS_DL_STEAM_ROOT")
        root = Path(env) if env else find_steam_root()
    if root is None or not root.exists():
        return []

    by_app_id = {app_id: domain for domain, app_id in STEAM_APP_IDS.items()}
    found: list[dict] = []
    for lib_path in parse_library_folders(root):
        steamapps = lib_path / "steamapps"
        for manifest in sorted(steamapps.glob("appmanifest_*.acf")):
            app_id = manifest.stem.split("_", 1)[-1]
            domain = by_app_id.get(app_id)
            if domain is None:
                continue
            try:
                text = manifest.read_text()
            except OSError:
                continue
            match = re.search(r'"installdir"\s+"([^"]+)"', text)
            if not match:
                continue
            name = match.group(1)
            game_dir = steamapps / "common" / name
            if not game_dir.is_dir():
                continue
            found.append({"domain": domain, "game_dir": str(game_dir), "name": name})

    found.sort(key=lambda g: g["name"].lower())
    return found
