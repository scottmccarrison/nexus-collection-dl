"""Service layer - business logic extracted from CLI for programmatic use."""

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
from .steam import find_game_dir, find_proton_prefix


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
class SyncResult:
    mods_downloaded: int
    mods_extracted: int
    errors: list[str]
    load_order_files: list[str]
    tracked: int = 0
    untracked: int = 0


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


@dataclass
class DeployResult:
    deployed_count: int
    conflicts: list[str]
    errors: list[str]
    game_dir: str
    has_sfse: bool
    skipped: int = 0


@dataclass
class AddResult:
    mod_name: str
    mod_id: int
    file_name: str
    success: bool
    error: str = ""


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

    def __init__(self, api_key: str | None = None):
        self._api_key = api_key
        self._api: NexusAPI | None = None

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
                mod_statuses.append(ModStatus(
                    mod_id=mod["mod_id"],
                    name=mod["mod_name"],
                    version=installed.version if installed else mod.get("version", ""),
                    file_id=mod["file_id"],
                    optional=mod.get("optional", False),
                    manual=False,
                    phase=installed.phase if installed else 0,
                    status="up_to_date",
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
                mod_statuses.append(ModStatus(
                    mod_id=mod_id,
                    name=ms.name,
                    version=ms.version,
                    file_id=ms.file_id,
                    optional=ms.optional,
                    manual=ms.manual,
                    phase=ms.phase,
                    status="installed",
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

        # Validate premium
        progress("init", 0.0, "Validating API key...")
        user_info = self.api.validate_key()
        if not user_info.get("is_premium", False):
            raise NexusPremiumRequired("Premium membership required for direct downloads.")

        # Fetch collection
        progress("fetch", 0.05, "Fetching collection data...")
        collection_data = self.api.get_collection_mods(
            collection_info.game_domain, collection_info.slug
        )

        mods = collection_data["mods"]
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

        # Download mods
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
        is_premium = user_info.get("is_premium", False)

        collection_info = parse_collection_url(state.collection_url)

        progress("fetch", 0.05, "Checking for updates...")
        collection_data = self.api.get_collection_mods(
            collection_info.game_domain, collection_info.slug
        )

        new_revision = collection_data["revision"]
        mods = collection_data["mods"]
        if skip_optional:
            mods = [m for m in mods if not m.get("optional", False)]

        to_install, to_update, up_to_date, to_remove = state.compare_with_collection(mods)

        downloaded = 0
        if (to_install or to_update) and is_premium:
            mods_to_download = to_install + to_update
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
        if not user_info.get("is_premium", False):
            return AddResult(
                mod_name="", mod_id=mod_info.mod_id, file_name="",
                success=False, error="Premium membership required"
            )

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
            "size_bytes": selected.get("size_in_bytes") or selected.get("size", 0),
            "optional": False,
            "requirements": [],
        }

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
                return DeployResult(0, [], ["Could not find game directory. Use game_dir param."], "", False)

        if not game_dir.exists():
            return DeployResult(0, [], [f"Game directory does not exist: {game_dir}"], str(game_dir), False)

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
        plan = classify_files(mods_dir, game_domain)

        if plan.total_files == 0:
            return DeployResult(0, [], [], str(game_dir), False, skipped=len(plan.skipped))

        # Deploy
        method = "copy" if use_copy else "symlink"
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
