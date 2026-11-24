"""Command-line interface for nexus-collection-dl."""

import sys
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import __version__
from .api import NexusAPIError
from .collection import CollectionParseError, ModParseError
from .deploy import DeployError, UndeployError
from .downloader import DownloadError
from .errmsg import exception_reason
from .extractor import ExtractionError
from .fomod_select import PickError
from .handler import (
    DESKTOP_ENTRY_NAME,
    DesktopInstaller,
    HandlerError,
    NxmRoutingError,
    add_mapping,
    append_nxm_event,
    append_nxm_log,
    capability_warnings,
    notification_daemon_available,
    nxm_log_path,
    remove_mapping,
    sanitize_message,
)
from .handler import (
    notify as handler_notify,
)
from .keystore import (
    KeyStoreError,
    api_key_path,
    remove_api_key,
    resolve_api_key,
    store_api_key,
)
from .nxm import NxmCollectionLinkError, NxmParseError
from .redaction import (
    MIN_REDACT_SECRET_LEN,
    boundary_redact,
    merge_secrets,
    uri_query_secrets,
)
from .service import ModManagerService, PendingDownload
from .state import StateError
from .version_check import check_for_update

console = Console()

# Subcommands invoked by the desktop handler (nxm:// clicks) or used to
# configure it: they must stay non-interactive and quiet - no banner, no
# update check, no prompts - so a browser-launched process terminates with
# useful output and failure codes instead of hanging on a prompt. The
# interactive shell is quiet too: its own greeting replaces the banner and
# an update prompt would fight the REPL for input.
_QUIET_SUBCOMMANDS = frozenset({"nxm", "handler", "shell"})


@click.group(invoke_without_command=True)
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
@click.option(
    "--json",
    "json_mode",
    is_flag=True,
    help="Emit machine-readable JSON on stdout (must come before the subcommand)",
)
@click.version_option(
    __version__,
    "--version",
    prog_name="nexus-dl",
    message="%(prog)s v%(version)s",
)
@click.pass_context
def main(
    ctx: click.Context,
    api_key: str | None,
    free: bool,
    skip_update_check: bool,
    json_mode: bool,
) -> None:
    """Download mod collections from Nexus Mods."""
    global console
    if json_mode:
        # stdout belongs to the JSON envelope and nothing else. Every human
        # diagnostic (banner, update notice, warnings, progress prose) moves
        # to stderr so a caller can pipe stdout straight into a parser.
        console = Console(stderr=True)
        if ctx.invoked_subcommand in {"shell", "nxm", "handler"}:
            raise click.UsageError(
                "--json is not supported for "
                f"'{ctx.invoked_subcommand}': it is interactive or config-only, "
                "not a result-producing command"
            )
    quiet = json_mode or ctx.invoked_subcommand in _QUIET_SUBCOMMANDS
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
    ctx.obj["json_mode"] = json_mode
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

    if ctx.invoked_subcommand is None:
        _handle_bare_invocation(ctx, json_mode)


def _is_interactive() -> bool:
    """Can this run a REPL?

    Both streams have to be terminals. A pipe on either side means this
    is a script, a capture, or CI, not a person typing, and a REPL is
    not a thing to drop into any of those.
    """
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _handle_bare_invocation(ctx: click.Context, json_mode: bool) -> None:
    """What bare `nexus-dl` does. The front door.

    Three shapes. The middle one is why this is careful:

    - Interactive and configured: the shell is the default seat now.
    - Not interactive: usage and out. Never a REPL, never a prompt.
    - Interactive but unconfigured: the setup wizard, which detects what
      it can and writes the config the shell needs.
    """
    from .config import UserConfig

    if json_mode:
        # stdout is parsed output; an interactive shell cannot be the
        # answer to it. Same reason `--json` refuses `shell` and `nxm`.
        raise click.UsageError(
            "--json needs a command: bare invocation is interactive and "
            "this output is meant to be parsed"
        )
    if not _is_interactive():
        # The guard. A script calling bare nexus-dl gets usage, not a REPL.
        console.print(ctx.get_help())
        return

    cfg = UserConfig.load()
    if not cfg.is_configured():
        # First run. The wizard is the front door: detect what it can,
        # ask for the rest, write the config that makes the shell useful.
        ctx.invoke(
            setup,
            non_interactive=False,
            collections_root=None,
            game=(),
            install_handler=False,
        )
        return

    from .shell import run_shell

    svc = ctx.obj["service"]
    sys.exit(run_shell(svc, Path(cfg.collections_root), console=console))


def _is_json(ctx: click.Context) -> bool:
    return bool(ctx.obj and ctx.obj.get("json_mode"))


def _maybe_gc_nudge(ctx: click.Context, mods_dir: Path) -> None:
    """Point at reclaimable archive disk after a work command.

    The user owns whether to run gc; the tool owns making sure the disk
    is visible while they are working. Silent in --json mode so stdout
    stays parseable (machines read 'gc --json' for the number).
    """
    if _is_json(ctx):
        return
    from .gc import NUDGE_THRESHOLD_BYTES, nudge_message

    msg = nudge_message(mods_dir, NUDGE_THRESHOLD_BYTES)
    if msg:
        console.print(f"\n[yellow]{escape(msg)}[/yellow]")


def _fail(
    ctx: click.Context,
    exc: BaseException,
    hint: str | None = None,
    result: Any = None,
    code: str | None = None,
) -> None:
    """Report a failure and exit 1.

    In JSON mode this is the only thing written to stdout: an error envelope
    carrying a stable ``code``, the exception's real class name, its message,
    and an optional remedy. ``result`` is kept when an operation partially
    completed, so a caller gets the outcome and the detail together.

    In human mode this preserves the existing red error line plus hint.
    Never raises: this always exits.
    """
    if _is_json(ctx):
        from .jsonout import emit_failure

        emit_failure(exc, hint=hint, result=result, code=code)
    else:
        console.print(f"[red]Error:[/red] {escape(str(exc))}")
        if hint:
            console.print(hint)
    sys.exit(1)


