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
* Every command accepts ``--help`` as its sole argument (e.g.
  ``/add --help``) and ``/help <command>`` shows the same compact
  usage (form, one-line summary, a few usage lines with one realistic
  example) instead of executing. Help triggers only when ``--help`` is
  the entire argument, so it can never swallow a URL or name that
  merely contains that text; a collection literally named "--help" is
  still reachable by index. A bare ``--help`` line shows the command
  list.
* /status has two forms. Bare /status is a tool-wide LOCAL report
  (games/collections overview, removal tombstones, health checks) that
  never touches the network and works with no collection selected, and
  stays tool-wide even when a collection is selected. /status
  <index|name> keeps the original per-collection revision comparison
  (svc.get_status, network) and adds the collection's local details.
* The prompt shows the current collection ("nexus-dl [Name]> ") and
  plain "nexus-dl>" when none is selected. A root holding exactly one
  collection is selected at startup with a note; larger or empty roots
  start unselected. An explicit /use remembers the selection in
  <ROOT>/.nexus-shell.json (one non-secret value: the collection's
  directory name) and startup restores it when that directory still
  holds .nexus-state.json; anything missing, stale, or corrupt is
  ignored silently. The file is written only on explicit /use, never on
  auto-select or restore.
* /use with no argument reports the current selection (or "none
  selected") plus the collection list and changes nothing.
* Collection selectors resolve EXACT case-sensitive names first (so a
  collection named "2024" is reachable by name), then ASCII-digit
  indexes, then unique case-insensitive names.
* /sync to an already-resolved collection (bare or index/name) updates
  that collection's OWN directory even when its directory name differs
  from the API's name (service.sync's target_dir). URL-form /sync keeps
  the default: a new collection directory under the root.
* No new dependencies: stdlib input() plus the rich console the CLI
  already uses. No completion, no history.
