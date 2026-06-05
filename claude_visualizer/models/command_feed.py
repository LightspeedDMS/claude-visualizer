"""Pure rolling command-feed model for the bottom Commands panel (story #4).

This module is deliberately UI-free (no ``textual`` import) so it can be
unit-tested in isolation and reused by any view.  It consumes
:class:`~claude_visualizer.events.CommandEvent` instances (one per ``Bash``
tool_use, already emitted by the engine in story #2) and maintains a
**newest-on-top**, **NON-deduplicated**, **capacity-bounded** log of commands
together with the origin metadata the panel renders (project tag, short session
id, subagent flag) and a per-row timestamp.

Backing store: a ``collections.deque(maxlen=config.command_feed_max)``.  A
``deque`` with ``maxlen`` is the natural fit for this feed:

- ``append`` is O(1) and, at capacity, automatically evicts the entry at the
  OPPOSITE end — so pushing the newest command drops the OLDEST (AC4 "oldest
  scrolls off the bottom") with no manual bookkeeping and a *structural* upper
  bound on memory (MESSI #14 anti-unbounded).
- It performs NO deduplication: an identical command run again is a distinct
  ``append`` and therefore a distinct row (AC2 "the feed is a log").

Internal ordering is oldest → newest (left → right); :meth:`rows` returns the
*reversed* view so the newest entry is first (AC1 newest-on-top).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Deque, List, Optional

from claude_visualizer.config import AppConfig
from claude_visualizer.events import CommandEvent

# Number of leading session-id characters kept for the compact origin tag —
# matches the MRU / diff-queue models so origin tags read identically across
# all three panels.
_SHORT_SESSION_LEN = 8


@dataclass(frozen=True)
class CommandFeedEntry:
    """One row in the Commands feed: a command plus its origin + timestamp.

    Immutable so a snapshot handed to the view can never be mutated underneath
    the model.  ``ts`` is the event's transcript timestamp (may be ``None`` for
    a synthetic/un-timestamped event); the panel formats it for display.
    """

    command: str
    ts: Optional[datetime]
    project_tag: str
    short_session: str
    is_subagent: bool


class CommandFeedModel:
    """Newest-on-top, NON-deduplicated, bounded rolling log of Bash commands."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        # Oldest (left) → newest (right).  ``maxlen`` makes the oldest entry
        # fall off automatically when a newer one is appended at capacity (AC4).
        self._entries: Deque[CommandFeedEntry] = deque(maxlen=config.command_feed_max)

    def record(self, event: CommandEvent) -> CommandFeedEntry:
        """Append ``event`` as a new row (NO dedup); return the entry recorded.

        Every ``CommandEvent`` produces its own row even when identical to a
        prior one — the feed is a log, not a deduplicated set (AC2).  At
        capacity the deque's ``maxlen`` evicts the oldest entry (AC4).
        """
        entry = CommandFeedEntry(
            command=event.command,
            ts=event.ts,
            project_tag=event.project_tag,
            short_session=event.session_id[:_SHORT_SESSION_LEN],
            is_subagent=event.is_subagent,
        )
        self._entries.append(entry)
        return entry

    def rows(self) -> List[CommandFeedEntry]:
        """Return entries newest-first as a fresh list (safe to mutate)."""
        return list(reversed(self._entries))
