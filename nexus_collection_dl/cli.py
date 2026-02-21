"""Command-line interface for nexus-collection-dl."""

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from datetime import datetime, timezone

from .api import NexusAPI, NexusAPIError, NexusPremiumRequired, NexusRateLimited
from .collection import CollectionParseError, ModParseError, parse_collection_url, parse_mod_url
from .deploy import (
    DeployedFile,
    classify_files,
    deploy as deploy_files,
    get_game_ini_path,
    get_plugins_txt_dest,
    undeploy as undeploy_files,
    write_game_ini,
    write_plugins_txt,
)
from .downloader import DownloadError, Downloader
from .extractor import ExtractionError, extract_archive, is_archive, move_file
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

console = Console()


@click.group()
@click.option(
    "--api-key",
    envvar="NEXUS_API_KEY",
    help="Nexus Mods API key (or set NEXUS_API_KEY env var)",
)
@click.pass_context
def main(ctx: click.Context, api_key: str | None) -> None:
    """Download mod collections from Nexus Mods."""
    ctx.ensure_object(dict)
    ctx.obj["api_key"] = api_key


@main.command()
@click.argument("collection_url")
@click.argument("mods_dir", type=click.Path(path_type=Path))
@click.option("--skip-optional", is_flag=True, help="Skip optional mods")
@click.option("--no-load-order", is_flag=True, help="Skip load order generation")
@click.pass_context
def sync(
    ctx: click.Context,
    collection_url: str,
    mods_dir: Path,
    skip_optional: bool,
    no_load_order: bool,
) -> None:
    """
    Download a collection to the specified directory.

    COLLECTION_URL: URL of the Nexus Mods collection
    MODS_DIR: Directory to download mods to
    """
    api_key = ctx.obj.get("api_key")

    # Parse collection URL
    try:
        collection_info = parse_collection_url(collection_url)
    except CollectionParseError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    console.print(
        f"[bold]Collection:[/bold] {collection_info.game_domain} / {collection_info.slug}"
    )

    # Initialize API
    try:
        api = NexusAPI(api_key)
        user_info = api.validate_key()
        is_premium = user_info.get("is_premium", False)

        if not is_premium:
            console.print(
                "[yellow]Warning:[/yellow] Premium membership required for direct downloads."
            )
            console.print("You can still view collection contents with 'status' command.")
            sys.exit(1)

        console.print(f"[green]Authenticated as:[/green] {user_info.get('name')}")

    except NexusAPIError as e:
        console.print(f"[red]API Error:[/red] {e}")
        sys.exit(1)

    # Fetch collection data
    console.print("[dim]Fetching collection data...[/dim]")
    try:
        collection_data = api.get_collection_mods(
            collection_info.game_domain, collection_info.slug
        )
    except NexusAPIError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    mods = collection_data["mods"]
    if skip_optional:
        mods = [m for m in mods if not m.get("optional", False)]

    console.print(f"[bold]Collection:[/bold] {collection_data['name']}")
    console.print(f"[bold]Revision:[/bold] {collection_data['revision']}")
    console.print(f"[bold]Mods to download:[/bold] {len(mods)}")

    if not mods:
        console.print("[yellow]No mods to download.[/yellow]")
        return

    # Initialize state
    state = CollectionState(mods_dir)
    if state.exists():
        state.load()
        console.print("[dim]Existing state found, will update.[/dim]")

    state.set_collection_info(
        url=collection_info.url,
        name=collection_data["name"],
        revision=collection_data["revision"],
        game_domain=collection_data["game_domain"],
    )

    # Download mods
    downloader = Downloader(api)
    downloaded = []

    def on_download_complete(mod_info: dict, path: Path) -> None:
        downloaded.append((mod_info, path))

    console.print("\n[bold]Downloading mods...[/bold]")
    results = downloader.download_mods(
        game_domain=collection_data["game_domain"],
        mods=mods,
        target_dir=mods_dir,
        on_complete=on_download_complete,
    )

    # Extract archives
    console.print("\n[bold]Extracting archives...[/bold]")
    for mod_info, file_path in results:
        try:
            if is_archive(file_path):
                console.print(f"[dim]Extracting {file_path.name}...[/dim]")
                extract_archive(file_path, mods_dir)
                # Remove archive after extraction
                file_path.unlink()
            else:
                # Non-archive file (e.g., .pak), just keep it
                console.print(f"[dim]Installed {file_path.name}[/dim]")

            # Update state
            state.add_mod(mod_info)

        except ExtractionError as e:
            console.print(f"[red]Extraction error:[/red] {e}")

    # Save state
    state.save()

    # Generate load order
    if not no_load_order:
        console.print("\n[bold]Generating load order...[/bold]")
        _generate_load_order(collection_data, mods_dir, state, api)

    console.print(f"\n[green]Successfully synced {len(results)} mods![/green]")
    console.print(f"[dim]State saved to {state.state_file}[/dim]")


