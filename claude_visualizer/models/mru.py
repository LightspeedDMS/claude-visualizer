"""Pure most-recently-used (MRU) file model for the MRU panel.

This module is deliberately UI-free (no ``textual`` import) so it can be
unit-tested in isolation and reused by any view.  It consumes
:class:`~claude_visualizer.events.FileModifiedEvent` instances and maintains a
newest-first, de-duplicated, capacity-bounded list of files together with the
origin metadata the panel renders (project tag, short session id, subagent
flag, last operation).

Backing store: an ``OrderedDict`` keyed by ``file_path``.  Insertion order is
oldest → newest, so the *end* of the dict is the most-recent entry.  When the
same file is touched again we move it to the end (move-to-front semantically),
and when capacity is exceeded we evict from the front (least-recently-used).
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from claude_visualizer.config import AppConfig
from claude_visualizer.events import FileModifiedEvent, FileOp

# Number of leading session-id characters kept for compact display.
_SHORT_SESSION_LEN = 8


@dataclass(frozen=True)
class MruEntry:
    """One row in the MRU panel: a file plus its origin metadata.

    ``ts`` is the event's transcript timestamp (may be ``None`` for an
    un-timestamped event); the panel formats it for display.
    """

    file_path: str
    project_tag: str
    short_session: str
    is_subagent: bool
    op: FileOp
    ts: Optional[datetime] = None


class MruModel:
    """Newest-first, deduplicated, bounded model of recently-modified files."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        # file_path -> MruEntry, ordered oldest (front) → newest (end).
        self._entries: "OrderedDict[str, MruEntry]" = OrderedDict()
        # Selected row for the view (story #3 wires keyboard navigation here).
        self.highlighted_path: Optional[str] = None

    def record(self, event: FileModifiedEvent) -> MruEntry:
        """Insert/refresh ``event``'s file at the front; enforce capacity.

        Dedup is by ``file_path``: a repeat touch moves the file to the front
        and refreshes its origin fields.  Once the model exceeds
        ``config.mru_max`` the least-recently-used entry is evicted.
        Returns the entry that was recorded.
        """
        entry = MruEntry(
            file_path=event.file_path,
            project_tag=event.project_tag,
            short_session=event.session_id[:_SHORT_SESSION_LEN],
            is_subagent=event.is_subagent,
            op=event.op,
            ts=event.ts,
        )

        # Move-to-front: drop any existing entry for this path first so the
        # re-insertion lands at the end (newest position).
        if entry.file_path in self._entries:
            del self._entries[entry.file_path]
        self._entries[entry.file_path] = entry

        # LRU fall-off: evict from the front until within capacity.
        while len(self._entries) > self._config.mru_max:
            self._entries.popitem(last=False)

        return entry

    def rows(self) -> List[MruEntry]:
        """Return entries newest-first by timestamp as a fresh list (safe to mutate).

        Sorted by ``ts`` descending so the display is chronological regardless
        of the order the pipeline drained events from multiple sessions.
        Entries with ``ts=None`` (un-timestamped) sort to the end.

        The sort key is a 2-tuple ``(has_ts, ts)`` — both fields are reversed,
        so ``True`` (has a timestamp) sorts before ``False`` (no timestamp), and
        among timestamped entries the latest ``ts`` sorts first.  This avoids
        any comparison between timezone-aware and timezone-naive datetimes.
        """
        entries = list(self._entries.values())
        entries.sort(
            key=lambda e: (e.ts is not None, e.ts),
            reverse=True,
        )
        return entries
