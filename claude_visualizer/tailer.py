"""Incremental byte-offset tailer for Claude Code transcript files.

A :class:`TailState` remembers, per file, how many bytes have been consumed
and any trailing partial line.  :func:`read_new` opens the file, seeks to the
remembered offset, reads to EOF, and returns only COMPLETE lines.

Guarantees / invariants:
- All offset math is in **bytes**.  The file is read in BINARY mode and
  ``size_seen`` advances by the true number of bytes consumed
  (``len(raw_chunk)``), never by re-encoding a decoded string.  Re-encoding
  would inflate the offset whenever a line contains bytes that are not valid
  UTF-8 (``errors="replace"`` turns each bad byte into a 3-byte U+FFFD), which
  would desync the cursor and silently corrupt/drop later lines.  Claude Code
  transcripts embed arbitrary file / tool-output content, so non-UTF-8 bytes
  are realistic — hence the binary cursor (MESSI #13 anti-silent-failure, AC8).
- A trailing fragment (bytes after the last ``\\n``) is NEVER returned as a
  line; it is held in ``partial_buffer`` (as raw bytes) until a newline
  completes it.
- ``size_seen`` always points at the byte offset just past the last ``\\n``
  that has been surfaced (so re-reads never double-emit a complete line).
- Rotation/truncation is self-healing: if the file shrank below ``size_seen``
  or its inode changed, the state resets and re-seeds from scratch.
- Cold-start (first attach) seeks ``max(0, size - seed_tail_bytes)`` and
  discards the leading partial line, so we tail recent activity without
  replaying the entire history.
- Lines longer than ``config.max_line_bytes`` are dropped (OOM guard); the cap
  is applied to the raw **byte** length of each complete line.
- The OOM guard ALSO bounds the still-accumulating ``partial_buffer``: a single
  newline-less write (e.g. a Claude Code 'Write' tool emitting a multi-MB JSONL
  line) cannot grow the buffer without limit.  When the trailing fragment (raw
  bytes, no ``\\n`` yet) would exceed ``config.max_line_bytes`` it is discarded
  rather than retained, the state enters an ``overlong_skip`` realign, and every
  subsequent read keeps dropping bytes until that line's ``\\n`` closes it — the
  line AFTER the overlong one then resumes cleanly.  The drop is deliberate and
  logged at DEBUG (never a silent swallow), and ``size_seen`` still counts every
  discarded byte so the cursor stays byte-exact (MESSI #14 / #13, AC8).
- A vanished file or permission error yields ``[]`` (never raises) so the
  poll loop tolerates files disappearing between scans.
- Each complete line is decoded with ``errors="replace"`` for display/parse;
  the replacement is purely cosmetic and never feeds back into the offset.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import List

from claude_visualizer.config import AppConfig

logger = logging.getLogger(__name__)

# Newline separator, in bytes — lines are split on raw bytes so the cursor math
# stays byte-exact regardless of multi-byte / invalid UTF-8 content.
_NEWLINE = b"\n"


@dataclass
class TailState:
    """Mutable per-file cursor for incremental tailing.

    ``inode is None`` marks a cold-start (never read): the next
    :func:`read_new` performs the tail-seed.  Once read, ``inode`` holds the
    file's inode so rotation can be detected.

    ``partial_buffer`` holds RAW BYTES (not text): the trailing fragment after
    the last newline, carried forward so the byte cursor never has to round-trip
    through a (lossy) decode/encode cycle.
    """

    path: str
    inode: int | None = None
    size_seen: int = 0
    partial_buffer: bytes = b""
    # Set when a cold-start seed landed inside a line that had no newline in
    # the seeded chunk: the discarded partial head is still "open", so the
    # NEXT read must also drop everything up to its first newline (otherwise
    # that newline's empty leading segment would surface as a blank line).
    pending_seed_skip: bool = False
    # Set when the accumulating trailing fragment (still no newline) grew past
    # ``config.max_line_bytes``.  The over-cap fragment is discarded — held to
    # ``b""`` instead of growing unbounded — and this flag marks the overlong
    # line as still "open": the NEXT read keeps dropping bytes up to its first
    # newline (same realign mechanic as ``pending_seed_skip``), so the overlong
    # line is dropped whole and the line AFTER it resumes cleanly.  ``size_seen``
    # still counts every discarded byte, so the cursor stays byte-exact.
    overlong_skip: bool = False


def _reset(state: TailState) -> None:
    """Forget consumed bytes and any buffered fragment (rotation/truncation)."""
    state.size_seen = 0
    state.partial_buffer = b""
    state.pending_seed_skip = False
    state.overlong_skip = False


def _split_complete_lines(
    data: bytes, drop_leading_partial: bool
) -> tuple[List[bytes], bytes]:
    """Split ``data`` (raw bytes) into (complete_lines, trailing_fragment).

    Complete lines are the byte segments terminated by ``\\n``.  Any bytes
    after the final ``\\n`` form the trailing fragment (carried forward as
    bytes).

    When ``drop_leading_partial`` is True (cold-start seek landed mid-line),
    the first segment up to the first ``\\n`` is discarded because it is the
    tail of a line whose head we skipped.
    """
    segments = data.split(_NEWLINE)
    # The last element is always the trailing fragment (possibly b"").
    trailing = segments[-1]
    complete = segments[:-1]
    if drop_leading_partial and complete:
        complete = complete[1:]
    elif drop_leading_partial and not complete:
        # No newline at all in the seeded chunk: the whole thing is a partial
        # head we must discard, not buffer.
        return [], b""
    return complete, trailing


def read_new(state: TailState, config: AppConfig) -> List[str]:
    """Read newly-appended COMPLETE lines from ``state.path``.

    Mutates ``state`` (advances ``size_seen``, updates ``partial_buffer`` and
    ``inode``).  Returns the list of complete lines surfaced by this call,
    oldest first, each decoded to ``str`` (``errors="replace"``).  Never
    raises: missing/vanished files yield ``[]``.

    The file is read in binary mode and the cursor advances by the exact byte
    count consumed, so a line containing invalid UTF-8 never desyncs the
    offset (see module docstring / MESSI #13).
    """
    try:
        st = os.stat(state.path)
    except (FileNotFoundError, PermissionError, OSError):
        return []

    current_size = st.st_size
    current_inode = st.st_ino

    cold_start = state.inode is None
    rotated = (not cold_start) and current_inode != state.inode
    truncated = (not cold_start) and current_size < state.size_seen

    drop_leading_partial = False
    # Why we are dropping a leading partial this read (None = not dropping).
    # Determines which "still open" flag the continuation is carried back to,
    # so a cold-start seed skip and an overlong-line skip never cross-wire.
    drop_cause: str | None = None

    if cold_start:
        # Tail-seed: start near the end so we don't replay full history.
        start = max(0, current_size - config.seed_tail_bytes)
        state.size_seen = start
        state.partial_buffer = b""
        state.pending_seed_skip = False
        state.overlong_skip = False
        drop_leading_partial = start > 0
        drop_cause = "seed" if drop_leading_partial else None
    elif rotated or truncated:
        _reset(state)
        drop_leading_partial = False
    elif state.pending_seed_skip:
        # A prior seed dropped a partial head that had no newline yet; keep
        # dropping until this read's first newline closes it.
        drop_leading_partial = True
        drop_cause = "seed"
    elif state.overlong_skip:
        # A prior read discarded an over-cap newline-less fragment; the overlong
        # line is still open, so keep dropping bytes until its newline closes it
        # — then the line AFTER it resumes cleanly (same mechanic as seed skip).
        drop_leading_partial = True
        drop_cause = "overlong"

    # Always record the current inode (covers cold-start and rotation).
    state.inode = current_inode

    if current_size <= state.size_seen and not state.partial_buffer:
        # Nothing new to read and no buffered fragment to worry about.
        return []

    try:
        with open(state.path, "rb") as fh:
            fh.seek(state.size_seen)
            chunk = fh.read()
    except (FileNotFoundError, PermissionError, OSError):
        return []

    if not chunk:
        return []

    # Advance the byte cursor by EXACTLY the number of bytes read — the true
    # byte count, never a re-encoded length.  This is the crux of the fix:
    # invalid UTF-8 in `chunk` cannot inflate the offset.
    state.size_seen += len(chunk)

    # On a fresh seed (or while still skipping a seeded / overlong partial head)
    # we may have landed mid-line; otherwise prepend the byte fragment we were
    # holding from the previous read.
    if drop_leading_partial:
        complete, trailing = _split_complete_lines(chunk, drop_leading_partial=True)
        # If no newline appeared, the discarded head is still open: keep
        # skipping on the next read. A newline closes it. Carry the "still open"
        # state back to ONLY the flag that caused this drop so a seed skip and
        # an overlong skip never bleed into each other.
        still_open = _NEWLINE not in chunk
        state.pending_seed_skip = still_open and drop_cause == "seed"
        state.overlong_skip = still_open and drop_cause == "overlong"
    else:
        combined = state.partial_buffer + chunk
        complete, trailing = _split_complete_lines(combined, drop_leading_partial=False)

    # Bound the accumulating fragment (MESSI #14 anti-unbounded-loop / OOM
    # guard).  ``trailing`` is the still-open line (no newline yet); the
    # COMPLETE-line cap below cannot help it because it is not yet a line.  A
    # single newline-less multi-MB write (e.g. a Claude Code 'Write' tool line)
    # would otherwise grow ``partial_buffer`` without limit.  When it exceeds
    # the cap we DELIBERATELY drop it — held to b"" rather than retained — and
    # enter the overlong realign so the next read keeps discarding until this
    # line's newline closes it.  ``size_seen`` already counted every byte read,
    # so the cursor stays byte-exact across the drop (no desync, no replay).
    if len(trailing) > config.max_line_bytes:
        logger.debug(
            "tailer: discarding overlong line fragment in %s "
            "(%d bytes accumulated, cap=%d); realigning to next newline",
            state.path,
            len(trailing),
            config.max_line_bytes,
        )
        trailing = b""
        state.overlong_skip = True
        state.pending_seed_skip = False

    state.partial_buffer = trailing

    # OOM guard: drop any complete line whose RAW byte length exceeds the cap,
    # then decode the survivors per-line for display/parse.  Decoding happens
    # only AFTER the byte-based offset and cap are settled, so the lossy
    # replacement never affects the cursor.
    result: List[str] = [
        line.decode("utf-8", errors="replace")
        for line in complete
        if len(line) <= config.max_line_bytes
    ]
    return result
