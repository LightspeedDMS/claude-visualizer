"""Pure diff computation for the Live Diff panel (story #3).

This module is deliberately UI-free (no ``textual`` / ``rich`` import) so the
diff *logic* is unit-testable in isolation and reusable by any view.  It turns
a :class:`~claude_visualizer.events.FileModifiedEvent` into a structured list
of :class:`DiffSegment` ``(kind, text)`` pairs; the UI maps each kind to a
colour via the :data:`COLOR_FOR_KIND` data table.

Two shapes, per the read-only constraint:

- **Edit** (``old_string`` → ``new_string``): a line diff computed with stdlib
  :class:`difflib.SequenceMatcher`.  Equal lines → ``CONTEXT`` (dim), removed
  lines → ``DEL`` (red), added lines → ``ADD`` (green).  (AC1)
- **Write** (``full_content``): rendered as labelled WHOLE-FILE ADDITIONS —
  a single ``HEADER`` marker followed by one ``ADD`` per line.  There is NO
  fabricated before/after, because a read-only tail cannot know the prior
  on-disk content (so a Write is honestly "all new", never a synthetic diff).
  (AC2)

The combined body is capped at ``config.diff_max_lines``; the overflow is
replaced by one trailing ``TRUNCATION`` footer naming how many lines were
omitted (AC10).
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

from claude_visualizer.config import AppConfig
from claude_visualizer.events import FileModifiedEvent, FileOp


class DiffKind(Enum):
    """Classification of one rendered diff line, mapped to a colour by the UI."""

    ADD = "ADD"  # a line present only in the new content (green)
    DEL = "DEL"  # a line present only in the old content (red)
    CONTEXT = "CONTEXT"  # an unchanged line shown for context (dim)
    HEADER = "HEADER"  # a structural label (e.g. whole-file-write marker)
    TRUNCATION = "TRUNCATION"  # the "…(truncated, N more lines)" footer


# Colour mapping kept as DATA (not baked into a renderer) so the logic stays
# pure/testable and the UI simply looks up the style for each segment kind.
COLOR_FOR_KIND = {
    DiffKind.ADD: "green",
    DiffKind.DEL: "red",
    DiffKind.CONTEXT: "dim",
    DiffKind.HEADER: "bold cyan",
    DiffKind.TRUNCATION: "dim",
}

# Marker text that flags a Write rendering as whole-file additions (so the
# viewer never mistakes all-green output for a real before/after diff).
WHOLE_FILE_WRITE_LABEL = "whole-file write"


@dataclass(frozen=True)
class DiffSegment:
    """One renderable diff line: its semantic ``kind`` plus the display text.

    ``line_no`` is the 1-based source line number shown in the gutter:
    - CONTEXT / ADD: new-side line number
    - DEL: old-side line number
    - HEADER / TRUNCATION: ``None`` (no gutter number for structural markers)
    """

    kind: DiffKind
    text: str
    line_no: Optional[int] = None


def _split_lines(text: Optional[str]) -> List[str]:
    """Split content into lines, treating ``None`` as empty (no lines)."""
    if not text:
        return []
    return text.split("\n")


def _edit_segments(event: FileModifiedEvent) -> List[DiffSegment]:
    """Line-diff ``old_string`` → ``new_string`` into ADD/DEL/CONTEXT (AC1)."""
    old_lines = _split_lines(event.old_string)
    new_lines = _split_lines(event.new_string)
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)

    segments: List[DiffSegment] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k, line in enumerate(old_lines[i1:i2]):
                segments.append(
                    DiffSegment(DiffKind.CONTEXT, f"  {line}", line_no=j1 + k + 1)
                )
        elif tag == "delete":
            for k, line in enumerate(old_lines[i1:i2]):
                segments.append(
                    DiffSegment(DiffKind.DEL, f"- {line}", line_no=i1 + k + 1)
                )
        elif tag == "insert":
            for k, line in enumerate(new_lines[j1:j2]):
                segments.append(
                    DiffSegment(DiffKind.ADD, f"+ {line}", line_no=j1 + k + 1)
                )
        elif tag == "replace":
            # A replace is the old block removed AND the new block added.
            for k, line in enumerate(old_lines[i1:i2]):
                segments.append(
                    DiffSegment(DiffKind.DEL, f"- {line}", line_no=i1 + k + 1)
                )
            for k, line in enumerate(new_lines[j1:j2]):
                segments.append(
                    DiffSegment(DiffKind.ADD, f"+ {line}", line_no=j1 + k + 1)
                )
    return segments


def _write_segments(event: FileModifiedEvent) -> List[DiffSegment]:
    """Render a Write as a labelled header + all-ADD body, no DEL (AC2)."""
    header = DiffSegment(
        DiffKind.HEADER,
        f"{WHOLE_FILE_WRITE_LABEL}: {event.file_path}",
    )
    body = [
        DiffSegment(DiffKind.ADD, f"+ {line}", line_no=i + 1)
        for i, line in enumerate(_split_lines(event.full_content))
    ]
    return [header, *body]


def _is_body(kind: DiffKind) -> bool:
    """Body lines are the diff content subject to the line cap (not headers)."""
    return kind in (DiffKind.ADD, DiffKind.DEL, DiffKind.CONTEXT)


def _truncate(segments: List[DiffSegment], max_lines: int) -> List[DiffSegment]:
    """Cap BODY segments at ``max_lines``; append a footer for the overflow.

    Headers are preserved as-is (they are structural, not body lines).  When
    the body exceeds the cap the surplus body lines are dropped and a single
    ``…(truncated, N more lines)`` footer is appended (AC10).
    """
    kept: List[DiffSegment] = []
    body_kept = 0
    body_total = sum(1 for s in segments if _is_body(s.kind))
    for seg in segments:
        if _is_body(seg.kind):
            if body_kept >= max_lines:
                continue  # drop overflow body lines; footer reports the count
            body_kept += 1
        kept.append(seg)

    omitted = body_total - body_kept
    if omitted > 0:
        kept.append(
            DiffSegment(
                DiffKind.TRUNCATION,
                f"…(truncated, {omitted} more lines)",
            )
        )
    return kept


def compute_diff(event: FileModifiedEvent, config: AppConfig) -> List[DiffSegment]:
    """Compute the structured, colour-tagged diff for ``event``.

    Edit → unified line diff (ADD/DEL/CONTEXT); Write → labelled whole-file
    additions (HEADER + ADD only).  The body is truncated to
    ``config.diff_max_lines`` with a trailing footer when it overflows.
    """
    if event.op == FileOp.WRITE:
        segments = _write_segments(event)
    else:
        segments = _edit_segments(event)
    return _truncate(segments, config.diff_max_lines)
