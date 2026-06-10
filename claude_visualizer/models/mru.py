"""Pure most-recently-used (MRU) file model for the MRU panel.

This module is deliberately UI-free (no ``textual`` import) so it can be
unit-tested in isolation and reused by any view.  It consumes
:class:`~claude_visualizer.events.FileModifiedEvent` instances and maintains a
newest-arrival-first, event-keyed, capacity-bounded list of file modification
events together with the origin metadata the panel renders.

Backing store: an ``OrderedDict`` keyed by ``(file_path, ts)``.  Insertion
order is oldest-arrival → newest-arrival, so the *end* of the dict is the
most-recent entry.  When the same ``(file_path, ts)`` key is seen again we
move it to the end (move-to-front semantically).  When capacity is exceeded
we evict from the front (least-recently-arrived).

Each distinct ``(file_path, ts)`` pair is a separate row — the panel is an
event log, not a file list.  Same file path with different timestamps produces
separate rows.  ``rows()`` returns entries in newest-arrival-first order
(insertion order reversed), with NO timestamp sorting.

The old ``highlighted_path`` (``str``) attribute is retained for backward
compatibility with existing code that has not yet migrated.  New code should
use ``highlighted_key`` (``tuple | None``) which holds the ``event_key`` of
the currently-highlighted entry.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from claude_visualizer.config import AppConfig
from claude_visualizer.events import FileModifiedEvent, FileOp

# Number of leading session-id characters kept for compact display.
_SHORT_SESSION_LEN = 8


@dataclass(frozen=True)
class MruEntry:
    """One row in the MRU panel: a file modification event plus its origin metadata.

    ``ts`` is the event's transcript timestamp (may be ``None`` for an
    un-timestamped event); the panel formats it for display.

    ``event_key`` is the ``(file_path, ts)`` tuple that uniquely identifies
    this event in the model's backing store.
    """

    file_path: str
    project_tag: str
    short_session: str
    is_subagent: bool
    op: FileOp
    ts: Optional[datetime] = None

    @property
    def event_key(self) -> Tuple[str, Optional[datetime]]:
        """Return the ``(file_path, ts)`` key for this entry."""
        return (self.file_path, self.ts)


class MruModel:
    """Newest-arrival-first, event-keyed, bounded model of file modification events."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        # (file_path, ts) -> MruEntry, ordered oldest-arrival (front) → newest-arrival (end).
        self._entries: "OrderedDict[Tuple[str, Optional[datetime]], MruEntry]" = (
            OrderedDict()
        )
        # Backward-compat: path-based highlight (old callers that haven't migrated).
        self.highlighted_path: Optional[str] = None
        # New: event-key-based highlight; takes precedence in new code.
        self.highlighted_key: Optional[Tuple[str, Optional[datetime]]] = None

    def record(self, event: FileModifiedEvent) -> MruEntry:
        """Insert ``event`` as a new entry; enforce capacity.

        The key is ``(event.file_path, event.ts)``.  If that exact key already
        exists it is moved to the most-recent position (move-to-front) and its
        origin fields are refreshed.  Same file path with a *different* timestamp
        is always a separate entry — no dedup across distinct events.

        Once the model exceeds ``config.mru_max`` the least-recently-arrived
        entry is evicted.  Returns the entry that was recorded.
        """
        entry = MruEntry(
            file_path=event.file_path,
            project_tag=event.project_tag,
            short_session=event.session_id[:_SHORT_SESSION_LEN],
            is_subagent=event.is_subagent,
            op=event.op,
            ts=event.ts,
        )

        key = entry.event_key  # (file_path, ts)

        # Move-to-front for the same (path, ts) key: drop existing so re-insertion
        # lands at the end (newest-arrival position).
        if key in self._entries:
            del self._entries[key]
        self._entries[key] = entry

        # LRU fall-off: evict from the front until within capacity.
        while len(self._entries) > self._config.mru_max:
            self._entries.popitem(last=False)

        return entry

    def rows(self) -> List[MruEntry]:
        """Return entries newest-arrival-first as a fresh list (safe to mutate).

        Order is the reverse of insertion order (newest arrival at index 0).
        No timestamp sorting is applied — arrival order IS display order.
        """
        entries = list(self._entries.values())
        entries.reverse()
        return entries
