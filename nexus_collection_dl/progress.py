"""A progress line for long terminal operations.

Two shapes, one call:

- On a TTY the line is updated in place, so a 738-file deploy is one
  moving line rather than 738 lines of scrollback.
- Off a TTY (a pipe, a log file, a harness capture) the same calls print
  periodic lines on a time cadence instead, so the reader sees movement
  without a flood.

Disabled is silent, which is what ``--json`` needs: that output is
parsed, and progress prose in the middle of it would break the parse.

Progress is diagnostics and goes to stderr, never stdout, so piping the
real output somewhere keeps the pipe clean.

This is deliberately not Rich. Rich's Progress is a bar with its own
console, spinner, and refresh loop; what is wanted here is one line that
moves, usable from a library call with no console in scope.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import TextIO


class ProgressLine:
    """One moving progress line. See the module docstring for the shapes.

    ``clock`` is injectable so tests can step time without sleeping.
    """

    def __init__(
        self,
        total: int,
        label: str,
        *,
        stream: TextIO | None = None,
        is_tty: bool | None = None,
        enabled: bool = True,
        interval: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.total = total
        self.label = label
        self.stream: TextIO = sys.stderr if stream is None else stream
        self.is_tty = (
            bool(getattr(self.stream, "isatty", lambda: False)())
            if is_tty is None
            else is_tty
        )
        self.enabled = enabled
        self.interval = interval
        self._clock = clock
        self._started = clock()
        self._last_print = 0.0
        self._last_width = 0
        self._last_done = 0
        self._last_detail = ""

    def _render(self, done: int, detail: str) -> str:
        counter = f"{done}/{self.total}" if self.total else self.label
        head = f"{self.label} {counter}" if self.total else self.label
        return f"{head}  {detail}" if detail else head

    def update(self, done: int, detail: str = "") -> None:
        """Report progress. Cheap to call; the cadence is enforced here."""
        if not self.enabled:
            return
        self._last_done = done
        self._last_detail = detail
        now = self._clock()
        if self.is_tty:
            # Every call repaints: it is one line and the terminal
            # absorbs the overwrite.
            self._paint(self._render(done, detail))
            self._last_print = now
        else:
            # Off a TTY only a line on the cadence, so a long loop is a
            # trickle of lines rather than one per file.
            if self._last_print != 0.0 and (now - self._last_print) < self.interval:
                return
            self._emit(self._render(done, detail))
            self._last_print = now

    def close(self, detail: str = "") -> None:
        """End the current line: land the last state and move past it.

        Resumable on purpose. A caller interleaving other output (the
        deploy phases print their own messages around the write loop)
        closes the line so the message gets its own row, and the next
        update() starts a fresh one. The last state lands even when it
        fell between cadence ticks, so the reader is never left holding
        a stale count.
        """
        if not self.enabled:
            return
        text = self._render(self._last_done, detail or self._last_detail)
        if self.is_tty:
            self._paint(text)
            self.stream.write("\n")
            self.stream.flush()
        else:
            self._emit(text)
        self._last_width = 0
        self._last_print = 0.0

    def _paint(self, text: str) -> None:
        """Overwrite the current line on a TTY."""
        pad = max(0, self._last_width - len(text))
        self.stream.write("\r" + text + (" " * pad))
        self.stream.flush()
        self._last_width = len(text)

    def _emit(self, text: str) -> None:
        self.stream.write(text + "\n")
        self.stream.flush()
