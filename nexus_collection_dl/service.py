"""Service layer - business logic extracted from CLI for programmatic use."""

import contextlib
import hashlib
import os
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .fomod_plan import ModuleConfig
    from .fomod_select import Pick

from .api import NexusAPI, NexusAPIError
from .collection import (
    CollectionParseError,
    parse_collection_url,
    parse_mod_url,
)
from .deploy import (
    BackupStore,
    DeployError,
    DeployJournal,
    UndeployError,
    classify_files,
    get_game_ini_path,
    get_plugins_txt_dest,
    recover_ownership,
    undeploy_records,
    write_game_ini,
    write_plugins_txt,
)
from .deploy import (
    deploy as deploy_files,
)
from .downloader import (
    Downloader,
    DownloadError,
    SignedLink,
    validate_download_filename,
)
from .errmsg import exception_reason
from .extractor import (
    ExtractionError,
    FomodChoicesError,
    FomodInstallerError,
    FomodUnreadableError,
    can_enumerate_members,
    extract_archive,
    find_fomod_installer,
    find_module_config,
    is_archive,
    read_archive_member,
    sniff_archive_magic,
)
from .fomod_plan import decode_config_text, load_module_config_bytes
from .fomod_select import PickError
from .handler import (
    DesktopInstaller,
    NxmRoutingError,
    _desktop_notification_setting,
    capability_warnings,
    notifications_enabled_in_desktop,
    resolve_collection_dir,
)
from .keystore import (
    ENV_VAR_NAME,
    SOURCE_ENV,
    SOURCE_FLAG,
    SOURCE_STORED,
    KeyStoreError,
    resolve_api_key,
)
from .loadorder import LoadOrderGenerator
from .loot_sort import (
    is_bethesda_game,
    is_loot_available,
    merge_plugin_orders,
    sort_plugins_with_loot,
    write_loot_plugins_txt,
)
from .manifest import CollectionManifest, ManifestError, download_and_parse_manifest
from .nxm import NxmLinkInfo, parse_nxm_url
from .state import (
    LOCK_FILENAME,
    STATE_FILENAME,
    CollectionLock,
    CollectionState,
    LoadOrderRegenError,
    StateError,
)
from .steam import STEAM_APP_IDS, find_game_dir, find_proton_prefix

# progress callback: (event_type, percentage 0-1, message)
ProgressCallback = Callable[[str, float, str], None]


def _sanitize_dirname(name: str) -> str:
    """Turn a collection name into a safe directory name."""
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = name.strip(". ")
    return name or "collection"


def _state_text(value: Any, fallback: str = "") -> str:
    """Coerce a state-derived value to plain text; never raises.

    Hand-edited state can hold non-string scalars where text is
    expected (a numeric collection_name, say). Scan/list output is
    normalized here so sorting and name matching work on real strings;
    display code keeps its own safe-stringify as a second line of
    defense.
    """
    if isinstance(value, str):
        return value
    if value is None:
        return fallback
    try:
        return str(value)
    except Exception:
        return fallback


def _fomod_config_for_archive(archive_path: Path) -> "ModuleConfig | None":
    """Read an archive's FOMOD installer config without extracting it.

    Returns None for an archive that is not a FOMOD mod. Raises for the
    two refusals: an archive this tool cannot enumerate at all (we do not
    know whether it is FOMOD, and extracting on a guess is the harm this
    exists to stop), and a script-based installer whose options are
    computed rather than declared. ``--no-extract`` is the escape hatch
    for a mod the caller knows is safe.
    """
    if not can_enumerate_members(archive_path):
        raise ExtractionError(
            f"could not inspect {archive_path.name} for a FOMOD installer: "
            f"this archive format cannot be listed here. Extracting on a "
            f"guess could install every option of a FOMOD mod at once. If "
            f"you know it is not a FOMOD mod, use --no-extract and extract "
            f"it yourself."
        )
    member = find_module_config(archive_path)
    if member is None:
        marker = find_fomod_installer(archive_path)
        if marker is not None:
            raise FomodInstallerError(marker, archive_path)
        return None

    raw = read_archive_member(archive_path, member)
    config = load_module_config_bytes(raw) if raw is not None else None
    if config is None:
        raise FomodUnreadableError(member, archive_path)
    return config


def _unused_picks_warning(name: str, no_extract: bool) -> str:
    """The message for picks the caller asked for that nothing could apply.

    Built away from the call sites so the text has one home: both the add
    and the import path report the same two causes the same way.
    """
    if no_extract:
        return (
            f"{name} was kept unextracted, so the requested FOMOD option "
            f"choices were not used. Extract it (or import it) to install "
            f"specific options."
        )
    return (
        f"{name} has no FOMOD installer, so the requested FOMOD option "
        f"choices were not used."
    )


def _fomod_config_record(archive_path: Path) -> dict[str, str] | None:
    """The archive's FOMOD installer config as a storable record.

    Returns ``{"member": <archive-relative path>, "xml": <text>}`` or None
    when the archive carries no ModuleConfig.xml or its bytes cannot be
    read. Unlike :func:`_fomod_config_for_archive` this never raises: it
    runs at extract time over archives that are being extracted anyway,
    and a mod with no record still has the in-tree config deploy
    discovers on its own.
    """
    if not can_enumerate_members(archive_path):
        return None
    member = find_module_config(archive_path)
    if member is None:
        return None
    raw = read_archive_member(archive_path, member)
    if raw is None:
        return None
    text = decode_config_text(raw)
    if text is None:
        return None
    return {"member": member, "xml": text}


def _nxm_fomod_blocker(archive_path: Path) -> str | None:
    """A message when this archive must not be extracted without choices.

    Only a positively identified FOMOD is blocked. An archive this tool
    cannot enumerate at all returns None: extraction has its own honest
    failure for that, and refusing every uninspectable archive would stop
    downloads that are not FOMOD at all.

    Extracting a FOMOD without its option choices installs the wrong
    content, and for the common all-Optional installer that content is
    nothing at all, which looks exactly like success. This is the click
    path's half of the rule ``add`` and ``import`` already follow.
    """
    try:
        config = _fomod_config_for_archive(archive_path)
    except FomodInstallerError as e:
        return exception_reason(e)
    except FomodUnreadableError as e:
        # The installer was found, so this is a FOMOD mod whatever its
        # bytes look like. Extracting it now is exactly the harm this
        # exists to stop.
        return exception_reason(e)
    except ExtractionError:
        # Not inspectable at all: not proof of a FOMOD mod, and blocking
        # every uninspectable download would stop plain mods too.
        return None
    if config is None:
        return None
    parent = archive_path.parent
    return (
        f"{archive_path.name} is a FOMOD mod and needs option choices before "
        f"it can be extracted; extracting it now would install the wrong "
        f"content. Run 'nexus-dl import {parent} --fomod-option "
        f"\"Group:Option\"' to install it with your choices. The archive is "
        f"kept at {archive_path}."
    )


def _resolve_fomod_picks(
    config: "ModuleConfig",
    archive_name: str,
    provided: "Sequence[Pick] | None",
    stored: "Sequence[dict[str, str]] | None",
    on_fomod_prompt: "Callable[..., Sequence[Pick]] | None",
) -> "list[Pick]":
    """Decide which options a FOMOD archive installs.

    Command-line picks win outright, and a pick that names nothing in the
    installer is an error rather than a silent mismatch. Without them, the
    picks stored from a previous install are kept as far as they still
    name real options and the rest is asked again when there is somebody
    to ask; with nobody to ask, the kept picks stand and the groups they
    no longer cover fall back to the installer's own defaults. With
    nothing at all, refuse: guessing is the harm this exists to stop.
    """
    from .fomod_select import Pick, PickError, normalize_picks, picks_to_selections

    if provided:
        return normalize_picks(provided, config)

    kept: list[Pick] = []
    if stored:
        stored_picks = [
            Pick(step=str(d.get("step", "")), group=str(d.get("group", "")),
                 option=str(d.get("option", "")))
            for d in stored
        ]
        try:
            selections, _unmatched = picks_to_selections(stored_picks, config)
        except PickError:
            # A name that became ambiguous when the installer changed is
            # dropped; anything unambiguous is still kept below.
            selections = {}
        for (step_name, group_name), option_names in selections.items():
            for option in option_names:
                kept.append(Pick(step=step_name, group=group_name, option=option))

    if on_fomod_prompt is not None:
        asked = on_fomod_prompt(config, archive_name, kept)
        return kept + list(asked)

    if kept or stored:
        return kept
    raise FomodChoicesError(archive_name)


@dataclass
class ModStatus:
    mod_id: int
    name: str
    version: str
    file_id: int
    optional: bool
    manual: bool
    phase: int
    status: str = ""  # up_to_date, update_available, not_installed, removed


@dataclass
class StatusResult:
    collection_name: str
    collection_url: str
    game_domain: str
    installed_revision: int
    latest_revision: int | None
    mods: list[ModStatus]
    deployed_at: str | None
    deployed_file_count: int
    game_dir: str
    track_sync_enabled: bool
    # Distinct mod_ids skipped because they are tombstoned as removed
    # (see SyncResult.removed_skipped). 0 when the comparison could not
    # reach the API (the offline fallback lists installed mods only).
    removed_skipped: int = 0


@dataclass
class PendingDownload:
    mod_id: int
    mod_name: str
    file_id: int
    filename: str
    size_bytes: int
    browser_url: str


@dataclass
class ImportResult:
    matched: int
    extracted: int
    still_pending: int
    errors: list[str]
    load_order_files: list[str]
    warnings: list[str] = field(default_factory=list)


@dataclass
class FileConflict:
    file_path: str
    winner: str
    loser: str


@dataclass
class SyncResult:
    mods_downloaded: int
    mods_extracted: int
    errors: list[str]
    load_order_files: list[str]
    tracked: int = 0
    untracked: int = 0
    pending_downloads: list[PendingDownload] = field(default_factory=list)
    collection_dir: Path | None = None
    conflicts: list[FileConflict] = field(default_factory=list)
    # Distinct mod_ids skipped because they are tombstoned as removed.
    # Counted per mod page, never per file: a multi-file mod whose page
    # was removed is skipped once.
    removed_skipped: int = 0


@dataclass
class UpdateResult:
    to_install: list[dict]
    to_update: list[dict]
    up_to_date: list[dict]
    to_remove: list[int]
    downloaded: int
    errors: list[str]
    new_revision: int
    load_order_files: list[str]
    tracked: int = 0
    untracked: int = 0
    pending_downloads: list[PendingDownload] = field(default_factory=list)
    # Distinct mod_ids skipped because they are tombstoned as removed
    # (see SyncResult.removed_skipped).
    removed_skipped: int = 0


@dataclass
class DeployWarning:
    code: str
    message: str
    severity: str  # "error" or "warning"


@dataclass
class DeployResult:
    deployed_count: int
    conflicts: list[str]
    errors: list[str]
    game_dir: str
    has_sfse: bool
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class AddResult:
    mod_name: str
    mod_id: int
    file_name: str
    success: bool
    error: str = ""
    pending_download: PendingDownload | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class NxmResult:
    """Outcome of handling one nxm:// individual-file link.

    Carries safe display fields only: never the link credentials, and
    error text is built from redacted sources.
    """

    success: bool
    status: str
    # downloaded, already_downloaded, extracted_from_archive,
    # saved_to_output, error
    game_domain: str
    mod_id: int
    file_id: int
    mod_name: str = ""
    filename: str = ""
    collection_dir: Path | None = None
    output_path: Path | None = None
    extracted: bool = False
    error: str = ""
    re_click_required: bool = False  # user must re-click on the website
    # What the collection state says about this file now: "downloaded",
    # "pending_download", or "" when nothing was recorded. Separate from
    # `status` because a failed extraction can still leave a recoverable
    # pending entry, and the caller needs to tell "run import to finish"
    # apart from "nothing was recorded".
    download_status: str = ""
    # Non-fatal issues on an otherwise successful run (e.g. load order
    # regeneration failed after the download was already completed and
    # saved). Truthful completion: success stays True, the warning says
    # what did not finish and how to retry it.
    warnings: list[str] = field(default_factory=list)


def _metadata_size_bytes(selected: dict) -> int:
    """Exact expected byte size from file metadata, 0 when unknown.

    Raises ValueError when Nexus sends a size of an unexpected type or a
    negative count: a hostile or broken endpoint must not produce a raw
    int() traceback deep in the download flow; callers convert this into
    a domain failure.
    """
    raw = selected.get("size_in_bytes")
    if raw is None or raw == "":
        return 0
    try:
        size = int(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"unexpected size_in_bytes type: {type(raw).__name__}"
        ) from None
    if size < 0:
        raise ValueError("negative size_in_bytes")
    return size


@dataclass
class ModFileInfo:
    file_id: int
    name: str
    version: str
    category: str
    size_bytes: int


def _noop_progress(event: str, pct: float, msg: str) -> None:
    pass


def _file_md5(path: Path) -> str | None:
    """Compute the MD5 hex digest of a file, or None if it cannot be read.

    Module-level so tests can monkeypatch it to observe lazy hashing.
    """
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


# Files this tool itself generates inside the mods dir. They are internal
# output, never download candidates, and must not be matched or hashed.
_INTERNAL_FILENAMES = frozenset({
    STATE_FILENAME.lower(),
    "load-order.txt",
    "plugins.txt",
})

# Archive extensions whose real files always carry a magic signature, so an
# invalid magic under these names means corrupt content or an error page.
# (.r00 split parts carry no magic and are deliberately excluded.)
_MAGIC_VERIFIED_SUFFIXES = frozenset({".zip", ".7z", ".rar"})


def _retained_archive_is_sane(archive: Path, expected_md5: str | None) -> bool:
    """Offline sanity of a retained archive before trusting it.

    Requires a real file, valid magic bytes for suffixes whose real
    content always carries a signature, and, when the cached manifest
    knows a well-formed hash for the identity, matching bytes. The state
    stores no expected byte size, so the hash is the offline
    expected-content check when one is known.
    """
    if not archive.is_file():
        return False
    if (
        archive.suffix.lower() in _MAGIC_VERIFIED_SUFFIXES
        and sniff_archive_magic(archive) is None
    ):
        return False
    return not (expected_md5 is not None and _file_md5(archive) != expected_md5)