@main.command()
@click.argument("mods_dir", type=click.Path(exists=True, path_type=Path))
@click.option("--skip-optional", is_flag=True, help="Skip optional mods")
@click.option("--dry-run", is_flag=True, help="Show what would be updated without downloading")
@click.option("--no-load-order", is_flag=True, help="Skip load order generation")
@click.pass_context
def update(
    ctx: click.Context,
    mods_dir: Path,
    skip_optional: bool,
    dry_run: bool,
    no_load_order: bool,
) -> None:
    """
    Check for and download updated mods.

    MODS_DIR: Directory containing previously synced mods
    """
    api_key = ctx.obj.get("api_key")

    # Load existing state
    state = CollectionState(mods_dir)
    try:
        state.load()
    except StateError as e:
        console.print(f"[red]Error:[/red] {e}")
        console.print("Run 'sync' first to download a collection.")
        sys.exit(1)

    console.print(f"[bold]Collection:[/bold] {state.collection_name}")
    console.print(f"[bold]Current revision:[/bold] {state.collection_revision}")

    # Initialize API
    try:
        api = NexusAPI(api_key)
        if not dry_run:
            user_info = api.validate_key()
            if not user_info.get("is_premium", False):
                console.print(
                    "[yellow]Warning:[/yellow] Premium membership required for downloads."
                )
                dry_run = True
    except NexusAPIError as e:
        console.print(f"[red]API Error:[/red] {e}")
        sys.exit(1)

    # Parse collection URL from state
    try:
        collection_info = parse_collection_url(state.collection_url)
    except CollectionParseError as e:
        console.print(f"[red]Error:[/red] Invalid collection URL in state: {e}")
        sys.exit(1)

    # Fetch latest collection data
    console.print("[dim]Checking for updates...[/dim]")
    try:
        collection_data = api.get_collection_mods(
            collection_info.game_domain, collection_info.slug
        )
    except NexusAPIError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    new_revision = collection_data["revision"]
    console.print(f"[bold]Latest revision:[/bold] {new_revision}")

    mods = collection_data["mods"]
    if skip_optional:
        mods = [m for m in mods if not m.get("optional", False)]

    # Compare with installed
    to_install, to_update, up_to_date, to_remove = state.compare_with_collection(mods)

    if not to_install and not to_update:
        console.print("[green]Everything is up to date![/green]")
        return

    # Show what will be changed
    if to_install:
        console.print(f"\n[bold]New mods to install:[/bold] {len(to_install)}")
        for mod in to_install:
            console.print(f"  + {mod['mod_name']}")

    if to_update:
        console.print(f"\n[bold]Mods to update:[/bold] {len(to_update)}")
        for mod in to_update:
            installed = state.get_mod(mod["mod_id"])
            old_ver = installed.version if installed else "?"
            console.print(f"  ~ {mod['mod_name']} ({old_ver} -> {mod['version']})")

    if to_remove:
        console.print(f"\n[bold]Mods no longer in collection:[/bold] {len(to_remove)}")
        for mod_id in to_remove:
            installed = state.get_mod(mod_id)
            name = installed.name if installed else str(mod_id)
            console.print(f"  - {name}")

    if dry_run:
        console.print("\n[yellow]Dry run - no changes made.[/yellow]")
        return

    # Download new/updated mods
    mods_to_download = to_install + to_update
    if mods_to_download:
        console.print("\n[bold]Downloading...[/bold]")
        downloader = Downloader(api)
        results = downloader.download_mods(
            game_domain=collection_data["game_domain"],
            mods=mods_to_download,
            target_dir=mods_dir,
        )

        # Extract archives
        console.print("\n[bold]Extracting...[/bold]")
        for mod_info, file_path in results:
            try:
                if is_archive(file_path):
                    extract_archive(file_path, mods_dir)
                    file_path.unlink()
                state.add_mod(mod_info)
            except ExtractionError as e:
                console.print(f"[red]Extraction error:[/red] {e}")

    # Update state
    state.collection_revision = new_revision
    state.save()

    # Generate load order
    if not no_load_order:
        console.print("\n[bold]Generating load order...[/bold]")
        _generate_load_order(collection_data, mods_dir, state, api)

    console.print(f"\n[green]Update complete![/green]")


