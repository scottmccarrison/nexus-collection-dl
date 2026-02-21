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
    # Try magic bytes first
    try:
        with open(filepath, "rb") as f:
            header = f.read(8)

        # ZIP: PK (0x50 0x4B)
        if header[:2] == b"PK":
            return "zip"
        # 7z: 7z signature
        if header[:6] == b"7z\xbc\xaf'\x1c":
            return "7z"
        # RAR: Rar!
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
    elif suffix == ".rar":
        return "rar"

    return None


def extract_archive(archive_path: Path, target_dir: Path) -> list[Path]:
    """
    Extract an archive to the target directory.

    Returns list of extracted file paths.
    """
    archive_type = detect_archive_type(archive_path)
    if archive_type is None:
        raise ExtractionError(f"Unknown archive type: {archive_path}")

    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        if archive_type == "zip":
            return _extract_zip(archive_path, target_dir)
        elif archive_type == "7z":
            return _extract_7z(archive_path, target_dir)
        elif archive_type == "rar":
            return _extract_rar(archive_path, target_dir)
    except Exception as e:
        raise ExtractionError(f"Failed to extract {archive_path}: {e}")

    return []


def _extract_zip(archive_path: Path, target_dir: Path) -> list[Path]:
    """Extract a ZIP archive."""
    extracted = []
    with zipfile.ZipFile(archive_path, "r") as zf:
        for member in zf.namelist():
            # Skip directories
            if member.endswith("/"):
                continue
            zf.extract(member, target_dir)
            extracted.append(target_dir / member)
    return extracted


def _extract_7z(archive_path: Path, target_dir: Path) -> list[Path]:
    """Extract a 7z archive. Falls back to system 7z for unsupported codecs (e.g. BCJ2)."""
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
        # py7zr can't handle this codec - try system 7z
        return _extract_7z_system(archive_path, target_dir)


def _extract_7z_system(archive_path: Path, target_dir: Path) -> list[Path]:
    """Extract a 7z archive using the system 7z command."""
    sz_bin = shutil.which("7z") or shutil.which("7zz")
    if not sz_bin:
        raise ExtractionError(
            f"py7zr cannot extract {archive_path.name} (unsupported compression). "
            "Install p7zip-full (apt install p7zip-full) for broader 7z support."
        )

    result = subprocess.run(
        [sz_bin, "x", str(archive_path), f"-o{target_dir}", "-y"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ExtractionError(
            f"7z extraction failed for {archive_path.name}: {result.stderr.strip()}"
        )

    extracted = []
    for path in target_dir.rglob("*"):
        if path.is_file():
            extracted.append(path)
    return extracted


def _extract_rar(archive_path: Path, target_dir: Path) -> list[Path]:
    """Extract a RAR archive."""
    extracted = []
    with rarfile.RarFile(archive_path, "r") as rf:
        rf.extractall(target_dir)
        for member in rf.namelist():
            path = target_dir / member
            if path.is_file():
                extracted.append(path)
    return extracted


def is_archive(filepath: Path) -> bool:
    """Check if a file is a supported archive."""
    return detect_archive_type(filepath) is not None


def move_file(src: Path, dest_dir: Path) -> Path:
    """Move a non-archive file to destination directory."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    src.rename(dest)
    return dest
