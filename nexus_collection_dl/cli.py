"""Command-line interface for nexus-collection-dl."""

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .api import NexusAPI, NexusAPIError, NexusPremiumRequired, NexusRateLimited
from .collection import CollectionParseError, parse_collection_url
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


if __name__ == "__main__":
    main()
