"""Command-line interface for nexus-collection-dl."""

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

import click
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .api import (
    NexusAPIError,
    redact_encoded_secret_forms,
    redact_literal_values,
)
from .collection import CollectionParseError, ModParseError
from .deploy import DeployError, UndeployError
from .downloader import DownloadError
from .handler import (
    DESKTOP_ENTRY_NAME,
    HandlerError,
    NxmRoutingError,
    add_mapping,
    append_nxm_log,
    notification_daemon_available,
    notify as handler_notify,
    nxm_log_path,
    remove_mapping,
    sanitize_message,
)
from .handler import DesktopInstaller
from .keystore import (
    KeyStoreError,
    api_key_path,
    remove_api_key,
    resolve_api_key,
    store_api_key,
)
from .nxm import NxmCollectionLinkError, NxmParseError
from .service import ModManagerService, PendingDownload, _select_mod_file
from .state import StateError
from .version_check import check_for_update
from . import __version__

console = Console()

# Subcommands invoked by the desktop handler (nxm:// clicks) or used to
# configure it: they must stay non-interactive and quiet - no banner, no
# update check, no prompts - so a browser-launched process terminates with
# useful output and failure codes instead of hanging on a prompt.
_QUIET_SUBCOMMANDS = frozenset({"nxm", "handler"})


@click.group()
@click.option(
    "--api-key",
    envvar="NEXUS_API_KEY",
    help=(
        "Nexus Mods API key. Falls back to NEXUS_API_KEY, then to the "
        "key stored with 'nexus-dl handler set-api-key'."
    ),
)
@click.option(
    "--free",
    is_flag=True,
    hidden=True,
    help="Force free-user mode (for testing)",
)
@click.option(
    "--skip-update-check",
    is_flag=True,
    hidden=True,
    help="Skip automatic version check",
)
@click.pass_context
def main(ctx: click.Context, api_key: str | None, free: bool, skip_update_check: bool) -> None:
    """Download mod collections from Nexus Mods."""
    quiet = ctx.invoked_subcommand in _QUIET_SUBCOMMANDS
    if not quiet:
        update_msg = check_for_update()
        if update_msg:
            console.print(f"[yellow]{update_msg}[/yellow]")
        else:
            console.print(f"[dim]nexus-dl v{__version__}[/dim]")

    # Key resolution precedence lives in keystore.resolve_api_key:
    # explicit flag > NEXUS_API_KEY env > stored key. The option's
    # envvar wiring hides where the value came from, so click's
    # parameter source distinguishes the flag from the environment and
    # the resolver re-reads the environment itself. Desktop-launched
    # nxm:// clicks have no shell environment; the stored key is what
    # authenticates them.
    explicit = (
        api_key
        if ctx.get_parameter_source("api_key") is click.ParameterSource.COMMANDLINE
        else None
    )
    try:
        resolved_key, key_source = resolve_api_key(explicit)
    except KeyStoreError as e:
        # A broken key store must not break every command: warn and
        # continue keyless. 'handler remove-api-key' can still clean up.
        console.print(f"[yellow]Warning:[/yellow] {escape(str(e))}")
        resolved_key, key_source = None, None

    ctx.ensure_object(dict)
    ctx.obj["api_key"] = resolved_key
    ctx.obj["api_key_source"] = key_source
    ctx.obj["force_free"] = free
    if ctx.invoked_subcommand == "nxm":
        # Deferred construction: the nxm command builds the service inside
        # its own guarded flow, so a construction-time failure also writes
        # the redacted nxm status-log line and desktop notification
        # instead of escaping this callback as a raw traceback with no
        # visible outcome. Every other command keeps the eager behavior.
        ctx.obj["service"] = None
        ctx.obj["service_factory"] = lambda: ModManagerService(
            resolved_key, force_free=free
        )
    else:
        ctx.obj["service"] = ModManagerService(resolved_key, force_free=free)

    if not skip_update_check and not quiet:
        from .updater import check_and_prompt_update
        check_and_prompt_update(console)


