"""FIFO display-queue state machine for the Live Diff panel (story #3).

This module is pure and UI-free (no ``textual`` import): it owns ALL of the
diff panel's timing, scrolling, overflow and idle behaviour, and exposes a
single :meth:`DiffQueueModel.tick` that returns an immutable
:class:`DisplayState` snapshot for the UI to render.  Time is injected as a
``now: Callable[[], float]`` so the whole state machine is deterministic under
test (a fake clock) with no real sleeps.

Behaviour (acceptance criteria from issue #3):

- **No coalescing (updated).**  Each ``(file_path, ts)`` pair is a separate
  queue entry — same file with different timestamps is always a new entry.
  Exact same ``(file_path, ts)`` key still deduplicates (move-to-front).
- **Bounded dwell + auto-scroll (AC6).**  The current diff dwells at least
  ``min_dwell_seconds`` and at most ``max_dwell_seconds``; its scroll offset
  advances from 0 to the bottom across ``[0, max_dwell]`` so a tall diff visibly
  moves.  The queue advances to the next file once the current diff has been
  fully scrolled past the minimum dwell, or unconditionally at the maximum.
- **Idle rests on latest (AC7).**  When the queue drains the panel keeps
  showing the latest file's diff (``is_idle``) — it never blanks.
- **Overflow drops stalest-unseen (AC8).**  The queue is capped at
  ``diff_queue_max``; on overflow the stalest UNSEEN entry (front of the FIFO)
  is dropped — never silently — and surfaced through ``plus_n_more``.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Optional, Tuple

from claude_visualizer.config import AppConfig
from claude_visualizer.diffing import DiffSegment, compute_diff
from claude_visualizer.events import FileModifiedEvent, FileOp

# Number of leading session-id characters kept for the compact origin tag.
_SHORT_SESSION_LEN = 8

# Type alias for event keys: (file_path, ts) tuple.
EventKey = Tuple[str, Optional[datetime]]


@dataclass(frozen=True)
class DisplayState:
    """Immutable snapshot of what the diff panel should show right now.

    The UI renders ``visible_segments`` (the scroll window of height
    ``viewport_height``) coloured by each segment's kind, with a header built
    from ``model``/``used_thinking``/``file_path``/origin/``ts``, and a ``+N more``
    badge when ``plus_n_more`` is non-zero.  ``ts`` is the displayed event's
    transcript timestamp (may be ``None``); the header formats it for display.

    ``event_key`` is the ``(file_path, ts)`` tuple identifying this event — the
    same key used by the MRU model and diff queue's internal stores.  ``None``
    in the pre-activity empty state.
    """

    file_path: Optional[str]
    segments: List[DiffSegment]
    visible_segments: List[DiffSegment]
    scroll_offset: int
    model: Optional[str]
    used_thinking: bool
    is_subagent: bool
    project_tag: str
    short_session: str
    op: Optional[FileOp]
    plus_n_more: int
    is_idle: bool
    ts: Optional[datetime] = None
    is_pinned: bool = False
    event_key: Optional[EventKey] = None


@dataclass
class _Item:
    """A queued/displayed file plus its event (keyed by (file_path, ts))."""

    file_path: str
    event: FileModifiedEvent


class DiffQueueModel:
    """FIFO, event-keyed display queue with dwell/scroll/idle/overflow."""

    def __init__(self, config: AppConfig, now: Callable[[], float]) -> None:
        self._config = config
        self._now = now
        # Pending UNSEEN files, FIFO: front (first) is the stalest, back is the
        # most recently arrived.  Keyed by (file_path, ts) so same-key events
        # are deduplicated (move-to-front); different ts = different entry.
        self._queue: "OrderedDict[EventKey, _Item]" = OrderedDict()
        # The item currently being displayed (None until the first tick).
        self._current: Optional[_Item] = None
        # Clock value at which the current item began displaying (dwell anchor).
        self._current_started_at: float = 0.0
        # Count of UNSEEN files dropped due to overflow — folded into the
        # ``+N more`` indicator so a drop is never silent (AC8).
        self._overflow_dropped: int = 0

        # Per-event cache used to rebuild any event's diff on pin.
        # Keyed by (file_path, ts) tuple — updated in record() so any event
        # the user can see in the MRU can be pinned even after the diff queue
        # has already advanced past it.
        self._event_by_key: "OrderedDict[EventKey, FileModifiedEvent]" = OrderedDict()

        # Pin state set by pin(); cleared by tick() when both expiry and
        # new-event conditions are met.
        self._pinned_key: Optional[EventKey] = None
        self._pin_time: float = 0.0
        self._new_event_since_pin: bool = False
        # Manual scroll offset within the pinned diff (mouse-wheel scroll).
        # Reset to 0 on every new pin() call; adjusted by scroll_pin_by().
        self._pin_scroll: int = 0

    # -- introspection (tests) --------------------------------------------

    def queued_count(self) -> int:
        """Number of pending UNSEEN items (excludes the displayed one)."""
        return len(self._queue)

    # -- recording ---------------------------------------------------------

    def record(self, event: FileModifiedEvent) -> None:
        """Record a file modification event, keyed by (file_path, ts).

        The key is ``(event.file_path, event.ts)``.  If that exact key already
        exists in the queue it is moved to the back (most-recent position) and
        its event is refreshed.  Different timestamps for the same file path
        are always separate entries — no path-based coalescing.

        On overflow the stalest UNSEEN front entry is dropped (AC8).
        """
        key: EventKey = (event.file_path, event.ts)

        # Update the per-event pin cache (bounded LRU).
        self._event_by_key[key] = event
        self._event_by_key.move_to_end(key)
        while len(self._event_by_key) > self._config.cache_max_file_events:
            self._event_by_key.popitem(last=False)

        if self._pinned_key is not None:
            self._new_event_since_pin = True

        # If the current displayed item has exactly this key, refresh it.
        if (
            self._current is not None
            and self._current.file_path == event.file_path
            and self._current.event.ts == event.ts
        ):
            self._current.event = event
            return

        # Move-to-back in the FIFO queue for the same key (dedup, keep recency).
        if key in self._queue:
            self._queue[key].event = event
            self._queue.move_to_end(key)
            return

        self._queue[key] = _Item(file_path=event.file_path, event=event)
        self._enforce_cap()

    def _enforce_cap(self) -> None:
        """Drop stalest-UNSEEN front entries beyond ``diff_queue_max`` (AC8)."""
        cap = self._config.diff_queue_max
        while len(self._queue) > cap:
            self._queue.popitem(last=False)  # FIFO front = stalest unseen
            self._overflow_dropped += 1

    def pin(self, key: EventKey, now: float) -> bool:
        """Pin the event identified by ``key`` for display (returns False if unknown).

        ``key`` is a ``(file_path, ts)`` tuple matching an event previously
        passed to :meth:`record`.  Returns ``False`` if the key is not in the
        event cache (event was never seen or was evicted).
        """
        if key not in self._event_by_key:
            return False
        self._pinned_key = key
        self._pin_time = now
        self._new_event_since_pin = False
        self._pin_scroll = 0
        return True

    def scroll_pin_by(self, delta: int, viewport_height: int) -> None:
        """Scroll the pinned diff by ``delta`` lines (positive=down, negative=up).

        No-op when not pinned.  Clamps to [0, max_scroll] where
        max_scroll = len(segments) - viewport_height.
        """
        if self._pinned_key is None:
            return
        event = self._event_by_key.get(self._pinned_key)
        if event is None:
            return
        segments = compute_diff(event, self._config)
        max_scroll = max(0, len(segments) - max(1, viewport_height))
        self._pin_scroll = max(0, min(self._pin_scroll + delta, max_scroll))

    def fast_forward_to_latest(self, now: float) -> None:
        """Skip cache-replay animation: rest immediately on the most-recent item.

        Discards all pending queue entries and places the newest item directly
        into the display slot so the very first tick shows the latest file as
        idle rather than animating through every replayed entry in order.
        ``_event_by_key`` is deliberately left intact so any replayed file can
        still be pinned via click or keyboard.
        """
        if not self._queue:
            return
        last_key, last_item = self._queue.popitem(last=True)
        self._queue.clear()
        self._current = last_item
        self._current_started_at = now

    # -- ticking -----------------------------------------------------------

    def tick(self, now: float, viewport_height: int) -> Optional[DisplayState]:
        """Advance the state machine to ``now`` and return what to display.

        Returns ``None`` only when nothing has ever been shown and the queue is
        empty; otherwise it always returns a populated state (resting on the
        latest file when idle — never blank).
        """
        if self._pinned_key is not None:
            elapsed_pin = now - self._pin_time
            expired = elapsed_pin >= self._config.min_pin_seconds
            if expired and self._new_event_since_pin:
                # Conditions met: release pin.  Reset dwell anchor so the
                # resumed queue item gets a fresh window, not a stale one.
                self._pinned_key = None
                self._pin_scroll = 0
                self._current_started_at = now
                # Fall through to normal queue logic below.
            else:
                # Still pinned: build state from cached event.
                event = self._event_by_key.get(self._pinned_key)
                if event is not None:
                    segments = compute_diff(event, self._config)
                    window = max(1, viewport_height)
                    visible = segments[self._pin_scroll : self._pin_scroll + window]
                    return DisplayState(
                        file_path=self._pinned_key[0],
                        segments=segments,
                        visible_segments=visible,
                        scroll_offset=self._pin_scroll,
                        model=event.model,
                        used_thinking=event.used_thinking,
                        is_subagent=event.is_subagent,
                        project_tag=event.project_tag,
                        short_session=event.session_id[:_SHORT_SESSION_LEN],
                        op=event.op,
                        plus_n_more=self._plus_n_more(),
                        is_idle=False,
                        ts=event.ts,
                        is_pinned=True,
                        event_key=self._pinned_key,
                    )
                else:
                    # Pinned event vanished from cache — clear and fall through.
                    self._pinned_key = None
                    self._pin_scroll = 0

        # Promote the first item the moment anything is queued.  A FRESHLY
        # promoted diff is rendered for this frame and only becomes eligible to
        # advance on a LATER tick (a strictly greater clock value) — otherwise,
        # with min_dwell == 0, an item could be promoted and advanced past in
        # the same tick, silently skipping it.
        if self._current is None:
            if not self._queue:
                return self._empty_state()
            self._promote_next(now)
            assert self._current is not None  # _promote_next always sets _current
            segments = compute_diff(self._current.event, self._config)
            max_scroll = max(0, len(segments) - max(1, viewport_height))
            scroll_offset = self._scroll_for(now - self._current_started_at, max_scroll)
            return self._build_state(segments, scroll_offset, viewport_height)

        assert self._current is not None  # invariant after promotion

        segments = compute_diff(self._current.event, self._config)
        max_scroll = max(0, len(segments) - max(1, viewport_height))
        elapsed = now - self._current_started_at
        scroll_offset = self._scroll_for(elapsed, max_scroll)
        fully_shown = max_scroll == 0 or scroll_offset >= max_scroll

        if self._should_advance(elapsed, fully_shown):
            self._promote_next(now)
            assert self._current is not None  # _promote_next always sets _current
            segments = compute_diff(self._current.event, self._config)
            max_scroll = max(0, len(segments) - max(1, viewport_height))
            scroll_offset = self._scroll_for(0.0, max_scroll)

        return self._build_state(segments, scroll_offset, viewport_height)

    # -- advance / scroll helpers -----------------------------------------

    def _should_advance(self, elapsed: float, fully_shown: bool) -> bool:
        """True when the queue should advance to the next file (AC6).

        Requires a next file to exist (else we REST on the current diff, AC7).
        Advance when the diff has been fully scrolled past the minimum dwell,
        or unconditionally once the maximum dwell elapses.
        """
        if not self._queue:
            return False
        if elapsed >= self._config.max_dwell_seconds:
            return True
        return fully_shown and elapsed >= self._config.min_dwell_seconds

    def _scroll_for(self, elapsed: float, max_scroll: int) -> int:
        """Scroll offset that ramps 0 → ``max_scroll`` across ``[0, max_dwell]``.

        A short diff (``max_scroll == 0``) never scrolls.  A tall diff is fully
        scrolled by the time the maximum dwell elapses, giving a readable, time-
        proportional auto-scroll the UI can render as a moving window (AC6).
        """
        if max_scroll <= 0:
            return 0
        span = self._config.max_dwell_seconds
        if span <= 0:
            return max_scroll
        progress = max(0.0, min(1.0, elapsed / span))
        return min(max_scroll, math.floor(progress * max_scroll))

    def _promote_next(self, now: float) -> None:
        """Move the front of the FIFO into the display slot, anchoring dwell."""
        key, item = self._queue.popitem(last=False)
        self._current = item
        self._current_started_at = now

    # -- state construction ------------------------------------------------

    def _plus_n_more(self) -> int:
        """Pending UNSEEN items plus overflow drops → the ``+N more`` count."""
        return len(self._queue) + self._overflow_dropped

    def _empty_state(self) -> DisplayState:
        """The pre-activity state: nothing shown, nothing queued."""
        return DisplayState(
            file_path=None,
            segments=[],
            visible_segments=[],
            scroll_offset=0,
            model=None,
            used_thinking=False,
            is_subagent=False,
            project_tag="",
            short_session="",
            op=None,
            plus_n_more=self._plus_n_more(),
            is_idle=True,
            ts=None,
            event_key=None,
        )

    def _build_state(
        self,
        segments: List[DiffSegment],
        scroll_offset: int,
        viewport_height: int,
    ) -> DisplayState:
        """Assemble the DisplayState for the current item and scroll window."""
        assert self._current is not None
        evt = self._current.event
        window = max(1, viewport_height)
        visible = segments[scroll_offset : scroll_offset + window]
        return DisplayState(
            file_path=self._current.file_path,
            segments=segments,
            visible_segments=visible,
            scroll_offset=scroll_offset,
            model=evt.model,
            used_thinking=evt.used_thinking,
            is_subagent=evt.is_subagent,
            project_tag=evt.project_tag,
            short_session=evt.session_id[:_SHORT_SESSION_LEN],
            op=evt.op,
            plus_n_more=self._plus_n_more(),
            is_idle=not self._queue,
            ts=evt.ts,
            event_key=(self._current.file_path, evt.ts),
        )