@main.command()
@click.argument("mods_dir", type=click.Path(exists=True, path_type=Path))
@click.pass_context
def status(ctx: click.Context, mods_dir: Path) -> None:
    """
    Show status of installed mods vs collection.

    MODS_DIR: Directory containing previously synced mods
    """
    api_key = ctx.obj.get("api_key")

    # Load existing state
    state = CollectionState(mods_dir)
    try:
        state.load()
    except StateError as e:
        console.print(f"[red]Error:[/red] {e}")
        console.print("Run 'sync' first to download a collection.")
        sys.exit(1)

    console.print(f"[bold]Collection:[/bold] {state.collection_name}")
    console.print(f"[bold]URL:[/bold] {state.collection_url}")
    console.print(f"[bold]Installed revision:[/bold] {state.collection_revision}")
    console.print(f"[bold]Installed mods:[/bold] {len(state.mods)}")

    # Try to fetch latest from API
    try:
        api = NexusAPI(api_key)
        collection_info = parse_collection_url(state.collection_url)
        collection_data = api.get_collection_mods(
            collection_info.game_domain, collection_info.slug
        )

        latest_revision = collection_data["revision"]
        console.print(f"[bold]Latest revision:[/bold] {latest_revision}")

        if latest_revision != state.collection_revision:
            console.print("[yellow]Update available![/yellow]")

        # Compare
        to_install, to_update, up_to_date, to_remove = state.compare_with_collection(
            collection_data["mods"]
        )

        # Show table
        table = Table(title="Mod Status")
        table.add_column("Mod", style="cyan")
        table.add_column("Installed", style="green")
        table.add_column("Latest", style="blue")
        table.add_column("Status")

        for mod in up_to_date:
            installed = state.get_mod(mod["mod_id"])
            table.add_row(
                mod["mod_name"][:40],
                installed.version if installed else "-",
                mod["version"] or "-",
                "[green]Up to date[/green]",
            )

        for mod in to_update:
            installed = state.get_mod(mod["mod_id"])
            table.add_row(
                mod["mod_name"][:40],
                installed.version if installed else "-",
                mod["version"] or "-",
                "[yellow]Update available[/yellow]",
            )

        for mod in to_install:
            table.add_row(
                mod["mod_name"][:40],
                "-",
                mod["version"] or "-",
                "[blue]Not installed[/blue]",
            )

        for mod_id in to_remove:
            installed = state.get_mod(mod_id)
            if installed:
                table.add_row(
                    installed.name[:40],
                    installed.version,
                    "-",
                    "[red]Removed from collection[/red]",
                )

        console.print(table)

    except NexusAPIError as e:
        console.print(f"[yellow]Could not fetch latest data:[/yellow] {e}")
        console.print("\n[bold]Installed mods:[/bold]")
        for mod in state.mods.values():
            console.print(f"  - {mod.name} v{mod.version}")