def _emit_result(ctx: click.Context, result: Any) -> bool:
    """Emit a JSON envelope if JSON mode is on. Returns True when it did."""
    if not _is_json(ctx):
        return False
    from .jsonout import emit_result

    emit_result(result)
    return True


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
            show_progress=not _is_json(ctx),
        )
    except (NexusAPIError, CollectionParseError, StateError) as e:
        _fail(ctx, e)

    if _emit_result(ctx, result):
        return

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

    _maybe_gc_nudge(ctx, result.collection_dir or mods_dir)


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
            _fail(ctx, e)

        console.print(f"[bold]Collection:[/bold] {status.collection_name}")
        console.print(f"[bold]Current revision:[/bold] {status.installed_revision}")
        if status.latest_revision:
            console.print(f"[bold]Latest revision:[/bold] {status.latest_revision}")

        updates = [m for m in status.mods if m.status == "update_available"]
        new = [m for m in status.mods if m.status == "not_installed"]
        removed = [m for m in status.mods if m.status == "removed"]

        if _emit_result(ctx, {
            "dry_run": True,
            "status": status,
            "new": [m.name for m in new],
            "updates": [m.name for m in updates],
            "removed": [m.name for m in removed],
        }):
            return

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
            show_progress=not _is_json(ctx),
        )
    except (NexusAPIError, CollectionParseError, StateError) as e:
        _fail(ctx, e)

    if _emit_result(ctx, result):
        return

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
        console.print("\n[green]Update complete![/green]")

    _maybe_gc_nudge(ctx, mods_dir)


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
        _fail(ctx, e, hint="Run 'sync' first to download a collection.")

    if _emit_result(ctx, result):
        return

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
        _fail(ctx, e)

    if _emit_result(ctx, {"load_order_files": files}):
        return

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
            _fail(ctx, e)

        if game_dir is None and state.game_dir:
            game_dir = Path(state.game_dir)
        if game_dir is None:
            game_dir = find_game_dir(state.game_domain)
        if game_dir is None:
            _fail(ctx, FileNotFoundError("Could not find game directory."))

        # Load manifest choices for FOMOD filtering
        manifest_choices = {}
        if state.manifest_data:
            from .manifest import CollectionManifest
            manifest = CollectionManifest.from_dict(state.manifest_data)
            manifest_choices = manifest.mod_choices
        # The same picks and recorded configs the real deploy uses: a dry
        # run has to describe that deployment, not a different one.
        fomod_picks = {
            mod.file_id: mod.fomod_picks
            for mod in state.mods.values()
            if mod.fomod_picks
        }
        fomod_configs = {
            mod.file_id: {**mod.fomod_config, "mod_id": mod.mod_id}
            for mod in state.mods.values()
            if mod.fomod_config
        }
        plan = classify_files(
            mods_dir,
            state.game_domain,
            mod_choices=manifest_choices,
            fomod_picks=fomod_picks,
            fomod_configs=fomod_configs,
            mod_names={mod.file_id: mod.name for mod in state.mods.values()},
        )
        if _emit_result(ctx, {
            "dry_run": True,
            "game_root_files": plan.root_destinations,
            "data_files": plan.data_destinations,
            "skipped": len(plan.skipped),
            "total_files": plan.total_files,
            "warnings": plan.warnings,
        }):
            return
        console.print(f"  Game root files: {plan.root_destinations}")
        console.print(f"  Data files: {plan.data_destinations}")
        console.print(f"  Skipped: {len(plan.skipped)}")
        for w in plan.warnings:
            console.print(f"  [yellow]Warning:[/yellow] {w}")
        console.print(f"\n[yellow]Would deploy {plan.total_files} files (dry run).[/yellow]")
        return

    svc = ctx.obj["service"]
    from .progress import ProgressLine

    json_mode = _is_json(ctx)
    # The write loop is the long silent phase of a deploy. One moving
    # line, suppressed entirely under --json where the output is parsed.
    line = ProgressLine(total=0, label="Deploying", enabled=not json_mode)

    def on_file_progress(done: int, total: int, detail: str) -> None:
        line.total = total
        line.update(done, detail)

    def on_progress(event: str, pct: float, msg: str) -> None:
        if event == "deploy":
            # The per-file calls drive the line, so a coarse deploy
            # message would fight it for the same row. The line says what
            # the banner said and more: where the loop is right now.
            return
        line.close()
        _cli_progress(event, pct, msg)

    try:
        result = svc.deploy(
            mods_dir,
            game_dir=game_dir,
            prefix=prefix,
            use_copy=use_copy,
            on_progress=on_progress,
            on_file_progress=on_file_progress,
        )
    except (StateError, NexusAPIError, DeployError) as e:
        line.close()
        _fail(ctx, e)
    line.close()

    if _emit_result(ctx, result):
        return

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
        console.print("\n[bold yellow]SFSE detected![/bold yellow]")
        console.print("  To launch with mods, set your Steam shortcut TARGET to:")
        console.print(f"  [cyan]{sfse_path}[/cyan]")

    _maybe_gc_nudge(ctx, mods_dir)


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
        _fail(ctx, e)
    except StateError as e:
        _fail(ctx, e)

    if _emit_result(ctx, {"removed": removed}):
        return

    if removed == 0:
        console.print("[yellow]No deployment found. Nothing to undeploy.[/yellow]")
    else:
        console.print(f"\n[green]Removed {removed} files. Game directory restored.[/green]")


def _ask_int(prompt_text: str, maximum: int, allow_none: bool) -> int | None:
    while True:
        raw = click.prompt(prompt_text, default="", show_default=False).strip()
        if not raw:
            if allow_none:
                return None
            continue
        try:
            n = int(raw)
        except ValueError:
            continue
        if 1 <= n <= maximum:
            return n


def _ask_many(prompt_text: str, maximum: int, at_least_one: bool) -> list[int]:
    while True:
        raw = click.prompt(prompt_text, default="", show_default=False).strip()
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if not parts:
            if not at_least_one:
                return []
            continue
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            continue
        if all(1 <= n <= maximum for n in nums):
            return nums


class _ClickAsker:
    """The picker's front-end on click prompts (the `add`/`import` CLI)."""

    def tell(self, text: str) -> None:
        click.echo(text)

    def choose_one(self, prompt: str, count: int, allow_none: bool) -> int | None:
        try:
            return _ask_int(prompt, count, allow_none=allow_none)
        except (KeyboardInterrupt, EOFError):
            from .fomod_select import PickAborted

            raise PickAborted from None

    def choose_many(self, prompt: str, count: int, at_least_one: bool) -> list[int]:
        try:
            return _ask_many(prompt, count, at_least_one=at_least_one)
        except (KeyboardInterrupt, EOFError):
            from .fomod_select import PickAborted

            raise PickAborted from None


