"""Deploy mods from staging directory to game directory."""

import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Bethesda plugin/archive extensions
BETHESDA_EXTENSIONS = {".esm", ".esp", ".esl", ".ba2"}

# Known asset directories that belong under Data/
ASSET_DIRS = {
    "geometries",
    "textures",
    "meshes",
    "scripts",
    "sound",
    "materials",
    "interface",
    "video",
    "terrain",
    "strings",
    "music",
    "shadersfx",
    "vis",
    "seq",
    "lodsettings",
    "grass",
    "facegen",
    "dialogueviews",
    "source",
}

# SFSE files that go in game root (not Data/)
SFSE_ROOT_PATTERNS = {"sfse_loader.exe", "sfse_1_0_32.dll", "sfse_steam_loader.dll"}

# Files/dirs to skip during deployment
SKIP_PATTERNS = {
    "fomod",
    ".nexus-state.json",
    "load-order.txt",
    "plugins.txt",
    "__folder_managed_by_vortex",
}

SKIP_EXTENSIONS = {".txt", ".md", ".jpg", ".jpeg", ".png", ".gif", ".pdf", ".log"}

# Starfield INI settings for mod support
GAME_INI_SETTINGS = {
    "starfield": {
        "filename": "StarfieldCustom.ini",
        "sections": {
            "Archive": {
                "bInvalidateOlderFiles": "1",
                "sResourceDataDirsFinal": "",
            },
        },
    },
    "skyrimspecialedition": {
        "filename": "SkyrimCustom.ini",
        "sections": {
            "Archive": {
                "bInvalidateOlderFiles": "1",
                "sResourceDataDirsFinal": "",
            },
        },
    },
    "fallout4": {
        "filename": "Fallout4Custom.ini",
        "sections": {
            "Archive": {
                "bInvalidateOlderFiles": "1",
                "sResourceDataDirsFinal": "",
            },
        },
    },
}

# Game names for Proton Documents path
GAME_DOC_NAMES = {
    "starfield": "Starfield",
    "skyrimspecialedition": "Skyrim Special Edition",
    "fallout4": "Fallout4",
}

# Game names for Proton AppData/Local path
GAME_APPDATA_NAMES = {
    "starfield": "Starfield",
    "skyrimspecialedition": "Skyrim Special Edition",
    "fallout4": "Fallout4",
}

# Pattern for numbered option folders (e.g., "00 - Base", "01 - Main Files")
NUMBERED_OPTION_RE = re.compile(r"^\d{2,3}\s*[-_]\s*")

# Pattern for versioned SFSE directories (e.g., "sfse_0_2_18")
SFSE_VERSION_DIR_RE = re.compile(r"^sfse_[\d_]+$", re.IGNORECASE)


@dataclass
class DeployedFile:
    """Record of a single deployed file."""

    src: str
    dest: str
    method: str  # "symlink" or "copy"

    def to_dict(self) -> dict:
        return {"src": self.src, "dest": self.dest, "method": self.method}

    @classmethod
    def from_dict(cls, data: dict) -> "DeployedFile":
        return cls(src=data["src"], dest=data["dest"], method=data["method"])


@dataclass
class DeploymentPlan:
    """Classification of files to deploy."""

    game_root_files: list[tuple[Path, Path]] = field(default_factory=list)
    data_files: list[tuple[Path, Path]] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def total_files(self) -> int:
        return len(self.game_root_files) + len(self.data_files)