def _generate_load_order(
    collection_data: dict,
    mods_dir: Path,
    state: CollectionState,
    api: NexusAPI | None = None,
) -> None:
    """Download manifest and generate load order files."""
    download_link = collection_data.get("download_link")
    if not download_link:
        console.print("[yellow]No download link available — skipping load order.[/yellow]")
        return

    # Download and parse manifest (pass authenticated session for relative API paths)
    try:
        session = api.session if api else None
        manifest = download_and_parse_manifest(download_link, session=session)
    except ManifestError as e:
        console.print(f"[yellow]Could not parse manifest:[/yellow] {e}")
        return

    # Build mod_requirements dict from collection data
    mod_requirements: dict[int, list[int]] = {}
    for mod in collection_data.get("mods", []):
        reqs = mod.get("requirements", [])
        if reqs:
            mod_requirements[mod["mod_id"]] = reqs

    game_domain = collection_data["game_domain"]

    # Generate load order
    generator = LoadOrderGenerator(
        manifest=manifest,
        mods=collection_data["mods"],
        mod_requirements=mod_requirements,
        game_domain=game_domain,
    )

    try:
        written = generator.generate(mods_dir)
        for path in written:
            console.print(f"  [green]Wrote[/green] {path.name}")
    except Exception as e:
        console.print(f"[yellow]Load order generation failed:[/yellow] {e}")
        return

    # LOOT sorting for Bethesda games
    if is_bethesda_game(game_domain) and is_loot_available():
        console.print("[dim]Running LOOT plugin sort...[/dim]")
        loot_sorted = sort_plugins_with_loot(game_domain, mods_dir, None)
        merged = merge_plugin_orders(manifest.plugins, loot_sorted)
        used_loot = loot_sorted is not None
        plugins_path = mods_dir / "plugins.txt"
        write_loot_plugins_txt(merged, plugins_path, game_domain, used_loot)
        source = "LOOT-sorted" if used_loot else "collection metadata"
        console.print(f"  [green]Wrote[/green] plugins.txt ({source})")
    elif is_bethesda_game(game_domain) and not is_loot_available():
        console.print(
            "[dim]Install libloot for LOOT-sorted plugin order "
            "(pip install loot)[/dim]"
        )

    # Cache manifest in state
    state.manifest_data = manifest.to_dict()
    state.mod_rules = manifest.mod_rules
    state.save()


@main.command(name="load-order")
@click.argument("mods_dir", type=click.Path(exists=True, path_type=Path))
@click.pass_context
def load_order(ctx: click.Context, mods_dir: Path) -> None:
    """Regenerate load order from cached manifest."""
    state = CollectionState(mods_dir)
    try:
        state.load()
    except StateError as e:
        console.print(f"[red]Error:[/red] {e}")
        console.print("Run 'sync' first to download a collection.")
        sys.exit(1)

    if not state.manifest_data:
        console.print(
            "[red]Error:[/red] No cached manifest found. "
            "Run 'sync' or 'update' first to download the collection manifest."
        )
        sys.exit(1)

    console.print(f"[bold]Collection:[/bold] {state.collection_name}")
    console.print(f"[bold]Game:[/bold] {state.game_domain}")

    # Reconstruct manifest from cached data
    manifest = CollectionManifest.from_dict(state.manifest_data)

    # Build mod_requirements from state
    mod_requirements: dict[int, list[int]] = {}
    for mod_id, mod_state in state.mods.items():
        if mod_state.requirements:
            mod_requirements[mod_id] = mod_state.requirements

    # Rebuild mods list from state
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

    console.print("[bold]Generating load order...[/bold]")
    try:
        written = generator.generate(mods_dir)
        for path in written:
            console.print(f"  [green]Wrote[/green] {path.name}")
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    # LOOT sorting for Bethesda games
    if is_bethesda_game(state.game_domain) and is_loot_available():
        console.print("[dim]Running LOOT plugin sort...[/dim]")
        loot_sorted = sort_plugins_with_loot(state.game_domain, mods_dir, None)
        merged = merge_plugin_orders(manifest.plugins, loot_sorted)
        used_loot = loot_sorted is not None
        plugins_path = mods_dir / "plugins.txt"
        write_loot_plugins_txt(merged, plugins_path, state.game_domain, used_loot)
        source = "LOOT-sorted" if used_loot else "collection metadata"
        console.print(f"  [green]Wrote[/green] plugins.txt ({source})")
    elif is_bethesda_game(state.game_domain) and not is_loot_available():
        console.print(
            "[dim]Install libloot for LOOT-sorted plugin order "
            "(pip install loot)[/dim]"
        )

    console.print("[green]Load order regenerated![/green]")


