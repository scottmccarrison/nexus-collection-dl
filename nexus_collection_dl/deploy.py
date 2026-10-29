"""Deploy mods from staging directory to game directory.

Deployment is tracked and reversible:

- Every destination file is fingerprinted (SHA256 for copies, exact link
  target for symlinks) so nexus-dl always knows whether a file on disk is
  still the one it deployed.
- Pre-existing files at a destination (regular files or symlinks) are backed
  up byte-for-byte, including permissions and symlink targets, in a private
  content-addressed store under the collection's state directory
  (``.nexus-backups/``). Undeploy restores those originals.
- Files that were modified outside nexus-dl after deployment are refused on
  removal/replacement instead of being silently destroyed.
- Each mutation is journaled (``.nexus-journal.jsonl``) before and after it
  happens, so an interrupted deploy/undeploy can be recovered on the next run
  without guessing.
"""

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

# Bethesda plugin/archive extensions
BETHESDA_EXTENSIONS = {".esm", ".esp", ".esl", ".ba2"}

# BG3 pak file extension
BG3_PAK_EXTENSION = ".pak"

# BG3 virtual texture extensions (go to Data/ directly, not in pak)
BG3_VIRTUAL_TEXTURE_EXTENSIONS = {".gts", ".gtp"}

# BG3 loose file extensions that go into Data/ structure
BG3_LOOSE_EXTENSIONS = {
    ".dds", ".DDS",  # textures
    ".gr2", ".GR2",  # Granny2 animations/models
    ".lsf", ".lsx",  # Larian save formats
    ".cur",          # cursors
    ".ttf", ".otf",  # fonts
    ".xaml",         # UI
}

# BG3 native mod paths - go to game bin/ not Data/
BG3_NATIVE_MOD_PATHS = {
    "bin",
    "nativemods",
}

# Known asset directories that belong under Data/ (Bethesda)
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
    "__macosx",
}

SKIP_EXTENSIONS = {".txt", ".md", ".jpg", ".jpeg", ".png", ".gif", ".pdf", ".log",
                   ".url", ".lnk", ".json"}

