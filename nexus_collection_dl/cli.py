"""Command-line interface for nexus-collection-dl."""

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .api import NexusAPIError
from .collection import CollectionParseError, ModParseError
from .service import ModManagerService, _select_mod_file
from .state import StateError

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
    ctx.obj["service"] = ModManagerService(api_key)


def _cli_progress(event: str, pct: float, msg: str) -> None:
    """Print progress messages to Rich console."""
    console.print(f"[dim]{msg}[/dim]")


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
    svc = ctx.obj["service"]
    try:
        result = svc.sync(
            collection_url, mods_dir,
            skip_optional=skip_optional,
            no_load_order=no_load_order,
            on_progress=_cli_progress,
        )
    except (NexusAPIError, CollectionParseError, StateError) as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    if result.errors:
        for err in result.errors:
            console.print(f"[red]Error:[/red] {err}")

    console.print(
        f"\n[green]Successfully synced {result.mods_downloaded} mods "
        f"({result.mods_extracted} extracted)![/green]"
    )
    if result.load_order_files:
        for f in result.load_order_files:
            console.print(f"  [green]Wrote[/green] {f}")
    if result.tracked or result.untracked:
        console.print(f"[dim]Tracked {result.tracked}, untracked {result.untracked} on Nexus.[/dim]")


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
    svc = ctx.obj["service"]

    if dry_run:
        # For dry-run, use get_status which doesn't download anything
        try:
            status = svc.get_status(mods_dir)
        except (NexusAPIError, StateError) as e:
            console.print(f"[red]Error:[/red] {e}")
            sys.exit(1)

        console.print(f"[bold]Collection:[/bold] {status.collection_name}")
        console.print(f"[bold]Current revision:[/bold] {status.installed_revision}")
        if status.latest_revision:
            console.print(f"[bold]Latest revision:[/bold] {status.latest_revision}")

        updates = [m for m in status.mods if m.status == "update_available"]
        new = [m for m in status.mods if m.status == "not_installed"]
        removed = [m for m in status.mods if m.status == "removed"]

        if not updates and not new:
            console.print("[green]Everything is up to date![/green]")
            return

        if new:
            console.print(f"\n[bold]New mods to install:[/bold] {len(new)}")
            for mod in new:
                console.print(f"  + {mod.name}")
        if updates:
            console.print(f"\n[bold]Mods to update:[/bold] {len(updates)}")
            for mod in updates:
                console.print(f"  ~ {mod.name}")
        if removed:
            console.print(f"\n[bold]Mods no longer in collection:[/bold] {len(removed)}")
            for mod in removed:
                console.print(f"  - {mod.name}")

        console.print("\n[yellow]Dry run - no changes made.[/yellow]")
        return

    try:
        result = svc.update(
            mods_dir,
            skip_optional=skip_optional,
            no_load_order=no_load_order,
            on_progress=_cli_progress,
        )
    except (NexusAPIError, CollectionParseError, StateError) as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    if not result.to_install and not result.to_update:
        console.print("[green]Everything is up to date![/green]")
        return

    if result.to_install:
        console.print(f"\n[bold]Installed:[/bold] {len(result.to_install)} new mods")
    if result.to_update:
        console.print(f"[bold]Updated:[/bold] {len(result.to_update)} mods")
    if result.errors:
        for err in result.errors:
            console.print(f"[red]Error:[/red] {err}")

    console.print(f"\n[green]Update complete![/green]")