@main.command()
@click.argument("mods_dir", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--game-dir",
    type=click.Path(path_type=Path),
    help="Game install directory (auto-detected from Steam if not specified)",
)
@click.option(
    "--prefix",
    type=click.Path(path_type=Path),
    help="Proton/Wine prefix path (auto-detected if not specified)",
)
@click.option("--copy", "use_copy", is_flag=True, help="Copy files instead of symlinking")
@click.option("--dry-run", is_flag=True, help="Show what would be deployed without doing it")
@click.pass_context
def deploy(
    ctx: click.Context,
    mods_dir: Path,
    game_dir: Path | None,
    prefix: Path | None,
    use_copy: bool,
    dry_run: bool,
) -> None:
    """
    Deploy mods from staging directory to game directory.

    MODS_DIR: Directory containing previously synced mods
    """
    # Load state
    state = CollectionState(mods_dir)
    try:
        state.load()
    except StateError as e:
        console.print(f"[red]Error:[/red] {e}")
        console.print("Run 'sync' first to download a collection.")
        sys.exit(1)

    game_domain = state.game_domain
    console.print(f"[bold]Collection:[/bold] {state.collection_name}")
    console.print(f"[bold]Game:[/bold] {game_domain}")

    # Detect game directory (flag > saved state > auto-detect)
    if game_dir is None and state.game_dir:
        game_dir = Path(state.game_dir)
        console.print(f"[dim]Using saved game directory.[/dim]")
    if game_dir is None:
        console.print("[dim]Detecting game directory from Steam...[/dim]")
        game_dir = find_game_dir(game_domain)
        if game_dir is None:
            console.print(
                "[red]Error:[/red] Could not find game directory. "
                "Use --game-dir to specify it manually."
            )
            sys.exit(1)

    if not game_dir.exists():
        console.print(f"[red]Error:[/red] Game directory does not exist: {game_dir}")
        sys.exit(1)

    console.print(f"[bold]Game directory:[/bold] {game_dir}")

    # Detect Proton prefix (flag > saved state > auto-detect)
    if prefix is None and state.proton_prefix:
        prefix = Path(state.proton_prefix)
        console.print(f"[dim]Using saved Proton prefix.[/dim]")
    if prefix is None:
        prefix = find_proton_prefix(game_domain)
    if prefix:
        console.print(f"[bold]Proton prefix:[/bold] {prefix}")

    # Clean up previous deployment if present
    if state.deployed_files and not dry_run:
        console.print(
            f"\n[dim]Removing previous deployment ({len(state.deployed_files)} files)...[/dim]"
        )
        removed = undeploy_files(state.deployed_files)
        console.print(f"[dim]Removed {removed} previously deployed files.[/dim]")
        state.deployed_files = []
        state.deployed_at = None

    # Classify files
    console.print("\n[dim]Classifying files...[/dim]")
    plan = classify_files(mods_dir, game_domain)

    console.print(f"  Game root files: {len(plan.game_root_files)}")
    console.print(f"  Data files: {len(plan.data_files)}")
    console.print(f"  Skipped: {len(plan.skipped)}")

    if plan.total_files == 0:
        console.print("[yellow]No files to deploy.[/yellow]")
        return

    # Deploy
    method = "copy" if use_copy else "symlink"
    action = "Would deploy" if dry_run else "Deploying"
    console.print(f"\n[bold]{action} {plan.total_files} files via {method}...[/bold]")

    result = deploy_files(plan, game_dir, method=method, dry_run=dry_run)

    # Write plugins.txt to AppData
    if prefix and not dry_run:
        plugins_src = mods_dir / "plugins.txt"
        plugins_dest = get_plugins_txt_dest(prefix, game_domain)
        if plugins_dest and plugins_src.exists():
            write_plugins_txt(plugins_src, plugins_dest)
            console.print(f"  [green]Wrote[/green] plugins.txt -> {plugins_dest}")

    # Write game INI
    if prefix and not dry_run:
        ini_path = get_game_ini_path(prefix, game_domain)
        if ini_path:
            if write_game_ini(ini_path, game_domain):
                console.print(f"  [green]Wrote[/green] {ini_path.name} -> {ini_path}")

    # Show conflicts
    if result.conflicts:
        console.print(f"\n[yellow]Conflicts ({len(result.conflicts)}):[/yellow]")
        for conflict in result.conflicts[:10]:
            console.print(f"  [yellow]![/yellow] {conflict}")
        if len(result.conflicts) > 10:
            console.print(f"  ... and {len(result.conflicts) - 10} more")

    # Show errors
    if result.errors:
        console.print(f"\n[red]Errors ({len(result.errors)}):[/red]")
        for error in result.errors[:10]:
            console.print(f"  [red]x[/red] {error}")

    # Summary
    console.print(
        f"\n[green]Deployed {len(result.deployed)} files "
        f"({len(result.conflicts)} conflicts, {len(result.errors)} errors)[/green]"
    )

    # Save deployment state
    if not dry_run:
        state.game_dir = str(game_dir)
        if prefix:
            state.proton_prefix = str(prefix)
        state.deployed_files = [f.to_dict() for f in result.deployed]
        state.deployed_at = datetime.now(timezone.utc).isoformat()
        state.save()
        console.print(f"[dim]Deployment state saved.[/dim]")

    # Post-deploy: SFSE launch instructions
    has_sfse = any(
        "sfse_loader" in str(f.dest).lower() for f in result.deployed
    )
    if has_sfse:
        sfse_path = game_dir / "sfse_loader.exe"
        console.print(f"\n[bold yellow]SFSE detected![/bold yellow]")
        console.print(
            "  To launch with mods, set your Steam shortcut TARGET to:"
        )
        console.print(f"  [cyan]{sfse_path}[/cyan]")
        console.print(
            "  Do NOT rename sfse_loader.exe - SFSE needs the original "
            "game exe alongside it."
        )