"""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .api import NexusAPIError
from .collection import CollectionParseError, ModParseError
from .downloader import DownloadError
from .extractor import ExtractionError
from .handler import EVENTS_FILENAME, HandlerError
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
    HealthCheck,
    ModManagerService,
    NxmResult,
    SyncResult,
    collect_health,
    collection_local_details,
    collection_manifest_mod_ids,
    get_collection_url,
    list_collections,
    list_mods,
    scan_collections,
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

@dataclass(frozen=True)
class _CommandHelp:
    """Help text for one command.

    The /help table shows ``form`` and ``summary``; ``/help <command>``
    and ``<command> --help`` add the compact ``usage`` block. ``form``
    is the single source of the argument syntax, so the table and the
    per-command help can never drift apart.
    """

    form: str
    summary: str
    usage: tuple[str, ...] = ()
    # /exit is an alias row, documented under /quit and hidden from the
    # table (its --help still works).
    in_table: bool = True

    @property
    def name(self) -> str:
        """Command word without arguments or leading slash (e.g. 'add')."""
        return self.form.split()[0].lstrip("/").lower()


_COMMAND_HELP = (
    _CommandHelp("/help", "show this command list", (
        "/help",
        "/help sync",
    )),
    _CommandHelp("/collections", "list collections under the root", (
        "/collections",
    )),
    _CommandHelp("/games", "list games with collection and mod counts", (
        "/games",
    )),
    _CommandHelp(
        "/use [<index|name>]",
        "select the collection other commands work on, or show the "
        "current one",
        (
            "/use 2",
            "/use Clean Visuals",
            ("/use             show the current selection and the "
            "collection list"),
        ),
    ),
    _CommandHelp(
        "/mods [collection]",
        "list tracked mods (default: current collection)",
        (
            "/mods",
            "/mods 2",
        ),
    ),
    _CommandHelp(
        "/add <url>",
        "add one mod (https:// mod page or nxm:// link)",
        (
            "/add https://www.nexusmods.com/skyrimspecialedition/mods/12345",
            "/add nxm://skyrimspecialedition/mods/12345/files/67890",
        ),
    ),
    _CommandHelp(
        "/sync [<index|name|url>]",
        "sync a collection (default: the current collection)",
        (
            ("/sync https://next.nexusmods.com/skyrimspecialedition/"
            "collections/abc123"),
            "/sync 2",
            "/sync",
        ),
    ),
    _CommandHelp(
        "/remove <file_id|mod:id>",
        "remove a mod from the current collection",
        (
            "/remove 456",
            "/remove mod:123",
        ),
    ),
    _CommandHelp(
        "/deploy [<index|name>]",
        "deploy a collection to the game directory (dry-run then "
        "confirm)",
        (
            "/deploy",
            "/deploy 2",
            "/deploy Clean Visuals",
        ),
    ),
    _CommandHelp(
        "/status [<index|name>]",
        "tool-wide overview and health report, or one collection vs the "
        "latest revision",
        (
            ("/status     tool-wide overview and health, no network "
            "(tool-wide even with a collection selected)"),
            ("/status 2   one collection vs the latest revision "
            "(needs network)"),
        ),
    ),
    _CommandHelp("/quit", "leave the shell (also /exit, or Ctrl-D)"),
    _CommandHelp(
        "/exit", "leave the shell (alias of /quit)", ("/exit",), in_table=False
    ),
)

# Command word (no slash) -> help spec. Every key of _COMMANDS has one:
# tests/test_shell.py asserts the two stay in sync.
_HELP_BY_NAME = {spec.name: spec for spec in _COMMAND_HELP}

# The only input that confirms a /remove. Anything else, including "no"
# and Ctrl-C, aborts with no change.
_CONFIRM_YES = "yes"


class _ShellAsker:
    """The picker's front-end on the shell's plain ``input()`` lines.

    Mirrors the CLI's click prompts so a click-driven pick and an
    ``add``-time pick run the same walk and see the same questions.
    Ctrl-C, Ctrl-D, and any other input failure give no answer rather
    than a guessed one: a broken stream must not select an option.
    """

    def __init__(self, shell: "_ShellSession") -> None:
        self._shell = shell

    def tell(self, text: str) -> None:
        self._shell._emit(f"  {text}")

    def _read(self, prompt: str) -> str | None:
        try:
            return self._shell.input_fn(prompt)
        except (KeyboardInterrupt, EOFError):
            return None
        except Exception:
            return None

    def choose_one(self, prompt: str, count: int, allow_none: bool) -> int | None:
        while True:
            raw = self._read(prompt + ": ")
            if raw is None:
                return None
            raw = raw.strip()
            if not raw:
                if allow_none:
                    return None
                continue
            try:
                n = int(raw)
            except ValueError:
                continue
            if 1 <= n <= count:
                return n

    def choose_many(self, prompt: str, count: int, at_least_one: bool) -> list[int]:
        while True:
            raw = self._read(prompt + ": ")
            if raw is None:
                return []
            parts = [p.strip() for p in raw.strip().split(",") if p.strip()]
            if not parts:
                if not at_least_one:
                    return []
                continue
            try:
                nums = [int(p) for p in parts]
            except ValueError:
                continue
            if all(1 <= n <= count for n in nums):
                return nums

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
        # Byte offset into each click-events file, so the drain reports
        # only events that arrived after the last one consumed. Keyed by
        # the events file path.
        self._event_offsets: dict[Path, int] = {}

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

        Applied to every line the shell prints, success text included.
        Tiered like the typed-error branch: long secrets are redacted
        everywhere, while a short absorbed value (a one-character
        key= from a typed link, say) keeps the MIN_REDACT_SECRET_LEN
        floor for bare prose so it cannot garble ordinary output like
        "Synced 1 mods"; short secrets are still redacted inside URI
        query strings and percent-encoded runs.
        """
        return boundary_redact(
            text,
            self._secrets,
            strip_uris=False,
            min_secret_len=MIN_REDACT_SECRET_LEN,
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
    # current collection: prompt, startup selection, persistence
    # ------------------------------------------------------------------

    def _prompt(self) -> str:
        """Prompt text: the current collection when one is selected.

        Plain input() text; the name is sanitized so control characters
        in a hand-edited collection name cannot garble the prompt.
        """
        if self.current is None:
            return "nexus-dl> "
        return f"nexus-dl [{_prompt_name(self.current.name)}]> "

    def _startup_selection(self) -> None:
        """Best-effort initial selection; never fails the session.

        The remembered selection (written by an earlier explicit /use)
        wins when its directory still holds a readable collection;
        otherwise a root with exactly one collection selects it with a
        note. Nothing here writes the memory file: only explicit /use
        remembers.
        """
        try:
            colls = self._collection_listing()
            remembered = _load_remembered_name(self.root)
        except Exception:
            return
        if remembered is not None:
            match = self._match_remembered(colls, remembered)
            if match is not None:
                self.current = match
                self._emit(
                    f"Using '{_safe_text(match.name)}' "
                    f"({_safe_text(match.path)})."
                )
                return
        if len(colls) == 1:
            self.current = colls[0]
            self._emit(
                f"Using '{_safe_text(self.current.name)}' "
                "(only collection here)."
            )

    def _match_remembered(
        self, colls: list[CollectionSummary], name: str
    ) -> CollectionSummary | None:
        """Find the collection a remembered dir name points at, or None.

        The remembered value is a directory name: a child directory of
        the root wins, then the root itself (root-as-collection).
        """
        target = self.root / name
        for c in colls:
            if c.path == target:
                return c
        for c in colls:
            if c.path == self.root and c.path.name == name:
                return c
        return None

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------

    def run(self) -> int:
        """Run the REPL until quit or end of input. Always exits 0."""
        self._emit(
            "nexus-dl shell. Type /help for commands, /quit to exit."
        )
        self._startup_selection()
        self._init_event_offsets()
        input_failures = 0
        while True:
            self._drain_events()
            try:
                line = self.input_fn(self._prompt())
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
                # success. Services save incrementally, so the honest
                # report is "maybe partial", not "nothing done".
                self._emit_failure(
                    "interrupted; the operation may have partially "
                    "completed; check /status"
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
                    f"Unknown command '{_safe_text(command)}'. "
                    "For the command list, try /help."
                )
                return
            rest = rest.strip()
            # Per-command help: --help as the ENTIRE argument shows the
            # command's usage instead of executing. A narrower trigger
            # could swallow a URL or name that merely contains the text.
            if rest.lower() == "--help":
                spec = _HELP_BY_NAME.get(command.lower().lstrip("/"))
                if spec is not None:
                    self._show_command_help(spec)
                    return
                # No help entry (impossible: every command has one);
                # fall through and let the command answer for itself.
            handler(self, rest)
            return

        low = stripped.lower()
        if low == "--help":
            # Design call: a bare --help line shows the command list
            # (it can never be a collection selector here: non-slash
            # lines are only ever links; a collection named "--help" is
            # still reachable by index through /use).
            self._cmd_help("")
            return
        if low.startswith(("nxm://", "http://", "https://")):
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
            return f"  (no collections under {_safe_text(self.root)})"
        return "\n".join(
            f"  {i}: {_safe_text(c.name)} [{_safe_text(c.game_domain) or '-'}]"
            for i, c in enumerate(colls, 1)
        )

    def _resolve_collection(self, selector: str) -> CollectionSummary:
        """Resolve a collection selector to exactly one collection.

        Precedence: an exact case-sensitive NAME match first (so a
        collection named "2024" is reachable by name), then an INDEX of
        ASCII digits only (the ordinal shown by /collections; unicode
        digits are never interpreted as an index), then a unique
        case-insensitive name. Anything else raises ValueError listing
        the indexes; duplicate display names are always shown with
        their indexes.
        """
        selector = selector.strip()
        if not selector:
            raise ValueError("usage: /use [<index|name>]")
        # Display copy of the selector: echoed back in error messages,
        # so control characters collapse before they reach the terminal.
        shown = _safe_text(selector)
        colls = self._collection_listing()
        if not colls:
            raise ValueError(
                f"no collections found under {_safe_text(self.root)}"
            )

        exact = [c for c in colls if c.name == selector]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise ValueError(
                f"'{shown}' matches {len(exact)} collections; "
                "pick one by index:\n" + self._format_listing(colls)
            )

        if selector.isascii() and selector.isdecimal():
            index = int(selector)
            if 1 <= index <= len(colls):
                return colls[index - 1]
            raise ValueError(
                f"no collection with index {shown}\n"
                + self._format_listing(colls)
            )

        folded = [c for c in colls if c.name.lower() == selector.lower()]
        if len(folded) == 1:
            return folded[0]
        if len(folded) > 1:
            raise ValueError(
                f"'{shown}' matches {len(folded)} collections "
                "(names differ only by case); pick one by index:\n"
                + self._format_listing(colls)
            )
        raise ValueError(
            f"no collection named '{shown}'\n"
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
    # click-event drain
    # ------------------------------------------------------------------

    def _events_files(self) -> list[Path]:
        """Every click-events file under root: root itself and each subdir."""
        candidates = [self.root / EVENTS_FILENAME]
        try:
            for child in self.root.iterdir():
                if child.is_dir():
                    candidates.append(child / EVENTS_FILENAME)
        except OSError:
            pass
        return [p for p in candidates if p.exists()]

    def _init_event_offsets(self) -> None:
        """Tail-seed: skip events that predate the session."""
        self._event_offsets = {}
        for path in self._events_files():
            try:
                self._event_offsets[path] = path.stat().st_size
            except OSError:
                pass

    def _drain_events(self) -> None:
        """Read and report click events since the last drain.

        Called once per loop iteration, before the prompt. One report
        block for the whole drain, then one deploy prompt per collection
        that saw at least one deployable event.
        """
        new_events: list[dict] = []
        for path in self._events_files():
            offset = self._event_offsets.get(path, 0)
            try:
                size = path.stat().st_size
                if size < offset:
                    offset = 0  # rotated or truncated
                if size == offset:
                    self._event_offsets[path] = offset
                    continue
                with open(path, "rb") as f:
                    f.seek(offset)
                    raw = f.read()
            except OSError:
                continue

            last_nl = raw.rfind(b"\n")
            if last_nl == -1:
                continue  # no complete line yet
            complete_bytes = raw[: last_nl + 1]
            self._event_offsets[path] = offset + len(complete_bytes)

            for line_bytes in complete_bytes.splitlines():
                line = line_bytes.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    if isinstance(event, dict):
                        new_events.append(event)
                except (json.JSONDecodeError, ValueError):
                    continue  # skip malformed lines silently

        if not new_events:
            return

        count = len(new_events)
        header = "Click activity while the shell was running"
        if count > 1:
            header += f" ({count} events)"
        self._emit(header + ":")

        deployable: dict[str, list[dict]] = {}
        need_choices: dict[str, list[dict]] = {}
        for event in new_events:
            self._emit(f"  {self._format_event_line(event)}")
            outcome = str(event.get("outcome", ""))
            if outcome in ("downloaded", "extracted_from_archive"):
                coll = str(event.get("collection_name", ""))
                deployable.setdefault(coll, []).append(event)
            elif outcome == "pending_choices":
                coll = str(event.get("collection_name", ""))
                need_choices.setdefault(coll, []).append(event)

        # FOMOD mods waiting on their option choices, before the deploy
        # question: what they install is only known once the picks are in,
        # and a collection that just gained files should be offered the
        # deploy that would place them.
        for coll_name, events in need_choices.items():
            sample = events[0]
            coll_dir = sample.get("collection_dir", "")
            if coll_dir and not Path(coll_dir).is_dir():
                self._emit(
                    f"'{_safe_text(coll_name)}' (directory gone; "
                    "nothing to choose for)"
                )
                continue
            count = len(events)
            plural = "s" if count != 1 else ""
            answer = self._confirm(
                f"{count} FOMOD mod{plural} in '{_safe_text(coll_name)}' "
                f"need option choices. Choose now? [yes/no]: "
            )
            if answer != _CONFIRM_YES:
                continue
            for event in events:
                if self._offer_fomod_choices(Path(coll_dir), event):
                    deployable.setdefault(coll_name, []).append(event)

        for coll_name, events in deployable.items():
            # One prompt per collection regardless of how many events.
            # The event's own collection_dir is authoritative; a vanished
            # directory is noted and skipped.
            sample = events[0]
            coll_dir = sample.get("collection_dir", "")
            if coll_dir and not Path(coll_dir).is_dir():
                self._emit(
                    f"'{_safe_text(coll_name)}' (directory gone; "
                    "nothing to deploy)"
                )
                continue
            answer = self._confirm(
                f"Deploy '{_safe_text(coll_name)}' now? [yes/no]: "
            )
            if answer == _CONFIRM_YES:
                self._deploy_by_path(Path(coll_dir))

    def _offer_fomod_choices(self, coll_dir: Path, event: dict) -> bool:
        """Prompt for one clicked FOMOD mod's options and install it.

        Returns True when it installed, so the caller can offer the
        deploy that places the new files. Nothing is installed without an
        explicit pick; a mod with no answers stays pending.
        """
        from .fomod_select import prompt_for_picks

        mod_name = _safe_text(str(event.get("mod_name", "")))
        file_id = event.get("file_id", 0)
        try:
            file_id = int(file_id)
        except (TypeError, ValueError):
            self._emit_failure(f"cannot pick options for '{mod_name}': no file id")
            return False

        def ask(config, archive_name, kept):
            return prompt_for_picks(config, archive_name, kept, _ShellAsker(self))

        try:
            report = self.svc.install_fomod_picks(
                coll_dir, file_id, on_fomod_prompt=ask
            )
        except Exception as e:
            self._emit_failure(f"'{mod_name}': {_safe_text(str(e))}")
            return False
        self._emit(f"  {_safe_text(report)}")
        return True

    def _format_event_line(self, event: dict) -> str:
        """One human line for a click event; all values display-safe."""
        mod = _safe_text(event.get("mod_name", ""))
        fid = event.get("file_id", 0)
        coll = _safe_text(event.get("collection_name", ""))
        outcome = str(event.get("outcome", ""))
        if outcome == "downloaded":
            return f"'{mod}' (file {fid}) downloaded into '{coll}'"
        if outcome == "extracted_from_archive":
            return f"'{mod}' (file {fid}) extracted into '{coll}'"
        if outcome == "already_downloaded":
            return f"'{mod}' (file {fid}) is already downloaded in '{coll}'"
        if outcome == "pending_download":
            return f"'{mod}' (file {fid}) is awaiting import in '{coll}'"
        if outcome == "pending_choices":
            return (
                f"'{mod}' (file {fid}) needs FOMOD option choices in "
                f"'{coll}' (run import --fomod-option to install it)"
            )
        # error
        reason = _safe_text(event.get("reason", ""))
        if mod and fid:
            return f"'{mod}' (file {fid}) failed ({reason})"
        return f"download failed ({reason})"

    def _deploy_by_path(self, coll_dir: Path) -> None:
        """Deploy a collection by its directory (from an event line)."""
        colls = self._collection_listing()
        for c in colls:
            if c.path == coll_dir:
                self._deploy_flow(c)
                return
        self._emit_failure(
            f"collection directory not found under root: "
            f"{_safe_text(str(coll_dir))}"
        )

    # ------------------------------------------------------------------
    # deploy
    # ------------------------------------------------------------------

    def _deploy_flow(self, target: CollectionSummary) -> None:
        """Dry-run, confirm, deploy. Shared by /deploy and the event drain.

        Nothing touches the game directory until the user answers yes to
        the second prompt. add_nxm's guarantee stands: nothing is ever
        deployed automatically.
        """
        from .deploy import DeployError, classify_files
        from .state import CollectionState
        from .steam import find_game_dir

        try:
            state = CollectionState(target.path)
            state.load()
        except StateError as e:
            self._emit_error(str(e) or type(e).__name__)
            return

        game_dir: Path | None = None
        if state.game_dir:
            game_dir = Path(state.game_dir)
        if game_dir is None:
            game_dir = find_game_dir(state.game_domain)
        if game_dir is None:
            self._emit_error("Could not find game directory.")
            return

        manifest_choices: dict = {}
        if state.manifest_data:
            from .manifest import CollectionManifest

            manifest = CollectionManifest.from_dict(state.manifest_data)
            manifest_choices = manifest.mod_choices

        try:
            plan = classify_files(
                target.path, state.game_domain, mod_choices=manifest_choices
            )
        except (StateError, OSError, ValueError) as e:
            self._emit_error(str(e) or type(e).__name__)
            return

        self._emit(
            f"Would deploy {plan.total_files} files to "
            f"{_safe_text(str(game_dir))} "
            f"({len(plan.game_root_files)} game root, "
            f"{len(plan.data_files)} data, {len(plan.skipped)} skipped)."
        )

        answer = self._confirm(
            f"Deploy '{_safe_text(target.name)}' now? [yes/no]: "
        )
        if answer != _CONFIRM_YES:
            self._emit("Deploy cancelled.")
            return

        try:
            result = self.svc.deploy(target.path, game_dir=game_dir, use_copy=False)
        except DeployError as e:
            self._emit_error(str(e) or type(e).__name__)
            return

        if result.errors:
            for err in result.errors:
                self._emit_error(err)
        if result.conflicts:
            self._emit(f"Conflicts ({len(result.conflicts)}):")
            for conflict in result.conflicts[:10]:
                self._emit(f"  ! {_safe_text(str(conflict))}")
            if len(result.conflicts) > 10:
                self._emit(
                    f"  ... and {len(result.conflicts) - 10} more"
                )
        if result.warnings:
            for w in result.warnings:
                self._emit_warning(str(w))

        if result.errors:
            self._emit_error(
                f"Deploy incomplete: {result.deployed_count} file(s) "
                f"deployed, {len(result.errors)} error(s)."
            )
        else:
            self._emit(
                f"Deployed {result.deployed_count} files "
                f"({len(result.conflicts)} conflicts, "
                f"{len(result.errors)} errors)"
            )

    def _cmd_deploy(self, rest: str) -> None:
        target = (
            self._resolve_collection(rest) if rest else self._require_current()
        )
        self._deploy_flow(target)

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------

    def _show_command_help(self, spec: "_CommandHelp") -> None:
        """Compact per-command help: form, one-line summary, usage block.

        Renders instead of executing; touches no state and no service.
        """
        self._emit(f"{spec.form}  {spec.summary}")
        if spec.usage:
            self._emit("usage:")
            for line in spec.usage:
                self._emit(f"  {line}")

    def _cmd_help(self, rest: str) -> None:
        if rest:
            # /help <command> is an alias for <command> --help. The
            # command word is matched with or without its leading slash.
            token = rest.split()[0]
            spec = _HELP_BY_NAME.get(token.lstrip("/").lower())
            if spec is None:
                self._emit_failure(
                    f"Unknown command '{_safe_text(token)}'. "
                    "For the command list, try /help."
                )
                return
            self._show_command_help(spec)
            return
        table = Table(title="Shell commands")
        table.add_column("Command", style="cyan", no_wrap=True)
        table.add_column("Description")
        for spec in _COMMAND_HELP:
            if not spec.in_table:
                continue
            # Escape both cells: rich markup must not swallow bracketed
            # arguments like /mods [collection].
            table.add_row(escape(spec.form), escape(spec.summary))
        self.console.print(table)
        self._emit(
            "Bare nxm:// or https:// links on a line are handled as /add."
        )
        self._emit(
            "Every command takes a bare --help (its only argument) or "
            "/help <command> for its usage."
        )

    def _cmd_quit(self, rest: str) -> None:
        raise _Quit()

    def _cmd_collections(self, rest: str) -> None:
        if rest:
            raise ValueError("usage: /collections")
        self._print_collections()

    def _print_collections(self) -> None:
        """The index-numbered collection table shared by /collections and
        bare /use."""
        colls = self._collection_listing()
        if not colls:
            self._emit(f"No collections found under {_safe_text(self.root)}.")
            return
        table = Table(title=f"Collections under {escape(_safe_text(self.root))}")
        table.add_column("#", justify="right")
        table.add_column("Name", style="cyan")
        table.add_column("Game", style="green")
        table.add_column("Mods", justify="right")
        table.add_column("Pending", justify="right")
        for i, c in enumerate(colls, 1):
            table.add_row(
                str(i),
                escape(self._scrub(_safe_text(c.name))),
                escape(self._scrub(_safe_text(c.game_domain) or "-")),
                str(c.mod_count),
                str(c.pending_count),
            )
        self.console.print(table)

    def _cmd_games(self, rest: str) -> None:
        if rest:
            raise ValueError("usage: /games")
        colls = self._collection_listing()
        if not colls:
            self._emit(f"No collections found under {_safe_text(self.root)}.")
            return
        totals: dict[str, list[int]] = {}
        for c in colls:
            row = totals.setdefault(_safe_text(c.game_domain) or "-", [0, 0])
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
            # Self-report only: shows the current selection (or its
            # absence) plus the collection list, and changes nothing.
            if self.current is None:
                self._emit("Current collection: none selected.")
            else:
                self._emit(
                    f"Current collection: '{_safe_text(self.current.name)}' "
                    f"({_safe_text(self.current.path)})."
                )
            self._print_collections()
            return
        self.current = self._resolve_collection(rest)
        self._emit(
            f"Using '{_safe_text(self.current.name)}' "
            f"({_safe_text(self.current.path)})."
        )
        # Remembered only on explicit selection: startup auto-select and
        # restore never write the memory file.
        if not _save_remembered_name(self.root, self.current.path.name):
            self._emit_warning(
                f"could not remember this selection in "
                f"{_SHELL_STATE_FILENAME}"
            )

    def _cmd_mods(self, rest: str) -> None:
        if rest:
            target = self._resolve_collection(rest)
        else:
            target = self._require_current()
        mods = list_mods(target.path)
        if not mods:
            self._emit(f"No mods tracked in '{_safe_text(target.name)}'.")
            return
        table = Table(title=f"Mods in {escape(self._scrub(_safe_text(target.name)))}")
        table.add_column("file_id", justify="right")
        table.add_column("Name", style="cyan")
        table.add_column("Version", style="green")
        table.add_column("Download status")
        for m in mods:
            table.add_row(
                str(m.file_id),
                escape(self._scrub(_safe_text(m.name))),
                escape(self._scrub(_safe_text(m.version) or "-")),
                escape(self._scrub(_safe_text(m.download_status))),
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
            nxm_result = self.svc.add_nxm(rest, mods_dir=current.path)
            self._report_nxm(nxm_result)
        elif low.startswith(("http://", "https://")):
            add_result = self.svc.add_mod(rest, current.path)
            self._report_add(add_result)
        else:
            raise ValueError(
                "/add expects an http(s) mod page URL or an nxm link"
            )

    def _report_add(self, result: AddResult) -> None:
        # Only reachable after _require_current in _cmd_add, so a
        # collection is always selected here.
        current = self._require_current()
        # Failed results render as failures before any success text.
        if not result.success:
            self._emit_failure(result.error or "the mod could not be added.")
            return
        member = self._is_collection_member(result.mod_id)
        if result.pending_download:
            p = result.pending_download
            if member:
                self._emit(self._re_add_line(result.mod_name))
            self._emit(f"Free account - download manually: {_safe_text(p.mod_name)}")
            self._emit(f"  {p.browser_url}")
            self._emit(
                f"Save the file to {_safe_text(current.path)}, then run "
                "'nexus-dl import' on that directory."
            )
            return
        if member:
            # Re-adding a mod the collection already manages: "manual"
            # would misreport it (the next sync folds it back into the
            # collection-managed set).
            self._emit(self._re_add_line(result.mod_name))
            return
        self._emit(
            f"Added '{_safe_text(result.mod_name)}' as manual mod (phase 999)."
        )

    def _re_add_line(self, mod_name) -> str:
        """The collection-member re-add wording, one shared phrasing."""
        current = self._require_current()
        return (
            f"Re-added '{_safe_text(mod_name)}' to "
            f"'{_safe_text(current.name)}'; it is a collection "
            "member, not a manual mod."
        )

    def _is_collection_member(self, mod_id: int) -> bool:
        """Is mod_id part of the current collection's cached manifest?

        Reporting-only distinction between a re-added collection member
        and a genuinely new manual mod. Never raises: when membership
        cannot be determined, the manual wording stands.
        """
        try:
            current = self._require_current()
            return mod_id in collection_manifest_mod_ids(current.path)
        except Exception:
            return False

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
                f"'{_safe_text(result.mod_name)}' (file {result.file_id}) is "
                "already downloaded in this collection."
            )
        elif result.status == "extracted_from_archive":
            if self._is_collection_member(result.mod_id):
                self._emit(self._re_add_line(result.mod_name))
            self._emit(
                f"Finished '{_safe_text(result.mod_name)}' "
                f"(file {result.file_id}) by "
                "extracting the retained archive; no download was needed."
            )
        elif result.status == "saved_to_output":
            self._emit(f"Saved {_safe_text(result.output_path)}.")
        else:
            if self._is_collection_member(result.mod_id):
                self._emit(self._re_add_line(result.mod_name))
            where = (
                f" into {_safe_text(result.collection_dir)}"
                if result.collection_dir
                else ""
            )
            self._emit(
                f"Downloaded '{_safe_text(result.mod_name)}'{where}."
            )
            if result.extracted:
                self._emit(
                    "Archive extracted. Run 'nexus-dl deploy' when ready; "
                    "nothing was deployed automatically."
                )
        for warning in result.warnings:
            self._emit_warning(warning)

    def _sync_target(self, rest: str) -> tuple[str, Path | None]:
        """Resolve the /sync target to (collection URL, destination).

        Three forms: an http(s) collection URL (synced as-is into a new
        directory under the root, named from the API), an index or name
        resolved exactly like /use, or nothing to resync the current
        collection. The index/name and resync forms use the
        collection_url the collection's state recorded at its last
        sync AND pin the destination to the collection's own directory,
        so a renamed directory or a root-as-collection layout updates
        exactly the selected collection. A state with no recorded URL
        gets an honest error pointing at the URL form instead of a
        guess.
        """
        target = rest.strip()
        if target.lower().startswith(("http://", "https://")):
            return target, None
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
                f"no collection URL is stored for "
                f"'{_safe_text(summary.name)}'; "
                "pass it explicitly\n"
                "usage: /sync <collection-url>"
            )
        return url, summary.path

    def _cmd_sync(self, rest: str) -> None:
        url, target_dir = self._sync_target(rest)
        result: SyncResult = self.svc.sync(
            url, self.root, target_dir=target_dir
        )
        # Per-mod errors render before any success text.
        for err in result.errors:
            self._emit_error(err)
        if result.pending_downloads:
            self._emit(
                f"{len(result.pending_downloads)} mod(s) need a manual "
                "download:"
            )
            for p in result.pending_downloads:
                self._emit(
                    f"  {_safe_text(p.mod_name)}: {_safe_text(p.filename)}"
                )
        summary = (
            f"Synced {result.mods_downloaded} mods "
            f"({result.mods_extracted} extracted)"
        )
        if result.errors:
            summary += f", {len(result.errors)} error(s)"
        if result.removed_skipped:
            summary += f"; {result.removed_skipped} removed mod(s) skipped"
        self._emit(summary + ".")
        if result.tracked or result.untracked:
            self._emit(
                f"Tracked {result.tracked}, untracked {result.untracked} "
                "on Nexus."
            )
        if result.conflicts:
            # Rendered like the CLI sync command: the count plus the
            # first 20 (file: winner overwrote loser), never silently.
            self._emit_warning(
                f"{len(result.conflicts)} file conflict(s) "
                "(last mod wins):"
            )
            for c in result.conflicts[:20]:
                self._emit(
                    f"  {_safe_text(c.file_path)}: {_safe_text(c.winner)} "
                    f"overwrote {_safe_text(c.loser)}"
                )
            if len(result.conflicts) > 20:
                self._emit(
                    f"  ... and {len(result.conflicts) - 20} more"
                )
        if result.collection_dir:
            self._emit(
                f"Collection directory: {_safe_text(result.collection_dir)}"
            )
        for f in result.load_order_files:
            self._emit(f"Wrote {_safe_text(f)}")

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
            shown = _safe_text(summary.name)
            self._emit(
                f"Remove '{shown}' "
                f"(file_id {summary.file_id}, mod_id {summary.mod_id}) "
                "and all its files?"
            )
            self._emit(
                "this removes the mod from the collection and it stays "
                "removed through sync/update; files on disk are kept; "
                "re-add restores it"
            )
            answer = self._confirm(
                f"Remove '{_prompt_name(summary.name)}'? [yes/no]: "
            )
            if answer != _CONFIRM_YES:
                self._emit("Cancelled; nothing was removed.")
                return

        # An id that is not in the listing cannot be removed at all:
        # remove_mod validates before mutating anything and raises
        # StateError, so the service's own error is surfaced directly
        # instead of prompting for a removal that cannot happen.
        result = self.svc.remove_mod(current.path, file_id=file_id, mod_id=mod_id)
        name = (
            _safe_text(summary.name)
            if summary is not None
            else f"mod {mod_id or file_id}"
        )
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
        """Tool-wide local report, or one collection vs the latest revision.

        Bare /status is always tool-wide (even with a collection
        selected) and never touches the network. /status <index|name>
        keeps the original per-collection revision comparison
        (svc.get_status, network) and adds the collection's local
        details.
        """
        if rest:
            self._status_collection(self._resolve_collection(rest))
        else:
            self._status_overview()

    def _status_overview(self) -> None:
        """(a) games/collections overview, (b) removals, (c) health.

        Local only: nothing here reads the network, and no collection
        needs to be selected. Every section renders independently so a
        failing section cannot hide the rest (health checks guard
        themselves and render ERROR lines instead of raising).
        """
        scan = scan_collections(self.root)

        # (a) per-game rollup (parallels /games) plus totals. State
        # values are coerced with _safe_text: one non-string value must
        # not abort the later sections.
        if scan.collections:
            totals: dict[str, list[int]] = {}
            for c in scan.collections:
                row = totals.setdefault(
                    _safe_text(c.game_domain) or "-", [0, 0, 0]
                )
                row[0] += 1
                row[1] += c.mod_count
                row[2] += c.pending_count
            table = Table(title="Games and collections")
            table.add_column("Game", style="green")
            table.add_column("Collections", justify="right")
            table.add_column("Total mods", justify="right")
            table.add_column("Pending", justify="right")
            for game in sorted(totals):
                collections, mods, pending = totals[game]
                table.add_row(
                    escape(self._scrub(game)),
                    str(collections),
                    str(mods),
                    str(pending),
                )
            self.console.print(table)
            self._emit(
                f"Total: {len(totals)} game(s), {len(scan.collections)} "
                f"collection(s), "
                f"{sum(c.mod_count for c in scan.collections)} mod(s), "
                f"{sum(c.pending_count for c in scan.collections)} pending."
            )
        else:
            self._emit(f"No collections found under {_safe_text(self.root)}.")

        # (b) removal tombstones per collection (state.removed_mods).
        if scan.collections:
            table = Table(title="Removals")
            table.add_column("Collection", style="cyan")
            table.add_column("Tombstoned mods", justify="right")
            tombstoned = 0
            for c in scan.collections:
                try:
                    count = collection_local_details(c.path).tombstone_count
                except Exception as e:
                    self._emit_warning(
                        f"could not read removal state for "
                        f"'{_safe_text(c.name)}': {type(e).__name__}"
                    )
                    table.add_row(escape(self._scrub(_safe_text(c.name))), "?")
                    continue
                tombstoned += count
                table.add_row(escape(self._scrub(_safe_text(c.name))), str(count))
            self.console.print(table)
            if tombstoned:
                self._emit(
                    f"Total removed (tombstoned): {tombstoned} mod(s). They "
                    "stay removed through sync/update; re-adding a mod "
                    "clears its tombstone."
                )

        # (c) health report: one guarded line per check.
        self._emit("Health")
        for check in collect_health(
            self.root,
            scan=scan,
            api_key_present=bool(getattr(self.svc, "_api_key", None)),
        ):
            self._render_health_check(check)

    def _render_health_check(self, check: HealthCheck) -> None:
        """One health line; a WARN/ERROR never carries a traceback."""
        color = {"OK": "green", "WARN": "yellow", "ERROR": "red"}.get(
            check.status, "yellow"
        )
        self.console.print(
            f"[{color}]{escape(check.status)}[/{color}] "
            + escape(self._scrub(_safe_text(f"{check.label}: {check.detail}")))
        )
        if check.remedy:
            self.console.print(
                f"      fix: {escape(self._scrub(_safe_text(check.remedy)))}"
            )

    def _status_collection(self, target: CollectionSummary) -> None:
        """Existing revision comparison plus the collection's local details."""
        local = collection_local_details(target.path)
        result = self.svc.get_status(target.path)
        self._emit(f"Collection: {_safe_text(result.collection_name)}")
        self._emit(f"State path: {_safe_text(local.state_path)}")
        self._emit(
            f"Collection URL: "
            f"{_safe_text(_display_url(local.collection_url)) or '-'}"
        )
        self._emit(
            f"Local: {local.mod_count} mod(s), {local.pending_count} "
            f"pending, {local.tombstone_count} removed (tombstoned)"
        )
        if result.removed_skipped:
            self._emit(
                f"Removed mods skipped: {result.removed_skipped} "
                "(tombstoned)."
            )
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
                escape(self._scrub(_safe_text(m.name))),
                escape(self._scrub(_safe_text(m.version) or "-")),
                escape(self._scrub(_safe_text(m.status))),
            )
        self.console.print(table)

    _COMMANDS: ClassVar[dict[str, Any]] = {
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
        "/deploy": _cmd_deploy,
        "/status": _cmd_status,
    }