def _fomod_prompt(config: Any, archive_name: str, kept: Any) -> list[Any]:
    """Ask for the FOMOD options ``kept`` does not already cover.

    Wired in as the service's prompt callback. The service has kept
    whatever still names a real option; only groups without a pick are
    asked. This is never installed for ``--json`` or a non-tty, which
    pass no callback at all and get a refusal instead.
    """
    from .fomod_select import prompt_for_picks

    return prompt_for_picks(config, archive_name, kept, _ClickAsker())


def _collect_fomod_picks(
    option_values: tuple[str, ...], config_path: Path | None
) -> list[Any] | None:
    """Combine ``--fomod-option`` flags and a ``--fomod-config`` file."""
    from .fomod_select import load_picks_file, parse_option_flags

    picks: list[Any] = []
    if option_values:
        picks.extend(parse_option_flags(option_values))
    if config_path is not None:
        picks.extend(load_picks_file(config_path))
    return picks or None


def _fomod_prompter(ctx: click.Context, picks: list[Any] | None) -> Any:
    """The prompt callback, only when there is somebody to answer."""
    if picks or _is_json(ctx) or not sys.stdin.isatty():
        return None
    return _fomod_prompt


@main.command()
@click.argument("mod_url")
@click.argument("mods_dir", type=click.Path(exists=True, path_type=Path))
@click.option("--file-id", type=int, default=None, help="Specific file ID to download")
@click.option("--no-load-order", is_flag=True, help="Skip load order regeneration")
@click.option("--no-extract", is_flag=True, help="Keep downloaded archives without extracting")
@click.option(
    "--fomod-option",
    "fomod_options",
    multiple=True,
    help="FOMOD choice as 'Group:Option' or 'Step:Group:Option' (repeatable)",
)
@click.option(
    "--fomod-config",
    "fomod_config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="JSON file of FOMOD picks for names that need quoting",
)
@click.pass_context
def add(
    ctx: click.Context,
    mod_url: str,
    mods_dir: Path,
    file_id: int | None,
    no_load_order: bool,
    no_extract: bool,
    fomod_options: tuple[str, ...],
    fomod_config_path: Path | None,
) -> None:
    """
    Add a single mod by URL.

    MOD_URL: Nexus Mods mod page URL (e.g. https://www.nexusmods.com/starfield/mods/123)
    MODS_DIR: Directory containing a previously synced collection

    A FOMOD mod asks which of its options to install. Pass the picks with
    --fomod-option/--fomod-config, or run this at a terminal and answer
    the prompts; with neither, the mod is refused rather than installed
    with every option at once.
    """
    svc = ctx.obj["service"]
    try:
        picks = _collect_fomod_picks(fomod_options, fomod_config_path)
        result = svc.add_mod(
            mod_url, mods_dir,
            file_id=file_id,
            no_load_order=no_load_order,
            no_extract=no_extract,
            on_progress=_cli_progress,
            show_progress=not _is_json(ctx),
            fomod_picks=picks,
            on_fomod_prompt=_fomod_prompter(ctx, picks),
        )
    except (NexusAPIError, ModParseError, StateError, ExtractionError, PickError) as e:
        _fail(ctx, e)
    except Exception as e:
        from .fomod_select import PickAborted

        if isinstance(e, PickAborted):
            console.print("[yellow]Cancelled; nothing installed.[/yellow]")
            sys.exit(1)
        raise

    if not result.success:
        _fail(
            ctx,
            RuntimeError(result.error or "add failed"),
            result=result,
            code="operation_failed",
        )

    if _emit_result(ctx, result):
        return

    if result.warnings:
        for w in result.warnings:
            console.print(f"  [yellow]Warning:[/yellow] {w}")

    if result.pending_download:
        p = result.pending_download
        console.print("[yellow]Free account - download manually:[/yellow]")
        console.print(f"  [cyan]{p.mod_name}[/cyan]: {p.browser_url}")
        console.print(f"\nSave to [cyan]{mods_dir}[/cyan], then run:")
        console.print(f"  [bold]nexus-dl import {mods_dir}[/bold]")
    else:
        console.print(f"[green]Added '{result.mod_name}' as manual mod (phase 999).[/green]")

    _maybe_gc_nudge(ctx, mods_dir)


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
        _fail(ctx, e)

    if _emit_result(ctx, {"mod_id": synthetic_id, "name": name}):
        return

    console.print(
        f"[green]Registered '{name}' as manual mod "
        f"(ID: {synthetic_id}, phase 999).[/green]"
    )


@main.command(name="import")
@click.argument("mods_dir", type=click.Path(exists=True, path_type=Path))
@click.option("--no-load-order", is_flag=True, help="Skip load order regeneration")
@click.option("--no-extract", is_flag=True, help="Keep downloaded archives without extracting")
@click.option(
    "--fomod-option",
    "fomod_options",
    multiple=True,
    help="FOMOD choice as 'Group:Option' or 'Step:Group:Option' (repeatable)",
)
@click.option(
    "--fomod-config",
    "fomod_config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="JSON file of FOMOD picks; add \"file\" to scope a pick to one archive",
)
@click.pass_context
def import_cmd(
    ctx: click.Context,
    mods_dir: Path,
    no_load_order: bool,
    no_extract: bool,
    fomod_options: tuple[str, ...],
    fomod_config_path: Path | None,
) -> None:
    """
    Import manually downloaded files for pending mods.

    MODS_DIR: Directory containing previously synced mods and downloaded files

    FOMOD mods in the batch need option choices. Use --fomod-config with a
    "file" key per pick when the batch holds more than one of them.
    """
    svc = ctx.obj["service"]
    try:
        picks = _collect_fomod_picks(fomod_options, fomod_config_path)
        result = svc.import_downloads(
            mods_dir,
            no_load_order=no_load_order,
            no_extract=no_extract,
            on_progress=_cli_progress,
            fomod_picks=picks,
            on_fomod_prompt=_fomod_prompter(ctx, picks),
        )
    except (StateError, ExtractionError, PickError) as e:
        _fail(ctx, e)
    except Exception as e:
        from .fomod_select import PickAborted

        if isinstance(e, PickAborted):
            console.print("[yellow]Cancelled; nothing installed.[/yellow]")
            sys.exit(1)
        raise

    if _emit_result(ctx, result):
        return

    if result.warnings:
        for w in result.warnings:
            console.print(f"  [yellow]Warning:[/yellow] {w}")

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

    _maybe_gc_nudge(ctx, mods_dir)