@dataclass
class DeployResult:
    """Result of a deployment operation."""

    deployed: list[DeployedFile] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def classify_file(rel_path: Path, game_domain: str) -> tuple[str, Path] | None:
    """
    Classify a single file and return (target_base, relative_dest) or None to skip.

    target_base is "root" for game root or "data" for Data/ directory.
    """
    parts = rel_path.parts
    name = rel_path.name
    name_lower = name.lower()
    suffix_lower = rel_path.suffix.lower()

    # Skip metadata/docs
    if name_lower in SKIP_PATTERNS or name_lower.startswith("readme"):
        return None
    if suffix_lower in SKIP_EXTENSIONS and suffix_lower not in BETHESDA_EXTENSIONS:
        return None
    if any(p.lower() in SKIP_PATTERNS for p in parts):
        return None

    # SFSE root files (sfse_loader.exe, sfse_*.dll) - deploy to game root
    if name_lower.startswith("sfse_") and suffix_lower in (".exe", ".dll"):
        return ("root", Path(name))

    # Handle SFSE/Plugins/ at various depths
    lower_parts = [p.lower() for p in parts]
    if "sfse" in lower_parts:
        sfse_idx = lower_parts.index("sfse")
        remainder = Path(*parts[sfse_idx:])
        # Strip leading "data/" if present
        if lower_parts[0] == "data" and sfse_idx == 1:
            return ("data", remainder)
        elif sfse_idx == 0:
            return ("data", remainder)

    # Explicit Data/ prefix
    if lower_parts[0] == "data" and len(parts) > 1:
        remainder = Path(*parts[1:])
        return ("data", remainder)

    # Loose plugin/archive files at root
    if len(parts) == 1 and suffix_lower in BETHESDA_EXTENSIONS:
        return ("data", Path(name))

    # Known asset directories at root level
    if lower_parts[0] in ASSET_DIRS:
        return ("data", rel_path)

    # Any other file - try to deploy under Data/
    # This catches things like Meshes/, Textures/ nested inside subdirs
    for i, part_lower in enumerate(lower_parts):
        if part_lower in ASSET_DIRS:
            return ("data", Path(*parts[i:]))

    # If it's a DLL at root, might be an SFSE plugin or ASI loader
    if len(parts) == 1 and suffix_lower == ".dll":
        return ("root", Path(name))

    # INI files at root - could be game config
    if len(parts) == 1 and suffix_lower == ".ini":
        return ("root", Path(name))

    # Unknown file - deploy under Data/ as fallback for anything with
    # a game-relevant extension, skip otherwise
    return ("data", rel_path)


def classify_files(mods_dir: Path, game_domain: str) -> DeploymentPlan:
    """Walk the staging directory and classify all files for deployment."""
    plan = DeploymentPlan()

    for file_path in sorted(mods_dir.rglob("*")):
        if not file_path.is_file():
            continue

        rel = file_path.relative_to(mods_dir)
        parts = rel.parts

        # Skip hidden/tool files at root
        if parts[0].startswith("."):
            plan.skipped.append((rel, "hidden file"))
            continue

        # Strip wrapper directories (numbered options, SFSE version dirs)
        effective_parts = list(parts)
        while effective_parts and (
            NUMBERED_OPTION_RE.match(effective_parts[0])
            or SFSE_VERSION_DIR_RE.match(effective_parts[0])
        ):
            effective_parts = effective_parts[1:]

        if not effective_parts:
            plan.skipped.append((rel, "empty after stripping option folders"))
            continue

        effective_rel = Path(*effective_parts)
        result = classify_file(effective_rel, game_domain)

        if result is None:
            plan.skipped.append((rel, "metadata/docs"))
            continue

        target_base, dest_rel = result
        if target_base == "root":
            plan.game_root_files.append((file_path, dest_rel))
        else:
            plan.data_files.append((file_path, dest_rel))

    return plan


def _deploy_file(src: Path, dest: Path, method: str) -> None:
    """Deploy a single file via symlink or copy."""
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.is_symlink() or dest.exists():
        if dest.is_symlink():
            dest.unlink()
        elif dest.is_file():
            dest.unlink()

    if method == "symlink":
        dest.symlink_to(src.resolve())
    else:
        shutil.copy2(src, dest)