@main.command()
@click.argument("mods_dir", type=click.Path(exists=True, path_type=Path))
@click.pass_context
def status(ctx: click.Context, mods_dir: Path) -> None:
    """
    Show status of installed mods vs collection.

    MODS_DIR: Directory containing previously synced mods
    """
    svc = ctx.obj["service"]
    try:
        result = svc.get_status(mods_dir)
    except StateError as e:
        console.print(f"[red]Error:[/red] {e}")
        console.print("Run 'sync' first to download a collection.")
        sys.exit(1)

    console.print(f"[bold]Collection:[/bold] {result.collection_name}")
    console.print(f"[bold]URL:[/bold] {result.collection_url}")
    console.print(f"[bold]Installed revision:[/bold] {result.installed_revision}")
    console.print(f"[bold]Installed mods:[/bold] {len(result.mods)}")

    if result.latest_revision is not None:
        console.print(f"[bold]Latest revision:[/bold] {result.latest_revision}")
        if result.latest_revision != result.installed_revision:
            console.print("[yellow]Update available![/yellow]")

    table = Table(title="Mod Status")
    table.add_column("Mod", style="cyan")
    table.add_column("Version", style="green")
    table.add_column("Status")
    table.add_column("Phase", style="dim")

    status_styles = {
        "up_to_date": "[green]Up to date[/green]",
        "update_available": "[yellow]Update available[/yellow]",
        "not_installed": "[blue]Not installed[/blue]",
        "removed": "[red]Removed from collection[/red]",
        "installed": "[green]Installed[/green]",
        "manual": "[cyan]Manual[/cyan]",
    }

    for mod in result.mods:
        table.add_row(
            mod.name[:40],
            mod.version or "-",
            status_styles.get(mod.status, mod.status),
            str(mod.phase),
        )

    console.print(table)


@main.command(name="load-order")
@click.argument("mods_dir", type=click.Path(exists=True, path_type=Path))
@click.pass_context
def load_order(ctx: click.Context, mods_dir: Path) -> None:
    """Regenerate load order from cached manifest."""
    svc = ctx.obj["service"]
    try:
        files = svc.regenerate_load_order(mods_dir)
    except StateError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    for f in files:
        console.print(f"  [green]Wrote[/green] {f}")
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
    if dry_run:
        # For dry-run, use deploy module directly
        from .deploy import classify_files
        from .state import CollectionState
        from .steam import find_game_dir

        state = CollectionState(mods_dir)
        try:
            state.load()
        except StateError as e:
            console.print(f"[red]Error:[/red] {e}")
            sys.exit(1)

        if game_dir is None and state.game_dir:
            game_dir = Path(state.game_dir)
        if game_dir is None:
            game_dir = find_game_dir(state.game_domain)
        if game_dir is None:
            console.print("[red]Error:[/red] Could not find game directory.")
            sys.exit(1)

        plan = classify_files(mods_dir, state.game_domain)
        console.print(f"  Game root files: {len(plan.game_root_files)}")
        console.print(f"  Data files: {len(plan.data_files)}")
        console.print(f"  Skipped: {len(plan.skipped)}")
        console.print(f"\n[yellow]Would deploy {plan.total_files} files (dry run).[/yellow]")
        return

    svc = ctx.obj["service"]
    try:
        result = svc.deploy(
            mods_dir,
            game_dir=game_dir,
            prefix=prefix,
            use_copy=use_copy,
            on_progress=_cli_progress,
        )
    except (StateError, NexusAPIError) as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    if result.errors:
        for err in result.errors:
            console.print(f"[red]Error:[/red] {err}")
        if result.deployed_count == 0:
            sys.exit(1)

    if result.conflicts:
        console.print(f"\n[yellow]Conflicts ({len(result.conflicts)}):[/yellow]")
        for conflict in result.conflicts[:10]:
            console.print(f"  [yellow]![/yellow] {conflict}")
        if len(result.conflicts) > 10:
            console.print(f"  ... and {len(result.conflicts) - 10} more")

    console.print(
        f"\n[green]Deployed {result.deployed_count} files "
        f"({len(result.conflicts)} conflicts, {len(result.errors)} errors)[/green]"
    )

    if result.has_sfse:
        sfse_path = Path(result.game_dir) / "sfse_loader.exe"
        console.print(f"\n[bold yellow]SFSE detected![/bold yellow]")
        console.print("  To launch with mods, set your Steam shortcut TARGET to:")
        console.print(f"  [cyan]{sfse_path}[/cyan]")


