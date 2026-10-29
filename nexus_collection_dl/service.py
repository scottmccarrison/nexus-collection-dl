"""Service layer - business logic extracted from CLI for programmatic use."""

import os
import re
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .api import NexusAPI, NexusAPIError, NexusPremiumRequired
from .collection import parse_collection_url, parse_mod_url, CollectionParseError, ModParseError
from .deploy import (
    BackupStore,
    DeployError,
    DeployJournal,
    UndeployError,
    classify_files,
    deploy as deploy_files,
    get_game_ini_path,
    get_plugins_txt_dest,
    recover_ownership,
    undeploy_records,
    write_game_ini,
    write_plugins_txt,
)
from .downloader import DownloadError, Downloader
from .extractor import ExtractionError, extract_archive, is_archive, sniff_archive_magic
from .loadorder import LoadOrderGenerator
from .loot_sort import (
    is_bethesda_game,
    is_loot_available,
    merge_plugin_orders,
    sort_plugins_with_loot,
    write_loot_plugins_txt,
)
from .manifest import CollectionManifest, ManifestError, download_and_parse_manifest
from .state import CollectionState, CollectionLock, StateError, STATE_FILENAME
from .steam import STEAM_APP_IDS, find_game_dir, find_proton_prefix


# progress callback: (event_type, percentage 0-1, message)
ProgressCallback = Callable[[str, float, str], None]