def _as_int(value: Any) -> int | None:
    """Best-effort int conversion for manifest identity fields."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _normalize_version(version: Any) -> str:
    """Normalize a version string for comparison.

    Browser downloads encode versions with hyphens ("1-0-3") while the API
    reports dots ("1.0.3"); treat the separators as equivalent. Non-string
    state values (hand-edited or malformed state files) are coerced safely.
    """
    if not isinstance(version, str):
        version = "" if version is None else str(version)
    return version.strip().lower().replace(".", "-")


# Browser downloads are named "{logical}-{modId}-{version}-{unixTimestamp}".
# The timestamp is unix seconds (9-10 digits today); 6-12 digits stays
# permissive for old uploads while still requiring a long numeric tail.
_BROWSER_TIMESTAMP_RE = r"\d{6,12}"


def _browser_suffix_match(logical_name: str, mod_id: Any, version: Any, stem: str) -> bool:
    """Check a disk filename stem against the Nexus browser-download suffix.

    Requires the full "{logical}-{modId}-{version}-{timestamp}" shape built
    from known mod/file identity. Never matches on loose word overlap: if the
    identity cannot be established, this returns False and the item stays
    pending instead of risking a false match.

    logical_name should be the filename stem, so pending entries stored with
    an extension ("Foo.zip") match browser files the same way extension-less
    display names ("Foo") do.
    """
    mod_id_int = _as_int(mod_id) or 0
    if mod_id_int <= 0:
        return False  # identity unknown; refuse to guess
    logical = logical_name.strip().lower()
    if not logical:
        return False
    prefix = f"{logical}-{mod_id_int}-"
    s = stem.lower()
    if not s.startswith(prefix):
        return False
    rest = s[len(prefix):]
    m = re.match(rf"^(.+)-({_BROWSER_TIMESTAMP_RE})$", rest)
    if not m:
        return False
    file_version = m.group(1)
    normalized_expected = _normalize_version(version)
    if normalized_expected:
        return _normalize_version(file_version) == normalized_expected
    # Version unknown: still require the version segment to start with a digit
    return file_version[0].isdigit()


# Same shape the downloader enforces (downloader._MD5_RE): only a
# well-formed 32-hex-digit MD5 ever counts as a known checksum. A
# malformed value would be silently skipped downstream, turning
# "verify or refuse" into "publish unverified", so it is dropped at
# ingestion instead of being carried around as if it were a hash.
_MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def _is_wellformed_md5(value: Any) -> bool:
    """True only for a 32-hex-digit MD5 the downloader would enforce."""
    return isinstance(value, str) and bool(_MD5_RE.match(value.strip()))


def _expected_md5_maps(
    manifest_data: Any,
) -> tuple[dict[tuple[int, int], str], dict[str, str]]:
    """Build expected-MD5 lookups from cached manifest mod_rules.

    Returns (by_identity, by_name):
      by_identity: (mod_id, file_id) -> md5
      by_name:     lowercase logical filename -> md5

    Keys seen with conflicting hashes are dropped instead of guessed: two
    rules sharing a logicalFileName with different content must never attach
    one mod's hash to another's pending item. Identity scoping is preferred
    by callers; the name map is only a fallback for unique, unambiguous
    hashes. Malformed or incomplete manifest entries are skipped silently;
    hashes are a verification aid, never a requirement for matching.
    """
    identity_hits: dict[tuple[int, int], set[str]] = {}
    name_hits: dict[str, set[str]] = {}
    if not isinstance(manifest_data, dict):
        return {}, {}
    rules = manifest_data.get("mod_rules")
    if not isinstance(rules, list):
        return {}, {}
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        for side in ("source", "reference"):
            entry = rule.get(side)
            if not isinstance(entry, dict):
                continue
            md5 = ""
            for key in ("fileMD5", "fileMd5", "file_md5"):
                value = entry.get(key)
                if isinstance(value, str) and value.strip():
                    md5 = value.strip().lower()
                    break
            # A malformed hash is worse than no hash: downstream it would
            # silently disable verification, so drop it here and treat the
            # file as having no known checksum.
            if not _is_wellformed_md5(md5):
                continue
            mod_id = _as_int(entry.get("modId")) or _as_int(entry.get("modID"))
            file_id = _as_int(entry.get("fileID")) or _as_int(entry.get("fileId"))
            if mod_id and file_id:
                identity_hits.setdefault((mod_id, file_id), set()).add(md5)
            for key in ("logicalFileName", "logicalFilename"):
                value = entry.get(key)
                if isinstance(value, str) and value.strip():
                    name_hits.setdefault(value.strip().lower(), set()).add(md5)
    by_identity = {k: next(iter(v)) for k, v in identity_hits.items() if len(v) == 1}
    by_name = {k: next(iter(v)) for k, v in name_hits.items() if len(v) == 1}
    return by_identity, by_name


def _attach_expected_md5(state: CollectionState, mods: list[dict]) -> None:
    """Attach already-known manifest MD5s to mod dicts before downloading.

    The downloader verifies these before publishing a file. Hashes come only
    from the cached manifest keyed by exact (mod_id, file_id) identity, so no
    extra API calls are made and a hash is never attached across identities.
    """
    by_identity, _ = _expected_md5_maps(state.manifest_data)
    if not by_identity:
        return
    for mod in mods:
        if mod.get("expected_md5"):
            continue
        mod_id = _as_int(mod.get("mod_id"))
        file_id = _as_int(mod.get("file_id"))
        if mod_id is None or file_id is None:
            continue
        md5 = by_identity.get((mod_id, file_id))
        # Defense in depth: only a well-formed hash is ever attached, so a
        # malformed value can never masquerade as a known checksum.
        if _is_wellformed_md5(md5):
            mod["expected_md5"] = md5


def _validate_no_overlap(mods_dir: Path, game_dir: Path) -> str | None:
    """Reject staging/game directory overlap. Returns an error message or None.

    Deploying a staging directory onto itself (or into a directory that
    contains it) would make undeploy delete staged mod files, so any overlap
    is refused before anything is mutated.
    """
    staging = os.path.realpath(mods_dir)
    game = os.path.realpath(game_dir)
    sep = os.sep
    if staging == game:
        return (
            "Refusing to deploy: the staging directory and the game directory "
            f"are the same path ({staging})."
        )
    if game.startswith(staging + sep):
        return (
            "Refusing to deploy: the game directory "
            f"({game}) is inside the staging directory ({staging})."
        )
    if staging.startswith(game + sep):
        return (
            "Refusing to deploy: the staging directory "
            f"({staging}) is inside the game directory ({game})."
        )
    return None


def _resolve_import_match(
    filename: str,
    mod_id: int,
    version: Any,
    candidates: list[Path],
    expected_md5: str | None,
    md5_cache: dict[Path, str | None],
    archive_cache: dict[Path, bool],
) -> tuple[Path | None, list[Path], list[Path]]:
    """Find the single disk file that fulfills one pending download.

    Returns (matched_path, ambiguous_candidates, hash_rejected):
      - matched_path: the single file to import, or None
      - ambiguous_candidates: several files match by name, none verified
      - hash_rejected: files matching by name that failed content-hash
        verification (stale, corrupt, or the wrong download)

    When an expected MD5 is available from the manifest, content verification
    governs acceptance: name matches (exact filename, exact stem, browser
    suffix) are accepted only among hash-verified candidates, and a name
    match that fails verification is rejected instead of imported. A stale
    same-named download therefore loses to a verified file with the right
    content, and never passes silently when nothing verifies.

    When no expected MD5 is available, deterministic name precedence applies,
    strongest identity first:
      1. exact full filename, case-insensitive (premium downloads keep the
         archive name)
      2. exact stem match, so logical/display names without extensions
         (GraphQL file.name) match "Name.zip"/"Name.7z" (archives only)
      3. Nexus browser suffix "{logical}-{modId}-{version}-{timestamp}"
         parsed from known identity (archives only)

    A level with several hits is ambiguous: matching continues at the next
    level so stronger evidence can resolve it, and if nothing resolves, the
    ambiguity is reported instead of silently picking a file.
    """
    filename_lower = filename.strip().lower()
    stem_lower = Path(filename_lower).stem if filename_lower else ""

    # Name-pattern levels only consider archives, so loose extracted files
    # (e.g. "ModName.pak" from an earlier import) can never false-match.
    def _is_archive_candidate(p: Path) -> bool:
        if p not in archive_cache:
            archive_cache[p] = is_archive(p)
        return archive_cache[p]

    exact = [p for p in candidates if filename_lower and p.name.lower() == filename_lower]
    stem_hits = [
        p for p in candidates
        if stem_lower and _is_archive_candidate(p) and p.stem.lower() == stem_lower
    ]
    suffix_hits = [
        p for p in candidates
        if stem_lower and _is_archive_candidate(p)
        and _browser_suffix_match(stem_lower, mod_id, version, p.stem)
    ]

    if expected_md5:
        # Hash lazily, at most once per unique candidate, and only for
        # plausible downloads: files with a name relationship to the pending
        # item or archive-like files. Loose extracted content is never hashed.
        name_related = set(exact) | set(stem_hits) | set(suffix_hits)
        verified: list[Path] = []
        for p in candidates:
            if p not in name_related and not _is_archive_candidate(p):
                continue
            if p not in md5_cache:
                md5_cache[p] = _file_md5(p)
            if md5_cache[p] == expected_md5:
                verified.append(p)

        if verified:
            # Prefer the strongest name identity among verified candidates.
            # Multiple verified hits are byte-identical (same MD5), e.g. a
            # browser re-download saved as "Foo (1).zip", so ties resolve
            # deterministically by name.
            verified_set = set(verified)
            for hits in (exact, stem_hits, suffix_hits):
                picks = [p for p in hits if p in verified_set]
                if picks:
                    return min(picks, key=lambda p: p.name.lower()), [], []
            return min(verified, key=lambda p: p.name.lower()), [], []

        # Nothing verified: report name matches that failed verification
        for hits in (exact, stem_hits, suffix_hits):
            if hits:
                return None, [], hits
        return None, [], []

    # No expected hash available: deterministic name precedence
    ambiguous: list[Path] = []
    if len(exact) == 1:
        return exact[0], [], []
    if exact:
        ambiguous = exact

    if len(stem_hits) == 1:
        return stem_hits[0], [], []
    if stem_hits and not ambiguous:
        ambiguous = stem_hits

    if len(suffix_hits) == 1:
        return suffix_hits[0], [], []
    if suffix_hits and not ambiguous:
        ambiguous = suffix_hits

    return None, ambiguous, []


def _filter_removed_mods(
    mods: list[dict[str, Any]], state: CollectionState
) -> tuple[list[dict[str, Any]], int]:
    """Drop manifest entries whose mod_id is tombstoned as removed.

    This is the ONE service-level tombstone filter, applied to the
    manifest input of sync and update before anything consumes it
    (comparison, pending registration, downloads, load order generation).
    Filtering at the input keeps every comparison bucket
    (to_install/up_to_date/to_update) consistent and maintains the
    invariant that no state entry exists for a tombstoned mod: nothing
    downstream can register or download a removed page. Tombstones key
    on mod_id, so a revision bump (new file_id, same mod page) is
    skipped just the same.

    Returns (filtered_mods, skipped_count) where skipped_count is the
    number of DISTINCT mod_ids skipped: a multi-file mod whose page was
    removed is counted once, not once per file.
    """
    kept: list[dict[str, Any]] = []
    skipped_ids: set[int] = set()
    for mod in mods:
        mod_id = _as_int(mod.get("mod_id"))
        if mod_id is not None and state.is_removed(mod_id):
            skipped_ids.add(mod_id)
            continue
        kept.append(mod)
    return kept, len(skipped_ids)


@dataclass
class CollectionSummary:
    """One collection discovered by a local scan (see list_collections)."""

    name: str
    game_domain: str
    mod_count: int
    pending_count: int
    path: Path


@dataclass
class GameSummary:
    """One game's rollup of collections and mods (see list_games)."""

    game_domain: str
    collection_count: int
    mod_count: int


@dataclass
class ModSummary:
    """One tracked mod file from local state (see list_mods)."""

    file_id: int
    mod_id: int
    name: str
    version: str
    filename: str
    download_status: str
    # The stored FOMOD option picks as {"step", "group", "option"}
    # triples. They drive the deploy plan, so the listing has to carry
    # them: otherwise "what did I pick" is unanswerable from the CLI.
    fomod_picks: list[dict] = field(default_factory=list)


@dataclass
class RemoveModResult:
    """Outcome of ModManagerService.remove_mod.

    removed_file_ids/removed_mod_ids report exactly what left the state
    file; warnings carry non-fatal follow-up failures (e.g. load order
    regeneration) after the removal itself was already persisted.
    """

    removed_file_ids: list[int]
    removed_mod_ids: list[int]
    warnings: list[str]


@dataclass
class SkippedCollection:
    """One candidate collection dir a scan could not read (see scan_collections).

    ``reason`` is a short, secret-free diagnostic (the exception type
    plus the first line of its message, truncated) so a diagnostic view
    can name broken state files without echoing their contents.
    """

    name: str
    path: Path
    reason: str


@dataclass
class CollectionScan:
    """Result of scan_collections: readable collections plus skipped ones."""

    collections: list[CollectionSummary]
    skipped: list[SkippedCollection]


def scan_collections(root: Path) -> CollectionScan:
    """Scan root for collections, reporting the ones that could not be read.

    Same candidate rules as list_collections (root itself plus its
    immediate subdirectories holding .nexus-state.json) and the same
    tolerance for broken states, but skipped directories are reported
    with a short reason instead of dropped silently, so callers that
    want a diagnostic view (the shell's /status health report) can
    surface them without changing list_collections' silent-skip
    contract. Never touches the network. Sorted by name.
    """
    root = Path(root)
    candidates: list[Path] = []
    if (root / STATE_FILENAME).is_file():
        candidates.append(root)
    if root.is_dir():
        candidates.extend(
            child
            for child in sorted(root.iterdir())
            if child.is_dir() and (child / STATE_FILENAME).is_file()
        )

    summaries: list[CollectionSummary] = []
    skipped: list[SkippedCollection] = []
    for cdir in candidates:
        try:
            state = CollectionState(cdir)
            state.load()
        except Exception as e:
            # Unreadable, malformed, or partially hand-edited state:
            # record it and keep scanning. (load() can surface
            # StateError, OSError, or int()/type errors on bad keys.)
            # The safe stringify pattern keeps a whitespace-only
            # message from raising here and hiding every collection.
            # exception_reason never returns blank and never raises, so
            # there is no empty-list indexing to guard against here.
            reason = exception_reason(e)[:120]
            skipped.append(
                SkippedCollection(
                    name=cdir.name,
                    path=cdir,
                    reason=reason,
                )
            )
            continue
        summaries.append(
            CollectionSummary(
                # Names are coerced to text here: the sort below and
                # every name comparison downstream need real strings,
                # and a non-string name in hand-edited state must not
                # abort the scan. Truthiness keeps the dir-name
                # fallback exactly as before (empty/absent -> dir name).
                name=_state_text(state.collection_name or cdir.name),
                game_domain=_state_text(state.game_domain),
                mod_count=len(state.mods),
                pending_count=len(state.get_pending_mods()),
                path=cdir,
            )
        )
    summaries.sort(key=lambda s: (s.name.lower(), str(s.path)))
    return CollectionScan(collections=summaries, skipped=skipped)


def list_collections(root: Path) -> list[CollectionSummary]:
    """List collections under root from local state only (read-only).

    Scans root itself plus its immediate subdirectories for
    .nexus-state.json files, so both a collections root and a single
    collection directory are valid inputs. Invalid or unreadable state
    files are skipped silently: one broken collection must never hide
    the healthy ones. Never touches the network. Sorted by name.
    """
    return scan_collections(root).collections


def list_games(root: Path) -> list[GameSummary]:
    """Roll collections up by game domain (read-only, no network).

    Same scan as list_collections, aggregated so a caller can see what is
    present per game without reading every collection. Games with no
    readable collections simply do not appear. Sorted by game domain.
    """
    totals: dict[str, list[int]] = {}
    for c in list_collections(root):
        domain = _state_text(c.game_domain) or "-"
        row = totals.setdefault(domain, [0, 0])
        row[0] += 1
        row[1] += c.mod_count
    return [
        GameSummary(game_domain=domain, collection_count=counts[0], mod_count=counts[1])
        for domain, counts in sorted(totals.items())
    ]


def _requirements_from_mod_info(info: dict | None) -> list[int]:
    """Requirement mod_ids from a mod-info payload, or [].

    The Nexus GraphQL collection query carries these under
    ``modRequirements.nexusRequirements.nodes[].modId``. The REST mod
    endpoint may or may not carry an equivalent. Both shapes are
    accepted; anything else is "no list", not "no requirements", so the
    caller stays silent rather than inventing an all-clear.
    """
    if not isinstance(info, dict):
        return []
    direct = info.get("requirements") or info.get("dependencies")
    if isinstance(direct, list):
        out: list[int] = []
        for item in direct:
            if isinstance(item, int):
                out.append(item)
            elif isinstance(item, dict) and "modId" in item:
                try:
                    out.append(int(item["modId"]))
                except (TypeError, ValueError):
                    continue
        return out
    nested = info.get("modRequirements") or {}
    nexus = nested.get("nexusRequirements") or {}
    nodes = nexus.get("nodes") or []
    out = []
    for node in nodes:
        if isinstance(node, dict) and "modId" in node:
            try:
                out.append(int(node["modId"]))
            except (TypeError, ValueError):
                continue
    return out


def missing_requirements(state: CollectionState, requirements: list[int] | None) -> list[int]:
    """Required mod_ids this collection does not have.

    The check behind the `add` dependency warning. Collections handle
    dependencies implicitly (the author includes them); `add` takes one
    mod and has no idea what else that mod needs.

    Warn, never block: a missing dependency is a thing the user may know
    about and have decided against. And an empty or absent list means
    "nothing known", not "nothing needed": we cannot check what we
    cannot see, and a false warning is worse than none.
    """
    if not requirements:
        return []
    installed = {ms.mod_id for ms in state.mods.values()}
    return [r for r in requirements if r not in installed]


def list_mods(mods_dir: Path) -> list[ModSummary]:
    """List tracked mods from local state only (read-only, no network).

    Unlike get_status this never contacts the API: it reflects exactly
    what .nexus-state.json tracks right now. Sorted by name (ties break
    by file_id for determinism). Removed mods are absent because removal
    deletes their state entries.
    """
    state = CollectionState(mods_dir)
    state.load()
    summaries = [
        ModSummary(
            file_id=ms.file_id,
            mod_id=ms.mod_id,
            # Coerced to text here: the name sort below needs real
            # strings even when hand-edited state holds other types.
            name=_state_text(ms.name),
            version=ms.version,
            filename=ms.filename,
            download_status=ms.download_status,
            fomod_picks=list(ms.fomod_picks),
        )
        for ms in state.mods.values()
    ]
    summaries.sort(key=lambda m: (m.name.lower(), m.file_id))
    return summaries


def get_collection_url(mods_dir: Path) -> str:
    """Return the collection URL recorded in a collection's state.

    This is the URL the collection was last synced from, the one
    ``sync``/``update`` can be pointed at again without a trip back to
    the website. Returns "" when the state records none (for example a
    hand-created state file). Read-only and offline: raises StateError
    when the state cannot be read, exactly like list_mods.
    """
    state = CollectionState(mods_dir)
    state.load()
    return state.collection_url


@dataclass
class CollectionLocalDetails:
    """Local-only snapshot of one collection (see collection_local_details)."""

    name: str
    game_domain: str
    mod_count: int
    pending_count: int
    tombstone_count: int
    collection_url: str
    state_path: Path


def collection_local_details(mods_dir: Path) -> CollectionLocalDetails:
    """Read one collection's local counts and metadata (read-only, no network).

    Reports tracked mods, pending downloads, removal tombstones
    (state.removed_mods), the collection_url recorded at the last sync,
    and the state file path. Raises StateError exactly like list_mods
    when the state cannot be read.
    """
    mods_dir = Path(mods_dir)
    state = CollectionState(mods_dir)
    state.load()
    return CollectionLocalDetails(
        name=_state_text(state.collection_name or mods_dir.name),
        game_domain=_state_text(state.game_domain),
        mod_count=len(state.mods),
        pending_count=len(state.get_pending_mods()),
        tombstone_count=len(state.removed_mods),
        collection_url=_state_text(state.collection_url),
        state_path=mods_dir / STATE_FILENAME,
    )


def collection_manifest_mod_ids(mods_dir: Path) -> set[int]:
    """mod_ids that belong to this collection's cached manifest (local).

    Used by the shell to tell a re-added collection member from a
    genuinely new manual mod. Reads only the manifest cached in state
    (state.manifest_data); no network. Returns an empty set when no
    manifest is cached or it carries no recognizable mod ids. Raises
    StateError when the state file is invalid and ValueError on
    malformed state keys, exactly like list_mods.
    """
    state = CollectionState(mods_dir)
    state.load()
    data = state.manifest_data
    ids: set[int] = set()
    if not isinstance(data, dict):
        return ids
    rules = data.get("mod_rules")
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            for side in ("source", "reference"):
                entry = rule.get(side)
                if not isinstance(entry, dict):
                    continue
                mod_id = _as_int(entry.get("modId")) or _as_int(
                    entry.get("modID")
                )
                if mod_id:
                    ids.add(mod_id)
    phases = data.get("mod_phases")
    if isinstance(phases, dict):
        for key in phases:
            mod_id = _as_int(key)
            if mod_id:
                ids.add(mod_id)
    return ids