@main.command()
@click.argument("mods_dir", type=click.Path(exists=True, path_type=Path))
@click.pass_context
def undeploy(ctx: click.Context, mods_dir: Path) -> None:
    """
    Remove all deployed mod files from game directory.

    MODS_DIR: Directory containing previously synced mods
    """
    state = CollectionState(mods_dir)
    try:
        state.load()
    except StateError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    if not state.deployed_files:
        console.print("[yellow]No deployment found. Nothing to undeploy.[/yellow]")
        return

    console.print(f"[bold]Collection:[/bold] {state.collection_name}")
    console.print(f"[bold]Game directory:[/bold] {state.game_dir}")
    console.print(f"[bold]Deployed files:[/bold] {len(state.deployed_files)}")
    console.print(f"[bold]Deployed at:[/bold] {state.deployed_at}")

    console.print("\n[bold]Removing deployed files...[/bold]")
    removed = undeploy_files(state.deployed_files)

    # Clear deployment state
    state.deployed_files = []
    state.deployed_at = None
    state.game_dir = ""
    state.save()

    console.print(f"\n[green]Removed {removed} files. Game directory restored.[/green]")


def _select_mod_file(
    files: list[dict], file_id_override: int | None = None
) -> dict | None:
    """
    Select a file from the mod's file list.

    Priority:
    1. file_id override (exact match)
    2. MAIN category, highest file_id (most recent)
    3. Any non-archived file, highest file_id
    4. None (caller should list files for user to pick)
    """
    if not files:
        return None

    if file_id_override is not None:
        for f in files:
            if f.get("file_id") == file_id_override:
                return f
        return None

    # Category IDs: 1=MAIN, 2=UPDATE, 3=OPTIONAL, 4=OLD, 5=MISC, 6=ARCHIVED
    main_files = [f for f in files if f.get("category_id") == 1]
    if main_files:
        return max(main_files, key=lambda f: f.get("file_id", 0))

    non_archived = [f for f in files if f.get("category_id") != 6]
    if non_archived:
        return max(non_archived, key=lambda f: f.get("file_id", 0))

    return None