def _parse_int(text: str, what: str) -> int:
    try:
        return int(text.strip())
    except ValueError:
        raise ValueError(
            f"{what} must be a whole number, got '{_safe_text(text)}'"
        ) from None


def _display_url(url: str) -> str:
    """URL with query and fragment stripped, for display only.

    A recorded collection URL can carry link credentials in its query
    (key/expires), so the shell never displays query strings; what is
    left is scrubbed again at render time.
    """
    return url.split("#", 1)[0].split("?", 1)[0]


def _safe_text(value, fallback: str = "-") -> str:
    """Stringify a state or user value for display; never raises.

    Hand-edited state can hold non-string scalars where text is
    expected (a numeric game_domain, say), and hostile or garbled text
    can carry terminal control characters. Values are coerced to plain
    text and control characters (ESC, newline, BEL) collapse to
    spaces, so echoed text can never move the cursor or clear the
    screen.
    """
    if isinstance(value, str):
        text = value
    elif value is None:
        return fallback
    else:
        try:
            text = str(value)
        except Exception:
            return fallback
    return re.sub(r"[\x00-\x1f\x7f]+", " ", text)


def _prompt_name(name) -> str:
    """Collection name rendered safely for the input() prompt."""
    return _safe_text(name).strip()


# Root-scoped shell memory: {"current": "<collection dir name>"}.
# One non-secret value; see _load/_save_remembered_name.
_SHELL_STATE_FILENAME = ".nexus-shell.json"


def _load_remembered_name(root: Path) -> str | None:
    """The collection dir name remembered for root, or None.

    Missing, unreadable, or corrupt files are ignored silently: a
    stale memory must never break a session. The file stores exactly
    one value, a directory name, and never a secret.
    """
    try:
        data = json.loads(
            (Path(root) / _SHELL_STATE_FILENAME).read_text()
        )
        name = data.get("current")
    except Exception:
        return None
    return name if isinstance(name, str) and name else None


def _save_remembered_name(root: Path, name: str) -> bool:
    """Remember the current collection dir name for root (best effort).

    Atomic replace so a crash mid-write cannot leave a corrupt file.
    Returns False on any I/O failure; the caller treats remembrance as
    a convenience, never a requirement.
    """
    path = Path(root) / _SHELL_STATE_FILENAME
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps({"current": name}))
        os.replace(tmp, path)
        return True
    except OSError:
        return False


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