class ModManagerService:
    """Business logic for managing Nexus Mods collections."""

    def __init__(self, api_key: str | None = None, force_free: bool = False):
        self._api_key = api_key
        self._api: NexusAPI | None = None
        self._force_free = force_free

    def _check_premium(self, user_info: dict) -> bool:
        """Check if user has premium, respecting force_free override."""
        if self._force_free:
            return False
        return user_info.get("is_premium", False)

    @property
    def api(self) -> NexusAPI:
        if self._api is None:
            self._api = NexusAPI(self._api_key)
        return self._api

    def validate_api_key(self) -> dict:
        """Test API key validity. Returns user info dict."""
        return self.api.validate_key()

    def get_status(self, mods_dir: Path) -> StatusResult:
        """Get collection status with per-mod comparison against latest."""
        state = CollectionState(mods_dir)
        state.load()

        mod_statuses: list[ModStatus] = []
        latest_revision = None
        removed_skipped = 0

        # Try to fetch latest from API
        try:
            collection_info = parse_collection_url(state.collection_url)
            collection_data = self.api.get_collection_mods(
                collection_info.game_domain, collection_info.slug
            )
            latest_revision = collection_data["revision"]

            # The one service-level tombstone filter, exactly like
            # sync/update: a removed mod page must never show up here as
            # installable. This also feeds `update --dry-run`, which
            # reports through this path.
            manifest_mods, removed_skipped = _filter_removed_mods(
                collection_data["mods"], state
            )
            collection_data = {**collection_data, "mods": manifest_mods}

            to_install, to_update, up_to_date, to_remove = state.compare_with_collection(
                collection_data["mods"]
            )

            for mod in up_to_date:
                installed = state.get_mod(mod["mod_id"])
                if installed and installed.download_status == "pending_download":
                    mod_status = "pending_download"
                else:
                    mod_status = "up_to_date"
                mod_statuses.append(ModStatus(
                    mod_id=mod["mod_id"],
                    name=mod["mod_name"],
                    version=installed.version if installed else mod.get("version", ""),
                    file_id=mod["file_id"],
                    optional=mod.get("optional", False),
                    manual=False,
                    phase=installed.phase if installed else 0,
                    status=mod_status,
                ))

            for mod in to_update:
                installed = state.get_mod(mod["mod_id"])
                mod_statuses.append(ModStatus(
                    mod_id=mod["mod_id"],
                    name=mod["mod_name"],
                    version=installed.version if installed else "",
                    file_id=mod["file_id"],
                    optional=mod.get("optional", False),
                    manual=False,
                    phase=installed.phase if installed else 0,
                    status="update_available",
                ))

            for mod in to_install:
                mod_statuses.append(ModStatus(
                    mod_id=mod["mod_id"],
                    name=mod["mod_name"],
                    version=mod.get("version", ""),
                    file_id=mod["file_id"],
                    optional=mod.get("optional", False),
                    manual=False,
                    phase=0,
                    status="not_installed",
                ))

            for file_id in to_remove:
                installed = state.get_file(file_id)
                if installed:
                    mod_statuses.append(ModStatus(
                        mod_id=installed.mod_id,
                        name=installed.name,
                        version=installed.version,
                        file_id=file_id,
                        optional=installed.optional,
                        manual=installed.manual,
                        phase=installed.phase,
                        status="removed",
                    ))

        except (NexusAPIError, CollectionParseError):
            # Offline/no API - just show installed mods
            for file_id, ms in state.mods.items():
                status = "pending_download" if ms.download_status == "pending_download" else "installed"
                mod_statuses.append(ModStatus(
                    mod_id=ms.mod_id,
                    name=ms.name,
                    version=ms.version,
                    file_id=file_id,
                    optional=ms.optional,
                    manual=ms.manual,
                    phase=ms.phase,
                    status=status,
                ))

        # Add manual mods that aren't already in the list
        listed_file_ids = {m.file_id for m in mod_statuses}
        for file_id, ms in state.mods.items():
            if file_id not in listed_file_ids and ms.manual:
                mod_statuses.append(ModStatus(
                    mod_id=ms.mod_id,
                    name=ms.name,
                    version=ms.version,
                    file_id=file_id,
                    optional=ms.optional,
                    manual=True,
                    phase=ms.phase,
                    status="manual",
                ))

        return StatusResult(
            collection_name=state.collection_name,
            collection_url=state.collection_url,
            game_domain=state.game_domain,
            installed_revision=state.collection_revision,
            latest_revision=latest_revision,
            mods=mod_statuses,
            deployed_at=state.deployed_at,
            deployed_file_count=len(state.deployed_files),
            game_dir=state.game_dir,
            track_sync_enabled=state.track_sync_enabled,
            removed_skipped=removed_skipped,
        )

    def sync(
        self,
        collection_url: str,
        mods_dir: Path,
        skip_optional: bool = False,
        no_load_order: bool = False,
        no_extract: bool = False,
        on_progress: ProgressCallback | None = None,
        target_dir: Path | None = None,
        show_progress: bool = True,
    ) -> SyncResult:
        """Download an entire collection.

        ``mods_dir`` is the collections root: by default the collection
        lands in a subdirectory named from the API's collection name.
        ``target_dir`` overrides that destination with an exact
        directory (used by the shell for an already-resolved collection,
        so a renamed directory or a root-as-collection layout updates
        the selected directory instead of a name-derived one). Callers
        that pass only ``mods_dir`` see the original behavior.
        """
        progress = on_progress or _noop_progress
        errors: list[str] = []
        load_order_files: list[str] = []

        # Parse URL
        collection_info = parse_collection_url(collection_url)

        # Check premium status
        progress("init", 0.0, "Validating API key...")
        user_info = self.api.validate_key()
        is_premium = self._check_premium(user_info)

        # Fetch collection
        progress("fetch", 0.05, "Fetching collection data...")
        collection_data = self.api.get_collection_mods(
            collection_info.game_domain, collection_info.slug
        )

        # Destination: the caller's exact directory when given, else a
        # per-collection subdirectory named from the API.
        if target_dir is not None:
            mods_dir = Path(target_dir)
        else:
            dir_name = _sanitize_dirname(collection_data["name"])
            mods_dir = mods_dir / dir_name
        mods_dir.mkdir(parents=True, exist_ok=True)
        with CollectionLock(mods_dir):
            # Initialize state first: the tombstone filter below reads it.
            state = CollectionState(mods_dir)
            if state.exists():
                state.load()

            # The single tombstone filter, applied to the manifest input so
            # every consumer sees the same filtered manifest: the free-account
            # pending registration, the premium download paths (including the
            # empty-state full download), compare_with_collection, and load
            # order generation. removed_skipped counts DISTINCT mod_ids.
            manifest_mods, removed_skipped = _filter_removed_mods(
                collection_data["mods"], state
            )
            collection_data = {**collection_data, "mods": manifest_mods}
            if removed_skipped:
                progress(
                    "fetch", 0.06,
                    f"Skipping {removed_skipped} removed mod(s)",
                )

            mods = manifest_mods
            if skip_optional:
                mods = [m for m in mods if not m.get("optional", False)]

            state.set_collection_info(
                url=collection_info.url,
                name=collection_data["name"],
                revision=collection_data["revision"],
                game_domain=collection_data["game_domain"],
            )

            if not mods:
                # Empty collection or every mod optional and skipped: persist the
                # collection info so status/update recognize this collection.
                state.save()
                return SyncResult(
                    0, 0, [], [], collection_dir=mods_dir,
                    removed_skipped=removed_skipped,
                )

            if not is_premium:
                # Free user: register mods as pending_download
                progress("pending", 0.5, "Building pending download list...")
                pending_downloads: list[PendingDownload] = []
                game_domain = collection_data["game_domain"]
                for mod in mods:
                    mod_id = mod["mod_id"]
                    # Don't regress already-downloaded files (check by file_id)
                    existing = state.get_file(mod["file_id"])
                    if existing and existing.download_status == "downloaded":
                        continue
                    browser_url = f"https://www.nexusmods.com/{game_domain}/mods/{mod_id}?tab=files&file_id={mod['file_id']}"
                    size_bytes = int(mod.get("size_bytes", 0) or mod.get("size", 0) or 0)
                    pending = PendingDownload(
                        mod_id=mod_id,
                        mod_name=mod["mod_name"],
                        file_id=mod["file_id"],
                        filename=mod.get("filename", ""),
                        size_bytes=size_bytes,
                        browser_url=browser_url,
                    )
                    pending_downloads.append(pending)
                    mod_info_dict = dict(mod)
                    mod_info_dict["download_status"] = "pending_download"
                    mod_info_dict["browser_url"] = browser_url
                    state.add_mod(mod_info_dict)

                state.save()

                # Cache manifest + generate load order even for free users
                # (the manifest download is a public URL, doesn't need premium)
                load_order_files = []
                if not no_load_order:
                    progress("loadorder", 0.8, "Caching manifest and generating load order...")
                    lo_files = self._generate_load_order(collection_data, mods_dir, state)
                    load_order_files = lo_files

                progress("done", 1.0, f"Registered {len(pending_downloads)} mods for manual download")
                return SyncResult(
                    mods_downloaded=0,
                    mods_extracted=0,
                    errors=errors,
                    load_order_files=load_order_files,
                    pending_downloads=pending_downloads,
                    collection_dir=mods_dir,
                    removed_skipped=removed_skipped,
                )

            # Premium user: download directly
            # Skip mods already downloaded with matching file_id
            if state.mods:
                to_install, to_update, up_to_date, _ = state.compare_with_collection(mods)
                mods_to_download = to_install + to_update
                skipped = len(up_to_date)
                if skipped:
                    progress("download", 0.1, f"Skipping {skipped} already-downloaded mods")
                # Re-register up-to-date mods in state so they stay tracked
                for mod in up_to_date:
                    state.add_mod(mod)
            else:
                mods_to_download = mods
                skipped = 0

            # Prefetch the collection manifest once when it is not cached yet, so
            # download integrity checks can use its MD5s even on a first sync.
            # This is the same single public fetch _generate_load_order would do
            # later, passed through to avoid a redundant second download. If it
            # fails, downloads proceed with size-only verification.
            prefetched_manifest = None
            if state.manifest_data is None and collection_data.get("download_link"):
                try:
                    prefetched_manifest = download_and_parse_manifest(
                        collection_data["download_link"], session=self.api.session
                    )
                except ManifestError:
                    prefetched_manifest = None
            if prefetched_manifest is not None:
                state.manifest_data = prefetched_manifest.to_dict()
                state.mod_rules = prefetched_manifest.mod_rules
                state.save()

            total_mods = len(mods_to_download)
            downloader = Downloader(self.api)

            if not mods_to_download:
                results = []
            else:
                def on_download_progress(bytes_dl: int, total_bytes: int) -> None:
                    if total_bytes > 0:
                        pct = 0.1 + 0.6 * (bytes_dl / total_bytes)
                        progress("download", pct, f"Downloading... ({bytes_dl}/{total_bytes} bytes)")

                progress("download", 0.1, f"Downloading {total_mods} mods...")
                _attach_expected_md5(state, mods_to_download)
                results = downloader.download_mods(
                    game_domain=collection_data["game_domain"],
                    mods=mods_to_download,
                    target_dir=mods_dir,
                    on_progress=on_download_progress,
                    show_progress=show_progress,
                )

            # Extract archives
            progress("extract", 0.75, "Extracting archives...")
            extracted = 0
            file_owners: dict[str, str] = {}
            conflicts: list[FileConflict] = []
            for i, (mod_info, file_path) in enumerate(results):
                try:
                    config_record = None
                    contributed: list[str] = []
                    if not no_extract and is_archive(file_path):
                        final_files = extract_archive(file_path, mods_dir)
                        for ef in final_files:
                            rel = str(ef.relative_to(mods_dir))
                            contributed.append(rel)
                            prev_owner = file_owners.get(rel)
                            if prev_owner:
                                conflicts.append(FileConflict(rel, mod_info["mod_name"], prev_owner))
                            file_owners[rel] = mod_info["mod_name"]
                        config_record = _fomod_config_record(file_path)
                        extracted += 1
                    else:
                        rel = str(file_path.relative_to(mods_dir))
                        contributed.append(rel)
                        prev_owner = file_owners.get(rel)
                        if prev_owner:
                            conflicts.append(FileConflict(rel, mod_info["mod_name"], prev_owner))
                        file_owners[rel] = mod_info["mod_name"]
                    state.add_mod(mod_info)
                    if config_record is not None:
                        state.mods[mod_info["file_id"]].fomod_config = config_record
                    # Attribution for `disable`: the extraction tree is
                    # merged, so without this a disabled mod cannot be
                    # told apart from its neighbours at deploy time.
                    state.mods[mod_info["file_id"]].contributed_files = contributed
                    state.save()
                    pct = 0.75 + 0.15 * ((i + 1) / len(results))
                    progress("extract", pct, f"Extracted {mod_info['mod_name']}")
                except ExtractionError as e:
                    errors.append(f"Extraction error for {mod_info['mod_name']}: {exception_reason(e)}")

            # Generate load order
            if not no_load_order:
                progress("loadorder", 0.9, "Generating load order...")
                lo_files = self._generate_load_order(
                    collection_data, mods_dir, state, manifest=prefetched_manifest
                )
                load_order_files = lo_files

            # Track sync
            tracked, untracked = self._maybe_sync_tracked(state)
            skip_msg = f", {skipped} skipped" if skipped else ""
            progress("done", 1.0, f"Synced {len(results)} mods successfully{skip_msg}")

            return SyncResult(
                mods_downloaded=len(results),
                mods_extracted=extracted,
                errors=errors,
                load_order_files=load_order_files,
                tracked=tracked,
                untracked=untracked,
                collection_dir=mods_dir,
                conflicts=conflicts,
                removed_skipped=removed_skipped,
            )

    def update(
        self,
        mods_dir: Path,
        skip_optional: bool = False,
        no_load_order: bool = False,
        no_extract: bool = False,
        on_progress: ProgressCallback | None = None,
        show_progress: bool = True,
    ) -> UpdateResult:
        """Check for and download updates."""
        progress = on_progress or _noop_progress
        errors: list[str] = []
        load_order_files: list[str] = []

        with CollectionLock(mods_dir):
            state = CollectionState(mods_dir)
            state.load()

            progress("init", 0.0, "Validating API key...")
            user_info = self.api.validate_key()
            is_premium = self._check_premium(user_info)

            collection_info = parse_collection_url(state.collection_url)

            progress("fetch", 0.05, "Checking for updates...")
            collection_data = self.api.get_collection_mods(
                collection_info.game_domain, collection_info.slug
            )

            new_revision = collection_data["revision"]
            # The single tombstone filter, applied to the manifest input so
            # every consumer sees the same filtered manifest (comparison
            # buckets, pending registration, downloads, load order). This is
            # the revision-bump guard: an entry with a NEW file_id for a
            # removed mod page is skipped just the same. removed_skipped
            # counts DISTINCT mod_ids.
            manifest_mods, removed_skipped = _filter_removed_mods(
                collection_data["mods"], state
            )
            collection_data = {**collection_data, "mods": manifest_mods}
            if removed_skipped:
                progress(
                    "fetch", 0.06,
                    f"Skipping {removed_skipped} removed mod(s)",
                )
            mods = manifest_mods
            if skip_optional:
                mods = [m for m in mods if not m.get("optional", False)]

            to_install, to_update, up_to_date, to_remove = state.compare_with_collection(mods)

            downloaded = 0
            pending_downloads: list[PendingDownload] = []
            if to_install or to_update:
                mods_to_download = to_install + to_update
                if is_premium:
                    downloader = Downloader(self.api)

                    def on_download_progress(bytes_dl: int, total_bytes: int) -> None:
                        if total_bytes > 0:
                            pct = 0.1 + 0.6 * (bytes_dl / total_bytes)
                            progress("download", pct, "Downloading...")

                    progress("download", 0.1, f"Downloading {len(mods_to_download)} mods...")
                    _attach_expected_md5(state, mods_to_download)
                    results = downloader.download_mods(
                        game_domain=collection_data["game_domain"],
                        mods=mods_to_download,
                        target_dir=mods_dir,
                        on_progress=on_download_progress,
                        show_progress=show_progress,
                    )

                    progress("extract", 0.75, "Extracting archives...")
                    for mod_info, file_path in results:
                        try:
                            config_record = None
                            contributed: list[str] = []
                            if not no_extract and is_archive(file_path):
                                final_files = extract_archive(file_path, mods_dir)
                                contributed = [
                                    str(ef.relative_to(mods_dir))
                                    for ef in final_files
                                ]
                                config_record = _fomod_config_record(file_path)
                            else:
                                # A retained archive or a loose file is
                                # this mod's footprint; disable needs it.
                                contributed = [
                                    str(file_path.relative_to(mods_dir))
                                ]
                            state.add_mod(mod_info)
                            if config_record is not None:
                                state.mods[mod_info["file_id"]].fomod_config = config_record
                            state.mods[mod_info["file_id"]].contributed_files = contributed
                            downloaded += 1
                        except ExtractionError as e:
                            errors.append(f"Extraction error for {mod_info['mod_name']}: {exception_reason(e)}")
                else:
                    # Free user: register as pending
                    game_domain = collection_data["game_domain"]
                    for mod in mods_to_download:
                        mod_id = mod["mod_id"]
                        existing = state.get_file(mod["file_id"])
                        if existing and existing.download_status == "downloaded":
                            continue
                        browser_url = f"https://www.nexusmods.com/{game_domain}/mods/{mod_id}?tab=files&file_id={mod['file_id']}"
                        size_bytes = int(mod.get("size_bytes", 0) or mod.get("size", 0) or 0)
                        pending_downloads.append(PendingDownload(
                            mod_id=mod_id,
                            mod_name=mod["mod_name"],
                            file_id=mod["file_id"],
                            filename=mod.get("filename", ""),
                            size_bytes=size_bytes,
                            browser_url=browser_url,
                        ))
                        mod_info_dict = dict(mod)
                        mod_info_dict["download_status"] = "pending_download"
                        mod_info_dict["browser_url"] = browser_url
                        state.add_mod(mod_info_dict)

            state.collection_revision = new_revision
            state.save()

            if not no_load_order and (to_install or to_update):
                progress("loadorder", 0.9, "Generating load order...")
                load_order_files = self._generate_load_order(collection_data, mods_dir, state)

            tracked, untracked = self._maybe_sync_tracked(state)
            progress("done", 1.0, "Update complete")

            return UpdateResult(
                to_install=to_install,
                to_update=to_update,
                up_to_date=up_to_date,
                to_remove=to_remove,
                downloaded=downloaded,
                errors=errors,
                new_revision=new_revision,
                load_order_files=load_order_files,
                tracked=tracked,
                untracked=untracked,
                pending_downloads=pending_downloads,
                removed_skipped=removed_skipped,
            )

    def add_mod(
        self,
        mod_url: str,
        mods_dir: Path,
        file_id: int | None = None,
        no_load_order: bool = False,
        no_extract: bool = False,
        on_progress: ProgressCallback | None = None,
        show_progress: bool = True,
        fomod_picks: "Sequence[Pick] | None" = None,
        on_fomod_prompt: "Callable[..., Sequence[Pick]] | None" = None,
    ) -> AddResult:
        """Add a single mod by Nexus URL.

        ``fomod_picks`` are the FOMOD option choices to install, when the
        caller already has them (a flag or a choices file). Without them
        and with ``on_fomod_prompt`` set, the installer's groups are
        handed to that callback to ask about. Without either, a FOMOD
        archive is refused rather than extracted to every option at once.
        """
        progress = on_progress or _noop_progress

        mod_info = parse_mod_url(mod_url)

        with CollectionLock(mods_dir):
            state = CollectionState(mods_dir)
            state.load()

            # Validate game domain matches
            if state.game_domain and state.game_domain.lower() != mod_info.game_domain.lower():
                return AddResult(
                    mod_name="", mod_id=mod_info.mod_id, file_name="",
                    success=False,
                    error=f"Mod is for '{mod_info.game_domain}' but collection is for '{state.game_domain}'"
                )

            progress("init", 0.0, "Validating API key...")
            user_info = self.api.validate_key()
            is_premium = self._check_premium(user_info)

            progress("fetch", 0.1, "Fetching mod info...")
            nexus_mod = self.api.get_mod_info(mod_info.game_domain, mod_info.mod_id)
            mod_name = nexus_mod.get("name", f"Mod {mod_info.mod_id}")

            files = self.api.get_mod_files(mod_info.game_domain, mod_info.mod_id)
            if not files:
                return AddResult(
                    mod_name=mod_name, mod_id=mod_info.mod_id, file_name="",
                    success=False, error="No files found for this mod"
                )

            selected = _select_mod_file(files, file_id)
            if selected is None:
                return AddResult(
                    mod_name=mod_name, mod_id=mod_info.mod_id, file_name="",
                    success=False,
                    error=f"Could not select file. Use file_id param. Available: {[f['file_id'] for f in files]}"
                )

            selected_file_id = selected["file_id"]
            selected_name = selected.get("name", selected.get("file_name", ""))
            selected_version = selected.get("version", "")

            download_info = {
                "mod_id": mod_info.mod_id,
                "mod_name": mod_name,
                "file_id": selected_file_id,
                "filename": selected.get("file_name", selected_name),
                "version": selected_version,
                # Only size_in_bytes is an exact byte count. The legacy REST
                # "size" field is in KB and must never be verified as bytes, so
                # when it is the only value available the size check is skipped.
                "size_bytes": int(selected.get("size_in_bytes") or 0),
                "optional": False,
                "requirements": [],
            }

            if not is_premium:
                # Free user: register as pending download
                browser_url = f"https://www.nexusmods.com/{mod_info.game_domain}/mods/{mod_info.mod_id}?tab=files&file_id={selected_file_id}"
                download_info["manual"] = True
                download_info["download_status"] = "pending_download"
                download_info["browser_url"] = browser_url
                state.add_mod(download_info)
                # State is keyed by file_id, not mod_id
                state.mods[selected_file_id].phase = 999
                # Registering this mod is an explicit re-add, so its removal
                # tombstone (if any) is cleared with the same commit.
                state.clear_removed(mod_info.mod_id)
                state.save()

                pending = PendingDownload(
                    mod_id=mod_info.mod_id,
                    mod_name=mod_name,
                    file_id=selected_file_id,
                    filename=download_info["filename"],
                    size_bytes=download_info["size_bytes"],
                    browser_url=browser_url,
                )
                progress("done", 1.0, f"Registered {mod_name} for manual download")
                return AddResult(
                    mod_name=mod_name, mod_id=mod_info.mod_id, file_name=selected_name,
                    success=True, pending_download=pending,
                )

            def on_download_progress(bytes_dl: int, total_bytes: int) -> None:
                if total_bytes > 0:
                    pct = 0.2 + 0.6 * (bytes_dl / total_bytes)
                    progress("download", pct, f"Downloading {mod_name}...")

            progress("download", 0.2, f"Downloading {mod_name}...")
            downloader = Downloader(self.api)
            _attach_expected_md5(state, [download_info])
            results = downloader.download_mods(
                game_domain=mod_info.game_domain,
                mods=[download_info],
                target_dir=mods_dir,
                on_progress=on_download_progress,
                show_progress=show_progress,
            )

            if not results:
                return AddResult(
                    mod_name=mod_name, mod_id=mod_info.mod_id, file_name=selected_name,
                    success=False, error="Download failed"
                )

            progress("extract", 0.85, "Extracting...")
            # Read the FOMOD installer (if any) and settle the option
            # choices BEFORE extracting anything, so a multi-file add
            # extracts nothing when one file's choices cannot be
            # resolved. Picks recorded by a previous install are kept as
            # far as they still name real options.
            resolved_picks: list = []
            fomod_seen = False
            if not no_extract:
                previous = state.mods.get(selected_file_id)
                stored_picks = previous.fomod_picks if previous is not None else None
                for _mod, file_path in results:
                    if not is_archive(file_path):
                        continue
                    config = _fomod_config_for_archive(file_path)
                    if config is None:
                        continue
                    fomod_seen = True
                    resolved_picks = _resolve_fomod_picks(
                        config,
                        file_path.name,
                        fomod_picks,
                        stored_picks,
                        on_fomod_prompt,
                    )

            # Picks the caller asked for that nothing could apply must not
            # vanish: the add succeeds, but it says so.
            add_warnings: list[str] = []
            if fomod_picks and not fomod_seen:
                add_warnings.append(_unused_picks_warning(mod_name, no_extract))
            # Warn, never block, when this mod's requirements are not
            # installed. Collections include their dependencies; `add`
            # takes one mod and has no idea what else it needs. The list
            # comes from whatever the mod info carries, and silence when
            # it carries none: we cannot check what we cannot see, and a
            # false warning is worse than none.
            reqs = _requirements_from_mod_info(nexus_mod)
            for req_id in missing_requirements(state, reqs):
                add_warnings.append(
                    f"missing dependency: mod {req_id} is not installed"
                )
            config_record = None
            contributed: list[str] = []
            for _mod, file_path in results:
                try:
                    if not no_extract and is_archive(file_path):
                        final_files = extract_archive(file_path, mods_dir)
                        contributed.extend(
                            str(ef.relative_to(mods_dir)) for ef in final_files
                        )
                        config_record = _fomod_config_record(file_path)
                    else:
                        # A retained archive or a loose file is this
                        # mod's footprint; disable needs to know it.
                        contributed.append(str(file_path.relative_to(mods_dir)))
                except ExtractionError as e:
                    return AddResult(
                        mod_name=mod_name, mod_id=mod_info.mod_id, file_name=selected_name,
                        success=False, error=f"Extraction error: {exception_reason(e)}"
                    )

            # Register in state as manual
            download_info["manual"] = True
            state.add_mod(download_info)
            # State is keyed by file_id, not mod_id
            state.mods[selected_file_id].phase = 999
            state.mods[selected_file_id].contributed_files = contributed
            if config_record is not None:
                state.mods[selected_file_id].fomod_config = config_record
            if resolved_picks:
                state.mods[selected_file_id].fomod_picks = [
                    {"step": p.step, "group": p.group, "option": p.option}
                    for p in resolved_picks
                ]
            # Registering this mod is an explicit re-add, so its removal
            # tombstone (if any) is cleared with the same commit.
            state.clear_removed(mod_info.mod_id)
            state.save()

            self._maybe_sync_tracked(state)

            if not no_load_order:
                progress("loadorder", 0.95, "Regenerating load order...")
                try:
                    self._regen_load_order_from_state(mods_dir, state)
                except LoadOrderRegenError:
                    # The add is already committed and AddResult has no
                    # warning channel: a regeneration failure must not turn
                    # a committed add into a claimed failure. Kept silent
                    # as before; 'nexus-dl load-order' retries.
                    pass

            progress("done", 1.0, f"Added {mod_name}")
            return AddResult(
                mod_name=mod_name, mod_id=mod_info.mod_id, file_name=selected_name,
                success=True, warnings=add_warnings,
            )

    def add_local(self, name: str, mods_dir: Path, no_load_order: bool = False) -> int:
        """Register a pre-existing local mod. Returns synthetic mod_id."""
        with CollectionLock(mods_dir):
            state = CollectionState(mods_dir)
            state.load()

            # State is keyed by file_id: pick a negative id that is unique among
            # both file_id keys and mod_ids, and use it for both so repeated
            # add-local calls never collide and the phase lookup below is valid.
            existing_ids = set(state.mods.keys())
            existing_mod_ids = {ms.mod_id for ms in state.mods.values()}
            synthetic_id = -1
            while synthetic_id in existing_ids or synthetic_id in existing_mod_ids:
                synthetic_id -= 1

            mod_info = {
                "mod_id": synthetic_id,
                "mod_name": name,
                "file_id": synthetic_id,
                "filename": "",
                "version": "",
                "optional": False,
                "manual": True,
            }

            state.add_mod(mod_info)
            state.mods[synthetic_id].phase = 999
            state.save()

            if not no_load_order:
                try:
                    self._regen_load_order_from_state(mods_dir, state)
                except LoadOrderRegenError:
                    # The registration is already committed and add_local
                    # returns only the synthetic id: same silent-tolerated
                    # contract as add_mod, retried via 'nexus-dl load-order'.
                    pass

            return synthetic_id

    def remove_mod(
        self,
        mods_dir: Path,
        *,
        file_id: int | None = None,
        mod_id: int | None = None,
        no_load_order: bool = False,
    ) -> RemoveModResult:
        """Remove one mod from collection state (safe removal).

        Exactly one of file_id/mod_id must be given (ValueError
        otherwise); file_id resolves to its mod_id, and ALL state entries
        of that mod are removed. Unknown ids raise StateError before
        anything is mutated.

        This never touches payload files on disk: extracted content and
        retained archives stay exactly as they are (cleaning up deployed
        files is undeploy's job). State is updated atomically under the
        collection lock; a save failure raises StateError with the prior
        state file left standing. Afterwards the load order is
        regenerated from the remaining mods unless no_load_order is set;
        a regeneration failure after the save is reported as a warning
        because the removal itself is already persisted.

        Removal is recorded as a tombstone so it survives sync/update:
        the tombstone keys on mod_id (the Nexus mod page), so a later
        collection revision that assigns a new file id to the same page
        cannot silently reinstall it. Tombstones apply to positive
        Nexus mod_ids only: local synthetic mods (negative ids from
        add_local) are removed without one (nothing in sync/update can
        reinstall them), and legacy entries whose mod_id defaults to 0
        are removed without tombstoning id 0 (0 is not a real mod page).
        A tombstone is cleared by explicitly re-adding the mod. Note
        that older builds ignore the removed_mods key and drop it on
        save, so a downgrade can resurrect removed mods.
        """
        if (file_id is None) == (mod_id is None):
            raise ValueError("exactly one of file_id or mod_id is required")

        warnings: list[str] = []
        with CollectionLock(mods_dir):
            state = CollectionState(mods_dir)
            state.load()

            if file_id is not None:
                entry = state.get_file(file_id)
                if entry is None:
                    raise StateError(
                        f"No mod file with file_id {file_id} in {mods_dir}"
                    )
                target_mod_id = entry.mod_id
            else:
                # The guard above guarantees exactly one of the two is set.
                assert mod_id is not None
                if state.get_mod(mod_id) is None:
                    raise StateError(
                        f"No mod with mod_id {mod_id} in {mods_dir}"
                    )
                target_mod_id = mod_id

            removed_file_ids = sorted(
                fid
                for fid, ms in state.mods.items()
                if ms.mod_id == target_mod_id
            )
            removed_name = state.mods[removed_file_ids[0]].name if removed_file_ids else ""
            state.remove_mod(target_mod_id)
            # Tombstone positive Nexus mod pages only (see docstring).
            if target_mod_id > 0:
                state.mark_removed(target_mod_id, removed_name)
            try:
                state.save()
            except Exception as e:
                # CollectionState.save() is atomic (temp file + rename):
                # the prior state file stands unchanged on any failure.
                raise StateError(
                    f"Failed to save state after removing mod "
                    f"{target_mod_id}: {exception_reason(e)}"
                ) from e

            if not no_load_order:
                try:
                    self._regen_load_order_from_state(mods_dir, state)
                except Exception as e:
                    # Truthful partial success: the removal is persisted
                    # and only the load order regeneration failed.
                    warnings.append(
                        f"Mod {target_mod_id} was removed and the removal "
                        f"was saved, but load order regeneration failed: "
                        f"{exception_reason(e)}. Run 'nexus-dl load-order {mods_dir}' to "
                        f"retry."
                    )

        return RemoveModResult(
            removed_file_ids=removed_file_ids,
            removed_mod_ids=[target_mod_id],
            warnings=warnings,
        )

    def get_mod_files(self, mod_url: str) -> list[ModFileInfo]:
        """Get available files for a mod by URL."""
        mod_info = parse_mod_url(mod_url)
        files = self.api.get_mod_files(mod_info.game_domain, mod_info.mod_id)
        category_names = {1: "Main", 2: "Update", 3: "Optional", 4: "Old", 5: "Misc", 6: "Archived"}
        return [
            ModFileInfo(
                file_id=f.get("file_id", 0),
                name=f.get("name", f.get("file_name", "")),
                version=f.get("version", ""),
                category=category_names.get(f.get("category_id", 0), "Unknown"),
                # Legacy "size" is in KB; only size_in_bytes is reported as bytes
                size_bytes=(f.get("size_in_bytes") or 0),
            )
            for f in files
        ]

    def pre_deploy_checks(
        self, state: "CollectionState", game_dir: Path, mods_dir: Path, method: str,
        prefix: Path | None = None,
    ) -> list["DeployWarning"]:
        """Run pre-deployment validation checks.

        Checks report on the effective values for this deploy (the
        ``game_dir`` and ``prefix`` the caller resolved), not on what
        happens to be recorded in state. A first deploy of a game the
        auto-detect list does not know, with ``--game-dir`` and
        ``--prefix`` given on the command line, must pass: the user did
        set them.
        """
        warnings: list[DeployWarning] = []

        # 1. Proton prefix missing (None means nothing resolved one: not
        # the parameter, not state, not auto-detection).
        if prefix is None:
            warnings.append(DeployWarning(
                "no_prefix", "Proton prefix not found - plugins.txt and INI won't be written", "warning"
            ))

        # 2. No mods extracted
        mod_files = [f for f in mods_dir.iterdir() if f.is_file() or f.is_dir()]
        # Filter out internal files this tool generates in the mods dir
        # (.nexus-state.json, .nexus-state.lock, .nexus-journal.jsonl,
        # .nexus-backups/)
        mod_files = [f for f in mod_files if not f.name.startswith(".")]
        if not mod_files:
            warnings.append(DeployWarning(
                "no_mods", "No mods found in mods directory", "error"
            ))

        # 3. Game dir not writable
        if not os.access(game_dir, os.W_OK):
            warnings.append(DeployWarning(
                "not_writable", f"Game directory is not writable: {game_dir}", "error"
            ))

        # 4. Symlinks not supported (only check if using symlink method)
        if method == "symlink":
            test_link = game_dir / ".nexus_symlink_test"
            try:
                test_link.symlink_to(mods_dir)
                test_link.unlink()
            except OSError:
                warnings.append(DeployWarning(
                    "no_symlinks",
                    "Filesystem does not support symlinks - use --copy instead",
                    "error",
                ))

        # 5. Pending downloads
        pending = state.get_pending_mods()
        if pending:
            warnings.append(DeployWarning(
                "pending_downloads",
                f"{len(pending)} mod(s) still pending download",
                "warning",
            ))

        # 6. Game not in auto-detect, and no game dir from any source
        if state.game_domain not in STEAM_APP_IDS and not game_dir:
            warnings.append(DeployWarning(
                "unknown_game",
                f"Game '{state.game_domain}' not in auto-detect list - game dir must be set manually",
                "error",
            ))

        # 7. Stale deployment
        if state.deployed_at and game_dir:
            game_path = Path(game_dir)
            # Check common game executables
            for exe_name in ["Starfield.exe", "SkyrimSE.exe", "Fallout4.exe"]:
                exe = game_path / exe_name
                if exe.exists():
                    import datetime
                    exe_mtime = datetime.datetime.fromtimestamp(
                        exe.stat().st_mtime, tz=datetime.timezone.utc
                    )
                    deployed = datetime.datetime.fromisoformat(state.deployed_at)
                    if exe_mtime > deployed:
                        warnings.append(DeployWarning(
                            "stale_deploy",
                            "Game executable was modified after last deployment - consider redeploying",
                            "warning",
                        ))
                    break

        return warnings

    def deploy(
        self,
        mods_dir: Path,
        game_dir: Path | None = None,
        prefix: Path | None = None,
        use_copy: bool = False,
        on_progress: ProgressCallback | None = None,
        on_file_progress: "Callable[[int, int, str], None] | None" = None,
    ) -> DeployResult:
        """Deploy mods from staging to game directory.

        The deployment is tracked and reversible: originals at destination
        paths are backed up before being replaced, every mutation is
        journaled, and files changed outside nexus-dl are refused instead of
        overwritten. The previous deployment is removed safely first; if any
        old entry cannot be safely removed, the redeploy stops and the
        remaining entries stay in state for retry.

        ``on_file_progress(done, total, detail)`` fires once per file in
        the write loop. It is separate from ``on_progress`` because the
        loop is the long silent phase and a caller rendering a progress
        line needs the raw counts, not a pre-baked message.
        """
        progress = on_progress or _noop_progress

        with CollectionLock(mods_dir):
            state = CollectionState(mods_dir)
            state.load()
            game_domain = state.game_domain

            # Resolve game directory
            if game_dir is None and state.game_dir:
                game_dir = Path(state.game_dir)
            if game_dir is None:
                progress("detect", 0.05, "Detecting game directory from Steam...")
                game_dir = find_game_dir(game_domain)
                if game_dir is None:
                    return DeployResult(0, [], ["Could not find game directory. Use game_dir param."], "", False, warnings=[])

            if not game_dir.exists():
                return DeployResult(0, [], [f"Game directory does not exist: {game_dir}"], str(game_dir), False, warnings=[])

            overlap_error = _validate_no_overlap(mods_dir, game_dir)
            if overlap_error:
                return DeployResult(0, [], [overlap_error], str(game_dir), False, warnings=[])

            # Trusted roots for recovery come from configuration only (never
            # from journal content): the resolved game dir plus any recorded
            # game dir/prefix from previous runs.
            recovery_roots: list[Path] = []
            for candidate in (game_dir, state.game_dir, prefix, state.proton_prefix):
                if candidate:
                    p = Path(candidate)
                    if p not in recovery_roots:
                        recovery_roots.append(p)

            # Recover from any interrupted deploy/undeploy before mutating.
            ownership, reports = recover_ownership(
                state.deployed_files, mods_dir, recovery_roots
            )
            journal = DeployJournal(mods_dir)
            if journal.exists() or reports:
                state.deployed_files = list(ownership.values())
                state.save()
                journal.clear()

            # Resolve Proton prefix before the checks, so they report on
            # the value this deploy will actually use. The parameter wins
            # over state, and auto-detection is the last resort.
            if prefix is None and state.proton_prefix:
                prefix = Path(state.proton_prefix)
            if prefix is None:
                prefix = find_proton_prefix(game_domain)

            # Pre-deploy validation.
            #
            # Proton/Wine cannot read Linux symlinks that cross outside
            # the prefix, so a symlink deploy to a Proton game produces
            # files the game sees by name but cannot load. Force copy
            # mode whenever a Proton prefix is in play.
            method = "copy" if use_copy else "symlink"
            proton_symlink_override = method == "symlink" and prefix
            if proton_symlink_override:
                method = "copy"
            check_warnings = self.pre_deploy_checks(state, game_dir, mods_dir, method, prefix)
            deploy_warnings = [w.message for w in check_warnings]
            deploy_warnings.extend(reports)
            if proton_symlink_override:
                deploy_warnings.append(
                    "Using copy mode: Proton games cannot read symlinks "
                    "through the Wine prefix."
                )

            # Block on errors
            errors_found = [w for w in check_warnings if w.severity == "error"]
            if errors_found:
                return DeployResult(
                    0, [], [w.message for w in errors_found], str(game_dir), False,
                    warnings=deploy_warnings,
                )

            # Preflight: classify BEFORE any destructive removal of the old
            # deployment, so a staging directory with nothing deployable
            # never tears down a working deployment.
            progress("classify", 0.15, "Classifying files...")
            manifest_choices = {}
            expected_plugins: list[str] = []
            if state.manifest_data:
                manifest = CollectionManifest.from_dict(state.manifest_data)
                manifest_choices = manifest.mod_choices
                # Plugin names the load order knows. Used at publish to
                # fix up a case mismatch before the game sees it.
                expected_plugins = [
                    str(p.get("filename") or p.get("name") or "")
                    for p in (manifest.plugins or [])
                    if (p.get("filename") or p.get("name"))
                ]
            # FOMOD option picks recorded by standalone adds and imports.
            fomod_picks = {
                mod.file_id: mod.fomod_picks
                for mod in state.mods.values()
                if mod.fomod_picks
            }
            # Installer configs recorded at extract time. The tree cannot
            # hold more than one (a later mod's fomod/ModuleConfig.xml
            # overwrites an earlier one), so these copies are what let
            # deploy plan for every FOMOD mod at once.
            fomod_configs = {
                mod.file_id: {**mod.fomod_config, "mod_id": mod.mod_id}
                for mod in state.mods.values()
                if mod.fomod_config
            }
            # What the game looks like from here: the load order the
            # installer's fileDependency/gameDependency conditions are
            # answered against. Without it those conditions are
            # undecidable and multi-game installers fall back to the
            # folder heuristic.
            from .game_state import read_game_state

            game_state = read_game_state(prefix, game_domain)
            plan = classify_files(
                mods_dir,
                game_domain,
                mod_choices=manifest_choices,
                fomod_picks=fomod_picks,
                fomod_configs=fomod_configs,
                mod_names={
                    mod.file_id: mod.name for mod in state.mods.values()
                },
                game=game_state,
            )

            # A disabled mod is "temporarily not installed": its state
            # entry, its picks, and its files on disk all stay, but its
            # files must not land. The extraction tree is merged, so this
            # works through the attribution recorded at extract time.
            # A mod with no attribution cannot be isolated, and says so
            # rather than guessing.
            disabled_paths: set[str] = set()
            unattributed: list[str] = []
            for mod in state.mods.values():
                if mod.enabled:
                    continue
                if mod.contributed_files:
                    disabled_paths.update(mod.contributed_files)
                else:
                    unattributed.append(mod.name)
            if disabled_paths:
                plan.data_files = [
                    (src, dest)
                    for src, dest in plan.data_files
                    if str(src.relative_to(mods_dir)) not in disabled_paths
                ]
                plan.game_root_files = [
                    (src, dest)
                    for src, dest in plan.game_root_files
                    if str(src.relative_to(mods_dir)) not in disabled_paths
                ]
            for name in unattributed:
                deploy_warnings.append(
                    f"{name} is disabled but its files cannot be isolated "
                    "(no per-mod attribution on record for it); re-add or "
                    "re-sync it to record one"
                )

            deploy_warnings.extend(plan.warnings)

            if plan.total_files == 0:
                errors: list[str] = []
                if ownership:
                    errors.append(
                        "Nothing to deploy; the existing deployment was left "
                        "in place (run undeploy to remove it)."
                    )
                return DeployResult(0, [], errors, str(game_dir), False, skipped=len(plan.skipped), warnings=deploy_warnings)

            backups = BackupStore(mods_dir)
            roots = []
            for candidate in (game_dir, state.game_dir, prefix, state.proton_prefix):
                if candidate:
                    p = Path(candidate)
                    if p not in roots:
                        roots.append(p)

            # Remove previous deployment safely; stop the redeploy when any
            # old entry cannot be verified and removed.
            if ownership:
                progress("undeploy", 0.1, "Removing previous deployment...")
                kept, problems, _notes, _cleared = undeploy_records(ownership, roots, backups, journal)
                if kept:
                    state.deployed_files = kept
                    state.save()
                    # Journal kept: entries were not all safely resolved, so
                    # dangling intents must stay recoverable for the next run.
                    errors = [
                        (f"Redeploy stopped: {len(kept)} previously deployed "
                        f"file(s) could not be safely removed. Resolve the "
                        f"reported issues and run deploy (or undeploy) again.")
                    ] + problems
                    return DeployResult(
                        0, [], errors, str(game_dir), False, warnings=deploy_warnings
                    )
                state.deployed_files = []
                state.deployed_at = None

            # Deploy
            progress("deploy", 0.3, f"Deploying {plan.total_files} files via {method}...")

            def _file_progress(done: int, total: int, detail: str) -> None:
                if on_file_progress is not None:
                    on_file_progress(done, total, detail)
                # The write loop is the long silent phase; 0.3 to 1.0 is
                # the span left after classify/undeploy.
                frac = (done / total) if total else 1.0
                progress("deploy", 0.3 + 0.7 * frac, detail)

            result = deploy_files(
                plan, game_dir, method=method, dry_run=False,
                backups=backups, journal=journal, ownership=ownership,
                on_progress=_file_progress,
                expected_plugins=expected_plugins,
            )

            # Write plugins.txt and the game INI through the same tracking,
            # but only when the file deployment itself had no failures.
            if prefix and result.errors:
                deploy_warnings.append(
                    "Skipped plugins.txt/game INI writes because the "
                    "deployment reported errors."
                )
            if prefix and not result.errors:
                plugins_src = mods_dir / "plugins.txt"
                # When the staging plugins.txt has no plugin entries (the
                # manifest carried no plugin data, or the file is just
                # comments), build one from what was actually deployed.
                # The deployed files are the ground truth for plugin names.
                existing_plugins = ""
                if plugins_src.exists():
                    for line in plugins_src.read_text().splitlines():
                        stripped = line.strip()
                        if stripped and not stripped.startswith("#"):
                            existing_plugins = stripped
                            break
                if not existing_plugins:
                    plugin_exts = (".esp", ".esm", ".esl")
                    seen_plugins: set[str] = set()
                    plugin_lines: list[str] = []
                    for df in result.deployed:
                        if df.kind != "mod":
                            continue
                        name = Path(df.dest).name
                        if not name.lower().endswith(plugin_exts):
                            continue
                        if name not in seen_plugins:
                            seen_plugins.add(name)
                            plugin_lines.append(f"*{name}")
                    if plugin_lines:
                        plugins_src.write_text(
                            "# Plugin Load Order (generated from deployed files)\n"
                            "# Generated by nexus-collection-dl\n"
                            f"# Game: {game_domain}\n"
                            "#\n"
                            "# Lines beginning with * are enabled.\n"
                            "\n"
                            + "\n".join(plugin_lines) + "\n"
                        )
                plugins_dest = get_plugins_txt_dest(prefix, game_domain)
                if plugins_dest and plugins_src.exists():
                    try:
                        write_plugins_txt(
                            plugins_src, plugins_dest,
                            root=prefix, backups=backups, journal=journal,
                            ownership=ownership,
                        )
                    except (DeployError, OSError) as e:
                        result.errors.append(f"plugins.txt: {exception_reason(e)}")
                ini_path = get_game_ini_path(prefix, game_domain)
                if ini_path:
                    try:
                        write_game_ini(
                            ini_path, game_domain,
                            root=prefix, backups=backups, journal=journal,
                            ownership=ownership,
                        )
                    except (DeployError, OSError) as e:
                        result.errors.append(f"game INI: {exception_reason(e)}")

            # Save state
            state.game_dir = str(game_dir)
            if prefix:
                state.proton_prefix = str(prefix)
            state.deployed_files = list(ownership.values())
            state.deployed_at = (
                datetime.now(timezone.utc).isoformat() if ownership else None
            )
            state.save()
            if not result.errors:
                # Only a clean run may discard the journal: every record is
                # accounted for in the saved state. On errors, keep it so
                # dangling intents stay recoverable on the next run.
                journal.clear()

            has_sfse = any("sfse_loader" in str(f.dest).lower() for f in result.deployed)

            progress("done", 1.0, f"Deployed {len(result.deployed)} files")
            return DeployResult(
                deployed_count=len(result.deployed),
                conflicts=result.conflicts,
                errors=result.errors,
                game_dir=str(game_dir),
                has_sfse=has_sfse,
                skipped=len(result.skipped),
                warnings=deploy_warnings,
            )

    def undeploy(self, mods_dir: Path) -> int:
        """Remove all deployed mod files and restore backed-up originals.

        Returns the number of cleared deployment entries on full success.
        Raises UndeployError (after persisting state) when any entry was
        refused or failed, carrying the removed count plus the refused and
        failed paths, so callers never report a partial undeploy as clean.
        Refused/failed entries stay in state so the operation is retryable.
        """
        with CollectionLock(mods_dir):
            state = CollectionState(mods_dir)
            state.load()

            roots = [Path(p) for p in (state.game_dir, state.proton_prefix) if p]
            ownership, reports = recover_ownership(state.deployed_files, mods_dir, roots)
            journal = DeployJournal(mods_dir)
            if journal.exists() or reports:
                state.deployed_files = list(ownership.values())
                state.save()
                journal.clear()

            if not ownership:
                if state.deployed_files or state.deployed_at:
                    state.deployed_files = []
                    state.deployed_at = None
                    state.game_dir = ""
                    state.save()
                return 0

            backups = BackupStore(mods_dir)
            kept, problems, _notes, cleared = undeploy_records(ownership, roots, backups, journal)

            if kept:
                # Keep the remaining records (and backups) for retry; never
                # clear state or the journal before a fully safe undeploy.
                state.deployed_files = kept
                state.save()
                refused = [m for m in problems if m.startswith("refused")]
                failed = [m for m in problems if not m.startswith("refused")]
                raise UndeployError(removed=cleared, refused=refused, failed=failed)

            state.deployed_files = []
            state.deployed_at = None
            state.game_dir = ""
            state.save()
            journal.clear()
            return cleared

    def regenerate_load_order(self, mods_dir: Path) -> list[str]:
        """Regenerate load order from cached manifest. Returns list of written file names."""
        state = CollectionState(mods_dir)
        state.load()

        if not state.manifest_data:
            raise StateError("No cached manifest found. Run sync or update first.")

        files = self._regen_load_order_from_state(mods_dir, state)
        return files

    def track_sync_enable(self, mods_dir: Path) -> tuple[int, int]:
        """Enable track sync and run initial sync. Returns (tracked, untracked)."""
        with CollectionLock(mods_dir):
            state = CollectionState(mods_dir)
            state.load()

            state.track_sync_enabled = True
            state.save()

            return self._sync_tracked_mods(state)

    def track_sync_disable(self, mods_dir: Path) -> None:
        """Disable track sync."""
        with CollectionLock(mods_dir):
            state = CollectionState(mods_dir)
            state.load()
            state.track_sync_enabled = False
            state.save()

    def track_sync_push(self, mods_dir: Path) -> tuple[int, int]:
        """One-shot sync regardless of enable/disable state.

        Read-only with respect to local state (only Nexus-side tracking
        changes), so it deliberately does not take the collection lock.
        """
        state = CollectionState(mods_dir)
        state.load()
        return self._sync_tracked_mods(state)

    def install_fomod_picks(
        self,
        mods_dir: Path,
        file_id: int,
        picks: "Sequence[Pick] | None" = None,
        on_fomod_prompt: "Callable[..., Sequence[Pick]] | None" = None,
    ) -> str:
        """Install a FOMOD archive that is waiting on its option choices.

        This is how a ``pending_choices`` entry gets finished: the archive
        the click kept is extracted with the given picks (or the ones the
        prompt callback collects) and the mod is recorded as downloaded
        with those picks stored. Returns a one-line human report.

        Raises the same errors the ``add`` path does: ``PickError`` for a
        pick the installer does not offer, ``FomodChoicesError`` when
        there is nothing to pick from and nobody to ask, ``StateError``
        when the entry or its archive is gone.
        """
        with CollectionLock(mods_dir):
            state = CollectionState(mods_dir)
            state.load()
            mod = state.mods.get(file_id)
            if mod is None:
                raise StateError(
                    f"no tracked mod with file id {file_id} in this collection"
                )
            archive = mods_dir / mod.filename
            if not mod.filename or not archive.exists():
                raise StateError(
                    f"the archive for {mod.name or mod.filename} is not in "
                    f"{mods_dir}; click the download link again to fetch it"
                )

            config = _fomod_config_for_archive(archive)
            if config is None:
                raise StateError(f"{archive.name} is not a FOMOD mod")

            resolved = _resolve_fomod_picks(
                config, archive.name, picks, mod.fomod_picks, on_fomod_prompt
            )
            if not resolved:
                raise FomodChoicesError(archive.name)

            final_files = extract_archive(archive, mods_dir)
            config_record = _fomod_config_record(archive)
            mod.download_status = "downloaded"
            mod.browser_url = ""
            mod.installed_at = datetime.now(timezone.utc).isoformat()
            # Attribution for `disable` and `gc`: the extraction tree is
            # merged, so without this a disabled mod cannot be told
            # apart from its neighbours at deploy time.
            mod.contributed_files = [
                str(ef.relative_to(mods_dir)) for ef in final_files
            ]
            mod.fomod_picks = [
                {"step": p.step, "group": p.group, "option": p.option}
                for p in resolved
            ]
            if config_record is not None:
                mod.fomod_config = config_record
            # Finishing the install is explicit re-add intent, so any
            # removal tombstone for the page goes with the same commit.
            state.clear_removed(mod.mod_id)
            state.save()

        return (
            f"{mod.name or mod.filename} installed with "
            f"{len(resolved)} option choice(s)"
        )

    def import_downloads(
        self,
        mods_dir: Path,
        no_load_order: bool = False,
        no_extract: bool = False,
        on_progress: ProgressCallback | None = None,
        fomod_picks: "Sequence[Pick] | None" = None,
        on_fomod_prompt: "Callable[..., Sequence[Pick]] | None" = None,
    ) -> ImportResult:
        """Match manually downloaded files to pending mods, extract, and update state.

        Matching is deterministic and conservative. When the cached manifest
        provides an expected content hash, verification governs: name matches
        (exact archive filename, exact stem, or the Nexus browser suffix
        "{name}-{modId}-{version}-{timestamp}") are accepted only if the
        content hash checks out, and a name match with wrong content is
        rejected with an error. Without a known hash, name precedence applies:
        exact filename, exact stem, then the browser suffix parsed from known
        identities. Ambiguous or unverifiable candidates stay pending with a
        useful error instead of being guessed at, each disk file fulfills at
        most one pending item per run, and state is persisted after each
        successful import before the archive is cleaned up.
        """
        progress = on_progress or _noop_progress
        errors: list[str] = []
        import_warnings: list[str] = []

        with CollectionLock(mods_dir):
            state = CollectionState(mods_dir)
            state.load()

            # Sorted by file_id so contested files resolve deterministically.
            # Tombstoned mods are never re-registered by import: a pending
            # entry whose mod_id is tombstoned as removed is skipped (it
            # stays pending and is surfaced by update's to_remove) rather
            # than fulfilled, because fulfilling it would resurrect a mod
            # the user removed. Extraction-failure recovery still works:
            # that path commits its pending entry AFTER clearing the
            # tombstone, so the entry is not tombstoned when import sees it.
            pending = sorted(
                (ms for ms in state.get_pending_mods()
                 if not state.is_removed(ms.mod_id)),
                key=lambda ms: ms.file_id,
            )
            if not pending:
                return ImportResult(
                    matched=0, extracted=0,
                    still_pending=len(state.get_pending_mods()),
                    errors=[], load_order_files=[],
                )

            # Candidate files: skip hidden files (.nexus-state.json, .DS_Store,
            # config dotfiles) and files this tool generates inside the mods dir,
            # so internal state and generated output are never matched or hashed.
            candidates = [
                f for f in sorted(mods_dir.iterdir())
                if f.is_file()
                and not f.name.startswith(".")
                and f.name.lower() not in _INTERNAL_FILENAMES
            ]

            by_identity_md5, by_name_md5 = _expected_md5_maps(state.manifest_data)
            md5_cache: dict[Path, str | None] = {}
            archive_cache: dict[Path, bool] = {}
            claimed: dict[Path, str] = {}  # disk file -> pending mod that consumed it

            matched = 0
            extracted = 0
            progress("import", 0.1, f"Scanning for {len(pending)} pending downloads...")

            for i, mod_state in enumerate(pending):
                progress("import", 0.1 + 0.8 * (i / len(pending)), f"Checking {mod_state.name}...")

                filename = (mod_state.filename or "").strip()
                mod_id = _as_int(mod_state.mod_id) or 0
                file_id = _as_int(mod_state.file_id) or 0
                expected_md5 = by_identity_md5.get((mod_id, file_id))
                if expected_md5 is None and filename:
                    expected_md5 = by_name_md5.get(filename.lower())

                available = [p for p in candidates if p not in claimed]
                match, ambiguous, hash_rejected = _resolve_import_match(
                    filename, mod_id, mod_state.version,
                    available, expected_md5, md5_cache, archive_cache,
                )

                if match is None and not ambiguous and not hash_rejected and claimed:
                    # Every match for this item was already consumed by an earlier
                    # pending item in this run: report the collision explicitly.
                    used, _, _ = _resolve_import_match(
                        filename, mod_id, mod_state.version,
                        list(claimed), expected_md5, md5_cache, archive_cache,
                    )
                    if used is not None:
                        errors.append(
                            f"Skipped {mod_state.name}: the matching file {used.name} "
                            f"already matched {claimed[used]} in this run."
                        )
                        continue

                if match is None:
                    if hash_rejected:
                        names = ", ".join(sorted(p.name for p in hash_rejected))
                        errors.append(
                            f"Content hash mismatch for {mod_state.name}: {names} matches "
                            f"the name but not the expected content hash for this file "
                            f"(stale, corrupt, or the wrong download). Delete it, "
                            f"re-download the file, then re-run import."
                        )
                    elif ambiguous:
                        names = ", ".join(sorted(p.name for p in ambiguous))
                        errors.append(
                            f"Ambiguous match for {mod_state.name}: several files could be "
                            f"'{mod_state.filename}' ({names}). Keep only the right one, "
                            f"then re-run import."
                        )
                    else:
                        hint = (
                            f" Download it from {mod_state.browser_url} and"
                            if mod_state.browser_url else ""
                        )
                        errors.append(
                            f"No local file found for {mod_state.name} (expected "
                            f"'{mod_state.filename}').{hint} place it in {mods_dir}, "
                            f"then re-run import."
                        )
                    continue

                # A file matched under an archive name must contain real archive
                # content: an error page or truncated download saved as
                # .zip/.7z/.rar must never count as downloaded, in any mode.
                if (
                    match.suffix.lower() in _MAGIC_VERIFIED_SUFFIXES
                    or Path(filename).suffix.lower() in _MAGIC_VERIFIED_SUFFIXES
                ) and sniff_archive_magic(match) is None:
                    errors.append(
                        f"Validation error for {mod_state.name}: {match.name} is not "
                        f"valid archive content (corrupt, incomplete, or an error "
                        f"page). Delete it, re-download the file, then re-run import."
                    )
                    continue

                claimed[match] = mod_state.name
                matched += 1

                is_arch = is_archive(match)
                resolved_picks: list = []
                config_record = None
                scoped = None
                fomod_seen = False
                contributed: list[str] = []
                try:
                    from .fomod_select import picks_for_file

                    scoped = (
                        picks_for_file(fomod_picks, match.name)
                        if fomod_picks
                        else None
                    )
                    if not no_extract and is_arch:
                        config = _fomod_config_for_archive(match)
                        if config is not None:
                            fomod_seen = True
                            resolved_picks = _resolve_fomod_picks(
                                config,
                                match.name,
                                scoped,
                                mod_state.fomod_picks,
                                on_fomod_prompt,
                            )
                        final_files = extract_archive(match, mods_dir)
                        contributed = [
                            str(ef.relative_to(mods_dir)) for ef in final_files
                        ]
                        config_record = _fomod_config_record(match)
                        extracted += 1
                    else:
                        # A retained archive or a loose file: whatever is
                        # on disk is this mod's footprint, and disable
                        # needs to know it.
                        contributed = [str(match.relative_to(mods_dir))]
                except FomodInstallerError as e:
                    # Same contract as a failed extraction: report it and
                    # leave the item pending rather than abort the batch.
                    # The message is self-contained, so it is the report.
                    errors.append(f"{mod_state.name}: {exception_reason(e)}")
                    continue
                except (FomodChoicesError, PickError) as e:
                    # Choices we cannot settle are the same shape of
                    # refusal: report, keep the archive, leave pending.
                    errors.append(f"{mod_state.name}: {exception_reason(e)}")
                    continue
                except ExtractionError as e:
                    # A failed extraction must never mark the mod downloaded.
                    # The archive stays on disk and the item stays pending;
                    # partially moved content cannot be rolled back.
                    errors.append(f"Extraction error for {mod_state.name}: {exception_reason(e)}")
                    continue

                # Picks the caller asked for that nothing could apply must
                # not vanish: the import succeeds, but it says so.
                if scoped and not fomod_seen:
                    import_warnings.append(
                        _unused_picks_warning(mod_state.name, no_extract)
                    )

                mod_state.download_status = "downloaded"
                mod_state.browser_url = ""
                mod_state.installed_at = datetime.now(timezone.utc).isoformat()
                if config_record is not None:
                    mod_state.fomod_config = config_record
                if resolved_picks:
                    mod_state.fomod_picks = [
                        {"step": p.step, "group": p.group, "option": p.option}
                        for p in resolved_picks
                    ]
                # Attribution for `disable` and `gc`: the extraction tree
                # is merged, so without this a disabled mod cannot be
                # told apart from its neighbours at deploy time.
                mod_state.contributed_files = contributed
                # Importing this mod's file is an explicit re-add of the mod
                # page, so any tombstone for it is cleared with the commit.
                state.clear_removed(mod_state.mod_id)
                state.save()

                pct = 0.1 + 0.8 * ((i + 1) / len(pending))
                progress("import", pct, f"Imported {mod_state.name}")

            state.save()

            # Regenerate load order if any mods were matched. Imports are already
            # persisted at this point, so post-save failures are reported as
            # errors instead of crashing the run.
            load_order_files: list[str] = []
            if matched > 0 and not no_load_order:
                progress("loadorder", 0.95, "Regenerating load order...")
                try:
                    load_order_files = self._regen_load_order_from_state(mods_dir, state)
                except Exception as e:
                    errors.append(f"Load order regeneration failed: {exception_reason(e)}")

            # Sync tracked mods (same as premium sync does after downloading)
            if matched > 0:
                try:
                    self._maybe_sync_tracked(state)
                except Exception as e:
                    errors.append(f"Track sync failed: {exception_reason(e)}")

            still_pending = len(state.get_pending_mods())
            progress("done", 1.0, f"Imported {matched} mods ({still_pending} still pending)")

            return ImportResult(
                matched=matched,
                extracted=extracted,
                still_pending=still_pending,
                errors=errors,
                load_order_files=load_order_files,
                warnings=import_warnings,
            )

    def add_nxm(
        self,
        uri: str,
        mods_dir: Path | None = None,
        output_dir: Path | None = None,
        no_load_order: bool = False,
        no_extract: bool = False,
        on_progress: ProgressCallback | None = None,
    ) -> NxmResult:
        """Handle one nxm:// individual-file download link.

        Flow (collection target):
          1. strict parse and local expiry check (no network),
          2. resolve the collection directory (read-only, no network),
          3. acquire the collection lock and load state under it,
          4. consistency checks: game match, file/mod identity, archive
             collisions; a proven local duplicate returns with no network,
          5. account validation: user_id identity check, premium status
             (including profile expiry), free-account credential rules,
          6. metadata fetch, signed download, integrity verification,
             extraction, state update, load order, tracked-mod sync.

        The key/expires credential pair is transient: it is forwarded to
        the download-link request only and never written to state,
        results, logs, or error messages. The URI user_id is a client
        identity check and is never sent to the API.

        Raises NxmParseError/NxmCollectionLinkError (parser), NxmRoutingError
        (no or ambiguous target), StateBusyError (concurrent operation).
        Failures never claim completion and never mutate prior state.
        """
        progress = on_progress or _noop_progress
        info = parse_nxm_url(uri)

        if info.is_expired():
            # Fail locally: an expired pair can never be redeemed, so no
            # request is made and nothing is mutated.
            return self._nxm_failure(
                info,
                "This download link has expired. Click 'Mod manager "
                "download' on the Nexus website again to get a fresh link.",
                re_click_required=True,
            )

        if output_dir is not None:
            return self._nxm_to_output(info, Path(output_dir), progress)

        # Routing is read-only: no directories, files, or state are
        # created for an unmapped or ambiguous game.
        target = resolve_collection_dir(info.game_domain, mods_dir)

        with CollectionLock(target):
            state = CollectionState(target)
            state.load()

            # Fail closed on the game check (defense in depth behind the
            # routing checks): a state with no game domain is never a
            # wildcard that accepts links for any game.
            if not state.game_domain:
                return self._nxm_failure(
                    info,
                    f"The collection state in {target} has no game domain; "
                    "run 'nexus-dl sync' there to create it properly.",
                    collection_dir=target,
                )
            if state.game_domain.lower() != info.game_domain.lower():
                return self._nxm_failure(
                    info,
                    f"This link is for '{info.game_domain}' but {target} "
                    f"holds a '{state.game_domain}' collection.",
                    collection_dir=target,
                )

            existing = state.get_file(info.file_id)
            if existing is not None and existing.mod_id != info.mod_id:
                return self._nxm_failure(
                    info,
                    f"File {info.file_id} is already registered under mod "
                    f"{existing.mod_id} ({existing.name}) in this "
                    f"collection, but the link claims mod {info.mod_id}. "
                    "Refusing to mix identities.",
                    collection_dir=target,
                )

            if (
                existing is not None
                and existing.download_status == "downloaded"
                and existing.filename
            ):
                archive = target / existing.filename
                if archive.is_file() and (
                    archive.suffix.lower() not in _MAGIC_VERIFIED_SUFFIXES
                    or sniff_archive_magic(archive) is not None
                ):
                    # Proven local duplicate: the artifact exists and is
                    # sane, so this is a no-op with zero network calls and,
                    # when no tombstone is set, zero writes. Re-clicking a
                    # removed mod's download link is explicit re-add intent,
                    # so a tombstone for the page is cleared here too (and
                    # only then is state saved).
                    dup_warnings: list[str] = []
                    if state.is_removed(info.mod_id):
                        state.clear_removed(info.mod_id)
                        try:
                            state.save()
                        except (OSError, StateError) as e:
                            dup_warnings.append(
                                "The file was already downloaded, but the "
                                "record of its earlier removal could not be "
                                f"cleared: {exception_reason(e)}. Nothing else failed; fix "
                                "the underlying problem (permissions, disk "
                                "space) and re-run to clear it."
                            )
                    return NxmResult(
                        success=True,
                        status="already_downloaded",
                        game_domain=info.game_domain,
                        mod_id=info.mod_id,
                        file_id=info.file_id,
                        mod_name=existing.name,
                        filename=existing.filename,
                        collection_dir=target,
                        download_status="downloaded",
                        warnings=dup_warnings,
                    )
                # Artifact gone or corrupt: do not blindly trust the state
                # flag; fall through and re-download. With archives kept
                # after extraction, a healthy downloaded entry hits the
                # already_downloaded no-op above, so this is only the
                # lost-or-damaged case. Zero-network handling of a
                # retained sane archive lives above (downloaded entries:
                # already_downloaded no-op) and below (pending entries:
                # extraction is completed from the archive).

            if (
                existing is not None
                and existing.download_status == "pending_choices"
                and existing.filename
                and not no_extract
                and (target / existing.filename).exists()
            ):
                # A FOMOD waiting on its option choices: a re-click must
                # not extract it, and there is nothing to download either.
                retained = target / existing.filename
                message = _nxm_fomod_blocker(retained) or (
                    f"{existing.filename} is a FOMOD mod that still needs "
                    f"option choices before it can be extracted. Run "
                    f"'nexus-dl import {target} --fomod-option "
                    f"\"Group:Option\"' to install it with your choices. "
                    f"The archive is kept at {retained}."
                )
                return self._nxm_failure(
                    info,
                    message,
                    collection_dir=target,
                    mod_name=existing.name,
                    filename=existing.filename,
                    download_status="pending_choices",
                )

            if (
                existing is not None
                and existing.download_status == "pending_download"
                and existing.filename
                and not no_extract
            ):
                archive = target / existing.filename
                # Offline integrity preflight: real file, valid magic for
                # magic-verified suffixes, and a match against the cached
                # manifest hash when one is known (the state stores no
                # expected byte size, so the hash is the offline
                # expected-content datum; the same verification 'import'
                # uses). An archive that fails any check is not trusted
                # for extraction.
                by_identity_md5, _ = _expected_md5_maps(state.manifest_data)
                expected_md5 = by_identity_md5.get((info.mod_id, info.file_id))
                if _retained_archive_is_sane(archive, expected_md5):
                    # Failed-extraction recovery with no download traffic:
                    # a pending entry whose verified archive was retained
                    # on disk finishes its extraction from that archive
                    # instead of re-downloading into the strict no-clobber
                    # refusal (the refusal would be guaranteed, because the
                    # retained archive still occupies the destination).
                    # This restores the recovery path a failed extraction
                    # promises: "import or a re-click can finish the job".
                    # No file download happens; when track-sync is enabled
                    # the post-save tracked-mod sync below may still make
                    # API calls. The archive is kept, not deleted: this run
                    # did not download it, and keeping it means a later
                    # re-click dedupes to already_downloaded with zero
                    # network.
                    progress(
                        "extract", 0.5,
                        f"Extracting retained archive {archive.name}...",
                    )
                    try:
                        final_files = extract_archive(archive, target)
                    except ExtractionError as e:
                        # Entry stays pending and the archive stays put:
                        # nothing was downloaded, nothing is overwritten.
                        return self._nxm_failure(
                            info,
                            f"The retained archive at {archive} could not "
                            f"be extracted: {exception_reason(e)}. Run 'nexus-dl import "
                            f"{target}' to retry, or remove the archive and "
                            "click the download link again for a fresh "
                            "copy.",
                            collection_dir=target,
                            mod_name=existing.name,
                            filename=existing.filename,
                        )
                    config_record = _fomod_config_record(archive)
                    existing.download_status = "downloaded"
                    existing.browser_url = ""
                    existing.installed_at = datetime.now(timezone.utc).isoformat()
                    # Attribution for `disable` and `gc`. The archive is
                    # kept on purpose here, so the payload list is what
                    # tells gc it is now redundant.
                    existing.contributed_files = [
                        str(ef.relative_to(target)) for ef in final_files
                    ]
                    if config_record is not None:
                        existing.fomod_config = config_record
                    # Committing this completion is explicit re-add intent,
                    # so the page's removal tombstone (if any) is cleared
                    # with the same save.
                    state.clear_removed(info.mod_id)
                    try:
                        state.save()
                    except (OSError, StateError) as e:
                        return self._nxm_failure(
                            info,
                            "Extraction from the retained archive succeeded "
                            "but the collection state could not be saved: "
                            f"{exception_reason(e)}. Extracted files were left in place and "
                            f"the archive is kept at {archive}. Fix the "
                            "underlying problem (permissions, disk space) "
                            f"and run 'nexus-dl import {target}'.",
                            collection_dir=target,
                            mod_name=existing.name,
                            filename=existing.filename,
                        )
                    # Same post-save follow-ups as the download path:
                    # truthful warnings on an otherwise successful result.
                    warnings: list[str] = []
                    try:
                        self._maybe_sync_tracked(state)
                    except Exception as e:
                        warnings.append(f"Tracked-mod sync failed: {exception_reason(e)}")
                    if not no_load_order:
                        progress("loadorder", 0.9, "Regenerating load order...")
                        try:
                            self._regen_load_order_from_state(target, state)
                        except Exception as e:
                            warnings.append(
                                f"Load order regeneration failed: {exception_reason(e)}. Run "
                                f"'nexus-dl load-order {target}' to retry."
                            )
                    progress("done", 1.0, f"Extracted {existing.name}")
                    return NxmResult(
                        success=True,
                        status="extracted_from_archive",
                        game_domain=info.game_domain,
                        mod_id=info.mod_id,
                        file_id=info.file_id,
                        mod_name=existing.name,
                        filename=existing.filename,
                        collection_dir=target,
                        extracted=True,
                        download_status="downloaded",
                        warnings=warnings,
                    )
                # Retained archive failed the sanity checks (missing
                # content, bad magic, or a known-hash mismatch): fall
                # through to the download path, where the strict
                # no-clobber publish refuses to replace it with
                # actionable guidance.

            progress("init", 0.05, "Validating API key...")
            account_error = self._nxm_account_error(info, collection_dir=target)
            if account_error:
                return self._nxm_failure(
                    info, account_error, collection_dir=target,
                    re_click_required=True,
                )

            progress("fetch", 0.1, "Fetching file metadata...")
            selected, meta_error = self._nxm_file_metadata(info)
            if meta_error:
                return self._nxm_failure(
                    info, meta_error, collection_dir=target
                )
            # _nxm_file_metadata returns a dict on success and only pairs a
            # None dict with an error string.
            assert selected is not None
            try:
                filename = validate_download_filename(
                    selected.get("file_name") or selected.get("name")
                )
            except DownloadError as e:
                return self._nxm_failure(info, exception_reason(e), collection_dir=target)

            mod_name = self._nxm_mod_name(info, existing=existing)

            # Archive collisions: never silently overwrite an archive that
            # belongs to a different file_id or to no tracked entry.
            for fid, ms in state.mods.items():
                if fid != info.file_id and ms.filename and ms.filename == filename:
                    return self._nxm_failure(
                        info,
                        f"'{filename}' already belongs to file {fid} "
                        f"({ms.name}) in this collection. Refusing to "
                        "overwrite it with a different file. Use --output "
                        "to save this download elsewhere.",
                        collection_dir=target,
                        filename=filename,
                    )
            owned_by_this = existing is not None and existing.filename == filename
            # lexists: a dangling symlink at the destination also counts as
            # an existing entry that must never be silently replaced.
            dest_existed = os.path.lexists(target / filename)
            if dest_existed and not owned_by_this:
                return self._nxm_failure(
                    info,
                    f"'{filename}' already exists in {target} and is not "
                    "this file's tracked archive. Refusing to overwrite "
                    "it. Remove it or use --output.",
                    collection_dir=target,
                    filename=filename,
                )
            # Diagnose why this file's own artifact still occupies the
            # destination, so the guaranteed no-clobber refusal below
            # names the real situation instead of guessing "corrupt":
            # either the artifact failed the sanity checks, or a
            # --no-extract run skipped the finish-extraction recovery for
            # an otherwise sane retained archive.
            owned_dest_hint = ""
            if dest_existed and owned_by_this:
                owned_dest_hint = (
                    " The existing file is this collection's own archive "
                    "for this download, and it failed the sanity checks "
                    "(corrupt or unreadable). Remove it and click the "
                    "download link again, or download the file in your "
                    f"browser and run 'nexus-dl import {target}'."
                )
                if (
                    no_extract
                    and existing is not None
                    and existing.download_status == "pending_download"
                ):
                    retained = target / existing.filename
                    by_identity, _ = _expected_md5_maps(state.manifest_data)
                    known_md5 = by_identity.get((info.mod_id, info.file_id))
                    if _retained_archive_is_sane(retained, known_md5):
                        owned_dest_hint = (
                            " The destination holds this file's retained "
                            "archive, which passed the sanity checks, but "
                            "this run passed --no-extract, so the "
                            "finish-extraction recovery was skipped and "
                            "the fresh download was refused rather than "
                            "replace the retained file. Re-run without "
                            "--no-extract to finish the extraction from "
                            "the retained archive, or remove the file to "
                            "force a fresh download."
                        )

            try:
                size_bytes = _metadata_size_bytes(selected)
            except ValueError:
                return self._nxm_failure(
                    info,
                    "File metadata from Nexus Mods is malformed (unexpected "
                    "file size); refusing to download without a verifiable "
                    "expected size.",
                    collection_dir=target,
                    filename=filename,
                )

            download_info = {
                "mod_id": info.mod_id,
                "mod_name": mod_name,
                "file_id": info.file_id,
                "filename": filename,
                "version": selected.get("version", ""),
                # Only size_in_bytes is an exact byte count (legacy "size"
                # is KB and must never be verified as bytes).
                "size_bytes": size_bytes,
                "optional": bool(existing.optional) if existing is not None else False,
                "requirements": existing.requirements if existing is not None else [],
            }
            # Cached manifest MD5 only (exact identity match); no extra
            # API calls and no guaranteed hash when unavailable.
            _attach_expected_md5(state, [download_info])
            # Verified-before-publish is only real when there is something
            # to verify against: with no positive byte size and no
            # well-formed cached manifest MD5 the downloader would publish
            # whatever arrived (a malformed hash is silently skipped
            # downstream, not enforced). Refuse the download instead
            # (nothing is written); the browser-download + import workflow
            # stays available.
            if size_bytes <= 0 and not _is_wellformed_md5(
                download_info.get("expected_md5")
            ):
                return self._nxm_failure(
                    info,
                    "Nexus Mods provided no file size and this collection "
                    "has no usable cached checksum for this file, so the "
                    "download could not be verified and nothing was "
                    "written. Download the file in your browser and run "
                    f"'nexus-dl import {target}', or retry the link later.",
                    collection_dir=target,
                    mod_name=mod_name,
                    filename=filename,
                )
            signed = None
            if info.has_credentials:
                # has_credentials is true only when both are present.
                assert info.key is not None and info.expires is not None
                signed = SignedLink(key=info.key, expires=info.expires)

            def on_download_progress(bytes_dl: int, total_bytes: int) -> None:
                if total_bytes > 0:
                    pct = 0.2 + 0.6 * (bytes_dl / total_bytes)
                    progress("download", pct, f"Downloading {mod_name}...")

            progress("download", 0.2, f"Downloading {filename}...")
            downloader = Downloader(self.api)
            try:
                path = downloader.download_mod(
                    info.game_domain,
                    download_info,
                    target,
                    on_progress=on_download_progress,
                    signed=signed,
                    # Never replace ANY existing destination entry on the
                    # nxm path, including this file's own corrupt archive:
                    # the publish itself is the enforcement point (a hard
                    # link that fails when anything exists at the final
                    # path), so a file created or replaced by anyone else
                    # between the precheck above and the publish can never
                    # be silently destroyed.
                    no_clobber=True,
                )
            except DownloadError as e:
                # State is untouched and any previous files are intact:
                # a failed download never claims completion.
                message = exception_reason(e)
                if owned_dest_hint and "refusing to overwrite" in message:
                    message += owned_dest_hint
                return self._nxm_failure(
                    info, message, collection_dir=target,
                    mod_name=mod_name, filename=filename,
                )

            extracted = False
            config_record = None
            # Default footprint is the downloaded file itself: a retained
            # archive or a loose file is what this mod put on disk. An
            # extraction below replaces it with the published payload.
            contributed = [str(path.relative_to(target))]
            if not no_extract and is_archive(path):
                blocker = _nxm_fomod_blocker(path)
                if blocker is not None:
                    # A FOMOD with no option choices: never extract it.
                    # Registering the entry as needing choices is what
                    # lets `import` finish the job with the user's picks.
                    pending_recorded = False
                    if existing is None:
                        download_info["manual"] = True
                        download_info["download_status"] = "pending_choices"
                        state.add_mod(download_info)
                        state.mods[info.file_id].phase = 999
                        # Same explicit re-add intent as the failed-
                        # extraction branch below.
                        state.clear_removed(info.mod_id)
                        try:
                            state.save()
                            pending_recorded = True
                        except (OSError, StateError):
                            pending_recorded = False
                    else:
                        previous_status = existing.download_status
                        existing.download_status = "pending_choices"
                        state.clear_removed(info.mod_id)
                        try:
                            state.save()
                            pending_recorded = True
                        except (OSError, StateError):
                            existing.download_status = previous_status
                    return self._nxm_failure(
                        info,
                        blocker,
                        collection_dir=target,
                        mod_name=mod_name,
                        filename=filename,
                        download_status=(
                            "pending_choices" if pending_recorded else ""
                        ),
                    )
                try:
                    final_files = extract_archive(path, target)
                    contributed = [
                        str(ef.relative_to(target)) for ef in final_files
                    ]
                    extracted = True
                    config_record = _fomod_config_record(path)
                except ExtractionError as e:
                    # Extraction failed: never claim completion. The
                    # verified archive stays on disk and the entry stays
                    # (or becomes) pending so `nexus-dl import` can finish
                    # the job without another download.
                    state_note = ""
                    # Set only when a pending entry actually landed in
                    # state, so the result can tell "run import to finish"
                    # apart from "nothing was recorded".
                    pending_recorded = False
                    if existing is None:
                        download_info["manual"] = True
                        download_info["download_status"] = "pending_download"
                        state.add_mod(download_info)
                        state.mods[info.file_id].phase = 999
                        # Committing this pending entry is explicit re-add
                        # intent: clear the page's removal tombstone with the
                        # same commit so `nexus-dl import` can finish the
                        # recovery (import skips tombstoned entries).
                        state.clear_removed(info.mod_id)
                        try:
                            state.save()
                            pending_recorded = True
                        except (OSError, StateError) as save_exc:
                            # Same honest handling as the sibling branches:
                            # return a failed NxmResult instead of raising
                            # raw past the caller. No pending entry was
                            # recorded, so import cannot see this download;
                            # the kept archive is re-click recovery instead.
                            return self._nxm_failure(
                                info,
                                f"Download succeeded but extraction "
                                f"failed: {exception_reason(e)}. The archive is kept at "
                                f"{path}, but the collection state could "
                                f"not be updated ({exception_reason(save_exc)}), so nothing "
                                f"was recorded and 'nexus-dl import' "
                                f"cannot see it. Fix the underlying "
                                f"problem (permissions, disk space), then "
                                f"move the archive aside and click the "
                                f"download link again.",
                                collection_dir=target,
                                mod_name=mod_name,
                                filename=filename,
                            )
                    elif existing.download_status == "downloaded":
                        # Recover a previously-downloaded entry: without
                        # this regression the fresh archive would strand
                        # forever. A failed extraction retains the archive
                        # on disk, and a retained valid archive is exactly
                        # what the dedupe check keys on, so re-clicks would
                        # return already_downloaded with zero network while
                        # import only processes pending entries. Going
                        # back to pending lets either path finish the
                        # extraction. Best-effort save under the already-
                        # held collection lock: a save failure must not
                        # mask the extraction error, and the prior on-disk
                        # state (and in-memory status and tombstones) is
                        # left standing.
                        existing.download_status = "pending_download"
                        # Committing the pending regression is explicit
                        # re-add intent (see the branch above).
                        removed_snapshot = state.removed_mods.get(info.mod_id)
                        state.clear_removed(info.mod_id)
                        try:
                            state.save()
                            pending_recorded = True
                        except (OSError, StateError) as save_exc:
                            existing.download_status = "downloaded"
                            if removed_snapshot is not None:
                                state.removed_mods[info.mod_id] = removed_snapshot
                            state_note = (
                                " The collection state could not be "
                                f"updated ({exception_reason(save_exc)}), so the entry "
                                "remains marked as downloaded; fix the "
                                "underlying problem (permissions, disk "
                                "space), then remove the archive and "
                                "click the download link again."
                            )
                    return self._nxm_failure(
                        info,
                        f"Download succeeded but extraction failed: {exception_reason(e)}. "
                        f"The archive is kept at {path}; run 'nexus-dl "
                        f"import {target}' to retry.{state_note}",
                        collection_dir=target,
                        mod_name=mod_name,
                        filename=filename,
                        download_status=(
                            "pending_download" if pending_recorded else ""
                        ),
                    )

            if existing is not None:
                existing.download_status = "downloaded"
                existing.browser_url = ""
                existing.installed_at = datetime.now(timezone.utc).isoformat()
                existing.filename = filename
                version = selected.get("version")
                if version:
                    existing.version = str(version)
                # Attribution for `disable` and `gc`: the extraction tree
                # is merged, so without this a disabled mod cannot be
                # told apart from its neighbours at deploy time.
                existing.contributed_files = contributed
                if config_record is not None:
                    existing.fomod_config = config_record
            else:
                download_info["manual"] = True
                download_info["download_status"] = "downloaded"
                state.add_mod(download_info)
                # State is keyed by file_id, not mod_id
                state.mods[info.file_id].phase = 999
                state.mods[info.file_id].contributed_files = contributed
                if config_record is not None:
                    state.mods[info.file_id].fomod_config = config_record
            # Completing this download is explicit re-add intent, so the
            # page's removal tombstone (if any) is cleared with the same
            # save that marks the file downloaded.
            state.clear_removed(info.mod_id)
            try:
                state.save()
            except (OSError, StateError) as e:
                # The download and extraction happened but the completion
                # could not be persisted. Never claim completion: the
                # verified archive is kept (NOT deleted) so the work is
                # not lost, and the failure says exactly how to recover.
                extracted_note = (
                    " Extracted files were left in place." if extracted else ""
                )
                return self._nxm_failure(
                    info,
                    f"The download completed but the collection state could "
                    f"not be saved: {exception_reason(e)}. The verified archive is kept at "
                    f"{path}.{extracted_note} Nothing was marked as "
                    f"downloaded. Fix the underlying problem (permissions, "
                    f"disk space) and run 'nexus-dl import {target}'"
                    + (
                        ""
                        if existing is not None
                        else ", or move the archive aside and retry the link"
                    )
                    + ".",
                    collection_dir=target,
                    mod_name=mod_name,
                    filename=filename,
                )

            # Same post-download steps as add/import: tracked-mod sync and
            # load order regeneration. Both run AFTER the successful save,
            # so their failure can no longer crash a completed download:
            # it is reported as a truthful warning on a successful result.
            # No deployment is ever triggered.
            warnings = []
            try:
                self._maybe_sync_tracked(state)
            except Exception as e:
                warnings.append(f"Tracked-mod sync failed: {exception_reason(e)}")
            if not no_load_order:
                progress("loadorder", 0.9, "Regenerating load order...")
                try:
                    self._regen_load_order_from_state(target, state)
                except Exception as e:
                    warnings.append(
                        f"Load order regeneration failed: {exception_reason(e)}. Run "
                        f"'nexus-dl load-order {target}' to retry."
                    )

            progress("done", 1.0, f"Downloaded {mod_name}")
            return NxmResult(
                success=True,
                status="downloaded",
                game_domain=info.game_domain,
                mod_id=info.mod_id,
                file_id=info.file_id,
                mod_name=mod_name,
                filename=filename,
                collection_dir=target,
                extracted=extracted,
                download_status="downloaded",
                warnings=warnings,
            )

    def _nxm_to_output(
        self, info: NxmLinkInfo, output_dir: Path, progress: ProgressCallback
    ) -> NxmResult:
        """Standalone verified-archive drop: no state, no extraction.

        The output directory must already exist; nothing else is created.
        The download goes through the same integrity verification, and a
        payload that is not real archive content is removed instead of
        left behind.
        """
        if not output_dir.is_dir():
            raise NxmRoutingError(f"{output_dir} is not an existing directory.")

        progress("init", 0.05, "Validating API key...")
        account_error = self._nxm_account_error(info)
        if account_error:
            return self._nxm_failure(info, account_error, re_click_required=True)

        progress("fetch", 0.1, "Fetching file metadata...")
        selected, meta_error = self._nxm_file_metadata(info)
        if meta_error:
            return self._nxm_failure(info, meta_error)
        # _nxm_file_metadata returns a dict on success and only pairs a
        # None dict with an error string.
        assert selected is not None
        try:
            filename = validate_download_filename(
                selected.get("file_name") or selected.get("name")
            )
        except DownloadError as e:
            return self._nxm_failure(info, exception_reason(e))

        # Refuse ANY existing destination entry BEFORE downloading
        # (regular file, symlink, or dangling symlink): --output must
        # never overwrite something the user already has. The publish
        # itself is additionally no-clobber, so a file created after this
        # precheck is refused too instead of replaced.
        if os.path.lexists(output_dir / filename):
            return self._nxm_failure(
                info,
                f"{filename} already exists in {output_dir}. Refusing to "
                "overwrite it; remove it or choose another --output "
                "directory.",
                filename=filename,
            )

        try:
            size_bytes = _metadata_size_bytes(selected)
        except ValueError:
            return self._nxm_failure(
                info,
                "File metadata from Nexus Mods is malformed (unexpected "
                "file size); refusing to download without a verifiable "
                "expected size.",
                filename=filename,
            )

        # A standalone drop has no cached manifest to supply an MD5, so a
        # positive byte size is the only possible verification: without
        # it the download would be published unchecked. Refuse instead.
        if size_bytes <= 0:
            return self._nxm_failure(
                info,
                "Nexus Mods provided no file size for this file, so the "
                "download could not be verified and nothing was written. "
                "Retry the link later.",
                filename=filename,
            )

        download_info = {
            "mod_id": info.mod_id,
            "mod_name": self._nxm_mod_name(info),
            "file_id": info.file_id,
            "filename": filename,
            "version": selected.get("version", ""),
            "size_bytes": size_bytes,
        }
        signed = None
        if info.has_credentials:
            # has_credentials is true only when both are present.
            assert info.key is not None and info.expires is not None
            signed = SignedLink(key=info.key, expires=info.expires)

        progress("download", 0.2, f"Downloading {filename}...")
        downloader = Downloader(self.api)
        try:
            path = downloader.download_mod(
                info.game_domain, download_info, output_dir, signed=signed,
                no_clobber=True,
            )
        except DownloadError as e:
            return self._nxm_failure(info, exception_reason(e), filename=filename)

        # Archive-only drop: a payload without real archive magic (error
        # page, loose file, truncated download) is removed, not left behind.
        if sniff_archive_magic(path) is None:
            with contextlib.suppress(OSError):
                path.unlink()
            return self._nxm_failure(
                info,
                f"{filename} is not valid archive content; --output only "
                "saves verified mod archives. The file was removed.",
                filename=filename,
            )

        progress("done", 1.0, f"Saved {filename}")
        return NxmResult(
            success=True,
            status="saved_to_output",
            game_domain=info.game_domain,
            mod_id=info.mod_id,
            file_id=info.file_id,
            mod_name=download_info["mod_name"],
            filename=filename,
            output_path=path,
        )

    # -- nxm helpers (no locking here; callers hold the collection lock) --

    def _nxm_failure(
        self,
        info: NxmLinkInfo,
        error: str,
        collection_dir: Path | None = None,
        mod_name: str = "",
        filename: str = "",
        re_click_required: bool = False,
        download_status: str = "",
    ) -> NxmResult:
        """Build a failed NxmResult from safe components only."""
        return NxmResult(
            success=False,
            status="error",
            game_domain=info.game_domain,
            mod_id=info.mod_id,
            file_id=info.file_id,
            mod_name=mod_name,
            filename=filename,
            collection_dir=collection_dir,
            error=error,
            re_click_required=re_click_required,
            download_status=download_status,
        )

    def _nxm_premium(self, user_info: dict) -> bool:
        """Premium status honoring force_free and premium profile expiry."""
        if self._force_free:
            return False
        if not user_info.get("is_premium", False):
            return False
        expiry = user_info.get("profile_expiry")
        # An expired profile is treated as a free account.
        lapsed = (
            isinstance(expiry, (int, float))
            and not isinstance(expiry, bool)
            and expiry > 0
            and expiry <= time.time()
        )
        return not lapsed

    def _nxm_account_error(
        self, info: NxmLinkInfo, collection_dir: Path | None = None
    ) -> str | None:
        """Validate the account against the link. Returns an error or None.

        One network round trip (validate_key). The URI user_id is checked
        against the API key's account and never forwarded to the API. A
        free account without a website-issued key/expires pair cannot
        redeem a direct link; the fix is a fresh click on the website (or
        the browser-download + import workflow), never a silent fallback.
        """
        user_info = self.api.validate_key()
        is_premium = self._nxm_premium(user_info)

        if info.user_id is not None:
            account_user = _as_int(user_info.get("user_id"))
            if account_user is None:
                account_user = _as_int(user_info.get("id"))
            if account_user is None:
                # Fail closed: without an account identifier in the API
                # response the link's user_id cannot be checked, so the
                # download is refused instead of proceeding unverified.
                return (
                    "Could not verify which Nexus account the configured "
                    "API key belongs to (the API response carried no "
                    "account identifier), so this link's account could not "
                    "be checked and nothing was downloaded. Try again; if "
                    "this persists, verify NEXUS_API_KEY."
                )
            if account_user != info.user_id:
                return (
                    "This download link was issued for a different Nexus "
                    "account than the configured API key. Log in to the "
                    "website with the account that owns this API key (or "
                    "update NEXUS_API_KEY), then click the download "
                    "button again."
                )

        if not info.has_credentials and not is_premium:
            hint = (
                f" and run 'nexus-dl import {collection_dir}'"
                if collection_dir is not None
                else ""
            )
            return (
                "This link carries no download credentials, and free "
                "accounts cannot request direct download links. On the "
                "mod page, click 'Mod manager download' while logged in "
                "to get a personalized link, or download the file in "
                f"your browser{hint}."
            )
        return None

    def _nxm_file_metadata(self, info: NxmLinkInfo) -> tuple[dict | None, str | None]:
        """Fetch the file entry for the link's file_id from the mod's files."""
        files = self.api.get_mod_files(info.game_domain, info.mod_id)
        selected = next(
            (f for f in files if _as_int(f.get("file_id")) == info.file_id), None
        )
        if selected is None:
            return None, (
                f"File {info.file_id} was not found on mod {info.mod_id} "
                "(it may have been removed or archived)."
            )
        return selected, None

    def _nxm_mod_name(self, info: NxmLinkInfo, existing: Any = None) -> str:
        """Best-effort mod display name; never blocks the download."""
        if existing is not None and existing.name:
            return existing.name
        with contextlib.suppress(NexusAPIError):
            nexus_mod = self.api.get_mod_info(info.game_domain, info.mod_id)
            if isinstance(nexus_mod, dict) and nexus_mod.get("name"):
                return str(nexus_mod["name"])
        return f"Mod {info.mod_id}"

    # -- internal helpers --

    def _generate_load_order(
        self,
        collection_data: dict,
        mods_dir: Path,
        state: CollectionState,
        manifest: CollectionManifest | None = None,
    ) -> list[str]:
        """Generate load order from a manifest. Returns list of written filenames.

        `manifest` may be passed in when the caller already fetched it (e.g.
        sync prefetches it for download MD5 verification); otherwise it is
        downloaded here.
        """
        if manifest is None:
            download_link = collection_data.get("download_link")
            if not download_link:
                return []

            try:
                session = self.api.session
                manifest = download_and_parse_manifest(download_link, session=session)
            except ManifestError:
                return []

        mod_requirements: dict[int, list[int]] = {}
        for mod in collection_data.get("mods", []):
            reqs = mod.get("requirements", [])
            if reqs:
                mod_requirements[mod["mod_id"]] = reqs

        game_domain = collection_data["game_domain"]

        generator = LoadOrderGenerator(
            manifest=manifest,
            mods=collection_data["mods"],
            mod_requirements=mod_requirements,
            game_domain=game_domain,
        )

        written_files: list[str] = []
        try:
            written = generator.generate(mods_dir)
            written_files = [p.name for p in written]
        except Exception:
            return []

        if is_bethesda_game(game_domain) and is_loot_available():
            loot_sorted = sort_plugins_with_loot(game_domain, mods_dir, None)
            merged = merge_plugin_orders(manifest.plugins, loot_sorted)
            used_loot = loot_sorted is not None
            plugins_path = mods_dir / "plugins.txt"
            write_loot_plugins_txt(merged, plugins_path, game_domain, used_loot)
            written_files.append("plugins.txt")
        elif is_bethesda_game(game_domain):
            pass  # LOOT not available, collection metadata used

        # Cache manifest
        state.manifest_data = manifest.to_dict()
        state.mod_rules = manifest.mod_rules
        state.save()

        return written_files

    def _regen_load_order_from_state(self, mods_dir: Path, state: CollectionState) -> list[str]:
        """Regenerate load order from cached manifest + current state mods.

        Raises LoadOrderRegenError when ANY part of the regeneration
        fails (manifest parsing, generation, or writing the output
        files): the caller's state change is already persisted at that
        point, and callers surface the failure as a warning or error on
        their result (or, for the explicit load-order command, as a
        clean StateError). Failures are never silently swallowed here
        and never escape as raw exceptions.
        """
        try:
            return self._regen_load_order_unwrapped(mods_dir, state)
        except LoadOrderRegenError:
            raise
        except Exception as e:
            raise LoadOrderRegenError(
                f"Load order regeneration failed: {exception_reason(e)}"
            ) from e

    def _regen_load_order_unwrapped(self, mods_dir: Path, state: CollectionState) -> list[str]:
        """Body of _regen_load_order_from_state; its exceptions are wrapped."""
        if not state.manifest_data:
            return []

        manifest = CollectionManifest.from_dict(state.manifest_data)

        # Inject phase 999 for manual mods (keyed by mod_id in manifest)
        for ms in state.mods.values():
            if ms.manual:
                manifest.mod_phases[ms.mod_id] = 999

        mod_requirements: dict[int, list[int]] = {}
        for mod_state in state.mods.values():
            if mod_state.requirements:
                mod_requirements[mod_state.mod_id] = mod_state.requirements

        mods_list = [
            {
                "mod_id": ms.mod_id,
                "mod_name": ms.name,
                "file_id": file_id,
                "version": ms.version,
                "filename": ms.filename,
                "optional": ms.optional,
            }
            for file_id, ms in state.mods.items()
        ]

        generator = LoadOrderGenerator(
            manifest=manifest,
            mods=mods_list,
            mod_requirements=mod_requirements,
            game_domain=state.game_domain,
        )

        written_files: list[str] = []
        try:
            written = generator.generate(mods_dir)
            written_files = [p.name for p in written]
        except Exception as e:
            raise LoadOrderRegenError(
                f"Load order generation failed: {exception_reason(e)}"
            ) from e

        if is_bethesda_game(state.game_domain) and is_loot_available():
            loot_sorted = sort_plugins_with_loot(state.game_domain, mods_dir, None)
            merged = merge_plugin_orders(manifest.plugins, loot_sorted)
            used_loot = loot_sorted is not None
            plugins_path = mods_dir / "plugins.txt"
            write_loot_plugins_txt(merged, plugins_path, state.game_domain, used_loot)
            written_files.append("plugins.txt")

        return written_files

    def _sync_tracked_mods(self, state: CollectionState) -> tuple[int, int]:
        """Sync local mod list with Nexus tracked mods."""
        remote = self.api.get_tracked_mods()
        remote_ids = {m["mod_id"] for m in remote if m["domain_name"] == state.game_domain}
        # State is keyed by file_id; Nexus tracks mod_ids. A multi-file mod
        # contributes one mod_id no matter how many files it has.
        local_ids = {ms.mod_id for ms in state.mods.values() if ms.mod_id > 0}

        to_track = local_ids - remote_ids
        to_untrack = remote_ids - local_ids

        for mid in to_track:
            self.api.track_mod(state.game_domain, mid)
        for mid in to_untrack:
            self.api.untrack_mod(state.game_domain, mid)

        return len(to_track), len(to_untrack)

    def _maybe_sync_tracked(self, state: CollectionState) -> tuple[int, int]:
        """Run track sync if enabled, swallowing errors."""
        if not state.track_sync_enabled:
            return 0, 0
        try:
            return self._sync_tracked_mods(state)
        except NexusAPIError:
            return 0, 0