@main.group(name="track-sync")
def track_sync_group():
    """Manage Nexus Mods tracked-mod sync."""


@track_sync_group.command()
@click.argument("mods_dir", type=click.Path(exists=True, path_type=Path))
@click.pass_context
def enable(ctx: click.Context, mods_dir: Path) -> None:
    """Enable automatic tracked-mod sync for this collection."""
    svc = ctx.obj["service"]
    try:
        tracked, untracked = svc.track_sync_enable(mods_dir)
    except (NexusAPIError, StateError) as e:
        _fail(ctx, e)

    if _emit_result(ctx, {"tracked": tracked, "untracked": untracked}):
        return
    console.print("[green]Track sync enabled.[/green]")
    console.print(f"Tracked {tracked}, untracked {untracked} on Nexus.")


@track_sync_group.command()
@click.argument("mods_dir", type=click.Path(exists=True, path_type=Path))
@click.pass_context
def disable(ctx: click.Context, mods_dir: Path) -> None:
    """Disable automatic tracked-mod sync. Existing tracked mods are unchanged."""
    svc = ctx.obj["service"]
    try:
        svc.track_sync_disable(mods_dir)
    except StateError as e:
        _fail(ctx, e)

    if _emit_result(ctx, {"enabled": False}):
        return
    console.print("[green]Track sync disabled.[/green] Existing tracked mods on Nexus are unchanged.")


@track_sync_group.command()
@click.argument("mods_dir", type=click.Path(exists=True, path_type=Path))
@click.pass_context
def push(ctx: click.Context, mods_dir: Path) -> None:
    """One-shot sync of tracked mods (ignores enable/disable state)."""
    svc = ctx.obj["service"]
    try:
        tracked, untracked = svc.track_sync_push(mods_dir)
    except (NexusAPIError, StateError) as e:
        _fail(ctx, e)

    if _emit_result(ctx, {"tracked": tracked, "untracked": untracked}):
        return
    console.print(f"Tracked {tracked}, untracked {untracked} on Nexus.")


@main.command()
@click.argument("mods_dir", type=click.Path(exists=True, path_type=Path))
@click.option("--file-id", type=int, default=None, help="Remove by Nexus file id")
@click.option("--mod-id", type=int, default=None, help="Remove by Nexus mod id")
@click.option("--no-load-order", is_flag=True, help="Skip load order regeneration")
@click.option("--dry-run", is_flag=True, help="Show what would be removed, change nothing")
@click.option("--force", is_flag=True, help="Remove even if another mod requires it")
@click.pass_context
def remove(
    ctx: click.Context,
    mods_dir: Path,
    file_id: int | None,
    mod_id: int | None,
    no_load_order: bool,
    dry_run: bool,
    force: bool,
) -> None:
    """
    Remove a mod from the collection state and from disk.

    Exactly one of --file-id or --mod-id is required. Look ids up with
    'nexus-dl mods MODS_DIR' rather than guessing. Gone means gone: the
    state entry, the files the mod contributed, and its retained archive
    are all deleted. Re-adding the mod downloads it again. A mod another
    one requires is refused unless --force.
    """
    from .service import list_mods

    svc = ctx.obj["service"]

    if (file_id is None) == (mod_id is None):
        _fail(
            ctx,
            ValueError("exactly one of --file-id or --mod-id is required"),
            hint="Use 'nexus-dl mods MODS_DIR' to look up the id.",
        )

    if dry_run:
        try:
            candidates = [
                m for m in list_mods(mods_dir)
                if (file_id is not None and m.file_id == file_id)
                or (mod_id is not None and m.mod_id == mod_id)
            ]
        except StateError as e:
            _fail(ctx, e)
        if not candidates:
            _fail(
                ctx,
                StateError("no matching mod found"),
                hint="Use 'nexus-dl mods MODS_DIR' to look up the id.",
            )
        if _emit_result(ctx, {"dry_run": True, "mods": candidates}):
            return
        console.print(f"[yellow]Dry run:[/yellow] would remove {len(candidates)} mod(s):")
        for m in candidates:
            console.print(f"  {m.name} (file_id={m.file_id}, mod_id={m.mod_id})")
        return

    try:
        result = svc.remove_mod(
            mods_dir, file_id=file_id, mod_id=mod_id,
            no_load_order=no_load_order, force=force,
        )
    except (StateError, ValueError) as e:
        _fail(ctx, e)

    if _emit_result(ctx, result):
        return

    removed = len(result.removed_file_ids) or len(result.removed_mod_ids)
    if not removed:
        console.print("[yellow]Nothing removed:[/yellow] no matching mod found.")
    else:
        console.print(f"[green]Removed {removed} mod(s).[/green]")
    for w in result.warnings:
        console.print(f"[yellow]Warning:[/yellow] {escape(w)}")


def _set_mod_enabled(
    ctx: click.Context,
    mods_dir: Path,
    file_id: int | None,
    mod_id: int | None,
    enabled: bool,
) -> None:
    """Shared body of `enable` and `disable`. State-only; nothing deploys."""
    from .state import CollectionState, StateError

    if (file_id is None) == (mod_id is None):
        _fail(
            ctx,
            ValueError("exactly one of --file-id or --mod-id is required"),
            hint="Use 'nexus-dl mods MODS_DIR' to look up the id.",
        )
    try:
        state = CollectionState(mods_dir)
        state.load()
        hits = [
            ms
            for ms in state.mods.values()
            if (file_id is not None and ms.file_id == file_id)
            or (mod_id is not None and ms.mod_id == mod_id)
        ]
        if not hits:
            _fail(
                ctx,
                ValueError("no matching mod found"),
                hint="Use 'nexus-dl mods MODS_DIR' to look up the id.",
            )
        for ms in hits:
            ms.enabled = enabled
        state.save()
    except StateError as e:
        _fail(ctx, e)

    verb = "Enabled" if enabled else "Disabled"
    if _emit_result(
        ctx, {"enabled": enabled, "mods": [{"file_id": h.file_id, "name": h.name} for h in hits]}
    ):
        return
    console.print(f"[green]{verb} {len(hits)} mod(s).[/green]")
    if not enabled:
        console.print(
            "[dim]Off means temporarily not installed: the state entry and "
            "the files on disk stay. Run 'deploy' to apply.[/dim]"
        )


