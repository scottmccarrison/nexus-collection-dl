"""Archive extraction for mod files."""

import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import py7zr
import rarfile

from .errmsg import exception_reason


class ExtractionError(Exception):
    """Raised when archive extraction fails."""


class FomodInstallerError(ExtractionError):
    """The archive carries a FOMOD installer this tool cannot read.

    Script-based installers (``fomod/script.cs``, ``fomod/script.lua``)
    compute their own install from game state; there are no option groups
    to present and nothing to parse. Rather than extract every file the
    script might or might not want, callers refuse and keep the archive.
    """

    def __init__(self, member: str, archive_path: Path) -> None:
        self.member = member
        self.archive_path = archive_path
        super().__init__(
            f"{member} marks this archive as a FOMOD mod with a script-based "
            f"installer. Script installers decide what to install themselves "
            f"and nexus-dl cannot read them, so there are no options to pick "
            f"and extracting now would install every file the script might "
            f"want, including ones meant to be mutually exclusive. The "
            f"archive is kept at {archive_path}. Install it with a tool that "
            f"runs FOMOD scripts, or extract it yourself with --no-extract."
        )


class FomodChoicesError(ExtractionError):
    """The archive carries a readable installer and no choices were given.

    Distinct from :class:`FomodInstallerError` on purpose: that one means
    we cannot read the installer at all, this one means the caller has
    not said which options to install. A harness can branch on the code.
    """

    def __init__(self, archive_name: str) -> None:
        self.archive_name = archive_name
        super().__init__(
            f"{archive_name} is a FOMOD mod and needs option choices before "
            f"it can be extracted. Pass --fomod-option 'Group:Option' "
            f"(repeatable) or --fomod-config <file.json>, or run this "
            f"interactively and answer the prompts. Extracting without a "
            f"choice would install every option at once, including options "
            f"meant to be mutually exclusive."
        )


class FomodUnreadableError(ExtractionError):
    """The archive is a FOMOD mod, but its option groups could not be read.

    Distinct from an uninspectable archive, which is not proof of anything
    and is deliberately left alone. Here the installer was found, so the
    archive is a FOMOD mod and extracting it would install every option at
    once. The message is the refusal.
    """

    def __init__(self, member: str, archive_path: Path) -> None:
        self.member = member
        self.archive_path = archive_path
        super().__init__(
            f"found {member} in {archive_path.name} but could not read it, "
            f"so its option groups are unknown. Extracting without them "
            f"could install every option at once. The archive is kept at "
            f"{archive_path}. Use --no-extract and extract it yourself if "
            f"you know what it should install."
        )


def sniff_archive_magic(filepath: Path) -> str | None:
    """Detect archive type by magic bytes alone.

    Returns 'zip', '7z', 'rar', or None when the content does not start with
    a known archive signature (or the file cannot be read). Unlike
    detect_archive_type this never falls back to the filename extension, so
    callers can tell real archive content from an error page or truncated
    download saved under an archive name.
    """
    try:
        with open(filepath, "rb") as f:
            header = f.read(8)
    except OSError:
        return None

    if header[:2] == b"PK":
        return "zip"
    if header[:6] == b"7z\xbc\xaf'\x1c":
        return "7z"
    if header[:4] == b"Rar!":
        return "rar"
    return None