def _select_mod_file(
    files: list[dict], file_id_override: int | None = None
) -> dict | None:
    """Select a file from the mod's file list.

    Priority:
    1. file_id override (exact match)
    2. MAIN category, highest file_id (most recent)
    3. Any non-archived file, highest file_id
    4. None
    """
    if not files:
        return None

    if file_id_override is not None:
        for f in files:
            if f.get("file_id") == file_id_override:
                return f
        return None

    main_files = [f for f in files if f.get("category_id") == 1]
    if main_files:
        return max(main_files, key=lambda f: f.get("file_id", 0))

    non_archived = [f for f in files if f.get("category_id") != 6]
    if non_archived:
        return max(non_archived, key=lambda f: f.get("file_id", 0))

    return None


# ---------------------------------------------------------------------------
# health report (read-only tool-wide diagnostics; see collect_health)
# ---------------------------------------------------------------------------


@dataclass
class HealthCheck:
    """One line of the /status health report (see collect_health)."""

    label: str
    # "OK", "WARN", or "ERROR" (ERROR = the check itself failed).
    status: str
    detail: str
    remedy: str | None = None


def _probe_api_key_source() -> tuple[str | None, str | None]:
    """(source, error) for the effective API key; the value is discarded.

    Wraps keystore.resolve_api_key so the health report can say where a
    key comes from without any secret reaching it by construction: the
    resolved value is dropped here and never leaves this function.
    """
    try:
        _value, source = resolve_api_key(None)
    except KeyStoreError as e:
        return None, exception_reason(e)
    return source, None


