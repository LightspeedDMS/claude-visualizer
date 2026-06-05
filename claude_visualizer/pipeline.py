"""Async orchestration: discovery → tail → parse → bounded event queue.

The :class:`Pipeline` runs the read-only monitoring engine inside a single
asyncio loop.  It is the *producer* half of the application:

    discover() + active_set()  →  one TailState per active file
    awatch(projects_root)      →  change notification (liveness)
    bounded poll               →  read_new() each active file
    EventExtractor.extract()   →  Event objects (+ requestId→thinking enrich)
    asyncio.Queue (bounded)    →  handed to the consumer (the UI)

Routing of events into panel models is a *separate* pure function
(:func:`route_event`) so the same dispatch logic is shared by the Textual UI
and by the tests, with zero IO on the routing path.

Design constraints honoured:
- **Anti-fallback / anti-silent-failure**: the discovery and watch loops only
  swallow the *documented* benign races already absorbed by ``discover`` /
  ``tailer.read_new`` (vanished files).  ``asyncio.CancelledError`` is
  re-raised so shutdown is never masked.
- **Bounded everything**: the queue is bounded (back-pressure, never unbounded
  memory growth); ``awatch`` is given a bounded ``rust_timeout`` so the poll
  cadence is honoured even when the filesystem emits no change events; the
  number of tailed files is capped by ``active_set``.
- **No restart needed for new sessions** (AC7): every discovery tick rebuilds
  the active set, so a transcript that appears mid-run is picked up and tailed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Dict, Optional

from watchfiles import awatch

from claude_visualizer.config import AppConfig
from claude_visualizer.discovery import active_set, discover
from claude_visualizer.events import CommandEvent, Event, FileModifiedEvent
from claude_visualizer.models.command_feed import CommandFeedModel
from claude_visualizer.models.diff_queue import DiffQueueModel
from claude_visualizer.models.mru import MruModel
from claude_visualizer.parser import EventExtractor
from claude_visualizer.tailer import TailState, read_new

# Upper bound on queued events awaiting consumption.  Generous enough that a
# burst of activity is buffered, small enough that a stalled consumer applies
# back-pressure instead of growing memory without limit.
_QUEUE_MAXSIZE = 10_000


def route_event(
    event: Event,
    mru_model: MruModel,
    diff_queue: Optional[DiffQueueModel] = None,
    command_feed: Optional[CommandFeedModel] = None,
) -> None:
    """Dispatch one parsed event into the appropriate panel model(s).

    Pure and synchronous (no IO, no awaiting) so it is safe to call on the
    UI's refresh path.  Routing is by event type:

    - A ``FileModifiedEvent`` is recorded into the MRU files panel model AND,
      when supplied, into the Diff panel's display queue (``diff_queue``) — the
      queue coalesces by file and owns its own timing, so recording here is
      position/coalesce-aware (story #3).
    - A ``CommandEvent`` is recorded into the bottom Commands feed model
      (``command_feed``) when supplied — appended newest-on-top with NO dedup
      (story #4).  ``CommandEvent``s never touch the MRU/diff panels (those are
      a files view); ``FileModifiedEvent``s never touch the command feed.

    ``diff_queue`` and ``command_feed`` are both optional so narrower callers
    (and the existing tests) need not construct models they don't exercise.
    """
    if isinstance(event, FileModifiedEvent):
        mru_model.record(event)
        if diff_queue is not None:
            diff_queue.record(event)
    elif isinstance(event, CommandEvent):
        if command_feed is not None:
            command_feed.record(event)


class Pipeline:
    """Live producer of transcript events for the UI to consume.

    Lifecycle: :meth:`start` spins up the discovery and watch background
    tasks; :meth:`get_event` yields parsed events in arrival order;
    :meth:`stop` cancels the tasks (idempotent).  All public coroutines are
    safe to call from the Textual event loop.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._queue: "asyncio.Queue[Event]" = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        # One long-lived parser for the whole stream: extended-thinking blocks
        # arrive in an entry that PRECEDES the tool_use entry sharing the same
        # requestId, so correlation is stateful across lines and must persist
        # between reads (its requestId map is bounded — see EventExtractor).
        self._extractor = EventExtractor(config)
        # Active tailers, keyed by absolute path.  Rebuilt each discovery tick.
        self._tails: Dict[str, TailState] = {}
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Launch the discovery + watch background tasks."""
        if self._tasks:
            return
        self._stop.clear()
        # Prime the active set immediately so a file already present at startup
        # is tailed before the first change notification arrives.
        self._refresh_active_set()
        self._tasks = [
            asyncio.create_task(self._discovery_loop(), name="cv-discovery"),
            asyncio.create_task(self._watch_loop(), name="cv-watch"),
        ]

    async def stop(self) -> None:
        """Cancel background tasks and drop tail state (idempotent)."""
        self._stop.set()
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tails.clear()

    def is_running(self) -> bool:
        """True while background tasks are live and not yet stopped."""
        return bool(self._tasks) and not self._stop.is_set()

    # -- consumer API ------------------------------------------------------

    async def get_event(self) -> Event:
        """Await and return the next event in arrival order."""
        return await self._queue.get()

    # -- internals ---------------------------------------------------------

    def _refresh_active_set(self) -> None:
        """Recompute the active file set and reconcile the tailer dict.

        New active files get a cold-start :class:`TailState`; files that have
        aged out of the active window are dropped (their handles are not held
        open between reads, so dropping the state is a clean close).
        """
        discovered = discover(self._config.projects_root)
        active = set(active_set(discovered, self._config))

        # Add tailers for newly active files (AC7: mid-run sessions).
        for path in active:
            if path not in self._tails:
                self._tails[path] = TailState(path=path)

        # Drop tailers for files no longer active to bound memory.
        for path in list(self._tails):
            if path not in active:
                del self._tails[path]

    def _drain_active_tailers(self) -> None:
        """Read newly-appended complete lines from every active tailer.

        Parses each complete line and enqueues the resulting events.  Uses a
        non-blocking ``put_nowait`` guarded by ``QueueFull`` so a momentarily
        saturated queue never deadlocks the read loop; the oldest unconsumed
        event is the natural casualty of a slow consumer, which for a live
        activity feed is the correct trade-off (freshness over completeness).
        """
        for state in list(self._tails.values()):
            for line in read_new(state, self._config):
                for event in self._extractor.extract(line, state.path):
                    self._enqueue(event)

    def _enqueue(self, event: Event) -> None:
        """Put an event on the queue, dropping the oldest if saturated."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()  # discard oldest to make room
            except asyncio.QueueEmpty:
                pass
            self._queue.put_nowait(event)

    async def _discovery_loop(self) -> None:
        """Periodically rebuild the active set until stopped."""
        interval = self._config.discovery_interval_seconds
        while not self._stop.is_set():
            self._refresh_active_set()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue  # interval elapsed → loop and rescan

    async def _watch_loop(self) -> None:
        """React to filesystem changes AND poll on a bounded cadence.

        ``awatch`` cannot watch a path that does not exist yet, and
        ``projects_root`` (default ``~/.claude/projects``) may legitimately be
        absent when the app launches on a fresh machine.  So the watcher is
        wrapped in an outer loop that first waits for the root to appear
        (draining tailers each poll so any files that show up are surfaced),
        then attaches ``awatch``.  If the watcher ever exits because the root
        was removed, control returns to the wait phase rather than crashing.
        """
        poll_ms = max(1, int(self._config.poll_interval_seconds * 1000))
        # Drain once up front so a file present at startup surfaces promptly.
        self._drain_active_tailers()
        while not self._stop.is_set():
            await self._wait_for_root()
            if self._stop.is_set():
                break
            await self._watch_existing_root(poll_ms)

    async def _wait_for_root(self) -> None:
        """Poll (bounded by poll_interval) until projects_root exists.

        Each tick also drains the active tailers so that, the moment the
        discovery loop registers files under a freshly-created root, their
        content surfaces without waiting for ``awatch`` to attach.
        """
        interval = self._config.poll_interval_seconds
        while not self._stop.is_set() and not Path(self._config.projects_root).exists():
            self._drain_active_tailers()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def _watch_existing_root(self, poll_ms: int) -> None:
        """Run ``awatch`` over an existing root until stop or root removal.

        ``yield_on_timeout=True`` makes ``awatch`` yield an empty change set
        every ``rust_timeout`` ms, giving poll-driven liveness on top of
        change-driven latency.  ``FileNotFoundError`` (root removed mid-watch)
        returns cleanly so the outer loop can wait for it to reappear.
        """
        try:
            async for _changes in awatch(
                self._config.projects_root,
                stop_event=self._stop,
                rust_timeout=poll_ms,
                yield_on_timeout=True,
                recursive=True,
                debounce=poll_ms,
                step=max(1, min(poll_ms, 50)),
            ):
                if self._stop.is_set():
                    break
                self._drain_active_tailers()
        except FileNotFoundError:
            # Root vanished mid-watch; outer loop re-waits for it to return.
            return
