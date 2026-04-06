"""Archive extraction for mod files."""

import shutil
import subprocess
import zipfile
from pathlib import Path

import py7zr
import rarfile


class ExtractionError(Exception):
    """Raised when archive extraction fails."""
    pass


def detect_archive_type(filepath: Path) -> str | None:
    """
    Detect archive type by magic bytes, then fall back to extension.

    Returns: 'zip', '7z', 'rar', or None if not an archive.
    """
    # Try magic bytes first (most reliable — extension can be wrong or missing)
    try:
        with open(filepath, "rb") as f:
            header = f.read(8)

        if header[:2] == b"PK":
            return "zip"
        if header[:6] == b"7z\xbc\xaf'\x1c":
            return "7z"
        if header[:4] == b"Rar!":
            return "rar"
    except (OSError, IOError):
        pass

    # Fall back to extension
    suffix = filepath.suffix.lower()
    if suffix == ".zip":
        return "zip"
    elif suffix == ".7z":
        return "7z"
    elif suffix in (".rar", ".r00"):
        return "rar"

    return None


def detect_archive_type_from_url(url: str) -> str | None:
    """
    Detect archive type from a CDN URL (before downloading).

    CDN URLs like https://cdn.nexusmods.com/files/ModName.rar?md5=...
    have the extension before the query string.
    """
    # Strip query string
    path = url.split("?")[0]
    suffix = Path(path).suffix.lower()

    if suffix == ".zip":
        return "zip"
    elif suffix == ".7z":
        return "7z"
    elif suffix in (".rar", ".r00"):
        return "rar"
    return None


def _move_staging_contents(staging_dir: Path, target_dir: Path) -> list[Path]:
    """Move all files from staging directory to target, preserving structure."""
    moved_files = []
    for src in staging_dir.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(staging_dir)
        dest = target_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        moved_files.append(dest)
    return moved_files


def extract_archive(archive_path: Path, target_dir: Path) -> list[Path]:
    """
    Extract an archive to the target directory, preserving internal structure.

    Uses a staging directory to avoid conflicts when the archive contains
    a top-level folder matching the archive filename.

    IMPORTANT: Structure is preserved (not flattened) because BG3 mods often
    have internal folders like ModName/Data/Public/... that must be kept intact
    for deploy.py to correctly classify files.

    Returns list of extracted file paths relative to target_dir.
    """
    archive_type = detect_archive_type(archive_path)
    if archive_type is None:
        raise ExtractionError(f"Unknown archive type: {archive_path}")

    target_dir.mkdir(parents=True, exist_ok=True)

    # Use a clean temp path to avoid special character issues with system tools
    staging_dir = Path("/tmp") / f"nexus_extract_{archive_path.stem[:20]}"

    try:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True)

        if archive_type == "zip":
            extracted = _extract_zip(archive_path, staging_dir)
        elif archive_type == "7z":
            extracted = _extract_7z(archive_path, staging_dir)
        elif archive_type == "rar":
            extracted = _extract_rar(archive_path, staging_dir)
        else:
            extracted = []

        # Move extracted files from staging to target, preserving structure
        final_files = _move_staging_contents(staging_dir, target_dir)
        return final_files

    except Exception as e:
        raise ExtractionError(f"Failed to extract {archive_path}: {e}")
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)


def _extract_zip(archive_path: Path, target_dir: Path) -> list[Path]:
    """Extract a ZIP archive, preserving directory structure."""
    extracted = []
    with zipfile.ZipFile(archive_path, "r") as zf:
        for member in zf.namelist():
            if member.endswith("/"):
                continue
            # ZipFile.extract preserves directory structure
            dest = zf.extract(member, target_dir)
            extracted.append(Path(dest))
    return extracted


def _extract_7z(archive_path: Path, target_dir: Path) -> list[Path]:
    """Extract a 7z archive. Falls back to system 7z for unsupported codecs."""
    try:
        extracted = []
        with py7zr.SevenZipFile(archive_path, "r") as szf:
            szf.extractall(target_dir)
            for name in szf.getnames():
                path = target_dir / name
                if path.is_file():
                    extracted.append(path)
        return extracted
    except (py7zr.UnsupportedCompressionMethodError, py7zr.Bad7zFile):
        return _extract_7z_system(archive_path, target_dir)


def _extract_7z_system(archive_path: Path, target_dir: Path) -> list[Path]:
    """Extract using system 7z command (handles BCJ2 and other complex codecs)."""
    sz_bin = shutil.which("7z") or shutil.which("7zz")
    if not sz_bin:
        raise ExtractionError(
            f"py7zr cannot extract {archive_path.name} (unsupported compression). "
            "Install p7zip-full (apt install p7zip-full / dnf install p7zip-plugins)."
        )

    # Use -o with the target_dir path directly (already a clean /tmp path)
    result = subprocess.run(
        [sz_bin, "x", str(archive_path), f"-o{target_dir}", "-y"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ExtractionError(
            f"7z extraction failed for {archive_path.name}: {result.stderr.strip()}"
        )

    return [p for p in target_dir.rglob("*") if p.is_file()]


def _extract_rar(archive_path: Path, target_dir: Path) -> list[Path]:
    """
    Extract a RAR archive.

    Tries rarfile (Python) first, then unar (system) as fallback.
    unar handles RAR5 which rarfile may not support on all systems.
    """
    # Try rarfile first
    try:
        extracted = []
        with rarfile.RarFile(archive_path, "r") as rf:
            rf.extractall(target_dir)
            for member in rf.namelist():
                path = target_dir / member
                if path.is_file():
                    extracted.append(path)
        return extracted
    except Exception:
        pass

    # Fall back to unar (handles RAR5)
    unar_bin = shutil.which("unar")
    if unar_bin:
        result = subprocess.run(
            [unar_bin, "-o", str(target_dir), "-f", str(archive_path)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return [p for p in target_dir.rglob("*") if p.is_file()]

    # Try system unrar
    unrar_bin = shutil.which("unrar")
    if unrar_bin:
        result = subprocess.run(
            [unrar_bin, "x", "-y", str(archive_path), str(target_dir) + "/"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return [p for p in target_dir.rglob("*") if p.is_file()]

    raise ExtractionError(
        f"Cannot extract RAR: {archive_path.name}. "
        "Install unar (apt install unar / dnf install unar) for RAR5 support."
    )


def is_archive(filepath: Path) -> bool:
    """Check if a file is a supported archive."""
    return detect_archive_type(filepath) is not None


def move_file(src: Path, dest_dir: Path) -> Path:
    """Move a non-archive file to destination directory."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    src.rename(dest)
    return dest