def _probe_handler_status() -> dict:
    """Read-only desktop-handler diagnostics (handler.DesktopInstaller)."""
    return DesktopInstaller().status()


def _flock_held(lock_path: Path) -> bool | None:
    """Is a kernel flock currently held on lock_path? None when unknowable.

    Reads /proc/locks (Linux) and matches FLOCK entries by device and
    inode, which is strictly read-only: probing with a real flock would
    momentarily conflict with a concurrent writer and could make it see
    a spurious busy error. flock locks are released by the kernel when
    the holding process exits, so an entry here always means a live
    holder and no lock file can ever be "stale".
    """
    try:
        st = os.stat(lock_path)
        lines = (Path("/proc/locks")).read_text().splitlines()
    except OSError:
        return None
    major, minor = os.major(st.st_dev), os.minor(st.st_dev)
    for line in lines:
        parts = line.split()
        if len(parts) < 6 or parts[1] != "FLOCK":
            continue
        try:
            dev_major, dev_minor, inode = parts[5].split(":")
            found = (
                int(dev_major, 16),
                int(dev_minor, 16),
                int(inode, 10),
            )
        except ValueError:
            continue
        if found == (major, minor, st.st_ino):
            return True
    return False


def _lock_state(dir_path: Path) -> str:
    """One collection's state-lock state: "held", "free", or "unknown"."""
    lock_path = Path(dir_path) / LOCK_FILENAME
    try:
        if not lock_path.is_file():
            return "free"
    except OSError:
        return "unknown"
    held = _flock_held(lock_path)
    if held is None:
        return "unknown"
    return "held" if held else "free"