def _cli_progress(event: str, pct: float, msg: str) -> None:
    """Print progress messages to Rich console."""
    if event == "download":
        return  # Rich progress bar in downloader.py handles this
    console.print(f"[dim]{msg}[/dim]")


def _print_pending_downloads(pending: list[PendingDownload], mods_dir: Path) -> None:
    """Print a table of pending downloads with browser URLs."""
    table = Table(title="Pending Downloads (manual)")
    table.add_column("Mod", style="cyan")
    table.add_column("Filename", style="green")
    table.add_column("Size", style="dim")
    table.add_column("URL")

    for p in pending:
        size_str = _format_size(p.size_bytes) if p.size_bytes else "-"
        table.add_row(p.mod_name[:40], p.filename, size_str, p.browser_url)

    console.print(table)
    console.print(
        f"\n[yellow]Free account detected.[/yellow] Download the files above through your browser, "
        f"save them to [cyan]{mods_dir}[/cyan], then run:"
    )
    console.print(f"  [bold]nexus-dl import {mods_dir}[/bold]\n")


def _format_size(size_bytes: int) -> str:
    """Format byte size to human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


@main.command()
@click.argument("collection_url")
@click.argument("mods_dir", type=click.Path(path_type=Path))
@click.option("--skip-optional", is_flag=True, help="Skip optional mods")
@click.option("--no-load-order", is_flag=True, help="Skip load order generation")
@click.option("--no-extract", is_flag=True, help="Keep downloaded archives without extracting")
@click.pass_context
def sync(
    ctx: click.Context,
    collection_url: str,
    mods_dir: Path,
    skip_optional: bool,
    no_load_order: bool,
    no_extract: bool,
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
            no_extract=no_extract,
            on_progress=_cli_progress,
        )
    except (NexusAPIError, CollectionParseError, StateError) as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    if result.collection_dir:
        console.print(f"[dim]Collection directory: {result.collection_dir}[/dim]")

    if result.errors:
        for err in result.errors:
            console.print(f"[red]Error:[/red] {err}")

    if result.pending_downloads:
        # Point users at the per-collection subdirectory the state lives in,
        # not the parent mods directory. (The update command passes its
        # MODS_DIR directly to service.update, which operates on that
        # directory in place, so its callsite is already correct.)
        _print_pending_downloads(
            result.pending_downloads, result.collection_dir or mods_dir
        )
    else:
        console.print(
            f"\n[green]Successfully synced {result.mods_downloaded} mods "
            f"({result.mods_extracted} extracted)![/green]"
        )
        if result.load_order_files:
            for f in result.load_order_files:
                console.print(f"  [green]Wrote[/green] {f}")
        if result.tracked or result.untracked:
            console.print(f"[dim]Tracked {result.tracked}, untracked {result.untracked} on Nexus.[/dim]")

    if result.conflicts:
        console.print(f"\n[yellow]{len(result.conflicts)} file conflict(s) (last mod wins):[/yellow]")
        for c in result.conflicts[:20]:
            console.print(f"  [dim]{c.file_path}:[/dim] [bold]{c.winner}[/bold] overwrote {c.loser}")
        if len(result.conflicts) > 20:
            console.print(f"  [dim]... and {len(result.conflicts) - 20} more[/dim]")


@main.command()
@click.argument("mods_dir", type=click.Path(exists=True, path_type=Path))
@click.option("--skip-optional", is_flag=True, help="Skip optional mods")
@click.option("--dry-run", is_flag=True, help="Show what would be updated without downloading")
@click.option("--no-load-order", is_flag=True, help="Skip load order generation")
@click.option("--no-extract", is_flag=True, help="Keep downloaded archives without extracting")
@click.pass_context
def update(
    ctx: click.Context,
    mods_dir: Path,
    skip_optional: bool,
    dry_run: bool,
    no_load_order: bool,
    no_extract: bool,
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
            no_extract=no_extract,
            on_progress=_cli_progress,
        )
    except (NexusAPIError, CollectionParseError, StateError) as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    if not result.to_install and not result.to_update:
        console.print("[green]Everything is up to date![/green]")
        return

    if result.pending_downloads:
        _print_pending_downloads(result.pending_downloads, mods_dir)
    else:
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
        "pending_download": "[yellow]Pending download[/yellow]",
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

        # Load manifest choices for FOMOD filtering
        manifest_choices = {}
        if state.manifest_data:
            from .manifest import CollectionManifest
            manifest = CollectionManifest.from_dict(state.manifest_data)
            manifest_choices = manifest.mod_choices
        plan = classify_files(mods_dir, state.game_domain, mod_choices=manifest_choices)
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
    except (StateError, NexusAPIError, DeployError) as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    if result.errors:
        for err in result.errors:
            console.print(f"[red]Error:[/red] {err}")

    if result.conflicts:
        console.print(f"\n[yellow]Conflicts ({len(result.conflicts)}):[/yellow]")
        for conflict in result.conflicts[:10]:
            console.print(f"  [yellow]![/yellow] {conflict}")
        if len(result.conflicts) > 10:
            console.print(f"  ... and {len(result.conflicts) - 10} more")

    if result.warnings:
        for w in result.warnings:
            console.print(f"  [yellow]Warning:[/yellow] {w}")

    if result.errors:
        # Partial deployment: never report a clean success. State keeps the
        # refused/failed entries so deploy/undeploy can be retried.
        console.print(
            f"\n[red]Deploy incomplete: {result.deployed_count} file(s) deployed, "
            f"{len(result.errors)} error(s).[/red]"
        )
        sys.exit(1)

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
    except UndeployError as e:
        # Partial undeploy: show exactly what was refused/failed. State keeps
        # those entries, so undeploy can be re-run after fixing them.
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
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
@click.option("--no-extract", is_flag=True, help="Keep downloaded archives without extracting")
@click.pass_context
def add(
    ctx: click.Context,
    mod_url: str,
    mods_dir: Path,
    file_id: int | None,
    no_load_order: bool,
    no_extract: bool,
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
            no_extract=no_extract,
            on_progress=_cli_progress,
        )
    except (NexusAPIError, ModParseError, StateError) as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    if not result.success:
        console.print(f"[red]Error:[/red] {result.error}")
        sys.exit(1)

    if result.pending_download:
        p = result.pending_download
        console.print(f"[yellow]Free account - download manually:[/yellow]")
        console.print(f"  [cyan]{p.mod_name}[/cyan]: {p.browser_url}")
        console.print(f"\nSave to [cyan]{mods_dir}[/cyan], then run:")
        console.print(f"  [bold]nexus-dl import {mods_dir}[/bold]")
    else:
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


@main.command(name="import")
@click.argument("mods_dir", type=click.Path(exists=True, path_type=Path))
@click.option("--no-load-order", is_flag=True, help="Skip load order regeneration")
@click.option("--no-extract", is_flag=True, help="Keep downloaded archives without extracting")
@click.pass_context
def import_cmd(
    ctx: click.Context,
    mods_dir: Path,
    no_load_order: bool,
    no_extract: bool,
) -> None:
    """
    Import manually downloaded files for pending mods.

    MODS_DIR: Directory containing previously synced mods and downloaded files
    """
    svc = ctx.obj["service"]
    try:
        result = svc.import_downloads(
            mods_dir,
            no_load_order=no_load_order,
            no_extract=no_extract,
            on_progress=_cli_progress,
        )
    except StateError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    if result.errors:
        for err in result.errors:
            console.print(f"[red]Error:[/red] {err}")

    if result.matched == 0 and result.still_pending > 0:
        console.print(f"[yellow]No matching files found.[/yellow] {result.still_pending} mods still pending.")
        console.print("Make sure downloaded files are in the mods directory with their original filenames.")
    else:
        console.print(
            f"\n[green]Imported {result.matched} mods "
            f"({result.extracted} extracted).[/green]"
        )
        if result.still_pending > 0:
            console.print(f"[yellow]{result.still_pending} mods still pending download.[/yellow]")
        if result.load_order_files:
            for f in result.load_order_files:
                console.print(f"  [green]Wrote[/green] {f}")


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
    force_free = ctx.obj.get("force_free", False)
    console.print(f"[bold]Starting web UI on http://127.0.0.1:{port}[/bold]")
    console.print(f"[dim]Mods directory: {mods_dir}[/dim]")
    if force_free:
        console.print(f"[yellow]Free-user mode forced[/yellow]")
    create_and_run(api_key=api_key, mods_dir=mods_dir, port=port, force_free=force_free)


def _notify_body(text: str) -> str:
    """First line of a URL-sanitized message, safe for a desktop popup."""
    cleaned = sanitize_message(str(text)).strip()
    first = cleaned.splitlines()[0] if cleaned else ""
    return first[:200] or "See the terminal output for details."


# Static reason codes for the nxm status log. Deliberately fixed
# strings, never exception text: a log line must not carry free-form
# message content that could hold a reflected URL or credential.
# Order matters: subclasses are checked before their base classes.
_NXM_REASON_BY_TYPE = (
    (NxmCollectionLinkError, "collection_link"),
    (NxmParseError, "parse"),
    (NxmRoutingError, "routing"),
    (HandlerError, "handler"),
    (NexusAPIError, "api"),
    (DownloadError, "download"),
    (StateError, "state"),
)


def _nxm_reason(exc: BaseException) -> str:
    """Map an exception to its static nxm-log reason code."""
    for cls, code in _NXM_REASON_BY_TYPE:
        if isinstance(exc, cls):
            return code
    return "error"


def _nxm_ids(result) -> str:
    """Safe 'mod=N file=N ' prefix for a log line ('' when unknown)."""
    if result.mod_id is not None and result.file_id is not None:
        return f"mod={result.mod_id} file={result.file_id} "
    return ""


def _log_nxm(outcome: str, detail: str = "") -> None:
    """Record one status line in the nxm log; must never fail the command."""
    try:
        append_nxm_log(outcome, detail)
    except Exception:
        pass  # best-effort logging: a broken log must never break nxm


def _boundary_secrets(ctx: click.Context, uri: str) -> tuple:
    """Literal secrets to redact from text leaving the nxm error boundary.

    Query extraction mirrors the nxm parser's acceptance rules rather
    than parse_qs: parameter names are percent-decoded once and
    case-folded (so 'KEY=' and '%6bey=' are both the 'key' parameter),
    and values are percent-decoded once while a literal '+' stays a '+'
    (parse_qs would turn it into a space, putting a value that is not
    the real credential in scope and leaving the real one out). Both
    the raw and the decoded value join the scope. Values are never
    displayed, only used for replacement.
    """
    base: list = []
    api_key = (ctx.obj or {}).get("api_key")
    if api_key:
        base.append(api_key)
    try:
        query = urlsplit(uri).query
    except ValueError:
        query = ""
    for segment in query.split("&"):
        if not segment or "=" not in segment:
            continue
        raw_name, raw_value = segment.split("=", 1)
        if unquote(raw_name).lower() not in ("key", "expires"):
            continue
        for value in (raw_value, unquote(raw_value)):
            if value:
                base.append(value)
    unique: list = []
    for value in base:
        if value not in unique:
            unique.append(value)
    return tuple(unique)


# Floor for BARE-LITERAL prose replacement at typed-error boundaries
# only. Real API keys and link credentials are long opaque strings (the
# key store enforces the same 8-character minimum), while replacing a
# one-character literal would mangle ordinary guidance words like
# "link". This exemption NEVER applies to percent-encoded runs or to
# occurrences inside URI query strings, where a short credential cannot
# be ordinary prose. Accepted residual risk (see _boundary_redact): a
# short-but-valid credential reflected bare in prose survives
# typed-branch redaction; the catch-all branch applies no floor.
_MIN_REDACT_SECRET_LEN = 8

# Any-scheme URI, for confining short-secret bare-literal redaction to
# query strings.
_ANY_URI_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://\S+")


def _redact_secrets_in_uri_queries(text: str, secrets: tuple) -> str:
    """Bare-literal redaction of short scoped secrets inside URI queries.

    Only the part after the first '?' of each URI is touched; URIs
    without a query are left alone. Inside a query string even a short
    credential cannot be ordinary prose, so the length floor does not
    apply there.
    """

    def scrub(match: "re.Match[str]") -> str:
        uri = match.group(0)
        head, sep, query = uri.partition("?")
        if not sep:
            return uri
        for value in secrets:
            query = re.sub(
                re.escape(value), "[redacted]", query, flags=re.IGNORECASE
            )
        return head + sep + query

    return _ANY_URI_RE.sub(scrub, text)


def _boundary_redact(
    ctx: click.Context,
    uri: str,
    text: str,
    strip_uris: bool = True,
    min_secret_len: int = 0,
) -> str:
    """Full redaction scope for text leaving through the nxm boundary.

    Every scoped secret (API key, link key/expires in raw and decoded
    form) is redacted in raw and percent-encoded shapes. When
    ``min_secret_len`` is set (typed domain-error branches), secrets
    shorter than it are exempted from bare-literal prose replacement
    only: they are still redacted in percent-encoded runs and inside
    URI query strings. Typed branches also skip URI stripping because
    their messages are redacted at the source and may deliberately
    carry a redacted API URL or the nxm:// scheme name in guidance.
    """
    secrets = _boundary_secrets(ctx, uri)
    long_secrets = tuple(s for s in secrets if len(s) >= min_secret_len)
    short_secrets = tuple(s for s in secrets if len(s) < min_secret_len)
    out = redact_literal_values(text, long_secrets)
    if short_secrets:
        out = redact_encoded_secret_forms(out, short_secrets)
        out = _redact_secrets_in_uri_queries(out, short_secrets)
    return sanitize_message(out) if strip_uris else out


@main.command()
@click.argument("uri", metavar="NXM_URI")
@click.option(
    "--mods-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Collection directory to download into (overrides the game mapping)",
)
@click.option(
    "--output",
    "output_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Save the verified archive to this directory without touching any collection",
)
@click.option("--no-extract", is_flag=True, help="Keep the downloaded archive without extracting")
@click.option("--no-load-order", is_flag=True, help="Skip load order regeneration")
@click.option(
    "--notify/--no-notify",
    default=None,
    help="Desktop notification (default: only when not attached to a terminal)",
)
@click.pass_context
def nxm(
    ctx: click.Context,
    uri: str,
    mods_dir: Path | None,
    output_dir: Path | None,
    no_extract: bool,
    no_load_order: bool,
    notify: bool | None,
) -> None:
    """
    Handle an nxm:// download link from the Nexus website.

    NXM_URI: link from 'Mod manager download', usually passed automatically
    by the desktop handler installed with 'nexus-dl handler install'.
    Link credentials are used for the request only and are never stored.
    """
    # Notification policy: an explicit --notify/--no-notify flag wins;
    # otherwise notify unless stdout is a terminal, so a paste-run in a
    # terminal prints inline instead of popping up. The desktop-handler
    # path is deterministic regardless of stdout: its Exec line passes
    # --notify and the entry runs headless (Terminal=false), so every
    # launched outcome notifies. Do NOT infer "no terminal" from the
    # launch context: the old Terminal=true entry made a launched run's
    # stdout a TTY, and the default then suppressed the very
    # notification the click needed (disproven by live smoke testing).
    want_notify = notify if notify is not None else (not sys.stdout.isatty())

    if mods_dir is not None and output_dir is not None:
        console.print("[red]Error:[/red] --mods-dir and --output cannot be combined.")
        _log_nxm("error", "reason=usage")
        if want_notify:
            handler_notify(
                "nexus-dl: download failed",
                "--mods-dir and --output cannot be combined.",
            )
        sys.exit(1)

    def fail(message: str) -> None:
        # escape(): messages may contain bracketed text (e.g. "[redacted]")
        # that must print literally instead of being parsed as rich markup.
        console.print(f"[red]Error:[/red] {escape(message)}")
        if want_notify:
            handler_notify("nexus-dl: download failed", _notify_body(message))
        sys.exit(1)

    try:
        # The group callback defers service construction for this command
        # so a construction failure lands in the guarded handlers below.
        svc = ctx.obj["service"]
        if svc is None:
            svc = ctx.obj["service_factory"]()
        result = svc.add_nxm(
            uri,
            mods_dir=mods_dir,
            output_dir=output_dir,
            no_load_order=no_load_order,
            no_extract=no_extract,
            on_progress=_cli_progress,
        )
    except NxmCollectionLinkError as e:
        # Recognized collection shape: helpful guidance, no unsafe URL.
        console.print(f"[yellow]{escape(str(e))}[/yellow]")
        _log_nxm("error", "reason=collection_link")
        if want_notify:
            handler_notify(
                "nexus-dl: collection link",
                "Collection nxm:// links are not supported. Use nexus-dl sync with the collection URL.",
            )
        sys.exit(1)
    except (NxmParseError, NxmRoutingError, HandlerError) as e:
        _log_nxm("error", f"reason={_nxm_reason(e)}")
        # Typed errors are redacted at the source, but a reflected
        # credential in any case/encoding variant is caught here too.
        # Short values stay out of scope so deliberate guidance prose
        # (which may contain those letters) is not mangled.
        fail(_boundary_redact(ctx, uri, str(e), strip_uris=False,
                              min_secret_len=_MIN_REDACT_SECRET_LEN))
    except (NexusAPIError, DownloadError, StateError) as e:
        _log_nxm("error", f"reason={_nxm_reason(e)}")
        fail(_boundary_redact(ctx, uri, str(e), strip_uris=False,
                              min_secret_len=_MIN_REDACT_SECRET_LEN))
    except Exception as e:
        # Outer boundary: any failure not matched above (including the
        # deferred service construction) still leaves one redacted status
        # line and the notification instead of an invisible traceback.
        # The log carries a static reason code only. Free-form text gets
        # the full scope: literal secrets in every case/encoding variant,
        # then URL/URI stripping covering ANY scheme with a query and
        # nxm:// with or without one.
        _log_nxm("error", "reason=error")
        detail = _boundary_redact(ctx, uri, str(e)).strip()
        fail(detail or f"Unexpected failure ({type(e).__name__}).")

    if not result.success:
        reason = "re_click_required" if result.re_click_required else "rejected"
        _log_nxm("error", f"{_nxm_ids(result)}reason={reason}")
        if result.error:
            console.print(f"[red]Error:[/red] {escape(result.error)}")
        if result.re_click_required:
            console.print(
                "[yellow]Click 'Mod manager download' on the Nexus website "
                "again, then retry.[/yellow]"
            )
        if want_notify:
            handler_notify("nexus-dl: download failed", _notify_body(result.error))
        sys.exit(1)

    if result.status == "already_downloaded":
        console.print(
            f"[green]'{escape(str(result.mod_name))}' (file {result.file_id}) is already "
            f"downloaded in this collection.[/green]"
        )
        summary = f"Already downloaded: {result.mod_name}"
    elif result.status == "extracted_from_archive":
        console.print(
            f"[green]Finished '{escape(str(result.mod_name))}' (file "
            f"{result.file_id}) by extracting the retained archive; no "
            "download was needed.[/green]"
        )
        summary = f"Extracted {result.mod_name} from the retained archive"
    elif result.status == "saved_to_output":
        console.print(f"[green]Saved {escape(str(result.output_path))}[/green]")
        summary = f"Saved {result.filename}"
    else:
        where = (
            f" into {escape(str(result.collection_dir))}" if result.collection_dir else ""
        )
        console.print(
            f"[green]Downloaded '{escape(str(result.mod_name))}'{where}.[/green]"
        )
        if result.extracted:
            console.print(
                "[dim]Archive extracted. Run 'nexus-dl deploy' when ready; "
                "nothing was deployed automatically.[/dim]"
            )
        summary = f"Downloaded {result.mod_name}"
    # Non-fatal follow-up failures on a completed download (e.g. load
    # order): truthful warnings, still a success exit.
    for warning in result.warnings:
        console.print(f"[yellow]Warning:[/yellow] {escape(str(warning))}")
    # One status line for the after-the-fact trail: ids and the fixed
    # status token only, never names, URIs, or credentials.
    _log_nxm(
        "warn" if result.warnings else "success",
        f"{_nxm_ids(result)}status={result.status}",
    )
    if want_notify:
        handler_notify("nexus-dl: download complete", summary)


@main.group(name="handler")
def handler_group():
    """Configure nxm:// link handling (game mappings and desktop registration)."""