@main.command(name="disable")
@click.argument("mods_dir", type=click.Path(exists=True, path_type=Path))
@click.option("--file-id", type=int, default=None, help="Disable by Nexus file id")
@click.option("--mod-id", type=int, default=None, help="Disable by Nexus mod id")
@click.pass_context
def disable_mod(
    ctx: click.Context, mods_dir: Path, file_id: int | None, mod_id: int | None
) -> None:
    """Turn a mod off without removing it.

    The mod keeps its state entry, its FOMOD picks, and its files on
    disk; its files just stop landing at deploy. Run 'deploy' to apply.
    Exactly one of --file-id or --mod-id is required.
    """
    _set_mod_enabled(ctx, mods_dir, file_id, mod_id, enabled=False)


@main.command(name="enable")
@click.argument("mods_dir", type=click.Path(exists=True, path_type=Path))
@click.option("--file-id", type=int, default=None, help="Enable by Nexus file id")
@click.option("--mod-id", type=int, default=None, help="Enable by Nexus mod id")
@click.pass_context
def enable_mod(
    ctx: click.Context, mods_dir: Path, file_id: int | None, mod_id: int | None
) -> None:
    """Turn a mod back on. Run 'deploy' to apply.

    Exactly one of --file-id or --mod-id is required.
    """
    _set_mod_enabled(ctx, mods_dir, file_id, mod_id, enabled=True)


@main.command()
@click.argument("mods_dir", type=click.Path(exists=True, path_type=Path))
@click.option("--apply", is_flag=True, help="Actually delete. Without this it is a dry run.")
@click.pass_context
def gc(ctx: click.Context, mods_dir: Path, apply: bool) -> None:
    """Reclaim disk from archives whose job is done.

    Dry-run by default: it reports what is reclaimable and touches
    nothing until --apply. Never deletes the backup store, and never
    deletes an archive that is the only copy of its mod.
    """
    from .gc import apply_gc, human_bytes, plan_gc

    plan = plan_gc(mods_dir)
    if _emit_result(
        ctx,
        {
            "dry_run": not apply,
            "reclaimable": [str(p) for p in plan.reclaimable],
            "bytes_reclaimable": plan.bytes_reclaimable,
            "kept": [{"path": str(p), "reason": r} for p, r in plan.kept],
        },
    ):
        return

    if not plan.reclaimable and not plan.kept:
        console.print(
            "[dim]No archives to reclaim here. Downloads keep their "
            "archive so this command can buy the disk back once the "
            "files are extracted.[/dim]"
        )
        return
    if plan.reclaimable:
        for p in plan.reclaimable:
            console.print(f"  [dim]{escape(str(p))}[/dim]")
        console.print(
            f"[green]{len(plan.reclaimable)} archive(s), "
            f"{human_bytes(plan.bytes_reclaimable)} reclaimable.[/green]"
        )
    for p, reason in plan.kept:
        console.print(f"  [yellow]kept[/yellow] {escape(str(p.name))}: {reason}")
    if not apply:
        if plan.reclaimable:
            console.print("[dim]Dry run. Nothing was deleted. Re-run with --apply.[/dim]")
        else:
            console.print(
                "[dim]Nothing to reclaim yet: every archive is still "
                "the only copy of its mod.[/dim]"
            )
    else:
        freed = apply_gc(plan)
        console.print(f"[green]Freed {human_bytes(freed)}.[/green]")