@dataclass
class HealthProbes:
    """Injectable host-probe seams for collect_health.

    Defaults call the real helpers; tests replace the fields so no host
    subprocess (xdg-mime, gsettings, notify-send) ever runs in a test.
    Every callable may raise: collect_health renders an unexpected
    failure as an ERROR line and keeps going. None of them ever handle
    secret values except api_key_source's default, which discards the
    key inside _probe_api_key_source.
    """

    # (source, error) for the effective API key; never the value.
    api_key_source: Callable[
        [], tuple[str | None, str | None]
    ] = _probe_api_key_source
    # DesktopInstaller().status(): entry installed, xdg-mime default.
    handler_status: Callable[[], dict] = _probe_handler_status
    # True/False, or None when the desktop/switch is unknown.
    notifications_enabled: Callable[[], "bool | None"] = (
        lambda: notifications_enabled_in_desktop()
    )
    # (schema, key, fix command) for this desktop, or None when unknown.
    notification_setting: Callable[[], "tuple[str, str, str] | None"] = (
        lambda: _desktop_notification_setting()
    )
    # xdg-mime / notify-send findings (handler.capability_warnings).
    capability_warnings: Callable[[], list[str]] = (
        lambda: capability_warnings()
    )
    # Per-collection lock state: "free", "held", or "unknown".
    lock_state: Callable[[Path], str] = _lock_state