@handler_group.command(name="map")
@click.argument("game")
@click.argument(
    "collection_dir",
    metavar="COLLECTION_DIR",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
def handler_map(game: str, collection_dir: Path) -> None:
    """Map GAME to a synced COLLECTION_DIR for nxm:// downloads."""
    try:
        add_mapping(game, collection_dir)
    except HandlerError as e:
        console.print(f"[red]Error:[/red] {escape(str(e))}")
        sys.exit(1)
    console.print(
        f"[green]Mapped '{escape(game.strip().lower())}' to "
        f"{escape(str(collection_dir))}[/green]"
    )


@handler_group.command(name="unmap")
@click.argument("game")
def handler_unmap(game: str) -> None:
    """Remove all mappings for GAME."""
    try:
        removed = remove_mapping(game)
    except HandlerError as e:
        console.print(f"[red]Error:[/red] {escape(str(e))}")
        sys.exit(1)
    if removed:
        console.print(f"[green]Removed mapping for '{escape(game.strip().lower())}'.[/green]")
    else:
        console.print(f"[yellow]No mapping exists for '{escape(game.strip().lower())}'.[/yellow]")


@handler_group.command(name="set-api-key")
def handler_set_api_key() -> None:
    """Store the Nexus API key for browser-launched downloads.

    The key is read from hidden input (never a command-line argument,
    never echoed) and stored with owner-only permissions, so nxm://
    clicks authenticate even though desktop-launched processes cannot
    see NEXUS_API_KEY from your shell startup files.
    """
    # Hidden prompt: the value never appears on screen, in argv, or in
    # shell history. click re-prompts on empty input and aborts on EOF.
    key = click.prompt("Nexus API key", hide_input=True)
    try:
        path = store_api_key(key)
    except KeyStoreError as e:
        console.print(f"[red]Error:[/red] {escape(str(e))}")
        sys.exit(1)
    console.print(f"[green]API key stored in {escape(str(path))} (0600).[/green]")
    console.print(
        "[dim]'Mod manager download' clicks now authenticate with this "
        "key. 'nexus-dl handler status' shows the active key source; "
        "'nexus-dl handler remove-api-key' deletes it.[/dim]"
    )


@handler_group.command(name="remove-api-key")
def handler_remove_api_key() -> None:
    """Delete the stored API key.

    Removes only the key file; game mappings and the desktop handler
    record are left untouched.
    """
    try:
        removed = remove_api_key()
    except KeyStoreError as e:
        console.print(f"[red]Error:[/red] {escape(str(e))}")
        sys.exit(1)
    if removed:
        console.print(
            f"[green]Removed the stored API key from "
            f"{escape(str(api_key_path()))}.[/green]"
        )
    else:
        console.print("[yellow]No stored API key to remove.[/yellow]")


@handler_group.command(name="status")
@click.pass_context
def handler_status(ctx: click.Context) -> None:
    """Show game mappings and desktop handler status."""
    try:
        info = DesktopInstaller().status()
    except HandlerError as e:
        console.print(f"[red]Error:[/red] {escape(str(e))}")
        sys.exit(1)

    if info["mappings"]:
        table = Table(title="Game mappings")
        table.add_column("Game", style="cyan")
        table.add_column("Collection directory", style="green")
        for game, dirs in sorted(info["mappings"].items()):
            for directory in dirs:
                table.add_row(game, directory)
        console.print(table)
    else:
        console.print(
            "No game mappings configured. Add one with:\n"
            "  nexus-dl handler map GAME COLLECTION_DIR"
        )

    if info["entry_installed"]:
        console.print(
            f"[bold]Desktop entry:[/bold] {escape(str(info['entry_path']))} "
            "[green](installed)[/green]"
        )
    else:
        console.print(
            f"[bold]Desktop entry:[/bold] not installed "
            f"({escape(str(info['entry_path']))}). "
            "Install with 'nexus-dl handler install'."
        )
    if info["executable"]:
        if info["executable_exists"]:
            console.print(f"[bold]Executable:[/bold] {escape(str(info['executable']))}")
        else:
            console.print(
                f"[bold]Executable:[/bold] [red]{escape(str(info['executable']))} no longer "
                "exists[/red] - the virtualenv may have moved or been "
                "recreated. Re-run 'nexus-dl handler install'."
            )
    if info["is_default"]:
        console.print(f"[bold]Default nxm handler:[/bold] [green]{DESKTOP_ENTRY_NAME}[/green]")
    elif info["current_default"]:
        console.print(
            f"[bold]Default nxm handler:[/bold] "
            f"{escape(str(info['current_default']))} (not nexus-dl)"
        )
    elif info["xdg_mime_available"]:
        console.print("[bold]Default nxm handler:[/bold] none")
    else:
        console.print(
            "[bold]Default nxm handler:[/bold] unknown (xdg-mime not available)"
        )
    if info["previous_default"]:
        console.print(
            f"[dim]Previous default (restored on uninstall): "
            f"{escape(str(info['previous_default']))}[/dim]"
        )

    # API key availability: source only, never the value. The main
    # group callback resolved it once, so this reports exactly what the
    # commands in this run will use.
    source = (ctx.obj or {}).get("api_key_source")
    if source == "flag":
        console.print("[bold]API key:[/bold] available (from the --api-key flag)")
    elif source == "env":
        console.print("[bold]API key:[/bold] available (from NEXUS_API_KEY)")
    elif source == "stored":
        console.print(
            f"[bold]API key:[/bold] available (stored: "
            f"{escape(str(api_key_path()))})"
        )
    else:
        console.print(
            "[bold]API key:[/bold] not set - store one with 'nexus-dl handler "
            "set-api-key' for click-through downloads, export NEXUS_API_KEY, "
            "or pass --api-key"
        )


@handler_group.command(name="install")
def handler_install() -> None:
    """Install the user desktop entry so nxm:// links open nexus-dl.

    User-level only (XDG applications dir); remembers the previous
    default handler so uninstall can restore it. Re-running is
    idempotent: the entry is always rewritten, so stale entries from
    older versions are replaced.
    """
    try:
        entry = DesktopInstaller().install()
    except (HandlerError, OSError) as e:
        console.print(f"[red]Error:[/red] {escape(str(e))}")
        sys.exit(1)
    console.print(f"[green]NXM handler installed: {escape(str(entry))}[/green]")
    console.print(
        "[dim]'Mod manager download' links on Nexus Mods now open nexus-dl. "
        "Map each game first with 'nexus-dl handler map GAME COLLECTION_DIR'.[/dim]"
    )
    # Best-effort notification self-test: launched clicks run headless and
    # report outcomes as notifications, so a session without a reachable
    # daemon deserves a visible warning pointing at the log fallback. A
    # missing probe tool skips silently and never fails the install.
    if notification_daemon_available() is False:
        console.print(
            "[yellow]Warning:[/yellow] no notification daemon was detected "
            "on this session, so click outcomes may not be visible as "
            "notifications. Each click still appends a status line to "
            f"{escape(str(nxm_log_path()))}; check that log after clicking."
        )


@handler_group.command(name="uninstall")
def handler_uninstall() -> None:
    """Remove the desktop entry; restores the previous default if ours."""
    try:
        message = DesktopInstaller().uninstall()
    except (HandlerError, OSError) as e:
        console.print(f"[red]Error:[/red] {escape(str(e))}")
        sys.exit(1)
    console.print(f"[green]{escape(message)}[/green]")


if __name__ == "__main__":
    main()