def detect_archive_type(filepath: Path) -> str | None:
    """
    Detect archive type by magic bytes, then fall back to extension.

    Returns: 'zip', '7z', 'rar', or None if not an archive.
    """
    # Try magic bytes first (most reliable — extension can be wrong or missing)
    magic = sniff_archive_magic(filepath)
    if magic:
        return magic

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
    a top-level folder matching the archive filename. The staging
    directory is private to this call: a unique hidden mkdtemp directory
    (mode 0700) inside target_dir, never a shared predictable name, so
    concurrent extractions of same-named archives cannot delete or mix
    each other's staged content. Staging on the destination filesystem
    keeps the final move a same-device rename, and the dot prefix keeps
    staged content invisible to the import and deploy scanners until it
    is published under its final names. Staging is removed on success
    and on every failure path.

    IMPORTANT: Structure is preserved (not flattened) because BG3 mods often
    have internal folders like ModName/Data/Public/... that must be kept intact
    for deploy.py to correctly classify files.

    Returns list of extracted file paths relative to target_dir.
    """
    archive_type = detect_archive_type(archive_path)
    if archive_type is None:
        raise ExtractionError(f"Unknown archive type: {archive_path}")

    target_dir.mkdir(parents=True, exist_ok=True)

    staging_dir: Path | None = None
    try:
        # Unique per call and created 0700: mkdtemp never collides with a
        # concurrent extraction, and system tools (7z/unar) receive the
        # path through argv lists, so special characters in the target
        # directory's path are safe.
        staging_dir = Path(
            tempfile.mkdtemp(prefix=".nexus-extract-", dir=target_dir)
        )

        if archive_type == "zip":
            _extract_zip(archive_path, staging_dir)
        elif archive_type == "7z":
            _extract_7z(archive_path, staging_dir)
        elif archive_type == "rar":
            _extract_rar(archive_path, staging_dir)
        else:
            pass

        # Move extracted files from staging to target, preserving structure
        final_files = _move_staging_contents(staging_dir, target_dir)
        return final_files

    except Exception as e:
        raise ExtractionError(f"Failed to extract {archive_path}: {exception_reason(e)}")
    finally:
        if staging_dir is not None:
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

    # Use -o with the staging path directly; argv passing keeps any
    # characters in the path safe.
    result = subprocess.run(
        [sz_bin, "x", str(archive_path), f"-o{target_dir}", "-y"],
        capture_output=True,
        text=True,
        check=False,
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
    # rarfile cannot handle some archives (RAR5, corrupt, or no backend);
    # falling through to unar/unrar is the intended path, so a silent
    # except here is not a swallowed failure worth logging.
    except Exception:  # noqa: S110
        pass

    # Fall back to unar (handles RAR5)
    unar_bin = shutil.which("unar")
    if unar_bin:
        result = subprocess.run(
            [unar_bin, "-o", str(target_dir), "-f", str(archive_path)],
            capture_output=True,
            text=True,
            check=False,
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
            check=False,
        )
        if result.returncode == 0:
            return [p for p in target_dir.rglob("*") if p.is_file()]

    raise ExtractionError(
        f"Cannot extract RAR: {archive_path.name}. "
        "Install unar (apt install unar / dnf install unar) for RAR5 support."
    )


# FOMOD installer markers. A `fomod` folder alone is not proof: a mod may
# ship only `fomod/header.png`, which is not an installer and must not be
# refused. Requiring a known filename means an exotic installer can slip
# through, and that is the cheaper error of the two.
_FOMOD_MARKER_NAMES = frozenset({"moduleconfig.xml", "script.lua", "info.xml"})


def find_fomod_installer(archive_path: Path) -> str | None:
    """Return the member path proving this archive is a FOMOD mod, else None.

    Enumeration only; nothing is written and nothing is extracted. A
    member counts when its path has a ``fomod`` segment (any case, any
    depth) and its basename is one of ``ModuleConfig.xml``, ``script.lua``,
    or ``info.xml`` (any case). So ``fomod/ModuleConfig.xml``,
    ``Some Mod/fomod/script.lua`` and ``FOMOD/Info.XML`` all count.

    Returns None in two different situations and callers must tell them
    apart: None when the archive was read and carries no installer, and
    None when the archive could not be enumerated at all. Use
    ``can_enumerate_members`` before relying on the negative.
    """
    names = _list_archive_members(archive_path)
    if names is None:
        return None
    for name in names:
        if _fomod_marker_from_name(name) is not None:
            return name
    return None


def find_module_config(archive_path: Path) -> str | None:
    """The member path of this archive's ``ModuleConfig.xml``, or None.

    Narrower than :func:`find_fomod_installer`: an archive can carry a
    marker we cannot read (``script.cs``, ``info.xml`` alone) and that
    case must be told apart from one whose option groups we can parse.
    The two are different refusals with different remedies.
    """
    members = _list_archive_members(archive_path)
    if members is None:
        return None
    for name in members:
        marker = _fomod_marker_from_name(name)
        if marker is not None and marker.replace("\\", "/").rsplit("/", 1)[-1].lower() == "moduleconfig.xml":
            return marker
    return None


# A member read fails on the archive's own terms: no such member, a
# corrupt archive, or compression nothing here can decode. Programming
# errors are deliberately not in this set; a blanket ``except Exception``
# is what let a nonexistent py7zr method read as "could not read it".
_UNREADABLE_MEMBER_ERRORS = (
    KeyError,
    OSError,
    zipfile.BadZipFile,
    rarfile.Error,
    py7zr.exceptions.ArchiveError,
    py7zr.exceptions.PasswordRequired,
)


def read_archive_member(archive_path: Path, member: str) -> bytes | None:
    """Read one member's bytes without extracting the whole archive.

    Returns None when the member cannot be read, and callers turn that
    into a refusal. Failures that are not about the member at all are
    not converted into that answer.
    """
    archive_type = detect_archive_type(archive_path)
    try:
        if archive_type == "zip":
            with zipfile.ZipFile(archive_path, "r") as zf:
                return zf.read(member)
        if archive_type == "7z":
            return _read_7z_member(archive_path, member)
        if archive_type == "rar":
            with rarfile.RarFile(archive_path, "r") as rf:
                return rf.read(member)
    except _UNREADABLE_MEMBER_ERRORS:
        return None
    return None


def _read_7z_member(archive_path: Path, member: str) -> bytes | None:
    """Read one member of a 7z archive.

    py7zr has no single-member read, so the one member is extracted to a
    private temp directory. Archives using compression py7zr cannot
    decode (BCJ2 in particular, common in Nexus mods) fall back to
    system ``7z``, the same split :func:`extract_archive` uses.
    """
    try:
        with py7zr.SevenZipFile(archive_path, "r") as szf:
            target = _match_member_name(szf.getnames(), member)
            if target is None:
                return None
            with tempfile.TemporaryDirectory() as td:
                szf.extract(path=td, targets=[target])
                extracted = [p for p in Path(td).rglob("*") if p.is_file()]
                return extracted[0].read_bytes() if extracted else None
    except (py7zr.exceptions.ArchiveError, py7zr.exceptions.PasswordRequired):
        pass
    return _read_7z_member_system(archive_path, member)


def _match_member_name(names: list[str], member: str) -> str | None:
    """The stored name for ``member``, matched ignoring case and slashes."""
    wanted = member.replace("\\", "/").lower()
    for name in names:
        if name.replace("\\", "/").lower() == wanted:
            return name
    return None


def _read_7z_member_system(archive_path: Path, member: str) -> bytes | None:
    """Read one member through system 7z, which prints it to stdout."""
    sz_bin = shutil.which("7z") or shutil.which("7zz")
    if not sz_bin:
        return None
    result = subprocess.run(
        [sz_bin, "e", "-so", "-y", str(archive_path), member],
        capture_output=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def _fomod_marker_from_name(name: str) -> str | None:
    """Return ``name`` when it is a FOMOD installer member, else None.

    A member counts when its path has a ``fomod`` segment (any case, any
    depth) and its basename is one of ``ModuleConfig.xml``, ``script.lua``,
    or ``info.xml`` (any case). Separators may be ``/`` or ``\\``; real
    FOMOD configs use backslashes in their own ``source`` paths and
    archives carry both.
    """
    parts = [p for p in name.replace("\\", "/").split("/") if p]
    if len(parts) < 2:
        return None
    if parts[-2].lower() != "fomod":
        return None
    if parts[-1].lower() in _FOMOD_MARKER_NAMES:
        return name
    return None


def can_enumerate_members(archive_path: Path) -> bool:
    """True when the archive's member list can be read at all.

    The library path (zipfile, py7zr, rarfile) always can. The subprocess
    extraction fallbacks cannot be listed with what this module has, and
    those callers should warn rather than pretend the check ran.
    """
    return _list_archive_members(archive_path) is not None


def _list_archive_members(archive_path: Path) -> list[str] | None:
    """Member names without extracting, or None when enumeration fails.

    Returns None rather than raising because a failure here is not an
    extraction failure: it means we could not look, and the caller decides
    whether that is fatal. An empty list is a real answer (an empty
    archive) and is not the same as None.
    """
    archive_type = detect_archive_type(archive_path)
    try:
        if archive_type == "zip":
            with zipfile.ZipFile(archive_path, "r") as zf:
                return list(zf.namelist())
        if archive_type == "7z":
            with py7zr.SevenZipFile(archive_path, "r") as szf:
                return list(szf.getnames())
        if archive_type == "rar":
            with rarfile.RarFile(archive_path, "r") as rf:
                return list(rf.namelist())
    except Exception:
        return None
    # Unknown type, or one only the subprocess fallbacks can open.
    return None


def is_archive(filepath: Path) -> bool:
    """Check if a file is a supported archive."""
    return detect_archive_type(filepath) is not None


def move_file(src: Path, dest_dir: Path) -> Path:
    """Move a non-archive file to destination directory."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    src.rename(dest)
    return dest