def default_health_probes() -> HealthProbes:
    """Real host probes for collect_health; tests substitute fakes."""
    return HealthProbes()


def _health_api_key(
    probes: HealthProbes, api_key_present: bool
) -> list[HealthCheck]:
    """API-key availability and source; the value is never inspected."""
    source, error = probes.api_key_source()
    if error:
        return [
            HealthCheck(
                "API key",
                "WARN",
                "the stored key file could not be read",
                f"{error}",
            )
        ]
    if source == SOURCE_STORED:
        return [HealthCheck("API key", "OK", "stored (keystore)")]
    if source == SOURCE_ENV:
        return [
            HealthCheck(
                "API key", "OK", f"set ({ENV_VAR_NAME} environment variable)"
            )
        ]
    if source == SOURCE_FLAG or api_key_present:
        return [HealthCheck("API key", "OK", "passed to this session")]
    return [
        HealthCheck(
            "API key",
            "WARN",
            "not set",
            f"export {ENV_VAR_NAME} or run 'nexus-dl handler set-api-key'",
        )
    ]


def _health_handler(probes: HealthProbes) -> list[HealthCheck]:
    """nxm handler install state and the xdg-mime default (read-only)."""
    info = probes.handler_status()
    if not info.get("entry_installed"):
        return [
            HealthCheck(
                "nxm handler",
                "WARN",
                "not installed",
                "Install with 'nexus-dl handler install' to handle "
                "nxm:// clicks.",
            )
        ]
    if not info.get("executable_exists"):
        exe = info.get("executable")
        where = f" at {exe}" if exe else ""
        return [
            HealthCheck(
                "nxm handler",
                "WARN",
                f"installed but its executable is missing{where}",
                "Re-run 'nexus-dl handler install' to point the desktop "
                "entry at a working nexus-dl.",
            )
        ]
    if info.get("is_default"):
        return [
            HealthCheck(
                "nxm handler",
                "OK",
                "installed and the default nxm:// handler",
            )
        ]
    current = info.get("current_default")
    if current:
        return [
            HealthCheck(
                "nxm handler",
                "WARN",
                f"installed but not the default (current default: {current})",
                "Re-run 'nexus-dl handler install' to take back the "
                "nxm:// default.",
            )
        ]
    if info.get("xdg_mime_available"):
        return [
            HealthCheck(
                "nxm handler",
                "WARN",
                "installed but no default nxm:// handler is registered",
                "Re-run 'nexus-dl handler install'.",
            )
        ]
    return [
        HealthCheck(
            "nxm handler",
            "OK",
            "installed (default handler unknown: xdg-mime not available)",
        )
    ]


