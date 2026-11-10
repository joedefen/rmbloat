#!/usr/bin/env python3
"""Single-line status output for the startup scan and probe phases.

Both phases run for seconds to minutes and each used to write its own escape
sequences straight to ``stderr``: the scan appended plain newline-terminated
lines while probing rewound the cursor with a bare ``\\r`` and left it there.
Mixing the two styles had three consequences:

* the progress line was never terminated, so anything that printed next (for
  example the ``Starting concurrent ffprobe`` message on stdout) landed on the
  same row as the percentages;
* a phase that never reached a terminal state left a half-drawn line behind
  when curses took over the screen;
* there was no guarantee that the *last* percentage seen was 100%.

These helpers keep each phase to one self-contained status line:

* on a terminal the line is rewritten in place, so only its last state shows
  and it is always terminated with a newline;
* when stderr is redirected, intermediate updates are dropped and only the
  opening and final states are written as ordinary lines, which keeps log
  files readable.
"""

import shutil
import sys


def _is_tty() -> bool:
    """Return True when stderr is an interactive terminal."""
    try:
        return bool(sys.stderr.isatty())
    except (AttributeError, ValueError):
        return False


def _flush() -> None:
    """Flush stderr, tolerating wrappers that do not expose ``flush``."""
    flush = getattr(sys.stderr, 'flush', None)
    if flush is not None:
        flush()


def _fit(text: str) -> str:
    """Truncate *text* so an in-place rewrite cannot wrap onto a second row.

    A wrapped status line breaks ``\\r`` based rewriting: the cursor returns to
    the start of the last physical row and leaves the wrapped remnant above it.
    Keeping the text narrower than the terminal avoids that entirely.
    """
    try:
        width = shutil.get_terminal_size((80, 24)).columns
    except (OSError, ValueError):
        width = 80
    if width <= 8 or len(text) < width:
        return text
    return text[:width - 4] + '...'


def begin(text: str) -> None:
    """Show *text* as the current status.

    On a terminal this leaves the cursor on an unterminated line so the next
    :func:`update` or :func:`end` rewrites it in place.
    """
    if _is_tty():
        sys.stderr.write(f"\r{_fit(text)}\033[K")
    else:
        sys.stderr.write(f"{text}\n")
    _flush()


def update(text: str) -> None:
    """Rewrite the status line.

    Ignored when stderr is not a terminal: a redirected stream has no cursor to
    rewind, and every intermediate percentage would become its own line.
    """
    if not _is_tty():
        return
    sys.stderr.write(f"\r{_fit(text)}\033[K")
    _flush()


def end(text: str) -> None:
    """Replace the status line with its final *text* and terminate it."""
    if _is_tty():
        sys.stderr.write(f"\r{_fit(text)}\033[K\n")
    else:
        sys.stderr.write(f"{text}\n")
    _flush()