@main.command()
@click.argument("mods_dir", type=click.Path(exists=True, path_type=Path))
@click.pass_context
def undeploy(ctx: click.Context, mods_dir: Path) -> None:
    """
    Remove all deployed mod files from game directory.

    MODS_DIR: Directory containing previously synced mods
    """
    svc = ctx.obj["service"]
    try:
        removed = svc.undeploy(mods_dir)
    except StateError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    if removed == 0:
        console.print("[yellow]No deployment found. Nothing to undeploy.[/yellow]")
    else:
        console.print(f"\n[green]Removed {removed} files. Game directory restored.[/green]")


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
    svc = ctx.obj["service"]
    try:
        result = svc.add_mod(
            mod_url, mods_dir,
            file_id=file_id,
            no_load_order=no_load_order,
            on_progress=_cli_progress,
        )
    except (NexusAPIError, ModParseError, StateError) as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    if not result.success:
        console.print(f"[red]Error:[/red] {result.error}")
        sys.exit(1)

    console.print(f"[green]Added '{result.mod_name}' as manual mod (phase 999).[/green]")


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
    svc = ctx.obj["service"]
    try:
        synthetic_id = svc.add_local(name, mods_dir, no_load_order=no_load_order)
    except StateError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    console.print(
        f"[green]Registered '{name}' as manual mod "
        f"(ID: {synthetic_id}, phase 999).[/green]"
    )


@main.group(name="track-sync")
def track_sync_group():
    """Manage Nexus Mods tracked-mod sync."""
    pass


@track_sync_group.command()
@click.argument("mods_dir", type=click.Path(exists=True, path_type=Path))
@click.pass_context
def enable(ctx: click.Context, mods_dir: Path) -> None:
    """Enable automatic tracked-mod sync for this collection."""
    svc = ctx.obj["service"]
    try:
        tracked, untracked = svc.track_sync_enable(mods_dir)
        console.print("[green]Track sync enabled.[/green]")
        console.print(f"Tracked {tracked}, untracked {untracked} on Nexus.")
    except (NexusAPIError, StateError) as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


@track_sync_group.command()
@click.argument("mods_dir", type=click.Path(exists=True, path_type=Path))
@click.pass_context
def disable(ctx: click.Context, mods_dir: Path) -> None:
    """Disable automatic tracked-mod sync. Existing tracked mods are unchanged."""
    svc = ctx.obj["service"]
    try:
        svc.track_sync_disable(mods_dir)
        console.print("[green]Track sync disabled.[/green] Existing tracked mods on Nexus are unchanged.")
    except StateError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


@track_sync_group.command()
@click.argument("mods_dir", type=click.Path(exists=True, path_type=Path))
@click.pass_context
def push(ctx: click.Context, mods_dir: Path) -> None:
    """One-shot sync of tracked mods (ignores enable/disable state)."""
    svc = ctx.obj["service"]
    try:
        tracked, untracked = svc.track_sync_push(mods_dir)
        console.print(f"Tracked {tracked}, untracked {untracked} on Nexus.")
    except (NexusAPIError, StateError) as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


@main.command()
@click.argument("mods_dir", type=click.Path(exists=True, path_type=Path))
@click.option("--port", type=int, default=5000, help="Port to serve on")
@click.pass_context
def serve(ctx: click.Context, mods_dir: Path, port: int) -> None:
    """Launch the web UI for managing mods."""
    from .web import create_and_run

    api_key = ctx.obj["api_key"]
    console.print(f"[bold]Starting web UI on http://127.0.0.1:{port}[/bold]")
    console.print(f"[dim]Mods directory: {mods_dir}[/dim]")
    create_and_run(api_key=api_key, mods_dir=mods_dir, port=port)


if __name__ == "__main__":
    main()