def _health_notifications(probes: HealthProbes) -> list[HealthCheck]:
    """Desktop notification-display switch; silent when unknowable.

    Reuses handler's batch-E probe helpers: an unknown desktop, missing
    gsettings, or an unreadable switch is not a finding at all (no line
    is rendered), exactly like the install-time behavior. A switch
    known to be off gets a WARN with the exact fix command.
    """
    spec = probes.notification_setting()
    if spec is None:
        return []
    enabled = probes.notifications_enabled()
    if enabled is None:
        return []
    schema, key, fix = spec
    if enabled:
        return [
            HealthCheck(
                "Notifications", "OK", "enabled in this desktop's settings"
            )
        ]
    return [
        HealthCheck(
            "Notifications",
            "WARN",
            f"disabled in this desktop's settings ({schema} {key} is false)",
            fix,
        )
    ]


def _health_capabilities(probes: HealthProbes) -> list[HealthCheck]:
    """xdg-mime / notify-send findings from handler.capability_warnings."""
    spec = probes.notification_setting()
    fix = spec[2] if spec is not None else None
    warnings = list(probes.capability_warnings())
    if fix:
        # The notification-switch warning is the Notifications check's
        # finding (same fix string from the same settings table); drop
        # it here so one problem renders exactly once.
        warnings = [w for w in warnings if fix not in w]
    if not warnings:
        return [
            HealthCheck(
                "Desktop tools",
                "OK",
                "xdg-mime and notify-send are present and working",
            )
        ]
    return [HealthCheck("Desktop tools", "WARN", w) for w in warnings]


def _health_state_files(scan: CollectionScan) -> list[HealthCheck]:
    """Broken state files the collection listings skip silently."""
    if not scan.skipped:
        count = len(scan.collections)
        detail = (
            f"{count} collection state file(s) readable"
            if count
            else "no collection state files found"
        )
        return [HealthCheck("State files", "OK", detail)]
    names = ", ".join(f"{s.name} [{s.reason}]" for s in scan.skipped)
    return [
        HealthCheck(
            "State files",
            "WARN",
            f"{len(scan.skipped)} unreadable state file(s) that the "
            f"listings skip: {names}",
            "Fix or remove those .nexus-state.json files (or re-sync "
            "the collections).",
        )
    ]


def _health_locks(
    probes: HealthProbes, scan: CollectionScan
) -> list[HealthCheck]:
    """Report in-use state locks; stale locks cannot exist here.

    The writer lock is a kernel flock, released automatically when the
    holding process exits, and the lock file is never removed and
    carries no state: a leftover .nexus-state.lock is normal and is
    never reported. The read-only probe only separates "in use by a
    running operation" from "free"; a lock in use is not stale.
    """
    dirs = [c.path for c in scan.collections] + [
        s.path for s in scan.skipped
    ]
    if not dirs:
        return [HealthCheck("State locks", "OK", "no collections to check")]
    states = [probes.lock_state(d) for d in dirs]
    if all(s == "unknown" for s in states):
        return [
            HealthCheck(
                "State locks",
                "OK",
                "skipped: lock state could not be read on this platform "
                "(flock locks are released on exit in any case, so stale "
                "lock files cannot exist)",
            )
        ]
    held = sum(1 for s in states if s == "held")
    unknown = sum(1 for s in states if s == "unknown")
    if held:
        detail = (
            f"{held} of {len(dirs)} collection state lock(s) in use by "
            "running operations (not stale)"
        )
    else:
        detail = "no collection state lock is in use"
    detail += (
        "; flock locks are released on exit, so stale lock files "
        "cannot exist"
    )
    if unknown:
        detail += f" ({unknown} not checked)"
    return [HealthCheck("State locks", "OK", detail)]


def _health_user_config() -> list[HealthCheck]:
    """Has the tool ever been set up?

    The front door's signal: a missing or unusable user config is a
    first run, and `health` is the one place that looks at the whole
    installation, so it is where a fresh install gets noticed.
    """
    from .config import UserConfig

    cfg = UserConfig.load()
    if cfg.is_configured():
        return [HealthCheck("User config", "OK", f"collections root: {cfg.collections_root}")]
    return [
        HealthCheck(
            "User config",
            "WARN",
            "not configured (first run)",
            remedy=(
                "Set a collections root: the directory collection folders "
                "live under. Until then every command asks for its paths."
            ),
        )
    ]


def collect_health(
    root: Path,
    *,
    scan: CollectionScan | None = None,
    api_key_present: bool = False,
    probes: HealthProbes | None = None,
) -> list[HealthCheck]:
    """Build the tool-wide health report. Read-only, never networked.

    One entry per check (the desktop-tools check can produce one entry
    per finding). Every check is individually guarded: an unexpected
    failure renders as an ERROR line naming the exception type, and the
    remaining checks still run. No output carries a secret: the API-key
    check reports the source label only, and its probe discards the key
    value by construction.
    """
    probes = probes or default_health_probes()
    if scan is None:
        scan = scan_collections(root)

    def guarded(label, fn) -> list[HealthCheck]:
        try:
            return fn()
        except Exception as e:
            reason = (exception_reason(e) or "").strip().splitlines()
            detail = f"check failed ({type(e).__name__})"
            if reason:
                detail = f"check failed ({type(e).__name__}: {reason[0][:60]})"
            return [HealthCheck(label, "ERROR", detail)]

    checks: list[HealthCheck] = []
    for label, fn in (
        ("User config", _health_user_config),
        ("API key", lambda: _health_api_key(probes, api_key_present)),
        ("nxm handler", lambda: _health_handler(probes)),
        ("Notifications", lambda: _health_notifications(probes)),
        ("Desktop tools", lambda: _health_capabilities(probes)),
        ("State files", lambda: _health_state_files(scan)),
        ("State locks", lambda: _health_locks(probes, scan)),
    ):
        checks.extend(guarded(label, fn))
    return checks
