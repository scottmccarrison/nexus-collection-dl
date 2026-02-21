"""Steam library detection and game path resolution."""

import re
from pathlib import Path


# Map Nexus game domains to Steam app IDs
STEAM_APP_IDS = {
    "starfield": "1716740",
    "skyrimspecialedition": "489830",
    "fallout4": "377160",
    "falloutnewvegas": "22380",
    "fallout3": "22300",
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