def _regen_load_order_from_state(mods_dir: Path, state: CollectionState) -> None:
    """Regenerate load order from cached manifest + current state mods."""
    if not state.manifest_data:
        console.print("[dim]No cached manifest - skipping load order regen.[/dim]")
        return

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

    try:
        written = generator.generate(mods_dir)
        for path in written:
            console.print(f"  [green]Wrote[/green] {path.name}")
    except Exception as e:
        console.print(f"[yellow]Load order regen failed:[/yellow] {e}")
        return

    if is_bethesda_game(state.game_domain) and is_loot_available():
        console.print("[dim]Running LOOT plugin sort...[/dim]")
        loot_sorted = sort_plugins_with_loot(state.game_domain, mods_dir, None)
        merged = merge_plugin_orders(manifest.plugins, loot_sorted)
        used_loot = loot_sorted is not None
        plugins_path = mods_dir / "plugins.txt"
        write_loot_plugins_txt(merged, plugins_path, state.game_domain, used_loot)
        source = "LOOT-sorted" if used_loot else "collection metadata"
        console.print(f"  [green]Wrote[/green] plugins.txt ({source})")


@main.command()
@click.argument("mod_url")
@click.argument("mods_dir", type=click.Path(exists=True, path_type=Path))
@click.option("--file-id", type=int, default=None, help="Specific file ID to download")
@click.option("--no-load-order", is_flag=True, help="Skip load order regeneration")
@click.pass_context
def add(
    ctx: click.Context,
    mod_url: str,
    mods_dir: Path,
    file_id: int | None,
    no_load_order: bool,
) -> None:
    """
    Add a single mod by URL.

    MOD_URL: Nexus Mods mod page URL (e.g. https://www.nexusmods.com/starfield/mods/123)
    MODS_DIR: Directory containing a previously synced collection
    """
    api_key = ctx.obj.get("api_key")

    # Parse mod URL
    try:
        mod_info = parse_mod_url(mod_url)
    except ModParseError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    console.print(
        f"[bold]Mod:[/bold] {mod_info.game_domain} / mod {mod_info.mod_id}"
    )

    # Load existing state
    state = CollectionState(mods_dir)
    try:
        state.load()
    except StateError as e:
        console.print(f"[red]Error:[/red] {e}")
        console.print("Run 'sync' first to download a collection.")
        sys.exit(1)

    # Validate game domain matches
    if state.game_domain and state.game_domain.lower() != mod_info.game_domain.lower():
        console.print(
            f"[red]Error:[/red] Mod is for '{mod_info.game_domain}' but collection "
            f"is for '{state.game_domain}'"
        )
        sys.exit(1)

    # Initialize API and validate premium
    try:
        api = NexusAPI(api_key)
        user_info = api.validate_key()
        if not user_info.get("is_premium", False):
            console.print(
                "[yellow]Warning:[/yellow] Premium membership required for direct downloads."
            )
            sys.exit(1)
        console.print(f"[green]Authenticated as:[/green] {user_info.get('name')}")
    except NexusAPIError as e:
        console.print(f"[red]API Error:[/red] {e}")
        sys.exit(1)

    # Fetch mod info
    try:
        nexus_mod = api.get_mod_info(mod_info.game_domain, mod_info.mod_id)
    except NexusAPIError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    mod_name = nexus_mod.get("name", f"Mod {mod_info.mod_id}")
    console.print(f"[bold]Name:[/bold] {mod_name}")

    # Fetch file list
    try:
        files = api.get_mod_files(mod_info.game_domain, mod_info.mod_id)
    except NexusAPIError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    if not files:
        console.print("[red]Error:[/red] No files found for this mod.")
        sys.exit(1)

    # Select file
    selected = _select_mod_file(files, file_id)
    if selected is None:
        if file_id is not None:
            console.print(f"[red]Error:[/red] File ID {file_id} not found.")
        else:
            console.print("[yellow]Could not auto-select a file. Available files:[/yellow]")
        console.print()
        table = Table(title="Available Files")
        table.add_column("File ID", style="cyan")
        table.add_column("Name")
        table.add_column("Version")
        table.add_column("Category")
        table.add_column("Size")
        category_names = {1: "Main", 2: "Update", 3: "Optional", 4: "Old", 5: "Misc", 6: "Archived"}
        for f in files:
            cat = category_names.get(f.get("category_id", 0), "Unknown")
            size_kb = (f.get("size_in_bytes") or f.get("size", 0)) / 1024
            size_str = f"{size_kb:.0f} KB" if size_kb < 1024 else f"{size_kb/1024:.1f} MB"
            table.add_row(
                str(f.get("file_id", "")),
                f.get("name", f.get("file_name", "")),
                f.get("version", "-"),
                cat,
                size_str,
            )
        console.print(table)
        console.print("\nRe-run with --file-id to pick a specific file.")
        sys.exit(1)

    selected_file_id = selected["file_id"]
    selected_name = selected.get("name", selected.get("file_name", ""))
    selected_version = selected.get("version", "")
    console.print(
        f"[bold]File:[/bold] {selected_name} (ID: {selected_file_id}, v{selected_version})"
    )

    # Download
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

    console.print("\n[bold]Downloading...[/bold]")
    downloader = Downloader(api)
    results = downloader.download_mods(
        game_domain=mod_info.game_domain,
        mods=[download_info],
        target_dir=mods_dir,
    )

    if not results:
        console.print("[red]Error:[/red] Download failed.")
        sys.exit(1)

    # Extract
    console.print("[bold]Extracting...[/bold]")
    for _mod, file_path in results:
        try:
            if is_archive(file_path):
                console.print(f"[dim]Extracting {file_path.name}...[/dim]")
                extract_archive(file_path, mods_dir)
                file_path.unlink()
            else:
                console.print(f"[dim]Installed {file_path.name}[/dim]")
        except ExtractionError as e:
            console.print(f"[red]Extraction error:[/red] {e}")
            sys.exit(1)

    # Register in state as manual
    download_info["manual"] = True
    state.add_mod(download_info)
    state.mods[mod_info.mod_id].phase = 999
    state.save()

    console.print(f"[green]Added '{mod_name}' as manual mod (phase 999).[/green]")

    # Regen load order
    if not no_load_order:
        console.print("\n[bold]Regenerating load order...[/bold]")
        _regen_load_order_from_state(mods_dir, state)


@main.command(name="add-local")
@click.argument("name")
@click.argument("mods_dir", type=click.Path(exists=True, path_type=Path))
@click.option("--no-load-order", is_flag=True, help="Skip load order regeneration")
@click.pass_context
def add_local(
    ctx: click.Context,
    name: str,
    mods_dir: Path,
    no_load_order: bool,
) -> None:
    """
    Register a local mod that's already in the mods directory.

    NAME: Display name for the mod
    MODS_DIR: Directory containing a previously synced collection
    """
    # Load existing state
    state = CollectionState(mods_dir)
    try:
        state.load()
    except StateError as e:
        console.print(f"[red]Error:[/red] {e}")
        console.print("Run 'sync' first to download a collection.")
        sys.exit(1)

    # Generate synthetic negative mod_id
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

    console.print(
        f"[green]Registered '{name}' as manual mod "
        f"(ID: {synthetic_id}, phase 999).[/green]"
    )

    # Regen load order
    if not no_load_order:
        console.print("\n[bold]Regenerating load order...[/bold]")
        _regen_load_order_from_state(mods_dir, state)


if __name__ == "__main__":
    main()