def deploy(
    plan: DeploymentPlan,
    game_dir: Path,
    method: str = "symlink",
    dry_run: bool = False,
) -> DeployResult:
    """Execute a deployment plan."""
    result = DeployResult()
    data_dir = game_dir / "Data"

    # Deploy game root files (SFSE, root DLLs)
    for src, dest_rel in plan.game_root_files:
        dest = game_dir / dest_rel
        if dry_run:
            result.deployed.append(DeployedFile(str(src), str(dest), method))
            continue
        try:
            _deploy_file(src, dest, method)
            result.deployed.append(DeployedFile(str(src), str(dest), method))
        except OSError as e:
            result.errors.append(f"{dest_rel}: {e}")

    # Deploy Data/ files
    seen_dests: dict[Path, Path] = {}
    for src, dest_rel in plan.data_files:
        dest = data_dir / dest_rel

        # Track conflicts (multiple sources -> same dest)
        if dest in seen_dests:
            result.conflicts.append(
                f"{dest_rel}: overwritten by {src.name} (was {seen_dests[dest].name})"
            )
        seen_dests[dest] = src

        if dry_run:
            result.deployed.append(DeployedFile(str(src), str(dest), method))
            continue
        try:
            _deploy_file(src, dest, method)
            result.deployed.append(DeployedFile(str(src), str(dest), method))
        except OSError as e:
            result.errors.append(f"{dest_rel}: {e}")

    return result


def undeploy(deployed_files: list[dict]) -> int:
    """Remove all deployed files. Returns count of files removed."""
    removed = 0
    for entry in deployed_files:
        dest = Path(entry["dest"])
        if dest.is_symlink() or dest.exists():
            try:
                dest.unlink()
                removed += 1
                # Clean up empty parent dirs up to Data/ or game root
                _cleanup_empty_parents(dest.parent)
            except OSError:
                pass
    return removed


def _cleanup_empty_parents(directory: Path) -> None:
    """Remove empty parent directories, stopping at Data/ or game root."""
    try:
        while directory.name and directory.name not in ("Data", "data"):
            if not any(directory.iterdir()):
                directory.rmdir()
                directory = directory.parent
            else:
                break
    except OSError:
        pass


def write_plugins_txt(src: Path, dest: Path) -> bool:
    """Copy plugins.txt from staging dir to game's AppData path."""
    if not src.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return True


def write_game_ini(ini_path: Path, game_domain: str) -> bool:
    """Create or update the game's custom INI file for mod support."""
    settings = GAME_INI_SETTINGS.get(game_domain)
    if not settings:
        return False

    ini_path.parent.mkdir(parents=True, exist_ok=True)

    # Read existing content if present
    existing_lines = []
    if ini_path.exists():
        existing_lines = ini_path.read_text().splitlines()

    # Parse existing sections
    existing_sections: dict[str, dict[str, str]] = {}
    current_section = ""
    for line in existing_lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current_section = stripped[1:-1]
            existing_sections.setdefault(current_section, {})
        elif "=" in stripped and current_section:
            key, _, value = stripped.partition("=")
            existing_sections[current_section][key.strip()] = value.strip()

    # Merge our required settings
    for section, keys in settings["sections"].items():
        existing_sections.setdefault(section, {})
        for key, value in keys.items():
            existing_sections[section][key] = value

    # Write back
    lines = []
    for section, keys in existing_sections.items():
        lines.append(f"[{section}]")
        for key, value in keys.items():
            lines.append(f"{key}={value}")
        lines.append("")

    ini_path.write_text("\n".join(lines))
    return True


def get_plugins_txt_dest(prefix: Path, game_domain: str) -> Path | None:
    """Get the destination path for plugins.txt in the Proton prefix."""
    appdata_name = GAME_APPDATA_NAMES.get(game_domain)
    if not appdata_name:
        return None
    return (
        prefix
        / "drive_c"
        / "users"
        / "steamuser"
        / "AppData"
        / "Local"
        / appdata_name
        / "plugins.txt"
    )


def get_game_ini_path(prefix: Path, game_domain: str) -> Path | None:
    """Get the destination path for the game's custom INI in the Proton prefix."""
    doc_name = GAME_DOC_NAMES.get(game_domain)
    if not doc_name:
        return None
    ini_settings = GAME_INI_SETTINGS.get(game_domain)
    if not ini_settings:
        return None
    return (
        prefix
        / "drive_c"
        / "users"
        / "steamuser"
        / "Documents"
        / "My Games"
        / doc_name
        / ini_settings["filename"]
    )
