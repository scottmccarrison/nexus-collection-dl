"""Service layer - business logic extracted from CLI for programmatic use."""

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .api import NexusAPI, NexusAPIError, NexusPremiumRequired
from .collection import parse_collection_url, parse_mod_url, CollectionParseError, ModParseError
from .deploy import (
    classify_files,
    deploy as deploy_files,
    get_game_ini_path,
    get_plugins_txt_dest,
    undeploy as undeploy_files,
    write_game_ini,
    write_plugins_txt,
)
from .downloader import DownloadError, Downloader
from .extractor import ExtractionError, extract_archive, is_archive
from .loadorder import LoadOrderGenerator
from .loot_sort import (
    is_bethesda_game,
    is_loot_available,
    merge_plugin_orders,
    sort_plugins_with_loot,
    write_loot_plugins_txt,
)
from .manifest import CollectionManifest, ManifestError, download_and_parse_manifest
from .state import CollectionState, StateError
from .steam import STEAM_APP_IDS, find_game_dir, find_proton_prefix


# progress callback: (event_type, percentage 0-1, message)
ProgressCallback = Callable[[str, float, str], None]


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
class SyncResult:
    mods_downloaded: int
    mods_extracted: int
    errors: list[str]
    load_order_files: list[str]
    tracked: int = 0
    untracked: int = 0
    pending_downloads: list[PendingDownload] = field(default_factory=list)


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

            for mod_id in to_remove:
                installed = state.get_mod(mod_id)
                if installed:
                    mod_statuses.append(ModStatus(
                        mod_id=mod_id,
                        name=installed.name,
                        version=installed.version,
                        file_id=installed.file_id,
                        optional=installed.optional,
                        manual=installed.manual,
                        phase=installed.phase,
                        status="removed",
                    ))

        except (NexusAPIError, CollectionParseError):
            # Offline/no API - just show installed mods
            for mod_id, ms in state.mods.items():
                status = "pending_download" if ms.download_status == "pending_download" else "installed"
                mod_statuses.append(ModStatus(
                    mod_id=mod_id,
                    name=ms.name,
                    version=ms.version,
                    file_id=ms.file_id,
                    optional=ms.optional,
                    manual=ms.manual,
                    phase=ms.phase,
                    status=status,
                ))

        # Add manual mods that aren't already in the list
        listed_ids = {m.mod_id for m in mod_statuses}
        for mod_id, ms in state.mods.items():
            if mod_id not in listed_ids and ms.manual:
                mod_statuses.append(ModStatus(
                    mod_id=mod_id,
                    name=ms.name,
                    version=ms.version,
                    file_id=ms.file_id,
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

        mods = collection_data["mods"]
        dupes = collection_data.get("duplicates_removed", 0)
        if dupes:
            progress("fetch", 0.06, f"Deduplicated {dupes} duplicate mod entries")
        if skip_optional:
            mods = [m for m in mods if not m.get("optional", False)]

        if not mods:
            return SyncResult(0, 0, [], [])

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

        if not is_premium:
            # Free user: register mods as pending_download
            progress("pending", 0.5, "Building pending download list...")
            pending_downloads: list[PendingDownload] = []
            game_domain = collection_data["game_domain"]
            for mod in mods:
                mod_id = mod["mod_id"]
                # Don't regress already-downloaded mods
                existing = state.get_mod(mod_id)
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
            )

        # Premium user: download directly
        total_mods = len(mods)
        downloader = Downloader(self.api)

        def on_download_progress(bytes_dl: int, total_bytes: int) -> None:
            if total_bytes > 0:
                pct = 0.1 + 0.6 * (bytes_dl / total_bytes)
                progress("download", pct, f"Downloading... ({bytes_dl}/{total_bytes} bytes)")

        progress("download", 0.1, f"Downloading {total_mods} mods...")
        results = downloader.download_mods(
            game_domain=collection_data["game_domain"],
            mods=mods,
            target_dir=mods_dir,
            on_progress=on_download_progress,
        )

        # Extract archives
        progress("extract", 0.75, "Extracting archives...")
        extracted = 0
        for i, (mod_info, file_path) in enumerate(results):
            try:
                if not no_extract and is_archive(file_path):
                    extract_archive(file_path, mods_dir)
                    file_path.unlink()
                    extracted += 1
                state.add_mod(mod_info)
                pct = 0.75 + 0.15 * ((i + 1) / len(results))
                progress("extract", pct, f"Extracted {mod_info['mod_name']}")
            except ExtractionError as e:
                errors.append(f"Extraction error for {mod_info['mod_name']}: {e}")

        state.save()

        # Generate load order
        if not no_load_order:
            progress("loadorder", 0.9, "Generating load order...")
            lo_files = self._generate_load_order(collection_data, mods_dir, state)
            load_order_files = lo_files

        # Track sync
        tracked, untracked = self._maybe_sync_tracked(state)
        progress("done", 1.0, f"Synced {len(results)} mods successfully")

        return SyncResult(
            mods_downloaded=len(results),
            mods_extracted=extracted,
            errors=errors,
            load_order_files=load_order_files,
            tracked=tracked,
            untracked=untracked,
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
                    existing = state.get_mod(mod_id)
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
            "size_bytes": int(selected.get("size_in_bytes") or selected.get("size", 0) or 0),
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
            state.mods[mod_info.mod_id].phase = 999
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
        state.mods[mod_info.mod_id].phase = 999
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

        existing_ids = set(state.mods.keys())
        synthetic_id = -1
        while synthetic_id in existing_ids:
            synthetic_id -= 1

        mod_info = {
            "mod_id": synthetic_id,
            "mod_name": name,
            "file_id": 0,
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
                size_bytes=(f.get("size_in_bytes") or f.get("size", 0)),
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
        # Filter out state file
        mod_files = [f for f in mod_files if f.name != ".nexus-state.json"]
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
        """Deploy mods from staging to game directory."""
        progress = on_progress or _noop_progress

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

        # Pre-deploy validation
        method = "copy" if use_copy else "symlink"
        check_warnings = self.pre_deploy_checks(state, game_dir, mods_dir, method)
        deploy_warnings = [w.message for w in check_warnings]

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

        # Remove previous deployment
        if state.deployed_files:
            progress("undeploy", 0.1, "Removing previous deployment...")
            undeploy_files(state.deployed_files)
            state.deployed_files = []
            state.deployed_at = None

        # Classify files
        progress("classify", 0.2, "Classifying files...")
        # Load manifest choices for FOMOD filtering
        manifest_choices = {}
        if state.manifest_data:
            manifest = CollectionManifest.from_dict(state.manifest_data)
            manifest_choices = manifest.mod_choices
        plan = classify_files(mods_dir, game_domain, mod_choices=manifest_choices)

        if plan.total_files == 0:
            return DeployResult(0, [], [], str(game_dir), False, skipped=len(plan.skipped), warnings=deploy_warnings)

        # Deploy
        progress("deploy", 0.3, f"Deploying {plan.total_files} files via {method}...")
        result = deploy_files(plan, game_dir, method=method, dry_run=False)

        # Write plugins.txt
        if prefix:
            plugins_src = mods_dir / "plugins.txt"
            plugins_dest = get_plugins_txt_dest(prefix, game_domain)
            if plugins_dest and plugins_src.exists():
                write_plugins_txt(plugins_src, plugins_dest)

        # Write game INI
        if prefix:
            ini_path = get_game_ini_path(prefix, game_domain)
            if ini_path:
                write_game_ini(ini_path, game_domain)

        # Save state
        state.game_dir = str(game_dir)
        if prefix:
            state.proton_prefix = str(prefix)
        state.deployed_files = [f.to_dict() for f in result.deployed]
        state.deployed_at = datetime.now(timezone.utc).isoformat()
        state.save()

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
        """Remove all deployed mod files. Returns count of removed files."""
        state = CollectionState(mods_dir)
        state.load()

        if not state.deployed_files:
            return 0

        removed = undeploy_files(state.deployed_files)

        state.deployed_files = []
        state.deployed_at = None
        state.game_dir = ""
        state.save()

        return removed

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
        """Match manually downloaded files to pending mods, extract, and update state."""
        progress = on_progress or _noop_progress
        errors: list[str] = []

        state = CollectionState(mods_dir)
        state.load()

        pending = state.get_pending_mods()
        if not pending:
            return ImportResult(matched=0, extracted=0, still_pending=0, errors=[], load_order_files=[])

        # Build filename lookup (case-insensitive)
        files_on_disk = {f.name.lower(): f for f in mods_dir.iterdir() if f.is_file()}

        matched = 0
        extracted = 0
        progress("import", 0.1, f"Scanning for {len(pending)} pending downloads...")

        for i, mod_state in enumerate(pending):
            filename_lower = mod_state.filename.lower()
            if filename_lower not in files_on_disk:
                continue

            file_path = files_on_disk[filename_lower]
            matched += 1

            try:
                if not no_extract and is_archive(file_path):
                    extract_archive(file_path, mods_dir)
                    file_path.unlink()
                    extracted += 1
            except ExtractionError as e:
                errors.append(f"Extraction error for {mod_state.name}: {e}")
                continue

            mod_state.download_status = "downloaded"
            mod_state.browser_url = ""
            mod_state.installed_at = datetime.now(timezone.utc).isoformat()
            pct = 0.1 + 0.8 * ((i + 1) / len(pending))
            progress("import", pct, f"Imported {mod_state.name}")

        state.save()

        # Regenerate load order if any mods were matched
        load_order_files: list[str] = []
        if matched > 0 and not no_load_order:
            progress("loadorder", 0.95, "Regenerating load order...")
            load_order_files = self._regen_load_order_from_state(mods_dir, state)

        # Sync tracked mods (same as premium sync does after downloading)
        if matched > 0:
            self._maybe_sync_tracked(state)

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
        self, collection_data: dict, mods_dir: Path, state: CollectionState
    ) -> list[str]:
        """Download manifest and generate load order. Returns list of written filenames."""
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

        # Inject phase 999 for manual mods
        for mod_id, ms in state.mods.items():
            if ms.manual:
                manifest.mod_phases[mod_id] = 999

        mod_requirements: dict[int, list[int]] = {}
        for mod_id, mod_state in state.mods.items():
            if mod_state.requirements:
                mod_requirements[mod_id] = mod_state.requirements

        mods_list = [
            {
                "mod_id": mod_id,
                "mod_name": ms.name,
                "file_id": ms.file_id,
                "version": ms.version,
                "filename": ms.filename,
                "optional": ms.optional,
            }
            for mod_id, ms in state.mods.items()
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
        local_ids = {mid for mid in state.mods if mid > 0}

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
