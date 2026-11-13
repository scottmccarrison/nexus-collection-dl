"""Interactive ``nexus-dl shell`` REPL over the service layer.

``nexus-dl shell [ROOT_DIR]`` (ROOT_DIR defaults to the current
directory) opens a small command loop for browsing the collections and
mods under ROOT_DIR and for the operations that want immediate
feedback: adding one mod, syncing a collection, and removing a mod from
a collection.

Design notes:

* A "collection" is ROOT_DIR itself or an immediate (depth-1)
  subdirectory holding .nexus-state.json, exactly the scan
  service.list_collections performs. Nothing deeper is probed.
* The loop never dies. Domain errors (StateError and friends), OSError
  from state I/O, ValueError from argument validation, and failed
  results are rendered and the loop continues; a final catch-all keeps
  even unexpected failures from ending the session. Ctrl-D exits 0 with
  a goodbye; Ctrl-C at a prompt cancels the pending input; Ctrl-C
  during a service call reports the interruption and never claims
  success.
* Credential hygiene: no nxm:// link is ever echoed back, and every
  rendering goes through the shared boundary redactor (redaction.py)
  with the API key plus every link credential seen on input lines so
  far in scope.
* Removal goes through ModManagerService.remove_mod: the tombstone is
  written at the service layer (so sync/update honor it), payload files
  on disk are never deleted, and re-adding the mod clears the tombstone.
* /sync takes three targets: a collection URL, an index or name
  resolved exactly like /use, or nothing to resync the current
  collection. The last two sync the collection_url the state recorded
  at its last sync (service.get_collection_url) and fail with a
  pointer to the URL form when the state records none.
* No new dependencies: stdlib input() plus the rich console the CLI
  already uses. No completion, no history.
"""

from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .api import NexusAPIError
from .collection import CollectionParseError, ModParseError
from .downloader import DownloadError
from .extractor import ExtractionError
from .handler import HandlerError
from .nxm import NxmParseError
from .redaction import (
    MIN_REDACT_SECRET_LEN,
    boundary_redact,
    merge_secrets,
    text_uri_secrets,
    uri_tokens,
)
from .service import (
    AddResult,
    CollectionSummary,
    ModManagerService,
    NxmResult,
    SyncResult,
    get_collection_url,
    list_collections,
    list_mods,
)
from .state import StateBusyError, StateError

# Domain failures that must render and keep the loop going.
# StateBusyError is listed explicitly for documentation even though it
# subclasses StateError; NxmParseError and ModParseError join the list
# because the add paths raise them as ordinary input problems, exactly
# like CollectionParseError for sync.
_DOMAIN_ERRORS = (
    StateError,
    StateBusyError,
    NexusAPIError,
    DownloadError,
    ExtractionError,
    CollectionParseError,
    ModParseError,
    NxmParseError,
    HandlerError,
)

_HELP_LINES = (
    ("/help", "show this command list"),
    ("/collections", "list collections under the root"),
    ("/games", "list games with collection and mod counts"),
    ("/use <index|name>", "select the collection other commands work on"),
    ("/mods [collection]", "list tracked mods (default: current collection)"),
    ("/add <url>", "add one mod (https:// mod page or nxm:// link)"),
    (
        "/sync [<index|name|url>]",
        "sync a collection (default: the current collection)",
    ),
    ("/remove <file_id|mod:id>", "remove a mod from the current collection"),
    ("/status", "show collection status vs the latest revision"),
    ("/quit", "leave the shell (also /exit, or Ctrl-D)"),
)

# The only input that confirms a /remove. Anything else, including "no"
# and Ctrl-C, aborts with no change.
_CONFIRM_YES = "yes"

# Consecutive input-read failures tolerated before the shell gives up on
# its input stream. One bad read is recoverable (a non-UTF-8 byte on
# piped stdin, a hiccuping tty); a stream that cannot produce a single
# line must end the session cleanly instead of spinning forever.
_MAX_INPUT_FAILURES = 3


class _Quit(Exception):
    """Control flow for /quit: unwinds to run(), which exits 0."""