def _sanitize_dirname(name: str) -> str:
    """Turn a collection name into a safe directory name."""
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = name.strip(". ")
    return name or "collection"


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
            if not md5:
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
        key = (_as_int(mod.get("mod_id")), _as_int(mod.get("file_id")))
        md5 = by_identity.get(key)
        if md5:
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

        # Try to fetch latest from API
        try:
            collection_info = parse_collection_url(state.collection_url)
            collection_data = self.api.get_collection_mods(
                collection_info.game_domain, collection_info.slug
            )
            latest_revision = collection_data["revision"]

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
        )

    def sync(
        self,
        collection_url: str,
        mods_dir: Path,
        skip_optional: bool = False,
        no_load_order: bool = False,
        no_extract: bool = False,
        on_progress: ProgressCallback | None = None,
    ) -> SyncResult:
        """Download an entire collection."""
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

        # Resolve mods_dir to a per-collection subdirectory
        dir_name = _sanitize_dirname(collection_data["name"])
        mods_dir = mods_dir / dir_name
        mods_dir.mkdir(parents=True, exist_ok=True)

        mods = collection_data["mods"]
        dupes = collection_data.get("duplicates_removed", 0)
        if dupes:
            progress("fetch", 0.06, f"Deduplicated {dupes} duplicate mod entries")
        if skip_optional:
            mods = [m for m in mods if not m.get("optional", False)]

        # Initialize state
        state = CollectionState(mods_dir)
        if state.exists():
            state.load()
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
            return SyncResult(0, 0, [], [], collection_dir=mods_dir)

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
            load_order_files: list[str] = []
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
            )

        # Extract archives
        progress("extract", 0.75, "Extracting archives...")
        extracted = 0
        file_owners: dict[str, str] = {}
        conflicts: list[FileConflict] = []
        for i, (mod_info, file_path) in enumerate(results):
            try:
                if not no_extract and is_archive(file_path):
                    final_files = extract_archive(file_path, mods_dir)
                    for ef in final_files:
                        rel = str(ef.relative_to(mods_dir))
                        prev_owner = file_owners.get(rel)
                        if prev_owner:
                            conflicts.append(FileConflict(rel, mod_info["mod_name"], prev_owner))
                        file_owners[rel] = mod_info["mod_name"]
                    file_path.unlink()
                    extracted += 1
                else:
                    rel = str(file_path.relative_to(mods_dir))
                    prev_owner = file_owners.get(rel)
                    if prev_owner:
                        conflicts.append(FileConflict(rel, mod_info["mod_name"], prev_owner))
                    file_owners[rel] = mod_info["mod_name"]
                state.add_mod(mod_info)
                state.save()
                pct = 0.75 + 0.15 * ((i + 1) / len(results))
                progress("extract", pct, f"Extracted {mod_info['mod_name']}")
            except ExtractionError as e:
                errors.append(f"Extraction error for {mod_info['mod_name']}: {e}")

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
        )

    def update(
        self,
        mods_dir: Path,
        skip_optional: bool = False,
        no_load_order: bool = False,
        no_extract: bool = False,
        on_progress: ProgressCallback | None = None,
    ) -> UpdateResult:
        """Check for and download updates."""
        progress = on_progress or _noop_progress
        errors: list[str] = []
        load_order_files: list[str] = []

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
        mods = collection_data["mods"]
        dupes = collection_data.get("duplicates_removed", 0)
        if dupes:
            progress("fetch", 0.06, f"Deduplicated {dupes} duplicate mod entries")
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
                        progress("download", pct, f"Downloading...")

                progress("download", 0.1, f"Downloading {len(mods_to_download)} mods...")
                _attach_expected_md5(state, mods_to_download)
                results = downloader.download_mods(
                    game_domain=collection_data["game_domain"],
                    mods=mods_to_download,
                    target_dir=mods_dir,
                    on_progress=on_download_progress,
                )

                progress("extract", 0.75, "Extracting archives...")
                for mod_info, file_path in results:
                    try:
                        if not no_extract and is_archive(file_path):
                            extract_archive(file_path, mods_dir)
                            file_path.unlink()
                        state.add_mod(mod_info)
                        downloaded += 1
                    except ExtractionError as e:
                        errors.append(f"Extraction error for {mod_info['mod_name']}: {e}")
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
        )

    def add_mod(
        self,
        mod_url: str,
        mods_dir: Path,
        file_id: int | None = None,
        no_load_order: bool = False,
        no_extract: bool = False,
        on_progress: ProgressCallback | None = None,
    ) -> AddResult:
        """Add a single mod by Nexus URL."""
        progress = on_progress or _noop_progress

        mod_info = parse_mod_url(mod_url)

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
        )

        if not results:
            return AddResult(
                mod_name=mod_name, mod_id=mod_info.mod_id, file_name=selected_name,
                success=False, error="Download failed"
            )

        progress("extract", 0.85, "Extracting...")
        for _mod, file_path in results:
            try:
                if not no_extract and is_archive(file_path):
                    extract_archive(file_path, mods_dir)
                    file_path.unlink()
            except ExtractionError as e:
                return AddResult(
                    mod_name=mod_name, mod_id=mod_info.mod_id, file_name=selected_name,
                    success=False, error=f"Extraction error: {e}"
                )

        # Register in state as manual
        download_info["manual"] = True
        state.add_mod(download_info)
        # State is keyed by file_id, not mod_id
        state.mods[selected_file_id].phase = 999
        state.save()

        self._maybe_sync_tracked(state)

        if not no_load_order:
            progress("loadorder", 0.95, "Regenerating load order...")
            self._regen_load_order_from_state(mods_dir, state)

        progress("done", 1.0, f"Added {mod_name}")
        return AddResult(
            mod_name=mod_name, mod_id=mod_info.mod_id, file_name=selected_name,
            success=True,
        )

    def add_local(self, name: str, mods_dir: Path, no_load_order: bool = False) -> int:
        """Register a pre-existing local mod. Returns synthetic mod_id."""
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
            self._regen_load_order_from_state(mods_dir, state)

        return synthetic_id

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
        self, state: "CollectionState", game_dir: Path, mods_dir: Path, method: str
    ) -> list["DeployWarning"]:
        """Run pre-deployment validation checks."""
        warnings: list[DeployWarning] = []

        # 1. Proton prefix missing
        if find_proton_prefix(state.game_domain) is None and not state.proton_prefix:
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

        # 6. Game not in auto-detect
        if state.game_domain not in STEAM_APP_IDS and not state.game_dir:
            warnings.append(DeployWarning(
                "unknown_game",
                f"Game '{state.game_domain}' not in auto-detect list - game dir must be set manually",
                "error",
            ))

        # 7. Stale deployment
        if state.deployed_at and state.game_dir:
            game_path = Path(state.game_dir)
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
    ) -> DeployResult:
        """Deploy mods from staging to game directory.

        The deployment is tracked and reversible: originals at destination
        paths are backed up before being replaced, every mutation is
        journaled, and files changed outside nexus-dl are refused instead of
        overwritten. The previous deployment is removed safely first; if any
        old entry cannot be safely removed, the redeploy stops and the
        remaining entries stay in state for retry.
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

            # Pre-deploy validation
            method = "copy" if use_copy else "symlink"
            check_warnings = self.pre_deploy_checks(state, game_dir, mods_dir, method)
            deploy_warnings = [w.message for w in check_warnings]
            deploy_warnings.extend(reports)

            # Block on errors
            errors_found = [w for w in check_warnings if w.severity == "error"]
            if errors_found:
                return DeployResult(
                    0, [], [w.message for w in errors_found], str(game_dir), False,
                    warnings=deploy_warnings,
                )

            # Resolve Proton prefix
            if prefix is None and state.proton_prefix:
                prefix = Path(state.proton_prefix)
            if prefix is None:
                prefix = find_proton_prefix(game_domain)

            # Preflight: classify BEFORE any destructive removal of the old
            # deployment, so a staging directory with nothing deployable
            # never tears down a working deployment.
            progress("classify", 0.15, "Classifying files...")
            manifest_choices = {}
            if state.manifest_data:
                manifest = CollectionManifest.from_dict(state.manifest_data)
                manifest_choices = manifest.mod_choices
            plan = classify_files(mods_dir, game_domain, mod_choices=manifest_choices)

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
                        f"Redeploy stopped: {len(kept)} previously deployed "
                        f"file(s) could not be safely removed. Resolve the "
                        f"reported issues and run deploy (or undeploy) again."
                    ] + problems
                    return DeployResult(
                        0, [], errors, str(game_dir), False, warnings=deploy_warnings
                    )
                state.deployed_files = []
                state.deployed_at = None

            # Deploy
            progress("deploy", 0.3, f"Deploying {plan.total_files} files via {method}...")
            result = deploy_files(
                plan, game_dir, method=method, dry_run=False,
                backups=backups, journal=journal, ownership=ownership,
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
                plugins_dest = get_plugins_txt_dest(prefix, game_domain)
                if plugins_dest and plugins_src.exists():
                    try:
                        write_plugins_txt(
                            plugins_src, plugins_dest,
                            root=prefix, backups=backups, journal=journal,
                            ownership=ownership,
                        )
                    except (DeployError, OSError) as e:
                        result.errors.append(f"plugins.txt: {e}")
                ini_path = get_game_ini_path(prefix, game_domain)
                if ini_path:
                    try:
                        write_game_ini(
                            ini_path, game_domain,
                            root=prefix, backups=backups, journal=journal,
                            ownership=ownership,
                        )
                    except (DeployError, OSError) as e:
                        result.errors.append(f"game INI: {e}")

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
        state = CollectionState(mods_dir)
        state.load()

        state.track_sync_enabled = True
        state.save()

        return self._sync_tracked_mods(state)

    def track_sync_disable(self, mods_dir: Path) -> None:
        """Disable track sync."""
        state = CollectionState(mods_dir)
        state.load()
        state.track_sync_enabled = False
        state.save()

    def track_sync_push(self, mods_dir: Path) -> tuple[int, int]:
        """One-shot sync regardless of enable/disable state."""
        state = CollectionState(mods_dir)
        state.load()
        return self._sync_tracked_mods(state)

    def import_downloads(
        self,
        mods_dir: Path,
        no_load_order: bool = False,
        no_extract: bool = False,
        on_progress: ProgressCallback | None = None,
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

        state = CollectionState(mods_dir)
        state.load()

        # Sorted by file_id so contested files resolve deterministically
        pending = sorted(state.get_pending_mods(), key=lambda ms: ms.file_id)
        if not pending:
            return ImportResult(matched=0, extracted=0, still_pending=0, errors=[], load_order_files=[])

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
            try:
                if not no_extract and is_arch:
                    extract_archive(match, mods_dir)
                    extracted += 1
            except ExtractionError as e:
                # A failed extraction must never mark the mod downloaded.
                # The archive stays on disk and the item stays pending;
                # partially moved content cannot be rolled back.
                errors.append(f"Extraction error for {mod_state.name}: {e}")
                continue

            mod_state.download_status = "downloaded"
            mod_state.browser_url = ""
            mod_state.installed_at = datetime.now(timezone.utc).isoformat()
            # Persist before any destructive cleanup so a mid-run failure
            # cannot lose progress for already-imported items.
            state.save()

            if not no_extract and is_arch:
                try:
                    match.unlink()
                except OSError as e:
                    # The import itself succeeded and is persisted; a leftover
                    # archive is reported but does not roll back the success.
                    errors.append(f"Could not remove {match.name} after import: {e}")

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
                errors.append(f"Load order regeneration failed: {e}")

        # Sync tracked mods (same as premium sync does after downloading)
        if matched > 0:
            try:
                self._maybe_sync_tracked(state)
            except Exception as e:
                errors.append(f"Track sync failed: {e}")

        still_pending = len(state.get_pending_mods())
        progress("done", 1.0, f"Imported {matched} mods ({still_pending} still pending)")

        return ImportResult(
            matched=matched,
            extracted=extracted,
            still_pending=still_pending,
            errors=errors,
            load_order_files=load_order_files,
        )

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
        """Regenerate load order from cached manifest + current state mods."""
        if not state.manifest_data:
            return []

        manifest = CollectionManifest.from_dict(state.manifest_data)

        # Inject phase 999 for manual mods (keyed by mod_id in manifest)
        for file_id, ms in state.mods.items():
            if ms.manual:
                manifest.mod_phases[ms.mod_id] = 999

        mod_requirements: dict[int, list[int]] = {}
        for file_id, mod_state in state.mods.items():
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
        except Exception:
            return []

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
