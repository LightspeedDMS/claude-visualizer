"""The blocking full-screen Textual application — the complete 3-panel TUI.

Layout (3 regions, CSS grid):

    ┌────────────────────┬────────────────────┐
    │ MRU Files (active) │ Live Diff (#3)     │   top row
    ├────────────────────┴────────────────────┤
    │ Commands — live cross-session Bash feed  │   bottom row
    └──────────────────────────────────────────┘

All three regions are wired (MVP feature-complete):
- **Top-left — MRU Files** (story #2): a live, newest-first list of files
  modified across ALL sessions (including subagents).  The row whose diff is
  currently on screen is highlighted (F3 ↔ F4 sync, AC9).
- **Top-right — Live Diff** (story #3): a colour-mapped diff of the most
  recently modified file, headed by ``model · 🧠 · filename · origin``, driven
  by a FIFO display queue that coalesces by file, auto-scrolls within dwell
  bounds, rests on the latest when idle, and surfaces ``+N more`` on overflow.
- **Bottom — Commands** (story #4): a live, newest-on-top, NON-deduplicated,
  width-truncated, origin-tagged rolling feed of every ``Bash`` command across
  all sessions and subagents, capped so the oldest entries scroll off.

Runtime wiring:
- On mount the app builds an :class:`~claude_visualizer.models.mru.MruModel`, a
  :class:`~claude_visualizer.models.diff_queue.DiffQueueModel` (clock injected),
  a :class:`~claude_visualizer.models.command_feed.CommandFeedModel`, and a
  :class:`~claude_visualizer.pipeline.Pipeline`; starts the pipeline; and
  launches a Textual *worker* that consumes events and routes them with
  :func:`~claude_visualizer.pipeline.route_event` into ALL THREE panel models.
- A periodic :meth:`~textual.app.App.set_interval` callback ticks the diff queue
  (``DiffQueueModel.tick(now, panel_height)``), repaints the Diff panel from the
  returned :class:`DisplayState`, mirrors the displayed path into the MRU
  model's ``highlighted_path`` and repaints the MRU panel, and repaints the
  Commands feed.  The queue owns ALL diff timing; the UI just renders.
- Neither the consume loop nor the refresh tick touches the filesystem or the
  parser — the pipeline owns all IO; the render path is pure (no IO/parse).
- On unmount the refresh interval timer is STOPPED (so no deferred tick fires
  into a torn-down tree), the consume loop's ``_running`` flag is cleared, and
  the pipeline is stopped (background tasks cancelled, tail state dropped);
  Textual restores the terminal on exit.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.timer import Timer
from textual.worker import Worker

from claude_visualizer.cache import CacheDB
from claude_visualizer.config import AppConfig
from claude_visualizer.events import CommandEvent, FileModifiedEvent
from claude_visualizer.models.command_feed import CommandFeedModel
from claude_visualizer.models.diff_queue import DiffQueueModel
from claude_visualizer.models.mru import MruModel
from claude_visualizer.pipeline import Pipeline, route_event
from claude_visualizer.ui.panels import (
    CommandsPanel,
    DiffPanel,
    HorizontalSeparator,
    MruFilesPanel,
    SplitterHandle,
    diff_viewport_height,
)

# Fallback content width for the bottom Commands panel before the first layout
# measures it, so the very first repaint truncates rows to a sensible width.
_COMMANDS_DEFAULT_WIDTH = 80
_MRU_MIN_WIDTH = 10  # minimum MRU panel columns (prevents collapse)
_BOTTOM_MIN_HEIGHT = 3  # minimum bottom panel rows (title + 2 commands)
_SPLITTER_STEP = 2  # columns per ←/→ press
_SPLITTER_STEP_V = 1  # rows per ↑/↓ press


class VisualizerApp(App):
    """Blocking full-screen app: live MRU panel + live Diff panel."""

    CSS = """
    Screen {
        layout: vertical;
    }
    #top-row {
        layout: horizontal;
        height: 1fr;
        overflow: hidden hidden;
    }
    #mru-panel {
        width: 40;
        height: 100%;
        padding: 0 1;
    }
    #top-right {
        width: 1fr;
        height: 100%;
        padding: 0 1;
    }
    #bottom {
        height: 8;
        padding: 0 1;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("p", "pin_current", "Pin current diff"),
    ]

    def __init__(
        self,
        config: AppConfig,
        *,
        now: Optional[Callable[[], float]] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._config = config
        # Injected clock makes the diff queue's timing deterministic under test;
        # production uses a real monotonic clock so dwell/scroll advance live.
        self._now: Callable[[], float] = now if now is not None else time.monotonic
        self._model = MruModel(config)
        self._diff_queue = DiffQueueModel(config, now=self._now)
        self._command_feed = CommandFeedModel(config)
        self._pipeline = Pipeline(config)
        # Stored so the worker can be observed in teardown.
        self._consumer: Optional[Worker[None]] = None
        # The periodic refresh interval's Timer handle, captured from
        # ``set_interval`` so ``on_unmount`` can STOP it — otherwise a deferred
        # tick can fire after the panels are removed and raise ``NoMatches``
        # (the story #3 teardown bug).  ``None`` until mounted.
        self._refresh_timer: Optional[Timer] = None
        # Lifecycle gate for the consume loop: set True on mount, cleared on
        # unmount so the loop has an explicit termination condition (MESSI #14).
        self._running = False
        # SQLite persistence cache — opened on mount, closed on unmount.
        # None when cache_path is None (disabled) or if open fails.
        self._cache: Optional[CacheDB] = None
        # Outer-box CSS width for the MRU panel.  size.width returns content
        # width (outer − padding = 40 − 2 = 38), so reading it back in the
        # grow/shrink actions would compute 38+2=40 — a no-op.  Tracking the
        # outer value sidesteps that padding-offset mismatch entirely.
        self._mru_width = 40

    def compose(self) -> ComposeResult:
        """Build the 3-region layout: a top row (2 cells) over a bottom cell."""
        with Horizontal(id="top-row"):
            yield MruFilesPanel(id="mru-panel")
            yield SplitterHandle()
            yield DiffPanel(id="top-right")
        yield HorizontalSeparator()
        yield CommandsPanel(id="bottom")

    async def on_mount(self) -> None:
        """Start the pipeline, the event consumer, and the diff refresh tick."""
        # Render all three panels once so they show their waiting state.
        self._mru_panel().update_from_model(self._model)
        self._diff_panel().update_from_state(None)
        self._commands_panel().update_from_model(
            self._command_feed, self._commands_width()
        )
        # Open cache and replay persisted events so the app resumes its
        # last-seen state on restart.  A corrupt or missing DB is logged and
        # the app continues without persistence — never prevents startup.
        if self._config.cache_path is not None:
            try:
                self._cache = CacheDB(self._config.cache_path)
                for evt in self._cache.load_file_events():
                    route_event(evt, self._model, self._diff_queue)
                for cmd_evt in self._cache.load_command_events():
                    route_event(
                        cmd_evt,
                        self._model,
                        self._diff_queue,
                        command_feed=self._command_feed,
                    )
                self._diff_queue.fast_forward_to_latest(self._now())
                state = self._diff_queue.tick(self._now(), self._diff_viewport_height())
                self._diff_panel().update_from_state(state)
                self._mru_panel().update_from_model(self._model)
                self._commands_panel().update_from_model(
                    self._command_feed, self._commands_width()
                )
            except Exception as exc:
                if self._cache is not None:
                    self._cache.close()
                self._cache = None
                self.log.warning(
                    f"Cache unavailable, running without persistence: {exc}"
                )
        self._running = True
        await self._pipeline.start()
        self._consumer = self.run_worker(
            self._consume_events(), name="cv-consumer", group="pipeline"
        )
        # Drive the diff queue on a bounded cadence: it owns dwell/scroll, so the
        # UI just ticks and repaints.  This advances files and scrolls a tall
        # diff in real time (AC6), keeps the idle rest-on-latest (AC7), and
        # repaints the live Commands feed (story #4 AC5).  The handle is stored
        # so ``on_unmount`` can stop it (no deferred tick after teardown).
        self._refresh_timer = self.set_interval(
            self._config.diff_refresh_seconds, self._refresh_panels
        )

    async def on_unmount(self) -> None:
        """Stop the refresh timer, the consumer, and the pipeline on exit.

        Stopping the interval timer FIRST is what fixes the story #3 teardown
        bug: once stopped, no deferred tick can fire into a tree whose panels
        have already been removed (which previously raised ``NoMatches``).
        """
        if self._refresh_timer is not None:
            self._refresh_timer.stop()
        if self._cache is not None:
            self._cache.close()
            self._cache = None
        self._running = False
        await self._pipeline.stop()

    def _mru_panel(self) -> MruFilesPanel:
        return self.query_one("#mru-panel", MruFilesPanel)

    def _diff_panel(self) -> DiffPanel:
        return self.query_one("#top-right", DiffPanel)

    def _commands_panel(self) -> CommandsPanel:
        return self.query_one("#bottom", CommandsPanel)

    def _commands_width(self) -> int:
        """Character width available for a command row in the bottom panel.

        Textual's ``content_size`` is the box's CONTENT region — it already
        excludes the border and padding — so it is exactly the width a command
        row may occupy, and :func:`format_command_row` truncates to it (AC3).
        Before the first layout that width is 0 (unmeasured), so we fall back to
        :data:`_COMMANDS_DEFAULT_WIDTH` to keep the very first repaint sensible;
        floored at 1 so a row is never asked to fit 0 columns.
        """
        width = self._commands_panel().content_size.width
        if width <= 0:
            return _COMMANDS_DEFAULT_WIDTH
        return max(1, width)

    def _diff_viewport_height(self) -> int:
        """Rows available for the diff body inside the top-right panel.

        Delegates to the pure :func:`~claude_visualizer.ui.panels.diff_viewport_height`
        so the app and the unit tests share one source of truth for the scroll-
        window sizing (panel height minus chrome, floored, default pre-layout).
        """
        return diff_viewport_height(self._diff_panel().size.height)

    def _refresh_panels(self) -> None:
        """Tick the diff queue and repaint all three panels (the periodic tick).

        Pure render path (no IO): ``tick`` returns the current
        :class:`DisplayState`; the Diff panel renders it, the MRU model's
        ``highlighted_path`` is set to the displayed file so its row lights up
        (AC9) and the MRU panel is repainted to follow it, and the bottom
        Commands panel is repainted from the command-feed model so the feed
        updates live (story #4 AC5).

        DEFENSIVE GUARD: the whole body is wrapped in ``try/except NoMatches`` so
        a tick that is dispatched AFTER the app has unmounted (panels removed)
        is a no-op rather than an exception.  ``on_unmount`` already stops the
        timer; this is the belt-and-suspenders that makes the story #3 deferred-
        tick ``NoMatches`` structurally impossible.
        """
        try:
            state = self._diff_queue.tick(self._now(), self._diff_viewport_height())
            self._diff_panel().update_from_state(state)
            # Mirror the displayed file into the MRU highlight (None when
            # idle/empty clears it); repaint so the highlight follows the queue.
            self._model.highlighted_path = state.file_path if state else None
            self._mru_panel().update_from_model(self._model)
            # Repaint the live Commands feed at the panel's current width (AC5).
            self._commands_panel().update_from_model(
                self._command_feed, self._commands_width()
            )
        except NoMatches:
            # Panels are gone (post-unmount deferred tick) → nothing to repaint.
            return

    def on_key(self, event) -> None:
        """Handle arrow keys for splitter resize before any child can consume them."""
        if event.key == "left":
            self.action_shrink_mru()
            event.stop()
        elif event.key == "right":
            self.action_grow_mru()
            event.stop()
        elif event.key == "up":
            self.action_grow_bottom()
            event.stop()
        elif event.key == "down":
            self.action_shrink_bottom()
            event.stop()

    # --- Arrow-key splitter resize actions -----------------------------------

    def action_shrink_mru(self) -> None:
        """Move vertical splitter left: MRU panel loses 2 columns."""
        self._mru_width = max(_MRU_MIN_WIDTH, self._mru_width - _SPLITTER_STEP)
        self.query_one("#mru-panel").styles.width = self._mru_width

    def action_grow_mru(self) -> None:
        """Move vertical splitter right: MRU panel gains 2 columns."""
        self._mru_width += _SPLITTER_STEP
        self.query_one("#mru-panel").styles.width = self._mru_width

    def action_grow_bottom(self) -> None:
        """Move horizontal splitter up: bottom Commands panel gains 1 row."""
        bottom = self.query_one("#bottom")
        bottom.styles.height = bottom.size.height + _SPLITTER_STEP_V

    def action_shrink_bottom(self) -> None:
        """Move horizontal splitter down: bottom Commands panel loses 1 row."""
        bottom = self.query_one("#bottom")
        bottom.styles.height = max(
            _BOTTOM_MIN_HEIGHT, bottom.size.height - _SPLITTER_STEP_V
        )

    def action_pin_current(self) -> None:
        """Pin the diff currently displayed in the Diff panel (keyboard shortcut `p`).

        Keyboard fallback for mouse click: whatever file is currently highlighted
        in the MRU list (i.e. shown in the Diff panel) is pinned for at least
        min_pin_seconds so the user can read it without it auto-advancing.
        """
        path = self._model.highlighted_path
        if path and self._diff_queue.pin(path, self._now()):
            try:
                state = self._diff_queue.tick(self._now(), self._diff_viewport_height())
                self._diff_panel().update_from_state(state)
                self._mru_panel().update_from_model(self._model)
            except Exception:
                pass  # panel gone (unmount race) — ignore

    def on_diff_panel_diff_scrolled(self, message: DiffPanel.DiffScrolled) -> None:
        """Scroll the pinned diff by one line per wheel tick.

        Delegates to ``DiffQueueModel.scroll_pin_by()``, which is a no-op when
        the diff is not pinned so non-pinned auto-scroll is unaffected.  After
        adjusting the scroll offset a full tick is run so the visible window
        updates immediately without waiting for the next periodic refresh.
        """
        self._diff_queue.scroll_pin_by(message.delta, self._diff_viewport_height())
        try:
            state = self._diff_queue.tick(self._now(), self._diff_viewport_height())
            self._diff_panel().update_from_state(state)
            self._model.highlighted_path = state.file_path if state else None
            self._mru_panel().update_from_model(self._model)
        except NoMatches:
            pass  # panels gone (unmount race) — ignore

    def on_mru_files_panel_file_clicked(
        self, message: MruFilesPanel.FileClicked
    ) -> None:
        """Pin the clicked file in the diff panel for at least min_pin_seconds."""
        pinned = self._diff_queue.pin(message.file_path, self._now())
        if pinned:
            try:
                state = self._diff_queue.tick(self._now(), self._diff_viewport_height())
                self._diff_panel().update_from_state(state)
                self._model.highlighted_path = message.file_path
                self._mru_panel().update_from_model(self._model)
            except Exception:
                pass  # panel gone (unmount race) — ignore

    async def _consume_events(self) -> None:
        """Drain pipeline events into the THREE panel models and refresh.

        Pure consumer: route the event (synchronous, no IO) into the MRU model,
        the diff queue, AND the command feed, then nudge an immediate repaint of
        the MRU and Commands panels so a newly observed file/command surfaces
        promptly (AC5).  The Diff panel is repainted by the periodic tick (the
        queue owns timing).  Bounded by ``_running``, which is cleared on
        unmount, so the loop has an explicit termination condition (MESSI #14).
        """
        mru_panel = self._mru_panel()
        commands_panel = self._commands_panel()
        while self._running:
            event = await self._pipeline.get_event()
            route_event(
                event,
                self._model,
                self._diff_queue,
                command_feed=self._command_feed,
            )
            mru_panel.update_from_model(self._model)
            commands_panel.update_from_model(self._command_feed, self._commands_width())
            if self._cache is not None:
                try:
                    if isinstance(event, FileModifiedEvent):
                        self._cache.record_file_event(
                            event, self._config.cache_max_file_events
                        )
                    elif isinstance(event, CommandEvent):
                        self._cache.record_command_event(
                            event, self._config.cache_max_command_events
                        )
                except Exception as exc:
                    self.log.warning(f"Cache write failed: {exc}")