class _ShellSession:
    """One interactive session; see run_shell for the entry point."""

    def __init__(
        self,
        svc: ModManagerService,
        root: Path,
        input_fn,
        console: Console,
    ):
        self.svc = svc
        self.root = Path(root)
        self.input_fn = input_fn
        self.console = console
        # Current collection chosen with /use; None until then.
        self.current: CollectionSummary | None = None
        # Literal secret values redacted from everything the session
        # prints: the API key plus, for every URI typed on an input
        # line, its key/expires query values (and for nxm:// links the
        # entire link). See _absorb_secrets.
        self._secrets: tuple = merge_secrets(
            [str(getattr(svc, "_api_key", "") or "")]
        )

    # ------------------------------------------------------------------
    # rendering
    # ------------------------------------------------------------------

    def _absorb_secrets(self, line: str) -> None:
        """Widen the redaction scope with any credentials on an input line.

        Query key/expires values of every URI join the scope, and an
        nxm:// link joins it whole: the link must never be echoed back,
        so it is redacted from any output in any case or encoding
        variant.
        """
        absorbed = list(text_uri_secrets(line))
        absorbed.extend(
            token
            for token in uri_tokens(line)
            if token.lower().startswith("nxm:")
        )
        self._secrets = merge_secrets(self._secrets, absorbed)

    def _scrub(self, text: str) -> str:
        """Redact scoped secrets from routine output.

        Applied to every line the shell prints, success text included,
        so a query credential can never surface anywhere. Literal
        values are redacted with no length floor: values in scope are
        opaque credentials, so over-redacting is the safe direction.
        """
        return boundary_redact(
            text, self._secrets, strip_uris=False, min_secret_len=0
        )

    def _scrub_error(self, text: str) -> str:
        """Redaction scope for typed domain errors.

        Matches the nxm boundary's typed branch: bare-literal redaction
        only for secrets of at least MIN_REDACT_SECRET_LEN so deliberate
        guidance prose is not mangled, while percent-encoded forms and
        URI query strings are always redacted. These messages are
        already redacted at their source; this is defense in depth.
        """
        return boundary_redact(
            text,
            self._secrets,
            strip_uris=False,
            min_secret_len=MIN_REDACT_SECRET_LEN,
        )

    def _scrub_failure(self, text: str) -> str:
        """Redaction scope for argument and catch-all failures.

        Full paranoia, matching the nxm boundary's outer branch: every
        scoped secret in any case or encoding variant is redacted with
        no length floor, and URI-shaped text is stripped so a reflected
        link can never be echoed.
        """
        return boundary_redact(
            text, self._secrets, strip_uris=True, min_secret_len=0
        )

    def _emit(self, text: str) -> None:
        """Print routine output, escaped and secret-redacted."""
        self.console.print(escape(self._scrub(str(text))))

    def _emit_error(self, text: str) -> None:
        """Print a typed domain error and continue."""
        self.console.print(
            f"[red]Error:[/red] {escape(self._scrub_error(str(text)))}"
        )

    def _emit_failure(self, text: str) -> None:
        """Print a failure (argument or catch-all) and continue."""
        self.console.print(
            f"[red]Error:[/red] {escape(self._scrub_failure(str(text)))}"
        )

    def _emit_warning(self, text: str) -> None:
        self.console.print(
            f"[yellow]Warning:[/yellow] {escape(self._scrub(str(text)))}"
        )

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------

    def run(self) -> int:
        """Run the REPL until quit or end of input. Always exits 0."""
        self._emit(
            "nexus-dl shell. Type /help for commands, /quit to exit."
        )
        input_failures = 0
        while True:
            try:
                line = self.input_fn("nexus-dl> ")
            except EOFError:
                # Ctrl-D: goodbye, exit 0.
                self._emit("Goodbye.")
                return 0
            except KeyboardInterrupt:
                # Ctrl-C at the prompt cancels the pending line only.
                self._emit("Cancelled.")
                continue
            except Exception as e:
                # Any other input failure (non-UTF-8 bytes on piped
                # stdin, a broken tty) is reported through the redacted
                # failure path and retried; a stream that keeps failing
                # ends the session cleanly instead of spinning forever.
                input_failures += 1
                self._emit_failure(
                    str(e) or f"input failed ({type(e).__name__})"
                )
                if input_failures >= _MAX_INPUT_FAILURES:
                    self._emit("Input is unusable; goodbye.")
                    return 0
                continue

            input_failures = 0
            try:
                self._absorb_secrets(line)
                self._run_line(line)
            except _Quit:
                self._emit("Goodbye.")
                return 0
            except KeyboardInterrupt:
                # Interrupted during a service call: never claim
                # success, never mark anything done.
                self._emit_failure(
                    "interrupted; the operation did not complete and "
                    "nothing was marked done"
                )
            except _DOMAIN_ERRORS as e:
                self._emit_error(str(e) or type(e).__name__)
            except (OSError, ValueError) as e:
                # OSError from state I/O, ValueError from argument
                # validation: report and keep going.
                self._emit_failure(str(e) or type(e).__name__)
            except Exception as e:
                # Outer boundary: nothing escapes the loop, and
                # free-form text gets the full redaction scope.
                detail = self._scrub_failure(str(e)).strip()
                self._emit_failure(
                    detail or f"Unexpected failure ({type(e).__name__})."
                )

    def _run_line(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return

        if stripped.startswith("/"):
            command, _, rest = stripped.partition(" ")
            handler = self._COMMANDS.get(command.lower())
            if handler is None:
                self._emit_failure(
                    f"Unknown command '{command}'. "
                    "For the command list, try /help."
                )
                return
            handler(self, rest.strip())
            return

        low = stripped.lower()
        if low.startswith("nxm://") or low.startswith(("http://", "https://")):
            # A bare link on a line is the same thing as /add <link>.
            self._cmd_add(stripped)
            return

        self._emit_failure(
            "Unknown input. For the command list, try /help. "
            "Bare nxm or https links are handled as /add."
        )

    # ------------------------------------------------------------------
    # collection resolution
    # ------------------------------------------------------------------

    def _collection_listing(self) -> list[CollectionSummary]:
        return list_collections(self.root)

    def _format_listing(self, colls: list[CollectionSummary]) -> str:
        """Index-numbered listing used by /collections and error hints."""
        if not colls:
            return f"  (no collections under {self.root})"
        return "\n".join(
            f"  {i}: {c.name} [{c.game_domain or '-'}]"
            for i, c in enumerate(colls, 1)
        )

    def _resolve_collection(self, selector: str) -> CollectionSummary:
        """Resolve a collection selector to exactly one collection.

        The selector is the ordinal index shown by /collections (always
        unambiguous) or the rest of the line as a name: an exact
        case-sensitive match wins first, then a unique case-insensitive
        match. Anything else raises ValueError listing the indexes;
        duplicate display names are always shown with their indexes.
        """
        selector = selector.strip()
        if not selector:
            raise ValueError("usage: /use <index|name>")
        colls = self._collection_listing()
        if not colls:
            raise ValueError(f"no collections found under {self.root}")

        if selector.isdigit():
            index = int(selector)
            if 1 <= index <= len(colls):
                return colls[index - 1]
            raise ValueError(
                f"no collection with index {selector}\n"
                + self._format_listing(colls)
            )

        exact = [c for c in colls if c.name == selector]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise ValueError(
                f"'{selector}' matches {len(exact)} collections; "
                "pick one by index:\n" + self._format_listing(colls)
            )

        folded = [c for c in colls if c.name.lower() == selector.lower()]
        if len(folded) == 1:
            return folded[0]
        if len(folded) > 1:
            raise ValueError(
                f"'{selector}' matches {len(folded)} collections "
                "(names differ only by case); pick one by index:\n"
                + self._format_listing(colls)
            )
        raise ValueError(
            f"no collection named '{selector}'\n"
            + self._format_listing(colls)
        )

    def _require_current(self) -> CollectionSummary:
        if self.current is None:
            raise ValueError(
                "no collection selected yet; use /use <index|name> first "
                "(see /collections)"
            )
        return self.current

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------

    def _cmd_help(self, rest: str) -> None:
        table = Table(title="Shell commands")
        table.add_column("Command", style="cyan", no_wrap=True)
        table.add_column("Description")
        for command, description in _HELP_LINES:
            # Escape both cells: rich markup must not swallow bracketed
            # arguments like /mods [collection].
            table.add_row(escape(command), escape(description))
        self.console.print(table)
        self._emit(
            "Bare nxm:// or https:// links on a line are handled as /add."
        )

    def _cmd_quit(self, rest: str) -> None:
        raise _Quit()

    def _cmd_collections(self, rest: str) -> None:
        if rest:
            raise ValueError("usage: /collections")
        colls = self._collection_listing()
        if not colls:
            self._emit(f"No collections found under {self.root}.")
            return
        table = Table(title=f"Collections under {escape(str(self.root))}")
        table.add_column("#", justify="right")
        table.add_column("Name", style="cyan")
        table.add_column("Game", style="green")
        table.add_column("Mods", justify="right")
        table.add_column("Pending", justify="right")
        for i, c in enumerate(colls, 1):
            table.add_row(
                str(i),
                escape(self._scrub(c.name)),
                escape(self._scrub(c.game_domain or "-")),
                str(c.mod_count),
                str(c.pending_count),
            )
        self.console.print(table)

    def _cmd_games(self, rest: str) -> None:
        if rest:
            raise ValueError("usage: /games")
        colls = self._collection_listing()
        if not colls:
            self._emit(f"No collections found under {self.root}.")
            return
        totals: dict[str, list[int]] = {}
        for c in colls:
            row = totals.setdefault(c.game_domain or "-", [0, 0])
            row[0] += 1
            row[1] += c.mod_count
        table = Table(title="Games")
        table.add_column("Game", style="green")
        table.add_column("Collections", justify="right")
        table.add_column("Total mods", justify="right")
        for game in sorted(totals):
            collections, mods = totals[game]
            table.add_row(
                escape(self._scrub(game)), str(collections), str(mods)
            )
        self.console.print(table)

    def _cmd_use(self, rest: str) -> None:
        if not rest:
            raise ValueError("usage: /use <index|name>")
        self.current = self._resolve_collection(rest)
        self._emit(f"Using '{self.current.name}' ({self.current.path}).")

    def _cmd_mods(self, rest: str) -> None:
        if rest:
            target = self._resolve_collection(rest)
        else:
            target = self._require_current()
        mods = list_mods(target.path)
        if not mods:
            self._emit(f"No mods tracked in '{target.name}'.")
            return
        table = Table(title=f"Mods in {escape(self._scrub(target.name))}")
        table.add_column("file_id", justify="right")
        table.add_column("Name", style="cyan")
        table.add_column("Version", style="green")
        table.add_column("Download status")
        for m in mods:
            table.add_row(
                str(m.file_id),
                escape(self._scrub(m.name)),
                escape(self._scrub(m.version or "-")),
                escape(self._scrub(m.download_status)),
            )
        self.console.print(table)

    def _cmd_add(self, rest: str) -> None:
        current = self._require_current()
        if not rest:
            raise ValueError(
                "usage: /add <http(s) mod page URL or nxm link>"
            )
        low = rest.lower()
        if low.startswith("nxm://"):
            # mods_dir is passed explicitly so the service never falls
            # back to the global handler game mappings.
            result = self.svc.add_nxm(rest, mods_dir=current.path)
            self._report_nxm(result)
        elif low.startswith("http://") or low.startswith("https://"):
            result = self.svc.add_mod(rest, current.path)
            self._report_add(result)
        else:
            raise ValueError(
                "/add expects an http(s) mod page URL or an nxm link"
            )

    def _report_add(self, result: AddResult) -> None:
        # Failed results render as failures before any success text.
        if not result.success:
            self._emit_failure(result.error or "the mod could not be added.")
            return
        if result.pending_download:
            p = result.pending_download
            self._emit(f"Free account - download manually: {p.mod_name}")
            self._emit(f"  {p.browser_url}")
            self._emit(
                f"Save the file to {self.current.path}, then run "
                "'nexus-dl import' on that directory."
            )
            return
        self._emit(f"Added '{result.mod_name}' as manual mod (phase 999).")

    def _report_nxm(self, result: NxmResult) -> None:
        # "error" status is a failure even when the success flag claims
        # otherwise: an internally inconsistent result must never render
        # as success text.
        if not result.success or result.status == "error":
            self._emit_failure(result.error or "the download did not complete.")
            if result.re_click_required:
                self._emit(
                    "Click 'Mod manager download' on the Nexus website "
                    "again, then retry."
                )
            return
        if result.status == "already_downloaded":
            self._emit(
                f"'{result.mod_name}' (file {result.file_id}) is already "
                "downloaded in this collection."
            )
        elif result.status == "extracted_from_archive":
            self._emit(
                f"Finished '{result.mod_name}' (file {result.file_id}) by "
                "extracting the retained archive; no download was needed."
            )
        elif result.status == "saved_to_output":
            self._emit(f"Saved {result.output_path}.")
        else:
            where = (
                f" into {result.collection_dir}"
                if result.collection_dir
                else ""
            )
            self._emit(f"Downloaded '{result.mod_name}'{where}.")
            if result.extracted:
                self._emit(
                    "Archive extracted. Run 'nexus-dl deploy' when ready; "
                    "nothing was deployed automatically."
                )
        for warning in result.warnings:
            self._emit_warning(warning)

    def _sync_target_url(self, rest: str) -> str:
        """Resolve the /sync target to the collection URL to sync.

        Three forms: an http(s) collection URL (synced as-is), an
        index or name resolved exactly like /use, or nothing to resync
        the current collection. The index/name and resync forms use the
        collection_url the collection's state recorded at its last
        sync, so nobody has to go re-find a collection they already
        have on disk. A state with no recorded URL gets an honest error
        pointing at the URL form instead of a guess.
        """
        target = rest.strip()
        if target.lower().startswith(("http://", "https://")):
            return target
        if target:
            summary = self._resolve_collection(target)
        elif self.current is not None:
            summary = self.current
        else:
            raise ValueError(
                "no collection selected yet; use /use <index|name> first "
                "(see /collections)\n"
                "usage: /sync [<index|name|url>]"
            )
        url = get_collection_url(summary.path)
        if not url:
            raise ValueError(
                f"no collection URL is stored for '{summary.name}'; "
                "pass it explicitly\n"
                "usage: /sync <collection-url>"
            )
        return url

    def _cmd_sync(self, rest: str) -> None:
        url = self._sync_target_url(rest)
        result: SyncResult = self.svc.sync(url, self.root)
        # Per-mod errors render before any success text.
        for err in result.errors:
            self._emit_error(err)
        if result.pending_downloads:
            self._emit(
                f"{len(result.pending_downloads)} mod(s) need a manual "
                "download:"
            )
            for p in result.pending_downloads:
                self._emit(f"  {p.mod_name}: {p.filename}")
        summary = (
            f"Synced {result.mods_downloaded} mods "
            f"({result.mods_extracted} extracted)"
        )
        if result.errors:
            summary += f", {len(result.errors)} error(s)"
        if result.removed_skipped:
            summary += f"; {result.removed_skipped} removed mod(s) skipped"
        self._emit(summary + ".")
        if result.collection_dir:
            self._emit(f"Collection directory: {result.collection_dir}")
        for f in result.load_order_files:
            self._emit(f"Wrote {f}")

    def _cmd_remove(self, rest: str) -> None:
        current = self._require_current()
        if not rest:
            raise ValueError("usage: /remove <file_id | mod:mod_id>")
        if rest.startswith("mod:"):
            file_id, mod_id = None, _parse_int(rest[4:], "mod id")
        else:
            file_id, mod_id = _parse_int(rest, "file_id"), None

        # Resolve the display name and ids for the confirmation text.
        summary = None
        for m in list_mods(current.path):
            if file_id is not None and m.file_id == file_id:
                summary = m
                break
            if mod_id is not None and m.mod_id == mod_id:
                summary = m
                break

        if summary is not None:
            self._emit(
                f"Remove '{summary.name}' "
                f"(file_id {summary.file_id}, mod_id {summary.mod_id}) "
                "and all its files?"
            )
            self._emit(
                "this removes the mod from the collection and it stays "
                "removed through sync/update; files on disk are kept; "
                "re-add restores it"
            )
            answer = self._confirm(f"Remove '{summary.name}'? [yes/no]: ")
            if answer != _CONFIRM_YES:
                self._emit("Cancelled; nothing was removed.")
                return

        # An id that is not in the listing cannot be removed at all:
        # remove_mod validates before mutating anything and raises
        # StateError, so the service's own error is surfaced directly
        # instead of prompting for a removal that cannot happen.
        result = self.svc.remove_mod(current.path, file_id=file_id, mod_id=mod_id)
        name = summary.name if summary is not None else f"mod {mod_id or file_id}"
        # Render the summary from the result defensively: join the id
        # lists instead of indexing, so an empty-ids result (which the
        # real service never returns) still renders after the removal
        # rather than failing on a list index.
        removed_mods = ", ".join(str(i) for i in result.removed_mod_ids) or "-"
        removed_files = (
            ", ".join(str(i) for i in result.removed_file_ids) or "-"
        )
        self._emit(
            f"Removed '{name}' (mod_id {removed_mods}); "
            f"file(s) {removed_files} left the collection."
        )
        for warning in result.warnings:
            self._emit_warning(warning)

    def _confirm(self, prompt: str) -> str:
        """Read a confirmation line; anything but a clean "yes" aborts.

        Ctrl-C, Ctrl-D, and any other input failure all abort (return
        "no"): a broken input stream must never confirm a destructive
        action. Failures are rendered through the redacted path so they
        stay visible.
        """
        try:
            return self.input_fn(prompt).strip().lower()
        except (KeyboardInterrupt, EOFError):
            return "no"
        except Exception as e:
            self._emit_failure(
                str(e) or f"input failed ({type(e).__name__})"
            )
            return "no"

    def _cmd_status(self, rest: str) -> None:
        if rest:
            raise ValueError("usage: /status")
        current = self._require_current()
        result = self.svc.get_status(current.path)
        self._emit(f"Collection: {result.collection_name}")
        self._emit(f"Installed revision: {result.installed_revision}")
        if result.latest_revision is not None:
            note = (
                " (update available)"
                if result.latest_revision != result.installed_revision
                else ""
            )
            self._emit(f"Latest revision: {result.latest_revision}{note}")
        table = Table(title="Mod status")
        table.add_column("Name", style="cyan")
        table.add_column("Version", style="green")
        table.add_column("Status")
        for m in result.mods:
            table.add_row(
                escape(self._scrub(m.name)),
                escape(self._scrub(m.version or "-")),
                escape(self._scrub(m.status)),
            )
        self.console.print(table)

    _COMMANDS = {
        "/help": _cmd_help,
        "/quit": _cmd_quit,
        "/exit": _cmd_quit,
        "/collections": _cmd_collections,
        "/games": _cmd_games,
        "/use": _cmd_use,
        "/mods": _cmd_mods,
        "/add": _cmd_add,
        "/sync": _cmd_sync,
        "/remove": _cmd_remove,
        "/status": _cmd_status,
    }


def _parse_int(text: str, what: str) -> int:
    try:
        return int(text.strip())
    except ValueError:
        raise ValueError(
            f"{what} must be a whole number, got {text!r}"
        ) from None


def run_shell(
    svc: ModManagerService,
    root: Path,
    input_fn=input,
    console: Console | None = None,
) -> int:
    """Run the interactive shell; returns the process exit code.

    ``input_fn`` is injectable for tests (it must behave like input():
    return one line per call, raise EOFError at end of input and
    KeyboardInterrupt on interrupt). ``console`` defaults to a fresh
    rich Console.
    """
    session = _ShellSession(
        svc, root, input_fn, console if console is not None else Console()
    )
    return session.run()