# Starfield/Skyrim/Fallout INI settings for mod support
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
    kind: str = "mod"  # "mod", "plugins", or "ini"

    def to_dict(self) -> dict:
        return {
            "src": self.src,
            "dest": self.dest,
            "method": self.method,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DeployedFile":
        return cls(
            src=data["src"],
            dest=data["dest"],
            method=data["method"],
            kind=data.get("kind", "mod"),
        )

    @classmethod
    def from_record(cls, record: dict) -> "DeployedFile":
        return cls(
            src=record.get("src", ""),
            dest=record["dest"],
            method=record.get("method", ""),
            kind=record.get("kind", "mod"),
        )


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


def _is_bg3_game(game_domain: str) -> bool:
    """Check if this is a Baldur's Gate 3 deployment."""
    return game_domain.lower() in ("baldursgate3", "baldurs_gate_3", "bg3")


def classify_file(rel_path: Path, game_domain: str) -> tuple[str, Path] | None:
    """
    Classify a single file and return (target_base, relative_dest) or None to skip.

    target_base values:
        "root"    - game installation root (e.g. bin/)
        "data"    - game Data/ directory
        "staging" - stays in mod staging dir (e.g. .pak files for BG3)

    For BG3:
        .pak files stay in the staging/collection directory and are symlinked to Data/
        bin/ and NativeMods/ go to the game's bin/ directory
        Loose files (DDS, GR2, etc.) with a Data/ prefix go to game Data/
        Virtual textures (.gts, .gtp) go to game Data/
    """
    parts = rel_path.parts
    name = rel_path.name
    name_lower = name.lower()
    suffix_lower = rel_path.suffix.lower()
    lower_parts = [p.lower() for p in parts]

    # Skip metadata/docs/junk
    if name_lower in {p.lower() for p in SKIP_PATTERNS}:
        return None
    if name_lower.startswith("readme"):
        return None
    if suffix_lower in SKIP_EXTENSIONS and suffix_lower not in BETHESDA_EXTENSIONS:
        return None
    if any(p.lower() in SKIP_PATTERNS for p in parts):
        return None
    # Skip Mac OS metadata folders
    if "__macosx" in lower_parts:
        return None

    # ── BG3-specific handling ──────────────────────────────────────────────────
    if _is_bg3_game(game_domain):
        # .pak files stay in the staging directory.
        # nexus-dl's deploy step symlinks all .pak files from staging → Data/
        if suffix_lower == ".pak":
            return ("staging", Path(name))

        # bin/ and NativeMods/ → game root bin/
        if lower_parts[0] in BG3_NATIVE_MOD_PATHS:
            return ("root", Path("bin") / Path(*parts[1:]))

        # Explicit Data/ prefix → strip it and deploy to game Data/
        if lower_parts[0] == "data" and len(parts) > 1:
            remainder = Path(*parts[1:])
            return ("data", remainder)

        # Generated/ prefix → deploy to game Data/Generated/
        if lower_parts[0] == "generated" and len(parts) > 1:
            return ("data", rel_path)

        # Virtual textures → Data/ root
        if suffix_lower in BG3_VIRTUAL_TEXTURE_EXTENSIONS:
            return ("data", Path(name))

        # Loose DDS/GR2/etc at root level → Data/ root
        if len(parts) == 1 and suffix_lower.lower() in {e.lower() for e in BG3_LOOSE_EXTENSIONS}:
            return ("data", Path(name))

        # Subfolder containing Data/ structure (e.g. "ModName/Data/Public/...")
        if "data" in lower_parts:
            data_idx = lower_parts.index("data")
            if data_idx < len(parts) - 1:
                remainder = Path(*parts[data_idx + 1:])
                return ("data", remainder)

        # DLL files at root → game bin/ (native mods)
        if len(parts) == 1 and suffix_lower == ".dll":
            return ("root", Path("bin") / "NativeMods" / name)

        # TOML/INI configs for native mods at root → game bin/NativeMods/
        if len(parts) == 1 and suffix_lower in (".toml", ".ini"):
            return ("root", Path("bin") / "NativeMods" / name)

        # Everything else for BG3 → skip (loose files without clear destination)
        return None

    # ── Non-BG3 (Bethesda) handling ───────────────────────────────────────────

    # SFSE root files
    if name_lower.startswith("sfse_") and suffix_lower in (".exe", ".dll"):
        return ("root", Path(name))

    # Handle SFSE/Plugins/ at various depths
    if "sfse" in lower_parts:
        sfse_idx = lower_parts.index("sfse")
        remainder = Path(*parts[sfse_idx:])
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

    # Asset dirs nested inside subdirs
    for i, part_lower in enumerate(lower_parts):
        if part_lower in ASSET_DIRS:
            return ("data", Path(*parts[i:]))

    # DLL at root → game root
    if len(parts) == 1 and suffix_lower == ".dll":
        return ("root", Path(name))

    # INI files at root
    if len(parts) == 1 and suffix_lower == ".ini":
        return ("root", Path(name))

    return ("data", rel_path)


def classify_files(
    mods_dir: Path,
    game_domain: str,
    mod_choices: dict[int, dict] | None = None,
) -> DeploymentPlan:
    """Walk the staging directory and classify all files for deployment."""
    from .fomod import build_fomod_skip_set
    fomod_skip = build_fomod_skip_set(mods_dir, mod_choices or {})

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

        # Skip unselected FOMOD option folders
        if parts[0] in fomod_skip:
            plan.skipped.append((rel, "unselected FOMOD option"))
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
            plan.skipped.append((rel, "skipped"))
            continue

        target_base, dest_rel = result

        if target_base == "staging":
            # For BG3 paks: they stay in place, deploy will symlink them to Data/
            plan.data_files.append((file_path, dest_rel))
        elif target_base == "root":
            plan.game_root_files.append((file_path, dest_rel))
        else:
            plan.data_files.append((file_path, dest_rel))

    return plan




# ---------------------------------------------------------------------------
# Safe deployment machinery
# ---------------------------------------------------------------------------

BACKUPS_DIRNAME = ".nexus-backups"
JOURNAL_FILENAME = ".nexus-journal.jsonl"

# Backup blobs are content-addressed: names are exactly a lowercase SHA256
# hex digest. Anything else (traversal, absolute paths, junk from a corrupted
# record) is rejected before it can steer reads or deletes outside the store.
_BACKUP_NAME_RE = re.compile(r"^[0-9a-f]{64}$")


def _check_backup_name(name: str) -> str:
    if not isinstance(name, str) or not _BACKUP_NAME_RE.match(name):
        raise DeployError(f"invalid backup reference in deployment record: {name!r}")
    return name


class DeployError(Exception):
    """Raised when a deployment operation is refused or fails.

    Refusals are deliberate: nexus-dl fails closed rather than destroy files
    it cannot positively identify as its own.
    """
    pass


class UndeployError(DeployError):
    """Raised when undeploy could not safely remove everything.

    Carries structured detail so CLI/web callers can show exactly which
    paths were refused or failed instead of reporting a clean success.
    """

    def __init__(self, removed: int, refused: list[str], failed: list[str]):
        self.removed = removed
        self.refused = refused
        self.failed = failed
        parts = [f"Undeploy incomplete: removed {removed} file(s)."]
        if refused:
            parts.append(f"Refused ({len(refused)}):")
            parts.extend(f"  - {r}" for r in refused)
        if failed:
            parts.append(f"Failed ({len(failed)}):")
            parts.extend(f"  - {f}" for f in failed)
        parts.append(
            "State was kept for the remaining entries; fix the reported "
            "issues and run undeploy again."
        )
        super().__init__("\n".join(parts))


def sha256_file(path: Path) -> str:
    """SHA256 hex digest of a file's bytes. Raises DeployError if unreadable."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError as e:
        raise DeployError(f"cannot read {path}: {e}")
    return h.hexdigest()


def is_legacy_record(rec: dict) -> bool:
    """True for ownership records written before 0.2.1.

    Legacy records carry only src/dest/method: no deployment fingerprint and
    no backup information, so originals destroyed by pre-0.2.1 deploys cannot
    be restored and removals require a content match against the staging
    source.
    """
    return (
        "sha256" not in rec
        and "link_target" not in rec
        and "had_original" not in rec
    )


class BackupStore:
    """Content-addressed store for originals replaced during deployment.

    Backups live in a private directory (0700) under the collection's state
    directory and are named by the SHA256 of their content, so identical
    original content is stored once even when several destinations or
    repeated deploys reference it. Backup files are never deleted while any
    operation is failing; they are only dropped after their content has been
    verifiably restored to its original location.
    """

    def __init__(self, mods_dir: Path):
        self.dir = Path(mods_dir) / BACKUPS_DIRNAME

    def _ensure_dir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass

    def path_for(self, name: str) -> Path:
        return self.dir / _check_backup_name(name)

    def store_file(self, path: Path) -> tuple[str, int]:
        """Back up a regular file. Returns (backup_name, original_mode).

        The copy is staged inside the backup directory and renamed into
        place, and the copy's hash is verified against the pre-read hash so
        a file changing mid-backup is detected instead of stored.
        """
        path = Path(path)
        st = path.stat()
        mode = stat.S_IMODE(st.st_mode)
        sha = sha256_file(path)
        self._ensure_dir()
        final = self.dir / sha
        if final.exists():
            return sha, mode
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".tmp-backup-")
        try:
            h = hashlib.sha256()
            with os.fdopen(fd, "wb") as out, open(path, "rb") as src_f:
                while True:
                    chunk = src_f.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    h.update(chunk)
                out.flush()
                os.fsync(out.fileno())
            if h.hexdigest() != sha:
                raise DeployError(
                    f"{path} changed while it was being backed up; refusing "
                    f"to deploy over a moving file"
                )
            os.replace(tmp, final)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return sha, mode

    def verify(self, name: str, expected_sha: str) -> bool:
        """True when the named backup exists and hashes to expected_sha."""
        try:
            p = self.path_for(name)
        except DeployError:
            return False
        if not p.is_file():
            return False
        try:
            return sha256_file(p) == expected_sha
        except DeployError:
            return False

    def drop(self, name: str) -> None:
        """Remove a backup whose content is confirmed restored and unreferenced.

        Invalid names are ignored rather than resolved: a corrupted record
        must never steer a delete outside the backup store.
        """
        try:
            p = self.path_for(name)
        except DeployError:
            return
        try:
            p.unlink()
        except OSError:
            pass


def validate_dest(dest: Path, root: Path, create_parents: bool = False) -> None:
    """Ensure dest is safely contained within root before any mutation.

    Fails closed when:
      - dest is not absolute or lexically inside root,
      - any parent directory between root and dest is a symlink (so we never
        mutate through a link that points outside the game directory),
      - after parent creation, the fully resolved parent falls outside the
        fully resolved root.
    """
    dest = Path(dest)
    root = Path(root)
    if not dest.is_absolute():
        raise DeployError(f"refusing non-absolute destination path: {dest}")
    norm_dest = Path(os.path.normpath(dest))
    norm_root = Path(os.path.normpath(root))
    try:
        rel = norm_dest.relative_to(norm_root)
    except ValueError:
        raise DeployError(
            f"refusing destination outside the trusted directory: {norm_dest} "
            f"is not inside {norm_root}"
        )
    cur = norm_root
    for part in rel.parts[:-1]:
        cur = cur / part
        if cur.is_symlink():
            raise DeployError(
                f"refusing to mutate through symlinked parent directory: {cur}"
            )
    if create_parents:
        dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.parent.exists():
        raise DeployError(
            f"destination parent directory does not exist: {dest.parent}"
        )
    real_parent = Path(os.path.realpath(dest.parent))
    real_root = Path(os.path.realpath(norm_root))
    if real_parent != real_root and real_root not in real_parent.parents:
        raise DeployError(
            f"refusing destination whose resolved parent {real_parent} falls "
            f"outside the resolved trusted directory {real_root}"
        )


def _pick_root(dest: Path, roots: list[Path]) -> Path | None:
    """Return the most specific trusted root containing dest, or None."""
    norm = Path(os.path.normpath(dest))
    best: Path | None = None
    for root in roots:
        r = Path(os.path.normpath(root))
        try:
            norm.relative_to(r)
        except ValueError:
            continue
        if best is None or len(str(r)) > len(str(best)):
            best = r
    return best


def fingerprint_state(dest: Path, rec: dict) -> tuple[str, str | None, str | None]:
    """Classify what currently sits at dest relative to an ownership record.

    Returns (state, actual_sha256, actual_link_target) where state is one of:
      "deployed" - dest still holds exactly what the record deployed
      "original" - dest holds the pre-deployment original recorded in rec
      "absent"   - dest does not exist
      "unknown"  - something else (externally modified/replaced)

    Legacy records are matched against their staging source instead of a
    recorded fingerprint.
    """
    try:
        st = os.lstat(dest)
    except FileNotFoundError:
        return "absent", None, None
    except OSError:
        return "unknown", None, None

    if stat.S_ISLNK(st.st_mode):
        try:
            target = os.readlink(dest)
        except OSError:
            return "unknown", None, None
        link_target = rec.get("link_target")
        if link_target is not None:
            if target == link_target:
                return "deployed", None, target
            if rec.get("orig_link_target") is not None and target == rec["orig_link_target"]:
                return "original", None, target
            return "unknown", None, target
        # Legacy symlink record: the deployed link pointed at the source.
        if rec.get("method") == "symlink":
            src = rec.get("src", "")
            try:
                resolved = str(Path(src).resolve())
            except OSError:
                resolved = ""
            if src and target in (src, resolved):
                return "deployed", None, target
        return "unknown", None, target

    if stat.S_ISREG(st.st_mode):
        rec_sha = rec.get("sha256")
        if rec_sha:
            try:
                actual = sha256_file(dest)
            except DeployError:
                return "unknown", None, None
            if actual == rec_sha:
                return "deployed", actual, None
            if rec.get("orig_sha256") and actual == rec["orig_sha256"]:
                return "original", actual, None
            return "unknown", actual, None
        # Symlink-method records carry no content sha: a regular file at
        # dest can only be the recorded original or something external.
        # (Legacy records lack sha256, link_target, AND had_original, so
        # they never have orig_sha256 and fall through to the source match.)
        if rec.get("orig_sha256"):
            try:
                actual = sha256_file(dest)
            except DeployError:
                return "unknown", None, None
            if actual == rec["orig_sha256"]:
                return "original", actual, None
            return "unknown", actual, None
        # Legacy copy record: compare against the staging source content.
        src = rec.get("src", "")
        src_path = Path(src)
        if src and src_path.is_file():
            try:
                actual = sha256_file(dest)
                if actual == sha256_file(src_path):
                    return "deployed", actual, None
            except DeployError:
                return "unknown", None, None
        return "unknown", None, None

    return "unknown", None, None


def matches_deployed(dest: Path, rec: dict) -> bool:
    """True when dest still holds exactly what rec deployed."""
    return fingerprint_state(dest, rec)[0] == "deployed"


class DeployJournal:
    """Append-only journal of deployment mutations.

    An intent line is fsynced before each destination mutation and a
    completion line is appended after it. recover_ownership() folds the
    journal into the saved ownership records on the next run, resolving
    dangling intents by fingerprinting destinations, so an interrupted
    deploy/undeploy never requires guessing and never loses track of files.
    """

    def __init__(self, mods_dir: Path):
        self.path = Path(mods_dir) / JOURNAL_FILENAME

    def exists(self) -> bool:
        return self.path.exists()

    def append(self, entry: dict) -> None:
        entry = dict(entry)
        entry["v"] = 1
        entry["ts"] = datetime.now(timezone.utc).isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps(entry) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def deploy_intent(self, record: dict) -> None:
        self.append({"op": "deploy_intent", "record": record})

    def deploy_done(self, dest: str) -> None:
        self.append({"op": "deploy_done", "dest": str(dest)})

    def undeploy_intent(self, dest: str) -> None:
        self.append({"op": "undeploy_intent", "dest": str(dest)})

    def undeploy_done(self, dest: str) -> None:
        self.append({"op": "undeploy_done", "dest": str(dest)})

    def clear(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass

    def read(self) -> list[dict]:
        entries: list[dict] = []
        try:
            with open(self.path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        entries.append({"op": "corrupt"})
        except FileNotFoundError:
            return []
        except OSError:
            return []
        return entries


def _stage_symlink(parent: Path, target: str) -> Path | None:
    """Create a uniquely named symlink in parent for atomic os.replace publish."""
    for _ in range(10):
        candidate = Path(parent) / f".nexus-link-{os.getpid()}-{uuid4().hex[:12]}"
        try:
            os.symlink(target, candidate)
            return candidate
        except FileExistsError:
            continue
        except OSError:
            return None
    return None


def _restore_original(
    dest: Path, rec: dict, backups: BackupStore, pre_verified: bool = False
) -> tuple[bool, str]:
    """Restore the recorded original at dest, atomically replacing whatever
    is currently there (typically the deployed file).

    The original is staged beside dest and published with os.replace only
    after its hash is re-verified, so dest is never left empty and a corrupt
    backup copy never replaces the deployed file. Returns (ok, message).
    """
    orig_target = rec.get("orig_link_target")
    if orig_target:
        tmp_link = _stage_symlink(dest.parent, orig_target)
        if tmp_link is None:
            return False, f"could not stage the original symlink for {dest}"
        try:
            os.replace(tmp_link, dest)
            return True, ""
        except OSError as e:
            return False, f"could not restore original symlink at {dest}: {e}"
        finally:
            try:
                tmp_link.unlink(missing_ok=True)  # no-op once replaced
            except OSError:
                pass

    backup_name = rec.get("backup")
    orig_sha = rec.get("orig_sha256")
    if not backup_name or not orig_sha:
        return False, f"no backup recorded for the original of {dest}; cannot restore"
    if not pre_verified and not backups.verify(backup_name, orig_sha):
        return False, (
            f"backup of the original of {dest} is missing or corrupt "
            f"({backup_name}); refusing to restore"
        )
    try:
        backup_path = backups.path_for(backup_name)
    except DeployError as e:
        return False, str(e)

    tmp: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), prefix=".nexus-restore-")
        tmp = Path(tmp_name)
        h = hashlib.sha256()
        with os.fdopen(fd, "wb") as out, open(backup_path, "rb") as bf:
            while True:
                chunk = bf.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                h.update(chunk)
            out.flush()
            os.fsync(out.fileno())
        if h.hexdigest() != orig_sha:
            return False, (
                f"backup of the original of {dest} failed verification during "
                f"restore (corrupt copy); the deployed file was left in place"
            )
        mode = rec.get("orig_mode")
        if isinstance(mode, int):
            os.chmod(tmp, mode)
        os.replace(tmp, dest)
        tmp = None
    except OSError as e:
        return False, f"could not restore original at {dest}: {e}"
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
    return True, ""


def recover_ownership(
    deployed_files: list[dict], mods_dir: Path, roots: list[Path]
) -> tuple[dict[str, dict], list[str]]:
    """Fold the mutation journal into the saved ownership records.

    Returns (ownership, reports):
      ownership: dest -> record, exactly one effective record per destination
      reports: human-readable notes about recovered or kept entries

    `roots` are the trusted directories from configuration (game dir, proton
    prefix), never from journal content. Any recovery mutation (restoring an
    original) is containment-validated against those roots first; a journal
    entry pointing outside them is kept for review and never touched.
    Dangling intents (crash between intent and completion) are resolved by
    fingerprinting the destination against the record. Files that cannot be
    positively identified are kept for review, never deleted, and backups are
    retained whenever a recovery step fails.
    """
    ownership: dict[str, dict] = {}
    for rec in deployed_files:
        if isinstance(rec, dict) and rec.get("dest"):
            ownership[str(rec["dest"])] = rec

    journal = DeployJournal(mods_dir)
    entries = journal.read()
    if not entries:
        return ownership, []

    backups = BackupStore(mods_dir)
    reports: list[str] = []
    pending_deploys: dict[str, dict] = {}
    pending_undeploys: list[str] = []

    def _safe_restore(dest_str: str, rec: dict) -> tuple[bool, str]:
        """Restore an original only inside a trusted, validated location."""
        dest = Path(dest_str)
        root = _pick_root(dest, roots)
        if root is None:
            return False, (
                "destination is not inside any trusted game/prefix directory "
                "on record; refusing to restore"
            )
        try:
            validate_dest(dest, root, create_parents=False)
        except DeployError as e:
            return False, str(e)
        return _restore_original(dest, rec, backups)

    for entry in entries:
        op = entry.get("op")
        if op == "deploy_intent":
            rec = entry.get("record")
            if isinstance(rec, dict) and rec.get("dest"):
                pending_deploys[str(rec["dest"])] = rec
        elif op == "deploy_done":
            dest = str(entry.get("dest", ""))
            rec = pending_deploys.pop(dest, None)
            if rec is not None:
                ownership[dest] = rec
            else:
                reports.append(
                    f"journal: deploy completion without a matching intent for "
                    f"{dest} (ignored)"
                )
        elif op == "undeploy_intent":
            dest = str(entry.get("dest", ""))
            if dest not in pending_undeploys:
                pending_undeploys.append(dest)
        elif op == "undeploy_done":
            dest = str(entry.get("dest", ""))
            if dest in pending_undeploys:
                pending_undeploys.remove(dest)
            ownership.pop(dest, None)
        elif op == "corrupt":
            reports.append("journal: skipped an unreadable journal line")

    # Dangling deploy intents: interrupted after backup/intent, before or
    # after install, without the completion line.
    for dest_str, rec in pending_deploys.items():
        dest = Path(dest_str)
        fp = fingerprint_state(dest, rec)[0]
        if fp == "deployed":
            ownership[dest_str] = rec
            reports.append(
                f"recovered {dest}: install completed but was not recorded; "
                f"now tracked"
            )
        elif fp == "original":
            reports.append(
                f"recovered {dest}: interrupted before install; original left "
                f"in place"
            )
        elif fp == "absent":
            if rec.get("had_original"):
                ok, msg = _safe_restore(dest_str, rec)
                if ok:
                    reports.append(
                        f"recovered {dest}: restored the original after an "
                        f"interrupted deploy"
                    )
                else:
                    ownership[dest_str] = rec
                    reports.append(
                        f"{dest}: interrupted deploy and restore failed: {msg}"
                    )
            # No original recorded: nothing was there, nothing was installed.
        else:
            ownership[dest_str] = rec
            reports.append(
                f"{dest}: an interrupted deploy left unexpected content at "
                f"the destination; keeping the record for review (nothing "
                f"was deleted)"
            )

    # Dangling undeploy intents: interrupted midway through removal.
    for dest_str in pending_undeploys:
        rec = ownership.get(dest_str)
        if rec is None:
            continue
        dest = Path(dest_str)
        fp = fingerprint_state(dest, rec)[0]
        if fp == "deployed":
            reports.append(
                f"{dest}: interrupted undeploy; removal will be retried on "
                f"the next run"
            )
        elif fp == "original":
            ownership.pop(dest_str, None)
            reports.append(
                f"recovered {dest}: undeploy had already restored the original"
            )
        elif fp == "absent":
            if rec.get("had_original"):
                ok, msg = _safe_restore(dest_str, rec)
                if ok:
                    ownership.pop(dest_str, None)
                    reports.append(
                        f"recovered {dest}: restored the original after an "
                        f"interrupted undeploy"
                    )
                else:
                    reports.append(
                        f"{dest}: interrupted undeploy and restore failed: {msg}"
                    )
            else:
                ownership.pop(dest_str, None)
        else:
            reports.append(
                f"{dest}: an interrupted undeploy left unexpected content at "
                f"the destination; keeping the record for review (nothing "
                f"was deleted)"
            )

    return ownership, reports


def prepare_deploy(
    src: Path,
    dest: Path,
    method: str,
    root: Path,
    backups: BackupStore,
    prior: dict | None = None,
    kind: str = "mod",
) -> tuple[dict, dict]:
    """Validate one src -> dest deployment and secure any original backup.

    Returns (record, current_state). The only mutation performed here is
    securing the backup of an unowned original. Raises DeployError whenever
    deploying would be unsafe or irreversible:

      - destination outside the trusted root, or reached through a symlinked
        parent directory,
      - destination is a directory or special file,
      - destination holds a file we deployed that was modified externally
        (mod files only; shared config files are re-merged instead),
      - source is missing or unreadable.

    `prior` is the previous ownership record for this destination (from an
    earlier run or earlier in this run), or None.
    """
    src = Path(src)
    dest = Path(dest)

    ours = False
    inherit: dict | None = None
    external = False
    if prior is not None:
        fp = fingerprint_state(dest, prior)[0]
        if fp == "deployed":
            ours = True
            inherit = prior
        elif fp == "absent":
            # Our file was deleted externally; reinstalling destroys nothing.
            inherit = prior
        elif fp == "original":
            # Destination holds the pristine original again (e.g. a manual
            # restore): treat it as an unowned original and back it up.
            inherit = None
        elif kind == "mod":
            raise DeployError(
                f"refused: {dest} was changed outside nexus-dl after it was "
                f"deployed; not replacing it (restore or remove it manually, "
                f"then re-run)"
            )
        else:
            # Shared config files (plugins.txt, game INI) are also written by
            # the game itself. External edits are re-merged, and the pristine
            # original backup from the first tracked write is carried forward.
            inherit = prior
            external = True

    validate_dest(dest, root, create_parents=True)

    try:
        st = os.lstat(dest)
    except FileNotFoundError:
        st = None
    except OSError as e:
        raise DeployError(f"cannot inspect destination {dest}: {e}")

    if st is not None and not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
        raise DeployError(
            f"refused: {dest} is not a regular file or symlink; cannot deploy over it"
        )

    had_original = False
    orig_sha: str | None = None
    orig_target: str | None = None
    orig_mode: int | None = None
    backup_name: str | None = None

    if inherit is not None and inherit.get("had_original"):
        had_original = True
        orig_sha = inherit.get("orig_sha256")
        orig_target = inherit.get("orig_link_target")
        orig_mode = inherit.get("orig_mode")
        backup_name = inherit.get("backup")

    if st is not None and not ours:
        if inherit is not None:
            # Config re-merge: pristine-original fields were already carried
            # from inherit above (which may legitimately record "no
            # original"); just secure the current externally-managed content
            # so nothing is overwritten without a backup.
            if stat.S_ISREG(st.st_mode):
                backups.store_file(dest)
        elif stat.S_ISLNK(st.st_mode):
            orig_target = os.readlink(dest)
            had_original = True
        else:
            backup_name, orig_mode = backups.store_file(dest)
            orig_sha = backup_name  # content-addressed: name is the sha256
            had_original = True

    # Planned deployment fingerprint: a snapshot of what we are installing.
    if method == "symlink":
        resolved = Path(src).resolve()
        if not resolved.is_file():
            raise DeployError(
                f"refused: source file is missing or not a regular file: {src}"
            )
        link_target: str | None = str(resolved)
        sha: str | None = None
    elif method == "copy":
        if not src.is_file():
            raise DeployError(
                f"refused: source file is missing or not a regular file: {src}"
            )
        link_target = None
        sha = sha256_file(src)
    else:
        raise DeployError(f"unknown deploy method: {method}")

    record = {
        "src": str(src),
        "dest": str(dest),
        "method": method,
        "kind": kind,
        "sha256": sha,
        "link_target": link_target,
        "had_original": had_original,
        "orig_sha256": orig_sha,
        "orig_link_target": orig_target,
        "orig_mode": orig_mode,
        "backup": backup_name,
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    # Snapshot of the destination's current state so install_deploy can
    # detect races between prepare and install (TOCTOU).
    current: dict = {"exists": st is not None, "external": external,
                     "sha256": None, "link_target": None}
    if st is not None:
        if stat.S_ISLNK(st.st_mode):
            current["link_target"] = os.readlink(dest)
        else:
            current["sha256"] = sha256_file(dest)

    return record, current


def install_deploy(
    rec: dict, src: Path, current: dict, backups: BackupStore
) -> None:
    """Install a prepared record. The journal intent must already be written.

    Re-checks the destination against the state prepare_deploy observed and
    refuses if it changed in between. Copies are staged in the destination
    directory (same filesystem) and atomically renamed into place, so the
    destination is never left half-written.
    """
    dest = Path(rec["dest"])
    src = Path(src)

    try:
        st = os.lstat(dest)
    except FileNotFoundError:
        st = None
    except OSError as e:
        raise DeployError(f"cannot inspect destination {dest}: {e}")

    if (st is not None) != current["exists"]:
        raise DeployError(
            f"refused: {dest} appeared or disappeared during deploy; re-run deploy"
        )
    if st is not None:
        if current.get("external"):
            # Externally managed config content: re-secure if it changed
            # again between prepare and install, then proceed.
            if stat.S_ISREG(st.st_mode) and sha256_file(dest) != current.get("sha256"):
                backups.store_file(dest)
        elif stat.S_ISLNK(st.st_mode):
            if os.readlink(dest) != current.get("link_target"):
                raise DeployError(
                    f"refused: {dest} changed during deploy; re-run deploy"
                )
        elif stat.S_ISREG(st.st_mode):
            if sha256_file(dest) != current.get("sha256"):
                raise DeployError(
                    f"refused: {dest} changed during deploy; re-run deploy"
                )
        else:
            raise DeployError(
                f"refused: {dest} is not a regular file or symlink"
            )

    # Publish atomically: never remove the destination before its
    # replacement is staged and verified, so a mid-install failure leaves
    # whatever is currently at dest (the original or our previous deploy)
    # completely untouched.
    if rec["method"] == "symlink":
        tmp_link = _stage_symlink(dest.parent, rec["link_target"])
        if tmp_link is None:
            raise DeployError(f"could not stage the symlink for {dest}")
        try:
            os.replace(tmp_link, dest)
        except OSError as e:
            raise DeployError(f"could not publish the symlink at {dest}: {e}")
        finally:
            try:
                tmp_link.unlink(missing_ok=True)  # no-op once replaced
            except OSError:
                pass
        return

    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".nexus-deploy-")
    try:
        h = hashlib.sha256()
        with os.fdopen(fd, "wb") as out, open(src, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                h.update(chunk)
            out.flush()
            os.fsync(out.fileno())
        if h.hexdigest() != rec["sha256"]:
            raise DeployError(
                f"source file {src} changed during deploy; refusing to "
                f"install an inconsistent copy"
            )
        try:
            os.chmod(tmp, stat.S_IMODE(src.stat().st_mode))
        except OSError:
            pass
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def deploy_tracked_copy(
    src: Path,
    dest: Path,
    root: Path,
    backups: BackupStore,
    journal: DeployJournal,
    ownership: dict[str, dict],
    kind: str = "mod",
) -> dict:
    """Deploy one file with backup, journal, and ownership tracking."""
    dest_str = str(Path(dest))
    prior = ownership.get(dest_str)
    record, current = prepare_deploy(
        src, dest, "copy", root, backups, prior=prior, kind=kind
    )
    journal.deploy_intent(record)
    install_deploy(record, src, current, backups)
    # Installed: record ownership first; a lost done line recovers by
    # fingerprint on the next run.
    ownership[dest_str] = record
    try:
        journal.deploy_done(dest_str)
    except OSError:
        pass
    return record


def deploy_tracked_content(
    dest: Path,
    data: bytes,
    root: Path,
    backups: BackupStore,
    journal: DeployJournal,
    ownership: dict[str, dict],
    kind: str,
) -> dict:
    """Write generated content to dest with backup and ownership tracking.

    Used for the game INI, whose content is merged from the existing file
    rather than copied from staging. The pristine original from the first
    tracked write is what undeploy restores.
    """
    dest = Path(dest)
    dest_str = str(dest)
    inherit = ownership.get(dest_str)

    validate_dest(dest, root, create_parents=True)
    try:
        st = os.lstat(dest)
    except FileNotFoundError:
        st = None
    except OSError as e:
        raise DeployError(f"cannot inspect destination {dest}: {e}")
    if st is not None and not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
        raise DeployError(
            f"refused: {dest} is not a regular file or symlink; cannot write over it"
        )

    had_original = False
    orig_sha: str | None = None
    orig_target: str | None = None
    orig_mode: int | None = None
    backup_name: str | None = None
    if inherit is not None and inherit.get("had_original"):
        had_original = True
        orig_sha = inherit.get("orig_sha256")
        orig_target = inherit.get("orig_link_target")
        orig_mode = inherit.get("orig_mode")
        backup_name = inherit.get("backup")

    if st is not None:
        if inherit is not None:
            # Rewrite of a tracked config: original fields were inherited
            # above; secure the current content before overwriting.
            if stat.S_ISREG(st.st_mode):
                backups.store_file(dest)
        elif stat.S_ISLNK(st.st_mode):
            orig_target = os.readlink(dest)
            had_original = True
        else:
            backup_name, orig_mode = backups.store_file(dest)
            orig_sha = backup_name
            had_original = True

    record = {
        "src": "",
        "dest": dest_str,
        "method": "copy",
        "kind": kind,
        "sha256": hashlib.sha256(data).hexdigest(),
        "link_target": None,
        "had_original": had_original,
        "orig_sha256": orig_sha,
        "orig_link_target": orig_target,
        "orig_mode": orig_mode,
        "backup": backup_name,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    journal.deploy_intent(record)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".nexus-deploy-")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        # Atomic overwrite: never unlink dest first, so a write failure
        # leaves the current content untouched.
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # Installed: record ownership first so the file stays tracked even when
    # the completion line cannot be written.
    ownership[dest_str] = record
    try:
        journal.deploy_done(dest_str)
    except OSError:
        pass  # a missing done line recovers by fingerprint on the next run
    return record


def _cleanup_empty_parents(directory: Path, stop_root: Path) -> None:
    """Remove empty directories below stop_root after a successful removal.

    Never removes stop_root itself, anything above it, directories named
    Data/data (part of the game layout), symlinks, or non-empty directories.
    """
    stop = Path(os.path.normpath(stop_root))
    try:
        directory = Path(os.path.normpath(directory))
        while stop in directory.parents and directory.name.lower() != "data":
            if directory.is_symlink() or not directory.is_dir():
                break
            if any(directory.iterdir()):
                break
            directory.rmdir()
            directory = directory.parent
    except OSError:
        pass


def undeploy_record(
    rec: dict, roots: list[Path], backups: BackupStore
) -> tuple[str, str]:
    """Safely remove one owned deployment and restore its original.

    Returns (status, message) where status is:
      "removed"  - our file removed, no original existed
      "restored" - our file removed and the original restored byte-identical
      "gone"     - destination was already clear; stale record dropped
      "refused"  - destination changed externally or is untrusted; untouched
      "failed"   - removal/restore could not complete safely; nothing lost

    Refusals never delete anything: a file that no longer matches the
    recorded fingerprint may be user data.
    """
    dest_str = str(rec.get("dest", ""))
    dest = Path(dest_str)
    root = _pick_root(dest, roots)
    if root is None:
        return "refused", (
            f"refused: {dest} is not inside any trusted game/prefix directory "
            f"on record; not touching it"
        )
    try:
        if dest.parent.exists():
            validate_dest(dest, root, create_parents=False)
    except DeployError as e:
        return "refused", str(e)

    try:
        st = os.lstat(dest)
    except FileNotFoundError:
        st = None
    except OSError as e:
        return "failed", f"{dest}: cannot inspect destination: {e}"

    kind = rec.get("kind", "mod")

    if is_legacy_record(rec):
        if st is None:
            return "gone", ""
        fp = fingerprint_state(dest, rec)[0]
        if fp == "deployed":
            try:
                os.unlink(dest)
            except OSError as e:
                return "failed", f"{dest}: could not remove deployed file: {e}"
            _cleanup_empty_parents(dest.parent, root)
            return "removed", (
                f"{dest}: removed pre-0.2.1 deployment; any original file at "
                f"this path was not backed up by that version and cannot be "
                f"restored"
            )
        return "refused", (
            f"refused: {dest} does not match the pre-0.2.1 record (content "
            f"differs from the staging source or the source is gone); not "
            f"removing it"
        )

    if st is None:
        if rec.get("had_original"):
            ok, msg = _restore_original(dest, rec, backups)
            if ok:
                return "restored", (
                    f"{dest}: deployed file was already gone; restored the "
                    f"original"
                )
            return "failed", (
                f"{dest}: deployed file was already gone and restoring the "
                f"original failed: {msg}"
            )
        return "gone", ""

    fp = fingerprint_state(dest, rec)[0]
    note = ""
    if fp == "original":
        return "gone", f"{dest}: original already in place; cleared stale record"
    if fp == "unknown":
        if kind == "mod":
            return "refused", (
                f"refused: {dest} was changed outside nexus-dl after it was "
                f"deployed; not removing it (restore or delete it manually, "
                f"then re-run undeploy)"
            )
        # Shared config file edited by the game/user: secure the current
        # content, then restore the pristine original.
        if stat.S_ISREG(st.st_mode):
            backups.store_file(dest)
            note = " (externally modified content was backed up first)"
        elif not stat.S_ISLNK(st.st_mode):
            return "refused", f"refused: {dest} is not a regular file or symlink"

    # Verify the original can be restored BEFORE touching our file, then
    # restore it atomically OVER the deployed file: dest is never left empty
    # and a failed restore leaves the deployed file completely untouched.
    pre_verified = False
    if rec.get("had_original") and not rec.get("orig_link_target"):
        backup_name = rec.get("backup")
        orig_sha = rec.get("orig_sha256")
        if not backup_name or not orig_sha or not backups.verify(backup_name, orig_sha):
            return "failed", (
                f"{dest}: the backup of the original file is missing or "
                f"corrupt; refusing to remove the deployed file (restore the "
                f"backup in {backups.dir} or remove the file manually)"
            )
        pre_verified = True

    if rec.get("had_original"):
        ok, msg = _restore_original(dest, rec, backups, pre_verified=pre_verified)
        if not ok:
            return "failed", (
                f"{dest}: could not restore the original over the deployed "
                f"file: {msg}"
            )
        return "restored", (f"{dest}: restored the original file{note}" if note else "")

    try:
        os.unlink(dest)
    except OSError as e:
        return "failed", f"{dest}: could not remove deployed file: {e}"

    _cleanup_empty_parents(dest.parent, root)
    return "removed", ""


def undeploy_records(
    ownership: dict[str, dict],
    roots: list[Path],
    backups: BackupStore,
    journal: DeployJournal | None = None,
) -> tuple[list[dict], list[str], list[str], int]:
    """Safely remove every owned deployment.

    Mutates `ownership` in place: cleared entries are removed, refused or
    failed entries are kept so they stay retryable. Returns
    (kept_records, problems, notes, cleared_count) where problems are the
    refusal/failure messages for kept entries and notes are informational
    messages about cleared entries. Backups of successfully restored
    originals are dropped only when no remaining record references them; on
    any failure all backups are retained.
    """
    kept: list[dict] = []
    problems: list[str] = []
    notes: list[str] = []
    cleared = 0

    for dest_str in list(ownership.keys()):
        rec = ownership[dest_str]
        if journal is not None:
            journal.undeploy_intent(dest_str)
        status, msg = undeploy_record(rec, roots, backups)
        if status in ("removed", "restored", "gone"):
            ownership.pop(dest_str, None)
            cleared += 1
            backup_name = rec.get("backup")
            if status == "restored" and backup_name:
                if not any(
                    r.get("backup") == backup_name for r in ownership.values()
                ):
                    backups.drop(backup_name)
            if journal is not None:
                journal.undeploy_done(dest_str)
            if msg:
                notes.append(msg)
        else:
            kept.append(rec)
            problems.append(msg or f"{dest_str}: {status}")

    return kept, problems, notes, cleared


def deploy(
    plan: DeploymentPlan,
    game_dir: Path,
    method: str = "symlink",
    dry_run: bool = False,
    *,
    backups: BackupStore | None = None,
    journal: DeployJournal | None = None,
    ownership: dict[str, dict] | None = None,
) -> DeployResult:
    """Execute a deployment plan.

    Non-dry-run deployments require a BackupStore, DeployJournal, and an
    ownership dict (dest -> record) so every mutation is backed up,
    journaled, and reversible. `ownership` is updated in place and always
    holds exactly one effective record per destination: when mods conflict,
    the last file in deterministic (sorted) plan order wins, the original is
    backed up once, and the conflict is reported.
    """
    if not dry_run and (backups is None or journal is None or ownership is None):
        raise ValueError(
            "deploy() requires backups, journal, and ownership for "
            "non-dry-run deployments"
        )

    result = DeployResult()
    game_dir = Path(game_dir)
    data_dir = game_dir / "Data"

    def _deploy_one(src: Path, dest: Path, dest_rel: Path) -> None:
        if dry_run:
            result.deployed.append(DeployedFile(str(src), str(dest), method))
            return
        dest_str = str(dest)
        try:
            prior = ownership.get(dest_str)
            record, current = prepare_deploy(
                src, dest, method, game_dir, backups, prior=prior, kind="mod"
            )
            journal.deploy_intent(record)
            install_deploy(record, src, current, backups)
        except DeployError as e:
            result.errors.append(str(e))
            return
        except OSError as e:
            result.errors.append(f"{dest_rel}: {e}")
            return
        # Installed: record ownership before anything else so the file stays
        # tracked even if the completion line cannot be written.
        ownership[dest_str] = record
        result.deployed.append(DeployedFile.from_record(record))
        try:
            journal.deploy_done(dest_str)
        except OSError:
            pass  # a missing done line recovers by fingerprint on the next run

    # Game root files (native mods, SFSE, root DLLs)
    seen_dests: dict[Path, Path] = {}
    for src, dest_rel in plan.game_root_files:
        dest = game_dir / dest_rel
        if dest in seen_dests:
            result.conflicts.append(
                f"{dest_rel}: overwritten by {src.name} (was {seen_dests[dest].name})"
            )
        seen_dests[dest] = src
        _deploy_one(src, dest, dest_rel)

    # Data/ files (includes BG3 .pak symlinks)
    for src, dest_rel in plan.data_files:
        dest = data_dir / dest_rel
        if dest in seen_dests:
            result.conflicts.append(
                f"{dest_rel}: overwritten by {src.name} (was {seen_dests[dest].name})"
            )
        seen_dests[dest] = src
        _deploy_one(src, dest, dest_rel)

    return result


def write_plugins_txt(
    src: Path,
    dest: Path,
    *,
    root: Path,
    backups: BackupStore,
    journal: DeployJournal,
    ownership: dict[str, dict],
) -> dict | None:
    """Copy plugins.txt to the game's AppData path with full tracking.

    The pre-existing plugins.txt (if any) is backed up once and restored on
    undeploy. Returns the ownership record, or None when there is nothing to
    copy.
    """
    src = Path(src)
    if not src.exists():
        return None
    return deploy_tracked_copy(
        src, Path(dest), root, backups, journal, ownership, kind="plugins"
    )


def build_game_ini_content(ini_path: Path, game_domain: str) -> str | None:
    """Build the merged game INI content, or None for unknown games.

    Preserves existing sections/keys and applies the mod-support settings.
    """
    settings = GAME_INI_SETTINGS.get(game_domain)
    if not settings:
        return None

    existing_lines = []
    ini_path = Path(ini_path)
    if ini_path.exists():
        existing_lines = ini_path.read_text().splitlines()

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

    for section, keys in settings["sections"].items():
        existing_sections.setdefault(section, {})
        for key, value in keys.items():
            existing_sections[section][key] = value

    lines = []
    for section, keys in existing_sections.items():
        lines.append(f"[{section}]")
        for key, value in keys.items():
            lines.append(f"{key}={value}")
        lines.append("")

    return "\n".join(lines)


def write_game_ini(
    ini_path: Path,
    game_domain: str,
    *,
    root: Path,
    backups: BackupStore,
    journal: DeployJournal,
    ownership: dict[str, dict],
) -> dict | None:
    """Create or update the game's custom INI with full tracking.

    The user's original INI is backed up once, before the first tracked
    write, and restored byte-identical on undeploy. Later writes re-merge
    from the current content (the game itself edits these files), and the
    current content is secured before each overwrite.
    """
    content = build_game_ini_content(Path(ini_path), game_domain)
    if content is None:
        return None
    return deploy_tracked_content(
        Path(ini_path),
        content.encode("utf-8"),
        root,
        backups,
        journal,
        ownership,
        kind="ini",
    )


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