@main.command()
@click.argument(
    "root_dir",
    required=False,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.pass_context
def collections(ctx: click.Context, root_dir: Path | None) -> None:
    """List collections under ROOT_DIR (default: current directory).

    Read-only and offline: reflects local state only, never contacts the
    Nexus API. Unreadable collections are skipped.
    """
    from .service import list_collections

    root = root_dir or Path.cwd()
    found = list_collections(root)

    if _emit_result(ctx, {"root": root, "collections": found}):
        return

    if not found:
        console.print(f"[dim]No collections found under {escape(str(root))}.[/dim]")
        return
    table = Table(title="Collections")
    table.add_column("#", style="dim", justify="right")
    table.add_column("Name", style="cyan")
    table.add_column("Game", style="green")
    table.add_column("Mods", justify="right")
    table.add_column("Pending", justify="right")
    for i, c in enumerate(found, 1):
        table.add_row(str(i), c.name, c.game_domain, str(c.mod_count), str(c.pending_count))
    console.print(table)


@main.command()
@click.argument(
    "root_dir",
    required=False,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.pass_context
def games(ctx: click.Context, root_dir: Path | None) -> None:
    """Summarise collections and mods per game under ROOT_DIR.

    Read-only and offline. See 'nexus-dl collections' for the per-collection
    view.
    """
    from .service import list_games

    root = root_dir or Path.cwd()
    found = list_games(root)

    if _emit_result(ctx, {"root": root, "games": found}):
        return

    if not found:
        console.print(f"[dim]No collections found under {escape(str(root))}.[/dim]")
        return
    table = Table(title="Games")
    table.add_column("Game", style="green")
    table.add_column("Collections", justify="right")
    table.add_column("Total mods", justify="right")
    for g in found:
        table.add_row(g.game_domain, str(g.collection_count), str(g.mod_count))
    console.print(table)


@main.command()
@click.argument("mods_dir", type=click.Path(exists=True, path_type=Path))
@click.pass_context
def mods(ctx: click.Context, mods_dir: Path) -> None:
    """List tracked mods from local state (read-only, no network).

    Reflects exactly what .nexus-state.json tracks right now. Use this to
    find the --file-id/--mod-id values that 'nexus-dl remove' needs.
    """
    from .service import list_mods

    try:
        found = list_mods(mods_dir)
    except StateError as e:
        _fail(ctx, e, hint="Run 'sync' first to download a collection.")

    if _emit_result(ctx, {"mods": found}):
        return

    if not found:
        console.print("[dim]No mods tracked.[/dim]")
        return
    table = Table(title="Mods")
    table.add_column("Name", style="cyan")
    table.add_column("Version", style="green")
    table.add_column("file_id", justify="right")
    table.add_column("mod_id", justify="right")
    table.add_column("Status", style="dim")
    table.add_column("Picks", style="dim")
    for m in found:
        table.add_row(
            m.name,
            m.version or "-",
            str(m.file_id),
            str(m.mod_id),
            m.download_status,
            _format_picks(m.fomod_picks),
        )
    console.print(table)


def _format_picks(picks: list, limit: int = 3) -> str:
    """Option names from stored FOMOD picks, truncated with a count.

    A mod with a dozen options reads as the first few plus how many are
    hidden, not as one endless cell. Empty means no picks recorded.
    """
    names = [str(p.get("option", "")) for p in picks if isinstance(p, dict)]
    names = [n for n in names if n]
    if not names:
        return "-"
    if len(names) <= limit:
        return ", ".join(names)
    shown = ", ".join(names[:limit])
    return f"{shown} (+{len(names) - limit} more)"


@main.command()
@click.option(
    "--non-interactive",
    is_flag=True,
    help="No prompts. Requires --collections-root.",
)
@click.option(
    "--collections-root",
    type=click.Path(path_type=Path),
    default=None,
    help="Directory collection folders live under.",
)
@click.option(
    "--game",
    multiple=True,
    help="A known game as domain=path (repeatable).",
)
@click.option(
    "--install-handler",
    is_flag=True,
    help=(
        "Also install the user desktop entry so nxm:// links open nexus-dl. "
        "Opt-in: it writes a .desktop file and registers a MIME default."
    ),
)
@click.pass_context
def setup(
    ctx: click.Context,
    non_interactive: bool,
    collections_root: Path | None,
    game: tuple[str, ...],
    install_handler: bool,
) -> None:
    """Configure nexus-dl: where collections live and what games you have.

    Interactive by default: it detects your Steam root and installed
    games, asks for what it cannot detect, and writes the config. With
    --non-interactive it takes flags only and never prompts, which is
    what a script or a harness needs.

    It can also install the nxm:// desktop handler, which is the 0.4.0
    headline feature and used to live in the docs as a checklist item
    nobody reached. Interactive setup offers it; --install-handler asks
    for it explicitly. Both are opt-in, because installing a desktop
    entry and flipping a MIME default is a change to the system.

    This writes ~/.config/nexus-dl/config.json. Bare `nexus-dl` reads it
    to open the shell, so getting it set up is what turns the first run
    into the workbench.
    """
    from .config import UserConfig
    from .steam import list_installed_games

    def _install_handler() -> bool:
        """Offer and perform the desktop-handler install. True if done."""
        from .handler import DesktopInstaller

        try:
            entry = DesktopInstaller().install()
        except Exception as e:
            # A failed install must not undo a successful setup.
            console.print(
                f"[yellow]Handler not installed:[/yellow] {escape(exception_reason(e))}"
            )
            return False
        console.print(f"[green]NXM handler installed:[/green] {escape(str(entry))}")
        console.print(
            "[dim]'Mod manager download' links now open nexus-dl. Map each "
            "game with 'nexus-dl handler map GAME COLLECTION_DIR'.[/dim]"
        )
        return True

    if non_interactive:
        if collections_root is None:
            _fail(
                ctx,
                click.UsageError(
                    "--non-interactive needs --collections-root: with no "
                    "prompts there is nothing to guess from"
                ),
            )
        games: list[dict] = []
        for spec in game:
            if "=" not in spec:
                _fail(
                    ctx,
                    click.UsageError(f"--game needs domain=path, got {spec!r}"),
                )
            domain, _, game_dir = spec.partition("=")
            games.append({"domain": domain.strip(), "game_dir": game_dir.strip()})
        cfg = UserConfig(collections_root=str(collections_root), games=games)
        cfg.save()
        handler_installed = _install_handler() if install_handler else False
        if _emit_result(
            ctx,
            {
                "collections_root": cfg.collections_root,
                "games": cfg.games,
                "handler_installed": handler_installed,
            },
        ):
            return
        console.print(f"[green]Configured.[/green] Collections root: {cfg.collections_root}")
        return

    if not _is_interactive():
        _fail(
            ctx,
            click.UsageError(
                "setup prompts, and this is not a terminal. Use "
                "--non-interactive with --collections-root."
            ),
        )

    console.print("[bold]nexus-dl setup[/bold]")
    detected = list_installed_games()
    if detected:
        console.print(f"Found {len(detected)} installed game(s) this tool can manage:")
        for g in detected:
            console.print(f"  [cyan]{g['domain']}[/cyan]  {g['game_dir']}")
    else:
        console.print("[dim]No managed games detected from Steam.[/dim]")

    root = collections_root
    if root is None:
        suggested = Path.home() / "nexus-collections"
        answer = click.prompt("Collections root", default=str(suggested), show_default=True)
        root = Path(answer)

    games = []
    for g in detected:
        if click.confirm(f"Add {g['name']} ({g['domain']})?", default=True):
            games.append({"domain": g["domain"], "game_dir": g["game_dir"]})

    cfg = UserConfig(collections_root=str(root), games=games)
    cfg.save()
    console.print(f"[green]Configured.[/green] Collections root: {cfg.collections_root}")
    if cfg.games:
        console.print(f"Games: {', '.join(g['domain'] for g in cfg.games)}")

    if install_handler:
        _install_handler()
    else:
        console.print(
            "\n[bold]nxm:// link handler[/bold] (optional, the 0.4.0 "
            "headline feature): lets a 'Mod manager download' click on "
            "the Nexus website land in nexus-dl."
        )
        if click.confirm("Install it now?", default=True):
            _install_handler()
        else:
            console.print(
                "[dim]Skipped. Install later with 'nexus-dl handler install'.[/dim]"
            )


@main.command()
def serve() -> None:
    """The web UI was removed in 0.4.0. Use `nexus-dl shell` instead.

    A stub, not a command: it names the replacement and exits non-zero.
    A bare "no such command" told an upgrading user nothing about what
    happened to the thing they used to type.
    """
    console.print(
        "[yellow]The web UI was removed in 0.4.0.[/yellow] "
        "Use [bold]nexus-dl shell[/bold] instead: it covers the same "
        "workflows and is where the tool is heading."
    )
    raise SystemExit(2)


@main.command()
@click.argument(
    "root_dir",
    required=False,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.pass_context
def health(ctx: click.Context, root_dir: Path | None) -> None:
    """Run local health checks for the tool installation.

    Read-only and offline. Reports API key source (never the value), nxm
    handler state, desktop notification capability, and lock state.

    This is a report, not a gate: it exits 0 even when a check reports WARN
    or ERROR. Read the per-check status rather than the exit code.
    """
    from .service import collect_health

    root = root_dir or Path.cwd()
    try:
        checks = collect_health(root, api_key_present=bool(ctx.obj.get("api_key")))
    except Exception as e:  # a failing check must not hide the rest
        _fail(ctx, e)

    if _emit_result(ctx, {"root": root, "checks": checks}):
        return

    if not checks:
        console.print("[dim]No checks ran.[/dim]")
        return
    for c in checks:
        style = {"OK": "green", "WARN": "yellow", "ERROR": "red"}.get(c.status, "dim")
        console.print(f"[{style}]{c.status}[/{style}] {c.label}: {escape(c.detail)}")
        if c.remedy:
            console.print(f"      [dim]{escape(c.remedy)}[/dim]")


@main.command(name="shell")
@click.argument(
    "root_dir",
    metavar="ROOT_DIR",
    required=False,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.pass_context
def shell_cmd(ctx: click.Context, root_dir: Path | None) -> None:
    """
    Open an interactive shell over the service layer.

    ROOT_DIR: Directory holding the collections to browse (default: the
    current directory). Type /help inside for the command list.
    """
    from .shell import run_shell

    svc = ctx.obj["service"]
    root = root_dir if root_dir is not None else Path.cwd()
    sys.exit(run_shell(svc, root, console=console))


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
    except Exception:  # noqa: S110 - best-effort logging: a broken log must never break nxm
        pass


def _record_nxm_event(collection_dir: Path | None, payload: dict) -> None:
    """Append one click event to a collection's events file; best-effort.

    A running `nexus-dl shell` tails this file to learn that a browser
    click landed in a collection it has open. Written only when a
    collection can be named: a parse failure or a routing failure touched
    no collection and produces no event.

    Same posture as _log_nxm: a broken event file must never fail a
    download.
    """
    if collection_dir is None:
        return
    try:
        append_nxm_event(collection_dir, payload)
    except Exception:  # noqa: S110 - best-effort: a broken event file must never break nxm
        pass


def _nxm_event_payload(result, outcome: str) -> dict:
    """Build an event payload from an NxmResult's already-redacted fields."""
    return {
        "outcome": outcome,
        "collection_name": result.collection_dir.name if result.collection_dir else "",
        "collection_dir": str(result.collection_dir) if result.collection_dir else "",
        "mod_id": result.mod_id or 0,
        "file_id": result.file_id or 0,
        "mod_name": result.mod_name,
        "filename": result.filename,
        "extracted": bool(result.extracted),
        "reason": (
            "re_click_required" if result.re_click_required else ""
        ) if outcome == "error" else "",
    }


def _boundary_secrets(ctx: click.Context, uri: str) -> tuple:
    """Literal secrets to redact from text leaving the nxm error boundary.

    Thin wrapper over redaction.merge_secrets/uri_query_secrets so the
    nxm boundary and the interactive shell share ONE redaction helper
    (see redaction.py for the query-extraction rules; they mirror the
    nxm parser's acceptance rules rather than parse_qs).
    """
    api_key = (ctx.obj or {}).get("api_key")
    return merge_secrets(
        [api_key] if api_key else [],
        uri_query_secrets(uri),
    )


# Floor for bare-literal prose replacement at typed-error boundaries;
# the rationale lives at redaction.MIN_REDACT_SECRET_LEN.
_MIN_REDACT_SECRET_LEN = MIN_REDACT_SECRET_LEN


def _boundary_redact(
    ctx: click.Context,
    uri: str,
    text: str,
    strip_uris: bool = True,
    min_secret_len: int = 0,
) -> str:
    """Full redaction scope for text leaving through the nxm boundary.

    Thin wrapper over redaction.boundary_redact binding the secret scope
    for one request (API key plus the link's query credentials).
    """
    return boundary_redact(
        text,
        _boundary_secrets(ctx, uri),
        strip_uris=strip_uris,
        min_secret_len=min_secret_len,
    )


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

    def redacted(value: object) -> str:
        # Result fields and progress messages are untrusted display
        # data, not pre-redacted text: a hostile or compromised endpoint
        # can reflect the API key or the link's key/expires values into
        # file names, mod names, warnings, error text, or progress
        # lines (the same scope the exception branches above redact
        # against). Same boundary scope and short-secret floor as those
        # branches; the notify path adds URI stripping on top via
        # _notify_body.
        return _boundary_redact(ctx, uri, str(value), strip_uris=False,
                                min_secret_len=_MIN_REDACT_SECRET_LEN)

    def safe_progress(event: str, pct: float, msg: str) -> None:
        # Progress lines print BEFORE the result block and echo
        # endpoint-controlled names ("Downloading {filename}",
        # "Downloaded {mod_name}", "Saved {filename}", "Extracted
        # {name}"), so they get the same boundary redaction scope.
        # escape() for the same bracketed-text reason as fail(). The
        # event=="download" suppression inside _cli_progress is
        # unchanged.
        _cli_progress(event, pct, escape(redacted(msg)))

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
            on_progress=safe_progress,
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
        # A target given on the command line is a collection we can name,
        # so the running shell hears about the failure. A mapped target is
        # resolved inside the service and is not visible here; that click
        # reports through nxm.log only.
        _record_nxm_event(
            mods_dir,
            {
                "outcome": "error",
                "collection_dir": str(mods_dir) if mods_dir else "",
                "collection_name": mods_dir.name if mods_dir else "",
                "reason": _nxm_reason(e),
            },
        )
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

    # Every user-visible piece of a result-based outcome goes through
    # boundary redaction before display or notification, the same
    # scope safe_progress applied to the progress lines above; only the
    # raw numeric ids and fixed status tokens are printed as-is.
    error_text = redacted(result.error)
    mod_name = redacted(result.mod_name)
    filename = redacted(result.filename)

    if not result.success:
        # A pending entry is not a failure: the download landed and the
        # tool did the designed thing (kept the archive, never extracted
        # a FOMOD blind). Report what is pending and how to finish it;
        # only a flat failure gets the failure headline.
        pending = result.download_status in ("pending_download", "pending_choices")
        if pending:
            reason = result.download_status
            _log_nxm("warn", f"{_nxm_ids(result)}reason={reason}")
        else:
            reason = "re_click_required" if result.re_click_required else "rejected"
            _log_nxm("error", f"{_nxm_ids(result)}reason={reason}")
        event_outcome = result.download_status if pending else "error"
        _record_nxm_event(
            result.collection_dir, _nxm_event_payload(result, event_outcome)
        )
        if result.error:
            if pending:
                console.print(f"[yellow]Waiting:[/yellow] {escape(error_text)}")
            else:
                console.print(f"[red]Error:[/red] {escape(error_text)}")
        if result.re_click_required:
            console.print(
                "[yellow]Click 'Mod manager download' on the Nexus website "
                "again, then retry.[/yellow]"
            )
        if want_notify:
            if pending:
                what = (
                    "is waiting for option choices"
                    if result.download_status == "pending_choices"
                    else "is waiting for its download to finish"
                )
                handler_notify(f"{mod_name or 'A mod'} {what}",
                               _notify_body(error_text))
            else:
                handler_notify("nexus-dl: download failed",
                               _notify_body(error_text))
        sys.exit(1)

    if result.status == "already_downloaded":
        console.print(
            f"[green]'{escape(mod_name)}' (file {result.file_id}) is already "
            f"downloaded in this collection.[/green]"
        )
        summary = f"Already downloaded: {mod_name}"
    elif result.status == "extracted_from_archive":
        console.print(
            f"[green]Finished '{escape(mod_name)}' (file "
            f"{result.file_id}) by extracting the retained archive; no "
            "download was needed.[/green]"
        )
        summary = f"Extracted {mod_name} from the retained archive"
    elif result.status == "saved_to_output":
        console.print(f"[green]Saved {escape(redacted(result.output_path))}[/green]")
        summary = f"Saved {filename}"
    else:
        where = (
            f" into {escape(redacted(result.collection_dir))}"
            if result.collection_dir else ""
        )
        console.print(
            f"[green]Downloaded '{escape(mod_name)}'{where}.[/green]"
        )
        if result.extracted:
            console.print(
                "[dim]Archive extracted. Run 'nexus-dl deploy' when ready; "
                "nothing was deployed automatically.[/dim]"
            )
        summary = f"Downloaded {mod_name}"
    # Non-fatal follow-up failures on a completed download (e.g. load
    # order): truthful warnings, still a success exit.
    for warning in result.warnings:
        console.print(f"[yellow]Warning:[/yellow] {escape(redacted(warning))}")
    # One status line for the after-the-fact trail: ids and the fixed
    # status token only, never names, URIs, or credentials.
    _log_nxm(
        "warn" if result.warnings else "success",
        f"{_nxm_ids(result)}status={result.status}",
    )
    # A standalone --output drop changed no collection, so there is
    # nothing for a running shell to notice.
    if result.status != "saved_to_output":
        _record_nxm_event(
            result.collection_dir, _nxm_event_payload(result, result.status)
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
@click.pass_context
def handler_map(ctx: click.Context, game: str, collection_dir: Path) -> None:
    """Map GAME to a synced COLLECTION_DIR for nxm:// downloads."""
    try:
        add_mapping(game, collection_dir)
    except HandlerError as e:
        _fail(ctx, e)
    console.print(
        f"[green]Mapped '{escape(game.strip().lower())}' to "
        f"{escape(str(collection_dir))}[/green]"
    )


@handler_group.command(name="unmap")
@click.argument("game")
@click.pass_context
def handler_unmap(ctx: click.Context, game: str) -> None:
    """Remove all mappings for GAME."""
    try:
        removed = remove_mapping(game)
    except HandlerError as e:
        _fail(ctx, e)
    if removed:
        console.print(f"[green]Removed mapping for '{escape(game.strip().lower())}'.[/green]")
    else:
        console.print(f"[yellow]No mapping exists for '{escape(game.strip().lower())}'.[/yellow]")


@handler_group.command(name="set-api-key")
@click.pass_context
def handler_set_api_key(ctx: click.Context) -> None:
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
        _fail(ctx, e)
    console.print(f"[green]API key stored in {escape(str(path))} (0600).[/green]")
    console.print(
        "[dim]'Mod manager download' clicks now authenticate with this "
        "key. 'nexus-dl handler status' shows the active key source; "
        "'nexus-dl handler remove-api-key' deletes it.[/dim]"
    )


@handler_group.command(name="remove-api-key")
@click.pass_context
def handler_remove_api_key(ctx: click.Context) -> None:
    """Delete the stored API key.

    Removes only the key file; game mappings and the desktop handler
    record are left untouched.
    """
    try:
        removed = remove_api_key()
    except KeyStoreError as e:
        _fail(ctx, e)
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
        _fail(ctx, e)

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
@click.pass_context
def handler_install(ctx: click.Context) -> None:
    """Install the user desktop entry so nxm:// links open nexus-dl.

    User-level only (XDG applications dir); remembers the previous
    default handler so uninstall can restore it. Re-running is
    idempotent: the entry is always rewritten, so stale entries from
    older versions are replaced.
    """
    # Capability probe (warnings only, never a refusal): the click flow
    # needs xdg-mime to register, notify-send to report outcomes, and a
    # desktop that actually displays notifications. Findings print
    # before install so a degraded system sees them even when install
    # then stops with an error. Desktop identity is never a gate.
    for warning in capability_warnings():
        console.print(f"[yellow]Warning:[/yellow] {escape(warning)}")
    try:
        entry = DesktopInstaller().install()
    except (HandlerError, OSError) as e:
        _fail(ctx, e)
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
@click.pass_context
def handler_uninstall(ctx: click.Context) -> None:
    """Remove the desktop entry; restores the previous default if ours."""
    try:
        message = DesktopInstaller().uninstall()
    except (HandlerError, OSError) as e:
        _fail(ctx, e)
    console.print(f"[green]{escape(message)}[/green]")


if __name__ == "__main__":
    main()
